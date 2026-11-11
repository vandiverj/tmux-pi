"""Put a message into a worker's tmux pane, press Enter, and confirm it was submitted.

Claude Code reads a fast burst of keys as a paste, so an Enter sent right after the text becomes part of the paste and
nothing is submitted. send() puts the text in, waits SETTLE_SECONDS, sends Enter, then checks that the worker took it.

How the text goes in (MS2; evidence in the MS2 task's main-repro/: Claude Code 2.1.322, tmux 3.7c, macOS):
  * Claude task: one bracketed paste. `load-buffer -b <name> -` (the text on stdin: no file, no shell) into a buffer named
    for this send, then `paste-buffer -p -d` (bracketed if the TUI asked for it; the buffer is deleted after; if the paste
    fails, send() deletes it). Control characters go as spaces (see paste()). Typed keys (`send-keys -l`) lost the start of a long text: whole up to 1016 characters, 494
    of 1516 arrived, and from about 3000 characters the TUI split them into several pasted chunks. The paste arrived whole
    at every length tried, 216 to 6016 characters; from about 800 characters Claude Code wraps it in the prompt as
    `<pasted_content id=..>` ... `</pasted_content ..>`, which the check below allows for.
  * pi task, or no task dir: typed keys, as before. pi 1.1.0's editor (pi-tui components/editor.js handlePaste) shows a
    bracketed paste over 1000 characters or 10 lines as a "[paste #N ...]" marker, so the pane check below would read a
    long paste as gone from the box at once. There is no evidence yet that pi loses typed keys.

Delivery id. send() puts a marker like "[tp:0a1b2c3d4e/1089] " (TOKEN_RE) FIRST, then the text: 10 random hex digits,
new for every call, and the length of the text as normalize() measures it (whitespace and control characters left out).
It is for the sender's bookkeeping and has no meaning to the worker. It goes first so that a message never starts with a
character that changes the worker's input mode (MS2 round 2). Claude Code 2.1.322: a first "!" switches the box to bash
mode, typed or pasted (YE/Iii in its prompt-input module); "?" typed into an empty box opens the help and is swallowed;
"/" runs a slash command at submit and opens the command list; "@" opens the file list. pi 1.1.0: onSubmit trims the
text, then a first "!" runs bash and "/name" runs a command. "[" is none of these, in either. The text itself is not
changed. The id is put in once, with the text. A retry presses Enter again and does not make a new one.

How "submitted" is decided. The harness comes from the task: meta.json "worker" ("claude" or "pi"), and only when meta.json
has none, state.json "harness". Telemetry that is missing never changes the harness.
  * Claude task: this send's own submit record, never the screen. The plugin's UserPromptSubmit hook (report.sh,
    prompt_record) appends {n, at, tokens, marks} to `promptLog` in state.json (last 8 records). marks is
    prompt_marks(prompt): per marker, the normalized length of the prompt after it and a sha256 of the first <length>
    normalized characters after it. Rule: submitted is true only when a record holds this call's marker with the digest
    of this call's whole text, so every character of the text arrived, in order, right after the marker. Text before the
    marker (the box already held some; the paste wrapper's opening tag) and after our text (the wrapper's closing tag)
    does not matter. A record that holds our marker with any other digest means the marker arrived and part of the text
    after it did not: the result is submitted false with "received" and "length" (normalized characters after the marker,
    at most length, and the length sent), and send() returns at once: the partial prompt was submitted, so no more Enters
    are pressed and nothing is sent again into a box that may hold the rest. If the start of the message is lost, the
    marker is lost with it: no record holds it, and the result is submitted false after the Enter retries, with no
    "received". Nothing else counts: not a higher `promptSubmits`, not a record of the same text sent earlier (it carries
    that send's marker), not a record that arrives late for an earlier send. A missing state.json means no record yet,
    never the pane check.
    Limits: if the plugin hooks are not installed in the worker, no record ever arrives and the result is submitted false
    even though the text may have gone in. `promptLog` keeps 8 records, so a record can be missed only if 8 more prompts
    are submitted within one POLL_SECONDS. Whitespace and control characters are not compared (a box may fold or drop
    them), so a text that lost only those reads as whole. If the worker changes other characters (a paste placeholder in
    the hook's `prompt`, a Unicode normal form), the digest differs: a false "not submitted", never a false "submitted".
    "received" can count text after ours (the wrapper's closing tag) when the middle of ours was lost.
  * pi task, or a send with no task dir: the pane check. agent/extensions/reporter.ts has no prompt-submit record (its
    agent_start only sets state "working"), so the typed text (marker included) must be gone from the bottom PANE_LINES
    lines of capture-pane. What it proves is only that the last PROBE_CHARS non-space characters left the input region:
    for a long message it says nothing about the start, so a message whose start was lost reads as submitted. Other
    limits: it cannot tell a submit from a worker that cleared or redrew its box for another reason; text echoed into the
    conversation just above the box can keep it "present" for up to PANE_LINES lines (a false "not submitted", which costs
    an extra Enter at most); a screen that never showed the text at all reads as submitted.

If there is no confirmation within CONFIRM_SECONDS, Enter is sent again, at most RETRIES more times. The text is put in
once and never again. Returns {"submitted": bool, "attempts": N} where attempts counts Enters sent, plus "received" and
"length" when only part of the text arrived.
"""
import hashlib
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
# The delivery id put in before the text: "[tp:" + 10 random hex + "/" + the length of the text (normalized) + "]".
# report.sh holds the same two patterns and the same prompt_marks() code; a test runs both on the same prompts.
TOKEN_PATTERN = r"\[tp:[0-9a-f]{10}/[0-9]{1,7}\]"
TOKEN_RE = re.compile(TOKEN_PATTERN)
# What is left out of the text on both sides before it is measured and digested: whitespace and control characters, which
# a terminal input box may fold, drop or redraw. Python's re on both sides, so the two cannot disagree on what \s means.
NORM_PATTERN = r"[\s\x00-\x1f\x7f]+"


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


def normalize(text):
    return re.sub(NORM_PATTERN, "", text)


def digest(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def new_token(length):
    """A delivery id for a text whose normalized length is `length` (matches TOKEN_RE). New on every call."""
    return "[tp:{}/{}]".format(secrets.token_hex(5), length)


def prompt_marks(prompt):
    """What the hook records for a submitted `prompt`: one {id, after, sha256} per delivery id in it, in order. `after` is
    the normalized length of all the prompt after the id; `sha256` digests the first N normalized characters after it, N
    being the length the id carries (all of them when fewer are there). report.sh runs the same code."""
    marks = []
    for m in TOKEN_RE.finditer(prompt):
        n = int(m.group()[:-1].rsplit("/", 1)[1])
        after = normalize(prompt[m.end():])
        marks.append({"id": m.group(), "after": len(after), "sha256": digest(after[:n])})
    return marks


def arrival(task_dir, token, text):
    """What the submit records in state.json say about the send of `text` (normalized) that put in `token` first.
    None: no record holds the token. Else {"whole": bool, "received": n} where whole means some record of the token has the
    digest of the whole text; received is the normalized length after the id in the best record (at most len(text);
    it can include text after ours, such as the paste wrapper's closing tag, when the middle of ours was lost)."""
    log = _state(task_dir).get("promptLog")
    if not isinstance(log, list):
        return None
    want, best = digest(text), None
    for r in log:
        marks = r.get("marks") if isinstance(r, dict) else None
        for mark in marks if isinstance(marks, list) else []:
            if not isinstance(mark, dict) or mark.get("id") != token:
                continue
            after = mark.get("after") if isinstance(mark.get("after"), int) else 0
            if mark.get("sha256") == want and after >= len(text):
                return {"whole": True, "received": len(text)}
            best = max(best or 0, min(after, len(text)))
    return None if best is None else {"whole": False, "received": best}


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


def paste(pane, text):
    """Put `text` into the pane as one bracketed paste: a buffer named for this call, loaded from stdin, pasted with -p
    and deleted by -d. Control characters (C0 and DEL, tab and ESC included) are pasted as spaces: tmux 3.7's paste-buffer
    would otherwise write them as vis(3) escapes such as "\\034" (more characters, so a false "not submitted"), and no ESC
    can end the bracket ("ESC[201~") and have the rest read as keys. normalize() leaves both out, so the check is unchanged.
    tmux's own sanitizing (no -S) stays on as a second guard. If loading or pasting fails, the buffer is deleted here and the
    CalledProcessError raised."""
    name = "tp-send-" + secrets.token_hex(6)
    data = re.sub(r"[\x00-\x1f\x7f]", " ", text).encode("utf-8", "surrogatepass")
    try:
        _tmux("load-buffer", "-b", name, "-", input=data, capture_output=True)
        _tmux("paste-buffer", "-p", "-d", "-b", name, "-t", pane, capture_output=True)
    except subprocess.CalledProcessError:
        subprocess.run(["tmux", "delete-buffer", "-b", name], capture_output=True)
        raise


def send(pane, text, task_dir=None, settle=SETTLE_SECONDS, confirm=None, retries=RETRIES, token=None):
    """Put a new delivery id and `text` (newlines flattened) into the pane, press Enter, confirm. Claude: one bracketed
    paste; pi or no task dir: typed keys. Raises CalledProcessError if tmux fails. `token`: the delivery id to use, made by
    the caller with new_token(len(normalize(flatten(text)))) so it can record it (tp-agent message); a new one otherwise."""
    body = normalize(flatten(text))
    if token is None or not TOKEN_RE.fullmatch(token) or token != "[tp:{}/{}]".format(token[4:14], len(body)):
        token = new_token(len(body))
    text = token + " " + flatten(text)
    if confirm is None:
        try:
            confirm = float(os.environ.get(CONFIRM_ENV, CONFIRM_SECONDS))
        except ValueError:
            confirm = CONFIRM_SECONDS
    record = uses_record(task_dir)
    if record:
        paste(pane, text)
    else:
        _tmux("send-keys", "-t", pane, "-l", text, capture_output=True)
    attempts = 0
    for _ in range(retries + 1):
        time.sleep(settle)
        _tmux("send-keys", "-t", pane, "Enter", capture_output=True)
        attempts += 1
        deadline = time.monotonic() + confirm
        while True:
            if record:
                got = arrival(task_dir, token, body)
                if got and got["whole"]:
                    return {"submitted": True, "attempts": attempts}
                if got:  # submitted, but not all of it: never press Enter or retype into a box that may hold the rest
                    return {"submitted": False, "attempts": attempts, "received": got["received"], "length": len(body)}
            elif not pane_has_text(pane, text):
                return {"submitted": True, "attempts": attempts}
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)
    return {"submitted": False, "attempts": attempts}
