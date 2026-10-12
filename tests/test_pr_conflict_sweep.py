"""Exercise the PR conflict sweep against a throwaway board and faked gh/hermes/collector calls."""
from __future__ import annotations

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("pr_conflict_sweep", ROOT / "scripts" / "pr-conflict-sweep.py")
sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sweep)

REPO = "example/repo"
# Throwaway repositories must not inherit the operator's Git identity or hooks.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}


def pr(number, branch, sha="a" * 40, mergeable="CONFLICTING", draft=False):
    return {"number": number, "title": f"feat: change {number}", "headRefName": branch,
            "headRefOid": sha, "isDraft": draft, "mergeable": mergeable,
            "url": f"https://github.com/{REPO}/pull/{number}"}


class FakeCommands:
    """Answers gh/collector reads from fixtures and records every hermes write. git runs for real
    in `repo` when one is given."""

    def __init__(self, prs, verdicts=None, collector_fails=(), pr_comment_fails=False, repo=None,
                 create_fails=False):
        self.prs = prs
        self.verdicts = verdicts or {}
        self.collector_fails = set(collector_fails)
        self.pr_comment_fails = pr_comment_fails
        self.repo = repo
        self.create_fails = create_fails
        self.writes: list[list[str]] = []
        self.pr_comments: list[list[str]] = []

    def __call__(self, argv):
        if argv[:3] == ["gh", "pr", "comment"]:
            self.pr_comments.append(argv)
            return (1, "", "HTTP 403") if self.pr_comment_fails else (0, "", "")
        if argv[0] == "gh":
            return 0, json.dumps(self.prs), ""
        if argv[0] == "git":
            r = subprocess.run(argv, cwd=self.repo, env=GIT_ENV, capture_output=True, text=True, check=False)
            return r.returncode, r.stdout, r.stderr
        if argv[0] == "hermes":
            self.writes.append(argv)
            if argv[4] == "create":
                return (1, "", "board locked") if self.create_fails else (0, json.dumps({"id": "t_new"}), "")
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
                         "collector": "/abs/collect.py", "adopt_assignee": "builder"}

    def git(self, *args, cwd=None):
        r = subprocess.run(["git", *args], cwd=cwd or self.clone, env=GIT_ENV, capture_output=True,
                           text=True, check=True)
        return r.stdout.strip()

    def remote_branch(self, branch):
        """A bare origin holding `branch` one commit past main, and a clone of it (the board's
        workdir) with `branch` checked out. Returns the branch head on origin."""
        self.origin, self.clone = self.dir / "origin.git", self.dir / "clone"
        self.git("init", "--quiet", "--bare", "-b", "main", str(self.origin), cwd=self.dir)
        self.git("clone", "--quiet", str(self.origin), str(self.clone), cwd=self.dir)
        self.git("commit", "--quiet", "--allow-empty", "-m", "base")
        self.git("push", "--quiet", "origin", "HEAD:main")
        self.git("switch", "--quiet", "-c", branch)
        self.git("commit", "--quiet", "--allow-empty", "-m", "change")
        self.git("push", "--quiet", "origin", f"HEAD:{branch}")
        return self.git("rev-parse", "HEAD")

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

    def test_human_held_and_done_prs_are_reported_once(self):
        self.card("t_tri", "triage", assignee="auditor", branch="tri")
        self.card("t_done", "done", assignee="builder", branch="dn")
        fake = FakeCommands([pr(1, "tri"), pr(2, "dn")])
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("#1", report)
        self.assertIn("t_tri (triage", report)
        self.assertIn("#2", report)
        self.assertIn("t_done is done", report)
        self.assertEqual("", self.tick(FakeCommands([pr(1, "tri"), pr(2, "dn")])))

    def test_ownerless_pr_is_adopted_on_a_local_branch_once_per_head(self):
        # The operator left the PR's branch checked out (clean) in the board's own checkout, as
        # the Silk root was for #3579; the card must not need that branch checked out.
        sha = self.remote_branch("ci/fix")
        fake = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        report = self.tick(fake)
        self.assertEqual(sha, self.git("rev-parse", "refs/heads/adopt/pr-3"))
        self.assertEqual("ci/fix", self.git("branch", "--show-current"))  # operator's checkout untouched
        [create] = fake.writes
        self.assertEqual(["hermes", "kanban", "--board", "example", "create"], create[:5])
        flags = dict(zip(create[6::2], create[7::2]))
        self.assertEqual("builder", flags["--assignee"])
        self.assertEqual("worktree", flags["--workspace"])
        self.assertEqual("adopt/pr-3", flags["--branch"])
        self.assertEqual("pr-conflict-sweep", flags["--created-by"])
        self.assertEqual(f"pr-conflict-sweep:3:{sha}", flags["--idempotency-key"])
        body = flags["--body"]
        for needle in ("## Goal", "## Acceptance criteria", pr(3, "ci/fix")["url"],
                       "git push origin HEAD:refs/heads/ci/fix", "git merge origin/main",
                       "package-lock.json", "rebase-regenerate.md"):
            self.assertIn(needle, body)
        [posted] = fake.pr_comments
        self.assertIn("t_new", posted[-1])
        self.assertIn("**builder**", posted[-1])
        self.assertIn("adopted as t_new", report)
        again = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        self.assertEqual("", self.tick(again))
        self.assertEqual([], again.writes)

    def test_open_adoption_card_owns_the_pr(self):
        self.card("t_adopt", "running", assignee="builder", branch="adopt/pr-3", claim="lock")
        fake = FakeCommands([pr(3, "ci/fix")])
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_finished_adoption_card_is_replaced_when_the_pr_conflicts_again(self):
        sha = self.remote_branch("ci/fix")
        self.card("t_old", "done", assignee="builder", branch="adopt/pr-3")
        fake = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        self.tick(fake)
        self.assertEqual([("create", None)], [(argv[4], None) for argv in fake.writes])

    def test_dirty_checkout_of_the_head_is_left_alone(self):
        sha = self.remote_branch("ci/fix")
        (self.clone / "wip.txt").write_text("in progress\n")
        fake = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("uncommitted changes", report)
        self.assertNotIn("adopt/pr-3", self.git("branch", "--list"))

    def test_unpushed_local_commits_are_left_alone(self):
        sha = self.remote_branch("ci/fix")
        self.git("commit", "--quiet", "--allow-empty", "-m", "local only")
        fake = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        report = self.tick(fake)
        self.assertEqual([], fake.writes)
        self.assertIn("1 unpushed commit", report)

    def test_pr_that_moved_since_listing_waits_silently(self):
        self.remote_branch("ci/fix")
        fake = FakeCommands([pr(3, "ci/fix", sha="f" * 40)], repo=self.clone)
        self.assertEqual("", self.tick(fake))
        self.assertEqual([], fake.writes)

    def test_failed_adoption_card_create_is_retried(self):
        sha = self.remote_branch("ci/fix")
        report = self.tick(FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone, create_fails=True))
        self.assertIn("board locked", report)
        retry = FakeCommands([pr(3, "ci/fix", sha=sha)], repo=self.clone)
        self.tick(retry)
        self.assertEqual("create", retry.writes[0][4])

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
