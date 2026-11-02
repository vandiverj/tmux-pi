"""lib/trial_shadow.py: what a run owns: the worktree it made (proved before each git command and removal) and the identities it
recorded for its run dir, state dir, session and panes. Fixtures: trial_shadow_case.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

class WorktreeOwnershipTests(ShadowCase):
    """Review 3: ownership is proven by what the run itself recorded when it created the worktree, and proven again before every git
    command or removal there. A tree the run did not create, or one swapped in while it ran, is left exactly as it was."""

    def other_tree(self, at, commit=True):
        """Another lane's registered worktree (dirty, and with a commit of its own past the base) that no shadow may touch."""
        git(self.repo, "worktree", "add", "--detach", str(at), self.base)
        if commit:
            (Path(at) / "lib" / "x.py").write_text("another lane's commit\n")
            git(at, "commit", "-q", "-am", "other lane commit")
        (Path(at) / "lib" / "x.py").write_text("another lane's uncommitted work\n")
        (Path(at) / "lib" / "theirs.py").write_text("untracked\n")
        return Path(at)

    def fingerprint(self, tree):
        return (git(tree, "rev-parse", "HEAD"), git(tree, "status", "--porcelain"), (Path(tree) / "lib" / "x.py").read_text(),
                git(tree, "diff", "--cached", "--stat"), os.path.realpath(str(tree)) in self.worktrees().replace("\\", ""))

    def swapped_run(self, how):
        """A run with its own worktree made; then `how` replaces what is at the worktree place, as a worker could. Returns the run and
        the other tree."""
        rdir, _ = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        self.addCleanup(lambda: shutil.rmtree(str(run.worktree), ignore_errors=True))
        self.edit(run)
        other = self.other_tree(self.root / "other-lane")
        aside = self.root / "aside"
        if how == "link":
            shutil.move(str(run.worktree), str(aside))
            os.symlink(str(other), str(run.worktree))
        elif how == "moved-worktree":
            shutil.move(str(run.worktree), str(aside))
            git(self.repo, "worktree", "move", "-f", str(other), str(run.worktree))
            other = run.worktree
        elif how == "dotgit":
            admin = ts._gitfile_target(other)
            (run.worktree / ".git").write_text("gitdir: {}\n".format(admin))
        return run, other

    def test_f1_a_worktree_that_was_there_before_the_run_is_left_untouched_by_cleanup(self):
        rdir, info = self.prepare()
        other = self.other_tree(self.tree(rdir))  # registered, dirty, and not made by this run (the run never reached make_worktree)
        before = self.fingerprint(other)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any(NOT_OURS in l for l in leftovers), leftovers)
        self.assertEqual(self.fingerprint(other), before)

    def test_f1_stop_of_a_queued_run_leaves_a_worktree_it_did_not_create(self):
        rdir, info = self.prepare()
        other = self.other_tree(self.tree(rdir))
        before = self.fingerprint(other)
        done = ts.stop(rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(any(NOT_OURS in l for l in done["cleanup_errors"]), done["cleanup_errors"])

    def test_f1_a_run_that_finds_a_worktree_at_its_place_makes_none_and_removes_none(self):
        rdir, info = self.prepare()
        other = self.other_tree(self.tree(rdir))
        before = self.fingerprint(other)
        run, done = self.run_stub(rdir)
        self.assertEqual(done["outcome"], "error")
        self.assertFalse((rdir / "state" / "tasks" / run.task_id).exists(), "no worker was started")
        self.assertEqual(self.fingerprint(other), before)

    def test_f1_the_runs_own_worktree_is_still_removed(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        self.assertEqual(run.owner.record["path"], os.path.realpath(str(run.worktree)))
        self.assertNotIn("owned_worktree", ts.read_run(rdir), "the ownership record is not kept in run.json, which the worker can reach")
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse(run.worktree.exists())
        self.assertEqual(ts.cleanup(rdir, self.env), [], "a second cleanup finds nothing and does nothing")

    def test_f1_a_worktree_made_at_the_place_after_the_runs_own_was_removed_is_not_the_runs(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        other = self.other_tree(self.tree(rdir))  # git may reuse the admin name of the removed one
        before = self.fingerprint(other)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(any(NOT_OURS in l for l in leftovers), leftovers)

    def test_f2_a_link_swapped_in_before_the_reset_is_caught_before_any_git_command_there(self):
        run, other = self.swapped_run("link")
        before = self.fingerprint(other)
        with self.assertRaises(ts.ShadowError) as caught:
            run.collect_work()
        self.assertIn(NOT_OURS, str(caught.exception))
        self.assertEqual(self.fingerprint(other), before, "no reset, no add in the other tree")
        leftovers = ts.cleanup(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(leftovers)

    def test_f2_another_registered_worktree_moved_into_the_place_is_caught_too(self):
        run, other = self.swapped_run("moved-worktree")
        before = self.fingerprint(other)
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.assertEqual(self.fingerprint(other), before)
        ts.cleanup(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)

    def test_f2_a_dot_git_file_rewritten_to_another_tree_is_caught_too(self):
        run, other = self.swapped_run("dotgit")
        before = self.fingerprint(other)
        with self.assertRaises(ts.ShadowError):
            run.collect_work()
        self.assertEqual(self.fingerprint(other), before)

    def test_f2_the_swap_while_the_worker_runs_ends_the_run_in_error_and_leaves_the_other_tree(self):
        rdir, info = self.prepare()
        other = self.other_tree(self.root / "other-lane")
        before = self.fingerprint(other)

        def swap(n, run):
            if n == 1:
                shutil.move(str(run.worktree), str(self.root / "aside"))
                os.symlink(str(other), str(run.worktree))
                self.settle_worker(run)
        run, done = self.run_stub(rdir, on_poll=swap)
        self.assertEqual(done["outcome"], "error")
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(os.path.islink(str(self.tree(rdir))), "the link is left, not followed")

    # -- review 4: the model changed, not a site (see the report's design)

    def worker_replaces_tree(self, run):
        """What a worker can do with git alone: remove the run's tree and make another registered, detached, dirty one at the same place."""
        old_admin = ts._gitfile_target(run.worktree)
        git(self.repo, "worktree", "remove", "--force", str(run.worktree))
        other = self.other_tree(run.worktree)
        self.assertEqual(ts._gitfile_target(other), old_admin, "git reused the admin directory name: no name tells the two trees apart")
        return other

    def made_run(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        return run

    def test_r4_f1_a_tree_made_at_the_place_after_the_runs_own_was_removed_is_left_by_the_runners_cleanup(self):
        run = self.made_run()
        other = self.worker_replaces_tree(run)
        before = self.fingerprint(other)
        leftovers = run.cleanup()
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(any(NOT_OURS in l for l in leftovers), leftovers)

    def test_r4_f1_the_same_replacement_is_left_by_an_external_stop_and_cleanup(self):
        run = self.made_run()
        other = self.worker_replaces_tree(run)
        before = self.fingerprint(other)
        done = ts.stop(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(any(NOT_OURS in l for l in done["cleanup_errors"]), done["cleanup_errors"])
        ts.cleanup(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before)

    def forge_and_swap(self):
        """The worker moves another registered tree into the place and rewrites every field of run.json that could name it."""
        run, other = self.swapped_run("moved-worktree")
        real = os.path.realpath(str(run.worktree))
        ts.update_run(run.rdir, owned_worktree={"path": real, "gitdir": ts._gitfile_target(run.worktree), "heads_before": [], "created": ts.now()},
                      worktree_removed=None, worktree=str(run.worktree), project=run.info["project"])
        return run, other

    def test_r4_f2_a_forged_run_json_cannot_make_another_tree_the_runs_for_the_runner_or_for_a_stop(self):
        run, other = self.forge_and_swap()
        before = self.fingerprint(other)
        run.cleanup()
        self.assertEqual(self.fingerprint(other), before, "the runner's cleanup")
        ts.cleanup(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before, "an external cleanup")
        ts.stop(run.rdir, self.env)
        self.assertEqual(self.fingerprint(other), before, "an external stop")

    def test_r4_f2_a_state_dir_the_worker_points_at_mains_stops_none_of_mains_tasks(self):
        main_task = self.local_task(self.state)  # made first: a bare `new-window` of the fixture lands in the newest session, and a trial's is killed
        rdir, info = self.prepare()
        pane = json.loads((self.state / "tasks" / main_task / "meta.json").read_text())["pane"]
        ts.update_run(rdir, state=str(self.state))
        ts.cleanup(rdir, self.env)
        self.assertTrue(self.alive(pane), "Main's worker is still running")

    def test_r4_f3_a_state_dir_replaced_by_a_link_to_mains_stops_none_of_mains_tasks_and_the_session_still_ends(self):
        main_task = self.local_task(self.state)
        rdir, info = self.prepare()
        pane = json.loads((self.state / "tasks" / main_task / "meta.json").read_text())["pane"]
        shutil.move(str(rdir / "state"), str(self.root / "aside-state"))
        os.symlink(str(self.state), str(rdir / "state"))
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(self.alive(pane), "Main's worker is still running")
        self.assertTrue(any("not the run's own directory" in l for l in leftovers), leftovers)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names(), "the session is the backstop that needs no state dir")

    def test_r4_the_place_is_unique_to_the_attempt_and_the_record_is_outside_the_run_dir(self):
        first, _ = self.prepare(run="sh-one")
        second, _ = self.prepare(run="sh-two")
        names = [self.tree(first), self.tree(second)]
        self.assertNotEqual(names[0].name, names[1].name)
        for rdir, place in zip((first, second), names):
            self.assertRegex(place.name, r"^wt-[0-9a-f]{12}$")
            self.assertEqual(place.parent, rdir)
        run = ts.Run(first, self.env)
        run.make_worktree()
        record = ts.owner_file(run.project, run.run)
        self.assertTrue(record.is_file())
        self.assertNotIn(os.path.realpath(str(first)), os.path.realpath(str(record)), "outside the run dir (and so outside the worktree's parent)")
        self.assertEqual(json.loads(record.read_text())["path"], os.path.realpath(str(run.worktree)))

    def test_r4_a_place_that_exists_is_never_reused_even_an_empty_directory(self):
        rdir, info = self.prepare()
        place = self.tree(rdir)
        place.mkdir()
        run, done = self.run_stub(rdir)
        self.assertEqual(done["outcome"], "error")
        self.assertTrue(place.is_dir() and not list(place.iterdir()), "left exactly as found")
        self.assertFalse((rdir / "state" / "tasks" / run.task_id).exists(), "no worker was started")

    def test_r4_cleanup_does_not_prune_the_repository_and_so_leaves_other_lanes_entries(self):
        run = self.made_run()
        gone = self.root / "lane-on-an-unmounted-volume"
        git(self.repo, "worktree", "add", "--detach", str(gone), self.base)
        shutil.move(str(gone), str(self.root / "moved-off"))  # git would call this entry prunable
        entry = str(gone.resolve())
        self.assertIn(entry, self.worktrees().replace("\\", ""))
        self.assertEqual(run.cleanup(), [])
        self.assertIn(entry, self.worktrees().replace("\\", ""), "another lane's entry is not ours to prune")

    def test_r4_the_tree_is_not_removed_while_the_worker_may_still_run(self):
        run = self.made_run()
        with mock.patch.object(ts.trial_session, "end_exact", return_value=False):  # the session cannot be ended
            leftovers = run.cleanup()
        self.assertTrue(run.worktree.is_dir(), "no removal is raced against a worker that may still be running")
        self.assertTrue(any("may still be running" in l for l in leftovers), leftovers)
        self.assertEqual(run.cleanup(), [], "once the session is gone the tree goes")
        self.assertFalse(run.worktree.exists())

    def test_r4_an_unreadable_owner_record_leaves_the_tree_and_the_session(self):
        """Review 5: with no identity recorded by the run, a stop has nothing to match, so it ends nothing (a session found by name
        is not proof that it is the run's)."""
        run = self.made_run()
        ts.owner_file(run.project, run.run).write_text("{not json")
        leftovers = ts.cleanup(run.rdir, self.env)
        self.assertTrue(run.worktree.is_dir())
        self.assertTrue(any("owner record" in l for l in leftovers), leftovers)
        self.assertIn("tp-trial-" + run.run, self.session_names())
        self.assertEqual(run.cleanup(), [], "the runner itself holds what it recorded and does not read the file back")
        self.assertNotIn("tp-trial-" + run.run, self.session_names())

    def test_r4_f3_a_link_swapped_in_while_the_worker_runs_still_ends_the_worker_and_the_session(self):
        rdir, info = self.prepare()
        other = self.other_tree(self.root / "other-lane")
        before = self.fingerprint(other)

        def swap_then_fail(n, run):
            shutil.move(str(run.worktree), str(self.root / "aside"))
            os.symlink(str(other), str(run.worktree))
            raise ts.Interrupted()  # or a failed Main-slot count: any way the runner ends
        run, done = self.run_stub(rdir, on_poll=swap_then_fail)
        self.assertEqual(done["outcome"], "stopped")
        self.assertFalse(self.alive(self.worker_pane), "the worker was ended whatever the worktree proof said")
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual(self.fingerprint(other), before)
        self.assertTrue(os.path.islink(str(run.worktree)), "the link is left, not followed")
        self.assertTrue(any(NOT_OURS in l for l in done["cleanup_errors"]), done["cleanup_errors"])

    def test_r4_f3_stop_ends_the_worker_and_the_session_though_the_tree_is_no_longer_the_runs(self):
        rdir, info = self.prepare()
        run = StubSpawnRun(rdir, self.env)
        run.make_worktree()
        run.spawn()
        other = self.other_tree(self.root / "other-lane")
        before = self.fingerprint(other)
        shutil.move(str(run.worktree), str(self.root / "aside"))
        os.symlink(str(other), str(run.worktree))
        self.assertTrue(self.alive(self.worker_pane))
        done = ts.stop(rdir, self.env)
        self.assertFalse(self.alive(self.worker_pane))
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual(self.fingerprint(other), before)
        self.assertEqual(done["phase"], "done")

    def test_f3_an_attempt_spent_before_the_runner_began_starts_no_worker_and_makes_no_worktree(self):
        rdir, info = self.prepare(timeout_s=300)
        ts.update_run(rdir, created=self.ago(301))
        trees = len(self.worktrees().split("\n\n"))
        run, done = self.run_stub(rdir)
        self.assertEqual(done["outcome"], "timeout")
        self.assertIn("worker was not started", done["error"])
        self.assertFalse((rdir / "state" / "tasks" / run.task_id).exists(), "tp-spawn (here its stand-in) was never reached")
        self.assertEqual(len(self.worktrees().split("\n\n")), trees)
        self.assertNotIn("path", json.loads(ts.owner_file(info["project"], info["run"]).read_text()), "no tree was made, so none is recorded")

    def test_f3_time_spent_on_the_worktree_is_checked_again_just_before_tp_spawn(self):
        rdir, info = self.prepare(timeout_s=300)
        clock = Clock(step=100.0)
        calls = []

        class SlowTree(ts.Run):
            def make_worktree(self):
                super().make_worktree()
                clock.now += 301.0  # the worktree and the shared deps took the whole attempt

            def command(self, args, timeout=ts.CMD_TIMEOUT):
                calls.append(args)
                return subprocess.CompletedProcess(args, 1, "", "tp-spawn must not run")
        run, done = self.run_stub(rdir, clock=clock, run_class=SlowTree)
        self.assertEqual((done["outcome"], calls), ("timeout", []), "tp-spawn was not called")
        self.assertIn("before tp-spawn", done["error"])
        self.assertFalse((self.tree(rdir)).exists(), "the worktree made is cleaned up")

    def test_f3_an_attempt_with_time_left_still_spawns(self):
        rdir, info = self.prepare(timeout_s=300)
        ts.update_run(rdir, created=self.ago(250))

        def poll(n, r):  # the worker settles, then the reviewer does: a reviewer that never answers is `review_timeout`, not `completed`
            if n == 1:
                self.settle_worker(r)
            else:
                for p in (r.state / "tasks").iterdir():
                    if (p / "review-of").exists() and not (p / "state.json").exists():
                        self.settle(p, "waiting", "APPROVED\nRESULT_STATUS: complete\n## Coverage\nReviewed lib/x.py in full.\n")
        run, done = self.run_stub(rdir, on_poll=poll)
        self.assertEqual(done["outcome"], "completed")

# ---------------------------------------------------------------------------------------------------- recorded identities (review 5)

class RecordedIdentityTests(ShadowCase):
    """Review 5 (the worker runs as the same user): every kill, signal and write into a run directory first matches an identity the
    run recorded itself, never a path, a pid or a pane read from a file the worker can edit."""

    def runner(self, rdir):
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "tp-trial", "run", "--run-dir", str(rdir)], stdin=subprocess.PIPE)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def test_prepare_records_the_run_dir_the_state_dir_and_the_session_before_any_worker_exists(self):
        rdir, info = self.prepare()
        record = json.loads(ts.owner_file(info["project"], info["run"]).read_text())
        self.assertEqual((record["rdir_id"]["dev"], record["rdir_id"]["ino"]), (os.stat(rdir).st_dev, os.stat(rdir).st_ino))
        self.assertEqual(record["state_id"]["ino"], os.stat(rdir / "state").st_ino)
        found = trial_session.named(info["session"], self.env)
        self.assertEqual(record["session"], {"id": found[0], "created": found[1], "name": info["session"]})
        self.assertEqual(record["panes"], [])
        self.assertNotIn("path", record, "no tree yet")

    # -- F2: stop signals only this project's runner of this run
    def test_f2_stop_does_not_signal_another_projects_runner_of_a_run_with_the_same_name(self):
        rdir, info = self.prepare(run="sh-same")
        elsewhere = self.root / "other-project" / ".tmux-pi" / "trial" / "sh-same"
        stranger = self.runner(elsewhere)  # another project's runner: same run name, other directory
        ts.update_run(rdir, pid=stranger.pid, phase="wait")  # what a worker can write into run.json
        done = ts.stop(rdir, self.env, sleep=lambda _: stranger.poll())
        self.assertIsNone(stranger.poll(), "the other project's runner was not signalled")
        self.assertEqual(done["phase"], "done")

    def test_f2_pid_is_run_needs_the_whole_run_directory(self):
        rdir, _ = self.prepare(run="sh-whole")
        elsewhere = self.root / "other-project" / ".tmux-pi" / "trial" / "sh-whole"
        child = self.runner(elsewhere)
        self.assertTrue(ts.pid_is_run(child.pid, elsewhere))
        self.assertFalse(ts.pid_is_run(child.pid, rdir))

    # -- F3: a task's pane is the worker's to edit
    def test_f3_a_task_whose_meta_names_a_main_workers_pane_does_not_stop_it(self):
        main_task = self.local_task(self.state)  # made first: a bare new-window of the fixture lands in the newest session
        main_pane = json.loads((self.state / "tasks" / main_task / "meta.json").read_text())["pane"]
        rdir, info = self.prepare()
        self.session_pane(rdir, "sleep 300")
        self.record_panes(rdir)
        self.trial_task(rdir, "trial-evil", main_pane)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(self.alive(main_pane), "Main's worker is still running")
        self.assertTrue(any("not a pane this run recorded" in l for l in leftovers), leftovers)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names(), "the run's own session is still ended")

    def test_f3_the_runner_does_not_stop_a_main_worker_either_when_the_worker_edits_its_meta(self):
        main_task = self.local_task(self.state)
        main_pane = json.loads((self.state / "tasks" / main_task / "meta.json").read_text())["pane"]
        rdir, info = self.prepare()

        def redirect_then_settle(n, run):
            meta = run.task_path() / "meta.json"
            data = json.loads(meta.read_text())
            data["pane"] = main_pane
            meta.write_text(json.dumps(data))
            self.settle_worker(run)
        run, done = self.run_stub(rdir, on_poll=redirect_then_settle)
        self.assertTrue(self.alive(main_pane), "Main's worker is still running")
        self.assertEqual(done["outcome"], "error")
        self.assertIn("not a pane this run recorded", done["error"])
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def test_f3_a_pane_in_the_runs_session_that_it_did_not_record_is_not_stopped_by_name_but_dies_with_the_session(self):
        rdir, info = self.prepare()
        pane = self.session_pane(rdir, "sleep 300")  # joined later: never recorded
        self.trial_task(rdir, "trial-late", pane)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any("not a pane this run recorded" in l for l in leftovers), leftovers)
        self.assertFalse(self.alive(pane), "the session kill takes it")

    def test_f3_a_recorded_pane_that_was_moved_to_another_session_is_not_stopped(self):
        rdir, info, pane = self.live_run_with_task("sh-moved")
        self.tmux("new-session", "-d", "-s", "elsewhere", "sleep 300")
        self.tmux("break-pane", "-d", "-s", pane, "-t", "elsewhere:")
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any("not this run's" in l for l in leftovers), leftovers)
        self.assertTrue(self.alive(pane), "a pane that left the run's session is no longer the run's to end")

    def test_f3_a_recorded_pane_in_the_runs_session_is_stopped(self):
        rdir, info, pane = self.live_run_with_task("sh-own")
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse(self.alive(pane))

    # -- review 6 F1: every file `tp-agent stop` reads from the task dir is the worker's to write; process.json names what is signalled

    def pane_dies(self, pane):
        """Wait for the event: tmux marks the pane dead (the pane stays, remain-on-exit)."""
        for _ in range(600):
            if not self.alive(pane):
                return
            subprocess.run(["perl", "-e", "select(undef,undef,undef,0.05)"])
        self.fail("pane {} did not die".format(pane))

    def test_g1_a_process_record_naming_a_stranger_is_not_signalled_when_the_pane_is_gone(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-pj-gone")
        self.tmux("kill-pane", "-t", pane)
        task = rdir / "state" / "tasks" / "trial-live"
        self.plant(task, pane, child, start)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError) as caught:
            run.agent("stop", "trial-live")
        self.assertIn("process.json", str(caught.exception))
        self.assertIsNone(child.poll(), "the stranger was not signalled")
        self.assertFalse((task / "stopped.json").exists(), "tp-agent stop did not run")

    def test_g1_a_process_record_naming_a_stranger_is_not_signalled_when_meta_names_no_pane(self):
        child, start = self.stranger()
        rdir, info = self.prepare(run="sh-pj-nopane")
        self.session_pane(rdir, "sleep 300")
        self.record_panes(rdir)
        task = self.trial_task(rdir, "trial-nopane", "")
        self.plant(task, "%0", child, start)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-nopane")
        self.assertIsNone(child.poll())
        self.assertFalse((task / "stopped.json").exists())

    def test_g1_a_process_record_naming_a_stranger_under_a_recorded_panes_name_is_refused(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-pj-pid")
        task = rdir / "state" / "tasks" / "trial-live"
        self.plant(task, pane, child, start)  # the run's pane, someone else's pid
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertIsNone(child.poll())
        self.assertTrue(self.alive(pane), "nothing was ended")

    def test_g1_a_process_record_naming_a_pane_the_run_did_not_record_is_refused(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-pj-pane")
        other, _ = self.pane("sleep 300")  # a pane outside the run's session
        task = rdir / "state" / "tasks" / "trial-live"
        self.plant(task, other, child, start)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertIsNone(child.poll())
        self.assertTrue(self.alive(other))

    def test_g1_a_process_record_that_is_not_a_plain_file_is_refused(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-link")
        task = rdir / "state" / "tasks" / "trial-live"
        os.symlink(str(self.root / "nowhere"), str(task / "process.json"))
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertTrue(self.alive(pane))

    def test_g1_a_recorded_pids_start_time_that_no_longer_matches_is_refused(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-reuse")
        run = ts.Run(rdir, self.env)
        pid = run.owner.record["pane_procs"][pane]["pid"]
        run.owner.record["pane_procs"][pane]["start"] = "Mon Jan  1 00:00:00 2001"  # the pid now runs something that started later
        (rdir / "state" / "tasks" / "trial-live" / "process.json").write_text(json.dumps(
            {"pane": pane, "pid": pid, "pgid": os.getpgid(pid), "group": os.getpgid(pid) == pid, "members": [[pid, "x"]], "recorded": "2026-10-31T10:00:00+00:00"}))
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertTrue(self.alive(pane))

    def test_g1_cleanup_leaves_a_stranger_alone_reports_the_task_and_still_ends_the_session(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-pj-clean")
        self.tmux("kill-pane", "-t", pane)
        self.plant(rdir / "state" / "tasks" / "trial-live", pane, child, start)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any("trial-live" in l and "process.json" in l for l in leftovers), leftovers)
        self.assertIsNone(child.poll(), "the stranger was not signalled")
        self.assertNotIn("tp-trial-" + info["run"], self.session_names(), "the session kill still takes the run's own panes")

    def test_g1_a_record_that_matches_what_the_run_recorded_is_stopped(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-own")
        run = ts.Run(rdir, self.env)
        ident = run.owner.record["pane_procs"][pane]
        (rdir / "state" / "tasks" / "trial-live" / "process.json").write_text(json.dumps(
            {"pane": pane, "pid": ident["pid"], "pgid": ident["pgid"], "group": ident["pgid"] == ident["pid"],
             "members": [[ident["pid"], ident["start"]]], "recorded": "2026-10-31T10:00:00+00:00"}))
        done = run.agent("stop", "trial-live")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.alive(pane))

    def test_g1_end_author_needs_no_process_record(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-ea")
        run = ts.Run(rdir, self.env)
        self.assertEqual(run.owner.check_stop(rdir / "state" / "tasks" / "trial-live", self.env, "end-author"), None)

    # -- review 6 F1: what the run records about a pane is its process, taken while the pane is alive
    def test_g1_a_recorded_pane_carries_its_pid_group_and_start_time(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-rec")
        pid = int(self.tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
        start = " ".join(subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], text=True, capture_output=True).stdout.split())
        record = json.loads(ts.owner_file(info["project"], info["run"]).read_text())
        self.assertEqual(record["pane_procs"][pane], {"pid": pid, "pgid": os.getpgid(pid), "start": start})

    def test_g1_a_pane_that_is_already_dead_when_recorded_has_no_process_identity(self):
        rdir, info = self.prepare(run="sh-pj-dead")
        pane = self.session_pane(rdir, "sleep 300")
        os.kill(int(self.tmux("display-message", "-p", "-t", pane, "#{pane_pid}")), 9)
        self.pane_dies(pane)
        self.record_panes(rdir)
        record = json.loads(ts.owner_file(info["project"], info["run"]).read_text())
        self.assertIn(pane, record["panes"])
        self.assertNotIn(pane, record["pane_procs"], "a dead pane's pid may already be someone else's")

    def test_g1_an_owner_record_with_a_malformed_process_identity_is_not_used(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-bad")
        owner = ts.owner_file(info["project"], info["run"])
        record = json.loads(owner.read_text())
        record["pane_procs"][pane]["pid"] = "1"
        owner.write_text(json.dumps(record))
        self.assertTrue(any("does not describe this run" in l for l in ts.cleanup(rdir, self.env)))

    # -- review 6 N2: prepare that fails after it made the session leaves nothing behind
    def test_n2_a_prepare_that_fails_after_the_session_was_made_ends_it_and_removes_the_run_dir(self):
        with mock.patch.object(ts.Owner, "begin", side_effect=ts.ShadowError("begin failed")):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-n2", model=MODEL, thinking="medium", env=self.env)
        self.assertNotIn("tp-trial-sh-n2", self.session_names())
        self.assertFalse(trial_session.run_dir(self.repo, "sh-n2").exists())
        self.assertFalse(ts.owner_file(str(self.repo.resolve()), "sh-n2").exists())

    def test_n2_a_session_that_was_there_before_prepare_is_not_ended_by_its_failure(self):
        state = trial_session.state_dir(self.repo.resolve(), "sh-n2b")
        state.mkdir(parents=True)
        trial_session.ensure(self.repo.resolve(), "sh-n2b", self.env)
        shutil.rmtree(str(trial_session.run_dir(self.repo.resolve(), "sh-n2b")))  # a stale session, its directory gone
        with mock.patch.object(ts.Owner, "begin", side_effect=ts.ShadowError("begin failed")):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-n2b", model=MODEL, thinking="medium", env=self.env)
        self.assertIn("tp-trial-sh-n2b", self.session_names(), "not made by this prepare")
        self.assertFalse(trial_session.run_dir(self.repo, "sh-n2b").exists())

    # -- F4: a run directory that is (or became) a link
    def test_f4_a_run_dir_replaced_by_a_link_to_another_run_stops_none_of_that_runs_tasks_and_changes_nothing_there(self):
        other, other_info, other_pane = self.live_run_with_task("sh-other")
        rdir, info = self.prepare(run="sh-mine")
        before = (other / "run.json").read_bytes()
        shutil.move(str(rdir), str(self.root / "aside-run"))
        os.symlink(str(other), str(rdir))
        with self.assertRaises(ts.ShadowError) as caught:
            ts.stop(rdir, self.env)
        self.assertIn("not the run's own directory", str(caught.exception))
        self.assertTrue(self.alive(other_pane), "the other run's worker is still running")
        self.assertIn("tp-trial-sh-other", self.session_names())
        self.assertEqual((other / "run.json").read_bytes(), before, "nothing was written through the link")
        self.assertNotIn("tp-trial-sh-mine", self.session_names(), "the run's own session goes by its recorded id")

    def test_f4_a_run_dir_replaced_by_another_directory_is_refused_the_same_way(self):
        rdir, info = self.prepare()
        shutil.move(str(rdir), str(self.root / "aside-run"))
        rdir.mkdir()
        (rdir / "run.json").write_text(json.dumps({"phase": "wait", "pid": os.getpid()}))
        with self.assertRaises(ts.ShadowError):
            ts.stop(rdir, self.env)
        self.assertEqual(json.loads((rdir / "run.json").read_text()), {"phase": "wait", "pid": os.getpid()}, "the stand-in directory is not written")
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def test_f4_the_runner_holds_its_run_dir_so_a_rename_and_a_link_redirect_nothing(self):
        other, other_info, other_pane = self.live_run_with_task("sh-other")
        rdir, info = self.prepare(run="sh-mine")
        before = (other / "run.json").read_bytes()
        aside = self.root / "aside-run"

        def swap(n, run):
            os.rename(str(rdir), str(aside))
            os.symlink(str(other), str(rdir))
        run, done = self.run_stub(rdir, on_poll=swap)
        self.assertEqual(done["outcome"], "error")
        self.assertTrue(self.alive(other_pane), "the other run's worker is still running")
        self.assertEqual((other / "run.json").read_bytes(), before, "nothing was written into the other run")
        self.assertEqual(json.loads((aside / "run.json").read_text())["outcome"], "error", "the run's own run.json, held open, got the outcome")
        self.assertNotIn("tp-trial-sh-mine", self.session_names())
        self.assertIn("tp-trial-sh-other", self.session_names())

    # -- F5: the session is ended by the id recorded
    def replacement_session(self, run, set_state):
        name = "tp-trial-" + run
        self.tmux("kill-session", "-t", "=" + name)
        self.tmux("new-session", "-d", "-s", name, "sleep 300")
        if set_state:
            self.tmux("set-option", "-t", name, "@tp_state_dir", set_state)
        return name

    def forge_created(self, info, created="1"):
        """What a worker could write into the owner file: the run's session id with a created time that is not tmux's."""
        owner = ts.owner_file(info["project"], info["run"])
        record = json.loads(owner.read_text())
        record["session"]["created"] = created
        owner.write_text(json.dumps(record))

    def test_f5_a_session_that_took_the_name_after_the_runs_own_ended_is_not_killed(self):
        rdir, info = self.prepare()
        name = self.replacement_session(info["run"], None)
        self.assertEqual(ts.cleanup(rdir, self.env), [], "the run's own session is gone; nothing of the run is left to end")
        self.assertIn(name, self.session_names(), "and the stranger on the name is left alone")

    def test_f5_nor_one_that_copies_the_state_dir_option(self):
        rdir, info = self.prepare()
        name = self.replacement_session(info["run"], info["state"])
        ts.cleanup(rdir, self.env)
        self.assertIn(name, self.session_names(), "name and state dir are not the identity; the id is")

    def test_f5_the_id_with_another_created_time_is_not_the_session_the_run_made(self):
        rdir, info = self.prepare()
        self.forge_created(info)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertIn("tp-trial-" + info["run"], self.session_names(), "the id matched but not the created time: not killed")
        self.assertTrue(any("not the session this run created" in l for l in leftovers), leftovers)

    def test_f5_the_runs_own_session_is_ended_by_its_id(self):
        rdir, info = self.prepare()
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual(ts.cleanup(rdir, self.env), [], "and a session that is already gone is fine")

    def test_f5_a_session_the_worker_renamed_is_still_the_runs_and_is_ended(self):
        rdir, info = self.prepare()
        self.tmux("rename-session", "-t", "=tp-trial-" + info["run"], "renamed-by-worker")
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertNotIn("renamed-by-worker", self.session_names(), "the id and created time are the identity, not the name")

    def test_f5_a_stranger_on_the_name_does_not_keep_the_tree_and_is_not_killed(self):
        run = self.made_run()
        name = self.replacement_session(run.run, None)
        leftovers = run.cleanup()
        self.assertEqual(leftovers, [])
        self.assertFalse(run.worktree.exists(), "the run's own session is gone, so its worker is too; the tree goes")
        self.assertIn(name, self.session_names())

    def test_f5_a_forged_session_record_keeps_the_tree_and_kills_nothing(self):
        run = self.made_run()
        self.forge_created({"project": run.owner.project, "run": run.run})
        leftovers = ts.cleanup(run.rdir, self.env)
        self.assertIn("tp-trial-" + run.run, self.session_names())
        self.assertTrue(any("not the session this run created" in l for l in leftovers), leftovers)
        self.assertTrue(run.worktree.exists(), "the session is not shown to be gone, so a worker may be in the tree")

    def made_run(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        return run


if __name__ == "__main__":
    unittest.main()
