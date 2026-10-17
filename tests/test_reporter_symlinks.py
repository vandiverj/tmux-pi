"""Pi reporters and the dispatcher never write tmux-pi state through a planted symlink; real extension modules, synthetic data."""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTENSIONS = ROOT / "agent/extensions"


class ReporterSymlinkTests(unittest.TestCase):
    def test_runtime(self):
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/reporter_symlink_runtime.mjs")],
                             cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_dispatcher_followup_refuses_a_linked_followups_file(self):
        run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), "followup-symlinks"],
                             cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr)
        data = json.loads(run.stdout)
        self.assertEqual(data["outFollow"], "OUTSIDE\n")  # the append did not follow the link
        self.assertIn("ELOOP", data["refused"])
        self.assertTrue(data["metaUnchanged"])  # meta.json was replaced, not written through
        self.assertFalse(data["metaIsLink"])
        self.assertEqual(data["outsideFiles"], ["followups.md", "meta.json"])

    def test_state_writers_go_through_the_helper(self):
        # Raw fs writers follow a link at the target or in a parent dir. The extensions that write state use lib/state-write.ts;
        # the only raw writes left are into a fresh mkdtemp dir (dispatcher.ts) that holds copies handed to tp-spawn.
        raw = re.compile(r"\b(writeFileSync|appendFileSync|renameSync|copyFileSync|createWriteStream|mkdirSync)\(")
        for name in ("reporter", "sensitive", "escalate", "dispatcher"):
            text = (EXTENSIONS / f"{name}.ts").read_text()
            self.assertIn('from "./lib/state-write.ts"', text, name)
            for number, line in enumerate(text.splitlines(), 1):
                if raw.search(line):
                    self.assertIn('join(tmp, "', line, f"{name}.ts:{number} writes state without the helper: {line.strip()}")


if __name__ == "__main__":
    unittest.main()
