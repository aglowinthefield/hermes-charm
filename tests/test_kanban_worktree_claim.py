"""Decision table for the worktree-claim hook, run against throwaway boards."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "agent-hooks" / "kanban-worktree-claim.py"


class WorktreeClaimHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.boards = base / "boards"
        self.repo = base / "repo"
        self.wt = self.repo / ".worktrees" / "t_abc123"
        self.other_wt = self.repo / ".worktrees" / "t_unbound"
        for path in (self.wt / "apps", self.other_wt):
            path.mkdir(parents=True)
        self.bin = base / "bin"
        self.bin.mkdir()
        fake = self.bin / "hermes"
        fake.write_text("#!/bin/sh\nprintf '  - jester\\n  - page\\n  - princess\\n  - scribe\\n'\n")
        fake.chmod(0o755)
        self.stranger = subprocess.Popen(["sleep", "60"])
        self.board([("t_abc123", "SIL-1: port feeds", "scribe", "triage", str(self.wt), None)])

    def tearDown(self):
        self.stranger.kill()
        self.stranger.wait()
        self.temp.cleanup()

    def board(self, tasks, name="acme"):
        path = self.boards / name
        path.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path / "kanban.db")) as conn:
            conn.execute("DROP TABLE IF EXISTS tasks")
            conn.execute(
                "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
                "workspace_kind TEXT DEFAULT 'worktree', workspace_path TEXT, worker_pid INTEGER)"
            )
            conn.executemany(
                "INSERT INTO tasks (id, title, assignee, status, workspace_path, worker_pid) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                tasks,
            )
            conn.commit()

    def run_hook(self, tool, tool_input, cwd=None):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("HERMES_", "KANBAN_"))}
        env["KANBAN_BOARDS_DIR"] = str(self.boards)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        payload = {"hook_event_name": "pre_tool_call", "tool_name": tool,
                   "tool_input": tool_input, "cwd": str(cwd or self.repo)}
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)

    def assertBlocked(self, result):
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        decision = json.loads(result.stdout)
        self.assertEqual("block", decision["decision"])
        self.assertIn("t_abc123", decision["reason"])
        self.assertIn("hermes kanban --board acme reassign t_abc123 default --reclaim", decision["reason"])
        self.assertIn("t_abc123", result.stderr)

    def assertAllowed(self, result):
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("", result.stdout)

    def terminal(self, command, **kwargs):
        return self.run_hook("terminal", {"command": command}, **kwargs)

    def test_commit_after_cd_into_dispatchable_card_worktree_is_blocked(self):
        self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && git add -- a.ts && git commit -m x"))

    def test_every_held_status_blocks_because_a_dashboard_move_can_dispatch_it(self):
        for status in ("triage", "blocked", "todo", "ready", "running", "review", "scheduled"):
            with self.subTest(status=status):
                self.board([("t_abc123", "t", "scribe", status, str(self.wt), None)])
                self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && git push"))

    def test_card_reassigned_to_a_non_dispatchable_profile_is_allowed(self):
        for assignee in ("default", "tom"):
            with self.subTest(assignee=assignee):
                self.board([("t_abc123", "t", assignee, "running", str(self.wt), None)])
                self.assertAllowed(self.terminal(f"cd {self.wt.as_posix()} && git push"))

    def test_finished_cards_are_allowed(self):
        for status in ("done", "archived"):
            with self.subTest(status=status):
                self.board([("t_abc123", "t", "scribe", status, str(self.wt), None)])
                self.assertAllowed(self.terminal(f"cd {self.wt.as_posix()} && git push"))

    def test_the_cards_own_live_worker_is_allowed(self):
        self.board([("t_abc123", "t", "scribe", "running", str(self.wt), os.getpid())])
        self.assertAllowed(self.terminal(f"cd {self.wt.as_posix()} && git commit -m x && git push"))

    def test_another_process_holding_the_claim_does_not_admit_this_session(self):
        self.board([("t_abc123", "t", "scribe", "running", str(self.wt), self.stranger.pid)])
        self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && git push"))

    def test_read_only_git_is_allowed(self):
        self.assertAllowed(self.terminal(
            f"cd {self.wt.as_posix()} && git status --short && git log --oneline -3 && git diff origin/main"))

    def test_history_writing_git_verbs_are_blocked(self):
        for verb in ("rebase origin/main", "reset --hard HEAD~1", "merge x", "cherry-pick abc",
                     "checkout -- a.ts", "stash", "pull", "push --force-with-lease"):
            with self.subTest(verb=verb):
                self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && git {verb}"))

    @unittest.skipUnless(sys.platform == "win32", "Git Bash drive paths")
    def test_git_bash_drive_path_targets_the_worktree(self):
        drive, rest = self.wt.as_posix().split(":", 1)
        self.assertBlocked(self.terminal(f"cd /{drive.lower()}{rest} && git push"))

    def test_git_dash_c_targets_the_worktree(self):
        self.assertBlocked(self.terminal(f"git -C {self.wt.as_posix()} push"))

    def test_workdir_argument_targets_the_worktree(self):
        self.assertBlocked(self.run_hook("terminal", {"command": "git push", "workdir": str(self.wt)}))

    def test_session_cwd_inside_the_worktree_targets_it(self):
        self.assertBlocked(self.terminal("git commit -m x", cwd=self.wt / "apps"))

    def test_nested_shell_is_unwrapped(self):
        self.assertBlocked(self.terminal(f"bash -lc 'cd {self.wt.as_posix()} && git push'"))

    def test_gh_pr_mutations_from_the_worktree_are_blocked(self):
        for args in ("pr create --draft --title x --body-file b.md", "pr ready 3217", "pr edit 1 --title y"):
            with self.subTest(args=args):
                self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && gh {args}"))

    def test_gh_reads_are_allowed(self):
        self.assertAllowed(self.terminal(f"cd {self.wt.as_posix()} && gh pr checks 3217 && gh pr view 3217"))

    def test_file_tools_writing_into_the_worktree_are_blocked(self):
        self.assertBlocked(self.run_hook("write_file", {"path": str(self.wt / "apps" / "a.ts"), "content": "x"}))
        self.assertBlocked(self.run_hook("patch", {"path": str(self.wt / "apps" / "a.ts"),
                                                   "old_string": "a", "new_string": "b"}))

    def test_v4a_patch_naming_a_worktree_file_is_blocked(self):
        body = f"*** Begin Patch\n*** Update File: {self.wt.as_posix()}/apps/a.ts\n@@\n-a\n+b\n*** End Patch"
        self.assertBlocked(self.run_hook("patch", {"mode": "patch", "patch": body}))

    def test_writes_outside_any_claimed_worktree_are_allowed(self):
        self.assertAllowed(self.terminal(f"cd {self.repo.as_posix()} && git commit -m x"))
        self.assertAllowed(self.terminal(f"cd {self.other_wt.as_posix()} && git push"))
        self.assertAllowed(self.run_hook("write_file", {"path": str(self.repo / "a.ts"), "content": "x"}))

    def test_cards_whose_workspace_is_the_repo_root_do_not_claim_the_repo(self):
        self.board([("t_root", "t", "scribe", "todo", str(self.repo), None)])
        self.assertAllowed(self.terminal(f"cd {self.repo.as_posix()} && git commit -m x"))

    def test_quoted_text_is_not_a_command(self):
        self.assertAllowed(self.terminal(f"echo 'cd {self.wt.as_posix()} && git push'"))

    def test_an_unreadable_board_does_not_hide_a_claim_on_another(self):
        (self.boards / "broken").mkdir()
        (self.boards / "broken" / "kanban.db").write_text("not a database")
        self.assertBlocked(self.terminal(f"cd {self.wt.as_posix()} && git push"))

    def test_unparseable_command_naming_a_claimed_worktree_fails_closed(self):
        result = self.terminal(f"cd {self.wt.as_posix()} && git commit -m 'unbalanced")
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)

    def test_malformed_payload_is_allowed(self):
        env = dict(os.environ, KANBAN_BOARDS_DIR=str(self.boards))
        result = subprocess.run([sys.executable, str(HOOK)], input="not json",
                                capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main()
