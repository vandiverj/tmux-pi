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

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai/gpt-6-sol"
TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux not installed")
class TmuxCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.sock = f"tp-t3-{os.getpid()}-{self.id().split('.')[-1][-20:]}"
        fake = self.root / "bin"
        fake.mkdir()
        wrapper = fake / "tmux"
        # Pane queries and kills are real; everything that would start a worker is a no-op.
        wrapper.write_text(f'''#!/bin/sh
case "$1" in
 list-panes) [ ! -f "$TP_STATE_DIR/list-fail" ] || {{ echo "no server" >&2; exit 1; }}
   if [ -f "$TP_STATE_DIR/list-rows" ]; then cat "$TP_STATE_DIR/list-rows"; exit 0; fi
   exec {TMUX} -L {self.sock} "$@" ;;
 display-message) if [ -n "$TP_MAIN_CWD" ]; then case "$*" in *pane_current_path*) printf '%s\\n' "$TP_MAIN_CWD"; exit 0 ;; esac; fi
   exec {TMUX} -L {self.sock} "$@" ;;
 kill-pane) if [ -f "$TP_STATE_DIR/kill-fail" ]; then echo "cannot kill pane" >&2; exit 1; fi; exec {TMUX} -L {self.sock} "$@" ;;
 new-session|new-window-real) exec {TMUX} -L {self.sock} "$@" ;;
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
     pane=$({TMUX} -L {self.sock} new-window -d -P -F '#{{pane_id}}' "sh -c 'sleep 300; :' sleeper $last") || exit 1
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
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(self.root / "work-store"),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.tmux("new-session", "-d", "-s", "anchor", "sleep 300")
        self.addCleanup(self.stop_server)

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
