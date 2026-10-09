"""Find the kanban card that owns a GitHub PR, and the profile that implemented it.

Shared by the sweeps that send a parked PR back to its card (pr-conflict-sweep,
trunk-queue-sweep). Read-only: callers make every board write through the hermes CLI.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

OPEN = ("todo", "ready", "running", "review", "triage", "blocked", "scheduled")
MOVING = ("todo", "ready", "running")
HUMAN_HELD = ("triage", "blocked", "scheduled")


def owning_card(db: Path, pr: dict) -> sqlite3.Row | None:
    """Same branch name, or a run whose `metadata.published_pr` is this PR. Open cards win."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        ids = {row["id"] for row in conn.execute(
            "SELECT id FROM tasks WHERE branch_name = ?", (pr["headRefName"],))}
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
            f"SELECT id, status, assignee, claim_lock, created_at FROM tasks WHERE id IN ({marks})",
            tuple(ids)))
    cards.sort(key=lambda card: (card["status"] in OPEN, card["created_at"] or 0), reverse=True)
    return cards[0]


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
