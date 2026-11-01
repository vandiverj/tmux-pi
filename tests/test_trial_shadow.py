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
NOT_OURS = "not the tree this run created"  # what a refusal to act on a tree says
OTHERS = "- lane beta (lane/beta) owns \"lib/x.py\"\nDo not edit these paths; if the task needs them, stop and send tp-agent request."


SESSION_WRAPPER = """#!/bin/sh
case "$1" in
 kill-session) exec {real} -L {sock} "$@" ;;
 has-session|new-session|set-option|show-options|set-environment|show-environment)
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
        pane = self.case.session_pane(self.rdir, self.case.worker_command(task))  # a pane in the run's own session, as tp-spawn makes
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

    def session_pane(self, rdir, command):
        """A real pane inside the trial session of the run in `rdir` (not the fixture's newest session, which is where a bare new-window
        goes). Returns its id. It is not recorded as the run's: `record_panes` does that, as the run does after tp-spawn."""
        target = "=" + trial_session.session_name(Path(rdir).name) + ":"
        pane = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", target, command)
        self.tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        return pane

    def record_panes(self, rdir):
        owner = ts.Owner(rdir)
        try:
            owner.record_panes(self.env)
        finally:
            owner.close()

    def tree(self, rdir):
        """The place of the run's worktree: chosen by `prepare`, unique to the run (`wt-<id>` under its directory)."""
        return Path(ts.read_run(rdir)["worktree"])

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

    def ago(self, seconds):
        """A run.json `created` time `seconds` before now."""
        then = ts.datetime.datetime.now(ts.datetime.timezone.utc) - ts.datetime.timedelta(seconds=seconds)
        return then.strftime("%Y-%m-%dT%H:%M:%SZ")

    def snapshot(self, root):
        out = {}
        for path in sorted(Path(root).rglob("*")):
            if path.is_file() and not path.is_symlink():
                st = path.stat()
                out[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        return out

    def run_stub(self, rdir, on_poll=None, clock=None, record_module=None, spawn_error=None, run_class=StubSpawnRun):
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

        run = run_class(rdir, self.env, sleep=sleep, clock=clock, record_module=record_module)
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
        git(self.repo, "update-ref", "refs/tmux-pi/shadow/{}/head".format(info["run"]), self.base)
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
        run, done = self.run_stub(rdir, on_poll=lambda n, r: self.settle_worker(r))
        self.assertEqual(done["outcome"], "completed")


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

    def trial_task(self, rdir, name, pane, state="working"):
        task = rdir / "state" / "tasks" / name
        task.mkdir()
        (task / "meta.json").write_text(json.dumps({"id": name, "worker": "pi", "model": MODEL, "pane": pane, "role": "coder", "workdir": str(self.repo)}))
        (task / "state.json").write_text(json.dumps({"state": state}))
        return task

    def live_run_with_task(self, run):
        """A prepared run with a task whose worker pane is in the run's session and recorded, as after tp-spawn."""
        rdir, info = self.prepare(run=run)
        task = rdir / "state" / "tasks" / "trial-live"
        task.mkdir()
        pane = self.session_pane(rdir, self.worker_command(task))
        self.record_panes(rdir)
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "worker": "pi", "model": MODEL, "pane": pane, "role": "coder", "workdir": str(self.repo)}))
        (task / "state.json").write_text(json.dumps({"state": "working"}))
        return rdir, info, pane

    # -- what prepare records
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
    def stranger(self):
        """A process that is not the run's, in a group of its own (Main's worker, say), and its `ps` start time."""
        child = subprocess.Popen(["sleep", "300"], start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        start = " ".join(subprocess.run(["ps", "-o", "lstart=", "-p", str(child.pid)], text=True, capture_output=True).stdout.split())
        return child, start

    def plant(self, task, pane, who, start, **over):
        """The process record `tp-kill` writes itself, as a worker can write it, naming `who`."""
        rec = {"pane": pane, "pid": who.pid, "pgid": who.pid, "group": True, "members": [[who.pid, start]], "recorded": "2026-10-31T10:00:00+00:00"}
        (task / "process.json").write_text(json.dumps(dict(rec, **over)))

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
            run.end_task("stop", "trial-live")
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
            run.end_task("stop", "trial-nopane")
        self.assertIsNone(child.poll())
        self.assertFalse((task / "stopped.json").exists())

    def test_g1_a_process_record_naming_a_stranger_under_a_recorded_panes_name_is_refused(self):
        child, start = self.stranger()
        rdir, info, pane = self.live_run_with_task("sh-pj-pid")
        task = rdir / "state" / "tasks" / "trial-live"
        self.plant(task, pane, child, start)  # the run's pane, someone else's pid
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.end_task("stop", "trial-live")
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
            run.end_task("stop", "trial-live")
        self.assertIsNone(child.poll())
        self.assertTrue(self.alive(other))

    def test_g1_a_process_record_that_is_not_a_plain_file_is_refused(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-link")
        task = rdir / "state" / "tasks" / "trial-live"
        os.symlink(str(self.root / "nowhere"), str(task / "process.json"))
        run = ts.Run(rdir, self.env)
        with self.assertRaises(ts.ShadowError):
            run.end_task("stop", "trial-live")
        self.assertTrue(self.alive(pane))

    def test_g1_a_recorded_pids_start_time_that_no_longer_matches_is_refused(self):
        rdir, info, pane = self.live_run_with_task("sh-pj-reuse")
        run = ts.Run(rdir, self.env)
        pid = run.owner.record["pane_procs"][pane]["pid"]
        run.owner.record["pane_procs"][pane]["start"] = "Mon Jan  1 00:00:00 2001"  # the pid now runs something that started later
        (rdir / "state" / "tasks" / "trial-live" / "process.json").write_text(json.dumps(
            {"pane": pane, "pid": pid, "pgid": os.getpgid(pid), "group": os.getpgid(pid) == pid, "members": [[pid, "x"]], "recorded": "2026-10-31T10:00:00+00:00"}))
        with self.assertRaises(ts.ShadowError):
            run.end_task("stop", "trial-live")
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
        done = run.end_task("stop", "trial-live")
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
