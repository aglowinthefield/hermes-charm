#!/bin/sh
# Focused tests for Linear intake's native branch and notification fields.
set -eu
python3 - "$(dirname "$0")/../scripts/linear-intake.py" <<'PY'
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

# The fixture is independent of deployment or operator settings.
fixture_home = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
fixture_config = Path(fixture_home.name) / "deployment.json"
fixture_config.write_text(json.dumps({"linear-intake": {
    "home": fixture_home.name,
    "op_ref": "op://Test/Linear/credential",
    "team_to_board": {"TEST": "example"},
    "contract_by_board": {"example": "example/repo"},
    "implementer": "builder", "implementer_label": "Builder",
    "orchestrator": "planner", "orchestrator_label": "Planner",
    "hold_labels": ["needs-triage", "needs-info"],
    "reviewer": "auditor", "review_path": str(Path(fixture_home.name) / "REVIEW.md"),
    "notify_platform": "test-platform", "notify_chat_id": "test-chat",
    "notify_chat_type": "channel", "notifier_profile": "test-notifier",
    "delivery_mode": "notify", "net_fails_before_alert": 3,
    "notifier": str(Path(fixture_home.name) / "absent-notifier")
}}))
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))

spec = importlib.util.spec_from_file_location("linear_intake", sys.argv[1])
assert spec and spec.loader
linear_intake = importlib.util.module_from_spec(spec)
with patch.dict(os.environ, {"HERMES_DEPLOYMENT_CONFIG": str(fixture_config),
                             "HERMES_HOME": fixture_home.name}):
    spec.loader.exec_module(linear_intake)


def issue(*, children=None):
    return {
        "id": "linear-id",
        "identifier": "TEST-123",
        "title": "Fix card hydration & retries",
        "url": "https://example.test/issue/TEST-123/fix-card-hydration",
        "description": "Implement it",
        "priorityLabel": "High",
        "state": {"name": "Todo"},
        "labels": {"nodes": []},
        "project": {"name": "Web"},
        "parent": None,
        "children": {"nodes": children or []},
        "team": {"key": "TEST"},
    }


class LinearIntakeTest(unittest.TestCase):
    def fake_create(self, command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"id": "t_direct", "created_at": linear_intake.RUN_START}),
            stderr="",
        )

    def test_direct_implementation_uses_native_branch_field(self):
        with patch.object(linear_intake, "branch_taken", return_value=False), \
             patch.object(linear_intake.subprocess, "run", side_effect=self.fake_create) as run:
            task_id, created = linear_intake.create_card("example", issue())
        self.assertEqual((task_id, created), ("t_direct", True))
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--branch") + 1], "test-123-fix-card-hydration-retries")
        self.assertEqual(command[command.index("--assignee") + 1], "builder")
        self.assertIn("--workspace", command)

    def test_orchestration_exception_does_not_force_a_branch(self):
        with patch.object(linear_intake.subprocess, "run", side_effect=self.fake_create) as run:
            linear_intake.create_card(
                "example", issue(children=[{"id": "child", "identifier": "TEST-124"}])
            )
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--assignee") + 1], "planner")
        self.assertNotIn("--branch", command)
        self.assertNotIn("--workspace", command)

    def test_subscription_uses_native_terminal_event_delivery(self):
        with patch.object(
            linear_intake.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ) as run:
            linear_intake.subscribe_card("example", "t_direct")
        self.assertEqual(
            run.call_args.args[0],
            [
                "hermes", "kanban", "--board", "example", "notify-subscribe", "t_direct",
                "--platform", "test-platform", "--chat-id", "test-chat",
                "--chat-type", "channel", "--notifier-profile", "test-notifier",
                "--delivery-mode", "notify",
            ],
        )

    def test_subscription_failure_is_retryable(self):
        with patch.object(
            linear_intake.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 2, stdout="", stderr="delivery unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "delivery unavailable"):
                linear_intake.subscribe_card("example", "t_direct")

    def test_existing_reconciliation_targets_only_the_exact_intake_root(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            home = Path(directory)
            board = home / "kanban" / "boards" / "example"
            board.mkdir(parents=True)
            db = board / "kanban.db"
            conn = sqlite3.connect(db)
            conn.executescript("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, status TEXT, created_by TEXT,
                    idempotency_key TEXT, branch_name TEXT
                );
                CREATE TABLE kanban_notify_subs (
                    task_id TEXT, platform TEXT, chat_id TEXT
                );
                INSERT INTO tasks VALUES
                    ('t_manual', 'ready', 'planner', 'TEST-123', 'test-123-fix-card-hydration-retries'),
                    ('t_child', 'ready', 'planner', 'TEST-123/part', NULL),
                    ('t_root', 'ready', 'linear-intake', 'TEST-123', NULL);
            """)
            conn.commit()
            with patch.object(linear_intake, "HOME", home):
                self.assertEqual(linear_intake.intake_root_on_board("example", "TEST-123"), "t_root")
                collision_safe = linear_intake.branch_for("example", issue())
                self.assertRegex(collision_safe, r"^test-123-fix-card-hydration-retries-[0-9a-f]{8}$")
                self.assertFalse(linear_intake.card_is_subscribed("example", "t_root"))
                conn.execute(
                    "INSERT INTO kanban_notify_subs VALUES (?, ?, ?)",
                    ("t_root", "test-platform", "test-chat"),
                )
                conn.commit()
                self.assertTrue(linear_intake.card_is_subscribed("example", "t_root"))
            conn.close()

    def test_existing_open_pull_request_is_treated_as_external_work(self):
        pull_request = {
            "number": 3332,
            "url": "https://github.com/example/repo/pull/3332",
            "state": "OPEN",
            "title": "feat: implement TEST-123",
            "body": "Closes TEST-123",
            "mergedAt": None,
        }
        with patch.object(
            linear_intake.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["gh"], 0, stdout=json.dumps([pull_request]), stderr=""
            ),
        ) as run:
            reason = linear_intake.existing_external_work("example", issue())
        self.assertIn("open PR #3332", reason)
        self.assertEqual(run.call_args.args[0][:4], ["gh", "pr", "list", "--repo"])

    def test_malformed_pull_request_lookup_fails_open(self):
        with patch.object(
            linear_intake.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["gh"], 0, stdout="null", stderr=""
            ),
        ):
            self.assertEqual(linear_intake.matching_pull_requests("example", "TEST-123"), [])

    def test_existing_merged_pull_request_is_treated_as_external_work(self):
        pull_request = {
            "number": 3332,
            "url": "https://github.com/example/repo/pull/3332",
            "state": "MERGED",
            "title": "feat: implement TEST-123",
            "body": "Closes TEST-123",
            "mergedAt": "2026-01-01T00:00:00Z",
        }
        with patch.object(
            linear_intake.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                ["gh"], 0, stdout=json.dumps([pull_request]), stderr=""
            ),
        ):
            reason = linear_intake.existing_external_work("example", issue())
        self.assertEqual(reason, "merged PR #3332")

    def test_existing_repository_branch_is_treated_as_external_work(self):
        command_results = {
            "gh": subprocess.CompletedProcess(["gh"], 0, stdout="[]", stderr=""),
            "git": subprocess.CompletedProcess(
                ["git"], 0, stdout="upstream/test-123-fix-card-hydration\n", stderr=""
            ),
        }

        def fake_run(command, **kwargs):
            return command_results[command[0]]

        with patch.object(linear_intake, "board_repo", return_value=Path("/repo")), \
             patch.object(linear_intake.subprocess, "run", side_effect=fake_run):
            reason = linear_intake.existing_external_work("example", issue())
        self.assertIn("repository branch", reason)
        self.assertIn("upstream/test-123-fix-card-hydration", reason)

    def test_main_skips_issue_with_external_work(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"seen": []}))
            with patch.object(linear_intake, "STATE", state), \
                 patch.object(linear_intake, "linear_key", return_value="key"), \
                 patch.object(linear_intake, "fetch_open_assigned", return_value=[issue()]), \
                 patch.object(linear_intake, "intake_root_on_board", return_value=None), \
                 patch.object(linear_intake, "already_on_board", return_value=None), \
                 patch.object(linear_intake, "existing_external_work", return_value="open PR #3332"), \
                 patch.object(linear_intake, "create_card") as create, \
                 patch.object(linear_intake, "notify"):
                output = io.StringIO()
                with redirect_stdout(output):
                    linear_intake.main()
                saved = json.loads(state.read_text())

        create.assert_not_called()
        self.assertIn("TEST-123 already has external work (open PR #3332); skipped", output.getvalue())
        self.assertIn("TEST-123", saved["seen"])

    def test_held_issue_dispatches_only_after_its_label_is_removed(self):
        labels = [{"name": "Needs-Triage"}]
        def assigned(_key):
            held_issue = issue()
            held_issue["labels"] = {"nodes": list(labels)}
            return [held_issue]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"seen": []}))
            with patch.object(linear_intake, "STATE", state), \
                 patch.object(linear_intake, "linear_key", return_value="key"), \
                 patch.object(linear_intake, "fetch_open_assigned", side_effect=assigned), \
                 patch.object(linear_intake, "intake_root_on_board", return_value=None), \
                 patch.object(linear_intake, "already_on_board", return_value=None), \
                 patch.object(linear_intake, "subscribe_card"), \
                 patch.object(linear_intake, "notify"), \
                 patch.object(linear_intake, "create_card",
                              return_value=("t_new", True)) as create:
                def tick():
                    out = io.StringIO()
                    with redirect_stdout(out):
                        linear_intake.main()
                    return out.getvalue(), json.loads(state.read_text())

                printed, saved = tick()
                create.assert_not_called()
                self.assertIn("TEST-123 held by label 'Needs-Triage'", printed)
                self.assertNotIn("TEST-123", saved["seen"])

                printed, _ = tick()
                create.assert_not_called()
                self.assertEqual(printed, "", "a hold is reported once, not every tick")

                labels[:] = [{"name": "ready-for-agent"}]
                printed, saved = tick()
                create.assert_called_once()
                self.assertIn("example/t_new <- TEST-123", printed)
                self.assertIn("TEST-123", saved["seen"])
                self.assertEqual(saved["held"], {})


def blocked(identifier, *blockers):
    """An issue blocked by (identifier, state type) pairs, as Linear's inverseRelations report them."""
    item = issue()
    item["identifier"] = identifier
    item["inverseRelations"] = {"nodes": [
        {"type": "blocks", "issue": {"identifier": ident, "state": {"type": state}}}
        for ident, state in blockers
    ]}
    return item


class LinearIntakeBlockerTest(unittest.TestCase):
    """Linear 'blocked by' relations become kanban parent edges, so a dependent card waits in todo."""

    def run_ticks(self, assigned, cards):
        """Tick main() against fixed assigned issues; `cards` maps identifier -> live board card."""
        created = []

        def create(_board, item, parents=()):
            task_id = f"t_{item['identifier'].lower()}"
            created.append((item["identifier"], list(parents)))
            cards[item["identifier"]] = task_id
            return task_id, True

        directory = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(directory.cleanup)
        state = Path(directory.name) / "state.json"
        state.write_text(json.dumps({"seen": []}))
        patches = [
            patch.object(linear_intake, "STATE", state),
            patch.object(linear_intake, "linear_key", return_value="key"),
            patch.object(linear_intake, "fetch_open_assigned", side_effect=lambda _k: assigned),
            patch.object(linear_intake, "intake_root_on_board", return_value=None),
            patch.object(linear_intake, "already_on_board", side_effect=lambda _b, i: cards.get(i)),
            patch.object(linear_intake, "existing_external_work", return_value=None),
            patch.object(linear_intake, "subscribe_card"),
            patch.object(linear_intake, "notify"),
            patch.object(linear_intake, "create_card", side_effect=create),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        def tick():
            out = io.StringIO()
            with redirect_stdout(out):
                linear_intake.main()
            return out.getvalue(), json.loads(state.read_text())

        return created, tick

    def test_open_blocker_with_a_card_becomes_the_parent(self):
        created, tick = self.run_ticks([blocked("TEST-2", ("TEST-1", "started"))], {"TEST-1": "t_one"})
        tick()
        self.assertEqual(created, [("TEST-2", ["t_one"])])

    def test_finished_blocker_is_ignored(self):
        created, tick = self.run_ticks(
            [blocked("TEST-2", ("TEST-1", "completed"), ("TEST-3", "canceled"))], {})
        tick()
        self.assertEqual(created, [("TEST-2", [])])

    def test_blocker_and_dependent_assigned_together_link_in_one_tick(self):
        # Dependent listed first: the chain must still resolve blockers-first within the tick.
        assigned = [blocked("TEST-3", ("TEST-2", "triage")),
                    blocked("TEST-2", ("TEST-1", "triage")),
                    blocked("TEST-1")]
        created, tick = self.run_ticks(assigned, {})
        tick()
        self.assertEqual(created, [("TEST-1", []), ("TEST-2", ["t_test-1"]), ("TEST-3", ["t_test-2"])])

    def test_open_blocker_without_a_card_holds_until_the_card_exists(self):
        cards = {}
        created, tick = self.run_ticks([blocked("TEST-2", ("TEST-1", "todo"))], cards)
        printed, saved = tick()
        self.assertEqual(created, [])
        self.assertIn("TEST-2 held by open blocker TEST-1", printed)
        self.assertNotIn("TEST-2", saved["seen"])

        printed, _ = tick()
        self.assertEqual(printed, "", "a blocker hold is reported once, not every tick")

        cards["TEST-1"] = "t_one"
        printed, saved = tick()
        self.assertEqual(created, [("TEST-2", ["t_one"])])
        self.assertIn("TEST-2", saved["seen"])
        self.assertEqual(saved["held"], {})

    def test_create_card_passes_each_parent(self):
        with patch.object(linear_intake, "branch_taken", return_value=False), \
             patch.object(linear_intake.subprocess, "run",
                          side_effect=LinearIntakeTest.fake_create.__get__(self)) as run:
            linear_intake.create_card("example", issue(), parents=["t_a", "t_b"])
        command = run.call_args.args[0]
        self.assertEqual([command[i + 1] for i, arg in enumerate(command) if arg == "--parent"],
                         ["t_a", "t_b"])


suite = unittest.TestSuite([
    unittest.defaultTestLoader.loadTestsFromTestCase(LinearIntakeTest),
    unittest.defaultTestLoader.loadTestsFromTestCase(LinearIntakeBlockerTest),
])
result = unittest.TextTestRunner(verbosity=2).run(suite)
fixture_home.cleanup()
raise SystemExit(not result.wasSuccessful())
PY
