"""A dispatch or retry into a lane tells its worker which paths other open lanes own (packet section `Paths other lanes own`):
lib/lane_commands.py paths_owned_elsewhere / other_lanes_section, bin/tp-agent launch, bin/tp-spawn --other-lane-paths. The
repositories and tmux are the fixtures of tests/test_lane_writer.py; no model runs."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lane_writer import LaneCase, git  # noqa: E402
from lane_fixture import MODEL, ROOT  # noqa: E402  (ROOT also locates tools/agent/lane-info.sh)

sys.path.insert(0, str(ROOT / "lib"))
import lane_commands  # noqa: E402
import lanes  # noqa: E402

SECTION = "## Paths other lanes own"
SENTENCE = "Do not edit these paths; if the task needs them, stop and send tp-agent request."


class OtherLanePathsTests(LaneCase):
    snapshot_lane = False  # measured 7% CPU-s gain, under the 10% bar
    beta = property(lambda self: self.root / "proj-wt-beta")

    def make_beta(self, touches="docs", edit="lib/x.py", closed_step=None):
        """Lane beta (declared `touches`), with `edit` changed in its tree and optionally a step closed."""
        self.lane_ok(name="beta", extra=("--repo", None, "--domain", "the docs", "--touches", touches))
        if edit:
            (self.beta / edit).write_text("changed in beta\n")
        if closed_step:
            common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            record = lanes.read_lane(common, "beta")
            entry = {"step": closed_step, "commit": git(self.beta, "rev-parse", "HEAD"), "tree": git(self.beta, "rev-parse", "HEAD^{tree}"),
                     "review": "rev-1", "verdict": "APPROVED", "closed_at": "2026-10-08T10:00:00+00:00"}
            with lanes.lane_lock(common):
                lanes.write_lane(common, dict(record, closed=[entry]))

    def packet(self, task):
        return (task / "packet.md").read_text()

    def test_a_dispatch_lists_the_other_open_lane_and_says_when_it_has_closed_no_step(self):
        rows = (("a dispatch lists the other open lane, its branch, its last closed step and the overlapping path", "L1",
                 ("beta", "lane/beta", "L1", "lib/x.py", SENTENCE)),
                ("a lane with no closed step says so", None, ("no step closed yet",)))
        for number, (name, closed_step, words) in enumerate(rows):
            with self.subTest(name):
                if number:
                    self.reset_fixture()  # each row dispatches from an empty state
                self.make_beta(closed_step=closed_step)
                _, task = self.lane_ok()
                packet = self.packet(task)
                self.assertIn(SECTION, packet)
                section = packet.split(SECTION, 1)[1]
                for word in words:
                    self.assertIn(word, section)

    def test_no_section_unless_an_open_lane_owns_an_overlapping_path(self):
        def no_overlap():
            self.make_beta(edit="docs/d.md")  # beta changes only what alpha (lib) does not touch

        def alone():
            pass  # a dispatch into the lane alone

        def not_open():
            self.make_beta()
            common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
            record = lanes.read_lane(common, "beta")
            with lanes.lane_lock(common):
                lanes.write_lane(common, dict(record, state="abandoned"))

        rows = (("no overlap, no section", no_overlap), ("a dispatch into the lane alone has no section", alone),
                ("a lane that is not open is not listed", not_open))
        for number, (name, prepare) in enumerate(rows):
            with self.subTest(name):
                if number:
                    self.reset_fixture()  # each row dispatches from an empty state
                prepare()
                _, task = self.lane_ok()
                self.assertNotIn(SECTION, self.packet(task))
                self.assertNotIn("Do not edit these paths", self.packet(task))

    def test_a_later_task_in_the_existing_lane_and_a_retry_of_it_have_the_section(self):
        _, first = self.lane_ok()
        self.make_beta()
        self.settle(first)
        _, second = self.existing(step="s1")  # a second task in the step the first one left unfinished
        self.assertIn(SECTION, self.packet(second))
        self.assertIn("lib/x.py", self.packet(second))
        self.settle(second)
        run = self.raw("retry", second.name, "--reason", "again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("lib/x.py", self.packet(self.state / "tasks" / json.loads(run.stdout)["id"]).split(SECTION, 1)[1])

    def test_a_record_that_cannot_be_read_is_left_out_and_does_not_refuse(self):
        self.make_beta()
        self.lane_ok()
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual([e["name"] for e in lane_commands.paths_owned_elsewhere(str(self.repo), "alpha")], ["beta"])
        (common / "tmux-pi/lanes/beta/lane.json").write_text("not json")
        self.assertEqual(lane_commands.paths_owned_elsewhere(str(self.repo), "alpha"), [])

    def section(self, *paths):
        return lane_commands.other_lanes_section([{"name": "beta", "branch": "lane/beta", "last_closed": None, "paths": list(paths)}])

    def test_every_overlapping_path_is_named_with_no_cap(self):
        paths = [f"lib/f{n}.py" for n in range(21)]
        text = self.section(*paths)
        for path in paths:
            self.assertIn(f'`"{path}"`', text)
        self.assertNotIn("more", text)
        self.assertEqual(lane_commands.other_lanes_section([]), "")

    def test_a_path_is_shown_as_a_json_string_in_a_span_it_cannot_close(self):
        rows = (("an ordinary path is shown as a json string in a code span", ("lib/x.py",), ['`"lib/x.py"`']),
                ("a path with a backtick is named in a span it cannot close", ("lib/a.py", "lib/`important.py", "lib/x``y.py"),
                 ['`` "lib/`important.py" ``',  # CommonMark: a longer fence, padded with a space each side
                  '``` "lib/x``y.py" ```', '`"lib/a.py"`']),
                ("a backslash n and a real newline are shown differently", ("lib/a\\nb.py", "lib/a\nb.py"), [r'`"lib/a\\nb.py"`', r'`"lib/a\nb.py"`']),
                ("leading and trailing spaces stay visible inside the quotes", (" lib/x.py ",), ['`" lib/x.py "`']))
        for name, paths, shown in rows:
            with self.subTest(name):
                text = self.section(*paths)
                for expected in shown:
                    self.assertIn(expected, text)

    def test_a_path_with_a_control_character_is_named_escaped_on_one_line(self):
        text = self.section("lib/a\nb.py", "lib/t\tab.py", "lib/e\x1b[0m.py")
        for shown in (r'"lib/a\nb.py"', r'"lib/t\tab.py"', r'"lib/e\u001b[0m.py"'):
            self.assertIn(shown, text)
        body = text.split("\n\n", 1)[0]
        self.assertEqual(len(body.splitlines()), 1)
        self.assertFalse(any(ord(c) < 32 and c != "\n" for c in text))

    def test_a_non_ascii_path_is_shown_escaped_on_one_line(self):
        text = self.section("lib/é.py")
        self.assertIn('`"lib/\\u00e9.py"`', text)
        self.assertTrue(text.isascii())


class LaneFactsTests(LaneCase):
    """The packet section `## Lane` and the worker's LANE_BASE (lib/lane_commands.py lane_facts, bin/tp-agent launch, bin/tp-spawn
    --lane-facts-file / --worker-env LANE_BASE=). Extends this module because it already builds a dispatched lane in a fixture."""
    def commit(self, name, text="v\n"):
        (self.worktree / name).write_text(text)
        git(self.worktree, "add", name)
        git(self.worktree, "commit", "-q", "-m", "Label cells" if name == "b.txt" else "first lane commit")

    def record(self):
        return lanes.read_lane(Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")), "alpha")

    def test_a_dispatch_puts_the_section_after_the_working_directory_and_exports_lane_base(self):
        _, task = self.lane_ok()
        packet, run_sh = (task / "packet.md").read_text(), (task / "run.sh").read_text()
        base = self.record()["base"]["commit"]
        self.assertRegex(packet, r"## Lane \(snapshot at \d{4}-\d\d-\d\dT[\d:]+Z; refresh: tools/agent/lane-info\.sh " + re.escape(str(self.worktree)))
        self.assertLess(packet.index("Working directory:"), packet.index("## Lane"))
        self.assertIn(f"lane: alpha  step: s1  branch: lane/alpha  worktree: {self.worktree}", packet)
        self.assertIn(f"base: lane/integrate {base[:7]}  (LANE_BASE is set to this sha)", packet)
        self.assertIn(f"export LANE_BASE={base}\n", run_sh)
        # every worker gets an LM Studio request cap of 0 and its own counter file (lib/router.py local_open)
        self.assertIn(f"export TP_LMSTUDIO_BUDGET=0 TP_LMSTUDIO_BUDGET_FILE={task}/lmstudio-budget.count\n", run_sh)
        # lane-info.sh with no base argument now prints that base
        line = next(l for l in run_sh.splitlines() if l.startswith("export LANE_BASE="))
        out = subprocess.run(["bash", "-c", f'{line}\n{ROOT}/tools/agent/lane-info.sh {self.worktree}'], text=True, capture_output=True, timeout=30)
        self.assertIn(base[:7], out.stdout)

    def test_the_block_after_two_commits_and_an_uncommitted_file(self):
        self.lane_ok()
        self.commit("a.txt")
        self.commit("b.txt")
        (self.worktree / "lib/x.py").write_text("changed\n")
        (self.worktree / "new.txt").write_text("n\n")
        record = self.record()
        text = lane_commands.lane_facts(record, "s1")
        base, head = record["base"]["commit"], git(self.worktree, "rev-parse", "HEAD")
        expected = [
            rf"## Lane \(snapshot at \d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ; refresh: tools/agent/lane-info\.sh {re.escape(str(self.worktree))}\)",
            rf"lane: alpha  step: s1  branch: lane/alpha  worktree: {re.escape(str(self.worktree))}",
            rf"base: lane/integrate {base[:7]}  \(LANE_BASE is set to this sha\)",
            rf'HEAD: {head[:7]} "Label cells"  ahead 2 / behind 0',
            r"changed since base \(2\): A a.txt, A b.txt",
            r"uncommitted: M lib/x.py, \?\? new.txt",
            r"last closed step: none"]
        lines = text.split("\n")
        self.assertEqual(len(lines), len(expected), text)
        for line, pattern in zip(lines, expected):
            self.assertRegex(line, "^" + pattern + "$")

    def test_a_long_changed_list_is_capped_at_twenty_and_says_how_many_more(self):
        self.lane_ok()
        for n in range(23):
            (self.worktree / f"f{n:02}.txt").write_text("v\n")
        git(self.worktree, "add", ".")
        git(self.worktree, "commit", "-q", "-m", "many")
        for n in range(21):  # the cap is for the changed list only: every uncommitted path is listed
            (self.worktree / f"u{n:02}.txt").write_text("v\n")
        lines = lane_commands.lane_facts(self.record(), "s1").split("\n")
        line = next(l for l in lines if l.startswith("changed since base"))
        self.assertTrue(line.startswith("changed since base (23): A f00.txt, "), line)
        self.assertTrue(line.endswith("A f19.txt, and 3 more"), line)
        self.assertNotIn("f20.txt", line)
        uncommitted = next(l for l in lines if l.startswith("uncommitted:"))
        self.assertEqual(uncommitted, "uncommitted: " + ", ".join(f"?? u{n:02}.txt" for n in range(21)))

    def test_the_refresh_command_survives_a_worktree_path_with_a_space(self):
        self.lane_ok()
        spaced = self.root / "my project"
        spaced.symlink_to(self.worktree)
        first = lane_commands.lane_facts(dict(self.record(), worktree=str(spaced)), "s1").split("\n")[0]
        command = first.split("refresh: ", 1)[1][:-1]  # up to the closing parenthesis
        self.assertEqual(shlex.split(command), ["tools/agent/lane-info.sh", str(spaced)])

    def test_a_broken_worktree_gives_the_one_unavailable_line(self):
        self.lane_ok()
        record = dict(self.record(), worktree=str(self.root / "no-such-tree"))
        text = lane_commands.lane_facts(record, "s1")
        self.assertRegex(text, r"^lane facts unavailable: \S.*$")
        self.assertNotIn("\n", text)
        self.assertEqual(lane_commands.lane_facts({"name": "x"}, "s1").split(":")[0], "lane facts unavailable")  # a record that lacks fields

    def test_a_task_that_is_not_in_a_lane_has_neither_the_section_nor_the_variable(self):
        _, task = self.dispatch(self.repo)
        self.assertNotIn("## Lane", (task / "packet.md").read_text())
        self.assertNotIn("LANE_BASE", (task / "run.sh").read_text())


if __name__ == "__main__":
    unittest.main()
