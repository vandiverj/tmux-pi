"""lib/trial_shadow.py: the steps of a run (timeout, preempt, spawn failure, records, the attempt clock) and the idempotent cleanup.
Fixtures: trial_shadow_case.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

class RunStepTests(ShadowCase):
    def test_e_a_timeout_kills_the_window_and_records_outcome_timeout(self):
        rdir, info = self.prepare(timeout_s=300)
        clock = Clock(step=100.0)
        run, done = self.run_stub(rdir, clock=clock)
        self.assertEqual((done["outcome"], done["wait_result"], done["phase"]), ("timeout", "timeout", "done"))
        self.assertFalse(self.alive(self.worker_pane), "the worker's window is killed")
        self.assertGreaterEqual(clock.now, 300, "the deadline was reached by the stepped clock, not by waiting")
        record, = [r for r in self.records() if r["arm"] == "qwen"]
        self.assertEqual((record["outcome"], record["mode"], record["pair_id"]), ("timeout", "shadow", info["pair_id"]))
        self.assertFalse([p for p in (rdir / "state" / "tasks").iterdir() if p.name.startswith("review-")], "a timed out run is not reviewed")
        self.assertEqual(done["cleanup_errors"], [])
        self.assertFalse((self.tree(rdir)).exists())
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def test_f2_a_local_run_stops_as_preempted_when_main_fills_its_slots_while_it_waits(self):
        self.local_task(self.state)  # Main holds one of its two slots when the shadow starts
        rdir, info = self.prepare(model=LOCAL, timeout_s=300)
        seen = {}

        def poll(n, run):
            seen["polls"] = n
            if n == 3:
                self.local_task(self.state)  # Main starts another local worker: both slots are held
        clock = Clock(step=10.0)
        run, done = self.run_stub(rdir, on_poll=poll, clock=clock)
        self.assertEqual((done["outcome"], done["wait_result"], done["phase"]), ("preempted", "preempted", "done"))
        self.assertEqual(seen["polls"], 3,"it stopped at the first poll that saw Main full, not at the deadline")
        self.assertLess(clock.now, 100, "the 300 s limit was never reached")
        self.assertFalse(self.alive(self.worker_pane), "the worker's window is killed")
        record, = [r for r in self.records() if r["arm"] == "qwen"]
        self.assertEqual(record["outcome"], "preempted")
        self.assertFalse([p for p in (rdir / "state" / "tasks").iterdir() if p.name.startswith("review-")], "a preempted run is not reviewed")
        self.assertEqual(done["cleanup_errors"], [])
        self.assertFalse((self.tree(rdir)).exists())
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def test_f2_main_below_its_limit_does_not_stop_the_run_and_a_worker_that_settled_is_kept(self):
        self.local_task(self.state)
        rdir, info = self.prepare(model=LOCAL)

        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.local_task(self.state)  # Main is full by the time the worker is read as settled
                self.settle_worker(run)
            elif n == 2:
                for p in (run.state / "tasks").iterdir():
                    if (p / "review-of").exists():
                        self.settle(p, "waiting", "RESULT_STATUS: complete\nAPPROVED\n")
        run, done = self.run_stub(rdir, on_poll=poll)
        self.assertEqual(done["outcome"], "completed", "a settled worker is taken whatever Main holds")

    def test_f2_a_model_that_is_not_local_never_yields_to_main(self):
        self.local_task(self.state)
        self.local_task(self.state)
        rdir, info = self.prepare(timeout_s=300)
        run, done = self.run_stub(rdir, clock=Clock(step=100.0))
        self.assertEqual(done["outcome"], "timeout")

    def test_f2_an_unreadable_count_ends_the_run_with_an_error_not_a_zero(self):
        rdir, info = self.prepare(model=LOCAL)
        with mock.patch.object(trial_session, "local_active", side_effect=trial_session.SessionError("cannot count")):
            run, done = self.run_stub(rdir)
        self.assertEqual(done["outcome"], "error")
        self.assertIn("cannot count", done["error"])
        self.assertFalse(self.alive(self.worker_pane))

    def test_f3_the_attempt_limit_includes_the_time_tp_spawn_took(self):
        rdir, info = self.prepare(timeout_s=300)
        clock = Clock(step=100.0)
        polls = []

        class SlowSpawn(StubSpawnRun):
            def spawn(self):
                clock.now += 250.0  # the worktree and tp-spawn took 250 s of the 300
                super().spawn()
        run, done = self.run_stub(rdir, clock=clock, on_poll=lambda n, r: polls.append(n), run_class=SlowSpawn)
        self.assertEqual((done["outcome"], done["wait_result"]), ("timeout", "timeout"))
        self.assertEqual(polls, [1], "the worker had the 50 s that were left: one poll, not the whole 300 again")
        self.assertLess(clock.now, 400)
        self.assertFalse(self.alive(self.worker_pane))

    def test_f3_the_limit_counts_from_prepare_not_from_the_runner_starting(self):
        rdir, info = self.prepare(timeout_s=300)
        clock = Clock(step=100.0)
        ts.update_run(rdir, created=(ts.datetime.datetime.now(ts.datetime.timezone.utc) - ts.datetime.timedelta(seconds=290)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        run = StubSpawnRun(rdir, self.env, sleep=clock.sleep, clock=clock)
        self.assertAlmostEqual(run.attempt_deadline() - clock.now, 10.0, delta=3.0, msg="290 s of the 300 had passed before the runner began")
        ts.update_run(rdir, created="not a time")
        self.assertEqual(StubSpawnRun(rdir, self.env, sleep=clock.sleep, clock=clock).attempt_deadline(), 300.0)

    def test_a_worker_that_settles_is_reviewed_and_ended_without_losing_its_pane_text(self):
        rdir, info = self.prepare()
        seen = {}

        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.settle_worker(run)
            elif n == 2:  # the review task exists now; settle it
                reviews = [p for p in (run.state / "tasks").iterdir() if (p / "review-of").exists()]
                seen["reviews"] = [p.name for p in reviews]
                for p in reviews:
                    self.settle(p, "waiting", "RESULT_STATUS: complete\nAPPROVED\n")

        run, done = self.run_stub(rdir, poll)
        self.assertEqual((done["outcome"], done["wait_result"]), ("completed", "settled"))
        self.assertEqual(done["diff_files"], 2)
        self.assertEqual(len(seen.get("reviews", [])), 1, done)
        self.assertEqual(done["review_id"], seen["reviews"][0])
        qwen, = [r for r in self.records() if r["arm"] == "qwen"]
        normal, = [r for r in self.records() if r["arm"] == "normal"]
        self.assertEqual((qwen["pair_id"], normal["pair_id"]), (info["pair_id"], info["pair_id"]))
        self.assertEqual((qwen["outcome"], normal["outcome"]), ("completed", "completed"))
        self.assertEqual(qwen["normal_reviewer_model"], "claude-sonnet-5-5")

    def test_the_run_and_its_record_say_where_the_worker_worked(self):
        rdir, info = self.prepare()
        worktree = str(self.tree(rdir))
        self.assertEqual(ts.read_run(rdir)["workdir"], worktree)
        _, done = self.run_stub(rdir, lambda n, run: self.settle_worker(run) if n == 1 else None)
        self.assertEqual(done["workdir"], worktree)
        qwen, = [r for r in self.records() if r["arm"] == "qwen"]
        self.assertEqual(qwen["workdir"], worktree)
        self.assertNotIn("workdir", [r for r in self.records() if r["arm"] == "normal"][0], "the normal arm worked in its own lane tree")

    def test_a_worker_that_fails_is_recorded_as_failed_and_not_reviewed(self):
        rdir, _ = self.prepare()

        def poll(n, run):
            (run.task_path() / "exit").write_text("1\n")
            self.tmux("kill-pane", "-t", self.worker_pane)

        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["outcome"], "worker_failed", done)
        self.assertIsNone(done.get("review_id"))
        self.assertEqual([r["outcome"] for r in self.records() if r["arm"] == "qwen"], ["worker_failed"])
        self.assertFalse((self.tree(rdir)).exists())

    def test_g_a_failed_spawn_cleans_up_the_worktree_the_session_and_the_refs(self):
        rdir, info = self.prepare()
        before = self.worktrees()
        _, done = self.run_stub(rdir, spawn_error="tp-spawn refused: nope")
        self.assertEqual(done["outcome"], "spawn_failed")
        self.assertIn("tp-spawn refused", done["error"])
        self.assertFalse((self.tree(rdir)).exists())
        self.assertEqual(self.worktrees().count("worktree "), 1)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        self.assertEqual(self.worktrees(), before)

    def test_f7_a_spawn_failure_with_no_task_still_writes_a_qwen_record_saying_so(self):
        rdir, info = self.prepare()
        _, done = self.run_stub(rdir, spawn_error="tp-spawn refused: nope")
        by_arm = {r["arm"]: r for r in self.records()}
        self.assertEqual(sorted(by_arm), ["normal", "qwen"])
        qwen = by_arm["qwen"]
        self.assertEqual((qwen["outcome"], qwen["run"], qwen["source_task"], qwen["mode"]), ("spawn_failed", info["run"], "src-1", "shadow"))
        self.assertEqual((qwen["pair_id"], qwen["model"], qwen["effort"], qwen["base_commit"]), (by_arm["normal"]["pair_id"], MODEL, "medium", self.base))
        self.assertIn("tp-spawn refused", qwen["error"])
        self.assertTrue(qwen["no_task"])
        self.assertEqual(qwen["role"], "coder")
        self.assertEqual(done["outcome"], "spawn_failed")

    def test_f7_a_spawn_that_failed_after_making_its_task_is_recorded_from_that_task(self):
        rdir, info = self.prepare()

        class Late(StubSpawnRun):
            def spawn(self):
                super().spawn()
                raise ts.ShadowError("tp-spawn refused: late")

        self.spawn_error = None
        done = Late(rdir, self.env, sleep=lambda s: None, clock=Clock()).execute()
        qwen, = [r for r in self.records() if r["arm"] == "qwen"]
        self.assertEqual((done["outcome"], qwen["outcome"]), ("spawn_failed", "spawn_failed"))
        self.assertNotIn("no_task", qwen)
        self.assertEqual(qwen["task_id"], info["task_id"])

    def test_g_an_interrupt_during_the_review_still_records_ends_the_worker_and_cleans_up(self):
        rdir, info = self.prepare()

        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.settle_worker(run)
            else:
                raise ts.Interrupted()

        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["outcome"], "stopped")
        self.assertTrue(done.get("interrupted"))
        self.assertFalse((self.tree(rdir)).exists())
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual([r["outcome"] for r in self.records() if r["arm"] == "qwen"], ["stopped"])

    def test_g_an_unexpected_error_is_a_failed_run_with_a_reason_and_still_cleans_up(self):
        rdir, info = self.prepare()

        def poll(n, run):
            raise RuntimeError("boom")

        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["outcome"], "error")
        self.assertIn("RuntimeError: boom", done["error"])
        self.assertFalse((self.tree(rdir)).exists())
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertFalse(self.alive(self.worker_pane), "the worker is ended too")

    def test_a_run_executes_once(self):
        rdir, _ = self.prepare()
        self.run_stub(rdir, spawn_error="x")
        with self.assertRaises(ts.ShadowError):
            ts.execute(rdir, self.env)

class CleanupTests(ShadowCase):
    def test_cleanup_is_idempotent_and_total(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        run.owner.keep_ref("refs/tmux-pi/shadow/{}/head".format(info["run"]), self.base)  # a ref the run made and recorded (one it did not record: BranchTests)
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse((self.tree(rdir)).exists())
        self.assertEqual(self.refs("refs/tmux-pi"), "")

    def test_cleanup_removes_a_worktree_git_no_longer_knows(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        shutil.rmtree(ts._gitfile_target(run.worktree))  # git's entry for the tree is gone; the directory (ours by identity) stays
        (self.tree(rdir) / "stale").write_text("x")
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse(self.tree(rdir).exists())

    def test_cleanup_ends_a_task_that_still_runs(self):
        rdir, info = self.prepare()
        task = rdir / "state" / "tasks" / "trial-live"
        task.mkdir()
        pane = self.session_pane(rdir, self.worker_command(task))
        self.record_panes(rdir)  # what the run does after tp-spawn
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "worker": "pi", "model": MODEL, "pane": pane, "role": "coder", "workdir": str(self.repo)}))
        (task / "state.json").write_text(json.dumps({"state": "working"}))
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse(self.alive(pane))

    def test_cleanup_never_removes_a_worktree_path_outside_the_run_directory(self):
        """run.json is a file the worker can edit: what it says about the worktree decides nothing."""
        rdir, info = self.prepare()
        outside = self.root / "precious"
        outside.mkdir()
        (outside / "keep").write_text("x")
        ts.update_run(rdir, worktree=str(outside))
        ts.cleanup(rdir, self.env)
        self.assertTrue((outside / "keep").exists())

    def other_worktree(self):
        """A second, registered, dirty worktree of the same repository: another lane's tree, which no shadow may touch."""
        other = self.root / "other-lane"
        git(self.repo, "worktree", "add", "--detach", str(other), self.base)
        (other / "lib" / "x.py").write_text("another lane's uncommitted work\n")
        return other

    def assert_other_untouched(self, other):
        self.assertEqual((other / "lib" / "x.py").read_text(), "another lane's uncommitted work\n")
        self.assertIn(str(other.resolve()), self.worktrees().replace("\\", ""), "git still has the other worktree registered")

    def test_f1_a_run_record_pointing_at_another_registered_dirty_worktree_removes_nothing(self):
        rdir, info = self.prepare()
        other = self.other_worktree()
        ts.update_run(rdir, worktree=str(other))
        ts.cleanup(rdir, self.env)
        self.assert_other_untouched(other)

    def test_f1_a_link_in_the_runs_worktree_place_to_another_worktree_removes_nothing(self):
        rdir, info = self.prepare()
        other = self.other_worktree()
        os.symlink(str(other), str(self.tree(rdir)))
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any(NOT_OURS in l for l in leftovers), leftovers)
        self.assert_other_untouched(other)

    def test_f1_a_record_that_names_another_project_or_run_moves_nothing_cleanup_does(self):
        """Project, run, state dir and session come from where the run directory is, not from run.json."""
        rdir, info = self.prepare()
        other = self.other_worktree()
        ts.update_run(rdir, project=str(other), run="sh-elsewhere", state=str(other), session="tp-trial-sh-elsewhere")
        ts.cleanup(rdir, self.env)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names(), "the run's own session, derived from its directory, was ended")
        self.assert_other_untouched(other)

    def test_f1_a_run_that_starts_from_such_a_record_does_not_touch_that_tree(self):
        rdir, info = self.prepare()
        other = self.other_worktree()
        ts.update_run(rdir, worktree=str(other))
        run, done = self.run_stub(rdir)
        self.assertEqual(done["outcome"], "error")
        self.assertIn("not this run's own place", done["error"])
        self.assert_other_untouched(other)
        self.assertEqual(git(other, "status", "--porcelain"), "M lib/x.py", "nothing was staged or reset there")


if __name__ == "__main__":
    unittest.main()
