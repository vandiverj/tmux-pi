"""Exercise bin/tp-claude with a fake claude; no models, live sessions or real Claude config."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "claude" / "settings.json"


class ClaudeLauncherSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.fake = self.base / "bin"
        self.fake.mkdir()
        self.args = self.base / "args"
        claude = self.fake / "claude"
        claude.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$TEST_CLAUDE_ARGS"\n')
        claude.chmod(0o755)
        self.env = {**os.environ, "PATH": str(self.fake) + os.pathsep + os.environ["PATH"],
                    "TEST_CLAUDE_ARGS": str(self.args),
                    "TP_CLAUDE_CONFIG_DIR": str(self.base / "config")}
        for key in ("TP_TASK_DIR", "TP_MODE"):
            self.env.pop(key, None)
        self.before = SHARED.read_bytes()
        self.addCleanup(lambda: self.assertEqual(SHARED.read_bytes(), self.before))

    def run_launcher(self, *args, env=None, **extra):
        return subprocess.run([shutil.which("bash"), str(ROOT / "bin/tp-claude"), *args],
                              env={**(env or self.env), **extra},
                              text=True, capture_output=True, timeout=15)

    def launched(self, *args, **extra):
        run = self.run_launcher(*args, **extra)
        self.assertEqual(run.returncode, 0, run.stderr)
        argv = self.args.read_text().split("\0")[:-1]
        self.assertEqual(argv.count("--settings"), 1)
        return argv, argv[argv.index("--settings") + 1]

    def assert_shared_wiring(self, argv):
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(argv[argv.index("--plugin-dir") + 1], str(ROOT / "claude" / "plugins"))
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(ROOT / "claude" / "mcp.json"))
        hooks = json.loads((ROOT / "claude/plugins/tmux-pi/hooks/hooks.json").read_text())
        self.assertIn("PostToolUse", hooks["hooks"])

    def test_orchestrator_main_gets_inline_settings_with_lower_bash_limit(self):
        argv, settings = self.launched("--extra", "value", TP_MODE="orchestrator")
        composed = json.loads(settings)
        self.assertEqual(composed.pop("bashOutputMaxChars"), 10000)
        self.assertEqual(composed, json.loads(self.before))
        self.assertEqual(argv[-2:], ["--extra", "value"])
        self.assert_shared_wiring(argv)

    def test_worker_keeps_shared_settings_file(self):
        argv, settings = self.launched(TP_MODE="orchestrator", TP_TASK_DIR=str(self.base / "task"))
        self.assertEqual(settings, str(SHARED))
        self.assert_shared_wiring(argv)

    def test_strict_and_unset_mode_keep_shared_settings_file(self):
        for extra in ({"TP_MODE": "strict"}, {}):
            with self.subTest(extra=extra):
                _, settings = self.launched(**extra)
                self.assertEqual(settings, str(SHARED))

    def test_main_fails_closed_without_jq(self):
        tools = self.base / "nojq"
        tools.mkdir()
        for name in ("dirname", "readlink", "mkdir"):
            (tools / name).symlink_to(shutil.which(name))
        (tools / "claude").symlink_to(self.fake / "claude")
        env = {**self.env, "PATH": str(tools)}
        run = self.run_launcher(env=env, TP_MODE="orchestrator")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("jq is required", run.stderr)
        self.assertFalse(self.args.exists())
        # Workers do not need jq.
        run = self.run_launcher(env=env, TP_MODE="orchestrator", TP_TASK_DIR=str(self.base / "task"))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(self.args.exists())


if __name__ == "__main__":
    unittest.main()
