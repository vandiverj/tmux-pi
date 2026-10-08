"""Worker cwd selection with real tp-spawn/tp-review and fake tmux (no agent launch)."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class WorkdirTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.state = self.base / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.original = self.base / "original project"
        self.project = self.base / "new project"
        self.other = self.base / "later project"
        for path in (self.original, self.project, self.other):
            path.mkdir()
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
 display-message)
   [ "$5" = '#{pane_current_path}' ] && [ "$4" = '%main' ] || exit 1
   printf '%s\\n' "$TP_MAIN_CWD" ;;
 new-window)
   printf '%s\\n' "$*" >> "$TP_TMUX_LOG"
   printf '%%fake\\n' ;;
esac
''')
        tmux.chmod(0o755)
        self.log = self.base / "tmux.log"
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture",
                    "TP_MAIN_PANE": "%main", "TP_MAIN_CWD": str(self.project),
                    "TP_TMUX_LOG": str(self.log), "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)

    def cli(self, script, *args, **updates):
        env = {**self.env, **updates}
        return subprocess.run([str(ROOT / "bin" / script), *args], env=env, text=True,
                              capture_output=True, timeout=25)

    def meta(self, run):
        self.assertEqual(run.returncode, 0, run.stderr)
        task_dir = self.state / "tasks" / run.stdout.strip()
        return task_dir, json.loads((task_dir / "meta.json").read_text())

    def test_main_pane_cwd_not_state_or_callers_cwd(self):
        run = self.cli("tp-spawn", "--worker", "claude", "--context", "context.txt", "Read context")
        self.assertNotEqual(run.returncode, 0)  # relative context belongs to main cwd
        (self.project / "context.txt").write_text("project data")
        task, meta = self.meta(self.cli("tp-spawn", "--worker", "claude", "--context", "context.txt", "Read context"))
        self.assertEqual(meta["workdir"], str(self.project.resolve()))
        self.assertIn("project data", (task / "packet.md").read_text())
        self.assertIn(f"cd {str(self.project.resolve()).replace(' ', chr(92) + ' ')}", (task / "run.sh").read_text())
        self.assertIn(f"-c {self.project.resolve()}", self.log.read_text())
        self.assertNotEqual(meta["workdir"], str(self.original.resolve()))

    def test_missing_or_invalid_main_fails_closed(self):
        for updates in ({"TP_MAIN_PANE": ""}, {"TP_MAIN_CWD": ""}, {"TP_MAIN_CWD": str(self.base / "gone")}):
            with self.subTest(updates=updates):
                run = self.cli("tp-spawn", "--worker", "claude", "Task", **updates)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("workdir" if updates.get("TP_MAIN_CWD") else "main pane", run.stderr)
        self.assertFalse(list((self.state / "tasks").iterdir()))
        self.assertFalse(self.log.exists())

    def test_explicit_project_and_review_inherit_across_main_move(self):
        author, meta = self.meta(self.cli("tp-spawn", "--worker", "pi", "--workdir", str(self.original), "Task"))
        self.assertEqual(meta["workdir"], str(self.original.resolve()))
        (author / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (author / "state.json").write_text('{"state":"waiting"}')
        # The current pane now belongs to a different project, but review must
        # use the durable author cwd. --force skips policy routing only.
        reviewer, review_meta = self.meta(self.cli("tp-review", "--force", meta["id"], TP_MAIN_CWD=str(self.other)))
        self.assertEqual(review_meta["worker"], "claude")
        self.assertEqual(review_meta["workdir"], str(self.original.resolve()))
        self.assertIn(f"-c {self.original.resolve()}", self.log.read_text().splitlines()[-1])
        self.assertEqual((reviewer / "review-of").read_text().strip(), meta["id"])
        self.assertEqual(json.loads((author / "meta.json").read_text())["review"], review_meta["id"])

    def test_claude_author_pi_reviewer_and_explicit_path_without_main(self):
        author, meta = self.meta(self.cli("tp-spawn", "--worker", "claude", "--workdir", str(self.project),
                                          "Task", TP_MAIN_PANE=""))
        (author / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (author / "exit").write_text("0")
        _, reviewer_meta = self.meta(self.cli("tp-review", "--force", meta["id"], TP_MAIN_PANE=""))
        self.assertEqual(reviewer_meta["worker"], "pi")
        self.assertEqual(reviewer_meta["workdir"], meta["workdir"])

    def test_invalid_overrides_and_missing_author_cwd_do_not_spawn(self):
        for path in ("relative", str(self.base / "missing"), "/", str(self.state)):
            with self.subTest(path=path):
                self.assertNotEqual(self.cli("tp-spawn", "--workdir", path, "Task").returncode, 0)
        task = self.state / "tasks" / "old"
        task.mkdir()
        (task / "meta.json").write_text('{"id":"old","worker":"pi"}')
        (task / "result.md").write_text("Done\nRESULT_STATUS: complete\n")
        (task / "state.json").write_text('{"state":"waiting"}')
        run = self.cli("tp-review", "--force", "old")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("author workdir missing", run.stderr)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
