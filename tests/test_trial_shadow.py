"""lib/trial_shadow.py and bin/tp-trial (trial lane L3, design 3/9/10): a shadow runs the brief a normal coder got in a throwaway
detached worktree and its own session, reviews the diff and writes a record, and leaves the real lane, step, branches and
Main's state dir exactly as they were. This file: the pure functions, the source, prepare, the collect step, stop, the command
line, the folded-in pi request and one full run. Fixtures: trial_shadow_case.py. The other groups are test_trial_shadow_run / _owner / _gate /
_worker_files.

One full run goes through the real tp-spawn, tp-agent and tp-review, with `new-window` stubbed, so nothing starts a model.
No test waits a fixed time: every wait is a poll callback that settles the task, or a clock that jumps.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

# ------------------------------------------------------------------------------------------------------------- pure functions

class PureTests(unittest.TestCase):
    def test_other_lane_section_is_the_body_up_to_the_next_heading(self):
        packet = "# Task\n\nt\n\n{}\n{}\n\n## After\n\nz\n".format(SECTION, OTHERS)
        self.assertEqual(ts.other_lane_section(packet), OTHERS)
        self.assertEqual(ts.other_lane_section(packet.replace("## After", "# After")), OTHERS)
        self.assertEqual(ts.other_lane_section("# Task\n\nno section\n"), "")
        self.assertEqual(ts.other_lane_section("{}\n{}".format(SECTION, OTHERS)), OTHERS, "a section at the end of the packet")

    def test_remap_context_points_the_lanes_files_at_the_shadow_tree(self):
        specs = ["/lane/wt/lib/a.py", "/lane/wt/lib/a.py:3-9", "/lane/wt", "/lane/wt-other/x", "/elsewhere/f", "rel/f.py", "rel/f.py:1-2"]
        got = ts.remap_context(specs, "/lane/wt", "/trial/worktree")
        self.assertEqual(got, ["/trial/worktree/lib/a.py", "/trial/worktree/lib/a.py:3-9", "/trial/worktree", "/lane/wt-other/x", "/elsewhere/f",
                               "rel/f.py", "rel/f.py:1-2"])
        self.assertEqual(ts.remap_context(["/lane/wt/a"], "", "/t"), ["/lane/wt/a"], "no recorded working tree: nothing is rewritten")

    def source(self, **kw):
        return {"role": "coder", "tests": "affected", "deliverable": "Report", "other_lane_paths": OTHERS, "context": ["/wt/a.py:1-4"], "notes": ["n1"],
                "workdir": "/wt", **kw}

    def test_the_spawn_argv_names_the_explicit_model_and_never_asks_the_router(self):
        trial = {"model": "lmstudio/qwen/qwen3.6-35b-a3b", "thinking": "medium"}
        args = ts.spawn_args(self.source(), "sh-a-1", "/p/wt", "/p", trial, "/p/request.md", "trial-sh-a-1")
        self.assertEqual(args[args.index("--model") + 1], "lmstudio/qwen/qwen3.6-35b-a3b")
        self.assertEqual(args[args.index("--thinking") + 1], "medium")
        self.assertEqual(args[args.index("--worker") + 1], "pi")
        for forbidden in ("--lane", "--step", "--level", "--escalate", "--route", "--local-only", "--parent", "--review-of"):
            self.assertNotIn(forbidden, args)
        self.assertEqual(args[-2:], ["--file", "/p/request.md"])
        self.assertEqual(args[args.index("--workdir") + 1], "/p/wt")
        self.assertEqual(args[args.index("--preamble-from") + 1], "/p")
        self.assertIn("/p/wt/a.py:1-4", args)
        self.assertEqual(args[args.index("--other-lane-paths") + 1], OTHERS)
        self.assertEqual(args[args.index("--tests") + 1], "affected")
        envs = [args[i + 1] for i, a in enumerate(args) if a == "--worker-env"]
        self.assertEqual(sorted(envs), ["TP_DOCS_TOOLS=1", "TP_TRIAL_ID=sh-a-1"])
        notes = [args[i + 1] for i, a in enumerate(args) if a == "--note"]
        self.assertEqual(notes, ["n1", ts.workdir_text("/p/wt", "/p"), ts.TRIAL_NOTE])
        self.assertIn("/p/wt", ts.workdir_text("/p/wt", "/p"))

    def test_no_other_lane_section_no_flag_and_no_role_no_flag(self):
        args = ts.spawn_args(self.source(other_lane_paths="", role="", tests=None, deliverable=""), "r", "/w", "/p", {"model": "m", "thinking": "low"}, "/f", "t")
        for flag in ("--other-lane-paths", "--role", "--tests", "--deliverable"):
            self.assertNotIn(flag, args)

    def test_the_shadow_code_never_loads_the_router_that_escalation_lives_in(self):
        code = "import sys; sys.path.insert(0, {!r}); import trial_shadow, trial_session; " \
               "print(sorted(m for m in ('model_router', 'route_resolve', 'router_catalog', 'router') if m in sys.modules))".format(str(ROOT / "lib"))
        run = subprocess.run([sys.executable, "-I", "-c", code], text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "[]")

    def test_run_ids_are_valid_session_names(self):
        self.assertEqual(ts.default_run_id("coder-1db5d40f79", "20261031"), "sh-coder-1db5d40f79-20261031")
        self.assertTrue(trial_session.RUN.fullmatch(ts.default_run_id("Very_Long Task.ID/with*odd" * 4, "20261031120000")))
        self.assertTrue(trial_session.RUN.fullmatch(ts.default_run_id("!!!", "1")))

    def test_the_record_module_is_the_real_one_when_it_exists_else_the_stub(self):
        import importlib.util
        want = "trial_record" if importlib.util.find_spec("trial_record") else "trial_record_stub"
        self.assertEqual(ts.load_record_module().__name__, want)

# ---------------------------------------------------------------------------------------------------------------- the source

class SourceTests(ShadowCase):
    def test_the_source_is_read_from_the_task_dir_and_not_changed(self):
        before = self.snapshot(self.state)
        source = ts.read_source(self.state, "src-1")
        self.assertEqual((source["base_commit"], source["tests"], source["notes"], source["role"]), (self.base, "affected", ["keep it small"], "coder"))
        self.assertEqual(source["request"], "Make a focused change in lib/x.py\n")
        self.assertEqual(source["other_lane_paths"], OTHERS)
        self.assertEqual(source["reviewer_model"], "claude-sonnet-5-5")
        self.assertEqual(self.snapshot(self.state), before)

    def test_what_cannot_be_shadowed_is_refused_with_the_reason(self):
        self.make_source("rev-1", review_of=True)
        self.make_source("nobase-1", dispatch={"base_commit": None})
        self.make_source("badbase-1", dispatch={"base_commit": "not-a-commit; rm"})
        self.make_source("norequest-1", request=None)
        self.make_source("badctx-1", dispatch={"context": "x"})
        for task, word in (("gone-1", "no task"), ("rev-1", "review"), ("nobase-1", "base_commit"), ("badbase-1", "base_commit"),
                           ("norequest-1", "request.md"), ("badctx-1", "context"), ("../x", "not a task id")):
            with self.subTest(task=task):
                with self.assertRaises(ts.ShadowError) as caught:
                    ts.read_source(self.state, task)
                self.assertIn(word, str(caught.exception))

    def test_an_archived_task_can_be_the_source(self):
        task = self.state / "tasks" / "src-1"
        (self.state / "archive").mkdir()
        shutil.move(str(task), str(self.state / "archive" / "src-1"))
        self.assertEqual(ts.read_source(self.state, "src-1")["base_commit"], self.base)

# ------------------------------------------------------------------------------------------------------------------ prepare

class PrepareTests(ShadowCase):
    def test_d_it_refuses_to_start_when_main_has_two_local_workers_and_creates_nothing(self):
        self.local_task(self.state)
        self.local_task(self.state)
        before = (self.session_names(), self.worktrees(), self.refs("refs"))
        with self.assertRaises(ts.ShadowError) as caught:
            ts.prepare(self.state, "src-1", model=LOCAL, thinking="medium", env=self.env)
        self.assertIn("Main holds 2 of its 2 local worker slots", str(caught.exception))
        self.assertFalse((self.repo / ".tmux-pi" / "trial").exists(), "no run directory, no session, no worktree")
        self.assertEqual((self.session_names(), self.worktrees(), self.refs("refs")), before)

    def test_d_with_a_free_slot_it_starts_and_the_run_holds_its_own_state_dir(self):
        self.local_task(self.state)
        rdir, info = self.prepare(model=LOCAL)
        self.assertEqual(info["phase"], "queued")
        self.assertEqual(info["state"], str(rdir / "state"))
        self.assertIn("tp-trial-" + info["run"], self.session_names())
        self.assertEqual(info["base_commit"], self.base)
        text = (rdir / "request.md").read_text()
        self.assertTrue(text.startswith(ts.workdir_text(self.tree(rdir), self.repo)), "the first request names the absolute working tree first")
        self.assertTrue(text.endswith("\n\nMake a focused change in lib/x.py\n"), "then the request the normal coder got")
        self.assertEqual(info["workdir"], str(self.tree(rdir)))
        trial_shadow_cleanup = ts.cleanup(rdir, self.env)
        self.assertEqual(trial_shadow_cleanup, [])

    def test_a_second_local_trial_run_is_refused_while_one_runs(self):
        rdir, _ = self.prepare(model=LOCAL, run="sh-one")
        self.local_task(trial_session.state_dir(self.repo, "sh-one"), name="T-1")
        with self.assertRaises(ts.ShadowError) as caught:
            ts.prepare(self.state, "src-1", model=LOCAL, thinking="medium", env=self.env, run="sh-two")
        self.assertIn("trial already runs 1 local worker", str(caught.exception))
        self.assertFalse(trial_session.run_dir(self.repo, "sh-two").exists())

    def test_f2_a_second_local_shadow_is_refused_while_the_first_is_only_queued(self):
        """No task exists yet for a run that is prepared or starting; the run itself holds the slot."""
        rdir, first = self.prepare(model=LOCAL, run="sh-one")
        self.assertEqual(first["phase"], "queued")
        with self.assertRaises(ts.ShadowError) as caught:
            ts.prepare(self.state, "src-1", model=LOCAL, thinking="medium", env=self.env, run="sh-two")
        self.assertIn("run sh-one", str(caught.exception))
        self.assertFalse(trial_session.run_dir(self.repo, "sh-two").exists())
        ts.update_run(rdir, phase="done", outcome="completed")  # a finished run frees the slot
        ts.prepare(self.state, "src-1", model=LOCAL, thinking="medium", env=self.env, run="sh-two")

    def test_f2_a_queued_run_of_a_model_that_is_not_local_holds_no_local_slot(self):
        self.prepare(model=MODEL, run="sh-one")
        ts.prepare(self.state, "src-1", model=LOCAL, thinking="medium", env=self.env, run="sh-two")

    def test_f2_the_recheck_before_spawn_lets_only_the_run_that_came_first_go_on(self):
        """Two runs prepared together (the guard in `prepare` patched to pass, as in a race): at spawn time the earlier one goes on,
        the later one is refused and never calls tp-spawn."""
        calls = []

        class Counting(ts.Run):
            def command(self, args, timeout=ts.CMD_TIMEOUT):
                calls.append(self.run)
                return subprocess.CompletedProcess(args, 1, "", "stop here")

        with mock.patch.object(trial_session, "guard", return_value=(True, "")):
            rdir_a, a = self.prepare(model=LOCAL, run="sh-a")
            rdir_b, b = self.prepare(model=LOCAL, run="sh-b")
        ts.update_run(rdir_a, created=self.ago(2))  # prepared moments ago: the attempt's clock is not spent
        ts.update_run(rdir_b, created=self.ago(1))
        first = Counting(rdir_a, self.env, sleep=lambda s: None, clock=Clock())
        first.note(phase="spawn")
        first.recheck_capacity()  # nothing ahead of it
        second = Counting(rdir_b, self.env, sleep=lambda s: None, clock=Clock())
        with self.assertRaises(ts.ShadowError) as caught:
            second.recheck_capacity()
        self.assertIn("not started: the trial already runs 1 local worker", str(caught.exception))
        self.assertIn("run sh-a", str(caught.exception))
        done = second.execute()
        self.assertEqual((done["outcome"], calls), ("spawn_failed", []), "tp-spawn was never called for the run that came second")
        self.assertIn("not started", done["error"])

    def test_f2_the_recheck_counts_a_worker_that_already_runs_whatever_the_order(self):
        rdir_a, _ = self.prepare(model=LOCAL, run="sh-a")
        ts.update_run(rdir_a, created="2026-10-31T10:00:09Z")
        with mock.patch.object(trial_session, "guard", return_value=(True, "")):
            rdir_b, _ = self.prepare(model=LOCAL, run="sh-b")
        ts.update_run(rdir_b, created="2026-10-31T10:00:00Z")
        self.local_task(trial_session.state_dir(self.repo, "sh-a"), name="T-1")
        with self.assertRaises(ts.ShadowError):
            ts.Run(rdir_b, self.env).recheck_capacity()

    def test_f2_the_recheck_yields_to_main_filling_its_slots(self):
        rdir, _ = self.prepare(model=LOCAL, run="sh-a")
        self.local_task(self.state)
        self.local_task(self.state)
        with self.assertRaises(ts.ShadowError) as caught:
            ts.Run(rdir, self.env).recheck_capacity()
        self.assertIn("Main holds 2 of its 2 local worker slots", str(caught.exception))

    def test_f5_a_zero_or_negative_limit_is_refused_before_anything_is_made(self):
        sessions = self.session_names()
        for kw in ({"timeout_s": 0}, {"timeout_s": -5}, {"review_timeout_s": 0}, {"review_timeout_s": -1}, {"poll_s": 0}, {"poll_s": -2.0}):
            with self.subTest(kw=kw):
                with self.assertRaises(ts.ShadowError) as caught:
                    ts.prepare(self.state, "src-1", model=MODEL, thinking="medium", env=self.env, run="sh-bad", **kw)
                self.assertIn("must be a positive number", str(caught.exception))
        self.assertFalse(trial_session.run_dir(self.repo, "sh-bad").exists())
        self.assertEqual(self.session_names(), sessions)

    def test_the_other_refusals_create_nothing(self):
        self.prepare(run="sh-dup")
        sessions = self.session_names()
        cases = (({"run": "sh-dup"}, "already exists"), ({"run": "Bad Run"}, "trial run id"), ({"pair_id": None, "run": "sh-new", "thinking": "bogus"}, "thinking"))
        for kw, word in cases:
            with self.subTest(kw=kw):
                kw = dict(kw)
                thinking = kw.pop("thinking", "medium")
                with self.assertRaises((ts.ShadowError, trial_session.SessionError)) as caught:
                    ts.prepare(self.state, "src-1", model=MODEL, thinking=thinking, env=self.env, **kw)
                self.assertIn(word, str(caught.exception))
        self.assertEqual(self.session_names(), sessions)
        self.assertFalse(trial_session.run_dir(self.repo, "sh-new").exists())

    def test_a_base_commit_that_is_not_in_the_repository_is_refused(self):
        self.make_source("ghost-1", dispatch={"base_commit": "0123456789abcdef0123456789abcdef01234567"})
        with self.assertRaises(ts.ShadowError) as caught:
            ts.prepare(self.state, "ghost-1", model=MODEL, thinking="medium", env=self.env)
        self.assertIn("does not exist", str(caught.exception))

# ---------------------------------------------------------------------------------------------------------------- the pieces

class CollectWorkTests(ShadowCase):
    def worktree_run(self):
        rdir, _ = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))
        return run

    def test_c_the_soft_reset_makes_the_review_diff_non_empty_and_the_messages_are_read_first(self):
        run = self.worktree_run()
        self.edit(run, commits=(("lib/x.py", "x edited\n", "Change x\n\nwith a body"), ("docs/d.md", "d edited\n", "Change d")))
        self.assertEqual(git(run.worktree, "diff", "HEAD", "--stat"), "", "before the reset a review of HEAD sees only the untracked file or nothing")
        run.collect_work()
        self.assertEqual(run.commit_messages, ["Change x\n\nwith a body", "Change d"], "read before the reset, oldest first")
        self.assertEqual(git(run.worktree, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(run.worktree, "log", self.base + "..HEAD", "--oneline"), "", "after the reset the messages can no longer be read")
        names = git(run.worktree, "diff", "--cached", "--name-only", "HEAD").split()
        self.assertEqual(sorted(names), ["docs/d.md", "lib/new.py", "lib/x.py"])
        self.assertEqual(run.diff[0], 3)
        self.assertGreater(run.diff[1], 0)
        patch = (run.rdir / "final.patch").read_text()
        self.assertIn("+x edited", patch)
        self.assertIn("+new", patch, "the file the worker never added is in the review and the patch")

    def test_c_the_record_is_built_from_the_messages_read_before_the_reset(self):
        run = self.worktree_run()
        records = RecordingRecords()
        run.records = records
        self.edit(run, commits=(("lib/x.py", "x edited\n", "Change x"),))
        (run.task_path()).mkdir(parents=True)
        (run.task_path() / "meta.json").write_text(json.dumps({"id": run.task_id, "role": "coder", "model": MODEL}))
        run.outcome = "completed"
        run.collect_work()
        run.write_record()
        (arm, kw), = records.calls
        self.assertEqual((arm, kw["commit_messages"], kw["diff_files"]), ("qwen", ["Change x"], 2))
        self.assertGreater(kw["diff_lines"], 0)

    def test_the_workers_last_commit_stays_reachable_until_cleanup_and_then_goes(self):
        run = self.worktree_run()
        self.edit(run)
        head = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        ref = "refs/tmux-pi/shadow/{}/head".format(run.run)
        self.assertEqual(git(self.repo, "rev-parse", ref), head)
        self.assertEqual(ts.cleanup(run.rdir, self.env), [])
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        self.assertFalse(run.worktree.exists())

    def test_a_branch_the_worker_made_is_left_where_it_is_and_reported_and_the_real_branches_are_not_touched(self):
        # review 8 F2: the runner made no branch, so it deletes none (more in test_trial_shadow_worker_files.BranchTests)
        run = self.worktree_run()
        before = self.refs("refs/heads")
        git(run.worktree, "checkout", "-q", "-b", "worker-branch")
        self.edit(run)
        tip = git(run.worktree, "rev-parse", "HEAD")
        run.collect_work()
        self.assertEqual(self.refs("refs/heads"), before + "\nrefs/heads/worker-branch " + tip)
        self.assertEqual(run.info["branch_left"], {"ref": "refs/heads/worker-branch", "commit": tip})
        self.assertNotIn("stray_branch", run.info)
        detached = subprocess.run(["git", "-C", str(run.worktree), "symbolic-ref", "-q", "HEAD"], capture_output=True, text=True)
        self.assertEqual(detached.returncode, 1, "the worktree is left detached, not on a branch")

    def test_a_worker_that_changed_nothing_leaves_an_empty_diff_not_an_error(self):
        run = self.worktree_run()
        run.collect_work()
        self.assertEqual(run.diff, (0, 0))
        self.assertEqual(run.commit_messages, [])

    def test_a_worker_on_the_main_branch_name_cannot_move_it(self):
        run = self.worktree_run()
        main_tip = git(self.repo, "rev-parse", "main")
        self.edit(run)
        run.collect_work()
        self.assertEqual(git(self.repo, "rev-parse", "main"), main_tip)

class StopTests(ShadowCase):
    def runner(self, rdir):
        """A stand-in for a run's runner: its command line names tp-trial and the run directory, and it lives until its stdin closes
        (or it is signalled), so it never outlives the test. Cleanups run last-in first-out: kill, reap, then close the pipe."""
        child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()", "tp-trial", "run", "--run-dir", str(rdir)],
                                 stdin=subprocess.PIPE)
        self.addCleanup(child.stdin.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    def test_pid_is_run_requires_the_runner_command_line_and_the_run(self):
        rdir, info = self.prepare()
        child = self.runner(rdir)
        self.assertTrue(ts.pid_is_run(child.pid, rdir))
        self.assertFalse(ts.pid_is_run(child.pid, rdir.with_name("sh-other")))
        for pid in (None, 0, 1, -5, "12", True, os.getpid()):
            self.assertFalse(ts.pid_is_run(pid, rdir), pid)

    def test_stop_signals_the_runner_waits_for_it_then_cleans_and_marks_the_run(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        child = self.runner(rdir)

        def sleep(_):
            child.poll()  # the poll is the wait (and reaps the child): the runner's exit is the event

        ts.update_run(rdir, pid=child.pid, phase="wait")
        done = ts.stop(rdir, self.env, sleep=sleep)
        self.assertIsNotNone(child.poll(), "the runner was signalled and has gone")
        self.assertEqual((done["phase"], done["outcome"]), ("done", "stopped"))
        self.assertFalse((self.tree(rdir)).exists())
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def test_stop_of_a_finished_run_only_cleans(self):
        rdir, info = self.prepare()
        self.run_stub(rdir, spawn_error="x")
        done = ts.stop(rdir, self.env)
        self.assertEqual((done["phase"], done["outcome"]), ("done", "spawn_failed"))

# --------------------------------------------------------------------------------------------------------- the command line

class CommandLineTests(ShadowCase):
    def tp_trial(self, *args, env=None, exe=None):
        return subprocess.run([str(exe or ROOT / "bin" / "tp-trial"), *args], env=env or {**self.env, "TP_STATE_DIR": str(self.state)},
                              text=True, capture_output=True, timeout=120)

    def test_shadow_refuses_with_a_message_and_status_one_when_main_is_full(self):
        self.local_task(self.state)
        self.local_task(self.state)
        run = self.tp_trial("shadow", "src-1", "--model", LOCAL)
        self.assertEqual(run.returncode, 1)
        self.assertIn("tp-trial: not started: Main holds 2 of its 2 local worker slots", run.stderr)
        self.assertFalse((self.repo / ".tmux-pi" / "trial").exists())

    def test_shadow_of_a_task_that_does_not_exist_is_refused(self):
        run = self.tp_trial("shadow", "nope-1")
        self.assertEqual(run.returncode, 1)
        self.assertIn("no task 'nope-1'", run.stderr)

    def test_status_lists_runs_and_stop_names_a_missing_one(self):
        self.assertEqual(self.tp_trial("status", "--project", str(self.repo)).stdout, "")
        rdir, info = self.prepare()
        out = self.tp_trial("status", "--project", str(self.repo)).stdout
        self.assertIn("run=" + info["run"], out)
        self.assertIn("phase=queued", out)
        as_json = json.loads(self.tp_trial("status", info["run"], "--project", str(self.repo), "--json").stdout)
        self.assertEqual(as_json["source_task"], "src-1")
        missing = self.tp_trial("stop", "sh-nothing", "--project", str(self.repo))
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no trial run 'sh-nothing'", missing.stderr)
        stopped = self.tp_trial("stop", info["run"], "--project", str(self.repo), "--json")
        self.assertEqual(json.loads(stopped.stdout)["phase"], "done", stopped.stderr)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

    def without_main_state(self, **extra):
        """An environment in which Main's state dir cannot be found: no TP_STATE_DIR, not in tmux, and a tmux that fails if asked."""
        broken = self.root / "no-tmux-bin"
        broken.mkdir(exist_ok=True)
        (broken / "tmux").write_text("#!/bin/sh\nexit 1\n")
        (broken / "tmux").chmod(0o755)
        env = {k: v for k, v in self.env.items() if k not in ("TP_STATE_DIR", "TMUX", "TMUX_PANE")}
        env["PATH"] = str(broken) + os.pathsep + env["PATH"]
        return {**env, **extra}

    def test_f1_the_project_is_found_from_a_state_dir_that_is_under_sessions(self):
        """tp makes <project>/.tmux-pi/sessions/<id>: the project is three levels up, not the directory above."""
        sessions = self.repo / ".tmux-pi" / "sessions" / "0123abcd"
        sessions.mkdir(parents=True)
        rdir, info = self.prepare()
        for layout in (sessions, self.repo / ".tmux-pi"):  # the session-id layout and the flat .tmux-pi layout
            with self.subTest(state=str(layout)):
                env = self.without_main_state()
                listed = self.tp_trial("status", "--state-dir", str(layout), env=env)
                self.assertIn("run=" + info["run"], listed.stdout, listed.stderr)
                one = json.loads(self.tp_trial("status", info["run"], "--state-dir", str(layout), "--json", env=env).stdout)
                self.assertEqual(one["source_task"], "src-1")
        env = self.without_main_state(TP_STATE_DIR=str(sessions))
        self.assertIn("run=" + info["run"], self.tp_trial("status", env=env).stdout, "TP_STATE_DIR alone is enough")
        stopped = self.tp_trial("stop", info["run"], "--state-dir", str(sessions), "--json", env=self.without_main_state())
        self.assertEqual(json.loads(stopped.stdout)["phase"], "done", stopped.stderr)
        self.assertEqual(self.tp_trial("normal", info["run"], "--state-dir", str(sessions), env=self.without_main_state()).returncode, 0)
        self.assertTrue(any(r["arm"] == "normal" for r in self.records()))

    def test_f1_a_state_dir_of_another_shape_asks_for_the_project(self):
        odd = self.root / "elsewhere" / "state"
        odd.mkdir(parents=True)
        run = self.tp_trial("status", "--state-dir", str(odd), env=self.without_main_state())
        self.assertEqual(run.returncode, 1)
        self.assertIn("give --project", run.stderr)

    def test_f5_status_stop_and_normal_work_with_the_project_alone(self):
        rdir, info = self.prepare()
        env = self.without_main_state()
        self.assertIn("run=" + info["run"], self.tp_trial("status", "--project", str(self.repo), env=env).stdout)
        no_project = self.tp_trial("status", env=env)
        self.assertEqual(no_project.returncode, 1)
        self.assertIn("no tmux-pi state directory", no_project.stderr)
        self.assertEqual(self.tp_trial("normal", info["run"], "--project", str(self.repo), env=env).returncode, 0)
        stopped = self.tp_trial("stop", info["run"], "--project", str(self.repo), "--json", env=env)
        self.assertEqual(json.loads(stopped.stdout)["phase"], "done", stopped.stderr)

    def test_f5_shadow_refuses_a_timeout_that_is_not_positive(self):
        for flag in ("--timeout-s", "--review-timeout-s", "--poll-s"):
            for value in ("0", "-3", "abc"):
                with self.subTest(flag=flag, value=value):
                    run = self.tp_trial("shadow", "src-1", flag, value)
                    self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                    self.assertIn(flag, run.stderr)
        self.assertFalse((self.repo / ".tmux-pi" / "trial").exists())

    def test_an_unknown_subcommand_runs_bin_tp_trial_x_when_that_file_exists(self):
        tree = self.root / "tree"
        (tree / "bin").mkdir(parents=True)
        shutil.copy(str(ROOT / "bin" / "tp-trial"), str(tree / "bin" / "tp-trial"))
        shutil.copytree(str(ROOT / "lib"), str(tree / "lib"))
        plugin = tree / "bin" / "tp-trial-hello"
        plugin.write_text('#!/bin/sh\necho "plugin got: $*"\n')
        plugin.chmod(0o755)
        run = self.tp_trial("hello", "a", "--b", exe=tree / "bin" / "tp-trial")
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "plugin got: a --b"), run.stderr)
        missing = self.tp_trial("nothere", exe=tree / "bin" / "tp-trial")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("invalid choice", missing.stderr)
        plain = plugin.with_name("tp-trial-notexec")
        plain.write_text("#!/bin/sh\necho x\n")
        self.assertNotEqual(self.tp_trial("notexec", exe=tree / "bin" / "tp-trial").returncode, 0)
        self.assertNotEqual(self.tp_trial("../bin/tp-trial-hello", exe=tree / "bin" / "tp-trial").returncode, 0)

# ------------------------------------------------------------------------------------------------ the folded-in pi request

class _Chat(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(body)
        n = len(self.server.requests)
        base = {"id": "c{}".format(n), "object": "chat.completion.chunk", "created": 1, "model": body.get("model")}
        usage = {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}
        if n == 1:  # the first turn asks to read a file, so pi sends a second request carrying the tool result
            call = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "read", "arguments": json.dumps({"path": "x.txt"})}}
            chunks = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "tool_calls": [call]}, "finish_reason": None}]),
                      dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}], usage=usage)]
        else:
            chunks = [dict(base, choices=[{"index": 0, "delta": {"role": "assistant", "content": "Done."}, "finish_reason": None}]),
                      dict(base, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}], usage=usage)]
        data = ("".join("data: {}\n\n".format(json.dumps(c)) for c in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@unittest.skipUnless(shutil.which("pi"), "pi not installed")
class ExplicitQwenRequestTests(unittest.TestCase):
    """An explicit-Qwen run (--model lmstudio/qwen/... --thinking medium): the request pi sends for the first turn and the request it
    sends after the tool result both carry reasoning_effort "medium"; none carries "none", which belongs to the follow-up judge
    (design §8) and to --thinking off. A stub server answers; nothing leaves the machine."""

    def run_pi(self, thinking):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        server = http.server.HTTPServer(("127.0.0.1", 0), _Chat)
        server.requests = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        agent = Path(temp.name) / "agent"
        agent.mkdir()
        models = json.loads((ROOT / "agent" / "models.json").read_text())
        models["providers"]["lmstudio"]["baseUrl"] = "http://127.0.0.1:{}/v1".format(server.server_address[1])
        (agent / "models.json").write_text(json.dumps(models))
        work = Path(temp.name) / "work"
        work.mkdir()
        (work / "x.txt").write_text("hello\n")
        env = {**os.environ, "PI_CODING_AGENT_DIR": str(agent)}
        run = subprocess.run(["pi", "-p", "--no-skills", "--no-context-files", "--no-extensions", "--no-session", "--tools", "read", "--model",
                              "lmstudio/qwen/qwen3.6-35b-a3b", "--thinking", thinking, "Read x.txt"],
                             cwd=str(work), env=env, text=True, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return server.requests

    def test_the_first_run_and_its_follow_up_request_never_carry_reasoning_effort_none(self):
        requests = self.run_pi("medium")
        self.assertEqual(len(requests), 2, "the first turn and the follow-up after the tool result")
        self.assertEqual([r["messages"][-1]["role"] for r in requests], ["user", "tool"])
        for request in requests:
            self.assertEqual(request["reasoning_effort"], "medium")
            self.assertEqual(request["max_completion_tokens"], 32768)
            self.assertEqual(request["model"], "qwen/qwen3.6-35b-a3b")

    def test_the_check_can_fail_thinking_off_does_send_none(self):
        requests = self.run_pi("off")
        self.assertEqual([r["reasoning_effort"] for r in requests], ["none", "none"])

# ------------------------------------------------------------------------------------------------------------------ full run

class FullRunTests(ShadowCase):
    """The real tp-spawn, tp-agent and tp-review against a real lane; only `new-window` (the worker launch) is stubbed."""

    def lanes_listing(self):
        """`tp-agent lanes` of the fixture repository. It lists the lanes of the repository it runs in, so it runs there; run from
        the test process's own directory it would compare the real repository's lanes, which other work changes at any time."""
        run = subprocess.run([str(ROOT / "bin" / "tp-agent"), "lanes"], env=self.env, cwd=str(self.repo), text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return [{k: v for k, v in row.items() if k != "age"} for row in json.loads(run.stdout)]  # "age" is the clock, not the lane

    def test_a_full_shadow_run_changes_nothing_of_the_real_lane_and_cleans_up(self):
        item, source = self.lane_ok(name="alpha", tests="affected")
        self.settle(source)
        # the source's own record: a normal coder's work in its lane
        self.source_id = item["id"]
        lane_tree = Path(json.loads((source / "meta.json").read_text())["workdir"])
        (lane_tree / "lib" / "x.py").write_text("changed by the normal coder\n")
        branch_before = self.refs("refs/heads")
        lane_before = self.lanes_listing()
        self.assertEqual([row["name"] for row in lane_before], ["alpha"], "the listing is the fixture repository's own, with the lane under test")
        main_before = self.snapshot(self.state)
        sessions_before = self.session_names()
        worktrees_before = self.worktrees()
        tip_before = git(self.repo, "rev-parse", "lane/alpha")

        info = ts.prepare(self.state, item["id"], model=MODEL, thinking="medium", env=self.env, timeout_s=900, review_timeout_s=900)
        rdir = trial_session.run_dir(info["project"], info["run"])
        seen = {}

        def poll(n, run):
            for task in sorted((run.state / "tasks").iterdir()):
                if (task / "state.json").exists():
                    continue
                if (task / "review-of").exists():
                    seen["review_packet"] = (task / "packet.md").read_text()
                    self.settle(task, "waiting", "RESULT_STATUS: complete\nAPPROVED\n")
                else:
                    # the fixture's stub new-window names a pane that does not exist; a real tp-spawn leaves a pane in the run's own session
                    meta_now = json.loads((task / "meta.json").read_text())
                    meta_now["pane"] = self.session_pane(rdir, "sleep 300")
                    (task / "meta.json").write_text(json.dumps(meta_now))
                    run.owner.record_panes(self.env)
                    seen["spawned_meta"] = json.loads((task / "meta.json").read_text())
                    seen["run_sh"] = (task / "run.sh").read_text()
                    seen["packet"] = (task / "packet.md").read_text()
                    seen["request_md"] = (task / "request.md").read_text()
                    self.edit(run)
                    self.settle(task)

        run = ts.Run(rdir, self.env, sleep=lambda s: poll(0, run), clock=Clock())
        done = run.execute()

        self.assertEqual(done["outcome"], "completed", done)
        # what the shadow worker was given: the same request, the explicit model, the worktree, the trial's env
        meta = seen["spawned_meta"]
        self.assertEqual((meta["model"], meta["thinking"], meta["worker"]), (MODEL, "medium", "pi"))
        self.assertNotIn("route_info", meta, "an explicit model is never routed or escalated")
        self.assertEqual(os.path.realpath(meta["workdir"]), os.path.realpath(self.tree(rdir)))
        self.assertNotIn("lane", meta)
        self.assertIn("export TP_TRIAL_ID={}".format(info["run"]), seen["run_sh"])
        self.assertIn("export TP_DOCS_TOOLS=1", seen["run_sh"])
        self.assertIn("Make a focused change", seen["packet"])
        self.assertIn(ts.TRIAL_NOTE, seen["packet"])
        # Phase 0: the worker is told, in the request and in the packet, which tree is its own
        told = ts.workdir_text(os.path.realpath(self.tree(rdir)), os.path.realpath(self.repo))
        self.assertIn(told, seen["packet"])
        self.assertTrue(seen["request_md"].startswith(told))
        # (c) the review saw the diff of the soft-reset worktree
        self.assertIn("lib/new.py", seen["review_packet"])
        self.assertIn("lib/x.py", seen["review_packet"])
        self.assertEqual(done["diff_files"], 2)
        # (b) Main's state dir is untouched, (f) so are the lane, its branch, every branch and the lanes listing
        self.assertEqual(self.snapshot(self.state), main_before)
        self.assertEqual(self.refs("refs/heads"), branch_before)
        self.assertEqual(git(self.repo, "rev-parse", "lane/alpha"), tip_before)
        self.assertEqual(self.lanes_listing(), lane_before)
        # (g) the worktree and shadow refs are gone, the session ended
        self.assertFalse((self.tree(rdir)).exists())
        self.assertEqual(self.worktrees(), worktrees_before)
        self.assertEqual(self.refs("refs/tmux-pi"), "")
        self.assertEqual(self.session_names(), sessions_before)
        self.assertEqual(done["cleanup_errors"], [])
        # the pair
        arms = {r["arm"]: r for r in self.records()}
        self.assertEqual(sorted(arms), ["normal", "qwen"])
        self.assertEqual(arms["qwen"]["pair_id"], arms["normal"]["pair_id"])
        self.assertEqual(arms["qwen"]["task_id"], info["task_id"])
        self.assertEqual(arms["normal"]["task_id"], item["id"])


if __name__ == "__main__":
    unittest.main()
