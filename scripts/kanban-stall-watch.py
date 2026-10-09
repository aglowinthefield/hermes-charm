#!/usr/bin/env python3
"""Alert when a Hermes kanban board has queued work but nothing it can start.

A board stalls when every `todo` card waits, directly or through other todo cards, on a card
that only a human can move: one in `review` (the operator's inbox), `triage`, `blocked` or
`scheduled`. The dispatcher sees zero `ready` cards and runs nothing, and the board looks quiet
rather than stuck. On 2026-10-07 one board held 76 todo cards behind such cards, 57 of them
behind human-held ones, while running nothing.

Each tick, per board under ~/.hermes/kanban/boards: when nothing is ready or running and todo
cards remain, report the held cards ranked by how many todo cards they hold, plus todo cards no
held card explains (a dependency cycle, or a promotion the dispatcher has not run yet). The
first stalled observation only arms the alert, because a card finishing between dispatcher ticks
also leaves zero ready briefly. An ongoing stall repeats once a day; recovery reports once.

Run by `hermes cron` with --no-agent, so it costs no inference tokens. Silent when there is
nothing new; cron delivers whatever reaches stdout. Read-only against the boards.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

HELD = ("triage", "blocked", "review", "scheduled")
OPEN = ("todo", "ready", "running") + HELD
PARKED = "ready, not dispatchable"  # a ready card whose assignee the dispatcher never spawns
GRACE_SECONDS = 10 * 60
REMIND_SECONDS = 24 * 60 * 60
TOP = 3


def read_board(db: Path) -> tuple[dict[str, dict], list[tuple[str, str]]]:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        marks = ",".join("?" * len(OPEN))
        tasks = {row["id"]: dict(row) for row in conn.execute(
            f"SELECT id, title, assignee, status FROM tasks WHERE status IN ({marks})", OPEN)}
        links = [(p, c) for p, c in conn.execute("SELECT parent_id, child_id FROM task_links")
                 if p in tasks and c in tasks]
    return tasks, links


def held_counts(tasks: dict[str, dict], links: list[tuple[str, str]]) -> tuple[list[tuple[int, str]], set[str]]:
    """Rank held cards by the todo cards they transitively hold; return everything they explain."""
    children = defaultdict(set)
    for parent, child in links:
        children[parent].add(child)
    explained: set[str] = set()
    ranked = []
    for task_id, task in tasks.items():
        if task["status"] not in HELD + (PARKED,):
            continue
        seen: set[str] = set()
        stack = list(children[task_id])
        while stack:
            node = stack.pop()
            if node not in seen:
                seen.add(node)
                stack.extend(children[node])
        held = {node for node in seen if tasks[node]["status"] == "todo"}
        explained |= held
        if held:
            ranked.append((len(held), task_id))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked, explained


def describe(board: str, tasks: dict[str, dict], links: list[tuple[str, str]], todo: int) -> str:
    ranked, explained = held_counts(tasks, links)
    lines = [f"Kanban board `{board}` is stalled: 0 ready/running, {todo} todo waiting."]
    if ranked:
        lines.append("Top blockers (cards only a human can move):")
    for count, task_id in ranked[:TOP]:
        task = tasks[task_id]
        title = (task["title"] or task_id).splitlines()[0][:80]
        lines.append(f"- {task_id} ({task['status']}, {task['assignee']}) {title}: holds {count} todo")
    unexplained = todo - len(explained)
    if unexplained:
        lines.append(f"- {unexplained} todo not held by any of those: check for a dependency cycle "
                     f"or a stalled dispatcher.")
    lines.append(f"Inspect: hermes kanban --board {board} list --status todo")
    return "\n".join(lines)


def dispatch_profiles(home: Path) -> frozenset[str] | None:
    """Assignees the dispatcher spawns (`kanban.dispatch_profiles`); None when unknown.

    A `ready` card on any other assignee (the operator's inbox lane) is never claimed, so it
    holds its children exactly like a `review` card. Counting it as movement hid a 58-todo stall.
    """
    try:
        import yaml
        config = yaml.safe_load((home / "config.yaml").read_text()) or {}
        profiles = (config.get("kanban") or {}).get("dispatch_profiles")
    except Exception:  # no yaml module or unreadable config: fall back to counting every ready card
        return None
    return frozenset(profiles) if isinstance(profiles, list) and profiles else None


def check_board(db: Path, state: dict, now: float, spawnable: frozenset[str] | None = None) -> str:
    board = db.parent.name
    tasks, links = read_board(db)
    if spawnable is not None:
        for task in tasks.values():
            if task["status"] == "ready" and task["assignee"] not in spawnable:
                task["status"] = PARKED
    statuses = [task["status"] for task in tasks.values()]
    moving = sum(status in ("ready", "running") for status in statuses)
    todo = statuses.count("todo")
    entry = state.get(board)
    if moving or not todo:
        state.pop(board, None)
        if entry and entry.get("alerted_at"):
            return f"Kanban board `{board}` is moving again: {moving} ready/running, {todo} todo."
        return ""
    if entry is None:
        state[board] = {"since": now}
        return ""
    alerted = entry.get("alerted_at")
    due = now - entry["since"] >= GRACE_SECONDS if alerted is None else now - alerted >= REMIND_SECONDS
    if not due:
        return ""
    entry["alerted_at"] = now
    return describe(board, tasks, links, todo)


def live_boards(boards: Path) -> list[Path]:
    """Board databases Hermes itself would list: valid slugs, not archived."""
    found = []
    for db in sorted(boards.glob("*/kanban.db")):
        if db.parent.name.startswith(("_", "-")):  # `_archived/` holds removed boards
            continue
        try:
            if json.loads((db.parent / "board.json").read_text()).get("archived"):
                continue
        except (OSError, ValueError, AttributeError):
            pass  # no or unreadable metadata: Hermes treats the board as live
        found.append(db)
    return found


def run(boards: Path, state_path: Path, now: float, spawnable: frozenset[str] | None = None) -> str:
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    report = []
    for db in live_boards(boards):
        try:
            message = check_board(db, state, now, spawnable)
        except sqlite3.Error as exc:  # one bad board must not silence the others
            message = f"Kanban board `{db.parent.name}`: stall watch could not read it: {exc}"
        if message:
            report.append(message)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(state_path)
    return "\n\n".join(report)


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    boards = Path(os.environ.get("KANBAN_BOARDS_DIR") or home / "kanban" / "boards")
    report = run(boards, home / "kanban" / "stall-watch.json", time.time(), dispatch_profiles(home))
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
