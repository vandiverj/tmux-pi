"""Lanes L2: `lane create` and `lanes` (lib/lane_commands.py, bin/tp-agent). Every repository here is a temporary one."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import lane_commands  # noqa: E402
import lanes  # noqa: E402

ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
MACHINE = {"max_active_lanes": 2, "defaulted": []}


def git(cwd, *args):
    run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True, env=ENV)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(os.path.realpath(self.tmp.name))
        self.repo = self.parent / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        (self.repo / "lib").mkdir()
        (self.repo / "lib/x.py").write_text("x\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "branch", "lane/integrate")  # the default base; lanes branch from the integration tip
        self.common = lanes.git_common_dir(self.repo)

    def create(self, name="alpha", touches=("lib/a",), domain="the a part", **kw):
        return lane_commands.create_lane(str(self.repo), name, domain, list(touches), machine=MACHINE, **kw)

    def snapshot(self):
        """Everything creation may not change when it fails: refs, worktrees, the main checkout and info/exclude. The lane
        lock file and the empty lanes directory are the only bookkeeping creation may leave (see the module docstring)."""
        common = Path(self.common)
        exclude = common / "info/exclude"
        listing = sorted(str(p.relative_to(self.parent)) for p in self.parent.rglob("*")
                         if ".git/objects" not in str(p) and ".git/logs" not in str(p) and ".git/tmux-pi" not in str(p))
        lanes_dir = common / "tmux-pi/lanes"
        return {"refs": git(self.repo, "for-each-ref"), "worktrees": git(self.repo, "worktree", "list", "--porcelain"),
                "head": git(self.repo, "rev-parse", "HEAD"), "status": git(self.repo, "status", "--porcelain"),
                "exclude": exclude.read_text() if exclude.exists() else None, "listing": listing,
                "lane_entries": sorted(p.name for p in lanes_dir.iterdir() if p.name != ".lock") if lanes_dir.exists() else [],
                "worktree_entries": sorted(p.name for p in (common / "worktrees").iterdir()) if (common / "worktrees").exists() else []}


class CreateTests(Scratch):
    def test_a_lane_gets_a_branch_a_worktree_and_a_record_that_matches_git(self):
        value = self.create()
        record = value["lane"]
        self.assertEqual(value["warnings"], [])
        self.assertEqual(record["branch"], "lane/alpha")
        self.assertEqual(record["worktree"], str(self.parent / "proj-wt-alpha"))
        self.assertEqual(record["base"], {"ref": "lane/integrate", "commit": git(self.repo, "rev-parse", "main")})
        self.assertEqual(record["state"], "open")
        self.assertEqual(record["kind"], "work")
        self.assertEqual(lanes.read_lane(self.common, "alpha"), record)
        lanes.lane_reality(record)
        self.assertEqual(git(record["worktree"], "symbolic-ref", "HEAD"), "refs/heads/lane/alpha")
        self.assertEqual(git(record["worktree"], "rev-parse", "HEAD"), record["base"]["commit"])

    def test_creation_changes_only_the_new_worktree_branch_and_record(self):
        (self.repo / "a.txt").write_text("edited\n")  # an uncommitted change in the main checkout survives
        before = self.snapshot()
        self.create()
        after = self.snapshot()
        for key in ("head", "status", "exclude"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(after["lane_entries"], ["alpha"])
        self.assertEqual((self.repo / "a.txt").read_text(), "edited\n")
        refs_before = set(before["refs"].splitlines())
        added = set(after["refs"].splitlines()) - refs_before
        self.assertEqual(len(added), 1)
        self.assertTrue(next(iter(added)).endswith("refs/heads/lane/alpha"))
        self.assertEqual(refs_before - set(after["refs"].splitlines()), set())
        new_paths = set(after["listing"]) - set(before["listing"])
        allowed = ("proj-wt-alpha", "proj/.git/worktrees", "proj/.git/refs/heads/lane")
        self.assertTrue(all(p.startswith(allowed) for p in new_paths), new_paths)

    def test_absent_configuration_defaults_are_recorded(self):
        record = lane_commands.create_lane(str(self.repo), "alpha", "d", ["lib"],
                                          machine={"max_active_lanes": 2, "defaulted": ["max_active_lanes"]})["lane"]
        self.assertEqual(record["defaulted"], ["branch_prefix", "integration_branch", "integration_lane", "max_active_lanes",
                                               "worktree_pattern"])

    def test_a_configured_prefix_and_pattern_are_used_and_not_recorded_as_defaulted(self):
        (self.repo / ".tmux-pi").mkdir()
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"branch_prefix": "x/", "worktree_pattern": "{repo}-lane-{name}",
                                                                    "integration_branch": "x/int", "integration_lane": "int"}))
        git(self.repo, "branch", "x/int")
        record = self.create()["lane"]
        self.assertEqual((record["branch"], record["worktree"]), ("x/alpha", str(self.parent / "proj-lane-alpha")))
        self.assertEqual(record["defaulted"], [])

    def test_a_malformed_repository_config_refuses_before_anything_is_made(self):
        (self.repo / ".tmux-pi").mkdir()
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"worktree_pattern": "{repo}-wt"}))
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create()
        self.assertIn("worktree_pattern", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_base_can_be_a_branch_or_a_commit(self):
        first = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "second")
        record = self.create(base=first)["lane"]
        self.assertEqual(record["base"], {"ref": first, "commit": first})
        self.assertEqual(self.create("beta", base="main")["lane"]["base"]["ref"], "main")

    def test_the_default_base_is_the_integration_tip_not_the_main_checkouts_branch(self):
        integration = git(self.repo, "rev-parse", "lane/integrate")
        (self.repo / "b.txt").write_text("b\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "main moves on")
        self.assertNotEqual(git(self.repo, "rev-parse", "main"), integration)
        self.assertEqual(self.create()["lane"]["base"], {"ref": "lane/integrate", "commit": integration})
        git(self.repo, "checkout", "-q", "--detach")  # a detached main checkout does not matter either
        self.assertEqual(self.create("beta")["lane"]["base"]["commit"], integration)

    def test_a_configured_integration_branch_is_the_default_base(self):
        (self.repo / ".tmux-pi").mkdir()
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"branch_prefix": "x/", "worktree_pattern": "{repo}-lane-{name}",
                                                                    "integration_branch": "x/int", "integration_lane": "int"}))
        git(self.repo, "branch", "x/int")
        self.assertEqual(self.create()["lane"]["base"]["ref"], "x/int")

    def test_a_missing_integration_branch_refuses_naming_both_ways_out_and_makes_nothing(self):
        git(self.repo, "branch", "-q", "-D", "lane/integrate")
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create()
        text = str(caught.exception)
        self.assertIn("lane/integrate does not exist", text)
        self.assertIn("--base", text)
        self.assertIn("create the integration branch", text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.create(base="main")["lane"]["base"]["ref"], "main")

    def test_creation_from_a_linked_worktree_uses_the_main_working_tree(self):
        other = self.parent / "other"
        git(self.repo, "worktree", "add", "-q", "-b", "side", str(other))
        record = lane_commands.create_lane(str(other), "alpha", "d", ["lib"], machine=MACHINE, base="main")["lane"]
        self.assertEqual(record["repo"], str(self.repo))
        self.assertEqual(record["worktree"], str(self.parent / "proj-wt-alpha"))

    def test_bad_inputs_refuse_and_change_nothing(self):
        before = self.snapshot()
        cases = [dict(name="Bad_Name"), dict(name="archive"), dict(name="integrate"), dict(touches=[]), dict(touches=["../x"]),
                 dict(touches=["/abs"]), dict(touches=["a", "a"]), dict(domain=""), dict(domain="x" * 201), dict(domain="a\nb"),
                 dict(base="nosuchref"), dict(base="--output=x")]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                self.create(**case)
        with self.assertRaises(ValueError):
            lane_commands.create_lane(str(self.parent / "nope"), "alpha", "d", ["lib"], machine=MACHINE)
        self.assertEqual(self.snapshot(), before)


class RefusalAndRollbackTests(Scratch):
    def test_an_existing_path_is_never_reused_or_touched(self):
        path = self.parent / "proj-wt-alpha"
        path.mkdir()
        (path / "keep.txt").write_text("mine\n")
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create()
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual((path / "keep.txt").read_text(), "mine\n")
        self.assertEqual(self.snapshot(), before)

    def test_an_existing_branch_or_a_conflicting_branch_is_not_adopted(self):
        git(self.repo, "branch", "lane/alpha")
        git(self.repo, "branch", "lane/beta/deep")
        tip = git(self.repo, "rev-parse", "lane/alpha")
        before = self.snapshot()
        for name in ("alpha", "beta"):
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                self.create(name)
            self.assertIn("branch", str(caught.exception))
        self.assertEqual(git(self.repo, "rev-parse", "lane/alpha"), tip)
        self.assertEqual(self.snapshot(), before)

    def test_the_active_lane_limit_refuses_before_anything_is_made(self):
        self.create("one")
        self.create("two")
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create("three")
        self.assertIn("limit 2", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_an_existing_record_of_the_same_name_refuses_and_is_untouched(self):
        self.create()
        text = (Path(self.common) / "tmux-pi/lanes/alpha/lane.json").read_text()
        with self.assertRaises(ValueError):
            self.create()
        self.assertEqual((Path(self.common) / "tmux-pi/lanes/alpha/lane.json").read_text(), text)

    def test_a_corrupt_record_blocks_creation_naming_the_file(self):
        self.create("one")
        (Path(self.common) / "tmux-pi/lanes/one/lane.json").write_text("{")
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create("two")
        self.assertIn("one/lane.json", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_record_write_failure_leaves_no_branch_worktree_or_record(self):
        before = self.snapshot()
        with mock.patch.object(lanes, "write_lane", side_effect=ValueError("disk full")):
            with self.assertRaises(ValueError) as caught:
                self.create()
        self.assertIn("disk full", str(caught.exception))
        self.assertIn("Nothing was left behind", str(caught.exception))
        self.assertEqual(self.snapshot(), before)
        self.create()  # the slot is free again

    def test_a_failure_after_the_record_is_written_removes_the_record_too(self):
        before = self.snapshot()
        with mock.patch.object(lanes, "lane_reality", side_effect=ValueError("not what git says")):
            with self.assertRaises(ValueError) as caught:
                self.create()
        self.assertIn("not what git says", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_a_failure_in_git_after_it_made_the_worktree_rolls_back_both(self):
        hooks = self.parent / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        before = self.snapshot()
        with self.assertRaises(ValueError) as caught:
            self.create()
        self.assertNotIn("could not be undone", str(caught.exception))
        self.assertEqual(self.snapshot(), before)

    def test_what_cannot_be_undone_is_named_with_the_command_that_undoes_it(self):
        with mock.patch.object(lanes, "write_lane", side_effect=ValueError("disk full")), \
                mock.patch.object(lanes, "discard_lane_record", side_effect=ValueError("busy")), \
                mock.patch.object(lane_commands.lanes, "registered_worktrees", wraps=lanes.registered_worktrees):
            with self.assertRaises(ValueError) as caught:
                self.create()
        text = str(caught.exception)
        self.assertIn("could not be undone", text)
        self.assertIn("the record for lane alpha", text)
        # the worktree and branch were removed by the rollback itself
        self.assertNotIn("git -C", text)
        self.assertFalse((self.parent / "proj-wt-alpha").exists())

    def test_a_worktree_that_will_not_go_is_reported_with_its_command_and_a_moved_branch_is_not_deleted(self):
        real = lanes._git

        def refusing(args, cwd, ok=(0,)):
            if args[:2] == ["worktree", "remove"]:
                raise ValueError("locked")
            return real(args, cwd, ok)

        with mock.patch.object(lanes, "write_lane", side_effect=ValueError("disk full")), mock.patch.object(lanes, "_git", refusing):
            with self.assertRaises(ValueError) as caught:
                self.create()
        text = str(caught.exception)
        self.assertIn("worktree remove --force", text)
        self.assertIn(str(self.parent / "proj-wt-alpha"), text)


class OwnedAndRollbackStrictnessTests(Scratch):
    def owned(self, *paths):
        (self.repo / ".tmux-pi").mkdir(exist_ok=True)
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"owned": list(paths)}))

    def test_a_touch_inside_or_around_an_owned_path_refuses_before_anything_is_made(self):
        self.owned("lib", "docs/TASKS.md")
        before = self.snapshot()
        for touches in (["lib/x.py"], ["lib"], ["docs/TASKS.md"], ["bin", "docs"]):
            with self.subTest(touches=touches), self.assertRaises(ValueError) as caught:
                self.create(touches=touches)
            self.assertIn("is owned by the orchestrator", str(caught.exception))
            self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.create(touches=["libx", "bin"])["lane"]["touches"], ["bin", "libx"])

    def test_plain_removal_keeps_what_a_hook_wrote_and_reports_the_command(self):
        hooks = self.parent / "hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        hook.write_text("#!/bin/sh\necho mine > hook-made.txt\nexit 1\n")
        hook.chmod(0o755)
        git(self.repo, "config", "core.hooksPath", str(hooks))
        with self.assertRaises(ValueError) as caught:
            self.create()
        text = str(caught.exception)
        wt = self.parent / "proj-wt-alpha"
        self.assertTrue((wt / "hook-made.txt").exists())  # not discarded
        self.assertIn(f"git -C {self.repo} worktree remove --force {wt}", text)
        self.assertIn("branch -d lane/alpha", text)
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "lane/alpha"), git(self.repo, "rev-parse", "main"))
        self.assertEqual(lanes.lane_names(self.common), [])

    def test_rollback_never_uses_force_flags(self):
        calls = []
        real = lanes._git

        def spy(args, cwd, ok=(0,)):
            calls.append(list(args))
            return real(args, cwd, ok)

        with mock.patch.object(lanes, "write_lane", side_effect=ValueError("disk full")), mock.patch.object(lanes, "_git", spy):
            with self.assertRaises(ValueError):
                self.create()
        self.assertTrue(any(c[:2] == ["worktree", "remove"] for c in calls))
        self.assertFalse(any("--force" in c or "-D" in c or "-f" in c for c in calls), calls)


class OverlapTests(Scratch):
    def test_overlap_warns_and_creates(self):
        self.create("one", touches=["lib/a"], domain="first")
        value = self.create("two", touches=["lib/a/b.py"], domain="second")
        self.assertEqual(len(value["warnings"]), 1)
        warning = value["warnings"][0]
        self.assertEqual((warning["kind"], warning["lanes"], warning["paths"]), ("lane-overlap", ["one", "two"], ["lib/a/b.py"]))
        self.assertEqual(warning["basis"], ["declared/declared"])
        self.assertEqual(warning["claims"], {"one": "first", "two": "second"})

    def test_a_prefix_that_is_not_a_path_part_does_not_overlap(self):
        self.create("one", touches=["lib/a"])
        self.assertEqual(self.create("two", touches=["lib/ab.py"])["warnings"], [])

    def test_measured_paths_of_the_other_lane_count(self):
        one = self.create("one", touches=["docs"])["lane"]
        Path(one["worktree"], "lib").mkdir(exist_ok=True)
        Path(one["worktree"], "lib/new.py").write_text("n\n")  # uncommitted, untracked
        value = self.create("two", touches=["lib"])
        self.assertEqual(value["warnings"][0]["paths"], ["lib/new.py"])
        self.assertEqual(value["warnings"][0]["basis"], ["declared/measured"])


class ListTests(Scratch):
    def test_two_lanes_and_a_hand_made_worktree_are_listed_and_the_worktree_is_untouched(self):
        hand = self.parent / "proj-wt-hand"
        git(self.repo, "worktree", "add", "-q", "-b", "hand-branch", str(hand))
        (hand / "note.txt").write_text("hand\n")
        self.create("one")
        self.create("two", touches=["docs"])
        before = self.snapshot()
        rows = lane_commands.list_lanes(str(self.repo))
        self.assertEqual([(r["name"], r["state"]) for r in rows], [("one", "open"), ("two", "open"), ("proj-wt-hand", "unrecorded")])
        self.assertEqual(rows[2], {"name": "proj-wt-hand", "state": "unrecorded", "repo": str(self.repo), "worktree": str(hand)})
        self.assertEqual((hand / "note.txt").read_text(), "hand\n")
        self.assertEqual(sorted(p.name for p in hand.iterdir()), [".git", "a.txt", "lib", "note.txt"])
        self.assertEqual(self.snapshot(), before)

    def test_a_row_has_the_designed_fields_with_derived_values(self):
        record = self.create("one")["lane"]
        Path(record["worktree"], "lib/x.py").write_text("changed\n")
        git(record["worktree"], "commit", "-q", "-am", "c")
        Path(record["worktree"], "scratch.txt").write_text("s\n")
        (row,) = lane_commands.list_lanes(str(self.repo))
        self.assertEqual(set(row), {"name", "kind", "state", "repo", "branch", "worktree", "base", "domain", "touches", "open_step",
                                    "steps_closed", "occupant", "queued", "waiting_on", "ahead", "dirty", "last_closed",
                                    "overlaps", "disk_kb"})
        self.assertEqual((row["ahead"], row["dirty"], row["steps_closed"], row["last_closed"]), (1, 1, 0, None))
        # these come from a session's tasks and the lease (lib/lane_steps.py), which a listing without a session does not read
        self.assertEqual((row["open_step"], row["occupant"], row["queued"], row["waiting_on"]), (None, None, None, None))
        self.assertIsInstance(row["disk_kb"], int)
        self.assertGreater(row["disk_kb"], 0)

    def test_listing_does_not_write_and_does_not_refresh_the_index(self):
        record = self.create("one")["lane"]
        index = Path(git(record["worktree"], "rev-parse", "--absolute-git-dir")) / "index"
        before = index.stat()
        lane_commands.list_lanes(str(self.repo))
        after = index.stat()
        self.assertEqual((before.st_mtime_ns, before.st_ino), (after.st_mtime_ns, after.st_ino))

    def test_without_any_record_nothing_is_listed_not_even_other_worktrees(self):
        git(self.repo, "worktree", "add", "-q", "-b", "hand-branch", str(self.parent / "h"))
        self.assertEqual(lane_commands.list_lanes(str(self.repo)), [])
        self.assertFalse((Path(self.common) / "tmux-pi").exists())

    def test_a_bad_lane_is_listed_with_its_error_and_the_others_still_appear(self):
        self.create("one")
        self.create("two", touches=["docs"])
        (Path(self.common) / "tmux-pi/lanes/one/lane.json").write_text("{")
        rows = lane_commands.list_lanes(str(self.repo))
        self.assertEqual([r["name"] for r in rows], ["one", "two", "proj-wt-one"])
        self.assertIn("may belong to one", rows[2]["note"])
        self.assertEqual(rows[0]["state"], "error")
        self.assertIn("one/lane.json", rows[0]["error"])
        self.assertEqual(rows[1]["state"], "open")

    def test_a_lane_that_disagrees_with_git_is_listed_with_error(self):
        record = self.create("one")["lane"]
        git(self.repo, "worktree", "remove", "--force", record["worktree"])
        (row,) = lane_commands.list_lanes(str(self.repo))
        self.assertEqual(row["name"], "one")
        self.assertIn("does not exist", row["error"])

    def test_a_name_selects_one_lane_and_an_unknown_name_refuses(self):
        self.create("one")
        self.create("two", touches=["docs"])
        self.assertEqual([r["name"] for r in lane_commands.list_lanes(str(self.repo), "two")], ["two"])
        with self.assertRaises(ValueError) as caught:
            lane_commands.list_lanes(str(self.repo), "nine")
        self.assertIn("no lane nine", str(caught.exception))

    def test_overlapping_lanes_show_the_overlap(self):
        self.create("one", touches=["lib"])
        self.create("two", touches=["lib/x.py"])
        rows = lane_commands.list_lanes(str(self.repo))
        for row in rows:
            self.assertEqual(row["overlaps"][0]["paths"], ["lib/x.py"])

    def test_du_failing_with_a_number_on_stdout_is_not_a_measurement(self):
        fake = subprocess.CompletedProcess(["du"], 1, stdout="9\t/x\n", stderr="du: /x/y: Permission denied\n")
        with mock.patch.object(lane_commands.subprocess, "run", return_value=fake):
            self.assertEqual(lane_commands._disk_kb("/x"), (None, "du: /x/y: Permission denied"))
        fake = subprocess.CompletedProcess(["du"], 1, stdout="9\t/x\n", stderr="")
        with mock.patch.object(lane_commands.subprocess, "run", return_value=fake):
            self.assertEqual(lane_commands._disk_kb("/x"), (None, "du exited 1"))
        fake = subprocess.CompletedProcess(["du"], 0, stdout="9\t/x\n", stderr="")
        with mock.patch.object(lane_commands.subprocess, "run", return_value=fake):
            self.assertEqual(lane_commands._disk_kb("/x"), (9, None))

    def test_a_rename_is_one_dirty_entry_and_both_paths_are_measured(self):
        record = self.create("one")["lane"]
        wt = Path(record["worktree"])
        git(wt, "mv", "a.txt", "renamed.txt")
        (wt / "new.txt").write_text("n\n")
        (row,) = lane_commands.list_lanes(str(self.repo))
        self.assertEqual(row["dirty"], 2)
        self.assertEqual(lane_commands._measured(record), ["a.txt", "new.txt", "renamed.txt"])

    def test_a_disk_timeout_is_null_with_a_reason_never_zero(self):
        self.create("one")
        with mock.patch.object(lane_commands.subprocess, "run", side_effect=subprocess.TimeoutExpired("du", 5)):
            kb, error = lane_commands._disk_kb(str(self.repo))
        self.assertIsNone(kb)
        self.assertIn("longer than", error)
        with mock.patch.object(lane_commands, "_disk_kb", return_value=(None, "slow")):
            (row,) = lane_commands.list_lanes(str(self.repo))
        self.assertEqual((row["disk_kb"], row["disk_error"]), (None, "slow"))


class CommandTests(Scratch):
    def run_agent(self, *args, cwd=None):
        env = {k: v for k, v in ENV.items() if k not in ("TMUX", "TMUX_PANE", "TP_STATE_DIR")}
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], cwd=cwd or self.repo, env=env, text=True, capture_output=True, timeout=60)

    def test_lane_create_and_lanes_through_the_command(self):
        first = self.run_agent("lane", "create", "one", "--domain", "first", "--touches", "lib/a", "docs")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["lane"]["touches"], ["docs", "lib/a"])
        second = self.run_agent("lane", "create", "two", "--domain", "second", "--touches", "lib/a/b.py", "--repo", str(self.repo), cwd=self.parent)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("warning: lanes one and two overlap on lib/a/b.py", second.stderr)
        listed = self.run_agent("lanes")
        self.assertEqual([r["name"] for r in json.loads(listed.stdout)], ["one", "two"])
        self.assertEqual([r["name"] for r in json.loads(self.run_agent("lanes", "two").stdout)], ["two"])

    def test_a_refusal_is_a_tp_agent_error_with_exit_1_and_nothing_made(self):
        run = self.run_agent("lane", "create", "Bad", "--domain", "d", "--touches", "lib")
        self.assertEqual(run.returncode, 1)
        self.assertTrue(run.stderr.startswith("tp-agent: invalid lane name"), run.stderr)
        self.assertEqual(run.stdout, "")
        self.assertEqual(git(self.repo, "worktree", "list").count("\n"), 0)

    def test_a_directory_that_is_not_a_repository_refuses(self):
        run = self.run_agent("lanes", cwd=self.parent)
        self.assertEqual(run.returncode, 1)
        self.assertIn("tp-agent:", run.stderr)


if __name__ == "__main__":
    unittest.main()
