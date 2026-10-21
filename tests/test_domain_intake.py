"""Domain intake decisions against deployment files and real board databases."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from test_integrations_config import fixture, SCRIPTS


class DomainIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.config = fixture(self.home)
        self.settings = self.config["linear-intake"]
        self.settings["label_to_board"] = {"domain:ingestion": "example-ingestion"}
        self.settings["lead_by_board"] = {"example-ingestion": "ingestion-lead"}
        self.settings["contract_by_board"]["example-ingestion"] = "example/repo"
        self.board("example")
        self.board("example-ingestion")

    def board(self, name, archived=False):
        directory = self.home / "kanban/boards" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "board.json").write_text(json.dumps({"archived": archived}))
        with sqlite3.connect(directory / "kanban.db") as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS tasks "
                         "(id TEXT, title TEXT, status TEXT, idempotency_key TEXT, created_by TEXT)")
        return directory

    def evaluate(self, expression, issue=None):
        config = self.home / "deployment.json"
        config.write_text(json.dumps(self.config))
        code = ("import sys,runpy,json; sys.path.insert(0,sys.argv[1]); "
                "m=runpy.run_path(sys.argv[1]+'/linear-intake.py'); "
                "issue=json.loads(sys.argv[2]); print(json.dumps(eval(sys.argv[3],m,{'issue':issue})))")
        env = {**os.environ, "HERMES_DEPLOYMENT_CONFIG": str(config),
               "HERMES_HOME": str(self.home)}
        result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS),
                                 json.dumps(issue), expression], env=env,
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def issue(self, *labels):
        return {"identifier": "TEST-2", "team": {"key": "TEST"},
                "labels": {"nodes": [{"name": label} for label in labels]}}

    def test_label_selects_board_and_unlabeled_issues_keep_team_fallback(self):
        for labels, expected in [((), "example"), (("bug",), "example"),
                                  (("Domain:Ingestion",), "example-ingestion")]:
            with self.subTest(labels=labels):
                self.assertEqual(self.evaluate("route_board(issue)", self.issue(*labels)),
                                 [expected, None])

    def test_unknown_and_conflicting_domains_fall_back_with_a_reason(self):
        for labels in [("domain:unknown",), ("domain:ingestion", "domain:core")]:
            with self.subTest(labels=labels):
                board, reason = self.evaluate("route_board(issue)", self.issue(*labels))
                self.assertEqual(board, "example")
                self.assertTrue(reason)

    def test_unavailable_or_unrelated_destination_never_receives_work(self):
        for destination in ["missing", "../outside", "archived", "other-project"]:
            with self.subTest(destination=destination):
                self.settings["label_to_board"]["domain:ingestion"] = destination
                self.settings["contract_by_board"][destination] = "example/repo"
                if destination == "archived":
                    self.board(destination, archived=True)
                if destination == "other-project":
                    self.board(destination)
                    self.settings["contract_by_board"][destination] = "other/repo"
                board, reason = self.evaluate("route_board(issue)", self.issue("domain:ingestion"))
                self.assertEqual(board, "example")
                self.assertTrue(reason)

    def test_unmapped_team_cannot_be_routed_by_a_domain_label(self):
        issue = self.issue("domain:ingestion")
        issue["team"]["key"] = "OTHER"
        self.assertEqual(self.evaluate("route_board(issue)", issue), [None, None])

    def test_domain_lead_receives_intake_before_implementation(self):
        issue = self.issue("domain:ingestion")
        issue["description"] = "An implementation-ready task"
        self.assertEqual(self.evaluate("assignee_for(issue, 'example-ingestion')", issue),
                         "ingestion-lead")
        self.assertEqual(self.evaluate("assignee_for(issue, 'example')", issue), "builder")

    def test_deployments_without_domain_mapping_keep_existing_routing(self):
        del self.settings["label_to_board"]
        self.assertEqual(self.evaluate("route_board(issue)", self.issue()), ["example", None])

    def test_existing_work_is_found_across_boards_even_with_prefixed_titles(self):
        directory = self.home / "kanban/boards/example"
        with sqlite3.connect(directory / "kanban.db") as conn:
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                         ("existing", "[Imports] TEST-1: Existing work", "running", None, "planner"))
        self.assertEqual(self.evaluate("covering_card('example-ingestion', 'TEST-1')"),
                         ["example", "existing"])
        with sqlite3.connect(directory / "kanban.db") as conn:
            conn.execute("UPDATE tasks SET status='archived'")
        self.assertIsNone(self.evaluate("covering_card('example-ingestion', 'TEST-1')"))

    def test_cross_board_blocker_waits_for_linear_completion(self):
        directory = self.home / "kanban/boards/example"
        with sqlite3.connect(directory / "kanban.db") as conn:
            conn.execute("INSERT INTO tasks VALUES (?,?,?,?,?)",
                         ("parent", "TEST-1: Dependency", "running", "TEST-1", "linear-intake"))
        issue = self.issue("domain:ingestion")
        issue["inverseRelations"] = {"nodes": [{"type": "blocks", "issue": {
            "identifier": "TEST-1", "state": {"type": "started"}}}]}
        parents, reason = self.evaluate("blocker_cards('example-ingestion', issue)", issue)
        self.assertEqual(parents, [])
        self.assertIn("example", reason)
        issue["inverseRelations"]["nodes"][0]["issue"]["state"]["type"] = "completed"
        self.assertEqual(self.evaluate("blocker_cards('example-ingestion', issue)", issue), [[], None])

    def test_unreadable_existing_board_refuses_duplicate_lookup(self):
        (self.home / "kanban/boards/example/kanban.db").write_text("not a database")
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.evaluate("covering_card('example-ingestion', 'TEST-1')")
        self.assertIn("Cannot check existing work on example", error.exception.stderr)


class DomainReviewTests(unittest.TestCase):
    def test_delivery_uses_board_and_bot_profile_and_retries_failed_sends(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / "deployment.json"
            config.write_text(json.dumps(fixture(home)))
            database = home / "kanban/boards/example-ingestion/kanban.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as conn:
                conn.executescript("""
                    CREATE TABLE tasks (id TEXT, title TEXT, status TEXT, assignee TEXT);
                    CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT);
                    CREATE TABLE task_runs (id INTEGER, task_id TEXT, metadata TEXT);
                    INSERT INTO tasks VALUES ('review-card','Domain review','review','operator');
                    INSERT INTO task_events VALUES (7,'review-card','review_requested',
                        '{"reviewer":"operator","implementer":"builder","summary":"Ready"}');
                """)
            binary = home / "bin"
            binary.mkdir()
            sender = binary / "hermes"
            sender.write_text("#!" + sys.executable + "\nimport json,os,sys\n"
                              "with open(os.environ['SEND_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                              "sys.exit(int(os.environ.get('FAIL_SEND','0')))\n")
            sender.chmod(0o755)
            env = {**os.environ, "HERMES_DEPLOYMENT_CONFIG": str(config),
                   "HERMES_HOME": str(home), "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                   "KANBAN_REVIEW_BOARD": "example-ingestion",
                   "KANBAN_REVIEW_NOTIFY_TARGET": "test:ingestion",
                   "KANBAN_REVIEW_NOTIFY_PROFILE": "lead", "SEND_LOG": str(home / "sends.jsonl")}
            hook = SCRIPTS.parent / "agent-hooks/kanban-review-parked.sh"
            cursor = home / "agent-hooks/state/kanban-review-parked.example-ingestion.cursor"
            for fail, expected_cursor in [("1", "0"), ("0", "7"), ("0", "7")]:
                result = subprocess.run(["sh", str(hook)], env={**env, "FAIL_SEND": fail},
                                        input="{}", capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(cursor.read_text(), expected_cursor)
            calls = [json.loads(line) for line in (home / "sends.jsonl").read_text().splitlines()]
            self.assertEqual(len(calls), 2)
            for call in calls:
                self.assertEqual(call[:3], ["-p", "lead", "send"])
                self.assertEqual(call[call.index("-t") + 1], "test:ingestion")


if __name__ == "__main__":
    unittest.main()
