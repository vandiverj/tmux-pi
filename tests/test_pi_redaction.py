"""pi tool_result redaction in agent/extensions/sensitive.ts; runs the real tp-redact, no models."""
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PiRedactionTests(unittest.TestCase):
    def test_tool_result_handler(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/sensitive_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
