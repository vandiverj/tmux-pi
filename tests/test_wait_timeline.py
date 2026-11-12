"""tools/waits/timeline.py (HB6a, WT1 arm A): the derivation rules as one table, and the case-1 controlled-delay scoring.

The task directories are producer-shaped, copied field for field from:
  meta.json            bin/tp-spawn (created as date -u +%Y-%m-%dT%H:%M:%SZ, worker, role, lane, lane_step, workdir)
  state.json           claude/plugins/tmux-pi/hooks/report.sh (Claude: promptLog [{n, at, tokens, marks}], updated, state)
                       and agent/extensions/reporter.ts (pi: no promptLog), as in a real task dir of 2026-11-12
  test-runs/<n>.json   tools/agent/test-run.sh (tp-test-run:v1, seconds and exit, no start/end; the file time is the end)
  gate.json            tools/agent/pre-commit-gate.sh / lib/gate_record.py (tp-gate:v1 started/finished)
  requests/, events    lib/hub_records.py (request record and tp-event:v1 line shapes; resolution.by "main" as tp-agent writes)
  collected/stopped/reaped.json, exit, result.md, review-of, capability-request.json   bin/tp-agent, run.sh, bin/tp-review
  transcripts          Claude Code lines (type user/assistant, timestamp, message.id, tool_use/tool_result) and pi lines
                       (type message, role user/assistant/toolResult, toolCall/toolCallId), times and ids only
"""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("wait_timeline", ROOT / "tools" / "waits" / "timeline.py")
timeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(timeline)

T0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()


def iso_ms(s):
    return datetime.fromtimestamp(T0 + s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def iso_s(s):
    return datetime.fromtimestamp(T0 + s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_off(s):
    return datetime.fromtimestamp(T0 + s, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


class Session:
    """A session directory built as of a time: every file has the time its producer writes it (and the time a queue file
    is removed), and materialize(now) writes only what exists at `now`, with that mtime."""

    def __init__(self, root):
        self.root = Path(root)
        self.tasks = self.root / "tasks"
        self.files = []  # (t_written, t_removed or None, relpath, content: str | callable(now) -> str | None)

    def put(self, t, rel, content, removed=None):
        self.files.append((t, removed, rel, content))

    def materialize(self, now):
        shutil.rmtree(self.root, ignore_errors=True)
        self.tasks.mkdir(parents=True)
        for t, removed, rel, content in self.files:
            if t > now or (removed is not None and removed <= now):
                continue
            text = content(now) if callable(content) else content
            if text is None:
                continue
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            os.utime(path, (T0 + t, T0 + t))
        return self.tasks

    # --- producer shapes
    def task(self, ident, created, worker, role="coder", review_of=None, lane=None):
        meta = {"id": ident, "worker": worker, "model": "m", "thinking": "medium", "pane": "%1", "workdir": "/nonexistent",
                "created": iso_s(created), "role": role}
        if lane:
            meta.update(lane=lane, lane_step="S1")
        self.put(created, "tasks/{}/meta.json".format(ident), json.dumps(meta, indent=2))
        if review_of:
            self.put(created, "tasks/{}/review-of".format(ident), review_of + "\n")

    def claude_state(self, ident, prompts, last=None):
        def content(now):
            log = [{"n": i + 1, "at": iso_s(t), "tokens": [], "marks": []} for i, t in enumerate(prompts) if t <= now]
            state = {"id": ident, "harness": "claude", "state": "working", "tool": "", "updated": iso_s(min(now, last or now))}
            if log:
                state.update(promptTurn=len(log), promptSubmits=len(log), promptLog=log[-8:])
            return json.dumps(state, indent=2)
        self.put(0, "tasks/{}/state.json".format(ident), content)

    def claude_transcript(self, ident, lines, skew=0.4):
        """lines: (t, kind, data): prompt | use (mid, [(id, name)]) | text (mid) | result ([ids])."""
        rel = "tasks/{}/transcript.jsonl".format(ident)

        def content(now):
            out = [json.dumps({"type": "custom-title", "sessionId": "x"})]  # untimed lines are skipped
            for t, kind, data in lines:
                if t > now:
                    continue
                ts = iso_ms(t + skew)
                if kind == "prompt":
                    out.append(json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": "PROMPT"}}))
                elif kind == "result":
                    out.append(json.dumps({"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": i, "content": "OUT"} for i in data]}}))
                elif kind == "use":
                    mid, calls = data
                    out.append(json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": mid, "role": "assistant",
                        "content": [{"type": "tool_use", "id": i, "name": n, "input": {}} for i, n in calls]}}))
                else:
                    out.append(json.dumps({"type": "assistant", "timestamp": ts, "message": {"id": data, "role": "assistant",
                        "content": [{"type": "text", "text": "SECRET-REPLY"}]}}))
            return "\n".join(out) + "\n"
        self.put(0, rel, content)

    def pi_transcript(self, ident, lines):
        """lines: (t, kind, data): prompt | call (start, [(id, name)]) | text (start) | result (id)."""
        rel = "tasks/{}/transcript.jsonl".format(ident)

        def content(now):
            out = [json.dumps({"type": "session", "id": "s", "timestamp": iso_ms(lines[0][0] - 1)})]
            for n, (t, kind, data) in enumerate(lines):
                if t > now:
                    continue
                if kind == "prompt":
                    msg = {"role": "user", "content": [{"type": "text", "text": "PROMPT"}], "timestamp": int((T0 + t) * 1000)}
                elif kind == "result":
                    msg = {"role": "toolResult", "toolCallId": data, "toolName": "bash", "durationMs": 1, "isError": False,
                           "content": [{"type": "text", "text": "OUT"}], "timestamp": int((T0 + t) * 1000)}
                else:
                    start, calls = data if kind == "call" else (data, [])
                    msg = {"role": "assistant", "timestamp": int((T0 + start) * 1000), "durationMs": int((t - start) * 1000),
                           "stopReason": "toolUse" if calls else "stop",
                           "content": [{"type": "toolCall", "id": i, "name": nm, "arguments": {}} for i, nm in calls] or
                                      [{"type": "text", "text": "SECRET-REPLY"}]}
                out.append(json.dumps({"type": "message", "id": "l{}".format(n), "timestamp": iso_ms(t), "message": msg}))
            return "\n".join(out) + "\n"
        self.put(0, rel, content)

    def pi_state(self, ident, at):
        self.put(at, "tasks/{}/state.json".format(ident), json.dumps({"id": ident, "harness": "pi", "state": "waiting",
                                                                      "updated": iso_ms(at)}))

    def test_run(self, ident, n, end, seconds, exit_code=0):
        self.put(end, "tasks/{}/test-runs/{}.json".format(ident, n), json.dumps({
            "schema": "tp-test-run:v1", "label": "", "cmd": ["python3", "-m", "unittest"], "cwd": "/w", "head": "h", "tree": "t",
            "exit": exit_code, "timed_out": False, "seconds": seconds, "log": "/l.log", "summary": "Ran 1 test in 1s"}))

    def gate(self, ident, started, finished):
        self.put(finished, "tasks/{}/gate.json".format(ident), json.dumps({
            "schema": "tp-gate:v1", "worktree": "/w", "base": {"ref": "main", "sha": "s"}, "head": "h", "tree": "t", "files": [],
            "checks": [], "exit": 0, "started": iso_off(started), "finished": iso_off(finished), "log": "/l.log"}))

    def request(self, ident, rid, kind, opened, resolved=None, by="main"):
        def record(now):
            r = {"id": rid, "task": ident, "kind": kind, "opened": iso_ms(opened), "text_sha256": "0" * 64, "summary": "SECRET-ASK",
                 "status": "open"}
            if resolved is not None and resolved <= now:
                r.update(status="resolved", resolved=iso_ms(resolved), resolution={"by": by, "how": "message", "ref": None})
            return json.dumps(r, indent=2)
        self.put(opened, "tasks/{}/requests/{}.json".format(ident, rid), record)

    def events(self, ident, items):
        """items: (t, kind, ref, detail)."""
        def content(now):
            return "".join(json.dumps({"v": 1, "id": "ev-{}".format(n), "at": iso_ms(t), "task": ident, "actor": "main", "kind": k,
                                       "ref": ref, "detail": d, "source": "file", "confidence": "exact"}) + "\n"
                           for n, (t, k, ref, d) in enumerate(items) if t <= now) or None
        self.put(min(i[0] for i in items), "tasks/{}/events.jsonl".format(ident), content)

    def capability(self, ident, requested, answered=None):
        def content(now):
            r = {"kind": "context", "what": "SECRET-WHAT", "reason": "r", "status": "pending", "requested": iso_off(requested)}
            if answered is not None and answered <= now:
                r.update(status="answered", answered=iso_off(answered))
            return json.dumps(r, indent=2)
        self.put(requested, "tasks/{}/capability-request.json".format(ident), content)

    def finish(self, ident, result=None, collected=None, stopped=None, reaped=None, exit_at=None):
        if result is not None:
            self.put(result, "tasks/{}/result.md".format(ident), "SECRET-RESULT\nRESULT_STATUS: complete\n")
        if collected is not None:
            self.put(collected, "tasks/{}/collected.json".format(ident), json.dumps({"collected": iso_off(collected)}))
        if stopped is not None:
            self.put(stopped, "tasks/{}/stopped.json".format(ident), json.dumps({"stopped": iso_s(stopped)}))
        if reaped is not None:
            self.put(reaped, "tasks/{}/reaped.json".format(ident), json.dumps({"reaped": iso_off(reaped), "pane": "%1",
                                                                                "pid": 1, "signal": "SIGTERM"}))
        if exit_at is not None:
            self.put(exit_at, "tasks/{}/exit".format(ident), "0\n")

    def queued(self, ident, created, removed, reason):
        self.put(created, "queue/{}.json".format(ident), json.dumps({
            "id": ident, "role": "coder", "worker": "claude", "model": "m", "created": iso_s(created), "after": ["coder-b"],
            "queue_reason": reason, "workdir": "/w"}), removed=removed)


REQ = "req-01JZ0000000000000000000000"
REQ2 = "req-01JZ0000000000000000000001"


def case1(root):
    """The planted schedule of case 1 (seconds after T0) and its ground truth [(task, state, start, end)]."""
    s = Session(root)
    truth = []
    # coder-a: a 90 s test run inside a shell call, then a review holds it until Main collects
    s.task("coder-a", 0, "claude")
    s.claude_state("coder-a", [2])
    s.claude_transcript("coder-a", [(2, "prompt", None), (10, "use", ("m1", [("a1", "Read")])), (12, "result", ["a1"]),
                                    (16, "text", "m2"), (20, "use", ("m2", [("a2", "Bash")])), (115, "result", ["a2"]),
                                    (130, "text", "m3")])
    s.test_run("coder-a", 1, 112, 90)
    s.finish("coder-a", result=130, collected=305, reaped=305, exit_at=305)
    truth += [("coder-a", "starting", 0, 2), ("coder-a", "model", 2, 10), ("coder-a", "tool", 10, 12), ("coder-a", "model", 12, 20),
              ("coder-a", "tool", 20, 22), ("coder-a", "test", 22, 112), ("coder-a", "tool", 112, 115),
              ("coder-a", "model", 115, 130), ("coder-a", "waiting:main", 130, 140), ("coder-a", "waiting:review", 140, 300),
              ("coder-a", "waiting:main", 300, 305)]
    # the review of coder-a (pi)
    r = "review-coder-a-1"
    s.task(r, 140, "pi", role="code-reviewer", review_of="coder-a")
    s.pi_transcript(r, [(142, "prompt", None), (150, "call", (142, [("r1", "bash")])), (170, "result", "r1"),
                        (180, "call", (170, [("r2", "read")])), (181, "result", "r2"), (290, "text", 181)])
    s.pi_state(r, 290)
    s.finish(r, result=290, collected=300, exit_at=300)
    truth += [(r, "starting", 140, 142), (r, "model", 142, 150), (r, "tool", 150, 170), (r, "model", 170, 180),
              (r, "tool", 180, 181), (r, "model", 181, 290), (r, "waiting:main", 290, 300)]
    # coder-b: overlaps coder-a; asks a question the owner answers through Main (tp-agent writes resolution.by "main")
    s.task("coder-b", 50, "claude")
    s.claude_state("coder-b", [53, 200])
    s.claude_transcript("coder-b", [(53, "prompt", None), (60, "use", ("b1", [("b1", "Bash")])), (61, "result", ["b1"]),
                                    (65, "text", "b2"), (200, "prompt", None), (220, "text", "b3")])
    s.request("coder-b", REQ, "capability", 60.5, resolved=200)
    s.events("coder-b", [(60.5, "request.opened", REQ, {"kind": "capability"}),
                         (200, "request.resolved", REQ, {"kind": "capability", "by": "main", "how": "message"})])
    s.capability("coder-b", 60, answered=200)
    s.finish("coder-b", result=220, collected=230, reaped=230, exit_at=230)
    truth += [("coder-b", "starting", 50, 53), ("coder-b", "model", 53, 60), ("coder-b", "tool", 60, 61),
              ("coder-b", "model", 61, 65), ("coder-b", "waiting:owner-input", 65, 200), ("coder-b", "model", 200, 220),
              ("coder-b", "waiting:main", 220, 230)]
    # coder-c: dispatched with --after coder-b at 100; queued until coder-b is collected, starts at 235
    s.queued("coder-c", 100, 235, "waiting for coder-b")
    s.task("coder-c", 235, "claude")
    s.claude_state("coder-c", [237])
    s.claude_transcript("coder-c", [(237, "prompt", None), (240, "use", ("c1", [("c1", "Edit")])), (241, "result", ["c1"]),
                                    (260, "text", "c2")])
    s.finish("coder-c", result=260, collected=270, reaped=270, exit_at=270)
    truth += [("coder-c", "waiting:dependency", 100, 235), ("coder-c", "starting", 235, 237), ("coder-c", "model", 237, 240),
              ("coder-c", "tool", 240, 241), ("coder-c", "model", 241, 260), ("coder-c", "waiting:main", 260, 270)]
    # coder-d: never takes its first prompt (H9), still there at the report time
    s.task("coder-d", 150, "claude")
    s.put(152, "tasks/coder-d/state.json", json.dumps({"id": "coder-d", "harness": "claude", "state": "idle", "updated": iso_s(152)}))
    truth += [("coder-d", "stalled", 150, 320)]
    # coder-e: pi worker killed mid-tool (stopped by Main, then reaped)
    s.task("coder-e", 20, "pi")
    s.pi_transcript("coder-e", [(22, "prompt", None), (30, "call", (22, [("e1", "bash")]))])
    s.finish("coder-e", stopped=80, reaped=80, exit_at=81)
    truth += [("coder-e", "starting", 20, 22), ("coder-e", "model", 22, 30), ("coder-e", "interrupted", 30, 80)]
    return s, truth


def state_at(report, task, t):
    for row in report["tasks"]:
        if row["task"] == task:
            for i in row["intervals"]:
                if i["start"] <= t < i["end"]:
                    return i["state"]
    return None


def score(report, truth):
    """(right, total, misses by (task, truth state, reported state)): per second of the ground truth."""
    right = total = 0
    misses = {}
    for task, state, a, b in truth:
        for sec in range(a, b):
            total += 1
            got = state_at(report, task, T0 + sec + 0.5)
            if got == state:
                right += 1
            else:
                key = (task, state, got)
                misses[key] = misses.get(key, 0) + 1
    return right, total, misses


def stall_delay(root):
    """Seconds from coder-d's start to the first report (taken every second) that shows it stalled."""
    s, _ = case1(root)
    for now in range(150, 271):
        report = timeline.build(s.materialize(now), now=T0 + now)
        if state_at(report, "coder-d", T0 + 150.5) == "stalled":
            return now - 150
    return None


def one(root, ident="w", worker="claude", created=0, prompts=(2,), lines=(), finish=None, extra=None):
    """A session with one task (plus what `extra` adds); returns (session, tasks dir as of 1000 s)."""
    s = Session(root)
    s.task(ident, created, worker)
    if worker == "claude":
        s.claude_state(ident, list(prompts))
        if lines:
            s.claude_transcript(ident, list(lines), skew=0)
    elif lines:
        s.pi_transcript(ident, list(lines))
    if finish:
        s.finish(ident, **finish)
    if extra:
        extra(s)
    return s


class WaitTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wt1.")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def report(self, session, now):
        return timeline.build(session.materialize(now), now=T0 + now)

    def test_rules(self):
        done = {"result": 20, "collected": 30, "exit_at": 30}
        talk = [(2, "prompt", None), (5, "use", ("m1", [("t1", "Bash")])), (15, "result", ["t1"]), (20, "text", "m2")]

        def at(report, t, task="w"):
            return state_at(report, task, T0 + t)

        def problems(report):
            return " | ".join(p["problem"] for p in report["problems"])

        rows = [
            # (name, session builder, now, checks: [(fn(report) -> value, expected)])
            ("D1 finished: span ends at collected, reply waits for Main",
             lambda r: one(r, lines=talk, finish=done), 100,
             [(lambda x: x["tasks"][0]["end"] - T0, 30), (lambda x: at(x, 25), "waiting:main"), (lambda x: at(x, 10), "tool")]),
            ("D1 stopped wins over a later collected; reaped and exit only without them",
             lambda r: one(r, lines=talk, finish={"stopped": 18, "collected": 40, "reaped": 19, "exit_at": 19}), 100,
             [(lambda x: x["tasks"][0]["end"] - T0, 18), (lambda x: x["tasks"][0]["end_source"], "stopped.json")]),
            ("D1 running: last interval open at now",
             lambda r: one(r, lines=talk[:2]), 12,
             [(lambda x: x["tasks"][0]["running"], True), (lambda x: x["tasks"][0]["intervals"][-1].get("open"), True),
              (lambda x: x["tasks"][0]["intervals"][-1]["state"], "tool")]),
            ("D3 stall: no prompt past 60 s is stalled from created (naive: starting)",
             lambda r: one(r, prompts=()), 61, [(lambda x: at(x, 1), "stalled")]),
            ("D3 stall threshold not reached: starting, open",
             lambda r: one(r, prompts=()), 59, [(lambda x: at(x, 1), "starting")]),
            ("D3 a reply without any prompt record is not a stall (pi, no transcript): unknown until the reply",
             lambda r: one(r, worker="pi", finish={"result": 80, "collected": 90, "exit_at": 90}), 100,
             [(lambda x: at(x, 30), "unknown"), (lambda x: at(x, 85), "waiting:main")]),
            ("D4 killed mid-tool is interrupted (naive: tool until the end)",
             lambda r: one(r, lines=talk[:2], finish={"stopped": 12, "exit_at": 13}), 100,
             [(lambda x: at(x, 8), "interrupted"), (lambda x: at(x, 3), "model")]),
            ("D4 a new prompt abandons an open call: interrupted, not tool forever",
             lambda r: one(r, prompts=(2, 30), lines=talk[:2] + [(30, "prompt", None), (40, "text", "m3")],
                           finish={"result": 40, "collected": 50, "exit_at": 50}), 100,
             [(lambda x: at(x, 10), "interrupted"), (lambda x: at(x, 35), "model")]),
            ("D4 idle between turns waits on Main's next prompt",
             lambda r: one(r, prompts=(2, 50), lines=talk + [(50, "prompt", None), (60, "text", "m3")],
                           finish={"result": 60, "collected": 70, "exit_at": 70}), 100,
             [(lambda x: at(x, 30), "waiting:main"), (lambda x: at(x, 55), "model")]),
            ("D4 pi parallel calls count once as tool",
             lambda r: one(r, worker="pi", lines=[(2, "prompt", None), (5, "call", (2, [("p1", "bash"), ("p2", "read")])),
                                                 (6, "result", "p2"), (15, "result", "p1"), (20, "text", 15)], finish=done), 100,
             [(lambda x: at(x, 10), "tool"), (lambda x: at(x, 3), "model"), (lambda x: x["tasks"][0]["by_state"]["tool"], 10.0)]),
            ("D2 review holds its coder: waiting:review (naive: waiting:main)",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 90, "exit_at": 90},
                           extra=lambda s: (s.task("rev", 40, "pi", role="code-reviewer", review_of="w"),
                                            s.finish("rev", result=70, collected=80, exit_at=80))), 100,
             [(lambda x: at(x, 30), "waiting:main"), (lambda x: at(x, 50), "waiting:review"), (lambda x: at(x, 85), "waiting:main")]),
            ("D2 a request open while the worker works: activity wins; idle seconds wait",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 60, "exit_at": 60},
                           extra=lambda s: s.request("w", REQ, "capability", 6, resolved=50)), 100,
             [(lambda x: at(x, 10), "tool"), (lambda x: at(x, 30), "waiting:main"),
              (lambda x: [i["source"] for i in x["tasks"][0]["intervals"] if i["start"] <= T0 + 30 < i["end"]][0],
               "requests/{}.json".format(REQ))]),
            ("R11 permission request waits on permission; resolved by owner is owner input",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 90, "exit_at": 90},
                           extra=lambda s: (s.request("w", REQ, "permission", 21, resolved=40),
                                            s.request("w", REQ2, "input", 41, resolved=60, by="owner"))), 100,
             [(lambda x: at(x, 30), "waiting:permission"), (lambda x: at(x, 50), "waiting:owner-input")]),
            ("R10 events.jsonl stands in for a missing request record",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 90, "exit_at": 90},
                           extra=lambda s: s.events("w", [(21, "request.opened", REQ, {"kind": "permission"}),
                                                          (40, "request.resolved", REQ, {"kind": "permission", "by": "hook"})])), 100,
             [(lambda x: at(x, 30), "waiting:permission")]),
            ("R12 pause.confirmed: paused by Main until activity",
             lambda r: one(r, prompts=(2, 50), lines=talk + [(50, "prompt", None), (60, "text", "m3")],
                           finish={"result": 60, "collected": 70, "exit_at": 70},
                           extra=lambda s: s.events("w", [(25, "pause.confirmed", None, {})])), 100,
             [(lambda x: [i["wait_reason"] for i in x["tasks"][0]["intervals"] if i["start"] <= T0 + 30 < i["end"]][0],
               "paused by Main")]),
            ("R14 capability-request.json alone is a Main wait; beside a request record it is not counted twice",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 90, "exit_at": 90},
                           extra=lambda s: (s.capability("w", 25, answered=40), s.request("w", REQ, "capability", 25.5,
                                                                                          resolved=40))), 100,
             [(lambda x: at(x, 30), "waiting:main"), (lambda x: sum(1 for i in x["tasks"][0]["intervals"]
                                                                   if i["source"] == "capability-request.json"), 0)]),
            ("R15 test run inside a shell call is test; R16 gate record too",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (s.test_run("w", 1, 12, 4), s.gate("w", 13, 14))), 100,
             [(lambda x: at(x, 10), "test"), (lambda x: at(x, 13.5), "test"), (lambda x: at(x, 6), "tool")]),
            ("R15 without a transcript a test run paints between prompt and reply",
             lambda r: one(r, finish=done, extra=lambda s: s.test_run("w", 1, 12, 4)), 100,
             [(lambda x: at(x, 10), "test"), (lambda x: at(x, 5), "unknown")]),
            ("D7 test-run mtime older than created: dropped and listed",
             lambda r: one(r, created=100, prompts=(102,), finish={"result": 120, "collected": 130, "exit_at": 130},
                           extra=lambda s: s.test_run("w", 1, 50, 4)), 200,
             [(lambda x: "record ends before created" in problems(x), True), (lambda x: "test" in x["tasks"][0]["by_state"], False)]),
            ("D7 a 2 s clock disagreement is silent; 5 s is listed",
             lambda r: one(r, created=10, prompts=(12,), lines=[(8.5, "prompt", None), (15, "text", "m")],
                           finish={"result": 15, "collected": 20, "exit_at": 20}), 100,
             [(lambda x: "before-created" in problems(x), False)]),
            ("D7 (5 s)",
             lambda r: one(r, created=10, prompts=(12,), lines=[(5, "prompt", None), (15, "text", "m")],
                           finish={"result": 15, "collected": 20, "exit_at": 20}), 100,
             [(lambda x: "before-created" in problems(x), True)]),
            ("D8 overlap counts once in elapsed and per worker in accumulated",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (s.task("v", 10, "claude"), s.claude_state("v", [11]),
                                                                        s.finish("v", result=35, collected=40, exit_at=40))), 100,
             [(lambda x: x["totals"]["elapsed"], 40.0), (lambda x: x["totals"]["busy"], 40.0),
              (lambda x: x["totals"]["accumulated"], 60.0)]),
            ("D9 critical path: gap between tasks is unknown; review hold names the focus",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 300, "exit_at": 300},
                           extra=lambda s: (s.task("rev", 40, "pi", role="code-reviewer", review_of="w"),
                                            s.finish("rev", result=290, collected=295, exit_at=295),
                                            s.task("next", 330, "claude"), s.claude_state("next", [331]),
                                            s.finish("next", result=340, collected=350, exit_at=350))), 400,
             [(lambda x: x["critical_path"]["tasks"], ["w", "next"]), (lambda x: x["critical_path"]["focus"]["state"], "waiting:review"),
              (lambda x: x["critical_path"]["by_state"]["unknown"] >= 30, True)]),
            ("R22 a queued task waits by its reason (lane, slots, dependency)",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (s.queued("q1", 5, None, "waiting for lane x's writer w"),
                                                                        s.queued("q2", 5, None, "global worker slots full"),
                                                                        s.queued("q3", 5, None, "dependency w has not started"))), 50,
             [(lambda x: at(x, 10, "q1"), "waiting:lane-lease"), (lambda x: at(x, 10, "q2"), "waiting:worker-slot"),
              (lambda x: at(x, 10, "q3"), "waiting:dependency")]),
            ("D10 malformed and missing records are listed, never raise",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (
                 s.put(1, "tasks/w/state.json", "{not json"), s.put(1, "tasks/w/test-runs/1.json", "[1]"),
                 s.put(1, "tasks/w/requests/{}.json".format(REQ), "{}"), s.put(1, "tasks/w/collected.json", '{"collected": "x"}'),
                 s.put(1, "tasks/w/events.jsonl", "nope\n"), s.put(1, "tasks/bad/meta.json", "{}"),
                 s.put(1, "tasks/w2/meta.json", json.dumps({"created": iso_s(0), "worker": "claude"})),
                 s.put(1, "tasks/w2/transcript-path", "/etc/passwd\n"))), 100,
             [(lambda x: len(x["problems"]) >= 7, True), (lambda x: "transcript-path does not name" in problems(x), True),
              (lambda x: x["tasks"][0]["end_source"], "exit (mtime)")]),
        ]
        for name, build, now, checks in rows:
            with self.subTest(name):
                root = Path(self.tmp) / str(abs(hash(name)))
                report = self.report(build(root), now)
                for fn, expected in checks:
                    self.assertEqual(fn(report), expected, name)

    def test_case1_controlled_delays(self):
        root = Path(self.tmp) / "case1"
        session, truth = case1(root)
        report = self.report(session, 320)
        right, total, misses = score(report, truth)
        # arm A loses exactly the two waits it has no record of: the queue wait after the queue file is deleted, and who
        # answered a request (tp-agent writes resolution.by "main" even when the owner decided). Everything else is right.
        self.assertEqual(misses, {("coder-c", "waiting:dependency", None): 135,
                                  ("coder-b", "waiting:owner-input", "waiting:main"): 135})
        self.assertEqual((right, total), (775, 1045))
        self.assertEqual(report["critical_path"]["focus"]["state"], "waiting:review")
        self.assertEqual(report["totals"]["elapsed"], 320.0)
        review = next(r for r in report["tasks"] if r["task"] == "review-coder-a-1")
        self.assertEqual([(i["state"], i["actor"]) for i in review["intervals"][:2]], [("starting", "reviewer"), ("model", "reviewer")])
        # live, mid-schedule: the queued child is visible with its reason, and the stall shows 60 s after the start
        live = self.report(session, 120)
        self.assertEqual(state_at(live, "coder-c", T0 + 110), "waiting:dependency")
        self.assertEqual(stall_delay(Path(self.tmp) / "stall"), 61)  # STALL_S is exceeded, not reached
        # read-only and text-free: the session tree is unchanged and no transcript or record text is printed
        before = sorted((str(p), p.stat().st_mtime) for p in root.rglob("*"))
        out = timeline.render(timeline.build(root / "tasks", now=T0 + 320))
        out += json.dumps(timeline._jsonable(timeline.build(root / "tasks", now=T0 + 320)))
        self.assertEqual(before, sorted((str(p), p.stat().st_mtime) for p in root.rglob("*")))
        for secret in ("SECRET", "PROMPT", "OUT\""):
            self.assertNotIn(secret, out)

    def test_cli(self):
        """One CLI test: argument parsing and exit codes."""
        session, _ = case1(Path(self.tmp) / "cli")
        tasks = session.materialize(320)
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(timeline.main([str(tasks), "--task", "coder-a", "--json", "--now", iso_s(320)]), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual([r["task"] for r in data["tasks"]], ["coder-a"])
        self.assertEqual(set(data["tasks"][0]["intervals"][0]), {"task", "actor", "state", "start", "end", "wait_reason", "source",
                                                                 "confidence"})
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                timeline.main([str(tasks), "--now", "yesterday"])


if __name__ == "__main__":
    unittest.main()
