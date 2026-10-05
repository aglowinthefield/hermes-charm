#!/bin/sh
# Regression coverage for kanban-worktree-deps.sh. Run: sh test-kanban-worktree-deps.sh
set -eu
H="$(dirname "$0")/kanban-worktree-deps.sh"
DIR=$(mktemp -d)
trap 'rm -rf "$DIR"' EXIT
WS="$DIR/worktree"
BIN="$DIR/bin"
LOG="$DIR/worktree-deps.log"
MARK="$DIR/bootstrap-ran"
mkdir -p "$WS/scripts" "$BIN"
printf '24\n' >"$WS/.nvmrc"

cat >"$BIN/fnm" <<'EOF'
#!/bin/sh
printf '%s\n' "$*" >>"$FNM_CALLS"
case "${1:-}" in
  env)
    [ "${2:-}" = "--shell" ] && [ "${3:-}" = "bash" ] || exit 3
    exit 0
    ;;
  use)
    [ "${FNM_FAIL:-0}" = 1 ] && exit 4
    exit 0
    ;;
  *) exit 5 ;;
esac
EOF
cat >"$BIN/node" <<'EOF'
#!/bin/sh
printf 'v24.12.0\n'
EOF
cat >"$WS/scripts/worktree-deps.sh" <<'EOF'
#!/bin/sh
node --version >"$NODE_MARK"
printf '%s\n' "$1" >"$MARK"
EOF
chmod +x "$BIN/fnm" "$BIN/node" "$WS/scripts/worktree-deps.sh"

export PATH="$BIN:$PATH" FNM_CALLS="$DIR/fnm-calls" NODE_MARK="$DIR/node-version" MARK
payload=$(printf '{"extra":{"workspace_path":"%s"}}' "$WS")
printf '%128s' 'old log' >"$LOG"
printf '%s' "$payload" | KANBAN_WORKTREE_DEPS_LOG="$LOG" KANBAN_WORKTREE_DEPS_LOG_MAX_BYTES=64 sh "$H"

i=0
while [ ! -f "$MARK" ] && [ "$i" -lt 100 ]; do sleep 0.02; i=$((i + 1)); done
[ -f "$MARK" ] || { echo 'FAIL bootstrap did not run' >&2; exit 1; }
[ -f "$LOG.1" ] || { echo 'FAIL oversized log was not rotated' >&2; exit 1; }
grep -qx 'env --shell bash' "$FNM_CALLS"
grep -qx 'use --silent-if-unchanged' "$FNM_CALLS"
grep -qx 'v24.12.0' "$NODE_MARK"
i=0
while ! grep -q 'done (v24.12.0)' "$LOG" 2>/dev/null && [ "$i" -lt 100 ]; do sleep 0.02; i=$((i + 1)); done
grep -q 'done (v24.12.0)' "$LOG"
echo 'ok    pinned Node is selected and oversized logs rotate'

rm -f "$MARK"
: >"$FNM_CALLS"
printf '%s' "$payload" | FNM_FAIL=1 KANBAN_WORKTREE_DEPS_LOG="$LOG" sh "$H"
i=0
while ! grep -q 'could not select the repository-pinned Node version' "$LOG" 2>/dev/null && [ "$i" -lt 100 ]; do sleep 0.02; i=$((i + 1)); done
grep -q 'could not select the repository-pinned Node version' "$LOG"
[ ! -f "$MARK" ] || { echo 'FAIL bootstrap ran after fnm selection failed' >&2; exit 1; }
echo 'ok    bootstrap stops instead of using the wrong Node version'

rm -f "$MARK"
: >"$LOG"
printf '%s' "$payload" | KANBAN_FNM_COMMAND=missing-fnm KANBAN_WORKTREE_DEPS_LOG="$LOG" sh "$H"
i=0
while ! grep -q 'fnm is required' "$LOG" 2>/dev/null && [ "$i" -lt 100 ]; do sleep 0.02; i=$((i + 1)); done
grep -q 'fnm is required' "$LOG"
[ ! -f "$MARK" ] || { echo 'FAIL bootstrap ran without fnm for a pinned repository' >&2; exit 1; }
echo 'ok    pinned repositories stop when fnm is unavailable'
