#!/bin/sh
# Decision-table test for pr-body-humanized.py. Run: sh test-pr-body-humanized.sh
H="$(dirname "$0")/pr-body-humanized.py"
D="$(mktemp -d)"
trap 'rm -rf "$D"' EXIT
fail=0
t() { # $1 expect(allow|block) $2 command
  out=$(jq -n --arg c "$2" --arg cwd "$D" '{hook_event_name:"pre_tool_call",tool_name:"terminal",cwd:$cwd,tool_input:{command:$c}}' | python3 "$H" 2>/dev/null); rc=$?
  got=allow; [ $rc -eq 2 ] && got=block
  [ $rc -ne 0 ] && [ $rc -ne 2 ] && got="error($rc)"
  if [ "$got" = "$1" ]; then printf 'ok    %-5s  %s\n' "$1" "$(printf '%s' "$2" | head -1)"; else printf 'FAIL  want %s got %s  %s\n' "$1" "$got" "$2"; fail=1; fi
}
# reason() prints what the agent sees for a command
reason() { jq -n --arg c "$1" --arg cwd "$D" '{tool_input:{command:$c},cwd:$cwd}' | python3 "$H" 2>/dev/null | jq -r .reason; }
GH="gh pr"   # assembled so this file never contains the literal trigger
API="gh api"
EM=$(printf '\342\200\224')     # em dash
printf 'Summary\n\n- Adds the `x` flag.\n' > "$D/clean.md"
printf 'Summary\n\nThis is a pivotal change %s really.\n' "$EM" > "$D/dirty.md"
printf '{"title":"t","body":"Fixes the parser."}' > "$D/clean.json"
printf '{"title":"t","body":"I delve into it."}' > "$D/dirty.json"

# not a PR body
t allow 'git status'
t allow "$GH view 1844"
t allow "$GH edit 1844 --title \"x\""
t allow "$GH edit 1844 --add-label perf"
t allow "$GH comment 1844 --body 'I think this is pivotal'"
t allow "$GH create --title t"
t allow "echo hello | ghpr create --body x"
t allow "echo \"$GH create --body 'I delve'\""
t allow "grep -n '$GH create --body' foo.sh"
t allow "printf '%s' '{\"c\":\"$GH create --title t --body I\"}' > /tmp/x.json"
t allow "git commit -m \"\$(printf 'see $GH create --body')\""
t allow "$API repos/o/r/pulls"
t allow "$API repos/o/r/pulls/12/comments -f body='I delve'"
t allow "$API repos/o/r/issues/12/comments -f body='I delve'"
t allow "$API graphql -f query='{ viewer { login } }'"

# clean bodies pass, wherever the command puts them
t allow "$GH create --draft --title \"t\" --body \"Fixes the parser.\""
t allow "$GH create --title t -b \"Fixes the parser.\""
t allow "$GH edit 1844 --body=\"Fixes the parser.\""
t allow "$GH edit 1844 --body-file clean.md"
t allow "$GH edit 1844 --body-file $D/clean.md"
t allow "cd /x && $GH create --title t --body-file - <<'EOF'
it's a body with an I/O fix and \`my_var\` in code
EOF"
t allow "$GH create --title t --body \"\$(cat <<'EOF'
Summary

- Fixes the parser.
EOF
)\""
t allow "$GH create --body \"\$(cat clean.md)\""
t allow "git push && $GH create --title t --body x"
t allow "sh -c '$GH create --body x'"
t allow "url=\$($GH create --title t --body x)"
t allow "$API repos/o/r/pulls -f title=x -f body=y -f head=b -f base=main"
t allow "$API -X PATCH repos/o/r/pulls/12 -F body=@clean.md"
t allow "$API repos/o/r/pulls --input clean.json"
t allow "$API graphql -f query='mutation { updatePullRequest(input:{pullRequestId:\"x\",body:\"Fixes it.\"}) { clientMutationId } }'"
t allow "$GH create --body '> I said this in the issue'"

# dirty bodies are blocked, in every position the detector knows
t block "$GH create --draft --title \"t\" --body \"I fixed it.\""
t block "$GH create --title t -b \"a pivotal change\""
t block "$GH edit 1844 --body-file dirty.md"
t block "$GH edit 1844 -F $D/dirty.md"
t block "cd /x && $GH create --title t --body-file - <<'EOF'
it's a body $EM with a dash
EOF"
t block "$GH create --title t --body \"\$(cat <<'EOF'
Additionally, this helps.
EOF
)\""
t block "$GH create --body \"\$(cat dirty.md)\""
t block "$GH edit 1844 --body=\"In order to fix it\""
t block "git push && $GH create --title t --body 'I did it'"
t block "  $GH create --title t --body 'my change'"
t block "command $GH create --body 'This serves as a fix'"
t block "env $GH create --body 'Not just faster, but safer'"
t block "env -u FOO BAR=1 $GH create --body 'I hope this helps'"
t block "/usr/bin/$GH create --body 'We leverage it'"
t block "exec $GH edit 1 --body 'a seamless fix'"
t block "timeout 30 $GH create --body 'I did'"
t block "sh -c '$GH create --body \"I did\"'"
t block "bash -lc \"cd /x && $GH create --body 'my fix'\""
t block "eval '$GH create --body \"my fix\"'"
t block "url=\$($GH create --title t --body 'my fix')"
t block "echo \"\$($GH create --title t --body 'my fix')\""
t block "$API repos/o/r/pulls -f title=x -f body='I did' -f head=b -f base=main"
t block "$API -X PATCH repos/o/r/pulls/12 -F body=@dirty.md"
t block "$API repos/o/r/pulls --input dirty.json"
t block "$API --method PATCH /repos/o/r/pulls/12 --raw-field=body='I did'"
t block "$API graphql -f query='mutation { updatePullRequest(input:{pullRequestId:\"x\",body:\"I did\"}) { clientMutationId } }'"

# bodies the hook cannot see are blocked with a request for --body-file
t block "$GH edit 1844 -F -"
t block "$GH create --fill"
t block "$GH create --fill-first --draft"
t block "$GH create -T bug_report.md"
t block "$GH create --body \"\$BODY\""
t block "$GH edit 1844 --body-file missing.md"
t block "$API repos/o/r/pulls --input missing.json"

# the old marker is a claim, not a bypass
t block "HERMES_HUMANIZED=1 $GH create --title t --body 'I did it'"
t block "HERMES_HUMANIZED=1 $GH create --fill"
t allow "HERMES_HUMANIZED=1 $GH create --title t --body 'Fixes it.'"

# the reason names the line, the rule and the skill
r=$(reason "$GH create --body-file dirty.md")
case "$r" in *"line 3: em dash"*"line 3: AI vocabulary \"pivotal\""*"unslop/SKILL.md"*) echo "ok    reason lists hits by line" ;; *) echo "FAIL  reason: $r"; fail=1 ;; esac
r=$(reason "$GH create --fill")
case "$r" in *"--body-file"*) echo "ok    reason asks for --body-file" ;; *) echo "FAIL  reason: $r"; fail=1 ;; esac

[ $fail -eq 0 ] && echo "ALL PASS" || { echo "FAILURES"; exit 1; }
