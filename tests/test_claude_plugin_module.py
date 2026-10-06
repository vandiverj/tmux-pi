import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "claude" / "plugins" / "tmux-pi"
CLAUDE = shutil.which("claude")


class ClaudePluginModuleTests(unittest.TestCase):
    def test_hooks_json_keeps_classic_hooks_beside_the_module(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
        self.assertEqual(hooks["modules"], ["./register.tsx"])
        self.assertTrue((PLUGIN / "hooks" / "register.tsx").is_file())
        # The cross-harness contract stays in the command hooks.
        commands = json.dumps(hooks["hooks"])
        self.assertIn("report.sh", commands)
        self.assertIn("sensitive-read.sh", commands)

    def test_manifest_names_the_state_contract(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["types"], "./types/index.d.ts")
        self.assertTrue((PLUGIN / "types" / "index.d.ts").is_file())

    def test_generated_types_are_ignored(self):
        ignored = (ROOT / ".gitignore").read_text().splitlines()
        self.assertIn("claude/plugins/tmux-pi/.claude-plugin/types/", ignored)

    @unittest.skipUnless(CLAUDE, "claude is not installed")
    def test_plugin_validates(self):
        # Validate the plugin folder itself; its parent has no manifest.
        ran = subprocess.run([CLAUDE, "plugin", "validate", str(PLUGIN)], capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("./register.tsx hooks: session.start", ran.stdout)

    @unittest.skipUnless(CLAUDE, "claude is not installed")
    def test_plugin_tests_pass(self):
        ran = subprocess.run([CLAUDE, "plugin", "test", str(PLUGIN)], capture_output=True, text=True, timeout=180)
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
