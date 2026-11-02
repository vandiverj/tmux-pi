"""lib/trial_shadow.py and lib/trial_session.py: the one gate every tp-* command of a trial goes through, and what the worker can
write that holds or steers the runner. Fixtures: trial_shadow_case.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

class GateTests(ShadowCase):
    """Review 7 F1: every tp-* command the trial starts goes through one gate (`trial_session.start_tp`, its table `TP`). A command
    that can end anything, directly or through what it runs (tp-agent stop, end-author, review; tp-spawn), runs only after the
    run's own check (`Owner.check_stop`) passes; a command, subcommand, flag or argument the table does not name is never started."""

    def outside(self, name="outside"):
        """A live pane in a session of its own that is not the run's (Main's worker, say)."""
        pane = self.tmux("new-session", "-d", "-s", name, "-P", "-F", "#{pane_id}", "sleep 300")
        self.tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        return pane

    def forge_pane(self, task, pane):
        meta = json.loads((task / "meta.json").read_text())
        meta["pane"] = pane
        (task / "meta.json").write_text(json.dumps(meta))

    def settled_task(self, run):
        rdir, info, pane = self.live_run_with_task(run)
        task = rdir / "state" / "tasks" / "trial-live"
        self.settle(task, "waiting", "RESULT_STATUS: complete\nDone.\n")
        return rdir, info, pane, task

    # -- the reviewer's case: review runs end-author of the pane meta.json names
    def test_f1_a_review_does_not_end_a_pane_outside_the_run_that_meta_names_after_the_worker_ended(self):
        outside = self.outside()
        rdir, info = self.prepare()
        case = self

        class Forging(StubSpawnRun):
            def collect_work(self):
                super().collect_work()
                case.forge_pane(self.task_path(), outside)  # after end_worker, before review: the worker's last word

        def poll(n, run):
            if n == 1:
                self.settle_worker(run)
        run, done = self.run_stub(rdir, poll, run_class=Forging)
        self.assertTrue(self.alive(outside), "the pane outside the run was not ended")
        self.assertIn("not a pane this run recorded", done.get("review_error") or "", done)
        self.assertIsNone(done.get("review_id"))
        self.assertEqual([p.name for p in (rdir / "state" / "tasks").iterdir() if (p / "review-of").exists()], [], "no review started")

    def test_f1_end_author_of_a_task_whose_meta_names_a_pane_outside_the_run_is_refused(self):
        outside = self.outside()
        rdir, info, pane, task = self.settled_task("sh-g-ea")
        self.forge_pane(task, outside)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("end-author", "trial-live")
        self.assertTrue(self.alive(outside))
        self.assertFalse((task / "reaped.json").exists(), "tp-agent end-author did not run")

    def test_f1_review_of_a_task_whose_meta_names_a_pane_outside_the_run_is_refused(self):
        outside = self.outside()
        rdir, info, pane, task = self.settled_task("sh-g-rv")
        self.forge_pane(task, outside)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("review", "--force", "trial-live")
        self.assertTrue(self.alive(outside))

    def test_f1_stop_through_agent_is_checked_as_well(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-g-st")
        self.plant(rdir / "state" / "tasks" / "trial-live", pane, child, start)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertIsNone(child.poll(), "the stranger was not signalled")

    def test_probe_a_meta_json_python_cannot_read_but_jq_can_is_refused(self):
        # tp-kill takes the pane with jq, which reads a stream of values and skips a byte-order mark; the check read the file
        # with Python, which refuses both, and took that as "no pane"
        for n, forged in enumerate(('{{"pane": "{}"}} {{}}', '\ufeff{{"pane": "{}"}}', '{{"pane": "{}"}}\n{{"pane": ""}}')):
            with self.subTest(forged=forged):
                outside = self.outside("outside-{}".format(n))
                rdir, info, pane = self.live_run_with_task("sh-g-jq{}".format(n))
                task = rdir / "state" / "tasks" / "trial-live"
                meta = (task / "meta.json").read_text()
                (task / "meta.json").write_text(forged.format(outside), encoding="utf-8")
                self.assertEqual(subprocess.run(["jq", "-r", ".pane // empty", str(task / "meta.json")], text=True, capture_output=True).stdout.split()[:1], [outside])
                run = ts.Run(rdir, self.env)
                with self.assertRaises(ts.ShadowError) as caught:
                    run.agent("stop", "trial-live")
                self.assertIn("meta.json", str(caught.exception))
                self.assertTrue(self.alive(outside), "the pane outside the run was not ended")
                self.assertFalse((task / "stopped.json").exists(), "tp-agent stop did not run")
                (task / "meta.json").write_text(meta)
                with self.assertRaises(ts.ShadowError):  # end-author and review read the same file: refused as well
                    (task / "meta.json").write_text(forged.format(outside), encoding="utf-8")
                    run.agent("end-author", "trial-live")
                self.assertTrue(self.alive(outside))

    def test_probe_a_meta_json_that_is_a_fifo_is_refused_without_blocking_the_runner(self):
        # a read of a FIFO waits for a writer: the runner would hang in the check and the attempt clock never end the worker
        rdir, info, pane = self.live_run_with_task("sh-g-fifo")
        task = rdir / "state" / "tasks" / "trial-live"
        (task / "meta.json").unlink()
        os.mkfifo(str(task / "meta.json"))
        done = threading.Event()

        def writer():  # lets a blocked reader go on (with a meta.json that names no pane), so the old code fails instead of hanging
            while not done.is_set():
                try:
                    fd = os.open(str(task / "meta.json"), os.O_WRONLY | os.O_NONBLOCK)
                except OSError:  # no reader yet (ENXIO)
                    done.wait(0.01)
                    continue
                try:
                    os.write(fd, b"{}")
                except OSError:
                    pass
                finally:
                    os.close(fd)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        try:
            run = ts.Run(rdir, self.env)
            with self.assertRaises(ts.ShadowError) as caught:
                run.agent("stop", "trial-live")
            self.assertIn("not a plain file", str(caught.exception))
        finally:
            done.set()
            thread.join()
        self.assertTrue(self.alive(pane), "nothing was stopped")

    def test_probe_a_deeply_nested_meta_or_process_record_is_a_refusal_and_not_a_crash(self):
        deep = '{"pane": "%0", "x": ' + "[" * 200000 + "]" * 200000 + "}"
        for name in ("meta.json", "process.json"):
            with self.subTest(file=name):
                rdir, info, pane = self.live_run_with_task("sh-g-deep-" + name.split(".")[0])
                task = rdir / "state" / "tasks" / "trial-live"
                (task / name).write_text(deep)
                run = ts.Run(rdir, self.env)
                with self.assertRaises(ts.ShadowError):
                    run.agent("stop", "trial-live")
                self.assertTrue(self.alive(pane))
                self.settle(task, "waiting", "RESULT_STATUS: complete\nDone.\n")
                for verb in (["end-author"], ["review", "--force"]):  # a ShadowError, which `Run.review` records as review_error
                    with self.assertRaises(ts.ShadowError) as caught:
                        run.agent(*verb, "trial-live")
                    self.assertIn(name, str(caught.exception))
                self.assertTrue(self.alive(pane))

    def test_probe_a_run_json_the_worker_broke_does_not_keep_the_session_kill_from_running(self):
        # every refusal ends in "the session kill ends the pane": a note the run could not write must not skip the cleanup
        for n, broken in enumerate(("not json\n", "[" * 200000 + "]" * 200000)):
            with self.subTest(broken=broken[:10]):
                outside = self.outside("outside-rj{}".format(n))
                rdir, info = self.prepare(run="sh-g-rj{}".format(n))

                def poll(count, run):
                    if count == 1:
                        self.forge_pane(run.task_path(), outside)  # end-author / stop of it is refused: only the session kill is left
                        (rdir / "run.json").write_text(broken)
                with self.assertRaises(ts.ShadowError):  # the run's own record cannot be read back at the end
                    self.run_stub(rdir, poll)
                self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env), "the run's session was ended")
                self.assertFalse(self.alive(self.worker_pane), "the worker's pane went with the session")
                self.assertTrue(self.alive(outside), "the pane outside the run was not ended")

    def test_probe_a_run_lock_the_worker_holds_does_not_stop_the_runner_or_its_cleanup(self):
        # .run.lock is in the run dir: an unbounded wait on it would stop the runner, the attempt clock and the session kill
        import fcntl
        outside = self.outside("outside-lk")
        rdir, info = self.prepare(run="sh-g-lk")
        held = []

        def poll(count, run):
            if count == 1:
                self.forge_pane(run.task_path(), outside)
                handle = open(str(rdir / ".run.lock"), "a")
                fcntl.flock(handle, fcntl.LOCK_EX)  # what the worker can do, and keep doing
                held.append(handle)
        try:
            with mock.patch.object(ts, "LOCK_WAIT_S", 0):
                run, done = self.run_stub(rdir, poll)  # returns: a note it cannot write fails the run, it does not wait without end
                with self.assertRaises(ts.ShadowError) as caught:
                    ts.Run(rdir, self.env).note(x=1)
        finally:
            for handle in held:
                handle.close()
        self.assertEqual(len(held), 1)
        self.assertIn(".run.lock", str(caught.exception))
        self.assertNotEqual(done.get("phase"), "done", "no note was written while the worker held the lock")
        self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env), "the run's session was ended")
        self.assertFalse(self.alive(self.worker_pane), "the worker's pane went with the session")
        self.assertTrue(self.alive(outside), "the pane outside the run was not ended")

    def feed_fifo(self, path, data):
        """Make `path` a FIFO, with a writer that lets a blocked reader go on with `data` (the old code then fails instead of
        hanging). Returns the stop for the writer."""
        path = Path(path)
        if path.exists() or path.is_symlink():
            path.unlink()
        os.mkfifo(str(path))
        done = threading.Event()

        def writer():
            while not done.is_set():
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_NONBLOCK)
                except OSError:  # no reader yet (ENXIO)
                    done.wait(0.01)
                    continue
                try:
                    os.write(fd, data)
                except OSError:
                    pass
                finally:
                    os.close(fd)
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()

        def stop():
            done.set()
            thread.join()
        return stop

    def test_probe_a_file_the_worker_can_write_is_read_only_as_a_plain_file_never_waiting_on_a_fifo(self):
        # every read of a file in the run dir, the state dir, the worktree or the owner dir: a FIFO there would stop the runner (the
        # attempt clock, the session kill) or `tp-trial stop` / `tp-trial shadow` until a writer came
        rdir, info, pane = self.live_run_with_task("sh-g-ff")
        task = rdir / "state" / "tasks" / "trial-live"
        run_json = (rdir / "run.json").read_bytes()
        owner_record = ts.owner_file(info["project"], info["run"])
        owner_bytes = owner_record.read_bytes()
        worktree = self.root / "a-tree"  # what a worktree's .git file is (the run's tree is made at spawn)
        worktree.mkdir()
        gitfile = "gitdir: {}\n".format(self.repo / ".git" / "worktrees" / "x").encode()
        (worktree / ".git").write_bytes(gitfile)

        def plain(path, data):
            path.unlink()
            path.write_bytes(data)
        cases = (
            ("read_run", rdir / "run.json", run_json, lambda: ts.read_run(rdir), run_json),
            ("note", rdir / "run.json", b"{}", lambda: ts.Run(rdir, self.env).note(x=1), run_json),
            ("update_run", rdir / "run.json", b"{}", lambda: ts.update_run(rdir, x=1), run_json),
            ("run_info", rdir / "run.json", run_json, lambda: trial_session._run_info(rdir), run_json),
            ("json", task / "meta.json", b'{"pane": "%1"}', lambda: ts._json(task / "meta.json"), (task / "meta.json").read_bytes()),
            ("owner", owner_record, owner_bytes, lambda: ts.Owner(rdir).problem, owner_bytes),
            ("gitfile", worktree / ".git", gitfile, lambda: ts._gitfile_target(worktree), gitfile),
        )
        for name, path, data, call, restore in cases:
            with self.subTest(site=name):
                stop = self.feed_fifo(path, data)
                try:
                    try:
                        got = call()
                    except ts.ShadowError as problem:
                        got = problem
                finally:
                    stop()
                    plain(path, restore)
                if name in ("read_run", "note", "update_run"):
                    self.assertIsInstance(got, ts.ShadowError)
                    self.assertIn("not a plain file", str(got))
                elif name in ("run_info", "json"):
                    self.assertEqual(got, {})
                elif name == "owner":
                    self.assertIn("not a plain file", got or "")
                else:
                    self.assertIsNone(got)
        with self.assertRaises(OSError):
            stop = self.feed_fifo(rdir / "probe", b"x")
            try:
                trial_session.read_plain(rdir / "probe")
            finally:
                stop()
        self.assertTrue(self.alive(pane))

    def test_probe_a_deeply_nested_owner_record_or_task_file_is_a_problem_and_not_a_crash(self):
        rdir, info, pane = self.live_run_with_task("sh-g-dn")
        deep = "[" * 200000 + "]" * 200000
        task = rdir / "state" / "tasks" / "trial-live"
        (task / "dispatch.json").write_text(deep)
        self.assertEqual(ts._json(task / "dispatch.json"), {})
        ts.owner_file(info["project"], info["run"]).write_text(deep)
        self.assertIn("cannot be read", ts.Owner(rdir).problem)
        self.assertTrue(self.alive(pane))

    def test_probe_a_temp_file_the_worker_planted_is_never_written_through_or_waited_on(self):
        # run.json and the owner record are replaced through `<name>.tmp.<runner pid>`; the worker can read the pid in run.json
        import stat
        rdir, info, pane = self.live_run_with_task("sh-g-tmp")
        victim = self.root / "victim"
        victim.write_text("mine\n")
        record = ts.owner_file(info["project"], info["run"])

        def owner_write():
            self.session_pane(rdir, "sleep 300")  # a new pane in the run's session: the record changes and is written
            ts.Owner(rdir).record_panes(self.env)
        for name, target, act in (("run.json", rdir / "run.json", lambda: ts.Run(rdir, self.env).note(x=1)), ("owner", record, owner_write)):
            with self.subTest(site=name, planted="link"):
                tmp = target.with_name("{}.tmp.{}".format(target.name, os.getpid()))
                os.symlink(str(victim), str(tmp))
                try:
                    act()
                    left = os.path.lexists(str(tmp))
                finally:
                    if os.path.lexists(str(tmp)):
                        os.unlink(str(tmp))
                    victim_text = victim.read_text()
                    victim.write_text("mine\n")
                self.assertEqual(victim_text, "mine\n", "nothing was written through the link")
                self.assertTrue(stat.S_ISREG(os.lstat(str(target)).st_mode))
                self.assertFalse(left)
        with self.subTest(site="run.json", planted="fifo"):
            tmp = rdir / "run.json.tmp.{}".format(os.getpid())
            os.mkfifo(str(tmp))
            done = threading.Event()

            def reader():  # lets a blocked writer go on, so the old code fails instead of hanging
                fd = os.open(str(tmp), os.O_RDONLY | os.O_NONBLOCK)
                try:
                    while not done.is_set():
                        try:
                            os.read(fd, 65536)
                        except BlockingIOError:
                            pass
                        done.wait(0.01)
                finally:
                    os.close(fd)
            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            try:
                ts.Run(rdir, self.env).note(y=2)
            finally:
                done.set()
                thread.join()
            self.assertTrue(stat.S_ISREG(os.lstat(str(rdir / "run.json")).st_mode), "run.json is a plain file")
            self.assertEqual(json.loads((rdir / "run.json").read_text())["y"], 2)
        self.assertTrue(self.alive(pane))

    def test_probe_the_worker_cannot_change_the_runners_settings_through_run_json(self):
        # each note merges run.json; the runner's attempt clock, poll, review limit and model are what prepare wrote, never what the
        # worker wrote into the file since
        rdir, info = self.prepare(run="sh-g-set", timeout_s=300)
        slept = []

        class Recording(Clock):
            def sleep(self, seconds):
                slept.append(seconds)
                super().sleep(seconds)
        clock = Recording(step=100.0)

        def poll(count, run):
            if count == 1:
                forged = json.loads((rdir / "run.json").read_text())
                forged.update(timeout_s=10 ** 9, poll_s=10 ** 9, review_timeout_s=10 ** 9, main_max_local_workers=99, model="openai/forged")
                (rdir / "run.json").write_text(json.dumps(forged))
            if count == 50:  # the old runner would wait 10**9 s: end it here, so the test fails instead of running on
                raise RuntimeError("the runner was still waiting at poll 50")
        run, done = self.run_stub(rdir, poll, clock=clock)
        self.assertEqual(done["outcome"], "timeout", done.get("error"))
        self.assertEqual(set(slept), {info["poll_s"]}, "every poll slept what prepare set")
        self.assertEqual((run.info["timeout_s"], run.info["model"], run.info["review_timeout_s"]), (300, info["model"], info["review_timeout_s"]))
        self.assertFalse(self.alive(self.worker_pane))

    def test_probe_a_base_commit_the_worker_wrote_never_reaches_git_as_an_option(self):
        # the normal arm's diff is `git diff --numstat <base_commit>`: "--output=<file>" there would have git overwrite the file
        victim = self.root / "victim"
        for where in ("tp-trial normal", "the runner's own end"):
            with self.subTest(where=where):
                victim.write_text("mine\n")
                rdir, info = self.prepare(run="sh-g-nb{}".format(len(where)))

                def forge():
                    forged = json.loads((rdir / "run.json").read_text())
                    forged["base_commit"] = "--output={}".format(victim)
                    (rdir / "run.json").write_text(json.dumps(forged))
                if where == "tp-trial normal":
                    forge()
                    try:
                        ts.record_normal(rdir, self.env, RecordingRecords())
                    except ts.ShadowError:
                        pass
                else:
                    self.run_stub(rdir, lambda count, run: forge() if count == 1 else self.settle_worker(run) if count == 2 else None)
                self.assertEqual(victim.read_text(), "mine\n", "git was not handed the worker's option")

    def test_probe_the_tasks_the_worker_makes_cannot_put_off_the_session_kill(self):
        # the cleanup stops each task dir under state/tasks through tp-agent before it ends the session; the worker can make as many
        # as it likes (each up to two commands of CMD_TIMEOUT), so the step as a whole has one budget
        rdir, info, pane = self.live_run_with_task("sh-g-many")
        for n in range(5):
            fake = rdir / "state" / "tasks" / "trial-fake{}".format(n)
            fake.mkdir()
            (fake / "meta.json").write_text("{}")
        calls = []
        real_tp = ts.Owner.tp

        def counted(owner, args, env, timeout=ts.CMD_TIMEOUT):
            calls.append((Path(str(args[0])).name,) + tuple(str(a) for a in args[1:]))
            return real_tp(owner, args, env, timeout)
        with mock.patch.object(ts, "STOP_TASKS_S", 0, create=True), mock.patch.object(ts.Owner, "tp", counted):
            left = ts.cleanup(rdir, self.env)
        self.assertEqual(calls, [], "no task is stopped once the budget is spent")
        self.assertTrue(any("not stopped" in item for item in left), left)
        self.assertIsNone(trial_session.named(trial_session.session_name(info["run"]), self.env), "the run's session was ended")
        self.assertFalse(self.alive(pane), "the worker's pane went with the session")

    def test_probe_a_run_lock_the_worker_holds_does_not_stop_tp_trial_shadow_after_launch(self):
        # `tp-trial shadow` notes the runner's pid with `update_run` once the runner (and so the worker) may be going
        import fcntl
        rdir, info, pane = self.live_run_with_task("sh-g-ul")
        with open(str(rdir / ".run.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with mock.patch.object(ts, "LOCK_WAIT_S", 0):
                with self.assertRaises(ts.ShadowError) as caught:
                    ts.update_run(rdir, pid=1)
        self.assertIn(".run.lock", str(caught.exception))
        self.assertIsNone(json.loads((rdir / "run.json").read_text())["pid"], "run.json was not changed")

    def test_f1_the_own_pane_is_still_ended_by_end_author(self):
        rdir, info, pane, task = self.settled_task("sh-g-own")
        run = ts.Run(rdir, self.env)
        done = run.agent("end-author", "trial-live")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.alive(pane))

    # -- the table: nothing it does not name is started
    def test_f1_a_subcommand_not_in_the_table_is_refused_and_nothing_is_started(self):
        rdir, info, pane = self.live_run_with_task("sh-g-tab")
        run = ts.Run(rdir, self.env)
        for args in (("collect", "trial-live"), ("reap", "--yes"), ("retry", "trial-live", "--reason", "x"), ("message", "trial-live", "--text", "x"),
                     ("dispatch", "--role", "coder"), ("tick",), ("index",), ("guard-review", "trial-live"), ("objective", "--text", "x"),
                     ("request", "--kind", "tool"), ("frobnicate", "trial-live")):
            with self.subTest(args=args):
                with mock.patch.object(trial_session.subprocess, "run") as started, mock.patch.object(ts.subprocess, "run") as started_here:
                    with self.assertRaises(ts.ShadowError) as caught:
                        run.agent(*args)
                started.assert_not_called()
                started_here.assert_not_called()
                self.assertIn("not a command the trial may start", str(caught.exception))
        self.assertTrue(self.alive(pane))

    def test_f1_a_flag_or_argument_not_in_the_table_is_refused(self):
        rdir, info, pane = self.live_run_with_task("sh-g-flag")
        run = ts.Run(rdir, self.env)
        agent, spawn = ROOT / "bin" / "tp-agent", ROOT / "bin" / "tp-spawn"
        for argv in ([agent, "review", "--force-alive", "trial-live"], [agent, "stop", "--all", "trial-live"], [agent, "stop", "trial-live", "extra"],
                     [agent, "inspect", "../trial-live"], [agent, "inspect", "-h"], [agent, "list", "--main", "x"], [agent],
                     [spawn, "--task-id", "x", "--view"], [spawn, "--task-id", "x", "--worker"], [spawn, "--worker", "pi"],
                     [spawn, "--task-id", "x", "--task-id", "y"], [spawn, "--task-id", "../x"],
                     [ROOT / "bin" / "tp-kill", "trial-live"], [ROOT / "bin" / "tp-review", "trial-live"], ["tp-agent", "stop", "trial-live"],
                     ["/bin/sh", "-c", "true"], [ROOT / "bin" / "tp-trial", "stop", "x"], []):
            with self.subTest(argv=argv):
                with mock.patch.object(trial_session.subprocess, "run") as started, mock.patch.object(ts.subprocess, "run") as started_here:
                    with self.assertRaises(ts.ShadowError):
                        run.command(argv)
                started.assert_not_called()
                started_here.assert_not_called()
        self.assertTrue(self.alive(pane))

    def test_f1_a_command_that_can_end_something_is_never_started_without_a_check(self):
        for argv in ([ROOT / "bin" / "tp-agent", "stop", "x"], [ROOT / "bin" / "tp-agent", "end-author", "x"],
                     [ROOT / "bin" / "tp-agent", "review", "--force", "x"], [ROOT / "bin" / "tp-spawn", "--task-id", "x"]):
            with self.subTest(argv=argv):
                with mock.patch.object(trial_session.subprocess, "run") as started:
                    with self.assertRaises(trial_session.SessionError):
                        trial_session.start_tp(argv, self.env)
                started.assert_not_called()

    def test_f1_every_subprocess_of_the_trial_is_started_in_a_known_place(self):
        """The gate holds only while no other code starts a process: the functions that do, in the three files, are these."""
        import ast
        calls = {("subprocess", n) for n in ("run", "Popen", "check_output", "call", "check_call", "getoutput", "getstatusoutput")}
        calls |= {("os", n) for n in ("kill", "killpg", "system", "popen", "execv", "execve", "execvp", "execl", "execlp", "spawnv", "spawnvp", "posix_spawn")}
        found = {}
        for path in (ROOT / "lib" / "trial_shadow.py", ROOT / "lib" / "trial_session.py", ROOT / "bin" / "tp-trial"):
            for fn in ast.walk(ast.parse(path.read_text())):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                                and (node.func.value.id, node.func.attr) in calls):
                            found.setdefault(path.name, set()).add((fn.name, node.func.value.id + "." + node.func.attr))
        self.assertEqual(found, {
            "trial_shadow.py": {("git", "subprocess.run"), ("pid_is_run", "subprocess.run"), ("stop", "os.kill")},
            "trial_session.py": {("_tmux", "subprocess.run"), ("process_start", "subprocess.run"), ("_alive", "os.kill"),
                                 ("start_tp", "subprocess.run"), ("start_tp", "subprocess.Popen")},
            "tp-trial": {("main_state", "subprocess.check_output"), ("main_state", "subprocess.run"), ("main", "os.execv")},
        })

    # -- probes: what else tp-agent stop follows
    def test_f1_stop_is_refused_while_the_session_shows_the_task_in_its_viewer_slot(self):
        outside = self.outside()
        rdir, info, pane = self.live_run_with_task("sh-g-view")
        name = "=tp-trial-sh-g-view:"
        self.tmux("set-option", "-t", name, "@tp_viewing", "trial-live")  # tp-kill would run tp-view -, which swaps in @tp_viewer_pane
        self.tmux("set-option", "-t", name, "@tp_viewer_pane", outside)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError) as caught:
            run.agent("stop", "trial-live")
        self.assertIn("@tp_viewing", str(caught.exception))
        self.assertTrue(self.alive(outside))
        self.assertFalse((rdir / "state" / "tasks" / "trial-live" / "stopped.json").exists(), "tp-agent stop did not run")

    def test_f1_stop_is_refused_when_another_session_holds_the_runs_name(self):
        outside = self.outside()
        rdir, info, pane = self.live_run_with_task("sh-g-name")
        self.tmux("rename-session", "-t", "=tp-trial-sh-g-name", "renamed-by-worker")
        self.tmux("new-session", "-d", "-s", "tp-trial-sh-g-name", "sleep 300")  # tp-agent resolves TP_SESSION by name: this one
        for option, value in (("@tp_state_dir", info["state"]), ("@tp_viewing", "trial-live"), ("@tp_viewer_pane", outside)):
            self.tmux("set-option", "-t", "=tp-trial-sh-g-name:", option, value)
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError) as caught:
            run.agent("stop", "trial-live")
        self.assertIn("not the one this run created", str(caught.exception))
        self.assertTrue(self.alive(outside))
        self.assertTrue(self.alive(pane), "nothing was stopped")

    def test_f1_stop_is_refused_when_the_queue_holds_an_entry_for_the_task_or_is_a_link(self):
        rdir, info, pane = self.live_run_with_task("sh-g-q")
        foreign = self.root / "foreign-queue"
        foreign.mkdir()
        (foreign / "trial-live.json").write_text("{}")
        queue = rdir / "state" / "queue"
        os.symlink(str(foreign), str(queue))  # tp-agent stop would unlink <queue>/trial-live.json: a file that is not the run's
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError) as caught:
            run.agent("stop", "trial-live")
        self.assertIn("queue", str(caught.exception))
        self.assertTrue((foreign / "trial-live.json").exists(), "the file the link leads to is kept")
        os.unlink(str(queue))
        queue.mkdir()
        (queue / "trial-live.json").write_text("{}")
        with self.assertRaises(ts.ShadowError):
            run.agent("stop", "trial-live")
        self.assertTrue((queue / "trial-live.json").exists())
        self.assertTrue(self.alive(pane))

    def test_f1_cleanup_goes_through_the_same_gate(self):
        outside = self.outside()
        rdir, info, pane = self.live_run_with_task("sh-g-clean")
        self.tmux("set-option", "-t", "=tp-trial-sh-g-clean:", "@tp_viewing", "trial-live")
        self.tmux("set-option", "-t", "=tp-trial-sh-g-clean:", "@tp_viewer_pane", outside)
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue(any("@tp_viewing" in l for l in leftovers), leftovers)
        self.assertTrue(self.alive(outside))
        self.assertNotIn("tp-trial-sh-g-clean", self.session_names(), "the session is still ended by its id")

    def test_f1_the_runner_is_started_through_the_gate(self):
        rdir, info = self.prepare(run="sh-g-launch")
        with mock.patch.object(trial_session, "start_tp", wraps=trial_session.start_tp) as gate, \
                mock.patch.object(trial_session.subprocess, "Popen") as popen:
            popen.return_value.pid = 4242
            self.assertEqual(ts.launch(rdir, info["project"], self.env), 4242)
        gate.assert_called_once()
        self.assertEqual([str(a) for a in popen.call_args[0][0]][1:], [str(ROOT / "bin" / "tp-trial"), "run", "--run-dir", str(rdir)])

    # -- N2: a failed prepare undoes only what it made
    def test_n2_a_prepare_that_loses_a_race_to_another_prepare_of_the_same_run_changes_nothing_of_it(self):
        real_guard = trial_session.guard
        other = {}

        def racing(*args, **kw):
            if not other:
                other["started"] = True  # the other prepare's own guard call goes straight through
                other["info"] = ts.prepare(self.state, self.source_id, run="sh-race", model=MODEL, thinking="medium", env=self.env)
                project = other["info"]["project"]
                other["run"] = ts.run_file(trial_session.run_dir(project, "sh-race")).read_text()
                other["owner"] = ts.owner_file(project, "sh-race").read_text()
                other["session"] = trial_session.named("tp-trial-sh-race", self.env)
            return real_guard(*args, **kw)
        with mock.patch.object(trial_session, "guard", side_effect=racing):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-race", model=MODEL, thinking="medium", env=self.env)
        project = other["info"]["project"]
        self.assertEqual(ts.run_file(trial_session.run_dir(project, "sh-race")).read_text(), other["run"])
        self.assertEqual(ts.owner_file(project, "sh-race").read_text(), other["owner"])
        self.assertEqual(trial_session.named("tp-trial-sh-race", self.env), other["session"])

    def test_n2_a_session_another_start_made_after_the_check_is_not_ended(self):
        real_ensure = trial_session.ensure

        def racing(project, run, env=None, **kw):
            state = trial_session.state_dir(project, run)
            self.tmux("new-session", "-d", "-s", "tp-trial-" + run, "sleep 300")  # another start of the run made it first
            self.tmux("set-option", "-t", "=tp-trial-" + run + ":", "@tp_state_dir", str(state))
            return real_ensure(project, run, env, **kw)
        with mock.patch.object(trial_session, "ensure", side_effect=racing), \
                mock.patch.object(ts.Owner, "begin", side_effect=ts.ShadowError("begin failed")):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-n2r", model=MODEL, thinking="medium", env=self.env)
        self.assertIn("tp-trial-sh-n2r", self.session_names(), "not made by this prepare")

    def test_n2_a_run_dir_put_in_place_of_the_one_prepare_made_is_not_removed(self):
        moved = self.root / "moved-run-dir"

        def swap(owner, env):
            os.rename(str(owner.rdir), str(moved))
            owner.rdir.mkdir()
            (owner.rdir / "someone-elses").write_text("keep")
            raise ts.ShadowError("begin failed")
        with mock.patch.object(ts.Owner, "begin", autospec=True, side_effect=swap):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-n2s", model=MODEL, thinking="medium", env=self.env)
        self.assertTrue((trial_session.run_dir(self.repo, "sh-n2s") / "someone-elses").exists(), "not the directory this prepare made")
        self.assertNotIn("tp-trial-sh-n2s", self.session_names(), "the session it made is still ended")

    def test_n2_an_owner_record_of_another_run_dir_is_not_removed(self):
        owner = ts.owner_file(str(self.repo.resolve()), "sh-n2o")

        def other_record(_owner, env):
            owner.parent.mkdir(parents=True, exist_ok=True)
            owner.write_text(json.dumps({"rdir_id": {"dev": 1, "ino": 2}}))  # another prepare's record, written meanwhile
            raise ts.ShadowError("begin failed")
        with mock.patch.object(ts.Owner, "begin", autospec=True, side_effect=other_record):
            with self.assertRaises(ts.ShadowError):
                ts.prepare(self.state, self.source_id, run="sh-n2o", model=MODEL, thinking="medium", env=self.env)
        self.assertTrue(owner.exists(), "the record names another run directory")


if __name__ == "__main__":
    unittest.main()
