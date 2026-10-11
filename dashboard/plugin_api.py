"""Read-only backend for the dashboard's Aide tab, mounted at /api/plugins/charm/.

Everything comes from files Aide's jobs already write: the profile's cron jobs.json and per-run
output files, the incident watch's state file, and the kanban boards it points at."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from fastapi import APIRouter

router = APIRouter()

ROOT = Path.home() / ".hermes"
AIDE = ROOT / "profiles" / "aide"
ACTIVITY_LIMIT = 40
SILENT = '{"wakeAgent": false}'


def jobs():
    data = json.loads((AIDE / "cron" / "jobs.json").read_text())
    keep = ("id", "name", "schedule_display", "state", "deliver", "no_agent", "last_run_at",
            "last_status", "last_error", "next_run_at", "failure_streak")
    return [{k: job.get(k) for k in keep} for job in (data["jobs"] if isinstance(data, dict) else data)]


def card(board, task):
    db = ROOT / "kanban" / "boards" / board / "kanban.db"
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT title, status FROM tasks WHERE id = ?", (task,)).fetchone()
    return {"title": row[0], "status": row[1]} if row else {"title": None, "status": "missing"}


def incidents():
    path = ROOT / "kanban" / "incident-watch.json"
    state = json.loads(path.read_text()) if path.exists() else {}
    return [{"board": board, "card": inc["card"], **card(board, inc["card"]),
             "held": len(inc["held"]), "members": len(inc["members"]), "opened_at": inc["opened_at"],
             "sample": inc["samples"][0][:400] if inc["samples"] else ""}
            for board, entry in state.items() for inc in entry.get("incidents", {}).values()
            if inc.get("card")]


def activity(names):
    """The newest runs that said something. Each output file is a header, a `---` line, then
    whatever the script (and the agent, when it woke) printed."""
    runs = []
    for job_dir in (AIDE / "cron" / "output").iterdir():
        if job_dir.name not in names:
            continue
        for run in sorted(job_dir.glob("*.md"), reverse=True)[:ACTIVITY_LIMIT]:
            body = run.read_text(errors="replace").partition("\n---\n")[2].strip()
            if body and body != SILENT:
                runs.append({"job": names[job_dir.name], "at": run.stem, "body": body[:4000]})
    return sorted(runs, key=lambda r: r["at"], reverse=True)[:ACTIVITY_LIMIT]


@router.get("/aide")
def aide():
    listed = jobs()
    return {"jobs": listed, "incidents": incidents(),
            "activity": activity({job["id"]: job["name"] for job in listed})}
