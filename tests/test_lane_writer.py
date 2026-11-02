"""Lanes L3: dispatch into a lane, the one-writer rule and the preamble (bin/tp-agent, bin/tp-spawn, bin/tp-review,
lib/worker_process.py). The numbered checks are those of the design's table (3.1). Every repository is a temporary one and
tmux is the private fixture server of tests/lane_fixture.py; no model runs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lane_fixture import GIT_ENV, MODEL, ROOT, TmuxCase, git  # noqa: E402,F401  (GIT_ENV, git: other lane test files import them from here)


class LaneCase(TmuxCase):
    """A scratch repository (with the integration branch) beside the fixture's state directory. The repository is built once per
    class and copied for each test; the tmux server is the class's (TmuxCase.shared_server)."""
    shared_server = True
    snapshot_lane = False  # True: `lane_ok()` with the default arguments restores the class's first real dispatch (see restore_lane)
    _lane_snapshot = None

    @property
    def stable_root(self):  # a copied lane holds absolute paths (worktree .git file, lane.json), so it must come back to the same path
        return self.snapshot_lane

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._lane_snapshot = None
        cls.template = cls.class_root / "proj-template"
        cls.template.mkdir()
        git(cls.template, "init", "-q", "-b", "main")
        (cls.template / "lib").mkdir()
        (cls.template / "lib/x.py").write_text("x\n")
        (cls.template / "docs").mkdir()
        (cls.template / "docs/d.md").write_text("d\n")
        git(cls.template, "add", ".")
        git(cls.template, "commit", "-q", "-m", "first")
        git(cls.template, "branch", "lane/integrate")
        (cls.template / ".tmux-pi").mkdir()  # the git-ignored preamble lives only in the main tree
        (cls.template / ".tmux-pi/worker-preamble.md").write_text("PREAMBLE-OF-THE-MAIN-TREE\n")
        git(cls.template, "config", "core.excludesFile", "/dev/null")
        (cls.template / ".git/info/exclude").write_text(".tmux-pi/\n")

    def setUp(self):
        super().setUp()
        self.repo = self.root / "proj"
        shutil.copytree(self.template, self.repo, symlinks=True)
        self.worktree = self.root / "proj-wt-alpha"
        self.env["TP_MAIN_CWD"] = str(self.repo)  # the main pane's directory, which a dispatch into an existing lane starts from

    def fingerprint(self):
        """Every path under the root (relative) with its mode and, for a file, the hash of its contents (for a link, its target): two
        roots with the same fingerprint hold the same tree, whatever their mtimes (setUp makes them new each test)."""
        rows = []
        for folder, dirs, files in os.walk(self.root):
            for name in dirs + files:
                path = Path(folder) / name
                info = path.lstat()
                if path.is_symlink():
                    content = os.readlink(path)
                elif path.is_dir():
                    content = ""
                else:
                    content = hashlib.sha256(path.read_bytes()).hexdigest()
                rows.append((str(path.relative_to(self.root)), info.st_mode, content))
        return sorted(rows)

    def reset_fixture(self):
        """Put the root back as setUp left it (empty state, a fresh repository copy), for a test that dispatches from nothing twice."""
        for child in self.root.iterdir():
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
        self.lay_out_root()
        shutil.copytree(self.template, self.repo, symlinks=True)

    def restore_lane(self):
        """Replace the root's content with the class's snapshot (the root right after its first real dispatch). Returns that dispatch's stdout."""
        _, stdout, saved = self._lane_snapshot
        shutil.rmtree(self.root)
        shutil.copytree(saved, self.root, symlinks=True)
        return stdout

    def lane_args(self, name="alpha", step="s1", role="coder", extra=("--repo", None, "--domain", "lib part", "--touches", "lib"), tests="none"):
        extra = [str(self.repo) if part is None else part for part in extra]
        return ["dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                "--deliverable", "Report", f"--lane={name}", f"--step={step}", "--tests", tests, *extra]

    def lane_dispatch(self, **kw):
        return self.raw(*self.lane_args(**kw))

    def lane_ok(self, snapshot=True, **kw):
        """A dispatch that must succeed; returns (item, task dir). With `snapshot_lane`, the first call in the class is a real dispatch
        whose resulting root is copied aside, keyed on the root just before it; a later default call with the same key restores that copy
        instead of dispatching. A call with other arguments is always real, and neither takes nor uses the snapshot."""
        key = self.snapshot_key(snapshot, **kw)
        if key and self._lane_snapshot and self._lane_snapshot[0] == key:
            return self.lane_item(self.restore_lane())
        run = self.lane_dispatch(**kw)
        self.assertEqual(run.returncode, 0, run.stderr)
        if key and not self._lane_snapshot:
            saved = self.class_root / "snapshot"
            shutil.copytree(self.root, saved, symlinks=True)
            type(self)._lane_snapshot = (key, run.stdout, saved)
        return self.lane_item(run.stdout)

    def snapshot_key(self, snapshot=True, **kw):
        """What a snapshot is valid for: the default call (the arguments of a bare `lane_ok()`; `step="s1"` is that call) on this root's
        tree (relative paths, modes, contents) and environment. None for any other call, and when the class has not opted in: those
        are always real dispatches."""
        if not (snapshot and self.snapshot_lane) or self.lane_args(**kw) != self.lane_args():
            return None
        return (self.fingerprint(), sorted(self.env.items()))

    def lane_item(self, stdout):
        item = json.loads(stdout)
        return item, self.state / "tasks" / item["id"]

    def existing(self, **kw):
        """Dispatch into the lane that already exists: no declaration flags."""
        return self.lane_ok(extra=(), **kw)

    def refused(self, run, *words):
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        for word in words:
            self.assertIn(word, run.stderr)

    def no_lane(self):
        self.assertFalse(self.worktree.exists())
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")

    def lane_dir(self):
        return Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "tmux-pi/lanes/alpha"


class DispatchIntoALaneTests(LaneCase):
    def test_a_new_lane_is_created_and_the_task_runs_in_its_tree_with_the_tags_recorded(self):
        item, task = self.lane_ok()
        self.assertEqual((item["lane"], item["lane_step"], item["lane_created"]), ("alpha", "s1", True))
        self.assertTrue(self.worktree.is_dir())
        meta = json.loads((task / "meta.json").read_text())
        self.assertEqual(os.path.realpath(meta["workdir"]), os.path.realpath(self.worktree))
        self.assertEqual((meta["lane"], meta["lane_step"]), ("alpha", "s1"))
        self.assertEqual(os.path.realpath(meta["preamble_from"]), os.path.realpath(self.repo))
        record = json.loads((self.lane_dir() / "lane.json").read_text())
        self.assertEqual(record["name"], "alpha")

    def test_the_preamble_comes_from_the_main_tree_because_the_lane_has_none(self):
        _, task = self.lane_ok()
        self.assertFalse((self.worktree / ".tmux-pi/worker-preamble.md").exists())
        self.assertIn("PREAMBLE-OF-THE-MAIN-TREE", (task / "system-prompt.md").read_text())

    def test_the_lanes_own_preamble_wins_when_it_has_one(self):
        self.lane_ok()
        (self.worktree / ".tmux-pi").mkdir(exist_ok=True)
        (self.worktree / ".tmux-pi/worker-preamble.md").write_text("PREAMBLE-OF-THE-LANE\n")
        # the first task still holds the lane (it has a pane); end it so a second can start
        first = next((self.state / "tasks").iterdir())
        pane, _ = self.pane()
        self.attach(first, pane)
        self.settle(first)
        self.cli("collect", first.name)
        item, task = self.existing(step="s1")
        prompt = (task / "system-prompt.md").read_text()
        self.assertIn("PREAMBLE-OF-THE-LANE", prompt)
        self.assertNotIn("PREAMBLE-OF-THE-MAIN-TREE", prompt)

    def test_a_second_task_for_the_same_lane_is_refused_while_a_writer_is_alive_check_18(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "lane alpha already has a writer", item["id"])
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)

    def test_a_queued_item_holds_the_lane_too_check_18(self):
        item, task = self.lane_ok()  # the stub gives it no live pane, so it is not "alive"; queue one by hand
        queued = {"id": "coder-queued0001", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s1", "preamble_from": str(self.repo)}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-queued0001.json").write_text(json.dumps(queued))
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "already has a writer", "coder-queued0001")

    def test_a_reviewer_waits_for_a_writer_but_not_for_another_reviewer_check_18(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        # a reviewer waits for the live writer ...
        self.refused(self.lane_dispatch(role="code-reviewer", step="s1", extra=()), "already has a writer", item["id"])
        self.tmux("kill-pane", "-t", pane)
        # ... but a live reviewer does not hold another reviewer; it does hold a writer
        reviewer, reviewer_task = self.lane_ok(role="code-reviewer", step="s1", extra=())
        pane2, _ = self.pane()
        self.attach(reviewer_task, pane2)
        self.lane_ok(role="code-reviewer", step="s1", extra=())
        self.refused(self.lane_dispatch(step="s1", extra=()), "in use by a reviewer", reviewer["id"])

    def test_a_dead_pane_does_not_hold_the_lane(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        self.tmux("kill-pane", "-t", pane)
        item2, _ = self.existing(step="s1")
        self.assertEqual(item2["lane_created"], False)

    def test_two_dispatches_at_once_create_the_lane_once_and_only_one_gets_the_writer(self):
        _, pid = self.pane()
        (self.state / "list-rows").write_text(f"%9999 0 {pid}\n")  # a started task's stub pane is live, so it holds the lane
        procs = [subprocess.Popen([str(ROOT / "bin/tp-agent"), *self.lane_args(step="s1")], env=self.env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        outs = [p.communicate(timeout=90) + (p.returncode,) for p in procs]
        self.assertEqual(sorted(o[2] for o in outs), [0, 1], outs)
        refusal = next(o[1] for o in outs if o[2])  # a writer, or (when it planned after the other finished) an existing lane
        self.assertTrue("already has a writer" in refusal or "already exists" in refusal, refusal)
        self.assertEqual(len(list(self.root.glob("proj-wt-*"))), 1)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)

    def test_an_explicit_branch_names_the_new_lanes_branch(self):
        item, _ = self.lane_ok(extra=("--repo", None, "--domain", "lib part", "--touches", "lib", "--branch", "DMP-23-Seed-Scaffold"))
        self.assertTrue(item["lane_created"])
        record = json.loads((self.lane_dir() / "lane.json").read_text())
        self.assertEqual(record["branch"], "DMP-23-Seed-Scaffold")
        self.assertEqual(git(self.worktree, "symbolic-ref", "--short", "HEAD"), "DMP-23-Seed-Scaffold")
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")

    def test_a_case_insensitive_branch_clash_is_refused_before_any_worktree_exists(self):
        git(self.repo, "branch", "DMP-23-Seed-Scaffold")
        run = self.lane_dispatch(extra=("--repo", None, "--domain", "lib part", "--touches", "lib", "--branch", "dmp-23-seed-scaffold"))
        self.refused(run, "dmp-23-seed-scaffold")
        self.no_lane()
        self.assertEqual(len(list((self.state / "tasks").iterdir())) if (self.state / "tasks").exists() else 0, 0)


class RefusalTableTests(LaneCase):
    """The design's numbered checks. A refusal makes no lane, no task and no queue entry. One test per lane state, one subTest per
    row of the check; a row that changes the repository puts it back before the next."""

    def nothing_started(self):
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(list((self.state / "queue").glob("*.json")) if (self.state / "queue").exists() else [], [])

    def no_dispatch(self, **kw):
        return lambda: self.lane_dispatch(**kw)

    def no_lane_rows(self):
        """(label, make_run, words, setup, teardown): every dispatch here is a new lane that is refused before anything is made."""
        new = ("--repo", None, "--domain", "d")
        rows = [(f"01 lane name {name!r}", self.no_dispatch(name=name), ("lane",)) for name in ("Alpha", "-x", "a b", "archive", "a" * 30)]
        rows += [(f"02 step id {step!r}", self.no_dispatch(step=step), ("step id",)) for step in ("bad step", "-s")]
        rows += [
            ("03 no --step", lambda: self.raw(*(arg for arg in self.lane_args() if not arg.startswith("--step="))), ("--step",)),
            ("04 --workdir with a lane", lambda: self.raw(*self.lane_args(), "--workdir", str(self.repo)), ("--workdir",)),
            ("05 --tests full", self.no_dispatch(tests="full"), ("pass --tests affected or --tests none", "run once, from the main checkout")),
            ("09 no --domain", self.no_dispatch(extra=("--repo", None)), ("--domain",)),
            ("09 no --touches", self.no_dispatch(extra=("--repo", None, "--domain", "x")), ("--touches",)),
        ]
        rows += [(f"14 touch {touches}", self.no_dispatch(extra=(*new, "--touches", touches)), (word,))
                 for touches, word in (("/abs", "is absolute"), ("../up", "not normalised"), ("a/../b", "not normalised"))]
        return rows

    def test_a_new_lane_that_is_refused_makes_no_lane_no_task_and_no_queue_entry(self):
        for label, make_run, words in self.no_lane_rows():
            with self.subTest(label):
                self.refused(make_run(), *words)
                self.no_lane()
                self.nothing_started()
        with self.subTest("14 a touch the orchestrator owns"):
            (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"owned": ["docs"]}))
            self.refused(self.lane_dispatch(extra=("--repo", None, "--domain", "d", "--touches", "docs/d.md")), "is owned by the orchestrator")
            self.no_lane()
            self.nothing_started()
            (self.repo / ".tmux-pi/lanes.json").unlink()
        with self.subTest("08 a branch in the way"):
            git(self.repo, "branch", "lane/alpha")
            self.refused(self.lane_dispatch(), "lane/alpha")
            self.assertFalse(self.worktree.exists())
            self.nothing_started()
            git(self.repo, "branch", "-D", "lane/alpha")
        with self.subTest("08 a path in the way is never reused or touched"):
            self.worktree.mkdir()
            (self.worktree / "keep.txt").write_text("mine\n")
            self.refused(self.lane_dispatch(), "already exists")
            self.assertEqual((self.worktree / "keep.txt").read_text(), "mine\n")
            self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")
            self.nothing_started()

    def test_06_a_malformed_machine_config_refuses_naming_key_and_file(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import lanes
        bad = self.root / "orchestration.json"
        bad.write_text(json.dumps({"lanes": {"max_active_lanes": 0}}))
        with self.assertRaises(ValueError) as caught:
            lanes.load_machine_config(bad)
        self.assertIn("max_active_lanes", str(caught.exception))
        self.assertIn("config/orchestration.json", str(caught.exception))
        bad.write_text(json.dumps({"lanes": 5}))
        with self.assertRaises(ValueError):
            lanes.load_machine_config(bad)
        bad.write_text("{not json")
        with self.assertRaises(ValueError):
            lanes.load_machine_config(bad)
        bad.write_text("{}")
        self.assertEqual(lanes.load_machine_config(bad), {"max_active_lanes": 2, "defaulted": ["max_active_lanes"]})

    def test_a_dispatch_into_an_existing_lane_is_refused_by_its_record_its_flags_its_git_and_its_other_tasks(self):
        """Lane alpha exists and its first task is collected; each row breaks one thing, refuses, and puts it back."""
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        path = self.lane_dir() / "lane.json"
        good = path.read_text()
        with self.subTest("07 a corrupt record names the file"):
            path.write_text("{broken")
            self.refused(self.lane_dispatch(step="s1", extra=()), "lane.json")
            path.write_text(good)
        for flags in (("--domain", "other"), ("--touches", "docs"), ("--base", "main")):
            with self.subTest(f"10 declaration flag {flags[0]}"):
                self.refused(self.lane_dispatch(step="s1", extra=flags), "already exists")
        with self.subTest("11 a lane that is not open"):
            record = json.loads(good)
            record["state"] = "abandoned"
            path.write_text(json.dumps(record))
            self.refused(self.lane_dispatch(step="s1", extra=()), "abandoned")
            path.write_text(good)
        with self.subTest("12 a lane git does not agree with"):
            git(self.worktree, "checkout", "-q", "--detach")
            run = self.lane_dispatch(step="s1", extra=())
            self.assertEqual(run.returncode, 1, run.stdout)
            self.assertNotEqual(run.stderr.strip(), "")
            git(self.worktree, "checkout", "-q", "lane/alpha")
        with self.subTest("19 a task that cannot be ruled out as a writer"):
            # a live task with no recorded tree: it might be a writer on any tree
            other = self.state / "tasks" / "coder-notree0001"
            other.mkdir()
            (other / "meta.json").write_text(json.dumps({"id": other.name, "pane": "%9999", "role": "coder"}))
            (self.state / "list-rows").write_text(f"%9999 0 {self.pane()[1]}\n")
            self.refused(self.lane_dispatch(step="s1", extra=()), "no recorded working tree")
            (self.state / "list-rows").unlink()
            # a task directory with no readable record, while a process of it runs (its run.sh): it may be writing anywhere
            (other / "meta.json").write_text("{broken")
            pane, _ = self.pane(self.worker_command(other))
            self.refused(self.lane_dispatch(step="s1", extra=()), other.name, "no readable meta.json", f"tmux kill-pane -t {pane}")
            (other / "meta.json").unlink()
            self.refused(self.lane_dispatch(step="s1", extra=()), other.name, "no readable meta.json")
            self.tmux("kill-pane", "-t", pane)
            # a pane table that cannot be read
            other.rmdir()
            (self.state / "list-fail").write_text("")
            self.refused(self.lane_dispatch(step="s1", extra=()), "tmux panes")
            (self.state / "list-fail").unlink()
            self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), [item["id"]])
        with self.subTest("10 an existing lane can be located with --repo"):
            run = self.lane_dispatch(step="s1", extra=("--repo", str(self.repo)))
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_13_the_active_lane_limit_refuses_a_new_lane(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import lanes
        limit = lanes.load_machine_config()["max_active_lanes"]
        for index in range(limit):
            self.lane_ok(name=f"l{index}", extra=("--repo", None, "--domain", f"d{index}", "--touches", f"area{index}"))
        run = self.lane_dispatch(name="extra", extra=("--repo", None, "--domain", "e", "--touches", "elsewhere"))
        self.refused(run, "lane")
        self.assertFalse((self.root / "proj-wt-extra").exists())


class NoWayAroundTheLaneTests(LaneCase):
    def make_lane(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])

    def plain(self, *workdir, role="coder"):
        return self.raw("dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL,
                        "--thinking", "medium", "--deliverable", "Report", *workdir)

    def test_a_plain_dispatch_with_the_lane_tree_as_workdir_refuses_naming_the_lane(self):
        self.make_lane()
        before = sorted(p.name for p in (self.state / "tasks").iterdir())
        self.refused(self.plain("--workdir", str(self.worktree)), "alpha", "--lane")
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)

    def test_a_subdirectory_and_a_symlink_into_the_lane_refuse_too(self):
        self.make_lane()
        (self.worktree / "lib").mkdir(exist_ok=True)
        self.refused(self.plain("--workdir", str(self.worktree / "lib")), "alpha")
        link = self.root / "link"
        link.symlink_to(self.worktree)
        self.refused(self.plain("--workdir", str(link)), "alpha")

    def test_the_main_tree_still_dispatches_without_a_lane(self):
        self.make_lane()
        run = self.plain("--workdir", str(self.repo))
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_tp_spawn_itself_refuses_a_lane_tree_without_the_lane(self):
        # the backstop that covers every spawner (dispatcher.ts, subagents.ts, the MCP server) at once
        self.make_lane()
        run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--check", "--role", "coder", "--workdir", str(self.worktree), "--", "check"],
                             env=self.env, text=True, capture_output=True, timeout=60)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("alpha", run.stderr)

    def test_lane_flags_without_a_lane_refuse(self):
        for flag, value in (("--step", "s1"), ("--domain", "d"), ("--touches", "lib"), ("--repo", str(self.repo)), ("--base", "main")):
            self.refused(self.plain(flag, value), flag, "--lane")


class RetryAndReviewTests(LaneCase):
    def test_retry_carries_the_lane_and_step_from_the_prior_meta_and_runs_in_the_lane_tree(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        retried = self.cli("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        meta = json.loads((self.state / "tasks" / retried["id"] / "meta.json").read_text())
        self.assertEqual((meta["lane"], meta["lane_step"]), ("alpha", "s1"))
        self.assertEqual(os.path.realpath(meta["workdir"]), os.path.realpath(self.worktree))
        self.assertEqual(os.path.realpath(meta["preamble_from"]), os.path.realpath(self.repo))

    def test_retry_takes_its_lane_from_nowhere_but_the_prior_meta(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL, "--lane", "other", "--step", "z")
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), [item["id"]])

    def test_retry_of_a_half_tagged_task_refuses(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        meta = json.loads((task / "meta.json").read_text())
        del meta["lane_step"]
        (task / "meta.json").write_text(json.dumps(meta))
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.refused(run, "incomplete lane")

    def test_retry_beside_a_second_live_writer_in_the_lane_refuses(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        second, task2 = self.existing(step="s1")
        pane, _ = self.pane()
        self.attach(task2, pane)
        run = self.raw("retry", item["id"], "--reason", "Again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn(pane, run.stderr)

    def test_the_reviewer_gets_the_lane_tags(self):
        item, task = self.lane_ok()
        self.settle(task)
        run = subprocess.run([str(ROOT / "bin/tp-review"), item["id"]], env=self.env, text=True, capture_output=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        reviewers = [json.loads(p.read_text()) for p in self.state.glob("tasks/review-*/meta.json")]
        self.assertEqual(len(reviewers), 1, (run.stdout, run.stderr))
        self.assertEqual((reviewers[0]["lane"], reviewers[0]["lane_step"]), ("alpha", "s1"))
        self.assertEqual(os.path.realpath(reviewers[0]["workdir"]), os.path.realpath(self.worktree))


class TickTests(LaneCase):
    def queued(self, ident, step):
        item = {"id": ident, "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": step, "preamble_from": str(self.repo),
                "queue_reason": "waiting"}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue" / f"{ident}.json").write_text(json.dumps(item))
        return item

    def test_tick_leaves_an_item_queued_while_the_lanes_writer_is_alive_and_starts_it_after(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        self.queued("coder-aaaaaaaaaa", "s1")
        self.cli("tick")
        entry = json.loads((self.state / "queue/coder-aaaaaaaaaa.json").read_text())
        self.assertIn("waiting for lane alpha's writer", entry.get("queue_reason", ""))
        self.tmux("kill-pane", "-t", pane)
        self.cli("tick")
        self.assertFalse((self.state / "queue/coder-aaaaaaaaaa.json").exists())
        self.assertTrue((self.state / "tasks/coder-aaaaaaaaaa").is_dir())

    def test_the_oldest_queued_item_of_a_lane_starts_first(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.attach(task, "%8888")  # not on the pane table: the first task is over
        self.cli("collect", item["id"])
        self.queued("coder-aaaaaaaaaa", "s1")
        self.queued("coder-bbbbbbbbbb", "s1")
        _, pid = self.pane()  # every launched task has the stub's pane %9999; this row makes that pane live, as a started worker's is
        (self.state / "list-rows").write_text(f"%9999 0 {pid}\n")
        self.cli("tick")
        started = sorted(p.name for p in (self.state / "tasks").iterdir() if p.name.startswith("coder-") and p.name != item["id"])
        self.assertEqual(started, ["coder-aaaaaaaaaa"])
        self.assertTrue((self.state / "queue/coder-bbbbbbbbbb.json").exists())

    def test_an_unreadable_queue_entry_for_the_lane_refuses_dispatch(self):
        self.lane_ok()
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-broken0001.json").write_text(json.dumps({"id": "coder-broken0001", "lane": "alpha"}))
        run = self.lane_dispatch(step="s1", extra=())
        self.refused(run, "cannot be read")


class ConcurrentCreateTests(LaneCase):
    """A dispatch planned for a new lane that finds the lane made meanwhile keeps only a declaration that matches the record."""

    def admit(self, **declared):
        from importlib.machinery import SourceFileLoader
        agent = SourceFileLoader("tp_agent_admit", str(ROOT / "bin/tp-agent")).load_module()
        sys.path.insert(0, str(ROOT / "lib"))
        import lane_commands
        import lanes
        lane_commands.create_lane(str(self.repo), "alpha", "lib part", ["lib"], machine={"max_active_lanes": 2, "defaulted": []})
        plan = {"name": "alpha", "step": "s1", "common": lanes.git_common_dir(self.repo), "domain": "lib part", **declared}
        agent.lane_admit(self.state, plan, {"role": "coder"})

    def test_a_different_branch_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.admit(branch="DMP-1-Other")
        self.assertIn("created meanwhile with branch lane/alpha", str(caught.exception))

    def test_a_different_base_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            self.admit(base_requested="main")
        self.assertIn("created meanwhile with base lane/integrate", str(caught.exception))


class LaneSnapshotTests(LaneCase):
    """The opt-in snapshot of the first lane dispatch (LaneCase.snapshot_lane): off unless a class asks, and a restored dispatch is
    the real one (lane record, task, git state, state dir, and what `lanes`, `lane close` and `lane remove` then do)."""
    snapshot_lane = True

    def normalized(self, text, task_id):
        text = text.replace(str(self.root), "<root>").replace(task_id, "<task>")
        text = re.sub(r"\d{4}-\d\d-\d\dT[\d:]+(?:Z|\+00:00)", "<time>", text)
        text = re.sub(r"\d{8}T\d{6}Z", "<time>", text)  # the archive name's compact stamp
        text = re.sub(r'"age": "[^"]*"', '"age": "<age>"', text)  # `lanes` shows the lane's age: a restored lane is older than a fresh one
        return re.sub(r"sel-[0-9a-f]{32}", "<selection>", text)

    def described(self, item, task):
        """What a dispatch left, with ids, times and the root path replaced."""
        lane = self.lane_dir()
        tree = lambda base: sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file())
        meta = json.loads((task / "meta.json").read_text())
        rows = {
            "item": json.dumps(item, sort_keys=True), "meta": json.dumps(meta, sort_keys=True), "meta keys": sorted(meta),
            "lane files": {name: (lane / name).read_text() for name in tree(lane)},
            "state files": tree(self.state), "worktrees": git(self.repo, "worktree", "list"),
            "branches": git(self.repo, "branch", "-a", "-v"), "status": git(self.worktree, "status", "--porcelain", "--branch"),
        }
        return json.loads(self.normalized(json.dumps(rows, sort_keys=True), task.name))

    def then(self, task_id):
        """What the commands that read and end a lane do from here: (exit code, stdout, stderr) of each, and the state after. Each must succeed."""
        out = []
        self.settle(self.state / "tasks" / task_id)  # the worker is done (a step with a running task cannot be closed or removed)
        for args in (("lanes", "--repo", str(self.repo)), ("lane", "remove", "alpha", "--repo", str(self.repo)),
                     ("lanes", "--repo", str(self.repo))):
            run = self.raw(*args)
            self.assertEqual(run.returncode, 0, f"{args}: {run.stdout}{run.stderr}")
            out.append((run.returncode, self.normalized(run.stdout, task_id), self.normalized(run.stderr, task_id)))
        out.append((self.worktree.exists(), git(self.repo, "branch", "--list", "lane/alpha"), self.lane_dir().exists()))
        return out

    def reserves_and_releases(self):
        """The lane is free to take: a writer's reservation by this process succeeds, shows in lease.json, and is released."""
        lease = self.lane_dir() / "lease.json"
        words = ["--lane", "alpha", "--workdir", str(self.worktree), "--task", "coder-probe0001"]
        script = str(ROOT / "lib/lane_lease.py")
        run = subprocess.run([sys.executable, script, "reserve", *words, "--role", "coder", "--session", str(self.state), "--pid", str(os.getpid())],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([h["task"] for h in json.loads(lease.read_text())["holders"]], ["coder-probe0001"])
        run = subprocess.run([sys.executable, script, "release", *words], text=True, capture_output=True, env=self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(lease.read_text())["holders"] if lease.exists() else [], [])

    def expect_dispatched(self, item, task):
        """What a lane's first dispatch must have left, stated outright (not compared with another copy of itself)."""
        record = json.loads((self.lane_dir() / "lane.json").read_text())
        self.assertEqual({k: record[k] for k in ("schema", "name", "kind", "state", "branch", "domain", "touches", "closed", "integrated", "deps")},
                         {"schema": "tp-lane:v1", "name": "alpha", "kind": "work", "state": "open", "branch": "lane/alpha",
                          "domain": "lib part", "touches": ["lib"], "closed": [], "integrated": None, "deps": "own"})
        self.assertEqual(record["worktree"], str(self.worktree))
        self.assertEqual(record["repo"], str(self.repo))
        self.assertEqual(record["base"]["ref"], "lane/integrate")
        self.assertEqual(record["base"]["commit"], git(self.repo, "rev-parse", "lane/integrate"))
        self.assertEqual(sorted(record["defaulted"]), ["branch_prefix", "integration_branch", "integration_lane", "worktree_pattern"])
        self.assertTrue(record["created"])
        self.assertEqual((item["lane"], item["lane_step"], item["lane_created"]), ("alpha", "s1", True))
        meta = json.loads((task / "meta.json").read_text())
        self.assertEqual((meta["lane"], meta["lane_step"]), ("alpha", "s1"))
        self.assertEqual(meta["pane"], "%9999")  # written after tp-spawn's `activate`, which refuses a task that holds no reservation
        self.assertTrue(self.worktree.is_dir())
        self.assertIn("lane/alpha", git(self.repo, "worktree", "list"))
        # the lease: a dispatch starts no worker, so the lane holds no occupant (a lease.json naming one would block the commands below)
        lease = self.lane_dir() / "lease.json"
        self.assertEqual(json.loads(lease.read_text())["holders"] if lease.exists() else [], [])

    def forget_snapshot(self):
        type(self)._lane_snapshot = None
        shutil.rmtree(self.class_root / "snapshot", ignore_errors=True)

    def count_dispatches(self):
        """A list that gains one entry for each real dispatch this test makes."""
        counted = []
        real = self.lane_dispatch
        self.lane_dispatch = lambda **kw: (counted.append(kw), real(**kw))[1]
        return counted

    def test_the_snapshot_is_off_by_default_and_a_key_changes_with_the_tree_the_environment_and_the_arguments(self):
        self.assertFalse(LaneCase.snapshot_lane)
        self.assertFalse(DispatchIntoALaneTests.snapshot_lane)
        self.assertFalse(DispatchIntoALaneTests.stable_root.fget(DispatchIntoALaneTests))
        key = self.snapshot_key()
        self.assertIsNotNone(key)
        self.assertIsNone(self.snapshot_key(snapshot=False))
        self.assertEqual(self.snapshot_key(step="s1", role="coder", name="alpha"), key)  # defaults spelled out are the default call
        for other in ({"name": "beta"}, {"role": "code-reviewer"}, {"step": "s2"}, {"extra": ()}, {"tests": "unit"}):
            self.assertIsNone(self.snapshot_key(**other), f"{other} is not the default call: it is never cached or restored")
        (self.state / "live-window").touch()  # a flag file
        self.assertNotEqual(self.snapshot_key(), key)
        (self.state / "live-window").unlink()
        self.assertEqual(self.snapshot_key(), key, "the same tree again is the same key")
        (self.repo / "lib/x.py").write_text("edited\n")  # a repository edit
        self.assertNotEqual(self.snapshot_key(), key)
        self.reset_fixture()
        self.assertEqual(self.snapshot_key(), key)
        edited = self.repo / "lib/x.py"  # same size, same mtime, other contents
        before = edited.stat()
        edited.write_text("y\n")
        os.utime(edited, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertNotEqual(self.snapshot_key(), key)
        self.reset_fixture()
        self.assertEqual(self.snapshot_key(), key)
        self.env["TP_SESSION"] = "other"
        self.assertNotEqual(self.snapshot_key(), key)

    def test_a_restored_dispatch_equals_the_real_one_and_every_later_command_agrees(self):
        self.forget_snapshot()  # another test of the class may have taken it already
        counted = self.count_dispatches()
        item, task = self.lane_ok()  # the first call is a real dispatch and takes the snapshot
        self.assertIsNotNone(self._lane_snapshot)
        self.expect_dispatched(item, task)
        self.reserves_and_releases()
        real = self.described(item, task), self.then(task.name)
        self.reset_fixture()
        self.assertFalse(self.worktree.exists())
        item2, task2 = self.lane_ok()  # restored
        self.assertEqual(len(counted), 1, "the second call must not dispatch")
        self.assertEqual(item2, item)  # the snapshot carries the real dispatch's own answer, id included
        self.expect_dispatched(item2, task2)
        self.reserves_and_releases()
        restored = self.described(item2, task2), self.then(task2.name)
        self.assertEqual(restored[0], real[0])
        self.assertEqual(restored[1], real[1])
        self.assertTrue(task2.is_dir())
        self.assertEqual(real[0]["worktrees"].count("lane/alpha"), 1)

    def test_a_call_that_differs_or_follows_a_touched_root_dispatches_for_real(self):
        self.forget_snapshot()
        counted = self.count_dispatches()
        self.lane_ok(name="beta")  # not the default call: real, and it does not take the snapshot
        self.assertIsNone(self._lane_snapshot)
        self.reset_fixture()
        self.lane_ok()  # the first default call takes it
        self.assertIsNotNone(self._lane_snapshot)
        self.reset_fixture()
        (self.state / "live-window").touch()
        self.lane_ok()  # a root that differs from the snapshot's: real
        self.assertEqual(len(counted), 3)
        self.reset_fixture()
        self.lane_ok()  # the snapshot is still the first default call's
        self.assertEqual(len(counted), 3)


class LeaseShapedSnapshotTests(LaneCase):
    """A class like LeaseCase: its setUp adds a second session's folders under the root after LaneCase.setUp."""
    snapshot_lane = True

    def setUp(self):
        super().setUp()
        (self.root / "session2" / ".tmux-pi" / "tasks").mkdir(parents=True)

    def test_the_second_dispatch_is_restored(self):
        counted = []
        real = self.lane_dispatch
        self.lane_dispatch = lambda **kw: (counted.append(kw), real(**kw))[1]
        self.lane_ok()
        self.reset_fixture()
        (self.root / "session2" / ".tmux-pi" / "tasks").mkdir(parents=True)
        self.lane_ok()
        self.assertEqual(len(counted), 1)
        self.assertTrue(self.worktree.is_dir())


class StepShapedSnapshotTests(LaneCase):
    """A class like StepCase: its first writer spells the default step out (`lane_ok(step="s1")`)."""
    snapshot_lane = True

    def test_the_second_dispatch_is_restored(self):
        counted = []
        real = self.lane_dispatch
        self.lane_dispatch = lambda **kw: (counted.append(kw), real(**kw))[1]
        self.lane_ok(step="s1")
        self.reset_fixture()
        self.lane_ok(step="s1")
        self.assertEqual(len(counted), 1)
        self.assertTrue(self.worktree.is_dir())


class AliveOnWorkdirTests(unittest.TestCase):
    def test_alive_on_tree_is_alive_on_workdir_with_the_same_answer(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import worker_process
        self.assertTrue(callable(worker_process.alive_on_workdir))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tasks = Path(tmp)
            self.assertEqual(worker_process.alive_on_workdir(tasks, tmp), [])


if __name__ == "__main__":
    unittest.main()
