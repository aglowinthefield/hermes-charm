#!/usr/bin/env python3
"""Report kanban workers that left their configured model chain, so a fallback is never silent.

Two ways a worker ends up somewhere the operator did not choose:

- A runtime fallback. Hermes logs `Fallback activated: <from> → <to> (<provider>)` or
  `Primary provider auth failed (...)` in the profile's agent.log, and carries on. Nothing else
  surfaces it: provider-drift runs in `log` mode.
- A card pin. A pin replaces the profile's primary, so the chain's head is never tried. On
  2026-10-02 a bulk `set-model` pinned most of coda to kimi-coding; on 2026-10-11 Kimi ran out of
  balance and those cards failed while Anthropic sat idle. Nothing pins cards on purpose since
  typesafe-model-router was disabled the same day, so every pin is reported.

Each tick reads every agent.log from a per-file byte cursor (a new file starts at its end, so
the first run does not replay history) and reports each pin once until it is cleared. Read-only.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

FALLBACK = re.compile(r"^(\S+ \S+) .*?(Fallback activated: .+|Primary provider auth failed \(.+?\))")
OPEN = ("triage", "todo", "ready", "running", "blocked", "review", "scheduled")


def agent_logs(home: Path) -> dict[str, Path]:
    logs = {"default": home / "logs" / "agent.log"}
    for path in sorted(home.glob("profiles/*/logs/agent.log")):
        logs[path.parents[1].name] = path
    return {name: path for name, path in logs.items() if path.is_file()}


def new_fallbacks(logs: dict[str, Path], cursors: dict[str, int]) -> list[str]:
    lines = []
    for profile, path in logs.items():
        stat = path.stat()
        inode, start = cursors.get(str(path), (stat.st_ino, stat.st_size))
        if inode != stat.st_ino or start > stat.st_size:  # rotated or truncated
            start = 0
        events: Counter[str] = Counter()
        last: dict[str, str] = {}
        with path.open("rb") as handle:
            handle.seek(start)
            for raw in handle:
                match = FALLBACK.match(raw.decode("utf-8", "replace"))
                if match:
                    events[match[2]] += 1
                    last[match[2]] = match[1][:16]
        cursors[str(path)] = (stat.st_ino, stat.st_size)
        for event, count in events.most_common():
            times = f" ×{count}" if count > 1 else ""
            lines.append(f"- {profile}: {event}{times} (last {last[event]})")
    return lines


def pinned_cards(boards: Path) -> dict[str, str]:
    """Open cards with a model or provider pin, keyed board/card → description."""
    pins = {}
    marks = ",".join("?" * len(OPEN))
    for db in sorted(boards.glob("*/kanban.db")):
        board = db.parent.name
        if board.startswith(("_", "-")):
            continue
        with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                f"SELECT id, assignee, status, provider_override, model_override FROM tasks "
                f"WHERE status IN ({marks}) AND (provider_override IS NOT NULL OR model_override IS NOT NULL)",
                OPEN).fetchall()
        for task, assignee, status, provider, model in rows:
            pins[f"{board}/{task}"] = f"{provider or '?'}/{model or '?'} ({status}, {assignee})"
    return pins


def run(home: Path, boards: Path, state_path: Path) -> str:
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    cursors = state.setdefault("cursors", {})
    report = []
    fallbacks = new_fallbacks(agent_logs(home), cursors)
    if fallbacks:
        report.append("Workers fell back off their primary model:\n" + "\n".join(fallbacks))
    pins = pinned_cards(boards)
    fresh = sorted(set(pins) - set(state.get("pins", [])))
    state["pins"] = sorted(pins)
    if fresh:
        lines = [f"- {key}: {pins[key]}" for key in fresh]
        lines.append("A pin skips the profile's chain. Clear one with "
                     "`hermes kanban --board <board> set-model <card> none`.")
        report.append("Cards pinned to a model:\n" + "\n".join(lines))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(state_path)
    return "\n\n".join(report)


def main() -> int:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    boards = Path(os.environ.get("KANBAN_BOARDS_DIR") or home / "kanban" / "boards")
    report = run(home, boards, home / "kanban" / "provider-fallback-watch.json")
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
