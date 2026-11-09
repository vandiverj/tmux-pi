"""Claude Code's folder-trust dialog, as a worker pane shows it: one detector for the bench runner, `tp-agent` and the status views.

Claude Code asks "Is this a project you created or one you trust?" before any hook loads, so a worker stopped there writes no
permission request and its task reads "starting" for ever. Folder trust is the owner's decision (bin/tp-claude-bootstrap,
docs/bench.md step 3): tmux-pi never answers the dialog. It only makes it visible.

Everything here is read-only: `read` runs the tmux pane capture (`capture-pane -p -J`) and nothing else, bounded by a timeout, in its own process
group so a hung tmux (and anything it started) is killed with it. A capture that fails or times out is "nothing seen"; the
caller's status stays what it was, and a row whose capture timed out is reported as "not checked" (NOT_CHECKED), never as clear.
"""
import os
import re
import shlex
import signal
import subprocess
import time

TRUST_DIALOG = re.compile(r"trust (?:this|the files in this) folder|do you trust", re.I)  # the dialog's own words, not the word "trust"
WORKSPACE = re.compile(r"accessing workspace:", re.I)
BOX = " \t│┃|╎╏║"  # the frame some Claude Code versions draw around the screen
DEFAULT_TIMEOUT = 5.0   # seconds one capture may take
TIMEOUT_ENV = "TP_TRUST_CAPTURE_TIMEOUT"  # a test hook, read at each call
DEFAULT_BUDGET = 15.0   # seconds one Scanner may spend on all its captures together
BUDGET_ENV = "TP_TRUST_SCAN_BUDGET"  # a test hook, read when a Scanner starts
NOT_CHECKED = "not checked"   # what a row says when its pane could not be looked at (timed out, or the scan's budget was spent)


def seconds_from(name, default):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


def timeout():
    return seconds_from(TIMEOUT_ENV, DEFAULT_TIMEOUT)


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
    """Looks at the panes of a list of rows, one `dialog` call per row.

    The bound: each capture is cut at `seconds` (5 s by default), and one Scanner spends at most `budget` (15 s by default) on all
    its captures together, however many panes hang. A pane that hangs does not stop the rows after it from being looked at: they
    are captured as long as budget remains. A row whose capture timed out, or that came after the budget was spent, was not
    checked: `checked` is False for it and its caller says so (NOT_CHECKED) instead of leaving it looking clear."""

    def __init__(self, seconds=None, budget=None):
        self.seconds = seconds
        self.budget = budget if budget else seconds_from(BUDGET_ENV, DEFAULT_BUDGET)
        self.started = None
        self.checked = True     # of the last `dialog` call: False when its pane could not be looked at

    def dialog(self, pane, workdir=""):
        """{"dir": folder} when pane `pane` shows the dialog, else None. Sets `checked`."""
        self.checked = True
        if not isinstance(pane, str) or not pane:
            return None     # no pane was recorded: nothing to look at
        now = time.monotonic()
        self.started = now if self.started is None else self.started
        left = self.budget - (now - self.started)
        if left <= 0:
            self.checked = False
            return None
        text, timed_out = read(pane, min(self.seconds or timeout(), left))
        self.checked = not timed_out
        return {"dir": folder(text, workdir)} if shown(text) else None
