#!/usr/bin/env python3
"""Pull newly assigned Linear issues onto a Hermes kanban board.

Run by `hermes cron` with --no-agent, so it costs no inference tokens. Each tick:
  1. read the Linear personal API key from 1Password (`op read`),
  2. list every open issue assigned to me (assignee isMe, state not completed/canceled),
  3. diff against the `seen` set in the state file — an identifier seen once is never
     re-created by this script, which is what "newly assigned" means here,
  4. for each new one whose team key maps to a board, skip if a live card for that issue is
     already on the board or if an existing PR/repository branch already carries the issue
     identifier, else create it for the configured implementer;
     issues with child issues or no description go to the configured orchestrator instead,
     an open Linear "blocked by" issue becomes the card's kanban parent (the card waits in
     todo until the blocker's card is done), and an open blocker with no card yet holds it,
  5. record it as seen; remember externally-owned work and alert if that work later disappears;
     print only what was created or needs attention (cron delivers stdout verbatim).

An issue carrying any configured `hold_labels` label (for example `needs-triage`) is held:
no card, and it is NOT recorded as seen, so removing the label later is what dispatches it.
Each hold is reported once, the first tick it appears.

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
NET_FAILS_BEFORE_ALERT = get("linear-intake", "net_fails_before_alert")
NOTIFIER = get("linear-intake", "notifier")
HOLD_LABELS = {label.strip().lower() for label in get("linear-intake", "hold_labels")}
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
      labels { nodes { name } }
      parent { identifier }
      children { nodes { identifier } }
      inverseRelations { nodes { type issue { identifier state { type } } } }
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


def hold_label(issue: dict) -> str | None:
    """The first label that keeps this issue off the board, if any."""
    for node in (issue.get("labels") or {}).get("nodes") or []:
        if node["name"].strip().lower() in HOLD_LABELS:
            return node["name"]
    return None


def assignee_for(issue: dict) -> str:
    """Use the orchestrator only when the issue itself signals missing decomposition work."""
    has_description = bool((issue.get("description") or "").strip())
    has_children = bool(issue.get("children", {}).get("nodes"))
    return IMPLEMENTER if has_description and not has_children else ORCHESTRATOR


def board_repo(board: str) -> Path | None:
    try:
        board_data = json.loads(
            (HOME / "kanban" / "boards" / board / "board.json").read_text()
        )
        if not isinstance(board_data, dict):
            return None
        raw_repo = board_data.get("default_workdir")
        if not isinstance(raw_repo, str) or not raw_repo:
            return None
        repo = Path(raw_repo).expanduser()
        return repo if repo.is_dir() else None
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


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
    repo = board_repo(board)
    if repo:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo), "for-each-ref", "--count=1", "--format=%(refname)",
                 f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"],
                capture_output=True, text=True, timeout=15, check=False,
            )
            return bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    return False


def matching_pull_requests(board: str, identifier: str) -> tuple[list[dict], bool]:
    """Return matching PRs and whether the GitHub lookup completed reliably.

    A failed lookup returns no matches with ``False``: intake must not silently
    drop new work because GitHub is unavailable.
    """
    repo = CONTRACT_BY_BOARD.get(board)
    if not repo:
        return [], False
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", repo, "--state", "all", "--search", identifier,
             "--limit", "1000", "--json", "number,url,state,title,body,mergedAt"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return [], False
        candidates = json.loads(result.stdout or "[]")
        if not isinstance(candidates, list):
            return [], False
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, json.JSONDecodeError):
        return [], False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(identifier)}(?![A-Za-z0-9])", re.IGNORECASE)
    return [
        pr for pr in candidates
        if isinstance(pr, dict)
        and pattern.search(f"{pr.get('title') or ''}\n{pr.get('body') or ''}")
    ], True


def matching_repository_branches(board: str, identifier: str) -> tuple[list[str], bool]:
    repo = board_repo(board)
    if not repo:
        return [], False
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref", "--format=%(refname:short)",
             "refs/heads", "refs/remotes"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0:
            return [], False
    except (OSError, subprocess.SubprocessError):
        return [], False
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(identifier.lower())}(?![A-Za-z0-9])")
    return [
        branch for branch in result.stdout.splitlines()
        if pattern.search(branch.lower())
    ], True


def existing_external_work(board: str, issue: dict) -> tuple[str | None, bool]:
    """Describe already-started repository work and lookup reliability."""
    identifier = issue["identifier"]
    prs, prs_reliable = matching_pull_requests(board, identifier)
    merged = [pr for pr in prs if pr.get("mergedAt") or str(pr.get("state", "")).upper() == "MERGED"]
    if merged:
        numbers = ", ".join(f"#{pr.get('number')}" for pr in merged)
        return f"merged PR {numbers}", True
    open_prs = [pr for pr in prs if str(pr.get("state", "")).upper() == "OPEN"]
    if open_prs:
        numbers = ", ".join(f"#{pr.get('number')}" for pr in open_prs)
        return f"open PR {numbers}", True
    branches, branches_reliable = matching_repository_branches(board, identifier)
    if branches:
        return f"repository branch(es) {', '.join(branches[:3])}", True
    return None, prs_reliable and branches_reliable


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
            f'- [ ] Open one ready-for-review PR and call `kanban_request_review(reviewer="{REVIEWER}")` with its URL.',
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
            "- Write the PR body and card comments under `~/.hermes/teams/board-writing.md`.",
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


def open_blockers(issue: dict) -> list[str]:
    """Identifiers of unfinished issues that block this one (Linear's inverse `blocks` relations)."""
    return [
        node["issue"]["identifier"]
        for node in (issue.get("inverseRelations") or {}).get("nodes") or []
        if node.get("type") == "blocks"
        and node["issue"]["state"]["type"] not in ("completed", "canceled")
    ]


def blocker_cards(board: str, issue: dict) -> tuple[list[str], str | None]:
    """(parent card ids, first open blocker with no live card on this board, or None)."""
    parents = []
    for blocker in open_blockers(issue):
        card = intake_root_on_board(board, blocker) or already_on_board(board, blocker)
        if not card:
            return [], blocker
        parents.append(card)
    return parents, None


def create_card(board: str, issue: dict, parents: list[str] | tuple[str, ...] = ()) -> tuple[str, bool]:
    """Returns (task_id, created). created=False when idempotency returned an existing card.
    Parents gate the card in todo until every parent card is done."""
    title = f"{issue['identifier']}: {issue['title']}"[:200]
    assignee = assignee_for(issue)
    cmd = ["hermes", "kanban", "--board", board, "create", "--assignee", assignee,
           "--idempotency-key", issue["identifier"], "--created-by", "linear-intake"]
    for parent in parents:
        cmd += ["--parent", parent]
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


def notify(subtitle: str, body: str) -> None:
    if os.access(NOTIFIER, os.X_OK):
        subprocess.run([NOTIFIER, "-title", "Hermes Kanban", "-subtitle", subtitle, "-message", body,
                        "-group", "hermes-linear-intake", "-sound", "default"],
                       capture_output=True, timeout=5, check=False)


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen: set[str] = set(state.get("seen", []))
    previously_held: dict[str, str] = state.get("held", {})
    raw_external_work = state.get("external_work", {})
    external_work: dict[str, str] = {
        ident: reason for ident, reason in raw_external_work.items()
        if isinstance(ident, str) and isinstance(reason, str)
    } if isinstance(raw_external_work, dict) else {}
    held: dict[str, str] = {}
    first_run = "seen" not in state

    key = linear_key()
    issues = fetch_open_assigned(key)

    report: list[str] = []
    pending: list[tuple[str, dict]] = []
    for issue in issues:
        ident = issue["identifier"]
        board = TEAM_TO_BOARD.get(issue["team"]["key"])
        if not board:
            continue
        if intake_root_on_board(board, ident):
            seen.add(ident)
            external_work.pop(ident, None)
            continue
        if ident in external_work:
            previous_external = external_work[ident]
            current_external, reliable = existing_external_work(board, issue)
            if current_external:
                external_work[ident] = current_external
            elif reliable:
                del external_work[ident]
                report.append(
                    f"{ident} external work disappeared ({previous_external}); "
                    "remove it from linear-intake-state.json to requeue"
                )
            if ident in seen:
                continue
        if ident in seen:
            continue
        if label := hold_label(issue):
            # Not marked seen: dropping the label is what releases it to the board.
            held[ident] = label
            if not first_run and previously_held.get(ident) != label:
                report.append(f"{ident} held by label {label!r}; no card until it is removed")
            continue
        if first_run:
            seen.add(ident)
            continue
        if existing := already_on_board(board, ident):
            seen.add(ident)
            report.append(f"{board}/{existing} already covers {ident}; skipped")
            continue
        external, _ = existing_external_work(board, issue)
        if external:
            seen.add(ident)
            external_work[ident] = external
            report.append(f"{ident} already has external work ({external}); skipped")
            continue
        pending.append((board, issue))

    def intake(board: str, issue: dict, parents: list[str]) -> None:
        ident = issue["identifier"]
        seen.add(ident)
        try:
            task_id, created = create_card(board, issue, parents)
        except Exception as exc:
            # Only retry next tick when no card covers the issue. If the card was created
            # but a late step failed, discarding the seen mark re-creates it on the next
            # tick, and a janitor archiving the fresh copies turns that into a
            # create/archive fight when fresh cards are repeatedly archived.
            if not (intake_root_on_board(board, ident) or already_on_board(board, ident)):
                seen.discard(ident)
            report.append(f"{ident}: {exc}")
            return
        if created:
            report.append(f"{board}/{task_id} <- {ident}: {issue['title']}  {issue['url']}")

    # An open Linear blocker becomes a kanban parent, so the card waits in todo until the
    # blocker's card is done. Repeat passes so a chain assigned in one tick links in that tick.
    waiting: list[tuple[str, dict, str]] = []
    progress = True
    while pending and progress:
        progress, waiting = False, []
        for board, issue in pending:
            parents, missing = blocker_cards(board, issue)
            if missing:
                waiting.append((board, issue, missing))
                continue
            progress = True
            intake(board, issue, parents)
        pending = [(board, issue) for board, issue, _ in waiting]
    for _board, issue, missing in waiting:
        # Not marked seen: the blocker's card appearing is what releases it to the board.
        ident, reason = issue["identifier"], f"blocker:{missing}"
        held[ident] = reason
        if previously_held.get(ident) != reason:
            report.append(f"{ident} held by open blocker {missing}; no card until the blocker has one")

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {
            "seen": sorted(seen),
            "held": dict(sorted(held.items())),
            "external_work": dict(sorted(external_work.items())),
            "last_run": now,
        },
        indent=2,
    ))

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
