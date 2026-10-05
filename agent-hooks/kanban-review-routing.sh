#!/bin/sh
# Consolidated review gate: every review goes implementer -> reviewer -> operator, and an operator
# handoff is allowed only when its exact PR head has passing CI. The hook is read-only: draft
# readiness and merging remain operator actions.
#
# kanban_request_review: a reviewer is required. Other workers must name the reviewer profile.
# Only the reviewer, and only from a review run, may park a card on the operator.
# kanban_complete: refused from a review run. Before this, sdlc-review and the built-in worker
# prompt told reviewers to approve by completing; on 2026-09-24/25 implementers reviewed their own
# cards that way, 65 of them went straight to done, and their PRs stayed draft.
#
# Operator sessions (configured operator, default) are not routed.
set -eu

PAYLOAD=$(cat)
field() { printf '%s' "$PAYLOAD" | jq -r "$1 // \"\""; }
TOOL=$(field .tool_name)
case "$TOOL" in kanban_request_review|kanban_complete) ;; *) exit 0 ;; esac

REVIEWER_PROFILE="${KANBAN_REVIEWER_PROFILE:-reviewer}"
OPERATOR="${KANBAN_HUMAN_REVIEW_ASSIGNEE:-operator}"
INVESTIGATION_PROFILE="${KANBAN_INVESTIGATION_PROFILE:-investigator}"
GH="${KANBAN_GH:-gh}"
PROFILE=$(field .profile)
case "$PROFILE" in "$OPERATOR"|default) exit 0 ;; esac

block() {
  jq -nc --arg reason "$1" '{decision:"block",reason:$reason}'
  exit 2
}

# Is this worker's current run a review run? Prints review|work|unknown.
# The card is found from tool_input.task_id, then HERMES_KANBAN_TASK, then the worker's cwd
# matched to a running card's workspace; the board from HERMES_KANBAN_DB, then every board.
# No -readonly: the boards are WAL databases, and a read-only connection can't create the -shm
# file, so whenever no other process had the board open the lookup failed with SQLITE_CANTOPEN
# and every jester handoff was refused as "unknown" (t_6806464e, t_25e9a567, 2026-09-29).
run_kind() {
  task=$(field .tool_input.task_id)
  [ -n "$task" ] || task="${HERMES_KANBAN_TASK:-}"
  case "$task" in *[!A-Za-z0-9_-]*) task="" ;; esac
  cwd=$(field .cwd | sed "s/'/''/g")
  boards_dir="${KANBAN_BOARDS_DIR:-${HERMES_HOME:-$HOME/.hermes}/kanban/boards}"
  for db in ${HERMES_KANBAN_DB:-} "$boards_dir"/*/kanban.db; do
    [ -f "$db" ] || continue
    if [ -n "$task" ]; then
      found=$(sqlite3 "$db" "select id from tasks where id='$task'" 2>/dev/null || true)
    elif [ -n "$cwd" ]; then
      found=$(sqlite3 "$db" "select id from tasks where workspace_path='$cwd' and status='running' limit 1" 2>/dev/null || true)
    else
      found=""
    fi
    [ -n "$found" ] || continue
    source=$(sqlite3 "$db" "select coalesce(json_extract(e.payload,'\$.source_status'),'') from tasks t join task_events e on e.task_id=t.id and e.kind='claimed' and json_extract(e.payload,'\$.run_id')=t.current_run_id where t.id='$found' order by e.id desc limit 1" 2>/dev/null || true)
    if [ "$source" = "review" ]; then echo review; else echo work; fi
    return
  done
  echo unknown
}

UNKNOWN="could not find this card's current run (no task_id, HERMES_KANBAN_TASK or matching workspace on any board), so the review route cannot be checked. Call kanban_block(kind=\"needs_input\") and say the review-routing hook could not resolve the card."

# The operator merged the PR while the card sat in review: there is nothing left to park on
# them, and this gate refuses non-OPEN PR handoffs, so completing is the only exit.
# Without this, a card merged mid-review deadlocks (t_64f9ae32, 2026-09-26).
pr_merged() {
  pr=$(field .tool_input.metadata.published_pr)
  printf '%s' "$pr" | grep -Eq '^https://github\.com/[^/]+/[^/]+/pull/[0-9]+/?$' || return 1
  [ "$($GH pr view "$pr" --json state -q .state 2>/dev/null)" = "MERGED" ]
}

if [ "$TOOL" = "kanban_complete" ]; then
  case "$(run_kind)" in
    work) exit 0 ;;
    unknown) block "kanban_complete refused: $UNKNOWN" ;;
  esac
  pr_merged && exit 0
  if [ "$PROFILE" = "$REVIEWER_PROFILE" ]; then
    block "kanban_complete refused: this is a review run, and completing an approved card skips the operator. Approve with kanban_request_review(reviewer=\"$OPERATOR\", summary=..., metadata={\"published_pr\": \"<PR URL>\"}); send it back with kanban_request_changes; escalate with kanban_block."
  fi
  block "kanban_complete refused: this is a review run, and only $REVIEWER_PROFILE reviews cards. Hand it over with kanban_request_review(reviewer=\"$REVIEWER_PROFILE\", summary=..., metadata={\"published_pr\": \"<PR URL>\"})."
fi

REVIEWER=$(field .tool_input.reviewer)
[ -n "$REVIEWER" ] || \
  block "kanban_request_review needs an explicit reviewer. Without one the card stays assigned to you and you end up reviewing your own work. Retry the same call with reviewer=\"$REVIEWER_PROFILE\"."

[ "$PROFILE" != "$INVESTIGATION_PROFILE" ] || \
  block "kanban_request_review refused: $INVESTIGATION_PROFILE is a read-only investigation lane and never owns a PR. Attach the findings and call kanban_complete; route implementation or durable documentation to the implementer."

if [ "$PROFILE" != "$REVIEWER_PROFILE" ]; then
  [ "$REVIEWER" = "$REVIEWER_PROFILE" ] || \
    block "kanban_request_review refused: reviewer=\"$REVIEWER\" is not allowed from $PROFILE. Every card is reviewed by $REVIEWER_PROFILE before the operator sees it; retry with reviewer=\"$REVIEWER_PROFILE\"."
  exit 0
fi

[ "$REVIEWER" = "$OPERATOR" ] || \
  block "kanban_request_review refused: $REVIEWER_PROFILE approves by parking the card on the operator. Retry with reviewer=\"$OPERATOR\"; send work back with kanban_request_changes."
case "$(run_kind)" in
  review) ;;
  work) block "kanban_request_review refused: $REVIEWER_PROFILE can only park a card it reviewed, and this run implemented it. Call kanban_block(kind=\"needs_input\") and ask the operator who should review it." ;;
  *) block "kanban_request_review refused: $UNKNOWN" ;;
esac

# This is now a valid reviewer -> operator handoff. No-code cards need an explicit receipt;
# code cards need an exact PR URL and passing checks at its current head. Skipped checks count
# as passing, matching GitHub's treatment of optional jobs. Nothing here mutates GitHub.
PR=$(field .tool_input.metadata.published_pr)
if [ -z "$PR" ]; then
  [ "$(field .tool_input.metadata.no_pr)" = "true" ] && exit 0
  block "Human-review handoff blocked: metadata.published_pr is required. Retry kanban_request_review(reviewer=\"$OPERATOR\", metadata={\"published_pr\":\"https://github.com/OWNER/REPO/pull/N\"}) after CI passes. If the card changed no code and has no PR, pass metadata={\"no_pr\": true} instead."
fi
printf '%s' "$PR" | grep -Eq '^https://github\.com/[^/]+/[^/]+/pull/[0-9]+/?$' || \
  block "Human-review handoff blocked: metadata.published_pr must be an exact GitHub pull-request URL."

PR_JSON=$($GH pr view "$PR" --json state,headRefOid 2>/dev/null) || \
  block "Human-review handoff blocked: could not read $PR with gh. Verify authentication and PR URL, then retry."
STATE=$(printf '%s' "$PR_JSON" | jq -r '.state // ""')
HEAD=$(printf '%s' "$PR_JSON" | jq -r '.headRefOid // ""')
[ "$STATE" = "OPEN" ] || block "Human-review handoff blocked: $PR is $STATE, not OPEN."
[ -n "$HEAD" ] || block "Human-review handoff blocked: could not resolve current PR head SHA for $PR."

CHECKS=$($GH pr checks "$PR" --json name,state,bucket,workflow,link 2>/dev/null) || \
  block "Human-review handoff blocked: could not read CI checks for $PR at ${HEAD%????????????????????????????????}."
COUNT=$(printf '%s' "$CHECKS" | jq 'length')
[ "$COUNT" -gt 0 ] || block "Human-review handoff blocked: no CI checks are reported for $PR at ${HEAD%????????????????????????????????}. Wait for checks to start, then retry."

NOT_PASSING=$(printf '%s' "$CHECKS" | jq -r '[.[] | select(.bucket != "pass" and .bucket != "skipping") | ((.workflow // "CI") + " / " + .name + " (" + (.state // .bucket // "unknown") + ")")] | join(", ")')
[ -z "$NOT_PASSING" ] || \
  block "Human-review handoff blocked: CI is not green for $PR at ${HEAD%????????????????????????????????}: $NOT_PASSING. Fix failures or wait for pending checks, then retry."

exit 0
