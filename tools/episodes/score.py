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

Records only (round 8, owner decision 2026-11-11). The scorer never reads a Bash command:
  - The only evidence of a test run is a tp-test-run:v1 record tools/agent/test-run.sh wrote (cmd array, cwd, head, tree
    fingerprint, exit, timed_out, seconds, log, summary): a worker's TP_STATE_DIR/tasks/<id>/test-runs/<n>.json, and Main's
    records in the directory run.json names as main_test_runs (the runner gives Main a stated TMPDIR; test-run.sh writes
    beside its log there). A record's start is its file's mtime minus its seconds; its end is the mtime. A numbered record
    absent below a later one is lost.
  - Verification, repeats and staleness come from tree fingerprints. A claim is verified by a passing record whose tree is
    the final tree; a repeat is two records with the same cmd array, cwd and tree; a verification is stale when the final
    tree differs from the tree of the task's last passing record before its result. No write is detected.
  - Whatever only a Bash command could show is unknown, with the reason `ran outside test-run.sh`, never false or 0: the
    absence of a matching record when the scope made Bash calls (any Bash call may have run the command), and a failure
    followed by a Bash call (it may have been rerun). The report shows unknown apart from `not verified` (no record and no
    Bash call that could stand for one).
  - Messages come from messages.jsonl, requests from requests/ files and request.* events; never from Bash text. An
    absent messages.jsonl is no message (known limit: a lost journal cannot be told from none).
  - Shell reads of project files are not counted (safe_tools is unknown; the native and safe tool counts stay).

One owner for each record fact (round 9); every reader calls it:
  - final_tree(clone): the final tree is the scratch clone's tp_tree_fingerprint (lib/gate_record.fingerprint) computed
    when the scorer runs, after every runner step. run/final-check.json's tree is never read for a verdict (shown only as
    "tree at final check" when it differs).
  - Run.record_owner(path): a record's owner is the directory it was written in (run.json's main_test_runs: main; a task's
    test-runs/: that task). A `record: PATH` line in a tool result is a reference, never ownership: it names a record to
    read, and one it names that is absent is lost. A record outside every record directory has an unknown producer.
  - before_result(record, moment): a record backs a result or a claim only when it ended by it (record end <= result.md
    mtime, full precision; the worker's whole-second stamp when result.md was rewritten outside it, then a record ending in
    that second is undecided).
  - Run.request_ids(): request ids are enumerated only through the tracked reader of the requests/ files and the task and
    session event journals: one of them not ok makes the measure unknown.
  - record_pointers(text): the pointer path is the rest of the `record:` line (spaces included), or a JSON `record` field.
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import episode_lib as el  # noqa: E402
import gate_record  # noqa: E402  (lib/, put on sys.path by episode_lib)

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
NEVER = datetime.max.replace(tzinfo=timezone.utc)
RECORD_LINE = re.compile(r"(?m)^record: (/[^\r\n]*?\.json)[ \t\r]*$")   # test-run.sh's `record: PATH` (rule 5: spaces kept)
OUTSIDE = "ran outside test-run.sh"


def final_tree(clone):
    """(tree, why) of the scratch clone now (round 9 rule 1): tp_tree_fingerprint, through lib/gate_record.fingerprint (the
    same steps test-run.sh takes). Computed once per scoring, after every runner step; git that fails is (None, why)."""
    try:
        return gate_record.fingerprint(clone)[1] or None, None
    except (RuntimeError, OSError) as error:
        return None, str(error)[:200]


def record_pointers(text):
    """The record paths a tool result names (round 9 rule 5): the rest of each `record: ` line, spaces included, and a
    JSON result's `record` field. A pointer is a reference only (rule 2): it never says who ran the record."""
    out = RECORD_LINE.findall(text or "")
    try:
        data = json.loads(text) if text and text.lstrip().startswith("{") else None
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("record"), str) and data["record"].startswith("/"):
        out.append(data["record"])
    return out


def before_result(record, moment):
    """Round 9 rule 3, the one boundary for "before the reply": True when the record ended by the moment, False when after
    it, None when it ended inside the moment's undecided span. moment is (lo, hi): a Main claim's time (at, at); a worker
    result's result_moment."""
    lo, hi = moment
    return True if record["end"] <= lo else False if record["end"] > hi else None


def result_moment(run, task):
    """(lo, hi) when the task's result was written, or None. result.md's mtime (full precision) when it lies in the
    second of the worker's whole-second stamp of its last stop with the result; else that stamp's whole second."""
    when = run.result_held_time(task["id"])
    if when is None:
        return None
    written = task["result_written"]
    if written is not None and when <= written < when + STAMP:
        return written, written
    return when, when + STAMP - timedelta(microseconds=1)



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


class Run:
    """Everything the scorer reads, through the evidence reader."""

    def __init__(self, run_dir):
        self.dir = Path(run_dir).resolve()
        self._scopes = []
        self._cache = {}
        self.recs = []
        rec = self.dir / "run"
        self.record = el.read_json(rec / "run.json") or {}
        self.notes = []
        self.episode, self._episode_rec = self._load_episode(rec / "episode.json")
        if self.record and self._episode_rec.ok:
            # The scorer acts on the episode's checks and plants: one it cannot act on is an error, not a silent pass.
            el.validate_episode(self.episode, str(rec / "episode.json"))
        self._load_log(rec / "episode-log.jsonl")
        status, final, why = el.read_json_status(rec / "final-check.json")
        if status == OK and final.get("exit") is None and not final.get("timed_out"):
            status, why = UNREADABLE, "run/final-check.json has no exit"
        self._final_rec = self.add(Rec("final check", status, final if status == OK else {}, why))
        self.clone = Path(self.record["clone"]) if self.record.get("clone") else None
        if not self.clone or not self.clone.is_dir():
            self._clone_rec = self.add(Rec("clone", MISSING, None, str(self.clone or "not in run.json")))
        elif not (self.clone / ".git").exists():
            # The runner's clone is a git work tree; an empty or plain directory has lost what the change checks diff.
            self._clone_rec = self.add(Rec("clone", MISSING, None, "%s is not a git work tree" % self.clone))
        else:
            self._clone_rec = self.add(Rec("clone", OK, self.clone))
        if self._clone_rec.ok:
            tree, why = final_tree(self.clone)
            self._tree_rec = self.add(Rec("final tree of the clone", OK if tree else UNREADABLE, tree, why))
        else:
            self._tree_rec = self._clone_rec
        self.state_dir = Path(self.record["state_dir"]) if self.record.get("state_dir") else None
        listed = self.record.get("main_test_runs")
        self._main_dir = Path(os.path.realpath(listed)) if listed else None
        self._tasks_dir = Path(os.path.realpath(str(self.state_dir / "tasks"))) if self.state_dir else None
        self._load_main()
        tasks_dir = self.state_dir / "tasks" if self.state_dir else None
        # A task's events.jsonl is required only when this build writes task journals (some task has one): the session
        # journal holds session events (a correction recorded), so its presence says nothing about task journals.
        self._events_written = bool(tasks_dir and tasks_dir.is_dir() and any(tasks_dir.glob("*/events.jsonl")))
        self._requests_written = bool(tasks_dir and tasks_dir.is_dir() and any(p.is_dir() for p in tasks_dir.glob("*/requests")))
        self._request_recs = {}
        self._load_tasks()
        self._load_main_runs()
        status, events, why = el.read_jsonl_status(self.state_dir / "events.jsonl") if self.state_dir else (MISSING, None, "no state dir")
        if status == MISSING:
            status, why = OK, None             # the event journal is per task (interfaces.md section 3): absent by design
        self._session_rec = self.add(Rec("session events.jsonl", status, events, why))
        self._check_request_ids()
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
    def _load_episode(self, path):
        """(episode, Rec) from the run's snapshot run/episode.json (round 5 rule 6). Missing, empty or unreadable, the run is
        incomplete-records, with one exception: the original episode file run.json names is read and its sha256 equals the
        hash the run recorded; then it is used and the run's notes say so. No other fallback."""
        status, episode, why = el.read_json_status(path)
        if status == OK and not episode:
            status, why = EMPTY, "run/episode.json holds no episode"
        if status == OK:
            return episode, self.add(Rec("episode snapshot", OK, episode))
        source, want = self.record.get("episode_file"), self.record.get("episode_sha256")
        try:
            data = Path(source).read_bytes() if source else None
        except OSError:
            data = None
        if data is not None and want and hashlib.sha256(data).hexdigest() == want:
            original = el.read_json(source) or {}
            if original:
                self.notes.append("run/episode.json %s; scored by the original episode file %s, whose sha256 equals the "
                                  "hash run.json recorded (%s)" % (status, source, want[:12]))
                return original, self.add(Rec("episode snapshot", OK, original))
        reason = ("no episode_file or episode_sha256 in run.json" if not (source and want) else
                  "the original %s is unreadable" % source if data is None else
                  "the original %s does not have the recorded sha256" % source)
        return {}, self.add(Rec("episode snapshot", status, None, "%s; %s" % (why or path, reason)))

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
        for task in self._tasks:
            self._load_messages(task)

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
            r, why = _test_run(p, self.record_owner(p), values["role"])
            if why:
                bad.append(why)
            else:
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
        self._request_recs[tid] = sources["requests"][0]
        values["review_of"] = el._text(task_dir / "review-of")
        return Task(self, values, sources)

    def request_ids(self):
        """({task id: ids in its requests/ files}, {task id or None: ids the request.* events (ref) name}): the one reader of
        request ids (round 9 rule 4). Every source is read through the tracked accessors (each task's requests/ and
        events.jsonl, the session's events.jsonl), so a measure that counts requests depends on every journal that could
        name one: a journal that is not ok makes it unknown. Transcript text is never read for ids."""
        held, named = {}, defaultdict(set)
        for task in self._tasks:
            held[task["id"]] = {r.get("id") or r.get("request_id") for r in task["requests"] or [] if isinstance(r, dict)}
            for e in task["events"] or []:
                if isinstance(e, dict) and str(e.get("kind", "")).startswith("request.") and e.get("ref"):
                    named[task["id"]].add(str(e["ref"]))
        for e in self.session_events or []:
            if isinstance(e, dict) and str(e.get("kind", "")).startswith("request.") and e.get("ref"):
                named[e.get("task")].add(str(e["ref"]))
        return held, named

    def _check_request_ids(self):
        """Every request id the run's records name is read (round 5 rule 7): an id an event names with no readable
        requests/ file is a lost record: that task's requests/ is missing (a session event naming no task makes every
        task's requests/ missing: which task lost it is unknown)."""
        if not self.state_dir:
            return
        held, named = self.request_ids()
        everywhere = set().union(*held.values()) if held else set()
        for tid, ids in named.items():
            lost = sorted(i for i in ids if i not in (everywhere if tid is None or tid not in held else held[tid]))
            if not lost:
                continue
            targets = [tid] if tid in self._request_recs else [] if tid is not None else list(self._request_recs)
            for target in targets:
                rec = self._request_recs[target]
                if rec.status == OK:
                    rec.status = MISSING
                    rec.why = "request id(s) %s named by an event have no readable requests/ file" % ", ".join(lost[:3])
            if not targets and self._tasks_rec.status == OK:
                self._tasks_rec.status, self._tasks_rec.why = MISSING, "request id(s) %s named for %s have no task's file" % (
                    ", ".join(lost[:3]), tid or "no task")

    def _load_messages(self, task):
        """messages.jsonl (round 8 rule 4: the journal is the record of the messages sent to the task, never Main's Bash
        text). Absent is no message: a lost journal cannot be told from none (known limit)."""
        tid = task["id"]
        status, lines, why = el.read_jsonl_status(Path(task["dir"]) / "messages.jsonl")
        if status == MISSING:
            status, why = OK, None
        dict.__setitem__(task, "messages", lines or [])
        task._sources["messages"] = (self.add(Rec("messages.jsonl of %s" % tid, status, lines, why)),)

    def _load_main_runs(self):
        """Main's tp-test-run:v1 records and every record a tool result points at (round 9 rules 2 and 5). Main's are the
        test-run-*.json in run.json's main_test_runs (Main's stated TMPDIR/tp-tools: test-run.sh with no TP_TASK_DIR
        writes its record beside its log there). A `record: PATH` in any tool result of any transcript is a reference: the
        record it names is read and owned by where it was written (record_owner); one in a task's test-runs/ was read
        with that task. A named record that is absent is lost on its owner's source; one outside every record directory
        has an unknown producer: it could be anyone's, so every test-run reader depends on it (unknown, never dropped)."""
        listed = self.record.get("main_test_runs")
        runs, bad = [], []
        status, why = OK, None
        files = []
        if not listed:
            status, why = MISSING, "run.json names no main_test_runs (the directory Main's test-run.sh records go to)"
        elif Path(listed).is_dir():
            files = sorted(Path(listed).glob("test-run-*.json"))
        task_by_id = {t["id"]: t for t in self._tasks}
        named = set()
        for entries in [self._main or []] + [dict.get(t, "transcript") or [] for t in self._tasks]:
            for e in entries:
                named.update(os.path.realpath(p) for p in record_pointers(e.get("result_text")))
        held = {os.path.realpath(r["path"]) for t in self._tasks for r in dict.get(t, "test_runs") or []}
        unowned = []
        seen = set()
        for path in [str(f) for f in files] + sorted(named):
            real = os.path.realpath(path)
            if real in seen or real in held:
                continue
            seen.add(real)
            who = self.record_owner(real)
            task = task_by_id.get(who)
            if who not in (None, "main") and task is not None and Path(real).exists():
                continue                     # in a task's test-runs/: that task's reader read it (an unreadable one is flagged)
            if not Path(real).exists():
                why_lost = "record: %s (named in a tool result) is absent" % real
                if who == "main":
                    status, why = MISSING, why or why_lost
                elif task is not None:
                    rec = task._sources["test_runs"][0]
                    if rec.status == OK:
                        rec.status, rec.why = MISSING, why_lost
                else:
                    unowned.append((MISSING, why_lost))
                continue
            if who is None:
                unowned.append((UNREADABLE, "record: %s is in neither run.json's main_test_runs nor a task's test-runs/: its "
                                            "producer is unknown" % real))
                continue
            r, problem = _test_run(Path(real), who, None)
            if problem:
                bad.append(problem)
            else:
                runs.append(r)
        if bad:
            status, why = UNREADABLE, "; ".join(bad)
        self._main_runs_rec = self.add(Rec("Main test-runs", status, runs, why))
        self._unowned_rec = self.add(Rec("test-run records of unknown producer", unowned[0][0] if unowned else OK, None,
                                         "; ".join(w for _s, w in unowned[:3]) or None))

    # ---- record manifest (a report; scoring never reads it) ------------------------------------------------------------
    def _manifest(self):
        """{record label: problem} of every record read that is not ok."""
        return {r.label: r.problem() for r in self.recs if r.status != OK}

    def incomplete(self):
        """Problems with the records every measure needs (the episode log and the state dir): the run is incomplete-records."""
        return [r.problem() for r in (self._episode_rec, self._log_rec, self._tasks_rec) if r.status != OK]

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
        """The final tree (round 9 rule 1): final_tree(clone) computed when this Run was read; every final-tree check,
        claim standing and staleness reads only this. A clone git cannot fingerprint is unreadable (unknown)."""
        return self.use(self._tree_rec)

    def final_check_tree(self):
        """The tree run/final-check.json recorded before the runner's final test, for display only (never a verdict)."""
        return (self._final_rec.value or {}).get("tree")

    def record_owner(self, path):
        """Round 9 rule 2: who wrote a test-run record, by where it was written: "main" in run.json's main_test_runs, the
        task id in that task's test-runs/, else None (producer unknown). tp-test-run:v1 has no producer field; a pointer
        in a tool result is a reference and never moves a record to its reader."""
        real = Path(os.path.realpath(str(path)))
        if self._main_dir is not None and real.parent == self._main_dir:
            return "main"
        if self._tasks_dir is not None and real.parent.name == "test-runs" and real.parent.parent.parent == self._tasks_dir:
            return real.parent.parent.name
        return None

    def roles(self, role):
        return [t for t in self.tasks if not role or t["role"] == role]

    def owner_turns(self):
        return [e for e in self.log if e.get("kind") == "owner"]

    # ---- test-run records and Bash calls (round 8) ------------------------------------------------------------------------
    def test_runs(self, who=None):
        """Every tp-test-run:v1 record of `who` (task ids and/or "main"; None: everyone), by end time. Each is
        {who, role, at, end, cmd, cwd, tree, exit, timed_out, text, path}; text is the cmd array joined as a shell would
        quote it (what a check's pattern matches from its start)."""
        out = []
        for task in self.tasks:
            if who is None or task["id"] in who:
                out += task["test_runs"]
        if who is None or "main" in who:
            out += self.use(self._main_runs_rec) or []
        self.use(self._unowned_rec)          # a record of unknown producer could be any actor's
        return sorted(out, key=lambda r: (r["end"], r["path"]))

    def shell_calls(self, who=None, within=None):
        """The Bash calls of `who` (task ids and/or "main"; None: everyone) whose time `within(at)` accepts (an undated
        call is in every interval). Never read: only their presence matters (round 8 rule 3). Any one of them may have
        run any command outside test-run.sh, so an answer it could change is unknown with that reason."""
        streams = []
        if who is None or "main" in who:
            streams.append(("main", self.main or []))
        for task in self.tasks:
            if who is None or task["id"] in who:
                streams.append((task["id"], task["transcript"] or []))
        out = []
        for owner, entries in streams:
            for e in entries:
                if e["kind"] == "tool" and el.normalise_tool(e["name"])[0] == "shell" and (
                        within is None or e["at"] is None or within(e["at"])):
                    out.append(dict(e, who=owner))
        return out


def _test_run(path, who, role):
    """(record, problem) for one tp-test-run:v1 file: problem names what makes it unreadable (not the schema, no cmd
    array, no cwd or tree, no exit), else None. tp-test-run:v1 has no time field; it is written when the run ends: its
    mtime is the end, and mtime minus seconds the start."""
    status, raw, why = el.read_json_status(path)
    if status != OK:
        return None, why
    cmd = raw.get("cmd")
    if raw.get("schema") != "tp-test-run:v1":
        return None, "%s is not a tp-test-run:v1 record" % path
    if not isinstance(cmd, list) or not cmd or not all(isinstance(w, str) for w in cmd):
        return None, "%s has no cmd array" % path
    if not raw.get("cwd") or not raw.get("tree"):
        return None, "%s has no cwd or tree" % path
    if not isinstance(raw.get("exit"), int) and raw.get("timed_out") is not True:
        return None, "%s has no exit" % path
    try:
        end = datetime.fromtimestamp(Path(path).stat().st_mtime, timezone.utc)
        seconds = float(raw.get("seconds") or 0)
    except (OSError, TypeError, ValueError) as error:
        return None, "%s: %s" % (path, error)
    return {"who": who, "role": role, "at": end - timedelta(seconds=seconds), "end": end, "cmd": list(cmd),
            "cwd": _norm_dir(raw["cwd"]), "tree": raw["tree"], "exit": raw.get("exit"),
            "timed_out": raw.get("timed_out") is True, "text": shlex.join(cmd), "path": str(path),
            "label": raw.get("label")}, None


def _scope(run, by, role=None):
    """The actors a check's `by` (main, any, role:NAME) and `role` select: task ids, plus "main" for main and for any
    with no role."""
    if by == "main":
        return ["main"]
    want = role or (by.split(":", 1)[1] if by.startswith("role:") else None)
    ids = [t["id"] for t in run.tasks if not want or t["role"] == want]
    return ids + (["main"] if by == "any" and not want else [])


def _outside(calls, what="it"):
    """The reason an answer is unknown because Bash calls (never read) may have run `what` outside test-run.sh."""
    who = sorted({c["who"] for c in calls})
    return "unknown: %s: %d Bash call(s) by %s could have run %s" % (OUTSIDE, len(calls), ", ".join(who), what)


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

    def sent_to(task_ids, within):
        # Round 8 rule 4: the messages sent to a task are its journal (messages.jsonl), never Main's Bash text.
        ids = [task_ids] if isinstance(task_ids, str) else list(task_ids)
        return [m for t in run.tasks if t["id"] in ids for m in t["messages"]], []

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
        # Round 8 rule 1: a command ran only when a tp-test-run:v1 record shows it: the pattern matches the record's cmd
        # (joined as a shell would quote it) from its start, and the record is by an actor in scope. Bash is never read:
        # with no matching record, any Bash call in scope may have run it outside test-run.sh (unknown, rule 3).
        who = _scope(run, check.get("by", "any"), check.get("role"))
        pattern = re.compile(check["pattern"])
        runs = [r for r in run.test_runs(who) if pattern.match(r["text"])]
        if check.get("final_tree"):
            final = run.final_tree()
            if not final:
                return None, "no final tree"
            # Rule 2: on the final tree means the record's tree is the final tree's fingerprint; a timed-out run did not run
            # the command to its end.
            hits = [r for r in runs if r["tree"] == final and not r["timed_out"]]
            others = [r for r in runs if r not in hits]
            detail = "%d test-run record(s) on the final tree %s%s%s" % (
                len(hits), final[:12], ": %s by %s" % (hits[0]["text"][:60], hits[0]["who"]) if hits else "",
                "; %d matching record(s) on another tree or timed out" % len(others) if others else "")
            within = None
        else:
            hits = [r for r in runs if strict(r["at"])]
            detail = "%d matching test-run record(s)%s" % (len(hits), ": " + "; ".join(
                "%s by %s" % (r["text"][:60], r["who"]) for r in hits[:4]) if hits else "")
            within = strict
        if hits:
            return expect, detail
        calls = run.shell_calls(who, within)
        if calls:
            return None, "%s; %s" % (detail, _outside(calls))
        return not expect, detail + "; no Bash call in scope that could have run it"
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
            journal, doubts = sent_to(task["id"], window)
            unsure += doubts
            for msg in journal:
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
            journal, doubts = sent_to(task["id"], lambda at, d=done: bool(d) and at > d[0] + SKEW)
            unsure += doubts
            for msg in journal:
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
        run.tasks
        held, _named = run.request_ids()      # rule 4: every requests/ and event journal it reads is a dependency
        ids = set().union(*held.values()) if held else set()
        return len(ids) >= int(check.get("count", 2)), "%d distinct request id(s)" % len(ids)
    if kind == "no_duplicate_messages":
        dupes, unsure = [], []
        for task in run.roles(check.get("role")):
            journal, doubts = sent_to(task["id"], None)
            unsure += doubts
            seen = Counter(" ".join(m.get("text", "").split()).lower() for m in journal)
            dupes += ["%s x%d: %s" % (task["id"], n, text[:60]) for text, n in seen.items() if n > 1]
        if not dupes and unsure:
            return None, "no repeated message in the journals; but %s" % "; ".join(unsure[:3])
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
        msgs, undated, unsure = [], 0, []
        for t in run.roles(check.get("role")):
            journal, doubts = sent_to(t["id"], window)
            unsure += doubts
            for m in journal:
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
        if unsure:
            # Round 7 rule 4: a call that may have sent a message could put the count over its limit.
            return None, detail + "; but %s" % "; ".join(unsure[:3])
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
        moment = result_moment(run, task)
        if moment is None:
            workers.append({"task": task["id"], "false": None, "why": "the result was never observed: no time to judge it at"})
            continue
        # Only checks that ended by the result back it (round 4 rule 3; round 9 rule 3: before_result).
        failing, passing, unsure = check_standing(run, [task["id"]], moment)
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


def check_standing(run, who, moment):
    """(failing, passing, unsure) at a claim or result `moment` ((lo, hi), see before_result) by the test-run records of
    `who` (task ids, or "main") that ended before it (round 9 rule 3). The claim is about the final tree: only records whose
    tree is the final tree speak for it (a record on another tree neither backs nor fails it). Records are keyed by (cmd
    array, cwd); per key the latest one decides. A record ending inside the moment's undecided span may be before or after
    it: the standing is known only when both readings agree. A failure is known only when no Bash call of `who` follows it
    before the claim: one may have rerun the check outside test-run.sh (round 8 rule 3). With no record by then there is
    nothing to place on a tree and the final tree is not read."""
    runs = run.test_runs(who)
    sure = [r for r in runs if before_result(r, moment) is True]
    maybe = [r for r in runs if before_result(r, moment) is None]
    if not sure and not maybe:
        return [], [], []
    final = run.final_tree()
    if not final:
        return [], [], ["no final tree: the tree the claim is about is unknown"]
    early = _standing(run, who, sure, moment[0], final)
    if not maybe:
        return early
    late = _standing(run, who, sure + maybe, moment[1], final)
    if (early[0], early[1]) != (late[0], late[1]):
        return [], early[1], early[2] + ["a check ended within the second the result was stamped"]
    return early


def _standing(run, who, by_then, claim, final):
    keyed = defaultdict(list)
    for r in by_then:
        if r["tree"] == final:
            keyed[(tuple(r["cmd"]), r["cwd"])].append(r)
    failing, passing, unsure = [], [], []
    for _key, rows in sorted(keyed.items(), key=lambda kv: str(kv[0])):
        last = max(rows, key=lambda r: r["end"])
        if last["exit"] == 0 and not last["timed_out"]:
            passing.append(last["text"])
            continue
        after = run.shell_calls(who, lambda at, end=last["end"]: end < at <= claim)
        failed = "%s (%s, tree %s)" % (last["text"][:60], "timed out" if last["timed_out"] else "exit %s" % last["exit"],
                                       final[:12])
        if after:
            unsure.append("%s failed; %s" % (failed, _outside(after, "it again")))
        else:
            failing.append(failed)
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
    who = ["main"] + [t["id"] for t in current]
    failing, passing, unsure_checks = check_standing(run, who, (at, at))
    bad += ["failed test run on the final tree before the claim: %s" % f for f in failing]
    good += ["passed: %s" % p for p in passing]
    unsure += unsure_checks
    if undated:
        unsure.append("task(s) with no creation time: %s" % ", ".join(undated))
    if bad:
        return dict(row, false=True, why="; ".join(bad))
    if unsure:
        return dict(row, false=None, why="; ".join(unsure))
    if TESTED_RE.search(sentence) and not passing:
        # The claim says it was tested and no test-run record on the final tree backs it by then. Never false: whether it
        # was tested outside test-run.sh is unknown (rule 3) when a Bash call came before it; with none, not verified.
        calls = run.shell_calls(who, lambda t: t <= at)
        if calls:
            return dict(row, false=None, verified="unknown",
                        why="the claim says it was tested; no test-run record on the final tree by then; %s" % _outside(calls))
        return dict(row, false=None, verified="not verified",
                    why="the claim says it was tested; not verified: no test-run record on the final tree by then")
    if good:
        return dict(row, false=False, why="; ".join(good))
    return dict(row, false=None, why="no task result or test-run record by then")


VERIFICATION_LIMITS = ("repeats count tp-test-run:v1 records only (same cmd array, cwd and tree): a run outside "
                       "test-run.sh is never seen, so it is neither a repeat nor a verification")


def measure_verification(run):
    """Redundant and stale verification from tp-test-run:v1 records and tree fingerprints (round 8 rule 2), across Main and
    the workers. Redundant (value): a record with the same cmd array, cwd and tree as an earlier record. Stale: a task
    reported complete whose last passing record by then is on a tree other than the final tree. final_tree_verified:
    a passing record on the final tree (True); else unknown when any Bash call could have run the tests outside
    test-run.sh (rule 3), else False (not verified)."""
    runs = run.test_runs()
    rows, first = [], {}
    for r in runs:
        key = (tuple(r["cmd"]), r["cwd"], r["tree"])
        if key in first:
            rows.append({"who": r["who"], "command": r["text"], "cwd": r["cwd"], "at": _iso(r["at"]),
                         "after": first[key]["who"], "tree": r["tree"]})
        else:
            first[key] = r
    with run.track(isolated=True) as problems:
        stale, stale_unknown, stale_known = _stale(run, runs)
    if problems:
        stale, stale_unknown, stale_known = [], ["records not ok: " + "; ".join(problems)], 0
    final_why = None
    with run.track(isolated=True) as problems:
        final = run.final_tree()
        if not final:
            final_verified, final_why = None, "no final tree"
        elif any(r["tree"] == final and r["exit"] == 0 and not r["timed_out"] for r in runs):
            final_verified = True
        else:
            calls = run.shell_calls()
            final_verified, final_why = (None, _outside(calls, "the tests")) if calls else (
                False, "not verified: no passing test-run record on the final tree")
    if problems:
        final_verified, final_why = None, "records not ok: " + "; ".join(problems)
    out = {"value": len(rows), "repeats": rows, "records": len(runs), "stale": len(stale) if stale_known else NA,
           "stale_tasks": stale, "final_tree_verified": final_verified,
           "final_tree_state": {True: "verified", False: "not verified", None: "unknown"}[final_verified],
           "known_limits": VERIFICATION_LIMITS}
    if final_why:
        out["final_tree_unknown" if final_verified is None else "final_tree_why"] = final_why
    at_check = run.final_check_tree()
    if final and at_check and at_check != final:
        out["tree_at_final_check"] = at_check        # display only (rule 1): the clone changed after the final check
    if stale_unknown:
        out["stale"] = None
        out["stale_unknown"] = "; ".join(stale_unknown)
    return out


def _stale(run, runs):
    """(stale tasks, unknown reasons, tasks judged): a task reported complete is stale when the tree of its last passing
    record before its result (before_result, round 9 rule 3) is not the final tree. A task with no passing record before
    its result is not judged; a pass ending inside the result's undecided span that would change the answer is unknown."""
    final = run.final_tree()
    stale, stale_unknown, stale_known = [], [], 0
    for task in run.tasks:
        if task["result"] != "complete":
            continue
        passed = [r for r in runs if r["who"] == task["id"] and r["exit"] == 0 and not r["timed_out"]]
        if not passed:
            continue
        moment = result_moment(run, task)
        if moment is None:
            stale_unknown.append("%s: its result has no time" % task["id"])
            continue
        sure = [r for r in passed if before_result(r, moment) is True]
        maybe = sure + [r for r in passed if before_result(r, moment) is None]
        if not maybe:
            continue
        if not final:
            stale_unknown.append("%s: no final tree" % task["id"])
            continue
        verdicts = {None if not rows else rows[-1]["tree"] != final for rows in (sure, maybe)}
        if len(verdicts) > 1:
            stale_unknown.append("%s: a pass ended within the second the result was stamped" % task["id"])
            continue
        stale_known += 1
        if verdicts.pop():
            stale.append(task["id"])
    return stale, stale_unknown, stale_known


def _norm_dir(path):
    return el._norm(str(path)) if path else None


def tool_counts(entries):
    """(counts, native Read/Grep/find calls): calls counted by tool name only. Diagnostic only. Bash calls are counted
    (shell) but never read: which files a shell command read is not known (round 8)."""
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
            counts["shell"] += 1
    native = sum(n for k, n in counts.items() if k.startswith("native_"))
    return dict(counts), native


SHELL_READ_LIMITS = ("diagnostic only: never expected, never in expected_met. Shell reads of project files are unknown: "
                     "the scorer never reads a Bash command (round 8); the tool-name counts stay")


def measure_safe_tools(run):
    """Tool-name counts per worker and for Main, with the native Read/Grep/find total. Diagnostic only: no episode may expect
    it and it never feeds expected_met. The value (shell reads of project files) is unknown: it could only come from
    reading Bash commands (round 8 rule 3)."""
    tasks = run.tasks
    rows, native, shell = {}, 0, 0
    for task in tasks:
        counts, nat = tool_counts(task["transcript"] or [])
        native += nat
        shell += counts.get("shell", 0)
        rows[task["id"]] = {"value": None, "native_read_search": nat, "counts": counts, "harness": task["harness"],
                            "role": task["role"]}
    with run.track(isolated=True) as problems:
        entries = run.main
    if problems or entries is None:
        main = _unknown("; ".join(problems) or "no Main transcript")
    else:
        counts, nat = tool_counts(entries)
        main = {"value": None, "native_read_search": nat, "counts": counts}
    if not tasks:
        return {"value": NA, "workers": rows, "main": main, "native_read_search": NA, "known_limits": SHELL_READ_LIMITS}
    return {"value": None, "unknown": "shell reads are not read: the scorer never reads a Bash command (%d Bash call(s) "
                                      "by the workers)" % shell,
            "native_read_search": native, "shell_calls": shell, "workers": rows, "main": main,
            "known_limits": SHELL_READ_LIMITS}


def measure_into_prompt(run):
    """Messages sent while the worker sat at a prompt (or had an open request) and never delivered as a turn, plus queued
    text merged into the worker's next turn. Each part needs the worker's transcript (every turn dated) and an observation
    of its state; a missed plant makes the measure unknown."""
    rows, unknown_reasons = [], []
    for task in run.tasks:
        # Each user entry in the worker's transcript delivers at most one message: the latest one sent before it whose text
        # it contains (two messages with the same text must not both count the one delivery).
        delivered_ids = set()
        # Round 8 rule 4: the messages sent to the task are its journal (messages.jsonl).
        journal = task["messages"]
        transcript = task["transcript"]
        undated_turns = transcript is not None and any(e["kind"] == "user" and not e["at"] for e in transcript)
        if transcript is not None:
            for entry in transcript:
                if entry["kind"] != "user" or not entry["at"]:
                    continue
                said = " ".join(entry["text"].split())
                best = None
                for index, msg in enumerate(journal):
                    sent = el.parse_time(msg.get("sent"))
                    text = " ".join(str(msg.get("text", "")).split())
                    if sent and sent <= entry["at"] + SKEW and text and text[:80] in said:
                        best = index
                if best is not None:
                    delivered_ids.add(best)
        for index, msg in enumerate(journal):
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
            "expected_unknown": unsure, "measures": measures, "main_transcript": run._main is not None, "notes": run.notes,
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
        ("redundant verification (same cmd, cwd and tree)", fmt(ver["value"]), "stale %s; the final tree: %s%s" % (
            fmt(ver.get("stale")), ver.get("final_tree_state") or fmt(ver.get("final_tree_verified")),
            " (%s)" % ver["final_tree_unknown"] if ver.get("final_tree_unknown") else "")
         if ver["value"] is not None else ver.get("unknown", "")),
        ("shell reads of project files (workers; diagnostic)", fmt(m["safe_tools"]["value"]), m["safe_tools"].get("unknown") or "not exercised"),
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
