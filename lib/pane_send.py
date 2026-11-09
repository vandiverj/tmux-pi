"""Type a message into a worker's tmux pane, press Enter, and confirm it was submitted.

Claude Code reads a fast burst of keys as a paste, so an Enter sent right after the text becomes part of the paste and
nothing is submitted. send() types the text, waits SETTLE_SECONDS, sends Enter, then checks that the worker took it.

How "submitted" is decided:
  * Claude worker (state.json harness "claude"): the worker's own record. The plugin's UserPromptSubmit hook
    (claude/plugins/tmux-pi/hooks/report.sh) bumps `promptSubmits` in state.json. A higher count than before the send
    means submitted. If the plugin hooks are not installed in that worker, the count never moves and the result is
    submitted false even though the text may have gone in.
  * Any other worker (pi, or no state.json): the pane check. agent/extensions/reporter.ts has no prompt-submit record
    (its agent_start only sets state "working"), so for pi the text must be gone from the bottom PANE_LINES lines of
    capture-pane. Limits: it cannot tell a submit from a worker that cleared or redrew its box for another reason; text
    echoed into the conversation just above the box can keep it "present" for up to PANE_LINES lines (a false "not
    submitted", which costs an extra Enter at most); a screen that never showed the text at all reads as submitted.

If there is no confirmation within CONFIRM_SECONDS, Enter is sent again, at most RETRIES more times. The text is typed
once and never retyped. Returns {"submitted": bool, "attempts": N} where attempts counts Enters sent.
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

SETTLE_SECONDS = 0.3   # pause between the typed text and Enter, so the TUI does not read the Enter as part of a paste
CONFIRM_SECONDS = 3.0  # how long to wait for a submit record before pressing Enter again
CONFIRM_ENV = "TP_SEND_CONFIRM_SECONDS"  # overrides CONFIRM_SECONDS (tests of the CLIs that cannot pass `confirm`)
RETRIES = 2            # extra Enters after the first
POLL_SECONDS = 0.05
PANE_LINES = 6         # bottom lines of the pane that count as the input region
PROBE_CHARS = 24       # tail of the text (spaces removed) looked for in the input region


def flatten(text):
    return text.replace("\n", " ")


def _tmux(*args, **kwargs):
    return subprocess.run(["tmux", *args], check=True, **kwargs)


def _state(task_dir):
    try:
        value = json.loads((Path(task_dir) / "state.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def submits(task_dir):
    count = _state(task_dir).get("promptSubmits")
    return count if isinstance(count, int) else 0


def uses_record(task_dir):
    return bool(task_dir) and _state(task_dir).get("harness") == "claude"


def _squash(text):
    return re.sub(r"\s+", "", text)


def pane_has_text(pane, text):
    """True while the tail of `text` is in the bottom PANE_LINES lines of the pane."""
    probe = _squash(text)[-PROBE_CHARS:]
    if not probe:
        return False
    shot = _tmux("capture-pane", "-p", "-t", pane, capture_output=True, text=True).stdout
    lines = [line for line in shot.splitlines() if line.strip()]
    return probe in _squash("".join(lines[-PANE_LINES:]))


def send(pane, text, task_dir=None, settle=SETTLE_SECONDS, confirm=None, retries=RETRIES):
    """Type `text` (newlines flattened), press Enter, and confirm. Raises CalledProcessError if tmux fails."""
    text = flatten(text)
    if confirm is None:
        try:
            confirm = float(os.environ.get(CONFIRM_ENV, CONFIRM_SECONDS))
        except ValueError:
            confirm = CONFIRM_SECONDS
    record = uses_record(task_dir)
    before = submits(task_dir) if record else 0
    _tmux("send-keys", "-t", pane, "-l", text, capture_output=True)
    attempts = 0
    for _ in range(retries + 1):
        time.sleep(settle)
        _tmux("send-keys", "-t", pane, "Enter", capture_output=True)
        attempts += 1
        deadline = time.monotonic() + confirm
        while True:
            if (submits(task_dir) > before) if record else not pane_has_text(pane, text):
                return {"submitted": True, "attempts": attempts}
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
    return {"submitted": False, "attempts": attempts}
