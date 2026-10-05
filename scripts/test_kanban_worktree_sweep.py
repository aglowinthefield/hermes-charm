#!/usr/bin/env python3
"""Regression tests for kanban-worktree-sweep.py."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("kanban-worktree-sweep.py")
SPEC = importlib.util.spec_from_file_location("kanban_worktree_sweep", SCRIPT)
assert SPEC and SPEC.loader
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


class SweepBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        run("git", "init", "-q", cwd=self.repo)
        run("git", "config", "user.email", "test@example.com", cwd=self.repo)
        run("git", "config", "user.name", "Test", cwd=self.repo)
        (self.repo / "tracked").write_text("base\n")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        run("git", "add", "tracked", ".gitignore", cwd=self.repo)
        run("git", "commit", "-qm", "base", cwd=self.repo)

        self.board = self.root / "boards" / "test-board"
        self.board.mkdir(parents=True)
        (self.board / "board.json").write_text(json.dumps({"default_workdir": str(self.repo)}))
        self.db = self.board / "kanban.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, status TEXT, completed_at INTEGER, worker_pid INTEGER
            );
            CREATE TABLE task_events (task_id TEXT, kind TEXT, created_at INTEGER);
            CREATE TABLE task_comments (task_id TEXT, body TEXT);
        """)
        conn.close()
        self.trash = self.root / "trash"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_worktree(self, name: str) -> Path:
        path = self.repo / ".worktrees" / name
        run("git", "worktree", "add", "-qb", name, str(path), cwd=self.repo)
        return path

    def add_task(self, task_id: str, status: str, *, age: int = 24 * 60 * 60,
                 worker_pid: int | None = None, completed: bool = False) -> None:
        timestamp = int(time.time()) - age
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)",
                     (task_id, status, timestamp if completed else None, worker_pid))
        conn.execute("INSERT INTO task_events VALUES (?, 'status_changed', ?)",
                     (task_id, timestamp))
        conn.commit()
        conn.close()

    def test_parked_reclaims_only_dependencies_and_reports_orphan(self) -> None:
        parked = self.add_worktree("t_parked")
        dependencies = parked / "node_modules"
        dependencies.mkdir()
        (dependencies / "package").write_text("reproducible")
        (parked / "untracked-work").write_text("must survive")
        self.add_task("t_parked", "review")

        orphan = self.add_worktree("manual-orphan")
        (orphan / "node_modules").mkdir()
        head_before = run("git", "rev-parse", "HEAD", cwd=parked)
        status_before = run("git", "status", "--porcelain", cwd=parked)

        with mock.patch.object(sweep, "TRASH", self.trash):
            dry_report: list[str] = []
            self.assertEqual([], sweep.sweep_board(self.db, True, dry_report))
            self.assertTrue(dependencies.is_dir())
            self.assertTrue(any("would reclaim node_modules" in line for line in dry_report))
            self.assertTrue(any("orphan worktree kept" in line for line in dry_report))

            report: list[str] = []
            trashed = sweep.sweep_board(self.db, False, report)

        self.assertEqual(1, len(trashed))
        self.assertFalse(dependencies.exists())
        self.assertTrue((parked / "untracked-work").is_file())
        self.assertTrue((orphan / "node_modules").is_dir())
        self.assertEqual(head_before, run("git", "rev-parse", "HEAD", cwd=parked))
        self.assertEqual(status_before, run("git", "status", "--porcelain", cwd=parked))

    def test_active_cards_are_untouched(self) -> None:
        running = self.add_worktree("t_running")
        (running / "node_modules").mkdir()
        self.add_task("t_running", "running")
        blocked = self.add_worktree("t_live")
        (blocked / "node_modules").mkdir()
        self.add_task("t_live", "blocked", worker_pid=1234)
        recent = self.add_worktree("t_recent")
        (recent / "node_modules").mkdir()
        self.add_task("t_recent", "blocked", age=60)

        with mock.patch.object(sweep, "pid_alive", side_effect=lambda pid: bool(pid)):
            report: list[str] = []
            self.assertEqual([], sweep.sweep_board(self.db, True, report))

        self.assertEqual([], report)
        self.assertTrue((running / "node_modules").is_dir())
        self.assertTrue((blocked / "node_modules").is_dir())
        self.assertTrue((recent / "node_modules").is_dir())

    def test_unignored_dependencies_are_kept(self) -> None:
        worktree = self.add_worktree("t_unignored")
        (worktree / ".gitignore").write_text("")
        dependencies = worktree / "node_modules"
        dependencies.mkdir()
        self.add_task("t_unignored", "blocked")

        report: list[str] = []
        self.assertEqual([], sweep.sweep_board(self.db, True, report))

        self.assertTrue(dependencies.is_dir())
        self.assertTrue(any("not safely ignored, kept" in line for line in report))

    def test_finished_clean_removes_tree_and_dirty_reports_every_run(self) -> None:
        clean = self.add_worktree("t_done")
        self.add_task("t_done", "done", completed=True)
        dirty = self.add_worktree("t_dirty")
        (dirty / "local-change").write_text("keep")
        self.add_task("t_dirty", "archived")
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO task_events VALUES (?, 'archived', ?)",
                     ("t_dirty", int(time.time()) - 3600))
        conn.execute("INSERT INTO task_comments VALUES (?, ?)",
                     ("t_dirty", f"{sweep.MARK} already reported"))
        conn.commit()
        conn.close()

        with mock.patch.object(sweep, "TRASH", self.trash):
            first_report: list[str] = []
            trashed = sweep.sweep_board(self.db, False, first_report)
            second_report: list[str] = []
            sweep.sweep_board(self.db, True, second_report)

        self.assertEqual(1, len(trashed))
        self.assertFalse(clean.exists())
        self.assertTrue(dirty.exists())
        self.assertTrue(any("holding" in line for line in first_report))
        self.assertTrue(any("holding" in line for line in second_report))
        self.assertFalse(any("would comment" in line for line in second_report))


if __name__ == "__main__":
    unittest.main()
