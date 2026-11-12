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
import sys
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
    def task(self, ident, created, worker, role="coder", review_of=None, lane=None, queued_at=None):
        meta = {"id": ident, "worker": worker, "model": "m", "thinking": "medium", "pane": "%1", "workdir": "/nonexistent",
                "created": iso_s(created), "role": role}
        if queued_at is not None:  # bin/tp-agent record_queue_wait: the queue entry's created (iso_s), or a malformed value
            meta["queued_at"] = iso_s(queued_at) if isinstance(queued_at, (int, float)) else queued_at
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

    def request(self, ident, rid, kind, opened, resolved=None, by="main", how="message", answer=None):
        """answer: (t, by) for hub_records.answer: an answer recorded on a request the worker's state change had closed."""
        def record(now):
            r = {"id": rid, "task": ident, "kind": kind, "opened": iso_ms(opened), "text_sha256": "0" * 64, "summary": "SECRET-ASK",
                 "status": "open"}
            if resolved is not None and resolved <= now:
                r.update(status="resolved", resolved=iso_ms(resolved), resolution={"by": by, "how": how, "ref": None})
                if answer is not None and answer[0] <= now:
                    r["resolution"]["answer"] = {"by": answer[1], "how": "message", "ref": None, "at": iso_ms(answer[0])}
            return json.dumps(r, indent=2)
        self.put(opened, "tasks/{}/requests/{}.json".format(ident, rid), record)

    def events(self, ident, items, folder="tasks"):
        """items: (t, kind, ref, detail) or (t, kind, ref, detail, state)."""
        def content(now):
            return "".join(json.dumps({"v": 1, "id": "ev-{}".format(n), "at": iso_ms(item[0]), "task": ident, "actor": "main",
                                       "kind": item[1], "ref": item[2], "detail": item[3],
                                       **({"state": item[4]} if len(item) > 4 else {}), "source": "file", "confidence": "exact"}) + "\n"
                           for n, item in enumerate(items) if item[0] <= now) or None
        self.put(min(i[0] for i in items), "{}/{}/events.jsonl".format(folder, ident), content)

    def cancelled(self, ident, created, log, at):
        """bin/tp-agent record_queue_cancel (`tp-agent stop` of a queued task, HB6b1 round 2): the entry and its queue.reason
        events (queued per log entry, backdated, then cancelled) in queue-cancelled/<id>/, from the stop on."""
        entry = {"id": ident, "role": "coder", "worker": "pi", "model": "m", "created": iso_s(created), "after": ["w"],
                 "queue_reason": log[-1][1] if log else "", "queue_log": [{"at": iso_s(t), "reason": r} for t, r in log], "workdir": "/w"}
        self.put(at, "queue-cancelled/{}/entry.json".format(ident), json.dumps(entry))
        self.events(ident, [(t, "queue.reason", None, {"reason": r}, "queued") for t, r in log]
                    + [(at, "queue.reason", None, {"reason": entry["queue_reason"]}, "cancelled")], folder="queue-cancelled")

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

    def queued(self, ident, created, removed, reason, log=None):
        """log: [(t, reason)] as bin/tp-agent with_queue_reason keeps it in queue_log (HB6b1); None: arm A's entry."""
        entry = {"id": ident, "role": "coder", "worker": "claude", "model": "m", "created": iso_s(created), "after": ["coder-b"],
                 "queue_reason": reason, "workdir": "/w"}
        if log is not None:
            entry["queue_log"] = [{"at": iso_s(t), "reason": r} for t, r in log]
        self.put(created, "queue/{}.json".format(ident), json.dumps(entry), removed=removed)

    def queue_events(self, ident, log, released):
        """bin/tp-agent record_queue_wait at the task's start (`released`): one queue.reason (state queued) per queue_log
        entry, backdated to its time, then one released. The lines exist only from `released` on."""
        def content(now):
            if now < released:
                return None
            items = [(t, "queued", r) for t, r in log] + [(released, "released", log[-1][1] if log else "")]
            return "".join(json.dumps({"v": 1, "id": "ev-q{}".format(n), "at": iso_s(t), "task": ident, "actor": "main",
                                       "kind": "queue.reason", "state": st, "detail": {"reason": r}, "source": "file",
                                       "confidence": "exact"}) + "\n" for n, (t, st, r) in enumerate(items))
        self.put(released, "tasks/{}/events.jsonl".format(ident), content)


REQ = "req-01JZ0000000000000000000000"
REQ2 = "req-01JZ0000000000000000000001"


def case1(root, arm_b=False):
    """The planted schedule of case 1 (seconds after T0) and its ground truth [(task, state, start, end)]. arm_b: the records
    HB6b1's producers add (coder-c's queue_log, queued_at and queue.reason events; coder-b's request resolved by owner)."""
    s = Session(root)
    truth = []
    # coder-a: a 90 s test run inside a shell call, then a review holds it until Main collects
    s.task("coder-a", 0, "claude")
    s.claude_state("coder-a", [2])
    s.claude_transcript("coder-a", [(2, "prompt", None), (10, "use", ("m1", [("a1", "Read")])), (12, "result", ["a1"]),
                                    (16, "text", "m2"), (20, "use", ("m2", [("a2", "Bash")])), (115, "result", ["a2"]),
                                    (130, "text", "m3")])
    s.test_run("coder-a", 1, 112, 90)
    s.finish("coder-a", result=130, collected=305, reaped=138, exit_at=138)  # tp-review ends the author first, uncollected
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
    by = "owner" if arm_b else "main"  # arm B: tp-agent message --resolves REQ --by owner --quote "..."
    s.request("coder-b", REQ, "capability", 60.5, resolved=200, by=by)
    s.events("coder-b", [(60.5, "request.opened", REQ, {"kind": "capability"}),
                         (200, "request.resolved", REQ, {"kind": "capability", "by": by, "how": "message"})])
    s.capability("coder-b", 60, answered=200)
    s.finish("coder-b", result=220, collected=230, reaped=230, exit_at=230)
    truth += [("coder-b", "starting", 50, 53), ("coder-b", "model", 53, 60), ("coder-b", "tool", 60, 61),
              ("coder-b", "model", 61, 65), ("coder-b", "waiting:owner-input", 65, 200), ("coder-b", "model", 200, 220),
              ("coder-b", "waiting:main", 220, 230)]
    # coder-c: dispatched with --after coder-b at 100; queued until coder-b is collected, starts at 235
    log = [(100, "waiting for coder-b")] if arm_b else None
    s.queued("coder-c", 100, 235, "waiting for coder-b", log=log)
    s.task("coder-c", 235, "claude", queued_at=100 if arm_b else None)
    if arm_b:
        s.queue_events("coder-c", log, 235)
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
        held = [(2, "prompt", None), (5, "use", ("m1", [("t1", "Bash")])), (50, "result", ["t1"]), (60, "text", "m2")]
        # bin/tp-review's order: the author is reaped (not collected), then the review is spawned
        review = lambda s: (s.task("rev", 31, "pi", role="code-reviewer", review_of="w"),  # noqa: E731
                            s.finish("rev", result=70, collected=80, exit_at=80))

        def at(report, t, task="w"):
            return state_at(report, task, T0 + t)

        def row(report, task):
            return next(r for r in report["tasks"] if r["task"] == task)

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
            ("D2 review holds its coder: waiting:review (naive: waiting:main); reaped before the review, collected after",
             lambda r: one(r, lines=talk, finish={"result": 20, "reaped": 30, "exit_at": 30, "collected": 90}, extra=review), 100,
             [(lambda x: at(x, 25), "waiting:main"), (lambda x: at(x, 50), "waiting:review"), (lambda x: at(x, 85), "waiting:main"),
              (lambda x: x["tasks"][0]["end_source"], "collected.json")]),
            ("D1/D2 reaped before its review and never collected: a reap ends the process, not the task; ends with the review",
             lambda r: one(r, lines=talk, finish={"result": 20, "reaped": 30, "exit_at": 30}, extra=review), 100,
             [(lambda x: at(x, 50), "waiting:review"), (lambda x: x["tasks"][0]["end"] - T0, 80),
              (lambda x: x["tasks"][0]["by_state"]["waiting:review"], 49.0),
              (lambda x: x["tasks"][0]["end_source"], "review rev collected.json")]),
            ("D1/D2 reaped, review still running: the coder is open on waiting:review",
             lambda r: one(r, lines=talk, finish={"result": 20, "reaped": 30, "exit_at": 30}, extra=review), 60,
             [(lambda x: at(x, 50), "waiting:review"), (lambda x: x["tasks"][0]["running"], True),
              (lambda x: (x["tasks"][0]["intervals"][-1]["state"], x["tasks"][0]["intervals"][-1].get("open")),
               ("waiting:review", True))]),
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
            ("R11 permission inside a tool call waits until resolved (naive: tool); a capability request beside work stays work",
             lambda r: one(r, lines=held, finish={"result": 60, "collected": 70, "exit_at": 70},
                           extra=lambda s: (s.request("w", REQ, "permission", 6, resolved=50, by="hook"),
                                            s.request("w", REQ2, "capability", 52, resolved=65))), 100,
             [(lambda x: at(x, 5.5), "tool"), (lambda x: at(x, 30), "waiting:permission"), (lambda x: at(x, 55), "model"),
              (lambda x: x["tasks"][0]["by_state"]["waiting:permission"], 44.0)]),
            ("R11 running tail: an open permission request in an open call is waiting:permission to now, open",
             lambda r: one(r, lines=held[:2], extra=lambda s: s.request("w", REQ, "permission", 6)), 40,
             [(lambda x: at(x, 30), "waiting:permission"),
              (lambda x: (x["tasks"][0]["intervals"][-1]["state"], x["tasks"][0]["intervals"][-1].get("open")),
               ("waiting:permission", True))]),
            ("R4 running tail with no request record: state.json permission (the hook's state now) waits from its update",
             lambda r: one(r, lines=held[:2], extra=lambda s: s.put(8, "tasks/w/state.json", json.dumps(
                 {"id": "w", "harness": "claude", "state": "permission", "tool": "x", "updated": iso_s(8)}))), 40,
             [(lambda x: at(x, 6), "tool"), (lambda x: at(x, 30), "waiting:permission")]),
            ("R10 events.jsonl stands in for a missing request record",
             lambda r: one(r, lines=talk, finish={"result": 20, "collected": 90, "exit_at": 90},
                           extra=lambda s: s.events("w", [(21, "request.opened", REQ, {"kind": "permission"}),
                                                          (40, "request.resolved", REQ, {"kind": "permission", "by": "hook"})])), 100,
             [(lambda x: at(x, 30), "waiting:permission")]),
            ("R10 events.jsonl permission inside a tool call (naive: tool)",
             lambda r: one(r, lines=held, finish={"result": 60, "collected": 70, "exit_at": 70},
                           extra=lambda s: s.events("w", [(6, "request.opened", REQ, {"kind": "permission"}),
                                                          (50, "request.resolved", REQ, {"kind": "permission", "by": "hook"})])), 100,
             [(lambda x: at(x, 30), "waiting:permission"), (lambda x: at(x, 5.5), "tool")]),
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
            ("D9 critical path: gap between tasks is unknown; review hold names the focus; an overlapping predecessor counts,"
             " so the path tiles the whole window from the first created to the last logical end (a later stop or review end on a reaped coder counts)",
             lambda r: one(r, lines=talk, finish={"result": 20, "reaped": 30, "exit_at": 30, "collected": 300},
                           extra=lambda s: (s.task("rev", 40, "pi", role="code-reviewer", review_of="w"),
                                            s.finish("rev", result=290, collected=295, exit_at=295),
                                            s.task("early", -50, "claude"), s.claude_state("early", [-49]),
                                            s.finish("early", result=10, collected=20, exit_at=20),
                                            s.task("next", 330, "claude"), s.claude_state("next", [331]),
                                            s.finish("next", result=340, collected=350, exit_at=350),
                                            s.task("late", 355, "claude"), s.claude_state("late", [356]),
                                            s.finish("late", result=380, stopped=390, exit_at=390),
                                            s.task("x", 400, "claude"), s.claude_state("x", [401]),
                                            s.finish("x", result=405, reaped=410, exit_at=410),
                                            s.task("rx", 411, "pi", role="code-reviewer", review_of="x"),
                                            s.finish("rx", result=440, reaped=445, exit_at=445))), 500,
             [(lambda x: x["critical_path"]["tasks"][:3], ["early", "w", "next"]),
              (lambda x: x["critical_path"]["tasks"][-1], "rx"),
              (lambda x: x["critical_path"]["intervals"][-1]["end"] - T0, 445.0),
              (lambda x: x["critical_path"]["intervals"][-1]["end"] == x["totals"]["window_end"], True),
              (lambda x: x["critical_path"]["focus"]["state"], "waiting:review"),
              (lambda x: x["critical_path"]["by_state"]["unknown"] >= 30, True),
              (lambda x: [(i["start"] - T0, i["end"] - T0) for i in x["critical_path"]["intervals"]][0][0], -50),
              (lambda x: all(abs(a["end"] - b["start"]) < 1e-9 for a, b in zip(x["critical_path"]["intervals"],
                                                                               x["critical_path"]["intervals"][1:])), True),
              (lambda x: sum(i["end"] - i["start"] for i in x["critical_path"]["intervals"]), 495.0)]),
            ("R22 a queued task waits by its reason (lane, slots, dependency)",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (s.queued("q1", 5, None, "waiting for lane x's writer w"),
                                                                        s.queued("q2", 5, None, "global worker slots full"),
                                                                        s.queued("q3", 5, None, "dependency w has not started"))), 50,
             [(lambda x: at(x, 10, "q1"), "waiting:lane-lease"), (lambda x: at(x, 10, "q2"), "waiting:worker-slot"),
              (lambda x: at(x, 10, "q3"), "waiting:dependency")]),
            ("B1 a started queued task: queued_at and its queue.reason events paint [queued_at, created] exact, each reason until"
             " the next (lane, then dependency); the row starts at queued_at (arm A: nothing before created)",
             lambda r: one(r, created=40, prompts=(42,), finish={"result": 60, "collected": 70, "exit_at": 70}, extra=lambda s: (
                 s.task("w", 40, "claude", queued_at=10),
                 s.queue_events("w", [(10, "waiting for lane x's writer v"), (25, "waiting for v")], 40))), 100,
             [(lambda x: at(x, 15), "waiting:lane-lease"), (lambda x: at(x, 30), "waiting:dependency"), (lambda x: at(x, 41), "starting"),
              (lambda x: x["tasks"][0]["start"] - T0, 10),
              (lambda x: [(i["source"], i["confidence"]) for i in x["tasks"][0]["intervals"] if i["start"] <= T0 + 30 < i["end"]][0],
               ("events.jsonl queue.reason", "exact"))]),
            ("B1 queued_at without any queue.reason event: the queued seconds are unknown and the gap is listed",
             lambda r: one(r, created=40, prompts=(42,), finish={"result": 60, "collected": 70, "exit_at": 70},
                           extra=lambda s: s.task("w", 40, "claude", queued_at=10)), 100,
             [(lambda x: at(x, 30), "unknown"), (lambda x: "queued_at but no queue.reason" in problems(x), True)]),
            ("B1 malformed queued_at, or one after created: not used, listed; the row is arm A's (starts at created)",
             lambda r: one(r, created=40, prompts=(42,), finish={"result": 60, "collected": 70, "exit_at": 70}, extra=lambda s: (
                 s.task("w", 40, "claude", queued_at="yesterday"), s.queue_events("w", [(10, "waiting for v")], 40),
                 s.task("v", 40, "claude", queued_at=50), s.claude_state("v", [42]))), 100,
             [(lambda x: x["tasks"][0]["start"] - T0, 40), (lambda x: at(x, 30), None),
              (lambda x: problems(x).count("queued_at unreadable or after created"), 2)]),
            ("B1 a still-queued item's queue_log: each reason holds from its time (slots, then dependency), exact; open at now",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: s.queued(
                 "q1", 5, None, "dependency w has not started", log=[(5, "global worker slots full"), (12, "dependency w has not started")])), 50,
             [(lambda x: at(x, 8, "q1"), "waiting:worker-slot"), (lambda x: at(x, 30, "q1"), "waiting:dependency"),
              (lambda x: [(i["confidence"], i.get("open")) for i in row(x, "q1")["intervals"]], [("exact", None), ("exact", True)])]),
            ("B1 a queue_log that is not a list: arm A's single derived segment from queue_reason",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: s.put(5, "queue/q1.json", json.dumps({
                 "id": "q1", "created": iso_s(5), "worker": "claude", "queue_reason": "waiting for w", "queue_log": "x"}))), 50,
             [(lambda x: [(i["state"], i["confidence"]) for i in row(x, "q1")["intervals"]], [("waiting:dependency", "derived")])]),
            ("B3 a permission prompt in a call closed approved (at the call's end): waiting:permission, inferred, said so;"
             " denied is named and exact",
             lambda r: one(r, lines=held, finish={"result": 60, "collected": 70, "exit_at": 70}, extra=lambda s: (
                 s.request("w", REQ, "permission", 6, resolved=50, by="hook", how="approved"),
                 s.task("v", 0, "claude"), s.claude_state("v", [2]), s.claude_transcript("v", held, skew=0),
                 s.finish("v", result=60, collected=70, exit_at=70),
                 s.request("v", REQ2, "permission", 6, resolved=30, by="hook", how="denied"))), 100,
             [(lambda x: at(x, 30), "waiting:permission"),
              (lambda x: [(i["confidence"], "approval instant is not recorded" in i["wait_reason"])
                          for i in row(x, "w")["intervals"] if i["start"] <= T0 + 30 < i["end"]][0], ("inferred", True)),
              (lambda x: [(i["confidence"], i["wait_reason"]) for i in row(x, "v")["intervals"] if i["start"] <= T0 + 20 < i["end"]][0],
               ("exact", "permission prompt inside a tool call denied")), (lambda x: at(x, 40, "v"), "tool")]),
            ("B3 round 2: approved without a transcript is inferred too, from the request file and from events alone",
             lambda r: one(r, prompts=(2,), finish={"result": 60, "collected": 70, "exit_at": 70}, extra=lambda s: (
                 s.request("w", REQ, "permission", 6, resolved=50, by="hook", how="approved"),
                 s.task("v", 0, "claude"), s.claude_state("v", [2]), s.finish("v", result=60, collected=70, exit_at=70),
                 s.events("v", [(6, "request.opened", REQ2, {"kind": "permission"}),
                                (30, "request.resolved", REQ2, {"kind": "permission", "by": "hook", "how": "approved"})]))), 100,
             [(lambda x: [next((i["state"], i["confidence"], "approval instant is not recorded" in i["wait_reason"])
                               for i in row(x, t)["intervals"] if i["start"] <= T0 + 20 < i["end"]) for t in ("w", "v")],
               [("waiting:permission", "inferred", True)] * 2)]),
            ("B2 round 2: an owner answer recorded on a request the worker's state change closed (file: resolution.answer;"
             " events: request.resolved detail.answer) is owner input; the close alone (by worker) stays main",
             lambda r: one(r, prompts=(2,), finish={"result": 60, "collected": 70, "exit_at": 70}, extra=lambda s: (
                 s.request("w", REQ, "input", 6, resolved=30, by="worker", how="left-state", answer=(31, "owner")),
                 s.request("w", REQ2, "input", 35, resolved=50, by="worker", how="left-state"),
                 s.task("v", 0, "claude"), s.claude_state("v", [2]), s.finish("v", result=60, collected=70, exit_at=70),
                 s.events("v", [(6, "request.opened", REQ, {"kind": "input"}),
                                (30, "request.resolved", REQ, {"kind": "input", "by": "worker", "how": "left-state"}),
                                (31, "request.resolved", REQ, {"kind": "input", "by": "owner", "how": "message", "answer": True})]))), 100,
             [(lambda x: (at(x, 20), at(x, 40), at(x, 20, "v")), ("waiting:owner-input", "waiting:main", "waiting:owner-input"))]),
            ("B1 round 2: a queued task stopped before it started (queue-cancelled/<id>/): its row runs from created to the"
             " cancel, each reason exact; a cancelled record without the cancelled event is listed, not drawn",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: (
                 s.cancelled("q1", 5, [(5, "global worker slots full"), (12, "waiting for w")], 40),
                 s.put(5, "queue-cancelled/q2/entry.json", json.dumps({"id": "q2", "created": iso_s(5)})))), 100,
             [(lambda x: at(x, 8, "q1"), "waiting:worker-slot"), (lambda x: at(x, 30, "q1"), "waiting:dependency"),
              (lambda x: (row(x, "q1")["end"] - T0, row(x, "q1")["role"], row(x, "q1")["running"]), (40, "cancelled", False)),
              (lambda x: {i["confidence"] for i in row(x, "q1")["intervals"]}, {"exact"}),
              (lambda x: ("q2" in [t["task"] for t in x["tasks"]], "no readable queue.reason cancelled" in problems(x)), (False, True))]),
            ("F5 (round 2): a queue reason dated after the report time (skewed clock, or a historical --now) never draws the"
             " row's unknown prefix past the row's end",
             lambda r: one(r, lines=talk, finish=done, extra=lambda s: s.queued("q9", 5, None, "waiting for w", log=[(80, "waiting for w")])),
             50,
             [(lambda x: [(i["start"] - T0, i["end"] - T0, i["state"]) for i in row(x, "q9")["intervals"]], [(5, 50, "unknown")])]),
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
        # arm B (HB6b1): with coder-c's queued_at, queue_log and queue.reason events and coder-b's answer recorded by owner,
        # both misses are gone; the queue wait is exact, from the events, after the queue file is deleted
        armb, armb_truth = case1(Path(self.tmp) / "case1b", arm_b=True)
        report_b = self.report(armb, 320)
        self.assertEqual(score(report_b, armb_truth), (1045, 1045, {}))
        row = next(r for r in report_b["tasks"] if r["task"] == "coder-c")
        self.assertEqual((row["intervals"][0]["source"], row["intervals"][0]["confidence"], row["start"] - T0),
                         ("events.jsonl queue.reason", "exact", 100))
        self.assertEqual(report_b["problems"], report["problems"])
        live_b = self.report(armb, 120)
        self.assertEqual([(i["state"], i["source"]) for i in next(r for r in live_b["tasks"] if r["task"] == "coder-c")["intervals"]],
                         [("waiting:dependency", "queue/coder-c.json queue_log")])
        # round 3: the path ends where the window ends. coder-d is still running at 320 (stalled since 150), so the path
        # runs 0..320 through it and its 170 s of stalled outweigh the review hold (the elapsed window already counted it)
        self.assertEqual(report["critical_path"]["focus"]["state"], "stalled")
        self.assertEqual(report["critical_path"]["intervals"][-1]["end"], report["totals"]["window_end"])
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


class StatusTest(unittest.TestCase):
    """tools/waits/status.py (HB6c): one real git repository with one lane per case and a session dir of task records; one
    table-driven method over the lanes, one CLI check."""
    NOW = T0 + 5000
    COVERAGE = "\n## Coverage\n- I read the whole diff and checked every changed line\n"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "lib"))
        sys.path.insert(0, str(ROOT / "tools" / "waits"))
        import lane_lease
        import lanes
        import status
        cls.lanes, cls.status, cls.lane_lease = lanes, status, lane_lease
        cls.tmp = tempfile.mkdtemp(prefix="hb6c.")
        cls.base = Path(os.path.realpath(cls.tmp))
        from lane_fixture import git
        cls.git = staticmethod(git)
        cls.repo = cls.make_repo(cls.base / "repo")
        cls.common = lanes.git_common_dir(cls.repo)
        cls.state = cls.base / "session"
        cls.tasks = cls.state / "tasks"
        cls.tasks.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def make_repo(cls, path):
        path.mkdir()
        cls.git(path, "init", "-q", "-b", "main")
        (path / "f").write_text("1")
        cls.git(path, "add", "f")
        cls.git(path, "commit", "-qm", "one")
        return path

    @classmethod
    def lane(cls, repo, name, closed=None, commit=False, raw=None):
        """A lane of `repo`: its worktree and branch, and its record (`raw`: this text instead). commit: one commit on its branch."""
        common = cls.lanes.git_common_dir(repo)
        wt = cls.base / (repo.name + "-wt-" + name)
        cls.git(repo, "worktree", "add", "-q", "-b", "lane/" + name, str(wt), "HEAD")
        if commit:
            (wt / "g").write_text(name)
            cls.git(wt, "add", "g")
            cls.git(wt, "commit", "-qm", name)
        head = cls.git(wt, "rev-parse", "HEAD")
        record = {"schema": "tp-lane:v1", "name": name, "kind": "work", "state": "open", "repo": str(repo), "git_common_dir": common,
                  "branch": "lane/" + name, "worktree": str(wt), "base": {"ref": "main", "commit": cls.git(repo, "rev-parse", "HEAD")},
                  "created": iso_off(-86400 * 2), "domain": "objective of " + name, "touches": ["lib"],
                  "closed": [], "integrated": None, "defaulted": [], "deps": "own"}
        if closed:
            record["closed"] = [{"step": closed, "commit": head, "tree": cls.git(wt, "rev-parse", "HEAD^{tree}"), "review": "rev-1",
                                 "verdict": "APPROVED", "closed_at": iso_off(-100)}]
        if raw is not None:
            path = Path(common) / "tmux-pi" / "lanes" / name
            path.mkdir(parents=True, exist_ok=True)
            (path / "lane.json").write_text(raw)
            return
        with cls.lanes.lane_lock(common):
            cls.lanes.write_lane(common, record)

    def task(self, ident, lane, created, life="working", review_of=None, step="S1", requests=(), raw_requests=(), reply=None, bad_meta=False):
        d = self.tasks / ident
        d.mkdir()
        if bad_meta:  # meta.json cannot be read: the lane tag survives only in dispatch.json (or the lane's lease)
            (d / "meta.json").write_text("{broken")
            (d / "dispatch.json").write_text(json.dumps({"lane": lane, "lane_step": step}))
            return
        meta = {"id": ident, "worker": "claude", "role": "code-reviewer" if review_of else "coder", "created": iso_s(created),
                "workdir": "/nonexistent", "lane": lane, "lane_step": step}
        (d / "meta.json").write_text(json.dumps(meta))
        if review_of:
            (d / "review-of").write_text(review_of + "\n")
        state = {"id": ident, "harness": "claude", "state": "working" if life == "working" else "waiting", "updated": iso_s(created + 1)}
        (d / "state.json").write_text(json.dumps(state))
        if life == "failed":
            (d / "exit").write_text("1")
        elif life == "settled":
            (d / "result.md").write_text((reply + "\nRESULT_STATUS: complete\n") if reply else "RESULT_STATUS: complete\ndone\n")
        for n, kind in enumerate(requests):
            (d / "requests").mkdir(exist_ok=True)
            rid = "req-" + "0" * 25 + "ABCDEFGH"[n]
            (d / "requests" / (rid + ".json")).write_text(json.dumps({"id": rid, "task": ident, "kind": kind, "status": "open",
                                                                      "opened": iso_s(created + 2), "summary": "s"}))
        for n, text in enumerate(raw_requests):
            (d / "requests").mkdir(exist_ok=True)
            (d / "requests" / ("req-" + "0" * 25 + "YZ"[n] + ".json")).write_text(text)

    def footprint(self):
        """Everything status.py must not change: the object listing, every index file, the lane worktrees' files and the session's."""
        out = {}
        for root in (Path(self.common), self.state, *self.base.glob("*-wt-*")):
            for path in sorted(root.rglob("*")):
                rel = str(path)
                if path.is_file() and "/.git/lock" not in rel and not rel.endswith(".lock"):
                    out[rel] = path.read_bytes() if "/objects/" not in rel else path.stat().st_size
                elif path.is_dir() and "/objects" in rel:
                    out[rel] = "dir"
        return out

    def test_next_action_table(self):
        lane = type(self).lane
        for name in ("perm", "cap", "none", "run", "revrun", "final", "rc", "appr", "unusable", "incomplete", "dead", "queued",
                     "twotasks", "chain", "olderrev", "badreq", "closedun", "dirty", "undisp", "unlease", "unnewer", "idonly",
                     "nokind", "capbad", "permbad"):
            lane(self.repo, name, commit=name == "closedun", closed="S1" if name == "closedun" else None)
        lane(self.repo, "closedmerged", closed="S1")
        lane(self.repo, "badlane", raw="{not json")
        foreign = self.make_repo(self.base / "other")
        lane(foreign, "foreign")
        t = self.task
        t("t-perm", "perm", 10, requests=["permission"])
        t("t-cap", "cap", 10, requests=["capability"])
        t("t-run", "run", 10)
        t("t-revrun", "revrun", 10, life="settled")
        t("r-revrun", "revrun", 20, life="working", review_of="t-revrun")
        t("t-final", "final", 10, life="settled")
        t("t-rc", "rc", 10, life="settled")
        t("r-rc", "rc", 20, review_of="t-rc", life="settled", reply="REQUEST_CHANGES\n1. broken. Closes by: code\n" + self.COVERAGE)
        t("t-appr", "appr", 10, life="settled")
        t("r-appr", "appr", 20, review_of="t-appr", life="settled", reply="APPROVED\n" + self.COVERAGE)
        t("t-unusable", "unusable", 10, life="settled")
        t("r-unusable", "unusable", 20, review_of="t-unusable", life="settled",
          reply="REQUEST_CHANGES\n1. note. Closes by: decision\n" + self.COVERAGE)
        t("t-incomplete", "incomplete", 10, life="settled")
        t("r-incomplete", "incomplete", 20, review_of="t-incomplete", life="settled", reply="APPROVED\n")
        t("t-dead", "dead", 10, life="failed")
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue" / "q-1.json").write_text(json.dumps({"lane": "queued", "lane_step": "S1", "role": "coder", "created": iso_s(10)}))
        t("t-two-a", "twotasks", 10, life="settled")    # the older one is final with no review; the newer one is running
        t("t-two-b", "twotasks", 30)
        t("t-chain-1", "chain", 10, life="settled")      # retry chain: the first attempt was rejected, the retry is running
        t("r-chain-1", "chain", 20, review_of="t-chain-1", life="settled", reply="REQUEST_CHANGES\nx Closes by: code\n" + self.COVERAGE)
        t("t-chain-2", "chain", 30)
        t("t-old-1", "olderrev", 10, life="settled")     # the review is of the older task; the newest has none
        t("r-old-1", "olderrev", 20, review_of="t-old-1", life="settled", reply="APPROVED\n" + self.COVERAGE)
        t("t-old-2", "olderrev", 30, life="settled")
        t("t-badreq", "badreq", 10, raw_requests=["{broken"])
        t("t-foreign", "foreign", 10)                    # a task of a lane of another repository
        t("t-dirty", "dirty", 10, life="settled")        # finished, no review; its worktree has uncommitted work (F1)
        (Path(self.lanes.read_lane(self.common, "dirty")["worktree"]) / "f").write_text("changed")
        (Path(self.lanes.read_lane(self.common, "dirty")["worktree"]) / "new-file").write_text("untracked")
        t("t-undisp", "undisp", 10, bad_meta=True)       # unreadable task the dispatch.json places in the step (F2)
        t("t-unlease", "unlease", 10, bad_meta=True)
        (self.tasks / "t-unlease" / "dispatch.json").unlink()   # no tag: only the lane's lease names it
        holder = {"kind": "worker", "access": "write", "task": "t-unlease", "role": "coder", "session": str(self.state),
                  "pid": 1, "start": "x", "pane": None, "taken": iso_s(10)}
        with self.lanes.lane_lock(self.common):
            self.lane_lease._write(self.common, "unlease", [holder])
        t("t-unnew-old", "unnewer", 10, life="settled")  # an older readable task beside a newer unreadable one
        t("t-unnew-new", "unnewer", 30, bad_meta=True)
        os.utime(self.tasks / "t-unnew-new" / "dispatch.json", (T0 + 30, T0 + 30))
        t("t-idonly", "idonly", 10, raw_requests=['{"id": "req-%s"}' % ("0" * 25 + "Y")])   # F3: an id and nothing else
        t("t-nokind", "nokind", 10, raw_requests=[json.dumps({"id": "req-" + "0" * 25 + "Y", "status": "open", "opened": iso_s(12)})])
        t("t-capbad", "capbad", 10, requests=["capability"], raw_requests=["{broken"])   # a valid request beside a corrupt one
        t("t-permbad", "permbad", 10, requests=["permission"], raw_requests=["{broken"])  # a readable permission still decides
        (self.tasks / "no-meta").mkdir()
        status = self.status
        before = self.footprint()
        report = status.build(self.tasks, self.repo, now=self.NOW)
        self.assertEqual(self.footprint(), before)  # F1: read-only means no write anywhere: objects, index, source, session files
        got = {r["lane"]: r for r in report["lanes"]}
        want = {  # lane: (rule, who)
            "perm": (1, "owner"), "cap": (2, "main"), "none": (3, "main"), "run": (4, "worker"), "revrun": (5, "reviewer"),
            "final": (6, "main"), "rc": (7, "main"), "appr": (8, "main"), "closedun": (9, "main"),
            "unusable": (10, "unknown"), "incomplete": (10, "unknown"), "dead": (10, "unknown"), "queued": (10, "unknown"),
            "twotasks": (4, "worker"), "chain": (4, "worker"), "olderrev": (6, "main"), "badreq": (10, "unknown"),
            "closedmerged": (10, "unknown"), "badlane": (10, "unknown"), "dirty": (6, "main"),
            "undisp": (10, "unknown"), "unlease": (10, "unknown"), "unnewer": (10, "unknown"), "idonly": (10, "unknown"),
            "nokind": (10, "unknown"), "capbad": (10, "unknown"), "permbad": (1, "owner")}
        self.assertEqual(sorted(got), sorted(want))     # the foreign lane is left out, every other open lane has a row
        for name, (rule, who) in want.items():
            with self.subTest(lane=name):
                self.assertEqual((got[name]["rule"], got[name]["who"]), (rule, who), got[name])
        self.assertEqual(got["chain"]["task"], "t-chain-2")
        self.assertEqual(got["twotasks"]["task"], "t-two-b")
        self.assertEqual(got["olderrev"]["review"], None)
        self.assertEqual(got["appr"]["review"], {"id": "r-appr", "verdict": "APPROVED"})
        self.assertEqual(got["perm"]["requests"][0]["kind"], "permission")
        self.assertEqual(got["run"]["state"]["state"], "stalled")  # timeline's word: no first prompt in 4990 s
        self.assertEqual((got["run"]["objective"], got["run"]["step"], got["run"]["age"]), ("objective of run", "S1", "2d"))
        self.assertIn("t-cap", got["cap"]["next"])        # F5: the row's own values replace TASK, REQ, LANE and STEP
        self.assertIn("req-" + "0" * 25 + "A", got["cap"]["next"])
        self.assertIn("review t-final", got["final"]["next"])
        self.assertIn("retry t-rc", got["rc"]["next"])
        self.assertIn("lane close appr S1", got["appr"]["next"])
        self.assertIn("lane remove closedun", got["closedun"]["next"])
        self.assertIn("t-unnew-new", got["unnewer"]["next"])
        wheres = " ".join(p["where"] + " " + p["problem"] for p in report["problems"])
        for word in ("no-meta", "req-", "badlane", "t-undisp", "t-unnew-new"):
            self.assertIn(word, wheres)
        order = [r["who"] for r in report["lanes"]]
        self.assertEqual(order, sorted(order, key=lambda w: status.WHO_ORDER[w]))  # owner, then main, then worker/reviewer, unknown last
        text = status.render(report)
        for name in want:
            if name != "badlane":
                self.assertIn("objective of " + name, text)   # F4: text mode shows every row field
        self.assertEqual(len([line for line in text.splitlines() if "  -> " in line]), len(want))
        for secret in ("done", "broken"):
            self.assertNotIn(secret, text)

    def test_cli(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.status.main([str(self.tasks), "--repo", str(self.repo), "--json", "--now", iso_s(5000)]), 0)
        self.assertIn("lanes", json.loads(buf.getvalue()))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.status.main([str(self.tasks), "--now", "yesterday"])
            with self.assertRaises(SystemExit):
                self.status.main([str(self.base / "nope")])


if __name__ == "__main__":
    unittest.main()
