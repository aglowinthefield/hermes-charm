#!/usr/bin/env python3
"""Send conflicting parked PRs back to the kanban card that owns them.

A PR often sits parked on the human review assignee while main moves on, until GitHub reports
it CONFLICTING and nobody notices before the merge attempt. Drafts count: workers open drafts and
only the operator marks them ready, so most parked PRs are still drafts. The card that produced
the PR is still the right owner: its implementer created the branch and may refresh its base.
So each tick, for every open PR the authenticated `gh` user authored in the configured repo:

  1. Skip anything not CONFLICTING. UNKNOWN means GitHub has not computed mergeability yet;
     listing the PR asks it to, so the next tick sees the real value.
  2. Find the owning card on the board: same branch name, or a run whose
     `metadata.published_pr` is this PR's URL. Open cards win over finished ones.
  3. Card in `review`, parked on the human assignee, with no live claim: run the
     configured relevance collector first. RELEVANT or NEEDS_REWORK gets a card comment with
     the conflicting files and the rebase playbook, then `reopen-review` and a reassign to the
     implementer from its last handoff into AI review (`reopen-review` alone restores whoever
     made the latest handoff, which is the AI reviewer once it has parked the card on the
     human), and an Aide comment on the PR saying who has it. Other verdicts are reported,
     never reopened.
  4. Card already moving (ready, running, todo, or under AI review): leave it alone.
     The implementer rebases before requesting review anyway.
  5. Card human-held (triage, blocked, scheduled), or finished: report it.
  6. No card owns the PR (the operator opened it outside the board), or only a finished
     adoption card does: adopt it. `--author @me` already limits the sweep to the operator's
     own PRs, and the operator authorized adopting them. Unless a checkout of the head branch
     holds uncommitted or unpushed work (someone is mid-change: report and leave it), fetch
     the head, point a local `adopt/pr-<n>` branch at it, and create a worktree card for the
     configured `adopt_assignee` that merges `origin/main` into the branch and pushes to the
     PR's own head. The local name keeps the operator's checkouts of the head untouched. Then
     comment on the PR saying who has it. The sweep never touches another author's branch.

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

from kanban_pr_owner import ADOPT_PREFIX, HUMAN_HELD, MOVING, adopt_branch, implementer_of, owning_card, pr_comment_argv

Runner = Callable[[list[str]], "tuple[int, str, str]"]

AUTHOR = "pr-conflict-sweep"
ACTIONABLE_VERDICTS = ("RELEVANT", "NEEDS_REWORK")
PLAYBOOK = "docs/agent-playbooks/rebase-regenerate.md"
FINISHED = ("done", "archived")


def list_conflicting(cmd: Runner, repo: str) -> list[dict]:
    rc, out, err = cmd(["gh", "pr", "list", "--repo", repo, "--author", "@me", "--state", "open",
                        "--limit", "200", "--json",
                        "number,title,url,headRefName,headRefOid,mergeable"])
    if rc != 0:
        raise RuntimeError(f"gh pr list failed: {err.strip()[-300:]}")
    return [pr for pr in json.loads(out or "[]") if pr.get("mergeable") == "CONFLICTING"]


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


def adoption_body(pr: dict, repo: str, conflicts: list[str]) -> str:
    """The adoption card's spec. The body (unlike a card comment) may carry the PR URL: the
    dispatcher's respawn guard reads comments only."""
    head, sha, number = pr["headRefName"], pr["headRefOid"], pr["number"]
    files = "\n".join(f"- `{path}`" for path in conflicts) or "- (collector listed none; merge to see)"
    return (
        f"## Goal\n\n"
        f"Bring #{number} ({pr['title']}) up to date with `main` so it stops conflicting. The "
        f"operator opened this PR outside the kanban board, and the operator authorized "
        f"`pr-conflict-sweep` to adopt their own conflicting PRs, so this card adopts it for this "
        f"one refresh.\n\n"
        f"- PR: {pr['url']}\n"
        f"- PR head on origin: `{head}` at `{sha}`\n"
        f"- This card's branch: `{adopt_branch(number)}`, starting at that head. It is a local name "
        f"only; the PR's branch may still be checked out in the operator's own checkout.\n\n"
        f"Conflicting files:\n{files}\n\n"
        f"## Steps\n\n"
        f"1. Run `gh pr view {number} --repo {repo} --json author,state,headRefName,headRefOid` and "
        f"`gh api user -q .login`. Continue only if the PR is OPEN, its author is that login, and "
        f"its head is still `{head}` at `{sha}`. Otherwise block with `needs_input` and say what changed.\n"
        f"2. `git fetch origin main`, then `git merge origin/main`. Merge, not rebase: the PR "
        f"keeps its history, needs no force-push, and its screenshot commit pins stay valid. "
        f"Resolve conflicts with the regenerate table and rules in `{PLAYBOOK}`; never hand-edit "
        f"a generated file. Rerun the branch's targeted tests.\n"
        f"3. Push to the PR's own branch, never a new one: `git push origin HEAD:refs/heads/{head}`. "
        f"A rejected push means the PR moved; block with `needs_input`, do not force.\n"
        f"4. Do not open a PR, edit the PR body, or change anything beyond the merge and its "
        f"conflict resolution. If main rewrote the area this PR changes, or a conflict needs a "
        f"product decision, block with `needs_input` instead of force-fitting it.\n"
        f"5. Wait for green required checks at the new head (`gh pr checks {number} --repo {repo} "
        f"--watch`), then `kanban_request_review` with `metadata.published_pr` set to {pr['url']}.\n\n"
        f"## Acceptance criteria\n\n"
        f"- #{number} reports MERGEABLE against `main` at a head this card pushed to `{head}`.\n"
        f"- Required checks are green at that head.\n"
        f"- No new PR, no force-push, and no change beyond the merge and its conflict resolution.\n"
    )


def worktrees_on(cmd: Runner, branch: str) -> list[str]:
    """Paths of the checkouts that have `branch` checked out."""
    rc, out, err = cmd(["git", "worktree", "list", "--porcelain"])
    if rc != 0:
        raise RuntimeError(f"git worktree list failed: {err.strip()[-200:]}")
    paths, current = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}" and current:
            paths.append(current)
    return paths


def adopt(pr: dict, settings: dict, cmd: Runner, label: str) -> tuple[bool, str]:
    """Hand an ownerless PR to a new worktree card. See step 6 of the module docstring."""
    number, head, sha = pr["number"], pr["headRefName"], pr["headRefOid"]
    local, tracking = adopt_branch(number), f"refs/remotes/origin/{head}"
    rc, _, err = cmd(["git", "fetch", "--quiet", "origin", f"+refs/heads/{head}:{tracking}"])
    if rc != 0:
        return False, f"- {label}: could not fetch `{head}`, retrying next tick: {err.strip()[-200:]}"
    rc, fetched, _ = cmd(["git", "rev-parse", "--verify", "--quiet", tracking])
    if rc != 0 or fetched.strip() != sha:
        return False, ""  # the PR moved since gh listed it; the next tick sees its new head
    try:
        checkouts = worktrees_on(cmd, head)
    except RuntimeError as exc:
        return False, f"- {label}: {exc}; retrying next tick."
    busy = []
    for path in checkouts:
        rc, dirty, _ = cmd(["git", "-C", path, "status", "--porcelain"])
        if rc != 0 or dirty.strip():
            busy.append(f"{path} has uncommitted changes")
    rc, ahead, _ = cmd(["git", "rev-list", "--count", f"{tracking}..refs/heads/{head}"])
    if rc == 0 and ahead.strip() not in ("", "0"):
        busy.append(f"local `{head}` has {ahead.strip()} unpushed commit(s)")
    if busy:
        return True, (f"- {label}: conflicting and no card owns it, but {'; '.join(busy)}. Left alone "
                      f"while that work is in progress; it is adopted on the next push once clean.")
    try:
        stale = worktrees_on(cmd, local)
    except RuntimeError as exc:
        return False, f"- {label}: {exc}; retrying next tick."
    if stale:
        return True, (f"- {label}: conflicting; `{local}` is still checked out at {stale[0]} by an "
                      f"earlier adoption. Remove that worktree, or route the rework by hand.")
    rc, _, err = cmd(["git", "branch", "--force", "--no-track", local, tracking])
    if rc != 0:
        return False, f"- {label}: could not point `{local}` at `{head}`, retrying next tick: {err.strip()[-200:]}"
    try:
        _, conflicts = relevance(cmd, settings["collector"], number)
    except (RuntimeError, ValueError):
        conflicts = []  # the card body still works; the worker sees the files when it merges
    assignee = settings["adopt_assignee"]
    rc, out, err = cmd(["hermes", "kanban", "--board", settings["board"], "create",
                        f"Refresh #{number} onto main: {pr['title'][:80]}",
                        "--assignee", assignee, "--workspace", "worktree", "--branch", local,
                        "--created-by", AUTHOR, "--idempotency-key", f"{AUTHOR}:{number}:{sha}",
                        "--body", adoption_body(pr, settings["repo"], conflicts), "--json"])
    try:
        task = json.loads(out)["id"] if rc == 0 else None
    except (ValueError, KeyError, TypeError):
        task = None
    if not task:
        return False, f"- {label}: could not create an adoption card, retrying next tick: {(err or out).strip()[-200:]}"
    files = ", ".join(f"`{path}`" for path in conflicts) or "none listed"
    rc, _, err = cmd(pr_comment_argv(pr["url"], task, (
        f"This PR conflicts with `main` and no kanban card owned it, so I created card {task} for "
        f"**{assignee}** to merge `main` into this branch and push here. The card goes to review "
        f"when CI is green.\n\nConflicting files: {files}"), sha))
    note = "" if rc == 0 else f" Commenting on the PR failed: {err.strip()[-200:]}"
    return True, f"- {label}: no card owned it; adopted as {task} for {assignee} to merge main.{note}"


def handle(pr: dict, settings: dict, db: Path, cmd: Runner) -> tuple[bool, str]:
    """Return (done, message). done=False leaves the PR unrecorded so the next tick retries."""
    number, label = pr["number"], f"#{pr['number']} ({pr['title'][:70]})"
    card = owning_card(db, pr)
    if card is None or (card["status"] in FINISHED
                        and (card["branch_name"] or "").startswith(ADOPT_PREFIX)):
        return adopt(pr, settings, cmd, label)
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
    files = ", ".join(f"`{path}`" for path in conflicts) or "none listed"
    rc, _, err = cmd(pr_comment_argv(pr["url"], task, (
        f"This PR conflicts with `main`. I sent card {task} back to **{implementer}** to rebase "
        f"onto `main` and push to this branch; the card returns to review when CI is green.\n\n"
        f"Conflicting files: {files}"), pr["headRefOid"]))
    note = "" if rc == 0 else f" Commenting on the PR failed: {err.strip()[-200:]}"
    return True, f"- {label}: reopened {task} for {implementer} to rebase ({verdict}).{note}"


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
                for key in ("repo", "board", "park_assignee", "collector", "adopt_assignee")}
    board_dir = home / "kanban" / "boards" / settings["board"]
    workdir = json.loads((board_dir / "board.json").read_text()).get("default_workdir") or None
    if workdir:
        workdir = str(Path(workdir).expanduser())
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
            if argv[0] == "hermes" or argv[:3] == ["gh", "pr", "comment"] or argv[:2] == ["git", "branch"]:
                print("would run:", " ".join(argv[:12]))
                return 0, json.dumps({"id": "t_dry_run"}) if "create" in argv else "", ""
            return real(argv)

    report = run(settings, board_dir / "kanban.db", state, cmd)
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
