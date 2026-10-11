"""Exercise the provider fallback watch against throwaway logs and board databases."""
from __future__ import annotations

from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("provider_fallback_watch", ROOT / "scripts" / "provider-fallback-watch.py")
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)

FELL = ("2026-10-11 12:14:39,285 INFO [20261011_121431_fb1a68] agent.chat_completion_helpers: "
        "Fallback activated: claude-opus-5-5 → gpt-6.1-sol (openai-codex)\n")
AUTH = ("2026-10-11 12:20:01,000 WARNING cli: Primary provider auth failed (Anthropic credentials "
        "are rate-limited for claude-opus-5-5; other Claude models remain available) falling back\n")


class FallbackWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.boards = self.home / "kanban" / "boards"
        self.state = self.home / "kanban" / "provider-fallback-watch.json"
        self.log = self.home / "profiles" / "scribe" / "logs" / "agent.log"
        self.log.parent.mkdir(parents=True)
        self.log.write_text("2026-10-11 09:00:00,000 INFO Fallback activated: old → history (kimi-coding)\n")

    def tearDown(self):
        self.temp.cleanup()

    def board(self, name, tasks):
        path = self.boards / name
        path.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path / "kanban.db")) as conn:
            conn.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, status TEXT, "
                         "provider_override TEXT, model_override TEXT)")
            conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", tasks)
            conn.commit()

    def tick(self):
        return watch.run(self.home, self.boards, self.state)

    def append(self, text):
        with self.log.open("a") as handle:
            handle.write(text)

    def test_first_tick_skips_history_then_reports_each_new_fallback_once(self):
        self.assertEqual(self.tick(), "")
        self.append(FELL + FELL + AUTH)
        report = self.tick()
        self.assertIn("- scribe: Fallback activated: claude-opus-5-5 → gpt-6.1-sol (openai-codex) ×2 "
                      "(last 2026-10-11 12:14)", report)
        self.assertIn("- scribe: Primary provider auth failed (Anthropic credentials are rate-limited "
                      "for claude-opus-5-5; other Claude models remain available) (last 2026-10-11 12:20)", report)
        self.assertNotIn("history", report)
        self.assertEqual(self.tick(), "")

    def test_rotated_log_is_read_from_the_start(self):
        self.tick()
        self.log.rename(self.log.with_suffix(".log.1"))  # what Hermes' RotatingFileHandler does
        self.log.write_text("x" * 500 + "\n" + FELL)
        self.assertIn("claude-opus-5-5 → gpt-6.1-sol", self.tick())

    def test_reports_each_open_pin_once_until_cleared(self):
        self.board("coda", [
            ("t_kimi", "scribe", "ready", "kimi-coding", "kimi-k2.6"),
            ("t_routed", "princess", "todo", "anthropic", "claude-sonnet-4-6"),
            ("t_done", "scribe", "done", "kimi-coding", "kimi-k3"),
            ("t_free", "scribe", "todo", None, None),
        ])
        report = self.tick()
        self.assertIn("- coda/t_kimi: kimi-coding/kimi-k2.6 (ready, scribe)", report)
        self.assertIn("- coda/t_routed: anthropic/claude-sonnet-4-6 (todo, princess)", report)
        self.assertNotIn("t_done", report)
        self.assertEqual(self.tick(), "")


if __name__ == "__main__":
    unittest.main()
