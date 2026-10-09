"""bin/tp-pi seeds agent/settings.json from agent/settings.example.json only when it is missing."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class SettingsTemplateTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tp-settings-"))
        self.addCleanup(shutil.rmtree, str(self.base), True)
        (self.base / "bin").mkdir()
        (self.base / "agent").mkdir()
        shutil.copy(ROOT / "bin/tp-pi", self.base / "bin/tp-pi")
        shutil.copy(ROOT / "agent/settings.example.json", self.base / "agent/settings.example.json")
        fake = self.base / "fake"
        fake.mkdir()
        (fake / "pi").write_text("#!/bin/sh\nexit 0\n")
        (fake / "pi").chmod(0o755)
        self.env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE", "PI_CODING_AGENT_DIR"):
            self.env.pop(key, None)

    def launch(self):
        run = subprocess.run([str(self.base / "bin/tp-pi")], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_copied_when_missing(self):
        self.launch()
        self.assertEqual((self.base / "agent/settings.json").read_text(),
                         (self.base / "agent/settings.example.json").read_text())

    def test_existing_file_is_kept(self):
        (self.base / "agent/settings.json").write_text('{"theme": "light"}\n')
        self.launch()
        self.assertEqual((self.base / "agent/settings.json").read_text(), '{"theme": "light"}\n')


if __name__ == "__main__":
    unittest.main()
