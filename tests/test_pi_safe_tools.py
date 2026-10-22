"""pi safe_read / safe_grep (agent/extensions/safe.ts) and their sensitive.ts gate; runs the real tp-redact, no models."""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pi_package():
    explicit = os.environ.get("PI_CODING_AGENT_PACKAGE")
    if explicit:
        return Path(explicit)
    pi = shutil.which("pi")
    return Path(pi).resolve().parents[2] if pi else None


class PiSafeToolsTests(unittest.TestCase):
    def test_runtime_against_installed_pi_packages(self):
        package = pi_package()
        if not package or not (package / "dist/index.js").exists():
            self.skipTest("installed Pi package not found")
        env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
        env["PI_CODING_AGENT_PACKAGE"] = str(package)
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/safe_runtime.mjs")],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_tilde_home_is_the_guards_account_home(self):
        # `~` in the tools and in the trust-store guard must lead to one home: the password database's, whatever $HOME says.
        env = dict(os.environ, HOME="/nonexistent-home-for-test")
        run = subprocess.run(["node", "-e", "process.stdout.write(require('os').userInfo().homedir)"],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        sys.path.insert(0, str(ROOT / "lib"))
        try:
            import trust
        finally:
            sys.path.remove(str(ROOT / "lib"))
        self.assertEqual(run.stdout, trust.home_dir())
        for source in ("agent/extensions/safe.ts", "claude/mcp/safe.mjs"):
            text = (ROOT / source).read_text()
            self.assertIn("userInfo().homedir", text)
            self.assertNotIn("homedir()", text.replace("userInfo().homedir", ""), source)
            self.assertIn('"--no-config", "--no-follow"', text, source)

    def test_tools_have_the_claude_servers_contract(self):
        # Same parameters as claude/mcp/safe.mjs, read from the two sources; `reveal` is not ported.
        server = (ROOT / "claude/mcp/safe.mjs").read_text()
        source = (ROOT / "agent/extensions/safe.ts").read_text()
        self.assertNotIn('name: "reveal"', source)
        for tool, params in (("safe_read", ("path", "offset", "limit")),
                             ("safe_grep", ("pattern", "path", "glob", "ignore_case", "include_ignored", "max_lines"))):
            self.assertIn('name: "%s"' % tool, source)
            for param in params:
                self.assertRegex(server, r"\b%s:" % param)
                self.assertRegex(source, r"\b%s: Type\." % param)
        for limit in ("MAX_FILE = 4_000_000", "MAX_CHARS = 200_000"):
            self.assertIn(limit, server)
            self.assertIn(limit, source)


if __name__ == "__main__":
    unittest.main()
