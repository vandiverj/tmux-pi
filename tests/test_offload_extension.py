"""Main-only tool-result offloading: extension behavior and launcher loading; no models."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pi_package():
    explicit = os.environ.get("PI_CODING_AGENT_PACKAGE")
    if explicit:
        return Path(explicit)
    pi = shutil.which("pi")
    return Path(pi).resolve().parents[2] if pi else None


class OffloadExtensionTests(unittest.TestCase):
    def test_runtime_against_installed_session_manager(self):
        package = pi_package()
        if not package or not (package / "dist/index.js").exists():
            self.skipTest("installed Pi package not found")
        env = {**os.environ, "PI_CODING_AGENT_PACKAGE": str(package)}
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE"):
            env.pop(key, None)
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/offload_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_launcher_loads_offload_for_main_only(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "pi"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            fake.chmod(0o755)
            env = {**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"], "TP_PI_NODE": ""}
            for key in ("TP_TASK_DIR", "TP_MODE"):
                env.pop(key, None)

            def args(**extra):
                run = subprocess.run([str(ROOT / "bin/tp-pi")], env={**env, **extra},
                                     capture_output=True, text=True, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr)
                return run.stdout.splitlines()

            offload = str(ROOT / "agent/extensions/offload.ts")
            self.assertIn(offload, args())
            self.assertNotIn(offload, args(TP_TASK_DIR=folder))


if __name__ == "__main__":
    unittest.main()
