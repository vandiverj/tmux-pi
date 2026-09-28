import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class DispatcherR1Fixtures(unittest.TestCase):
    def test_dispatcher_has_single_raw_request_accessor_and_uses_it_for_handoffs(self):
        src = (ROOT / "agent" / "extensions" / "dispatcher.ts").read_text(encoding="utf-8")
        self.assertIn("function requestText(dir: string)", src)
        self.assertIn('readText(join(dir, "request.md"))', src)
        # The only direct task.md read is inside requestText's legacy fallback.
        self.assertEqual(src.count('readText(join(dir, "task.md"))'), 1)
        self.assertIn("const request = requestText(dir);", src)
        self.assertIn("task: requestText(join(tasks, id))", src)
        self.assertIn("request: requestText(dir)", src)

    def test_review_classifier_uses_request_md_not_canonical_record(self):
        src = (ROOT / "bin" / "tp-review").read_text(encoding="utf-8")
        self.assertIn('route_file="$dir/request.md"', src)
        self.assertIn('from task_record import request_text', src)
        self.assertIn('--context "$dir/task.md"', src)


if __name__ == "__main__":
    unittest.main()
