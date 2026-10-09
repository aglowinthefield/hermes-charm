#!/usr/bin/env python3
"""Send PRs that failed out of the Trunk merge queue back to the kanban card that owns them.

The operator queues an approved PR with `/trunk merge` while its card sits parked on the
human review assignee. If Trunk then fails it out of the queue (a required check failed on
the combined head, or it conflicts with a PR merged ahead of it), the card stays parked and
nobody is told. So each tick, for every open non-draft PR the authenticated `gh` user
authored in the configured repo:

  1. Ask Trunk for the PR's queue entry (`getSubmittedPullRequest`, with its timeline). Never
     submitted, or in any state but `failed`, is silent. `pending_failure` waits for Trunk to
     settle the blame. A failure older than the PR's head commit is stale: someone already
     pushed after it, so it is recorded and skipped.
  2. A failure the implementer can fix (a required check failed, or a merge conflict) whose
     owning card is in `review`, parked on the human assignee with no live claim: comment the
     failing checks or the conflicting PR, `reopen-review`, then reassign to the implementer
     from its last handoff into AI review, as pr-conflict-sweep does.
  3. Card already moving or under AI review: leave it, and look again once it parks.
  4. Anything else (a queue or GitHub API failure, an unrecognised reason, a card that is
     human-held, finished or missing): report it once. The sweep never requeues a PR, creates
     a card, or touches a branch.

Each failure is acted on or reported once per queue entry; resubmitting the PR creates a new
entry and resets it. Trunk, gh and hermes failures are reported and retried next tick. Run by
`hermes cron` with --no-agent, so it costs no inference tokens. Silent when nothing changed.
--dry-run prints the hermes writes instead of running them and uses a throwaway state file.

TRUNK_API_TOKEN comes from the environment; `hermes cron` loads ~/.hermes/.env.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable
import urllib.error
import urllib.request

from kanban_pr_owner import MOVING, implementer_of, owning_card

Runner = Callable[[list[str]], "tuple[int, str, str]"]
TrunkLookup = Callable[[int], "dict | None"]


class TrunkAuthError(RuntimeError):
    """The token was refused; every other lookup this tick would fail the same way."""

AUTHOR = "trunk-queue-sweep"
API = "https://api.trunk.io/v1/getSubmittedPullRequest"
PLAYBOOK = "docs/agent-playbooks/rebase-regenerate.md"
CHECK_FAILED = "test_run_required_status_failed"
CONFLICT = "start_test_run_merge_conflict"


def list_open(cmd: Runner, repo: str) -> list[dict]:
    rc, out, err = cmd(["gh", "pr", "list", "--repo", repo, "--author", "@me", "--state", "open",
                        "--limit", "200", "--json", "number,title,url,headRefName,headRefOid,isDraft"])
    if rc != 0:
        raise RuntimeError(f"gh pr list failed: {err.strip()[-300:]}")
    return [pr for pr in json.loads(out or "[]") if not pr.get("isDraft")]


def trunk_client(token: str, repo: str, target_branch: str) -> TrunkLookup:
    owner, name = repo.split("/", 1)

    def lookup(number: int) -> dict | None:
        body = json.dumps({"repo": {"host": "github.com", "owner": owner, "name": name},
                           "targetBranch": target_branch, "pr": {"number": number},
                           "includeTimeline": True}).encode()
        request = urllib.request.Request(API, data=body, method="POST", headers={
            "x-api-token": token, "content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None  # never submitted to this queue
            if exc.code in (401, 403):
                raise TrunkAuthError(f"Trunk API HTTP {exc.code}: the key cannot read queue state")
            raise RuntimeError(f"Trunk API HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}")
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise RuntimeError(f"Trunk API: {exc}")

    return lookup


def last_failure(entry: dict) -> tuple[dict, int | None, str]:
    """The `failure` details, test PR number and timestamp of the latest `failed` timeline entry.

    Trunk returns the timeline newest first, but sort by timestamp rather than trust that."""
    failures = [event for event in entry.get("timeline") or [] if event.get("state") == "failed"]
    if not failures:
        return {}, None, ""
    event = max(failures, key=lambda e: e.get("timestamp") or "")
    return event.get("failure") or {}, (event.get("testRun") or {}).get("prNumber"), event.get("timestamp") or ""


def head_committed_at(cmd: Runner, repo: str, sha: str) -> str:
    rc, out, err = cmd(["gh", "api", f"repos/{repo}/commits/{sha}", "--jq", ".commit.committer.date"])
    if rc != 0:
        raise RuntimeError(f"gh api commit {sha[:12]} failed: {err.strip()[-200:]}")
    return out.strip()


def iso_order(stamp: str) -> str:
    """RFC 3339 UTC stamps compare as strings once the fraction and suffix are dropped."""
    return stamp.replace("Z", "")[:19]


def rework_comment(pr: dict, failure: dict, test_pr: int | None) -> str:
    # Bare #n only: the dispatcher reads a PR URL in a recent comment as "PR exists, do not
    # respawn", which would hold this exact rework. Check links are Actions URLs, kept when
    # they carry no /pull/ path.
    where = f" on its queue test PR #{test_pr}" if test_pr else ""
    head = f"#{pr['number']} failed out of the Trunk merge queue{where} at head {pr['headRefOid'][:12]}."
    if failure.get("reason") == CONFLICT:
        culprit = (failure.get("conflictCulprit") or {}).get("prNumber")
        against = f"#{culprit}, merged or queued ahead of it" if culprit else "main"
        return (
            f"{head} Trunk hit a merge conflict against {against}. Reopened by the Trunk queue "
            f"sweep so this card's implementer refreshes its own branch.\n\n"
            f"Follow `{PLAYBOOK}`: rebase `{pr['headRefName']}` onto `origin/main`, regenerate "
            f"generated files instead of hand-merging them, rerun the branch's targeted tests, push "
            f"with `--force-with-lease` to the same branch, wait for green CI at the new head, then "
            f"request review again. Do not open a new PR or requeue it; the operator queues it."
        )
    checks = []
    for status in failure.get("failureCausingStatuses") or []:
        line = f"- `{status.get('name', '?')}` ({status.get('conclusion', 'FAILED')})"
        url = status.get("url") or ""
        if url and "/pull/" not in url:
            line += f": {url}"
        checks.append(line)
    listed = "\n".join(checks) or "- (Trunk listed none; read the test PR's checks)"
    return (
        f"{head} Required checks failed on the queue's combined head, which is main plus any PRs "
        f"queued ahead of this one. Reopened by the Trunk queue sweep.\n\nFailing checks:\n{listed}\n\n"
        f"Reproduce on the branch rebased onto `origin/main`. If the failure comes from this "
        f"branch, fix it, push to the same branch, wait for green CI at the new head, then request "
        f"review again. If it is a flake or comes from main, block with `needs_input` naming the "
        f"check and the evidence instead of changing code. Do not open a new PR or requeue it."
    )


def handle(pr: dict, failure: dict, test_pr: int | None, settings: dict, db: Path,
           cmd: Runner) -> tuple[bool, str]:
    """Return (done, message). done=False leaves the entry unrecorded so the next tick retries."""
    label = f"#{pr['number']} ({pr['title'][:70]})"
    reason = failure.get("reason") or "unknown"
    if reason not in (CHECK_FAILED, CONFLICT):
        return True, (f"- {label}: failed out of the Trunk queue ({reason}). Not a code failure; "
                      f"requeue with `/trunk merge` once it clears.")
    card = owning_card(db, pr)
    if card is None:
        return True, f"- {label}: failed out of the Trunk queue ({reason}), and no kanban card owns it."
    task, status = card["id"], card["status"]
    if status in MOVING or (status == "review" and (card["assignee"] != settings["park_assignee"]
                                                     or card["claim_lock"])):
        return False, ""  # already moving or under AI review; look again once it parks
    if status != "review":
        return True, (f"- {label}: failed out of the Trunk queue ({reason}); owner card {task} is "
                      f"{status} on {card['assignee']}. Route it by hand.")
    implementer = implementer_of(db, task, settings["park_assignee"])
    if not implementer:
        return True, (f"- {label}: failed out of the Trunk queue ({reason}); owner card {task} has no "
                      f"implementer handoff on record. Route it by hand.")
    board = ["hermes", "kanban", "--board", settings["board"]]
    for argv in (board + ["comment", task, "--author", AUTHOR, rework_comment(pr, failure, test_pr)],
                 board + ["reopen-review", task, "--reason", "PR failed out of the Trunk merge queue"]):
        rc, _, err = cmd(argv)
        if rc != 0:
            return False, f"- {label}: could not {argv[4]} {task}, retrying next tick: {err.strip()[-200:]}"
    rc, _, err = cmd(board + ["reassign", task, implementer, "--reclaim", "--reason",
                              "trunk-queue-sweep: queue failure belongs to the card's implementer"])
    if rc != 0:
        return True, (f"- {label}: reopened {task}, but reassigning it to {implementer} failed: "
                      f"{err.strip()[-200:]}. Reassign it by hand.")
    return True, f"- {label}: failed out of the Trunk queue ({reason}); reopened {task} for {implementer}."


def run(settings: dict, db: Path, state_path: Path, cmd: Runner, trunk: TrunkLookup) -> str:
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    try:
        prs = list_open(cmd, settings["repo"])
    except (RuntimeError, ValueError) as exc:
        return f"Trunk queue sweep for {settings['repo']}: {exc}"
    lines, open_numbers = [], {str(pr["number"]) for pr in prs}
    for pr in prs:
        key = str(pr["number"])
        try:
            entry = trunk(pr["number"])
        except TrunkAuthError as exc:
            return f"Trunk queue sweep for {settings['repo']}: {exc}. Fix TRUNK_API_TOKEN's permissions."
        except RuntimeError as exc:
            lines.append(f"- #{key}: Trunk lookup failed, retrying next tick: {exc}")
            continue
        if not entry or entry.get("state") != "failed" or state.get(key) == entry.get("id"):
            continue
        failure, test_pr, failed_at = last_failure(entry)
        try:
            pushed_at = head_committed_at(cmd, settings["repo"], pr["headRefOid"])
        except RuntimeError as exc:
            lines.append(f"- #{key}: {exc}; retrying next tick")
            continue
        if failed_at and iso_order(pushed_at) > iso_order(failed_at):
            state[key] = entry.get("id")  # the branch moved on since the failure; nothing to route
            continue
        done, message = handle(pr, failure, test_pr, settings, db, cmd)
        if done:
            state[key] = entry.get("id")
        if message:
            lines.append(message)
    state = {key: entry_id for key, entry_id in state.items() if key in open_numbers}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp = state_path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True))
    temp.replace(state_path)
    if not lines:
        return ""
    return "\n".join([f"Trunk merge queue failures on {settings['repo']}:", *lines])


def main() -> int:
    from deployment_settings import get, hermes_environment

    home = Path(os.environ.get("HERMES_HOME") or get("trunk-queue-sweep", "home")).expanduser()
    settings = {key: get("trunk-queue-sweep", key)
                for key in ("repo", "board", "park_assignee", "target_branch")}
    token = os.environ.get("TRUNK_API_TOKEN", "").strip()
    if not token:
        print("Trunk queue sweep: TRUNK_API_TOKEN is not set (add it to ~/.hermes/.env).")
        return 0
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

    state = home / "kanban" / "trunk-queue-sweep.json"
    if "--dry-run" in sys.argv[1:]:
        real, state = cmd, Path(os.environ.get("TMPDIR") or "/tmp") / "trunk-queue-sweep.dry-run.json"
        state.unlink(missing_ok=True)

        def cmd(argv: list[str]) -> tuple[int, str, str]:
            if argv[0] == "hermes":
                print("would run:", " ".join(argv[:6]))
                return 0, "", ""
            return real(argv)

    report = run(settings, board_dir / "kanban.db", state, cmd,
                 trunk_client(token, settings["repo"], settings["target_branch"]))
    if report:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
