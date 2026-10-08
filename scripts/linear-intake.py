#!/usr/bin/env python3
"""Pull newly assigned Linear issues onto a Hermes kanban board.

Run by `hermes cron` with --no-agent, so it costs no inference tokens. Each tick:
  1. read the Linear personal API key from 1Password (`op read`),
  2. list every open issue assigned to me (assignee isMe, state not completed/canceled),
  3. diff against the `seen` set in the state file — an identifier seen once is never
     re-created by this script, which is what "newly assigned" means here,
  4. for each new one whose team key maps to a board, skip if a live card for that issue is
     already on the board (title prefix or idempotency-key prefix), else create it for the configured implementer;
     issues with child issues or no description go to the configured orchestrator instead,
  5. record it as seen; print only what was created (cron delivers stdout verbatim).

Cards skip Triage because `kanban.auto_decompose` is off. Implementation-ready cards land in
the implementer's worktree directly; the orchestrator is reserved for underspecified or multi-part work.

First run seeds `seen` with everything currently assigned and creates nothing.
To make the script pick an issue up again, delete its identifier from
~/.hermes/kanban/linear-intake-state.json.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RUN_START = int(time.time()) - 5

from deployment_settings import get, hermes_environment

_CONFIG_HOME = get("linear-intake", "home")
HOME = Path(os.environ.get("HERMES_HOME") or _CONFIG_HOME).expanduser()
OP_REF = get("linear-intake", "op_ref")
TEAM_TO_BOARD = get("linear-intake", "team_to_board")
CONTRACT_BY_BOARD = get("linear-intake", "contract_by_board")
IMPLEMENTER = get("linear-intake", "implementer")
ORCHESTRATOR = get("linear-intake", "orchestrator")
IMPLEMENTER_LABEL = get("linear-intake", "implementer_label")
ORCHESTRATOR_LABEL = get("linear-intake", "orchestrator_label")
REVIEWER = get("linear-intake", "reviewer")
REVIEW_PATH = get("linear-intake", "review_path")
NOTIFY_PLATFORM = get("linear-intake", "notify_platform")
NOTIFY_CHAT_ID = get("linear-intake", "notify_chat_id")
NOTIFY_CHAT_TYPE = get("linear-intake", "notify_chat_type")
NOTIFIER_PROFILE = get("linear-intake", "notifier_profile")
DELIVERY_MODE = get("linear-intake", "delivery_mode")
NET_FAILS_BEFORE_ALERT = get("linear-intake", "net_fails_before_alert")
NOTIFIER = get("linear-intake", "notifier")
STATE = HOME / "kanban" / "linear-intake-state.json"

QUERY = """
query($cursor: String) {
  issues(
    first: 100, after: $cursor,
    filter: {
      assignee: { isMe: { eq: true } },
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier title description url
      team { key }
      parent { identifier }
      children { nodes { identifier } }
    }
  }
}
"""


def linear_key() -> str:
    """LINEAR_API_KEY from the environment (~/.hermes/.env is loaded by hermes cron), else 1Password."""
    if key := os.environ.get("LINEAR_API_KEY", "").strip():
        return key
    r = subprocess.run(["op", "read", OP_REF], capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"LINEAR_API_KEY unset and op read failed: {r.stderr.strip()[-200:]}")
    return r.stdout.strip()


def linear(key: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.linear.app/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("errors"):
        raise RuntimeError(f"linear: {data['errors'][0].get('message')}")
    return data["data"]["issues"]


def fetch_open_assigned(key: str) -> list[dict]:
    out, cursor = [], None
    while True:
        page = linear(key, {"cursor": cursor})
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


def assignee_for(issue: dict) -> str:
    """Use the orchestrator only when the issue itself signals missing decomposition work."""
    has_description = bool((issue.get("description") or "").strip())
    has_children = bool(issue.get("children", {}).get("nodes"))
    return IMPLEMENTER if has_description and not has_children else ORCHESTRATOR


def branch_taken(board: str, branch: str) -> bool:
    db = HOME / "kanban" / "boards" / board / "kanban.db"
    if db.exists():
        conn = sqlite3.connect(str(db), timeout=10)
        try:
            if conn.execute("SELECT 1 FROM tasks WHERE branch_name = ? LIMIT 1", (branch,)).fetchone():
                return True
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    try:
        board_data = json.loads((HOME / "kanban" / "boards" / board / "board.json").read_text())
        repo = Path(board_data.get("default_workdir") or "").expanduser()
        if repo.is_dir():
            result = subprocess.run(
                ["git", "-C", str(repo), "for-each-ref", "--count=1", "--format=%(refname)",
                 f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            return bool(result.stdout.strip())
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        pass
    return False


def branch_for(board: str, issue: dict) -> str:
    """Collision-safe Linear-shaped branch for a direct implementation card."""
    ident = issue["identifier"].lower()
    title = re.sub(r"[^a-z0-9]+", "-", issue["title"].lower()).strip("-")
    base = f"{ident}-{title}"[:60].rstrip("-")
    if not branch_taken(board, base):
        return base
    suffix = hashlib.sha256(issue["id"].encode()).hexdigest()[:8]
    candidate = f"{base[:51].rstrip('-')}-{suffix}"
    if branch_taken(board, candidate):
        raise RuntimeError(f"no unused deterministic branch available for {issue['identifier']}")
    return candidate


def build_body(issue: dict, assignee: str) -> str:
    """Build an execution card or an exception-intake card."""
    ident = issue["identifier"]
    action = "Implement" if assignee == IMPLEMENTER else "Plan and route"
    lines = [
        "## Goal",
        f"{action} [{ident}]({issue['url']}): {issue['title']}",
        "",
        "## Context",
        f"- Spec: read [{ident}]({issue['url']}) from its task-tracking platform using the appropriate"
        " available integration before starting. The linked task is the authoritative specification.",
    ]
    if issue.get("parent"):
        lines.append(f"- Parent issue: {issue['parent']['identifier']}")
    children = [child["identifier"] for child in issue.get("children", {}).get("nodes", [])]
    if children:
        lines.append(f"- Child issues: {', '.join(children)}")
    if assignee == IMPLEMENTER:
        criteria = [
            "- [ ] Implement the linked task as one focused PR and verify the affected behavior.",
            f'- [ ] The PR body carries "Closes {ident}".',
            f'- [ ] Open one draft PR and call `kanban_request_review(reviewer="{REVIEWER}")` with its URL.',
        ]
    else:
        reason = "it has child issues" if children else "its task description is empty"
        criteria = [
            f"- [ ] Resolve the orchestration exception: {reason}.",
            f"- [ ] Route one implementation card to {IMPLEMENTER_LABEL} when this is one PR; fan out only when separate PRs are necessary.",
        ]
    lines += [
        "",
        "## Acceptance criteria",
        *criteria,
    ]
    if assignee == IMPLEMENTER:
        lines += [
            "",
            "## Before you start",
            f"- Read `{REVIEW_PATH}`, then the repo's `AGENTS.md`.",
            f"- If the issue is not implementation-ready or cannot land as one focused PR, block with `needs_input` and ask for {ORCHESTRATOR_LABEL} routing instead of creating child cards.",
        ]
    return "\n".join(lines) + "\n"


def already_on_board(board: str, identifier: str) -> str | None:
    """Live card for this issue, by title prefix or idempotency-key prefix.
    Orchestrator-created cards use keys like ISSUE-140/slug, so exact idempotency is not enough."""
    db = HOME / "kanban" / "boards" / board / "kanban.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status != 'archived' AND "
            "(title LIKE ? OR title LIKE ? OR idempotency_key = ? OR idempotency_key LIKE ?) LIMIT 1",
            (identifier + ":%", identifier + " %", identifier, identifier + "/%"),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None  # idempotency key on create still prevents exact duplicates
    finally:
        conn.close()


def intake_root_on_board(board: str, identifier: str) -> str | None:
    """Exact root created by this intake, never a manual card or child."""
    db = HOME / "kanban" / "boards" / board / "kanban.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status != 'archived' AND "
            "created_by = 'linear-intake' AND idempotency_key = ? LIMIT 1",
            (identifier,),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def card_is_subscribed(board: str, task_id: str) -> bool:
    db = HOME / "kanban" / "boards" / board / "kanban.db"
    if not db.exists():
        return False
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT 1 FROM kanban_notify_subs WHERE task_id = ? AND platform = ? "
            "AND chat_id = ? LIMIT 1",
            (task_id, NOTIFY_PLATFORM, NOTIFY_CHAT_ID),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def create_card(board: str, issue: dict) -> tuple[str, bool]:
    """Returns (task_id, created). created=False when idempotency returned an existing card."""
    title = f"{issue['identifier']}: {issue['title']}"[:200]
    assignee = assignee_for(issue)
    cmd = ["hermes", "kanban", "--board", board, "create", "--assignee", assignee,
           "--idempotency-key", issue["identifier"], "--created-by", "linear-intake"]
    if assignee == IMPLEMENTER:
        cmd += ["--workspace", "worktree", "--branch", branch_for(board, issue),
                "--completion-contract", CONTRACT_BY_BOARD[board]]
    cmd += ["--body", build_body(issue, assignee), "--json", title]
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=60, check=False, env=hermes_environment(HOME),
    )
    if r.returncode != 0:
        raise RuntimeError(f"kanban create failed: {r.stderr.strip()[-300:]}")
    data = json.loads(r.stdout)
    return data.get("id") or "", int(data.get("created_at") or 0) >= RUN_START


def subscribe_card(board: str, task_id: str) -> None:
    """Use Hermes' native terminal-event delivery for blocked/completed cards."""
    r = subprocess.run(
        ["hermes", "kanban", "--board", board, "notify-subscribe", task_id,
         "--platform", NOTIFY_PLATFORM, "--chat-id", NOTIFY_CHAT_ID,
         "--chat-type", NOTIFY_CHAT_TYPE, "--notifier-profile", NOTIFIER_PROFILE,
         "--delivery-mode", DELIVERY_MODE],
        capture_output=True, text=True, timeout=30, check=False, env=hermes_environment(HOME),
    )
    if r.returncode != 0:
        raise RuntimeError(f"kanban subscribe failed: {r.stderr.strip()[-300:]}")


def notify(subtitle: str, body: str) -> None:
    if os.access(NOTIFIER, os.X_OK):
        subprocess.run([NOTIFIER, "-title", "Hermes Kanban", "-subtitle", subtitle, "-message", body,
                        "-group", "hermes-linear-intake", "-sound", "default"],
                       capture_output=True, timeout=5, check=False)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen: set[str] = set(state.get("seen", []))
    first_run = "seen" not in state

    key = linear_key()
    issues = fetch_open_assigned(key)

    report: list[str] = []
    for issue in issues:
        ident = issue["identifier"]
        board = TEAM_TO_BOARD.get(issue["team"]["key"])
        if not board:
            continue
        if root := intake_root_on_board(board, ident):
            if not card_is_subscribed(board, root):
                try:
                    subscribe_card(board, root)
                    report.append(f"{board}/{root} subscription restored for {ident}")
                except Exception as exc:
                    report.append(f"{ident}: {exc}")
                    continue
            seen.add(ident)
            continue
        if ident in seen:
            continue
        seen.add(ident)
        if first_run:
            continue
        if existing := already_on_board(board, ident):
            report.append(f"{board}/{existing} already covers {ident}; skipped")
            continue
        try:
            task_id, created = create_card(board, issue)
            subscribe_card(board, task_id)
        except Exception as exc:
            # Only retry next tick when no card covers the issue. If the card was created
            # but subscribing (or a late step) failed, discarding the seen mark re-creates
            # it on the next tick, and a janitor archiving the fresh copies turns that
            # into a create/archive fight when fresh cards are repeatedly archived. The
            # subscription-restore path above already re-subscribes existing roots.
            if not (intake_root_on_board(board, ident) or already_on_board(board, ident)):
                seen.discard(ident)
            report.append(f"{ident}: {exc}")
            continue
        if created:
            report.append(f"{board}/{task_id} <- {ident}: {issue['title']}  {issue['url']}")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({"seen": sorted(seen), "last_run": now}, indent=2))

    if first_run:
        print(f"linear-intake: seeded {len(seen)} assigned issues as seen, nothing backfilled")
    elif report:
        print("\n".join(report))
        notify(f"{len(report)} Linear intake event(s)", report[0][:200])
    return 0


def net_failure_is_quiet() -> bool:
    """Count a consecutive network failure in the state file; True while under the alert threshold.
    main() rewrites the state without the counter, so any successful run resets it."""
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    state["net_failures"] = state.get("net_failures", 0) + 1
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))
    return state["net_failures"] < NET_FAILS_BEFORE_ALERT


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        if net_failure_is_quiet():
            sys.exit(0)
        print(f"linear-intake error (network, repeated): {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"linear-intake error: {exc}", file=sys.stderr)
        sys.exit(1)
