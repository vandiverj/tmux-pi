"""tools/smoke/g3_e2e.py in --fake mode on a tiny temp repo (never battle-soccer, never a model): the whole flow, a failure injected at
each step (cleanup ran, the error names the step), and each record check shown to fail by name when its record is broken.

The tmux isolation is tested here by construction. A shim is first on PATH for every test: it records each call's argv and env, and it
runs the real tmux only when the call carries a private TMUX_TMPDIR (a g3t-* directory) and no TMUX/TMUX_PANE, and for kill-server only
with `-L g3-*`; otherwise it records a violation and exits 99 without touching any server. The process env carries a decoy TMUX_TMPDIR,
TMUX and TMUX_PANE (the 'owner's server'): no call may use them, and the decoy directory must stay empty."""
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
exec "$G3_REAL_TMUX" "$@"
'''


def setUpModule():
    global PATCH, WORK, DECOY, LOG
    real = shutil.which("tmux")
    if not real:
        raise unittest.SkipTest("tmux is not installed")
    WORK = Path(tempfile.mkdtemp(prefix="g3test-")).resolve()
    DECOY = WORK / "owner-tmux-tmpdir"
    DECOY.mkdir()
    LOG = WORK / "shim.jsonl"
    (WORK / "bin").mkdir()
    shim = WORK / "bin" / "tmux"
    shim.write_text(SHIM)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    PATCH = mock.patch.dict(os.environ, {"PATH": f"{WORK / 'bin'}{os.pathsep}{os.environ['PATH']}", "G3_SHIM_LOG": str(LOG), "G3_REAL_TMUX": real,
                                         "TMUX_TMPDIR": str(DECOY), "TMUX": f"{DECOY}/default,1,0", "TMUX_PANE": "%999"})
    PATCH.start()


def tearDownModule():
    PATCH.stop()
    calls = records()
    try:
        assert not list(DECOY.iterdir()), f"the owner's TMUX_TMPDIR was used: {list(DECOY.iterdir())}"
        assert not Path(f"{LOG}.bad").exists(), "the shim refused a call"
        for call in calls:
            assert (call["TMUX_TMPDIR"] or "").rsplit("/", 1)[-1].startswith("g3t-"), call
            assert call["TMUX_TMPDIR"] != str(DECOY) and not call["TMUX"] and not call["TMUX_PANE"], call
            if "kill-server" in call["argv"]:
                assert call["argv"][0] == "-L" and call["argv"][1].startswith("g3-"), call
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


def records():
    return [json.loads(line) for line in LOG.read_text().splitlines()] if LOG.exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self.mark = len(records())
        self.out = WORK / f"{self.id().rsplit('.', 1)[-1]}.json"

    def tearDown(self):
        """The audit of every tmux call this test made: all in a private dir, none with the caller's TMUX, kill-server only on a g3-* socket."""
        for call in records()[self.mark:]:
            self.assertEqual(Path(call["TMUX_TMPDIR"] or "/none").name[:4], "g3t-", call)
            self.assertNotEqual(call["TMUX_TMPDIR"], str(DECOY))
            self.assertFalse(call["TMUX"] or call["TMUX_PANE"], call)
            if "kill-server" in call["argv"]:
                self.assertEqual(call["argv"][:1], ["-L"], call)
                self.assertTrue(call["argv"][1].startswith("g3-"), call)
        self.assertFalse(Path(f"{LOG}.bad").exists(), "the shim refused a call")
        self.assertEqual(list(DECOY.iterdir()), [], "the owner's TMUX_TMPDIR was touched")

    def smoke(self, *extra):
        """main() in this process: (rc, the JSON summary, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = g3.main(["--fake", "--out", str(self.out), *extra])
        return rc, json.loads(self.out.read_text()), err.getvalue()

    def assert_clean(self, summary):
        """Nothing of the run is left: the cleanup ran, undid everything, and removed its temp repo and its private tmux directory."""
        self.assertTrue(summary["cleanup"]["ran"])
        self.assertEqual(summary["cleanup"]["errors"], [])
        self.assertEqual(summary["cleanup"]["leftovers"], [])
        self.assertFalse(Path(summary["repo"]).exists())
        self.assertFalse(Path(summary["tmux_tmpdir"]).exists())


TAMPERS = {      # the broken record -> (the step that must fail, the check that must be named)
    "task-state": ("retry", "the retry to settle"),
    "lane-record": ("close", "lane record: the step is closed"),
    "review-verdict": ("review", "review verdict file: an approving verdict"),
    "scratch-commit": ("merge", "scratch commit: the added file holds exactly the requested line"),
}
CASES = {"flow": []}
CASES.update({f"inject-{step}": ["--inject-failure", step] for step in g3.STEPS if step != "setup"})
CASES.update({f"tamper-{name}": ["--tamper", name] for name in TAMPERS})
_RESULTS = {}


def results():
    """Every full fake run the module needs, started together as subprocesses (one run is ~11 s, almost all of it tp-review and the
    other tools; the runs share nothing: each has its own temp repo, state, TMUX_TMPDIR and private tmux server). Memoised."""
    if not _RESULTS:
        def one(item):
            label, extra = item
            out = WORK / f"{label}.json"
            run = subprocess.run([sys.executable, str(ROOT / "tools" / "smoke" / "g3_e2e.py"), "--fake", "--out", str(out), *extra],
                                 text=True, capture_output=True, timeout=300)
            return label, (run.returncode, json.loads(out.read_text()) if out.exists() else {}, run.stderr, run.stdout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            _RESULTS.update(pool.map(one, CASES.items()))
        _RESULTS["_calls"] = len(records())
    return _RESULTS


class FlowTest(Base):
    def test_whole_flow_fake(self):
        rc, summary, err, out = results()["flow"]
        self.assertEqual((rc, err), (0, ""), summary.get("error"))
        self.assertIn("g3_e2e: ok (fake)", out)
        self.assertTrue(summary["ok"])
        self.assertEqual([s["name"] for s in summary["steps"]], list(g3.STEPS))
        for step in summary["steps"]:
            self.assertEqual(step["rc"], 0, step)
            self.assertTrue(step["asserted"], f"step {step['name']} asserted nothing")
        self.assert_clean(summary)
        self.assertEqual(set(summary["ids"]), {"first", "retry", "reviewer"})
        self.assertGreater(len(records()), 3)      # the runs did use tmux, all through the shim

    def test_every_run_used_only_its_own_private_tmux(self):
        """The audit of the parallel runs (Base.tearDown audits the calls of one test; these were made before it)."""
        results()
        calls = records()
        self.assertGreater(len(calls), 40)
        dirs = {call["TMUX_TMPDIR"] for call in calls}
        self.assertEqual(len(dirs), len(CASES), "one private TMUX_TMPDIR per run")
        for call in calls:
            self.assertEqual(Path(call["TMUX_TMPDIR"]).name[:4], "g3t-", call)
            self.assertFalse(call["TMUX"] or call["TMUX_PANE"], call)
        servers = [c for c in calls if "kill-server" in c["argv"]]
        self.assertEqual(len(servers), len(CASES), "each run stopped its own server once")      # every run reached a server
        self.assertTrue(all(c["argv"][0] == "-L" and c["argv"][1].startswith("g3-") for c in servers), servers)
        self.assertEqual(len({(c["TMUX_TMPDIR"], c["argv"][1]) for c in servers}), len(CASES))


class InjectedFailureTest(Base):
    """A failure injected at each step: exit 1, the error names the step, the cleanup undid what the earlier steps made."""

    def test_failure_at_each_step(self):
        for step in g3.STEPS:
            if step == "setup":
                continue              # the earliest point: in-process, below
            with self.subTest(step=step):
                rc, summary, err, _ = results()[f"inject-{step}"]
                self.assertEqual(rc, 1)
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["failed_step"], step)
                self.assertIn(f"step '{step}'", err)
                self.assertIn("injected failure", err)
                self.assertEqual(summary["steps"][-1]["name"], step)
                self.assertEqual(len(summary["steps"]), g3.STEPS.index(step) + 1)
                self.assert_clean(summary)

    def test_failure_before_any_tmux_call_makes_none_and_stops_no_server(self):
        """The earliest point: the failure comes before the private server exists. The old script ran `kill-server` on the default
        server here; now no call is made at all (the private socket does not exist, so there is nothing to stop)."""
        rc, summary, err = self.smoke("--inject-failure", "setup")
        self.assertEqual(rc, 1)
        self.assertIn("step 'setup'", err)
        self.assert_clean(summary)
        self.assertEqual(records()[self.mark:], [])

    def test_missing_tmux_fails_setup_by_name_without_tmux_calls(self):
        with mock.patch.object(g3.shutil, "which", return_value=None):
            rc, summary, err = self.smoke()
        self.assertEqual(rc, 1)
        self.assertIn("step 'setup': tmux is not installed", err)
        self.assert_clean(summary)
        self.assertEqual(records()[self.mark:], [])


class TamperTest(Base):
    """Each record check fails, and by name, when the record it reads is broken."""

    def test_each_broken_record_fails_its_check(self):
        for tamper, (step, message) in TAMPERS.items():
            with self.subTest(tamper=tamper):
                rc, summary, err, _ = results()[f"tamper-{tamper}"]
                self.assertEqual(rc, 1)
                self.assertEqual(summary["failed_step"], step)
                self.assertIn(f"step '{step}'", err)
                self.assertIn(message, summary["error"])
                self.assert_clean(summary)


class GuardTest(Base):
    """kill-server is only sent when it provably reaches the run's own private server; no tmux call is made by these tests."""

    def make(self, *extra):
        args = g3.parse(["--out", str(self.out), *extra])
        smoke = g3.Smoke(args)
        self.addCleanup(shutil.rmtree, smoke.tmp, ignore_errors=True)
        if smoke.tmux_tmp:
            self.addCleanup(shutil.rmtree, smoke.tmux_tmp, ignore_errors=True)
        return smoke

    def test_fake_constructor_sets_the_private_socket_and_dir_before_any_call(self):
        smoke = self.make("--fake")
        self.assertEqual(smoke.tmux_cmd[1:], ["-L", smoke.sock])
        self.assertTrue(smoke.sock.startswith("g3-"))
        self.assertEqual(smoke.tmux_tmp.name[:4], "g3t-")
        env = smoke.env()
        self.assertEqual(env["TMUX_TMPDIR"], str(smoke.tmux_tmp))
        self.assertNotEqual(env["TMUX_TMPDIR"], str(DECOY))
        self.assertNotIn("TMUX", env)
        self.assertNotIn("TMUX_PANE", env)
        self.assertEqual(smoke.stop_private_server(), None)      # the private server never started: no call
        self.assertEqual(records()[self.mark:], [])

    def test_kill_server_refused_unless_all_three_hold(self):
        for what, breakit in {
            "no -L": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin]),
            "another socket": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin, "-L", "default"]),
            "an -S socket": lambda s: setattr(s, "tmux_cmd", [s.tmux_bin, "-S", "/tmp/x", "-L", s.sock]),
            "no private dir": lambda s: setattr(s, "tmux_tmp", Path(tempfile.gettempdir())),
            "not fake": lambda s: setattr(s, "fake", False),
        }.items():
            with self.subTest(what):
                smoke = self.make("--fake")
                breakit(smoke)
                with self.assertRaises(AssertionError):
                    smoke.stop_private_server()
        self.assertEqual(records()[self.mark:], [])

    def test_live_mode_never_stops_a_server_and_keeps_the_callers_tmux_tmpdir(self):
        smoke = self.make("--repo", str(WORK))
        self.assertFalse(smoke.fake)
        self.assertIsNone(smoke.tmux_tmp)
        self.assertEqual(smoke.tmux_cmd, [smoke.tmux_bin])
        self.assertEqual(smoke.env()["TMUX_TMPDIR"], str(DECOY))      # the caller's server, as given; TMUX/TMUX_PANE are dropped
        self.assertNotIn("TMUX", smoke.env())
        with self.assertRaises(AssertionError):
            smoke.stop_private_server()
        self.assertEqual(records()[self.mark:], [])

    def test_live_cleanup_kills_only_the_recorded_panes_and_its_own_session(self):
        smoke = self.make("--repo", str(WORK))
        smoke.session_made = True
        (smoke.state / "tasks" / "coder-x").mkdir()
        (smoke.state / "tasks" / "coder-x" / "meta.json").write_text(json.dumps({"pane": "%7"}))
        (smoke.state / "tasks" / "coder-y").mkdir()
        (smoke.state / "tasks" / "coder-y" / "meta.json").write_text(json.dumps({"pane": "-a; kill-server"}))
        calls = []
        with mock.patch.object(g3.Smoke, "quiet", lambda self, argv, **kw: calls.append([str(a) for a in argv]) or mock.Mock(returncode=0, stdout="", stderr="")):
            smoke.cleanup()
        tmux = [c[1:] for c in calls if c[0] == smoke.tmux_bin]
        self.assertIn(["kill-pane", "-t", "%7"], tmux)
        self.assertIn(["kill-session", "-t", f"={smoke.name}"], tmux)
        self.assertFalse([c for c in tmux if "kill-server" in c or "-a; kill-server" in c], tmux)
        self.assertEqual([c for c in tmux if c[0] == "kill-pane"], [["kill-pane", "-t", "%7"]])    # a pane id that is not %N is ignored

    def test_arguments(self):
        for argv in (["--out", "x"], ["--out", "x", "--tamper", "lane-record", "--repo", "r"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                g3.parse(argv)


if __name__ == "__main__":
    unittest.main()
