"""Isolated status-pane repaint checks; fake tmux, never a live session."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import time
import tty
import unittest

ROOT = Path(__file__).resolve().parents[1]
ESC = b"\x1b[H"


class StatusRepaintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        (self.state / "tasks").mkdir(parents=True)
        self.mockbin = self.base / "bin"
        self.mockbin.mkdir()
        (self.base / "cols").write_text("80\n")
        (self.base / "lines").write_text("24\n")
        self.script("tput", '#!/bin/sh\ncase "$1" in cols|lines) cat "$FAKE_BASE/$1";; esac\n')
        self.script("tmux", '''#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  show-options)
    case "$*" in
      *"@tp_state_dir"*) printf '%s\\n' "$FAKE_BASE/state" ;;
      *"@tp_viewing"*) cat "$FAKE_BASE/viewing" 2>/dev/null ;;
    esac ;;
  list-panes) printf '%%1 0\\n%%2 0\\n' ;;
esac
''')
        self.env = dict(os.environ, FAKE_BASE=str(self.base),
                        PATH=str(self.mockbin) + os.pathsep + os.environ["PATH"],
                        TERM="xterm")
        master, slave = pty.openpty()
        tty.setraw(slave)
        self.master = master
        self.proc = subprocess.Popen(["bash", str(ROOT / "bin/tp-status")],
                                     stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
                                     env=self.env, start_new_session=True)
        os.close(slave)
        self.addCleanup(self.stop)

    def script(self, name, text):
        path = self.mockbin / name
        path.write_text(text)
        path.chmod(0o755)

    def stop(self):
        # Close the pty first: on macOS a process in this session can remain
        # in the exiting state until its controlling pty is released.
        os.close(self.master)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)

    def output(self, seconds=0.5):
        end = time.monotonic() + seconds
        data = b""
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], max(0, end - time.monotonic()))
            if ready:
                try:
                    data += os.read(self.master, 65536)
                except OSError:
                    break
        return data

    def test_idle_no_workers_and_resize(self):
        first = self.output(0.6)
        self.assertIn(b"no subagents", first)
        self.assertEqual(first.count(ESC), 1)
        self.assertEqual(self.output(1.4), b"")
        (self.state / "main").mkdir()
        (self.state / "main" / "state.json").write_text(json.dumps({
            "model": "main-model", "tokens": {"input": 3, "output": 4}}))
        self.assertIn(b"main-model", self.output(1.4))
        self.assertEqual(self.output(1.4), b"")
        (self.base / "lines").write_text("25\n")
        self.assertEqual(self.output(2.5).count(ESC), 1)
        self.assertEqual(self.output(1.4), b"")

    def test_state_change_and_keyboard_selection(self):
        tasks = self.state / "tasks"
        for number in (1, 2):
            directory = tasks / f"job{number}"
            directory.mkdir()
            (directory / "meta.json").write_text(json.dumps({
                "id": f"job{number}", "worker": "pi", "pane": f"%{number}",
                "model": "", "thinking": ""}))
            (directory / "state.json").write_text(json.dumps({
                "state": "working", "model": "test-model", "tokens": {
                    "input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}))
            (directory / "task.md").write_text(f"task {number}\n")
        first = self.output(0.8)
        self.assertIn(b"job2", first)
        self.assertEqual(self.output(1.4), b"")
        os.write(self.master, b"k")
        moved = self.output(0.7)
        self.assertIn(b"task 1", moved)
        self.assertEqual(moved.count(ESC), 1)
        self.assertEqual(self.output(1.4), b"")
        (tasks / "job1" / "state.json").write_text(json.dumps({
            "state": "settled", "model": "test-model", "tokens": {
                "input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}))
        changed = self.output(1.5)
        self.assertIn(b"settled", changed)
        self.assertEqual(changed.count(ESC), 1)
        self.assertIn(b"task 1", changed)


if __name__ == "__main__":
    unittest.main()
