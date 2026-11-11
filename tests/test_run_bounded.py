"""tests/run_bounded.py: the bound stops what it started (and only that), and every run leaves one valid JSON record."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import run_bounded
import run_parallel

FIELDS = {"cmd", "files", "seconds", "rc", "timed_out", "load1", "per_file", "leftovers"}


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RunBoundedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.log = self.dir / "t" / "tests.jsonl"

    def run_py(self, code, timeout=10, grace=1, **kw):
        return run_bounded.run_bounded([sys.executable, "-c", code], timeout=timeout, grace=grace, log=self.log,
                                       echo=False, **kw)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assertGone(self, pids):
        """Wait for the event (each pid gone, reaped by init once killed), with a 10 s cap."""
        deadline = time.monotonic() + 10
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([p for p in pids if alive(p)], [])

    def leader_leaves_child(self, child, extra=""):
        """Code for a leader that starts `child` in its own group, waits until the child has written its pid (after
        its setup), and exits 0. Returns (code, pidfile); the cleanup kills the child if it is still there."""
        pidfile = self.dir / "child"
        child = child.replace("READY", f"import os; open({str(pidfile)!r} + '.tmp', 'w').write(str(os.getpid())); "
                                       f"os.rename({str(pidfile)!r} + '.tmp', {str(pidfile)!r})")
        code = ("import os, subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}]{extra})\n"
                f"while not os.path.exists({str(pidfile)!r}): time.sleep(0.01)\n")

        def cleanup():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.addCleanup(cleanup)
        return code, pidfile

    # Rule rows (see run_bounded.run_bounded): leader exited / group empty / deadline / reader done.
    def test_child_the_leader_left_is_waited_for_and_its_output_kept(self):
        # leader exits; a same-group child still holds stdout; it ends before the deadline
        code, pidfile = self.leader_leaves_child("READY\nimport time; time.sleep(1); print('late', flush=True)")
        record, output = self.run_py(code, timeout=20, grace=0.2)
        self.assertEqual((record["rc"], record["timed_out"]), (0, False))
        self.assertEqual(output, "late\n")  # read after the leader exited and after the reader's grace
        self.assertFalse(alive(int(pidfile.read_text())))  # the run ended only once the group was empty

    def test_child_the_leader_left_past_the_bound_is_killed_and_the_run_times_out(self):
        # the reviewer's case: leader exits 0; a same-group child holds stdout, ignores SIGTERM, sleeps
        code, pidfile = self.leader_leaves_child(
            "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nREADY\ntime.sleep(60)")
        started = time.monotonic()
        record, _ = self.run_py(code, timeout=1.5, grace=0.3)
        self.assertLess(time.monotonic() - started, 1.5 + 3 * 0.3 + 4)  # bound + TERM grace + KILL wait + reader
        self.assertEqual((record["rc"], record["timed_out"]), (None, True))
        self.assertEqual(self.records()[-1], record)
        self.assertGone([int(pidfile.read_text())])

    def test_child_with_its_output_closed_is_still_part_of_the_run(self):
        # leader exits; the reader has seen end of file; a same-group child lives on past the deadline
        code, pidfile = self.leader_leaves_child(
            "READY\nimport time; time.sleep(60)",
            extra=", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL")
        record, _ = self.run_py(code, timeout=1.5, grace=0.3)
        self.assertEqual((record["rc"], record["timed_out"]), (None, True))
        self.assertGone([int(pidfile.read_text())])

    def test_holder_outside_the_group_does_not_hold_a_finished_run(self):
        # leader exits and the group is empty; a process in its own session holds the pipe: not ours to wait for
        code, pidfile = self.leader_leaves_child("READY\nimport time; time.sleep(30)", extra=", start_new_session=True")
        started = time.monotonic()
        record, _ = self.run_py(code, timeout=20, grace=0.5)
        self.assertLess(time.monotonic() - started, 0.5 + 4)  # the reader's grace, not the 30 s holder
        self.assertEqual((record["rc"], record["timed_out"]), (0, False))
        self.assertTrue(alive(int(pidfile.read_text())))  # outside the group: never signalled

    # Resources x exit paths: pipe read end, pipe write end, reader thread, process group. Each is released on launch
    # failure, normal end, timeout and interrupt; these tests count descriptors and threads over repeated runs.
    @staticmethod
    def open_fds():
        for base in ("/dev/fd", "/proc/self/fd"):
            if os.path.isdir(base):
                return len(os.listdir(base))
        raise unittest.SkipTest("no fd directory")

    def assertStableAcross(self, action, repeat=8):
        action()  # warm up one-time allocations
        fds, threads = self.open_fds(), threading.active_count()
        for _ in range(repeat):
            action()
        self.assertEqual((self.open_fds(), threading.active_count()), (fds, threads))

    def test_launch_failure_leaks_no_descriptor(self):
        def fail():
            with self.assertRaises(OSError):
                run_bounded.run_bounded([str(self.dir / "no-such-command")], log=False, echo=False)
        self.assertStableAcross(fail)

    def test_interrupt_stops_the_group_and_leaks_no_descriptor_or_thread(self):
        def interrupt():
            with mock.patch.object(run_bounded, "wait_group", side_effect=[KeyboardInterrupt, True, True]), \
                    mock.patch.object(run_bounded, "stop_group", wraps=run_bounded.stop_group) as stopped:
                with self.assertRaises(KeyboardInterrupt):
                    run_bounded.run_bounded([sys.executable, "-c", "import time; time.sleep(60)"], grace=1,
                                            log=False, echo=False)
            stopped.assert_called_once()
        self.assertStableAcross(interrupt, repeat=3)

    def test_normal_and_timeout_runs_leak_no_descriptor_or_thread(self):
        self.assertStableAcross(lambda: self.run_py("print(1)"))
        self.assertStableAcross(lambda: self.run_py("import time; time.sleep(60)", timeout=0.3, grace=1), repeat=2)

    def test_clean_run_is_recorded_as_valid_json(self):
        record, output = self.run_py("print('hi')", files=["tests/test_x.py"])
        self.assertEqual(output, "hi\n")
        (logged,) = self.records()  # the folder was made, one line written
        self.assertEqual(logged, record)
        self.assertEqual(set(logged), FIELDS)
        self.assertEqual((logged["rc"], logged["timed_out"], logged["files"]), (0, False, ["tests/test_x.py"]))
        self.assertIsInstance(logged["seconds"], float)
        self.assertEqual(logged["leftovers"], [])

    def test_exit_code_and_unittest_seconds_are_recorded(self):
        record, _ = self.run_py("import sys; print('Ran 3 tests in 1.250s', file=sys.stderr); sys.exit(3)",
                                files=["tests/test_x.py"])
        self.assertEqual(record["rc"], 3)
        self.assertEqual(record["per_file"], {"tests/test_x.py": 1.25})
        self.run_py("pass")
        self.assertEqual(len(self.records()), 2)  # a second run appends

    def test_timeout_is_recorded_and_stops_a_child_that_cancels_its_own_alarm(self):
        code = "import signal, time; signal.alarm(0); time.sleep(60)"
        started = time.monotonic()
        record, _ = self.run_py(code, timeout=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertTrue(record["timed_out"])
        self.assertIsNone(record["rc"])  # the bound was hit: the leader's rc is not the run's
        self.assertTrue(self.records()[-1]["timed_out"])

    def test_term_ignoring_child_and_grandchild_in_the_group_are_killed(self):
        pidfile = self.dir / "pids"
        code = ("import os, signal, subprocess, sys, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "g = subprocess.Popen([sys.executable, '-c', 'import signal, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
                f"open({str(pidfile)!r}, 'w').write(f'{{os.getpid()}} {{g.pid}}')\n"
                "time.sleep(60)")
        record, _ = self.run_py(code, timeout=2)
        self.assertTrue(record["timed_out"])
        self.assertIsNone(record["rc"])
        self.assertGone([int(p) for p in pidfile.read_text().split()])

    def test_grandchild_holding_stdout_cannot_keep_the_runner_past_the_bound(self):
        # the leader sleeps; a grandchild in its own session (outside the group) keeps the output pipe open
        pidfile = self.dir / "holder"
        code = ("import subprocess, sys, time\n"
                "h = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)\n"
                f"open({str(pidfile)!r}, 'w').write(str(h.pid))\n"
                "time.sleep(60)")
        started = time.monotonic()
        record, _ = self.run_py(code, timeout=2, files=["tests/test_x.py"])
        elapsed = time.monotonic() - started
        try:
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.assertTrue(record["timed_out"])
        self.assertLess(elapsed, 2 + 1 + 1 + 4)  # timeout + grace + reader wait, plus slack; the holder lives 30 s
        self.assertEqual(self.records()[-1], record)

    def test_timeout_fills_per_file_with_the_seconds_it_ran(self):
        record, _ = self.run_py("import time; time.sleep(60)", timeout=1, files=["tests/test_x.py"])
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["per_file"], {"tests/test_x.py": record["seconds"]})

    def test_only_its_own_group_is_signalled(self):
        bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        record, _ = self.run_py("import time; time.sleep(60)", timeout=1)
        self.assertTrue(record["timed_out"])
        self.assertIsNone(bystander.poll())

    def test_command_line_exit_codes_and_default_log(self):
        env = {**os.environ, "TASK_DIR": str(self.dir)}
        env.pop("TMUX_TMPDIR", None)
        script = str(TESTS / "run_bounded.py")
        slow = subprocess.run([sys.executable, script, "--timeout", "1", "--grace", "1", "--",
                               sys.executable, "-c", "import time; time.sleep(60)"],
                              env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(slow.returncode, 124)
        ok = subprocess.run([sys.executable, script, "--", sys.executable, "-c", "pass"],
                            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        rows = [json.loads(l) for l in (self.dir / "tests.jsonl").read_text().splitlines()]  # $TASK_DIR/tests.jsonl
        self.assertEqual([r["timed_out"] for r in rows], [True, False])
        (self.dir / "test_tiny.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                               "    def test_a(self): pass\n")
        files = subprocess.run([sys.executable, script, "--durations", "1", str(self.dir / "test_tiny.py")],
                               env=env, capture_output=True, text=True, timeout=30)  # file mode passes --durations on
        self.assertEqual(files.returncode, 0, files.stderr)
        self.assertIn("Ran 1 test", files.stdout)
        last = json.loads((self.dir / "tests.jsonl").read_text().splitlines()[-1])
        self.assertEqual(list(last["per_file"]), [str(self.dir / "test_tiny.py")])

    def test_leftover_tmux_socket_is_reported_and_left_alone(self):
        tmpdir = self.dir / "tmx"
        sockets = tmpdir / f"tmux-{os.getuid()}"
        sockets.mkdir(parents=True)
        (sockets / "old").touch()
        os.environ["TMUX_TMPDIR"] = str(tmpdir)
        self.addCleanup(os.environ.pop, "TMUX_TMPDIR")
        record, _ = self.run_py(f"open({str(sockets / 'new')!r}, 'w').close()")
        self.assertEqual(record["leftovers"], ["new"])  # "old" was there before the run
        self.assertTrue((sockets / "new").exists())
        # env= is the child's environment, and its TMUX_TMPDIR (not this process's) is where leftovers are looked for
        other = self.dir / "own" / f"tmux-{os.getuid()}"
        other.mkdir(parents=True)
        record, _ = self.run_py(f"import os; open(os.path.join(os.environ['TMUX_TMPDIR'], 'tmux-{os.getuid()}', 'mine'), 'w')",
                                env=dict(os.environ, TMUX_TMPDIR=str(other.parent)))
        self.assertEqual((record["rc"], record["leftovers"]), (0, ["mine"]))


PASSING = """import os, unittest
class T(unittest.TestCase):
    def test_env(self):
        # the caller's session keys are gone, a caller's opt-in passes through, and the temp dirs are the child's own
        self.assertEqual([k for k in ("TMUX", "TMUX_PANE", "TP_SESSION", "TP_TASK_ID", "TP_TASK_DIR", "TP_STATE_DIR")
                          if k in os.environ], [])
        self.assertEqual(os.environ.get("TP_LIVE_FAKE"), "1")
        self.assertTrue(os.environ["TMUX_TMPDIR"].startswith("/tmp/tprp"), os.environ["TMUX_TMPDIR"])
        self.assertEqual(os.environ["TMPDIR"], os.environ["RP_CALLER_TMP"])  # the caller's, as serially
        with open(os.path.join(os.environ["RP_CALLER_TMP"], "tmux_tmpdir"), "w") as handle:
            handle.write(os.environ["TMUX_TMPDIR"])
    @unittest.skip("counted as skipped")
    def test_skip(self): pass
"""
CALLER = {"TMUX": "/tmp/caller,1,0", "TMUX_PANE": "%9", "TP_SESSION": "s", "TP_TASK_ID": "t", "TP_TASK_DIR": "/nowhere",
          "TP_STATE_DIR": "/nowhere", "TP_LIVE_FAKE": "1"}


class RunParallelTests(unittest.TestCase):
    """tests/run_parallel.py against a tiny tests dir: same ids as the loader, same verdict, isolated children."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.tests = self.dir / "tests"
        self.tests.mkdir()

    def write(self, name, body):
        (self.tests / (name + ".py")).write_text(body)

    def main(self, *argv):
        out = io.StringIO()
        caller_tmp = self.dir / "caller-tmp"
        caller_tmp.mkdir(exist_ok=True)
        caller = dict(CALLER, TMPDIR=str(caller_tmp), RP_CALLER_TMP=str(caller_tmp))
        with mock.patch.dict(os.environ, caller), contextlib.redirect_stdout(out):
            code = run_parallel.main(["--times", str(self.dir / "times.json"), "--log", str(self.dir / "log.jsonl"),
                                      "--outcomes-out", str(self.dir / "outcomes")] + list(argv), tests=self.tests)
        return code, out.getvalue()

    def loader_ids(self):
        """The ids `unittest.TestLoader().discover(tests, 'test_*.py')` lists, loaded in this process."""
        names = [p.stem for p in self.tests.glob("test_*.py")]
        self.addCleanup(lambda: [sys.modules.pop(n, None) for n in names])
        self.addCleanup(sys.path.remove, str(self.tests))  # discover put it there

        def flat(suite):
            for t in suite:
                yield from flat(t) if isinstance(t, unittest.TestSuite) else [t]
        return sorted(t.id() for t in flat(unittest.TestLoader().discover(str(self.tests), "test_*.py")))

    def test_default_jobs_follow_cores(self):
        self.assertEqual([run_parallel.default_jobs(c) for c in (18, 16, 4, 1)], [8, 8, 2, 1])

    def test_failing_crashing_and_hanging_modules_fail_the_run_with_their_ids_and_totals(self):
        self.write("test_rp_pass", PASSING)
        self.write("test_rp_fail", "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): pass\n"
                                   "    def test_bad(self): self.assertEqual(1, 2)\n"
                                   "class C(unittest.TestCase):\n"
                                   "    @classmethod\n    def setUpClass(cls): raise RuntimeError('fixture')\n"
                                   "    def test_c(self): pass\n")
        self.write("test_rp_exit", "import os, unittest\nclass T(unittest.TestCase):\n    def test_a(self): os._exit(3)\n")
        self.write("test_rp_hang", "import time, unittest\nclass T(unittest.TestCase):\n"
                                   "    def test_a(self): time.sleep(60)\n")
        with mock.patch.object(run_parallel, "max_jobs", return_value=4):  # four jobs on any host, two-core included
            code, out = self.main("-j", "4", "--timeout", "3")
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.exists((self.dir / "caller-tmp" / "tmux_tmpdir").read_text()))  # removed afterwards
        lines = out.rstrip().splitlines()
        self.assertRegex(lines[-3], r"^Ran 4 tests in [0-9.]+s$")  # 2 + 2 with results; a skipped test counts as run
        # errors: the class fixture, the unit that exited without a result, the unit that hung
        self.assertEqual(lines[-1], "FAILED (failures=1, errors=3, skipped=1)", out)
        self.assertEqual(dict(line.rsplit(" ", 1) for line in (self.dir / "outcomes").read_text().splitlines()), {
            "test_rp_pass.T.test_env": "ok", "test_rp_pass.T.test_skip": "skip", "test_rp_fail.T.test_ok": "ok",
            "test_rp_fail.T.test_bad": "fail", "test_rp_fail.C.test_c": "not-run", "setUpClass (test_rp_fail.C)": "error",
            "test_rp_exit.T.test_a": "unit-error", "test_rp_hang.T.test_a": "unit-error"})
        self.assertIn("FAIL: test_rp_fail.T.test_bad\n" + "-" * 70 + "\nTraceback", out)
        self.assertIn("AssertionError: 1 != 2", out)
        self.assertIn("ERROR: test_rp_exit (the unit's process): wrote no unittest result (exit 3)", out)
        self.assertIn("ERROR: test_rp_hang (the unit's process): hit the 3s timeout", out)
        self.assertRegex(out, r"\s[0-9.]+s  ok    test_rp_pass \(2 tests\)")  # per-module seconds and result
        self.assertNotIn("test_rp_pass.T.test_env", out)  # the env checks held in the child
        rows = {r["files"][0]: r for r in map(json.loads, (self.dir / "log.jsonl").read_text().splitlines())}
        self.assertEqual({m: r["timed_out"] for m, r in rows.items()},
                         {"test_rp_pass": False, "test_rp_fail": False, "test_rp_exit": False, "test_rp_hang": True})
        times = json.loads((self.dir / "times.json").read_text())
        self.assertLess(times["test_rp_pass"], times["test_rp_hang"])  # the next run starts the hang first

    def test_ids_equal_the_loaders_and_a_slow_module_is_split_by_class(self):
        self.write("test_rp_pass", PASSING)
        self.write("test_rp_two", "import unittest\nclass A(unittest.TestCase):\n    def test_1(self): pass\n"
                                  "    def test_2(self): pass\nclass B(unittest.TestCase):\n    def test_1(self): pass\n")
        (self.dir / "times.json").write_text(json.dumps({"test_rp_two": 999}))  # over --split-seconds
        code, out = self.main("--split-seconds", "100")
        self.assertEqual(code, 0, out)
        self.assertEqual(out.rstrip().splitlines()[-1], "OK (skipped=1)")
        self.assertIn("5 tests in 3 units from 2 modules", out)
        self.assertEqual([line.split()[0] for line in (self.dir / "outcomes").read_text().splitlines()], self.loader_ids())
        times = json.loads((self.dir / "times.json").read_text())
        self.assertEqual(sorted(k for k in times if k.startswith("test_rp_two")),
                         ["test_rp_two", "test_rp_two.A", "test_rp_two.B"])

    def test_plan_starts_longest_first_and_unrecorded_modules_by_size(self):
        groups = [("test_a", ["test_a.T.t"]), ("test_b", ["test_b.T.t"]), ("test_c", ["test_c.T.t"]),
                  ("test_d", ["test_d.T.t"])]
        units = run_parallel.plan(groups, {"test_a": 5, "test_b": 50}, {"test_c": 10, "test_d": 900})
        self.assertEqual([u["name"] for u in units], ["test_d", "test_c", "test_b", "test_a"])
        split = run_parallel.plan([("test_x", ["test_x.A.t1", "test_x.A.t2", "test_x.B.t"])], {"test_x": 300}, {}, 240)
        self.assertEqual([(u["name"], u["ids"], u["estimate"]) for u in split],
                         [("test_x.A", ["test_x.A.t1", "test_x.A.t2"], 200), ("test_x.B", ["test_x.B.t"], 100)])

    def test_serial_runs_the_one_process_discovery_without_the_callers_session(self):
        with mock.patch.dict(os.environ, CALLER), mock.patch.object(run_parallel.subprocess, "call", return_value=7) as call:
            self.assertEqual(run_parallel.main(["--serial"], tests=self.tests), 7)
        (cmd,), kwargs = call.call_args
        self.assertEqual(cmd, [sys.executable, "-m", "unittest", "discover", "-s", str(self.tests), "-p", "test_*.py"])
        self.assertEqual(sorted(set(CALLER) & set(kwargs["env"])), ["TP_LIVE_FAKE"])


if __name__ == "__main__":
    unittest.main()
