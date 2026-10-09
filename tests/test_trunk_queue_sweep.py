"""Exercise the Trunk queue sweep against a throwaway board and faked gh/Trunk/hermes calls."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("trunk_queue_sweep", ROOT / "scripts" / "trunk-queue-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)

REPO = "example/repo"


def pr(number, branch, sha="a" * 40, draft=False):
    return {"number": number, "title": f"feat: change {number}", "headRefName": branch,
            "headRefOid": sha, "isDraft": draft, "url": f"https://github.com/{REPO}/pull/{number}"}


def failed(entry="e1", reason="test_run_required_status_failed", statuses=(("test", "FAILURE"),),
           culprit=None, test_pr=901):
    failure = {"reason": reason, "blockReasons": [],
               "failureCausingStatuses": [
                   {"name": name, "conclusion": conclusion,
                    "url": f"https://github.com/{REPO}/actions/runs/1/job/2"}
                   for name, conclusion in statuses]}
    if culprit:
        failure["conflictCulprit"] = {"type": "queue_pr", "prNumber": culprit,
                                      "prUrl": f"https://github.com/{REPO}/pull/{culprit}"}
    return {"id": entry, "state": "failed", "timeline": [
        {"state": "testing", "testRun": {"prNumber": test_pr,
                                         "prUrl": f"https://github.com/{REPO}/pull/{test_pr}"}},
        {"state": "failed", "failure": failure,
         "testRun": {"prNumber": test_pr, "prUrl": f"https://github.com/{REPO}/pull/{test_pr}"}},
    ]}


class Fake:
    """Answers gh and Trunk reads from fixtures and records every hermes write."""

    def __init__(self, prs, entries=None, trunk_error=None):
        self.prs = prs
        self.entries = entries or {}
        self.trunk_error = trunk_error
        self.writes: list[list[str]] = []
        self.trunk_calls: list[int] = []

    def cmd(self, argv):
        if argv[0] == "gh":
            return 0, json.dumps(self.prs), ""
        self.writes.append(argv)
        return 0, "", ""

    def trunk(self, number):
        self.trunk_calls.append(number)
        if self.trunk_error:
            raise RuntimeError(self.trunk_error)
        return self.entries.get(number)  # None = never submitted (404)

    def actions(self):
        return [(argv[4], argv[5]) for argv in self.writes]


class TrunkQueueSweepTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.db = self.dir / "kanban.db"
        self.state = self.dir / "state.json"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,"
                         " branch_name TEXT, claim_lock TEXT, created_at INTEGER)")
            conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, metadata TEXT)")
            conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT,"
                         " payload TEXT, created_at INTEGER)")
            conn.commit()
        self.settings = {"repo": REPO, "board": "example", "park_assignee": "operator"}

    def card(self, task_id, status, assignee="operator", branch=None, claim=None,
             handoffs=(("builder", "auditor"), ("auditor", "operator"))):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, 1)",
                         (task_id, "title", assignee, status, branch, claim))
            for at, (implementer, reviewer) in enumerate(handoffs):
                conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                             (task_id, "review_requested",
                              json.dumps({"implementer": implementer, "reviewer": reviewer}), at))
            conn.commit()

    def tick(self, fake):
        return sweep.run(self.settings, self.db, self.state, fake.cmd, fake.trunk)

    def test_failed_queue_run_reopens_parked_card_for_its_implementer_once(self):
        self.card("t_one", "review", branch="b1")
        fake = Fake([pr(7, "b1")], {7: failed(statuses=(("test", "FAILURE"), ("build", "TIMED_OUT")))})
        report = self.tick(fake)
        self.assertEqual([("comment", "t_one"), ("reopen-review", "t_one"), ("reassign", "t_one")],
                         fake.actions())
        self.assertEqual(["builder", "--reclaim"], fake.writes[2][6:8])
        comment = fake.writes[0][-1]
        self.assertIn("#7", comment)
        self.assertIn("#901", comment)  # the queue's test PR, where the logs are
        self.assertIn("`test` (FAILURE)", comment)
        self.assertIn("`build` (TIMED_OUT)", comment)
        self.assertNotIn("/pull/", comment)  # a PR URL in a card comment holds respawns
        self.assertIn("t_one", report)

        again = Fake([pr(7, "b1")], {7: failed()})
        self.assertEqual("", self.tick(again))
        self.assertEqual([], again.writes)

        resubmitted = Fake([pr(7, "b1")], {7: failed(entry="e2")})
        self.tick(resubmitted)
        self.assertEqual(("reopen-review", "t_one"), resubmitted.actions()[1])

    def test_merge_conflict_failure_names_the_culprit_and_the_rebase_playbook(self):
        self.card("t_one", "review", branch="b1")
        fake = Fake([pr(7, "b1")], {7: failed(reason="start_test_run_merge_conflict", statuses=(),
                                               culprit=6)})
        self.tick(fake)
        comment = fake.writes[0][-1]
        self.assertIn("merge conflict", comment)
        self.assertIn("#6", comment)
        self.assertIn("rebase-regenerate.md", comment)

    def test_queue_infrastructure_failures_are_reported_not_reopened(self):
        self.card("t_one", "review", branch="b1")
        fake = Fake([pr(7, "b1")], {7: failed(reason="github_api_rate_limit_exceeded", statuses=())})
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("github_api_rate_limit_exceeded", report)
        self.assertIn("/trunk merge", report)
        self.assertEqual("", self.tick(Fake([pr(7, "b1")], {7: failed(reason="github_api_rate_limit_exceeded",
                                                                        statuses=())})))

    def test_unrecognised_failure_reason_is_reported_not_reopened(self):
        self.card("t_one", "review", branch="b1")
        fake = Fake([pr(7, "b1")], {7: failed(reason="something_new", statuses=())})
        self.assertIn("something_new", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_queued_testing_cancelled_and_never_submitted_prs_are_silent(self):
        for task, branch in (("t_a", "a"), ("t_b", "b"), ("t_c", "c"), ("t_d", "d"), ("t_e", "e")):
            self.card(task, "review", branch=branch)
        fake = Fake([pr(1, "a"), pr(2, "b"), pr(3, "c"), pr(4, "d"), pr(5, "e")],
                    {1: {"id": "x", "state": "testing"}, 2: {"id": "y", "state": "cancelled"},
                     3: {"id": "z", "state": "pending_failure"}, 4: {"id": "w", "state": "merged"}})
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_drafts_are_never_looked_up(self):
        self.card("t_a", "review", branch="a")
        fake = Fake([pr(1, "a", draft=True)], {1: failed()})
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.trunk_calls)

    def test_cards_already_moving_or_under_ai_review_are_left_alone(self):
        self.card("t_run", "running", assignee="builder", branch="r", claim="lock")
        self.card("t_ai", "review", assignee="auditor", branch="ai")
        fake = Fake([pr(1, "r"), pr(2, "ai")], {1: failed(), 2: failed()})
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_failed_pr_with_no_parked_card_is_reported_once(self):
        self.card("t_done", "done", assignee="builder", branch="dn")
        fake = Fake([pr(1, "dn"), pr(2, "nobody")], {1: failed(), 2: failed(entry="e9")})
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("#1", report)
        self.assertIn("#2", report)
        self.assertIn("no kanban card", report)
        self.assertEqual("", self.tick(Fake([pr(1, "dn"), pr(2, "nobody")], {1: failed(), 2: failed(entry="e9")})))

    def test_trunk_api_failure_is_reported_and_retried(self):
        self.card("t_one", "review", branch="b1")
        report = self.tick(Fake([pr(7, "b1")], trunk_error="HTTP 401"))
        self.assertIn("HTTP 401", report)
        retry = Fake([pr(7, "b1")], {7: failed()})
        self.tick(retry)
        self.assertEqual(("reopen-review", "t_one"), retry.actions()[1])

    def test_refused_token_reports_one_line_and_stops(self):
        self.card("t_one", "review", branch="b1")

        class Refused(Fake):
            def trunk(self, number):
                self.trunk_calls.append(number)
                raise sweep.TrunkAuthError("Trunk API HTTP 403")

        fake = Refused([pr(7, "b1"), pr(8, "b2")])
        report = self.tick(fake)
        self.assertEqual([7], fake.trunk_calls)
        self.assertEqual(1, len(report.splitlines()))
        self.assertIn("HTTP 403", report)

    def test_failed_reopen_is_retried_next_tick(self):
        self.card("t_one", "review", branch="b1")

        class Failing(Fake):
            def cmd(self, argv):
                if argv[0] == "hermes" and "reopen-review" in argv:
                    self.writes.append(argv)
                    return 1, "", "not in review?"
                return super().cmd(argv)

        report = self.tick(Failing([pr(7, "b1")], {7: failed()}))
        self.assertIn("not in review?", report)
        retry = Fake([pr(7, "b1")], {7: failed()})
        self.tick(retry)
        self.assertEqual(("reopen-review", "t_one"), retry.actions()[1])

    def test_closed_prs_drop_out_of_state(self):
        self.card("t_one", "review", branch="b1")
        self.tick(Fake([pr(7, "b1")], {7: failed()}))
        self.tick(Fake([], {}))
        self.assertEqual({}, json.loads(self.state.read_text()))


if __name__ == "__main__":
    unittest.main()
