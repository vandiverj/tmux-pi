"""Claude Code's folder-trust dialog, as a worker pane shows it: one detector for the bench runner, `tp-agent` and the status views.

Claude Code asks "Is this a project you created or one you trust?" before any hook loads, so a worker stopped there writes no
permission request and its task reads "starting" for ever. Folder trust is the owner's decision (bin/tp-claude-bootstrap,
docs/bench.md step 3): tmux-pi never answers the dialog. It only makes it visible.

Everything here is read-only: `capture` runs `tmux capture-pane -p` and nothing else, bounded by a timeout, in its own process
group so a hung tmux (and anything it started) is killed with it. A capture that fails or times out is "nothing seen"; the
caller's status stays what it was.
"""
import os
import re
import shlex
import signal
import subprocess

TRUST_DIALOG = re.compile(r"trust (?:this|the files in this) folder|do you trust", re.I)  # the dialog's own words, not the word "trust"
WORKSPACE = re.compile(r"accessing workspace:", re.I)
BOX = " \t│┃|╎╏║"  # the frame some Claude Code versions draw around the screen
DEFAULT_TIMEOUT = 5.0
TIMEOUT_ENV = "TP_TRUST_CAPTURE_TIMEOUT"  # seconds; a test hook, read at each call


def timeout():
    try:
        value = float(os.environ.get(TIMEOUT_ENV, ""))
    except ValueError:
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def shown(text):
    """True when `text` (a pane's screen) is Claude Code's folder-trust dialog."""
    return bool(TRUST_DIALOG.search(text or ""))


def folder(text, default=""):
    """The folder the dialog names (the line after "Accessing workspace:"), else `default` (the task's workdir)."""
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if WORKSPACE.search(line):
            for following in lines[i + 1:i + 6]:
                candidate = following.strip(BOX)
                if candidate:
                    return candidate if candidate.startswith(("/", "~")) else default
            break
    return default


def message(directory):
    """What the owner reads: where the worker is stopped and the one command that fixes it."""
    return (f"worker stopped at Claude Code's folder-trust dialog for {directory}; "
            f"the owner trusts it once: bin/tp-claude-bootstrap {shlex.quote(directory)}")


def read(pane, seconds=None):
    """(text, timed_out) of tmux pane `pane`: text is None when tmux fails or the pane is gone; timed_out when the call ran over
    `seconds` and was killed. Never sends keys."""
    try:
        # -J joins the lines the pane wrapped: a long worktree path must come back whole, or the fix would name half a folder
        proc = subprocess.Popen(["tmux", "capture-pane", "-p", "-J", "-t", pane], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except (OSError, ValueError):
        return None, False
    try:
        out, _ = proc.communicate(timeout=seconds or timeout())
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        return None, True
    return (out.decode("utf-8", "replace") if proc.returncode == 0 else None), False


class Scanner:
    """Looks at panes for the dialog. After one capture times out it looks no more (a hung tmux would otherwise cost the
    timeout once per row of a list)."""

    def __init__(self, seconds=None):
        self.seconds, self.hung = seconds, False

    def dialog(self, pane, workdir=""):
        """{"dir": folder} when pane `pane` shows the dialog, else None."""
        if self.hung or not isinstance(pane, str) or not pane:
            return None
        text, self.hung = read(pane, self.seconds)
        return {"dir": folder(text, workdir)} if shown(text) else None
