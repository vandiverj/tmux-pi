"""Lanes L6: `tp-agent lane remove NAME [--abandon]` and the age column of `tp-agent lanes` (lib/lane_remove.py). Every repository is
a temporary one, tmux is the private -L server of tests/lane_fixture.py and no model runs.

Every refusal is held to two things: it changes nothing, and the command it names clears it (the test runs that command and
removes the lane). The same table is in README.md, Lanes."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

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
        # the resume path and the long timeout live in lanes.py and lane_remove.py: neither may name a force flag in a git call
        for name in ("lib/lanes.py", "lib/lane_remove.py"):
            text = (ROOT / name).read_text()
            self.assertNotRegex(text, r"""["']--force["']""", name)
            self.assertNotRegex(text, r"""["']-f["']""", name)

    def test_a_name_that_is_not_a_lane_is_refused_with_the_command_that_lists_lanes(self):
        self.refused(self.remove(name="nothing"), "there is no lane nothing", "tp-agent lanes")
        self.refused(self.remove(name="Bad Name"), "invalid lane name")
        self.refused(self.remove(name="archive"), "reserved")


class ResumableRemoveTests(RemoveCase):
    """`git worktree remove` of a big tree outlives GIT_TIMEOUT and is killed half way. The removal gets its own long timeout, and
    a marker written once the tree was clean lets the next `lane remove` finish what was interrupted (never with --force)."""

    def setUp(self):
        super().setUp()
        self.calls = []

    def marker(self):
        return self.lane_dir() / "removing.json"

    def interrupt(self, damage):
        """Run the removal with `git worktree remove` replaced by: `damage()` (what a killed removal leaves), then the timeout."""
        real = lanes._git

        def fake(args, cwd, ok=(0,), timeout=None):
            self.calls.append(list(args))
            if args[:2] == ["worktree", "remove"]:
                damage()
                raise lanes.GitTimeout(f"git {' '.join(args)} could not run in {cwd} (TimeoutExpired)")
            return real(args, cwd, ok, timeout)

        with mock.patch.object(lanes, "_git", fake), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        return str(caught.exception)

    def finish(self):
        """Run the removal again, with git as it is, and record every git call it makes."""
        real = lanes._git

        def spy(args, cwd, ok=(0,), timeout=None):
            self.calls.append(list(args))
            return real(args, cwd, ok, timeout)

        with mock.patch.object(lanes, "_git", spy), contextlib.redirect_stderr(io.StringIO()) as err:
            done = lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        self.assertIn("resuming", err.getvalue())
        return done

    def no_force(self):
        self.assertTrue(self.calls)
        self.assertFalse(any("--force" in c or "-f" in c for c in self.calls), self.calls)

    def deleted_tracked(self):
        for name in ("lib/x.py", "docs/d.md"):
            (self.worktree / name).unlink()

    def ignore_node_modules(self):
        with open(self.repo / ".git/info/exclude", "a") as exclude:
            exclude.write("node_modules/\n")

    def admin_dirs(self):
        return sorted(p.name for p in (self.lane_dir().parent.parent.parent / "worktrees").glob("*"))

    # --- the long timeout ---------------------------------------------------------------------------------------------

    def test_only_the_worktree_removal_gets_the_long_timeout(self):
        self.writer("s1")
        timeouts = []
        real = subprocess.run

        def spy(command, *args, **kwargs):
            if command[1:2] == ["-C"]:  # the lane code's git calls (lanes._git)
                timeouts.append((command[3:5], kwargs.get("timeout")))
            return real(command, *args, **kwargs)

        with mock.patch.object(lanes, "REMOVE_TIMEOUT", 777), mock.patch.object(lanes.subprocess, "run", spy):
            lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        long = [args for args, timeout in timeouts if timeout == 777]
        self.assertEqual(long, [["worktree", "remove"]])
        self.assertEqual({timeout for args, timeout in timeouts if args != ["worktree", "remove"]}, {lanes.GIT_TIMEOUT})
        self.assertEqual(lanes.GIT_TIMEOUT, 30)
        self.assertGreaterEqual(lanes.REMOVE_TIMEOUT, 600)
        self.gone()

    def fake_git(self, delay):
        """A `git` ahead on PATH that sleeps `delay` seconds before `worktree remove` and is otherwise git."""
        directory = self.root / "slow-git"
        directory.mkdir(exist_ok=True)
        script = directory / "git"
        script.write_text(f'''#!/bin/sh
if [ "$3" = worktree ] && [ "$4" = remove ]; then exec sleep {delay}; fi
exec {shutil.which("git")} "$@"
''')
        script.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{directory}{os.pathsep}{os.environ['PATH']}"})

    def test_a_removal_that_times_out_says_to_run_it_again_and_the_second_run_finishes_it(self):
        self.writer("s1")
        with self.fake_git(30), mock.patch.object(lanes, "REMOVE_TIMEOUT", 1), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        text = str(caught.exception)
        self.assertIn("interrupted", text)
        self.assertIn(f"`tp-agent lane remove alpha --repo {self.repo}` again", text)
        self.assertTrue(self.worktree.is_dir())
        self.assertTrue(self.marker().is_file())
        value, _ = self.removes()
        self.assertEqual(value["worktree"], str(self.worktree))
        self.gone()

    def test_the_again_command_keeps_abandon(self):
        self.lane_with_work()
        with self.fake_git(30), mock.patch.object(lanes, "REMOVE_TIMEOUT", 1), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                lane_remove.remove_lane(str(self.state), self.repo, "alpha", abandon=True)
        self.assertIn(f"`tp-agent lane remove alpha --abandon --repo {self.repo}` again", str(caught.exception))
        self.removes("--abandon")
        self.gone()

    def test_a_slow_removal_says_it_is_still_going_and_a_quick_one_says_nothing(self):
        self.writer("s1")
        real = lanes._git

        def slow(args, cwd, ok=(0,), timeout=None):
            if args[:2] == ["worktree", "remove"]:
                time.sleep(1)
            return real(args, cwd, ok, timeout)

        with mock.patch.object(lane_remove, "PROGRESS_AFTER", 0.2), mock.patch.object(lanes, "_git", slow), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        self.assertIn("still removing lane alpha's worktree", err.getvalue())
        self.gone()
        self.writer("s1")
        value, quiet = self.removes()  # a quick removal prints nothing
        self.assertEqual(quiet, "")

    def test_ctrl_c_during_the_removal_says_to_run_it_again_and_keeps_the_marker(self):
        self.writer("s1")
        real = lanes._git

        def fake(args, cwd, ok=(0,), timeout=None):
            if args[:2] == ["worktree", "remove"]:
                raise KeyboardInterrupt
            return real(args, cwd, ok, timeout)

        with mock.patch.object(lanes, "_git", fake), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(KeyboardInterrupt):
                lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        self.assertIn(f"tp-agent lane remove alpha --repo {self.repo}", err.getvalue())
        self.assertTrue(self.marker().is_file())
        self.removes()
        self.gone()

    # --- the marker ---------------------------------------------------------------------------------------------------

    def test_the_marker_is_written_after_the_checks_and_only_for_a_clean_tree(self):
        self.writer("s1")
        (self.worktree / "lib/new.py").write_text("new\n")
        self.untouched(self.remove, "uncommitted path(s)")
        self.assertFalse(self.marker().exists())
        (self.worktree / "lib/new.py").unlink()
        self.queue_item(self.state, "coder-queued0001")
        self.untouched(self.remove, "has queued work")
        self.assertFalse(self.marker().exists())

    def test_the_marker_names_the_recorded_worktree_and_goes_into_the_archive(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        marker = json.loads(self.marker().read_text())
        self.assertEqual((marker["lane"], marker["worktree"], marker["clean"]), ("alpha", str(self.worktree), True))
        self.finish()
        self.gone()
        self.assertFalse(self.marker().exists())
        self.assertEqual(json.loads((self.archived()[0] / "removing.json").read_text())["worktree"], str(self.worktree))

    def test_a_lane_with_a_leftover_marker_cannot_be_created_again(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        with self.assertRaises(ValueError) as caught:
            lane_commands.create_lane(str(self.repo), "alpha", "d", ["lib"], machine={"max_active_lanes": 2, "defaulted": []})
        self.assertIn("already has a record", str(caught.exception))
        self.assertTrue(self.marker().is_file())

    # --- resuming -----------------------------------------------------------------------------------------------------

    def test_tracked_deletions_after_an_interrupted_removal_are_restored_and_the_removal_finishes(self):
        self.writer("s1")
        text = self.interrupt(self.deleted_tracked)
        self.assertIn("again", text)
        self.assertEqual(sorted(line.strip() for line in git(self.worktree, "status", "--porcelain").splitlines()), ["D docs/d.md", "D lib/x.py"])
        value = self.finish()
        self.assertEqual((value["removed"], value["worktree"]), (True, str(self.worktree)))
        self.gone()
        self.assertTrue(any(c[:2] == ["restore", "--worktree"] for c in self.calls))
        self.no_force()
        self.assertEqual(git(self.repo, "branch", "--format=%(refname:short)").split(), ["lane/integrate", "main"])

    def test_the_same_deletions_without_a_marker_still_refuse_and_nothing_is_restored(self):
        self.writer("s1")
        self.deleted_tracked()
        refusal = self.untouched(self.remove, "lane alpha's tree has 2 uncommitted path(s)")
        self.assertFalse((self.worktree / "lib/x.py").exists())
        self.assertFalse(self.marker().exists())
        self.run_named(refusal)
        self.removes()
        self.gone()

    def test_a_tree_whose_git_link_is_gone_is_deleted_with_its_own_entry_only(self):
        self.writer("s1")
        self.ignore_node_modules()  # ignored build output goes with the tree, as it does for git
        other = self.root / "unmounted"
        git(self.repo, "worktree", "add", "-q", "-b", "elsewhere", str(other))
        shutil.rmtree(other)  # prunable: a worktree on a drive that is not there
        before = self.admin_dirs()
        self.assertEqual(len(before), 2)

        def damage():
            self.deleted_tracked()
            (self.worktree / "node_modules/pkg").mkdir(parents=True)
            (self.worktree / "node_modules/pkg/index.js").write_text("x\n")
            (self.worktree / ".git").unlink()

        text = self.interrupt(damage)
        self.assertIn("again", text)
        self.assertTrue(self.worktree.is_dir())
        value = self.finish()
        self.assertEqual(value["worktree"], str(self.worktree))
        self.gone()
        self.assertEqual(len(self.admin_dirs()), 1)
        self.assertIn(str(other), git(self.repo, "worktree", "list", "--porcelain"))  # the unrelated entry is still there
        self.assertEqual(git(self.repo, "rev-parse", "--verify", "-q", "refs/heads/elsewhere"), git(self.repo, "rev-parse", "main"))
        self.assertFalse(any(c[:2] == ["worktree", "prune"] for c in self.calls))
        self.no_force()

    def lane_with_tracked_gitignores(self):
        """A lane whose tree tracks the .gitignore files (at the root and in lib/) that ignore its node_modules; main has them too."""
        self.writer("s1")
        for name, text in ((".gitignore", "node_modules/\n"), ("lib/.gitignore", "cache/\n")):
            (self.worktree / name).write_text(text)
        self.commit("ignore build output")
        git(self.repo, "merge", "-q", "--ff-only", "lane/alpha")
        self.assertEqual(git(self.worktree, "ls-files", ".gitignore", "lib/.gitignore").split(), [".gitignore", "lib/.gitignore"])

    def deleted_with_gitignores(self):
        """What a killed removal leaves: the .gitignore files may go before the ignored output does."""
        self.deleted_tracked()
        for name in (".gitignore", "lib/.gitignore"):
            (self.worktree / name).unlink()
        for name in ("node_modules/pkg", "lib/cache"):
            (self.worktree / name).mkdir(parents=True)
            (self.worktree / name / "a.js").write_text("x\n")

    def test_a_deleted_tracked_gitignore_does_not_make_ignored_output_look_like_someones_files(self):
        self.lane_with_tracked_gitignores()
        self.interrupt(self.deleted_with_gitignores)
        status = git(self.worktree, "status", "--porcelain")
        self.assertIn("?? node_modules/", status)  # the premise: without its .gitignore git calls the build output untracked
        value = self.finish()
        self.assertEqual((value["removed"], value["worktree"]), (True, str(self.worktree)))
        self.gone()
        self.assertTrue(any(c[:2] == ["restore", "--worktree"] and ":(literal).gitignore" in c and ":(literal)lib/.gitignore" in c for c in self.calls),
                        self.calls)
        self.no_force()

    def test_a_deleted_tracked_gitignore_in_a_tree_whose_git_link_is_gone_does_not_refuse_the_ignored_output(self):
        self.lane_with_tracked_gitignores()
        self.interrupt(lambda: (self.deleted_with_gitignores(), (self.worktree / ".git").unlink()))
        self.assertEqual(len(self.admin_dirs()), 1)
        value = self.finish()
        self.assertEqual(value["worktree"], str(self.worktree))
        self.gone()
        self.assertEqual(self.admin_dirs(), [])
        self.no_force()

    def refuses_an_untracked_file_after_a_deleted_gitignore(self, *fragments):
        (self.worktree / "notes.txt").write_text("mine\n")
        run = self.remove()
        self.refused(run, "lane alpha", *fragments)
        self.assertEqual((self.worktree / "notes.txt").read_text(), "mine\n")
        self.assertTrue(self.marker().is_file())
        self.assertTrue((self.worktree / "node_modules/pkg/a.js").is_file())  # the ignored output is left alone by the refusal

    def test_a_deleted_gitignore_does_not_hide_someones_untracked_file(self):
        self.lane_with_tracked_gitignores()
        self.interrupt(self.deleted_with_gitignores)
        self.refuses_an_untracked_file_after_a_deleted_gitignore("1 uncommitted path(s)", "stash push -u")

    def test_a_deleted_gitignore_does_not_hide_someones_untracked_file_in_a_tree_whose_git_link_is_gone(self):
        self.lane_with_tracked_gitignores()
        self.interrupt(lambda: (self.deleted_with_gitignores(), (self.worktree / ".git").unlink()))
        self.refuses_an_untracked_file_after_a_deleted_gitignore("notes.txt", "not tracked", "nothing was deleted")

    def test_a_tree_that_is_already_gone_only_has_its_entry_dropped(self):
        self.writer("s1")
        self.interrupt(lambda: shutil.rmtree(self.worktree))
        self.assertEqual(len(self.admin_dirs()), 1)
        self.finish()
        self.gone()
        self.assertEqual(self.admin_dirs(), [])
        self.no_force()

    def test_a_removal_interrupted_before_anything_was_deleted_is_resumed_as_a_normal_one(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        self.finish()
        self.gone()
        self.assertFalse(any(c[:2] == ["restore", "--worktree"] for c in self.calls))

    def test_work_that_is_not_in_the_branch_is_still_refused_without_abandon_on_resume(self):
        self.lane_with_work()
        (self.lane_dir() / "removing.json").write_text(json.dumps({"schema": lane_remove.MARKER_SCHEMA, "lane": "alpha", "worktree": str(self.worktree),
                                                                    "clean": True, "started": "2026-10-18T00:00:00Z"}))
        (self.worktree / ".git").unlink()
        self.refused(self.remove(), "lane alpha is open", "not integrated")
        self.assertTrue(self.worktree.is_dir())

    # --- what the resume never does -----------------------------------------------------------------------------------

    def test_a_marker_with_an_untracked_or_modified_path_refuses_and_changes_nothing(self):
        for what in ("untracked", "modified", "staged"):
            with self.subTest(what=what):
                self.reset()
                self.writer("s1")

                def damage():
                    (self.worktree / "docs/d.md").unlink()
                    if what == "untracked":
                        (self.worktree / "lib/new.py").write_text("new\n")
                    elif what == "modified":
                        (self.worktree / "lib/x.py").write_text("mine\n")
                    else:
                        (self.worktree / "lib/x.py").write_text("mine\n")
                        git(self.worktree, "add", "lib/x.py")

                self.interrupt(damage)
                status = git(self.worktree, "status", "--porcelain")
                run = self.remove()
                self.refused(run, "lane alpha", "1 uncommitted path(s)", "stash push -u")
                self.assertEqual(git(self.worktree, "status", "--porcelain"), status)
                self.assertFalse((self.worktree / "docs/d.md").exists())  # nothing was restored
                self.assertTrue(self.marker().is_file())
                self.run_named(run.stderr)
                self.removes()
                self.gone()

    def test_a_marker_for_another_worktree_is_refused_and_nothing_is_removed(self):
        self.writer("s1")
        other = self.root / "hand-made"
        git(self.repo, "worktree", "add", "-q", "-b", "hand", str(other))
        self.marker().write_text(json.dumps({"schema": lane_remove.MARKER_SCHEMA, "lane": "alpha", "worktree": str(other), "clean": True}))
        self.refused(self.remove(), "removal marker", "not one for this lane's recorded worktree")
        self.assertTrue(other.is_dir())
        self.assertTrue(self.worktree.is_dir())
        for bad in ("{", json.dumps({"schema": lane_remove.MARKER_SCHEMA, "lane": "alpha", "worktree": str(self.worktree), "clean": False}),
                    json.dumps({"schema": "other", "lane": "alpha", "worktree": str(self.worktree), "clean": True})):
            self.marker().write_text(bad)
            self.refused(self.remove(), "removal marker")
            self.assertTrue(self.worktree.is_dir())
        self.marker().unlink()  # deleting the marker is the way back to a normal removal
        self.removes()
        self.gone()

    def test_the_main_checkout_is_never_deleted_by_a_resume(self):
        self.writer("s1")
        path = self.lane_dir() / "lane.json"
        path.write_text(path.read_text().replace(json.loads(path.read_text())["worktree"], str(self.repo)))
        self.marker().write_text(json.dumps({"schema": lane_remove.MARKER_SCHEMA, "lane": "alpha", "worktree": str(self.repo), "clean": True}))
        self.refused(self.remove(), "lane alpha")
        self.assertTrue((self.repo / "lib/x.py").exists())
        self.assertTrue((self.repo / ".git").is_dir())

    def test_a_directory_git_has_no_entry_for_is_not_deleted(self):
        self.writer("s1")
        git(self.repo, "worktree", "remove", str(self.worktree))
        self.worktree.mkdir()
        (self.worktree / "precious.txt").write_text("mine\n")
        self.marker().write_text(json.dumps({"schema": lane_remove.MARKER_SCHEMA, "lane": "alpha", "worktree": str(self.worktree), "clean": True}))
        self.refused(self.remove(), "has no .git link and git has no entry for it")
        self.assertEqual((self.worktree / "precious.txt").read_text(), "mine\n")

    def test_a_git_link_that_is_not_the_plain_file_git_writes_is_not_followed(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        keep = self.root / "keep"
        keep.mkdir()
        (keep / "precious.txt").write_text("mine\n")
        (self.worktree / ".git").unlink()
        (self.worktree / ".git").symlink_to(keep)
        self.refused(self.remove(), "not the worktree git registered")
        self.assertEqual((keep / "precious.txt").read_text(), "mine\n")
        self.assertTrue(self.worktree.is_dir())

    def test_a_worktree_that_is_a_link_is_not_deleted_through_it(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        real = self.root / "real-tree"
        git(self.repo, "worktree", "remove", str(self.worktree))
        real.mkdir()
        (real / "precious.txt").write_text("mine\n")
        self.worktree.symlink_to(real)
        self.refused(self.remove(), "lane alpha")
        self.assertEqual((real / "precious.txt").read_text(), "mine\n")

    def test_a_locked_entry_is_left_when_the_tree_is_gone(self):
        self.writer("s1")
        self.interrupt(lambda: shutil.rmtree(self.worktree))
        entry = next((self.lane_dir().parent.parent.parent / "worktrees").glob("*"))
        (entry / "locked").write_text("mine\n")
        self.refused(self.remove(), "locked", "worktree unlock")
        self.assertTrue(entry.is_dir())


    # --- review findings: what a resume must prove again, since the marker only speaks for the moment it was written -----

    def refuses_resume(self, *fragments):
        with mock.patch.object(lanes, "_git", wraps=lanes._git) as spy, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(ValueError) as caught:
                lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        self.calls = [list(c.args[0]) for c in spy.call_args_list]
        for fragment in fragments:
            self.assertIn(fragment, str(caught.exception))
        self.assertFalse(any(c[:2] == ["worktree", "remove"] for c in self.calls), self.calls)
        self.assertFalse(any("--force" in c for c in self.calls))
        return str(caught.exception)

    def test_a_file_added_after_the_interruption_is_not_deleted_with_a_tree_whose_git_link_is_gone(self):
        self.writer("s1")
        self.interrupt(lambda: (self.deleted_tracked(), (self.worktree / ".git").unlink()))
        (self.worktree / "notes.txt").write_text("mine\n")
        before = self.admin_dirs()
        text = self.refuses_resume("nothing was deleted", "notes.txt", "not tracked")
        self.assertNotIn("stash push", text)  # there is no .git link for git to stash with
        self.assertEqual((self.worktree / "notes.txt").read_text(), "mine\n")
        self.assertEqual(self.admin_dirs(), before)
        self.assertTrue(self.marker().is_file())
        (self.worktree / "notes.txt").unlink()  # what the refusal asks for: the way forward
        self.finish()
        self.gone()

    def test_a_modified_tracked_file_in_a_tree_whose_git_link_is_gone_is_not_deleted(self):
        self.writer("s1")
        self.interrupt(lambda: (self.worktree / ".git").unlink())
        (self.worktree / "lib/x.py").write_text("mine\n")
        self.refuses_resume("nothing was deleted", "lib/x.py")
        self.assertEqual((self.worktree / "lib/x.py").read_text(), "mine\n")

    def test_a_locked_entry_refuses_before_the_leftovers_are_deleted(self):
        self.writer("s1")
        self.ignore_node_modules()

        def damage():
            self.deleted_tracked()
            (self.worktree / "node_modules").mkdir()  # ignored build output, which would go
            (self.worktree / "node_modules/index.js").write_text("x\n")
            (self.worktree / ".git").unlink()

        self.interrupt(damage)
        entry = next((self.lane_dir().parent.parent.parent / "worktrees").glob("*"))
        (entry / "locked").write_text("mine\n")
        self.refuses_resume("locked", "worktree unlock")
        self.assertTrue((self.worktree / "node_modules/index.js").is_file())
        self.assertTrue(entry.is_dir())

    def switch_branch(self):
        git(self.worktree, "switch", "-q", "-c", "personal")
        (self.worktree / "personal.txt").write_text("mine\n")
        git(self.worktree, "add", "personal.txt")
        git(self.worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "personal")

    def test_a_tree_that_was_switched_to_another_branch_after_the_interruption_is_not_removed(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        self.switch_branch()
        self.refuses_resume("no longer on lane/alpha", "left as it is")
        self.assertEqual((self.worktree / "personal.txt").read_text(), "mine\n")
        self.assertTrue(git(self.repo, "rev-parse", "--verify", "-q", "refs/heads/lane/alpha"))
        self.assertTrue(self.marker().is_file())
        git(self.worktree, "switch", "-q", "lane/alpha")  # what the refusal asks for
        self.finish()
        self.gone()

    def test_a_detached_tree_is_not_removed_by_a_resume(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        git(self.worktree, "switch", "-q", "--detach")
        self.refuses_resume("no longer on lane/alpha")
        self.assertTrue(self.worktree.is_dir())

    def test_a_tree_without_a_git_link_that_was_switched_to_another_branch_is_not_deleted(self):
        self.writer("s1")
        self.interrupt(lambda: None)
        self.switch_branch()
        (self.worktree / ".git").unlink()
        self.refuses_resume("no longer on lane/alpha", "nothing was deleted")
        self.assertEqual((self.worktree / "personal.txt").read_text(), "mine\n")
        self.assertEqual(len(self.admin_dirs()), 1)

    def test_a_removal_that_cannot_tell_whether_the_tree_is_clean_does_not_start_and_leaves_nothing_to_resume_wrongly(self):
        self.writer("s1")
        real = lanes._git
        calls = []

        def fake(args, cwd, ok=(0,), timeout=None):
            calls.append(list(args))
            if "status" in args:
                raise lanes.GitTimeout(f"git {' '.join(args)} could not run in {cwd} (TimeoutExpired)")
            return real(args, cwd, ok, timeout)

        with mock.patch.object(lanes, "_git", fake):
            with self.assertRaises(ValueError) as caught:
                lane_remove.remove_lane(str(self.state), self.repo, "alpha")
        self.assertIn("cannot tell whether", str(caught.exception))
        self.assertIn("nothing was removed", str(caught.exception))
        self.assertFalse(any(c[:2] == ["worktree", "remove"] for c in calls), calls)
        self.assertFalse(self.marker().exists())
        self.assertTrue((self.worktree / "lib/x.py").exists())
        self.removes()  # git as it is: the same removal runs
        self.gone()

    def test_a_removal_that_cannot_write_its_marker_does_not_start(self):
        self.writer("s1")
        real = lanes._git
        calls = []

        def spy(args, cwd, ok=(0,), timeout=None):
            calls.append(list(args))
            return real(args, cwd, ok, timeout)

        for failure in (ValueError("cannot read lanes (PermissionError)"),):
            with mock.patch.object(lanes, "_git", spy), mock.patch.object(lanes, "write_lane_file", side_effect=failure):
                with self.assertRaises(ValueError) as caught:
                    lane_remove.remove_lane(str(self.state), self.repo, "alpha")
            self.assertIn("cannot write the removal marker", str(caught.exception))
            self.assertIn("nothing was removed", str(caught.exception))
        self.assertFalse(any(c[:2] == ["worktree", "remove"] for c in calls), calls)
        self.assertFalse(self.marker().exists())
        self.assertTrue((self.worktree / "lib/x.py").exists())
        self.removes()
        self.gone()


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


class ConfiguredIntegrationBranchTests(unittest.TestCase):
    """`lane remove` honours integration_branch from lanes.json; branch_prefix "" names branches exactly like the lane."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent = Path(os.path.realpath(self.tmp.name))
        self.repo = self.parent / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("a\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first")
        self.state = self.parent / "state"
        self.state.mkdir()

    def configure(self, **config):
        (self.repo / ".tmux-pi").mkdir(exist_ok=True)
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps(config))

    def create(self, name="alpha"):
        machine = {"max_active_lanes": 2, "defaulted": []}
        return lane_commands.create_lane(str(self.repo), name, "d", ["lib"], machine=machine)["lane"]

    def commit_in(self, worktree):
        (Path(worktree) / "w.txt").write_text("work\n")
        git(worktree, "add", ".")
        git(worktree, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "work")

    def merge_into(self, branch, lane_branch):
        other = self.parent / "merge-tree"
        git(self.repo, "worktree", "add", "-q", str(other), branch)
        git(other, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff", "-m", "merge", lane_branch)
        git(self.repo, "worktree", "remove", str(other))

    def test_a_lane_merged_into_the_configured_integration_branch_is_removed(self):
        self.configure(integration_branch="dmp-phase-1")
        git(self.repo, "branch", "dmp-phase-1")
        record = self.create()
        self.assertEqual(record["base"]["ref"], "dmp-phase-1")
        self.commit_in(record["worktree"])
        self.merge_into("dmp-phase-1", record["branch"])
        done = lane_remove.remove_lane(str(self.state), str(self.repo), "alpha")
        self.assertEqual((done["abandoned"], done["integrated_in"]), (False, ["dmp-phase-1"]))
        self.assertEqual(git(self.repo, "branch", "--list", record["branch"]), "")
        self.assertFalse(Path(record["worktree"]).exists())

    def test_an_unmerged_lane_is_refused_naming_the_configured_branch(self):
        self.configure(integration_branch="dmp-phase-1")
        git(self.repo, "branch", "dmp-phase-1")
        record = self.create()
        self.commit_in(record["worktree"])
        with self.assertRaises(ValueError) as caught:
            lane_remove.remove_lane(str(self.state), str(self.repo), "alpha")
        self.assertIn("dmp-phase-1", str(caught.exception))
        self.assertNotIn("lane/integrate", str(caught.exception))
        self.assertTrue(Path(record["worktree"]).exists())

    def test_the_default_configuration_still_uses_lane_integrate(self):
        git(self.repo, "branch", "lane/integrate")
        record = self.create()
        self.commit_in(record["worktree"])
        with self.assertRaises(ValueError) as caught:
            lane_remove.remove_lane(str(self.state), str(self.repo), "alpha")
        self.assertIn("lane/integrate", str(caught.exception))
        self.merge_into("lane/integrate", record["branch"])
        done = lane_remove.remove_lane(str(self.state), str(self.repo), "alpha")
        self.assertEqual(done["integrated_in"], ["lane/integrate"])

    def test_an_empty_prefix_names_the_branch_like_the_lane_and_an_existing_branch_is_refused(self):
        self.configure(branch_prefix="", integration_branch="dmp-phase-1")
        git(self.repo, "branch", "dmp-phase-1")
        self.assertEqual(self.create("dmp-23-example")["branch"], "dmp-23-example")
        git(self.repo, "branch", "dmp-24-taken")
        for name in ("dmp-24-taken", "main", "dmp-phase-1"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.create(name)
        self.assertFalse((self.parent / "proj-wt-dmp-24-taken").exists())

    def test_a_non_empty_malformed_prefix_is_still_refused(self):
        for prefix in ("a..b/", "-x", " "):
            with self.subTest(prefix=prefix):
                self.configure(branch_prefix=prefix)
                with self.assertRaises(ValueError) as caught:
                    self.create()
                self.assertIn("branch_prefix", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
