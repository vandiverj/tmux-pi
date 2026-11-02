"""tests/run_bounded.py: the bound stops what it started (and only that), and every run leaves one valid JSON record."""
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


class RunParallelTests(unittest.TestCase):
    def test_default_jobs_follow_cores_and_load(self):
        with mock.patch("os.cpu_count", return_value=16):
            with mock.patch("os.getloadavg", return_value=(1.0, 1.0, 1.0)):
                self.assertEqual(run_parallel.default_jobs(), run_parallel.MAX_JOBS)  # quiet: cores//2, capped
            with mock.patch("os.getloadavg", return_value=(9.0, 1.0, 1.0)):
                self.assertEqual(run_parallel.default_jobs(), 2)  # busy
        with mock.patch("os.cpu_count", return_value=2), mock.patch("os.getloadavg", return_value=(0.0, 0, 0)):
            self.assertEqual(run_parallel.default_jobs(), 1)

    def test_module_run_is_logged_with_seconds_and_a_slow_module_is_stopped(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        tests = Path(temp.name)
        log = tests / "log.jsonl"
        (tests / "test_fast.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        (tests / "test_slow.py").write_text("import unittest, time\nclass T(unittest.TestCase):\n"
                                            "    def test_a(self): time.sleep(60)\n")
        with mock.patch.object(run_parallel, "TESTS", tests):
            fast = run_parallel.run("test_fast", timeout=20, log=log)
            slow = run_parallel.run("test_slow", timeout=1, log=log)
        self.assertEqual(fast[:3], ("test_fast", 0, 1))
        self.assertEqual(slow[:2], ("test_slow", 124))
        rows = [json.loads(l) for l in log.read_text().splitlines()]
        self.assertEqual([r["timed_out"] for r in rows], [False, True])
        self.assertIn("test_fast", rows[0]["per_file"])  # seconds per module, in run_bounded's format
        self.assertEqual(rows[0]["files"], ["test_fast"])


if __name__ == "__main__":
    unittest.main()
