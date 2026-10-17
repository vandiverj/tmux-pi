"""lib/index_text.py and `tp-agent index --text`: the index as the text the main agent reads.

The plugin module's startup append renders the same text in TypeScript (readIndex in claude/plugins/tmux-pi/hooks/register.tsx).
hooks/index-text-cases.ts holds the cases both renderers must answer identically: this file checks the Python renderer against them,
register.test.ts (`claude plugin test`) checks the module against the same file. Every store and state directory is temporary."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import index_text  # noqa: E402

CASES = ROOT / "claude" / "plugins" / "tmux-pi" / "hooks" / "index-text-cases.ts"


def units(text):
    return len(text.encode("utf-16-le")) // 2


class SharedCases(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = CASES.read_text()
        assert source.startswith("export default ")
        cls.cases = json.loads(source[len("export default "):])

    def test_there_are_cases_for_every_part_of_the_text(self):
        names = " | ".join(case["name"] for case in self.cases)
        for part in ("paused tier 1", "paused tier 2", "paused tier 3", "needs_input saturates", "refused", "work: recovery", "decisions",
                     "work: error only", "astral", "active cut", "C0 controls", "lone surrogates", "objective cut"):
            self.assertIn(part, names)

    def test_the_python_renderer_matches_every_shared_case(self):
        for case in self.cases:
            with self.subTest(case["name"]):
                self.assertEqual(index_text.render(case["index"]), case["text"])

    def test_no_case_exceeds_the_budget(self):
        for case in self.cases:
            with self.subTest(case["name"]):
                self.assertLessEqual(units(case["text"]), index_text.INDEX_CHARS)

    def test_the_budget_holds_for_any_size_of_paused_group(self):
        busy = [{"id": f"researcher-{i:04d}-abcdef", "status": "working", "role": "researcher"} for i in range(200)]
        for n in (0, 1, 20, 25, 30, 35, 40, 100, 150, 200, 250, 300, 301, 302, 303, 304, 305, 320, 400, 900):
            paused = lambda count, prefix, result: [{"id": f"{prefix}{i:04d}", "role": "coder", "harness": "pi", "status": "paused",
                                                      "result_status": result, "result_path": f"/state/tasks/{prefix}{i:04d}/result.md"}
                                                     for i in range(count)]
            unstatused, answered = paused((n + 1) // 2, "coder-u", None), paused(n // 2, "coder-p", "blocked")
            text = index_text.render({"objective": "x", "active": busy, "unstatused": unstatused, "paused": answered, "pending": []})
            with self.subTest(n=n):
                self.assertLessEqual(units(text), 6000)
                missing = [r for r in unstatused + answered if r["id"] not in text]
                if missing:
                    self.assertIn(f"… {len(missing)} more workers waiting for you not shown; run tp-agent list to see them", text)
                else:
                    self.assertNotIn("workers waiting for you not shown", text)

    def test_lone_surrogates_and_controls_are_never_raised_on_and_never_reach_the_text(self):
        soup = "\ud800\udc00\udbff\x00\x1b\t\n\r\ud83d\ude00\ude00\ud83d" * 400
        index = {"objective": soup, "active": [{"id": soup, "role": soup, "status": "working", "reason": soup}],
                 "work": {"attached": [{"id": soup, "title": soup, "next": soup, "sessions": [soup]}], "skipped": {"count": 1},
                          "decisions": {"items": [{"id": soup, "priority": soup, "blocking": True}]}}}
        for limit in (6000, 3000, 1500):
            with self.subTest(limit=limit):
                text = index_text.render(index, limit)
                self.assertLessEqual(units(text), limit)
                text.encode("utf-8")  # strict: no lone surrogate is left
                self.assertEqual([c for c in text if ord(c) < 0x20 and c not in "\n\t"], [])

    def test_a_value_of_the_wrong_type_is_skipped_not_raised(self):
        for index in (None, [], "x", {"active": "x", "pending": 3, "work": "x", "objective": 7},
                      {"work": {"attached": [1, None], "decisions": "x", "notes": []}}):
            with self.subTest(index=index):
                self.assertTrue(index_text.render(index).startswith(index_text.HEADER))


class TextOption(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.state = self.tmp / "project" / ".tmux-pi" / "run-one"
        (self.state / "tasks").mkdir(parents=True)
        (self.tmp / "work-config.json").write_text("{}")
        self.env = {**os.environ, "TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(self.tmp / "work-config.json"),
                    "TP_STATE_DIR": str(self.state)}
        self.env.pop("TMUX", None)

    def cli(self, *args):
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run

    def test_index_text_is_the_rendering_of_the_json_and_writes_nothing(self):
        (self.state / "session-objective.md").write_text("ship the loader\n")
        self.cli("work", "new", "Loader", "--id", "loader")
        self.cli("work", "attach", "loader")
        self.cli("work", "compacted", "--reason", "precompact:auto")
        as_text = self.cli("index", "--no-write", "--text")
        as_json = json.loads(self.cli("index", "--no-write").stdout)
        self.assertEqual(as_text.stdout, index_text.render(as_json) + "\n")
        self.assertTrue(as_text.stdout.startswith(index_text.HEADER + "\nobjective: ship the loader\n"))
        self.assertIn("work attached to this session (tp-agent work show ID):\n- loader ", as_text.stdout)
        self.assertIn("recovery pending (workspace attached at ", as_text.stdout)
        self.assertEqual(as_text.stderr, "")
        self.assertFalse((self.state / "session-index.json").exists())

    def test_hook_json_is_the_finished_session_start_output_in_ascii(self):
        (self.state / "session-objective.md").write_text("ship the loader \u2014 \U0001f600\n")
        run = self.cli("index", "--no-write", "--hook-json")
        self.assertTrue(run.stdout.isascii())
        self.assertEqual(run.stdout.count("\n"), 1)
        self.assertEqual(json.loads(run.stdout), {"hookSpecificOutput": {
            "hookEventName": "SessionStart", "additionalContext": self.cli("index", "--no-write", "--text").stdout[:-1]}})
        self.assertFalse((self.state / "session-index.json").exists())

    def test_index_text_with_nothing_to_say_is_the_bare_index(self):
        self.assertEqual(self.cli("index", "--no-write", "--text").stdout, "\n".join([
            index_text.HEADER, "objective: (none set)", "replied without a result status: none", "paused: none", "needs input: none",
            "active: none", "pending: none"]) + "\n")


if __name__ == "__main__":
    unittest.main()
