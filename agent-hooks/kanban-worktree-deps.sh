#!/bin/sh
# Hermes shell hook: on_kanban_worker_spawned
#
# Populate node_modules in a freshly created kanban worktree so the repo's
# pre-commit hook (which fails closed without lint-staged) can run and the
# worker's first `npm run …` isn't a several-minute install detour.
#
# Delegates to the repo's own scripts/worktree-deps.sh when present (APFS
# clone of the root node_modules, ~25 s, falls back to npm ci when the lockfile
# differs). Repos without that script are left alone. Runs in the background
# so it never holds the dispatcher lock; the worker's own commands will simply
# find node_modules already there by the time it has read the card.
#
# stdin: hook JSON; workspace_path is under .extra.
set -eu

payload=$(cat)
ws=$(printf '%s' "$payload" | jq -r '.extra.workspace_path // empty')
[ -n "$ws" ] && [ -d "$ws" ] || exit 0
[ -x "$ws/scripts/worktree-deps.sh" ] || exit 0
[ -e "$ws/node_modules/.bin/lint-staged" ] && exit 0

LOG="${KANBAN_WORKTREE_DEPS_LOG:-${HERMES_HOME:-$HOME/.hermes}/logs/kanban-worktree-deps.log}"
LOG_MAX_BYTES="${KANBAN_WORKTREE_DEPS_LOG_MAX_BYTES:-2097152}"
mkdir -p "$(dirname "$LOG")"
case "$LOG_MAX_BYTES" in ''|*[!0-9]*) LOG_MAX_BYTES=2097152 ;; esac
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -ge "$LOG_MAX_BYTES" ]; then
  mv -f "$LOG" "$LOG.1"
fi
(
  cd "$ws"
  # A pinned Node is mandatory when the repo declares one; native addons (re2)
  # built under the wrong major fail at require time, not install time.
  if [ -f .nvmrc ] || [ -f .node-version ]; then
    FNM_BIN="${KANBAN_FNM_COMMAND:-fnm}"
    command -v "$FNM_BIN" >/dev/null 2>&1 || {
      printf 'worktree-deps: fnm is required to select the repository-pinned Node version for %s\n' "$ws"
      exit 1
    }
    FNM_ENV=$("$FNM_BIN" env --shell bash) || {
      printf 'worktree-deps: fnm environment setup failed for %s\n' "$ws"
      exit 1
    }
    eval "$FNM_ENV"
    "$FNM_BIN" use --silent-if-unchanged >/dev/null 2>&1 || {
      printf 'worktree-deps: could not select the repository-pinned Node version for %s\n' "$ws"
      exit 1
    }
  fi
  printf '%s %s start\n' "$(date +%FT%T)" "$ws"
  ./scripts/worktree-deps.sh "$ws"
  printf '%s %s done (%s)\n' "$(date +%FT%T)" "$ws" "$(node -v 2>/dev/null || echo node?)"
) >>"$LOG" 2>&1 &
exit 0
