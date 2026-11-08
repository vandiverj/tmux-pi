"""tests/tp_agent_inproc.run gives the same rc, stdout and stderr as a bin/tp-agent subprocess (five commands, one per exit path)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tp_agent_inproc  # noqa: E402
from session_env import outside_session  # noqa: E402

AGENT = tp_agent_inproc.AGENT
COMMANDS = {
    "list (rc 0, json)": ["list"],
    "queue-view (rc 0, empty)": ["queue-view"],
    "index (rc 0)": ["index"],
    "inspect missing task (rc 1, tp-agent: error)": ["inspect", "no-such-task"],
    "unknown command (rc 2, argparse usage)": ["bogus"],
}


def stable(text):
    """Output with the clock removed: `index` prints the current second, which can differ between the two runs."""
    return re.sub(r'("updated": )"[^"]*"', r'\1"T"', text)


class InProcessMatchesSubprocess(unittest.TestCase):
    def test_same_rc_stdout_stderr(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cwd:
            env = {**outside_session(), "TP_STATE_DIR": state}
            env.pop("TMUX", None)
            times = []
            for name, argv in COMMANDS.items():
                with self.subTest(name):
                    t0 = time.monotonic()
                    sub = subprocess.run([sys.executable, str(AGENT), *argv], env=env, cwd=cwd, text=True, capture_output=True)
                    t1 = time.monotonic()
                    rc, out, err = tp_agent_inproc.run(argv, env, cwd)
                    t2 = time.monotonic()
                    times.append((name, t1 - t0, t2 - t1))
                    self.assertEqual((rc, stable(out), err), (sub.returncode, stable(sub.stdout), sub.stderr))
            for name, sub_s, in_s in times:
                print(f"  {name}: subprocess {sub_s:.3f}s, in-process {in_s:.3f}s", file=sys.stderr)

    def test_env_and_cwd_are_restored(self):
        before_env, before_cwd = dict(os.environ), os.getcwd()
        with tempfile.TemporaryDirectory() as state:
            tp_agent_inproc.run(["list"], {"TP_STATE_DIR": state, "ONLY_HERE": "1"}, state)
            tp_agent_inproc.run(["bogus"], {"TP_STATE_DIR": state})
        self.assertEqual((dict(os.environ), os.getcwd()), (before_env, before_cwd))

    def test_main_with_os_environ_keeps_the_environment(self):
        agent = tp_agent_inproc.load()
        with tempfile.TemporaryDirectory() as state:
            with mock.patch.dict(os.environ, {"TP_STATE_DIR": state}):
                with contextlib.redirect_stdout(io.StringIO()):
                    agent.main(["list"], os.environ)
                self.assertEqual(os.environ.get("TP_STATE_DIR"), state)

    def test_invalid_env_value_leaves_os_environ_intact(self):
        agent = tp_agent_inproc.load()
        before = dict(os.environ)
        with self.assertRaises(TypeError):
            agent.main(["list"], {"TP_STATE_DIR": 5})
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
