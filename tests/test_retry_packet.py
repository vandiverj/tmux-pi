"""tp-agent retry: a retry that carries its fix brief (--reason naming an .md file) in the request it starts with, and the
gate on a finding family a reviewer names twice. The fixture is the orchestration one (fake tmux, no model runs)."""
import json
import os
from pathlib import Path
import pwd
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestration  # noqa: E402  (the module, so its own tests are not collected twice)

MODEL = test_orchestration.MODEL
OLD = "Make a focused change"


class RetryCase(unittest.TestCase):
    for _name in ("setUp", "raw", "cli", "task_dir", "packet", "settle", "dispatch", "retry"):
        locals()[_name] = test_orchestration.OrchestrationTests.__dict__[_name]
    del _name

    def request(self, item):
        return (self.task_dir(item) / "request.md").read_text()

    def settled(self):
        item = self.dispatch()
        self.settle(item, "RESULT_STATUS: complete\nDone.\n")
        return item

    def fix_brief(self, name="fix.md", text="# Fix\n\nDo the other thing.\n"):
        path = self.root / name
        path.write_text(text)
        return str(path)


class FixBriefTests(RetryCase):
    def sentence(self, brief):
        return f"Read and carry out the fix brief {brief} in full. It replaces the task below where they differ."

    def test_a_reason_naming_a_brief_file_starts_the_new_request_with_it(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(self.request(again), f"{self.sentence(brief)}\n\n{OLD}\n")
        self.assertIn(f"# Task\n\n{self.sentence(brief)}\n\n{OLD}\n", self.packet(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))  # the Notes line stays

    def test_with_task_the_request_is_that_task_only(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief, "--task", "T")
        self.assertEqual(self.request(again), "T\n")
        self.assertNotIn("fix brief", self.request(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))
        self.assertNotIn("not used as a fix brief", self.packet(again))

    def test_a_plain_text_reason_leaves_the_request_as_it_was(self):
        for reason in ("plain words", "fix.md", "see /abs/fix.md for what changed", "/abs/fix.txt"):
            with self.subTest(reason=reason):
                item = self.settled()
                (self.root / "fix.md").write_text("relative, in the wrong place\n")
                again = self.retry(item, reason)
                self.assertEqual(self.request(again), f"{OLD}\n")
                self.assertNotIn("fix brief", self.packet(again))

    def test_a_refused_file_is_not_used_and_the_notes_say_why(self):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        secret = self.root / ".env.md"
        secret.write_text("SECRET-FIX-BRIEF\n")
        (self.root / "dir.md").mkdir()
        (self.root / "empty.md").write_text("")
        cases = [(str(secret), "is a .env file"),
                 (str(home / ".config/tmux-pi/trust/never-written-fix.md"), "is inside the trust store"),  # never created or read
                 (str(home / ".config/tmux-pi/state/trust/never-written-fix.md"), "is inside the trust store"),
                 (str(self.root / "missing.md"), "does not exist"),
                 (str(self.root / "dir.md"), "is not a regular file"),
                 (str(self.root / "empty.md"), "is empty")]
        for path, why in cases:
            with self.subTest(path=path):
                item = self.settled()
                again = self.retry(item, path)
                self.assertEqual(self.request(again), f"{OLD}\n")
                packet = self.packet(again)
                self.assertNotIn("Read and carry out the fix brief", packet)
                self.assertNotIn("SECRET-FIX-BRIEF", packet)
                self.assertIn("not used as a fix brief", packet)
                self.assertIn(why, packet)

    def test_a_retry_of_a_retry_with_plain_words_keeps_the_fix_brief_of_the_attempt_it_replaces(self):
        item = self.settled()
        brief = self.fix_brief()
        first = self.retry(item, brief)
        self.settle(first, "RESULT_STATUS: complete\nDone.\n")
        second = self.retry(first, "still wrong")
        self.assertEqual(self.request(second), self.request(first))

    def test_the_review_of_the_retry_reads_the_fix_brief_it_is_named_in(self):
        sys.path.insert(0, str(Path(test_orchestration.ROOT) / "lib"))
        import review_verdict
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(review_verdict.named_briefs(self.request(again)), [brief])


if __name__ == "__main__":
    unittest.main()
