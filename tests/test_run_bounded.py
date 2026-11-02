"""tests/run_bounded.py: the bound stops what it started (and only that), and every run leaves one valid JSON record."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
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

    def run_py(self, code, timeout=10, **kw):
        return run_bounded.run_bounded([sys.executable, "-c", code], timeout=timeout, grace=1, log=self.log,
                                       echo=False, **kw)

    def records(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

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
        self.assertNotEqual(record["rc"], 0)
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
        self.assertEqual(record["rc"], -signal.SIGKILL)
        pids = [int(p) for p in pidfile.read_text().split()]
        deadline = time.monotonic() + 10
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([p for p in pids if alive(p)], [])

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
