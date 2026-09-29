"""The QA command is an on-demand Pi skill, not a permanently expanded system prompt."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class MakeQaSkillTests(unittest.TestCase):
    def test_short_slash_alias_loads_discoverable_skill_on_demand(self):
        skill = (ROOT / "skills/make-qa/SKILL.md").read_text()
        alias = (ROOT / "agent/prompts/make-qa.md").read_text()
        launcher = (ROOT / "bin/tp-pi").read_text()
        self.assertTrue(skill.startswith("---\nname: make-qa\n"))
        self.assertIn("description:", skill.split("---", 2)[1])
        self.assertTrue(alias.startswith("---\n"))
        self.assertIn("advertised `make-qa` skill", alias)
        self.assertIn("Read its SKILL.md before drafting", alias)
        self.assertIn("${@:-", alias)
        self.assertIn('--skill "$ROOT/skills"', launcher)

    def test_skill_does_not_grant_ticket_or_deployment_authority(self):
        skill = (ROOT / "skills/make-qa/SKILL.md").read_text()
        self.assertIn("Do not update a ticket", skill)
        self.assertIn("Do not push, deploy, release", skill)
        self.assertIn("Tester-owned setup is the default", skill)
        self.assertIn("Separate **script-content blockers**", skill)


if __name__ == "__main__":
    unittest.main()
