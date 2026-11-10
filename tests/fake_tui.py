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
does what report.sh does on UserPromptSubmit: promptSubmits + 1 and a {n, at, tokens, marks} entry in promptLog in
state.json, from lib/pane_send.prompt_marks(box text) (tests/test_claude_report.py runs report.sh and that function on the
same prompts and requires the same record).
What arrives (MS2): --drop-first K loses the first K characters typed (the production loss: only the end arrived);
--keep-first K loses every character after the first K; --prefill TEXT starts with TEXT in the box; --twice submits the
box text twice over.
Paste (MS2 round 2): the fake asks for bracketed paste (ESC[?2004h), so `paste-buffer -p` arrives as ESC[200~ ... ESC[201~;
inside it a Return is text, never a submit. --wrap N: a bracketed paste of N or more characters goes into the prompt the way
Claude Code 2.1.322 puts a long paste (main-repro): "\n\n<pasted_content id=\"1f02\">\n" + text + "\n</pasted_content
id=\"1f02\">\n". --bang: Claude Code's bash mode: a "!" that arrives first into an empty box switches to bash mode; that
submit runs as a command, logs {"bash": text} and writes no record. With --foreign, every Return, submit or not, first adds a record for some other, earlier send (a delayed hook),
carrying a different delivery id. With --hold, a submit's record is kept back until the file <--log>.release exists (the
worker took the message, its hook is slow); records then land in order. Every raw read is appended to --raw so a test can
count how many times the text was typed.
"""
import argparse
import json
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import pane_send  # noqa: E402

args = argparse.ArgumentParser()
args.add_argument("--mode", default="normal")
args.add_argument("--paste-ms", type=float, default=150)
args.add_argument("--log", required=True)
args.add_argument("--raw", required=True)
args.add_argument("--ready", required=True)
args.add_argument("--task-dir")
args.add_argument("--foreign", action="store_true")
args.add_argument("--hold", action="store_true")
args.add_argument("--drop-first", type=int, default=0)
args.add_argument("--keep-first", type=int, default=-1)
args.add_argument("--prefill", default="")
args.add_argument("--twice", action="store_true")
args.add_argument("--wrap", type=int, default=0)
args.add_argument("--bang", action="store_true")
args = args.parse_args()

fd = sys.stdin.fileno()
old = termios.tcgetattr(fd)
tty.setraw(fd)
box, last, swallowed, held, seen = args.prefill, 0.0, False, [], 0
pending, pasting, pasted_text, bash = "", False, "", False
START, END = "\x1b[200~", "\x1b[201~"


def draw():
    if args.mode == "blind":
        return
    os.write(1, ("\x1b[2J\x1b[H> " + box).encode())


def write_record(marks):
    state = Path(args.task_dir) / "state.json"
    prev = json.loads(state.read_text()) if state.exists() else {}
    n = prev.get("promptSubmits", 0) + 1
    prev["promptSubmits"] = n
    prev["promptLog"] = (prev.get("promptLog", []) + [{"n": n, "at": "t", "tokens": [m["id"] for m in marks],
                                                        "marks": marks}])[-8:]
    state.write_text(json.dumps(prev))


def record(text):
    """What report.sh does on a UserPromptSubmit, for the prompt `text` (held back with --hold)."""
    marks = pane_send.prompt_marks(text)
    held.append(marks) if args.hold else write_record(marks)


def release():
    if held and Path(str(args.log) + ".release").exists():
        while held:
            write_record(held.pop(0))


def append(path, line):
    with open(path, "a") as out:
        out.write(line + "\n")


try:
    os.write(1, b"\x1b[?2004h")  # bracketed paste, as Claude Code and pi ask for it
    os.write(1, b"\x1b[2J\x1b[HLoading..." if args.mode == "blind" else b"")
    draw()
    Path(args.ready).write_text("ready")
    while True:
        if not select.select([fd], [], [], 0.02)[0]:
            release()
            continue
        data = os.read(fd, 4096)
        if not data:
            break
        release()
        now = time.monotonic()
        append(args.raw, json.dumps(data.decode("utf-8", "replace")))
        pending += data.decode("utf-8", "replace")
        while pending:
            if pending.startswith(START) or pending.startswith(END):
                if pending.startswith(START):
                    pasting, pasted_text = True, ""
                else:
                    pasting = False
                    if args.wrap and len(pasted_text) >= args.wrap:
                        box = box[:len(box) - len(pasted_text)] + ('\n\n<pasted_content id="1f02">\n' + pasted_text
                                                                  + '\n</pasted_content id="1f02">\n')
                    draw()
                pending = pending[len(START):]
                continue
            if pending[0] == "\x1b" and (START.startswith(pending) or END.startswith(pending)):
                break  # the rest of a marker is still on its way
            ch, pending = pending[0], pending[1:]
            pasted = pasting or (now - last) * 1000 < args.paste_ms
            if ch in "\r\n" and args.foreign and args.task_dir and not pasting:
                write_record(pane_send.prompt_marks("[tp:ffffffffff/16] an earlier message"))
            if ch in "\r\n" and not pasted and args.mode not in ("never", "blind"):
                if args.mode == "second" and not swallowed:
                    swallowed = True
                    continue
                box = box + " " + box if args.twice else box
                append(args.log, json.dumps({"bash" if bash else "submitted": box}))
                if args.task_dir and not bash:
                    record(box)
                box, bash = "", False
                os.write(1, b"\x1b[2J\x1b[Hworking...")
            elif ch in "\r\n":
                box += " "
                pasted_text += " " if pasting else ""
                draw()
            else:
                seen += 1
                if seen > args.drop_first and (args.keep_first < 0 or seen <= args.keep_first):
                    if args.bang and ch == "!" and box == "" and not bash:
                        bash = True
                    else:
                        box += ch
                        pasted_text += ch if pasting else ""
                draw()
        last = now
finally:
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
