"""Type a message into a worker's tmux pane, press Enter, and confirm it was submitted.

Claude Code reads a fast burst of keys as a paste, so an Enter sent right after the text becomes part of the paste and
nothing is submitted. send() types the text, waits SETTLE_SECONDS, sends Enter, then checks that the worker took it.

How "submitted" is decided. The harness comes from the task: meta.json "worker" ("claude" or "pi"), and only when meta.json
has none, state.json "harness". Telemetry that is missing never changes the harness.
  * Claude task: this message's own submit record, never the screen. The plugin's UserPromptSubmit hook
    (claude/plugins/tmux-pi/hooks/report.sh) appends {n, hash, at} to `promptLog` in state.json (last 8 records). The send
    reads `promptSubmits` before typing (no state.json = count 0), and is confirmed only by a record with n above that
    count and the hash of this message (prompt_hash). A higher count alone is not proof: a delayed hook for an earlier
    prompt can raise it. The snapshot rules out a repeat of the same text: the first send's record has n at or below the
    count read for the second send, so it cannot confirm it. Limit: if the first send was not confirmed and its record
    arrives after the second snapshot, it looks like the second's. If the plugin hooks are not installed in the worker, no
    record ever arrives and the result is submitted false even though the text may have gone in. The hook's `prompt` is
    not documented as byte-identical to the typed text (types/claude-code/index.d.ts says only `prompt: string` for the
    UserPromptSubmit hook, line 16362; the engine's own `prompt.submit` text is "as typed, pastes already expanded",
    line 9911), so both sides hash the text with all whitespace removed. If the box already held other text, the hook's
    prompt includes it, the hash differs, and the result is submitted false (the text did go in; the sender is told to
    check).
  * pi task, or a send with no task dir: the pane check. agent/extensions/reporter.ts has no prompt-submit record (its
    agent_start only sets state "working"), so the text must be gone from the bottom PANE_LINES lines of capture-pane.
    Limits: it cannot tell a submit from a worker that cleared or redrew its box for another reason; text echoed into the
    conversation just above the box can keep it "present" for up to PANE_LINES lines (a false "not submitted", which costs
    an extra Enter at most); a screen that never showed the text at all reads as submitted.

If there is no confirmation within CONFIRM_SECONDS, Enter is sent again, at most RETRIES more times. The text is typed
once and never retyped. Returns {"submitted": bool, "attempts": N} where attempts counts Enters sent.
"""
import hashlib
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


def _json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _state(task_dir):
    return _json(Path(task_dir) / "state.json")


def harness(task_dir):
    """"claude" or "pi" for the task: meta.json "worker", else state.json "harness"; None with no task dir or neither."""
    if not task_dir:
        return None
    for value in (_json(Path(task_dir) / "meta.json").get("worker"), _state(task_dir).get("harness")):
        if value in ("claude", "pi"):
            return value
    return None


def submits(task_dir):
    count = _state(task_dir).get("promptSubmits")
    return count if isinstance(count, int) else 0


def uses_record(task_dir):
    return harness(task_dir) == "claude"


def prompt_hash(text):
    """sha1 of `text` with all whitespace removed, first 12 hex chars. report.sh computes the same on the hook's prompt."""
    return hashlib.sha1(_squash(text).encode("utf-8")).hexdigest()[:12]


def delivered(task_dir, before, text):
    """True when a submit record after the snapshot `before` carries the hash of `text`."""
    log = _state(task_dir).get("promptLog")
    want = prompt_hash(text)
    return isinstance(log, list) and any(
        isinstance(r, dict) and isinstance(r.get("n"), int) and r["n"] > before and r.get("hash") == want for r in log)


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
            if delivered(task_dir, before, text) if record else not pane_has_text(pane, text):
                return {"submitted": True, "attempts": attempts}
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
    return {"submitted": False, "attempts": attempts}
