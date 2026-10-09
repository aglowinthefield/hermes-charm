"""Exercise the stall watch against throwaway board databases."""
from __future__ import annotations

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kanban_stall_watch", ROOT / "scripts" / "kanban-stall-watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

HOUR = 3600


class StallWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.boards = Path(self.temp.name) / "boards"
        self.state = Path(self.temp.name) / "stall-watch.json"

    def tearDown(self):
        self.temp.cleanup()

    def board(self, name, tasks, links=()):
        path = self.boards / name
        path.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path / "kanban.db")) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT)")
            conn.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?)", tasks)
            conn.executemany("INSERT INTO task_links VALUES (?, ?)", links)
            conn.commit()

    def tick(self, now):
        return watch.run(self.boards, self.state, now)

    def stalled_board(self):
        # t_rev waits on the operator and holds a three-card chain; t_a/t_b wait on each other.
        self.board("acme", [
            ("t_rev", "SIL-1: ADR", "tom", "review"),
            ("t_c1", "SIL-2: port", "scribe", "todo"),
            ("t_c2", "SIL-3: port", "scribe", "todo"),
            ("t_c3", "SIL-4: delete", "princess", "todo"),
            ("t_tri", "SIL-5: unclear", "scribe", "triage"),
            ("t_c4", "SIL-6: follow-up", "scribe", "todo"),
            ("t_a", "cycle a", "scribe", "todo"),
            ("t_b", "cycle b", "scribe", "todo"),
            ("t_old", "finished", "scribe", "done"),
        ], [
            ("t_rev", "t_c1"), ("t_c1", "t_c2"), ("t_c2", "t_c3"),
            ("t_tri", "t_c4"), ("t_a", "t_b"), ("t_b", "t_a"), ("t_old", "t_c1"),
        ])

    def test_moving_board_is_silent(self):
        self.board("acme", [("t_r", "x", "scribe", "ready"), ("t_t", "y", "scribe", "todo")], [("t_r", "t_t")])
        self.assertEqual("", self.tick(0))
        self.assertEqual("", self.tick(HOUR))

    def test_idle_board_is_not_a_stall(self):
        self.board("acme", [("t_d", "x", "scribe", "done"), ("t_rev", "y", "tom", "review")])
        self.assertEqual("", self.tick(0))
        self.assertEqual("", self.tick(HOUR))

    def test_stall_alerts_on_second_observation_with_top_blockers(self):
        self.stalled_board()
        self.assertEqual("", self.tick(0), "a single observation can be a dispatch-tick gap")
        report = self.tick(15 * 60)
        self.assertIn("acme", report)
        self.assertIn("0 ready/running, 6 todo", report)
        self.assertIn("t_rev", report)
        self.assertIn("holds 3", report)
        self.assertLess(report.index("t_rev"), report.index("t_tri"), "largest blocker first")
        self.assertIn("2 todo", report)
        self.assertIn("cycle", report)

    def test_ongoing_stall_reminds_daily_not_every_tick(self):
        self.stalled_board()
        self.tick(0)
        self.assertNotEqual("", self.tick(15 * 60))
        self.assertEqual("", self.tick(30 * 60))
        self.assertEqual("", self.tick(20 * HOUR))
        self.assertIn("t_rev", self.tick(25 * HOUR))

    def test_recovery_reports_once(self):
        self.stalled_board()
        self.tick(0)
        self.tick(15 * 60)
        with closing(sqlite3.connect(self.boards / "acme" / "kanban.db")) as conn:
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = 't_rev'")
            conn.execute("UPDATE tasks SET status = 'ready' WHERE id = 't_c1'")
            conn.commit()
        self.assertIn("moving again", self.tick(30 * 60))
        self.assertEqual("", self.tick(45 * 60))

    def test_unalerted_stall_that_clears_is_silent(self):
        self.stalled_board()
        self.tick(0)
        with closing(sqlite3.connect(self.boards / "acme" / "kanban.db")) as conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = 't_c1'")
            conn.commit()
        self.assertEqual("", self.tick(15 * 60))

    def test_archive_dirs_and_archived_boards_are_skipped(self):
        stalled = [("t_rev", "x", "tom", "review"), ("t_c", "y", "scribe", "todo")]
        self.board("_archived", stalled, [("t_rev", "t_c")])
        self.board("old", stalled, [("t_rev", "t_c")])
        (self.boards / "old" / "board.json").write_text('{"archived": true}')
        self.tick(0)
        self.assertEqual("", self.tick(HOUR))

    def test_broken_board_does_not_silence_others(self):
        (self.boards / "broken").mkdir(parents=True)
        (self.boards / "broken" / "kanban.db").write_text("not a database")
        self.stalled_board()
        self.tick(0)
        report = self.tick(15 * 60)
        self.assertIn("broken", report)
        self.assertIn("t_rev", report)

    def test_ready_card_on_a_non_dispatch_assignee_is_held_not_moving(self):
        # The dispatcher never claims tom's ready card, so its child can never start.
        self.board("acme", [("t_gate", "SIL-7: decide", "tom", "ready"), ("t_c", "SIL-8: build", "scribe", "todo")],
                   [("t_gate", "t_c")])
        spawnable = frozenset({"scribe", "jester"})
        self.assertEqual("", watch.run(self.boards, self.state, 0, spawnable))
        report = watch.run(self.boards, self.state, 15 * 60, spawnable)
        self.assertIn("0 ready/running, 1 todo", report)
        self.assertIn("t_gate (ready, not dispatchable, tom)", report)

    def test_dispatch_profiles_reads_config(self):
        home = Path(self.temp.name)
        self.assertIsNone(watch.dispatch_profiles(home))
        (home / "config.yaml").write_text("kanban:\n  dispatch_profiles:\n    - scribe\n    - page\n")
        self.assertEqual(frozenset({"scribe", "page"}), watch.dispatch_profiles(home))


if __name__ == "__main__":
    unittest.main()
