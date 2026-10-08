import json
import os
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

    def test_trust_guard_runs_first_and_only_before_tools(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]
        names = lambda group: [Path(h["command"].strip('"')).name for h in group["hooks"]]
        pre = hooks["PreToolUse"]
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["matcher"], "*")
        order = names(pre[0])
        self.assertEqual(order[0], "trust-guard.sh")
        self.assertLess(order.index("trust-guard.sh"), order.index("sensitive-read.sh"))
        self.assertLess(order.index("trust-guard.sh"), order.index("redact.sh"))
        self.assertEqual(order.count("trust-guard.sh"), 1)
        for event, groups in hooks.items():
            if event != "PreToolUse":
                self.assertNotIn("trust-guard.sh", [n for g in groups for n in names(g)], event)
        script = PLUGIN / "hooks" / "trust-guard.sh"
        self.assertTrue(os.access(script, os.X_OK))
        self.assertTrue(os.access(ROOT / "bin" / "tp-trust-guard", os.X_OK))

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
