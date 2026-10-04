"""Pi /refresh-status command: main-only registration, stub script success/failure/timeout; mock API."""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class OrchestrationRefreshTests(unittest.TestCase):
    def test_runtime(self):
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/orchestration_refresh_runtime.mjs")],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
