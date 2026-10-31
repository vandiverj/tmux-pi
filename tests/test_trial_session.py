"""lib/trial_session.py (trial lane L3, design §3/§8/§10): the trial's own tmux session and state dir, its environment, its
configuration, and the capacity guard. Real tmux on a private server (-L socket); tp-agent reads task dirs; no model runs."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lane_fixture import MODEL, ROOT, TMUX, TmuxCase  # noqa: E402

sys.path.insert(0, str(ROOT / "lib"))
import trial_session  # noqa: E402

LOCAL_MODEL = "slotstream/stub-qwen"


class PureTests(unittest.TestCase):
    def test_run_ids(self):
        for good in ("a", "sh-x-20261031", "0abc", "a" * 40):
            self.assertEqual(trial_session.valid_run(good), good)
        for bad in ("", "-a", "A", "a_b", "a b", "a/b", "../x", "a" * 41, "a.b", None, 7):
            with self.subTest(run=bad):
                with self.assertRaises(trial_session.SessionError):
                    trial_session.valid_run(bad)

    def test_the_session_name_and_state_dir_follow_the_run(self):
        self.assertEqual(trial_session.session_name("sh-a-1"), "tp-trial-sh-a-1")
        self.assertEqual(trial_session.state_dir("/p", "sh-a-1"), Path("/p/.tmux-pi/trial/sh-a-1/state"))

    def test_env_for_names_the_trial_and_drops_what_points_elsewhere(self):
        base = {"TMUX": "/x,1,0", "TMUX_PANE": "%1", "TP_TASK_ID": "t", "TP_TASK_DIR": "/d", "TP_MAIN_ID": "m", "TP_MAIN_CWD": "/c",
                "TP_WORK_DIR": "/w", "TP_DOCS_TOOLS": "1", "TP_TRIAL_ID": "x", "TP_STATE_DIR": "/main/state", "TP_SESSION": "main", "KEEP": "1"}
        env = trial_session.env_for("/trial/state", "tp-trial-x", base)
        self.assertEqual((env["TP_STATE_DIR"], env["TP_SESSION"], env["KEEP"]), ("/trial/state", "tp-trial-x", "1"))
        for key in ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_WORK_DIR", "TP_DOCS_TOOLS", "TP_TRIAL_ID"):
            self.assertNotIn(key, env)
        self.assertIn("TMUX", base, "the caller's mapping is not changed")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def load(self, value):
        path = self.dir / "o.json"
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return trial_session.load_config(path)

    def test_the_shipped_config_has_the_trial_block(self):
        trial, main = trial_session.load_config()
        self.assertEqual((trial["max_local_workers"], trial["attempt_timeout_s"], trial["model"], trial["thinking"]),
                         (1, 2700, "lmstudio/qwen/qwen3.6-35b-a3b", "medium"))
        self.assertEqual(main, json.loads((ROOT / "config" / "orchestration.json").read_text())["max_local_workers"])

    def test_the_shipped_config_keeps_the_keys_the_other_readers_use(self):
        value = json.loads((ROOT / "config" / "orchestration.json").read_text())
        for key in ("max_local_workers", "max_active_workers", "lanes"):
            self.assertIn(key, value)
        self.assertEqual(value["lanes"]["max_active_lanes"], 16)

    def test_a_missing_block_gives_the_defaults_and_a_partial_one_fills_in(self):
        self.assertEqual(self.load({"max_local_workers": 3})[0], trial_session.TRIAL_DEFAULTS)
        self.assertEqual(self.load({"max_local_workers": 3})[1], 3)
        trial, _ = self.load({"trial": {"attempt_timeout_s": 60}})
        self.assertEqual((trial["attempt_timeout_s"], trial["max_local_workers"]), (60, 1))

    def test_a_malformed_block_is_refused_with_the_key_named(self):
        cases = ({"trial": []}, {"trial": {"bogus": 1}}, {"trial": {"max_local_workers": 0}}, {"trial": {"max_local_workers": True}},
                 {"trial": {"attempt_timeout_s": "x"}}, {"trial": {"model": ""}}, {"trial": {"thinking": "fast"}}, {"max_local_workers": 0}, [], "{bad")
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(trial_session.SessionError):
                    self.load(case)


@unittest.skipUnless(TMUX, "tmux not installed")
class SessionTests(unittest.TestCase):
    """ensure/end against a real private tmux server (the wrapper only adds the -L socket)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.sock = "tp-ts-{}-{}".format(os.getpid(), self.id().split(".")[-1][-18:])
        fake = self.root / "bin"
        fake.mkdir()
        wrapper = fake / "tmux"
        wrapper.write_text('#!/bin/sh\nexec {} -L {} "$@"\n'.format(TMUX, self.sock))
        wrapper.chmod(0o755)
        self.env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        for key in ("TMUX", "TMUX_PANE"):
            self.env.pop(key, None)
        self.addCleanup(self.stop_server)

    def tmux(self, *args, check=True):
        return subprocess.run([TMUX, "-L", self.sock, *args], text=True, capture_output=True, check=check).stdout.strip()

    def stop_server(self):
        try:
            os.kill(int(self.tmux("display-message", "-p", "#{pid}")), signal.SIGKILL)
        except (subprocess.CalledProcessError, ProcessLookupError, ValueError):
            pass

    def sessions(self):
        run = subprocess.run([TMUX, "-L", self.sock, "list-sessions", "-F", "#{session_name}"], text=True, capture_output=True)
        return run.stdout.split() if run.returncode == 0 else []

    def option(self, name, key):
        return self.tmux("show-options", "-qv", "-t", "=" + name + ":", key)

    def test_ensure_makes_the_session_with_its_own_state_dir_and_marks_it(self):
        name, state = trial_session.ensure(str(self.project), "sh-a-1", self.env)
        self.assertEqual(name, "tp-trial-sh-a-1")
        self.assertEqual(state, self.project / ".tmux-pi" / "trial" / "sh-a-1" / "state")
        self.assertTrue((state / "tasks").is_dir())
        self.assertEqual(self.sessions(), [name])
        self.assertEqual(self.option(name, "@tp_state_dir"), str(state))
        self.assertEqual(self.option(name, "@tp_trial"), "sh-a-1")
        self.assertFalse((self.project / ".tmux-pi" / "tasks").exists(), "Main's state dir is not created or used")

    def test_the_docs_tools_switch_does_not_follow_the_callers_environment_into_the_session(self):
        env = {**self.env, "TP_DOCS_TOOLS": "1", "TP_TRIAL_ID": "leak"}
        name, _ = trial_session.ensure(str(self.project), "sh-a-9", env)
        # a fresh server takes its global environment from the first caller, so the global table is where a leak would show
        shown = self.tmux("show-environment", "-g") + self.tmux("show-environment", "-t", name)
        self.assertNotIn("TP_DOCS_TOOLS", shown)
        self.assertNotIn("TP_TRIAL_ID", shown)

    def test_lib_tp_sh_resolves_the_trial_state_from_the_session(self):
        """The reason for a session of its own: tp_session accepts the trial's TP_STATE_DIR only because the session names it."""
        name, state = trial_session.ensure(str(self.project), "sh-a-2", self.env)
        env = trial_session.env_for(state, name, self.env)
        run = subprocess.run(["bash", "-c", 'source "{}/lib/tp.sh"; tp_session'.format(ROOT)], env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout.strip()), (0, name), run.stderr)

    def test_ensure_twice_reuses_the_session(self):
        trial_session.ensure(str(self.project), "sh-a-3", self.env)
        trial_session.ensure(str(self.project), "sh-a-3", self.env)
        self.assertEqual(self.sessions(), ["tp-trial-sh-a-3"])

    def test_ensure_refuses_a_session_that_names_another_state_dir(self):
        self.tmux("new-session", "-d", "-s", "tp-trial-sh-a-4", "sleep 300")
        self.tmux("set-option", "-t", "tp-trial-sh-a-4", "@tp_state_dir", str(self.root / "elsewhere"))
        with self.assertRaises(trial_session.SessionError) as caught:
            trial_session.ensure(str(self.project), "sh-a-4", self.env)
        self.assertIn("not this run's", str(caught.exception))
        self.assertEqual(self.option("tp-trial-sh-a-4", "@tp_state_dir"), str(self.root / "elsewhere"), "it is not taken over")

    def test_ensure_refuses_a_state_dir_that_is_a_link(self):
        base = self.project / ".tmux-pi" / "trial" / "sh-a-5"
        base.mkdir(parents=True)
        (self.root / "real").mkdir()
        (base / "state").symlink_to(self.root / "real")
        with self.assertRaises(trial_session.SessionError):
            trial_session.ensure(str(self.project), "sh-a-5", self.env)
        self.assertEqual(self.sessions(), [])

    def test_end_kills_only_the_named_session_and_is_repeatable(self):
        trial_session.ensure(str(self.project), "sh-a-6", self.env)
        trial_session.ensure(str(self.project), "sh-a-7", self.env)
        self.assertTrue(trial_session.end("sh-a-6", self.env))
        self.assertEqual(self.sessions(), ["tp-trial-sh-a-7"])
        self.assertFalse(trial_session.end("sh-a-6", self.env))
        self.assertEqual(self.sessions(), ["tp-trial-sh-a-7"])

    def test_end_does_not_match_a_longer_name_by_prefix(self):
        trial_session.ensure(str(self.project), "sh-a-8x", self.env)
        self.assertFalse(trial_session.end("sh-a-8", self.env))
        self.assertEqual(self.sessions(), ["tp-trial-sh-a-8x"])


class GuardCase(TmuxCase):
    """The capacity guard against real task directories read by `tp-agent list` (panes are real, new-window is stubbed)."""

    def local_task(self, state_dir, model=LOCAL_MODEL, name=None):
        """A live local pi task in `state_dir`: a task dir, a real pane carrying its run.sh path, status working."""
        task_id = name or "L-{}".format(len(list((state_dir / "tasks").iterdir())) + 1)
        task = state_dir / "tasks" / task_id
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": task_id, "role": "coder", "worker": "pi", "model": model, "thinking": "medium",
                                                    "workdir": str(self.project), "deliverable": "Report", "created": "2026-10-31T10:00:00Z"}))
        pane, _ = self.pane(self.worker_command(task))
        meta = json.loads((task / "meta.json").read_text())
        meta["pane"] = pane
        (task / "meta.json").write_text(json.dumps(meta))
        return task_id

    def main_with(self, count, model=LOCAL_MODEL):
        for _ in range(count):
            self.local_task(self.state, model)

    def guard(self, model=LOCAL_MODEL, trial_max=1, main_max=2):
        trial = dict(trial_session.TRIAL_DEFAULTS, max_local_workers=trial_max)
        return trial_session.guard(self.state, self.project, trial, main_max, model, self.env)


class GuardTests(GuardCase):
    def trial_state(self, run):
        state = trial_session.state_dir(self.project, run)
        (state / "tasks").mkdir(parents=True, exist_ok=True)
        return state

    def test_local_active_counts_running_local_pi_tasks_only(self):
        self.local_task(self.state)
        self.local_task(self.state, model="mlx/other")
        self.local_task(self.state, model=MODEL)  # not a local model
        self.assertEqual(len(trial_session.local_active(self.state, self.env)), 2)

    def test_local_active_of_a_state_dir_with_no_tasks_is_empty(self):
        self.assertEqual(trial_session.local_active(self.root / "nowhere", self.env), [])

    def test_an_unlistable_state_dir_raises_and_is_never_counted_as_zero(self):
        self.local_task(self.state)
        answers = (subprocess.CompletedProcess([], 1, "", "boom"), subprocess.CompletedProcess([], 0, "not json", ""),
                   subprocess.CompletedProcess([], 0, "{}", ""))
        for answer in answers:
            with self.subTest(answer=answer.stdout or answer.returncode):
                with mock.patch.object(trial_session.subprocess, "run", return_value=answer):
                    with self.assertRaises(trial_session.SessionError):
                        trial_session.guard(self.state, self.project, trial_session.TRIAL_DEFAULTS, 2, LOCAL_MODEL, self.env)

    def test_a_local_model_starts_when_main_has_a_free_local_slot(self):
        self.main_with(1)
        self.assertEqual(self.guard(), (True, ""))

    def test_it_refuses_to_start_when_main_has_two_local_workers(self):
        self.main_with(2)
        ok, reason = self.guard()
        self.assertFalse(ok)
        self.assertIn("Main holds 2 of its 2 local worker slots", reason)

    def test_it_refuses_when_the_trial_already_runs_its_own_local_worker(self):
        self.local_task(self.trial_state("sh-x-1"), name="T-1")
        ok, reason = self.guard(trial_max=1)
        self.assertFalse(ok)
        self.assertIn("trial already runs 1 local worker", reason)
        self.assertEqual(self.guard(trial_max=2), (True, ""))

    def make_run(self, name, phase="queued", model=LOCAL_MODEL, pid=None, created="2026-10-31T10:00:00Z"):
        state = self.trial_state(name)
        (state.parent / "run.json").write_text(json.dumps({"run": name, "phase": phase, "model": model, "pid": pid, "created": created}))
        return state

    def test_f2_a_run_that_is_queued_or_starting_holds_a_slot_before_it_has_a_task(self):
        for phase in ("queued", "starting", "worktree", "spawn"):
            with self.subTest(phase=phase):
                self.make_run("sh-q", phase=phase)
                ok, reason = self.guard()
                self.assertFalse(ok)
                self.assertIn("trial already runs 1 local worker(s) (run sh-q)", reason)

    def test_f2_a_run_that_is_done_or_not_local_or_has_no_runner_left_holds_no_slot(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        cases = {"done": dict(phase="done"), "not local": dict(model=MODEL), "runner dead": dict(phase="wait", pid=gone.pid)}
        for label, kw in cases.items():
            with self.subTest(label):
                self.make_run("sh-q", **kw)
                self.assertEqual(self.guard(), (True, ""))

    def test_f2_a_run_with_a_live_runner_holds_a_slot(self):
        self.make_run("sh-q", phase="wait", pid=os.getpid())
        self.assertFalse(self.guard()[0])

    def test_f2_a_run_with_a_task_and_a_run_file_counts_once(self):
        state = self.make_run("sh-q")
        self.local_task(state, name="T-1")
        ok, reason = self.guard(trial_max=2)
        self.assertEqual(ok, True, reason)
        ok, reason = self.guard(trial_max=1)
        self.assertEqual((ok, "T-1" in reason, "run sh-q" in reason), (False, True, False))

    def test_f2_ahead_of_counts_only_queued_runs_that_came_first_and_always_a_running_worker(self):
        self.make_run("sh-a", created="2026-10-31T10:00:00Z")
        self.make_run("sh-b", created="2026-10-31T10:00:05Z")
        project, env = self.project, self.env
        names = lambda **kw: trial_session.trial_local(project, env=env, **kw)  # noqa: E731
        self.assertEqual(names(exclude_run="sh-b", ahead_of=("2026-10-31T10:00:05Z", "sh-b")), ["run sh-a"])
        self.assertEqual(names(exclude_run="sh-a", ahead_of=("2026-10-31T10:00:00Z", "sh-a")), [], "sh-b came later: it does not hold sh-a back")
        self.local_task(trial_session.state_dir(project, "sh-b"), name="T-9")
        self.assertEqual(names(exclude_run="sh-a", ahead_of=("2026-10-31T10:00:00Z", "sh-a")), ["T-9"], "but its running worker does")

    def test_a_non_local_model_is_not_limited_by_the_local_slots(self):
        self.main_with(2)
        self.assertEqual(self.guard(model="openai/gpt-6-sol"), (True, ""))

    def test_main_limit_follows_the_configured_value(self):
        self.main_with(2)
        self.assertEqual(self.guard(main_max=3), (True, ""))

    def test_the_guard_writes_nothing_into_mains_state_dir(self):
        self.main_with(2)
        before = sorted(str(p.relative_to(self.state)) + ":" + str(p.stat().st_mtime_ns) for p in self.state.rglob("*"))
        self.guard()
        after = sorted(str(p.relative_to(self.state)) + ":" + str(p.stat().st_mtime_ns) for p in self.state.rglob("*"))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
