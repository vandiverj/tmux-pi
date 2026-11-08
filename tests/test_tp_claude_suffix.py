import session_env

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from session_env import outside_session

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from redact_hook import SUFFIX, wrap


class ClaudeSuffixTests(unittest.TestCase):
    """Isolated launcher copy: no real Claude, config, credentials, or model."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "root"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "claude").mkdir()
        shutil.copy(ROOT / "bin/tp-claude", self.root / "bin/tp-claude")
        shutil.copy(ROOT / "claude/settings.json", self.root / "claude/settings.json")
        fake = self.base / "fake"
        fake.mkdir()
        claude = fake / "claude"
        claude.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@"\n')
        claude.chmod(0o755)
        self.env = {**outside_session(), "PATH": str(fake) + os.pathsep + os.environ["PATH"],
                    "TP_CLAUDE_CONFIG_DIR": str(self.base / "config")}
        self.prompt_file = self.base / "prompt.md"
        self.prompt_file.write_text("caller file\nsecond line\n")

    def test_allow_rule_matches_hook_suffix(self):
        allow = json.loads((ROOT / "claude/settings.json").read_text())["permissions"]["allow"]
        self.assertIn("Bash(" + SUFFIX.removeprefix(" || ") + ")", allow)

    def check_invalid_prompt(self, flags):
        run = subprocess.run(["bash", str(self.root / "bin/tp-claude"), *flags],
                             env=self.env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("tp-claude:", run.stderr)
        self.assertEqual(run.stdout, "")

    def check_prompt(self, mode, shape):
        shapes = {
            "none": ([], ""),
            "inline": (["--append-system-prompt", "caller inline"], "caller inline"),
            "file": (["--append-system-prompt-file", str(self.prompt_file)], self.prompt_file.read_text()),
            "mixed": (["--append-system-prompt", "first", "--append-system-prompt-file", str(self.prompt_file),
                       "--append-system-prompt", "last"], "first\n" + self.prompt_file.read_text() + "\nlast"),
            "equals": (["--append-system-prompt=caller inline"], "caller inline"),
            "file_equals": (["--append-system-prompt-file=" + str(self.prompt_file)], self.prompt_file.read_text()),
        }
        flags, caller = shapes[shape]
        run = subprocess.run(["bash", str(self.root / "bin/tp-claude"), *flags, "--name", "worker"],
                             env={**self.env, "TP_REDACTION": mode}, capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        argv = run.stdout.split("\0")[:-1]
        self.assertEqual(argv.count("--append-system-prompt"), 1)
        self.assertNotIn("--append-system-prompt-file", argv)
        prompt = argv[argv.index("--append-system-prompt") + 1]
        match = re.search(r"Append `([^`]+)`", prompt)
        self.assertIsNotNone(match, prompt)
        suffix = match.group(1)
        self.assertEqual(suffix, SUFFIX)
        self.assertEqual(prompt.count("Append `" + SUFFIX + "`"), 1)
        instruction = prompt[len(caller) + 1:] if caller else prompt
        if caller:
            self.assertTrue(prompt.startswith(caller + "\n"), repr(prompt))
        self.assertTrue(instruction.startswith("Append `" + SUFFIX + "`"))
        self.assertEqual(wrap("ls /nonexistent-dir" + suffix), "ls /nonexistent-dir" + suffix)
        self.assertEqual(argv[-2:], ["--name", "worker"])


def prompt_case(mode, shape):
    def test(self):
        self.check_prompt(mode, shape)
    return test


# Each row launches the stub once; mode policy is always, not just on.
for _mode in ("off", "shadow", "on"):
    for _shape in ("none", "inline", "file"):
        setattr(ClaudeSuffixTests, "test_prompt_" + _mode + "_" + _shape, prompt_case(_mode, _shape))
for _shape in ("mixed", "equals", "file_equals"):
    setattr(ClaudeSuffixTests, "test_prompt_on_" + _shape, prompt_case("on", _shape))


def invalid_case(flags):
    def test(self):
        self.check_invalid_prompt(flags)
    return test


for _name, _flags in {
    "missing_inline": ["--append-system-prompt"],
    "missing_file": ["--append-system-prompt-file"],
    "unreadable_file": ["--append-system-prompt-file", "/nonexistent-h10-prompt.md"],
}.items():
    setattr(ClaudeSuffixTests, "test_reject_" + _name, invalid_case(_flags))


if __name__ == "__main__":
    unittest.main()
