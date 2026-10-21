"""Find the kanban card that owns a GitHub PR, the profile that implemented it, and the PR a
card published; and sign Aide's comments on the PRs whose cards it moves.

Shared by the Aide jobs that act on a PR's card (pr-conflict-sweep, trunk-queue-sweep,
kanban-incident-watch). Read-only: callers make every write through the hermes and gh CLIs.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

OPEN = ("todo", "ready", "running", "review", "triage", "blocked", "scheduled")
MOVING = ("todo", "ready", "running")
HUMAN_HELD = ("triage", "blocked", "scheduled")
ADOPT_PREFIX = "adopt/pr-"


def adopt_branch(number: int) -> str:
    """The local branch an adoption card works on. It is not the PR's head name, because that
    branch may stay checked out wherever the operator left it, and git checks a branch out in
    one worktree at a time."""
    return f"{ADOPT_PREFIX}{number}"


def owning_card(db: Path, pr: dict) -> sqlite3.Row | None:
    """Same branch name (or the PR's adoption branch), or a run whose `metadata.published_pr`
    is this PR. Open cards win."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        ids = {row["id"] for row in conn.execute(
            "SELECT id FROM tasks WHERE branch_name IN (?, ?)",
            (pr["headRefName"], adopt_branch(pr["number"])))}
        for row in conn.execute("SELECT task_id, metadata FROM task_runs WHERE metadata LIKE ?",
                                (f"%{pr['url']}%",)):
            try:
                if json.loads(row["metadata"]).get("published_pr") == pr["url"]:
                    ids.add(row["task_id"])
            except (TypeError, ValueError, AttributeError):
                continue
        if not ids:
            return None
        marks = ",".join("?" * len(ids))
        cards = list(conn.execute(
            f"SELECT id, status, assignee, claim_lock, branch_name, created_at FROM tasks WHERE id IN ({marks})",
            tuple(ids)))
    cards.sort(key=lambda card: (card["status"] in OPEN, card["created_at"] or 0), reverse=True)
    return cards[0]


def owning_card_on(dbs: dict[str, Path], pr: dict) -> tuple[str, sqlite3.Row] | None:
    """`owning_card` across boards, as (board, card). Open cards win, then the newest."""
    found = [(board, card) for board, db in dbs.items()
             if db.exists() and (card := owning_card(db, pr)) is not None]
    if not found:
        return None
    return max(found, key=lambda pair: (pair[1]["status"] in OPEN, pair[1]["created_at"] or 0))


def implementer_of(db: Path, task_id: str, park_assignee: str) -> str | None:
    """Who built the card: the implementer on its latest handoff into AI review.

    `reopen-review` restores the implementer from the latest `review_requested` event, and when
    the AI reviewer has already parked the card on the human, that event names the reviewer.
    Skip handoffs to the human so the rework goes back to the profile that owns the branch."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'review_requested'"
                            " ORDER BY created_at DESC, id DESC", (task_id,)).fetchall()
    for (payload,) in rows:
        try:
            event = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("reviewer") != park_assignee and event.get("implementer"):
            return event["implementer"]
    return None


def published_pr(db: Path, task_id: str) -> str | None:
    """The PR URL the card's newest run published, if any."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        rows = conn.execute("SELECT metadata FROM task_runs WHERE task_id = ? AND metadata LIKE ?"
                            " ORDER BY id DESC", (task_id, "%published_pr%")).fetchall()
    for (metadata,) in rows:
        try:
            url = json.loads(metadata).get("published_pr")
        except (TypeError, ValueError, AttributeError):
            continue
        if url:
            return url
    return None


def pr_comment_argv(pr_url: str, task_id: str, body: str, sha: str = "") -> list[str]:
    """`gh pr comment` in the agent comment format the kanban workers use (silk-remix AGENTS.md):
    the machine-readable first line, the profile heading, and a footer naming the card."""
    footer = f"*Hermes kanban · {task_id}" + (f" · {sha[:7]}*" if sha else "*")
    text = f"<!-- hermes-agent: aide -->\n## 🧭 aide · supervisor\n\n{body}\n\n---\n{footer}"
    return ["gh", "pr", "comment", pr_url, "--body", text]
