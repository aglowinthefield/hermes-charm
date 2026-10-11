"""Exercise the incident watch against throwaway boards, a keyword stand-in for Jev and faked hermes writes."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kanban_incident_watch", ROOT / "scripts" / "kanban-incident-watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

NOW = 1_800_000_000
CAUSES = ("billing", "branch-env")  # what the stand-in treats as systemic, and as one cause each


def cause(text):
    return next((c for c in CAUSES if c in text), None)


def fake_ask(calls):
    """Score like Jev would on clear cases: systemic when a known cause is named, same when both name it."""
    def ask(state, questions):
        calls.append(questions)
        new = state["new_block"]["reason"]
        scores = {}
        for key in questions:
            if key == "systemic":
                scores[key] = 0.95 if cause(new) else 0.1
            else:
                other = state["candidates"][key.removeprefix("same_")]
                scores[key] = 0.95 if cause(new) and cause(new) == cause(other) else 0.05
        return scores
    return ask


class FakeHermes:
    """Records every hermes write and applies it to the board the way the CLI does."""

    db: Path

    def __init__(self, fail=()):
        self.writes: list[list[str]] = []
        self.fail = set(fail)

    def __call__(self, argv):
        self.writes.append(argv)
        sub = argv[4]
        if sub in self.fail:
            return 1, "", f"{sub} exploded"
        with closing(sqlite3.connect(self.db)) as conn:
            if sub == "create":
                task = f"t_inc{len(self.writes)}"
                conn.execute("INSERT INTO tasks VALUES (?, 'incident', 'tom', 'ready')", (task,))
                conn.commit()
                return 0, json.dumps({"id": task}), ""
            if sub == "unblock":
                conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (argv[5],))
                conn.commit()
        return 0, "", ""

    def ops(self):
        return [(argv[4], *argv[5:7]) for argv in self.writes if argv[4] in ("link", "unblock")]


class IncidentWatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.boards = Path(temp.name) / "boards"
        self.state = Path(temp.name) / "incident-watch.json"
        self.db = self.boards / "acme" / "kanban.db"
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT)")
            conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,"
                         " payload TEXT, created_at INTEGER)")
            conn.commit()
        self.calls: list[dict] = []
        FakeHermes.db = self.db

    def blocked(self, task_id, reason, at=NOW - 60, kind="blocked", status="blocked"):
        payload = {"error": reason} if kind == "gave_up" else {"reason": reason}
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, 'scribe', ?)", (task_id, f"SIL: {task_id}", status))
            conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                         (task_id, kind, json.dumps(payload), at))
            conn.commit()

    def set_status(self, task_id, status):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO tasks VALUES (?, 'incident', 'tom', ?)", (task_id, status))
            conn.commit()

    def tick(self, hermes, now=NOW):
        return watch.run(self.boards, self.state, now, fake_ask(self.calls), hermes, "tom")

    def test_three_cards_with_one_systemic_cause_are_held_behind_one_incident(self):
        self.blocked("t_a", "CI refused: billing limit reached")
        self.blocked("t_b", "every job failed, billing")
        self.blocked("t_x", "needs a product decision on copy")
        self.blocked("t_c", "Actions billing again")
        hermes = FakeHermes()
        report = self.tick(hermes)
        creates = [argv for argv in hermes.writes if argv[4] == "create"]
        self.assertEqual(1, len(creates))
        self.assertIn("tom", creates[0])
        incident = "t_inc1"
        self.assertEqual({("link", incident, t) for t in ("t_a", "t_b", "t_c")} |
                         {("unblock", t, "--reason") for t in ("t_a", "t_b", "t_c")}, set(hermes.ops()))
        self.assertIn(incident, report)
        self.assertNotIn("t_x", report)

        later = FakeHermes()
        self.blocked("t_d", "billing still broken")
        report = self.tick(later)
        self.assertEqual([("link", incident, "t_d"), ("unblock", "t_d", "--reason")], later.ops())
        self.assertEqual([], [argv for argv in later.writes if argv[4] == "create"])
        self.assertIn("t_d", report)

    def test_two_cards_are_not_an_incident_and_quiet_ticks_cost_no_jev_calls(self):
        self.blocked("t_a", "billing")
        self.blocked("t_b", "billing")
        hermes = FakeHermes()
        self.assertEqual("", self.tick(hermes))
        self.assertEqual([], hermes.writes)
        asked = len(self.calls)
        self.assertEqual("", self.tick(FakeHermes()))
        self.assertEqual(asked, len(self.calls))

    def test_card_local_blocks_never_cluster(self):
        for task in ("t_a", "t_b", "t_c"):
            self.blocked(task, "waiting on PR 12 for this card")
        hermes = FakeHermes()
        self.assertEqual("", self.tick(hermes))
        self.assertEqual([], hermes.writes)

    def test_different_systemic_causes_stay_apart(self):
        self.blocked("t_a", "billing")
        self.blocked("t_b", "branch-env unset")
        self.blocked("t_c", "billing")
        self.blocked("t_d", "branch-env unset")
        hermes = FakeHermes()
        self.assertEqual("", self.tick(hermes))
        self.assertEqual([], hermes.writes)

    def test_old_blocks_and_moving_cards_are_ignored(self):
        self.blocked("t_a", "billing", at=NOW - watch.WINDOW_SECONDS - 1)
        self.blocked("t_b", "billing", status="ready")
        self.blocked("t_c", "billing")
        self.blocked("t_d", "billing")
        hermes = FakeHermes()
        self.assertEqual("", self.tick(hermes))
        self.assertEqual([], hermes.writes)

    def test_dispatcher_give_ups_count(self):
        self.blocked("t_a", "billing", kind="gave_up")
        self.blocked("t_b", "billing", kind="block_loop_detected")
        self.blocked("t_c", "billing")
        hermes = FakeHermes()
        self.tick(hermes)
        self.assertEqual(3, len([op for op in hermes.ops() if op[0] == "link"]))

    def test_completing_the_incident_reports_the_release_once(self):
        for task in ("t_a", "t_b", "t_c"):
            self.blocked(task, "billing")
        self.tick(FakeHermes())
        self.set_status("t_inc1", "done")
        report = self.tick(FakeHermes())
        self.assertIn("t_inc1", report)
        self.assertIn("released 3", report)
        self.assertEqual("", self.tick(FakeHermes()))

    def test_failed_hold_is_reported_and_retried(self):
        for task in ("t_a", "t_b", "t_c"):
            self.blocked(task, "billing")
        report = self.tick(FakeHermes(fail={"unblock"}))
        self.assertIn("unblock exploded", report)
        retry = FakeHermes()
        self.tick(retry)
        self.assertEqual({"t_a", "t_b", "t_c"}, {op[1] for op in retry.ops() if op[0] == "unblock"})

    def test_jev_failure_is_reported_and_judges_nothing(self):
        for task in ("t_a", "t_b", "t_c"):
            self.blocked(task, "billing")

        def down(state, questions):
            raise watch.JevError("System One HTTP 503")
        report = watch.run(self.boards, self.state, NOW, down, FakeHermes(), "tom")
        self.assertIn("HTTP 503", report)
        hermes = FakeHermes()
        self.tick(hermes)
        self.assertEqual(3, len([op for op in hermes.ops() if op[0] == "link"]))


if __name__ == "__main__":
    unittest.main()
