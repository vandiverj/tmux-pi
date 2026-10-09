"""Lanes L3, the shared occupancy of a lane (lib/lane_lease.py, bin/tp-spawn, bin/tp-agent, bin/tp-review): the lease in the
repository's common git directory, taken as one reservation by tp-spawn for dispatch, tick, retry and review alike, and the
untagged-tree backstop across nested repositories. Repositories are temporary; tmux is the private fixture server. Races are
ordered by a held lane lock, never by sleeping: the test holds the lock, starts the process, and changes the lease inside the
lock, so the process cannot have looked before the change."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_lane_writer import LaneCase, git  # noqa: E402
import lane_lease  # noqa: E402
import lanes  # noqa: E402
import worker_process  # noqa: E402


def start_of(pid):
    return worker_process._ps()[pid][2]


class LeaseCase(LaneCase):
    def setUp(self):
        super().setUp()
        self.state2 = self.root / "session2" / ".tmux-pi"
        (self.state2 / "tasks").mkdir(parents=True)
        self.env2 = {**self.env, "TP_STATE_DIR": str(self.state2)}

    def run_in(self, env, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env, text=True, capture_output=True, timeout=90)

    def common(self):
        return Path(git(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()

    def lease(self):
        path = self.lane_dir() / "lease.json"
        return json.loads(path.read_text())["holders"] if path.exists() else []

    def holder(self, task="coder-held00001", pid=None, start=None, kind="reservation", access="write", role="coder", pane=None,
               taken="2001-01-01T00:00:00Z"):
        pid = os.getpid() if pid is None else pid
        return {"kind": kind, "access": access, "task": task, "role": role, "session": "elsewhere", "pid": pid,
                "start": start or start_of(pid), "pane": pane, "taken": taken}

    def put_lease(self, holders, raw=None):
        with lanes.lane_lock(self.common()):
            lanes.write_lane_file(self.common(), "alpha", "lease.json",
                                  raw if raw is not None else json.dumps({"schema": lane_lease.SCHEMA, "holders": holders}))

    def made_lane(self):
        """Alpha exists and nothing holds it (the stub pane of its first task is not on the server)."""
        item, task = self.lane_ok()
        self.assertEqual(self.lease(), [])
        return item, task

    def gone(self, pid):
        for _ in range(100):
            if pid not in worker_process._ps():
                return
            time.sleep(0.05)
        self.fail(f"process {pid} did not end")

    def held_then(self, holder, command, env=None):
        """Hold the lane lock, start `command`, put `holder` in the lease inside the lock, release. The command cannot have
        read the lease before the holder was there. Returns (returncode, stdout, stderr)."""
        with lanes.lane_lock(self.common()):
            proc = subprocess.Popen(command, env=env or self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            lanes.write_lane_file(self.common(), "alpha", "lease.json",
                                  json.dumps({"schema": lane_lease.SCHEMA, "holders": [holder]}))
        out, err = proc.communicate(timeout=120)
        return proc.returncode, out, err


class SharedOccupancyTests(LeaseCase):
    def test_a_started_worker_is_a_holder_in_the_common_git_directory_naming_session_task_process_and_pane(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        [holder] = self.lease()
        self.assertEqual((holder["kind"], holder["access"], holder["task"], holder["session"]), ("worker", "write", item["id"], str(self.state)))
        self.assertTrue(holder["pane"].startswith("%"))
        self.assertEqual(holder["start"], start_of(holder["pid"]))
        self.assertEqual(holder["pid"], int(self.tmux("display-message", "-p", "-t", holder["pane"], "#{pane_pid}")))

    def test_a_second_session_with_its_own_state_directory_sees_the_first_sessions_writer(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        [holder] = self.lease()
        run = self.run_in(self.env2, *self.lane_args(step="s1", extra=()))
        self.refused(run, "lane alpha already has a writer", item["id"], str(self.state))
        self.assertEqual(list(self.state2.glob("tasks/*")), [])
        self.tmux("kill-pane", "-t", holder["pane"])  # its process ends: the lease is stale, by death and not by age
        self.gone(holder["pid"])
        run = self.run_in(self.env2, *self.lane_args(step="s1", extra=()))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(list(self.state2.glob("tasks/*"))), 1)

    def test_a_second_session_reviewer_waits_for_the_writer_of_the_first(self):
        (self.state / "live-window").write_text("")
        item, _ = self.lane_ok()
        run = self.run_in(self.env2, *self.lane_args(role="code-reviewer", step="s1", extra=()))
        self.refused(run, "lane alpha already has a writer", item["id"])

    def test_a_lease_left_by_a_dead_process_is_stale_and_is_dropped(self):
        self.made_lane()
        dead = subprocess.Popen(["true"])
        dead.wait()
        self.put_lease([self.holder(pid=dead.pid, start="Mon Jan  1 00:00:00 2001")])
        item, _ = self.existing(step="s1")
        self.assertNotIn("coder-held00001", [h["task"] for h in self.lease()])

    def test_a_recycled_pid_with_another_start_time_is_not_the_holder(self):
        self.made_lane()
        self.put_lease([self.holder(start="Mon Jan  1 00:00:00 2001")])  # a live pid (this process), but not the recorded process
        self.existing(step="s1")

    def test_a_live_holder_holds_however_old_the_lease_is(self):
        self.made_lane()
        self.put_lease([self.holder(taken="2001-01-01T00:00:00Z")])
        self.refused(self.lane_dispatch(step="s1", extra=()), "coder-held00001", "already has a writer")

    def test_an_unreadable_or_invalid_lease_refuses_naming_the_file(self):
        self.made_lane()
        before = sorted(p.name for p in (self.state / "tasks").iterdir())
        for raw in ("{not json", json.dumps({"schema": "other", "holders": []}), json.dumps({"schema": lane_lease.SCHEMA, "holders": [1]}),
                    json.dumps({"schema": lane_lease.SCHEMA, "holders": [dict(self.holder(), pid="x")]})):
            self.put_lease(None, raw)
            self.refused(self.lane_dispatch(step="s1", extra=()), "lease.json")
            self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)
        lease = self.lane_dir() / "lease.json"
        lease.unlink()
        lease.symlink_to(self.root / "elsewhere.json")  # a lease that is a link is not read
        self.refused(self.lane_dispatch(step="s1", extra=()), "lease.json")
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)

    def test_a_reader_does_not_hold_another_reader_but_holds_a_writer(self):
        self.made_lane()
        self.put_lease([self.holder(task="review-other0001", access="read", role="code-reviewer")])
        self.lane_ok(role="code-reviewer", step="s1", extra=())
        self.refused(self.lane_dispatch(step="s1", extra=()), "review-other0001")


class ReservationTests(LeaseCase):
    def test_a_failed_start_releases_the_reservation(self):
        self.made_lane()
        (self.state / "window-fail").write_text("")
        run = self.lane_dispatch(step="s1", extra=())
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertEqual(self.lease(), [])
        (self.state / "window-fail").unlink()
        self.existing(step="s1")  # the lane is free again

    def test_a_start_that_fails_after_the_pane_exists_ends_the_pane_and_releases(self):
        self.made_lane()
        (self.state / "live-window").write_text("")
        (self.state / "break-after-window").write_text("")  # the pane table cannot be read when the worker is registered
        before = self.tmux("list-panes", "-a", "-F", "#{pane_id}").split()
        run = self.lane_dispatch(step="s1", extra=())
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertEqual(self.tmux("list-panes", "-a", "-F", "#{pane_id}").split(), before)  # the new pane was ended
        self.assertEqual(self.lease(), [])

    def test_a_new_lane_whose_start_fails_is_removed_again_when_nothing_holds_it(self):
        (self.state / "window-fail").write_text("")
        run = self.lane_dispatch()
        self.assertNotEqual(run.returncode, 0)
        self.no_lane()

    def test_a_reservation_by_a_process_that_died_is_stale(self):
        self.made_lane()
        child = subprocess.Popen(["sleep", "300"])
        holder = self.holder(pid=child.pid)
        child.kill()
        child.wait()
        self.put_lease([holder])
        self.existing(step="s1")

    def test_the_task_holds_the_lane_once(self):
        self.made_lane()
        lane_lease.reserve(self.common(), "alpha", "coder-once00001", "coder", "s", os.getpid())
        with self.assertRaisesRegex(ValueError, "already"):
            lane_lease.reserve(self.common(), "alpha", "coder-once00001", "coder", "s", os.getpid())
        lane_lease.release(self.common(), "alpha", "coder-once00001")
        self.assertEqual(self.lease(), [])

    def test_activating_a_pane_that_is_gone_leaves_nothing_alive(self):
        self.made_lane()
        lane_lease.reserve(self.common(), "alpha", "coder-once00001", "coder", "s", os.getpid())
        self.assertIsNone(lane_lease.activate(self.common(), "alpha", "coder-once00001", "%9999"))  # no such pane on the server
        self.assertEqual(self.lease(), [])


class EveryStartGoesThroughTheReservationTests(LeaseCase):
    """The review-versus-dispatch and tick windows: the lease changes while the process waits for the lane lock."""

    def spawn_command(self):
        return [str(ROOT / "bin/tp-spawn"), "--task-id", "coder-direct0001", "--role", "coder", "--worker", "pi", "--model", MODEL,
                "--thinking", "medium", "--tests", "none", "--workdir", str(self.worktree), "--lane", "alpha", "--lane-step", "s9",
                "--preamble-from", str(self.repo), "--", "do it"]

    def test_tp_spawn_with_a_lane_refuses_when_the_lease_has_a_writer_whoever_called_it(self):
        self.made_lane()
        code, out, err = self.held_then(self.holder(), self.spawn_command())
        self.assertNotEqual(code, 0, out)
        self.assertIn("coder-held00001", err)
        self.assertFalse((self.state / "tasks/coder-direct0001/meta.json").exists())

    def test_tp_spawn_with_a_lane_that_has_no_record_or_another_tree_starts_nothing(self):
        self.made_lane()
        for lane, workdir in (("nolane", str(self.worktree)), ("alpha", str(self.repo))):
            command = self.spawn_command()
            command[command.index("--lane") + 1] = lane
            command[command.index("--workdir") + 1] = workdir
            run = subprocess.run(command, env=self.env, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertFalse((self.state / "tasks/coder-direct0001").exists())

    def test_a_review_started_between_a_dispatchs_check_and_its_registration_is_refused(self):
        item, task = self.made_lane()
        self.settle(task)
        # a dispatch past its check holds a reservation and has not registered its task yet
        code, out, err = self.held_then(self.holder(), [str(ROOT / "bin/tp-review"), item["id"]])
        self.assertNotEqual(code, 0, out)
        self.assertIn("a reviewer must not run beside it", err)
        self.assertEqual(list(self.state.glob("tasks/review-*")), [])
        self.assertEqual(self.lease()[0]["task"], "coder-held00001")  # the refused reviewer took nothing and removed nothing

    def test_a_dispatch_started_between_a_reviews_guard_and_its_start_is_refused(self):
        self.made_lane()
        reviewer = self.holder(task="review-held00001", access="read", role="code-reviewer")
        code, out, err = self.held_then(reviewer, [str(ROOT / "bin/tp-agent"), *self.lane_args(step="s1", extra=())])
        self.assertNotEqual(code, 0, out)
        self.assertIn("review-held00001", err)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)

    def test_a_queued_item_is_not_started_by_tick_while_a_reservation_holds_the_lane(self):
        self.made_lane()
        item = {"id": "coder-aaaaaaaaaa", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s1", "preamble_from": str(self.repo)}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-aaaaaaaaaa.json").write_text(json.dumps(item))
        code, out, err = self.held_then(self.holder(), [str(ROOT / "bin/tp-agent"), "tick"])
        self.assertEqual(code, 0, err)  # tick reports; the item waits
        self.assertTrue((self.state / "queue/coder-aaaaaaaaaa.json").exists())
        self.assertFalse((self.state / "tasks/coder-aaaaaaaaaa").exists())
        self.assertEqual(json.loads(out)["started"], [])

    def test_a_retry_goes_through_the_same_reservation(self):
        item, task = self.made_lane()
        self.settle(task)
        self.cli("collect", item["id"])
        code, out, err = self.held_then(self.holder(), [str(ROOT / "bin/tp-agent"), "retry", item["id"], "--reason", "again"])
        self.assertNotEqual(code, 0, out)
        self.assertIn("coder-held00001", err)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)


class NestedCheckoutTests(LeaseCase):
    def nested(self):
        self.made_lane()
        sub = self.worktree / "vendor" / "sub"
        sub.mkdir(parents=True)
        git(sub, "init", "-q", "-b", "main")
        (sub / "deeper").mkdir()
        return sub

    def check(self, path, *extra):
        return subprocess.run([str(ROOT / "bin/tp-spawn"), "--check", "--role", "coder", "--workdir", str(path), *extra, "--", "check"],
                              env=self.env, text=True, capture_output=True, timeout=60)

    def test_a_nested_repository_inside_a_lane_tree_is_the_lanes_tree(self):
        sub = self.nested()
        for path in (sub, sub / "deeper"):
            run = self.check(path)
            self.assertNotEqual(run.returncode, 0, run.stdout)
            self.assertIn("alpha", run.stderr)
            self.refused(self.raw("dispatch", "--role", "coder", "--task", "x", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                                  "--deliverable", "Report", "--workdir", str(path)), "alpha", "--lane")

    def test_a_symlinked_path_to_the_nested_checkout_is_the_lanes_tree_too(self):
        sub = self.nested()
        link = self.root / "shortcut"
        link.symlink_to(sub)
        deep = self.root / "deep-shortcut"
        deep.symlink_to(sub / "deeper")
        for path in (link, deep):
            run = self.check(path)
            self.assertNotEqual(run.returncode, 0, run.stdout)
            self.assertIn("alpha", run.stderr)

    def test_the_lane_flags_start_in_a_nested_checkout_of_the_lane_only_through_the_lane(self):
        sub = self.nested()
        record, unrecorded = lanes.lane_containing(str(sub))
        self.assertEqual((record["name"], unrecorded), ("alpha", False))

    def test_a_repository_outside_every_lane_is_not_refused(self):
        self.nested()
        outside = self.root / "outside"
        outside.mkdir()
        git(outside, "init", "-q", "-b", "main")
        self.assertEqual(self.check(outside).returncode, 0)
        self.assertEqual(lanes.lane_containing(str(outside)), (None, False))

    def test_a_hand_made_worktree_is_still_only_a_warning(self):
        self.made_lane()
        git(self.repo, "worktree", "add", "-q", "-b", "hand", str(self.root / "hand-made"), "main")
        sub = self.root / "hand-made" / "n"
        sub.mkdir()
        git(sub, "init", "-q", "-b", "main")
        record, unrecorded = lanes.lane_containing(str(self.root / "hand-made"))
        self.assertEqual((record, unrecorded), (None, True))


if __name__ == "__main__":
    unittest.main()
