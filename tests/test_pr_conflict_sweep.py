"""Exercise the PR conflict sweep against a throwaway board and faked gh/hermes/collector calls."""
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
SPEC = importlib.util.spec_from_file_location("pr_conflict_sweep", ROOT / "scripts" / "pr-conflict-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)

REPO = "example/repo"


def pr(number, branch, sha="a" * 40, mergeable="CONFLICTING", draft=False):
    return {"number": number, "title": f"feat: change {number}", "headRefName": branch,
            "headRefOid": sha, "isDraft": draft, "mergeable": mergeable,
            "url": f"https://github.com/{REPO}/pull/{number}"}


class FakeCommands:
    """Answers gh/collector reads from fixtures and records every hermes write."""

    def __init__(self, prs, verdicts=None, collector_fails=(), pr_comment_fails=False):
        self.prs = prs
        self.verdicts = verdicts or {}
        self.collector_fails = set(collector_fails)
        self.pr_comment_fails = pr_comment_fails
        self.writes: list[list[str]] = []
        self.pr_comments: list[list[str]] = []

    def __call__(self, argv):
        if argv[:3] == ["gh", "pr", "comment"]:
            self.pr_comments.append(argv)
            return (1, "", "HTTP 403") if self.pr_comment_fails else (0, "", "")
        if argv[0] == "gh":
            return 0, json.dumps(self.prs), ""
        if argv[0] == "hermes":
            self.writes.append(argv)
            return 0, "", ""
        number = int(argv[-1])  # collector: python3 collect.py <n>
        if number in self.collector_fails:
            return 1, "", "boom"
        report = {"verdict": self.verdicts.get(number, "RELEVANT"),
                  "content": {"conflicts": ["packages/a.ts", "package-lock.json"]}}
        return 0, json.dumps({"reports": [report], "failures": []}), ""

    def actions(self):
        return [(argv[4], argv[5]) for argv in self.writes]  # (subcommand, task id)

    def acted(self):
        return [a for a in self.actions() if a[0] != "reassign"]


class SweepTests(unittest.TestCase):
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
        self.settings = {"repo": REPO, "board": "example", "park_assignee": "operator",
                         "collector": "/abs/collect.py"}

    def card(self, task_id, status, assignee="operator", branch=None, claim=None, published=None,
             handoffs=(("builder", "auditor"), ("auditor", "operator"))):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, 1)",
                         (task_id, "title", assignee, status, branch, claim))
            for at, (implementer, reviewer) in enumerate(handoffs):
                conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                             (task_id, "review_requested",
                              json.dumps({"implementer": implementer, "reviewer": reviewer}), at))
            if published:
                conn.execute("INSERT INTO task_runs (task_id, metadata) VALUES (?, ?)",
                             (task_id, json.dumps({"published_pr": published})))
            conn.commit()

    def tick(self, fake):
        return sweep.run(self.settings, self.db, self.state, fake)

    def test_parked_review_card_is_reopened_once_per_head(self):
        self.card("t_one", "review", branch="wt/t_one")
        fake = FakeCommands([pr(7, "wt/t_one")])
        report = self.tick(fake)
        self.assertEqual([("comment", "t_one"), ("reopen-review", "t_one"), ("reassign", "t_one")],
                         fake.actions())
        self.assertEqual(["builder", "--reclaim"], fake.writes[2][6:8])  # the implementer, not the reviewer
        comment = fake.writes[0][-1]
        self.assertIn("#7", comment)
        self.assertNotIn("https://github.com", comment)  # a PR URL in a comment holds respawns
        [posted] = fake.pr_comments
        self.assertEqual(pr(7, "wt/t_one")["url"], posted[3])
        self.assertTrue(posted[-1].startswith("<!-- hermes-agent: aide -->\n## 🧭 aide · supervisor"))
        self.assertIn("**builder**", posted[-1])
        self.assertIn("t_one · aaaaaaa", posted[-1])
        self.assertIn("rebase-regenerate.md", comment)
        self.assertIn("package-lock.json", comment)
        self.assertIn("t_one", report)

        again = FakeCommands([pr(7, "wt/t_one")])
        self.assertEqual("", self.tick(again))
        self.assertEqual([], again.writes)

        moved = FakeCommands([pr(7, "wt/t_one", sha="b" * 40)])
        self.tick(moved)
        self.assertEqual([("comment", "t_one"), ("reopen-review", "t_one")], moved.acted())

    def test_card_found_through_published_pr_receipt(self):
        self.card("t_two", "review", branch="other-branch",
                  published=f"https://github.com/{REPO}/pull/8")
        fake = FakeCommands([pr(8, "feature-branch")])
        self.tick(fake)
        self.assertEqual(("reopen-review", "t_two"), fake.acted()[-1])

    def test_mergeable_and_unknown_are_ignored(self):
        self.card("t_a", "review", branch="a")
        self.card("t_c", "review", branch="c")
        fake = FakeCommands([pr(1, "a", mergeable="MERGEABLE"), pr(3, "c", mergeable="UNKNOWN")])
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_conflicting_draft_parked_for_review_is_reopened(self):
        # Kanban workers open drafts and only the operator marks them ready, so a parked card's
        # PR is usually still a draft when main moves under it.
        self.card("t_d", "review", branch="d")
        fake = FakeCommands([pr(4, "d", draft=True)])
        self.tick(fake)
        self.assertEqual(("reopen-review", "t_d"), fake.acted()[-1])

    def test_active_or_in_review_cards_are_left_alone_silently(self):
        self.card("t_run", "running", assignee="builder", branch="r", claim="lock")
        self.card("t_ai", "review", assignee="auditor", branch="ai")
        self.card("t_claimed", "review", branch="cl", claim="lock")
        fake = FakeCommands([pr(1, "r"), pr(2, "ai"), pr(3, "cl")])
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_human_held_done_and_orphan_prs_are_reported_once(self):
        self.card("t_tri", "triage", assignee="auditor", branch="tri")
        self.card("t_done", "done", assignee="builder", branch="dn")
        fake = FakeCommands([pr(1, "tri"), pr(2, "dn"), pr(3, "nobody")])
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("#1", report)
        self.assertIn("t_tri (triage", report)
        self.assertIn("#2", report)
        self.assertIn("#3", report)
        self.assertIn("no kanban card", report)
        self.assertEqual("", self.tick(FakeCommands([pr(1, "tri"), pr(2, "dn"), pr(3, "nobody")])))

    def test_irrelevant_pr_is_reported_not_reopened(self):
        self.card("t_old", "review", branch="old")
        fake = FakeCommands([pr(5, "old")], verdicts={5: "SUPERSEDED"})
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("SUPERSEDED", report)

    def test_collector_failure_reports_and_retries_next_tick(self):
        self.card("t_x", "review", branch="x")
        report = self.tick(FakeCommands([pr(6, "x")], collector_fails=[6]))
        self.assertIn("relevance check failed", report)
        retry = FakeCommands([pr(6, "x")])
        self.tick(retry)
        self.assertEqual(("reopen-review", "t_x"), retry.acted()[-1])

    def test_resolved_conflict_clears_state_so_a_later_conflict_acts_again(self):
        self.card("t_one", "review", branch="wt/t_one")
        self.tick(FakeCommands([pr(7, "wt/t_one")]))
        self.tick(FakeCommands([pr(7, "wt/t_one", mergeable="MERGEABLE")]))
        self.assertEqual({}, json.loads(self.state.read_text()))
        again = FakeCommands([pr(7, "wt/t_one")])
        self.tick(again)
        self.assertEqual(("reopen-review", "t_one"), again.acted()[-1])

    def test_failed_reopen_is_reported_and_retried(self):
        self.card("t_one", "review", branch="wt/t_one")

        class Failing(FakeCommands):
            def __call__(self, argv):
                if argv[0] == "hermes" and "reopen-review" in argv:
                    self.writes.append(argv)
                    return 1, "", "not in review?"
                return super().__call__(argv)

        report = self.tick(Failing([pr(7, "wt/t_one")]))
        self.assertIn("not in review?", report)
        retry = FakeCommands([pr(7, "wt/t_one")])
        self.tick(retry)
        self.assertEqual(("reopen-review", "t_one"), retry.acted()[-1])

    def test_card_with_no_implementer_handoff_is_reported_not_reopened(self):
        self.card("t_odd", "review", branch="odd", handoffs=(("auditor", "operator"),))
        fake = FakeCommands([pr(9, "odd")])
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("no implementer", report)
        self.assertEqual([], fake.pr_comments)

    def test_failed_pr_comment_is_reported_and_not_retried(self):
        self.card("t_one", "review", branch="wt/t_one")
        report = self.tick(FakeCommands([pr(7, "wt/t_one")], pr_comment_fails=True))
        self.assertIn("reopened t_one", report)
        self.assertIn("Commenting on the PR failed: HTTP 403", report)
        again = FakeCommands([pr(7, "wt/t_one")])
        self.assertEqual("", self.tick(again))  # the card already moved; no second reopen or comment
        self.assertEqual([], again.pr_comments)

    def test_gh_failure_is_reported(self):
        def broken(argv):
            return 1, "", "HTTP 502"
        self.assertIn("HTTP 502", self.tick(broken))


if __name__ == "__main__":
    unittest.main()
