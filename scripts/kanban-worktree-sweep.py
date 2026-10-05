#!/usr/bin/env python3
"""Reclaim disk from finished and parked kanban worktrees, and warn when disk runs low.

Hermes creates `<repo>/.worktrees/<task-id>` for every worktree card and never removes it, and
agent-hooks/kanban-worktree-deps.sh gives each one its own node_modules. On 2026-09-23 that
added up to 190 worktrees and a 99%-full disk, and `git worktree add` began timing out at 60s,
which the dispatcher counts as a spawn failure. Run by `hermes cron` with --no-agent, so it
costs no inference tokens.

Each tick, for every board with a `default_workdir` repo:
  1. List all of that repo's registered worktrees directly under `<repo>/.worktrees`.
     A directory with no matching card is reported but never changed: without a card record,
     the sweeper cannot distinguish abandoned agent work from a human's manual worktree.
  2. Remove one only when ALL hold:
       - its card is `done` or `archived`, and was completed/archived GRACE_SECONDS ago or more,
       - no live worker pid is recorded on the card,
       - `git status --porcelain` shows nothing except deletions of tracked files (" D "):
         no modified file, no staged change, no untracked file.
     Branches and commits are never touched, so any removed tree can be recreated with
     `git worktree add .worktrees/<id> <branch>`.
     Removal is a rename into ~/.hermes/kanban/worktree-trash on the same volume (instant),
     `git worktree prune`, and a detached `rm -rf` of the trash, because deleting a 3 GB
     node_modules takes minutes and must not hold up the cron tick.
  3. For a `review` or `blocked` card idle for PARKED_IDLE_SECONDS with no live worker,
     reclaim only its root `node_modules`. Parked worktrees often contain unpushed commits or
     uncommitted files, while node_modules is gitignored and reproducible; removing the checkout
     would risk real work, but removing this dependency copy cannot alter git state. The directory
     is renamed into the same trash used above and deleted asynchronously.
  4. A finished card whose tree still holds uncommitted content is kept. It gets one comment
     naming the paths, so the content is visible on the card instead of silently kept on disk,
     and every tick reports the retained tree and its size even after that comment exists.
  5. When free space is below LOW_DISK_GIB, notify once (latched until it recovers).
     No hook can veto a spawn, so this warns; it does not stop the dispatcher.

Silent when nothing changed; cron delivers whatever reaches stdout.
Flags: --dry-run prints what would happen and changes nothing.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
BOARDS = Path(os.environ.get("KANBAN_BOARDS_DIR") or HOME / "kanban" / "boards")
TRASH = HOME / "kanban" / "worktree-trash"
LATCH = HOME / "kanban-worktree-sweep-latch.json"
MARK = "[worktree sweep]"
NOTIFIER = "/Applications/terminal-notifier.app/Contents/MacOS/terminal-notifier"
GRACE_SECONDS = 5 * 60  # was 30m; tightened 23 Sep 2026 for overnight 4-wide dispatch
PARKED_IDLE_SECONDS = 6 * 60 * 60
LOW_DISK_GIB = int(os.environ.get("WORKTREE_SWEEP_LOW_DISK_GIB", "25"))
TERMINAL = ("done", "archived")
PARKED = ("review", "blocked")


def notify(subtitle: str, body: str, group: str) -> None:
    if os.access(NOTIFIER, os.X_OK):
        subprocess.run([NOTIFIER, "-title", "Hermes Kanban", "-subtitle", subtitle, "-message", body,
                        "-group", group, "-sound", "default"], capture_output=True, timeout=5, check=False)
    elif shutil.which("osascript"):  # macOS fallback; no desktop to notify on Linux
        msg = body.replace('"', "'")
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Hermes Kanban" subtitle "{subtitle}"'],
                       capture_output=True, timeout=5, check=False)


def git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True


def registered_worktrees(repo: Path) -> list[Path]:
    r = git(repo, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return []
    root = (repo / ".worktrees").resolve()
    paths = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line[len("worktree "):])
            if p.parent == root:
                paths.append(p)
    return paths


def finished_at(conn: sqlite3.Connection, task: sqlite3.Row) -> int:
    """When the card left active work: completed_at for done, newest archive event otherwise."""
    if task["completed_at"]:
        return int(task["completed_at"])
    row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ? AND kind = 'archived'",
                       (task["id"],)).fetchone()
    return int(row[0] or 0)


def last_event_at(conn: sqlite3.Connection, task_id: str) -> int:
    """Return the newest recorded activity for a card."""
    row = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id = ?",
                       (task_id,)).fetchone()
    return int(row[0] or 0)


def disk_usage(path: Path) -> str:
    """Human-readable allocated size for an operator-facing retained-tree report."""
    try:
        result = subprocess.run(["du", "-sh", str(path)], capture_output=True, text=True,
                                timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "size unavailable"
    if result.returncode == 0 and result.stdout.split():
        return result.stdout.split()[0]
    return "size unavailable"


def dependencies_are_reproducible(worktree: Path) -> bool:
    """Require node_modules to be ignored and contain no tracked paths before removing it."""
    ignored = git(worktree, "check-ignore", "-q", "node_modules")
    tracked = git(worktree, "ls-files", "--", "node_modules")
    return ignored.returncode == 0 and tracked.returncode == 0 and not tracked.stdout.strip()


def already_commented(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM task_comments WHERE task_id = ? AND body LIKE ? LIMIT 1",
                       (task_id, f"{MARK}%")).fetchone()
    return row is not None


def sweep_board(db: Path, dry_run: bool, report: list[str]) -> list[Path]:
    board = db.parent.name
    try:
        meta = json.loads((db.parent / "board.json").read_text())
    except Exception:
        return []
    repo = Path(meta.get("default_workdir") or "").expanduser()
    if not repo.is_dir():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    trashed: list[Path] = []
    now = time.time()
    for wt in registered_worktrees(repo):
        if not wt.exists():
            continue  # stale registration; the prune below clears it
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (wt.name,)).fetchone()
        if task is None:
            report.append(f"{board}/{wt.name}: orphan worktree kept (no matching card): {wt}")
            continue
        if pid_alive(task["worker_pid"]):
            continue
        if task["status"] in PARKED:
            if now - last_event_at(conn, task["id"]) < PARKED_IDLE_SECONDS:
                continue
            dependencies = wt / "node_modules"
            if not dependencies.exists() and not dependencies.is_symlink():
                continue
            if not dependencies_are_reproducible(wt):
                report.append(f"{board}/{wt.name}: node_modules is not safely ignored, kept")
                continue
            if dry_run:
                report.append(f"{board}/{wt.name}: would reclaim node_modules ({task['status']}, "
                              f"idle at least {PARKED_IDLE_SECONDS // 3600}h)")
                continue
            TRASH.mkdir(parents=True, exist_ok=True)
            dest = TRASH / f"{board}-{wt.name}-node_modules-{int(now)}"
            try:
                dependencies.rename(dest)
            except OSError as e:
                report.append(f"{board}/{wt.name}: node_modules rename failed, kept: {e}")
                continue
            trashed.append(dest)
            report.append(f"{board}/{wt.name}: reclaimed node_modules ({task['status']})")
            continue
        if task["status"] not in TERMINAL:
            continue
        if now - finished_at(conn, task) < GRACE_SECONDS:
            continue
        st = git(wt, "status", "--porcelain", timeout=120)
        if st.returncode != 0:
            report.append(f"{board}/{wt.name}: git status failed, kept: {st.stderr.strip()[-200:]}")
            continue
        content = [ln for ln in st.stdout.splitlines() if ln and not ln.startswith(" D ")]
        if content:
            commented = already_commented(conn, task["id"])
            outcome = ""
            if not commented:
                shown = "\n".join(f"  {ln}" for ln in content[:15])
                more = f"\n  … and {len(content) - 15} more" if len(content) > 15 else ""
                body = (f"{MARK} This card is {task['status']}, but its worktree still holds "
                        f"{len(content)} uncommitted path(s), so it was kept:\n{shown}{more}\n"
                        f"Worktree: {wt}\nOnce nothing there is needed: rm -rf '{wt}' && "
                        f"git -C '{repo}' worktree prune")
                if dry_run:
                    outcome = "; would comment"
                else:
                    result = subprocess.run(
                        ["hermes", "kanban", "--board", board, "comment", task["id"],
                         "--author", "dispatcher", body],
                        capture_output=True, text=True, timeout=60)
                    outcome = "; commented" if result.returncode == 0 else "; comment failed"
            report.append(f"{board}/{wt.name}: kept, {len(content)} uncommitted path(s), "
                          f"holding {disk_usage(wt)}{outcome}")
            continue
        if dry_run:
            report.append(f"{board}/{wt.name}: would remove ({task['status']})")
            continue
        TRASH.mkdir(parents=True, exist_ok=True)
        dest = TRASH / f"{board}-{wt.name}-{int(now)}"
        try:
            wt.rename(dest)
        except OSError as e:
            report.append(f"{board}/{wt.name}: rename failed, kept: {e}")
            continue
        trashed.append(dest)
        report.append(f"{board}/{wt.name}: removed ({task['status']})")
    conn.close()
    if not dry_run:
        git(repo, "worktree", "prune")
    return trashed


def empty_trash(paths: list[Path]) -> None:
    """Delete in a detached process so a multi-minute node_modules delete never blocks the tick."""
    if paths:
        subprocess.Popen(["rm", "-rf", *map(str, paths)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def check_disk(report: list[str]) -> None:
    free_gib = shutil.disk_usage(Path.home()).free // 2**30
    try:
        latch = json.loads(LATCH.read_text())
    except Exception:
        latch = {}
    low = free_gib < LOW_DISK_GIB
    if low and not latch.get("low"):
        msg = (f"Disk has {free_gib} GiB free (warn below {LOW_DISK_GIB}). "
               f"`git worktree add` times out when it is nearly full, and the dispatcher counts that "
               f"as a spawn failure.")
        report.append(msg)
        notify("Disk low", msg, "hermes-kanban-disk")
    if low != bool(latch.get("low")):
        LATCH.write_text(json.dumps({"low": low, "free_gib": free_gib, "at": int(time.time())}))


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    report: list[str] = []
    trashed: list[Path] = []
    for db in sorted(BOARDS.glob("*/kanban.db")):
        trashed += sweep_board(db, dry_run, report)
    if not dry_run:
        # Leftovers from a tick whose detached delete was interrupted.
        trashed += [p for p in TRASH.glob("*") if p not in trashed] if TRASH.is_dir() else []
        empty_trash(trashed)
        check_disk(report)
    if report:
        print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
