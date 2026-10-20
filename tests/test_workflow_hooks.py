"""Exercise owned shell hooks and isolated workflow fixtures in the CI runner."""
from __future__ import annotations

import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "agent-hooks"


def fixture_env(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("HERMES_", "KANBAN_", "HOOK_SKILLS_"))}
    env["HERMES_HOME"] = str(home)
    return env


class ShellHookTests(unittest.TestCase):
    def run_shell_test(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as temp:
            env = fixture_env(Path(temp))
            result = subprocess.run(["sh", str(HOOKS / name)], env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("FAIL", result.stdout)


    def test_worktree_deps(self):
        self.run_shell_test("test-kanban-worktree-deps.sh")

    def test_review_routing(self):
        self.run_shell_test("test-kanban-review-routing.sh")

    def assert_unexpected_exits_rejected(self, name, hook):
        for status in (1, 127):
            with self.subTest(name=name, status=status), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                shutil.copy2(HOOKS / name, directory / name)
                (directory / hook).write_text(f"#!/bin/sh\nexit {status}\n")
                result = subprocess.run(["sh", str(directory / name)],
                                        env=fixture_env(directory), capture_output=True,
                                        text=True, timeout=60)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"got error({status})", result.stdout)


    def test_routing_table_rejects_unexpected_exits(self):
        self.assert_unexpected_exits_rejected("test-kanban-review-routing.sh", "kanban-review-routing.sh")


class InstalledWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "active-home"
        self.boards = self.home / "kanban" / "boards"
        self.board = self.boards / "fixture"
        self.board.mkdir(parents=True)
        self.db = self.board / "kanban.db"
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT,
                                    workspace_path TEXT, current_run_id INTEGER);
                CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT,
                                          kind TEXT, payload TEXT, created_at INTEGER);
                INSERT INTO tasks VALUES ('t_review', 'running', '/fixture/worktree', 22);
                INSERT INTO task_events VALUES (1, 't_review', 'claimed',
                    '{"run_id":22,"source_status":"review"}', 1);
                INSERT INTO task_events VALUES (2, 't_review', 'blocked',
                    '{"reason":"Verified with `git cherry origin/main fixture`."}', 2);
            """)
        self.installed = self.root / "installed-plugin" / "agent-hooks"
        shutil.copytree(HOOKS, self.installed)
        self.env = fixture_env(self.home)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "hermes-calls.jsonl"
        hermes = self.bin / "hermes"
        hermes.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "with open(os.environ['MOCK_HERMES_CALLS'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        hermes.chmod(0o755)
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env["PATH"],
                        MOCK_HERMES_CALLS=str(self.calls))

    def run_hook(self, name, payload):
        executable = sys.executable if name == "kanban-blocked-claim-audit.sh" else "sh"
        return subprocess.run([executable, str(self.installed / name)],
                              input=json.dumps(payload), env=self.env,
                              capture_output=True, text=True, timeout=10)

    def route_payload(self, profile="reviewer", reviewer="operator", task=True):
        args = {"reviewer": reviewer, "metadata": {"no_pr": True}}
        if task:
            args["task_id"] = "t_review"
        return {"tool_name": "kanban_request_review", "profile": profile,
                "cwd": "/fixture/worktree", "tool_input": args}

    def test_moved_routing_uses_active_home_without_db_override(self):
        for task in (True, False):
            with self.subTest(task=task):
                result = self.run_hook("kanban-review-routing.sh", self.route_payload(task=task))
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("", result.stdout)

    def test_routing_uses_boards_override_without_db_override(self):
        override = self.root / "override-boards"
        shutil.move(str(self.boards), override)
        self.env["KANBAN_BOARDS_DIR"] = str(override)
        result = self.run_hook("kanban-review-routing.sh", self.route_payload())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_investigation_profile_defaults_and_fixture_override(self):
        for profile, overrides in (("investigator", {}), ("page", {
                "KANBAN_REVIEWER_PROFILE": "jester",
                "KANBAN_HUMAN_REVIEW_ASSIGNEE": "tom",
                "KANBAN_INVESTIGATION_PROFILE": "page"})):
            with self.subTest(profile=profile):
                self.env.update(overrides)
                reviewer = overrides.get("KANBAN_REVIEWER_PROFILE", "reviewer")
                result = self.run_hook("kanban-review-routing.sh",
                                       self.route_payload(profile, reviewer))
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                reason = json.loads(result.stdout)["reason"]
                self.assertIn("investigation", reason)
                self.assertIn("implementer", reason)
                self.assertNotIn("Scribe", reason)

    def test_nested_block_payload_comments_only_on_fixture_board(self):
        result = self.run_hook("kanban-blocked-claim-audit.sh", {
            "hook_event_name": "kanban_task_blocked", "task_id": "wrong-task",
            "board": "wrong-board", "extra": {"task_id": "t_review", "board": "fixture"}})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(1, len(calls))
        self.assertEqual(["kanban", "--board", "fixture", "comment", "t_review"], calls[0][:5])
        self.assertIn("claim-audit (shadow)", calls[0][5])
        self.assertEqual(["--author", "claim-audit"], calls[0][-2:])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("running", db.execute("SELECT status FROM tasks").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT count(*) FROM task_events").fetchone()[0])

    def test_legacy_block_payload_and_boards_override(self):
        override = self.root / "override-boards"
        shutil.move(str(self.boards), override)
        self.env["KANBAN_BOARDS_DIR"] = str(override)
        result = self.run_hook("kanban-blocked-claim-audit.sh",
                               {"task_id": "t_review", "board": "fixture"})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.calls.is_file())

    def test_verified_block_claim_is_silent(self):
        logs = self.board / "logs"
        logs.mkdir()
        (logs / "t_review.log").write_text("git cherry origin/main fixture\n")
        result = self.run_hook("kanban-blocked-claim-audit.sh",
                               {"extra": {"task_id": "t_review", "board": "fixture"}})
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(self.calls.exists())

    def test_weekly_report_uses_override_and_is_read_only(self):
        override = self.root / "override-boards"
        shutil.move(str(self.boards), override)
        with closing(sqlite3.connect(override / "fixture" / "kanban.db")) as db:
            db.executescript("""
                CREATE TABLE task_comments (task_id TEXT, author TEXT, created_at INTEGER);
                INSERT INTO task_comments VALUES ('t_review', 'claim-audit', 1);
            """)
        self.env["KANBAN_BOARDS_DIR"] = str(override)
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "claim-audit-weekly.py")],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fixture: 0 flag(s) this week, 1 all time", result.stdout)
        self.assertFalse(self.calls.exists())

    def test_maintenance_paths_follow_active_home_or_boards_override(self):
        for name in ("kanban-giveup-watch.py", "kanban-worktree-sweep.py"):
            for override in (None, str(self.root / "override-boards")):
                with self.subTest(name=name, override=override):
                    env = self.env.copy()
                    if override:
                        env["KANBAN_BOARDS_DIR"] = override
                    spec = importlib.util.spec_from_file_location("fixture_maintenance", ROOT / "scripts" / name)
                    assert spec and spec.loader
                    module = importlib.util.module_from_spec(spec)
                    with mock.patch.dict(os.environ, env, clear=True):
                        spec.loader.exec_module(module)
                    self.assertEqual(Path(override) if override else self.boards, module.BOARDS)
                    self.assertEqual(self.home, module.HOME)
                    if name == "kanban-worktree-sweep.py":
                        self.assertEqual(self.home / "kanban" / "worktree-trash", module.TRASH)
                        self.assertEqual(self.home / "kanban-worktree-sweep-latch.json", module.LATCH)
                        self.assertEqual(300, module.GRACE_SECONDS)
                        self.assertEqual(21600, module.PARKED_IDLE_SECONDS)
                    else:
                        self.assertEqual(self.home / "agent-hooks" / "kanban-linear.py", module.LINEAR_HOOK)
                        self.assertFalse(module.LINEAR_HOOK.exists())


def load_tests(loader, tests, pattern):
    spec = importlib.util.spec_from_file_location(
        "owned_worktree_sweep_tests", ROOT / "scripts" / "test_kanban_worktree_sweep.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tests.addTests(loader.loadTestsFromModule(module))
    return tests
