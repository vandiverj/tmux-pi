"""Pi reporter by-model usage, model switch and restart restore; mock API, synthetic data."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class ReporterUsageTests(unittest.TestCase):
    def test_runtime(self):
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/reporter_runtime.mjs")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
