"""T12: a failing test a worker says it does not own gets its own investigating worker (lib/failing_tests.py and the collect and
dispatch --investigate-failure paths of bin/tp-agent). Repositories are temporary ones; tmux is the private fixture server of
tests/lane_fixture.py, so no worker starts and no model runs."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_lane_writer import LaneCase, git  # noqa: E402
import failing_tests  # noqa: E402

BLOCK = """RESULT_STATUS: complete
Done.

FAILING_TESTS:
- test: tests.test_x.TestY.test_z
  command: python3 -m unittest tests.test_x
  unmodified_base: not checked
- test: tests.test_w
  command: python3 -m unittest tests.test_w
  unmodified_base: yes, failed on a clean checkout at abc1234
  note: flaky on the first run

Next paragraph, not part of the block.
"""


class ParseTests(unittest.TestCase):
    def test_a_block_gives_its_entries_and_stops_at_the_blank_line(self):
        found = failing_tests.parse(BLOCK)
        self.assertEqual([e["test"] for e in found["entries"]], ["tests.test_x.TestY.test_z", "tests.test_w"])
        self.assertEqual(found["entries"][1]["unmodified_base"], "yes, failed on a clean checkout at abc1234")
        self.assertEqual(found["entries"][1]["note"], "flaky on the first run")
        self.assertEqual(found["malformed"], [])
        self.assertNotIn("Next paragraph", found["text"])

    def test_no_block_and_an_empty_block_are_none(self):
        for text in ("RESULT_STATUS: complete\nAll passed.\n", "FAILING_TESTS: none\n", "FAILING_TESTS:\n\nRESULT_STATUS: complete\n", ""):
            with self.subTest(text=text):
                self.assertIsNone(failing_tests.parse(text))

    def test_a_decorated_header_and_a_wrapped_value_are_read(self):
        found = failing_tests.parse("**FAILING_TESTS:**\n- test: a.b\n  command: python3 -m unittest\n    a.b\n  unmodified_base: not checked\n")
        self.assertEqual(found["entries"][0]["command"], "python3 -m unittest a.b")
        self.assertEqual(found["malformed"], [])

    def test_prose_and_missing_fields_are_kept_as_text_and_named_never_raised(self):
        found = failing_tests.parse("FAILING_TESTS:\ntests.test_old fails, not mine\n")
        self.assertEqual(found["entries"][0]["test"], "tests.test_old fails, not mine")
        self.assertIn("entry 1 has no command, unmodified_base", found["malformed"][0])
        self.assertIn("tests.test_old fails", found["text"])
        found = failing_tests.parse("FAILING_TESTS: tests.test_inline\n")
        self.assertEqual(found["entries"][0]["test"], "tests.test_inline")

    def test_a_heading_or_another_status_line_ends_the_block(self):
        found = failing_tests.parse("FAILING_TESTS:\n- test: a\n  command: c\n  unmodified_base: not checked\nRESULT_STATUS: complete\n")
        self.assertEqual(len(found["entries"]), 1)
        self.assertNotIn("RESULT_STATUS", found["text"])


class InvestigateCase(LaneCase):
    def start(self, report=BLOCK, into="report.md"):
        """A coder dispatched in the scratch repository, its report written and the task settled."""
        item, task = self.dispatch(self.repo)
        (task / into).write_text(report)
        self.settle(task)
        return item["id"], task

    def investigate(self, ident, *more):
        return self.raw("dispatch", "--investigate-failure", ident, "--worker", "pi", "--model", MODEL, "--thinking", "medium", *more)

    def shell(self, command):
        env = dict(self.env)
        env["PATH"] = os.pathsep.join([str(ROOT / "bin"), env["PATH"]])
        return subprocess.run(["sh", "-c", command], env=env, text=True, capture_output=True, timeout=90)


class CollectTests(InvestigateCase):
    def test_collect_surfaces_the_block_and_the_notice_without_changing_anything_else(self):
        ident, _ = self.start()
        run = self.raw("collect", ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertEqual(value["status"], "completed")
        self.assertEqual(len(value["failing_tests"]["entries"]), 2)
        self.assertTrue(value["failing_tests"]["source"].endswith("report.md"))
        self.assertIn(f"{ident} reports failing tests it does not own", run.stderr)
        self.assertIn(f"tp-agent dispatch --investigate-failure {ident}", run.stderr)

    def test_the_final_reply_carries_the_block_when_there_is_no_report(self):
        item, task = self.dispatch(self.repo)
        self.settle(task, result=BLOCK)
        value = json.loads(self.raw("collect", item["id"]).stdout)
        self.assertTrue(value["failing_tests"]["source"].endswith("result.md"))

    def test_a_report_without_the_block_behaves_as_before(self):
        item, task = self.dispatch(self.repo)
        self.settle(task)
        run = self.raw("collect", item["id"])
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("failing_tests", json.loads(run.stdout))
        self.assertEqual(run.stderr, "")

    def test_a_malformed_block_is_shown_as_text_and_collect_still_succeeds(self):
        ident, _ = self.start("RESULT_STATUS: complete\nFAILING_TESTS:\nthe thing in tests/test_old is red\n")
        run = self.raw("collect", ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertEqual(value["status"], "completed")
        self.assertIn("tests/test_old is red", value["failing_tests"]["text"])
        self.assertTrue(value["failing_tests"]["malformed"])

    def test_the_index_row_of_a_settled_task_carries_the_count(self):
        ident, _ = self.start()
        index = self.cli("index", "--no-tick")
        self.assertEqual([r["failing_tests"] for r in index["pending"] if r["id"] == ident], [2])
        other, task = self.dispatch(self.repo)
        self.settle(task)
        index = self.cli("index", "--no-tick")
        self.assertNotIn("failing_tests", [r for r in index["pending"] if r["id"] == other["id"]][0])


class DispatchTests(InvestigateCase):
    def test_the_task_records_the_commit_it_started_from(self):
        item, task = self.dispatch(self.repo)
        self.assertEqual(json.loads((task / "dispatch.json").read_text())["base_commit"], git(self.repo, "rev-parse", "HEAD"))

    def test_dispatch_still_needs_a_role_and_a_task(self):
        run = self.raw("dispatch", "--task", "x")
        self.assertEqual(run.returncode, 2)
        self.assertIn("--role", run.stderr)


class InvestigateTests(InvestigateCase):
    def test_a_tester_starts_in_a_clean_worktree_at_the_base_with_the_diff_and_the_block(self):
        ident, task = self.start()
        base = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "lib/x.py").write_text("changed by the task\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "the task's change")
        (self.repo / "lib/y.py").write_text("uncommitted\n")
        git(self.repo, "add", "lib/y.py")
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        tree = Path(value["worktree"])
        self.assertEqual(git(tree, "rev-parse", "HEAD"), base)
        self.assertEqual((tree / "lib/x.py").read_text(), "x\n")
        self.assertEqual(git(tree, "status", "--porcelain"), "")
        patch = Path(value["diff"]).read_text()
        self.assertIn("+changed by the task", patch)
        self.assertIn("+uncommitted", patch)
        meta = json.loads((self.state / "tasks" / value["id"] / "meta.json").read_text())
        self.assertEqual(meta["role"], "tester")
        self.assertEqual(meta["parent"], ident)
        self.assertEqual(Path(meta["workdir"]).resolve(), tree.resolve())
        request = (self.state / "tasks" / value["id"] / "request.md").read_text()
        self.assertIn("tests.test_x.TestY.test_z", request)
        self.assertIn("python3 -m unittest tests.test_w", request)
        self.assertIn("unmodified base", request)
        self.assertEqual(json.loads((self.state / "tasks" / value["id"] / "dispatch.json").read_text())["investigates"], ident)
        self.assertEqual(value["base_commit"], base)

    def test_the_patch_carries_untracked_files_and_reproduces_the_tree_without_touching_the_workdir(self):
        ident, task = self.start()
        (self.repo / "lib/x.py").write_text("modified tracked\n")
        (self.repo / "lib/new_test.py").write_text("brand new\n")
        before = (git(self.repo, "status", "--porcelain"), git(self.repo, "diff", "--cached"))
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        tree = Path(value["worktree"])
        self.assertEqual((git(self.repo, "status", "--porcelain"), git(self.repo, "diff", "--cached")), before)
        applied = subprocess.run(["git", "-C", str(tree), "apply", value["diff"]], text=True, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((tree / "lib/new_test.py").read_text(), "brand new\n")
        self.assertEqual((tree / "lib/x.py").read_text(), "modified tracked\n")
        git(tree, "add", "-A")
        git(self.repo, "add", "-A")  # compare trees; the index is only read here, after the workdir was checked above
        self.assertEqual(git(tree, "write-tree"), git(self.repo, "write-tree"))

    def test_the_task_text_says_what_the_patch_is_and_lists_commits_made_since_the_task_started(self):
        ident, task = self.start()
        (self.repo / "lib/x.py").write_text("later work by someone else\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "somebody else's commit")
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        request = (self.state / "tasks" / value["id"] / "request.md").read_text()
        self.assertIn("state of", request)
        self.assertIn("can include later or concurrent changes", request)
        self.assertIn("HEAD of", request)
        self.assertIn("somebody else's commit", request)
        self.assertIn("can include later or concurrent changes", value["patch_note"])
        self.assertIn("HEAD has moved", value["patch_note"])

    def test_a_task_collected_first_can_still_be_investigated(self):
        ident, _ = self.start()
        self.assertEqual(self.raw("collect", ident).returncode, 0)
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_a_second_investigation_is_said_not_refused(self):
        ident, _ = self.start()
        self.assertEqual(self.investigate(ident).returncode, 0)
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("already investigates", run.stderr)

    def test_a_report_without_the_block_names_the_command_that_works_and_it_does(self):
        item, task = self.dispatch(self.repo)
        self.settle(task, result="RESULT_STATUS: complete\ntests.test_old failed, not mine\n")
        run = self.investigate(item["id"])
        self.assertEqual(run.returncode, 1)
        self.assertIn("--failing-test", run.stderr)
        again = self.investigate(item["id"], "--failing-test", "tests.test_old, python3 -m unittest tests.test_old")
        self.assertEqual(again.returncode, 0, again.stderr)
        request = (self.state / "tasks" / json.loads(again.stdout)["id"] / "request.md").read_text()
        self.assertIn("tests.test_old, python3 -m unittest", request)
        # and the command exactly as the message prints it, with its placeholder filled
        printed = re.search(r"(tp-agent dispatch --investigate-failure \S+ --failing-test '[^']+')", run.stderr).group(1)
        typed = self.shell(printed.replace("TEST NAME and the command that fails", "tests.test_old") + f" --worker pi --model {MODEL} --thinking medium")
        self.assertEqual(typed.returncode, 0, typed.stderr)

    def test_an_unknown_task_names_list_and_list_works(self):
        run = self.investigate("coder-nosuchtask")
        self.assertEqual(run.returncode, 1)
        self.assertIn("unknown task: coder-nosuchtask", run.stderr)
        command = re.search(r"with: (tp-agent list)", run.stderr).group(1)
        self.assertEqual(self.shell(command).returncode, 0)

    def test_a_task_that_never_recorded_its_start_names_base_and_it_works(self):
        ident, task = self.start()
        (task / "dispatch.json").write_text("{}")
        run = self.investigate(ident)
        self.assertEqual(run.returncode, 1)
        self.assertIn("--base COMMIT", run.stderr)
        sha = git(self.repo, "rev-parse", "HEAD")
        typed = self.shell(re.search(r"(tp-agent dispatch --investigate-failure \S+ --base COMMIT)", run.stderr).group(1)
                           .replace("COMMIT", sha) + f" --worker pi --model {MODEL} --thinking medium")
        self.assertEqual(typed.returncode, 0, typed.stderr)

    def test_a_base_that_is_no_commit_leaves_nothing_behind(self):
        ident, _ = self.start()
        before = git(self.repo, "worktree", "list")
        run = self.investigate(ident, "--base", "deadbeefdeadbeef")
        self.assertEqual(run.returncode, 1)
        self.assertEqual(git(self.repo, "worktree", "list"), before)


if __name__ == "__main__":
    unittest.main()
