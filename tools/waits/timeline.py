#!/usr/bin/env python3
"""Where work waits: a per-task timeline built after the fact from the records a session already keeps (WT1 arm A, HB6a).

    python3 tools/waits/timeline.py SESSION_TASKS_DIR [--task ID ...] [--json] [--now ISO] [--lanes-dir DIR]

Per task, the ordered intervals {task, actor, state, start, end, wait_reason, source, confidence}; the totals (elapsed,
busy, accumulated: never mixed), the unknown share, and the critical path from the first task's created to the last end with
one suggested focus. The rules (records read, paint order, clock tolerance) are in
docs/plans/det-prep/hub/hb6/hb6a-brief.md and the HB6a report; the D-numbers below name them.

Read-only: it opens files for reading only and writes nothing anywhere. It prints no transcript text and no file content:
from a transcript it takes only line times, roles, tool names and ids. A missing or malformed record makes its piece absent
(its seconds stay `unknown`) and is listed under `problems`; it never raises.
"""
import argparse
import bisect
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import hub_records  # noqa: E402

TOL = 2.0        # D7: clock disagreement between sources tolerated silently, seconds
STALL_S = 60.0   # D3: no first prompt this long after created is a stall (H9)
SHORT_S = 60.0   # text output only: model/tool intervals shorter than this, in a row, print as one "working" line
STATES = ("starting", "stalled", "model", "tool", "test", "interrupted", "waiting:review", "waiting:main", "waiting:permission",
          "waiting:owner-input", "waiting:dependency", "waiting:worker-slot", "waiting:lane-lease", "unknown")
FOCUS = {
    "model": "model generation: smaller steps or a faster route for this kind of task",
    "tool": "tool calls other than recorded tests: look at the slowest tool kinds",
    "test": "test runs: the slowest test files in the test-run records",
    "interrupted": "work killed mid-call: why the task was stopped or reaped",
    "waiting:review": "review rounds: the review held the coder",
    "waiting:main": "Main's turns: replies waiting to be collected, prompts waiting to be sent",
    "waiting:permission": "permission prompts: the allow-list for the tool asked about",
    "waiting:owner-input": "questions to the owner: decide them earlier or in the brief",
    "waiting:dependency": "a queued task waiting on its predecessor",
    "waiting:worker-slot": "worker slots: the active-worker limit",
    "waiting:lane-lease": "the lane writer lease",
    "starting": "worker start-up time",
    "stalled": "a worker that never took its first prompt (H9 start check)",
    "unknown": "unrecorded time (between tasks: Main's turns or queue waits); arm B (queue and Main events) would record it",
}
ACTOR = {"test": "test", "waiting:review": "main", "waiting:main": "main", "waiting:owner-input": "owner",
         "waiting:lane-lease": "lane", "waiting:worker-slot": "main", "waiting:dependency": "main", "waiting:permission": "owner"}
QUEUE_REASONS = ((re.compile(r"^waiting for lane "), "waiting:lane-lease", "lane writer"),
                 (re.compile(r"slots full"), "waiting:worker-slot", "worker slots full"),
                 (re.compile(r"^(waiting for |dependency )"), "waiting:dependency", "predecessor not finished"))
TEST_RUN_NAME = re.compile(r"\d+\.json")
GATE_NAME = re.compile(r"gate(-[0-9]{8}-[0-9]{6}-[0-9]+)?\.json")


# ------------------------------------------------------------------ reading (read-only, never raises)

def parse_time(value):
    """Epoch seconds from an ISO-8601 text (Z or an offset; seconds or fractions) or an epoch number (seconds, or ms when
    it is too large to be seconds). None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if value != value or value <= 0:
            return None
        return value / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    match = re.fullmatch(r"(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)", text)
    if match:  # fromisoformat in 3.9 takes only 3 or 6 fraction digits
        frac = (match.group(2) or "")[1:]
        text = match.group(1) + ("." + (frac + "000000")[:6] if frac else "") + match.group(3)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def iso(t):
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Reader:
    """File access for one report: every read is listed when it fails, nothing is written."""

    def __init__(self):
        self.problems = []

    def problem(self, where, what):
        self.problems.append({"where": str(where), "problem": what})

    def json(self, path, label, need_dict=True):
        path = Path(path)
        try:
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, ValueError, UnicodeDecodeError):
            self.problem(path, "malformed " + label)
            return None
        if need_dict and not isinstance(value, dict):
            self.problem(path, "malformed " + label + " (not an object)")
            return None
        return value

    def mtime(self, path):
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    def text(self, path, limit=4096):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return None

    def jsonl(self, path, label):
        """The JSON objects of a JSONL file, one per line; bad lines are counted and listed once."""
        out, bad = [], 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(value, dict):
                        out.append(value)
                    else:
                        bad += 1
        except OSError:
            self.problem(path, "unreadable " + label)
            return None
        if bad:
            self.problem(path, "{} malformed line(s) in {}".format(bad, label))
        return out


# ------------------------------------------------------------------ painting

class Canvas:
    """Non-overlapping painted intervals over one span; a later paint overrides earlier ones on the seconds it covers."""

    def __init__(self, start, end):
        self.start, self.end = start, end
        self.items = []  # sorted (start, end, payload)

    def paint(self, start, end, payload):
        start, end = max(start, self.start), min(end, self.end)
        if end <= start:
            return
        starts = [i[0] for i in self.items]
        lo = max(0, bisect.bisect_left(starts, start) - 1)
        hi = bisect.bisect_right(starts, end)
        kept = []
        for a, b, p in self.items[lo:hi]:
            if b <= start or a >= end:
                kept.append((a, b, p))
                continue
            if a < start:
                kept.append((a, start, p))
            if b > end:
                kept.append((end, b, p))
        kept.append((start, end, payload))
        kept.sort(key=lambda i: i[0])
        self.items[lo:hi] = kept

    def merged(self):
        out = []
        for a, b, p in self.items:
            if out and out[-1][2] == p and abs(out[-1][1] - a) < 1e-9:
                out[-1] = (out[-1][0], b, p)
            else:
                out.append((a, b, p))
        return out


def payload(state, reason, source, confidence, actor=None):
    return (state, actor or ACTOR.get(state, ""), reason, source, confidence)


# ------------------------------------------------------------------ transcripts (times, roles, tool names and ids only)

def transcript_points(lines, harness):
    """[(t, kind, data)] from transcript lines: kind prompt | result (data: ids) | out (data: {mid, calls: [(id, name)]})."""
    points, seen = [], set()
    for index, line in enumerate(lines):
        if line.get("isSidechain") is True:
            continue
        t = parse_time(line.get("timestamp"))
        message = line.get("message") if isinstance(line.get("message"), dict) else {}
        content = message.get("content")
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        if t is None:
            continue
        kind = line.get("type")
        role = message.get("role")
        if harness == "pi" and kind == "message":
            kind = {"user": "user", "assistant": "assistant", "toolResult": "toolResult"}.get(role)
        if kind == "assistant":
            calls = []
            for b in blocks:
                if b.get("type") in ("tool_use", "toolCall") and isinstance(b.get("id"), str) and b["id"] not in seen:
                    seen.add(b["id"])
                    calls.append((b["id"], b.get("name") if isinstance(b.get("name"), str) else ""))
            mid = message.get("id") if harness != "pi" else line.get("id")
            points.append((t, "out", {"mid": mid if mid is not None else ("line", index), "calls": calls}))
        elif kind == "toolResult":
            ref = message.get("toolCallId")
            points.append((t, "result", [ref] if isinstance(ref, str) else []))
        elif kind == "user":
            ids = [b.get("tool_use_id") for b in blocks if b.get("type") == "tool_result"]
            if ids:
                points.append((t, "result", [i for i in ids if isinstance(i, str)]))
            elif isinstance(content, str) or any(b.get("type") == "text" for b in blocks):
                points.append((t, "prompt", None))
    points.sort(key=lambda p: p[0])
    return points


def transcript_segments(points):
    """(segments, tail, first_prompt). segments: [(start, end, state, name, call)] with state model | tool | idle; idle
    carries name "between-turns" when a prompt ends it. tail: (since, state, name, call) after the last point, or None.
    call is the id of the earliest open call of a tool segment; calls never resulted are returned in `orphans`."""
    open_calls = {}  # id -> (start, name)
    pending, segments, closed = False, [], set()
    first_prompt = next((p[0] for p in points if p[1] == "prompt"), None)
    abandoned = {}  # id -> time a prompt abandoned it

    def state_after(i):
        if open_calls:
            cid = min(open_calls, key=lambda k: open_calls[k][0])
            return "tool", open_calls[cid][1], cid
        if pending:
            return "model", "", None
        nxt = points[i + 1] if i + 1 < len(points) else None
        if points[i][1] == "out" and nxt is not None and nxt[1] == "out" and nxt[2]["mid"] == points[i][2]["mid"]:
            return "model", "", None
        return "idle", "", None

    for i, (t, kind, data) in enumerate(points):
        if kind == "prompt":
            for cid in list(open_calls):  # the harness abandoned these calls: a new prompt came before their results
                abandoned[cid] = t
                del open_calls[cid]
            pending = True
        elif kind == "result":
            for cid in data:
                if cid in open_calls:
                    del open_calls[cid]
                    closed.add(cid)
            pending = True
        else:
            pending = False
            for cid, name in data["calls"]:
                open_calls[cid] = (t, name)
        state, name, cid = state_after(i)
        if i + 1 < len(points):
            end = points[i + 1][0]
            if state == "idle" and points[i + 1][1] == "prompt":
                name = "between-turns"
            if end > t:
                segments.append((t, end, state, name, cid))
    tail = None
    if points:
        state, name, cid = state_after(len(points) - 1)
        tail = (points[-1][0], state, name, cid)
    return segments, tail, first_prompt, set(open_calls), abandoned


# ------------------------------------------------------------------ one task

def lanes_dir_for(reader, workdir, cache):
    """<git common dir>/tmux-pi/lanes for a worktree, read from its .git file (no git command)."""
    if not isinstance(workdir, str) or not workdir:
        return None
    if workdir in cache:
        return cache[workdir]
    found = None
    dot = Path(workdir) / ".git"
    try:
        if dot.is_dir():
            found = dot / "tmux-pi" / "lanes"
        elif dot.is_file():
            text = dot.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text[7:].strip())
                if not gitdir.is_absolute():
                    gitdir = (Path(workdir) / gitdir)
                common = gitdir
                commondir = gitdir / "commondir"
                if commondir.is_file():
                    rel = commondir.read_text(encoding="utf-8", errors="replace").strip()
                    common = Path(rel) if Path(rel).is_absolute() else gitdir / rel
                found = Path(os.path.normpath(str(common))) / "tmux-pi" / "lanes"
    except OSError:
        found = None
    cache[workdir] = found
    return found


def transcript_file(reader, task_dir):
    """The worker transcript: the path in transcript-path when it names a regular *.jsonl file, else transcript.jsonl."""
    pointer = task_dir / "transcript-path"
    if pointer.exists():
        text = (reader.text(pointer) or "").strip()
        path = Path(text) if text else None
        if path is None or not path.is_absolute() or path.suffix != ".jsonl" or path.name.startswith(".env"):
            reader.problem(pointer, "transcript-path does not name a *.jsonl file; not read")
        else:
            try:
                if stat.S_ISREG(os.stat(path).st_mode):
                    return path
                reader.problem(pointer, "transcript-path names something that is not a regular file")
            except OSError:
                reader.problem(pointer, "transcript-path names a missing file")
    local = task_dir / "transcript.jsonl"
    return local if local.is_file() else None


def load_task(reader, task_dir):
    """The raw facts of one task directory (times only), or None when it has no readable meta.json created."""
    meta = reader.json(task_dir / "meta.json", "meta.json")
    created = parse_time(meta.get("created")) if meta else None
    if meta is not None and created is None:
        reader.problem(task_dir / "meta.json", "meta.json has no readable created")
    if created is None:
        return None
    state = reader.json(task_dir / "state.json", "state.json") or {}
    harness = meta.get("worker") if meta.get("worker") in ("pi", "claude") else state.get("harness")
    review_of = (reader.text(task_dir / "review-of", 200) or "").strip() if (task_dir / "review-of").exists() else ""
    if review_of and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", review_of):
        reader.problem(task_dir / "review-of", "malformed review-of")
        review_of = ""
    marks = {}
    for name, key in (("collected.json", "collected"), ("stopped.json", "stopped"), ("reaped.json", "reaped")):
        value = reader.json(task_dir / name, name)
        if value is not None:
            t = parse_time(value.get(key))
            if t is None:
                reader.problem(task_dir / name, "{} has no readable {}".format(name, key))
            else:
                marks[key] = t
    if (task_dir / "exit").exists():
        marks["exit"] = reader.mtime(task_dir / "exit")
    if (task_dir / "result.md").exists():
        marks["result"] = reader.mtime(task_dir / "result.md")
    return {"id": task_dir.name, "dir": task_dir, "meta": meta, "state": state, "created": created, "harness": harness or "",
            "review_of": review_of, "marks": marks}


def span_end(task, now):
    """D1: (end, source, running)."""
    m = task["marks"]
    main = [(m[k], k + ".json") for k in ("collected", "stopped") if k in m]
    if main:
        t, src = min(main)
        return t, src, False
    if "reaped" in m:
        return m["reaped"], "reaped.json", False
    if m.get("exit") is not None:
        return m["exit"], "exit (mtime)", False
    return now, "now (running)", True


def clock_check(reader, task, where, t, end):
    if t is None:
        return
    if t < task["created"] - TOL:
        reader.problem(where, "before-created by {:.1f} s".format(task["created"] - t))
    elif t > end + TOL:
        reader.problem(where, "after-end by {:.1f} s".format(t - end))


def request_state(kind, by):
    if kind == "permission":
        return "waiting:permission"
    if by == "owner":
        return "waiting:owner-input"
    return "waiting:main"


def build_task(reader, task, tasks_by_review, now, lanes_cache, lanes_override):
    tdir = task["dir"]
    start = task["created"]
    end, end_source, running = span_end(task, now)
    if end < start:
        reader.problem(tdir, "end ({}) before created; span set to zero".format(end_source))
        end = start
    canvas = Canvas(start, end)
    instants = []
    for key, t in task["marks"].items():
        clock_check(reader, task, tdir / key, t, end)
    # transcript
    tfile = transcript_file(reader, tdir)
    lines = reader.jsonl(tfile, "transcript") if tfile else None
    points = transcript_points(lines, task["harness"]) if lines else []
    if lines is not None and not points:
        reader.problem(tfile, "transcript has no timed lines")
    segments, tail, t_first, orphans, abandoned = transcript_segments(points) if points else ([], None, None, set(), {})
    for p in points[:1] + points[-1:]:
        clock_check(reader, task, tfile, p[0], end)
    # first prompt (D3)
    log = task["state"].get("promptLog")
    prompts = []
    if isinstance(log, list):
        for item in log:
            if isinstance(item, dict) and parse_time(item.get("at")) is not None:
                prompts.append((item.get("n"), parse_time(item["at"])))
    elif log is not None:
        reader.problem(tdir / "state.json", "promptLog is not a list")
    first = [t for n, t in prompts if n == 1]
    candidates = [t for t in (first[0] if first else None, t_first) if t is not None]
    first_prompt = min(candidates) if candidates else (points[0][0] if points else None)
    for n, t in prompts:
        instants.append({"at": iso(t), "what": "prompt {}".format(n), "source": "state.json promptLog", "confidence": "exact"})
    # layer 1 + 2
    canvas.paint(start, end, payload("unknown", "no record explains this time", "-", "inferred", actor=""))
    if first_prompt is not None:
        canvas.paint(start, first_prompt, payload("starting", "launch to first prompt", "meta.json created, first prompt",
                                                   "exact", actor=_actor(task)))
    elif task["marks"].get("result") is not None:
        pass  # D3: a reply exists, so a prompt was taken that no record shows: the seconds before it stay unknown
    elif end - start > STALL_S:
        canvas.paint(start, end, payload("stalled", "no first prompt {:.0f} s after created".format(end - start),
                                         "meta.json created; no prompt in state.json or transcript", "inferred", actor=_actor(task)))
    else:
        canvas.paint(start, end, payload("starting", "no first prompt yet", "meta.json created", "exact", actor=_actor(task)))
    # layer 3: waits on Main between turns and after the reply
    for a, b, state, name, cid in segments:
        if state == "idle" and name == "between-turns":
            canvas.paint(a, b, payload("waiting:main", "turn ended; next prompt from Main", "transcript", "inferred"))
    reply = task["marks"].get("result")
    reply_src = "result.md (mtime)"
    if reply is None and tail is not None and tail[1] == "idle" and not running:
        reply, reply_src = tail[0], "transcript last line"
    if reply is not None and reply >= start:
        canvas.paint(reply, end, payload("waiting:main", "reply waits for Main ({})".format(end_source), reply_src, "derived"))
    elif reply is not None:
        reader.problem(tdir / "result.md", "result.md mtime before created; not used")
    # layer 4: review holds the coder
    for review in tasks_by_review.get(task["id"], []):
        r_end, r_src, r_running = span_end(review, now)
        canvas.paint(review["created"], r_end, payload("waiting:review", "review {} running".format(review["id"]),
                                                       review["id"] + " meta.json created, " + r_src, "exact"))
    # layer 5/6 from events.jsonl and requests
    events_path = tdir / "events.jsonl"
    events = reader.jsonl(events_path, "events.jsonl") if events_path.exists() else []
    events = events or []
    activity_starts = sorted(a for a, b, s, n, c in segments if s in ("model", "tool"))
    for ev in events:
        t = parse_time(ev.get("at"))
        kind = ev.get("kind")
        if t is None or not isinstance(kind, str):
            continue
        if kind == "pause.confirmed":
            nxt = next((a for a in activity_starts if a > t), end)
            canvas.paint(t, nxt, payload("waiting:main", "paused by Main", "events.jsonl pause.confirmed", "exact"))
        elif kind.startswith(("correction.", "pause.", "hold.")):
            instants.append({"at": iso(t), "what": kind, "source": "events.jsonl", "confidence": ev.get("confidence") or "exact"})
    req_ids, cap_opened = set(), []
    folder = tdir / "requests"
    if folder.is_dir() and not folder.is_symlink():
        for path in sorted(folder.glob("req-*.json")):
            record = reader.json(path, "request record")
            if record is None:
                continue
            opened = parse_time(record.get("opened"))
            if not hub_records.REQUEST_ID.fullmatch(str(record.get("id", ""))) or opened is None:
                reader.problem(path, "malformed request record (id or opened)")
                continue
            req_ids.add(record["id"])
            closed = parse_time(record.get("resolved")) if record.get("status") != "open" else None
            by = (record.get("resolution") or {}).get("by") if isinstance(record.get("resolution"), dict) else None
            kind = record.get("kind")
            if kind == "capability":
                cap_opened.append(opened)
            clock_check(reader, task, path, opened, end)
            canvas.paint(opened, closed if closed is not None else end,
                         payload(request_state(kind, by), "request {} {}".format(kind, "open" if closed is None else "resolved"),
                                 "requests/" + path.name, "exact"))
    opened_ev = {}
    for ev in events:
        ref = ev.get("ref")
        if ev.get("kind") in ("request.opened", "request.resolved") and isinstance(ref, str) and ref not in req_ids:
            detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
            t = parse_time(ev.get("at"))
            if t is None:
                continue
            if ev["kind"] == "request.opened":
                opened_ev[ref] = (t, detail.get("kind"))
            elif ref in opened_ev:
                o, kind = opened_ev.pop(ref)
                canvas.paint(o, t, payload(request_state(kind, detail.get("by")), "request {} resolved".format(kind),
                                           "events.jsonl request.*", "exact"))
                if kind == "capability":
                    cap_opened.append(o)
    for ref, (o, kind) in opened_ev.items():
        canvas.paint(o, end, payload(request_state(kind, None), "request {} open".format(kind), "events.jsonl request.opened",
                                     "exact"))
        if kind == "capability":
            cap_opened.append(o)
    cap = reader.json(tdir / "capability-request.json", "capability-request.json")
    if cap is not None:
        asked = parse_time(cap.get("requested"))
        answered = parse_time(cap.get("answered"))
        if asked is None:
            reader.problem(tdir / "capability-request.json", "capability-request.json has no readable requested")
        elif not any(abs(asked - o) <= TOL for o in cap_opened):
            canvas.paint(asked, answered if answered is not None else end,
                         payload("waiting:main", "capability request {}".format("answered" if answered else "pending"),
                                 "capability-request.json", "exact"))
    # layer 7: transcript activity
    for a, b, state, name, cid in segments:
        if state in ("model", "tool"):
            if state == "tool" and (cid in orphans or cid in abandoned):
                state = "interrupted"
            reason = hub_records.normalize_tool(name)["tool"] if state in ("tool", "interrupted") else ""
            canvas.paint(a, b, payload(state, reason, "transcript", "derived", actor=_actor(task)))
    if tail is not None and tail[1] in ("model", "tool"):
        state = tail[1] if running else "interrupted"
        reason = hub_records.normalize_tool(tail[2])["tool"] if tail[1] == "tool" else "model"
        if not running:
            reason = (reason + ": no result before " + end_source)
        canvas.paint(tail[0], end, payload(state, reason, "transcript", "derived", actor=_actor(task)))
    # layer 8: tests (D5)
    shell = [(a, b) for a, b, s, n, c in segments if s == "tool" and hub_records.normalize_tool(n)["tool"] == "shell"]
    if tail is not None and tail[1] == "tool" and running and hub_records.normalize_tool(tail[2])["tool"] == "shell":
        shell.append((tail[0], end))
    tests = []
    runs = tdir / "test-runs"
    if runs.is_dir():
        for path in sorted(runs.iterdir(), key=lambda p: (len(p.name), p.name)):
            if not TEST_RUN_NAME.fullmatch(path.name):
                continue
            record = reader.json(path, "test-run record")
            if record is None:
                continue
            seconds = record.get("seconds")
            mtime = reader.mtime(path)
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0 or mtime is None:
                reader.problem(path, "malformed test-run record (seconds)")
                continue
            exit_code = record.get("exit")
            tests.append((mtime - seconds, mtime, "test run {} (exit {})".format(path.stem, exit_code),
                          "test-runs/" + path.name + " seconds + mtime", "derived", path))
    for path in sorted(tdir.glob("gate*.json")):
        if not GATE_NAME.fullmatch(path.name):
            continue
        record = reader.json(path, "gate record")
        if record is None:
            continue
        a, b = parse_time(record.get("started")), parse_time(record.get("finished"))
        if a is None or b is None or b < a:
            reader.problem(path, "malformed gate record (started/finished)")
            continue
        tests.append((a, b, "pre-commit gate (exit {})".format(record.get("exit")), path.name + " started/finished", "exact", path))
    for a, b, reason, source, conf, path in tests:
        if b < start - TOL:
            reader.problem(path, "record ends before created; dropped")
            continue
        clock_check(reader, task, path, a, end)
        clock_check(reader, task, path, b, end)
        bounds = shell if points else [(first_prompt if first_prompt is not None else start, reply if reply is not None else end)]
        for lo, hi in bounds:
            canvas.paint(max(a, lo), min(b, hi), payload("test", reason, source, conf))
    # lanes (instants only)
    meta = task["meta"]
    lane = meta.get("lane")
    if isinstance(lane, str) and lane and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", lane):
        base = Path(lanes_override) if lanes_override else lanes_dir_for(reader, meta.get("workdir"), lanes_cache)
        if base is not None and (base / lane).is_dir():
            record = reader.json(base / lane / "lane.json", "lane.json")
            closed = record.get("closed") if record else None
            for item in closed if isinstance(closed, list) else []:
                if isinstance(item, dict) and item.get("step") == meta.get("lane_step") and parse_time(item.get("closed_at")):
                    instants.append({"at": iso(parse_time(item["closed_at"])), "what": "lane step {} closed".format(item["step"]),
                                     "source": "lane.json closed_at", "confidence": "exact"})
            lease = reader.json(base / lane / "lease.json", "lease.json")
            holders = lease.get("holders") if lease else None
            for h in holders if isinstance(holders, list) else []:
                if isinstance(h, dict) and h.get("task") == task["id"] and parse_time(h.get("taken")):
                    instants.append({"at": iso(parse_time(h["taken"])), "what": "lane lease taken", "source": "lease.json taken",
                                     "confidence": "exact"})
    state = task["state"]
    if state.get("updated") is not None:
        instants.append({"at": iso(parse_time(state.get("updated"))), "what": "last state {}".format(state.get("state")),
                         "source": "state.json updated", "confidence": "exact"})
    intervals = []
    for a, b, p in canvas.merged():
        intervals.append({"task": task["id"], "actor": p[1], "state": p[0], "start": a, "end": b, "wait_reason": p[2],
                          "source": p[3], "confidence": p[4]})
    if intervals and running:
        intervals[-1]["open"] = True
    instants.sort(key=lambda i: i["at"] or "")
    return {"task": task["id"], "harness": task["harness"], "role": _actor(task), "start": start, "end": end,
            "end_source": end_source, "running": running, "intervals": intervals, "instants": instants,
            "transcript": "read" if points else ("missing" if tfile is None else "unusable")}


def _actor(task):
    return "reviewer" if task["review_of"] else "worker"


def queued_items(reader, tasks_dir, now, known):
    """D-R22: a task still in the session queue (no task directory yet)."""
    out = []
    folder = Path(tasks_dir).parent / "queue"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.json")):
        item = reader.json(path, "queue record")
        if item is None:
            continue
        ident, created = item.get("id"), parse_time(item.get("created"))
        if not isinstance(ident, str) or created is None:
            reader.problem(path, "malformed queue record (id or created)")
            continue
        if ident in known:
            continue
        reason = item.get("queue_reason")
        state, why = "unknown", "queued; no reason recorded yet"
        if isinstance(reason, str) and reason:
            state, why = "waiting:main", "queued: held by a gate"
            for pattern, s, w in QUEUE_REASONS:
                if pattern.search(reason):
                    state, why = s, "queued: " + w
                    break
        if now < created:
            continue
        out.append({"task": ident, "harness": item.get("worker") or "", "role": "queued", "start": created, "end": now,
                    "end_source": "now (queued)", "running": True, "instants": [], "transcript": "none",
                    "intervals": [{"task": ident, "actor": ACTOR.get(state, ""), "state": state, "start": created, "end": now,
                                   "wait_reason": why, "source": "queue/" + path.name + " created, queue_reason",
                                   "confidence": "derived", "open": True}]})
    return out


# ------------------------------------------------------------------ totals and the critical path

def union_length(spans):
    total, cur = 0.0, None
    for a, b in sorted(spans):
        if cur is None or a > cur[1]:
            if cur:
                total += cur[1] - cur[0]
            cur = [a, b]
        else:
            cur[1] = max(cur[1], b)
    if cur:
        total += cur[1] - cur[0]
    return total


def by_state(intervals):
    out = {}
    for i in intervals:
        out[i["state"]] = out.get(i["state"], 0.0) + (i["end"] - i["start"])
    return out


def critical_path(rows):
    """D9: delayed completion runs from the first created to the last collected: walk back from the last-collected task
    (the last-ending one when nothing was collected) through the latest-ending predecessor; gaps are `unknown`."""
    if not rows:
        return {"tasks": [], "intervals": [], "by_state": {}, "interval": None, "focus": None}
    done = [r for r in rows if r["end_source"] == "collected.json"] or rows
    cur = max(done, key=lambda r: (r["end"], r["start"]))
    chain, used = [cur], {cur["task"]}
    while True:
        cands = [r for r in rows if r["task"] not in used and r["end"] <= cur["start"] + TOL]
        if not cands:
            break
        cur = max(cands, key=lambda r: (r["end"], r["start"]))
        chain.append(cur)
        used.add(cur["task"])
    chain.reverse()
    path, cursor = [], None
    for row in chain:
        if cursor is not None and row["start"] > cursor:
            path.append({"task": "-", "actor": "", "state": "unknown", "start": cursor, "end": row["start"],
                         "wait_reason": "between tasks: no record (Main's turn or a queue wait)", "source": "-",
                         "confidence": "inferred"})
        for i in row["intervals"]:
            a = max(i["start"], cursor) if cursor is not None else i["start"]
            if i["end"] > a:
                path.append(dict(i, start=a))
        cursor = max(cursor or row["end"], row["end"])
    totals = by_state(path)
    longest = max(path, key=lambda i: i["end"] - i["start"]) if path else None
    top = max(totals, key=totals.get) if totals else None
    return {"tasks": [r["task"] for r in chain], "intervals": path, "by_state": totals, "interval": longest,
            "focus": {"state": top, "seconds": totals.get(top), "suggest": FOCUS.get(top, "")} if top else None}


def build(tasks_dir, task_ids=None, now=None, lanes_dir=None):
    """The whole report as a dict (times as epoch seconds). Never raises on bad records."""
    reader = Reader()
    now = time.time() if now is None else now
    tasks_dir = Path(tasks_dir)
    try:
        dirs = sorted(p for p in tasks_dir.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        reader.problem(tasks_dir, "tasks directory unreadable")
        dirs = []
    tasks, by_review = {}, {}
    for d in dirs:
        task = load_task(reader, d)
        if task is None:
            continue
        tasks[task["id"]] = task
        if task["review_of"]:
            by_review.setdefault(task["review_of"], []).append(task)
    wanted = [t for t in task_ids if t in tasks] if task_ids else sorted(tasks, key=lambda k: (tasks[k]["created"], k))
    for t in task_ids or []:
        if t not in tasks:
            reader.problem(tasks_dir / t, "no task with a readable meta.json created")
    lanes_cache, rows = {}, []
    for ident in wanted:
        rows.append(build_task(reader, tasks[ident], by_review, now, lanes_cache, lanes_dir))
    if not task_ids:
        rows += queued_items(reader, tasks_dir, now, set(tasks))
    rows.sort(key=lambda r: (r["start"], r["task"]))
    for row in rows:
        row["by_state"] = by_state(row["intervals"])
        span = row["end"] - row["start"]
        row["unknown_share"] = (row["by_state"].get("unknown", 0.0) / span) if span > 0 else 0.0
    accumulated = sum(r["end"] - r["start"] for r in rows)
    acc_states = {}
    for r in rows:
        for k, v in r["by_state"].items():
            acc_states[k] = acc_states.get(k, 0.0) + v
    window = (min(r["start"] for r in rows), max(r["end"] for r in rows)) if rows else (None, None)
    path = critical_path(rows)
    path_span = sum(i["end"] - i["start"] for i in path["intervals"])
    return {
        "now": now, "tasks": rows, "problems": reader.problems,
        "totals": {"window_start": window[0], "window_end": window[1],
                   "elapsed": (window[1] - window[0]) if rows else 0.0,
                   "busy": union_length([(r["start"], r["end"]) for r in rows]),
                   "accumulated": accumulated, "accumulated_by_state": acc_states,
                   "unknown_share": (acc_states.get("unknown", 0.0) / accumulated) if accumulated else 0.0,
                   "path_unknown_share": (path["by_state"].get("unknown", 0.0) / path_span) if path_span else 0.0},
        "critical_path": path,
    }


# ------------------------------------------------------------------ output

def _jsonable(report):
    def conv(value, key=""):
        if isinstance(value, dict):
            return {k: conv(v, k) for k, v in value.items() if k != "dir"}
        if isinstance(value, list):
            return [conv(v, key) for v in value]
        if isinstance(value, float) and key in ("start", "end", "now", "window_start", "window_end"):
            return iso(value)
        if isinstance(value, float):
            return round(value, 3)
        return value
    return conv(report)


def _fmt_s(seconds):
    seconds = int(round(seconds or 0))
    return "{}m{:02d}s".format(seconds // 60, seconds % 60) if seconds >= 60 else "{}s".format(seconds)


def render(report):
    out = []
    for row in report["tasks"]:
        out.append("{}  {} {}  {} -> {} ({}){}  unknown {:.0%}  transcript {}".format(
            row["task"], row["harness"], row["role"], iso(row["start"]), iso(row["end"]), row["end_source"],
            "  RUNNING" if row["running"] else "", row["unknown_share"], row["transcript"]))
        run = []  # short model/tool intervals in a row are shown as one "working" line (--json has every interval)

        def flush():
            if run:
                parts = by_state(run)
                out.append("  {:>8}  {:<20} {:<8} {} {}  {}".format(
                    _fmt_s(run[-1]["end"] - run[0]["start"]), "working", run[0]["actor"], iso(run[0]["start"])[11:19],
                    iso(run[-1]["end"])[11:19], ", ".join("{} {}".format(k, _fmt_s(v)) for k, v in sorted(parts.items()))))
                del run[:]
        for i in row["intervals"]:
            length = i["end"] - i["start"]
            if i["state"] in ("model", "tool") and length < SHORT_S and not i.get("open"):
                run.append(i)
                continue
            flush()
            if length < 0.5 and not i.get("open"):
                continue
            out.append("  {:>8}  {:<20} {:<8} {} {}  {} [{}; {}]{}".format(
                _fmt_s(length), i["state"], i["actor"], iso(i["start"])[11:19], iso(i["end"])[11:19],
                i["wait_reason"], i["source"], i["confidence"], "  (open, now)" if i.get("open") else ""))
        flush()
    t = report["totals"]
    out.append("")
    out.append("elapsed {} (window, overlap once)  busy {}  accumulated {} (sum over workers)  unknown {:.0%} of accumulated, "
               "{:.0%} of the critical path".format(_fmt_s(t["elapsed"]), _fmt_s(t["busy"]), _fmt_s(t["accumulated"]),
                                                     t["unknown_share"], t["path_unknown_share"]))
    out.append("accumulated by state: " + ", ".join("{} {}".format(k, _fmt_s(v)) for k, v in
                                                    sorted(t["accumulated_by_state"].items(), key=lambda kv: -kv[1])))
    path = report["critical_path"]
    if path["intervals"]:
        out.append("critical path: " + " -> ".join(path["tasks"]))
        out.append("  by state: " + ", ".join("{} {}".format(k, _fmt_s(v)) for k, v in
                                               sorted(path["by_state"].items(), key=lambda kv: -kv[1])))
        i = path["interval"]
        out.append("  longest interval: {} {} {} -> {} ({}) {}".format(i["task"], i["state"], iso(i["start"]), iso(i["end"]),
                                                                       _fmt_s(i["end"] - i["start"]), i["wait_reason"]))
        f = path["focus"]
        out.append("  suggested focus: {} ({}): {}".format(f["state"], _fmt_s(f["seconds"]), f["suggest"]))
    if report["problems"]:
        out.append("problems ({}):".format(len(report["problems"])))
        for p in report["problems"]:
            out.append("  {}: {}".format(p["where"], p["problem"]))
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="timeline.py", description="Where work waits, from a session's existing records.")
    parser.add_argument("tasks_dir")
    parser.add_argument("--task", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--now", help="the report time (ISO-8601); default the clock")
    parser.add_argument("--lanes-dir", help="the lanes directory; default <git common dir>/tmux-pi/lanes of each task's workdir")
    args = parser.parse_args(argv)
    if not Path(args.tasks_dir).is_dir():
        parser.error("not a directory: " + args.tasks_dir)
    now = parse_time(args.now) if args.now else None
    if args.now and now is None:
        parser.error("--now is not an ISO-8601 time: " + args.now)
    report = build(args.tasks_dir, args.task or None, now, args.lanes_dir)
    print(json.dumps(_jsonable(report), indent=1) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
