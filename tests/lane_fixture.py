"""A tmux fixture shared by the process (T3) and lane (T10) tests.

Panes are real, on a private tmux server (-L socket); only the calls that would launch a worker are stubbed: `new-window`
prints a pane id that is not on the server, so a dispatched task has no live process until a test attaches a real pane."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests/ holds session_env
from session_env import outside_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai/gpt-6-sol"
TMUX = shutil.which("tmux")
GIT_ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")}, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args):
    """Run git in `cwd` (fixed author and committer, no inherited GIT_* variables); return stdout, stripped. A non-zero exit fails."""
    run = subprocess.run(["git", "-C", str(cwd)] + list(args), text=True, capture_output=True, env=GIT_ENV)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def write_wrappers(fake, sock):
    """Write the fake `tmux` and `ps` into `fake`; the tmux wrapper reaches the private server on `sock`."""
    wrapper = fake / "tmux"
    # Pane queries and kills are real; everything that would start a worker is a no-op.
    wrapper.write_text(f'''#!/bin/sh
case "$1" in
 list-panes) [ ! -f "$TP_STATE_DIR/list-fail" ] || {{ echo "no server" >&2; exit 1; }}
   if [ -f "$TP_STATE_DIR/list-rows" ]; then cat "$TP_STATE_DIR/list-rows"; exit 0; fi
   exec {TMUX} -L {sock} "$@" ;;
 display-message) if [ -n "$TP_MAIN_CWD" ]; then case "$*" in *pane_current_path*) printf '%s\\n' "$TP_MAIN_CWD"; exit 0 ;; esac; fi
   exec {TMUX} -L {sock} "$@" ;;
 kill-pane) if [ -f "$TP_STATE_DIR/kill-fail" ]; then echo "cannot kill pane" >&2; exit 1; fi; exec {TMUX} -L {sock} "$@" ;;
 new-session|new-window-real) exec {TMUX} -L {sock} "$@" ;;
 set-option) if [ -f "$TP_STATE_DIR/hang-after-activate" ]; then : > "$TP_STATE_DIR/hung"; sleep 300; fi; exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window)
   # Normally a pane id that is not on the server. With live-window a real pane running `sleep` is made (the worker command is
   # never run); with window-fail the call fails; with break-after-window the pane table is unreadable once the pane exists; with kill-fail kill-pane fails;
   # with hang-after-window the call stops after the pane exists.
   if [ -f "$TP_STATE_DIR/window-fail" ]; then echo "cannot create window" >&2; exit 1; fi
   if [ -f "$TP_STATE_DIR/live-window" ]; then
     # the command line carries the run.sh path, as a real worker's does: that is how a killed starter's pane is found again
     for last; do :; done
     pane=$({TMUX} -L {sock} new-window -d -P -F '#{{pane_id}}' "sh -c 'sleep 300; :' sleeper $last") || exit 1
     if [ -f "$TP_STATE_DIR/break-after-window" ]; then touch "$TP_STATE_DIR/list-fail"; fi
     # with hang-after-window the starter stops here, pane made and not yet reported: a test kills it in this interval
     if [ -f "$TP_STATE_DIR/hang-after-window" ]; then : > "$TP_STATE_DIR/hung"; sleep 300; fi
     printf '%s\\n' "$pane"
   else printf '%%9999\\n'; fi ;;
 *) exit 0 ;;
esac
''')
    wrapper.chmod(0o755)
    # ps shows what is really running, plus (while ghost-ps exists) one recorded row that never dies: a survivor of KILL.
    (fake / "ps").write_text(f'''#!/bin/sh
out=$(/bin/ps "$@"); printf '%s\\n' "$out"
if [ -f "$TP_STATE_DIR/ghost-ps" ]; then
  pid=$(cut -d' ' -f1 "$TP_STATE_DIR/ghost-ps")
  printf '%s\\n' "$out" | awk -v p="$pid" '$1 == p && $3 !~ /^Z/ {{ f = 1 }} END {{ exit !f }}' || cat "$TP_STATE_DIR/ghost-ps"
fi
''')
    (fake / "ps").chmod(0o755)


@unittest.skipUnless(TMUX, "tmux not installed")
class TmuxCase(unittest.TestCase):
    """A private tmux server and a state directory per test. A class that sets `shared_server = True` builds the wrappers and the
    server once for the class; after each test every session and window the test made is ended (the anchor stays), and the
    server is killed when the class ends. Such a class's tests must not depend on a fresh server."""
    shared_server = False
    stable_root = False  # a shared-server class may ask for each test's root at `class_root/run` (same path every test; a lane snapshot needs it)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not cls.shared_server:
            return
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.class_root = Path(temp.name).resolve()
        cls.sock = f"tp-t3-{os.getpid()}-{cls.__name__[-24:]}"
        cls.fake = cls.class_root / "bin"
        cls.fake.mkdir()
        write_wrappers(cls.fake, cls.sock)
        cls.addClassCleanup(cls.kill_server)  # registered last, so it runs before the folder is removed
        subprocess.run([TMUX, "-L", cls.sock, "new-session", "-d", "-s", "anchor", "sleep 86400"], check=True, capture_output=True)
        cls.anchor_window = subprocess.run([TMUX, "-L", cls.sock, "display-message", "-p", "-t", "anchor", "#{window_id}"], text=True,
                                           check=True, capture_output=True).stdout.strip()

    @classmethod
    def kill_server(cls):
        out = subprocess.run([TMUX, "-L", cls.sock, "display-message", "-p", "#{pid}"], text=True, capture_output=True)
        try:
            os.kill(int(out.stdout.strip()), signal.SIGKILL)
        except (ProcessLookupError, ValueError):
            pass

    def setUp(self):
        if self.shared_server and self.stable_root:
            self.root = self.class_root / "run"
            self.root.mkdir()
            self.addCleanup(shutil.rmtree, self.root, True)
        else:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            self.root = Path(temp.name).resolve()
        self.lay_out_root()
        if self.shared_server:
            fake = self.fake
        else:
            self.sock = f"tp-t3-{os.getpid()}-{self.id().split('.')[-1][-20:]}"
            fake = self.root / "bin"
            fake.mkdir()
            write_wrappers(fake, self.sock)
        self.env = {**outside_session(), "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(self.root / "work-store"),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        if self.shared_server:
            self.addCleanup(self.clear_server)
        else:
            self.tmux("new-session", "-d", "-s", "anchor", "sleep 300")
            self.addCleanup(self.stop_server)

    def lay_out_root(self):
        """The empty folders of a test's root: the project (with the state dir and its tasks folder) and `other`."""
        self.project = self.root / "project"
        self.project.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)

    def clear_server(self):
        """End what the test left on the shared server: every session but the anchor, every window but the anchor's own."""
        rows = [line.split() for line in self.tmux("list-windows", "-a", "-F", "#{session_name} #{window_id}").splitlines()]
        chain = []
        for session, window in rows:
            if session != "anchor":
                chain += ["kill-session", "-t", session, ";"]
            elif window != self.anchor_window:
                chain += ["kill-window", "-t", window, ";"]
        if chain:
            subprocess.run([TMUX, "-L", self.sock, *chain[:-1]], capture_output=True)

    def tmux(self, *args):
        return subprocess.run([TMUX, "-L", self.sock, *args], text=True, capture_output=True, check=True).stdout.strip()

    def stop_server(self):
        try:
            os.kill(int(self.tmux("display-message", "-p", "#{pid}")), signal.SIGKILL)
        except (subprocess.CalledProcessError, ProcessLookupError, ValueError):
            pass

    def pane(self, command="sleep 300"):
        """A real pane that outlives its command, like a worker's. Returns (pane id, pid)."""
        out = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id} #{pane_pid}", command)
        pane, pid = out.split()
        self.tmux("set-option", "-p", "-t", pane, "remain-on-exit", "on")
        return pane, int(pid)

    @staticmethod
    def worker_command(task):
        """A pane command that, like a real worker's, carries the task's own run.sh path (how its process is found again)."""
        return f"sh -c 'sleep 300; :' sleeper {task}/run.sh"

    def alive(self, pane):
        rows = dict(line.split() for line in self.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_dead}").splitlines())
        return rows.get(pane) == "0"

    def raw(self, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True, capture_output=True, timeout=60)

    def cli(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def dispatch(self, workdir=None, role="coder"):
        item = self.cli("dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL,
                        "--thinking", "medium", "--workdir", str(workdir or self.project), "--deliverable", "Report")
        return item, self.state / "tasks" / item["id"]

    def attach(self, task, pane):
        meta = json.loads((task / "meta.json").read_text())
        meta["pane"] = pane
        (task / "meta.json").write_text(json.dumps(meta))

    def settle(self, task, state="waiting", result="RESULT_STATUS: complete\nDone.\n"):
        (task / "state.json").write_text(json.dumps({"state": state}))
        if result is not None:
            (task / "result.md").write_text(result)

    def live_task(self, state="waiting", result="RESULT_STATUS: complete\nDone.\n", workdir=None, role="coder"):
        item, task = self.dispatch(workdir, role)
        pane, pid = self.pane(self.worker_command(task))
        self.attach(task, pane)
        self.settle(task, state, result)
        return item, task, pane, pid
