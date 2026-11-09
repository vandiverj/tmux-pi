#!/usr/bin/env python3
"""A fake worker TUI for the message-submit tests (no Claude, no pi, no model).

Runs in a tmux pane with the terminal in raw mode. Models Claude Code's paste detection: a Return that arrives within
--paste-ms of the previous key is part of pasted text (it adds to the box and does not submit). A Return after a pause
is a submit, except in the modes below.

  --mode normal   a real Return submits
  --mode never    nothing ever submits (the text just sits in the box)
  --mode second   the first real Return is swallowed; the second submits
  --mode blind    like never, and the typed text is not drawn either (a loading screen that is not taking input yet)
A submit clears the screen (like a TUI starting a turn), appends {"submitted": text} to --log, and, with --task-dir,
does what report.sh does on UserPromptSubmit: promptSubmits + 1 and a {n, hash, at} entry in promptLog in state.json (the
hash is sha1 of the box text without whitespace, first 12 hex chars: written here on its own, not imported). With --foreign,
every Return, submit or not, first adds a record for some other, earlier prompt (a delayed hook), as a record of a different
hash. Every raw read is appended to --raw so a test can count how many times the text was typed.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import termios
import time
import tty
from pathlib import Path

args = argparse.ArgumentParser()
args.add_argument("--mode", default="normal")
args.add_argument("--paste-ms", type=float, default=150)
args.add_argument("--log", required=True)
args.add_argument("--raw", required=True)
args.add_argument("--ready", required=True)
args.add_argument("--task-dir")
args.add_argument("--foreign", action="store_true")
args = args.parse_args()

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
box, last, swallowed = "", 0.0, False


def draw():
    if args.mode == "blind":
        return
    os.write(1, ("\x1b[2J\x1b[H> " + box).encode())


def record(text):
    """What report.sh does on a UserPromptSubmit, for the prompt `text`."""
    state = Path(args.task_dir) / "state.json"
    prev = json.loads(state.read_text()) if state.exists() else {}
    n = prev.get("promptSubmits", 0) + 1
    prev["promptSubmits"] = n
    prev["promptLog"] = (prev.get("promptLog", []) + [{"n": n, "at": "t", "hash": hashlib.sha1(
        re.sub(r"\s+", "", text).encode()).hexdigest()[:12]}])[-8:]
    state.write_text(json.dumps(prev))


def append(path, line):
    with open(path, "a") as out:
        out.write(line + "\n")


try:
    os.write(1, b"\x1b[2J\x1b[HLoading..." if args.mode == "blind" else b"")
    draw()
    Path(args.ready).write_text("ready")
    while True:
        data = os.read(fd, 4096)
        if not data:
            break
        now = time.monotonic()
        append(args.raw, json.dumps(data.decode("utf-8", "replace")))
        for ch in data.decode("utf-8", "replace"):
            pasted = (now - last) * 1000 < args.paste_ms
            if ch in "\r\n" and args.foreign and args.task_dir:
                record("an earlier prompt")
            if ch in "\r\n" and not pasted and args.mode not in ("never", "blind"):
                if args.mode == "second" and not swallowed:
                    swallowed = True
                    continue
                append(args.log, json.dumps({"submitted": box}))
                if args.task_dir:
                    record(box)
                box = ""
                os.write(1, b"\x1b[2J\x1b[Hworking...")
            elif ch in "\r\n":
                box += " "
                draw()
            else:
                box += ch
                draw()
        last = now
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
