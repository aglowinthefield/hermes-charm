#!/usr/bin/env python3
"""Surface dispatcher give-ups on the Hermes kanban boards.

When the consecutive-failure breaker trips, the dispatcher emits a `gave_up` event and flips
the card to `blocked` with `block_kind` NULL. That path never fires the `kanban_task_blocked`
shell hook, so the card looks like a needs-input block with no reason on it (t_fcd60cd0,
2026-09-15 20:01 and 2026-09-16 08:29). Run by `hermes cron` with --no-agent, so it costs no
inference tokens.

Each tick, for every board under ~/.hermes/kanban/boards: find blocked cards whose newest
`gave_up` event is newer than the last give-up comment this script left, then
  1. post one comment on the card naming the run, its outcome/error, and the resume command,
  2. mirror it to Linear through the existing agent-hooks/kanban-linear.py (stdin JSON),
  3. fire a macOS notification.
Silent when there is nothing new; cron delivers whatever reaches stdout.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
BOARDS = Path(os.environ.get("KANBAN_BOARDS_DIR") or HOME / "kanban" / "boards")
LINEAR_HOOK = HOME / "agent-hooks" / "kanban-linear.py"
MARK = "[dispatcher give-up]"
NOTIFIER = "/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier"


def notify(subtitle: str, body: str, group: str) -> None:
    if os.access(NOTIFIER, os.X_OK):
        subprocess.run([NOTIFIER, "-title", "Hermes Kanban", "-subtitle", subtitle, "-message", body,
                        "-group", group, "-sound", "default"], capture_output=True, timeout=5, check=False)
    elif shutil.which("osascript"):  # macOS fallback; no desktop to notify on Linux
        msg = body.replace('"', "'")
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Hermes Kanban" subtitle "{subtitle}"'],
                       capture_output=True, timeout=5, check=False)


def fmt_ts(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts))) if ts else "?"


def build_body(board: str, task: sqlite3.Row, gave_up: sqlite3.Row, run: sqlite3.Row | None) -> str:
    payload = {}
    try:
        payload = json.loads(gave_up["payload"] or "{}")
    except Exception:
        pass
    failures = payload.get("failures", task["consecutive_failures"])
    limit = payload.get("effective_limit", "?")
    phase = payload.get("retry_status") or "?"
    lines = [
        f"{MARK} The dispatcher stopped retrying this card at {fmt_ts(gave_up['created_at'])}: "
        f"{failures}/{limit} consecutive worker failures. Nobody asked for input; the card is "
        f"blocked because workers kept dying.",
    ]
    if run is not None:
        err = (run["error"] or task["last_failure_error"] or "").strip()
        lines.append(
            f"Last run #{run['id']} ({run['profile']}) {fmt_ts(run['started_at'])} → "
            f"{fmt_ts(run['ended_at'])} ended `{run['outcome']}`" + (f": {err[:300]}" if err else ".")
        )
        if run["summary"]:
            lines.append(f"Its last summary: {str(run['summary']).strip()[:400]}")
    lines.append(
        f"It was in the `{phase}` phase (assignee `{task['assignee']}`); unblocking returns it there. "
        f"Check the run log first: ~/.hermes/kanban/boards/{board}/logs/{task['id']}.log\n"
        f"Resume: hermes kanban --board {board} unblock {task['id']}"
    )
    return "\n".join(lines)


def check_board(db: Path, report: list[str]) -> None:
    board = db.parent.name
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tasks = conn.execute(
            "SELECT id, title, assignee, consecutive_failures, last_failure_error "
            "FROM tasks WHERE status = 'blocked' AND block_kind IS NULL"
        ).fetchall()
        for task in tasks:
            gave_up = conn.execute(
                "SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = 'gave_up' "
                "ORDER BY id DESC LIMIT 1", (task["id"],)
            ).fetchone()
            if gave_up is None:
                continue  # a real kanban_block without a kind; the blocked hook already covered it
            last_mark = conn.execute(
                "SELECT MAX(created_at) FROM task_comments WHERE task_id = ? AND body LIKE ?",
                (task["id"], MARK + "%"),
            ).fetchone()[0]
            if last_mark is not None and int(last_mark) >= int(gave_up["created_at"]):
                continue
            run = conn.execute(
                "SELECT id, profile, outcome, error, summary, started_at, ended_at FROM task_runs "
                "WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task["id"],)
            ).fetchone()
            body = build_body(board, task, gave_up, run)
            r = subprocess.run(
                ["hermes", "kanban", "--board", board, "comment", task["id"], "--author", "dispatcher", body],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if r.returncode != 0:
                report.append(f"{board}/{task['id']}: comment failed: {r.stderr.strip()[-300:]}")
                continue
            if LINEAR_HOOK.exists():
                subprocess.run(
                    [sys.executable, str(LINEAR_HOOK)],
                    input=json.dumps({"hook_event_name": "kanban_task_blocked",
                                      "extra": {"task_id": task["id"], "board": board,
                                                "assignee": task["assignee"], "reason": body}}),
                    capture_output=True, text=True, timeout=40, check=False,
                )
            first = (task["title"] or task["id"]).splitlines()[0][:80]
            notify(f"Gave up: {first}", body.splitlines()[0][:200], f"hermes-kanban-giveup-{task['id']}")
            report.append(f"{board}/{task['id']} {first}\n{body}")
    finally:
        conn.close()


def main() -> int:
    report: list[str] = []
    for db in sorted(BOARDS.glob("*/kanban.db")):
        if db.parent.name.startswith(("_", "-")):  # `_archived/` holds removed boards
            continue
        try:
            check_board(db, report)
        except Exception as exc:  # one bad board must not silence the others
            report.append(f"{db.parent.name}: watchdog error: {exc}")
    if report:
        print("\n\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
