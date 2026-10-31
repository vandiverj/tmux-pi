"""tp-spawn --worker-env (trial lane L3, design §3/§10): allowlisted KEY=VAL pairs are exported by run.sh, nothing else is; a spawn
without the option writes the same run.sh as before; --other-lane-paths works without --lane; --state-dir/--parent reach the
preamble only when lib/preamble.py takes them. Real tp-spawn, fake tmux, no model."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "trial_shadow" / "run.sh.golden"


class WorkerEnvCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.state = self.base / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.project = self.base / "project"
        self.project.mkdir()
        fake = self.base / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options)
   case "$*" in
     *'@tp_state_dir'*) printf '%s\\n' "$TP_STATE_DIR" ;;
     *'@tp_main_pane'*) printf '%s\\n' "$TP_MAIN_PANE" ;;
   esac ;;
 display-message) printf '%s\\n' "$TP_MAIN_CWD" ;;
 new-window) printf '%%fake\\n' ;;
esac
''')
        tmux.chmod(0o755)
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_MAIN_PANE": "%main",
                    "TP_MAIN_CWD": str(self.project), "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        for key in ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR"):
            self.env.pop(key, None)

    def spawn(self, *args, task="env-1", worker="claude"):
        return subprocess.run([str(ROOT / "bin" / "tp-spawn"), "--worker", worker, "--task-id", task, *args, "Do a thing"],
                              env=self.env, text=True, capture_output=True, timeout=60)

    def task(self, task="env-1"):
        return self.state / "tasks" / task

    def run_sh(self, task="env-1"):
        return (self.task(task) / "run.sh").read_text()


class WorkerEnvTests(WorkerEnvCase):
    def test_a_listed_key_is_exported_by_run_sh_and_the_task_still_starts(self):
        run = self.spawn("--worker-env", "TP_TRIAL_ID=sh-a-1", "--worker-env", "TP_DOCS_TOOLS=1")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), "env-1")
        text = self.run_sh()
        self.assertIn("export TP_TRIAL_ID=sh-a-1\n", text)
        self.assertIn("export TP_DOCS_TOOLS=1\n", text)
        self.assertLess(text.index("export TP_TASK_ID"), text.index("export TP_TRIAL_ID"))
        self.assertLess(text.index("export TP_TRIAL_ID"), text.index("tp-claude"))  # set before the worker starts

    def test_an_unlisted_key_is_refused_and_nothing_is_created(self):
        for key in ("PATH", "LD_PRELOAD", "TP_STATE_DIR", "TP_TASK_ID", "tp_trial_id", "TP_TRIAL_ID_X", ""):
            with self.subTest(key=key):
                run = self.spawn("--worker-env", key + "=x")
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("not allowed", run.stderr)
                self.assertFalse(list((self.state / "tasks").iterdir()), "a refused spawn leaves no task")

    def test_a_pair_without_equals_a_repeat_and_a_line_break_are_refused(self):
        cases = (("TP_TRIAL_ID",), ("TP_TRIAL_ID=a", "--worker-env", "TP_TRIAL_ID=b"), ("TP_TRIAL_ID=a\nexport EVIL=1",))
        for case in cases:
            with self.subTest(case=case):
                args = []
                for value in case:
                    args += ["--worker-env", value] if not value.startswith("--") else [value]
                run = self.spawn(*args)
                self.assertNotEqual(run.returncode, 0)
                self.assertFalse(list((self.state / "tasks").iterdir()))

    def test_values_are_quoted_so_the_shell_reads_them_back_as_text_and_runs_nothing(self):
        marker = self.base / "ran"
        nasty = "a b'c\"d $(touch {m}) `touch {m}` ; touch {m} \\ *".format(m=marker)
        run = self.spawn("--worker-env", "TP_DOCS_TOOLS=" + nasty)
        self.assertEqual(run.returncode, 0, run.stderr)
        line = next(l for l in self.run_sh().splitlines() if l.startswith("export TP_DOCS_TOOLS"))
        read = subprocess.run(["bash", "-c", line + '\nprintf %s "$TP_DOCS_TOOLS"'], text=True, capture_output=True, timeout=20)
        self.assertEqual(read.stdout, nasty)
        self.assertFalse(marker.exists(), "quoting must not run the value")

    def test_the_value_may_be_empty_or_contain_equals(self):
        run = self.spawn("--worker-env", "TP_TRIAL_ID=a=b", "--worker-env", "TP_DOCS_TOOLS=")
        self.assertEqual(run.returncode, 0, run.stderr)
        text = self.run_sh()
        self.assertIn("export TP_TRIAL_ID=a=b\n", text)
        self.assertIn("export TP_DOCS_TOOLS=''\n", text)

    def test_without_the_option_run_sh_is_what_it_was(self):
        """Golden (design §10 item 4): the run.sh of a spawn without --worker-env, paths normalised, equals the one written before
        this option existed."""
        run = self.spawn(task="gold-1")
        self.assertEqual(run.returncode, 0, run.stderr)
        task = self.task("gold-1")
        text = self.run_sh("gold-1")
        for value, name in ((str(task), "@TASK_DIR@"), (str(self.state), "@STATE@"), (str(self.project), "@WORKDIR@"), (str(ROOT), "@ROOT@")):
            text = text.replace(value, name)
        self.assertEqual(text, GOLDEN.read_text())
        self.assertNotIn("TP_TRIAL_ID", text)
        self.assertNotIn("TP_DOCS_TOOLS", text)


class OtherLanePathsWithoutLaneTests(WorkerEnvCase):
    SECTION = "## Paths other lanes own"

    def test_a_spawn_without_a_lane_passes_the_section_to_the_packet(self):
        body = "- lane beta (lane/beta) owns \"lib/x.py\"\nDo not edit these paths."
        run = self.spawn("--other-lane-paths", body)
        self.assertEqual(run.returncode, 0, run.stderr)
        packet = (self.task() / "packet.md").read_text()
        self.assertIn(self.SECTION, packet)
        self.assertIn("lib/x.py", packet.split(self.SECTION, 1)[1])

    def test_no_section_without_the_option(self):
        self.assertEqual(self.spawn().returncode, 0)
        self.assertNotIn(self.SECTION, (self.task() / "packet.md").read_text())


class PreambleArgsTests(WorkerEnvCase):
    """tp-spawn passes --state-dir and --parent to preamble.py for a task with a parent, only when preamble.py takes them."""

    def copy_tree(self, copy):
        copy.mkdir()
        for name in ("agent", "bin", "catalog", "claude", "config", "lib", "skills", "templates"):
            if (ROOT / name).exists():
                subprocess.run(["cp", "-R", str(ROOT / name), str(copy / name)], check=True, capture_output=True)

    def test_the_preamble_gets_state_dir_and_parent_when_it_supports_them(self):
        probe = self.base / "preamble-args"
        # a copy of the tree whose preamble.py records its argv and accepts the new options
        copy = self.base / "tree"
        self.copy_tree(copy)
        (copy / "lib" / "preamble.py").write_text(
            "import sys\nopen({!r}, 'w').write(' '.join(sys.argv[1:]))\n# --state-dir --parent\nprint('PREAMBLE')\n".format(str(probe)))
        env = dict(self.env)
        run = subprocess.run([str(copy / "bin" / "tp-spawn"), "--worker", "claude", "--task-id", "pa-1", "--parent", "parent-1", "Do"],
                             env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("--state-dir " + str(self.state), probe.read_text())
        self.assertIn("--parent parent-1", probe.read_text())

    def test_the_preamble_is_not_given_them_when_it_does_not_support_them(self):
        probe = self.base / "preamble-args"
        copy = self.base / "tree"
        self.copy_tree(copy)
        (copy / "lib" / "preamble.py").write_text("import sys\nopen({!r}, 'w').write(' '.join(sys.argv[1:]))\nprint('PREAMBLE')\n".format(str(probe)))
        run = subprocess.run([str(copy / "bin" / "tp-spawn"), "--worker", "claude", "--task-id", "pa-2", "--parent", "parent-1", "Do"],
                             env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("--state-dir", probe.read_text())
        self.assertNotIn("--parent", probe.read_text())


if __name__ == "__main__":
    unittest.main()
