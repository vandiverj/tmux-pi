"""lib/trial_shadow.py and bin/tp-trial (trial lane L3, design §3/§9/§10): a shadow runs the brief a normal coder got in a throwaway
detached worktree and its own tmux session, reviews the diff and writes a record, and leaves the real lane, step, branches and
Main's state dir exactly as they were.

Fixtures: the repositories and the private tmux server of tests/test_lane_writer.py. A fast group replaces only `Run.spawn` (a
real task dir with a real pane, written by hand) and steps an injected clock; one full run goes through the real tp-spawn,
tp-agent and tp-review, with `new-window` stubbed, so nothing starts a model. No test waits a fixed time: every wait is a poll
callback that settles the task, or a clock that jumps.

The full run takes minutes (tp-spawn is slow here); run it apart: `python3 -m unittest test_trial_shadow.FullRunTests` from tests/, and
the other classes by name (unittest's -k has no "not")."""
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_lane_writer as lw  # noqa: E402
import test_trial_session as tts  # noqa: E402
from lane_fixture import MODEL, ROOT, TMUX  # noqa: E402

sys.path.insert(0, str(ROOT / "lib"))
import trial_record_stub  # noqa: E402
import trial_session  # noqa: E402
import trial_shadow as ts  # noqa: E402

git = lw.git
LOCAL = tts.LOCAL_MODEL
SECTION = "## Paths other lanes own"
OTHERS = "- lane beta (lane/beta) owns \"lib/x.py\"\nDo not edit these paths; if the task needs them, stop and send tp-agent request."


SESSION_WRAPPER = """#!/bin/sh
case "$1" in
 has-session|kill-session|new-session|set-option|show-options|set-environment|show-environment)
   case " $* " in *" =tp-trial-"*|*" tp-trial-"*) exec {real} -L {sock} "$@" ;; esac ;;
esac
exec {fixture} "$@"
"""


class Clock:
    """A clock that jumps: each `sleep` advances it, so a deadline is reached by polling, never by waiting."""

    def __init__(self, step=100.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        return self.now

    def sleep(self, _seconds):
        self.now += self.step


class StubSpawnRun(ts.Run):
    """The run with its spawn replaced by a hand-made task: the shape tp-spawn leaves (meta, state, a real pane), no model."""
    case = None

    def spawn(self):
        self.note(phase="spawn")
        if self.case.spawn_error:
            raise ts.ShadowError(self.case.spawn_error)
        task = self.task_path()
        task.mkdir(parents=True)
        pane, _ = self.case.pane(self.case.worker_command(task))
        (task / "meta.json").write_text(json.dumps({"id": self.task_id, "worker": "pi", "model": self.info["model"], "thinking": self.info["thinking"],
                                                    "role": "coder", "workdir": str(self.worktree), "pane": pane, "created": "2026-10-31T10:00:00Z",
                                                    "deliverable": "Report"}))
        (task / "state.json").write_text(json.dumps({"state": "working"}))
        (task / "packet.md").write_text("# Task\n\nMake a focused change\n\n## Deliverable\n\nReport\n")  # what a review splits
        (task / "task.md").write_text("Make a focused change\n")
        self.case.worker_pane = pane
        self.note(spawned=ts.now())


class ShadowCase(lw.LaneCase):
    local_task = tts.GuardCase.local_task

    def setUp(self):
        super().setUp()
        self.spawn_error = None
        self.worker_pane = None
        self.source_id = "src-1"
        self.base = git(self.repo, "rev-parse", "HEAD")
        StubSpawnRun.case = self
        self.passthrough_trial_sessions()
        self.make_source()

    def passthrough_trial_sessions(self):
        """The fixture's tmux stub answers every session query with Main's own state. A trial session is real here: its session
        commands (and only those) reach the private server; new-window, list-panes and the rest stay the fixture's."""
        wrapper = self.root / "bin" / "tmux"
        fixture = wrapper.with_name("tmux-fixture")
        wrapper.rename(fixture)
        wrapper.write_text(SESSION_WRAPPER.format(real=TMUX, sock=self.sock, fixture=fixture))
        wrapper.chmod(0o755)

    def make_source(self, task_id="src-1", meta=None, dispatch=None, request="Make a focused change in lib/x.py\n", packet=None, review_of=False):
        task = self.state / "tasks" / task_id
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": task_id, "worker": "pi", "model": MODEL, "thinking": "medium", "workdir": str(self.repo),
                                                    "role": "coder", "deliverable": "Report", "created": "2026-10-31T09:00:00Z",
                                                    "reviewer": {"model": "claude-sonnet-5-5"}, **(meta or {})}))
        (task / "dispatch.json").write_text(json.dumps({"context": [], "omitted_context": [], "notes": ["keep it small"], "tests": "affected",
                                                        "base_commit": self.base, **(dispatch or {})}))
        if request is not None:
            (task / "request.md").write_text(request)
        (task / "packet.md").write_text(packet if packet is not None else "# Task\n\nx\n\n" + SECTION + "\n" + OTHERS + "\n\n## Next\n\nmore\n")
        if review_of:
            (task / "review-of").write_text("other")
        return task

    # -- running
    def prepare(self, model=MODEL, **kw):
        info = ts.prepare(self.state, self.source_id, model=model, thinking="medium", env=self.env, timeout_s=kw.pop("timeout_s", 600),
                          review_timeout_s=kw.pop("review_timeout_s", 600), **kw)
        rdir = trial_session.run_dir(info["project"], info["run"])
        (rdir / "state" / "live-window").write_text("")  # tp-spawn and tp-review in the trial's state get a real pane
        return rdir, info

    def session_names(self):
        run = subprocess.run([TMUX, "-L", self.sock, "list-sessions", "-F", "#{session_name}"], text=True, capture_output=True)
        return sorted(run.stdout.split()) if run.returncode == 0 else []

    def refs(self, pattern="refs/heads"):
        return git(self.repo, "for-each-ref", "--format=%(refname) %(objectname)", pattern)

    def worktrees(self):
        return git(self.repo, "worktree", "list", "--porcelain")

    def records(self, project=None):
        path = Path(project or self.repo) / ".tmux-pi" / "trial" / "records.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def snapshot(self, root):
        out = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file() and not path.is_symlink():
                st = path.stat()
                out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        return out

    def run_stub(self, rdir, on_poll=None, clock=None, record_module=None, spawn_error=None):
        """Execute the run with the stub spawn. `on_poll(n, run)` is called at each poll (the event that settles a task)."""
        self.spawn_error = spawn_error
        clock = clock or Clock()
        count = [0]
        holder = {}

        def sleep(seconds):
            count[0] += 1
            if on_poll:
                on_poll(count[0], holder["run"])
            clock.sleep(seconds)

        run = StubSpawnRun(rdir, self.env, sleep=sleep, clock=clock, record_module=record_module)
        holder["run"] = run
        return run, run.execute()

    def settle_worker(self, run, text="RESULT_STATUS: complete\nDone.\n"):
        self.settle(run.task_path(), "waiting", text)

    def edit(self, run, commits=(("lib/x.py", "x edited\n", "Change x"),), untracked=("lib/new.py", "new\n")):
        """What a worker leaves in its worktree: commits, and one file never added."""
        for name, text, message in commits:
            (run.worktree / name).write_text(text)
            git(run.worktree, "add", name)
            git(run.worktree, "commit", "-q", "-m", message)
        if untracked:
            (run.worktree / untracked[0]).write_text(untracked[1])


class RecordingRecords:
    """A record module that keeps what it was asked to build, in order, next to the stub it delegates to."""

    def __init__(self):
        self.calls = []

    def build(self, task_dir, arm, mode, pair_id, **kw):
        self.calls.append((arm, dict(kw)))
        return trial_record_stub.build(task_dir, arm, mode, pair_id, **kw)

    append = staticmethod(trial_record_stub.append)
    trial_dir = staticmethod(trial_record_stub.trial_dir)


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
        self.assertTrue(text.startswith(ts.workdir_text(rdir / "worktree", self.repo)), "the first request names the absolute working tree first")
        self.assertTrue(text.endswith("\n\nMake a focused change in lib/x.py\n"), "then the request the normal coder got")
        self.assertEqual(info["workdir"], str(rdir / "worktree"))
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
        ts.update_run(rdir_a, created="2026-10-31T10:00:00Z")
        ts.update_run(rdir_b, created="2026-10-31T10:00:01Z")
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

    def test_a_branch_the_worker_made_is_dropped_and_the_real_branches_are_not_touched(self):
        run = self.worktree_run()
        before = self.refs("refs/heads")
        git(run.worktree, "checkout", "-q", "-b", "worker-branch")
        self.edit(run)
        run.collect_work()
        self.assertEqual(self.refs("refs/heads"), before)
        self.assertEqual(run.info["stray_branch"], "refs/heads/worker-branch")
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
        self.assertFalse((rdir / "worktree").exists())
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())

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
        worktree = str(rdir / "worktree")
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
        self.assertFalse((rdir / "worktree").exists())

    def test_g_a_failed_spawn_cleans_up_the_worktree_the_session_and_the_refs(self):
        rdir, info = self.prepare()
        before = self.worktrees()
        _, done = self.run_stub(rdir, spawn_error="tp-spawn refused: nope")
        self.assertEqual(done["outcome"], "spawn_failed")
        self.assertIn("tp-spawn refused", done["error"])
        self.assertFalse((rdir / "worktree").exists())
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
        self.assertFalse((rdir / "worktree").exists())
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
        self.assertFalse((rdir / "worktree").exists())
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
        git(self.repo, "update-ref", "refs/tmux-pi/shadow/{}/head".format(info["run"]), self.base)
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse((rdir / "worktree").exists())
        self.assertEqual(self.refs("refs/tmux-pi"), "")

    def test_cleanup_removes_a_worktree_git_no_longer_knows(self):
        rdir, info = self.prepare()
        run = ts.Run(rdir, self.env)
        run.make_worktree()
        git(self.repo, "worktree", "remove", "--force", str(run.worktree))
        (rdir / "worktree").mkdir()
        (rdir / "worktree" / "stale").write_text("x")
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse((rdir / "worktree").exists())

    def test_cleanup_ends_a_task_that_still_runs(self):
        rdir, info = self.prepare()
        task = rdir / "state" / "tasks" / "trial-live"
        task.mkdir()
        pane, _ = self.pane(self.worker_command(task))
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "worker": "pi", "model": MODEL, "pane": pane, "role": "coder", "workdir": str(self.repo)}))
        (task / "state.json").write_text(json.dumps({"state": "working"}))
        self.assertEqual(ts.cleanup(rdir, self.env), [])
        self.assertFalse(self.alive(pane))

    def test_cleanup_never_removes_a_worktree_path_outside_the_run_directory(self):
        rdir, info = self.prepare()
        outside = self.root / "precious"
        outside.mkdir()
        (outside / "keep").write_text("x")
        ts.update_run(rdir, worktree=str(outside))
        leftovers = ts.cleanup(rdir, self.env)
        self.assertTrue((outside / "keep").exists())
        self.assertTrue(any("not directly under the run directory" in l for l in leftovers), leftovers)


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
        self.assertTrue(ts.pid_is_run(child.pid, info["run"]))
        self.assertFalse(ts.pid_is_run(child.pid, "sh-other"))
        for pid in (None, 0, 1, -5, "12", os.getpid()):
            self.assertFalse(ts.pid_is_run(pid, info["run"]), pid)

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
        self.assertFalse((rdir / "worktree").exists())
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
        self.assertEqual(os.path.realpath(meta["workdir"]), os.path.realpath(rdir / "worktree"))
        self.assertNotIn("lane", meta)
        self.assertIn("export TP_TRIAL_ID={}".format(info["run"]), seen["run_sh"])
        self.assertIn("export TP_DOCS_TOOLS=1", seen["run_sh"])
        self.assertIn("Make a focused change", seen["packet"])
        self.assertIn(ts.TRIAL_NOTE, seen["packet"])
        # Phase 0: the worker is told, in the request and in the packet, which tree is its own
        told = ts.workdir_text(os.path.realpath(rdir / "worktree"), os.path.realpath(self.repo))
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
        self.assertFalse((rdir / "worktree").exists())
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
