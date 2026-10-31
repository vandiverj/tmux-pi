"""Pi reporter strips a leading <think> block from result.md; mock API, synthetic data."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReporterThinkTests(unittest.TestCase):
    def test_leading_think_is_stripped_and_mid_text_think_is_kept(self):
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/pi_reporter_think.mjs")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
