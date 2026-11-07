"""lib/preamble.py with lib/agent_tools.py: the Tools block rendered from <workdir>/.tmux-pi/agent-tools.json."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "agent_tools"
sys.path.insert(0, str(ROOT / "lib"))
import agent_tools  # noqa: E402
import preamble  # noqa: E402

TOOL = {"name": "demo", "path": "$TP_ROOT/tools/agent/lane-info.sh", "roles": ["coder"], "use": "orient",
        "usage": "WORKTREE", "output": "a summary"}
EXAMPLE = ROOT / "templates" / "agent-tools.example.json"
STANDING_LINES = (
    "Do not re-request an unchanged range you already have; fetch more or refreshed context when you need it (after an edit, after a compaction).",
    "Read a range with the Read tool (offset, limit) or safe_read, not sed -n. Quote paths with ( ) [ ] or spaces.",
    "tp-agent request --kind skill|tool|context --what DESCRIPTION --reason REASON",
    "tp-agent message TASK --text TEXT replies to a question from Main; tools/agent/test-run.sh WORKTREE -- CMD runs a check and records it.",
)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.work = self.tmp / "work"
        self.work.mkdir()

    def put(self, name, text, where=None):
        path = (where or self.work) / ".tmux-pi" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def preamble_file(self, where=None):
        return self.put("worker-preamble.md", (FIXTURES / "worker-preamble.md").read_text(), where)

    def manifest(self, tools, where=None):
        return self.put("agent-tools.json", json.dumps(tools), where)


class GoldenTests(Base):
    """With no manifest the text is byte-identical to what lib/preamble.py gave before the Tools block existed."""

    def test_no_manifest_is_byte_identical_for_every_role(self):
        self.preamble_file()
        for role in ("coder", "tester", "researcher", ""):
            with self.subTest(role=role or "none"):
                expected = (FIXTURES / f"golden-{role or 'none'}.txt").read_bytes()
                self.assertEqual(preamble.load(self.work, role).encode(), expected)

    def test_coder_block_with_the_fixture_manifest_is_byte_identical(self):
        # golden-coder-with-tools.txt holds the full Tools block (the four standing lines and the entries);
        # $TP_ROOT stands for this checkout's path so the fixture does not depend on where the repo lives.
        self.preamble_file()
        self.manifest(json.loads((FIXTURES / "manifest.json").read_text()))
        text = preamble.load(self.work, "coder").replace(str(ROOT), "$TP_ROOT")
        self.assertEqual(text.encode(), (FIXTURES / "golden-coder-with-tools.txt").read_bytes())

    def test_no_manifest_cli_is_byte_identical(self):
        self.preamble_file()
        out = subprocess.run([sys.executable, str(ROOT / "lib" / "preamble.py"), str(self.work), "coder"], capture_output=True)
        self.assertEqual((out.returncode, out.stdout), (0, (FIXTURES / "golden-coder.txt").read_bytes()))

    def test_a_copy_of_preamble_py_alone_works_without_a_manifest_and_refuses_with_one(self):
        # tests/test_tp_review.py runs a copy of lib/preamble.py with no other lib file beside it.
        lone = self.tmp / "lone" / "lib"
        lone.mkdir(parents=True)
        shutil.copy(ROOT / "lib" / "preamble.py", lone / "preamble.py")
        (lone.parent / "agent").mkdir()
        shutil.copytree(ROOT / "agent" / "roles", lone.parent / "agent" / "roles")
        self.preamble_file()
        run = lambda: subprocess.run([sys.executable, str(lone / "preamble.py"), str(self.work), "coder"], capture_output=True)
        out = run()
        self.assertEqual((out.returncode, out.stdout), (0, (FIXTURES / "golden-coder.txt").read_bytes()))
        self.manifest([TOOL])
        out = run()
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(out.stdout, b"")

    def test_no_manifest_and_no_preamble_adds_nothing(self):
        self.assertEqual(preamble.load(self.work, "coder"), "")

    def test_prior_task_arguments_add_nothing_without_a_manifest(self):
        self.preamble_file()
        state = self.tmp / "state"
        (state / "tasks" / "p1").mkdir(parents=True)
        (state / "tasks" / "p1" / "result.md").write_text("done")
        self.assertEqual(preamble.load(self.work, "coder", None, str(state), "p1").encode(), (FIXTURES / "golden-coder.txt").read_bytes())


class RenderTests(Base):
    def test_the_block_follows_the_preamble_and_is_filtered_by_role(self):
        self.preamble_file()
        self.manifest(json.loads((FIXTURES / "manifest.json").read_text()))
        coder = preamble.load(self.work, "coder")
        golden = (FIXTURES / "golden-coder.txt").read_text()
        self.assertTrue(coder.startswith(golden + "\n\n## Tools\n\n"))
        for name in ("lane-info", "pre-commit-gate", "web-jest"):
            self.assertIn(f"- {name}: ", coder)
        researcher = preamble.load(self.work, "researcher")
        self.assertIn("- lane-info: ", researcher)
        self.assertNotIn("web-jest", researcher)
        self.assertNotIn("pre-commit-gate", researcher)
        reviewer = preamble.load(self.work, "code-reviewer")
        self.assertNotIn("- lane-info", reviewer)

    def test_every_role_gets_the_four_standing_lines_byte_for_byte(self):
        self.manifest([TOOL])
        for role in ("coder", "researcher", "code-reviewer", ""):
            with self.subTest(role=role):
                text = preamble.load(self.work, role)
                self.assertTrue(text.startswith("## Tools\n\n" + "\n".join(STANDING_LINES)))
                self.assertNotIn("Read a file once", text)

    def test_the_example_manifest_loads_for_every_role_and_names_the_four_tools(self):
        self.manifest(json.loads(EXAMPLE.read_text()))
        roles = sorted(p.stem for p in (ROOT / "agent" / "roles").glob("*.md"))
        self.assertTrue(roles)
        for role in roles:
            with self.subTest(role=role):
                text = preamble.load(self.work, role)
                self.assertLess(len(text.encode()), preamble.MAX_BYTES)
                print(f"example block bytes {role}: {len(text.encode())}", file=sys.stderr)
        coder = preamble.load(self.work, "coder")
        for name in ("lane-info", "pre-commit-gate", "web-jest", "test-run"):
            self.assertIn(f"- {name}: ", coder)
        self.assertIn(f"{ROOT}/tools/agent/test-run.sh WORKTREE [--timeout S] -- CMD...", coder)

    def test_every_entry_of_the_example_manifest_is_within_the_size_limit(self):
        for entry in json.loads(EXAMPLE.read_text())["tools"]:
            size = sum(len(entry[k].encode()) for k in ("name", "use", "usage", "output"))
            self.assertLessEqual(size, agent_tools.ENTRY_MAX, entry["name"])

    def test_the_listed_test_run_script_is_the_real_one_and_no_role_names_tp_agent_test_run(self):
        script = ROOT / "tools" / "agent" / "test-run.sh"
        self.assertTrue(os.access(script, os.X_OK))
        out = subprocess.run([str(script), str(self.work), "--", "true"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)  # not a git worktree: refused with the exit line last, not "not implemented"
        self.assertNotIn("not implemented", out.stdout)
        self.assertEqual(out.stdout.splitlines()[-1], "exit=2 log=-")
        for role in (ROOT / "agent" / "roles").glob("*.md"):
            self.assertNotIn("tp-agent test-run", role.read_text(), role.name)
        for role in ("coder", "tester"):
            self.assertIn("tools/agent/test-run.sh", (ROOT / "agent" / "roles" / f"{role}.md").read_text())

    def test_manifest_without_a_preamble_file_still_renders(self):
        self.manifest([TOOL])
        text = preamble.load(self.work, "coder")
        self.assertTrue(text.startswith("## Tools\n\n"))
        self.assertIn("- demo: orient: ", text)

    def test_entry_line_has_the_resolved_path_usage_and_output(self):
        self.manifest([TOOL])
        self.assertIn(f"- demo: orient: {ROOT}/tools/agent/lane-info.sh WORKTREE -> a summary", preamble.load(self.work, "coder"))

    def test_an_absolute_path_and_a_usage_that_starts_with_the_script_name(self):
        script = self.tmp / "mine.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        self.manifest([dict(TOOL, path=str(script), usage="mine.sh A B")])
        self.assertIn(f"- demo: orient: {script} A B -> a summary", preamble.load(self.work, "coder"))

    def test_the_block_is_deterministic(self):
        self.manifest(json.loads((FIXTURES / "manifest.json").read_text()))
        self.assertEqual(preamble.load(self.work, "coder"), preamble.load(self.work, "coder"))

    def test_the_manifest_of_the_working_tree_wins_over_source(self):
        source = self.tmp / "main"
        source.mkdir()
        self.manifest([dict(TOOL, name="from-source")], source)
        self.manifest([dict(TOOL, name="from-work")])
        text = preamble.load(self.work, "coder", str(source))
        self.assertIn("from-work", text)
        self.assertNotIn("from-source", text)

    def test_source_is_read_when_the_working_tree_has_no_manifest(self):
        source = self.tmp / "main"
        source.mkdir()
        self.manifest([dict(TOOL, name="from-source")], source)
        self.assertIn("- from-source: ", preamble.load(self.work, "coder", str(source)))


class RefusalTests(Base):
    def assertRefused(self, *needles, role="coder"):
        with self.assertRaises(ValueError) as caught:
            preamble.load(self.work, role)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))

    def test_a_path_that_does_not_exist_is_an_error(self):
        self.manifest([dict(TOOL, path="$TP_ROOT/tools/agent/nope.sh")])
        self.assertRefused("nope.sh", "does not exist")

    def test_a_file_that_is_not_executable_is_an_error(self):
        plain = self.tmp / "plain.sh"
        plain.write_text("x")
        plain.chmod(0o644)
        self.manifest([dict(TOOL, path=str(plain))])
        self.assertRefused("plain.sh", "not an executable file")

    def test_a_directory_is_not_an_executable_file(self):
        self.manifest([dict(TOOL, path=str(self.tmp))])
        self.assertRefused("not an executable file")

    def test_a_bad_entry_is_refused_even_for_a_role_it_is_not_listed_for(self):
        self.manifest([dict(TOOL, path="$TP_ROOT/tools/agent/nope.sh", roles=["tester"])])
        self.assertRefused("nope.sh", role="coder")
        self.assertRefused("nope.sh", role="researcher")

    def test_a_relative_path_is_an_error(self):
        self.manifest([dict(TOOL, path="tools/agent/lane-info.sh")])
        self.assertRefused("absolute", "$TP_ROOT")

    def test_another_variable_in_a_path_is_an_error(self):
        self.manifest([dict(TOOL, path="/x/$HOME/y.sh")])
        self.assertRefused("variable other than $TP_ROOT")

    def test_unknown_role_duplicate_name_missing_and_extra_fields(self):
        cases = {
            "unknown role 'wizard'": [dict(TOOL, roles=["wizard"])],
            "appears twice": [TOOL, TOOL],
            "missing use": [{k: v for k, v in TOOL.items() if k != "use"}],
            "unknown bonus": [dict(TOOL, bonus="x")],
            "name must be": [dict(TOOL, name="Bad Name")],
            "roles must be a non-empty list": [dict(TOOL, roles=[])],
            "one line": [dict(TOOL, use="two\nlines")],
            "must be a non-empty string": [dict(TOOL, output="")],
            "must be an object": ["demo"],
        }
        for needle, tools in cases.items():
            with self.subTest(needle):
                self.manifest(tools)
                self.assertRefused(needle)

    def test_invalid_json_and_a_wrong_shape(self):
        self.put("agent-tools.json", "{not json")
        self.assertRefused("not valid JSON")
        self.put("agent-tools.json", '"just a string"')
        self.assertRefused("list of entries")
        self.put("agent-tools.json", '{"tools": [], "more": 1}')
        self.assertRefused("exactly one key")

    def test_not_utf8(self):
        (self.work / ".tmux-pi").mkdir()
        (self.work / ".tmux-pi" / "agent-tools.json").write_bytes(b"\xff\xfe[]")
        self.assertRefused("not valid UTF-8")

    def test_an_entry_over_the_size_limit_is_refused_and_one_at_the_limit_is_not(self):
        base = sum(len(TOOL[k]) for k in ("name", "use", "usage", "output"))
        self.manifest([dict(TOOL, output="o" * (TOOL["output"].__len__() + agent_tools.ENTRY_MAX - base))])
        preamble.load(self.work, "coder")
        self.manifest([dict(TOOL, output="o" * (TOOL["output"].__len__() + agent_tools.ENTRY_MAX - base + 1))])
        self.assertRefused("the limit is %d" % agent_tools.ENTRY_MAX)

    def test_the_rendered_text_over_the_cap_is_refused_for_every_role(self):
        # Each entry is within its own limit; together with a large preamble they pass 8,000 bytes for the coder role only.
        self.put("worker-preamble.md", "x" * 6000 + "\n\n## Role: coder\n\n" + "y" * 800)
        tools = [dict(TOOL, name=f"tool-{i}", roles=["coder"], use="u" * 60, usage="a" * 40, output="o" * 30) for i in range(12)]
        self.manifest(tools)
        self.assertRefused("coder", str(preamble.MAX_BYTES))
        # The tester gets no entry of this manifest, yet the text of the role that does not fit refuses it too.
        self.assertRefused("coder", str(preamble.MAX_BYTES), role="tester")

    def test_a_manifest_that_fits_under_the_cap_is_accepted(self):
        self.preamble_file()
        self.manifest(json.loads((FIXTURES / "manifest.json").read_text()))
        for role in ("coder", "tester", "researcher"):
            self.assertLess(len(preamble.load(self.work, role).encode()), preamble.MAX_BYTES)

    def test_the_preamble_file_cap_still_applies(self):
        self.put("worker-preamble.md", "x" * (preamble.MAX_BYTES + 1))
        self.manifest([TOOL])
        self.assertRefused("limit is 8000")

    def test_cli_refuses_with_a_message_and_a_nonzero_exit(self):
        self.manifest([dict(TOOL, path="$TP_ROOT/tools/agent/nope.sh")])
        out = subprocess.run([sys.executable, str(ROOT / "lib" / "preamble.py"), str(self.work), "coder"], capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("nope.sh", out.stderr)
        self.assertEqual(out.stdout, "")


class PriorTaskTests(Base):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.manifest([TOOL])

    def task(self, ident, meta=None, result=False, report=False):
        directory = self.state / "tasks" / ident
        directory.mkdir(parents=True, exist_ok=True)
        if meta is not None:
            (directory / "meta.json").write_text(json.dumps(meta))
        if result:
            (directory / "result.md").write_text("result")
        if report:
            (directory / "report.md").write_text("report")
        return directory

    def block(self, parent):
        return preamble.load(self.work, "coder", None, str(self.state), parent)

    def test_no_parent_means_no_prior_line(self):
        self.task("p1", {"id": "p1"}, result=True)
        self.assertNotIn("Prior result", self.block(None))
        self.assertNotIn("Prior result", preamble.load(self.work, "coder"))

    def test_parent_with_a_result_and_a_review_report(self):
        p = self.task("p1", {"id": "p1", "reviews": ["r1"]}, result=True)
        r = self.task("r1", {"id": "r1"}, report=True)
        self.assertTrue(self.block("p1").endswith(f"Prior result: {p / 'result.md'}; review report: {r / 'report.md'}"))

    def test_the_newest_review_is_the_one_named_and_a_single_review_key_works(self):
        self.task("p1", {"reviews": ["r1", "r2"]}, result=True)
        self.task("r1", report=True)
        r2 = self.task("r2", report=True)
        self.assertIn(f"review report: {r2 / 'report.md'}", self.block("p1"))
        self.task("p2", {"review": "r1"}, result=True)
        self.assertIn(f"review report: {self.state / 'tasks' / 'r1' / 'report.md'}", self.block("p2"))

    def test_a_parent_without_reviews_names_only_the_result(self):
        p = self.task("p1", {"id": "p1"}, result=True)
        line = self.block("p1").splitlines()[-1]
        self.assertEqual(line, f"Prior result: {p / 'result.md'}")

    def test_the_review_of_a_retry_ancestor_is_found(self):
        self.task("p0", {"reviews": ["r0"]}, result=True)
        r0 = self.task("r0", report=True)
        self.task("p1", {"parent": "p0"}, result=True)
        self.assertIn(f"review report: {r0 / 'report.md'}", self.block("p1"))

    def test_a_file_that_does_not_exist_is_not_named(self):
        self.task("p1", {"reviews": ["r1"]}, result=False)
        self.task("r1", report=False)
        self.assertNotIn("Prior result", self.block("p1"))
        self.assertNotIn("review report", self.block("p1"))

    def test_an_unknown_or_unsafe_parent_adds_no_line(self):
        for parent in ("missing", "../escape", "a b", ""):
            with self.subTest(parent=parent):
                self.assertNotIn("Prior result", self.block(parent))

    def test_a_loop_in_the_lineage_ends(self):
        self.task("a", {"parent": "b"})
        self.task("b", {"parent": "a"})
        self.assertNotIn("review report", self.block("a"))

    def test_cli_passes_the_prior_task_through(self):
        p = self.task("p1", {}, result=True)
        out = subprocess.run([sys.executable, str(ROOT / "lib" / "preamble.py"), str(self.work), "coder",
                              "--state-dir", str(self.state), "--parent", "p1"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"Prior result: {p / 'result.md'}", out.stdout)


if __name__ == "__main__":
    unittest.main()
