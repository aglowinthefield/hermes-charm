#!/bin/sh
# Decision-table test for kanban-review-routing.sh. Run: sh test-kanban-review-routing.sh
H="$(dirname "$0")/kanban-review-routing.sh"
export KANBAN_REVIEWER_PROFILE=jester KANBAN_HUMAN_REVIEW_ASSIGNEE=tom KANBAN_INVESTIGATION_PROFILE=page
fail=0
DB=$(mktemp -d)/kanban.db
trap 'rm -rf "$(dirname "$DB")"' EXIT
export KANBAN_BOARDS_DIR="$(dirname "$DB")/empty-boards"
sqlite3 "$DB" "
create table tasks (id text primary key, status text, workspace_path text, current_run_id integer);
create table task_events (id integer primary key autoincrement, task_id text, run_id integer, kind text, payload text, created_at integer);
insert into tasks values ('t_work', 'running', '/wt/work', 11), ('t_rev', 'running', '/wt/rev', 22);
insert into task_events (task_id, kind, payload, created_at) values
  ('t_work', 'claimed', '{\"run_id\": 11}', 0),
  ('t_rev', 'claimed', '{\"run_id\": 21}', 0),
  ('t_rev', 'claimed', '{\"run_id\": 22, \"source_status\": \"review\"}', 0);"
export HERMES_KANBAN_DB="$DB"

t() { # $1 expect $2 tool $3 profile $4 reviewer $5 task $6 cwd $7 no_pr
  out=$(jq -n --arg tool "$2" --arg p "$3" --arg r "$4" --arg cwd "$6" --arg no_pr "$7" \
    '{tool_name:$tool,profile:$p,cwd:$cwd,tool_input:((if $r=="" then {} else {reviewer:$r} end) + (if $no_pr=="true" then {metadata:{no_pr:true}} else {} end))}' \
    | HERMES_KANBAN_TASK="$5" sh "$H" 2>/dev/null); rc=$?
  got=allow; [ $rc -eq 2 ] && got=block
  [ $rc -ne 0 ] && [ $rc -ne 2 ] && got="error($rc)"
  label="$2 $3 reviewer=${4:--} task=${5:--} cwd=${6:--}"
  if [ "$got" = "$1" ]; then printf 'ok    %-5s %s\n' "$1" "$label"; else printf 'FAIL  want %s got %s  %s\n' "$1" "$got" "$label"; fail=1; fi
}

# Implementers hand to jester, and only to jester.
t allow kanban_request_review scribe jester t_work
t block kanban_request_review scribe ""     t_work
t block kanban_request_review scribe scribe t_work
t block kanban_request_review scribe tom    t_work
t block kanban_request_review page   page   t_work
t block kanban_request_review page   jester t_work
t allow kanban_request_review princess jester t_work
t block kanban_request_review princess tom  t_work
# jester parks on tom only from a review run; no-code cards carry an explicit receipt.
t allow kanban_request_review jester tom    t_rev "" true
t allow kanban_request_review jester tom    ""     /wt/rev true
t block kanban_request_review jester tom    t_rev
t block kanban_request_review jester tom    t_work
t block kanban_request_review jester jester t_rev
t block kanban_request_review jester tom    ""
# Completing a review run skips the operator; completing work (research cards) is fine.
t block kanban_complete jester ""  t_rev
t block kanban_complete scribe ""  t_rev
t block kanban_complete scribe ""  ""     /wt/rev
t allow kanban_complete page   ""  t_work
t allow kanban_complete scribe ""  ""     /wt/work
t block kanban_complete scribe ""  t_missing
# Operator sessions and other tools pass through.
t allow kanban_complete tom     ""  t_rev
t allow kanban_request_review default tom t_work
t allow kanban_create scribe "" t_rev

# Mock GitHub for merged-card completion and exact-head CI handoff cases.
GH=$(dirname "$DB")/gh
GH_LOG=$(dirname "$DB")/gh.log
cat > "$GH" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$GH_LOG"
if [ "$1" = pr ] && [ "$2" = view ]; then
  case "$*" in
    *"--json state -q .state"*)
      case "$3" in */1) echo MERGED ;; */2) echo OPEN ;; */3) echo CLOSED ;; *) exit 1 ;; esac
      ;;
    *)
      case "$3" in
        */4|*/5|*/6|*/11) echo '{"state":"OPEN","isDraft":true,"headRefOid":"4444444444444444444444444444444444444444"}' ;;
        */10) echo '{"state":"OPEN","isDraft":false,"headRefOid":"4444444444444444444444444444444444444444"}' ;;
        */o/auto/pull/12|*/o/auto/pull/15|*/o/held/pull/16) echo '{"state":"OPEN","isDraft":false,"headRefOid":"4444444444444444444444444444444444444444","changedFiles":2,"files":[{"path":"src/a.ts"},{"path":"drizzle.config.ts"}]}' ;;
        */o/auto/pull/13) echo '{"state":"OPEN","isDraft":false,"headRefOid":"4444444444444444444444444444444444444444","changedFiles":2,"files":[{"path":"src/a.ts"},{"path":"drizzle/0001.sql"}]}' ;;
        */o/auto/pull/14) echo '{"state":"OPEN","isDraft":false,"headRefOid":"4444444444444444444444444444444444444444","changedFiles":101,"files":[{"path":"src/a.ts"}]}' ;;
        */7) echo '{"state":"CLOSED","headRefOid":"7777777777777777777777777777777777777777"}' ;;
        *) exit 1 ;;
      esac
      ;;
  esac
elif [ "$1" = pr ] && [ "$2" = checks ]; then
  case "$3" in
    */4|*/10|*/11|*/1[2-6]) echo '[{"name":"test","state":"SUCCESS","bucket":"pass","workflow":"CI","link":"https://ci/4"}]' ;;
    */5) echo '[{"name":"test","state":"PENDING","bucket":"pending","workflow":"CI","link":"https://ci/5"}]' ;;
    */6) echo '[]' ;;
    *) exit 1 ;;
  esac
elif [ "$1" = pr ] && [ "$2" = ready ]; then
  case "$3" in */11) exit 1 ;; esac
elif [ "$1" = pr ] && [ "$2" = merge ]; then
  case "$3" in */15) echo 'auto-merge is not allowed for this repository' >&2; exit 1 ;; esac
elif [ "$1" = pr ] && [ "$2" = comment ]; then
  :
else
  exit 99
fi
SH
chmod +x "$GH"
export KANBAN_GH="$GH" GH_LOG
# A PR the operator merged mid-review may be completed from the review run; nothing else may.
m() { # $1 expect $2 profile $3 published_pr
  out=$(jq -n --arg p "$2" --arg pr "$3" \
    '{tool_name:"kanban_complete",profile:$p,tool_input:{task_id:"t_rev",metadata:(if $pr=="" then {} else {published_pr:$pr} end)}}' \
    | sh "$H" 2>/dev/null); rc=$?
  got=allow; [ $rc -eq 2 ] && got=block
  [ $rc -ne 0 ] && [ $rc -ne 2 ] && got="error($rc)"
  label="kanban_complete $2 review-run pr=${3:--}"
  if [ "$got" = "$1" ]; then printf 'ok    %-5s %s\n' "$1" "$label"; else printf 'FAIL  want %s got %s  %s\n' "$1" "$got" "$label"; fail=1; fi
}
m allow jester https://github.com/o/r/pull/1
m allow scribe https://github.com/o/r/pull/1
m block jester https://github.com/o/r/pull/2
m block jester https://github.com/o/r/pull/3
m block jester https://github.com/o/r/pull/9
m block jester "not-a-url/pull/1"
m block jester ""

POLICY=$(dirname "$DB")/automerge.json
echo '{"o/auto":{"method":"squash","hold_paths":["drizzle/","fly.toml"]},"o/held":{"method":"fast-forward","hold_paths":[]}}' > "$POLICY"
export KANBAN_AUTOMERGE_POLICY="$POLICY"
h() { # $1 expect $2 published_pr
  out=$(jq -n --arg pr "$2" \
    '{tool_name:"kanban_request_review",profile:"jester",tool_input:{task_id:"t_rev",reviewer:"tom",metadata:{published_pr:$pr}}}' \
    | sh "$H" 2>/dev/null); rc=$?
  got=allow; [ $rc -eq 2 ] && got=block
  [ $rc -ne 0 ] && [ $rc -ne 2 ] && got="error($rc)"
  label="jester -> tom pr=$2"
  if [ "$got" = "$1" ]; then printf 'ok    %-5s %s\n' "$1" "$label"; else printf 'FAIL  want %s got %s  %s\n' "$1" "$got" "$label"; fail=1; fi
}
# Blocked handoffs never touch GitHub.
h block https://github.com/o/r/pull/5
h block https://github.com/o/r/pull/6
h block https://github.com/o/r/pull/7
h block https://github.com/o/r/pull/8
if grep -q 'pr ready' "$GH_LOG"; then
  printf 'FAIL  blocked handoff marked a PR ready\n'; fail=1
else
  printf 'ok    blocked handoffs leave GitHub alone\n'
fi
# Drafts are refused without a readiness mutation; ready PRs preserve the existing CI gate.
h block https://github.com/o/r/pull/4
h allow https://github.com/o/r/pull/10
h block https://github.com/o/r/pull/11
if grep -q 'pr ready' "$GH_LOG"; then
  printf 'FAIL  review gate mutated readiness\n'; fail=1
else
  printf 'ok    review gate never mutates readiness\n'
fi
if grep -q 'pr ready https://github.com/o/r/pull/10' "$GH_LOG"; then
  printf 'FAIL  already-ready PR was marked again\n'; fail=1
else
  printf 'ok    already-ready PR left alone\n'
fi
if grep -E ' api |pr merge|pr comment|--undo' "$GH_LOG" | grep -vq '/o/'; then
  printf 'FAIL  review gate made an unexpected GitHub write\n'; fail=1
else
  printf 'ok    repos without a policy get no GitHub writes\n'
fi

# Auto-merge: only repos in the operator's policy file, only when no held path changed and the
# file list is complete, always pinned to the reviewed head. Doubt leaves the PR for the operator.
lg() { if grep -qxF -- "$2" "$GH_LOG"; then printf 'ok    %s\n' "$1"; else printf 'FAIL  %s: missing %s\n' "$1" "$2"; fail=1; fi; }
nolg() { if grep -qF -- "$2" "$GH_LOG"; then printf 'FAIL  %s: found %s\n' "$1" "$2"; fail=1; else printf 'ok    %s\n' "$1"; fi; }
h allow https://github.com/o/auto/pull/12
lg "policy repo merges with its method at the reviewed head" "pr merge https://github.com/o/auto/pull/12 --auto --squash --match-head-commit 4444444444444444444444444444444444444444"
h allow https://github.com/o/auto/pull/13
nolg "held path leaves the PR for the operator" "pr merge https://github.com/o/auto/pull/13"
lg "held path is explained on the PR" "pr comment https://github.com/o/auto/pull/13 --body Auto-merge held for the operator: changes drizzle/0001.sql."
h allow https://github.com/o/auto/pull/14
nolg "truncated file list never merges" "pr merge https://github.com/o/auto/pull/14"
h allow https://github.com/o/auto/pull/15
lg "failed merge is explained on the PR" "pr comment https://github.com/o/auto/pull/15 --body Auto-merge was not enabled, so the operator merges this PR: auto-merge is not allowed for this repository"
h block https://github.com/o/held/pull/16
nolg "unknown merge method refuses before any write" "pr merge https://github.com/o/held/pull/16"
echo '{not json' > "$POLICY"
h block https://github.com/o/auto/pull/12
rm "$POLICY"
h block https://github.com/o/r/pull/4

rm -rf "$(dirname "$DB")"
exit $fail
