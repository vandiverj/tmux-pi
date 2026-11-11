#!/usr/bin/env python3
"""Scripted stand-in for a Claude worker (HB8 --fake). No model. Runs in a tmux pane in raw mode and writes the records a real worker
writes: TASK_DIR/state.json (state, tool, updated, promptSubmits), a Claude-format transcript (TASK_DIR/transcript.jsonl, named by
transcript-path), test-runs/<n>.json (the tp-test-run:v1 shape tools/agent/test-run.sh writes), result.md and report.md.

    worker.py TASK_DIR STEPS_JSON WORKDIR

Steps (closed list): {"state": S, "tool": T}; {"call": NAME, "input": {...}} (a Bash call is also run in WORKDIR);
{"edit": {"path", "old", "new"}} (old "" appends); {"test_run": CMD}; {"await": "message"}; {"result": STATUS, "text": T}.
Input: like Claude Code, typed/pasted text collects in the box; Return submits it; C-u clears it; a lone ESC in the permission
state denies the prompt (state -> working). A submit while the state is `permission` lands in the dialog: it is logged as
`into_prompt` and lost. A submit whose box held text before the newest message was pasted is logged with `merged_queued`.
Every input event goes to TASK_DIR/fake-worker.log (JSONL)."""
import json
import os
import select
import shlex
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import episode_lib as el  # noqa: E402
import gate_record  # noqa: E402

task_dir, steps_path, workdir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
steps = json.loads(steps_path.read_text())
state_path = task_dir / "state.json"
transcript = task_dir / "transcript.jsonl"
log_path = task_dir / "fake-worker.log"
(task_dir / "transcript-path").write_text(str(transcript) + "\n")
if not transcript.exists():
    # A real worker's transcript holds its first turn (the packet it was started with) before any tool call, so an empty
    # one is a lost record (round 4 rule 1), never this fake's normal output.
    first = next((p for p in (task_dir / "packet.md", task_dir / "task.md") if p.exists()), None)
    el.append_jsonl(transcript, {"type": "user", "timestamp": el.utc_now(), "message": {
        "role": "user", "content": first.read_text(encoding="utf-8") if first else "(no packet)"}})
fd = sys.stdin.fileno()
box = []


def out(text):
    os.write(sys.stdout.fileno(), text.replace("\n", "\r\n").encode())


def set_state(**fields):
    state = el.read_json(state_path, {}) or {}
    state.update(fields)
    state.setdefault("id", task_dir.name)
    state["harness"] = "claude"
    state["updated"] = el.utc_now()[:19] + "Z"
    el.write_json(state_path, state)


def tool_use(name, inp):
    call_id = "t%d" % time.time_ns()
    el.append_jsonl(transcript, {"type": "assistant", "timestamp": el.utc_now(),
                                 "message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id,
                                                                              "name": name, "input": inp}]}})
    return call_id


def tool_result(call_id, code, output=""):
    """The call's result as Claude writes it: an error result names the exit code (round 4 rule 5 reads it)."""
    error = code != 0
    text = ("Exit code %s\n%s" % (code, output) if code is not None else "timed out\n" + output) if error else output
    el.append_jsonl(transcript, {"type": "user", "timestamp": el.utc_now(),
                                 "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                                          "content": text[-2000:], "is_error": error}]}})


def run(command, timeout=900):
    try:
        done = subprocess.run(["/bin/sh", "-c", command], cwd=str(workdir), capture_output=True, text=True, timeout=timeout)
        return done.returncode, (done.stdout + done.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return None, "timeout"


def last_message():
    lines = el.read_jsonl(task_dir / "messages.jsonl") or []
    return lines[-1].get("text", "") if lines else ""


def await_message():
    """Block until a submit that is not swallowed by a permission prompt."""
    while True:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        data = os.read(fd, 4096).decode("utf-8", "replace")
        for ch in data:
            current = (el.read_json(state_path, {}) or {}).get("state")
            if ch == "\x1b":
                if current == "permission":
                    set_state(state="working", tool="")
                    el.append_jsonl(log_path, {"at": el.utc_now(), "denied": True})
                    out("\n[prompt denied]\n")
                else:
                    el.append_jsonl(log_path, {"at": el.utc_now(), "escape": True, "state": current})
            elif ch == "\x15":
                if box:
                    el.append_jsonl(log_path, {"at": el.utc_now(), "cleared": "".join(box)})
                del box[:]
            elif ch == "\x7f":
                if box:
                    box.pop()
            elif ch in "\r\n":
                text = "".join(box).strip()
                del box[:]
                if not text:
                    continue
                if current == "permission":
                    el.append_jsonl(log_path, {"at": el.utc_now(), "into_prompt": text})
                    continue
                message = last_message()
                entry = {"at": el.utc_now(), "submitted": text}
                if message and text != message and text.endswith(message):
                    entry["merged_queued"] = text[: -len(message)].strip()
                el.append_jsonl(log_path, entry)
                state = el.read_json(state_path, {}) or {}
                set_state(promptSubmits=int(state.get("promptSubmits", 0)) + 1, state="working")
                el.append_jsonl(transcript, {"type": "user", "timestamp": el.utc_now(),
                                             "message": {"role": "user", "content": text}})
                return text
            else:
                box.append(ch)
                out(ch)


def main():
    set_state(state="working", tool="", promptSubmits=1)
    test_runs = 0
    for step in steps:
        if "state" in step:
            set_state(state=step["state"], tool=step.get("tool", ""))
            if step["state"] == "permission":
                out("\nDo you want to proceed?\n> 1. Yes\n  2. No, and tell Claude what to do differently (esc)\n")
        elif "call" in step:
            call_id = tool_use(step["call"], step.get("input", {}))
            set_state(state="tool", tool=step["call"])
            if step["call"] == "Bash":
                tool_result(call_id, *run(step["input"].get("command", "")))
            else:
                tool_result(call_id, 0)
            set_state(state="working", tool="")
        elif "edit" in step:
            edit = step["edit"]
            path = workdir / edit["path"]
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            text = text + edit["new"] if edit["old"] == "" else text.replace(edit["old"], edit["new"], 1)
            path.write_text(text, encoding="utf-8")
            tool_result(tool_use("Edit", {"file_path": str(path), "old_string": edit["old"], "new_string": edit["new"]}), 0)
        elif "test_run" in step:
            command = step["test_run"]
            head, tree = gate_record.fingerprint(workdir)
            # A real worker runs its checks through tools/agent/test-run.sh, so its transcript shows the call that wrote
            # each record (the scorer counts them: a record lost from test-runs/ is then missing, round 4 rule 1).
            call_id = tool_use("Bash", {"command": "tools/agent/test-run.sh . --label %s -- /bin/sh -c %s" % (
                shlex.quote(command), shlex.quote(command))})
            t0 = time.monotonic()
            code, tail = run(command)
            tool_result(call_id, code, tail)
            test_runs += 1
            el.write_json(task_dir / "test-runs" / ("%d.json" % test_runs), {
                "schema": "tp-test-run:v1", "label": command, "cmd": ["/bin/sh", "-c", command], "cwd": str(workdir),
                "head": head, "tree": tree, "exit": code, "timed_out": code is None,
                "seconds": round(time.monotonic() - t0, 2), "log": None, "summary": tail.strip().splitlines()[-1:] or ""})
        elif "await" in step:
            out("\n> ")
            await_message()
        elif "result" in step:
            body = "%s\n\nRESULT_STATUS: %s\n" % (step.get("text", "Done."), step["result"])
            el.append_jsonl(transcript, {"type": "assistant", "timestamp": el.utc_now(), "message": {
                "role": "assistant", "content": [{"type": "text", "text": body}]}})
            (task_dir / "report.md").write_text(body)
            (task_dir / "result.md").write_text(body)
            set_state(state="waiting", tool="")
            out("\n" + body)
    out("\n> ")
    while True:          # idle at the prompt like a finished worker; the run's teardown ends the server
        try:
            await_message()
        except OSError:
            return


if __name__ == "__main__":
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        main()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
