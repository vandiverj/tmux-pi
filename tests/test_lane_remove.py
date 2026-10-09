"""Lanes L6: `tp-agent lane remove NAME [--abandon]` and the age column of `tp-agent lanes` (lib/lane_remove.py). Every repository is
a temporary one, tmux is the private -L server of tests/lane_fixture.py and no model runs.

Every refusal is held to two things: it changes nothing, and the command it names clears it (the test runs that command and
removes the lane). The same table is in README.md, Lanes."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import ROOT  # noqa: E402
from test_lane_steps import StepCase  # noqa: E402
from test_lane_writer import git  # noqa: E402
import lane_commands  # noqa: E402
import lane_lease  # noqa: E402
import lane_remove  # noqa: E402
import lanes  # noqa: E402
import worker_process  # noqa: E402


class RemoveCase(StepCase):
    def remove(self, *flags, name="alpha"):
        return self.raw("lane", "remove", name, "--repo", str(self.repo), *flags)

    def removes(self, *flags):
        run = self.remove(*flags)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout), run.stderr

    def archived(self):
        base = self.lane_dir().parent / "archive"
        return sorted(base.iterdir()) if base.is_dir() else []

    def gone(self):
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")
        self.assertFalse(self.lane_dir().exists())

    def lane_with_work(self, file="s1.py"):
        """A lane whose one commit is not in the main branch."""
        self.writer("s1")
        self.edit(file)
        return self.commit()

    def run_named(self, refusal, between=None):
        """Run the command a refusal names in backticks (with this fixture's environment), as a person would."""
        commands = re.findall(r"`([^`]+)`", refusal)
        self.assertTrue(commands, refusal)
        env = {**self.env, "PATH": f"{ROOT / 'bin'}{os.pathsep}{self.env['PATH']}"}
        run = subprocess.run(commands[0], shell=True, env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, f"{commands[0]}: {run.stderr}")
        return commands[0]

    def run_all_named(self, refusal, **env):
        """Run every command a refusal names in backticks, in order (a refusal that lists one per item)."""
        commands = re.findall(r"`([^`]+)`", refusal)
        self.assertTrue(commands, refusal)
        env = {**self.env, **env, "PATH": f"{ROOT / 'bin'}{os.pathsep}{self.env['PATH']}"}
        for command in commands:
            run = subprocess.run(command, shell=True, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 0, f"{command}: {run.stderr}")
        return commands

    def set_state(self, state):
        """A lane in a recorded state, as L7 will leave it (the record is the only thing set; no refusal is repaired by hand)."""
        path = self.lane_dir() / "lane.json"
        record = json.loads(path.read_text())
        record["state"] = state
        if state == "integrated":
            record["integrated"] = {"integration": "integrate-0123456789ab", "commit": record["base"]["commit"], "at": "2026-10-08T00:00:00+00:00"}
        path.write_text(json.dumps(record))

    def integrate_elsewhere(self):
        """Merge lane/alpha into lane/integrate and give main its own commit: lane/integrate and main have diverged, the main
        checkout stays on main, and the lane's tip is in lane/integrate only."""
        other = self.root / "integrate-tree"
        git(self.repo, "worktree", "add", "-q", str(other), "lane/integrate")
        git(other, "merge", "-q", "--no-ff", "-m", "integrate alpha", "lane/alpha")
        git(self.repo, "worktree", "remove", str(other))
        (self.repo / "docs/d.md").write_text(f"d changed on main {git(self.repo, 'rev-parse', 'HEAD')}\n")
        git(self.repo, "commit", "-q", "-am", "main moves on")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        for ref, expected in (("HEAD", 1), ("lane/integrate", 0)):
            code = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", "lane/alpha", ref]).returncode
            self.assertEqual(code, expected, ref)

    def raw_in(self, state, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env={**self.env, "TP_STATE_DIR": str(state)}, text=True,
                              capture_output=True, timeout=60)

    def queue_item(self, state, ident, lane="alpha", step="s2"):
        queued = {"id": ident, "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": "m", "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": lane, "lane_step": step, "preamble_from": str(self.repo)}
        (Path(state) / "queue").mkdir(parents=True, exist_ok=True)
        (Path(state) / "queue" / f"{ident}.json").write_text(json.dumps(queued))


class RemoveTests(RemoveCase):
    def test_a_lane_with_nothing_of_its_own_is_removed_and_its_record_is_archived(self):
        self.writer("s1")
        tip = git(self.worktree, "rev-parse", "HEAD")
        value, _ = self.removes()
        self.gone()
        self.assertEqual((value["lane"], value["abandoned"], value["tip"]), ("alpha", False, tip))
        self.assertEqual([p.name.split("-2")[0] for p in self.archived()], ["alpha"])
        archived = self.archived()[0]
        self.assertEqual(value["archived"], str(archived))
        self.assertEqual(json.loads((archived / "lane.json").read_text())["name"], "alpha")
        self.assertEqual(json.loads(self.raw("lanes", "--repo", str(self.repo)).stdout), [])

    def test_a_lane_whose_work_is_in_the_main_branch_is_removed_without_abandon(self):
        self.lane_with_work()
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge alpha", "lane/alpha")
        self.removes()
        self.gone()
        self.assertEqual(len(self.archived()), 1)

    def test_a_lane_that_can_be_created_again_after_it_was_removed(self):
        self.writer("s1")
        self.removes()
        self.writer("s1")
        self.assertTrue(self.worktree.is_dir())
        self.removes()
        self.assertEqual(len(self.archived()), 2)

    def test_unmerged_work_is_refused_untouched_and_abandon_clears_it(self):
        sha = self.lane_with_work()
        refusal = self.untouched(self.remove, "lane alpha is open with 0 closed step(s) not integrated", "--abandon", sha[:12])
        self.assertIn(f"tp-agent lane remove alpha --abandon --repo {self.repo}", refusal)
        self.run_named(refusal)  # the command the refusal names is the remove with --abandon
        self.gone()
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", sha), sha)

    def test_abandon_prints_the_tip_and_a_restoring_command_and_the_command_restores_the_branch(self):
        sha = self.lane_with_work()
        value, notice = self.removes("--abandon")
        self.assertIn(sha, notice)
        self.assertIn(f"git -C {self.repo} branch lane/alpha {sha}", notice)
        self.assertEqual(value["restore"], f"git -C {self.repo} branch lane/alpha {sha}")
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")
        subprocess.run(value["restore"], shell=True, check=True, env=self.env, capture_output=True)
        self.assertEqual(git(self.repo, "rev-parse", "lane/alpha"), sha)

    def test_the_tip_is_printed_before_the_branch_is_deleted_and_after_the_worktree_is_gone(self):
        sha = self.lane_with_work()
        seen = []
        lane_remove.remove_lane(self.state, self.repo, "alpha", abandon=True,
                                notify=lambda text: seen.append((text, git(self.repo, "branch", "--list", "lane/alpha"), self.worktree.exists())))
        self.assertEqual(len(seen), 1)
        text, branch, tree = seen[0]
        self.assertIn(sha, text)
        self.assertIn("lane/alpha", branch)  # still there when the tip is printed
        self.assertFalse(tree)  # the worktree was removed first: the branch is deleted only after it
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")

    def test_abandon_on_a_merged_lane_deletes_the_branch_without_force_and_prints_no_abandon_notice(self):
        self.lane_with_work()
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge alpha", "lane/alpha")
        value, notice = self.removes("--abandon")
        self.assertEqual((value["abandoned"], notice), (False, ""))

    def test_a_dirty_tree_is_refused_with_gits_message_and_the_stash_command_clears_it(self):
        self.writer("s1")
        (self.worktree / "lib/x.py").write_text("changed\n")
        (self.worktree / "lib/new.py").write_text("new\n")
        refusal = self.untouched(self.remove, "lane alpha's tree has 2 uncommitted path(s)", "contains modified or untracked files")
        self.run_named(refusal)
        self.removes()
        self.gone()
        self.assertIn("lane alpha: before remove", git(self.repo, "stash", "list"))

    def test_a_dirty_tree_is_refused_with_abandon_too(self):
        self.lane_with_work()
        (self.worktree / "lib/new.py").write_text("new\n")
        self.untouched(lambda: self.remove("--abandon"), "uncommitted path(s)")
        self.assertTrue((self.worktree / "lib/new.py").exists())

    def test_ignored_build_output_does_not_block_removal(self):
        self.writer("s1")
        with open(self.repo / ".git/info/exclude", "a") as exclude:
            exclude.write("build/\n*.o\n")
        (self.worktree / "build").mkdir()
        (self.worktree / "build/out.o").write_text("object\n")
        (self.worktree / "lib/x.o").write_text("object\n")
        self.assertEqual(git(self.worktree, "status", "--porcelain"), "")
        self.removes()
        self.gone()

    def test_a_live_process_is_refused_naming_the_command_that_ends_it(self):
        item, task = self.lane_ok()
        pane, pid = self.pane(self.worker_command(task))
        self.attach(task, pane)
        self.settle(task)
        holder = {"kind": "worker", "access": "write", "task": item["id"], "role": "coder", "session": str(self.state), "pid": pid,
                  "start": worker_process._ps()[pid][2], "pane": pane, "taken": "2026-10-08T00:00:00Z"}
        common = Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        with lanes.lane_lock(common):
            lanes.write_lane_file(common, "alpha", "lease.json", json.dumps({"schema": lane_lease.SCHEMA, "holders": [holder]}))
        refusal = self.untouched(self.remove, "lane alpha has a live process", item["id"], "to end it")
        self.assertTrue(self.alive(pane))
        # the clearing command is the one the lease refusal prints: `tp-agent collect ID` ends a finished worker
        self.run_named(refusal)
        self.assertFalse(self.alive(pane))
        self.removes()
        self.gone()

    def test_queued_work_is_refused_and_stop_clears_it(self):
        self.writer("s1")
        self.queue_item(self.state, "coder-queued0001")
        refusal = self.untouched(self.remove, "lane alpha has queued work: coder-queued0001")
        self.run_named(refusal)
        self.assertFalse((self.state / "queue/coder-queued0001.json").exists())
        self.removes()
        self.gone()

    def test_every_queued_item_is_listed_with_its_own_command_and_the_printed_commands_clear_them(self):
        self.writer("s1")
        self.queue_item(self.state, "coder-aaaa1111", step="s2")
        self.queue_item(self.state, "coder-bbbb2222", step="s3")
        self.queue_item(self.state, "coder-cccc3333", lane="other")  # another lane's item is not in the way
        refusal = self.untouched(self.remove, "lane alpha has queued work: coder-aaaa1111, coder-bbbb2222")
        self.assertNotIn("coder-cccc3333", refusal)
        commands = self.run_all_named(refusal)
        self.assertEqual(len(commands), 2, refusal)
        self.assertEqual(sorted(p.name for p in (self.state / "queue").glob("*.json")), ["coder-cccc3333.json"])
        self.removes()
        self.gone()

    def test_work_queued_in_another_session_refuses_and_the_printed_command_clears_it(self):
        self.writer("s1")
        sessions = self.root / "state-root/sessions"
        here, there = sessions / "aaaa1111", sessions / "bbbb2222"
        for session in (here, there):
            (session / "tasks").mkdir(parents=True)
        self.queue_item(there, "coder-theirs0001")
        self.queue_item(here, "coder-mine000001", step="s3")
        before = sorted(p.name for p in (there / "queue").glob("*.json"))
        run = self.raw_in(here, "lane", "remove", "alpha", "--repo", str(self.repo))
        self.refused(run, "lane alpha has queued work: coder-mine000001, coder-theirs0001", "tp-agent stop coder-theirs0001")
        self.assertIn(f"TP_STATE_DIR={there}", run.stderr)  # the other session's item is stopped in that session
        self.assertNotIn(f"TP_STATE_DIR={here}", run.stderr)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(sorted(p.name for p in (there / "queue").glob("*.json")), before)
        self.run_all_named(run.stderr, TP_STATE_DIR=str(here))  # typed in this session
        self.assertEqual(list((there / "queue").glob("*.json")) + list((here / "queue").glob("*.json")), [])
        run = self.raw_in(here, "lane", "remove", "alpha", "--repo", str(self.repo))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.gone()

    def test_a_recorded_state_alone_never_refuses_and_the_abandon_command_clears_every_state(self):
        for state in ("open", "integrating", "integrated", "abandoned"):
            with self.subTest(state=state):
                self.reset()
                sha = self.lane_with_work()
                self.set_state(state)
                refusal = self.untouched(self.remove, f"lane alpha is {state}", "not integrated", f"tp-agent lane remove alpha --abandon --repo {self.repo}")
                self.run_named(refusal)
                self.gone()
                self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", sha), sha)

    def test_a_lane_whose_work_is_in_the_main_branch_is_removed_in_every_state_without_abandon(self):
        for state in ("open", "integrating", "integrated", "abandoned"):
            with self.subTest(state=state):
                self.reset()
                self.lane_with_work()
                git(self.repo, "merge", "-q", "--no-ff", "-m", "merge alpha", "lane/alpha")
                self.set_state(state)
                value, notice = self.removes()
                self.assertEqual((value["abandoned"], notice), (False, ""))
                self.gone()

    def test_a_lane_merged_only_into_the_integration_branch_is_removed_without_abandon_in_both_states(self):
        for number, state in enumerate(("open", "integrated")):
            with self.subTest(state=state):
                self.reset()
                sha = self.lane_with_work(f"work{number}.py")
                self.integrate_elsewhere()
                self.set_state(state)
                main_before = git(self.repo, "rev-parse", "main")
                value, notice = self.removes()
                self.assertEqual((value["abandoned"], value["integrated_in"], notice), (False, ["lane/integrate"], ""))
                self.gone()
                # the work is still in the integration branch, and nothing but the lane's branch was touched
                self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", sha, "lane/integrate"]).returncode, 0)
                self.assertEqual(git(self.repo, "rev-parse", "main"), main_before)
                self.assertEqual(git(self.repo, "branch", "--format=%(refname:short)").split(), ["lane/integrate", "main"])

    def test_it_never_removes_a_worktree_no_record_names(self):
        self.writer("s1")
        other = self.root / "hand-made"
        git(self.repo, "worktree", "add", "-q", "-b", "hand", str(other))
        # a record turned toward the hand-made tree is refused by the check against git, and the tree is left
        path = self.lane_dir() / "lane.json"
        original = path.read_text()
        path.write_text(original.replace(json.loads(original)["worktree"], str(other)))
        run = self.remove()
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertTrue(other.is_dir())
        self.assertTrue(self.worktree.is_dir())
        path.write_text(original)
        # a name no record carries is refused too, whatever else the repository holds
        run = self.remove(name="hand-made")
        self.refused(run, "there is no lane hand-made", "tp-agent lanes")
        self.assertTrue(other.is_dir())
        self.removes()
        self.assertTrue(other.is_dir())

    def test_the_main_checkout_cannot_be_named_as_a_lane_worktree(self):
        self.writer("s1")
        path = self.lane_dir() / "lane.json"
        path.write_text(path.read_text().replace(json.loads(path.read_text())["worktree"], str(self.repo)))
        self.refused(self.remove(), "lane alpha")
        self.assertTrue((self.repo / "lib/x.py").exists())

    def test_there_is_no_force_in_the_code(self):
        source = (ROOT / "lib/lane_remove.py").read_text()
        for line in source.splitlines():
            if "--force" in line:
                self.assertTrue(line.strip().startswith("#") or "never --force" in line or "no --force" in line or "(never" in line, line)
        self.assertNotIn('"--force"', source)
        self.assertNotIn('"-f"', source)

    def test_a_name_that_is_not_a_lane_is_refused_with_the_command_that_lists_lanes(self):
        self.refused(self.remove(name="nothing"), "there is no lane nothing", "tp-agent lanes")
        self.refused(self.remove(name="Bad Name"), "invalid lane name")
        self.refused(self.remove(name="archive"), "reserved")


class AgeColumnTests(RemoveCase):
    def test_the_lanes_rows_carry_created_and_age(self):
        self.writer("s1")
        rows = json.loads(self.raw("lanes", "--repo", str(self.repo)).stdout)
        self.assertEqual(rows[0]["created"], self.record()["created"])
        self.assertRegex(rows[0]["age"], r"^[0-9]+[smhd]$")

    def test_age_text_uses_the_largest_whole_unit(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        for delta, text in ((timedelta(seconds=40), "40s"), (timedelta(minutes=12, seconds=5), "12m"), (timedelta(hours=5, minutes=59), "5h"),
                            (timedelta(days=3, hours=23), "3d"), (timedelta(seconds=-5), "0s")):
            self.assertEqual(lane_commands.age_text((now - delta).isoformat(timespec="seconds"), now), text)


if __name__ == "__main__":
    unittest.main()
