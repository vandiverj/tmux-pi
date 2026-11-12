import session_env  # noqa: F401  (first: drops the caller's tmux/task session before anything else runs)

"""HB8a episode harness: runner plan and teardown (fake mode, private tmux server), episode validation, and the scorer on
hand-made records for every measure, including the negative variants and missing telemetry (unknown, never 0).

No model is started and nothing goes to the network. The runner tests start tmux only through tools/episodes/run.py, which
uses a fresh TMUX_TMPDIR and its own `-L` socket and kills only that server."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EPISODES = ROOT / "tools" / "episodes"
sys.path.insert(0, str(EPISODES))
import episode_lib as el  # noqa: E402
import gate_record  # noqa: E402  (lib/, put on sys.path by episode_lib)
import score  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def whole(seconds):
    """A worker's own `updated` stamp: whole seconds, never later than the change."""
    return (T0 + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def bash_of(runs, exit=None):
    """The Bash calls that made these test-run records (start, seconds, record): a worker's transcript holds them. exit:
    also write each call's result (its record's exit when True)."""
    return [("tool", start, "Bash", {"command": " ".join(rec["cmd"]) if rec.get("cmd") else rec["label"]},
             rec.get("exit") if exit is True else exit) for start, _, rec in runs]


class Records:
    """A run directory written by hand: run/{run.json, episode.json, episode-log.jsonl, final-check.json}, a session state dir
    with task records, an optional Main transcript, and a clone directory marked as a work tree by an empty .git (repo()
    makes it a real git repository; a clone with no .git is a missing record)."""

    def __init__(self, root, variant="positive"):
        self.root = Path(root)
        self.state = self.root / "state"
        self.clone = self.root / "proj"
        (self.clone / ".git").mkdir(parents=True)
        self.checks, self.log, self.tasks = [], [], {}
        self.main = []              # None: no Main transcript listed; [] listed (holding only the owner turns sent)
        self.final = None
        self.variant = variant
        self.session_events = None
        self.expected = {}          # extra expected.* (measures)
        self.plants = []            # (step id, kind, extra fields): plant steps in the episode (the log says whether they happened)
        self.start_commit = None
        self.records = None         # expected.records; None: what the episode must list (el.required_records)
        self.bounds = True          # write run.start / run.end around the log (a complete runner log)
        self.main_runs = []         # (start, seconds, record): Main's tp-test-run:v1 records in run.json's main_test_runs
        self.main_runs_dir = True   # False: run.json names no main_test_runs
        # The clone's tree when the scorer runs (round 9 rule 1: score.final_tree fingerprints the clone, never reads
        # final-check.json). A hand-made clone has no git objects, so ScorerCase stands in for the fingerprint with this
        # value; None: the tree final-check.json saw (nothing changed after the final check), or unreadable without one. A
        # real repository (repo()) is fingerprinted for real.
        self.clone_tree = None

    def repo(self):
        """Make the clone a git repository with one commit of what is in it now; that commit is the run's start."""
        git = ["git", "-C", str(self.clone), "-c", "user.email=t@example.invalid", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", "start"], check=True)
        self.start_commit = subprocess.run(git + ["rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        return self

    def steps(self):
        """Episode steps for the log's step ids (so checks may name them) and the declared plants."""
        out, seen = [], set()
        for sid, kind, extra in self.plants:
            out.append(dict({"id": sid, "do": kind}, **dict({"timeout": 1} if kind == "hold_prompt" else {"text": "queued"}, **extra)))
            seen.add(sid)
        for entry in self.log:
            sid = entry.get("step")
            if not sid or sid in seen or entry["kind"] == "observe":
                continue
            seen.add(sid)
            kind = entry["kind"]
            if kind == "owner":
                out.append({"id": sid, "do": "owner", "tag": entry["tag"], "text": entry["text"]})
            elif kind == "hold_prompt":
                out.append({"id": sid, "do": "hold_prompt", "timeout": 1})
            elif kind == "queue_text":
                out.append({"id": sid, "do": "queue_text", "text": entry.get("text", "queued")})
            elif kind in ("compact", "restart.kill"):
                out.append({"id": sid, "do": "compact" if kind == "compact" else "restart_main"})
            else:
                out.append({"id": sid, "do": "note"})
        return out

    # ---- what happened -----------------------------------------------------------------------------------------------
    def step(self, seconds, step, kind="note", **fields):
        self.log.append(dict({"at": at(seconds), "kind": kind, "step": step}, **fields))

    def owner(self, seconds, step, tag="followup", text="a turn", rule=None):
        self.step(seconds, step, "owner", tag=tag, text=text, rule=rule)

    def observe(self, seconds, task, state, result=None, stamp=None, role=None):
        """stamp: the worker's `updated` in seconds (None: the poll's whole second); a string is written as is, "" leaves
        `updated` out."""
        role = role or self.tasks.get(task, {}).get("role", "coder")
        lane = self.tasks.get(task, {}).get("lane")
        entry = {"at": at(seconds), "kind": "observe", "task": task, "role": role, "lane": lane, "state": state,
                 "result": result, "tool": "", "updated": stamp if isinstance(stamp, str) else whole(seconds if stamp is None else stamp),
                 "pane": "%9"}
        if entry["updated"] == "":
            del entry["updated"]
        self.log.append(entry)

    def task(self, ident, role="coder", lane=None, created=0, packet="Do the task.", messages=(), test_runs=(), transcript=(),
             events=None, requests=None, result=None, workdir=(), parent=None):
        """workdir: () (the default) the clone; None no work dir in meta. transcript: () (the default) a transcript holding the packet as the worker's first turn; None no transcript;
        a list the entries."""
        if transcript == ():
            transcript = [("user", created, packet)]
        if workdir == ():
            workdir = self.clone
        self.tasks[ident] = {"role": role, "lane": lane, "created": created, "packet": packet, "messages": list(messages),
                             "test_runs": list(test_runs), "transcript": transcript, "events": events, "requests": requests,
                             "result": result, "workdir": workdir, "parent": parent}

    def said(self, seconds, text):
        self.main = (self.main or []) + [("text", seconds, text)]

    def main_run(self, start, seconds, record):
        """Main ran a test through tools/agent/test-run.sh with no TP_TASK_DIR: its record lands in main_test_runs."""
        self.main_runs.append((start, seconds, record))

    def ran(self, seconds, command, exit=None):
        """Main ran a Bash command; exit: also write its result (None: the transcript has no result for it)."""
        self.main = (self.main or []) + [("tool", seconds, "Bash", {"command": command}, exit)]

    # ---- write and score ---------------------------------------------------------------------------------------------
    @staticmethod
    def _transcript(path, entries):
        lines = []
        for n, entry in enumerate(entries, 1):
            if entry[0] == "tool":
                _, s, name, inp = entry[:4]
                code = entry[4] if len(entry) > 4 else None
                # entry[5]: the result's own time (round 5 rule 4); "undated": a result line with no timestamp.
                # entry[6]: the result's text (round 6 rule 5), else the exit as the harness words it.
                result_at = entry[5] if len(entry) > 5 else s
                text = entry[6] if len(entry) > 6 else "Exit code %d" % code if code else "ok"
                ident = "toolu_%d" % n
                lines.append({"type": "assistant", "timestamp": at(s), "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": ident, "name": name, "input": inp}]}})
                if code is not None:
                    line = {"type": "user", "timestamp": at(s if result_at == "undated" else result_at), "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": ident, "is_error": code != 0, "content": text}]}}
                    if result_at == "undated":
                        del line["timestamp"]
                    lines.append(line)
            elif entry[0] == "text":
                lines.append({"type": "assistant", "timestamp": at(entry[1]), "message": {"role": "assistant", "content": [
                    {"type": "text", "text": entry[2]}]}})
            elif entry[0] == "user":
                lines.append({"type": "user", "timestamp": at(entry[1]), "message": {"role": "user", "content": entry[2]}})
        Path(path).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    @staticmethod
    def _record(path, start, seconds, record, cwd):
        """One tp-test-run:v1 record as test-run.sh writes it, its mtime its end. cwd defaults to the worktree and cmd to
        the label split as a shell would (test-run.sh's default label is the cmd); a field given as None is left out."""
        record = dict({"schema": "tp-test-run:v1", "seconds": seconds, "cwd": str(cwd)}, **record)
        if "cmd" not in record and record.get("label"):
            record["cmd"] = shlex.split(record["label"])
        el.write_json(path, {k: v for k, v in record.items() if v is not None})
        end = (T0 + timedelta(seconds=start + seconds)).timestamp()
        os.utime(path, (end, end))

    def write(self):
        run = self.root / "run"
        run.mkdir(exist_ok=True)
        main_runs = run / "main-tmp" / "tp-tools"
        if main_runs.is_dir():
            shutil.rmtree(main_runs)
        for n, (start, seconds, record) in enumerate(self.main_runs, 1):
            main_runs.mkdir(parents=True, exist_ok=True)
            self._record(main_runs / ("test-run-proj-20260101-1200%02d-%d.json" % (n, n)), start, seconds, record, self.clone)
        for ident, t in self.tasks.items():
            d = self.state / "tasks" / ident
            d.mkdir(parents=True, exist_ok=True)
            meta = {"role": t["role"], "lane": t["lane"], "created": whole(t["created"]), "worker": "claude"}
            if t["workdir"]:
                meta["workdir"] = str(t["workdir"])
            if t["parent"]:
                meta["parent"] = t["parent"]
            el.write_json(d / "meta.json", meta)
            el.write_json(d / "state.json", {"state": "waiting"})
            (d / "packet.md").write_text(t["packet"], encoding="utf-8")
            if t["messages"]:
                (d / "messages.jsonl").write_text("".join(json.dumps({"sent": at(s), "text": text}) + "\n"
                                                          for s, text in t["messages"]), encoding="utf-8")
            for n, (start, seconds, record) in enumerate(t["test_runs"], 1):
                self._record(d / "test-runs" / ("%d.json" % n), start, seconds, record, t["workdir"] or self.clone)
            if t["transcript"] is not None:
                self._transcript(d / "transcript.jsonl", t["transcript"])
            if t["events"] is not None:
                (d / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in t["events"]), encoding="utf-8")
            if t["requests"] is not None:
                (d / "requests").mkdir(exist_ok=True)
                for n, r in enumerate(t["requests"], 1):
                    el.write_json(d / "requests" / ("%d.json" % n), r)
            if t["result"]:
                (d / "result.md").write_text("Done.\n\nRESULT_STATUS: %s\n" % t["result"], encoding="utf-8")
                # HB8b round 6: result.md's mtime is the last write and the result holds from then on. Not the wall clock now
                # (months after the fixture's runs): the first poll that showed it, as a real file is written before it is seen.
                seen = [el.parse_time(o["at"]) for o in self.log if o.get("task") == ident and o.get("result") == t["result"]]
                if seen:
                    os.utime(d / "result.md", (min(seen).timestamp(),) * 2)
        self.state.mkdir(parents=True, exist_ok=True)
        if self.session_events is not None:
            (self.state / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.session_events), encoding="utf-8")
        transcripts = []
        if self.main is not None:
            path = self.root / "main.jsonl"
            # Main's transcript holds each owner turn typed into it, as the real one (and the fake Main) does.
            turns = [("user", (el.parse_time(e["at"]) - T0).total_seconds(), e["text"]) for e in self.log if e["kind"] == "owner"]
            self._transcript(path, sorted(self.main + turns, key=lambda x: x[1]))
            transcripts = [str(path)]
        episode = {"schema": el.SCHEMA, "id": "T", "row": "T", "variant": self.variant, "title": "t", "project": {},
                   "steps": self.steps(), "expected": dict({"checks": self.checks}, **self.expected)}
        episode["expected"]["records"] = list(self.records) if self.records is not None else el.required_records(episode)
        el.write_json(run / "episode.json", episode)
        el.write_json(run / "run.json", {"schema": el.RUN_SCHEMA, "run_id": "T-1", "episode": "T", "row": "T",
                                         "variant": self.variant, "mode": "fake", "fake_script": "pass", "outcome": "played",
                                         "clone": str(self.clone), "state_dir": str(self.state), "main_transcripts": transcripts,
                                         "start_commit": self.start_commit,
                                         "main_test_runs": str(main_runs) if self.main_runs_dir else None})
        if self.final is not None:
            el.write_json(run / "final-check.json", self.final)
        elif (run / "final-check.json").exists():
            (run / "final-check.json").unlink()      # written again after a change: the record is gone, not stale
        log = list(self.log)
        if self.bounds:
            times = sorted(e["at"] for e in log if e.get("at"))
            log = [{"at": times[0] if times else at(0), "kind": "run.start"}] + (
                [{"at": times[0] if times else at(0), "kind": "main.ready", "ok": True}] if self.main is not None else []) + log + [
                {"at": times[-1] if times else at(0), "kind": "run.end"}]
        (run / "episode-log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log), encoding="utf-8")
        return self

    def tree(self):
        """What score.final_tree gives for this clone: (tree, why)."""
        if self.start_commit:
            return REAL_FINAL_TREE(self.clone)
        tree = self.clone_tree if self.clone_tree is not None else (self.final or {}).get("tree")
        return (tree, None) if tree else (None, "fatal: not a git repository (hand-made clone with no tree)")

    def score(self):
        self.write()
        return score.score(self.root)

    def check(self, check):
        """(ok, detail) of one check against these records."""
        self.write()
        return score.evaluate_check(score.Run(self.root), dict({"id": "c", "measure": "evidence"}, **check))


REAL_FINAL_TREE = getattr(score, "final_tree", lambda clone: (gate_record.fingerprint(clone)[1], None))
_CLONES = {}                    # clone path -> Records: the hand-made clones whose tree the fixture states


def _fixture_final_tree(clone):
    records = _CLONES.get(str(clone))
    return records.tree() if records is not None else REAL_FINAL_TREE(clone)


class ScorerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ep-score-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0
        score.final_tree = _fixture_final_tree
        self.addCleanup(setattr, score, "final_tree", REAL_FINAL_TREE)
        self.addCleanup(_CLONES.clear)

    def records(self, variant="positive", under=""):
        """under: a directory (with spaces, say) the run directory is made in."""
        self.n += 1
        r = Records(self.tmp / under / ("r%d" % self.n), variant)
        _CLONES[str(r.clone)] = r
        return r


# ---- measures -------------------------------------------------------------------------------------------------------------
class CompletedTests(ScorerCase):
    def setUp(self):
        super().setUp()

    def _base(self, final):
        r = self.records()
        (r.clone / "a.txt").write_text("quick: 20\n")
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20", "measure": "evidence"}]
        r.final = final
        return r

    def test_completed_yes_when_tests_pass_and_change_present(self):
        m = self._base({"exit": 0, "tree": "t1"}).score()["measures"]["completed"]
        self.assertIs(m["value"], True)
        self.assertEqual(m["semantic"], "needs_human")

    def test_completed_no_when_final_tests_fail(self):
        self.assertIs(self._base({"exit": 1, "tree": "t1"}).score()["measures"]["completed"]["value"], False)

    def test_unreadable_change_check_leaves_completed_unknown_even_when_tests_fail(self):
        """Round 4 rule 1 (review probe missing-change-completed): a part that was not read keeps the whole unknown."""
        r = self._base({"exit": 1, "tree": "t1"})
        r.checks = [{"id": "landed", "check": "change_present", "path": "missing.txt", "pattern": "x", "measure": "evidence"}]
        m = r.score()["measures"]["completed"]
        self.assertEqual((m["value"], m["tests_pass"], m["change_present"]), (None, False, None))

    def test_completed_unknown_without_final_check_not_zero(self):
        result = self._base(None).score()
        m = result["measures"]["completed"]
        self.assertIsNone(m["value"])
        self.assertIn("final check missing", m["unknown"])
        self.assertIn("| correctly completed | unknown |", score.markdown(result))


class CorrectionsTests(ScorerCase):
    def test_process_turns_counted_and_skipped_ones_reported(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.owner(5, "rule", "process", rule="coders-run-build")
        r.owner(9, "decide", "product")
        r.step(12, "rule2", "owner.skipped", tag="process", reason="unless held")
        m = r.score()["measures"]["corrections"]
        self.assertEqual((m["value"], m["product"], m["process_not_needed"], m["rules"]), (1, 1, 1, ["coders-run-build"]))


class TaggedMeasureTests(ScorerCase):
    def test_recurrence_held_and_leak_counted_negative_variant(self):
        r = self.records("negative")
        r.owner(5, "goal2", "goal")
        r.task("coder-1", created=10, packet="Change X. Run npm run build before you report.")
        r.task("researcher-1", role="researcher", created=12, packet="Look up labels. Run npm run build.")
        r.observe(11, "coder-1", "working")
        r.observe(13, "researcher-1", "working", role="researcher")
        r.checks = [
            {"id": "held", "check": "packet", "measure": "recurrence", "role": "coder", "after": "goal2", "pattern": "npm run build"},
            {"id": "no-leak", "check": "packet", "measure": "leak", "role": "researcher", "after": "goal2", "pattern": "npm run build",
             "expect": False}]
        m = r.score()["measures"]
        self.assertEqual(m["recurrence"]["value"], 0)
        self.assertEqual((m["leak"]["value"], m["leak"]["failed"]), (1, ["no-leak"]))
        self.assertEqual(m["irrelevant_checks"]["value"], "n/a")

    def test_tagged_measure_unknown_when_no_check_can_be_evaluated(self):
        r = self.records()
        r.main = None
        r.checks = [{"id": "x", "check": "main_said", "measure": "leak", "pattern": "paused", "expect": False}]
        m = r.score()["measures"]["leak"]
        self.assertIsNone(m["value"])
        self.assertEqual(m["unknown_checks"], ["x"])

    def test_packet_absent_role_passes_only_for_expect_false(self):
        r = self.records()
        r.owner(5, "goal2", "goal")
        self.assertEqual(r.check({"check": "packet", "role": "researcher", "after": "goal2", "pattern": "x", "expect": False})[0], True)
        self.assertIsNone(r.check({"check": "packet", "role": "researcher", "after": "goal2", "pattern": "x"})[0])


class WaitingTests(ScorerCase):
    def _run(self, main_at):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(10, "coder-1", "waiting", result="complete")
        r.owner(20, "status")
        if main_at is not None:
            r.said(main_at, "The coder has finished.")
        else:
            r.main = None
        return r.score()["measures"]["avoidable_waiting"]

    def test_owner_spoke_before_main_acted_is_avoidable(self):
        m = self._run(25)
        self.assertEqual((m["value"], m["seconds"]), (1, 10.0))

    def test_main_acted_first_is_not_avoidable(self):
        self.assertEqual(self._run(12)["value"], 0)

    def test_unknown_without_main_transcript(self):
        m = self._run(None)
        self.assertIsNone(m["value"])
        self.assertIn("records not ok: Main transcript missing (run.json lists none)", m["unknown"])


class FalseClaimTests(ScorerCase):
    def test_main_done_claim_on_incomplete_work_and_worker_failed_tests(self):
        r = self.records()
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20", "measure": "evidence"}]
        (r.clone / "a.txt").write_text("quick: 20\n")
        r.final = {"exit": 1, "tree": "t1"}
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=[(5, 1, {"label": "npm test", "tree": "t1", "exit": 1})])
        r.observe(1, "coder-1", "working")
        r.observe(7, "coder-1", "waiting", result="complete")
        r.said(10, "Done: quick matches are 20 shots.")
        r.said(11, "The README is not done yet.")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["value"], m["main_false"], m["worker_false"]), (2, 1, 1))
        self.assertEqual([c["claim"] for c in m["claims"]], ["Done: quick matches are 20 shots."])
        self.assertIn("exit 1", m["claims"][0]["why"])

    def test_unknown_without_main_transcript(self):
        r = self.records()
        r.main = None
        r.final = {"exit": 0, "tree": "t1"}
        self.assertIsNone(r.score()["measures"]["false_claims"]["value"])


class VerificationTests(ScorerCase):
    def test_repeat_without_edit_counted_stale_and_exact_duplicates(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        transcript = [("tool", 1, "Bash", {"command": "npm test"}), ("tool", 2, "Bash", {"command": "npm run test"}),
                      ("tool", 3, "Edit", {"file_path": "a"}, 0), ("tool", 4, "Bash", {"command": "npm test"})]
        runs = [(1, 0.5, {"label": "npm test", "tree": "t0", "exit": 0}), (2, 0.5, {"label": "npm test", "tree": "t0", "exit": 0})]
        r.task("coder-1", created=0, result="complete", transcript=transcript, test_runs=runs, workdir=r.clone)
        r.observe(0.5, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")
        m = r.score()["measures"]["verification"]
        # Records 1 and 2: same cmd array, cwd and tree t0: one repeat. The call at 4 wrote no record: never seen (rule 1).
        self.assertEqual((m["value"], m["records"], [x["tree"] for x in m["repeats"]]), (1, 2, ["t0"]))
        # The last pass ran on t0, the final tree is t1: stale. No passing record on t1, and Bash calls could have run the
        # tests outside test-run.sh: the final tree's state is unknown, not "not verified" (rule 3).
        self.assertEqual((m["stale"], m["stale_tasks"], m["final_tree_verified"], m["final_tree_state"]),
                         (1, ["coder-1"], None, "unknown"))
        self.assertIn("ran outside test-run.sh", m["final_tree_unknown"])

    def test_unknown_without_records(self):
        r = self.records()
        r.task("coder-1", created=0, transcript=None)
        m = r.score()["measures"]["verification"]
        # Repeats are a count of records (none: 0), but HB8b K1: the record inventory is read from every transcript's
        # pointers, so with coder-1's transcript lost the count is at least 0, unknown; the final tree's state cannot be
        # known without the final check.
        self.assertEqual((m["value"], m["at_least"], m["partial"]["final_tree_state"]), (None, 0, "unknown"))
        self.assertIn("transcript of coder-1 missing", m["unknown"])
        r.main_runs_dir = False                      # Main's records cannot be read: the count is unknown, not 0
        self.assertIsNone(r.score()["measures"]["verification"]["value"])


class SafeToolTests(ScorerCase):
    def test_native_reads_counted_safe_and_private_ones_not(self):
        """Round 8 rule 3: shell reads of project files would come from reading Bash: unknown, with the reason. The tool-name
        counts stay (diagnostic)."""
        r = self.records()
        transcript = [("tool", 1, "Read", {"file_path": "src/a.ts"}), ("tool", 2, "mcp__tmux-pi-safe__safe_read", {"path": "src/a.ts"}),
                      ("tool", 3, "Bash", {"command": "cat src/a.ts | head -5"}), ("tool", 4, "Bash", {"command": "cat /tmp/x.log"}),
                      ("tool", 5, "pi_tool", {})]
        r.task("coder-1", created=0, transcript=transcript)
        r.task("coder-2", created=0, transcript=None)
        r.observe(1, "coder-1", "working")
        r.observe(1, "coder-2", "working")
        m = r.score()["measures"]["safe_tools"]
        self.assertIsNone(m["value"])
        self.assertIn("transcript of coder-2 missing", m["unknown"])
        r.tasks["coder-2"]["transcript"] = [("user", 0, "Do the task.")]
        m = r.score()["measures"]["safe_tools"]
        self.assertIsNone(m["value"])
        self.assertIn("the scorer never reads a Bash command (2 Bash call(s)", m["unknown"])
        w = m["workers"]["coder-1"]
        self.assertEqual((w["value"], w["native_read_search"], w["counts"]["shell"], w["counts"]["safe_read"]), (None, 1, 2, 1))

    def test_main_count_is_unknown_without_main_transcript_and_workers_stay_known(self):
        """Round-3 review F10: with no Main transcript, Main's diagnostic counts are unknown, never a silent 0; the workers'
        tool-name counts are not Main's and stay known."""
        r = self.records()
        r.main = None
        r.task("coder-1", created=0, transcript=[("tool", 1, "Bash", {"command": "cat src/a.ts"}, 0)])
        r.observe(1, "coder-1", "working")
        m = r.score()["measures"]["safe_tools"]
        self.assertEqual((m["value"], m["main"]["value"], m["workers"]["coder-1"]["counts"]), (None, None, {"shell": 1}))

    def test_all_unknown_is_unknown(self):
        r = self.records()
        r.task("coder-1", created=0, transcript=None)
        self.assertIsNone(r.score()["measures"]["safe_tools"]["value"])


class SafeToolNeverExpectedTests(unittest.TestCase):
    def test_never_expected(self):
        """Moved from ShellReadDiagnosticTests (round 7): the diagnostic stays out of expected."""
        self.assertNotIn("safe_tools", el.EXPECTABLE_MEASURES)
        episode = {"schema": el.SCHEMA, "id": "x", "row": "E1", "variant": "positive", "title": "t", "project": {},
                   "steps": [{"id": "goal", "do": "owner", "tag": "goal", "text": "Do it."}],
                   "expected": {"records": list(el.BASE_RECORDS), "checks": [{"id": "c", "check": "main_said", "pattern": "ok"}],
                                "measures": {"safe_tools": 0}}}
        with self.assertRaisesRegex(el.EpisodeError, "expected measure safe_tools"):
            el.validate_episode(episode)


class IntoPromptTests(ScorerCase):
    def test_message_at_prompt_not_delivered_counts_delivered_does_not(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480."), (21, "Use 650.")],
               transcript=[("user", 21.5, "Use 650.")])
        r.observe(1, "coder-1", "working")
        r.observe(10, "coder-1", "permission")
        r.observe(20, "coder-1", "input")
        m = r.score()["measures"]["into_prompt"]
        self.assertEqual(m["value"], 1)
        self.assertEqual([x["into_prompt"] for x in m["messages"]], [True, False])

    def test_message_while_working_is_not_into_a_prompt(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Also add a test.")])
        r.observe(10, "coder-1", "working")
        self.assertEqual(r.score()["measures"]["into_prompt"]["value"], 0)

    def test_unknown_without_observations(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480.")], transcript=[])
        m = r.score()["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("observations of coder-1 missing", m["unknown"])


class RecoveryTests(ScorerCase):
    def _run(self, main=True, unblocked=True):
        r = self.records()
        r.step(10, "compact", "compact")
        if main:
            r.said(13, "Back after compaction; the coder is still working.")
        else:
            r.main = None
        r.task("coder-1", created=0)
        r.observe(1, "coder-1", "working")
        r.observe(20, "coder-1", "permission")
        if unblocked:
            r.observe(22, "coder-1", "working")
        r.step(40, "end", "teardown")
        return r.score()["measures"]["recovery"]

    def test_main_acted_after_compact_and_worker_left_prompt(self):
        m = self._run()
        self.assertEqual(m["value"], 0)
        self.assertEqual(m["events"][0]["first_main_action_seconds"], 3.0)
        self.assertEqual(m["workers"][0]["seconds"], 2.0)

    def test_worker_never_left_prompt_is_a_failure(self):
        m = self._run(unblocked=False)
        self.assertEqual(m["value"], 1)
        self.assertEqual(m["workers"][0]["seconds"], 20.0)

    def test_unknown_without_main_transcript(self):
        m = self._run(main=False)
        self.assertIsNone(m["value"])
        self.assertIn("Main transcript missing", m["unknown"])


# ---- checks -----------------------------------------------------------------------------------------------------------
class StrictWindowTests(ScorerCase):
    def test_main_reply_before_the_turn_is_not_its_answer(self):
        r = self.records()
        r.owner(10, "status")
        r.said(9.5, "Nothing is blocking either lane.")
        check = {"check": "main_said", "after": "status", "pattern": "Nothing is blocking"}
        self.assertIs(r.check(check)[0], False)
        r.said(10.5, "Nothing is blocking either lane.")
        self.assertIs(r.check(check)[0], True)

    def test_main_said_unknown_without_transcript_and_step_never_ran(self):
        r = self.records()
        r.main = None
        self.assertIsNone(r.check({"check": "main_said", "pattern": "x"})[0])
        r.said(1, "x")
        ok, detail = r.check({"check": "main_said", "after": "nope", "pattern": "x"})
        self.assertIsNone(ok)
        self.assertIn("never ran", detail)

    def test_test_run_dated_by_mtime_minus_seconds(self):
        r = self.records()
        r.owner(10, "goal2", "goal")
        # Ended at 11 after 3 s: started at 8, before goal2. Without the start date it would count as run after goal2.
        r.task("coder-1", created=0, transcript=None, test_runs=[(8, 3, {"label": "npm run build", "tree": "t", "exit": 0})])
        check = {"check": "command", "by": "role:coder", "after": "goal2", "pattern": "npm run build"}
        ok, detail = r.check(check)
        self.assertIsNone(ok)            # no transcript: an absent command is not proven absent
        self.assertIn("transcript of coder-1 missing", detail)
        r.tasks["coder-1"]["transcript"] = bash_of(r.tasks["coder-1"]["test_runs"])   # known: the only build began before goal2
        r.observe(1, "coder-1", "working")
        self.assertIs(r.check(check)[0], False)
        r.tasks["coder-1"]["test_runs"].append((12, 3, {"label": "npm run build", "tree": "t", "exit": 0}))
        r.tasks["coder-1"]["transcript"] = bash_of(r.tasks["coder-1"]["test_runs"])
        self.assertIs(r.check(check)[0], True)

    def test_command_with_transcript_not_counted_twice(self):
        r = self.records()
        r.owner(10, "goal2", "goal")
        r.task("coder-1", created=0, transcript=[("tool", 12, "Bash", {"command": "npm run build"})],
               test_runs=[(12, 1, {"label": "npm run build", "tree": "t", "exit": 0})])
        ok, detail = r.check({"check": "command", "by": "role:coder", "after": "goal2", "pattern": "npm run build"})
        self.assertIs(ok, True)
        self.assertTrue(detail.startswith("1 matching test-run record(s): npm run build by coder-1"), detail)


class FinalTreeTests(ScorerCase):
    CHECK = {"check": "command", "by": "any", "pattern": "npm (run )?test", "final_tree": True}

    def test_worker_test_run_on_final_tree(self):
        """Also round 8's test_pass_on_the_final_tree_is_verified (folded here): verified, not stale, backs the claim."""
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal"}
        r.main = []
        r.owner(0, "goal", "goal")
        runs = [(5, 1, {"label": "npm test", "tree": "tfinal", "exit": 0})]
        r.task("coder-1", created=0, transcript=bash_of(runs), test_runs=runs, result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(20, "coder-1", "waiting", result="complete")
        r.said(25, "Done: the tests pass.")
        self.assertIs(r.check(self.CHECK)[0], True)
        s = r.score()
        v, fc = s["measures"]["verification"], s["measures"]["false_claims"]
        self.assertEqual((v["final_tree_verified"], v["final_tree_state"], v["stale"], v["value"]), (True, "verified", 0, 0))
        self.assertEqual((fc["value"], fc["claims"][0]["false"], fc["workers"][0]["passing"]), (0, False, ["npm test"]))

    def test_round9_final_tree_is_the_clone_fingerprinted_when_scoring(self):
        """Round 9 rule 1 (review F1, fail-before on 492650f): final-check.json's tree was taken before the runner's final
        test; the clone changed after it (a generated report). The scorer fingerprints the clone itself: the pass on the
        final-check tree is on an earlier tree (not verified, stale, the claim not backed), and the old tree is only shown.
        A clone git cannot fingerprint is unknown, never not verified."""
        r = self.records()
        (r.clone / "a.txt").write_text("a\n")
        shutil.rmtree(r.clone / ".git")
        r.repo()
        tested = REAL_FINAL_TREE(r.clone)[0]
        r.final = {"exit": 0, "tree": tested}
        r.main = []
        r.owner(0, "goal", "goal")
        runs = [(5, 1, {"label": "npm test", "tree": tested, "exit": 0})]
        # No transcript: a Bash call could have tested outside test-run.sh (unknown, round 8 rule 3); records only here.
        r.task("coder-1", created=0, test_runs=runs, result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(20, "coder-1", "waiting", result="complete")
        r.said(25, "Done: the tests pass.")
        v = r.score()["measures"]["verification"]
        self.assertEqual((v["final_tree_verified"], v["stale"]), (True, 0))
        (r.clone / "report.txt").write_text("generated by the final test\n")
        s = r.score()
        v, fc = s["measures"]["verification"], s["measures"]["false_claims"]
        self.assertEqual((v["final_tree_verified"], v["stale"], v.get("tree_at_final_check")), (False, 1, tested))
        self.assertIs(r.check(self.CHECK)[0], False)
        self.assertEqual((fc["workers"][0]["passing"], fc["claims"][0]["false"]), ([], None))
        (r.clone / ".git" / "HEAD").write_text("garbage\n")            # git cannot read the clone: unknown
        s = r.score()
        v = s["measures"]["verification"]
        self.assertEqual((v["final_tree_verified"], v["stale"]), (None, None))
        self.assertIn("final tree of the clone", s["manifest"])
        self.assertIsNone(r.check(self.CHECK)[0])

    def test_main_run_with_no_write_after_it_is_on_the_final_tree(self):
        """Round 8 rules 2 and 5: Main's record (test-run.sh with no TP_TASK_DIR, in run.json's main_test_runs) on the
        final tree's fingerprint. A plain Bash `npm test` by Main is never read: unknown, not on the final tree."""
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        runs = [(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})]
        r.task("coder-1", created=0, transcript=bash_of(runs), test_runs=runs, workdir=r.root / "lane")
        r.observe(1, "coder-1", "working")
        r.ran(31, "npm test")
        ok, detail = r.check(self.CHECK)
        self.assertIsNone(ok)
        self.assertIn("ran outside test-run.sh", detail)
        r.main_run(31, 1, {"label": "npm test", "tree": "tfinal", "exit": 0})
        ok, detail = r.check(self.CHECK)
        self.assertIs(ok, True)
        self.assertIn("on the final tree tfinal: npm test by main", detail)

    def test_clean_final_state_is_not_proof_of_the_tested_tree(self):
        """Round 8 rule 2: only a record's tree places a run on the final tree; a clean final state or a commit time does
        not. With no Bash call that could have run it elsewhere, a record on another tree is a known no. Also round 8's
        test_pass_on_an_earlier_tree_is_stale (folded here): stale and not verified."""
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        runs = [(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})]
        r.task("coder-1", created=0, transcript=[("user", 0, "Do the task.")], test_runs=runs, workdir=r.root / "lane",
               result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(20, "coder-1", "waiting", result="complete")
        ok, detail = r.check(self.CHECK)
        self.assertIs(ok, False)
        self.assertIn("1 matching record(s) on another tree", detail)
        v = r.score()["measures"]["verification"]
        self.assertEqual((v["stale"], v["stale_tasks"], v["final_tree_verified"], v["final_tree_state"]),
                         (1, ["coder-1"], False, "not verified"))
        self.assertIn("not verified: no passing test-run record on the final tree", v["final_tree_why"])

    def test_unknown_without_fingerprint_or_records(self):
        r = self.records()
        self.assertIsNone(r.check(self.CHECK)[0])
        r.final = {"exit": 0, "tree": "tfinal"}
        r.task("coder-1", created=0, transcript=None)            # no transcript, no test-run: cannot tell
        self.assertIsNone(r.check(self.CHECK)[0])


class LaneTests(ScorerCase):
    def _lanes(self, b_start, b_lane="lane-b", b_packet="Update the README line."):
        r = self.records()
        r.task("coder-a", lane="lane-a", created=9, packet="Derby delay 650.")
        r.task("coder-b", lane=b_lane, created=b_start, packet=b_packet)
        r.observe(10, "coder-a", "working")
        r.observe(20, "coder-a", "waiting", result="complete")
        r.observe(b_start + 0.1, "coder-b", "working")
        r.observe(b_start + 10, "coder-b", "waiting", result="complete")
        r.step(60, "end", "teardown")
        return r

    def test_overlap_and_serialised(self):
        self.assertIs(self._lanes(15).check({"check": "tasks_overlap", "lanes": ["lane-a", "(?i)README"]})[0], True)
        self.assertIs(self._lanes(21).check({"check": "tasks_overlap", "lanes": ["lane-a", "(?i)README"]})[0], False)

    def test_ambiguous_or_same_lane_selector_is_unknown(self):
        r = self._lanes(15, b_packet="Derby README.")
        ok, detail = r.check({"check": "tasks_overlap", "lanes": ["(?i)derby", "lane-b"]})
        self.assertIsNone(ok)
        self.assertIn("matches lanes", detail)
        ok, detail = self._lanes(15).check({"check": "tasks_overlap", "lanes": ["lane-a", "(?i)derby"]})
        self.assertIsNone(ok)
        self.assertIn("both selectors", detail)
        self.assertIsNone(self._lanes(15).check({"check": "tasks_overlap", "lanes": ["lane-a", "nothing"]})[0])

    def test_start_gap_after_dependency(self):
        check = {"check": "start_gap", "lane": "lane-b", "after_lane": "lane-a", "within": 300}
        self.assertIs(self._lanes(25).check(check)[0], True)
        self.assertIs(self._lanes(15).check(check)[0], False)


class PauseTests(ScorerCase):
    def _run(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1)
        r.observe(1, "coder-1", "working")
        r.owner(11, "pause")
        r.observe(11.2, "coder-1", "tool")
        # The worker stopped at 14 (its own stamp); the runner's poll saw it only at 14.4.
        r.observe(14.4, "coder-1", "permission", stamp=14)
        r.checks = [{"id": "pause-backed", "check": "pause_backed", "measure": "recovery", "role": "coder", "after": "pause"}]
        return r

    def test_claim_after_worker_stopped_but_before_poll_is_backed(self):
        r = self._run()
        r.said(14.2, "Pausing it. Paused: it is stopped at a prompt.")
        r.write()
        run = score.Run(r.root)
        self.assertEqual(run.state_at("coder-1", T0 + timedelta(seconds=14.2)), "tool")      # the poll time lags
        self.assertEqual(run.state_at("coder-1", T0 + timedelta(seconds=14.2), stamped=True), "permission")
        self.assertIs(r.check(r.checks[0])[0], True)

    def test_claim_while_worker_busy_is_false(self):
        r = self._run()
        r.said(12, "Paused.")
        ok, detail = r.check(r.checks[0])
        self.assertIs(ok, False)
        self.assertIn("while coder-1 was tool", detail)
        r.observe(20, "coder-1", "working")          # the prompt was left: the false pause is the only recovery failure
        self.assertEqual(r.score()["measures"]["recovery"]["value"], 1)

    def test_conditional_or_negated_pause_is_not_a_claim(self):
        r = self._run()
        r.said(12, "I'll confirm once it has paused. It is not paused yet.")
        ok, detail = r.check(r.checks[0])
        self.assertIs(ok, True)
        self.assertIn("0 pause claim", detail)

    def test_pause_confirmed_events_used_when_present(self):
        r = self._run()
        r.main = []
        r.tasks["coder-1"]["events"] = [{"kind": "pause.confirmed", "at": at(12)}]
        ok, detail = r.check(r.checks[0])
        self.assertIs(ok, False)
        r.tasks["coder-1"]["events"] = [{"kind": "pause.confirmed", "at": at(14.5)}]
        self.assertIs(r.check(r.checks[0])[0], True)

    def test_unknown_without_observations_or_transcript(self):
        r = self.records()
        r.main = None
        r.owner(11, "pause")
        self.assertIsNone(r.check({"check": "pause_backed", "role": "coder", "after": "pause"})[0])
        r.task("coder-1", created=1)
        r.observe(1, "coder-1", "working")
        ok, detail = r.check({"check": "pause_backed", "role": "coder", "after": "pause"})
        self.assertIsNone(ok)
        self.assertIn("Main transcript missing", detail)


class MissingTelemetryTests(ScorerCase):
    def test_hb0_events_and_hb4_requests_unknown_on_this_build(self):
        r = self.records()
        r.task("coder-1", created=0)
        self.assertIsNone(r.check({"check": "event", "event": "pause.confirmed"})[0])
        self.assertIsNone(r.check({"check": "requests_distinct", "count": 2})[0])
        r.tasks["coder-1"]["requests"] = [{"id": "q1"}, {"id": "q2"}]
        self.assertIs(r.check({"check": "requests_distinct", "count": 2})[0], True)
        r.tasks["coder-1"]["events"] = [{"kind": "pause.confirmed", "at": at(3)}]
        self.assertIs(r.check({"check": "event", "event": "pause.confirmed"})[0], True)

    def test_state_and_message_checks(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480."), (21, "use  480.")])
        r.observe(1, "coder-1", "working")
        r.observe(10, "coder-1", "permission")
        r.observe(12, "coder-1", "working")
        r.observe(20, "coder-1", "permission")
        self.assertIs(r.check({"check": "state_seen", "role": "coder", "state": "permission", "count": 2})[0], True)
        self.assertIs(r.check({"check": "state_seen", "role": "coder", "state": "permission", "count": 3})[0], False)
        self.assertIs(r.check({"check": "message_count", "role": "coder", "max": 1})[0], False)
        ok, detail = r.check({"check": "no_duplicate_messages", "role": "coder"})
        self.assertIs(ok, False)
        self.assertIn("x2", detail)
        self.assertIsNone(r.check({"check": "message_count", "role": "researcher", "max": 1})[0])

    def test_change_checks_unknown_without_clone(self):
        r = self.records()
        shutil.rmtree(r.clone)
        self.assertIsNone(r.check({"check": "change_present", "path": "a", "pattern": "x"})[0])


class CompareTests(ScorerCase):
    def test_compare_counts_known_only_and_says_diagnostic(self):
        r = self.records()
        (r.clone / "a.txt").write_text("quick: 20\n")
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20", "measure": "evidence"}]
        r.final = {"exit": 0, "tree": "t"}
        known = r.score()
        r.final = None
        unknown = r.score()
        for arm, results in (("A", [known, unknown]), ("B", [known])):
            for n, result in enumerate(results):
                el.write_json(self.tmp / arm / ("%d.json" % n), dict(result, run_id="%s%d" % (arm, n)))
        (self.tmp / "A" / "junk.json").write_text("{}")
        data, text = score.compare(sorted(str(p) for p in self.tmp.glob("[AB]/*.json")))
        self.assertTrue(data["diagnostic"])
        self.assertIn("Diagnostic only: 2/1 run(s) per arm", text)
        self.assertIn("total 1 over 1 known run(s) (1 unknown, 0 n/a)", text)
        self.assertIn("unknown is not counted as 0", text)

    def test_score_cli_writes_json_and_markdown(self):
        r = self.records().write()
        self.assertEqual(score.main([str(r.root), "--quiet"]), 0)
        self.assertEqual(el.read_json(r.root / "run" / "score.json")["schema"], score.SCORE_SCHEMA)
        self.assertIn("| measure | value | note |", (r.root / "run" / "score.md").read_text())


# ---- a measure is known only when every record it needs exists and refers to the same thing (HB8a round 2) -------------
class PlantMissedTests(ScorerCase):
    """F2: a planted condition that did not happen makes what depends on it unknown, never a pass."""

    def _run(self, logged):
        r = self.records()
        r.main = []
        r.owner(0, "goal", "goal")
        r.plants = [("prompt", "hold_prompt", {"role": "coder"}), ("draft", "queue_text", {"role": "coder"})]
        if logged:
            r.step(2, "prompt", "hold_prompt", ok=False, reason="no live coder task reached permission|input within 1s")
        r.task("coder-1", created=1)
        r.observe(1, "coder-1", "working")
        r.observe(5, "coder-1", "waiting", result="complete")
        r.checks = [{"id": "prompt-seen", "check": "state_seen", "role": "coder", "state": "permission", "needs": ["prompt"]},
                    {"id": "prompt-left", "check": "recovered", "measure": "recovery", "role": "coder", "needs": ["prompt"]}]
        return r

    def test_missed_plant_makes_its_checks_and_measures_unknown(self):
        for logged in (True, False):          # logged as not done, or never reached because the run stopped first
            result = self._run(logged).score()
            self.assertTrue(result["plant_missed"], logged)
            self.assertEqual({c["id"]: c["ok"] for c in result["checks"]}, {"prompt-seen": None, "prompt-left": None})
            self.assertIn("plant-missed: prompt (hold_prompt)", result["checks"][0]["detail"])
            m = result["measures"]
            for name in ("into_prompt", "recovery", "avoidable_waiting"):
                self.assertIsNone(m[name]["value"], name)
                self.assertIn("plant-missed", m[name]["unknown"], name)
            self.assertIsNone(result["expected_met"])
            self.assertIn("plant-missed", result["expected_unknown"][0])

    def test_plant_done_scores_normally_and_compare_counts_missed(self):
        r = self._run(False)
        r.step(2, "prompt", "hold_prompt", ok=True, task="coder-1")
        r.step(3, "draft", "queue_text", ok=True, task="coder-1", text="queued")
        r.observe(2, "coder-1", "permission")
        done = r.score()
        self.assertFalse(done["plant_missed"])
        self.assertEqual(done["checks"][0]["ok"], True)
        missed = self._run(True).score()
        el.write_json(self.tmp / "A" / "1.json", dict(done, run_id="A1"))
        el.write_json(self.tmp / "A" / "2.json", dict(missed, run_id="A2"))
        data, text = score.compare([str(self.tmp / "A" / "1.json"), str(self.tmp / "A" / "2.json")])
        arm = data["arms"][str((self.tmp / "A").resolve())]
        self.assertEqual(arm["plant_missed"], ["A2"])
        self.assertEqual(arm["expected_met"]["unknown"], 2 - arm["expected_met"]["true"] - arm["expected_met"]["false"])
        self.assertIn("plant-missed", arm["measures"]["into_prompt"]["unknown_reasons"]["A2"])
        self.assertIn("| plant_missed | 1 run(s) |", text)
        self.assertIn("A2 into_prompt: plant-missed", text)


class UnknownTelemetryTests(ScorerCase):
    """F3, F9: an absence claim needs every stream; an unknown part never adds a known 0."""

    def test_absence_needs_every_worker_transcript(self):
        r = self.records()
        r.main = []
        r.task("coder-1", transcript=None)
        ok, detail = r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-1 missing", detail)
        r.tasks["coder-1"]["transcript"] = []
        self.assertIsNone(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0])  # never seen run
        r.observe(1, "coder-1", "working")      # seen running with an empty transcript: not ok either (round 4 rule 1)
        self.assertIsNone(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0])
        r.tasks["coder-1"]["transcript"] = [("user", 0, "Do the task.")]
        self.assertIs(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0], True)

    def test_queued_text_without_transcript_is_unknown_and_not_counted_known(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", transcript=None)
        r.observe(1, "coder-1", "working")
        r.task("coder-2")
        r.step(2, "draft", "queue_text", ok=True, task="coder-1", text="queued draft")
        result = r.score()
        m = result["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("transcript of coder-1 missing", m["unknown"])
        el.write_json(self.tmp / "A" / "1.json", dict(result, run_id="A1"))
        data, _ = score.compare([str(self.tmp / "A" / "1.json")])
        row = data["arms"][str((self.tmp / "A").resolve())]["measures"]["into_prompt"]
        self.assertEqual((row["n"], row["unknown"]), (0, 1))

    def test_message_into_prompt_without_transcript_is_unknown_not_true(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480.")], transcript=None)
        r.observe(10, "coder-1", "permission")
        m = r.score()["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("transcript of coder-1 missing", m["unknown"])


class ClaimEvidenceTests(ScorerCase):
    """F4: a completion claim is judged by the evidence when it was made."""

    def test_done_claim_over_a_blocked_result_is_false(self):
        r = self.records()
        (r.clone / "a.txt").write_text("quick: 20\n")
        r.final = {"exit": 0, "tree": "t1"}
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20"}]
        r.task("coder-1", result="blocked")
        r.observe(5, "coder-1", "waiting", result="blocked")
        r.said(10, "Done: everything is complete.")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["value"], m["main_false"]), (1, 1))
        self.assertIn("result blocked", m["claims"][0]["why"])

    def test_true_first_step_claim_stays_true_when_a_later_step_fails(self):
        r = self.records()
        r.final = {"exit": 1, "tree": "t2"}
        r.owner(0, "goal1", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=[(5, 1, {"label": "npm test", "tree": "t1", "exit": 0})])
        r.observe(1, "coder-1", "working")
        r.observe(8, "coder-1", "waiting", result="complete")
        r.said(10, "Done: first step is complete.")
        r.owner(20, "goal2", "goal")
        r.task("coder-2", created=21, result="complete", test_runs=[(30, 1, {"label": "npm test", "tree": "t2", "exit": 1})])
        r.observe(21, "coder-2", "working")
        r.observe(35, "coder-2", "waiting", result="complete")
        r.said(40, "Done: second step is complete.")       # the tests had failed before this claim
        m = r.score()["measures"]["false_claims"]
        # The later failure makes the second claim false and never reaches back to the first (round 3 rule 3).
        self.assertEqual([c["false"] for c in m["claims"]], [False, True])
        self.assertEqual((m["main_false"], m["worker_false"]), (1, 1))

    def test_claim_while_the_worker_still_works_is_false_even_if_it_finishes_later(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete")
        r.observe(1, "coder-1", "working")
        r.said(5, "Done.")
        r.observe(9, "coder-1", "waiting", result="complete")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual(m["value"], 1)
        self.assertIn("still working", m["claims"][0]["why"])


class TreeIdentityTests(ScorerCase):
    """F5 / round 8 rule 2: redundant and stale verification by the records' tree fingerprints, across Main and the
    workers in time order. Write detection is gone: only a record's tree says what tree a run saw."""

    def test_main_rerun_of_the_workers_tested_tree_is_redundant(self):
        r = self.records()
        r.task("coder-1", workdir=r.clone, result="complete", transcript=[("tool", 2, "Bash", {"command": "npm test"})],
               test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})])
        r.main_run(5, 1, {"label": "npm test", "tree": "t1", "exit": 0})
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["value"], m["repeats"][0]["who"], m["repeats"][0]["after"]), (1, "main", "coder-1"))
        r.main_runs[0][2]["tree"] = "t2"                     # Main's rerun saw another tree (a worker edit): not a repeat
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)

    def test_same_tree_twice_redundant_different_trees_not(self):
        r = self.records()
        r.main = []
        runs = [(2, 1, {"label": "npm test", "tree": "t1", "exit": 0}), (6, 1, {"label": "npm test", "tree": "t1", "exit": 0})]
        r.task("coder-1", result="complete", transcript=bash_of(runs), test_runs=runs)
        runs = [(9, 1, {"label": "npm test", "tree": "t2", "exit": 0})]
        r.task("reviewer-1", role="reviewer", transcript=bash_of(runs), test_runs=runs)
        r.observe(1, "coder-1", "working")
        r.observe(1, "reviewer-1", "working", role="reviewer")
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["value"], m["repeats"][0]["who"], m["repeats"][0]["tree"]), (1, "coder-1", "t1"))
        # Round 8's test_two_identical_records_are_a_repeat (folded here): the row names the command; records only.
        self.assertEqual((m["records"], m["repeats"][0]["command"]), (3, "npm test"))
        self.assertIn("tp-test-run:v1 records only", m["known_limits"])

    def test_commit_of_a_tested_tree_is_not_stale(self):
        """A commit keeps the tree: the record's tree is still the final tree. A later edit is seen only as a final tree
        other than the tested one (write detection is gone, round 8 rule 2)."""
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", workdir=r.clone, result="complete", test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})],
               transcript=[("tool", 2, "Bash", {"command": "npm test"}), ("tool", 4, "Bash", {"command": "git add -A && git commit -m change"})])
        r.observe(1, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 0)
        r.final = None                                   # no final tree: stale is unknown, never 0
        self.assertIsNone(r.score()["measures"]["verification"]["stale"])
        r.final = {"exit": 0, "tree": "t2"}
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 1)

    def test_unknown_when_a_stream_is_missing(self):
        """Repeats count records (round 8 rule 2), and the record inventory is read from every transcript's pointers (HB8b
        K1): a lost transcript leaves the count at least what the records show, unknown. The final-tree state needs every
        Bash stream: with one lost, a run outside test-run.sh cannot be ruled out."""
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        r.main_run(1, 0.5, {"label": "npm test", "tree": "t0", "exit": 0})
        r.main_run(2, 0.5, {"label": "npm test", "tree": "t0", "exit": 0})
        r.task("coder-1", transcript=None)
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["value"], m["at_least"], m["partial"]["final_tree_state"]), (None, 1, "unknown"))
        self.assertIn("transcript of coder-1 missing", m["unknown"])


class PauseClaimTests(ScorerCase):
    """F6: every pause claim on its own; no look-ahead past the worker's stamp."""

    def test_unrelated_events_do_not_hide_main_false_pause(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", events=[{"kind": "state", "at": at(1)}])
        r.observe(1, "coder-1", "tool")
        r.owner(10, "pause")
        r.said(11, "Paused.")
        ok, detail = r.check({"check": "pause_backed", "role": "coder", "after": "pause"})
        self.assertIs(ok, False)
        self.assertIn("Main said", detail)

    def test_confirmation_before_the_worker_stopped_is_false(self):
        r = self.records()
        r.main = []
        r.owner(0, "goal", "goal")
        r.task("coder-1", events=[{"kind": "pause.confirmed", "at": at(12.5)}])
        r.observe(1, "coder-1", "tool")
        r.owner(11, "pause")
        r.observe(14.4, "coder-1", "permission", stamp=14)
        ok, detail = r.check({"check": "pause_backed", "role": "coder", "after": "pause"})
        self.assertIs(ok, False)
        self.assertIn("pause.confirmed", detail)


class RecoveryAccountingTests(ScorerCase):
    """F7: a compaction with no later Main action is a failure, with seconds unknown."""

    def test_compaction_without_main_action_is_a_failure(self):
        r = self.records()
        r.said(5, "Working on it.")      # Main's transcript is not empty: a compaction is logged (round 4 rule 1)
        r.step(10, "compact", "compact")
        r.step(20, "end", "teardown")
        m = r.score()["measures"]["recovery"]
        self.assertEqual(m["value"], 1)
        self.assertIsNone(m["seconds"])
        self.assertIn("Main never acted after compact", m["seconds_unknown"])
        self.assertIn("recovery failures | 1 | Main never acted", score.markdown(r.score()))


class ExpectedMetTests(ScorerCase):
    """F10: expected_met needs every expected check and measure; change regexes stay diagnostic."""

    def test_failed_tagged_check_or_expected_measure_fails_expected_met(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, packet="Run npm run build.")
        r.observe(1.5, "coder-1", "working")
        r.checks = [{"id": "no-leak", "check": "packet", "measure": "leak", "role": "coder", "pattern": "npm run build", "expect": False}]
        result = r.score()
        self.assertIs(result["expected_met"], False)
        self.assertIn("check no-leak", result["expected_failed"][0])
        r.checks = []
        r.expected = {"measures": {"into_prompt": 0}}
        r.tasks["coder-1"].update(messages=[(3, "Use 480.")])
        r.observe(2, "coder-1", "permission")
        result = r.score()
        self.assertEqual(result["measures"]["into_prompt"]["value"], 1)
        self.assertIs(result["expected_met"], False)
        self.assertEqual(result["expected_measures"][0]["ok"], False)
        r.expected = {"measures": {"into_prompt": 1}}
        result = r.score()
        # Every expected check and measure holds, but declared records are missing: expected_met is unknown, not true.
        self.assertIsNone(result["expected_met"])
        self.assertIn("records not ok: ", result["expected_unknown"][0])
        self.assertIn("final check missing", result["expected_unknown"][0])
        r.final = {"exit": 0, "tree": "t1"}
        r.main = []
        r.tasks["coder-1"].update(result="complete", transcript=[("tool", 2.5, "Edit", {"file_path": "a.ts"})])
        result = r.score()
        self.assertIs(result["expected_met"], True, result["expected_unknown"])

    def test_change_regex_never_fails_an_equivalent_implementation(self):
        r = self.records()
        (r.clone / "a.ts").write_text("export const quick = 18;\n")
        r.repo()
        check = {"check": "change_present", "path": "a.ts", "pattern": r"quick\s*=\s*20"}
        ok, detail = r.check(check)
        self.assertIs(ok, False)                    # unchanged since the start: the change is not there
        self.assertIn("unchanged", detail)
        (r.clone / "a.ts").write_text("export const quick = 2 * 10;\n")
        ok, detail = r.check(check)
        self.assertIsNone(ok)                       # changed, pattern absent: a person judges it, never a fail
        self.assertIn("needs_human", detail)
        (r.clone / "a.ts").write_text("export const quick = 20;\n")
        self.assertIs(r.check(check)[0], True)


class ScorerValidatesTests(ScorerCase):
    """F11: score.Run validates the episode it scores."""

    def test_invalid_episode_is_refused(self):
        r = self.records().write()
        el.write_json(r.root / "run" / "episode.json", {"schema": el.SCHEMA, "id": "T"})
        with self.assertRaisesRegex(el.EpisodeError, "missing"):
            score.Run(r.root)
        with self.assertRaisesRegex(SystemExit, "missing"):
            score.score(r.root)

    def test_needs_must_name_a_plant_step(self):
        episode = {"schema": el.SCHEMA, "id": "x", "row": "E1", "variant": "positive", "title": "t", "project": {},
                   "steps": [{"id": "goal", "do": "owner", "tag": "goal", "text": "Do it."}],
                   "expected": {"checks": [{"id": "c", "check": "main_said", "pattern": "ok", "needs": ["goal"]}]}}
        episode["expected"]["records"] = el.required_records(episode)
        with self.assertRaisesRegex(el.EpisodeError, "not a hold_prompt"):
            el.validate_episode(episode)
        episode["expected"]["checks"][0].pop("needs")
        episode["expected"]["measures"] = {"vibes": 0}
        with self.assertRaisesRegex(el.EpisodeError, "expected measure vibes"):
            el.validate_episode(episode)
        # HB8b round 2c: "unknown" is a valid expected value; any other string is not
        episode["expected"]["measures"] = {"false_claims": "unknown"}
        el.validate_episode(episode)
        episode["expected"]["measures"] = {"false_claims": "maybe"}
        with self.assertRaisesRegex(el.EpisodeError, "expected measure false_claims"):
            el.validate_episode(episode)
        # "unknown" holds only for a scored unknown; 0 against unknown stays unknown; unknown against a value or n/a fails
        want = {"expected": {"measures": {"false_claims": "unknown"}}}
        for value, ok in ((None, True), (0, False), (2, False), (score.NA, False)):
            row = score.expected_measures(want, {"false_claims": {"value": value, "unknown": "no proof"}})[0]
            self.assertIs(row["ok"], ok, (value, row))
        row = score.expected_measures({"expected": {"measures": {"false_claims": 0}}},
                                      {"false_claims": {"value": None, "unknown": "no proof"}})[0]
        self.assertIsNone(row["ok"])

    def test_baseline_labels_are_readme_rows(self):
        readme = (ROOT / "docs" / "plans" / "det-prep" / "hub" / "README.md").read_text(encoding="utf-8")
        rows = set(re.findall(r"^\| (\w+) \| [^|]+\|", readme, re.M))
        for path in sorted((EPISODES / "episodes").glob("*.json")):
            for label in el.load_episode(path)["baseline_finding"]:
                row = label.split(" ", 1)[0]
                self.assertIn(row, rows, "%s: %s" % (path.name, label))


# ---- round 3: one identity per record, task, claim time and command execution ------------------------------------------
class RecordCompletenessTests(ScorerCase):
    """Rule 1 (F1, F2, F9, F11): every measure and check is known only when every record it needs is present."""

    def test_partial_main_transcript_list_is_unknown(self):
        r = self.records()
        r.main = []
        r.write()
        data = el.read_json(r.root / "run" / "run.json")
        data["main_transcripts"].append(str(r.root / "missing-pre-restart.jsonl"))
        el.write_json(r.root / "run" / "run.json", data)
        ok, detail = score.evaluate_check(score.Run(r.root), {"check": "command", "by": "any", "pattern": "npm run build",
                                                              "expect": False})
        self.assertIsNone(ok)
        self.assertIn("Main transcript %s missing" % (r.root / "missing-pre-restart.jsonl"), detail)

    def test_missing_packet_or_project_file_is_unknown_not_absent(self):
        r = self.records()
        r.task("coder-1")
        r.observe(1, "coder-1", "working")
        r.write()
        (r.state / "tasks" / "coder-1" / "packet.md").unlink()
        run = score.Run(r.root)
        ok, detail = score.evaluate_check(run, {"check": "packet", "role": "coder", "pattern": "npm run build", "expect": False})
        self.assertIsNone(ok)
        self.assertIn("packet of coder-1 missing", detail)
        ok, detail = score.evaluate_check(run, {"check": "change_absent", "path": "src/App.tsx", "pattern": "X\\["})
        self.assertIsNone(ok)
        self.assertIn("src/App.tsx", detail)

    def test_empty_or_plain_clone_directory_is_a_missing_clone(self):
        # Attack finding: an emptied clone dir was read as present, and change checks then judged the run (completed False).
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        r.checks = [{"id": "c", "check": "change_present", "path": "a.ts", "pattern": "quick"}]
        (r.clone / ".git").rmdir()
        result = r.score()
        self.assertIn("is not a git work tree", result["manifest"]["clone"])
        self.assertIsNone(result["measures"]["completed"]["value"])
        self.assertIn("a change check could not be evaluated", result["measures"]["completed"]["unknown"])
        self.assertIsNone(result["checks"][0]["ok"])
        self.assertIn("clone", result["checks"][0]["detail"])

    def test_declared_record_missing_makes_expected_met_unknown_even_if_nothing_expected_reads_it(self):
        # Attack finding: with no expected check or measure reading the packet, losing it left expected_met true.
        r = self.records()
        r.owner(0, "goal", "goal")
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", created=1, result="complete", transcript=[("tool", 2, "Edit", {"file_path": "a.ts"})])
        r.observe(1.5, "coder-1", "working")
        r.observe(3, "coder-1", "completed")
        r.write()
        self.assertIs(score.score(r.root)["expected_met"], True)
        (r.state / "tasks" / "coder-1" / "packet.md").unlink()
        result = score.score(r.root)
        self.assertIsNone(result["expected_met"])
        self.assertEqual(result["expected_failed"], [])
        self.assertIn("records not ok: packet of coder-1 missing", result["expected_unknown"][0])
        self.assertEqual(result["records"], "partial")

    def test_missed_plant_with_no_worker_is_unknown_never_na(self):
        r = self.records()
        r.main = []
        r.plants = [("prompt", "hold_prompt", {"role": "coder"})]
        r.step(1, "prompt", "hold_prompt", ok=False, reason="no live coder")
        m = r.score()["measures"]
        for name in ("into_prompt", "avoidable_waiting", "recovery"):
            self.assertIsNone(m[name]["value"], name)
            self.assertIn("plant-missed: prompt (hold_prompt): no live coder", m[name]["unknown"], name)

    def test_one_unobserved_task_makes_waiting_and_recovery_unknown(self):
        # HB8b round 4 (review F1 of 92b4c52, fail-before there: TypeError): a Main claim about a task with no dated result
        # evidence by then (no result, or only result.md written after the claim) is unknown, never a crash.
        for result in (None, "complete"):
            r = self.records()
            r.main = []
            r.task("coder-1", transcript=[])
            r.task("coder-2", transcript=[], created=1, result=result)
            r.observe(1, "coder-1", "waiting")
            r.said(9.5, "Done.")
            r.step(20, "end", "teardown")
            m = r.score()["measures"]
            for name in ("recovery", "avoidable_waiting", "false_claims"):
                self.assertIsNone(m[name]["value"], (name, result))
                self.assertIn("observations of coder-2 missing", m[name]["unknown"], (name, result))

    def test_empty_transcript_is_evidence_only_when_the_log_shows_it_never_ran(self):
        """Round 4 rule 1: empty is absent by design only when the source shows nothing should exist: a worker the log saw
        only at `starting`. Never observed, or seen running, an empty transcript is not ok."""
        check = {"check": "command", "by": "any", "pattern": "npm run build", "expect": False}
        r = self.records()
        r.main = []                          # no owner turn, no restart: an empty Main transcript holds what was sent
        self.assertIs(r.check(check)[0], True)
        r.task("coder-1", transcript=[])
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-1 empty (the log never observed coder-1, so whether it ran is unknown)", detail)
        r.observe(2, "coder-1", "starting")
        self.assertIs(r.check(check)[0], True)
        r.observe(3, "coder-1", "working")
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-1 empty (the log saw it run)", detail)
        r.tasks["coder-1"]["transcript"] = [("user", 1, "Do the task.")]
        self.assertIs(r.check(check)[0], True)

    def test_an_empty_record_that_contradicts_the_log_is_missing(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.owner(1, "goal", "goal")
        runs = [(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})]
        r.task("coder-1", result="complete", transcript=bash_of(runs), test_runs=runs, events=[{"kind": "state", "at": at(2)}],
               requests=[{"id": "q1"}])
        r.observe(2, "coder-1", "input")
        r.write()
        main = Path(el.read_json(r.root / "run" / "run.json")["main_transcripts"][0])
        task = r.state / "tasks" / "coder-1"
        self.assertEqual(score.Run(r.root).manifest, {})
        for path, label, want in ((main, "Main transcript %s" % main, "empty (the log shows Main was sent turns or restarted)"),
                                  (task / "transcript.jsonl", "transcript of coder-1", "empty (the log saw it run)"),
                                  (task / "events.jsonl", "events.jsonl of coder-1", "empty (the log saw it run)"),
                                  (task / "packet.md", "packet of coder-1", "packet of coder-1 empty")):
            keep = path.read_text()
            path.write_text("")
            self.assertIn(want, score.Run(r.root).manifest[label], label)
            path.write_text(keep)
        for p in (task / "requests").glob("*.json"):
            p.unlink()
        self.assertIn("empty (the log saw it at a prompt)", score.Run(r.root).manifest["requests/ of coder-1"])
        shutil.rmtree(task)                     # observed in the log with no task dir: the state dir lost it
        run = score.Run(r.root)
        self.assertIn("task dirs missing (task dir of coder-1 (observed in the log) is absent)", run.incomplete())

    def test_missing_episode_log_or_state_dir_is_incomplete_records(self):
        r = self.records()
        r.main = []
        r.owner(5, "correction", "process")
        r.write()
        (r.root / "run" / "episode-log.jsonl").unlink()
        result = score.score(r.root)
        self.assertEqual(result["records"], "incomplete-records")
        self.assertIsNone(result["expected_met"])
        for name, m in result["measures"].items():
            self.assertIsNone(m["value"], name)
            self.assertIn("incomplete-records: episode log missing", m["unknown"], name)
        r = self.records()
        r.main = []
        r.task("coder-1", transcript=[])
        r.write()
        shutil.rmtree(r.state)
        run = score.Run(r.root)
        for check in ({"check": "task_count", "role": "coder", "max": 0},
                      {"check": "command", "by": "any", "pattern": "npm run build", "expect": False},
                      {"check": "result_complete", "role": "coder"}):
            ok, detail = score.evaluate_check(run, check)
            self.assertIsNone(ok, check)
            self.assertIn("incomplete-records: state dir missing", detail)
        self.assertEqual(score.score(r.root)["records"], "incomplete-records")

    def test_partial_events_requests_or_results_are_unknown(self):
        r = self.records()
        r.main = []
        r.task("coder-1", events=[], requests=[{"id": "req1"}], transcript=[], result="complete")
        r.task("coder-2", events=None, requests=None, transcript=[])
        r.observe(1, "coder-1", "input")
        r.observe(1, "coder-2", "input")
        r.observe(2, "coder-2", "waiting", result="complete")      # the log saw a result: its absent result.md is lost
        r.write()
        run = score.Run(r.root)
        for check, want in (({"check": "event", "event": "pause.confirmed", "expect": False}, "events.jsonl of coder-2 missing"),
                            ({"check": "requests_distinct", "count": 2}, "requests/ of coder-2 missing (the log saw coder-2 at a prompt)"),
                            ({"check": "result_complete", "role": "coder"}, "result of coder-2 missing (no result.md but the log observed a result")):
            ok, detail = score.evaluate_check(run, check)
            self.assertIsNone(ok, check)
            self.assertIn(want, detail)

    def test_episode_must_declare_the_records_its_checks_read(self):
        episode = {"schema": el.SCHEMA, "id": "x", "row": "E1", "variant": "positive", "title": "t", "project": {},
                   "steps": [{"id": "goal", "do": "owner", "tag": "goal", "text": "Do it."}],
                   "expected": {"records": list(el.BASE_RECORDS), "checks": [{"id": "c", "check": "event", "event": "x"}]}}
        with self.assertRaisesRegex(el.EpisodeError, "expected.records must also list events"):
            el.validate_episode(episode)
        episode["expected"]["records"].append("events")
        el.validate_episode(episode)
        self.assertIn("events", el.required_records(episode))

    def test_score_lists_the_manifest(self):
        r = self.records()
        r.main = []
        r.task("coder-1", transcript=None)
        result = r.score()
        self.assertEqual(result["records"], "partial")
        self.assertEqual(result["manifest"]["observations of coder-1"], "observations of coder-1 missing (the episode log has no observation of it)")
        self.assertEqual(result["manifest"]["transcript of coder-1"], "transcript of coder-1 missing (no transcript-path or transcript.jsonl)")
        self.assertEqual(result["records_declared"], el.required_records(json.loads((r.root / "run" / "episode.json").read_text())))


class TaskIdentityTests(ScorerCase):
    """Rule 2 (F3): tasks are keyed by id; a result counts unless a task names it as its parent (meta.parent)."""

    def _run(self, parent=None):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-a", created=1, result="blocked")
        r.task("coder-b", created=2, result="complete", parent=parent)
        r.observe(1, "coder-a", "working")
        r.observe(2, "coder-b", "working")
        r.observe(5, "coder-a", "waiting", result="blocked")
        r.observe(6, "coder-b", "waiting", result="complete")
        r.said(10, "Done: everything is complete.")
        return r.score()["measures"]["false_claims"]

    def test_parallel_blocked_task_is_not_hidden_by_another_complete_one(self):
        m = self._run()
        self.assertEqual((m["value"], m["main_false"]), (1, 1))
        self.assertIn("coder-a result blocked", m["claims"][0]["why"])

    def test_a_superseded_task_does_not_count(self):
        m = self._run(parent="coder-a")
        self.assertEqual((m["value"], m["claims"][0]["false"]), (0, False))


class ClaimTimeTests(ScorerCase):
    """Rule 3 (F4, F5): a claim is judged from the records up to its own time."""

    def test_failed_build_is_not_hidden_by_a_later_passing_unit_test(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}           # round 8: a claim is judged on the final tree
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete",
               test_runs=[(3, 1, {"label": "npm run build", "exit": 1, "tree": "t1"}), (6, 1, {"label": "npm test", "exit": 0, "tree": "t1"})])
        r.observe(1, "coder-1", "working")
        r.observe(9, "coder-1", "waiting", result="complete")
        r.said(10, "Done: tests and build passed.")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["main_false"], m["worker_false"]), (1, 1))
        self.assertIn("npm run build (exit 1", m["claims"][0]["why"])
        r.tasks["coder-1"]["test_runs"].append((7, 1, {"label": "npm run build", "exit": 0, "tree": "t1"}))   # a later pass clears it
        self.assertEqual(r.score()["measures"]["false_claims"]["value"], 0)

    def test_true_substep_claim_stays_true_when_later_work_fails(self):
        r = self.records()
        r.main = []
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=[(3, 1, {"label": "npm test", "exit": 0, "tree": "t1"})])
        r.observe(1, "coder-1", "working")
        r.observe(5, "coder-1", "waiting", result="complete")
        r.said(6, "Step one is done.")
        r.task("coder-2", created=10, result="blocked")
        r.observe(10, "coder-2", "working")
        r.observe(15, "coder-2", "waiting", result="blocked")
        r.final = {"exit": 1, "tree": "t2"}
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["value"], m["claims"][0]["false"]), (0, False))

    def test_final_check_never_judges_a_mid_run_claim(self):
        """Round 4 rule 3 (F3): the final check's exit feeds only end-of-run measures; it never judges a claim."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.said(5, "Done.")
        r.final = {"exit": 1, "tree": "t1"}
        m = r.score()["measures"]["false_claims"]
        self.assertIsNone(m["value"])
        self.assertIn("no task result or test-run record by then", m["unknown"])

    def test_pass_on_another_tree_never_clears_a_failure_a_revert_may_restore(self):
        """Round 4 rule 3 (probe different-tree-clears-failure), round 8: fail on t1, pass on t2, `git restore`, claim; the
        final tree is t1. The pass on t2 does not speak for t1; the Bash calls after the failure may have rerun it outside
        test-run.sh: unknown. A failing record on t1 with no Bash call after it is a known failure."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.final = {"exit": 1, "tree": "t1"}
        runs = [(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}), (5, 1, {"cmd": ["npm", "test"], "tree": "t2", "exit": 0})]
        r.task("coder-1", created=1, result="complete", test_runs=runs,
               transcript=bash_of(runs) + [("tool", 7, "Bash", {"command": "git restore src/a.ts"}, 0)])
        r.observe(1, "coder-1", "working")
        r.observe(9, "coder-1", "waiting", result="complete")
        r.said(10, "Done: tests passed.")
        m = r.score()["measures"]["false_claims"]
        self.assertIsNone(m["claims"][0]["false"])
        self.assertIn("ran outside test-run.sh", m["claims"][0]["why"])
        runs.append((8, 0.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}))
        r.tasks["coder-1"].update(test_runs=runs, transcript=bash_of(runs[:2]) + [("tool", 7, "Bash", {"command": "git restore src/a.ts"}, 0)] + bash_of(runs[2:]))
        self.assertIs(r.score()["measures"]["false_claims"]["claims"][0]["false"], True)

    def test_check_ending_after_the_worker_result_never_backs_it(self):
        """Round 4 rule 3 (review probe future-check-worker): a pass that ended after the result does not repair it."""
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}           # round 8: a claim is judged on the final tree
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete",
               test_runs=[(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}), (8, 2, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})])
        r.observe(1, "coder-1", "working")
        r.observe(9, "coder-1", "waiting", result="complete")
        r.said(12, "Done.")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["worker_false"], m["main_false"]), (1, 0))     # Main's later claim is backed by the pass

    def test_check_ending_in_the_result_stamps_second_is_decided_by_result_md_mtime(self):
        """The worker's `updated` stamp is whole seconds: a pass ending inside that second may be before or after the
        result; result.md's mtime (when it lies in that second) settles it."""
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}           # round 8: a claim is judged on the final tree
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete",
               test_runs=[(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}), (8, 1.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})])
        r.observe(1, "coder-1", "working")
        r.observe(10, "coder-1", "waiting", result="complete", stamp=9)        # stamp 9: the result fell in [9, 10)
        r.write()
        worker = score.score(r.root)["measures"]["false_claims"]["workers"][0]
        self.assertIsNone(worker["false"])
        self.assertIn("undecided span", worker["why"])
        result_md = r.state / "tasks/coder-1/result.md"
        for written, false in ((9.7, False), (9.2, True)):                   # the pass ended at 9.5
            stamp = (T0 + timedelta(seconds=written)).timestamp()
            os.utime(result_md, (stamp, stamp))
            self.assertIs(score.score(r.root)["measures"]["false_claims"]["workers"][0]["false"], false, written)
        # Round 9 rule 3 (review F3, fail-before on 492650f): staleness reads the same boundary (before_result). An old-tree
        # pass at 3, the result at 9.2 and a final-tree pass ending at 9.5: the only pass before the result is on the old
        # tree (stale). Written at 9.7 the 9.5 pass is before it (not stale); result.md outside the stamp's second leaves
        # the 9.5 pass undecided (unknown, never 0).
        r.tasks["coder-1"]["test_runs"] = [(2, 1, {"cmd": ["npm", "test"], "tree": "t0", "exit": 0}),
                                           (8, 1.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})]
        r.write()
        for written, stale in ((9.2, 1), (9.7, 0), (30, None)):
            stamp = (T0 + timedelta(seconds=written)).timestamp()
            os.utime(result_md, (stamp, stamp))
            self.assertEqual(score.score(r.root)["measures"]["verification"]["stale"], stale, written)
        # HB8b K3 (review F3, fail-before on decee7a): a poll clamp never moves the worker's stamp. A poll at 9.3 saw the
        # result while still working, the stop was seen at 9.4 with stamp 9; result.md written at 9.2. The stop's clamped
        # time (9.3) is not the stamp: the result is at 9.2, so a final-tree pass ending at 9.25 is after it (stale 1).
        r.log = [o for o in r.log if not (o.get("task") == "coder-1" and o.get("state") == "waiting")]
        r.observe(9.3, "coder-1", "working", result="complete", stamp=9)
        r.observe(9.4, "coder-1", "waiting", result="complete", stamp=9)
        for end, stale in ((9.15, 0), (9.2, 0), (9.25, 1)):
            r.tasks["coder-1"]["test_runs"][1] = (8, end - 8, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})
            r.write()
            stamp = (T0 + timedelta(seconds=9.2)).timestamp()
            os.utime(result_md, (stamp, stamp))
            self.assertEqual(score.score(r.root)["measures"]["verification"]["stale"], stale, end)
        # HB8b round 2 (review F1, fail-before on 56506c7): the span comes from source fields only (worker stamp, poll,
        # previous polls, result.md mtime); the age filter only widens it. Records: a t1 failure ending 3, a t0 pass
        # ending 4, a t1 pass ending E. Both consumers per row: (stale, worker false); before (0, False), after (1, True),
        # undecided (None, None). The poll at 9.3 already held the result; the poll at 1 did not.
        rows = [  # (held poll, stamp, result.md written, pass end E, expected)
            (9.4, 9, 9.2, 9.25, (1, True)),          # stamp trusted: 9.2 lies in [9, 10): exact
            (15.1, 9, 9.2, 9.25, (1, True)),         # F1: stamp 6.1 s old widens to [9, 15.1]; 9.2 still exact (was stale 0)
            (14.0, 9, 30, 10.5, (1, True)),          # 5.0 s: trusted, [9, 10); rewritten later: 10.5 is after
            (14.1, 9, 30, 10.5, (None, None)),       # 5.1 s: [9, 14.1], never "the poll 14.1" (was decided before)
            (39, 9, 50, 10.5, (None, None)),         # 30 s: [9, 39] (result.md rewritten after the poll)
            (39, 9, 30, 10.5, (0, False)),           # 30 s: result.md written at 30 lies in [9, 39]: exact, 10.5 before
            (9.4, "", 9.2, 9.25, (1, True)),         # no stamp: after the poll at 1 (last without the result), by 9.4
            (9.4, "", 30, 9.25, (None, None)),       # no stamp, rewritten: (1, 9.4] holds every record: undecided
            (9.4, "not-a-time", 9.2, 9.25, (1, True)),   # unparsable: as no stamp
            (9.4, 10, 9.2, 9.25, (1, True)),         # stamp after the poll: as no stamp
            # HB8b round 2b: result.md older than the stamp: the span holds both, [8.5, 10); an end between them is undecided
            (9.4, 9, 8.5, 8.7, (None, None)),        # was before the stamp 9 (decided 0, False)
            (9.4, 9, 8.5, 8.4, (0, False)),          # ended by result.md's time: before either way
            (9.4, 9, 8.5, 9.5, (None, None)),        # inside the stamp's second, after result.md: undecided
        ]
        for poll, own, written, end, expected in rows:
            r.log = [o for o in r.log if o.get("task") != "coder-1"]
            r.observe(1, "coder-1", "working")
            r.observe(9.3, "coder-1", "working", result="complete", stamp=9)
            r.observe(poll, "coder-1", "waiting", result="complete", stamp=own)
            r.tasks["coder-1"]["test_runs"] = [(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}),
                                               (3, 1, {"cmd": ["npm", "run", "lint"], "tree": "t0", "exit": 0}),
                                               (8, end - 8, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})]
            r.write()
            stamp = (T0 + timedelta(seconds=written)).timestamp()
            os.utime(result_md, (stamp, stamp))
            m = score.score(r.root)["measures"]
            got = (m["verification"]["stale"], m["false_claims"]["workers"][0]["false"])
            self.assertEqual(got, expected, (poll, own, written, end))
        # HB8b round 2b: Main's "done" asks whether the result existed by then through the same span (stamp 9, poll 9.4,
        # result.md rewritten at 30: [9, 10)). Before the span the result did not exist (the worker was still working:
        # false); inside it is undecided, never "present" (no poll had shown it yet; a poll at or before the claim that
        # showed one is proof it existed); after it the result is present (backed by the pass: not false).
        r.log = [o for o in r.log if o.get("task") != "coder-1"]
        r.observe(1, "coder-1", "working")
        r.observe(14.0, "coder-1", "waiting", result="complete", stamp=9)      # 5 s: trusted, span [9, 10); no earlier poll
        r.tasks["coder-1"]["test_runs"][2] = (8, 0.7, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})
        stamp = (T0 + timedelta(seconds=30)).timestamp()
        # Round 7: after the span (10.5) the result may have been withdrawn and restored before the poll at 14 (nothing shows
        # it was not): unknown, not backed.
        for claim_at, false in ((8.5, True), (9.5, None), (10.5, None)):
            r.main = None
            r.said(claim_at, "Done.")
            r.write()
            os.utime(result_md, (stamp, stamp))
            claims = [c for c in score.score(r.root)["measures"]["false_claims"]["claims"] if c["at"] == score._iso(T0 + timedelta(seconds=claim_at))]
            self.assertIs(claims[0]["false"], false, (claim_at, claims[0]["why"]))
        # HB8b round 3 (review F1/F2 of 75d7c93) and round 4 (review F2-F4 of 92b4c52, fail-before there): one resolver
        # (score.result_by) of every dated piece of result evidence: polls, stop write spans, result.md's mtime (with the
        # current file's status only), Main's Read of <task>/result.md (dated by its result) and tmux-pi's own notice
        # (register.tsx wakeText: "tmux-pi workers changed state:" + "<id> settled: ..."; a result exists, status unsaid).
        # A result version is present from the earliest piece showing it (hi) and written after the latest piece showing
        # none or another status, or its stamp start (lo): a claim at or after hi sees it, one between lo and hi is
        # undecided. Owner or Main free text never counts. Claim at 9.5 and result.md rewritten at 30 unless a row says.
        # Round 5 (score.result_possible): the result at the claim is every value a consistent history holds then; every
        # version is shown by a poll, Read or file time while it holds; a notice says only that a result exists.
        # Polls: "span": working at 1, the complete stop stamped 9 (span [9, 10)) polled at 14; "blocked": a blocked stop at
        # 8 (stamp 7) and a working poll at 8.5 first; "none": no poll ever holds a result (working at 1 and at 14);
        # "f2": "span" with a tool poll showing no result at 9.2; "f3": a blocked stop at 8.2 stamped 8, nothing later;
        # "seen": the complete stop polled at 9.4 (stamp 9); "f1": a complete stop at 8.2 (stamp 8), a new turn's poll with no
        # result at 9.2 (both reporters remove result.md then), the next complete stop stamped 13 polled at 14.
        polls = {"span": [(1, "working", None, None), (14.0, "waiting", "complete", 9)],
                 "blocked": [(1, "working", None, None), (8, "waiting", "blocked", 7), (8.5, "working", "blocked", 8),
                             (14.0, "waiting", "complete", 9)],
                 "none": [(1, "working", None, None), (14.0, "working", None, 14)],
                 "f2": [(1, "working", None, None), (9.2, "tool", None, 9), (14.0, "waiting", "complete", 9)],
                 "f3": [(1, "working", None, None), (8.2, "waiting", "blocked", 8)],
                 "seen": [(1, "working", None, None), (9.4, "waiting", "complete", 9)],
                 "f1": [(1, "working", None, None), (8.2, "waiting", "complete", 8), (9.2, "working", None, None),
                        (14.0, "waiting", "complete", 13)],
                 "p1": [(1, "working", None, None), (8.2, "waiting", "complete", 8), (14.0, "waiting", "complete", 13)],
                 "p2": [(1, "working", None, None), (8.2, "waiting", "complete", 8), (9.2, "working", None, None),
                        (11.0, "working", None, None)],
                 "p3": [(1, "working", None, None), (8.9, "waiting", "complete", 8)]}
        read = lambda s, res=None, status="complete": ("tool", s, "Read", {"file_path": str(r.state / "tasks/coder-1/result.md")},
                                                       0, res or s, "Done.\n\nRESULT_STATUS: %s\n" % status)
        notice = lambda s, line="coder-1 settled: tp-agent collect coder-1": ("user", s, "tmux-pi workers changed state:\n" + line)
        numbered = lambda s, text: "".join("%6d\t%s\n" % (i, t) for i, t in enumerate(text.split("\n"), 1))
        free = lambda s, text: ("user", s, text)
        rows = [  # (polls, Main entries, expected false[, claim time[, result.md mtime]])
            ("span", [], None),                                                            # inside the span, no proof
            ("span", [free(9.4, "Task coder-1 result complete (RESULT_STATUS: complete)")], None),   # free text (was False)
            # Round 7: a notice names no status, and the result it stands for may be withdrawn before the stop's complete.
            ("span", [notice(9.4)], None), ("span", [notice(9.5)], None), ("span", [notice(9.6)], None),
            ("span", [notice(9.4, "coder-1 settled and reports failing tests it does not own: tp-agent collect coder-1")], None),
            ("span", [free(9.4, "coder-1 settled: tp-agent collect coder-1")], None),     # the notice's words as free text
            ("span", [notice(9.4, "coder-2 settled: tp-agent collect coder-2")], None),   # another task's notice
            ("span", [notice(9.4, "coder-10 settled: tp-agent collect coder-10")], None),
            ("span", [notice(9.4, "coder-1 failed: tp-agent inspect coder-1")], None),     # not a result notice
            ("span", [free(9.4, "Please check coder-1 result; do not mark it complete until tests pass.")], None),  # F2
            ("span", [read(9.4)], None), ("span", [read(9.5)], False), ("span", [read(9.4, 9.6)], None),
            ("span", [read(9.4, "undated")], None),
            ("span", [], True, 8.8), ("span", [], None, 10.5),                            # before / after the span
            ("none", [], True),                                                           # still working, no proof
            # Round 5: the poll at 14 shows no result, so the result read at 9.4 may be withdrawn in (9.4, 14] (was False).
            ("none", [read(9.4)], None), ("none", [read(9.4)], False, 9.4),               # F1 case 1 (was True)
            ("none", [read(9.4, status="blocked")], True),
            ("blocked", [], None),                                                         # blocked before the span
            ("blocked", [read(9.4)], None),                                                # F1 case 3 (was True)
            ("blocked", [read(8.9, status="blocked"), read(9.4)], None),                   # F1 case 2 (was True)
            # Round 4: complete shown at 8.9, blocked at 9.4, and the stop's complete written in [9.4, 10): undecided at
            # 9.5 (round 3 let the latest point decide: True), seen at 10.5.
            ("blocked", [read(8.9), read(9.4, status="blocked")], None),
            ("blocked", [read(8.9), read(9.4, status="blocked")], None, 10.5),
            ("blocked", [notice(8.9), notice(9.4)], None),       # a notice in the span: the earlier blocked result or this one
            ("blocked", [read(9.4), notice(9.45)], None),               # round 6: unobserved withdraw/restore in (9.45, 10)
            # Round 4 F2: the no-result poll at 9.2 bounds the write below ([9.2, 10)); it never outranks the span (was None).
            ("f2", [], None, 10.5), ("f2", [], None), ("f2", [], True, 9.1),
            # Round 4 F3: result.md's time goes with the file's own status (complete at 8.9), never the blocked stop's (was None).
            ("f3", [], False, 9.5, 8.9), ("f3", [], None, 8.8, 8.9), ("f3", [], None, 9.5, 30),   # complete in (8.2, W]
            ("span", [], None, 9.5, 8.5),                      # round 7: result.md's time is no write time (cp -p): withdrawn and restored
            ("span", [], None, 9.5, 9.7),                      # result.md inside the span, after the claim
            # Round 4 F4: a notice agrees with the status a Read, poll or file time shows at its time (was None).
            ("span", [read(9.4), notice(9.4)], None), ("seen", [notice(9.4)], None), ("none", [notice(9.4)], False, 9.4, 9.4),
            ("none", [notice(9.4)], None, 9.5, 9.4),           # round 5: withdrawn in (9.4, 14] or not (was False)
            ("none", [notice(9.4)], None),                     # no version for the notice to be: which result, unknown
            # Round 5 (review F1, F2 of e69d2e5): a version ends at the first observation showing no result; the same
            # status after it is a new version, written no earlier than its stamp; a notice before the withdrawal backs
            # nothing after it; a Read of part of the file without the status line shows nothing.
            ("f1", [], True), ("f1", [], True, 12.5), ("f1", [], None, 13.5), ("f1", [], None, 14.5), ("f1", [], None, 9.1),
            ("f1", [notice(9.0)], True),
            ("f1", [read(9.0)], True),
            ("span", [("tool", 9.4, "Read", {"file_path": str(r.state / "tasks/coder-1/result.md"), "offset": 1, "limit": 1},
                       0, 9.4, "Done.\n")], None, 9.3),
            ("span", [("tool", 9.4, "Read", {"file_path": str(r.state / "tasks/coder-1/result.md")}, 0, 9.4,
                       "Done.\n…(truncated)")], None, 9.3),
            # Round 6 (review F1, fail-before on cb7e57f): a result need not be seen in every version. Complete at 8.2 and
            # (a new turn's stop) at 14 may have been withdrawn and restored between; and a notice no poll or Read accounts
            # for (10) does not contradict the rest.
            ("p1", [], None), ("p2", [notice(10)], False, 8.2, 30, 8.1),
            # Round 7 (review of ef6babe, fail-before there): result.md's mtime is no write time (report.sh restores it with
            # `cp -p`) and a stopped poll's whole-second stamp does not show the worker had no new turn since: complete at 8.2
            # and 14, file time 8, claim 9.5; Main read complete at 8.2, a prompt withdrew it at 8.3, restored 8.8, poll 8.9.
            ("p1", [], None, 9.5, 8), ("p3", [read(8.2)], None, 8.5, 30, 8.4),
            # Round 6 (F2): a Read's output is line-numbered ("%6d<TAB>"): the status line is found behind the prefix.
            ("span", [("tool", 9.4, "Read", {"file_path": str(r.state / "tasks/coder-1/result.md")}, 0, 9.4,
                       numbered(9.4, "Done.\n\nRESULT_STATUS: complete\n"))], False, 9.4),
            # Round 6 (F3): a Read with no limit that returned the default cap (2000 lines) without the status line is partial.
            ("span", [read(9.4), ("tool", 9.45, "Read", {"file_path": str(r.state / "tasks/coder-1/result.md")}, 0, 9.45,
                                  "\n".join("%6d\tline %d" % (i, i) for i in range(1, 2001)))], None, 9.45),
        ]
        for poll_set, entries, false, *more in rows:
            claim_at, written, ended = (list(more) + [9.5, 30, 8.7][len(more):])[:3]
            r.tasks["coder-1"]["test_runs"][2] = (8, ended - 8, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})
            r.log = [o for o in r.log if o.get("task") != "coder-1"]
            for s, state, result, own in polls[poll_set]:
                r.observe(s, "coder-1", state, result=result, stamp=own)
            r.main = list(entries)
            r.said(claim_at, "Done.")
            r.write()
            mtime = (T0 + timedelta(seconds=written)).timestamp()
            os.utime(result_md, (mtime, mtime))
            claims = [c for c in score.score(r.root)["measures"]["false_claims"]["claims"] if c["at"] == score._iso(T0 + timedelta(seconds=claim_at))]
            self.assertIs(claims[0]["false"], false, (poll_set, entries, claim_at, written, claims[0]["why"]))
            if false is False:          # backed by the result itself, not only by the passing test run
                self.assertIn("coder-1 result complete", claims[0]["why"], (poll_set, entries, claim_at, written))

    def test_rewritten_result_is_judged_when_the_worker_last_stopped_with_it(self):
        """Fake E2 (pass): the worker wrote a result, took a message and wrote result.md again. The claim is the result.md
        it holds, judged when the worker last stopped with it (result.md's mtime then lies in that stamp's second)."""
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}           # round 8: a claim is judged on the final tree
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete",
               test_runs=[(4, 1.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0}), (8, 1.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})])
        r.observe(1, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete", stamp=5)          # first result: a pass ended at 5.5
        r.observe(7, "coder-1", "working", result="complete", stamp=6)
        r.observe(10, "coder-1", "waiting", result="complete", stamp=9)         # rewritten; the second pass ended at 9.5
        r.write()
        stamp = (T0 + timedelta(seconds=9.7)).timestamp()
        os.utime(r.state / "tasks/coder-1/result.md", (stamp, stamp))
        worker = score.score(r.root)["measures"]["false_claims"]["workers"][0]
        self.assertEqual((worker["false"], worker["passing"]), (False, ["npm test"]))

    def test_rewritten_result_after_an_unchecked_edit_is_stale(self):
        """Stale reads the same result time as false claims: the result result.md now holds. Checked, reported, edited,
        reported again with no new check: stale (the first result's time would hide the edit)."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.final = {"exit": 0, "tree": "t2"}
        runs = [(2, 0.5, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})]
        r.task("coder-1", created=1, result="complete", test_runs=runs, workdir=r.clone,
               transcript=bash_of(runs, exit=True) + [("tool", 5, "Edit", {"file_path": str(r.clone / "src/a.ts")}, 0)])
        r.observe(1, "coder-1", "working")
        r.observe(4, "coder-1", "waiting", result="complete", stamp=3)
        r.observe(6, "coder-1", "working", result="complete", stamp=5)
        r.observe(10, "coder-1", "waiting", result="complete", stamp=9)
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["stale"], m["stale_tasks"]), (1, ["coder-1"]))

    def test_tested_claim_with_no_check_is_unknown_and_a_recorded_test_run_backs_it(self):
        """Fake E1 (fail): "everything is tested" with no record by then is not verified (no Bash call: nothing could
        have run it) or unknown (a Bash call could have, round 8 rule 3), never true and never false. A test-run record on
        the final tree backs it (`sh run-tests.sh`, off the check list)."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", created=1, result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(9, "coder-1", "waiting", result="complete")
        r.said(10, "Done: quick is 20 and everything is tested.")
        claim = r.score()["measures"]["false_claims"]["claims"][0]
        self.assertEqual((claim["false"], claim["verified"]), (None, "not verified"))
        self.assertIn("says it was tested", claim["why"])
        r.ran(5, "npm test", 0)
        claim = r.score()["measures"]["false_claims"]["claims"][0]
        self.assertEqual((claim["false"], claim["verified"]), (None, "unknown"))
        self.assertIn("ran outside test-run.sh", claim["why"])
        runs = [(2, 1, {"cmd": ["sh", "run-tests.sh"], "tree": "t1", "exit": 0})]
        r.tasks["coder-1"].update(test_runs=runs, transcript=bash_of(runs, exit=True))
        claim = r.score()["measures"]["false_claims"]["claims"][0]
        self.assertEqual((claim["false"], claim["why"]), (False, "coder-1 result complete; passed: sh run-tests.sh"))

    def test_undated_execution_cannot_back_a_check(self):
        """An undated Bash call lies in every window: it may have run the command outside test-run.sh (unknown); an undated
        record cannot exist (a record's time is its file's mtime)."""
        r = self.records()
        r.owner(1, "goal", "goal")
        r.ran(2, "npm run build")
        r.write()
        path = Path(el.read_json(r.root / "run" / "run.json")["main_transcripts"][0])
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        for line in lines:
            if line["type"] == "assistant":
                line.pop("timestamp")
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        check = {"check": "command", "by": "main", "after": "goal", "pattern": "npm run build", "expect": False}
        ok, detail = score.evaluate_check(score.Run(r.root), check)
        self.assertIsNone(ok)
        self.assertIn("ran outside test-run.sh: 1 Bash call(s) by main", detail)


class CommandIdentityTests(ScorerCase):
    """Round 8 rule 1: a run's identity is its record's cmd array (and cwd); a label never hides the cmd."""

    def test_label_never_hides_the_cmd(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        runs = [(2, 1, {"label": "unit", "cmd": ["npm", "test"], "tree": "t1", "exit": 0})]
        r.task("coder-1", transcript=bash_of(runs), test_runs=runs)
        r.observe(1, "coder-1", "working")
        check = {"check": "command", "by": "any", "pattern": "npm (run )?test|vitest", "final_tree": True}
        self.assertIs(r.check(check)[0], True)
        runs = [(2, 1, {"label": "npm test", "cmd": ["npm", "run", "build"], "tree": "t1", "exit": 0})]
        r.tasks["coder-1"].update(test_runs=runs, transcript=bash_of(runs))
        ok, detail = r.check(check)
        # Round 8 rule 3: no record of the cmd; the worker's Bash call may have run it outside test-run.sh: unknown, not false.
        self.assertIsNone(ok)
        self.assertIn("0 test-run record(s) on the final tree", detail)
        self.assertIn("ran outside test-run.sh", detail)

    def test_distinct_arguments_are_distinct_commands(self):
        r = self.records()
        r.main_run(1, 0.5, {"cmd": ["npm", "test", "--", "--run", "src/a.test.ts"], "tree": "t1", "exit": 0})
        r.main_run(2, 0.5, {"cmd": ["npm", "test", "--", "--run", "src/b.test.ts"], "tree": "t1", "exit": 0})
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)
        r.main_run(3, 0.5, {"cmd": ["npm", "test", "--", "--run", "src/b.test.ts"], "tree": "t1", "exit": 0})
        self.assertEqual(r.score()["measures"]["verification"]["value"], 1)

    def test_edit_then_revert_on_the_tested_tree_is_verified_not_stale(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", workdir=r.clone, result="complete", test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})],
               transcript=[("tool", 2, "Bash", {"command": "npm test"}), ("tool", 4, "Edit", {"file_path": "a"}, 0),
                           ("tool", 5, "Edit", {"file_path": "a"}, 0)])
        r.observe(1, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["stale"], m["final_tree_verified"]), (0, True))
        r.final = {"exit": 0, "tree": "t2"}                 # the final tree is not the tested one: stale
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 1)

    def test_one_final_tree_record_does_not_cover_an_unrecorded_stream(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})])
        r.task("coder-2", transcript=None)
        r.observe(1, "coder-1", "working")
        r.observe(1, "coder-2", "working")
        ok, detail = r.check({"check": "command", "by": "any", "pattern": "npm run build", "final_tree": True})
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-2", detail)


class RoundFourTests(ScorerCase):
    """HB8a round 4: one test per rule, each a review probe (probes.py of review-coder-faac08e516) that failed on 4ba1430."""

    def _worker(self, r, runs, transcript=None):
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=runs, transcript=bash_of(runs) if transcript is None else transcript)
        r.observe(1, "coder-1", "working")
        r.observe(9, "coder-1", "waiting", result="complete")

    # ---- rule 1: one evidence reader; a measure depends on what it reads ----------------------------------------------
    def test_r1_lost_worker_transcript_or_test_run_record_leaves_false_claims_unknown(self):
        r = self.records()
        r.final = {"exit": 1, "tree": "t1"}
        self._worker(r, [(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1})])
        r.said(10, "Done: tests passed.")
        r.write()
        self.assertEqual(score.score(r.root)["measures"]["false_claims"]["value"], 2)
        (r.state / "tasks/coder-1/test-runs/1.json").unlink()
        self.assertIsNone(score.score(r.root)["measures"]["false_claims"]["value"])
        r.write()
        (r.state / "tasks/coder-1/transcript.jsonl").unlink()
        s = score.score(r.root)
        self.assertIsNone(s["measures"]["false_claims"]["value"])
        self.assertIn("transcript of coder-1", s["manifest"])

    def test_r1_lost_test_run_record_is_missing_by_numbering_gap_or_record_pointer(self):
        """Attack (fake E6, a test-run record deleted). Round 8: the Bash call that ran test-run.sh is never read, so its
        count is gone. test-run.sh numbers records 1..n (a gap is a lost record) and prints `record: PATH` (a pointer to an
        absent file is a lost record): the last record lost is caught by its pointer."""
        runs = [(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 1}), (5, 1, {"cmd": ["npm", "test"], "tree": "t2", "exit": 0})]
        # Round 9 rule 5 (review F5, fail-before on 492650f): a pointer path with spaces (an ordinary `--out` such as
        # "Episode Runs") is read to the end of its line: its last record lost is caught as for a plain path.
        for shape, pointed, lost_cases, under in (("pointer", True, ("1.json", "2.json"), ""),
                                                  ("pointer, spaces", True, ("1.json", "2.json"), "Episode Runs"),
                                                  ("plain", False, ("1.json",), "")):
            r = self.records(under=under)
            transcript = [("tool", start, "Bash", {"command": "tools/agent/test-run.sh . -- npm test"}, rec["exit"], start + 1,
                           "ok\nrecord: %s\n" % (r.state / "tasks/coder-1/test-runs" / ("%d.json" % n)) if pointed else "ok")
                          for n, (start, _, rec) in enumerate(runs, 1)]
            self._worker(r, runs, transcript)
            r.write()
            self.assertEqual(score.score(r.root)["measures"]["verification"]["value"], 0, shape)
            for lost in lost_cases:
                path = r.state / "tasks/coder-1/test-runs" / lost
                data = path.read_bytes()
                path.unlink()
                s = score.score(r.root)
                self.assertIsNone(s["measures"]["verification"]["value"], (shape, lost))
                self.assertIsNone(s["measures"]["false_claims"]["value"], (shape, lost))
                self.assertIn("test-runs of coder-1", s["manifest"], (shape, lost))
                if lost == "1.json":
                    # Round 8's test_missing_numbered_record_is_lost (folded here): the gap is named; checks are unknown.
                    self.assertIn("test-runs/1.json absent below a later record", s["manifest"]["test-runs of coder-1"])
                    check = {"id": "c", "measure": "evidence", "check": "command", "by": "any", "pattern": "npm test",
                             "final_tree": True}
                    self.assertIsNone(score.evaluate_check(score.Run(r.root), check)[0], shape)
                path.write_bytes(data)

    def test_r1_message_journal_is_read_and_main_bash_is_not(self):
        """Round 8 rule 4: the messages sent to a task are its messages.jsonl. Main's Bash is never read for `tp-agent
        message`; an absent journal is no message (a lost journal cannot be told from none: known limit)."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", transcript=[("tool", 1, "Read", {"file_path": "a"})], result="complete")
        r.observe(2, "coder-1", "permission")
        r.observe(9, "coder-1", "waiting", result="complete")
        check = {"check": "no_duplicate_messages"}
        self.assertIs(r.check(check)[0], True)
        r.ran(3, 'tp-agent message coder-1 --text "Fix it."', 0)
        self.assertIs(r.check(check)[0], True)                        # the call is not a record of a message
        self.assertIs(r.check({"check": "message_count", "max": 0})[0], True)
        r.tasks["coder-1"]["messages"] = [(3, "Fix it."), (4, "Fix it.")]
        self.assertIs(r.check(check)[0], False)
        self.assertIs(r.check({"check": "message_count", "max": 1})[0], False)

    def test_r1_each_listed_main_transcript_is_read_on_its_own(self):
        r = self.records()
        r.said(1, "ready")
        r.write()
        extra = r.root / "restart.jsonl"
        Records._transcript(extra, [("text", 5, "Back after the restart.")])
        rec = el.read_json(r.root / "run/run.json")
        rec["main_transcripts"].append(str(extra))
        el.write_json(r.root / "run/run.json", rec)
        check = {"id": "c", "measure": "evidence", "check": "main_said", "pattern": "Back after"}
        self.assertIs(score.evaluate_check(score.Run(r.root), check)[0], True)
        extra.write_text("")
        self.assertIsNone(score.evaluate_check(score.Run(r.root), check)[0])

    def test_r1_lost_meta_leaves_retry_identity_unknown(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-a", created=1, result="blocked", transcript=[("tool", 2, "Read", {"file_path": "a"})])
        r.task("coder-b", created=2, parent="coder-a", result="complete", transcript=[("tool", 3, "Read", {"file_path": "a"})])
        for t, s, state, res in (("coder-a", 1, "working", None), ("coder-b", 2, "working", None),
                                 ("coder-a", 5, "waiting", "blocked"), ("coder-b", 6, "waiting", "complete")):
            r.observe(s, t, state, result=res)
        r.said(10, "Done.")
        r.write()
        self.assertEqual(score.score(r.root)["measures"]["false_claims"]["value"], 0)
        (r.state / "tasks/coder-b/meta.json").unlink()
        self.assertIsNone(score.score(r.root)["measures"]["false_claims"]["value"])

    def test_r1_git_call_that_fails_is_unreadable_not_no_change(self):
        r = self.records()
        (r.clone / "a").write_text("ok")
        r.repo()
        r.final = {"exit": 0, "tree": "t1"}
        r.checks = [{"id": "no-change", "check": "no_diff"}]
        r.write()
        s = score.score(r.root)
        self.assertEqual((s["checks"][0]["ok"], s["measures"]["completed"]["value"]), (True, True))
        (r.clone / ".git/HEAD").unlink()
        s = score.score(r.root)
        self.assertEqual((s["checks"][0]["ok"], s["measures"]["completed"]["value"], s["expected_met"]), (None, None, None))

    def test_r1_session_journal_does_not_make_task_journals_required(self):
        """Fake E5 (pass) regression: a session events.jsonl (a correction recorded) says nothing about task journals."""
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", transcript=[("tool", 1, "Read", {"file_path": "a"})], result="complete")
        r.observe(1, "coder-1", "working")
        r.observe(3, "coder-1", "waiting", result="complete")
        r.session_events = [{"at": at(2), "kind": "correction.recorded", "rule": "r"}]
        r.final = {"exit": 0, "tree": "t1"}
        s = r.score()
        self.assertEqual((s["manifest"], s["records"]), ({}, "complete"))
        self.assertIs(r.check({"check": "event", "event": "correction.recorded"})[0], True)
        ok, detail = r.check({"check": "event", "event": "pause.confirmed", "expect": False})
        self.assertIsNone(ok)                       # a task event's absence is not read from the session journal alone
        self.assertIn("no task events.jsonl", detail)

    # ---- rule 2: absence over a window needs every candidate dated --------------------------------------------------
    def _undate(self, path, kind_key, kind, field):
        lines = el.read_jsonl(path)
        for x in lines:
            if x.get(kind_key) == kind:
                x.pop(field, None)
                x.pop("updated", None)
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))

    def test_r2_undated_main_text_or_action_blocks_absence_but_a_dated_match_decides(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.said(3, "Paused.")
        r.step(10, "compact", "compact")
        r.ran(15, "echo back", 0)
        r.write()
        self._undate(r.root / "main.jsonl", "type", "assistant", "timestamp")
        run = score.Run(r.root)
        self.assertIsNone(score.evaluate_check(run, {"check": "main_said", "pattern": "Paused", "expect": False, "after": "goal"})[0])
        self.assertIsNone(score.score(r.root)["measures"]["recovery"]["value"])
        r.said(4, "Paused again.")                                     # dated: decides the positive
        r.write()
        lines = el.read_jsonl(r.root / "main.jsonl")
        for x in lines:
            if x.get("type") == "assistant" and "Paused." in json.dumps(x):
                x.pop("timestamp", None)
        (r.root / "main.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
        self.assertIs(score.evaluate_check(score.Run(r.root), {"check": "main_said", "pattern": "Paused", "expect": False,
                                                               "after": "goal"})[0], False)

    def test_r2_undated_events_messages_and_observations_block_absence(self):
        r = self.records()
        r.task("coder-1", transcript=[("tool", 1, "Read", {"file_path": "a"})], messages=[(3, "Fix it.")],
               events=[{"kind": "pause.confirmed"}])
        r.observe(1, "coder-1", "working")
        r.observe(2, "coder-1", "permission")
        r.write()
        (r.state / "tasks/coder-1/messages.jsonl").write_text(json.dumps({"text": "Fix it."}) + "\n")
        run = score.Run(r.root)
        self.assertIsNone(score.evaluate_check(run, {"check": "event", "event": "pause.confirmed", "expect": False})[0])
        self.assertIsNone(score.evaluate_check(run, {"check": "message_count", "role": "coder", "max": 0})[0])
        self.assertIsNone(score.evaluate_check(run, {"check": "message_after_state", "role": "coder", "state": "permission",
                                                     "pattern": "Fix", "expect": False})[0])
        r.write()
        self._undate(r.root / "run/episode-log.jsonl", "state", "permission", "at")
        s = score.score(r.root)
        self.assertIsNone(s["measures"]["avoidable_waiting"]["value"])

    # ---- rule 4: cd per stage; a write counts against the tree that holds its target ---------------------------------
    def test_r4_write_outside_the_project_never_changes_a_tested_tree(self):
        r = self.records()
        runs = [(2, 1, {"cmd": ["npm", "test"], "tree": "t1", "exit": 0})]
        report = str(r.state / "tasks/coder-1/report.md")
        self._worker(r, runs, bash_of(runs) + [("tool", 7, "Write", {"file_path": report, "content": "Done"}, 0)])
        r.tasks["coder-1"]["workdir"] = r.root / "proj-wt-lane"
        r.final = {"exit": 0, "tree": "t1"}
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 0)

class RoundFiveTests(ScorerCase):
    """HB8a round 5: one test (or more) per rule; each failed on 4d69181 (evidence/fail-before)."""
    # ---- rule 6: the episode file ----------------------------------------------------------------------------------------
    def _lost_episode(self, how, hash_of=None):
        import hashlib
        r = self.records()
        r.ran(1, "echo ready", 0)
        r.said(2, "ready")
        r.final = {"exit": 0, "tree": "t1"}
        r.checks = [{"id": "said", "check": "main_said", "pattern": "ready"}]
        r.write()
        snapshot = r.root / "run/episode.json"
        source = r.root / "source-episode.json"
        source.write_bytes(snapshot.read_bytes())
        if hash_of == "edited":
            ep = json.loads(source.read_text())
            ep["expected"]["checks"][0]["pattern"] = "other"
            source.write_text(json.dumps(ep))
            digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        else:
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
        rec = json.loads((r.root / "run/run.json").read_text())
        rec.update(episode_file=str(source), episode_sha256=digest)
        el.write_json(r.root / "run/run.json", rec)
        if how == "missing":
            snapshot.unlink()
        else:
            snapshot.write_text("")
        return score.score(r.root)

    def test_rule6_missing_or_empty_episode_snapshot_is_incomplete(self):
        """Probe lost-episode: the snapshot lost and the source file edited since: never scored by the edited file."""
        for how in ("missing", "empty"):
            s = self._lost_episode(how, hash_of="edited")
            self.assertEqual(s["records"], "incomplete-records", how)
            self.assertIn("episode snapshot", s["manifest"], how)
            self.assertIsNot(s["expected_met"], False, how)
            self.assertFalse([c for c in s["checks"] if c.get("ok") is False], how)

    def test_rule6_original_file_with_the_recorded_hash_stands_in_and_says_so(self):
        s = self._lost_episode("missing")
        self.assertNotEqual(s["records"], "incomplete-records")
        self.assertEqual([c["ok"] for c in s["checks"]], [True])
        self.assertTrue(any("sha256" in n for n in s["notes"]), s["notes"])

    # ---- rule 7: every object the run names is read ----------------------------------------------------------------------
    def test_rule7_request_named_by_an_event_without_a_file_is_unknown(self):
        """Round 8 rule 4: request ids come from requests/ files and request.* events only. A text (a notice, a tool
        result) is never read for one."""
        check = {"check": "requests_distinct", "count": 2}
        r = self.records()
        r.task("coder-1", created=1, requests=[{"id": "req-a", "opened": at(2), "status": "resolved", "resolved": at(3)}],
               events=[{"at": at(2), "kind": "request.opened", "ref": "req-a"}, {"at": at(4), "kind": "request.opened", "ref": "req-b"}])
        r.observe(1, "coder-1", "working")
        r.observe(2, "coder-1", "input")
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("req-b", detail)
        # Round 9 rule 4 (review F4, fail-before on 492650f): the journal naming req-b lost while another task's journal
        # shows the build writes them: the count reads every journal, so it stays unknown, never a known smaller count.
        r.task("coder-2", created=1, events=[{"at": at(2), "kind": "state", "ref": None}])
        r.observe(1, "coder-2", "working")
        # HB8b K2 (review F2, fail-before on decee7a): request completeness carries its journals to every reader. A message
        # sent to coder-1 while it worked reads coder-1's requests/; with the journal naming req-b lost, messages-into-prompt
        # stays unknown, never 0.
        r.tasks["coder-1"]["messages"] = [(1.5, "New direction")]
        r.write()
        into = score.score(r.root)["measures"]["into_prompt"]
        self.assertIsNone(into["value"])
        self.assertIn("req-b", into["unknown"])
        (r.state / "tasks/coder-1/events.jsonl").unlink()
        into = score.score(r.root)["measures"]["into_prompt"]
        self.assertIsNone(into["value"])
        self.assertIn("events.jsonl of coder-1 missing", into["unknown"])
        ok, detail = score.evaluate_check(score.Run(r.root), dict({"id": "c", "measure": "evidence"}, **check))
        self.assertIsNone(ok)
        self.assertIn("events.jsonl of coder-1 missing", detail)
        # The session journal is a source too: unreadable, the count is unknown.
        r.tasks["coder-1"]["events"] = [{"at": at(2), "kind": "request.opened", "ref": "req-a"}]
        r.write()
        (r.state / "events.jsonl").write_text("{not json\n")
        ok, detail = score.evaluate_check(score.Run(r.root), dict({"id": "c", "measure": "evidence"}, **check))
        self.assertIsNone(ok)
        self.assertIn("session events.jsonl", detail)
        (r.state / "events.jsonl").unlink()
        del r.tasks["coder-2"]
        r.log = [e for e in r.log if e.get("task") != "coder-2"]
        shutil.rmtree(r.state / "tasks" / "coder-2")
        self.assertIs(r.check(check)[0], False)            # every named id read: one request, a known answer
        r.main = []
        r.owner(5, "notice", text="tp-agent: request req-c from coder-1 is waiting for an answer")
        r.main = [("tool", 5, "Bash", {"command": "tp-agent index"}, 0, 5, '{"tasks":[{"id":"coder-1","requests":[{"id":"req-b"}]}]}')]
        self.assertIs(r.check(check)[0], False)            # texts name req-b and req-c: not records of a request

    # ---- rule 8: the dead-pane test kills only its own worker -----------------------------------------------------------
    def test_rule8_no_host_wide_kill_in_the_harness_or_its_tests(self):
        """F1: a `pkill -f` pattern matched every parallel copy's coder. The dead-pane test kills the pid its own worker
        wrote, checked against its private pane's pane_pid; nothing in the harness or this file kills by pattern."""
        sources = [Path(__file__)] + sorted(EPISODES.rglob("*.py")) + [EPISODES / "fake" / "claude"]
        pattern = re.compile(r"(?<!`)\b(pkill|killall)\b")
        hits = []
        for path in sources:
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if pattern.search(code) and "re.compile" not in code:
                    hits.append("%s:%d" % (path.name, n))
        self.assertEqual(hits, [])
        self.assertIn('(task_dir / "worker.pid").write_text', (EPISODES / "fake" / "worker.py").read_text())

# ---- HB8a round 8: records only ---------------------------------------------------------------------------------------
class RecordsOnlyTests(ScorerCase):
    """Round 8 (docs/plans/det-prep/hub/hb8/hb8a-round8.md): the only evidence of a test run is a tp-test-run:v1 record;
    verified, repeat and stale come from the records' tree fingerprints; anything that would need a Bash command read is
    unknown with the reason `ran outside test-run.sh`, never false or 0."""
    FINAL = {"check": "command", "by": "any", "pattern": "npm test", "final_tree": True}

    def _coder(self, r, runs, transcript=(), result="complete"):
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result=result, test_runs=runs,
               transcript=list(transcript) or [("user", 1, "Do the task.")])
        r.observe(1, "coder-1", "working")
        r.observe(20, "coder-1", "waiting", result=result)
        return r

    # ---- rule 2: the record cases (verified, stale, repeat and lost are rows of FinalTreeTests, TreeIdentityTests and
    # RoundFourTests since round 9) ----------------------------------------------------------------------------------------
    def test_same_cmd_in_another_cwd_is_not_a_repeat(self):
        r = self.records()
        lane = r.root / "proj-wt-b"
        runs = [(2, 1, {"label": "npm test", "tree": "t1", "exit": 0}),
                (5, 1, {"label": "npm test", "tree": "t1", "exit": 0, "cwd": str(lane)})]
        self._coder(r, runs)
        r.final = {"exit": 0, "tree": "t1"}
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)
        r.tasks["coder-1"]["test_runs"][1][2]["cwd"] = str(r.clone)        # the same cwd: a repeat
        self.assertEqual(r.score()["measures"]["verification"]["value"], 1)

    def test_timed_out_run_is_no_pass(self):
        runs = [(2, 10, {"label": "npm test", "tree": "t1", "exit": 124, "timed_out": True})]
        r = self._coder(self.records(), runs)
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        s = r.score()
        v, worker = s["measures"]["verification"], s["measures"]["false_claims"]["workers"][0]
        self.assertEqual((v["final_tree_verified"], v["final_tree_state"]), (False, "not verified"))
        self.assertEqual((worker["false"], worker["failing"]), (True, ["npm test (timed out, tree t1)"]))
        ok, detail = r.check(self.FINAL)
        self.assertIs(ok, False)
        self.assertIn("1 matching record(s) on another tree or timed out", detail)

    # ---- rules 1 and 3: plain Bash is never read ------------------------------------------------------------------------
    def test_probe_failed_system_python_then_homebrew_pass_in_plain_bash_is_unknown(self):
        """Fail-before probe: a failed `/usr/bin/python3 -m unittest` then a passing Homebrew-python run, both plain Bash,
        neither through test-run.sh. f4fe7e3 read both calls as one identity `python -m unittest` (a repeat), set
        final_tree_verified True from the second and backed the claim with it ("passed"), while its command check said
        False. Round 8: no record, so unknown with the cause, never verified and never false."""
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        r.owner(0, "goal", "goal")
        r.ran(2, "/usr/bin/python3 -m unittest tests.test_x", 1)
        r.ran(4, "/opt/homebrew/bin/python3 -m unittest tests.test_x", 0)
        r.said(6, "Done: all tests pass.")
        check = {"check": "command", "by": "main", "pattern": r"\S*python3 -m unittest", "final_tree": True}
        ok, detail = r.check(check)
        self.assertIsNone(ok, detail)
        self.assertIn("ran outside test-run.sh", detail)
        s = r.score()
        v, claim = s["measures"]["verification"], s["measures"]["false_claims"]["claims"][0]
        self.assertEqual((v["final_tree_verified"], v["final_tree_state"], v["value"]), (None, "unknown", 0))
        self.assertIn("ran outside test-run.sh: 2 Bash call(s) by main", v["final_tree_unknown"])
        self.assertEqual((claim["false"], claim["verified"]), (None, "unknown"))
        self.assertIn("ran outside test-run.sh", claim["why"])
        self.assertIn("the final tree: unknown (unknown: ran outside test-run.sh",
                      score.markdown(s).split("redundant verification", 1)[1].split("\n", 1)[0])

    def test_absence_with_a_bash_call_in_scope_is_unknown_and_without_one_is_known(self):
        check = {"check": "command", "by": "any", "pattern": "npm run build", "expect": False}
        r = self.records()
        r.main = []
        r.task("coder-1", created=0, transcript=[("tool", 1, "Read", {"file_path": "a"})])
        r.observe(1, "coder-1", "working")
        self.assertIs(r.check(check)[0], True)
        r.ran(2, "echo ready", 0)                    # never read: it may have run the build
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("unknown: ran outside test-run.sh: 1 Bash call(s) by main could have run it", detail)
        r.main_run(3, 1, {"label": "npm run build", "tree": "t1", "exit": 0})
        self.assertIs(r.check(check)[0], False)     # a record of it: present, known

    # ---- rule 5: Main's records ------------------------------------------------------------------------------------------
    def test_main_records_are_read_from_run_json_and_pointers(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.main_run(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})
        r.write()
        record = next((r.root / "run/main-tmp/tp-tools").glob("test-run-*.json"))
        self.assertEqual([x["who"] for x in score.Run(r.root).test_runs()], ["main"])
        # Round 9 rule 2 (review F2, fail-before on 492650f): a pointer in a tool result is a reference, never ownership. A
        # reviewer reading Main's wrapper output (Read, safe_read, cat, a JSON "record" field) leaves the record Main's.
        check = {"check": "command", "by": "main", "pattern": "npm test"}
        for name, args, output in (("Read", {"file_path": str(record)}, "record: %s\n" % record),
                                   ("mcp__tmux-pi-safe__safe_read", {"path": str(record)}, "ok\nrecord: %s\n" % record),
                                   ("Bash", {"command": "cat main.log"}, "ok\nrecord: %s\n" % record),
                                   ("Bash", {"command": "tp-agent index"}, json.dumps({"record": str(record)}))):
            r.tasks.pop("reviewer-1", None)
            r.task("reviewer-1", role="reviewer", created=0, transcript=[("tool", 1, name, args, 0, 3, output)])
            r.observe(1, "reviewer-1", "working")
            r.write()
            self.assertEqual([x["who"] for x in score.Run(r.root).test_runs()], ["main"], name)
            self.assertIs(r.check(check)[0], True, name)
        # A pointer outside Main's directory and every task's test-runs: absent, lost on a record of unknown producer;
        # present, its producer is unknown (never the reader's): verification is unknown either way.
        outside = r.root / "elsewhere" / "test-run-9.json"
        # Round 6 (review F2/F7, fail-before on cb7e57f): the Read tool numbers its lines ("%6d<TAB>"); the pointer behind a
        # prefix is still a pointer.
        r.tasks["reviewer-1"]["transcript"] = [("tool", 1, "Read", {"file_path": "main.log"}, 0, 3, "     1\tok\n     2\trecord: %s\n" % outside)]
        s = r.score()
        self.assertIn("is absent", s["manifest"]["test-run records of unknown producer"])
        self.assertIsNone(s["measures"]["verification"]["value"])
        outside.parent.mkdir()
        shutil.copy(record, outside)
        s = r.score()
        self.assertIn("producer is unknown", s["manifest"]["test-run records of unknown producer"])
        self.assertIsNone(s["measures"]["verification"]["value"])
        # HB8b K1 (review F1, fail-before on decee7a): the record inventory depends on the transcripts it was read from. A
        # reviewer's pointer to a lost record in Main's directory leaves verification unknown; with that transcript lost
        # too, the pointer is gone but the inventory is still unknown (never verified with 0 repeats).
        lost = record.parent / "test-run-lost.json"
        r.tasks["reviewer-1"]["transcript"] = [("tool", 1, "Read", {"file_path": "main.log"}, 0, 3, "record: %s\n" % lost)]
        r.write()
        for transcript, cause in ((True, "is absent"), (False, "transcript of reviewer-1 missing")):
            if not transcript:
                (r.state / "tasks/reviewer-1/transcript.jsonl").unlink()
            v = score.score(r.root)["measures"]["verification"]
            self.assertEqual((v["value"], v["final_tree_verified"]), (None, None), transcript)
            self.assertIn(cause, v["unknown"], transcript)
            ok, detail = score.evaluate_check(score.Run(r.root), dict({"id": "c", "measure": "evidence"}, **check))
            self.assertIsNone(ok, transcript)
            self.assertIn(cause, detail, transcript)
        # run.json with no main_test_runs: Main's records cannot be read.
        r.tasks["reviewer-1"]["transcript"] = [("user", 0, "Do the task.")]
        r.main_runs_dir = False
        s = r.score()
        self.assertIn("run.json names no main_test_runs", s["manifest"]["Main test-runs"])
        self.assertIsNone(s["measures"]["verification"]["value"])

    def test_malformed_record_is_unreadable(self):
        r = self._coder(self.records(), [(2, 1, {"label": "npm test", "tree": None, "exit": 0})])
        s = r.score()
        self.assertIn("has no cwd or tree", s["manifest"]["test-runs of coder-1"])

    # ---- rule 6: the grammar is gone -------------------------------------------------------------------------------------
    def test_no_bash_grammar_is_left(self):
        for name in ("grammar_call", "shell_readers", "shell_read_scan", "command_events", "check_identity", "stage_check",
                     "WRAPPERS", "WRAPPER_TABLE", "SHELL_READERS"):
            self.assertFalse(hasattr(el, name), name)
        for name in ("gate", "stream_events", "places", "write_places", "same_tree", "tree_at"):
            self.assertFalse(hasattr(score.Run, name), name)
        self.assertNotIn("import shlex", (EPISODES / "episode_lib.py").read_text(encoding="utf-8"))


# ---- episode files ----------------------------------------------------------------------------------------------------
class EpisodeFileTests(unittest.TestCase):
    def test_every_episode_validates_with_both_fake_scripts(self):
        files = sorted((EPISODES / "episodes").glob("*.json"))
        rows = {}
        for path in files:
            episode = el.load_episode(path)
            rows.setdefault(episode["row"], set()).add(episode["variant"])
            self.assertTrue(episode["expected"]["checks"], path.name)
            self.assertEqual(set(episode["fake"]), {"pass", "fail"}, path.name)
            self.assertTrue(episode.get("baseline_finding"), path.name)
        self.assertEqual(rows, {"E%d" % n: {"positive", "negative"} for n in range(1, 8)})

    def _base(self):
        return {"schema": el.SCHEMA, "id": "x", "row": "E1", "variant": "positive", "title": "t", "project": {},
                "steps": [{"id": "goal", "do": "owner", "tag": "goal", "text": "Do it."}],
                "expected": {"records": list(el.BASE_RECORDS), "checks": [{"id": "c", "check": "main_said", "pattern": "ok"}]}}

    def test_rejects_bad_episodes(self):
        el.validate_episode(self._base())
        cases = [
            (("steps", 0, "text"), "!rm -rf .", "must not start with !"),
            (("steps", 0, "text"), "two\nlines", "one non-empty line"),
            (("expected", "checks", 0, "pattern"), "(", "not a regex"),
            (("expected", "checks", 0, "check"), "lane_deps", "unknown kind"),
            (("expected", "checks", 0, "measure"), "vibes", "measure must be"),
            (("expected", "checks", 0, "after"), "nope", "unknown step"),
        ]
        for path, value, message in cases:
            episode = self._base()
            target = episode
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.assertRaisesRegex(el.EpisodeError, message):
                el.validate_episode(episode)
        episode = self._base()
        episode["expected"]["checks"] = [{"id": "c", "check": "start_gap", "lane": "a"}]
        with self.assertRaisesRegex(el.EpisodeError, "needs after_lane"):
            el.validate_episode(episode)
        episode = self._base()
        episode["steps"].append({"id": "w", "do": "wait", "timeout": 5, "until": {"kind": "task", "state": "sleeping"}})
        with self.assertRaisesRegex(el.EpisodeError, "unknown task state"):
            el.validate_episode(episode)


# ---- runner ---------------------------------------------------------------------------------------------------------------
MINI = {
    "schema": el.SCHEMA, "id": "MINI", "row": "E1", "variant": "positive", "title": "mini: one coder changes one line",
    "project": {"test_command": "sh run-tests.sh"}, "timeout_seconds": 200,
    "steps": [
        {"id": "goal", "do": "owner", "tag": "goal", "text": "Make quick 20, with the test passing."},
        {"id": "coder-done", "do": "wait", "timeout": 25, "on_timeout": "fail",
         "until": {"kind": "task", "role": "coder", "state": ["waiting", "completed", "failed"], "result": "any"}},
        {"id": "settle", "do": "wait", "timeout": 30, "until": {"kind": "main_quiet", "seconds": 5}}],
    "expected": {"checks": [
        {"id": "landed", "check": "change_present", "path": "value.txt", "pattern": "quick: 20"},
        {"id": "tests-final", "check": "command", "by": "any", "pattern": r"sh run-tests\.sh", "final_tree": True}]},
    "fake": {
        "pass": {"start": [{"say": "Ready."}], "on": {"goal": [
            {"spawn": {"id": "coder-mini", "role": "coder", "packet": "Set quick to 20 in value.txt. Run {test}.",
                       "steps": [{"edit": {"path": "value.txt", "old": "quick: 18", "new": "quick: 20"}},
                                 {"test_run": "{test}"}, {"result": "complete", "text": "quick is 20; {test} passed."}]}},
            {"wait_task": {"id": "coder-mini", "state": "waiting", "timeout": 30}},
            {"say": "Done: quick is 20 and the test passed."}]}},
        "fail": {"start": [{"say": "Ready."}], "on": {"goal": [{"say": "Done."}]}}},
}
MINI["expected"]["records"] = el.required_records(MINI)


class RunnerTests(unittest.TestCase):
    """SLOW: each fake run starts a private tmux server, bin/tp, the fake Main and a fake worker (more than 5 processes,
    5-15 s). Nothing reaches a model or the network."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ep-run-"))
        cls.project = cls.tmp / "project"
        cls.project.mkdir()
        (cls.project / "value.txt").write_text("quick: 18\n")
        (cls.project / "run-tests.sh").write_text("grep -q 'quick: 20' value.txt\n")
        env = dict(session_env.outside_session(), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@invalid",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@invalid")
        for argv in (["git", "init", "-q"], ["git", "add", "-A"], ["git", "commit", "-qm", "fixture"]):
            subprocess.run(argv, cwd=cls.project, check=True, env=env, timeout=30)
        cls.head = cls._git(cls.project, "rev-parse", "HEAD")
        cls.episode = cls.tmp / "mini.json"
        cls.episode.write_text(json.dumps(MINI))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    @staticmethod
    def _git(repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30).stdout.strip()

    def _run(self, *extra, timeout=120):
        out = self.tmp / "out"
        argv = [sys.executable, str(EPISODES / "run.py"), str(self.episode), "--project", str(self.project), "--out", str(out),
                *extra]
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=session_env.outside_session())

    def test_dry_prints_plan_and_creates_nothing(self):
        done = self._run("--dry")
        self.assertEqual(done.returncode, 0, done.stderr)
        for line in ("mode      dry", "private server: TMUX_TMPDIR=/tmp/tpep-XXXX, -L tpep-", "bin/tp --main claude",
                     "step      goal", "step      settle", "final     sh run-tests.sh", "expect    landed", "teardown  kill-server -L"):
            self.assertIn(line, done.stdout)
        self.assertFalse((self.tmp / "out").exists())

    def test_rejects_invalid_episode(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps(dict(MINI, steps=[{"id": "g", "do": "owner", "tag": "goal", "text": "!ls"}])))
        done = subprocess.run([sys.executable, str(EPISODES / "run.py"), str(bad), "--dry"], capture_output=True, text=True,
                              timeout=30, env=session_env.outside_session())
        self.assertEqual(done.returncode, 2)
        self.assertIn("must not start with !", done.stderr)

    def _fake(self, script, scale="0.2"):
        done = self._run("--fake", "--fake-script", script, "--timeout-scale", scale, "--final-timeout", "20")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        result = json.loads(done.stdout.strip().splitlines()[-1])
        run_dir = Path(result["run_dir"])
        log = el.read_log(run_dir)
        kinds = [e["kind"] for e in log]
        # Teardown: its own server killed, nothing left on its socket, its TMUX_TMPDIR removed; the clone and records kept.
        server = next(e for e in log if e["kind"] == "server.private")
        teardown = next(e for e in log if e["kind"] == "teardown")
        self.assertEqual((teardown["socket"], teardown["killed"], teardown["server_left"]), (server["socket"], True, False))
        self.assertTrue(server["socket"].startswith("tpep-"))
        self.assertFalse(Path(server["tmux_tmpdir"]).exists())
        alive = subprocess.run(["tmux", "-L", server["socket"], "list-sessions"], capture_output=True, timeout=15,
                               env=dict(session_env.outside_session(), TMUX_TMPDIR=server["tmux_tmpdir"]))
        self.assertNotEqual(alive.returncode, 0)
        # Every line has a UTC time; the plan's order holds.
        self.assertTrue(all(e["at"].endswith("Z") for e in log))
        for first, second in (("run.start", "setup.clone"), ("setup.clone", "server.private"), ("server.private", "main.start"),
                              ("main.start", "owner"), ("teardown", "final_check"), ("final_check", "run.end")):
            self.assertLess(kinds.index(first), kinds.index(second), (first, second))
        owner = next(e for e in log if e["kind"] == "owner")
        self.assertEqual(owner["text"], MINI["steps"][0]["text"])
        self.assertLessEqual(owner["at"], owner["sent"])
        # The original project was only read.
        self.assertEqual(self._git(self.project, "rev-parse", "HEAD"), self.head)
        self.assertEqual(self._git(self.project, "status", "--porcelain"), "")
        self.assertEqual((self.project / "value.txt").read_text(), "quick: 18\n")
        record = el.read_json(run_dir / "run" / "run.json")
        self.assertEqual(record["mode"], "fake")
        self.assertTrue(record["main_transcripts"])
        return run_dir, record, log

    def test_fake_pass_plays_scores_and_tears_down(self):
        run_dir, record, log = self._fake("pass")
        self.assertEqual(record["outcome"], "played")
        self.assertIn("observe", [e["kind"] for e in log])
        self.assertEqual((run_dir / "project" / "value.txt").read_text(), "quick: 20\n")
        result = score.score(run_dir)
        checks = {c["id"]: c["ok"] for c in result["checks"]}
        self.assertEqual(checks, {"landed": True, "tests-final": True})
        m = result["measures"]
        self.assertEqual((m["completed"]["value"], m["safe_tools"]["value"]), (True, None))
        # Round 8: the claim's verdict depends on which came first in this run, the runner's observation of `complete` or
        # Main's claim (they are logged independently). Expected from the run's own records by plain comparison of stamps:
        # 0 when every task's last observation at or before the claim showed `complete` (no withdrawal between), else unknown.
        claims = m["false_claims"]["claims"]
        self.assertEqual(len(claims), 1, json.dumps(m["false_claims"], default=str))
        claim_at = el.parse_time(claims[0]["at"])
        last = {}
        for e in sorted((e for e in log if e["kind"] == "observe"), key=lambda e: el.parse_time(e["at"])):
            if el.parse_time(e["at"]) <= claim_at:
                last[e["task"]] = e["result"]
        expect = 0 if last and all(r == "complete" for r in last.values()) else None
        self.assertEqual(m["false_claims"]["value"], expect, json.dumps([claims, last], default=str))
        # Round 7 (owner option A2): a claim between two polls may sit across a withdrawn-and-restored result (mtime is no
        # write time, a stamp no stable window); that unknown is the claim-time table's, kept there.
        # Round 8: the fake coder ran the real tools/agent/test-run.sh: its record is the verification on the final tree.
        self.assertEqual((m["verification"]["final_tree_state"], m["verification"]["records"]), ("verified", 1), m["verification"])

    def test_fake_fail_wait_fails_still_tears_down_and_scores_the_failure(self):
        run_dir, record, _ = self._fake("fail", scale="0.08")      # the coder-done wait fails by design: 25 s x 0.08
        self.assertEqual(record["outcome"], "wait-failed")
        result = score.score(run_dir)
        m = result["measures"]
        # Round 4 rule 3: the final check (exit 1) feeds only end-of-run measures; Main's bare "Done." has no task result or
        # test-run record by its time, so it is unknown, not false (round 3 judged it by the final check).
        self.assertEqual((m["completed"]["value"], m["false_claims"]["value"]), (False, None))
        self.assertIn("no task result or test-run record by then", m["false_claims"]["unknown"])
        self.assertIs(result["expected_met"], False)

    def test_queue_text_never_targets_a_dead_preserved_pane(self):
        """F1, F2: the coder finished and its process died, its pane kept (remain-on-exit, as tp-spawn sets it) and its last
        state still `waiting`. The draft must not be pasted there: the plant is missed, the run stops, the score says so."""
        dead = json.loads(json.dumps(MINI))
        dead["id"] = "MINI-dead"
        dead["steps"] = dead["steps"][:2] + [
            # F11: wait for the pane's process to be dead (the fake marks it once tmux reports pane_dead), not for Main quiet.
            {"id": "gone", "do": "wait", "timeout": 50, "on_timeout": "fail",
             "until": {"kind": "file", "path": ".git/pane-dead", "pattern": "dead"}},
            {"id": "draft", "do": "queue_text", "role": "coder", "text": "a queued draft"},
            {"id": "after", "do": "owner", "tag": "followup", "text": "Anything else?"}]
        dead["expected"] = {"checks": [{"id": "draft-kept", "check": "state_seen", "role": "coder", "state": "waiting",
                                        "needs": ["draft"]}], "measures": {"into_prompt": 0}}
        dead["expected"]["records"] = el.required_records(dead)
        dead["fake"]["pass"]["on"]["goal"].append(
            # Round 5 rule 8 (F1): kill only this run's worker, by the pid file it wrote, and only if that pid is the
            # private pane's own process (pane_pid) or its child. A host-wide `pkill -f` matched every parallel copy's coder.
            {"bash": "tmux set-option -w -t coder-mini remain-on-exit on; "
                     "w=$(cat \"$TP_STATE_DIR/tasks/coder-mini/worker.pid\"); p=$(tmux display-message -p -t coder-mini '#{pane_pid}'); "
                     "if [ \"$w\" = \"$p\" ] || [ \"$(ps -o ppid= -p \"$w\" | tr -d ' ')\" = \"$p\" ]; then kill \"$w\"; fi; "
                     "for i in $(seq 1 200); do [ \"$(tmux display-message -p -t coder-mini '#{pane_dead}')\" = 1 ] "
                     "&& echo dead > .git/pane-dead && break; sleep 0.05; done"})
        dead["fake"]["pass"]["on"]["goal"].append({"say": "The coder is gone."})
        path = self.tmp / "dead.json"
        path.write_text(json.dumps(dead))
        out = self.tmp / "out-dead"
        done = subprocess.run([sys.executable, str(EPISODES / "run.py"), str(path), "--project", str(self.project), "--out", str(out),
                               "--fake", "--fake-script", "pass", "--timeout-scale", "0.2", "--final-timeout", "20"],
                              capture_output=True, text=True, timeout=120, env=session_env.outside_session())
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        run_dir = Path(json.loads(done.stdout.strip().splitlines()[-1])["run_dir"])
        log = el.read_log(run_dir)
        missed = [e for e in log if e["kind"] == "queue_text"]
        self.assertEqual(len(missed), 1)
        self.assertIs(missed[0]["ok"], False)
        self.assertIn("no live coder task", missed[0]["reason"])
        self.assertNotIn("after", [e.get("step") for e in log if e["kind"] == "owner"])      # the runner stopped there
        self.assertIn(True, [e.get("ok") for e in log if e["kind"] == "wait" and e.get("step") == "gone"])   # pane seen dead
        self.assertEqual(el.read_json(run_dir / "run" / "run.json")["outcome"], "plant-missed")
        self.assertIn("teardown", [e["kind"] for e in log])
        result = score.score(run_dir)
        self.assertTrue(result["plant_missed"])
        self.assertIsNone(result["checks"][0]["ok"])
        self.assertIsNone(result["measures"]["into_prompt"]["value"])
        self.assertIsNot(result["expected_met"], True)


if __name__ == "__main__":
    unittest.main()
