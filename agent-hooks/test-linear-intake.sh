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


suite = unittest.defaultTestLoader.loadTestsFromTestCase(LinearIntakeTest)
result = unittest.TextTestRunner(verbosity=2).run(suite)
fixture_home.cleanup()
raise SystemExit(not result.wasSuccessful())
PY
