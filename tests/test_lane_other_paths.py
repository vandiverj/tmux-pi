"""A dispatch or retry into a lane tells its worker which paths other open lanes own (packet section `Paths other lanes own`):
lib/lane_commands.py paths_owned_elsewhere / other_lanes_section, bin/tp-agent launch, bin/tp-spawn --other-lane-paths. The
repositories and tmux are the fixtures of tests/test_lane_writer.py; no model runs."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_lane_writer import LaneCase, git  # noqa: E402
from lane_fixture import MODEL, ROOT  # noqa: E402

sys.path.insert(0, str(ROOT / "lib"))
import lane_commands  # noqa: E402
import lanes  # noqa: E402

SECTION = "## Paths other lanes own"
SENTENCE = "Do not edit these paths; if the task needs them, stop and send tp-agent request."


class OtherLanePathsTests(LaneCase):
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

    def test_a_dispatch_lists_the_other_open_lane_its_branch_its_last_closed_step_and_the_overlapping_path(self):
        self.make_beta(closed_step="L1")
        _, task = self.lane_ok()
        packet = self.packet(task)
        self.assertIn(SECTION, packet)
        section = packet.split(SECTION, 1)[1]
        for word in ("beta", "lane/beta", "L1", "lib/x.py", SENTENCE):
            self.assertIn(word, section)

    def test_a_lane_with_no_closed_step_says_so(self):
        self.make_beta()
        _, task = self.lane_ok()
        self.assertIn("no step closed yet", self.packet(task).split(SECTION, 1)[1])

    def test_no_overlap_no_section(self):
        self.make_beta(edit="docs/d.md")  # beta changes only what alpha (lib) does not touch
        _, task = self.lane_ok()
        self.assertNotIn(SECTION, self.packet(task))
        self.assertNotIn("Do not edit these paths", self.packet(task))

    def test_a_dispatch_into_the_lane_alone_has_no_section(self):
        _, task = self.lane_ok()
        self.assertNotIn(SECTION, self.packet(task))

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

    def test_a_lane_that_is_not_open_is_not_listed(self):
        self.make_beta()
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        record = lanes.read_lane(common, "beta")
        with lanes.lane_lock(common):
            lanes.write_lane(common, dict(record, state="abandoned"))
        _, task = self.lane_ok()
        self.assertNotIn(SECTION, self.packet(task))

    def test_a_record_that_cannot_be_read_is_left_out_and_does_not_refuse(self):
        self.make_beta()
        self.lane_ok()
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        self.assertEqual([e["name"] for e in lane_commands.paths_owned_elsewhere(str(self.repo), "alpha")], ["beta"])
        (common / "tmux-pi/lanes/beta/lane.json").write_text("not json")
        self.assertEqual(lane_commands.paths_owned_elsewhere(str(self.repo), "alpha"), [])

    def test_the_section_caps_the_paths_it_lists_per_lane(self):
        entry = {"name": "beta", "branch": "lane/beta", "last_closed": None, "paths": [f"lib/f{n}.py" for n in range(25)] + ["lib/bad`name"]}
        text = lane_commands.other_lanes_section([entry])
        self.assertIn("`lib/f19.py`", text)
        self.assertNotIn("lib/f20.py", text)
        self.assertNotIn("bad`name", text)
        self.assertIn("and 6 more", text)
        self.assertEqual(lane_commands.other_lanes_section([]), "")


if __name__ == "__main__":
    unittest.main()
