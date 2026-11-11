#!/usr/bin/env python3
"""Score an episode run from its records (HB8a). Python 3.9, standard library only. Never starts a model.

    score.py RUN_DIR [--arm DIR] [--quiet]     write RUN_DIR/run/score.json and score.md, print the table
    score.py compare A/*.json B/*.json         per-measure counts per arm (an arm = the files' parent directory)

Reads only: RUN_DIR/run/{run.json, episode.json, episode-log.jsonl, final-check.json}, the session's task records
(TP_STATE_DIR/tasks/<id>/: meta, state, packet.md, task.md, result.md, messages.jsonl, test-runs/, events.jsonl, requests/,
the transcript named by transcript-path), the Main transcripts listed in run.json, lane records in the clone's .git, and
`git diff` of the clone against the run's start commit.

Every record is read once, through one reader that gives it a status (ok, missing, empty though the log says it ran,
unreadable, undated); every check and measure is computed in a reader scope, and one that read a record that is not ok
is unknown with the reasons (round 4 rule 1). Every measure is {"value": ..., ...}. value None means unknown (the reason
in `unknown`) and is never counted as 0; "n/a" means the episode does not exercise the measure. Semantic success is always
needs_human: the scorer lists the evidence a person reads to judge it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import episode_lib as el  # noqa: E402

SCORE_SCHEMA = "tp-episode-score:v1"
CHECKS = el.CHECK_KINDS               # evaluate_check handles exactly these
CHECK_MEASURES = el.CHECK_MEASURES
COMPLETE_RE = re.compile(r"(?i)\b(done|completed?|finished|all set|ready to merge|shipped|tests? (?:all )?pass(?:ed|es|ing)?)\b")
TESTED_RE = re.compile(r"(?i)\b(tested|tests? (?:all )?pass(?:ed|es|ing)?|builds? (?:pass(?:ed|es)?|succeed(?:ed|s)?|clean)|verified)\b")
PAUSED_RE = re.compile(r"(?i)\b(paused|on hold|stopped (it|the \w+|them))\b")
BUSY_STATES = ("starting", "working", "tool")
NEGATION_RE = re.compile(r"(?i)\b(not|isn't|aren't|wasn't|no|never|incomplete|yet|unless|once|when|if|will|until|before)\b|\?$")
NA = "n/a"
STAMP = timedelta(seconds=1)      # a worker's `updated` stamp: whole seconds, never later than the change
SKEW = timedelta(seconds=2)       # records written by different processes: allow this much clock/ordering slack


def _unknown(reason, **extra):
    return dict({"value": None, "unknown": reason}, **extra)


# ---- the evidence reader (round 4 rule 1) -----------------------------------------------------------------------------------
# Every record the scorer uses is read once into a Rec: its value and a status. A measure or check that reads a record whose
# status is not ok is unknown, with the reasons: the reader records each read in the scopes open at the time (Run.track), so
# what a value depends on is what its code read, never a hand-kept table.
OK, MISSING, EMPTY, UNREADABLE, UNDATED = "ok", "missing", "empty", "unreadable", "undated"
TEST_RUN_CALL = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?test-run\.sh\s")     # a call of tools/agent/test-run.sh
UNKNOWN_PLACE = "?"            # a write or execution whose directory the scorer cannot resolve
INF = float("inf")
NEVER = datetime.max.replace(tzinfo=timezone.utc)
END = (NEVER, INF)             # the end of the run: after every record


class Rec:
    """One record as read: label, status (ok, missing, empty: empty although the log shows it should hold something,
    unreadable, undated), value and why."""
    __slots__ = ("label", "status", "value", "why")

    def __init__(self, label, status=OK, value=None, why=None):
        self.label, self.status, self.value, self.why = label, status, value, why

    @property
    def ok(self):
        return self.status == OK

    def problem(self):
        return "%s %s%s" % (self.label, self.status, " (%s)" % self.why if self.why else "")


class Task(dict):
    """A task's records. Reading a key ([] or get) reports the record(s) the value came from to the reader."""

    def __init__(self, run, values, sources):
        dict.__init__(self, values)
        self._run, self._sources = run, sources

    def __getitem__(self, key):
        for rec in self._sources.get(key, ()):
            self._run.use(rec)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        return self[key] if key in self else default


def _point(at, order=INF):
    return (at, order) if at is not None else None


class Run:
    """Everything the scorer reads, through the evidence reader."""

    def __init__(self, run_dir):
        self.dir = Path(run_dir).resolve()
        self._scopes = []
        self._cache = {}
        self.recs = []
        rec = self.dir / "run"
        self.record = el.read_json(rec / "run.json") or {}
        self.episode = el.read_json(rec / "episode.json") or el.read_json(self.record.get("episode_file") or "") or {}
        if self.record:
            # The scorer acts on the episode's checks and plants: one it cannot act on is an error, not a silent pass.
            el.validate_episode(self.episode, str(rec / "episode.json"))
        self._load_log(rec / "episode-log.jsonl")
        status, final, why = el.read_json_status(rec / "final-check.json")
        if status == OK and (not final.get("tree") or (final.get("exit") is None and not final.get("timed_out"))):
            status, why = UNREADABLE, "run/final-check.json has no tree or exit"
        self._final_rec = self.add(Rec("final check", status, final if status == OK else {}, why))
        self.clone = Path(self.record["clone"]) if self.record.get("clone") else None
        if not self.clone or not self.clone.is_dir():
            self._clone_rec = self.add(Rec("clone", MISSING, None, str(self.clone or "not in run.json")))
        elif not (self.clone / ".git").exists():
            # The runner's clone is a git work tree; an empty or plain directory has lost what the change checks diff.
            self._clone_rec = self.add(Rec("clone", MISSING, None, "%s is not a git work tree" % self.clone))
        else:
            self._clone_rec = self.add(Rec("clone", OK, self.clone))
        self.state_dir = Path(self.record["state_dir"]) if self.record.get("state_dir") else None
        self._load_main()
        tasks_dir = self.state_dir / "tasks" if self.state_dir else None
        # A task's events.jsonl is required only when this build writes task journals (some task has one): the session
        # journal holds session events (a correction recorded), so its presence says nothing about task journals.
        self._events_written = bool(tasks_dir and tasks_dir.is_dir() and any(tasks_dir.glob("*/events.jsonl")))
        self._requests_written = bool(tasks_dir and tasks_dir.is_dir() and any(p.is_dir() for p in tasks_dir.glob("*/requests")))
        self._load_tasks()
        status, events, why = el.read_jsonl_status(self.state_dir / "events.jsonl") if self.state_dir else (MISSING, None, "no state dir")
        if status == MISSING:
            status, why = OK, None             # the event journal is per task (interfaces.md section 3): absent by design
        self._session_rec = self.add(Rec("session events.jsonl", status, events, why))
        end = [e["_at"] for e in self._log if e.get("kind") in ("teardown", "run.end") and e["_at"]]
        self.end = end[0] if end else (self._log[-1]["_at"] if self._log else None)
        self.plants = plant_status(self)
        self.missed = {sid: p for sid, p in self.plants.items() if p["ok"] is not True}
        self.declared = list((self.episode.get("expected") or {}).get("records") or [])
        self.manifest = self._manifest()

    # ---- reading -------------------------------------------------------------------------------------------------------
    def add(self, rec):
        self.recs.append(rec)
        return rec

    def use(self, rec):
        """The record's value; a status that is not ok goes into every open scope."""
        if rec.status != OK:
            self.flag(rec.problem())
        return rec.value

    def flag(self, text):
        for scope in self._scopes:
            if text not in scope:
                scope.append(text)

    @contextmanager
    def track(self, isolated=False):
        """A scope collecting the problems of every read made inside it (isolated: not passed to the enclosing scopes)."""
        scope = []
        saved = self._scopes
        self._scopes = [scope] if isolated else saved + [scope]
        try:
            yield scope
        finally:
            self._scopes = saved

    def cached(self, name, compute):
        """compute() once; every later call reports the problems of the reads it made, as if it read them again."""
        if name not in self._cache:
            with self.track(isolated=True) as scope:
                value = compute()
            self._cache[name] = (value, list(scope))
        value, problems = self._cache[name]
        for problem in problems:
            self.flag(problem)
        return value

    # ---- loading -------------------------------------------------------------------------------------------------------
    def _load_log(self, path):
        status, entries, why = el.read_jsonl_status(path)
        entries = entries or []
        if status == OK and not entries:
            status, why = EMPTY, "run/episode-log.jsonl has no entries"
        elif status == OK:
            lacking = [k for k in ("run.start", "run.end") if k not in {e.get("kind") for e in entries}]
            if lacking:
                status, why = MISSING, "run/episode-log.jsonl has no %s entry" % " or ".join(lacking)
        self._log_rec = self.add(Rec("episode log", status, entries, why))
        self._log = entries
        seen_before = {}
        for entry in self._log:
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
        self._obs = [e for e in self._log if e.get("kind") == "observe"]

    def _load_main(self):
        """Each listed Main transcript is its own record (round 4 rule 1): missing, unreadable, or empty when the log shows
        Main was sent turns (or restarted); and every owner turn the log sent must be in one of them."""
        listed = self.record.get("main_transcripts") or []
        self._main_recs = []
        if not listed:
            self._main_recs.append(self.add(Rec("Main transcript", MISSING, None, "run.json lists none")))
        owner = [e for e in self._log if e.get("kind") == "owner"]
        restarted = any(e.get("kind") in ("restart.kill", "restart.ready", "compact") for e in self._log)
        entries, segments = [], []
        for path in listed:
            status, lines, why = el.read_jsonl_status(path)
            got = el.transcript_from(lines) if status == OK else None
            if status == OK and not lines and (owner or restarted or len(listed) > 1):
                # A transcript file exists because its session wrote to it; empty, it has lost what that session did.
                status, why = EMPTY, "the log shows Main was sent turns or restarted"
            self._main_recs.append(self.add(Rec("Main transcript %s" % path, status, got, why)))
            segments.append(got)
            entries.extend(got or [])
        if listed and all(s is not None for s in segments):
            said = [" ".join(e["text"].split()) for e in entries if e["kind"] == "user"]
            lost = [e for e in owner if e.get("text") and not any(" ".join(str(e["text"]).split())[:60] in s for s in said)]
            if lost:
                self._main_recs.append(self.add(Rec("Main transcripts", MISSING, None, "owner turn %r (%s) is in no listed "
                                                    "transcript" % (str(lost[0]["text"])[:40], lost[0].get("at")))))
        self._main = entries if listed and all(r.ok for r in self._main_recs) else None

    def _load_tasks(self):
        if not self.state_dir or not self.state_dir.is_dir():
            self._tasks_rec = self.add(Rec("state dir", MISSING, [], str(self.state_dir or "not in run.json")))
            self._tasks, self._obs_recs = [], {}
            return
        base = self.state_dir / "tasks"
        dirs = sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
        ids = {p.name for p in dirs}
        phantoms = sorted({o.get("task") for o in self._obs if o.get("task") and o.get("task") not in ids})
        why = "task dir of %s (observed in the log) is absent" % ", ".join(phantoms) if phantoms else None
        self._obs_recs = {}
        self._tasks = [self._load_task(p) for p in dirs]
        self._tasks_rec = self.add(Rec("task dirs", MISSING if phantoms else OK, self._tasks, why))
        calls = self._message_calls()
        for task in self._tasks:
            self._load_messages(task, calls)

    def _load_task(self, task_dir):
        tid = task_dir.name
        states = [o.get("state") for o in self._obs if o.get("task") == tid]
        # The runner polls every task: one in the state dir that the log never observed has lost its record of states.
        self._obs_recs[tid] = self.add(Rec("observations of %s" % tid, OK if states else MISSING, None,
                                           None if states else "the episode log has no observation of it"))
        ran = any(s not in (None, "starting") for s in states)
        sources, values = {}, {"id": tid, "dir": str(task_dir)}
        status, meta, why = el.read_json_status(task_dir / "meta.json")
        meta = meta or {}
        if status == OK and not meta.get("role"):
            status, why = UNREADABLE, "meta.json of %s has no role" % tid
        meta_rec = self.add(Rec("meta of %s" % tid, status, meta, why))
        for key, field in (("role", "role"), ("lane", "lane"), ("workdir", "workdir"), ("parent", "parent"),
                           ("created", "created"), ("harness", "worker"), ("pane", "pane")):
            values[key] = meta.get(field)
            sources[key] = (meta_rec,)
        values["role"] = values["role"] or tid.split("-")[0]
        # meta.created has whole seconds; the runner's first sighting of the task is a later bound with sub-second time.
        created = el.parse_time(meta.get("created"))
        undated_obs = [o for o in self._obs if o.get("task") == tid and not o["_at"]]
        first = next((o["_at"] for o in self._obs if o.get("task") == tid and o["_at"]), None)
        if created and first and first >= created:
            created = first
        if not meta.get("created"):
            created_rec = self.add(Rec("creation time of %s" % tid, UNDATED, None, "meta.json has no created"))
        elif undated_obs:
            created_rec = self.add(Rec("creation time of %s" % tid, UNDATED, None, "an observation of it has no time"))
        else:
            created_rec = self.add(Rec("creation time of %s" % tid, OK))
        values["created_at"] = created
        sources["created_at"] = (meta_rec, created_rec)
        packet = _read(task_dir / "packet.md")
        packet = packet if packet is not None else _read(task_dir / "task.md")
        status = MISSING if packet is None else EMPTY if not packet.strip() else OK
        values["packet"], sources["packet"] = packet, (self.add(Rec("packet of %s" % tid, status, packet)),)
        runs, bad = [], []
        for p in sorted((task_dir / "test-runs").glob("*.json"), key=lambda p: _num(p.stem)):
            st, r, why = el.read_json_status(p)
            if st != OK:
                bad.append(why)
                continue
            # tp-test-run:v1 has no time field; it is written when the run ends: mtime minus seconds dates the start.
            r["_end"] = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc)
            r["_at"] = r["_end"] - timedelta(seconds=float(r.get("seconds") or 0))
            r["_trees"] = _record_trees(r)
            runs.append(r)
        values["test_runs"] = runs
        # tools/agent/test-run.sh numbers its records from 1 up: a gap below the highest is a lost record.
        numbers = {_num(p.stem) for p in (task_dir / "test-runs").glob("*.json")}
        gaps = [n for n in range(1, max(numbers) + 1) if n not in numbers] if numbers else []
        runs_rec = self.add(Rec("test-runs of %s" % tid, UNREADABLE if bad else MISSING if gaps else OK, runs,
                                "; ".join(bad) or ("test-runs/%s.json absent below a later record" % gaps[0] if gaps else None)))
        sources["test_runs"] = (runs_rec,)
        result = el.result_status(task_dir)
        exists = (task_dir / "result.md").exists()
        if result is None and exists:
            status, why = UNREADABLE, "result.md has no RESULT_STATUS line"
        elif result is None and not states:
            status, why = MISSING, "no result.md and the log never observed %s" % tid
        elif result is None and any(o.get("result") for o in self._obs if o.get("task") == tid):
            status, why = MISSING, "no result.md but the log observed a result for %s" % tid
        else:
            status, why = OK, None
        values["result"], sources["result"] = result, (self.add(Rec("result of %s" % tid, status, result, why)),)
        # result.md's mtime: when the result was written, finer than the worker's whole-second stamp (rewritten later, it
        # falls outside that second and is not used).
        values["result_written"] = datetime.fromtimestamp((task_dir / "result.md").stat().st_mtime, timezone.utc) if exists else None
        sources["result_written"] = sources["result"]
        path = el.transcript_path(task_dir)
        st, lines, why = el.read_jsonl_status(path) if path else (MISSING, None, "no transcript-path or transcript.jsonl")
        entries = el.transcript_from(lines) if st == OK else None
        if st == OK and not lines and (ran or runs or result or not states):
            # Empty is absent by design only when the log shows the task never left `starting`: never observed, it may
            # have run and lost its transcript.
            st, why = EMPTY, ("the log saw it run" if ran else "it wrote test-run records or a result" if runs or result
                              else "the log never observed %s, so whether it ran is unknown" % tid)
        values["transcript"], sources["transcript"] = entries, (self.add(Rec("transcript of %s" % tid, st, entries, why)),)
        # Each test-run.sh call in the transcript writes one record: fewer records than calls is a lost record.
        calls = sum(len(TEST_RUN_CALL.findall(e["input"].get("command") or "")) for e in entries or []
                    if e["kind"] == "tool" and e["name"] == "Bash" and isinstance(e.get("input"), dict))
        if runs_rec.status == OK and calls > len(runs):
            runs_rec.status, runs_rec.why = MISSING, "%d test-run.sh call(s) in the transcript, %d record(s)" % (calls, len(runs))
        st, events, why = el.read_jsonl_status(task_dir / "events.jsonl")
        if st == MISSING and not self._events_written:
            st, why = OK, None                 # this build writes no events.jsonl: absent by design
        elif st == OK and not events and ran:
            st, why = EMPTY, "the log saw it run"
        values["events"], sources["events"] = events, (self.add(Rec("events.jsonl of %s" % tid, st, events, why)),)
        at_prompt = any(s in el.PROMPT_STATES for s in states)
        requests = el.requests(task_dir)
        if requests is None:
            # Absent by design only when the log shows the task never at a prompt (no request could have opened), or this
            # build writes no requests/ for any task.
            st = MISSING if self._requests_written and (at_prompt or not states) else OK
            why = ("the log saw %s at a prompt" % tid if at_prompt else "%s was never observed" % tid) if st != OK else None
        else:
            bad_requests = [p.name for p in (task_dir / "requests").glob("*.json") if el.read_json_status(p)[0] != OK]
            st = UNREADABLE if bad_requests else EMPTY if not requests and at_prompt else OK
            why = ("unreadable: %s" % ", ".join(bad_requests)) if bad_requests else "the log saw it at a prompt" if st != OK else None
        values["requests"] = requests or []
        sources["requests"] = (self.add(Rec("requests/ of %s" % tid, st, requests, why)),)
        values["review_of"] = el._text(task_dir / "review-of")
        return Task(self, values, sources)

    def _message_calls(self):
        """{task id: (certain, possible)} `tp-agent message <id>` calls in Main's transcript; None when Main's transcript is
        not readable (then an absent journal cannot be told from a lost one)."""
        if self._main is None:
            return None
        calls = defaultdict(lambda: [0, 0])
        for entry in self._main:
            if entry["kind"] != "tool" or el.normalise_tool(entry["name"])[0] != "shell":
                continue
            stages = el.shell_stages(entry["input"].get("command", ""))
            for stage, (ran, _code) in zip(stages, el.stage_runs(stages, entry.get("exit"), entry.get("blocked"))):
                words = stage["words"]
                if os.path.basename(words[0]) == "tp-agent" and len(words) > 2 and words[1] == "message":
                    calls[words[2]][1] += 1
                    calls[words[2]][0] += 1 if ran else 0
        return calls

    def _load_messages(self, task, calls):
        """messages.jsonl: absent is ok only when Main's transcript (readable) shows no message sent to the task; a journal
        with fewer entries than Main's certain sends has lost some."""
        tid = task["id"]
        path = Path(task["dir"]) / "messages.jsonl"
        status, lines, why = el.read_jsonl_status(path)
        certain, possible = (calls or {}).get(tid, (0, 0))
        if status == MISSING:
            if calls is None:
                why = "Main's transcript, which would show messages sent, is not readable"
            elif possible:
                why = "Main's transcript shows %d `tp-agent message %s` call(s)" % (possible, tid)
            else:
                status, why = OK, None
        elif status == OK and calls is not None and len(lines) < certain:
            status, why = MISSING, "it holds %d message(s); Main's transcript shows %d sent" % (len(lines), certain)
        dict.__setitem__(task, "messages", lines or [])
        task._sources["messages"] = (self.add(Rec("messages.jsonl of %s" % tid, status, lines, why)),)

    # ---- record manifest (a report; scoring never reads it) ------------------------------------------------------------
    def _manifest(self):
        """{record label: problem} of every record read that is not ok."""
        return {r.label: r.problem() for r in self.recs if r.status != OK}

    def incomplete(self):
        """Problems with the records every measure needs (the episode log and the state dir): the run is incomplete-records."""
        return [r.problem() for r in (self._log_rec, self._tasks_rec) if r.status != OK]

    # ---- tracked accessors ---------------------------------------------------------------------------------------------
    @property
    def log(self):
        return self.use(self._log_rec) or []

    @property
    def tasks(self):
        self.use(self._tasks_rec)
        return self._tasks

    @property
    def main(self):
        for rec in self._main_recs:
            self.use(rec)
        return self._main

    @property
    def final(self):
        return self.use(self._final_rec) or {}

    @property
    def session_events(self):
        return self.use(self._session_rec)

    def clone_ok(self):
        return self.use(self._clone_rec) is not None

    def obs(self, task_id=None, strict=True):
        """Dated observations (of one task, or of all). Observations are the record of every task's state over time: one
        with no time could be anywhere in a window (round 4 rule 2), and a task never observed has lost its record.
        strict=False: an undated observation is left to the caller (undated_obs), for a check a dated match decides."""
        self.use(self._log_rec)
        if task_id is not None:
            mine = [o for o in self._obs if o.get("task") == task_id]
            if task_id in self._obs_recs:
                self.use(self._obs_recs[task_id])
            elif not mine:
                self.flag("observations of %s missing (no task dir)" % task_id)
        else:
            mine = self._obs
            self.tasks
            for rec in self._obs_recs.values():
                self.use(rec)
        if strict and any(not o["_at"] for o in mine):
            self.flag("observation(s) %sundated" % ("of %s " % task_id if task_id else ""))
        return [o for o in mine if o["_at"]]

    def undated_obs(self, task_id=None):
        return any(not o["_at"] for o in self._obs if task_id is None or o.get("task") == task_id)

    def task_events_written(self):
        return self._events_written

    def event_streams(self, named=False):
        """Every task's events.jsonl and the session's, or None when this build writes none (no events.jsonl anywhere:
        absent by design). When the build writes them, each one absent is missing."""
        if not self._events_written and self.session_events is None:
            return None
        streams = ([(t["id"], t["events"]) for t in self.tasks] if self._events_written else []) + [(None, self.session_events)]
        streams = [(owner, s) for owner, s in streams if s is not None]
        return streams if named else [s for _o, s in streams]

    def requests_written(self):
        return self._requests_written

    def start_commit(self):
        start = self.record.get("start_commit")
        if not start:
            self.flag("run.json has no start_commit")
        return start

    def git(self, *args, ok=(0,)):
        """One git call on the clone through the reader: a nonzero exit (or one not in `ok`) is unreadable, never an empty
        answer. Returns the CompletedProcess, or None."""
        if not self.clone_ok():
            return None
        label = "git %s" % " ".join(args)[:80]
        try:
            done = subprocess.run(["git", "-C", str(self.clone)] + list(args), capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            rec = Rec(label, UNREADABLE, None, str(error))
        else:
            rec = Rec(label, OK, done) if done.returncode in ok else Rec(
                label, UNREADABLE, None, "exit %d: %s" % (done.returncode, done.stderr.strip()[:120]))
        if not rec.ok:
            self.add(rec)
        return self.use(rec)

    # ---- lookups --------------------------------------------------------------------------------------------------------
    def step_time(self, step_id):
        for entry in self.log:
            if entry.get("step") == step_id and entry.get("kind") != "observe":
                if not entry["_at"]:
                    self.flag("step %s undated" % step_id)
                    continue
                return entry["_at"]
        return None

    def result_at(self, task_id, when):
        """(observed, result): whether the task was observed by `when` (worker-stamped time) and its result status then."""
        seen, result = False, None
        for obs in self.obs(task_id):
            if (obs.get("_t") or obs["_at"]) <= when:
                seen, result = True, obs.get("result")
        return seen, result

    def result_time(self, task_id):
        """When the task's result was first observed (worker-stamped), else None."""
        return next((o.get("_t") or o["_at"] for o in self.obs(task_id) if o.get("result")), None)

    def result_held_time(self, task_id):
        """When the worker last stopped (left working/tool/permission) holding a result (worker-stamped), else the first
        observed result. A worker that wrote result.md, took a message and wrote it again claims what result.md now holds."""
        stops, last = [], None
        for obs in self.obs(task_id):
            key = (obs.get("state"), obs.get("result"))
            if key != last and obs.get("result") and obs.get("state") not in BUSY_STATES + ("permission",):
                stops.append(obs.get("_t") or obs["_at"])
            last = key
        return stops[-1] if stops else self.result_time(task_id)

    def missed_kind(self, *kinds):
        return {sid: p for sid, p in self.missed.items() if p["kind"] in kinds}

    def state_at(self, task_id, when, stamped=False):
        """The task's last observed state at `when`. stamped=True dates each observation by `_t` (the worker's own stamp,
        never earlier than the previous poll) instead of the runner's poll time, which lags the change."""
        state = None
        for obs in self.obs(task_id):
            at = obs["_t"] if stamped and obs.get("_t") else obs["_at"]
            if at > when:
                continue
            state = obs.get("state")
        return state

    def entered(self, task_id=None, strict=True):
        """[(at, task, state, result, role)] each time an observed task changed state."""
        last, out = {}, []
        for obs in self.obs(task_id, strict):
            key = (obs.get("state"), obs.get("result"))
            if last.get(obs.get("task")) != key:
                last[obs.get("task")] = key
                out.append((obs["_t"], obs.get("task"), obs.get("state"), obs.get("result"), obs.get("role")))
        return out

    def main_activity_after(self, when):
        """(first dated Main action after `when`, whether Main has an action with no time). None for an unreadable Main."""
        main = self.main
        if main is None:
            return None, False
        acts = [e for e in main if e["kind"] in ("tool", "text")]
        first = next((e["at"] for e in acts if e["at"] and e["at"] > when), None)
        return first, any(not e["at"] for e in acts)

    def final_tree(self):
        return self.final.get("tree")

    def roles(self, role):
        return [t for t in self.tasks if not role or t["role"] == role]

    def owner_turns(self):
        return [e for e in self.log if e.get("kind") == "owner"]

    # ---- where a path is (round 4 rule 4) -------------------------------------------------------------------------------
    def places(self):
        """(project roots, deepest first; paths that are never project content)."""
        def compute():
            roots = [_norm_dir(self.clone)] if self.clone else []
            for task in self.tasks:
                if task.get("workdir"):
                    roots.append(_norm_dir(task["workdir"]))
            outside = [_norm_dir(p) for p in (self.state_dir, self.dir / "run", self.record.get("claude_config"), "/dev") if p]
            return sorted(set(r for r in roots if r), key=len, reverse=True), outside
        return self.cached("places", compute)

    def place(self, path, cwd):
        """The project root whose tree a path is in; None outside every project (a task dir, /tmp, the run records, a
        .git dir); UNKNOWN_PLACE when it cannot be resolved ($VAR, or relative with no known cwd)."""
        if not path or "$" in str(path) or "`" in str(path):
            return UNKNOWN_PLACE
        path = os.path.expanduser(str(path))
        if not path.startswith("/"):
            if cwd is None:
                return UNKNOWN_PLACE
            path = os.path.join(cwd, path)
        full = el._norm(path)
        roots, outside = self.places()
        if any(el._under(full, d) for d in outside):
            return None
        for root in roots:
            if el._under(full, root):
                return None if el._under(full, os.path.join(root, ".git")) else root
        return None

    def write_places(self, stage, cwd):
        """The places one writing stage changes: its output redirections and the files it writes (the last operand of
        cp/ln/rsync/install, every operand of mv/rm/touch/tee/truncate, sed -i/perl -i files, dd of=), else its cwd."""
        words = stage["words"]
        program, args = words[0], words[1:]
        targets = [t for t in stage["outputs"] if not el._norm(t if t.startswith("/") else "/x/" + t).startswith("/dev/")]
        ops = [a for a in args if not a.startswith("-")]
        if program in ("cp", "ln", "rsync", "install"):
            targets += ops[-1:]
        elif program == "dd":
            targets += [a[3:] for a in args if a.startswith("of=")]
        elif program in ("mv", "rm", "touch", "tee", "truncate"):
            targets += ops
        elif program == "sed" and any(a == "-i" or a.startswith("--in-place") or re.match(r"^-[a-zA-Z]*i", a) for a in args):
            targets += [a for a in el._operands("sed", [a for a in args if a != "-i" and not a.startswith("--in-place")])[0]
                        if a != "''" and a]
        elif program == "perl":
            skip = False
            for a in args:
                if skip:
                    skip = False
                elif a in ("-e", "-E") or re.match(r"^-[a-zA-Z]*e$", a):
                    skip = True
                elif not a.startswith("-"):
                    targets.append(a)
        elif program == "git" and "-C" in args[:-1]:
            targets.append(args[args.index("-C") + 1])
        if not targets:
            targets = ["."]
        if stage["opaque"]:
            return {UNKNOWN_PLACE}
        return {self.place(t, cwd) for t in targets}

    # ---- executions and writes ------------------------------------------------------------------------------------------
    def stream_events(self, main=True, workers=True):
        """[{at, order, kind: write|exec, who, role, cwd, place, ran, text, identity, tree, exit, timed_out, record}] across
        Main and every worker, in time order. Each Bash stage is placed in its own cwd (`cd` tracked per stage; a Claude
        harness keeps a cd inside its start dir for the next call, pi does not); only a stage that ran counts as run (ran
        None: it may not have; round 4 rule 5). A worker's tp-test-run:v1 record is the execution of the stage it matches
        (its cmd, cwd, tree and exit); a record no Bash call matches is an execution by itself, its writes included.
        Writes: Edit/Write tools and writing stages, placed by their target path (round 4 rule 4)."""
        if main and workers:
            return self.cached("events", self._stream_events)
        return self.cached("events:%s" % ("main" if main else "workers"), lambda: self._stream_events(main, workers))

    def _stream_events(self, main=True, workers=True):
        events, order = [], 0
        clone = _norm_dir(self.clone) if self.clone else None
        streams = [("main", None, self.main or [], clone, "claude", [])] if main else []
        for t in self.tasks if workers else []:
            streams.append((t["id"], t["role"], t["transcript"] or [], _norm_dir(t.get("workdir")), t.get("harness") or "claude",
                            list(t["test_runs"])))
        for who, role, entries, start, harness, unmatched in streams:
            cwd = start
            for entry in entries:
                if entry["kind"] != "tool":
                    continue
                tool = el.normalise_tool(entry["name"])[0]
                order += 1
                base = {"at": entry["at"], "order": order, "who": who, "role": role, "identity": None, "tree": None,
                        "record": None, "exit": None, "timed_out": False}
                if tool in ("edit", "write"):
                    target = entry["input"].get("file_path") or entry["input"].get("path") or entry["input"].get("notebook_path")
                    events.append(dict(base, kind="write", cwd=cwd, place=self.place(target, cwd), ran=True, text=target))
                    continue
                if tool != "shell":
                    continue
                stages = el.shell_stages(entry["input"].get("command", ""))
                here, outer = cwd, []              # outer: the cwd of each enclosing `( ... )`, restored when it ends
                for stage, (ran, code) in zip(stages, el.stage_runs(stages, entry.get("exit"), entry.get("blocked"))):
                    while len(outer) < stage.get("sub", 0):
                        outer.append(here)
                    while len(outer) > stage.get("sub", 0):
                        here = outer.pop()
                    if stage["cd"] is not None:
                        here = _resolve_cd(here, stage["cd"])
                        continue
                    if el.stage_writes(stage):
                        for place in sorted(self.write_places(stage, here), key=str):
                            events.append(dict(base, kind="write", cwd=here, place=place, ran=ran, text=" ".join(stage["words"])))
                    identity = el.check_identity(stage["words"]) or _recorded_identity(stage, unmatched)
                    record = None
                    for r in unmatched if identity else []:
                        if identity in r["_trees"] and entry["at"] and abs((r["_at"] - entry["at"]).total_seconds()) <= 5:
                            record = r
                            unmatched.remove(r)
                            break
                    if record is not None:
                        where = _norm_dir(record.get("cwd")) or here
                        events.append(dict(base, kind="exec", cwd=where, place=self.place(where, None) if where else UNKNOWN_PLACE,
                                           ran=True, text=identity, identity=identity, tree=record["_trees"][identity],
                                           record=record, exit=record.get("exit"), timed_out=bool(record.get("timed_out"))))
                    else:
                        events.append(dict(base, kind="exec", cwd=here, place=self.place(here, None) if here else UNKNOWN_PLACE,
                                           ran=ran, text=identity or " ".join(stage["words"]), identity=identity, exit=code))
                here = outer[0] if outer else here
                if harness == "pi":
                    cwd = start
                elif here and start and el._under(here, start):
                    cwd = here
                elif here != cwd and harness != "claude":
                    cwd = None                     # harness unknown: whether the cd persists is unknown
                elif here is None and any(s["cd"] is not None for s in stages):
                    cwd = None                     # a cd the scorer cannot resolve: where later calls run is unknown
                else:
                    cwd = start
            for r in unmatched:
                where = _norm_dir(r.get("cwd")) or start
                stages = el.shell_stages(el.record_command(r))
                code = r.get("exit")
                for stage, (ran, _c) in zip(stages, el.stage_runs(stages, code, False)):
                    order += 1
                    base = {"at": r["_at"], "order": order, "who": who, "role": role, "record": r, "cwd": where}
                    if stage["cd"] is not None:
                        continue
                    if el.stage_writes(stage):
                        for place in sorted(self.write_places(stage, where), key=str):
                            events.append(dict(base, kind="write", place=place, ran=ran, text=" ".join(stage["words"]),
                                               identity=None, tree=None, exit=None, timed_out=False))
                    identity = el.check_identity(stage["words"]) or _recorded_identity(stage, [r])
                    if identity:
                        events.append(dict(base, kind="exec", place=self.place(where, None) if where else UNKNOWN_PLACE,
                                           ran=True if ran else None, text=identity, identity=identity,
                                           tree=r["_trees"].get(identity), exit=code, timed_out=bool(r.get("timed_out"))))
        events.sort(key=lambda e: (e["at"] or NEVER, e["order"]))
        return events

    def same_tree(self, place, start, end):
        """Whether `place`'s tree at point `end` is the tree at point `start` (points are (at, order)), from the write
        history (round 4 rules 2 to 4): True when no write could have changed it in between, every candidate write dated;
        False when a write that ran falls in between (written: not proof of a different tree, never proof of the same);
        None when that is not known (an undated or unplaceable write, one that may not have run, or a place outside every
        project, whose writes are not followed)."""
        if start is None or end is None or place is None:
            return None
        unsure = False
        for w in self.stream_events():
            if w["kind"] != "write" or w["place"] is None:
                continue
            if place != UNKNOWN_PLACE and w["place"] not in (place, UNKNOWN_PLACE):
                continue
            if w["at"] is None:
                unsure = True
            elif start < (w["at"], w["order"]) <= end:
                if w["ran"] is True and w["place"] == place != UNKNOWN_PLACE:     # two unknown places may differ
                    return False
                unsure = True
        return None if unsure else True

    def tree_at(self, place, point):
        """The tree of `place` at `point` when a record fixes it: the latest recorded tree there with no write after it."""
        if place is None or place == UNKNOWN_PLACE:
            return None
        recorded = [e for e in self.stream_events() if e["kind"] == "exec" and e["tree"] and e["place"] == place and e["at"]
                    and (e["at"], e["order"]) <= point]
        if not recorded:
            return None
        last = recorded[-1]
        return last["tree"] if self.same_tree(place, (last["at"], last["order"]), point) is True else None


def _resolve_cd(here, target):
    if "$" in target or "`" in target or target == "-":
        return None
    target = os.path.expanduser(target)
    if target.startswith("/"):
        return el._norm(target)
    return el._norm(os.path.join(here, target)) if here else None


def _recorded_identity(stage, records):
    """A stage that is not on the check list is still a check when a tp-test-run:v1 record ran exactly it: the agent ran it
    through the test runner (the episode's own test command, say `sh run-tests.sh`). Its text is its identity."""
    text = " ".join(stage["words"])
    return text if any(text in r["_trees"] for r in records) else None


def _record_trees(record):
    """{check identity: tree} for one test-run record: its tree is fingerprinted before its cmd starts, so a check that runs
    after a write inside the same cmd ran on a tree the record does not name (None)."""
    out, written = {}, False
    for kind, text in el.command_events(el.record_command(record)):
        if kind == "write":
            written = True
        elif text not in out:
            out[text] = None if written else record.get("tree")
    if not out:
        out[" ".join(el.executions(el.record_command(record)))] = record.get("tree")
    return out


def _in_scope(event, by, role=None):
    if by == "main":
        return event["who"] == "main"
    if event["who"] == "main":
        return by == "any" and not role
    want = role or (by.split(":", 1)[1] if by.startswith("role:") else None)
    return not want or event["role"] == want


def plant_status(run):
    """{step id: {kind, ok, reason}} for every planted condition (hold_prompt, queue_text) of the episode. A plant the log
    does not show as done (ok false, or never reached because the run stopped first) is missed."""
    out = {}
    for step in run.episode.get("steps", []):
        if step.get("do") in el.PLANT_KINDS:
            out[step["id"]] = {"kind": step["do"], "ok": False, "reason": "never reached: the run stopped before this step"}
    for entry in run._log:
        if entry.get("kind") in el.PLANT_KINDS and entry.get("step"):
            ok = entry.get("ok") is True
            out[entry["step"]] = {"kind": entry["kind"], "ok": ok, "task": entry.get("task"),
                                  "reason": None if ok else entry.get("reason") or "the runner logged it as not done"}
    return out


def _missed_reason(missed):
    return "plant-missed: " + "; ".join("%s (%s): %s" % (sid, p["kind"], p["reason"]) for sid, p in sorted(missed.items()))


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
    """(ok True|False|None, detail). None = a plant it needs was missed, the step it is timed against never happened, the
    evidence cannot decide, or a record it read was not ok (round 4 rule 1: the reader says which)."""
    ok, detail, _problems = tracked_check(run, check)
    return ok, detail


def tracked_check(run, check):
    """(ok, detail, problems): the check computed in its own reader scope; any problem makes it None, the value it would
    have had kept in the detail as partial."""
    missed = {sid: p for sid, p in run.missed.items() if sid in check.get("needs", [])}
    if missed:
        return None, _missed_reason(missed), []
    with run.track() as problems:
        ok, detail = _evaluate_check(run, check)
    incomplete = run.incomplete()
    if incomplete:
        problems[:0] = ["incomplete-records: " + "; ".join(incomplete)]
    if problems:
        return None, "records not ok: %s | partial: %s, %s" % ("; ".join(problems), fmt(ok), detail), list(problems)
    return ok, detail, []


def _decide(hits, unsure, expect, detail):
    """Round 4 rule 2: a dated match decides that something happened; its absence is known only when no candidate could
    be in the window (no undated or uncertain one)."""
    if hits:
        return expect, detail
    if unsure:
        return None, "%s; but %s" % (detail, "; ".join(unsure[:3]))
    return not expect, detail


def _evaluate_check(run, check):
    kind = check["check"]
    after = run.step_time(check["after"]) if check.get("after") else None
    before = run.step_time(check["before"]) if check.get("before") else None
    if check.get("after") and after is None:
        return None, "step %s never ran" % check["after"]
    if check.get("before") and before is None:
        return None, "step %s never ran" % check["before"]
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
        if not run.clone_ok():
            return None, "no clone"
        text = _read(run.clone / check["path"])
        if text is None and kind == "change_absent":
            return None, "%s is not in the clone: the pattern's absence proves nothing" % check["path"]
        found = text is not None and re.search(check["pattern"], text, re.M) is not None
        detail = "%s %s in %s" % (check["pattern"], "found" if found else "not found", check["path"])
        if kind == "change_present" and not found:
            # The pattern is one way to write the change: it never fails an equivalent implementation. Not found fails only
            # when the file is unchanged since the start; a changed file without the pattern needs a person.
            changed = path_changed(run, check["path"])
            if changed is None:
                return None, detail + "; git cannot tell whether the file changed since the start"
            if changed:
                return None, detail + "; the file changed (needs_human: an equivalent change?)"
            return False, detail + "; the file is unchanged since the start"
        return (found if kind == "change_present" else not found), detail
    if kind == "no_diff":
        diff = git_diff(run, stat=False)
        if diff is None:
            return None, "git cannot diff the clone against the start commit"
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
        # Executions only: the pattern must match an executed command from its start (`npm run build` matches
        # `npm run build`, never `grep 'npm run build' README.md`), in a stage that ran (round 4 rule 5).
        by = check.get("by", "any")
        pattern = re.compile(check["pattern"])
        # Only the streams the check can match are read; the final-tree question reads every writer.
        events = run.stream_events(main=by in ("main", "any") or bool(check.get("final_tree")),
                                   workers=by != "main" or bool(check.get("final_tree")))
        execs = [e for e in events if e["kind"] == "exec" and _in_scope(e, by, check.get("role")) and pattern.match(e["text"])]
        if check.get("final_tree"):
            final = run.final_tree()
            if not final:
                return None, "no final tree"
            hits, unsure = [], []
            for e in execs:
                # No record of the tree it ran on: it is the final tree only when no write to the clone followed it.
                on_final = on_final_tree(run, e, final)
                if on_final:
                    hits.append(e)
                elif on_final is None:
                    unsure.append("%r (by %s) %s" % (e["text"][:60], e["who"], "may not have run on the final tree"))
            return _decide(hits, unsure, expect, "%d run(s) on the final tree %s%s" % (
                len(hits), final[:12], ": %s by %s" % (hits[0]["text"][:60], hits[0]["who"]) if hits else ""))
        hits = [e for e in execs if strict(e["at"]) and e["ran"] is True]
        unsure = ["%r (by %s) %s" % (e["text"][:60], e["who"], "has no time" if e["at"] is None else "may not have run")
                  for e in execs if e["at"] is None or (strict(e["at"]) and e["ran"] is not True)]
        return _decide(hits, unsure, expect, "%d matching command(s)%s" % (len(hits), ": " + "; ".join(
            "%s by %s" % (e["text"][:60], e["who"]) for e in hits[:4]) if hits else ""))
    if kind == "main_said":
        main = run.main
        if main is None:
            return None, "no Main transcript"
        said = [e for e in main if e["kind"] == "text" and re.search(check["pattern"], e["text"])]
        hits = [e["text"] for e in said if strict(e["at"])]
        unsure = ["a matching Main text has no time" for e in said if e["at"] is None][:1]
        return _decide(hits, unsure, expect, hits[0][:160] if hits else "no matching Main text")
    if kind == "state_seen":
        if not run.obs(strict=False):
            return None, "no observations"
        hits = [x for x in run.entered(strict=False) if x[2] in _list(check["state"])
                and (not check.get("role") or x[4] == check["role"]) and window(x[0])]
        enough = len(hits) >= int(check.get("count", 1))
        return _decide(enough, ["an observation has no time"] if run.undated_obs() else [], expect, "%d time(s)" % len(hits))
    if kind == "recovered":
        if not run.obs():
            return None, "no observations"
        rows = recovery_rows(run, check.get("role"), _list(check.get("from_state", list(el.PROMPT_STATES))))
        if not rows:
            return None, "no %s task entered %s" % (check.get("role", "any"), check.get("from_state", "a prompt"))
        ok = all(r["recovered"] for r in rows)
        return ok == expect, "; ".join("%s %s->%s in %ss" % (r["task"], r["from"], r["to"], r["seconds"]) for r in rows)
    if kind == "message_after_state":
        if not run.obs(strict=False):
            return None, "no observations"
        hits, unsure = [], []
        for task in run.roles(check.get("role")):
            entered = [x[0] for x in run.entered(task["id"], strict=False) if x[2] in _list(check["state"])]
            if run.undated_obs(task["id"]):
                unsure.append("an observation of %s has no time" % task["id"])
            for msg in task["messages"]:
                if not re.search(check.get("pattern", ""), msg.get("text", "")):
                    continue
                at = el.parse_time(msg.get("sent"))
                if at is None:
                    unsure.append("a message to %s has no time" % task["id"])
                elif entered and at >= entered[0] - SKEW and window(at):
                    hits.append((task["id"], msg.get("text", "")[:80]))
        if not hits and not unsure and not any(x[2] in _list(check["state"]) for x in run.entered(strict=False)):
            return None, "no task entered %s" % check["state"]
        return _decide(hits, unsure, expect, "%d message(s) %s" % (len(hits), hits[:2]))
    if kind == "message_to_done":
        if not run.obs(strict=False):
            return None, "no observations"
        hits, unsure = [], []
        for task in run.roles(check.get("role")):
            done = [x[0] for x in run.entered(task["id"], strict=False) if x[3] == "complete" or x[2] in ("completed", "stopped", "killed")]
            if run.undated_obs(task["id"]):
                unsure.append("an observation of %s has no time" % task["id"])
            for msg in task["messages"]:
                at = el.parse_time(msg.get("sent"))
                if at is None:
                    unsure.append("a message to %s has no time" % task["id"])
                elif done and at > done[0] + SKEW:
                    hits.append((task["id"], msg.get("text", "")[:80]))
        return _decide(hits, unsure, expect, "%d message(s) to finished tasks %s" % (len(hits), hits[:2]))
    if kind == "event":
        streams = run.event_streams()
        if streams is None:
            return None, "no events.jsonl (HB0 events not written by this build)"
        name = check["event"]
        matching = [e for s in streams for e in s if re.fullmatch(name, str(e.get("kind", "")))]
        hits = [e for e in matching if window(el.parse_time(e.get("at") or e.get("ts")))]
        undated = [e for e in matching if el.parse_time(e.get("at") or e.get("ts")) is None]
        want = int(check.get("count", 1)) if expect else 1
        enough = len(hits) >= want
        unsure = ["%d matching event(s) have no time" % len(undated)] if undated else []
        if not run.task_events_written():
            unsure.append("no task events.jsonl (HB0 task events not written by this build): only the session journal was read")
        if expect:
            return _decide(enough, unsure, True, "%d %s event(s)" % (len(hits), name))
        return _decide(bool(hits), unsure, False, "%d %s event(s)" % (len(hits), name))
    if kind == "requests_distinct":
        if not run.requests_written():
            return None, "no requests/ records (HB4 not in this build)"
        found = [r for t in run.tasks for r in (t["requests"] or [])]
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
        if any(s is None or None in s for s in spans.values()):
            return None, "a lane's task never left starting, or the run has no end time"
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
        if not run.roles(check.get("role")):
            return None, "no %s task" % check.get("role", "")
        msgs, undated = [], 0
        for t in run.roles(check.get("role")):
            for m in t["messages"]:
                at = el.parse_time(m.get("sent"))
                if at is None:
                    undated += 1
                elif window(at):
                    msgs.append((t["id"], m.get("text", "")[:60]))
        detail = "%d message(s) %s" % (len(msgs), msgs[:3])
        if len(msgs) > int(check["max"]):
            return False, detail
        if undated and len(msgs) + undated > int(check["max"]):
            return None, detail + "; but %d message(s) have no time" % undated
        return True, detail
    if kind == "pause_backed":
        # Every pause claim is examined on its own: Main's "paused" sentences and every pause.confirmed event. A claim is
        # false when a worker it covers was busy at that moment, by the worker's own stamp (no look-ahead: a confirmation
        # counts only after the worker stopped). A worker not yet observed at the claim makes that claim unknown.
        if not run.obs():
            return None, "no observations"
        main = run.main
        if main is None:
            return None, "no Main transcript (its pause claims cannot be read)"
        claims_seen = []
        for e in main:
            if e["kind"] != "text" or not (e["at"] is None or strict(e["at"])):
                continue
            for sentence in re.split(r"(?<=[.!?])\s+", e["text"]):
                if PAUSED_RE.search(sentence) and not NEGATION_RE.search(sentence):
                    claims_seen.append((e["at"], "Main said %r" % sentence[:60], None))
        for owner, events in run.event_streams(named=True) or []:
            for event in events:
                if event.get("kind") != "pause.confirmed":
                    continue
                at = el.parse_time(event.get("at") or event.get("ts"))
                if at is not None and not strict(at):
                    continue
                claims_seen.append((at, "pause.confirmed", event.get("task") or owner))
        bad, unsure = [], []
        for at, what, named in claims_seen:
            if at is None:
                unsure.append("%s has no time" % what)
                continue
            tasks = [t for t in run.tasks if t["id"] == named] if named else [
                t for t in run.roles(check.get("role")) if not (t["created_at"] and t["created_at"] > at)]
            if named and not tasks:
                unsure.append("%s names task %s, which has no record" % (what, named))
            for task in tasks:
                state = run.state_at(task["id"], at, stamped=True)
                if state in BUSY_STATES:
                    bad.append("%s at %s while %s was %s" % (what, _iso(at), task["id"], state))
                elif state is None:
                    unsure.append("%s at %s: %s not yet observed" % (what, _iso(at), task["id"]))
        if bad:
            return False, "; ".join(bad)
        if unsure:
            return None, "; ".join(unsure)
        return True, "%d pause claim(s), each with the worker stopped" % len(claims_seen)
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
    return sorted(tasks, key=lambda t: t["created_at"] or NEVER), ""


def _list(value):
    return [value] if isinstance(value, str) else list(value or [])


def busy_span(run, task_id):
    entered = run.entered(task_id)
    starts = [x[0] for x in entered if x[2] not in ("starting", None)]
    ends = [x[0] for x in entered if x[3] or x[2] in ("completed", "failed", "killed", "stopped")]
    if not starts:
        return None
    return starts[0], (ends[0] if ends else run.end)


def path_changed(run, path):
    """True/False whether `path` in the clone differs from the run's start commit (tracked diff or untracked); None when
    git cannot tell (each git call goes through the reader: a nonzero exit is unreadable, never "no change")."""
    start = run.start_commit()
    if not start:
        return None
    diff = run.git("diff", "--quiet", start, "--", path, ok=(0, 1))
    untracked = run.git("ls-files", "--others", "--exclude-standard", "--", path)
    if diff is None or untracked is None:
        return None
    return diff.returncode == 1 or bool(untracked.stdout.strip())


def git_diff(run, stat=True):
    """The clone's diff against the start commit plus its untracked files; None when git cannot give it."""
    start = run.start_commit()
    if not start:
        return None
    done = run.git(*(["diff"] + (["--stat"] if stat else []) + [start, "--"]))
    untracked = run.git("ls-files", "--others", "--exclude-standard")
    if done is None or untracked is None:
        return None
    files = untracked.stdout.strip()
    return done.stdout + ("untracked: %s\n" % " ".join(files.split()) if files else "")


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
    """The runner's final check feeds this end-of-run measure only (round 4 rule 3), never a claim."""
    final = run.final
    tests = None if not final else final.get("exit") == 0 and not final.get("timed_out")
    change_checks = [r for r in results if r["check"] in ("change_present", "change_absent", "no_diff") and r["measure"] == "evidence"]
    change = None if not change_checks or any(r["ok"] is None for r in change_checks) else all(r["ok"] for r in change_checks)
    # Strict, not Kleene: a part that could not be read leaves the whole unknown even when the other part failed (round 4
    # rule 1: a measure is unknown when any read it made was not ok); the known part stays visible beside it.
    value = None if tests is None or change is None else tests and change
    with run.track(isolated=True) as diff_problems:
        diff_stat = git_diff(run)            # evidence for a person; what it could not read is noted, not scored
    out = {"value": value, "tests_pass": tests, "change_present": change, "semantic": "needs_human",
           "evidence": {"final_check": final, "final_check_log": (final or {}).get("log"), "diff_stat": diff_stat,
                        "diff_stat_unreadable": diff_problems, "checks": [r["id"] for r in change_checks]}}
    if value is None:
        out["unknown"] = ("no final check" if tests is None else "no change check" if not change_checks
                          else "a change check could not be evaluated")
    return out


def measure_corrections(run):
    turns = run.owner_turns()
    sent = Counter(e.get("tag") for e in turns)
    skipped = Counter(e.get("tag") for e in run.log if e.get("kind") == "owner.skipped")
    return {"value": sent.get("process", 0), "process": sent.get("process", 0), "product": sent.get("product", 0),
            "goal": sent.get("goal", 0), "followup": sent.get("followup", 0),
            "process_not_needed": skipped.get("process", 0), "product_not_needed": skipped.get("product", 0),
            "rules": [e.get("rule") for e in turns if e.get("tag") == "process" and e.get("rule")]}


def measure_tagged(results, tag):
    """Failed checks of a measure. Known only when every check of it is known: a count over the known ones is `at_least`."""
    rows = [r for r in results if r["measure"] == tag]
    if not rows:
        return {"value": NA, "checks": []}
    known = [r for r in rows if r["ok"] is not None]
    failed = [r["id"] for r in known if not r["ok"]]
    out = {"value": len(failed), "of": len(rows), "unknown_checks": [r["id"] for r in rows if r["ok"] is None], "failed": failed}
    if len(known) < len(rows):
        out["value"] = None
        out["at_least"] = len(failed)
        out["unknown"] = "check(s) not evaluated: " + "; ".join("%s: %s" % (r["id"], r["detail"]) for r in rows if r["ok"] is None)
    return out


def measure_recovery(run, results):
    """Failures to recover: failed recovery checks, workers that never left a prompt, and each compaction/restart after
    which Main never acted (a failure whose seconds are unknown, never 0). "Main never acted" is known only when every
    Main action is dated (round 4 rule 2)."""
    missed = run.missed_kind("hold_prompt")
    events = []
    log = run.log
    for entry in log:
        if entry.get("kind") not in ("compact", "restart.kill"):
            continue
        if not entry["_at"]:
            run.flag("%s entry of the episode log undated" % entry["kind"])
            continue
        start = entry["_at"]
        if entry["kind"] == "restart.kill":
            readies = [e for e in log if e.get("kind") == "restart.ready"]
            if any(not e["_at"] for e in readies):
                run.flag("restart.ready entry of the episode log undated")
            ready = next((e["_at"] for e in readies if e["_at"] and e["_at"] >= start), None)
            start_from = ready or start
        else:
            start_from = start
        act, undated = run.main_activity_after(start_from)
        acted = None if run.main is None else True if act else None if undated else False
        events.append({"kind": entry["kind"].split(".")[0], "step": entry.get("step"), "at": _iso(start),
                       "first_main_action_seconds": _seconds(start, act) if act and not undated else None,
                       "main_acted": acted, "undated_main_actions": undated})
    workers = recovery_rows(run, None, list(el.PROMPT_STATES)) if run.obs() else None
    checks = measure_tagged(results, "recovery")
    if not events and not workers and checks["value"] == NA and not missed:
        return {"value": NA, "events": events, "workers": workers, "checks": checks}
    unknown = []
    failures = 0
    if checks["value"] is None:
        unknown.append(checks["unknown"])
        failures += checks["at_least"]
    elif checks["value"] != NA:
        failures += checks["value"]
    if workers is None and run.tasks:
        unknown.append("no observations of the workers")
    failures += sum(1 for w in workers or [] if not w["recovered"])
    for e in events:
        if e["main_acted"] is None:
            unknown.append("whether Main acted after %s at %s: %s" % (
                e["kind"], e["at"], "a Main action has no time" if e["undated_main_actions"] else "no Main transcript"))
        elif not e["main_acted"]:
            failures += 1
    if missed:
        unknown.append(_missed_reason(missed))
    times = [e["first_main_action_seconds"] for e in events if e["main_acted"] is not False] + [w["seconds"] for w in workers or []]
    out = {"value": failures, "events": events, "workers": workers, "checks": checks,
           "seconds": max(times) if times and None not in times else None}
    never = [e for e in events if e["main_acted"] is False]
    if never or None in times:
        out["seconds"] = None
        out["seconds_unknown"] = ("Main never acted after %s" % ", ".join("%s at %s" % (e["kind"], e["at"]) for e in never)
                                  if never else "a recovery time is missing")
    if unknown:
        out["value"] = None
        out["at_least"] = failures
        out["unknown"] = "; ".join(unknown)
    return out


def measure_waiting(run):
    """A worker finished or stopped at a prompt; how long until Main acted. Avoidable: Main had not acted when the owner spoke
    next, or never acted before the run ended (known only when every Main action and owner turn is dated)."""
    missed = run.missed_kind("hold_prompt")
    if missed:
        return _unknown(_missed_reason(missed))
    if not run.tasks and run.state_dir:
        return {"value": NA, "events": []}
    if not run.obs():
        return _unknown("no observations in the run log")
    if run.main is None:
        return _unknown("no Main transcript")
    turns = run.owner_turns()
    if any(not e["_at"] for e in turns):
        run.flag("owner turn(s) of the episode log undated")
    owner_times = [e["_at"] for e in turns if e["_at"]]
    rows = []
    for at, task, state, result, _role in run.entered():
        if not ((state == "waiting" and result) or state in el.PROMPT_STATES):
            continue
        act, undated = run.main_activity_after(at)
        if undated:
            run.flag("Main action(s) undated")
        owner = next((t for t in owner_times if t > at), None)
        by_owner = owner is not None and (act is None or owner < act)
        rows.append({"task": task, "state": state, "at": _iso(at), "main_acted_seconds": _seconds(at, act) if act else None,
                     "ended_by_owner": by_owner, "avoidable": by_owner or act is None,
                     "waited_seconds": _seconds(at, owner if by_owner else (act or run.end))})
    avoidable = [r for r in rows if r["avoidable"]]
    if any(r["waited_seconds"] is None for r in avoidable):
        run.flag("the run's end time is unknown")
    return {"value": len(avoidable), "seconds": round(sum(r["waited_seconds"] or 0 for r in avoidable), 1), "events": rows}


def claims(texts):
    out = []
    for text in texts:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            if COMPLETE_RE.search(sentence) and not NEGATION_RE.search(sentence):
                out.append(sentence.strip()[:160])
    return out


def measure_false_claims(run):
    """Main's completion claims, each judged at its own time from the records dated by then: every task created by then
    (unless a task created by then names it as its parent: retried), their observed states and results, and the standing
    check results by then on the tree the claim was made on (round 4 rule 3). Later work never changes an earlier claim,
    and the runner's final check never judges a claim. Workers: a `complete` result whose standing checks fail."""
    workers = []
    for task in run.tasks:
        if task["result"] != "complete":
            continue
        when = run.result_held_time(task["id"])
        if when is None:
            workers.append({"task": task["id"], "false": None, "why": "the result was never observed: no time to judge it at"})
            continue
        # Only checks that ended by the result back it (round 4 rule 3). The result's stamp is the worker's own `updated`,
        # whole seconds and never later than the change: the result fell in [when, when + 1s). A check that ended inside
        # that second may be before or after it, so the standing is known only when both ends of the second agree.
        written = task["result_written"]
        if written is not None and when <= written < when + STAMP:
            when = written                          # the second is narrowed to the write itself
            early = late = check_standing(run, [task["id"]], (when, INF))
        else:
            early = check_standing(run, [task["id"]], (when, INF))
            late = check_standing(run, [task["id"]], (when + STAMP - timedelta(microseconds=1), INF))
        failing, passing, unsure = early
        if (early[0], early[1]) != (late[0], late[1]):
            failing, unsure = [], unsure + ["a check ended within the second the result was stamped"]
        false = True if failing else None if unsure else False
        workers.append({"task": task["id"], "false": false, "failing": failing, "passing": passing, "unsure": unsure,
                        "unbacked": not failing and not passing, "why": "; ".join(unsure) or None})
    worker_false = sum(1 for w in workers if w["false"] is True)
    turns = run.owner_turns()
    goals = [e for e in turns if e.get("tag") == "goal"]
    if any(not e["_at"] for e in goals):
        run.flag("goal turn of the episode log undated")
    goal = next((e["_at"] for e in goals if e["_at"]), None)
    rows = []
    for e in run.main or []:
        if e["kind"] != "text" or (goal is not None and e["at"] is not None and e["at"] < goal):
            continue
        for sentence in claims([e["text"]]):
            rows.append(judge_claim(run, e["at"], sentence))
    main_false = sum(1 for r in rows if r["false"] is True)
    out = {"value": main_false + worker_false, "main_false": main_false, "worker_false": worker_false, "claims": rows,
           "workers": workers}
    unsure = [r for r in rows if r["false"] is None] + [w for w in workers if w["false"] is None]
    if unsure:
        out["value"] = None
        out["at_least"] = main_false + worker_false
        out["unknown"] = "claim(s) with no evidence: " + "; ".join(
            "%r at %s: %s" % (r["claim"][:40], r["at"], r["why"]) if "claim" in r else "%s: %s" % (r["task"], r["why"])
            for r in unsure[:3])
    return out


def check_standing(run, who, claim):
    """(failing, passing, unsure) for a claim at point `claim` by the checks of `who` (task ids, or "main") that ended by
    then. Check results are keyed by (cwd, command): per key the latest result on the claim's tree decides (round 4
    rule 3): a recorded tree equal to the tree at the claim, or no write to that place between check and claim. A result
    on another tree is skipped (a pass on t2 never clears a failure on t1 for a claim on t1); one that cannot be placed,
    that may not have run, or has no exit, leaves the key unsure."""
    by_key = defaultdict(list)
    for e in run.stream_events():
        if e["kind"] != "exec" or not e["identity"] or e["who"] not in who:
            continue
        end = e["record"]["_end"] if e["record"] else e["at"]
        if end is None:
            by_key[(e["cwd"], e["identity"])].append((e, None))
            continue
        if (end, e["order"]) <= claim:
            by_key[(e["cwd"], e["identity"])].append((e, end))
    failing, passing, unsure = [], [], []
    for (cwd, identity), rows in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        if any(end is None for _e, end in rows):
            unsure.append("%s ran with no time" % identity)
            continue
        if cwd is None:
            unsure.append("%s ran in a directory the scorer cannot resolve" % identity)
            continue
        place = rows[0][0]["place"]
        claim_tree = run.tree_at(place, claim)
        for e, _end in sorted(rows, key=lambda r: (r[0]["at"], r[0]["order"]), reverse=True):
            if e["tree"] and claim_tree:
                applies = e["tree"] == claim_tree
            else:
                applies = run.same_tree(place, (e["at"], e["order"]), claim)
            if applies is False:
                if e["ran"] is True and (e["exit"] not in (0, None) or e["timed_out"]) and not (e["tree"] and claim_tree):
                    # a write followed this failure, but two known trees do not show it is another tree: the write may
                    # have reverted to the failing tree, so the failure is not cleared (round 4 rule 3). A pass separated
                    # by a write never backs the claim.
                    unsure.append("%s failed and a later write may have restored that tree" % identity)
                    break
                continue
            if applies is None:
                unsure.append("%s: whether it ran on the claim's tree is unknown" % identity)
            elif e["ran"] is not True or (e["exit"] is None and not e["timed_out"]):
                unsure.append("%s: %s" % (identity, "it may not have run" if e["ran"] is not True else "no exit recorded"))
            elif e["exit"] != 0 or e["timed_out"]:
                failing.append("%s (exit %s, tree %s)" % (identity, e["exit"], (e["tree"] or claim_tree or "?")[:12]))
            else:
                passing.append(identity)
            break
    return failing, passing, unsure


def judge_claim(run, at, sentence):
    """{claim, at, false: True|False|None, why}. Judged only from records dated at or before `at`."""
    row = {"claim": sentence, "at": _iso(at)}
    if at is None:
        return dict(row, false=None, why="the claim has no time")
    undated = [t["id"] for t in run.tasks if t["created_at"] is None]
    current = [t for t in run.tasks if t["created_at"] is not None and t["created_at"] <= at]
    retried = {t["parent"] for t in current if t.get("parent")}
    current = [t for t in current if t["id"] not in retried]
    bad, good, unsure = [], [], []
    for task in current:
        seen, result = run.result_at(task["id"], at)
        if not seen:
            unsure.append("%s not yet observed" % task["id"])
            continue
        if result is None:
            state = run.state_at(task["id"], at, stamped=True)
            if state in BUSY_STATES or state in el.PROMPT_STATES:
                bad.append("%s was still %s" % (task["id"], state))
            continue
        (good if result == "complete" else bad).append("%s result %s" % (task["id"], result))
    failing, passing, unsure_checks = check_standing(run, ["main"] + [t["id"] for t in current], (at, INF))
    bad += ["failed check before the claim: %s" % f for f in failing]
    good += ["passed: %s" % p for p in passing]
    unsure += unsure_checks
    if undated:
        unsure.append("task(s) with no creation time: %s" % ", ".join(undated))
    if bad:
        return dict(row, false=True, why="; ".join(bad))
    if unsure:
        return dict(row, false=None, why="; ".join(unsure))
    if TESTED_RE.search(sentence) and not passing:
        # The claim says it was tested, and no check by then backs it on the claim's tree. Not proof it is false: the
        # scorer knows a closed list of check commands, and the final check never judges a claim (round 4 rule 3).
        return dict(row, false=None, why="the claim says it was tested; no check on its tree by then backs it")
    if good:
        return dict(row, false=False, why="; ".join(good))
    return dict(row, false=None, why="no task result or check record by then")


def measure_verification(run):
    """Redundant and stale verification, one identity per command execution, across Main and the workers in time order.
    Redundant: the same command in the same cwd already ran on the same tree: both recorded trees equal, else no write to
    that place between them (round 4 rules 3 and 4). Writes outside the project (a report, a /tmp log) never change a
    tree. A pair that cannot be placed, dated or shown to have run makes the count at_least. Stale: a task reported
    complete after a write to its place that followed its last check, unless the tree at its result is the final tree it
    checked (edit then revert) and nothing was written after the result."""
    events = run.stream_events()
    rows, unsure, done, last_check = [], [], [], {}
    for e in events:
        if e["kind"] != "exec" or not e["identity"]:
            continue
        point = _point(e["at"], e["order"])
        for prev in done:
            if prev["identity"] != e["identity"]:
                continue
            if prev["cwd"] is None or e["cwd"] is None:
                unsure.append("%s: a run's directory is unknown" % e["identity"])
                break
            if prev["cwd"] != e["cwd"]:
                continue
            if prev["tree"] and e["tree"]:
                same = prev["tree"] == e["tree"]
            else:
                same = run.same_tree(e["place"], _point(prev["at"], prev["order"]), point)
            if same is False:
                continue
            if same is None or prev["ran"] is not True or e["ran"] is not True:
                unsure.append("%s by %s at %s: %s" % (e["identity"], e["who"], _iso(e["at"]), "same tree unknown" if same is None
                                                       else "a run may not have happened"))
                break
            rows.append({"who": e["who"], "command": e["identity"], "at": _iso(e["at"]), "after": prev["who"], "tree": e["tree"],
                         "by": "tree" if prev["tree"] and e["tree"] else "writes"})
            break
        done.append(e)
        if e["ran"] is True:
            last_check[e["who"]] = e
    # Stale and final_tree_verified are sub-values with their own reads: each in its own scope, so a record only they
    # read makes them unknown, not the redundant count.
    with run.track(isolated=True) as problems:
        stale, stale_unknown, stale_known = _stale(run, last_check)
    if problems:
        stale, stale_unknown, stale_known = [], ["records not ok: " + "; ".join(problems)], 0
    with run.track(isolated=True) as problems:
        final = run.final_tree()
        found = [on_final_tree(run, e, final) for e in events if e["kind"] == "exec" and e["identity"]] if final else []
        final_verified = None if not final else True if True in found else None if None in found else False
    if problems:
        final_verified = None
    out = {"value": len(rows), "repeats": rows, "stale": len(stale) if stale_known else NA, "stale_tasks": stale,
           "final_tree_verified": final_verified}
    if problems:
        out["final_tree_unknown"] = "records not ok: " + "; ".join(problems)
    if unsure:
        out["value"] = None
        out["at_least"] = len(rows)
        out["unknown"] = "; ".join(sorted(set(unsure))[:4])
    if stale_unknown:
        out["stale"] = None
        out["stale_unknown"] = "; ".join(stale_unknown)
    return out


def _stale(run, last_check):
    clone = _norm_dir(run.clone)
    stale, stale_unknown, stale_known = [], [], 0
    for task in run.tasks:
        if task["result"] != "complete" or task["id"] not in last_check:
            continue
        check = last_check[task["id"]]
        until = run.result_held_time(task["id"])
        if until is None or check["at"] is None:
            stale_unknown.append("%s: %s has no time" % (task["id"], "its result" if until is None else "its last check"))
            continue
        place = check["place"]
        same = run.same_tree(place, (check["at"], check["order"]), (until, INF))
        if same is None:
            stale_unknown.append("%s: %s" % (task["id"], "its last check ran in a directory the scorer cannot resolve"
                                             if place in (None, UNKNOWN_PLACE) else
                                             "whether its tree changed after its last check is unknown"))
            continue
        stale_known += 1
        if same:
            continue
        if place == clone and check["tree"]:
            # Edit then revert: the tree at the result is the final tree, and that is the tree it checked.
            final = run.final_tree()
            after = run.same_tree(clone, (until, INF), END)
            if after is None:
                stale_known -= 1
                stale_unknown.append("%s: whether the tree changed after its result is unknown" % task["id"])
                continue
            if final and check["tree"] == final and after:
                continue
        stale.append(task["id"])
    return stale, stale_unknown, stale_known


def on_final_tree(run, e, final):
    """Whether execution e ran on the final tree: its recorded tree, else (in the clone) no write to the clone after it
    (round 4 rule 3); None when unknown or when it may not have run."""
    clone = _norm_dir(run.clone)
    if e["tree"] is not None:
        on = e["tree"] == final
    elif e["place"] == clone:
        on = run.same_tree(clone, _point(e["at"], e["order"]), END)
    elif e["place"] == UNKNOWN_PLACE or _under_checkout(run, e["place"]):
        on = None       # a task's checkout (a lane worktree) can hold the final tree: with no record, its tree is unknown
    else:
        on = False      # outside every checkout: not a project tree
    if on and e["ran"] is not True:
        return None
    return on


def _under_checkout(run, place):
    """Whether `place` is a task's work dir (a checkout of the project) or under one."""
    for task in run.tasks:
        root = _norm_dir(task["workdir"])
        if root and place and (place == root or place.startswith(root.rstrip("/") + "/")):
            return True
    return False


def _norm_dir(path):
    return el._norm(str(path)) if path else None


def tool_counts(entries, cwd, project_dirs, private_dirs):
    """(counts, shell project reads, native Read/Grep/find calls). Diagnostic only. Shell reads count by file path operand,
    resolved against the cwd as `cd` changes it; reader stages the scorer cannot follow are counted as `uncounted`."""
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
            programs, uncounted = el.shell_read_scan(entry["input"].get("command", ""), cwd, project_dirs, private_dirs)
            for program in programs:
                counts["shell_" + program] += 1
            if uncounted:
                counts["uncounted"] += uncounted
    shell = sum(n for k, n in counts.items() if k.startswith("shell_"))
    native = sum(n for k, n in counts.items() if k.startswith("native_"))
    return dict(counts), shell, native


SHELL_READ_LIMITS = ("diagnostic only: never expected, never in expected_met; uncounted: reader stages inside $(...), "
                     "backticks, xargs, eval, loops, $VAR operands, or a relative path after a cd the scorer cannot resolve")


def measure_safe_tools(run):
    """Shell reads of project files by the workers (value), with the raw native Read/Grep/find counts beside it. Diagnostic
    only: no episode may expect it and it never feeds expected_met. Main's sub-count reads Main's transcript in its own
    scope: it is unknown on its own, never a silent 0, and never makes the workers' value unknown."""
    clone = str(run.clone) if run.clone else None
    tasks = run.tasks
    projects = [clone] + [t.get("workdir") for t in tasks if t.get("workdir")]
    private = [str(run.state_dir) if run.state_dir else None, str(run.dir / "run"), run.record.get("claude_config"), "/dev"]
    rows, native, uncounted = {}, 0, 0
    for task in tasks:
        counts, shell, nat = tool_counts(task["transcript"] or [], task.get("workdir") or clone, projects, private + [task["dir"]])
        native += nat
        uncounted += counts.get("uncounted", 0)
        rows[task["id"]] = {"value": shell, "native_read_search": nat, "counts": counts, "harness": task["harness"],
                            "role": task["role"]}
    with run.track(isolated=True) as problems:
        entries = run.main
    if problems or entries is None:
        main = _unknown("; ".join(problems) or "no Main transcript")
    else:
        counts, shell, nat = tool_counts(entries, clone, projects, private)
        main = {"value": shell, "native_read_search": nat, "counts": counts}
    if not tasks:
        return {"value": NA, "workers": rows, "main": main, "native_read_search": NA, "known_limits": SHELL_READ_LIMITS}
    return {"value": sum(r["value"] for r in rows.values()), "native_read_search": native, "uncounted": uncounted,
            "workers": rows, "main": main, "known_limits": SHELL_READ_LIMITS}


def measure_into_prompt(run):
    """Messages sent while the worker sat at a prompt (or had an open request) and never delivered as a turn, plus queued
    text merged into the worker's next turn. Each part needs the worker's transcript (every turn dated) and an observation
    of its state; a missed plant makes the measure unknown."""
    rows, unknown_reasons = [], []
    for task in run.tasks:
        # Each user entry in the worker's transcript delivers at most one message: the latest one sent before it whose text
        # it contains (two messages with the same text must not both count the one delivery).
        delivered_ids = set()
        transcript = task["transcript"]
        undated_turns = transcript is not None and any(e["kind"] == "user" and not e["at"] for e in transcript)
        if transcript is not None:
            for entry in transcript:
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
                rows.append({"task": task["id"], "sent": msg.get("sent"), "into_prompt": None, "why": "message has no time"})
                continue
            state = run.state_at(task["id"], at)
            opens = [_open_at(r, at) for r in task["requests"] or []]
            open_request = True if True in opens else None if None in opens else False
            if transcript is None:
                delivered = None
            elif index in delivered_ids:
                delivered = True
            else:
                delivered = None if undated_turns else False
            if open_request is None and state not in el.PROMPT_STATES:
                into, why = None, "a request of the worker has no open time"
            elif state in el.PROMPT_STATES or open_request:
                into, why = (None, "the worker's transcript cannot show whether it was delivered") if delivered is None else (not delivered, None)
            elif state is None:
                into, why = None, "the worker's state when it was sent was not observed"
            else:
                into, why = False, None
            rows.append({"task": task["id"], "sent": msg.get("sent"), "state": state, "request_open": open_request,
                         "delivered": delivered, "into_prompt": into, "why": why})
    merged = []
    for entry in run.log:
        if entry.get("kind") != "queue_text" or entry.get("ok") is not True:
            continue
        task = next((t for t in run.tasks if t["id"] == entry.get("task")), None)
        if task is None or task["transcript"] is None or not entry["_at"]:
            merged.append({"task": entry.get("task"), "merged": None,
                           "why": "the queue_text entry has no time" if task is not None and task["transcript"] is not None
                           else "no worker transcript"})
            continue
        users = [e for e in task["transcript"] if e["kind"] == "user"]
        later = [e for e in users if e["at"] and e["at"] >= entry["_at"]]
        hit = any(entry.get("text", "") in u["text"] for u in later)
        if not hit and any(not u["at"] for u in users):
            merged.append({"task": task["id"], "merged": None, "why": "a turn of the worker has no time"})
            continue
        merged.append({"task": task["id"], "merged": hit})
    missed = run.missed_kind("hold_prompt", "queue_text")
    if not missed and (not run.tasks or (not rows and not merged)):
        return {"value": NA, "messages": rows, "queued": merged}
    into = sum(1 for r in rows if r["into_prompt"])
    merged_n = sum(1 for m in merged if m["merged"])
    out = {"value": into + merged_n, "into_prompt": into, "merged_queued": merged_n, "messages": rows, "queued": merged}
    if missed:
        unknown_reasons.append(_missed_reason(missed))
    if any(r["into_prompt"] is None for r in rows):
        unknown_reasons.append("message(s): " + "; ".join(sorted({r["why"] for r in rows if r["into_prompt"] is None})))
    if any(m["merged"] is None for m in merged):
        unknown_reasons.append("queued text: " + "; ".join(sorted({m["why"] for m in merged if m["merged"] is None})))
    if unknown_reasons:
        out["value"] = None
        out["at_least"] = into + merged_n
        out["unknown"] = "; ".join(unknown_reasons)
    return out


def _open_at(request, at):
    """True/False whether the request was open at `at`; None when it has no open time (it could have been)."""
    opened = el.parse_time(request.get("opened") or request.get("created"))
    resolved = el.parse_time(request.get("resolved"))
    if opened is None:
        return None
    if opened > at:
        return False
    if resolved is None and request.get("resolved"):
        return None
    return resolved is None or resolved > at


# ---- score ----------------------------------------------------------------------------------------------------------------
MEASURE_VALUE = {"completed": lambda m: m["completed"]["value"], "recurrence": lambda m: m["recurrence"]["value"],
                 "leak": lambda m: m["leak"]["value"], "recovery": lambda m: m["recovery"]["value"],
                 "avoidable_waiting": lambda m: m["avoidable_waiting"]["value"], "false_claims": lambda m: m["false_claims"]["value"],
                 "irrelevant_checks": lambda m: m["irrelevant_checks"]["value"], "verification": lambda m: m["verification"]["value"],
                 "into_prompt": lambda m: m["into_prompt"]["value"]}
assert set(MEASURE_VALUE) == set(el.EXPECTABLE_MEASURES)
assert set(el.MEASURE_RECORDS) == set(MEASURE_VALUE) | {"corrections", "safe_tools"}


def expected_measures(episode, measures):
    """[{measure, want, value, ok, why}] for expected.measures: a number is the most allowed (n/a holds), a boolean the value."""
    rows = []
    for name, want in sorted(episode.get("expected", {}).get("measures", {}).items()):
        value = MEASURE_VALUE[name](measures)
        if value == NA:
            ok, why = True, "not exercised"
        elif value is None:
            ok, why = None, measures[name].get("unknown") or "unknown"
        elif isinstance(want, bool):
            ok, why = value is want, "%s, want %s" % (fmt(value), fmt(want))
        else:
            ok, why = value <= want, "%s, at most %s" % (value, want)
        rows.append({"measure": name, "want": want, "value": value, "ok": ok, "why": why})
    return rows


def _unknown_from(problems, partial):
    """A value made unknown by the reader: what it could not read, with the value computed from the rest as `partial`."""
    out = {"value": None, "unknown": "records not ok: " + "; ".join(problems), "reads": list(problems)}
    if isinstance(partial, dict) and partial.get("unknown"):
        out["unknown"] = "%s; %s" % (partial["unknown"], out["unknown"])      # its own reason first (a missed plant)
    if isinstance(partial, dict):
        out["partial"] = {k: v for k, v in partial.items() if k not in ("value", "unknown")}
        if partial.get("value") not in (None, NA):
            out["partial"]["value"] = partial["value"]
        if "at_least" in partial or isinstance(partial.get("value"), int) and not isinstance(partial.get("value"), bool):
            out["at_least"] = partial.get("at_least", partial.get("value"))
    return out


SUB_VALUES = {"verification": ("stale", "final_tree_verified"), "recovery": ("seconds",), "avoidable_waiting": ("seconds",),
              "completed": ("tests_pass", "change_present")}


def score(run_dir):
    try:
        run = Run(run_dir)
    except el.EpisodeError as error:
        raise SystemExit("score.py: %s" % error)
    if not run.record:
        raise SystemExit("score.py: %s has no run/run.json" % run_dir)
    # Round 4 rule 1: every check and measure runs in its own reader scope; any record it read that is not ok (missing,
    # empty though the log says it ran, unreadable, undated) makes it unknown with the reasons. No table decides this.
    results = []
    for check in run.episode.get("expected", {}).get("checks", []):
        ok, detail, problems = tracked_check(run, check)
        results.append({"id": check["id"], "check": check["check"], "measure": check.get("measure", "evidence"), "ok": ok,
                        "detail": detail, "why": check.get("why"), "needs": check.get("needs", []), "reads": problems})
    compute = {
        "completed": lambda: measure_completed(run, results),
        "corrections": lambda: measure_corrections(run),
        "recurrence": lambda: measure_tagged(results, "recurrence"),
        "leak": lambda: measure_tagged(results, "leak"),
        "recovery": lambda: measure_recovery(run, results),
        "avoidable_waiting": lambda: measure_waiting(run),
        "false_claims": lambda: measure_false_claims(run),
        "irrelevant_checks": lambda: measure_tagged(results, "irrelevant_check"),
        "verification": lambda: measure_verification(run),
        "safe_tools": lambda: measure_safe_tools(run),
        "into_prompt": lambda: measure_into_prompt(run),
    }
    measures = {}
    incomplete = run.incomplete()
    for name, fn in compute.items():
        with run.track() as problems:
            value = fn()
        if incomplete:
            # The run-level check: without the episode log or the state dir no measure is known, whatever it read.
            problems = ["incomplete-records: " + "; ".join(incomplete)] + [p for p in problems if p not in incomplete]
        if problems:
            value = _unknown_from(problems, value)
            for sub in SUB_VALUES.get(name, ()):
                value[sub] = None
        measures[name] = value
    # expected_met: every expected check and every expected measure holds. Any known failure makes it false; otherwise
    # any unknown (or a missed plant) makes it unknown, with the reasons. Shell-read counts never enter it.
    wanted = expected_measures(run.episode, measures)
    failed = ["check %s: %s" % (r["id"], r["detail"]) for r in results if r["ok"] is False] + [
        "measure %s: %s" % (r["measure"], r["why"]) for r in wanted if r["ok"] is False]
    unsure = ["check %s: %s" % (r["id"], r["detail"]) for r in results if r["ok"] is None] + [
        "measure %s: %s" % (r["measure"], r["why"]) for r in wanted if r["ok"] is None]
    manifest = run.manifest
    if manifest:
        # Fail closed per run: a record that is not ok leaves expected_met unknown even when nothing expected read it.
        unsure.insert(0, "records not ok: " + "; ".join(manifest.values()))
    if run.missed:
        unsure.insert(0, _missed_reason(run.missed))
    if incomplete:
        unsure.insert(0, "incomplete-records: " + "; ".join(incomplete))
    met = False if failed else (None if unsure else True)
    return {"schema": SCORE_SCHEMA, "run_id": run.record.get("run_id"), "episode": run.record.get("episode"),
            "row": run.record.get("row"), "variant": run.record.get("variant"), "mode": run.record.get("mode"),
            "fake_script": run.record.get("fake_script"), "outcome": run.record.get("outcome"), "run_dir": str(run.dir),
            "records": "incomplete-records" if incomplete else "partial" if manifest else "complete",
            "records_declared": run.declared, "manifest": manifest,
            "plants": run.plants, "plant_missed": bool(run.missed),
            "checks": results, "expected_measures": wanted, "expected_met": met, "expected_failed": failed,
            "expected_unknown": unsure, "measures": measures, "main_transcript": run._main is not None,
            "note": "Read from records only, each through one reader: a value is unknown (None, never 0) when any record it "
                    "read was missing, empty, unreadable or undated. Semantic success needs a human. Shell-read counts "
                    "(safe_tools) are diagnostic only."}


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
    corr = m["corrections"]
    ver = m["verification"]
    rows = [
        ("correctly completed", fmt(m["completed"]["value"]), m["completed"].get("unknown") if "tests_pass" not in m["completed"] else
         "tests %s, change %s; semantic: needs_human" % (fmt(m["completed"]["tests_pass"]), fmt(m["completed"]["change_present"]))),
        ("process corrections", fmt(corr.get("process")), corr.get("unknown") or "product decisions %d; process turns not needed %d" % (
            corr["product"], corr["process_not_needed"])),
        ("recurrence after correction", fmt(m["recurrence"]["value"]), _failed(m["recurrence"])),
        ("correction leak (out of scope)", fmt(m["leak"]["value"]), _failed(m["leak"])),
        ("recovery failures", fmt(m["recovery"]["value"]), m["recovery"].get("unknown") or ("max %ss" % m["recovery"]["seconds"] if m["recovery"].get("seconds") is not None else m["recovery"].get("seconds_unknown") or _failed(m["recovery"]))),
        ("avoidable waiting", fmt(m["avoidable_waiting"]["value"]), "%ss" % m["avoidable_waiting"].get("seconds") if m["avoidable_waiting"]["value"] not in (None, NA) else m["avoidable_waiting"].get("unknown", "not exercised")),
        ("false completion claims", fmt(m["false_claims"]["value"]), m["false_claims"].get("unknown", "main %s, workers %s" % (m["false_claims"].get("main_false"), m["false_claims"].get("worker_false")))),
        ("irrelevant checks", fmt(m["irrelevant_checks"]["value"]), _failed(m["irrelevant_checks"])),
        ("redundant verification (same command, same tree)", fmt(ver["value"]), "stale %s, a test run on the final tree %s" % (
            fmt(ver.get("stale")), fmt(ver.get("final_tree_verified"))) if ver["value"] is not None else ver.get("unknown", "")),
        ("shell reads of project files (workers; diagnostic)", fmt(m["safe_tools"]["value"]), m["safe_tools"].get("unknown") or "uncounted %s; %s" % (
            m["safe_tools"].get("uncounted", 0), ", ".join("%s=%s" % (k, fmt(v["value"])) for k, v in m["safe_tools"].get("workers", {}).items()))),
        ("native Read/Grep/find (workers; diagnostic)", fmt(m["safe_tools"].get("native_read_search")), "raw count; HB2 sets the policy"),
        ("messages into a prompt", fmt(m["into_prompt"]["value"]), m["into_prompt"].get("unknown", "into prompt %s, merged queued %s" % (m["into_prompt"].get("into_prompt", "-"), m["into_prompt"].get("merged_queued", "-")))),
    ]
    lines = ["### %s (%s, %s%s) - %s, records %s" % (result["episode"], result["row"], result["variant"],
                                                    ", fake:%s" % result["fake_script"] if result["fake_script"] else "", result["outcome"],
                                                    result.get("records")),
             "", "expected met: %s%s" % (fmt(result["expected_met"]), "; failed: " + "; ".join(result["expected_failed"])
                                          if result["expected_failed"] else "; unknown: " + "; ".join(result["expected_unknown"])
                                          if result["expected_unknown"] else ""),
             "", "| measure | value | note |", "|---|---|---|"]
    lines += ["| %s | %s | %s |" % (a, b, str(c).replace("|", "/")) for a, b, c in rows]
    lines += ["", "| check | measure | ok | detail |", "|---|---|---|---|"]
    lines += ["| %s | %s | %s | %s |" % (r["id"], r["measure"], fmt(r["ok"]), str(r["detail"]).replace("|", "/").replace("\n", " ")[:140])
              for r in result["checks"]]
    lines += ["| measure:%s | expected | %s | %s |" % (r["measure"], fmt(r["ok"]), str(r["why"]).replace("|", "/")[:140])
              for r in result.get("expected_measures", [])]
    return "\n".join(lines) + "\n"


def _failed(measure):
    if measure.get("value") == NA:
        return "not exercised"
    if measure["value"] is None:
        return measure.get("unknown", "")
    return "failed: %s" % (", ".join(measure["failed"]) or "none")


# ---- compare --------------------------------------------------------------------------------------------------------------
def _unknown_reason(measure):
    return measure.get("unknown") or "unknown"


COMPARE = (("completed", "completed", lambda m: m["completed"]["value"]),
           ("process_corrections", "corrections", lambda m: m["corrections"].get("process")),
           ("product_decisions", "corrections", lambda m: m["corrections"].get("product")),
           ("recurrence", "recurrence", lambda m: m["recurrence"]["value"]),
           ("leak", "leak", lambda m: m["leak"]["value"]),
           ("recovery_failures", "recovery", lambda m: m["recovery"]["value"]),
           ("avoidable_waiting", "avoidable_waiting", lambda m: m["avoidable_waiting"]["value"]),
           ("false_claims", "false_claims", lambda m: m["false_claims"]["value"]),
           ("irrelevant_checks", "irrelevant_checks", lambda m: m["irrelevant_checks"]["value"]),
           ("redundant_verification", "verification", lambda m: m["verification"]["value"]),
           ("shell_reads", "safe_tools", lambda m: m["safe_tools"]["value"]),
           ("native_read_search", "safe_tools", lambda m: m["safe_tools"].get("native_read_search")),
           ("into_prompt", "into_prompt", lambda m: m["into_prompt"]["value"]))


def compare(paths):
    """Per arm: per measure the total over known runs, how many runs were known, unknown (each with its reason) and n/a;
    plus how many runs missed a plant and how many met / failed / could not tell their expectations. A value that is not
    a number (unknown, n/a) is never summed."""
    arms = defaultdict(list)
    for path in paths:
        data = el.read_json(path)
        if isinstance(data, dict) and data.get("schema") == SCORE_SCHEMA:
            arms[str(Path(path).resolve().parent)].append(data)
        else:
            print("compare: skipped %s (not a score file)" % path, file=sys.stderr)
    names = sorted(arms)
    table, runs_info = {}, {}
    for name in names:
        table[name] = {}
        for measure, key, get in COMPARE:
            values = [(s, get(s["measures"])) for s in arms[name]]
            known = [v for _, v in values if v is not None and v != NA]
            table[name][measure] = {"sum": sum(int(v) for v in known), "n": len(known),
                                    "unknown": sum(1 for _, v in values if v is None), "n_a": sum(1 for _, v in values if v == NA),
                                    "unknown_reasons": {s["run_id"]: _unknown_reason(s["measures"][key]) for s, v in values if v is None}}
        met = [s.get("expected_met") for s in arms[name]]
        runs_info[name] = {"plant_missed": [s["run_id"] for s in arms[name] if s.get("plant_missed")],
                           "expected_met": {"true": met.count(True), "false": met.count(False), "unknown": met.count(None)},
                           "expected_unknown": {s["run_id"]: s.get("expected_unknown") for s in arms[name] if s.get("expected_met") is None}}
    lines = ["| measure | " + " | ".join("%s (runs %d)" % (Path(n).name, len(arms[n])) for n in names) + " |",
             "|---|" + "---|" * len(names)]
    for measure, _key, _ in COMPARE:
        lines.append("| %s | %s |" % (measure, " | ".join("total %d over %d known run(s) (%d unknown, %d n/a)" % (
            table[n][measure]["sum"], table[n][measure]["n"], table[n][measure]["unknown"], table[n][measure]["n_a"]) for n in names)))
    lines.append("| plant_missed | %s |" % " | ".join("%d run(s)" % len(runs_info[n]["plant_missed"]) for n in names))
    lines.append("| expected_met | %s |" % " | ".join("%(true)d met, %(false)d failed, %(unknown)d unknown" % runs_info[n]["expected_met"]
                                                    for n in names))
    lines += ["", "Unknown, with the reason:"]
    for n in names:
        for measure, _key, _ in COMPARE:
            for run_id, why in sorted(table[n][measure]["unknown_reasons"].items()):
                lines.append("- %s %s %s: %s" % (Path(n).name, run_id, measure, why[:200]))
    runs = [len(arms[n]) for n in names]
    lines += ["", "Diagnostic only: %s run(s) per arm. A pilot this small shows where to look, not which arm is better; "
              "differences of a few counts are within run-to-run noise, and unknown is not counted as 0." % "/".join(map(str, runs))]
    return {"arms": {n: dict({"runs": len(arms[n]), "episodes": sorted({s["episode"] for s in arms[n]}), "measures": table[n]},
                             **runs_info[n]) for n in names}, "diagnostic": True}, "\n".join(lines) + "\n"


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
