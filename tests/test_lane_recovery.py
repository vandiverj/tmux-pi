"""Lanes L3, getting out of every stuck state (bin/tp-spawn, bin/tp-agent, bin/tp-review, lib/lane_lease.py,
lib/worker_process.py, agent/extensions/escalate.ts is in tests/escalate_runtime.mjs): a refusal never leaves a lane or a
session stuck, and each one names the command that clears it. Repositories are temporary; tmux is the private fixture server
of tests/lane_fixture.py. A starter is killed with SIGKILL in a fixed interval (the fixture's tmux stub stops there), never
after a sleep."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_lane_lease import LeaseCase  # noqa: E402
import worker_process  # noqa: E402


class RecoveryCase(LeaseCase):
    def queue(self, ident, step="s2"):
        item = {"id": ident, "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": step, "preamble_from": str(self.repo)}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue" / f"{ident}.json").write_text(json.dumps(item))

    def plain_dispatch(self):
        return self.raw("dispatch", "--role", "coder", "--task", "Make a focused change", "--worker", "pi", "--model", MODEL,
                        "--thinking", "medium", "--deliverable", "Report", "--workdir", str(self.project))

    def tasks(self, state=None):
        return sorted(p.name for p in ((state or self.state) / "tasks").iterdir())

    def kill_in_interval(self, switch):
        """Start a lane dispatch whose tp-spawn stops at `switch` in the fixture's tmux stub (the pane exists), then SIGKILL the
        whole process group: no trap runs. Returns the id of the task that was being started."""
        (self.state / "live-window").write_text("")
        (self.state / switch).write_text("")
        proc = subprocess.Popen([str(ROOT / "bin/tp-agent"), *self.lane_args()], env=self.env, text=True, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(600):
            if (self.state / "hung").exists():
                break
            time.sleep(0.1)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
            self.fail("the starter never reached the interval")
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=30)
        (self.state / switch).unlink()
        (self.state / "live-window").unlink()
        [task] = self.tasks()
        return task


class StuckSessionTests(RecoveryCase):
    def test_a_failed_non_lane_spawn_leaves_nothing_and_a_lane_dispatch_then_a_queued_tick_go_on(self):
        (self.state / "window-fail").write_text("")
        run = self.plain_dispatch()
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(self.tasks(), [])  # the task directory it made is gone: nothing for any later scan to trip over
        (self.state / "window-fail").unlink()
        self.lane_ok()
        self.queue("coder-aaaaaaaaaa")
        out = self.cli("tick")
        self.assertEqual(out["started"], ["coder-aaaaaaaaaa"], out)
        self.assertIn("coder-aaaaaaaaaa", self.tasks())

    def test_a_task_directory_with_no_record_and_no_process_is_warned_about_and_left_not_a_holder(self):
        orphan = self.state / "tasks" / "coder-orphan0001"  # what a spawn killed after mkdir leaves
        orphan.mkdir()
        run = self.lane_dispatch()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("coder-orphan0001", run.stderr)
        self.assertIn("rm -r", run.stderr)
        self.assertNotIn("--force-alive", run.stderr)
        self.assertTrue(orphan.is_dir())  # nothing is deleted behind the owner's back
        self.queue("coder-aaaaaaaaaa")
        out = self.cli("tick")
        self.assertEqual(out["started"], ["coder-aaaaaaaaaa"], out)
        self.assertTrue(orphan.is_dir())
        orphan.rmdir()  # the command the warning names clears it for good
        run = self.lane_dispatch(step="s3", extra=())
        self.assertNotIn("coder-orphan0001", run.stderr)

    def test_a_task_directory_with_no_record_but_a_running_process_refuses_naming_the_command_that_ends_it(self):
        self.lane_ok()
        orphan = self.state / "tasks" / "coder-orphan0002"
        orphan.mkdir()
        pane, _ = self.pane(self.worker_command(orphan))
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, "coder-orphan0002", "no readable meta.json", f"tmux kill-pane -t {pane}")
        self.assertNotIn("--force-alive", run.stderr)
        self.queue("coder-aaaaaaaaaa")
        out = self.cli("tick")  # tick waits: the item stays queued, nothing starts
        self.assertEqual(out["started"], [])
        self.assertTrue((self.state / "queue/coder-aaaaaaaaaa.json").exists())
        self.tmux("kill-pane", "-t", pane)  # the named command
        for _ in range(100):
            if not worker_process.task_processes(orphan):
                break
            time.sleep(0.05)
        out = self.cli("tick")  # the orphan only warns now, and the queued item starts
        self.assertEqual(out["started"], ["coder-aaaaaaaaaa"], out)

    def test_no_dispatch_refusal_offers_a_flag_dispatch_does_not_have(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        for run in (self.lane_dispatch(step="s2", extra=()), self.lane_dispatch(tests="full"),
                    self.lane_dispatch(role="code-reviewer", step="s2", extra=()), self.lane_dispatch(name="beta", step="s1")):
            self.assertNotIn("--force-alive", run.stderr)
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, item["id"], f"tp-agent stop {item['id']}")


class KilledStarterTests(RecoveryCase):
    def check_second_session_is_kept_out_until_the_pane_ends(self, task, holder_kind):
        [holder] = self.lease()
        self.assertEqual((holder["task"], holder["kind"]), (task, holder_kind))
        pids = worker_process.task_processes(self.state / "tasks" / task)
        self.assertTrue(pids, "the pane should still run")
        for run in (self.run_in(self.env2, *self.lane_args(step="s2", extra=())),
                    self.run_in(self.env2, *self.lane_args(role="code-reviewer", step="s2", extra=()))):
            self.refused(run, "lane alpha", task, str(self.state), "to end it:")
        self.assertEqual(self.tasks(self.state2), [])
        # the first session cannot start beside it either, and a task directory with no record is no excuse to force
        run = self.lane_dispatch(step="s2", extra=())
        self.refused(run, task)
        self.assertNotIn("--force-alive", run.stderr)
        return holder

    def test_a_starter_killed_after_the_pane_exists_keeps_the_lane_for_a_second_session(self):
        task = self.kill_in_interval("hang-after-window")
        self.assertFalse((self.state / "tasks" / task / "meta.json").exists())  # nothing registered it
        self.check_second_session_is_kept_out_until_the_pane_ends(task, "reservation")
        pane = self.tmux("list-panes", "-a", "-F", "#{pane_id} #{window_name}").splitlines()[-1].split()[0]
        run = self.lane_dispatch(step="s2", extra=())
        self.assertIn(f"tmux kill-pane -t {pane}", run.stderr)  # the message names the command that clears it
        self.tmux("kill-pane", "-t", pane)
        for _ in range(100):
            if not worker_process.task_processes(self.state / "tasks" / task):
                break
            time.sleep(0.05)
        run = self.run_in(self.env2, *self.lane_args(step="s2", extra=()))  # the reservation is stale now: its process is gone
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(self.tasks(self.state2)), 1)

    def test_a_starter_killed_after_the_reservation_became_the_pane_keeps_the_lane_too(self):
        task = self.kill_in_interval("hang-after-activate")
        self.assertFalse((self.state / "tasks" / task / "meta.json").exists())
        holder = self.check_second_session_is_kept_out_until_the_pane_ends(task, "worker")
        self.tmux("kill-pane", "-t", holder["pane"])
        self.gone(holder["pid"])
        run = self.run_in(self.env2, *self.lane_args(step="s2", extra=()))
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_a_pane_that_cannot_be_ended_keeps_the_holder_and_the_task_directory(self):
        (self.state / "live-window").write_text("")
        (self.state / "break-after-window").write_text("")  # activation cannot read the pane table: the start fails
        (self.state / "kill-fail").write_text("")  # and the pane cannot be ended
        run = self.lane_dispatch()
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("could not be ended", run.stderr)
        [task] = self.tasks()  # the directory stays: the process runs from it
        [holder] = self.lease()
        self.assertEqual(holder["task"], task)
        pane = [part for part in run.stderr.split() if part.startswith("%")][0]
        self.assertIn(f"tmux kill-pane -t {pane}", run.stderr)
        for switch in ("list-fail", "kill-fail", "break-after-window", "live-window"):
            (self.state / switch).unlink()
        run2 = self.run_in(self.env2, *self.lane_args(step="s2", extra=()))
        self.refused(run2, task, f"tmux kill-pane -t {pane}")
        self.tmux("kill-pane", "-t", pane)  # the named command
        for _ in range(100):
            if not worker_process.task_processes(self.state / "tasks" / task):
                break
            time.sleep(0.05)
        run2 = self.run_in(self.env2, *self.lane_args(step="s2", extra=()))
        self.assertEqual(run2.returncode, 0, run2.stderr)
        run = self.lane_dispatch(step="s3", extra=())  # and the leftover directory only warns now
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("--force-alive", run.stderr)


class HandOverAndPreambleTests(RecoveryCase):
    def spawn(self, *extra, env=None, preamble=False, task="coder-next00001"):
        command = [str(ROOT / "bin/tp-spawn"), "--task-id", task, "--role", "coder", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                   "--tests", "none", "--workdir", str(self.worktree), "--lane", "alpha", "--lane-step", "s1", *extra]
        if preamble:
            command += ["--preamble-from", str(self.repo)]
        return subprocess.run(command + ["--", "do it"], env=env or self.env, text=True, capture_output=True, timeout=90)

    def test_a_lane_worker_hands_its_work_to_its_successor_through_the_same_reservation(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        run = self.spawn()  # a successor with no hand-over is a second writer
        self.refused(run, item["id"])
        env = {**self.env, "TP_TASK_ID": item["id"]}
        run = self.spawn("--lane-replaces", item["id"], env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sorted(h["task"] for h in self.lease()), sorted([item["id"], "coder-next00001"]))
        # the predecessor stays in the lease until it is gone, and only it is let by
        run = self.spawn("--lane-replaces", item["id"], env=env, task="coder-third0001")
        self.refused(run, "coder-next00001")

    def test_a_hand_over_is_only_for_the_caller_itself_and_only_with_a_lane(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        for env in ({**self.env, "TP_TASK_ID": "coder-someoneelse"}, self.env):
            run = self.spawn("--lane-replaces", item["id"], env=env)
            self.refused(run, "--lane-replaces")
        command = [str(ROOT / "bin/tp-spawn"), "--task-id", "coder-next00002", "--role", "coder", "--worker", "pi", "--model", MODEL,
                   "--tests", "none", "--workdir", str(self.project), "--lane-replaces", item["id"], "--", "do it"]
        run = subprocess.run(command, env={**self.env, "TP_TASK_ID": item["id"]}, text=True, capture_output=True, timeout=60)
        self.refused(run, "--lane-replaces")

    def test_a_lane_tagged_start_without_a_preamble_source_gets_the_main_trees_preamble_and_its_review_too(self):
        self.lane_ok()
        for task in list(self.state.glob("tasks/*")):
            self.settle(task)
        self.cli("collect", task.name)
        run = self.spawn(task="coder-direct0001")
        self.assertEqual(run.returncode, 0, run.stderr)
        direct = self.state / "tasks/coder-direct0001"
        self.assertIn("PREAMBLE-OF-THE-MAIN-TREE", (direct / "system-prompt.md").read_text())
        meta = json.loads((direct / "meta.json").read_text())
        self.assertEqual(os.path.realpath(meta["preamble_from"]), os.path.realpath(self.repo))
        self.settle(direct)
        run = subprocess.run([str(ROOT / "bin/tp-review"), "coder-direct0001"], env=self.env, text=True, capture_output=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        [review] = self.state.glob("tasks/review-*")
        self.assertIn("PREAMBLE-OF-THE-MAIN-TREE", (review / "system-prompt.md").read_text())


class ReviewOverrideTests(RecoveryCase):
    def review(self, item, *args):
        return subprocess.run([str(ROOT / "bin/tp-review"), *args, item["id"]], env=self.env, text=True, capture_output=True, timeout=90)

    def test_force_alive_does_not_put_a_reviewer_beside_a_live_lane_writer_and_the_refusal_says_what_works(self):
        (self.state / "live-window").write_text("")
        item, task = self.lane_ok()
        self.settle(task)
        run = self.review(item, "--force-alive")  # skips ending the author, but not the lane's lease
        self.refused(run, item["id"], "--force-alive", f"tp-agent stop {item['id']}")
        self.assertEqual(sorted(p.name for p in self.state.glob("tasks/review-*")), [])
        help_text = subprocess.run([str(ROOT / "bin/tp-review"), "--help"], env=self.env, text=True, capture_output=True).stderr
        self.assertIn("does not apply to a lane", help_text)
        run = self.review(item)  # the working way: without the flag the settled author is ended first, then the review starts
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(list(self.state.glob("tasks/review-*"))), 1)

    def test_a_different_live_writer_is_ended_with_the_command_the_refusal_names(self):
        item, task = self.lane_ok()
        self.settle(task)
        other, other_task = self.lane_ok(step="s2", extra=())
        pane, _ = self.pane(self.worker_command(other_task))
        self.attach(other_task, pane)
        run = self.review(item)
        self.refused(run, other["id"], f"tp-agent stop {other['id']}")
        self.assertNotIn("--force-alive does not", run.stdout)
        self.settle(other_task)
        self.cli("stop", other["id"])  # the named command
        for _ in range(100):
            if not worker_process.task_processes(other_task):
                break
            time.sleep(0.05)
        self.assertEqual(self.review(item).returncode, 0)


if __name__ == "__main__":
    unittest.main()
