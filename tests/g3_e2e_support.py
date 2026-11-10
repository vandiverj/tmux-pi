"""Shared by tests/test_g3_e2e.py and tests/test_g3_e2e_tamper.py: the tmux shim, the audit of every tmux call, and a base class.

The tmux isolation is tested by construction. A recording shim is first on PATH for every test: it logs each call's argv and env, and it runs the
real tmux only when the call carries a private TMUX_TMPDIR (a g3t-* directory) and no TMUX/TMUX_PANE, and for kill-server only with `-L g3-*`;
otherwise it records a violation and exits 99 without touching any server. The process env carries a decoy TMUX_TMPDIR, TMUX and TMUX_PANE (the
"owner's server"): no call may use them, and the decoy directory must stay empty. After the tests, every recorded call is compared with the run
that made it: its TMUX_TMPDIR must be that run's own directory, its `-L` socket that run's own socket, and each run's `kill-server` must go to
that run's own socket and directory (equality with the run's summary, not a prefix)."""
import session_env  # noqa: F401  (first: drops the caller's session)

import concurrent.futures
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "smoke"))
import g3_e2e as g3  # noqa: E402

SHIM = '''#!/bin/sh
# records argv and the tmux-relevant env as one JSON line, then runs the real tmux only for a call that is safe by construction
python3 - "$@" <<'PY'
import json, os, sys
print(json.dumps({"argv": sys.argv[1:], "TMUX_TMPDIR": os.environ.get("TMUX_TMPDIR"), "TMUX": os.environ.get("TMUX"),
                  "TMUX_PANE": os.environ.get("TMUX_PANE")}), file=open(os.environ["G3_SHIM_LOG"], "a"))
PY
case "$TMUX_TMPDIR" in */g3t-*) ;; *) echo "shim: no private TMUX_TMPDIR" >&2; echo violation >> "$G3_SHIM_LOG.bad"; exit 99 ;; esac
[ -z "$TMUX" ] && [ -z "$TMUX_PANE" ] || { echo "shim: TMUX in env" >&2; echo violation >> "$G3_SHIM_LOG.bad"; exit 99; }
for a in "$@"; do
  if [ "$a" = kill-server ]; then
    [ "$1" = -L ] && case "$2" in g3-*) ;; *) echo "shim: kill-server on $2" >&2; echo violation >> "$G3_SHIM_LOG.bad"; exit 99 ;; esac || { echo "shim: kill-server without -L" >&2; echo violation >> "$G3_SHIM_LOG.bad"; exit 99; }
  fi
done
# Failure injection. Each variable is a space-separated list of subcommands (the word after -L <socket>); G3_SHIM_MATCH, when set, narrows every
# one of them to calls whose arguments contain that text (e.g. "-s g3-" is the run's own session, not the "anchor" one).
#   G3_SHIM_RIVAL: before the call, another party makes a session of the call's -s name, marked @g3_run=rival (a race lost by the run)
#   G3_SHIM_HANG : the call hangs instead of running
#   G3_SHIM_AFTER: the call runs (the tmux server did it) and then the client hangs, so the caller sees a timeout
listed() { [ -n "$1" ] && case " $1 " in *" $2 "*) return 0 ;; esac; return 1; }
M=0; case " $* " in *"$G3_SHIM_MATCH"*) M=1 ;; esac
if [ $M = 1 ] && listed "$G3_SHIM_RIVAL" "$3"; then
  name=; prev=; for a in "$@"; do [ "$prev" = -s ] && name=$a; prev=$a; done
  "$G3_REAL_TMUX" -L "$2" new-session -d -s "$name" "sleep 600" \\; set-option -t "=$name:" @g3_run rival >/dev/null 2>&1
fi
if [ $M = 1 ] && listed "$G3_SHIM_HANG" "$3"; then exec sleep 600; fi
if [ $M = 1 ] && listed "$G3_SHIM_AFTER" "$3"; then "$G3_REAL_TMUX" "$@" >/dev/null 2>&1; exec sleep 600; fi
exec "$G3_REAL_TMUX" "$@"
'''

# Every run in these tests carries a caller's TP_PI_NODE, whatever the machine running the suite has (Main's own is empty): a space, `=`
# and both quotes in one value, so every full run shows the value reaches the session as one argv word and comes back unchanged.
PI_NODE = "/opt/pi node/a=b \"q\" 'r'/bin/node"
CONFIG_DIR =".tmux" + "-pi"          # where a repository keeps its lane config (lanes.json)
WORK = DECOY = LOG = PATCH = None
NOTHING_MADE = {"session": False, "integration": False, "lane": False, "lane_branch": False, "worktree": None, "dirs": []}
RUNS = []          # the JSON summary of every run the module made (parallel or in-process)


def set_up_module():
    global PATCH, WORK, DECOY, LOG
    real = shutil.which("tmux")
    if not real:
        raise unittest.SkipTest("tmux is not installed")
    WORK = Path(tempfile.mkdtemp(prefix="g3test-")).resolve()
    DECOY = WORK / "owner-tmux-tmpdir"
    DECOY.mkdir()
    LOG = WORK / "shim.jsonl"
    RUNS.clear()
    (WORK / "bin").mkdir()
    shim = WORK / "bin" / "tmux"
    shim.write_text(SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    PATCH = mock.patch.dict(os.environ, {"PATH": f"{WORK / 'bin'}{os.pathsep}{os.environ['PATH']}", "G3_SHIM_LOG": str(LOG), "G3_REAL_TMUX": real,
                                         "TMUX_TMPDIR": str(DECOY), "TMUX": f"{DECOY}/default,1,0", "TMUX_PANE": "%999",
                                         "TP_PI_NODE": PI_NODE})
    PATCH.start()


def tear_down_module():
    PATCH.stop()
    try:
        assert not list(DECOY.iterdir()), f"the owner's TMUX_TMPDIR was used: {list(DECOY.iterdir())}"
        assert not Path(f"{LOG}.bad").exists(), "the shim refused a call"
        audit(records(), RUNS)
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def records():
    return [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []


def audit(calls, runs):
    """Every tmux call belongs to one run and uses only that run's own TMUX_TMPDIR and `-L` socket; every kill-server is the run's own.
    `runs` are summaries (they carry tmux_tmpdir and socket). Returns {TMUX_TMPDIR: number of kill-server calls}."""
    own = {run["tmux_tmpdir"]: run["socket"] for run in runs if run.get("tmux_tmpdir")}
    stopped = {}
    for call in calls:
        tmpdir, argv = call["TMUX_TMPDIR"], call["argv"]
        assert tmpdir in own, f"a tmux call outside every run's own TMUX_TMPDIR: {call}"
        assert not call["TMUX"] and not call["TMUX_PANE"], call
        assert argv[:2] == ["-L", own[tmpdir]], f"a tmux call not on its run's own socket {own[tmpdir]}: {call}"
        if "kill-server" in argv:
            assert argv == ["-L", own[tmpdir], "kill-server"], call
            stopped[tmpdir] = stopped.get(tmpdir, 0) + 1
    assert all(count == 1 for count in stopped.values()), f"a run stopped its server twice: {stopped}"
    return stopped


class Base(unittest.TestCase):
    def setUp(self):
        self.mark = len(records())
        self.out = WORK / f"{self.id().rsplit('.', 1)[-1]}.json"

    def tearDown(self):
        """The audit of every tmux call so far: private dir of its own run, none with the caller's TMUX, kill-server only the run's own."""
        audit(records()[self.mark:], RUNS)
        self.assertFalse(Path(f"{LOG}.bad").exists(), "the shim refused a call")
        self.assertEqual(list(DECOY.iterdir()), [], "the owner's TMUX_TMPDIR was touched")

    def smoke(self, *extra):
        """main() in this process: (rc, the JSON summary, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = g3.main(["--fake", "--out", str(self.out), *extra])
        summary = json.loads(self.out.read_text())
        RUNS.append(summary)
        return rc, summary, err.getvalue()

    def assert_clean(self, summary):
        """Nothing of the run is left: the cleanup ran, undid everything, and removed its temp repo and its private tmux directory."""
        self.assertTrue(summary["cleanup"]["ran"])
        self.assertEqual(summary["cleanup"]["errors"], [])
        self.assertEqual(summary["cleanup"]["leftovers"], [])
        self.assertEqual(summary["cleanup"]["kept"], [])
        self.assertFalse(Path(summary["repo"]).exists())
        self.assertFalse(Path(summary["tmux_tmpdir"]).exists())


class Parallel:
    """Full fake runs started together as subprocesses (one run is ~11 s, almost all of it the real tools; the runs share nothing: each has its own
    temp repo, state, TMUX_TMPDIR and private tmux server). Memoised per case table. A case is a list of extra arguments, or a dict
    {"args": [...], "run_id": "..."}: the run then gets that run id (so the test knows its lane name, and so the paths the config gives it,
    before the run starts)."""

    PINNED = ("import os, sys; sys.path.insert(0, os.environ['G3_TOOLS']); import g3_e2e; "
              "g3_e2e.new_run_id = lambda: os.environ['G3_RUN_ID']; sys.exit(g3_e2e.main(sys.argv[1:]))")

    def __init__(self, cases, workers=8):
        self.cases, self.workers, self.done = cases, workers, {}

    def __call__(self):
        if not self.done:
            def one(item):
                label, extra = item
                out = WORK / f"{label}.json"
                env, argv = None, [sys.executable, str(ROOT / "tools" / "smoke" / "g3_e2e.py")]
                if isinstance(extra, dict):
                    env = {**os.environ, "G3_TOOLS": str(ROOT / "tools" / "smoke"), "G3_RUN_ID": extra["run_id"]}
                    argv, extra = [sys.executable, "-c", self.PINNED], extra["args"]
                run = subprocess.run([*argv, "--fake", "--out", str(out), *extra], text=True, capture_output=True, timeout=300, env=env)
                return label, (run.returncode, json.loads(out.read_text()) if out.exists() else {}, run.stderr, run.stdout)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                self.done.update(pool.map(one, self.cases.items()))
            RUNS.extend(value[1] for value in self.done.values() if value[1])
        return self.done
