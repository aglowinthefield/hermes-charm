#!/usr/bin/env python3
"""Send conflicting operator-ready PRs back to the kanban card that owns them.

A PR the operator has marked ready (not a draft) often sits parked on the human review
assignee while main moves on, until GitHub reports it CONFLICTING and nobody notices before
the merge attempt. The card that produced the PR is still the right owner: its implementer
created the branch and may refresh its base. So each tick, for every open non-draft PR the
authenticated `gh` user authored in the configured repo:

  1. Skip anything not CONFLICTING. UNKNOWN means GitHub has not computed mergeability yet;
     listing the PR asks it to, so the next tick sees the real value.
  2. Find the owning card on the board: same branch name, or a run whose
     `metadata.published_pr` is this PR's URL. Open cards win over finished ones.
  3. Card in `review`, parked on the human assignee, with no live claim: run the
     configured relevance collector first. RELEVANT or NEEDS_REWORK gets a card comment with
     the conflicting files and the rebase playbook, then `reopen-review` and a reassign to the
     implementer from its last handoff into AI review (`reopen-review` alone restores whoever
     made the latest handoff, which is the AI reviewer once it has parked the card on the
     human). Other verdicts are reported, never reopened.
  4. Card already moving (ready, running, todo, or under AI review): leave it alone.
     The implementer rebases before requesting review anyway.
  5. Card human-held (triage, blocked, scheduled), finished, or missing: report it. The
     sweep never creates a card or touches a branch no card owns.

Each PR is acted on or reported once per head SHA; a new push or a resolved conflict resets
it. Failures (gh, collector, hermes) are reported and retried next tick. Run by `hermes cron`
with --no-agent, so it costs no inference tokens. Silent when nothing changed; cron delivers
whatever reaches stdout. --dry-run prints the hermes writes instead of running them and uses
a throwaway state file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from kanban_pr_owner import HUMAN_HELD, MOVING, implementer_of, owning_card

Runner = Callable[[list[str]], "tuple[int, str, str]"]

AUTHOR = "pr-conflict-sweep"
ACTIONABLE_VERDICTS = ("RELEVANT", "NEEDS_REWORK")
PLAYBOOK = "docs/agent-playbooks/rebase-regenerate.md"


def list_conflicting(cmd: Runner, repo: str) -> list[dict]:
    rc, out, err = cmd(["gh", "pr", "list", "--repo", repo, "--author", "@me", "--state", "open",
                        "--limit", "200", "--json",
                        "number,title,url,headRefName,headRefOid,isDraft,mergeable"])
    if rc != 0:
        raise RuntimeError(f"gh pr list failed: {err.strip()[-300:]}")
    return [pr for pr in json.loads(out or "[]")
            if pr.get("mergeable") == "CONFLICTING" and not pr.get("isDraft")]


def relevance(cmd: Runner, collector: str, number: int) -> tuple[str, list[str]]:
    rc, out, err = cmd([sys.executable, collector, str(number)])
    if rc != 0:
        raise RuntimeError(err.strip()[-300:] or f"collector exited {rc}")
    data = json.loads(out)
    if data.get("failures") or not data.get("reports"):
        raise RuntimeError(json.dumps(data.get("failures"))[-300:])
    report = data["reports"][0]
    return report.get("verdict") or "UNKNOWN", list(report.get("content", {}).get("conflicts") or [])


def rework_comment(pr: dict, conflicts: list[str]) -> str:
    # Bare #n only: the dispatcher reads a PR URL in a recent comment as "PR exists, do not
    # respawn", which would hold this exact rework.
    files = "\n".join(f"- `{path}`" for path in conflicts) or "- (collector listed none; rebase to see)"
    return (
        f"#{pr['number']} is CONFLICTING with main at head {pr['headRefOid'][:12]}. Reopened by the "
        f"conflict sweep so this card's implementer refreshes its own branch.\n\n"
        f"Conflicting files:\n{files}\n\n"
        f"Follow `{PLAYBOOK}`: rebase `{pr['headRefName']}` onto `origin/main`, regenerate generated "
        f"files instead of hand-merging them, rerun the branch's targeted tests, push with "
        f"`--force-with-lease` to the same branch, wait for green CI at the new head, then request "
        f"review again. Do not open a new PR. If main rewrote the area this branch changes, or a "
        f"conflict needs a product decision, block with `needs_input` instead of force-fitting it."
    )


def handle(pr: dict, settings: dict, db: Path, cmd: Runner) -> tuple[bool, str]:
    """Return (done, message). done=False leaves the PR unrecorded so the next tick retries."""
    number, label = pr["number"], f"#{pr['number']} ({pr['title'][:70]})"
    card = owning_card(db, pr)
    if card is None:
        return True, f"- {label}: conflicting, and no kanban card owns it. Resolve by hand."
    status, task = card["status"], card["id"]
    if status in MOVING or (status == "review" and (card["assignee"] != settings["park_assignee"]
                                                     or card["claim_lock"])):
        return False, ""  # already moving or under AI review; look again next tick
    if status in HUMAN_HELD:
        return True, f"- {label}: conflicting; owner card {task} ({status}, {card['assignee']}) is human-held."
    if status != "review":
        return True, f"- {label}: conflicting; owner card {task} is {status}. Route the rework by hand."
    implementer = implementer_of(db, task, settings["park_assignee"])
    if not implementer:
        return True, f"- {label}: conflicting; owner card {task} has no implementer handoff on record. Route it by hand."
    try:
        verdict, conflicts = relevance(cmd, settings["collector"], number)
    except (RuntimeError, ValueError) as exc:
        return False, f"- {label}: relevance check failed, retrying next tick: {exc}"
    if verdict not in ACTIONABLE_VERDICTS:
        return True, (f"- {label}: conflicting, but the relevance collector says {verdict}. Not reopened; "
                      f"close it or reopen {task} by hand.")
    board = ["hermes", "kanban", "--board", settings["board"]]
    for argv in (board + ["comment", task, "--author", AUTHOR, rework_comment(pr, conflicts)],
                 board + ["reopen-review", task, "--reason", "PR conflicts with main"]):
        rc, _, err = cmd(argv)
        if rc != 0:
            return False, f"- {label}: could not {argv[4]} {task}, retrying next tick: {err.strip()[-200:]}"
    # reopen-review may restore the reviewer; reclaim covers a dispatcher tick that already
    # spawned it between the two calls.
    rc, _, err = cmd(board + ["reassign", task, implementer, "--reclaim", "--reason",
                              "pr-conflict-sweep: rebase belongs to the card's implementer"])
    if rc != 0:
        return True, (f"- {label}: reopened {task}, but reassigning it to {implementer} failed: "
                      f"{err.strip()[-200:]}. Reassign it by hand.")
    return True, f"- {label}: reopened {task} for {implementer} to rebase ({verdict})."


def run(settings: dict, db: Path, state_path: Path, cmd: Runner) -> str:
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        prs = list_conflicting(cmd, settings["repo"])
    except (RuntimeError, ValueError) as exc:
        return f"PR conflict sweep for {settings['repo']}: {exc}"
    lines, current = [], {str(pr["number"]): pr["headRefOid"] for pr in prs}
    for pr in prs:
        key = str(pr["number"])
        if state.get(key) == pr["headRefOid"]:
            continue
        done, message = handle(pr, settings, db, cmd)
        if done:
            state[key] = pr["headRefOid"]
        if message:
            lines.append(message)
    state = {key: sha for key, sha in state.items() if current.get(key) == sha}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(state_path)
    if not lines:
        return ""
    return "\n".join([f"Conflicting PRs on {settings['repo']}:", *lines])


def main() -> int:
    from deployment_settings import get, hermes_environment

    home = Path(os.environ.get("HERMES_HOME") or get("pr-conflict-sweep", "home")).expanduser()
    settings = {key: get("pr-conflict-sweep", key)
                for key in ("repo", "board", "park_assignee", "collector")}
    board_dir = home / "kanban" / "boards" / settings["board"]
    workdir = json.loads((board_dir / "board.json").read_text()).get("default_workdir") or None
    env = hermes_environment(home)

    def cmd(argv: list[str]) -> tuple[int, str, str]:
        try:
            r = subprocess.run(argv, cwd=workdir, env=env, capture_output=True, text=True,
                               timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", str(exc)
        return r.returncode, r.stdout, r.stderr

    state = home / "kanban" / "pr-conflict-sweep.json"
    if "--dry-run" in sys.argv[1:]:
        real, state = cmd, Path(os.environ.get("TMPDIR") or "/tmp") / "pr-conflict-sweep.dry-run.json"
        state.unlink(missing_ok=True)

        def cmd(argv: list[str]) -> tuple[int, str, str]:
            if argv[0] == "hermes":
                print("would run:", " ".join(argv[:6]))
                return 0, "", ""
            return real(argv)

    report = run(settings, board_dir / "kanban.db", state, cmd)
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
