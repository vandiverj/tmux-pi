import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class EscalateExtensionTests(unittest.TestCase):
    def test_local_tool_error_handoff_preserves_lineage(self):
        env = os.environ.copy()
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/escalate_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_end_confirmation_describes_retention(self):
        source = (ROOT / "agent/extensions/subagents.ts").read_text()
        self.assertIn("archives eligible worker task records, retains protected records", source)
        self.assertNotIn("clears their task records", source)


if __name__ == "__main__":
    unittest.main()
