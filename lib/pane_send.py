"""Type a message into a worker's tmux pane, press Enter, and confirm it was submitted.

Claude Code reads a fast burst of keys as a paste, so an Enter sent right after the text becomes part of the paste and
nothing is submitted. send() types the text, waits SETTLE_SECONDS, sends Enter, then checks that the worker took it.

Delivery id. send() ends the text it types with a marker like " [tp:0a1b2c3d4e]" (TOKEN_RE): 10 random hex digits, new for
every call. It is a delivery id for the sender's bookkeeping and has no meaning to the worker; the worker may ignore it.
It is typed once, with the text. A retry presses Enter again and does not make a new one.

How "submitted" is decided. The harness comes from the task: meta.json "worker" ("claude" or "pi"), and only when meta.json
has none, state.json "harness". Telemetry that is missing never changes the harness.
  * Claude task: this send's own submit record, never the screen. The plugin's UserPromptSubmit hook
    (claude/plugins/tmux-pi/hooks/report.sh) finds every marker in the prompt it was given and appends
    {n, at, tokens: [...]} to `promptLog` in state.json (last 8 records). The send is confirmed only by a record whose
    tokens hold the marker this call typed. Nothing else counts: not a higher `promptSubmits`, not a record of the same
    text sent earlier (it carries that send's marker), not a record that arrives late for an earlier send. No text is
    compared, so no whitespace rule has to match between this module and the hook. A missing state.json means no record
    yet, never the pane check.
    Limits: if the plugin hooks are not installed in the worker, no record ever arrives and the result is submitted false
    even though the text may have gone in. `promptLog` keeps 8 records, so a record can be missed only if 8 more prompts
    are submitted within one POLL_SECONDS. What the hook's `prompt` holds for a pasted message is not proven byte for
    byte: the engine types say `prompt: string` for UserPromptSubmitHookInput (types/claude-code/index.d.ts line 16360,
    under .claude-plugin/ and git-ignored) and, for the engine's own prompt.submit, "The prompt's text as typed, pastes
    already expanded" (lines 9909-9914). A worker transcript shows a message typed by `tp-agent message` recorded in full
    (tasks/coder-36ec1ecbe7, transcript line 285, the 822-character "Main: one more change..." user entry). Not verified:
    the hook's `prompt` for a long message. If it held a paste placeholder instead of the text, the marker would be
    missing and the send would read as not submitted: a false "not submitted", never a false "submitted". If the box
    already held other text, the prompt holds it too, with our marker, and the send is confirmed.
  * pi task, or a send with no task dir: the pane check. agent/extensions/reporter.ts has no prompt-submit record (its
    agent_start only sets state "working"), so the typed text (marker included) must be gone from the bottom PANE_LINES
    lines of capture-pane. Limits: it cannot tell a submit from a worker that cleared or redrew its box for another
    reason; text echoed into the conversation just above the box can keep it "present" for up to PANE_LINES lines (a false
    "not submitted", which costs an extra Enter at most); a screen that never showed the text at all reads as submitted.

If there is no confirmation within CONFIRM_SECONDS, Enter is sent again, at most RETRIES more times. The text is typed
once and never retyped. Returns {"submitted": bool, "attempts": N} where attempts counts Enters sent.
"""
import json
import os
import re
import secrets
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
# The delivery id typed after the text. report.sh holds the same pattern to find it in the hook's prompt; a test keeps the
# two equal.
TOKEN_PATTERN = r"\[tp:[0-9a-f]{10}\]"
TOKEN_RE = re.compile(TOKEN_PATTERN)


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


def uses_record(task_dir):
    return harness(task_dir) == "claude"


def new_token():
    """A delivery id: "[tp:" + 10 random hex digits + "]" (matches TOKEN_RE). New on every call."""
    return "[tp:" + secrets.token_hex(5) + "]"


def delivered(task_dir, token):
    """True when a submit record in state.json carries `token`. No state.json, or no such record, is False."""
    log = _state(task_dir).get("promptLog")
    return isinstance(log, list) and any(
        isinstance(r, dict) and isinstance(r.get("tokens"), list) and token in r["tokens"] for r in log)


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
    """Type `text` (newlines flattened) and a new delivery id, press Enter, confirm. Raises CalledProcessError if tmux fails."""
    token = new_token()
    text = flatten(text) + " " + token
    if confirm is None:
        try:
            confirm = float(os.environ.get(CONFIRM_ENV, CONFIRM_SECONDS))
        except ValueError:
            confirm = CONFIRM_SECONDS
    record = uses_record(task_dir)
    _tmux("send-keys", "-t", pane, "-l", text, capture_output=True)
    attempts = 0
    for _ in range(retries + 1):
        time.sleep(settle)
        _tmux("send-keys", "-t", pane, "Enter", capture_output=True)
        attempts += 1
        deadline = time.monotonic() + confirm
        while True:
            if delivered(task_dir, token) if record else not pane_has_text(pane, text):
                return {"submitted": True, "attempts": attempts}
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
    return {"submitted": False, "attempts": attempts}
