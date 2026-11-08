"""T12: a failing test a worker says it does not own gets its own investigating worker (lib/failing_tests.py and the collect and
dispatch --investigate-failure paths of bin/tp-agent). Repositories are temporary ones; tmux is the private fixture server of
tests/lane_fixture.py, so no worker starts and no model runs."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
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


INC = "entry {} has no "

# Every row of the two parse tables, shared with the before/after script: NEGATIVE texts are no block; POSITIVE rows are
# (text, tests named, malformed).
NEGATIVE = (
    "FAILING_TESTS: none.", "FAILING_TESTS: None (all pass)", "FAILING_TESTS: none (nothing was run).",
    "FAILING_TESTS:\nnone", "FAILING_TESTS: **none**", "FAILING_TESTS: `none`", "FAILING_TESTS: none — every module passed",
    "FAILING_TESTS: No failures", "FAILING_TESTS: 0", "**FAILING_TESTS:** none", "FAILING_TESTS:\n\nAll the tests passed.",
    "FAILING_TESTS: tests.test_inline fails", "FAILING_TESTS:\ntests.test_old fails, not mine",
    "FAILING_TESTS:\n- none", "FAILING_TESTS:\n- n/a", "FAILING_TESTS:\n* **None**", "FAILING_TESTS:\n- no failures",
    "FAILING_TESTS:\n- Nothing failed.", "FAILING_TESTS: none\n- `n/a`", "FAILING_TESTS:\n- 0 failures",
    # round 2: no bullet, no entry; an entry must name a test
    "FAILING_TESTS:\ntest: tests.a\ncommand: c", "FAILING_TESTS:\n- test: none", "FAILING_TESTS:\n- command: pytest",
    "FAILING_TESTS:\n- all tests passed", "FAILING_TESTS:\n- every test passed", "FAILING_TESTS:\n- 0 tests failed",
    "FAILING_TESTS:\n- tests: all green",
    # round 3: a bullet whose first token is another key: label is a note; no other field supplies the id
    "FAILING_TESTS:\n- note: tests.test_a passed", "FAILING_TESTS:\n- reason: see tests/test_x.py",
    "FAILING_TESTS:\n- command: python3 -m unittest tests.test_a",
    "FAILING_TESTS:\n- command: python3 -m unittest tests.test_a\n  unmodified_base: not checked",
    "FAILING_TESTS:\n- note: x\n  test: tests.a",
    # round 4: a bare `label:` bullet other than test: is a note, even when the label or value looks like a test (the trade)
    "FAILING_TESTS:\n- test_case: tests.test_a passed", "FAILING_TESTS:\n- test_foo: AssertionError",
    # round 4, my own adversarial shapes: each tries to get a note, an emphasis form or a field to start or keep an entry
    "FAILING_TESTS:\n- **test**: none",  # emphasised label, no id
    "FAILING_TESTS:\n- tests.a failed\ntest: none",  # the explicit test: replaces the provisional id, and names nothing
    "FAILING_TESTS:\n- note: x\ntest: tests.a",  # a note closes the entry, so the unbulleted test: has none open
    "FAILING_TESTS:\n- note: see\n  tests.test_a",  # a wrapped line after a note continues nothing
    "FAILING_TESTS:\n- _note_: tests.test_a",  # underscore emphasis around another label
    "FAILING_TESTS:\n- Reason: tests/test_x.py::test_y failed",  # capitalised label with a pytest node id
    "FAILING_TESTS:\n- test: n/a\n  command: tests.test_a",  # a command naming a test never supplies the id
    # round 5: a continuation line after test: is a note, so it never supplies the id
    "FAILING_TESTS:\n- test: none\n  tests.test_a",
)

# round 6 (review-coder-58d858934d-204736 F1): a continuation right after a test: field is a note, even when the entry has
# another field before it; FIELD_VALUES checks that the command keeps its own text
REVIEW_R6_PROVISIONAL = ("FAILING_TESTS:\n- tests.a failed\n  command: pytest\n  test: tests.b\n  more text\n", ["tests.b"],
                         [INC.format(1) + "unmodified_base"])
REVIEW_R6_DUPLICATE = ("FAILING_TESTS:\n- test: tests.a\n  command: pytest\n  test: tests.b\n  more text\n", ["tests.a"],
                       ["entry 1 has no unmodified_base and more than one test: field"])

POSITIVE = (
    ("FAILING_TESTS:\n- `tests.test_x.T.test_y` fails with `20 != 16`.\n", ["`tests.test_x.T.test_y` fails with `20 != 16`."],
     [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- test: tests.test_a\n  command: python3 -m unittest tests.test_a\n  unmodified_base: not checked\n",
     ["tests.test_a"], []),
    ("FAILING_TESTS:\n- test: tests.test_a\n  command: c\n- test: tests.test_b\n  command: d\n  unmodified_base: yes\n",
     ["tests.test_a", "tests.test_b"], [INC.format(1) + "unmodified_base"]),
    ("FAILING_TESTS:\n* tests.test_a.T.test_one\n* tests.test_a.T.test_two - flaky\n",
     ["tests.test_a.T.test_one", "tests.test_a.T.test_two - flaky"],
     [INC.format(1) + "command, unmodified_base", INC.format(2) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- no.test_x fails\n", ["no.test_x fails"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- no_such_test fails\n", ["no_such_test fails"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- **src/game.test.ts** fails\n", ["**src/game.test.ts** fails"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- a/b.py::test_c\n", ["a/b.py::test_c"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- TestFoo.test_bar errors\n", ["TestFoo.test_bar errors"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- test_trial_session.TrialSessionTests.test_x: 20 != 16\n",
     ["test_trial_session.TrialSessionTests.test_x: 20 != 16"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- `tests.test_retry_packet` (module does not import)\n",
     ["`tests.test_retry_packet` (module does not import)"], [INC.format(1) + "command, unmodified_base"]),
    # round 3: a second test: field is one malformed entry, never a second entry
    ("FAILING_TESTS:\n- test: tests.a\n  command: c\n  test: tests.b\n", ["tests.a"],
     ["entry 1 has no unmodified_base and more than one test: field"]),
    # a note: field on an entry never supplies the id, and never ends the entry
    ("FAILING_TESTS:\n- test: tests.a\n  note: tests.b passed\n", ["tests.a"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS: see below\n- test: tests.test_a\n  command: c\n  unmodified_base: not checked\n", ["tests.test_a"], []),
    # round 4: the first explicit test: replaces a bullet's provisional id and is not a duplicate; a second one is
    ("FAILING_TESTS:\n- tests.a failed\n  test: tests.b\n", ["tests.b"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- tests.a failed\n  test: tests.b\n  test: tests.c\n", ["tests.b"],
     ["entry 1 has no command, unmodified_base and more than one test: field"]),
    # round 4, my own adversarial shapes: each must still be one entry
    ("FAILING_TESTS:\n- _test_: tests.a\n", ["tests.a"], [INC.format(1) + "command, unmodified_base"]),  # emphasised label
    ("FAILING_TESTS:\n- Test: tests.a\n", ["tests.a"], [INC.format(1) + "command, unmodified_base"]),  # label case
    ("FAILING_TESTS:\n- (tests.test_a) fails\n", ["(tests.test_a) fails"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- tests.a failed\n  note: tests.b passed\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- note: x\n- tests.a failed\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- tests.a failed\n- none\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- test: tests.a\n- test: none\n", ["tests.a"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- test:tests.a\n", ["test:tests.a"], [INC.format(1) + "command, unmodified_base"]),  # no space: not a label
    # round 5: a continuation after the id (a test: line or a bullet's first token) is a note and never changes the id
    ("FAILING_TESTS:\n- test: tests.a\n  tests.b\n", ["tests.a"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- tests.a failed\n  more text\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
    REVIEW_R6_PROVISIONAL, REVIEW_R6_DUPLICATE,
    # round 6: the I5 exception pinned. A prose or continuation note closes the entry, so a later test: is a note too
    ("FAILING_TESTS:\n- tests.a failed\nprose\n  test: tests.b\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
    ("FAILING_TESTS:\n- tests.a failed\n  more text\n  test: tests.b\n", ["tests.a failed"], [INC.format(1) + "command, unmodified_base"]),
)

# (text, entry index, field, value): what one field of one entry holds after wrapped (continuation) lines
FIELD_VALUES = (
    ("**FAILING_TESTS:**\n- test: tests.a\n  command: python3 -m unittest\n    tests.a\n  unmodified_base: not checked\n",
     0, "command", "python3 -m unittest tests.a"),
    (REVIEW_R6_PROVISIONAL[0], 0, "command", "pytest"),
    (REVIEW_R6_DUPLICATE[0], 0, "command", "pytest"),
)

# The exhaustive check: one or two concrete lines of each line kind, every block of 1 to 4 of them. (line, kind, the id the
# line names, or None). The kinds are written here, not imported, so the check does not trust the parser's classifier.
KIND_LINES = (
    ("- test: tests.a", "BULLET_TEST", "tests.a"),
    ("- test: none", "BULLET_TEST", "none"),
    ("- note: tests.b passed", "BULLET_LABEL", None),
    ("- tests.c failed", "BULLET_TEXT", "tests.c failed"),
    ("- nothing failed", "BULLET_TEXT", None),
    ("  test: tests.d", "FIELD_TEST", "tests.d"),
    ("  test: none", "FIELD_TEST", "none"),
    ("  command: python3 -m unittest tests.e", "FIELD_OTHER", None),
    ("  wrapped tests.f", "CONTINUATION", None),
    ("tests.g in prose", "OTHER", None),
    ("", "BLANK", None),
)
WRAPPED = "wrapped tests.f"


def invariant_violations(parse, limit=None):
    """(block lines, invariant, detail) for every block of 1-4 KIND_LINES that breaks I1-I5 under `parse`."""
    import itertools

    def ids(lines):
        found = parse("FAILING_TESTS:\n" + "\n".join(lines) + "\n")
        return found, ([e["test"] for e in found["entries"]] if found else [])

    out = []
    for size in range(1, 5):
        for combo in itertools.product(KIND_LINES, repeat=size):
            read = list(itertools.dropwhile(lambda row: row[1] == "BLANK", combo))  # leading blank lines are skipped
            read = list(itertools.takewhile(lambda row: row[1] != "BLANK", read))  # the first blank line after ends the block
            block = [row[0] for row in combo]
            found, got = ids(block)
            named = [row[2] for row in read if row[2] is not None]
            bad = []
            if any(i not in named for i in got):  # I1: an id is exactly the value of one id-naming line
                bad.append(("I1", got))
            if any(not failing_tests.TEST_ID.search(i) for i in got):  # I2: a counted id matches TEST_ID
                bad.append(("I2", got))
            if not any(failing_tests.TEST_ID.search(n) for n in named) and found is not None:  # I3: no valid id, no block
                bad.append(("I3", got))
            if any(row[1] == "CONTINUATION" for row in combo):
                other = ids([("  other words" if row[1] == "CONTINUATION" else row[0]) for row in combo])[1]
                if other != got:  # I4: a continuation's text never changes an id
                    bad.append(("I4", (got, other)))
                # I4: and it extends only the field whose FIELD_OTHER line (or a continuation of it) is right before it
                allowed, head = 0, None
                for row in read:
                    head = head if row[1] == "CONTINUATION" else row[1]
                    allowed += row[1] == "CONTINUATION" and head == "FIELD_OTHER"
                used = sum(v.count(WRAPPED) for e in (found or {"entries": []})["entries"] for k, v in e.items() if k != "test")
                if used > allowed:
                    bad.append(("I4", f"continuation text in {used} field(s), {allowed} allowed"))
            stripped = ids([row[0] for row in read if row[1] not in ("CONTINUATION", "OTHER", "BLANK")])[1]
            if stripped != got:  # I5: notes never change the ids (one exception, below)
                kinds = [row[1] for row in read]
                # A CONTINUATION or OTHER note closes the entry, so a later `test:` field can no longer replace a bullet's
                # provisional id. With the note removed it does. The ids differ only by those two values.
                closed = any(kinds[i] == "BULLET_TEXT" and read[i][2] and kinds[j] in ("CONTINUATION", "OTHER")
                             and kinds[k] == "FIELD_TEST" for i, j, k in itertools.combinations(range(len(read)), 3))
                if not (closed and set(got) - set(stripped) <= {r[2] for r in read if r[1] == "BULLET_TEXT"}
                        and set(stripped) - set(got) <= {r[2] for r in read if r[1] == "FIELD_TEST"}):
                    bad.append(("I5", (got, stripped)))
            out.extend((block, name, detail) for name, detail in bad)
            if limit and len(out) >= limit:
                return out
    return out


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
        found = failing_tests.parse(FIELD_VALUES[0][0])
        self.assertEqual(found["entries"][0]["test"], "tests.a")  # round 5: only a non-id field takes a continuation
        self.assertEqual(found["malformed"], [])
        for text, number, field, value in FIELD_VALUES:  # round 6: and only right after that field's own line
            with self.subTest(text=text):
                self.assertEqual(failing_tests.parse(text)["entries"][number][field], value)

    def test_every_block_of_up_to_four_kinds_of_line_keeps_the_invariants(self):
        """N2 round 6: I1-I5 over every block of 1-4 KIND_LINES (16104 blocks); and parse's docstring is TABLE."""
        violations = invariant_violations(failing_tests.parse, limit=5)
        self.assertEqual(violations, [])
        doc = [row.strip().strip("|").split("|") for row in failing_tests.parse.__doc__.splitlines() if row.strip().startswith("| ")]
        self.assertEqual([tuple(cell.strip() for cell in row[:3]) for row in doc[1:]],
                         [tuple(row[:3]) for row in failing_tests.TABLE])

    def test_bulleted_prose_and_missing_fields_are_kept_as_text_and_named_never_raised(self):
        found = failing_tests.parse("FAILING_TESTS:\n- tests.test_old fails, not mine\n")
        self.assertEqual(found["entries"][0]["test"], "tests.test_old fails, not mine")
        self.assertIn("entry 1 has no command, unmodified_base", found["malformed"][0])
        self.assertIn("tests.test_old fails", found["text"])

    def test_no_failures_in_any_wording_is_no_block(self):
        """N2: decided by structure: only a bulleted entry that names a test (or test:/command: fields) is a report."""
        for text in NEGATIVE:
            with self.subTest(text=text):
                self.assertIsNone(failing_tests.parse(text))

    def test_real_entry_shapes_still_give_entries_and_the_same_malformed_list(self):
        for text, names, malformed in POSITIVE:
            with self.subTest(text=text):
                found = failing_tests.parse(text)
                self.assertEqual([e.get("test") for e in found["entries"] if "test" in e], names)
                self.assertEqual(found["malformed"], malformed)

    def test_a_heading_or_another_status_line_ends_the_block(self):
        found = failing_tests.parse("FAILING_TESTS:\n- test: tests.a\n  command: c\n  unmodified_base: not checked\nRESULT_STATUS: complete\n")
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

    def test_a_task_saying_none_was_run_gets_no_failing_tests_row_or_notice(self):
        """N2: the index row (what the hook hint reads) carries no failing_tests, so the hint is the plain collect one."""
        item, task = self.dispatch(self.repo)
        self.settle(task, result="RESULT_STATUS: complete\nFAILING_TESTS: none (nothing was run).\n")
        ident = item["id"]
        index = self.cli("index", "--no-tick")
        self.assertNotIn("failing_tests", [r for r in index["pending"] if r["id"] == ident][0])
        run = self.raw("collect", ident)
        self.assertNotIn("failing_tests", json.loads(run.stdout))
        self.assertEqual(run.stderr, "")

    def test_a_malformed_block_is_shown_as_text_and_collect_still_succeeds(self):
        ident, _ = self.start("RESULT_STATUS: complete\nFAILING_TESTS:\n- tests/test_old is red\n")
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
