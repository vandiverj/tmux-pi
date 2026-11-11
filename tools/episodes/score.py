#!/usr/bin/env python3
"""Score an episode run from its records (HB8a). Python 3.9, standard library only. Never starts a model.

    score.py RUN_DIR [--arm DIR] [--quiet]     write RUN_DIR/run/score.json and score.md, print the table
    score.py compare A/*.json B/*.json         per-measure counts per arm (an arm = the files' parent directory)

Reads only: RUN_DIR/run/{run.json, episode.json, episode-log.jsonl, final-check.json}, the session's task records
(TP_STATE_DIR/tasks/<id>/: meta, state, packet.md, task.md, result.md, messages.jsonl, test-runs/, events.jsonl, requests/,
the transcript named by transcript-path), the Main transcripts listed in run.json, lane records in the clone's .git, and
`git diff` of the clone against the run's start commit.

Every measure is {"value": ..., ...}. value None means the records needed are missing ("unknown", with the reason in
`unknown`) and is never counted as 0; "n/a" means the episode does not exercise the measure. Semantic success is always
needs_human: the scorer lists the evidence a person reads to judge it.
"""
import argparse
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import episode_lib as el  # noqa: E402

SCORE_SCHEMA = "tp-episode-score:v1"
CHECKS = el.CHECK_KINDS               # evaluate_check handles exactly these
CHECK_MEASURES = el.CHECK_MEASURES
COMPLETE_RE = re.compile(r"(?i)\b(done|completed?|finished|all set|ready to merge|shipped|tests? (?:all )?pass(?:ed|es|ing)?)\b")
PAUSED_RE = re.compile(r"(?i)\b(paused|on hold|stopped (it|the \w+|them))\b")
BUSY_STATES = ("starting", "working", "tool")
NEGATION_RE = re.compile(r"(?i)\b(not|isn't|aren't|wasn't|no|never|incomplete|yet|unless|once|when|if|will|until|before)\b|\?$")
NA = "n/a"
SKEW = timedelta(seconds=2)       # records written by different processes: allow this much clock/ordering slack


def _unknown(reason, **extra):
    return dict({"value": None, "unknown": reason}, **extra)


class Run:
    """Everything the scorer reads, loaded once."""

    def __init__(self, run_dir):
        self.dir = Path(run_dir).resolve()
        rec = self.dir / "run"
        self.record = el.read_json(rec / "run.json") or {}
        self.episode = el.read_json(rec / "episode.json") or el.read_json(self.record.get("episode_file") or "") or {}
        self.log = el.read_log(self.dir)
        seen_before = {}
        for entry in self.log:
            entry["_at"] = el.parse_time(entry.get("at"))
            if entry.get("kind") == "observe":
                # The runner polls; the worker's own stamp (whole seconds, never later than the change) dates the change
                # when it is close to the observation, so Main reacting between change and poll is not read as waiting.
                # Never earlier than the poll that still saw the previous state.
                own = el.parse_time(entry.get("updated"))
                at = entry["_at"]
                t = own if own and at and timedelta(0) <= at - own <= timedelta(seconds=5) else at
                prev = seen_before.get(entry.get("task"))
                entry["_t"] = max(t, prev) if t and prev else t
                if at:
                    seen_before[entry.get("task")] = at
        self.final = el.read_json(rec / "final-check.json")
        self.clone = Path(self.record["clone"]) if self.record.get("clone") else None
        self.state_dir = Path(self.record["state_dir"]) if self.record.get("state_dir") else None
        self.tasks = []
        for snap in el.tasks(self.state_dir):
            task_dir = Path(snap["dir"])
            path = el.transcript_path(task_dir)
            snap["packet"] = _read(task_dir / "packet.md") or _read(task_dir / "task.md")
            snap["messages"] = el.read_jsonl(task_dir / "messages.jsonl") or []
            snap["test_runs"] = []
            for p in sorted((task_dir / "test-runs").glob("*.json"), key=lambda p: _num(p.stem)):
                r = el.read_json(p)
                if isinstance(r, dict):
                    # tp-test-run:v1 has no time field; it is written when the run ends: mtime minus seconds dates the start.
                    r["_at"] = datetime.fromtimestamp(p.stat().st_mtime - float(r.get("seconds") or 0), timezone.utc)
                    snap["test_runs"].append(r)
            snap["events"] = el.read_jsonl(task_dir / "events.jsonl")
            snap["requests"] = el.requests(task_dir)
            snap["transcript"] = el.transcript_entries(path) if path else None
            snap["created_at"] = el.parse_time(snap["created"])
            self.tasks.append(snap)
        # meta.created has whole seconds; the runner's first sighting of the task is a later bound with sub-second time.
        for snap in self.tasks:
            first = next((e["_at"] for e in self.log if e.get("kind") == "observe" and e.get("task") == snap["id"] and e["_at"]), None)
            if first and (snap["created_at"] is None or first >= snap["created_at"]):
                snap["created_at"] = first
        self.main = None
        paths = self.record.get("main_transcripts") or []
        if paths:
            entries = []
            for path in paths:
                got = el.transcript_entries(path)
                if got is not None:
                    entries.extend(got)
            self.main = entries if entries or any(Path(p).exists() for p in paths) else None
        self.session_events = el.read_jsonl(self.state_dir / "events.jsonl") if self.state_dir else None
        self.observations = [e for e in self.log if e.get("kind") == "observe" and e["_at"]]
        end = [e["_at"] for e in self.log if e.get("kind") in ("teardown", "run.end") and e["_at"]]
        self.end = end[0] if end else (self.log[-1]["_at"] if self.log else None)

    # ---- lookups --------------------------------------------------------------------------------------------------------
    def step_time(self, step_id):
        for entry in self.log:
            if entry.get("step") == step_id and entry["_at"] and entry.get("kind") != "observe":
                return entry["_at"]
        return None

    def state_at(self, task_id, when, stamped=False):
        """The task's last observed state at `when`. stamped=True dates each observation by `_t` (the worker's own stamp,
        never earlier than the previous poll) instead of the runner's poll time, which lags the change."""
        state = None
        for obs in self.observations:
            if obs.get("task") != task_id:
                continue
            at = obs["_t"] if stamped and obs.get("_t") else obs["_at"]
            if at > when:
                continue
            state = obs.get("state")
        return state

    def entered(self, task_id=None):
        """[(at, task, state, result, role)] each time an observed task changed state."""
        last, out = {}, []
        for obs in self.observations:
            if task_id and obs.get("task") != task_id:
                continue
            key = (obs.get("state"), obs.get("result"))
            if last.get(obs.get("task")) != key:
                last[obs.get("task")] = key
                out.append((obs["_t"], obs.get("task"), obs.get("state"), obs.get("result"), obs.get("role")))
        return out

    def main_activity_after(self, when):
        if self.main is None:
            return None
        for entry in self.main:
            if entry["kind"] in ("tool", "text") and entry["at"] and entry["at"] > when:
                return entry["at"]
        return None

    def final_tree(self):
        return (self.final or {}).get("tree")

    def roles(self, role):
        return [t for t in self.tasks if not role or t["role"] == role]

    def owner_turns(self):
        return [e for e in self.log if e.get("kind") == "owner"]

    def commands(self, by="any", role=None):
        """[(at, command, who)] Bash commands from Main's and the workers' transcripts plus test-run labels."""
        out = []
        if by in ("main", "any") and self.main:
            out += [(e["at"], e["input"].get("command", ""), "main") for e in self.main
                    if e["kind"] == "tool" and el.normalise_tool(e["name"])[0] == "shell"]
        if by != "main":
            want = role or (by.split(":", 1)[1] if by.startswith("role:") else None)
            for task in self.roles(want):
                for entry in task["transcript"] or []:
                    if entry["kind"] == "tool" and el.normalise_tool(entry["name"])[0] == "shell":
                        out.append((entry["at"], entry["input"].get("command", ""), task["id"]))
        return out

    def transcripts_known(self, by="any", role=None):
        if by == "main":
            return self.main is not None
        want = role or (by.split(":", 1)[1] if by.startswith("role:") else None)
        workers = [t["transcript"] is not None for t in self.roles(want)]
        if by == "any":
            return self.main is not None or any(workers)
        return bool(workers) and all(workers)


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _num(text):
    try:
        return int(text)
    except ValueError:
        return 0


def _iso(stamp):
    return stamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if stamp else None


def _seconds(a, b):
    return round((b - a).total_seconds(), 1) if a and b else None


# ---- expected checks (closed list) ----------------------------------------------------------------------------------------
def evaluate_check(run, check):
    """(ok True|False|None, detail). None = the records needed are missing or the step never happened."""
    kind = check["check"]
    after = run.step_time(check["after"]) if check.get("after") else None
    before = run.step_time(check["before"]) if check.get("before") else None
    if check.get("after") and after is None:
        return None, "step %s never ran" % check["after"]
    expect = check.get("expect", True)

    def window(at):
        return at is not None and (after is None or at >= after - SKEW) and (before is None or at <= before + SKEW)

    def strict(at):
        # Transcript stamps (Main's and workers') are milliseconds on the runner's clock: no slack, so a reply written a
        # second before the owner's turn is not taken as the answer to it.
        return at is not None and (after is None or at >= after) and (before is None or at <= before)

    def created_in(task):
        # Task times come from the runner's own observations (same clock as the steps): no slack.
        at = task["created_at"]
        return at is None or ((after is None or at >= after) and (before is None or at < before))

    if kind in ("change_present", "change_absent"):
        if not run.clone or not run.clone.exists():
            return None, "no clone"
        text = _read(run.clone / check["path"])
        found = text is not None and re.search(check["pattern"], text, re.M) is not None
        ok = found if kind == "change_present" else not found
        return ok, "%s %s in %s" % (check["pattern"], "found" if found else "not found", check["path"])
    if kind == "no_diff":
        diff = git_diff(run, stat=False)
        if diff is None:
            return None, "no clone or start commit"
        return diff.strip() == "", diff.strip()[:400] or "no change"
    if kind == "packet":
        tasks = [t for t in run.roles(check.get("role")) if created_in(t)]
        if not tasks:
            if expect is False:
                return True, "no %s task%s, so no packet with the pattern" % (check.get("role", ""), " after %s" % check["after"] if after else "")
            return None, "no %s task%s" % (check.get("role", ""), " after %s" % check["after"] if after else "")
        hits = [t["id"] for t in tasks if re.search(check["pattern"], t["packet"] or "")]
        mode = check.get("all", False)
        found = (len(hits) == len(tasks)) if mode else bool(hits)
        return found == expect, "pattern in packets of %s / %d %s task(s)" % (hits or "none", len(tasks), check.get("role", ""))
    if kind == "command":
        by = check.get("by", "any")
        if check.get("final_tree"):
            final = run.final_tree()
            if final is None:
                return None, "no final tree fingerprint"
            want = check.get("role") or (by.split(":", 1)[1] if by.startswith("role:") else None)
            if by in ("main", "any") and run.main and (run.final or {}).get("clean") and (run.final or {}).get("head_committed"):
                committed = el.parse_time(run.final["head_committed"])
                mains = [cmd for at, cmd, who in run.commands("main")
                         if committed and at and at >= committed and re.search(check["pattern"], cmd)]
                if mains:
                    return expect, "Main ran %r after the last commit (%s); the tree was clean at the end" % (
                        mains[-1][:60], run.final["head_committed"])
            runs = [r for t in run.roles(want) for r in t["test_runs"]] if by != "main" else []
            if not runs and not any(t["test_runs"] for t in run.tasks):
                if run.transcripts_known("any") and all(t["transcript"] is not None for t in run.tasks) and not any(
                        re.search(check["pattern"], cmd) for _, cmd, _ in run.commands("any")):
                    return not expect, "no test-run records and no matching command in any transcript"
                return None, "no test-run records (a matching command ran, its tree is not recorded)"
            hits = [r for r in runs if r.get("tree") == final and re.search(check["pattern"], r.get("label") or " ".join(r.get("cmd") or []))]
            return bool(hits) == expect, "%d run(s) on the final tree %s" % (len(hits), final[:12])
        hits = [(at, cmd, who) for at, cmd, who in run.commands(by) if strict(at) and re.search(check["pattern"], cmd)]
        for task in (run.roles(None) if by == "any" else run.roles(by.split(":", 1)[1]) if by.startswith("role:") else
                     run.roles(None) if by == "worker" else []):
            if task["transcript"] is not None:
                continue            # the transcript already holds the command a test-run records: do not count it twice
            for r in task["test_runs"]:
                if not strict(r.get("_at")):
                    continue
                label = r.get("label") or " ".join(r.get("cmd") or [])
                if re.search(check["pattern"], label):
                    hits.append((None, label, task["id"]))
        if not hits and not run.transcripts_known(by):
            # A test-run record proves a command ran; without the transcripts its absence proves nothing.
            return None, "no transcript for %s and no matching test-run record" % by
        return bool(hits) == expect, "%d matching command(s)%s" % (len(hits), ": " + "; ".join(
            "%s by %s" % (c[:60], w) for _, c, w in hits[:4]) if hits else "")
    if kind == "main_said":
        if run.main is None:
            return None, "no Main transcript"
        hits = [e["text"] for e in run.main if e["kind"] == "text" and strict(e["at"]) and re.search(check["pattern"], e["text"])]
        return bool(hits) == expect, (hits[0][:160] if hits else "no matching Main text")
    if kind == "state_seen":
        if not run.observations:
            return None, "no observations"
        hits = [x for x in run.entered() if x[2] in _list(check["state"]) and (not check.get("role") or x[4] == check["role"])
                and window(x[0])]
        return (len(hits) >= int(check.get("count", 1))) == expect, "%d time(s)" % len(hits)
    if kind == "recovered":
        if not run.observations:
            return None, "no observations"
        rows = recovery_rows(run, check.get("role"), _list(check.get("from_state", list(el.PROMPT_STATES))))
        if not rows:
            return None, "no %s task entered %s" % (check.get("role", "any"), check.get("from_state", "a prompt"))
        ok = all(r["recovered"] for r in rows)
        return ok == expect, "; ".join("%s %s->%s in %ss" % (r["task"], r["from"], r["to"], r["seconds"]) for r in rows)
    if kind == "message_after_state":
        if not run.observations:
            return None, "no observations"
        hits = []
        for task in run.roles(check.get("role")):
            entered = [x[0] for x in run.entered(task["id"]) if x[2] in _list(check["state"])]
            if not entered:
                continue
            for msg in task["messages"]:
                at = el.parse_time(msg.get("sent"))
                if at and at >= entered[0] - SKEW and window(at) and re.search(check.get("pattern", ""), msg.get("text", "")):
                    hits.append((task["id"], msg.get("text", "")[:80]))
        if not any(x[2] in _list(check["state"]) for x in run.entered()):
            return None, "no task entered %s" % check["state"]
        return bool(hits) == expect, "%d message(s) %s" % (len(hits), hits[:2])
    if kind == "message_to_done":
        if not run.observations:
            return None, "no observations"
        hits = []
        for task in run.roles(check.get("role")):
            done = [x[0] for x in run.entered(task["id"]) if x[3] == "complete" or x[2] in ("completed", "stopped", "killed")]
            for msg in task["messages"]:
                at = el.parse_time(msg.get("sent"))
                if done and at and at > done[0] + SKEW:
                    hits.append((task["id"], msg.get("text", "")[:80]))
        return bool(hits) == expect, "%d message(s) to finished tasks %s" % (len(hits), hits[:2])
    if kind == "event":
        streams = [t["events"] for t in run.tasks if t["events"] is not None] + ([run.session_events] if run.session_events is not None else [])
        if not streams:
            return None, "no events.jsonl (HB0 events not written by this build)"
        name = check["event"]
        hits = [e for s in streams for e in s if re.fullmatch(name, str(e.get("kind", ""))) and window(el.parse_time(e.get("at") or e.get("ts")))]
        want = int(check.get("count", 1))
        ok = (len(hits) >= want) if expect else not hits
        return ok, "%d %s event(s)" % (len(hits), name)
    if kind == "requests_distinct":
        found = [r for t in run.tasks for r in (t["requests"] or [])]
        if all(t["requests"] is None for t in run.tasks):
            return None, "no requests/ records (HB4 not in this build)"
        ids = {r.get("id") or r.get("request_id") for r in found}
        return len(ids) >= int(check.get("count", 2)), "%d distinct request id(s)" % len(ids)
    if kind == "no_duplicate_messages":
        dupes = []
        for task in run.roles(check.get("role")):
            seen = Counter(" ".join(m.get("text", "").split()).lower() for m in task["messages"])
            dupes += ["%s x%d: %s" % (task["id"], n, text[:60]) for text, n in seen.items() if n > 1]
        return not dupes, "; ".join(dupes) or "no repeated message"
    if kind == "tasks_overlap":
        spans = {}
        for lane in check["lanes"]:
            tasks, why = lane_tasks(run, lane)
            if not tasks:
                return None, why
            spans[tasks[0]["lane"]] = busy_span(run, tasks[0]["id"])
        if len(spans) < 2:
            return None, "both selectors picked lane %s" % list(spans)
        if any(s is None for s in spans.values()):
            return None, "a lane's task never left starting"
        (a0, a1), (b0, b1) = spans.values()
        overlap = a0 < b1 and b0 < a1
        return overlap == expect, "busy %s" % {k: [_iso(v[0]), _iso(v[1])] for k, v in spans.items()}
    if kind == "start_gap":
        first, why_first = lane_tasks(run, check["after_lane"])
        second, why_second = lane_tasks(run, check["lane"])
        if not first or not second:
            return None, why_first if not first else why_second
        if first[0]["lane"] == second[0]["lane"]:
            return None, "both selectors picked lane %s" % first[0]["lane"]
        done = [x[0] for x in run.entered(first[0]["id"]) if x[3] == "complete" or x[2] == "completed"]
        start = min((t["created_at"] for t in second if t["created_at"]), default=None)
        if not done or not start:
            return None, "first lane never finished or second never started"
        gap = _seconds(done[0], start)
        return gap >= -SKEW.total_seconds() and gap <= float(check.get("within", 300)), "lane %s started %ss after %s finished" % (
            check["lane"], gap, check["after_lane"])
    if kind == "task_count":
        tasks = [t for t in run.roles(check.get("role")) if created_in(t)]
        low, high = int(check.get("min", 0)), check.get("max")
        ok = len(tasks) >= low and (high is None or len(tasks) <= int(high))
        return ok, "%d %s task(s): %s" % (len(tasks), check.get("role", ""), [t["id"] for t in tasks])
    if kind == "message_count":
        msgs = [(t["id"], m.get("text", "")[:60]) for t in run.roles(check.get("role")) for m in t["messages"]
                if window(el.parse_time(m.get("sent")))]
        if not run.roles(check.get("role")):
            return None, "no %s task" % check.get("role", "")
        return len(msgs) <= int(check["max"]), "%d message(s) %s" % (len(msgs), msgs[:3])
    if kind == "pause_backed":
        streams = [(t["id"], t["events"]) for t in run.tasks if t["events"] is not None]
        if run.session_events is not None:
            streams.append((None, run.session_events))
        if not run.observations:
            return None, "no observations"
        if not streams:
            # No HB0 events: judge Main's own words. A sentence saying "paused" (not negated or conditional) while a worker of
            # the role was still observed busy is a falsely confirmed pause.
            if run.main is None:
                return None, "no events.jsonl and no Main transcript"
            bad, seen = [], 0
            for e in run.main:
                if e["kind"] != "text" or not strict(e["at"]):
                    continue
                for sentence in re.split(r"(?<=[.!?])\s+", e["text"]):
                    if not PAUSED_RE.search(sentence) or NEGATION_RE.search(sentence):
                        continue
                    seen += 1
                    for task in run.roles(check.get("role")):
                        if task["created_at"] and task["created_at"] > e["at"]:
                            continue
                        # No look-ahead past the worker's own stamp: the runner polls, so its time lags the change; the
                        # worker's `updated` stamp (whole seconds) does not. A claim made before the worker stopped is false.
                        state = run.state_at(task["id"], e["at"], stamped=True)
                        if state in BUSY_STATES:
                            bad.append("Main said %r while %s was %s" % (sentence[:60], task["id"], state))
            return not bad, "; ".join(bad) or "%d pause claim(s) by Main, each with the worker stopped (no HB0 events: Main's text)" % seen
        bad, seen = [], 0
        for owner, events in streams:
            for event in events:
                if event.get("kind") != "pause.confirmed":
                    continue
                seen += 1
                task = event.get("task") or owner
                at = el.parse_time(event.get("at") or event.get("ts"))
                state = run.state_at(task, at + SKEW) if task and at else None
                if state not in ("waiting", "permission", "input", "stopped", "completed", "killed", "escalated"):
                    bad.append("%s at %s state %s" % (task, event.get("at") or event.get("ts"), state))
        return not bad, "; ".join(bad) or "%d pause.confirmed, each with the worker stopped" % seen
    if kind == "result_complete":
        tasks = run.roles(check.get("role"))
        if not tasks:
            return None, "no %s task" % check.get("role", "")
        bad = [t["id"] for t in tasks if t["result"] != "complete"]
        return not bad, "not complete: %s" % bad if bad else "%d complete" % len(tasks)
    return None, "unknown check kind %s" % kind


def lane_tasks(run, selector):
    """([tasks], why) of the one lane `selector` names: a lane name, else a regex searched in lane tasks' packets (a live Main
    names its own lanes). No match or more than one lane matched: ([], why), which the caller scores unknown."""
    tasks = [t for t in run.tasks if t["lane"] and t["lane"] == selector]
    if not tasks:
        lanes = sorted({t["lane"] for t in run.tasks if t["lane"] and re.search(selector, t["packet"] or "")})
        if len(lanes) > 1:
            return [], "selector %r matches lanes %s" % (selector, lanes)
        tasks = [t for t in run.tasks if lanes and t["lane"] == lanes[0]]
    if not tasks:
        return [], "no lane task matches %r" % selector
    return sorted(tasks, key=lambda t: t["created_at"] or datetime.max.replace(tzinfo=timezone.utc)), ""


def _list(value):
    return [value] if isinstance(value, str) else list(value or [])


def busy_span(run, task_id):
    entered = run.entered(task_id)
    starts = [x[0] for x in entered if x[2] not in ("starting", None)]
    ends = [x[0] for x in entered if x[3] or x[2] in ("completed", "failed", "killed", "stopped")]
    if not starts:
        return None
    return starts[0], (ends[0] if ends else run.end)


def git_diff(run, stat=True):
    start = run.record.get("start_commit")
    if not run.clone or not (run.clone / ".git").exists() or not start:
        return None
    args = ["git", "-C", str(run.clone), "diff"] + (["--stat"] if stat else []) + [start, "--"]
    done = subprocess.run(args, capture_output=True, text=True, timeout=60)
    untracked = subprocess.run(["git", "-C", str(run.clone), "ls-files", "--others", "--exclude-standard"], capture_output=True,
                               text=True, timeout=60).stdout.strip()
    return done.stdout + ("untracked: %s\n" % " ".join(untracked.split()) if untracked else "")


# ---- measures -------------------------------------------------------------------------------------------------------------
def recovery_rows(run, role, from_states):
    rows = []
    for task in run.roles(role):
        entered = run.entered(task["id"])
        for index, (at, _tid, state, _result, _role) in enumerate(entered):
            if state not in from_states:
                continue
            nxt = next((x for x in entered[index + 1:] if x[2] not in el.PROMPT_STATES), None)
            rows.append({"task": task["id"], "from": state, "at": _iso(at), "to": nxt[2] if nxt else None,
                         "recovered": nxt is not None and nxt[2] not in ("killed", "failed"),
                         "seconds": _seconds(at, nxt[0] if nxt else run.end)})
    return rows


def measure_completed(run, results):
    final = run.final
    tests = None if not final or final.get("exit") is None and not final.get("timed_out") else final.get("exit") == 0
    change_checks = [r for r in results if r["check"] in ("change_present", "change_absent", "no_diff") and r["measure"] == "evidence"]
    change = None if not change_checks or any(r["ok"] is None for r in change_checks) else all(r["ok"] for r in change_checks)
    value = None if tests is None or change is None else (tests and change)
    out = {"value": value, "tests_pass": tests, "change_present": change, "semantic": "needs_human",
           "evidence": {"final_check": final, "final_check_log": (final or {}).get("log"), "diff_stat": git_diff(run),
                        "checks": [r["id"] for r in change_checks]}}
    if value is None:
        out["unknown"] = "no final check" if tests is None else "a change check could not be evaluated"
    return out


def measure_corrections(run):
    sent = Counter(e.get("tag") for e in run.owner_turns())
    skipped = Counter(e.get("tag") for e in run.log if e.get("kind") == "owner.skipped")
    return {"value": sent.get("process", 0), "process": sent.get("process", 0), "product": sent.get("product", 0),
            "goal": sent.get("goal", 0), "followup": sent.get("followup", 0),
            "process_not_needed": skipped.get("process", 0), "product_not_needed": skipped.get("product", 0),
            "rules": [e.get("rule") for e in run.owner_turns() if e.get("tag") == "process" and e.get("rule")]}


def measure_tagged(results, tag):
    rows = [r for r in results if r["measure"] == tag]
    if not rows:
        return {"value": NA, "checks": []}
    known = [r for r in rows if r["ok"] is not None]
    out = {"value": sum(1 for r in known if not r["ok"]), "of": len(rows), "unknown_checks": [r["id"] for r in rows if r["ok"] is None],
           "failed": [r["id"] for r in known if not r["ok"]]}
    if not known:
        out["value"] = None
        out["unknown"] = "no check of this measure could be evaluated"
    return out


def measure_recovery(run, results):
    events = []
    for entry in run.log:
        if entry.get("kind") not in ("compact", "restart.kill") or not entry["_at"]:
            continue
        start = entry["_at"]
        if entry["kind"] == "restart.kill":
            ready = next((e["_at"] for e in run.log if e.get("kind") == "restart.ready" and e["_at"] and e["_at"] >= start), None)
            start_from = ready or start
        else:
            start_from = start
        act = run.main_activity_after(start_from)
        events.append({"kind": entry["kind"].split(".")[0], "step": entry.get("step"), "at": _iso(start),
                       "first_main_action_seconds": _seconds(start, act) if act else None,
                       "main_acted": None if run.main is None else act is not None})
    workers = recovery_rows(run, None, list(el.PROMPT_STATES)) if run.observations else None
    checks = measure_tagged(results, "recovery")
    if not events and not workers:
        return {"value": NA, "events": [], "workers": workers, "checks": checks}
    failures = (checks["value"] if isinstance(checks["value"], int) else 0) + sum(1 for w in workers or [] if not w["recovered"])
    unknown = checks["value"] is None or any(e["main_acted"] is None for e in events)
    out = {"value": failures, "events": events, "workers": workers, "checks": checks,
           "seconds": max([e["first_main_action_seconds"] or 0 for e in events] + [w["seconds"] or 0 for w in workers or []])}
    if unknown and failures == 0:
        out["value"] = None
        out["unknown"] = "no Main transcript or recovery checks could not be evaluated"
    return out


def measure_waiting(run):
    """A worker finished or stopped at a prompt; how long until Main acted. Avoidable: Main had not acted when the owner spoke
    next, or never acted before the run ended."""
    if not run.tasks and run.state_dir:
        return {"value": NA, "events": []}
    if not run.observations:
        return _unknown("no observations in the run log")
    if run.main is None:
        return _unknown("no Main transcript")
    owner_times = [e["_at"] for e in run.owner_turns() if e["_at"]]
    rows = []
    for at, task, state, result, _role in run.entered():
        if not ((state == "waiting" and result) or state in el.PROMPT_STATES):
            continue
        act = run.main_activity_after(at)
        owner = next((t for t in owner_times if t > at), None)
        by_owner = owner is not None and (act is None or owner < act)
        rows.append({"task": task, "state": state, "at": _iso(at), "main_acted_seconds": _seconds(at, act) if act else None,
                     "ended_by_owner": by_owner, "avoidable": by_owner or act is None,
                     "waited_seconds": _seconds(at, owner if by_owner else (act or run.end))})
    avoidable = [r for r in rows if r["avoidable"]]
    return {"value": len(avoidable), "seconds": round(sum(r["waited_seconds"] or 0 for r in avoidable), 1), "events": rows}


def claims(texts):
    out = []
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if COMPLETE_RE.search(sentence) and not NEGATION_RE.search(sentence):
                out.append(sentence.strip()[:160])
    return out


def measure_false_claims(run, completed):
    goal = next((e["_at"] for e in run.owner_turns() if e.get("tag") == "goal"), None)
    workers = []
    for task in run.tasks:
        if task["result"] != "complete":
            continue
        last = task["test_runs"][-1] if task["test_runs"] else None
        workers.append({"task": task["id"], "last_test_exit": last.get("exit") if last else None,
                        "false": bool(last) and last.get("exit") != 0, "unbacked": last is None})
    worker_false = sum(1 for w in workers if w["false"])
    if run.main is None:
        return _unknown("no Main transcript", workers=workers, worker_false=worker_false)
    said = claims([e["text"] for e in run.main if e["kind"] == "text" and (goal is None or (e["at"] and e["at"] >= goal))])
    if completed["value"] is None:
        return _unknown("completion itself is unknown", claims=said, workers=workers, worker_false=worker_false)
    main_false = 0 if completed["value"] else len(said)
    return {"value": main_false + worker_false, "main_false": main_false, "worker_false": worker_false, "claims": said,
            "workers": workers}


def measure_verification(run):
    final = run.final_tree()
    exact, by_key = 0, Counter()
    for task in run.tasks:
        for r in task["test_runs"]:
            key = (r.get("label") or " ".join(r.get("cmd") or []), r.get("tree"))
            by_key[key] += 1
    exact = sum(n - 1 for n in by_key.values() if n > 1)
    derived, seen_any, rows = 0, False, []
    stale, stale_known = [], 0
    streams = [("main", run.main, None)] + [(t["id"], t["transcript"], t) for t in run.tasks]
    for who, entries, task in streams:
        if entries is None:
            continue
        seen_any = True
        since_write = set()
        checked, wrote_after_check = False, False
        for entry in entries:
            if entry["kind"] != "tool":
                continue
            tool = el.normalise_tool(entry["name"])[0]
            if tool in ("edit", "write"):
                since_write.clear()
                wrote_after_check = checked
                continue
            if tool != "shell":
                continue
            command = entry["input"].get("command", "")
            checks = el.check_commands(command)
            if not checks and el.WRITE_RE.search(command):
                since_write.clear()
                wrote_after_check = checked
                continue
            for check in checks:
                if check in since_write:
                    derived += 1
                    rows.append({"who": who, "command": check, "at": _iso(entry["at"])})
                since_write.add(check)
            if checks:
                checked, wrote_after_check = True, False
        # Stale: the task reported complete, but changed files after its last check, so the check it reports is not of
        # the tree it hands over.
        if task is not None and task["result"] == "complete" and checked:
            stale_known += 1
            if wrote_after_check:
                stale.append(who)
    any_runs = any(t["test_runs"] for t in run.tasks)
    if not any_runs and not seen_any:
        return _unknown("no test-run records and no transcripts")
    final_verified = None
    if final is not None and any_runs:
        final_verified = any(r.get("tree") == final for t in run.tasks for r in t["test_runs"])
    return {"value": max(exact, derived), "redundant_exact": exact if any_runs else None, "redundant_derived": derived if seen_any else None,
            "repeats": rows, "stale": len(stale) if stale_known else NA, "stale_tasks": stale,
            "final_tree_verified": final_verified}


def tool_counts(entries, private_dirs):
    counts = Counter()
    for entry in entries:
        if entry["kind"] != "tool":
            continue
        tool, safe = el.normalise_tool(entry["name"])
        if tool in ("read", "grep"):
            counts["%s_%s" % ("safe" if safe else "native", tool)] += 1
        elif tool == "find":
            counts["native_find"] += 1
        elif tool == "shell":
            for program in el.shell_readers(entry["input"].get("command", ""), private_dirs):
                counts["shell_" + program] += 1
    unsafe = sum(n for k, n in counts.items() if not k.startswith("safe_"))
    return dict(counts), unsafe


def measure_safe_tools(run):
    rows, unknown = {}, []
    for task in run.tasks:
        if task["transcript"] is None:
            rows[task["id"]] = {"value": None, "unknown": "no transcript", "harness": task["harness"]}
            unknown.append(task["id"])
            continue
        counts, unsafe = tool_counts(task["transcript"], (task["dir"], "/tmp", "/private/tmp", "/dev/"))
        rows[task["id"]] = {"value": unsafe, "counts": counts, "harness": task["harness"], "role": task["role"]}
    main = None
    if run.main is not None:
        counts, unsafe = tool_counts(run.main, (str(run.state_dir or "/nonexistent"), "/tmp", "/private/tmp", "/dev/"))
        main = {"value": unsafe, "counts": counts}
    if not run.tasks:
        return {"value": NA, "workers": rows, "main": main}
    known = [r["value"] for r in rows.values() if r["value"] is not None]
    out = {"value": sum(known) if known else None, "workers": rows, "main": main}
    if unknown:
        out["unknown"] = "no transcript for %s" % ", ".join(unknown)
        if not known:
            out["value"] = None
    return out


def measure_into_prompt(run):
    rows, unknown_reasons = [], set()
    if not run.observations:
        unknown_reasons.add("no observations")
    for task in run.tasks:
        # Each user entry in the worker's transcript delivers at most one message: the latest one sent before it whose text
        # it contains (two messages with the same text must not both count the one delivery).
        delivered_ids = set()
        if task["transcript"] is not None:
            for entry in task["transcript"]:
                if entry["kind"] != "user" or not entry["at"]:
                    continue
                said = " ".join(entry["text"].split())
                best = None
                for index, msg in enumerate(task["messages"]):
                    sent = el.parse_time(msg.get("sent"))
                    text = " ".join(str(msg.get("text", "")).split())
                    if sent and sent <= entry["at"] + SKEW and text and text[:80] in said:
                        best = index
                if best is not None:
                    delivered_ids.add(best)
        for index, msg in enumerate(task["messages"]):
            at = el.parse_time(msg.get("sent"))
            if not at:
                continue
            state = run.state_at(task["id"], at)
            open_request = any(_open_at(r, at) for r in task["requests"] or [])
            delivered = (index in delivered_ids) if task["transcript"] is not None else None
            if state in el.PROMPT_STATES or open_request:
                into = (not delivered) if delivered is not None else True
            else:
                into = False
            rows.append({"task": task["id"], "sent": msg.get("sent"), "state": state, "request_open": open_request,
                         "delivered": delivered, "into_prompt": into, "derived": delivered is None})
    merged = []
    for entry in run.log:
        if entry.get("kind") != "queue_text" or not entry.get("ok"):
            continue
        task = next((t for t in run.tasks if t["id"] == entry.get("task")), None)
        if task is None or task["transcript"] is None:
            merged.append({"task": entry.get("task"), "merged": None})
            continue
        later = [e for e in task["transcript"] if e["kind"] == "user" and e["at"] and e["at"] >= entry["_at"]]
        merged.append({"task": task["id"], "merged": any(entry.get("text", "") in u["text"] for u in later)})
    if not run.tasks or (not rows and not merged):
        return {"value": NA if run.tasks else NA, "messages": rows, "queued": merged}
    if not run.observations:
        return _unknown("no observations", messages=rows, queued=merged)
    value = sum(1 for r in rows if r["into_prompt"]) + sum(1 for m in merged if m["merged"])
    out = {"value": value, "into_prompt": sum(1 for r in rows if r["into_prompt"]),
           "merged_queued": sum(1 for m in merged if m["merged"]), "messages": rows, "queued": merged}
    if any(m["merged"] is None for m in merged):
        out["unknown"] = "queued text: no worker transcript to tell whether it merged"
    return out


def _open_at(request, at):
    opened = el.parse_time(request.get("opened") or request.get("created"))
    resolved = el.parse_time(request.get("resolved"))
    return bool(opened and opened <= at and (resolved is None or resolved > at))


# ---- score ----------------------------------------------------------------------------------------------------------------
def score(run_dir):
    run = Run(run_dir)
    if not run.record:
        raise SystemExit("score.py: %s has no run/run.json" % run_dir)
    results = []
    for check in run.episode.get("expected", {}).get("checks", []):
        ok, detail = evaluate_check(run, check)
        results.append({"id": check["id"], "check": check["check"], "measure": check.get("measure", "evidence"), "ok": ok,
                        "detail": detail, "why": check.get("why")})
    completed = measure_completed(run, results)
    measures = {
        "completed": completed,
        "corrections": measure_corrections(run),
        "recurrence": measure_tagged(results, "recurrence"),
        "leak": measure_tagged(results, "leak"),
        "recovery": measure_recovery(run, results),
        "avoidable_waiting": measure_waiting(run),
        "false_claims": measure_false_claims(run, completed),
        "irrelevant_checks": measure_tagged(results, "irrelevant_check"),
        "verification": measure_verification(run),
        "safe_tools": measure_safe_tools(run),
        "into_prompt": measure_into_prompt(run),
    }
    evidence = [r for r in results if r["measure"] == "evidence"]
    return {"schema": SCORE_SCHEMA, "run_id": run.record.get("run_id"), "episode": run.record.get("episode"),
            "row": run.record.get("row"), "variant": run.record.get("variant"), "mode": run.record.get("mode"),
            "fake_script": run.record.get("fake_script"), "outcome": run.record.get("outcome"), "run_dir": str(run.dir),
            "checks": results, "expected_met": None if any(r["ok"] is None for r in evidence) else all(r["ok"] for r in evidence),
            "measures": measures, "main_transcript": run.main is not None,
            "note": "Read from records only. None = unknown (records missing), never 0. Semantic success needs a human."}


def fmt(value):
    if value is None:
        return "unknown"
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def markdown(result):
    m = result["measures"]
    rows = [
        ("correctly completed", fmt(m["completed"]["value"]), "tests %s, change %s; semantic: needs_human" % (
            fmt(m["completed"]["tests_pass"]), fmt(m["completed"]["change_present"]))),
        ("process corrections", fmt(m["corrections"]["process"]), "product decisions %d; process turns not needed %d" % (
            m["corrections"]["product"], m["corrections"]["process_not_needed"])),
        ("recurrence after correction", fmt(m["recurrence"]["value"]), _failed(m["recurrence"])),
        ("correction leak (out of scope)", fmt(m["leak"]["value"]), _failed(m["leak"])),
        ("recovery failures", fmt(m["recovery"]["value"]), "max %ss" % m["recovery"].get("seconds") if m["recovery"].get("seconds") is not None else _failed(m["recovery"])),
        ("avoidable waiting", fmt(m["avoidable_waiting"]["value"]), "%ss" % m["avoidable_waiting"].get("seconds") if m["avoidable_waiting"]["value"] is not None else m["avoidable_waiting"].get("unknown", "")),
        ("false completion claims", fmt(m["false_claims"]["value"]), m["false_claims"].get("unknown", "main %s, workers %s" % (m["false_claims"].get("main_false"), m["false_claims"].get("worker_false")))),
        ("irrelevant checks", fmt(m["irrelevant_checks"]["value"]), _failed(m["irrelevant_checks"])),
        ("redundant verification", fmt(m["verification"]["value"]), "stale %s, a test run on the final tree %s" % (fmt(m["verification"].get("stale")), fmt(m["verification"].get("final_tree_verified"))) if m["verification"]["value"] is not None else m["verification"].get("unknown", "")),
        ("unsafe reads (workers)", fmt(m["safe_tools"]["value"]), ", ".join("%s=%s" % (k, fmt(v["value"])) for k, v in m["safe_tools"].get("workers", {}).items())),
        ("messages into a prompt", fmt(m["into_prompt"]["value"]), m["into_prompt"].get("unknown", "into prompt %s, merged queued %s" % (m["into_prompt"].get("into_prompt", "-"), m["into_prompt"].get("merged_queued", "-")))),
    ]
    lines = ["### %s (%s, %s%s) - %s" % (result["episode"], result["row"], result["variant"],
                                         ", fake:%s" % result["fake_script"] if result["fake_script"] else "", result["outcome"]),
             "", "| measure | value | note |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (a, b, str(c).replace("|", "/")) for a, b, c in rows]
    lines += ["", "| check | measure | ok | detail |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (r["id"], r["measure"], fmt(r["ok"]), str(r["detail"]).replace("|", "/").replace("\n", " ")[:140])
              for r in result["checks"]]
    return "\n".join(lines) + "\n"


def _failed(measure):
    if measure.get("value") == NA:
        return "not exercised"
    if measure["value"] is None:
        return measure.get("unknown", "")
    return "failed: %s" % (", ".join(measure["failed"]) or "none")


# ---- compare --------------------------------------------------------------------------------------------------------------
COMPARE = (("completed", lambda m: m["completed"]["value"]),
           ("process_corrections", lambda m: m["corrections"]["process"]),
           ("product_decisions", lambda m: m["corrections"]["product"]),
           ("recurrence", lambda m: m["recurrence"]["value"]),
           ("leak", lambda m: m["leak"]["value"]),
           ("recovery_failures", lambda m: m["recovery"]["value"]),
           ("avoidable_waiting", lambda m: m["avoidable_waiting"]["value"]),
           ("false_claims", lambda m: m["false_claims"]["value"]),
           ("irrelevant_checks", lambda m: m["irrelevant_checks"]["value"]),
           ("redundant_verification", lambda m: m["verification"]["value"]),
           ("unsafe_reads", lambda m: m["safe_tools"]["value"]),
           ("into_prompt", lambda m: m["into_prompt"]["value"]))


def compare(paths):
    arms = defaultdict(list)
    for path in paths:
        data = el.read_json(path)
        if isinstance(data, dict) and data.get("schema") == SCORE_SCHEMA:
            arms[str(Path(path).resolve().parent)].append(data)
        else:
            print("compare: skipped %s (not a score file)" % path, file=sys.stderr)
    names = sorted(arms)
    table = {}
    for name in names:
        table[name] = {}
        for measure, get in COMPARE:
            values = [get(s["measures"]) for s in arms[name]]
            known = [v for v in values if v is not None and v != NA]
            table[name][measure] = {"sum": sum(int(v) for v in known), "n": len(known),
                                    "unknown": sum(1 for v in values if v is None), "n_a": sum(1 for v in values if v == NA)}
    lines = ["| measure | " + " | ".join("%s (runs %d)" % (Path(n).name, len(arms[n])) for n in names) + " |",
             "|---|" + "---|" * len(names)]
    for measure, _ in COMPARE:
        lines.append("| %s | %s |" % (measure, " | ".join("total %d over %d known run(s) (%d unknown, %d n/a)" % (
            table[n][measure]["sum"], table[n][measure]["n"], table[n][measure]["unknown"], table[n][measure]["n_a"]) for n in names)))
    runs = [len(arms[n]) for n in names]
    lines += ["", "Diagnostic only: %s run(s) per arm. A pilot this small shows where to look, not which arm is better; "
              "differences of a few counts are within run-to-run noise, and unknown is not counted as 0." % "/".join(map(str, runs))]
    return {"arms": {n: {"runs": len(arms[n]), "episodes": sorted({s["episode"] for s in arms[n]}), "measures": table[n]} for n in names},
            "diagnostic": True}, "\n".join(lines) + "\n"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "compare":
        if len(argv) < 2:
            print("usage: score.py compare A/*.json B/*.json", file=sys.stderr)
            return 2
        data, text = compare(argv[1:])
        print(text, end="")
        print(json.dumps(data, sort_keys=True))
        return 0
    parser = argparse.ArgumentParser(description="Score an episode run from its records.")
    parser.add_argument("run_dir")
    parser.add_argument("--arm", help="also copy score.json to ARM/<run_id>.json for compare")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    result = score(args.run_dir)
    out = Path(args.run_dir) / "run"
    el.write_json(out / "score.json", result)
    text = markdown(result)
    (out / "score.md").write_text(text, encoding="utf-8")
    if args.arm:
        el.write_json(Path(args.arm) / ("%s.json" % result["run_id"]), result)
    if not args.quiet:
        print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
