#!/usr/bin/env python3
"""Hold kanban cards blocked by one systemic cause behind a single incident card.

When something outside the cards breaks (GitHub Actions billing, a dispatcher env var, an
approval rule), every card that reaches it re-diagnoses the same failure, blocks, is retried
and blocks again. On 2026-10-10 about 20 silk cards blocked one after another on "recent
account payments have failed", each with its own `gh api` investigation.

Each tick, per board under ~/.hermes/kanban/boards: every card in `blocked` whose newest
blocked, block_loop_detected or gave_up event is under WINDOW_SECONDS old and not yet judged
is put to Jev (TypeSafe System One) as narrow yes/no questions: is the cause systemic, and is
it the same cause as each open incident and each earlier systemic block. Then:

  1. Same cause as an open incident: hold the card behind it.
  2. Systemic and shared with MIN_CARDS - 1 earlier systemic blocks: create one incident card
     on the park assignee (which the dispatcher never spawns) and hold all of them.
  3. Otherwise remember a systemic block for later matches, and nothing else.

Holding is `kanban link <incident> <card>` then `kanban unblock`, which returns the card to
`todo` behind an open parent. Completing the incident card releases every held card to
`ready` at once; the next tick reports the release. Unlinking reverses a wrong hold.

A card is judged once per block event, so a quiet board costs no Jev calls. Jev or hermes
failures are reported and retried next tick. Prints what changed, which wakes the Aide
profile's cron agent; prints `{"wakeAgent": false}` when nothing did.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.request

from kanban_pr_owner import pr_comment_argv, published_pr

WINDOW_SECONDS = 24 * 60 * 60
MIN_CARDS = 3
SYSTEMIC_AT = 0.6  # calibrated 2026-10-11 on 49 silk block reasons; see the Charm commit
SAME_AT = 0.8
SAMPLES = 3       # reasons kept per incident to match differently worded members
MAX_POOL = 20
BLOCK_KINDS = ("blocked", "block_loop_detected", "gave_up")
CLOSED = ("done", "archived")
REASON_CHARS = 1500
JEV_URL = "https://api.typesafe.ai/v1/systemone"

Ask = Callable[[dict, dict], dict]
Runner = Callable[[list[str]], "tuple[int, str, str]"]

SYSTEMIC = {
    "type": "noul",
    "instructions": (
        "Is `new_block.reason` caused by something outside this card's own change that would "
        "block other cards the same way: a CI or hosting provider outage, billing or spending "
        "limit, missing credentials or secrets, a dispatcher or worker-environment defect, a "
        "broken tool install, or an approval or policy rule? Answer false when the cause is this "
        "card's own code, scope, review findings, a dependency on one specific other PR, or a "
        "decision only this card needs."),
    "criteria": {"true": "The cause sits outside this card and would block other cards the same way.",
                 "false": "The cause is specific to this card."},
}


def same_cause(key: str) -> dict:
    return {
        "type": "noul",
        "instructions": (f"Do `new_block.reason` and `candidates.{key}` describe the same underlying "
                         "cause, so that one fix outside both cards would unblock both?"),
        "criteria": {"true": "Same cause; one external fix clears both.",
                     "false": "Different causes, or each needs its own fix."},
    }


class JevError(RuntimeError):
    """Jev was unreachable or answered badly. Nothing is judged; the tick retries."""


def jev_asker(api_key: str) -> Ask:
    def ask(state: dict, questions: dict) -> dict:
        body = json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode()
        request = urllib.request.Request(JEV_URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "User-Agent": "charm-kanban-incident-watch"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                answers = json.loads(response.read())["answers"]
        except urllib.error.HTTPError as exc:  # the body can echo the submitted state back
            raise JevError(f"System One HTTP {exc.code}") from exc
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise JevError(f"System One failed: {exc.__class__.__name__}") from exc
        return {key: float(answer["noul"]) for key, answer in answers.items()
                if isinstance(answer, dict) and isinstance(answer.get("noul"), (int, float))}
    return ask


def read_blocked(db: Path, now: float) -> dict[str, dict]:
    """Blocked cards with a recent block event: {task: {event, at, reason, title}}."""
    marks = ",".join("?" * len(BLOCK_KINDS))
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        rows = conn.execute(
            f"SELECT t.id, t.title, e.id, e.created_at, e.payload FROM tasks t JOIN task_events e"
            f" ON e.id = (SELECT id FROM task_events WHERE task_id = t.id AND kind IN ({marks})"
            f"            ORDER BY created_at DESC, id DESC LIMIT 1)"
            f" WHERE t.status = 'blocked'", BLOCK_KINDS).fetchall()
    found = {}
    for task, title, event, at, payload in rows:
        if now - at > WINDOW_SECONDS:
            continue
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            data = {}
        reason = str(data.get("reason") or data.get("error") or "").strip()
        if reason:
            found[task] = {"event": event, "at": at, "reason": reason[:REASON_CHARS],
                           "title": (title or task).splitlines()[0][:80]}
    return found


def statuses(db: Path, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as conn:
        marks = ",".join("?" * len(ids))
        return dict(conn.execute(f"SELECT id, status FROM tasks WHERE id IN ({marks})", ids))


def incident_body(incident: dict) -> str:
    return "\n".join([
        "Kanban incident: several cards blocked on what Jev judged to be one cause outside them.",
        "", "First reason:", "", incident["samples"][0], "",
        "Held cards wait in todo behind this card. Fix the cause, then complete this card to "
        "release them all. To release one card only: `hermes kanban unlink <this> <card>`.",
    ])


def ensure_card(board: str, key: str, incident: dict, cmd: Runner, park: str) -> str | None:
    if incident.get("card"):
        return None
    rc, out, err = cmd(["hermes", "kanban", "--board", board, "create",
                        f"Incident: cards blocked on one cause ({len(incident['members'])})",
                        "--assignee", park, "--workspace", "scratch", "--json",
                        "--idempotency-key", f"incident-watch-{board}-{key}",
                        "--body", incident_body(incident)])
    try:
        incident["card"] = json.loads(out)["id"] if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        incident["card"] = None
    return None if incident["card"] else f"creating the incident card failed: {(err or out).strip()[-200:]}"


def hold(board: str, incident: dict, task: str, cmd: Runner) -> str | None:
    base = ["hermes", "kanban", "--board", board]
    rc, _, err = cmd(base + ["link", incident["card"], task])
    if rc != 0 and "exist" not in err.lower():
        return f"linking {task} failed: {err.strip()[-200:]}"
    rc, _, err = cmd(base + ["unblock", task, "--reason",
                             f"[incident-watch] Held behind {incident['card']}: blocked on the same cause "
                             f"as other cards. It returns to ready when that card is completed."])
    return None if rc == 0 else f"unblock {task} failed: {err.strip()[-200:]}"


def check_board(db: Path, entry: dict, now: float, ask: Ask, cmd: Runner, park: str) -> list[str]:
    board = db.parent.name
    incidents: dict = entry.setdefault("incidents", {})
    judged: dict = entry.setdefault("judged", {})
    pool: dict = entry.setdefault("pool", {})
    lines: list[str] = []

    live = statuses(db, [i["card"] for i in incidents.values() if i.get("card")])
    for key, incident in list(incidents.items()):
        if incident.get("card") and live.get(incident["card"], "archived") in CLOSED:
            lines.append(f"- {incident['card']}: incident closed; released {len(incident['held'])} held cards.")
            del incidents[key]

    blocked = read_blocked(db, now)
    for task in list(judged):
        if blocked.get(task, {}).get("event") != judged[task]:
            del judged[task]
    for task in list(pool):
        if task not in judged:
            del pool[task]

    for task in sorted(blocked, key=lambda t: blocked[t]["at"]):
        card = blocked[task]
        if judged.get(task) == card["event"]:
            continue
        candidates = {f"i{n}_{s}": text for n, incident in enumerate(incidents.values())
                      for s, text in enumerate(incident["samples"])}
        candidates.update({f"p_{t}": pool[t] for t in sorted(pool)[-MAX_POOL:]})
        questions = {"systemic": SYSTEMIC, **{f"same_{k}": same_cause(k) for k in candidates}}
        try:
            scores = ask({"new_block": {"card": task, "reason": card["reason"]}, "candidates": candidates},
                         questions)
        except JevError as exc:
            lines.append(f"- Jev unavailable, judging resumes next tick: {exc}")
            break
        judged[task] = card["event"]
        matched = [k for k in candidates if scores.get(f"same_{k}", 0) >= SAME_AT]
        hit = next((k for k in matched if k.startswith("i")), None)
        if hit:
            incident = list(incidents.values())[int(hit[1:].split("_")[0])]
            incident["members"].append(task)
            if len(incident["samples"]) < SAMPLES:
                incident["samples"].append(card["reason"])
        elif scores.get("systemic", 0) >= SYSTEMIC_AT:
            peers = [k[2:] for k in matched if k.startswith("p_")]
            if len(peers) + 1 < MIN_CARDS:
                pool[task] = card["reason"]
                continue
            group = sorted(peers, key=lambda t: blocked[t]["at"]) + [task]
            for peer in peers:
                del pool[peer]
            incidents[group[0]] = {"card": None, "members": group, "held": [],
                                   "samples": [blocked[t]["reason"] for t in group[:SAMPLES]],
                                   "opened_at": now}

    for key, incident in incidents.items():
        failure = ensure_card(board, key, incident, cmd, park)
        if failure:
            lines.append(f"- {failure}")
            continue
        new = [t for t in incident["members"] if t not in incident["held"] and t in blocked]
        for task in new:
            failure = hold(board, incident, task, cmd)
            if failure:
                lines.append(f"- {incident['card']}: {failure}")
                continue
            incident["held"].append(task)
            lines.append(f"- {incident['card']}: held {task} ({blocked[task]['title']})")
            pr_url = published_pr(db, task)
            if pr_url:
                rc, _, err = cmd(pr_comment_argv(pr_url, task, (
                    f"Card {task} is blocked by the same cause as {len(incident['members']) - 1} other "
                    f"cards, so I moved it to `todo` behind incident card {incident['card']} to stop it "
                    f"retrying. It returns to `ready` when the operator completes that card.")))
                if rc != 0:
                    lines.append(f"- {incident['card']}: commenting on {pr_url} failed: {err.strip()[-200:]}")
    if lines:
        lines.insert(0, f"Kanban board `{board}`:")
    return lines


def run(boards: Path, state_path: Path, now: float, ask: Ask, cmd: Runner, park: str) -> str:
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    report = []
    for db in sorted(boards.glob("*/kanban.db")):
        if db.parent.name.startswith(("_", "-")):
            continue
        try:
            report += check_board(db, state.setdefault(db.parent.name, {}), now, ask, cmd, park)
        except sqlite3.Error as exc:  # one bad board must not silence the others
            report.append(f"Kanban board `{db.parent.name}`: incident watch could not read it: {exc}")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(state_path)
    return "\n".join(report)


def main() -> int:
    from deployment_settings import get, hermes_environment

    # Boards live in the root home. HERMES_HOME names the profile running this (Aide's), not it.
    home = Path(os.environ.get("HERMES_KANBAN_HOME") or get("kanban-incident-watch", "home")).expanduser()
    park = get("kanban-incident-watch", "park_assignee")
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print("kanban-incident-watch: TYPESAFE_API_KEY is not set; no cards were judged.")
        return 1
    env = hermes_environment(home)
    dry = "--dry-run" in sys.argv[1:]

    def cmd(argv: list[str]) -> tuple[int, str, str]:
        if dry and argv[4] != "create":
            print("would run:", " ".join(argv[3:6]))
            return 0, "", ""
        if dry:
            return 0, json.dumps({"id": "t_DRYRUN"}), ""
        try:
            r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", str(exc)
        return r.returncode, r.stdout, r.stderr

    state = home / "kanban" / "incident-watch.json"
    if dry:
        state = Path(os.environ.get("TMPDIR") or "/tmp") / "incident-watch.dry-run.json"
        state.unlink(missing_ok=True)
    report = run(home / "kanban" / "boards", state, time.time(), jev_asker(api_key), cmd, park)
    print(report or json.dumps({"wakeAgent": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
