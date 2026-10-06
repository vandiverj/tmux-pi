"""The fixed-form card the router reads: what goes in, what is left out, and what is never sent."""
import tempfile
import unittest
from pathlib import Path

import route_fixture  # noqa: F401  (puts lib on sys.path)

import route_card as rc  # noqa: E402


class CardTests(unittest.TestCase):
    def test_role_first_sentence_constraints_and_deliverable_only(self):
        card = rc.build_card("coder", "Add pagination to the orders endpoint. Colour the buttons blue. Do not edit billing. "
                             "Never touch the schema. Mention the weather.", "Report paths. Then relax.")
        self.assertEqual(card.splitlines(), ["Role: coder", "Task: Add pagination to the orders endpoint.",
                                             "Constraints: Do not edit billing. | Never touch the schema.", "Deliverable: Report paths."])

    def test_long_paths_are_shortened_and_lengths_are_capped(self):
        task = "Read /Users/someone/Projects/app/.tmux-pi/sessions/0123456789abcdef/tasks/coder-1/brief.md and start. " + "x" * 400
        card = rc.build_card(None, task, "")
        self.assertIn("Role: worker", card)
        self.assertIn("coder-1/brief.md", card)
        self.assertNotIn("/Users/someone", card)
        self.assertLessEqual(len(card.splitlines()[1]), len("Task: ") + 200)

    def test_secret_shaped_values_never_reach_the_router(self):
        secret = "ghp_" + "a1B2" * 9
        card = rc.build_card("coder", "Use the token %s to call the API. Never log it." % secret, "Done. Key sk-ant-%s" % ("z9" * 15))
        self.assertNotIn(secret, card)
        self.assertNotIn("sk-ant-z9", card)
        self.assertIn("[secret]", card)

    def test_the_head_of_an_attached_brief_is_included_when_the_task_says_to_read_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            brief = Path(tmp) / "build-brief.md"
            brief.write_text("# Build brief\n\nReplace the model choice.\n" + "\n".join("line %d" % i for i in range(60)))
            other = Path(tmp) / "notes.md"
            other.write_text("not a brief")
            card = rc.build_card("coder", "Read the attached build brief first. Then implement it.", "Report.", [str(other), str(brief)], tmp)
            line = next(l for l in card.splitlines() if l.startswith("Brief: "))
            self.assertIn("# Build brief / Replace the model choice. / line 0", line)
            self.assertNotIn("line 30", line)
            self.assertLessEqual(len(line), len("Brief: ") + rc.BRIEF_CHARS)
            self.assertNotIn("not a brief", card)
            # relative specs with a line range resolve against the workdir; a task that does not say to read one adds nothing
            self.assertIn("Brief: ", rc.build_card("coder", "Read the brief first.", "", ["build-brief.md:1-5"], tmp))
            self.assertNotIn("Brief: ", rc.build_card("coder", "Fix the typo in the README.", "", [str(brief)], tmp))

    def test_a_sensitive_file_is_never_read_for_the_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("TOKEN=sekrit-value-1234567890")
            named = Path(tmp) / "brief.env"
            named.write_text("PASSWORD=hunter2hunter2")
            card = rc.build_card("coder", "Read the brief first.", "", [str(env), str(named)], tmp)
            self.assertNotIn("Brief: ", card)
            self.assertNotIn("sekrit", card)
            self.assertNotIn("hunter2", card)

    def test_the_card_hash_is_stable(self):
        card = rc.build_card("coder", "Fix it.", "Report.")
        self.assertEqual(rc.card_sha(card), rc.card_sha(rc.build_card("coder", "Fix it.", "Report.")))
        self.assertEqual(len(rc.card_sha(card)), 64)


if __name__ == "__main__":
    unittest.main()
