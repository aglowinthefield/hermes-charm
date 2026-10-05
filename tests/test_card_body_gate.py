import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "agent-hooks" / "kanban-card-body-gate.sh"
BODY = "## Goal\nx\n\n## Acceptance criteria\n- y\n"


def skill(root: Path, rel: str) -> None:
    path = root / rel / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nname: x\n---\n")


class CardBodyGateSkillPins(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "hermes"
        self.repo = Path(self.tmp.name) / "repo"
        skill(self.home, "skills/devops/home-skill")
        skill(self.repo, ".hermes/skills/repo-hermes-skill")
        skill(self.repo, ".agents/skills/repo-agents-skill")
        skill(Path(self.tmp.name) / "untrusted", ".hermes/skills/untrusted-skill")

    def tearDown(self):
        self.tmp.cleanup()

    def run_gate(self, pins, config=None):
        if config is not None:
            (self.home / "config.yaml").write_text(config)
        env = {k: v for k, v in os.environ.items() if k != "HOOK_SKILLS_DIRS"}
        env["HERMES_HOME"] = str(self.home)
        payload = {"tool_name": "kanban_create", "tool_input": {"body": BODY, "assignee": "scribe", "skills": pins}}
        return subprocess.run([sys.executable, str(GATE)], input=json.dumps(payload), capture_output=True, text=True, env=env)

    def trusted(self, extra=""):
        return (
            "model: x\nskills:\n  external_dirs:\n    - ~/.agents/skills\n"
            f"  trusted_project_dirs:\n    - {self.repo}\n{extra}  project_discovery: true\nother: 1\n"
        )

    def test_home_skill_resolves(self):
        self.assertEqual(self.run_gate(["home-skill"]).returncode, 0)

    def test_project_skills_in_trusted_dirs_resolve(self):
        result = self.run_gate(["repo-hermes-skill", "repo-agents-skill"], self.trusted())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_project_skills_need_a_trusted_dir(self):
        self.assertEqual(self.run_gate(["repo-hermes-skill"], "skills:\n  project_discovery: true\n").returncode, 2)
        self.assertEqual(self.run_gate(["repo-hermes-skill"]).returncode, 2)

    def test_symlinked_skill_resolves_like_the_loader(self):
        # Hermes's loader walks with followlinks=True; a skill shared by link must resolve too.
        skill(self.repo, ".claude/skills/linked-skill")
        os.symlink("../../.claude/skills/linked-skill", self.repo / ".hermes/skills/linked-skill")
        result = self.run_gate(["linked-skill"], self.trusted())
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_untrusted_project_skill_is_blocked(self):
        self.assertEqual(self.run_gate(["untrusted-skill"], self.trusted()).returncode, 2)

    def test_unknown_pin_is_blocked(self):
        result = self.run_gate(["no-such-skill"], self.trusted())
        self.assertEqual(result.returncode, 2)
        self.assertIn("no-such-skill", result.stderr)


if __name__ == "__main__":
    unittest.main()
