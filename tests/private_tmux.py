"""A private tmux server for tests that drive a fake worker TUI (tests/fake_tui.py).

The server lives in a fresh /tmp/ms1.XXXX TMUX_TMPDIR on its own -L socket. `bin_dir` holds a `tmux` wrapper that pins
that socket, so code that runs plain `tmux` reaches this server and never the default one. close() kills only this
socket's server."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REAL_TMUX = shutil.which("tmux")
FAKE = Path(__file__).resolve().parent / "fake_tui.py"


class PrivateTmux:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="ms1.", dir="/tmp"))
        self.socket = "ms1-" + self.dir.name.split(".")[-1]
        self.bin_dir = self.dir / "bin"
        self.bin_dir.mkdir()
        wrapper = self.bin_dir / "tmux"
        wrapper.write_text('#!/bin/sh\nunset TMUX TMUX_PANE\nexport TMUX_TMPDIR="{}"\nexec "{}" -L {} "$@"\n'.format(
            self.dir, REAL_TMUX, self.socket))
        wrapper.chmod(0o755)
        self.count = 0

    def env(self):
        return {"PATH": str(self.bin_dir) + os.pathsep + os.environ["PATH"], "TMUX_TMPDIR": str(self.dir)}

    def tmux(self, *args):
        return subprocess.run([str(self.bin_dir / "tmux"), *args], capture_output=True, text=True, timeout=10)

    def worker(self, mode="normal", task_dir=None, paste_ms=150, foreign=False):
        """Start a fake worker in a new session; returns (pane id, log path, raw path). Waits for its ready file."""
        self.count += 1
        log, raw, ready = (self.dir / "w{}.{}".format(self.count, n) for n in ("log", "raw", "ready"))
        cmd = [sys.executable, str(FAKE), "--mode", mode, "--paste-ms", str(paste_ms), "--log", str(log), "--raw", str(raw),
               "--ready", str(ready)] + (["--task-dir", str(task_dir)] if task_dir else []) + (["--foreign"] if foreign else [])
        made = self.tmux("new-session", "-d", "-s", "w{}".format(self.count), "-x", "100", "-y", "20", " ".join(map(_quote, cmd)))
        if made.returncode:
            raise RuntimeError(made.stderr)
        pane = self.tmux("list-panes", "-t", "w{}".format(self.count), "-F", "#{pane_id}").stdout.strip()
        deadline = time.monotonic() + 10
        while not ready.exists():  # waits on the worker's own ready file, not a fixed time
            if time.monotonic() > deadline:
                raise RuntimeError("fake worker did not start")
            time.sleep(0.02)
        return pane, log, raw

    def submitted(self, log):
        return [json.loads(line)["submitted"] for line in log.read_text().splitlines()] if log.exists() else []

    def typed(self, raw, text):
        """How many times `text` arrived at the worker as typed input."""
        return "".join(json.loads(line) for line in raw.read_text().splitlines()).count(text) if raw.exists() else 0

    def close(self):
        subprocess.run([REAL_TMUX, "-L", self.socket, "kill-server"], capture_output=True, env={**os.environ, "TMUX_TMPDIR": str(self.dir)})
        shutil.rmtree(self.dir, ignore_errors=True)


def _quote(word):
    import shlex
    return shlex.quote(word)
