"""Configured integrations run without a personal Hermes home."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def fixture(home):
    # No notification destination: card notifications belong to Aide, not these integrations.
    routing = {"home": str(home), "implementer": "builder", "implementer_label": "Builder",
               "reviewer": "auditor", "review_path": str(home / "REVIEW.md"),
               "net_fails_before_alert": 3}
    return {
        "linear-intake": {**routing, "op_ref": "op://Test/Linear/credential",
                          "team_to_board": {"TEST": "example"},
                          "contract_by_board": {"example": "example/repo"},
                          "orchestrator": "planner", "orchestrator_label": "Planner",
                          "hold_labels": ["needs-triage"],
                          "notifier": str(home / "absent-notifier")},
        "ci-watchdog": {**routing, "repo": "example/repo", "board": "example",
                        "contract": "example/repo", "daily_card_cap": 2,
                        "log_excerpt_cap": 3500, "watched_workflows": ["Tests", "Deploy"],
                        "deploy_workflows": ["Deploy"], "trap_notes": "Configured repo notes"},
        "review-notifications": {"home": str(home), "board": "example",
                                 "parking_assignee": "operator", "target": "test:review",
                                 "send_timeout": 8, "max_sends": 2},
    }


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / "deployment.json"
        self.config.write_text(json.dumps({"linear-intake": {"home": str(self.home)}}))
        self.env = {**os.environ, "HERMES_DEPLOYMENT_CONFIG": str(self.config)}
        for name in ["HERMES_HOME", "KANBAN_REVIEW_BOARD", "KANBAN_REVIEW_PARKING_ASSIGNEE",
                     "KANBAN_REVIEW_NOTIFY_TARGET"]:
            self.env.pop(name, None)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPTS / "deployment_settings.py"), *args],
                              env=env or self.env, text=True, capture_output=True)

    def test_helper_reads_explicit_configuration(self):
        self.assertTrue((SCRIPTS / "deployment_settings.py").is_file(),
                        "The deployment configuration helper must exist")
        result = self.cli("linear-intake", "home")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.home))

    def test_configured_home_expands_tilde(self):
        self.config.write_text(json.dumps({'review-notifications': {'home': '~/.hermes'}}))
        result = self.cli('review-notifications', 'home', env={**self.env, 'HOME': str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.home / '.hermes'))

    def test_configured_home_reaches_child_hermes_commands(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        issue = {'identifier': 'TEST-1', 'title': 'Example', 'description': 'Implement',
                 'url': 'https://example.test/issue', 'labels': {'nodes': []},
                 'state': {'name': 'Todo'}, 'priorityLabel': 'High', 'children': {'nodes': []}}
        for filename in ('linear-intake.py', 'ci-watchdog.py'):
            with self.subTest(filename=filename):
                module = self.load(filename)
                with patch.dict(os.environ, self.env, clear=True), patch.object(
                        module.subprocess, 'run', return_value=subprocess.CompletedProcess(
                            [], 0, stdout=json.dumps({'id': 'card', 'created_at': module.RUN_START}), stderr='')) as run:
                    if filename == 'linear-intake.py':
                        with patch.object(module, 'branch_taken', return_value=False):
                            module.create_card('example', issue)
                    else:
                        module.sh(['hermes', 'kanban', '--board', 'example', 'list'])
                    for call in run.call_args_list:
                        env = call.kwargs.get('env', {})
                        self.assertEqual(env.get('HERMES_HOME'), str(self.home))
                        self.assertEqual(env.get('HERMES_KANBAN_HOME'), str(self.home))

    def test_missing_or_invalid_configuration_refuses(self):
        env = dict(self.env)
        env.pop("HERMES_DEPLOYMENT_CONFIG")
        result = self.cli("linear-intake", "home", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HERMES_DEPLOYMENT_CONFIG", result.stderr)
        for text in ["{", "[]", "{}", '{"linear-intake": {"home": 42}}']:
            with self.subTest(text=text):
                self.config.write_text(text)
                result = self.cli("linear-intake", "home")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("deployment", result.stderr.lower())
        self.config.unlink()
        self.assertNotEqual(self.cli("linear-intake", "home").returncode, 0)

    def test_python_get_returns_structured_settings_and_cli_prints_json(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        spec = importlib.util.spec_from_file_location("settings_test", SCRIPTS / "deployment_settings.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(module.get("linear-intake", "team_to_board"), {"TEST": "example"})
            self.assertEqual(module.get("ci-watchdog", "daily_card_cap"), 2)
            with self.assertRaisesRegex(module.ConfigurationError, "requires"):
                module.get("linear-intake", "absent")
        result = self.cli("ci-watchdog", "watched_workflows")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["Tests", "Deploy"])

    def load(self, filename):
        spec = importlib.util.spec_from_file_location(filename.replace("-", "_"), SCRIPTS / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "path", [str(SCRIPTS), *sys.path]):
            spec.loader.exec_module(module)
        return module

    def test_integrations_refuse_missing_config_before_runtime_work(self):
        env = dict(self.env)
        env.pop("HERMES_DEPLOYMENT_CONFIG")
        for filename in ["scripts/linear-intake.py", "scripts/ci-watchdog.py",
                         "agent-hooks/kanban-review-parked.sh"]:
            with self.subTest(filename=filename):
                self.assertTrue((ROOT / filename).is_file(), "Packaged integration must exist")
                command = ["sh" if filename.endswith(".sh") else sys.executable, str(ROOT / filename)]
                result = subprocess.run(command, env=env, input="{}", capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("HERMES_DEPLOYMENT_CONFIG", result.stderr)
        self.assertFalse((self.home / "kanban").exists())

    def test_linear_commands_use_only_fixture_destinations(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        module = self.load("linear-intake.py")
        self.assertEqual(module.HOME, self.home)
        self.assertEqual(module.TEAM_TO_BOARD, {"TEST": "example"})
        issue = {"identifier": "TEST-1", "title": "Example", "description": "Linear-only spec text",
                 "url": "https://example.test/issue", "labels": {"nodes": []},
                 "state": {"name": "Todo"}, "priorityLabel": "High", "children": {"nodes": []}}
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps({"id": "card", "created_at": module.RUN_START}), stderr="")) as run, \
                patch.object(module, "branch_taken", return_value=False):
            module.create_card("example", issue)
            create = run.call_args.args[0]
        self.assertIn("builder", create)
        self.assertIn("example/repo", create)
        self.assertIn('reviewer="auditor"', module.build_body(issue, "builder"))
        self.assertIn(str(self.home / "REVIEW.md"), module.build_body(issue, "builder"))
        # The card links the issue instead of copying it; workers read it live from Linear.
        for assignee in ["builder", "planner"]:
            body = module.build_body(issue, assignee)
            self.assertNotIn("Linear-only spec text", body)
            self.assertIn("https://example.test/issue", body)
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, stdout="test-key", stderr="")) as run, patch.dict(os.environ, {}, clear=True):
            self.assertEqual(module.linear_key(), "test-key")
        self.assertEqual(run.call_args.args[0], ["op", "read", "op://Test/Linear/credential"])

    def test_card_instructions_use_the_linked_task_platform(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        module = self.load("linear-intake.py")
        for assignee, description, children in [
                ("builder", "Implement", []),
                ("planner", "", []),
                ("planner", "Plan children", [{"identifier": "TEST-2"}])]:
            with self.subTest(assignee=assignee, description=description):
                issue = {"identifier": "TEST-1", "title": "Example", "description": description,
                         "url": "https://example.test/issue", "labels": {"nodes": []},
                         "state": {"name": "Todo"}, "priorityLabel": "High",
                         "children": {"nodes": children}}
                body = module.build_body(issue, assignee)
                self.assertIn("[TEST-1](https://example.test/issue)", body)
                self.assertIn("task-tracking platform", body)
                self.assertIn("before starting", body)
                self.assertNotIn("linear", body.lower())

    def test_ci_live_write_path_is_mocked_and_state_is_isolated(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        module = self.load("ci-watchdog.py")
        module.save_state({"shadow": False, "seen": []})
        failed = {"databaseId": 1, "workflowName": "Tests", "conclusion": "failure",
                  "headSha": "a" * 40, "createdAt": "2026-01-01T00:00:00Z",
                  "url": "https://example.test/run/1", "displayTitle": "Test commit"}
        calls = []
        def fake_run(command, **kwargs):
            calls.append(command)
            if command[:3] == ["gh", "run", "list"]:
                stdout = json.dumps([failed])
            elif command[:3] == ["gh", "run", "view"]:
                stdout = "##[error] Type error: fixture failure"
            elif "create" in command:
                stdout = json.dumps({"id": "card", "created_at": module.RUN_START})
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")
        with patch.object(module.subprocess, "run", side_effect=fake_run), redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(), 0)
        self.assertEqual(module.STATE, self.home / "kanban/ci-watchdog-state.json")
        self.assertEqual(module.load_state()["seen"], ["1"])
        self.assertIn("example/repo", calls[0])
        create = next(c for c in calls if "create" in c)
        self.assertIn("builder", create)
        body = create[create.index("--body") + 1]
        self.assertIn("Configured repo notes", body)
        self.assertIn('reviewer="auditor"', body)
        # Card notifications belong to Aide: filing a card adds no Hermes delivery subscription.
        self.assertIs(calls[-1], create)
        self.assertFalse(any("notify-subscribe" in c for c in calls))

    def test_review_hook_reads_configuration_with_environment_overrides(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        binary = self.home / "bin"
        binary.mkdir()
        sqlite = binary / "sqlite3"
        sqlite.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_SQL_LOG"\nprintf "[]"\n')
        sqlite.chmod(0o755)
        for overrides, board, assignee in [({}, "example", "operator"),
                ({"KANBAN_REVIEW_BOARD": "override", "KANBAN_REVIEW_PARKING_ASSIGNEE": "human",
                  "KANBAN_REVIEW_NOTIFY_TARGET": "test:override"}, "override", "human")]:
            database = self.home / "kanban/boards" / board / "kanban.db"
            database.parent.mkdir(parents=True, exist_ok=True)
            database.touch()
            env = {**self.env, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "TEST_SQL_LOG": str(self.home / "sql.log"), **overrides}
            result = subprocess.run(["sh", str(ROOT / "agent-hooks/kanban-review-parked.sh")],
                                    env=env, input="{}", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            logged = (self.home / "sql.log").read_text()
            self.assertIn(str(database), logged)
            self.assertIn("t.assignee = '" + assignee + "'", logged)

    def test_review_hook_sends_only_to_configured_fixture_target(self):
        self.config.write_text(json.dumps(fixture(self.home)))
        database = self.home / "kanban/boards/example/kanban.db"
        database.parent.mkdir(parents=True)
        database.touch()
        binary = self.home / "bin"
        binary.mkdir()
        row = {"event_id": 7, "task_id": "fixture-card", "title": "Fixture review",
               "implementer": "builder", "summary": "Mock send only"}
        sqlite = binary / "sqlite3"
        sqlite.write_text('#!/bin/sh\nif [ "$1" = "-json" ]; then\n' +
                          "printf '%s' '" + json.dumps([row]) + "'\nfi\n")
        jq = binary / "jq"
        jq.write_text('#!' + sys.executable + '\nimport json, sys\n' +
                      'data = json.load(sys.stdin)\nquery = sys.argv[-1]\n' +
                      'if query == ".[]":\n    for row in data: print(json.dumps(row))\n' +
                      'else:\n    key = query.split(".", 1)[1].split()[0]\n' +
                      '    print(data.get(key, ""))\n')
        hermes = binary / "hermes"
        hermes.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_SEND_LOG"\nprintf "%s" "$HERMES_HOME" > "$TEST_SEND_HOME"\n')
        for executable in [sqlite, jq, hermes]:
            executable.chmod(0o755)
        for target in [None, "test:override"]:
            env = {**self.env, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "TEST_SEND_LOG": str(self.home / "send.log"), 'TEST_SEND_HOME': str(self.home / 'send-home')}
            if target:
                env["KANBAN_REVIEW_NOTIFY_TARGET"] = target
                (self.home / "agent-hooks/state/kanban-review-parked.example.cursor").unlink()
            result = subprocess.run(["sh", str(ROOT / "agent-hooks/kanban-review-parked.sh")],
                                    env=env, input="{}", capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(target or "test:review", (self.home / "send.log").read_text().splitlines())
            self.assertEqual((self.home / 'send-home').read_text(), str(self.home))
            self.assertEqual((self.home / "agent-hooks/state/kanban-review-parked.example.cursor").read_text(), "7")


if __name__ == "__main__":
    unittest.main()
