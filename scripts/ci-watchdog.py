#!/usr/bin/env python3
"""Watch configured repository main-branch CI for failures and route them to the kanban fleet.

Run by `hermes cron` with --no-agent, so detection costs no inference tokens. Each tick:
  1. list recent main-branch runs of the watched workflows via `gh`,
  2. drop runs already in the state file, runs that recovered (a newer run of the same
     workflow succeeded), and runs whose (workflow, head SHA) already has a card,
  3. classify each remaining failure from its failed-log excerpt:
       - flake-shaped (network/runner/fly API): re-run failed jobs once; a card only if
         the same run already had its one re-run,
       - infra-shaped (OOM/disk/secrets/Doppler/Neon): alert the operator, no card,
       - code-shaped (everything else): file an implementation card pre-loaded with the evidence,
  4. enforce the daily card cap; past it, alert instead of filing,
  5. print only what happened (cron delivers stdout verbatim; empty = silence).

SHADOW MODE: while the state file has "shadow": true (the default), nothing is created
and nothing is re-run — the would-be card / re-run is printed instead. Flip with:
  jq '.shadow = false' ~/.hermes/kanban/ci-watchdog-state.json > tmp && mv tmp <state>
First run reports currently open failures (so shadow output is reviewable) and then
marks every seen run id, so only NEW failures are reported from the second tick on.

Only main-branch runs are watched: PR-branch failures belong to the branch author.
Deploy/workflow-file fixes are never auto-filed as implementation cards — a failure inside
.github/workflows or the deploy pipeline alerts the operator instead.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

RUN_START = int(time.time()) - 5

from deployment_settings import get, hermes_environment

_CONFIG_HOME = get("ci-watchdog", "home")
HOME = Path(os.environ.get("HERMES_HOME") or _CONFIG_HOME).expanduser()
REPO = get("ci-watchdog", "repo")
BOARD = get("ci-watchdog", "board")
CONTRACT = get("ci-watchdog", "contract")
IMPLEMENTER = get("ci-watchdog", "implementer")
IMPLEMENTER_LABEL = get("ci-watchdog", "implementer_label")
REVIEWER = get("ci-watchdog", "reviewer")
REVIEW_PATH = get("ci-watchdog", "review_path")
DAILY_CARD_CAP = get("ci-watchdog", "daily_card_cap")
NET_FAILS_BEFORE_ALERT = get("ci-watchdog", "net_fails_before_alert")
LOG_EXCERPT_CAP = get("ci-watchdog", "log_excerpt_cap")
STATE = HOME / "kanban" / "ci-watchdog-state.json"
WATCHED_WORKFLOWS = set(get("ci-watchdog", "watched_workflows"))
DEPLOY_WORKFLOWS = set(get("ci-watchdog", "deploy_workflows"))

FLAKE_PATTERNS = re.compile(
    r"terminated signal received|no space left on device|ETIMEDOUT|ECONNRESET|EAI_AGAIN|"
    r"502 Bad Gateway|503 Service|api\.fly\.io|lost communication with the server|"
    r"operation timed out|network request failed|Internal Server Error",
    re.IGNORECASE,
)
INFRA_PATTERNS = re.compile(
    r"exit code: 137|JavaScript heap out of memory|doppler|secret.*(missing|not set|unset)|"
    r"NEON_API_KEY|NEON_PROJECT_ID|authentication failed|permission denied",
    re.IGNORECASE,
)

TRAP_NOTES = get("ci-watchdog", "trap_notes")


def sh(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    env = hermes_environment(HOME) if args and args[0] == 'hermes' else None
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            pass
    return {"shadow": True}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def recent_main_runs() -> list[dict]:
    r = sh(["gh", "run", "list", "--repo", REPO, "--branch", "main", "--limit", "60",
            "--json", "databaseId,workflowName,conclusion,headSha,createdAt,url,displayTitle"],
           timeout=90)
    if r.returncode != 0:
        raise ConnectionError(f"gh run list failed: {r.stderr.strip()[-200:]}")
    return [run for run in json.loads(r.stdout) if run["workflowName"] in WATCHED_WORKFLOWS]


def recovered(run: dict, runs: list[dict]) -> bool:
    """A newer successful run of the same workflow means main is green again."""
    return any(
        other["workflowName"] == run["workflowName"]
        and other["conclusion"] == "success"
        and other["createdAt"] > run["createdAt"]
        for other in runs
    )


def failed_log_excerpt(run_id: int) -> str:
    r = sh(["gh", "run", "view", str(run_id), "--repo", REPO, "--log-failed"], timeout=120)
    if r.returncode != 0:
        return f"(could not fetch log: {r.stderr.strip()[-150:]})"
    text = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if "##[error]" in line or "Type error" in line]
    if not hits:
        excerpt = lines[-30:]
    else:
        lo, hi = max(0, hits[0] - 8), min(len(lines), hits[-1] + 4)
        excerpt = lines[lo:hi]
    out = "\n".join(line.split("\t", 2)[-1] for line in excerpt)
    return out[-LOG_EXCERPT_CAP:]


def classify(excerpt: str, workflow: str) -> str:
    if workflow in DEPLOY_WORKFLOWS and "flyctl deploy stalled" in excerpt:
        return "flake"
    if INFRA_PATTERNS.search(excerpt):
        return "infra"
    if FLAKE_PATTERNS.search(excerpt):
        return "flake"
    return "code"


def card_covered(state: dict, workflow: str, sha: str) -> str | None:
    """A card already filed for this (workflow, SHA) — rerun failures of the same commit
    belong on the existing card, not a new one."""
    db = HOME / "kanban" / "boards" / BOARD / "kanban.db"
    if not db.exists():
        return None
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        row = conn.execute(
            "SELECT id FROM tasks WHERE status != 'archived' AND created_by = 'ci-watchdog' "
            "AND idempotency_key LIKE ? LIMIT 1",
            (f"ci-failure/{workflow}/{sha[:12]}%",),
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def build_body(run: dict, excerpt: str, klass: str) -> str:
    title = run["displayTitle"]
    return f"""\
## Goal
Fix the `{run['workflowName']}` failure on `main` at `{run['headSha'][:12]}`.

## Evidence
- Run: {run['url']}
- Merge/head commit: `{run['headSha'][:12]}` — {title}
- Class: {klass} (auto-classified by ci-watchdog; re-classify if the log says otherwise)

### Failed-log excerpt
```
{excerpt}
```

{TRAP_NOTES}

## Acceptance criteria
- [ ] Root cause identified from the run log (not just the excerpt above — pull the full log).
- [ ] Fix implemented in the worktree and the failing check reproduced locally first.
- [ ] One ready-for-review PR; required checks green at head before `kanban_request_review(reviewer="{REVIEWER}")`.
- [ ] If the real cause is infra/secrets/environment, block with `needs_input` instead of patching code.

## Before you start
- Read `{REVIEW_PATH}`, then the repo's `AGENTS.md`.
- If this duplicates an open PR already fixing the same failure, block with `needs_input` and name that PR.
"""


def create_card(run: dict, excerpt: str, klass: str) -> tuple[str, bool]:
    sha12 = run["headSha"][:12]
    wf_slug = re.sub(r"[^a-z0-9]+", "-", run["workflowName"].lower()).strip("-")
    key = f"ci-failure/{run['workflowName']}/{sha12}"
    branch = f"ci-fix/{wf_slug}-{sha12}"[:60]
    title = f"CI: {run['workflowName']} red on main at {sha12}"[:200]
    cmd = ["hermes", "kanban", "--board", BOARD, "create", "--assignee", IMPLEMENTER,
           "--idempotency-key", key, "--created-by", "ci-watchdog",
           "--workspace", "worktree", "--branch", branch,
           "--completion-contract", CONTRACT,
           "--body", build_body(run, excerpt, klass), "--json", title]
    r = sh(cmd, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"kanban create failed: {r.stderr.strip()[-300:]}")
    data = json.loads(r.stdout)
    return data.get("id") or "", int(data.get("created_at") or 0) >= RUN_START


def under_daily_cap(state: dict) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filed = state.get("filed", {})
    return filed.get(today, 0) < DAILY_CARD_CAP


def count_card(state: dict) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filed = state.setdefault("filed", {})
    filed.clear()
    filed[today] = filed.get(today, 0) + 1


def main() -> int:
    state = load_state()
    shadow = bool(state.get("shadow", True))
    seen: set[str] = set(state.get("seen", []))
    reruns: set[str] = set(state.get("reruns", []))
    first_run = "seen" not in state

    runs = recent_main_runs()
    failures = [r for r in runs if r["conclusion"] in ("failure", "timed_out")]

    report: list[str] = []
    for run in sorted(failures, key=lambda r: r["createdAt"]):
        run_id = str(run["databaseId"])
        if run_id in seen:
            continue
        seen.add(run_id)
        if not first_run and recovered(run, runs):
            continue
        if existing := card_covered(state, run["workflowName"], run["headSha"]):
            report.append(f"skip: {run['workflowName']}@{run['headSha'][:12]} already covered by {BOARD}/{existing}")
            continue

        excerpt = failed_log_excerpt(run["databaseId"])
        klass = classify(excerpt, run["workflowName"])
        label = f"{run['workflowName']} @ {run['headSha'][:12]} ({klass}) — {run['url']}"

        if klass == "flake" and run_id not in reruns:
            reruns.add(run_id)
            if shadow:
                report.append(f"SHADOW would re-run failed jobs: {label}")
            else:
                r = sh(["gh", "run", "rerun", run_id, "--repo", REPO, "--failed"], timeout=60)
                report.append(f"re-ran failed jobs ({'ok' if r.returncode == 0 else 'FAILED'}): {label}")
            continue

        if klass in ("infra", "flake"):
            report.append(f"ALERT ({klass}, needs a human): {label}\n---\n{excerpt[-1200:]}")
            continue

        if not under_daily_cap(state):
            report.append(f"ALERT: daily card cap ({DAILY_CARD_CAP}) reached, not filing: {label}")
            continue

        if shadow:
            report.append(
                f"SHADOW would file {IMPLEMENTER_LABEL} card: {label}\n---\n"
                f"{build_body(run, excerpt, klass)[:1800]}"
            )
            continue

        try:
            task_id, created = create_card(run, excerpt, klass)
        except Exception as exc:
            seen.discard(run_id)  # retry next tick
            report.append(f"{run['workflowName']}@{run['headSha'][:12]}: {exc}")
            continue
        if created:
            count_card(state)
            report.append(f"{BOARD}/{task_id} <- {label}")

    state.update({"seen": sorted(seen)[-500:], "reruns": sorted(reruns)[-200:],
                  "last_run": datetime.now(timezone.utc).isoformat()})
    save_state(state)

    if first_run:
        note = f"ci-watchdog: first run, shadow={shadow}, marked {len(seen)} run ids seen"
        print(f"{note}\n\n" + "\n\n".join(report) if report else note + ", nothing open")
    elif report:
        print("\n\n".join(report))
    return 0


def net_failure_is_quiet() -> bool:
    state = load_state()
    state["net_failures"] = state.get("net_failures", 0) + 1
    save_state(state)
    return state["net_failures"] < NET_FAILS_BEFORE_ALERT


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConnectionError, TimeoutError) as exc:
        if net_failure_is_quiet():
            sys.exit(0)
        print(f"ci-watchdog error (network, repeated): {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"ci-watchdog error: {exc}", file=sys.stderr)
        sys.exit(1)
