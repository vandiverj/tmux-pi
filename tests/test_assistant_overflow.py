"""ask_local keeps an over-cap reply on disk and returns an excerpt plus an evidence manifest; no models."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pi_package():
    explicit = os.environ.get("PI_CODING_AGENT_PACKAGE")
    if explicit:
        return Path(explicit)
    pi = shutil.which("pi")
    return Path(pi).resolve().parents[2] if pi else None


class AssistantOverflowTests(unittest.TestCase):
    def test_runtime(self):
        package = pi_package()
        if not package or not (package / "dist/index.js").exists():
            self.skipTest("installed Pi package not found")
        env = {**os.environ, "PI_CODING_AGENT_PACKAGE": str(package)}
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE", "TP_STATE_DIR"):
            env.pop(key, None)
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/assistant_overflow_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("ok", run.stdout)


if __name__ == "__main__":
    unittest.main()
