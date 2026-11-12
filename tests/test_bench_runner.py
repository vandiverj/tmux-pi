import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_fixture as fx  # noqa: E402,I001
from bench import runner, shell as shell_mod  # noqa: E402

DUMMY = {key: f"LEAK-{key}" for key in shell_mod.SESSION_KEYS}
ON_PURPOSE = ("TP_SESSION", "TP_STATE_DIR")  # the only session keys the runner sets, for its own session


def leaked(calls, session):
    """Every (program, key, value) in a recorded call whose session key the runner did not set on purpose."""
    bad = []
    for argv, env, _ in calls:
        for key in shell_mod.SESSION_KEYS:
            if key not in env:
                continue
            purposeful = key in ON_PURPOSE and "LEAK" not in env[key] and (key != "TP_SESSION" or env[key] == session)
            if not purposeful:
                bad.append((Path(argv[0]).name, key, env[key]))
    return bad


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.bench, self.ans, self.specs = fx.make_bench(self.root)
        self.session = "bench-ctl-1-abc123"
        patch = mock.patch.dict(os.environ, DUMMY)  # the owner's pane: a leak would show these values
        patch.start()
        self.addCleanup(patch.stop)

    def build(self, **shell_args):
        shell = fx.FakeShell(self.root, **shell_args)
        work, clock = fx.make_runner(self.root, shell, self.bench, self.ans)
        return shell, work, clock

    def only(self, *ids):
        return [s for s in self.specs if s["id"] in ids]

    def assert_cleaned(self, shell):
        self.assertEqual(shell.sessions, {})
        self.assertEqual(list((self.root / "work").iterdir()), [])


class ParityAndCleanup(RunnerCase):
    def test_full_run_sends_no_session_key_the_runner_did_not_set(self):
        shell, work, _ = self.build()
        paths = work.run(self.specs, 1)
        self.assertEqual(leaked(shell.calls, self.session), [])
        programs = {name for name, _ in shell.names()}
        self.assertTrue({"tmux", "git", "tp", "tp-agent", "tp-exit", "vitest"} <= programs)  # start, every task, stop all ran
        exit_call = next(c for c in shell.calls if Path(c[0][0]).name == "tp-exit")
        self.assertEqual(exit_call[1]["TP_SESSION"], self.session)
        self.assertEqual(exit_call[1]["TP_STATE_DIR"], str(self.root / "state" / self.session))
        self.assertNotIn("TMUX", exit_call[1])
        record = json.loads(paths[0].read_text())
        self.assertEqual(record["session"], self.session)
        self.assertRegex(record["session"], fx.SESSION_RE)
        self.assertIsNone(record["stop_refused"])
        self.assertEqual({t["id"]: t["status"] for t in record["tasks"]}, {"code-f": "pass", "local-q": "pass", "review-r": "scored"})
        self.assertEqual(record["tokens"]["cache_read"], None)  # unavailable is null, never 0
        self.assert_cleaned(shell)

    def test_the_parity_check_catches_a_missing_scrub(self):
        shell, work, _ = self.build()
        with mock.patch.object(shell_mod, "child_env", lambda base, extra=None: {**base, **(extra or {})}):
            work.run(self.only("local-q"), 1)
        bad = leaked(shell.calls, self.session)
        self.assertIn(("tp-exit", "TMUX", "LEAK-TMUX"), bad)
        self.assertIn(("tp", "TMUX_PANE", "LEAK-TMUX_PANE"), bad)

    def test_a_task_over_its_budget_fails_and_the_workspace_and_copy_are_still_removed(self):
        shell, work, _ = self.build(running=True)
        paths = work.run(self.only("local-q", "code-f"), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual({t["status"] for t in record["tasks"]}, {"timeout"})
        self.assertIn(("tp-agent", "stop"), shell.names())
        self.assert_cleaned(shell)

    def test_an_exception_mid_run_still_removes_the_workspace_and_copy(self):
        shell, work, _ = self.build()
        work.score_worker = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            work.run(self.only("local-q"), 1)
        self.assert_cleaned(shell)


class SessionGuard(RunnerCase):
    def test_refuses_to_start_in_an_existing_session_and_leaves_it_alone(self):
        shell, work, _ = self.build(sessions={self.session: "/owner/state"})
        with self.assertRaises(runner.RunnerError) as caught:
            work.run(self.only("local-q"), 1)
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(shell.sessions, {self.session: "/owner/state"})
        names = shell.names()
        self.assertNotIn(("tp", str(self.root / "work" / self.session / "project")), names)
        self.assertFalse([n for n, sub in names if n == "tp-exit" or sub == "kill-session"])
        self.assertEqual(list((self.root / "work").iterdir()), [])

    def test_refuses_to_stop_a_session_whose_state_dir_is_not_the_one_it_started(self):
        def swap(shell):  # another workspace takes the name over mid-run
            shell.sessions[self.session] = "/owner/state"
        shell, work, _ = self.build(on_inspect=swap)
        paths = work.run(self.only("local-q"), 1)
        record = json.loads(paths[0].read_text())
        self.assertIn("refused to stop", record["stop_refused"])
        self.assertEqual(shell.sessions, {self.session: "/owner/state"})
        self.assertFalse([n for n, sub in shell.names() if n == "tp-exit" or sub == "kill-session"])

    def test_a_session_the_runner_does_not_own_survives_every_stop_path(self):
        OWNER = "/owner/state"

        def exit_fails(shell, env):  # tp-exit fails and the session is taken over before the fallback
            shell.sessions[env["TP_SESSION"]] = OWNER
            return fx.Result(1, "", "boom")

        def exit_ok_but_stays(shell, env):  # tp-exit "succeeds" yet the name now belongs to another workspace
            shell.sessions[env["TP_SESSION"]] = OWNER
            return fx.OK

        def exit_ok_name_reused(shell, env):  # ours is gone and somebody reuses the name at once
            shell.sessions.pop(env["TP_SESSION"], None)
            shell.sessions[env["TP_SESSION"]] = OWNER
            return fx.OK

        def exit_fails_still_ours(shell, env):
            return fx.Result(1, "", "boom")

        rows = [("tp-exit fails, taken over", exit_fails, True), ("tp-exit ok, still there, taken over", exit_ok_but_stays, True),
                ("tp-exit ok, name reused", exit_ok_name_reused, True), ("tp-exit fails, still ours", exit_fails_still_ours, False)]
        for name, on_exit, survives in rows:
            with self.subTest(name):
                shell, work, _ = self.build(on_exit=on_exit)
                paths = work.run(self.only("local-q"), 1)
                record = json.loads(paths[0].read_text())
                kills = [c for c in shell.calls if c[0][1:2] == ["kill-session"]]
                if survives:
                    self.assertEqual(shell.sessions, {record["session"]: OWNER})
                    self.assertEqual(kills, [])
                    self.assertIn("refused to stop", record["stop_refused"])
                else:  # ours: the fallback kill is allowed and ends it
                    self.assertEqual(len(kills), 1)
                    self.assertEqual(shell.sessions, {})
                    self.assertIsNone(record["stop_refused"])

    def test_session_names_are_unique_per_repeat_and_keep_the_prefix(self):
        names = iter(["aaa111", "bbb222"])
        shell = fx.FakeShell(self.root)
        work, _ = fx.make_runner(self.root, shell, self.bench, self.ans)
        work.token = lambda: next(names)
        paths = work.run(self.only("local-q"), 2)
        self.assertEqual([json.loads(p.read_text())["session"] for p in paths], ["bench-ctl-1-aaa111", "bench-ctl-2-bbb222"])


OWNER = "/owner/state"


def tmux_down(flavor):
    """inject() for a tmux that, once `shell.down` is set, answers the probe's calls in one of the ways that prove nothing."""
    def inject(shell, argv):
        if not getattr(shell, "down", False) or Path(argv[0]).name != "tmux":
            return None
        if flavor == "exit-1" and argv[1] == "list-sessions":
            return fx.Result(1, "", "no server running on /tmp/tmux-501/default")
        if flavor == "has-session-message" and argv[1] == "list-sessions":
            return fx.Result(1, "", "can't find session: bench-ctl-1-abc123")
        if flavor == "timeout" and argv[1] == "list-sessions":
            return fx.Result(124, "", "")
        if flavor == "exception" and argv[1] == "list-sessions":
            raise OSError("tmux vanished")
        if flavor == "show-options" and argv[1] == "show-options":
            return fx.Result(1, "", "server exited unexpectedly")
        return None
    return inject


class SessionProof(RunnerCase):
    """Absence is proven, never inferred from a failed call: every probe outcome x every stop path says what is stopped, what is
    removed and what is recorded. Paths: normal (tasks finish), timeout (the task runs over budget), error (an exception mid run)."""
    UNKNOWN = ("exit-1", "has-session-message", "timeout", "exception", "show-options")

    def drive(self, path, outcome):
        flavor = outcome if outcome in self.UNKNOWN else None

        def arrange(shell):  # the world changes once the first task is being watched
            if outcome == "foreign":
                shell.sessions[self.session] = OWNER
            elif outcome == "gone":
                shell.sessions.pop(self.session, None)
            elif flavor:
                shell.down = True
        shell, work, _ = self.build(running=path == "timeout", on_inspect=arrange, inject=tmux_down(flavor) if flavor else None)
        results = self.root / "results" / "ctl"
        if path == "error":
            work.score_worker = mock.Mock(side_effect=RuntimeError("boom"))
            with self.assertRaises(RuntimeError):
                work.run(self.only("local-q"), 1)
            incident = results / "1.cleanup.json"
            return shell, (json.loads(incident.read_text()) if incident.exists() else None)
        return shell, json.loads(work.run(self.only("local-q"), 1)[0].read_text())

    def test_every_probe_outcome_on_every_stop_path(self):
        rows = {  # outcome: (stop, session survives, copy kept)
            "ours": ("stopped", False, False), "foreign": ("refused", True, False), "gone": ("refused", False, False),
            **{flavor: ("unknown", True, True) for flavor in self.UNKNOWN},
        }
        for outcome, (stop, survives, kept) in rows.items():
            for path in ("normal", "timeout", "error"):
                shutil.rmtree(self.root / "results", ignore_errors=True)  # every row is repeat 1 of a fresh results dir
                with self.subTest(outcome=outcome, path=path):
                    shell, data = self.drive(path, outcome)
                    destructive = [n for n, sub in shell.names() if n == "tp-exit" or sub == "kill-session"]
                    if stop == "stopped":
                        self.assertEqual(destructive, ["tp-exit"])
                    else:
                        self.assertEqual(destructive, [], "a session whose state is not proven ours must not be stopped")
                    self.assertEqual(bool(shell.sessions), survives)
                    work_dirs = list((self.root / "work").iterdir())
                    self.assertEqual(bool(work_dirs), kept, "the copy and work dir stay only when the session is unknown")
                    if path == "error" and stop == "stopped":
                        self.assertIsNone(data)  # nothing left over: no incident file
                        continue
                    self.assertEqual((data["stop"], data["cleanup"]), (stop, "incomplete" if kept else "complete"))
                    self.assertEqual(bool(data["stop_refused"]), stop != "stopped")
                    if kept:
                        self.assertEqual(data["leftover"]["work_dir"], str(work_dirs[0]))
                        self.assertTrue(Path(data["leftover"]["copy"]).exists())
                    if outcome == "gone":
                        self.assertIn("already gone", data["stop_refused"])
                    if stop == "stopped":
                        self.assertIsNone(data["stop_refused"])
                shutil.rmtree(self.root / "work", ignore_errors=True)
                (self.root / "work").mkdir()
                shutil.rmtree(self.root / "results", ignore_errors=True)

    def test_unknown_or_unremoved_after_our_own_action_keeps_the_copy(self):
        def exit_then_down(shell, env):  # tp-exit "ran" and then tmux stops answering
            shell.down = True
            return fx.OK

        def stuck(shell, argv):  # neither tp-exit nor kill-session ends it
            if argv[1:2] == ["kill-session"]:
                return fx.OK
            return None
        rows = [("tp-exit ran, then tmux unprovable", dict(on_exit=exit_then_down, inject=tmux_down("exit-1"))),
                ("session survives both stops", dict(on_exit=lambda s, e: fx.OK, inject=stuck))]
        for name, args in rows:
            with self.subTest(name):
                shell, work, _ = self.build(**args)
                data = json.loads(work.run(self.only("local-q"), 1)[0].read_text())
                self.assertEqual((data["stop"], data["cleanup"]), ("unknown", "incomplete"))
                self.assertTrue(data["stop_refused"])
                self.assertEqual([n for n, sub in shell.names() if n == "tp-exit" or sub == "kill-session"][:1], ["tp-exit"])
                self.assertEqual(len(list((self.root / "work").iterdir())), 1)
                shutil.rmtree(self.root / "work", ignore_errors=True)
                (self.root / "work").mkdir()
                shutil.rmtree(self.root / "results", ignore_errors=True)

    def test_a_tmux_that_cannot_answer_at_start_refuses_to_start(self):
        shell, work, _ = self.build(inject=lambda s, argv: fx.Result(1, "", "no server") if argv[1:2] == ["list-sessions"] else None)
        with self.assertRaises(runner.RunnerError) as caught:
            work.run(self.only("local-q"), 1)
        self.assertIn("cannot tell", str(caught.exception))
        self.assertNotIn("tp", [n for n, _ in shell.names()])


class NothingIsOverwritten(RunnerCase):
    """F1: every file the bench writes under results/<label>/ is created, never replaced; a number is chosen across all record kinds.
    Record kinds: run record (<n>.json), incident file (<n>.cleanup.json), artifacts dir (<n>/artifacts/)."""

    def attempt(self, how):
        """One run of label ctl. "ok": finishes (run record + artifacts). "fail": raises mid run while tmux cannot prove the session
        gone (incident file, and the claimed <n>/ dir). Each attempt has its own shell, as a retry from the command line does."""
        def arrange(shell):
            shell.down = True
        if how == "ok":
            shell, work, _ = self.build()
            work.run(self.only("local-q"), 1)
        else:  # "stopped": raises, but the session is proven gone: no incident file, only the claimed <n>/ dir remains
            shell, work, _ = self.build(**({} if how == "stopped" else dict(on_inspect=arrange, inject=tmux_down("exit-1"))))
            work.score_worker = mock.Mock(side_effect=RuntimeError("boom"))
            with self.assertRaises(RuntimeError):
                work.run(self.only("local-q"), 1)

    def snapshot(self):
        folder = self.root / "results" / "ctl"
        return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() else None) for p in folder.rglob("*")} if folder.is_dir() else {}

    def test_every_record_kind_for_each_first_and_second_attempt(self):
        expect_after = {"ok": {"1.json", "1/artifacts"}, "fail": {"1.cleanup.json", "1"}, "stopped": {"1"}}  # what attempt 1 leaves
        for first, second in (("ok", "ok"), ("fail", "fail"), ("fail", "ok"), ("ok", "fail"), ("stopped", "ok"), ("stopped", "fail")):
            with self.subTest(first=first, second=second):
                self.attempt(first)
                earlier = self.snapshot()
                self.assertLessEqual(expect_after[first], set(earlier), "the first attempt leaves its record kinds")
                self.attempt(second)
                later = self.snapshot()
                for name, content in earlier.items():
                    self.assertIn(name, later, f"{name} was removed")
                    self.assertEqual(later[name], content, f"{name} was rewritten")
                self.assertTrue(expect_after[second] and {n.replace("1", "2") for n in expect_after[second]} <= set(later), set(later))
                shutil.rmtree(self.root / "work", ignore_errors=True)
                (self.root / "work").mkdir()
                shutil.rmtree(self.root / "results", ignore_errors=True)


class NoResultIgnored(RunnerCase):
    """F2: a subprocess result is never dropped. Every argv kind the runner issues is made to fail, and each failure must be raised
    (ShellError) or opted out of with a reason; the run then either raises or records the damage."""

    def test_each_argv_kind_failing_is_raised_or_explicitly_tolerated(self):
        rows = {  # kind: how the run ends
            "git": "raises", "tmux": "raises", "tp": "raises", "tp-agent": "errors every task", "tp-exit": "stops by kill-session",
        }
        for kind, ends in rows.items():
            with self.subTest(kind):
                def fail_kind(shell, argv, kind=kind):
                    return fx.Result(1, "", f"injected {kind} failure") if Path(argv[0]).name == kind else None
                shell, work, _ = self.build(inject=fail_kind)
                if ends == "raises":
                    with self.assertRaises((runner.RunnerError, shell_mod.ShellError, runner.project.ProjectError)):
                        work.run(self.only("local-q"), 1)
                else:
                    data = json.loads(work.run(self.only("local-q"), 1)[0].read_text())
                    if kind == "tp-agent":
                        self.assertEqual([t["status"] for t in data["tasks"]], ["error"])
                    else:
                        self.assertEqual(data["stop"], "stopped")
                self.assertTrue(shell.failed, kind)
                reasons = {tuple(argv): why for argv, why in shell.tolerated}
                for argv in shell.failed:
                    self.assertTrue(argv in shell.raised or reasons.get(tuple(argv)), f"ignored: {argv}")
                shutil.rmtree(self.root / "work", ignore_errors=True)
                (self.root / "work").mkdir()
                shutil.rmtree(self.root / "results", ignore_errors=True)

    def test_a_tolerated_failure_that_feeds_a_record_field_is_null_there(self):
        # vitest (tolerated: its exit code is the score) crashing or timing out gives no test counts: tests is null, the task fails
        for code, stdout in ((1, ""), (124, ""), (1, "not json")):
            with self.subTest(exit=code, stdout=stdout):
                shell, work, _ = self.build(inject=lambda s, argv: fx.Result(code, stdout, "crash") if Path(argv[0]).name == "vitest" else None)
                task = json.loads(work.run(self.only("code-f"), 1)[0].read_text())["tasks"][0]
                self.assertEqual((task["status"], task["detail"]["tests"]), ("fail", None))
                shutil.rmtree(self.root / "work", ignore_errors=True)
                (self.root / "work").mkdir()
                shutil.rmtree(self.root / "results", ignore_errors=True)

    def test_a_failed_reset_stops_dispatch_and_marks_the_rest_not_run(self):
        # (name, which reset call is broken, injected answer); the first reset passes in the "second task" rows
        def git_sub(argv):
            return argv[argv.index("-C") + 2:] if "-C" in argv else argv[1:]
        broken = {
            "reset exits 1": lambda sub: fx.Result(1, "", "cannot lock ref") if sub[:1] == ["reset"] else None,
            "clean exits 1": lambda sub: fx.Result(1, "", "permission denied") if sub[:1] == ["clean"] else None,
            "HEAD is not the tag": lambda sub: fx.Result(0, "other\n", "") if sub[:2] == ["rev-parse", "HEAD^{commit}"] else None,
            "tree is dirty": lambda sub: fx.Result(0, " M src/game/f.ts\n?? stray.txt\n", "") if sub[:1] == ["status"] else None,
        }
        for name, answer in broken.items():
            for first_ok in (False, True):
                with self.subTest(name, first_task_resets_fine=first_ok):
                    resets = []

                    def inject(shell, argv, answer=answer):
                        sub = git_sub(argv)
                        if Path(argv[0]).name != "git":
                            return None
                        if sub[:1] == ["reset"]:
                            resets.append(1)
                        if len(resets) != (2 if first_ok else 1):
                            return None  # only this one task's reset breaks; a later reset would pass, so the tasks after it must be held back by the mark
                        return answer(sub)
                    shell, work, _ = self.build(inject=inject)
                    data = json.loads(work.run(self.specs, 1)[0].read_text())
                    states = {t["id"]: (t["status"], t["detail"].get("error")) for t in data["tasks"]}
                    ran = ["code-f"] if first_ok else []
                    for task_id in ("code-f", "local-q", "review-r"):
                        if task_id in ran:
                            self.assertEqual(states[task_id][0], "pass")
                        else:
                            self.assertEqual(states[task_id], ("not_run", "reset-failed"), task_id)
                    self.assertEqual(len(shell.dispatched), len(ran))
                    self.assertEqual(data["metrics"]["local_exact"], 0.0)  # still in the denominator
                    self.assertEqual(data["metrics"]["review_catch"], 0.0)
                    shutil.rmtree(self.root / "work", ignore_errors=True)
                    (self.root / "work").mkdir()
                    shutil.rmtree(self.root / "results", ignore_errors=True)

    def test_the_workspace_own_paths_are_allowed_to_be_dirty_after_a_reset(self):
        status = " M node_modules/x\n?? .tmux-pi/tasks/t1/\n?? node_modules\n"
        shell, work, _ = self.build(inject=lambda s, argv: fx.Result(0, status, "") if argv[-2:] == ["status", "--porcelain"] else None)
        data = json.loads(work.run(self.only("local-q"), 1)[0].read_text())
        self.assertEqual(data["tasks"][0]["status"], "pass")


class Evidence(RunnerCase):
    def test_every_path_a_completed_record_names_exists_after_the_copy_is_removed(self):
        shell, work, _ = self.build()
        record = json.loads(work.run(self.specs, 1)[0].read_text())
        self.assert_cleaned(shell)
        self.assertTrue(all(p for t in record["tasks"] for p in t["task_dirs"]))
        named = [p for t in record["tasks"] for p in (*t["task_dirs"], *t["transcripts"]) if p]
        for path in named:
            self.assertTrue(Path(path).exists(), path)
            self.assertIn(str(self.root / "results"), path)  # a copy, not the workspace's own path
        self.assertEqual([p for p in (self.root / "results").rglob(".env*")], [])  # secrets are never copied
        self.assertEqual(len(list((self.root / "results").rglob("transcript.jsonl"))), 3)

    def test_budget_is_checked_on_every_observation(self):
        # {terminal, running} x {seen before, seen after the 100 s budget}
        rows = [("terminal", 0, "pass"), ("terminal", 150, "timeout"), ("running", 0, "pass"), ("running", 150, "timeout")]
        for kind, late, expect in rows:
            with self.subTest(kind=kind, late=late):
                clock_box = []
                shell = fx.FakeShell(self.root, running=kind == "running", settle_after=3 if late == 0 else None,
                                     on_inspect=lambda s: clock_box[0].__setattr__("now", clock_box[0].now + late) if s.inspects == 0 else None)
                work, clock = fx.make_runner(self.root, shell, self.bench, self.ans)
                clock_box.append(clock)
                record = json.loads(work.run(self.only("local-q"), 1)[0].read_text())
                self.assertEqual(record["tasks"][0]["status"], expect)
                if kind == "terminal" and late:
                    self.assertNotIn(("tp-agent", "stop"), shell.names())  # it had finished: collected, not stopped
                    self.assertIn(("tp-agent", "collect"), shell.names())

    def test_a_result_that_is_not_utf8_or_is_empty_or_missing_is_scored_and_the_run_goes_on(self):
        # result.md as written by a worker x the two readers of it (local answer, review findings)
        good, find = f"ANSWER: {fx.EXPECTED}\n".encode(), fx.FINDINGS.encode()
        rows = [("local-q", good, "pass"), ("local-q", b"caf\xe9\n" + good, "pass"), ("local-q", b"\x80\xff\n" + good, "pass"),
                ("local-q", b"", "fail"), ("local-q", fx.MISSING, "fail"),
                ("review-r", find, "scored"), ("review-r", b"caf\xe9\n" + find, "scored"), ("review-r", b"\x80\n" + find, "scored"),
                ("review-r", b"", "scored"), ("review-r", fx.MISSING, "scored")]
        for task, data, expect in rows:
            with self.subTest(task=task, data=data):
                shell, work, _ = self.build(result_bytes=data)
                record = json.loads(work.run(self.only(task), 1)[0].read_text())
                self.assertEqual(record["tasks"][0]["status"], expect)

    def test_a_review_that_times_out_or_errors_still_counts_its_planted_defects(self):
        shell, work, _ = self.build(running=True)
        metrics = json.loads(work.run(self.only("review-r"), 1)[0].read_text())["metrics"]
        self.assertEqual(metrics["review_catch"], 0.0)  # 0 of 1 planted, not "nothing to measure"


class TrustDialog(RunnerCase):
    def test_a_worker_at_the_trust_dialog_is_marked_error_and_the_dialog_is_never_answered(self):
        """Three ways the bench learns of the dialog: its own capture of the pane (inspect says running), the inspect field alone (tp-agent
        inspect now answers needs_input + trust_dialog {dir}, and needs_input is terminal), and both. All give error: trust-dialog with the
        remediation, and a worker that never finished is stopped, not collected (TV1 round 2: the second row read "fail: worker ended needs_input")."""
        screen = "Do you trust the files in this folder?\n 1. Yes, proceed\n"
        for label, shell_args in (("capture", dict(running=True, pane_text=screen)), ("inspect field", dict(trust_inspect=True)),
                                  ("inspect field and capture", dict(trust_inspect=True, pane_text=screen))):
            with self.subTest(label):
                shell, work, _ = self.build(**shell_args)
                paths = work.run(self.only("local-q", "code-f", "review-r"), 1)
                tasks = json.loads(paths[0].read_text())["tasks"]
                self.assertEqual([(t["status"], t["detail"]["error"]) for t in tasks], [("error", "trust-dialog")] * 3)
                self.assertIn("bin/tp-claude-bootstrap", tasks[0]["detail"]["reason"])
                self.assertNotIn("send-keys", [sub for _, sub in shell.names()])
                self.assertEqual([ident for (program, sub), (argv, _, _) in zip(shell.names(), shell.calls) if (program, sub) == ("tp-agent", "stop")
                                  for ident in argv[2:3]], ["t1", "t2", "review-1"])
                self.assertNotIn(("tp-agent", "collect"), shell.names(), "an unfinished worker is stopped, not collected")
                self.assert_cleaned(shell)


class Arguments(unittest.TestCase):
    def test_env_pairs(self):
        self.assertEqual(runner.parse_env(["TP_X1_ARM=on", "TP_PI_NODE="]), {"TP_X1_ARM": "on", "TP_PI_NODE": ""})
        for bad in ("nokey", "1BAD=x", "TMUX=/tmp/x", "TP_SESSION=other", "TP_STATE_DIR=/x"):
            with self.assertRaises(runner.RunnerError, msg=bad):
                runner.parse_env([bad])

    def test_cost_guard(self):
        runner.check_guard(4, 10, 40)
        with self.assertRaises(runner.RunnerError):
            runner.check_guard(4, 11, 40)


if __name__ == "__main__":
    unittest.main()
