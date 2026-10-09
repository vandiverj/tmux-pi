"""Lanes L3: dispatch into a lane, the one-writer rule and the preamble (bin/tp-agent, bin/tp-spawn, bin/tp-review,
lib/worker_process.py). The numbered checks are those of the design's table (3.1). Every repository is a temporary one and
tmux is the private fixture server of tests/lane_fixture.py; no model runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lane_fixture import MODEL, ROOT, TmuxCase  # noqa: E402

GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True, env=GIT_ENV)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


class LaneCase(TmuxCase):
    """A scratch repository (with the integration branch) beside the fixture's state directory."""

    def setUp(self):
        super().setUp()
        self.repo = self.root / "proj"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "lib").mkdir()
        (self.repo / "lib/x.py").write_text("x\n")
        (self.repo / "docs").mkdir()
        (self.repo / "docs/d.md").write_text("d\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "first")
        git(self.repo, "branch", "lane/integrate")
        (self.repo / ".tmux-pi").mkdir()  # the git-ignored preamble lives only in the main tree
        (self.repo / ".tmux-pi/worker-preamble.md").write_text("PREAMBLE-OF-THE-MAIN-TREE\n")
        git(self.repo, "config", "core.excludesFile", "/dev/null")
        (self.repo / ".git/info/exclude").write_text(".tmux-pi/\n")
        self.worktree = self.root / "proj-wt-alpha"
        self.env["TP_MAIN_CWD"] = str(self.repo)  # the main pane's directory, which a dispatch into an existing lane starts from

    def lane_args(self, name="alpha", step="s1", role="coder", extra=("--repo", None, "--domain", "lib part", "--touches", "lib"), tests="none"):
        extra = [str(self.repo) if part is None else part for part in extra]
        return ["dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                "--deliverable", "Report", f"--lane={name}", f"--step={step}", "--tests", tests, *extra]

    def lane_dispatch(self, **kw):
        return self.raw(*self.lane_args(**kw))

    def lane_ok(self, **kw):
        run = self.lane_dispatch(**kw)
        self.assertEqual(run.returncode, 0, run.stderr)
        item = json.loads(run.stdout)
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
        item, task = self.existing(step="s2")
        prompt = (task / "system-prompt.md").read_text()
        self.assertIn("PREAMBLE-OF-THE-LANE", prompt)
        self.assertNotIn("PREAMBLE-OF-THE-MAIN-TREE", prompt)

    def test_a_second_task_for_the_same_lane_is_refused_while_a_writer_is_alive_check_18(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, "lane alpha already has a writer", item["id"])
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)

    def test_a_queued_item_holds_the_lane_too_check_18(self):
        item, task = self.lane_ok()  # the stub gives it no live pane, so it is not "alive"; queue one by hand
        queued = {"id": "coder-queued0001", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s0", "preamble_from": str(self.repo)}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-queued0001.json").write_text(json.dumps(queued))
        run = self.lane_dispatch(step="s2", extra=())
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
        self.refused(self.lane_dispatch(step="s2", extra=()), "in use by a reviewer", reviewer["id"])

    def test_a_dead_pane_does_not_hold_the_lane(self):
        item, task = self.lane_ok()
        pane, _ = self.pane()
        self.attach(task, pane)
        self.tmux("kill-pane", "-t", pane)
        item2, _ = self.existing(step="s2")
        self.assertEqual(item2["lane_created"], False)

    def test_two_dispatches_at_once_create_the_lane_once_and_only_one_gets_the_writer(self):
        _, pid = self.pane()
        (self.state / "list-rows").write_text(f"%9999 0 {pid}\n")  # a started task's stub pane is live, so it holds the lane
        procs = [subprocess.Popen([str(ROOT / "bin/tp-agent"), *self.lane_args(step=f"s{i}")], env=self.env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(2)]
        outs = [p.communicate(timeout=90) + (p.returncode,) for p in procs]
        self.assertEqual(sorted(o[2] for o in outs), [0, 1], outs)
        refusal = next(o[1] for o in outs if o[2])  # a writer, or (when it planned after the other finished) an existing lane
        self.assertTrue("already has a writer" in refusal or "already exists" in refusal, refusal)
        self.assertEqual(len(list(self.root.glob("proj-wt-*"))), 1)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)


class RefusalTableTests(LaneCase):
    """The design's numbered checks, one test each. A refusal makes no lane, no task and no queue entry."""

    def nothing_started(self):
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(list((self.state / "queue").glob("*.json")) if (self.state / "queue").exists() else [], [])

    def test_01_a_bad_lane_name(self):
        for name in ("Alpha", "-x", "a b", "archive", "a" * 30):
            self.refused(self.lane_dispatch(name=name), "lane")
        self.nothing_started()

    def test_02_a_bad_step_id(self):
        self.refused(self.lane_dispatch(step="bad step"), "step id")
        self.refused(self.lane_dispatch(step="-s"), "step id")
        self.no_lane()
        self.nothing_started()

    def test_03_a_lane_dispatch_without_a_step(self):
        run = self.raw(*(arg for arg in self.lane_args() if not arg.startswith("--step=")))
        self.refused(run, "--step")
        self.no_lane()

    def test_04_workdir_with_lane(self):
        run = self.raw(*self.lane_args(), "--workdir", str(self.repo))
        self.refused(run, "--workdir")
        self.no_lane()

    def test_05_the_whole_suite_is_refused_in_a_lane(self):
        self.refused(self.lane_dispatch(tests="full"), "pass --tests affected or --tests none", "run once, from the main checkout")
        self.no_lane()

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

    def test_07_a_corrupt_record_refuses_naming_the_file(self):
        self.lane_ok()
        record = self.lane_dir() / "lane.json"
        record.write_text("{broken")
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, "lane.json")

    def test_08_a_branch_or_path_in_the_way_refuses_before_anything_is_made(self):
        git(self.repo, "branch", "lane/alpha")
        self.refused(self.lane_dispatch(), "lane/alpha")
        self.assertFalse(self.worktree.exists())
        self.nothing_started()

    def test_08_an_existing_path_is_never_reused_or_touched(self):
        self.worktree.mkdir()
        (self.worktree / "keep.txt").write_text("mine\n")
        self.refused(self.lane_dispatch(), "already exists")
        self.assertEqual((self.worktree / "keep.txt").read_text(), "mine\n")
        self.assertEqual(git(self.repo, "branch", "--list", "lane/alpha"), "")
        self.nothing_started()

    def test_09_a_new_lane_needs_domain_and_touches(self):
        self.refused(self.lane_dispatch(extra=("--repo", None)), "--domain")
        self.refused(self.lane_dispatch(extra=("--repo", None, "--domain", "x")), "--touches")
        self.no_lane()

    def test_10_an_existing_lane_takes_no_declaration_flags(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        for flags in (("--domain", "other"), ("--touches", "docs"), ("--repo", str(self.repo)), ("--base", "main")):
            self.refused(self.lane_dispatch(step="s2", extra=flags), "already exists")

    def test_11_a_lane_that_is_not_open_refuses(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        path = self.lane_dir() / "lane.json"
        record = json.loads(path.read_text())
        record["state"] = "abandoned"
        path.write_text(json.dumps(record))
        self.refused(self.lane_dispatch(step="s2", extra=()), "abandoned")

    def test_12_a_lane_git_does_not_agree_with_refuses(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        git(self.worktree, "checkout", "-q", "--detach")
        run = self.lane_dispatch(step="s2", extra=())
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertNotEqual(run.stderr.strip(), "")

    def test_13_the_active_lane_limit_refuses_a_new_lane(self):
        sys.path.insert(0, str(ROOT / "lib"))
        import lanes
        limit = lanes.load_machine_config()["max_active_lanes"]
        for index in range(limit):
            self.lane_ok(name=f"l{index}", extra=("--repo", None, "--domain", f"d{index}", "--touches", f"area{index}"))
        run = self.lane_dispatch(name="extra", extra=("--repo", None, "--domain", "e", "--touches", "elsewhere"))
        self.refused(run, "lane")
        self.assertFalse((self.root / "proj-wt-extra").exists())

    def test_14_a_bad_declaration_refuses_before_anything_is_made(self):
        for touches, word in (("/abs", "is absolute"), ("../up", "not normalised"), ("a/../b", "not normalised")):
            self.refused(self.lane_dispatch(extra=("--repo", None, "--domain", "d", "--touches", touches)), word)
        self.no_lane()
        self.nothing_started()

    def test_14_a_touch_the_orchestrator_owns_refuses_before_anything_is_made(self):
        (self.repo / ".tmux-pi/lanes.json").write_text(json.dumps({"owned": ["docs"]}))
        self.refused(self.lane_dispatch(extra=("--repo", None, "--domain", "d", "--touches", "docs/d.md")), "is owned by the orchestrator")
        self.no_lane()
        self.nothing_started()

    def test_19_a_task_that_cannot_be_ruled_out_as_a_writer_refuses(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.cli("collect", item["id"])
        # a live task with no recorded tree: it might be a writer on any tree
        other = self.state / "tasks" / "coder-notree0001"
        other.mkdir()
        (other / "meta.json").write_text(json.dumps({"id": other.name, "pane": "%9999", "role": "coder"}))
        (self.state / "list-rows").write_text(f"%9999 0 {self.pane()[1]}\n")
        self.refused(self.lane_dispatch(step="s2", extra=()), "no recorded working tree")
        (self.state / "list-rows").unlink()
        # a task directory with no readable record, while a process of it runs (its run.sh): it may be writing anywhere
        (other / "meta.json").write_text("{broken")
        pane, _ = self.pane(self.worker_command(other))
        self.refused(self.lane_dispatch(step="s2", extra=()), other.name, "no readable meta.json", f"tmux kill-pane -t {pane}")
        (other / "meta.json").unlink()
        self.refused(self.lane_dispatch(step="s2", extra=()), other.name, "no readable meta.json")
        self.tmux("kill-pane", "-t", pane)
        # a pane table that cannot be read
        other.rmdir()
        (self.state / "list-fail").write_text("")
        self.refused(self.lane_dispatch(step="s2", extra=()), "tmux panes")
        (self.state / "list-fail").unlink()
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), [item["id"]])


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
        second, task2 = self.existing(step="s2")
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
        self.queued("coder-aaaaaaaaaa", "s2")
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
        self.queued("coder-aaaaaaaaaa", "s2")
        self.queued("coder-bbbbbbbbbb", "s3")
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
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, "cannot be read")


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
