#!/usr/bin/env python3
"""kanban_task_blocked hook: audit block-reason claims against the worker log.

Shadow mode (2026-10-04): comments on the card, never changes state.

Why: a scribe worker blocked t_f3ac65e9 claiming "all 6 commits are on
origin/main via #2603 ..." without ever running git cherry or git diff. The
claim was fabricated and the card left the pipeline on a false rationale.
Blocked cards bypass the review lane, so this hook is the only check.

How: reads the latest 'blocked' event reason for the task, extracts
backtick-quoted commands (git/gh/npm/npx/node/doppler/sqlite3), and greps the
worker log (logs/<task_id>.log beside the board DB) for each. Every claimed
command that never ran is reported in a card comment via the CLI.

stdin: hook JSON with task_id and board under extra (legacy top level accepted).
Always exits 0.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
COMMAND_RE = re.compile(r"`((?:git|gh|npm|npx|node|doppler|sqlite3)\s+[^`]+)`")
REPO_STATE_RE = re.compile(
    r"origin/main|\bon main\b|commits?|\bCI\b|checks?\s+(are\s+)?(green|pass)|"
    r"\bdiff\b|merged|CONFLICTING|superseded|already (on|in) main",
    re.IGNORECASE,
)
MAX_REASON_BYTES = 20000
MARKER = "claim-audit"


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return
    extra = payload.get("extra") or {}
    task_id = extra.get("task_id") or payload.get("task_id")
    board = extra.get("board") or payload.get("board") or os.environ.get("HERMES_KANBAN_BOARD")
    if not task_id or not board:
        return

    boards_dir = os.environ.get("KANBAN_BOARDS_DIR") or os.path.join(HERMES_HOME, "kanban", "boards")
    board_dir = os.path.join(boards_dir, board)
    db_path = os.path.join(board_dir, "kanban.db")
    log_path = os.path.join(board_dir, "logs", f"{task_id}.log")
    if not os.path.isfile(db_path):
        return

    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    row = db.execute(
        "select payload from task_events where task_id=? and kind='blocked'"
        " order by created_at desc limit 1",
        (task_id,),
    ).fetchone()
    db.close()
    if not row or not row[0]:
        return
    try:
        reason = json.loads(row[0]).get("reason", "")
    except json.JSONDecodeError:
        return
    reason = reason[:MAX_REASON_BYTES]

    claimed = sorted(set(COMMAND_RE.findall(reason)))
    if not claimed:
        # The worst fabrications assert repo state without naming any check.
        if REPO_STATE_RE.search(reason):
            body = (
                "claim-audit (shadow): this block reason asserts facts about repo/GitHub state "
                "but names no command that verified them (no backticked `git`/`gh`/... command). "
                "Treat it as unverified until someone runs a check, e.g. `git cherry origin/main <branch>` "
                "or `gh pr checks`. Hook: kanban-blocked-claim-audit (kanban_task_blocked, shadow mode; comments only)."
            )
            try:
                subprocess.run(
                    ["hermes", "kanban", "--board", board, "comment", task_id, body, "--author", MARKER],
                    timeout=60,
                    capture_output=True,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        return

    try:
        with open(log_path, "r", errors="replace") as fh:
            log_text = fh.read()
    except OSError:
        log_text = ""
    # Normalize whitespace so line-wrapped log lines still match.
    log_norm = re.sub(r"\s+", " ", log_text)

    never_ran = [c for c in claimed if re.sub(r"\s+", " ", c).strip() not in log_norm]
    if not never_ran:
        return

    lines = [
        f"claim-audit (shadow): this block reason cites command(s) that never appear in the worker log:",
    ]
    lines += [f"- `{c}`" for c in never_ran]
    lines.append(
        "Treat the claims they support as unverified until someone runs them. "
        "Hook: kanban-blocked-claim-audit (kanban_task_blocked, shadow mode; comments only)."
    )
    body = "\n".join(lines)
    try:
        subprocess.run(
            [
                "hermes", "kanban", "--board", board,
                "comment", task_id, body,
                "--author", MARKER,
            ],
            timeout=60,
            capture_output=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


if __name__ == "__main__":
    main()
