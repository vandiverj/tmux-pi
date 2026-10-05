"""Goal extension continuation, settlement and orphan reconcile; fake Pi and child-process boundary."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node is required for the goal runtime harness")
class GoalRuntimeTests(unittest.TestCase):
    def test_runtime(self):
        env = os.environ.copy()
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TP_STATE_DIR", "TP_SESSION", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/goal_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("goal runtime fake boundary ok", run.stdout)


if __name__ == "__main__":
    unittest.main()
