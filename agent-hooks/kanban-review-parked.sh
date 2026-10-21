#!/bin/sh
# Hermes shell hook: on_kanban_dispatch_tick
#
# Notify the operator on Discord when a kanban card parks in `review` assigned to
# the configured human parking assignee. Fires once per dispatcher tick (~20-60s);
# a cursor file makes each card notify exactly once per park (a card sent back and
# re-parked notifies again because its review_requested event id advances).
#
# stdin: hook JSON payload (unused beyond being drained).
# Dependencies: sqlite3, jq, hermes.
set -eu

SETTINGS="$(CDPATH= cd -- "$(dirname -- "$0")/../scripts" && pwd)/deployment_settings.py"
# Read each configured value independently; never evaluate generated shell code.
CONFIG_HOME=$(python3 "$SETTINGS" review-notifications home)
CONFIG_BOARD=$(python3 "$SETTINGS" review-notifications board)
CONFIG_ASSIGNEE=$(python3 "$SETTINGS" review-notifications parking_assignee)
CONFIG_TARGET=$(python3 "$SETTINGS" review-notifications target)
SEND_TIMEOUT=$(python3 "$SETTINGS" review-notifications send_timeout)
MAX_SENDS=$(python3 "$SETTINGS" review-notifications max_sends)
BOARD="${KANBAN_REVIEW_BOARD:-$CONFIG_BOARD}"
PARKING_ASSIGNEE="${KANBAN_REVIEW_PARKING_ASSIGNEE:-$CONFIG_ASSIGNEE}"
TARGET="${KANBAN_REVIEW_NOTIFY_TARGET:-$CONFIG_TARGET}"
set --
if [ -n "${KANBAN_REVIEW_NOTIFY_PROFILE:-}" ]; then
  set -- -p "$KANBAN_REVIEW_NOTIFY_PROFILE"
fi
HOME_DIR="${HERMES_HOME:-$CONFIG_HOME}"
export HERMES_HOME="$HOME_DIR"
DB="$HOME_DIR/kanban/boards/$BOARD/kanban.db"
STATE="$HOME_DIR/agent-hooks/state/kanban-review-parked.$BOARD.cursor"

cat >/dev/null || true   # drain stdin
[ -r "$DB" ] || exit 0
mkdir -p "$(dirname "$STATE")"
[ -f "$STATE" ] || printf '0' >"$STATE"
CURSOR=$(cat "$STATE")
case "$CURSOR" in ''|*[!0-9]*) CURSOR=0 ;; esac

# The latest review_requested event per task currently parked on the human assignee,
# newer than our cursor. payload.reviewer is the handoff target; when the AI reviewer parks a
# card it hands to the parking assignee, which is the "waiting on operator" signal.
ROWS=$(sqlite3 -json -cmd '.timeout 2000' "$DB" "
  SELECT e.id AS event_id, t.id AS task_id, t.title,
         json_extract(e.payload, '$.implementer') AS implementer,
         json_extract(e.payload, '$.summary') AS summary
  FROM tasks t
  JOIN task_events e ON e.task_id = t.id
  WHERE t.status = 'review'
    AND t.assignee = '$PARKING_ASSIGNEE'
    AND e.kind = 'review_requested'
    AND json_extract(e.payload, '$.reviewer') = '$PARKING_ASSIGNEE'
    AND e.id > $CURSOR
    AND e.id = (SELECT MAX(e2.id) FROM task_events e2
                WHERE e2.task_id = t.id AND e2.kind = 'review_requested')
  ORDER BY e.id ASC;
")
[ -n "$ROWS" ] && [ "$ROWS" != "[]" ] || exit 0

MAX_ID="$CURSOR"
# Hermes runs this hook inside the dispatcher tick and waits for it, so a hung Discord
# delivery stalls dispatch (and a shutting-down gateway, whose send path is already gone).
# Bound each send, cap sends per tick, and on a failed send stop without moving the cursor:
# the alert goes out on a later tick instead of being skipped.
SENT=0
echo "$ROWS" | jq -c '.[]' | while IFS= read -r row; do
  [ "$SENT" -lt "$MAX_SENDS" ] || break
  eid=$(echo "$row" | jq -r '.event_id')
  tid=$(echo "$row" | jq -r '.task_id')
  title=$(echo "$row" | jq -r '.title // ""')
  impl=$(echo "$row" | jq -r '.implementer // "?"')
  summary=$(echo "$row" | jq -r '(.summary // "") | .[0:400]')
  # PR URL lives in the approving run's metadata.published_pr; card text uses #N only.
  pr=$(sqlite3 -cmd '.timeout 2000' "$DB" "SELECT json_extract(metadata,'$.published_pr') FROM task_runs
        WHERE task_id='$tid' AND metadata IS NOT NULL AND json_extract(metadata,'$.published_pr') IS NOT NULL
        ORDER BY id DESC LIMIT 1;")
  msg="$title
card $tid · implementer $impl · approved by AI review, parked for you"
  [ -n "$pr" ] && msg="$msg
$pr"
  [ -n "$summary" ] && msg="$msg

$summary"
  # perl's alarm, not coreutils timeout: the gateway's PATH is not guaranteed to have it.
  /usr/bin/perl -e 'alarm shift; exec @ARGV' "$SEND_TIMEOUT" \
    hermes "$@" send -q -t "$TARGET" -s "🟣 Ready for your review" "$msg" >/dev/null 2>&1 || break
  SENT=$((SENT + 1))
  [ "$eid" -gt "$MAX_ID" ] && MAX_ID="$eid"
  printf '%s' "$MAX_ID" >"$STATE"
done
exit 0
