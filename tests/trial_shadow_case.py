"""Shared fixtures of the trial-shadow tests (lib/trial_shadow.py and bin/tp-trial; trial lane L3, design 3/9/10): the repositories and
the private server of tests/test_lane_writer.py, the stepped clock, a run whose spawn is a hand-made task, and `ShadowCase`.

The tests are in five files so that each run is well under the 300 s bound: test_trial_shadow (the flow, the command line and the
full run), test_trial_shadow_run (the steps and the cleanup), test_trial_shadow_owner (what a run owns), test_trial_shadow_gate (the
tp-* gate), and test_trial_shadow_worker_files (what a run does with a file the worker wrote that is not a plain file, and with
branches and refs). This module has no tests of its own."""
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
import trial_record  # noqa: E402
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
        bindir = self.root / "bin"  # per test, ahead of the class-shared fake dir (self.fake), which stays untouched
        bindir.mkdir()
        wrapper = bindir / "tmux"
        wrapper.write_text(SESSION_WRAPPER.format(real=TMUX, sock=self.sock, fixture=self.fake / "tmux"))
        wrapper.chmod(0o755)
        self.env["PATH"] = str(bindir) + os.pathsep + self.env["PATH"]

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
    """A record module that keeps what it was asked to build, in order, next to the real one (lib/trial_record.py) that it delegates to."""
    RECORDS = trial_record.RECORDS
    SCHEMA = trial_record.SCHEMA

    def __init__(self):
        self.calls = []

    def build(self, task_dir, arm, mode, pair_id, **kw):
        self.calls.append((arm, dict(kw)))
        return trial_record.build(task_dir, arm, mode, pair_id, **kw)

    no_task = staticmethod(trial_record.no_task)
    append = staticmethod(trial_record.append)
    trial_dir = staticmethod(trial_record.trial_dir)
