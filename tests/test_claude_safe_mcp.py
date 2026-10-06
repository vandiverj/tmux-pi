"""claude/mcp/safe.mjs over stdio: redacted reads and searches, and reveal. Synthetic env; no model, tmux or credentials."""
import json
import os
from pathlib import Path
import random
import shutil
import string
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "claude" / "mcp" / "safe.mjs"
HOOK = ROOT / "claude/plugins/tmux-pi/hooks/redact.sh"
NODE = shutil.which("node")
LAUNCH = "launch-test"


def fake(prefix, n=32, seed=0):
    rng = random.Random(prefix + str(seed))
    return prefix + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(n))


SECRET = fake("sk_live_")
AWS = fake("", 40, 3)


@unittest.skipUnless(NODE, "node is not installed")
class SafeMcpTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.home, self.proj, self.store = base / "home", base / "proj", base / "store"
        for d in (self.home / ".aws", self.proj / "apps/web"):
            d.mkdir(parents=True)
        self.prod = self.proj / "apps/web/.env.production"
        self.prod.write_text("API_URL=https://example.com\nCLERK_SECRET_KEY={}\n".format(SECRET))
        (self.home / ".aws/credentials").write_text("[default]\naws_access_key_id = AKIA{}\naws_secret_access_key = {}\n".format(
            "ABCDEFGHIJKLMNOP", AWS))
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.home), "TP_REDACTION_DIR": str(self.store),
                    "TP_REDACTION_LAUNCH": LAUNCH, "TP_REDACTION_LLM_URL": "http://127.0.0.1:9"}

    def rpc(self, *calls, env=None):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        for n, (name, arguments) in enumerate(calls, 2):
            requests.append({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        run = subprocess.run([NODE, str(SERVER)], input="".join(json.dumps(r) + "\n" for r in requests), text=True,
                             capture_output=True, timeout=60, cwd=self.proj, env=env or self.env)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {m["id"]: m["result"] for m in map(json.loads, run.stdout.splitlines())}
        return [(replies[n]["content"][0]["text"], bool(replies[n].get("isError"))) for n in range(2, len(calls) + 2)]

    def test_tools_and_consent_flag(self):
        run = subprocess.run([NODE, str(SERVER)], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
                             text=True, capture_output=True, timeout=15, env=self.env)
        tools = {t["name"]: t for t in json.loads(run.stdout)["result"]["tools"]}
        self.assertEqual(set(tools), {"safe_read", "safe_grep", "reveal"})
        self.assertTrue(tools["reveal"]["_meta"]["anthropic/requiresUserInteraction"])
        self.assertNotIn("_meta", tools["safe_read"])
        self.assertNotIn("_meta", tools["safe_grep"])

    def test_safe_read_returns_placeholders_for_a_file_the_normal_read_refuses(self):
        (text, error), = self.rpc(("safe_read", {"path": str(self.prod)}))
        self.assertFalse(error)
        self.assertNotIn(SECRET, text)
        self.assertIn("CLERK_SECRET_KEY=<REDACTED:", text)
        self.assertIn("API_URL=https://example.com", text)
        self.assertRegex(text.splitlines()[1], r"^\s+2\t")
        (limited, _), = self.rpc(("safe_read", {"path": "apps/web/.env.production", "offset": 2, "limit": 1}))
        self.assertEqual(len(limited.splitlines()), 1)
        self.assertRegex(limited, r"^\s+2\tCLERK_SECRET_KEY=<REDACTED:")

    def test_safe_read_handles_a_credential_store(self):
        (text, error), = self.rpc(("safe_read", {"path": "~/.aws/credentials"}))
        self.assertFalse(error)
        self.assertNotIn(AWS, text)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", text)
        self.assertIn("[default]", text)

    def test_safe_read_errors_do_not_leak_and_fail_closed(self):
        (text, error), = self.rpc(("safe_read", {"path": "apps/web"}))
        self.assertTrue(error)
        (text, error), = self.rpc(("safe_read", {"path": str(self.prod)}), env={**self.env, "TP_REDACTION_DIR": "/dev/null/x"})
        self.assertTrue(error)
        self.assertNotIn(SECRET, text)
        self.assertIn("withheld", text)

    def test_safe_grep_searches_hidden_env_files_with_redaction(self):
        (text, error), = self.rpc(("safe_grep", {"pattern": "CLERK_SECRET", "path": "."}))
        self.assertFalse(error)
        self.assertNotIn(SECRET, text)
        self.assertRegex(text, r"\.env\.production:2:CLERK_SECRET_KEY=<REDACTED:")
        (none, _), = self.rpc(("safe_grep", {"pattern": "zzz-no-such-text"}))
        self.assertEqual(none, "(no matches)")
        (capped, _), = self.rpc(("safe_grep", {"pattern": "=", "max_lines": 1}))
        self.assertEqual(len(capped.splitlines()), 2)  # one match plus the "more lines" note

    def test_reveal_returns_the_value_only_for_a_known_placeholder(self):
        (read, _), = self.rpc(("safe_read", {"path": str(self.prod)}))
        name = "<REDACTED:" + read.split("<REDACTED:")[1].split(">")[0] + ">"
        (revealed, error), (bad, bad_error), (shape, shape_error) = self.rpc(
            ("reveal", {"placeholder": name, "reason": "check the prefix"}),
            ("reveal", {"placeholder": "<REDACTED:" + "nothing_9" + ">", "reason": "x"}),
            ("reveal", {"placeholder": "not a placeholder", "reason": "x"}))
        self.assertFalse(error)
        self.assertEqual(revealed, "{} = {}".format(name, SECRET))
        self.assertTrue(bad_error and shape_error)
        self.assertNotIn(SECRET, bad + shape)
        (_, no_reason), = self.rpc(("reveal", {"placeholder": name, "reason": " "}))
        self.assertTrue(no_reason)

    def test_placeholders_match_the_hooks_for_the_same_launch(self):
        (read, _), = self.rpc(("safe_read", {"path": str(self.prod)}))
        event = {"hook_event_name": "PostToolUse", "tool_name": "Read", "cwd": str(self.proj), "session_id": "other-session",
                 "tool_input": {"file_path": str(self.prod)},
                 "tool_response": {"type": "text", "file": {"filePath": str(self.prod), "content": self.prod.read_text(),
                                                            "numLines": 2, "startLine": 1, "totalLines": 2}}}
        run = subprocess.run([str(HOOK)], input=json.dumps(event), text=True, capture_output=True, timeout=30,
                             env={**os.environ, **self.env, "TP_REDACTION": "on"})
        hooked = json.loads(run.stdout)["hookSpecificOutput"]["updatedToolOutput"]["file"]["content"]
        name = "<REDACTED:" + read.split("<REDACTED:")[1].split(">")[0] + ">"
        self.assertIn(name, hooked)
        self.assertTrue((self.store / (LAUNCH + ".json")).is_file())
        self.assertFalse((self.store / "other-session.json").exists())


if __name__ == "__main__":
    unittest.main()
