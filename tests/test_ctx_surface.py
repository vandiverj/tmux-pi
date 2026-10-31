"""CX4 ctx-tools: the folded-in CX1 review notes (N1-N8), the surface (argument checks, the 6 KB answer, the status vocabulary),
the deterministic brief, the five operations on stubbed and on fake-real navigators, and the call log. No model, no network,
no real navigator: the stubs and small fakes that copy the real answer shapes stand in."""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TP_CTX = ROOT / "bin" / "tp-ctx"
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import calls  # noqa: E402


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
    env.update(extra or {})
    return env


def run_pty(argv, env, cwd):
    """Run argv with a pseudo-terminal as stdin and stdout. Returns (exit code, terminal output, stderr text)."""
    master, slave = pty.openpty()
    errors = tempfile.TemporaryFile()
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=errors, env=env, cwd=cwd, close_fds=True)
    os.close(slave)
    out = b""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 30)
            if not ready:
                proc.kill()
                break
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        code = proc.wait(timeout=30)
    finally:
        os.close(master)
        errors.seek(0)
        err = errors.read().decode("utf-8", "replace")
        errors.close()
    return code, out.decode("utf-8", "replace"), err


class FoldedCx1Notes(unittest.TestCase):
    """CX1 review 2: N1 (a `..` after a symlink), N2/N3 (operator commands and status need a terminal to run unlogged), N7 (--log help)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = self.tmp / "project"
        (self.project / ".tmux-pi").mkdir(parents=True)
        self.task = self.tmp / "task"
        self.task.mkdir()

    def test_n1_a_task_dir_with_dot_dot_after_a_symlink_is_refused_and_nothing_is_written_through_it(self):
        other = self.tmp / "other"
        (other / "task").mkdir(parents=True)
        (self.tmp / "lnk").symlink_to(other)
        (self.tmp / "inner").mkdir()
        for value in (f"{self.tmp}/lnk/../task", f"{self.tmp}/./task", f"{self.tmp}//task", f"{self.task}/", f"{self.tmp}/inner/../task"):
            ran = []
            with self.assertRaisesRegex(calls.LogUnavailable, "normalized", msg=value):
                calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), env={"TP_TASK_DIR": value})
            self.assertEqual(ran, [], value)
        self.assertEqual(sorted(p.name for p in other.glob("**/*")), ["task"])
        self.assertFalse((self.task / calls.LOG_NAME).exists())

    def test_n1_a_normalized_task_dir_is_still_served_and_logged_at_the_path_that_was_checked(self):
        calls.call("t", {}, lambda: calls.Answer("ok", "x"), env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(len((self.task / calls.LOG_NAME).read_text().splitlines()), 1)

    def test_n1_the_command_line_gives_no_answer_for_a_dot_dot_task_dir(self):
        other = self.tmp / "other"
        (other / "task").mkdir(parents=True)
        (self.tmp / "lnk").symlink_to(other)
        done = subprocess.run([sys.executable, str(TP_CTX), "--project", str(self.project), "status"], capture_output=True, text=True,
                              env=clean_env({"TP_TASK_DIR": f"{self.tmp}/lnk/../task"}), timeout=60)
        self.assertEqual((done.returncode, done.stdout), (3, ""))
        self.assertFalse((other / "task" / calls.LOG_NAME).exists())

    def test_n2_n3_an_operator_command_runs_unlogged_only_at_a_terminal(self):
        ran = []
        for tty, outcome in ((True, "runs"), (False, "refused")):
            with mock.patch.object(calls, "on_terminal", return_value=tty):
                if outcome == "runs":
                    got = calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), operator=True, env={})
                    self.assertEqual(got.status, "ok")
                else:
                    with self.assertRaisesRegex(calls.LogUnavailable, "terminal"):
                        calls.call("t", {}, lambda: ran.append(2), operator=True, env={})
        self.assertEqual(ran, [1])

    def test_n2_n3_list_show_audit_and_status_give_no_answer_without_a_terminal_and_without_a_task_dir(self):
        base = [sys.executable, str(TP_CTX), "--project", str(self.project)]
        for words in (("knowledge", "list"), ("knowledge", "show", "kn-2026-10-31-x"), ("knowledge", "audit"), ("status",)):
            done = subprocess.run(base + list(words), capture_output=True, text=True, env=clean_env(), timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual((done.returncode, done.stdout), (3, ""), (words, done.stderr))
            self.assertIn("terminal", done.stderr, words)
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_n3_status_is_an_operator_command_that_a_person_at_a_terminal_may_run_unlogged(self):
        code, shown, errors = run_pty([sys.executable, str(TP_CTX), "--project", str(self.project), "status"], clean_env(), str(self.tmp))
        self.assertEqual(code, 0, errors)
        self.assertIn("engine: 1", shown)

    def test_n2_a_person_at_a_terminal_still_runs_list_unlogged(self):
        code, shown, errors = run_pty([sys.executable, str(TP_CTX), "--project", str(self.project), "knowledge", "list"], clean_env(),
                                      str(self.tmp))
        self.assertEqual((code, shown.strip()), (0, "no notes"), errors)

    def test_n7_the_log_option_says_it_is_for_an_operator_at_a_terminal(self):
        done = subprocess.run([sys.executable, str(TP_CTX), "--help"], capture_output=True, text=True, env=clean_env(), timeout=60)
        flat = " ".join(done.stdout.split())
        self.assertRegex(flat, r"--log LOG\s+call log file \(default.*\)[^-]*operator[^-]*terminal")


if __name__ == "__main__":
    unittest.main()
