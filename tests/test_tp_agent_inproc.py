"""tests/tp_agent_inproc.run gives the same rc, stdout and stderr as a bin/tp-agent subprocess (five commands, one per exit path)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tp_agent_inproc  # noqa: E402

AGENT = tp_agent_inproc.AGENT
COMMANDS = {
    "list (rc 0, json)": ["list"],
    "queue-view (rc 0, empty)": ["queue-view"],
    "index (rc 0)": ["index"],
    "inspect missing task (rc 1, tp-agent: error)": ["inspect", "no-such-task"],
    "unknown command (rc 2, argparse usage)": ["bogus"],
}


class InProcessMatchesSubprocess(unittest.TestCase):
    def test_same_rc_stdout_stderr(self):
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cwd:
            env = {**os.environ, "TP_STATE_DIR": state}
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
                    self.assertEqual((rc, out, err), (sub.returncode, sub.stdout, sub.stderr))
            for name, sub_s, in_s in times:
                print(f"  {name}: subprocess {sub_s:.3f}s, in-process {in_s:.3f}s", file=sys.stderr)

    def test_env_and_cwd_are_restored(self):
        before_env, before_cwd = dict(os.environ), os.getcwd()
        with tempfile.TemporaryDirectory() as state:
            tp_agent_inproc.run(["list"], {"TP_STATE_DIR": state, "ONLY_HERE": "1"}, state)
            tp_agent_inproc.run(["bogus"], {"TP_STATE_DIR": state})
        self.assertEqual((dict(os.environ), os.getcwd()), (before_env, before_cwd))


if __name__ == "__main__":
    unittest.main()
