import session_env  # noqa: F401  (first: drops the caller's tmux/task session before anything else runs)

"""HB8a episode harness: runner plan and teardown (fake mode, private tmux server), episode validation, and the scorer on
hand-made records for every measure, including the negative variants and missing telemetry (unknown, never 0).

No model is started and nothing goes to the network. The runner tests start tmux only through tools/episodes/run.py, which
uses a fresh TMUX_TMPDIR and its own `-L` socket and kills only that server."""
import json
import os
import re
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
import score  # noqa: E402

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def whole(seconds):
    """A worker's own `updated` stamp: whole seconds, never later than the change."""
    return (T0 + timedelta(seconds=int(seconds))).strftime("%Y-%m-%dT%H:%M:%SZ")


def bash_of(runs):
    """The Bash calls that made these test-run records (start, seconds, record): a worker's transcript holds them."""
    return [("tool", start, "Bash", {"command": " ".join(rec["cmd"]) if rec.get("cmd") else rec["label"]}) for start, _, rec in runs]


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
        self.main = None            # None: no Main transcript listed; [] listed and empty
        self.final = None
        self.variant = variant
        self.session_events = None
        self.expected = {}          # extra expected.* (measures)
        self.plants = []            # (step id, kind, extra fields): plant steps in the episode (the log says whether they happened)
        self.start_commit = None
        self.records = None         # expected.records; None: what the episode must list (el.required_records)
        self.bounds = True          # write run.start / run.end around the log (a complete runner log)

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
        role = role or self.tasks.get(task, {}).get("role", "coder")
        lane = self.tasks.get(task, {}).get("lane")
        self.log.append({"at": at(seconds), "kind": "observe", "task": task, "role": role, "lane": lane, "state": state,
                         "result": result, "tool": "", "updated": whole(seconds if stamp is None else stamp), "pane": "%9"})

    def task(self, ident, role="coder", lane=None, created=0, packet="Do the task.", messages=(), test_runs=(), transcript=None,
             events=None, requests=None, result=None, workdir=None, parent=None):
        self.tasks[ident] = {"role": role, "lane": lane, "created": created, "packet": packet, "messages": list(messages),
                             "test_runs": list(test_runs), "transcript": transcript, "events": events, "requests": requests,
                             "result": result, "workdir": workdir, "parent": parent}

    def said(self, seconds, text):
        self.main = (self.main or []) + [("text", seconds, text)]

    def ran(self, seconds, command):
        self.main = (self.main or []) + [("tool", seconds, "Bash", {"command": command})]

    # ---- write and score ---------------------------------------------------------------------------------------------
    @staticmethod
    def _transcript(path, entries):
        lines = []
        for entry in entries:
            if entry[0] == "tool":
                _, s, name, inp = entry
                lines.append({"type": "assistant", "timestamp": at(s), "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "name": name, "input": inp}]}})
            elif entry[0] == "text":
                lines.append({"type": "assistant", "timestamp": at(entry[1]), "message": {"role": "assistant", "content": [
                    {"type": "text", "text": entry[2]}]}})
            elif entry[0] == "user":
                lines.append({"type": "user", "timestamp": at(entry[1]), "message": {"role": "user", "content": entry[2]}})
        Path(path).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")

    def write(self):
        run = self.root / "run"
        run.mkdir(exist_ok=True)
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
                p = d / "test-runs" / ("%d.json" % n)
                el.write_json(p, dict({"schema": "tp-test-run:v1", "seconds": seconds}, **record))
                end = (T0 + timedelta(seconds=start + seconds)).timestamp()
                os.utime(p, (end, end))
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
                                         "start_commit": self.start_commit})
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

    def score(self):
        self.write()
        return score.score(self.root)

    def check(self, check):
        """(ok, detail) of one check against these records."""
        self.write()
        return score.evaluate_check(score.Run(self.root), dict({"id": "c", "measure": "evidence"}, **check))


class ScorerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ep-score-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def records(self, variant="positive"):
        self.n += 1
        return Records(self.tmp / ("r%d" % self.n), variant)


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

    def test_completed_unknown_without_final_check_not_zero(self):
        result = self._base(None).score()
        m = result["measures"]["completed"]
        self.assertIsNone(m["value"])
        self.assertIn("missing records: final_check: run/final-check.json", m["unknown"])
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
        return r.score()["measures"]["avoidable_waiting"]

    def test_owner_spoke_before_main_acted_is_avoidable(self):
        m = self._run(25)
        self.assertEqual((m["value"], m["seconds"]), (1, 10.0))

    def test_main_acted_first_is_not_avoidable(self):
        self.assertEqual(self._run(12)["value"], 0)

    def test_unknown_without_main_transcript(self):
        m = self._run(None)
        self.assertIsNone(m["value"])
        self.assertEqual(m["unknown"], "missing records: main_transcript: run.json lists no Main transcript")


class FalseClaimTests(ScorerCase):
    def test_main_done_claim_on_incomplete_work_and_worker_failed_tests(self):
        r = self.records()
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20", "measure": "evidence"}]
        (r.clone / "a.txt").write_text("quick: 20\n")
        r.final = {"exit": 1, "tree": "t1"}
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=[(5, 1, {"label": "npm test", "tree": "t0", "exit": 1})])
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
        r.final = {"exit": 0, "tree": "t1"}
        self.assertIsNone(r.score()["measures"]["false_claims"]["value"])


class VerificationTests(ScorerCase):
    def test_repeat_without_edit_counted_stale_and_exact_duplicates(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        transcript = [("tool", 1, "Bash", {"command": "npm test"}), ("tool", 2, "Bash", {"command": "npm run test"}),
                      ("tool", 3, "Edit", {"file_path": "a"}), ("tool", 4, "Bash", {"command": "npm test"}),
                      ("tool", 5, "Edit", {"file_path": "b"})]
        runs = [(1, 0.5, {"label": "npm test", "tree": "t0", "exit": 0}), (2, 0.5, {"label": "npm test", "tree": "t0", "exit": 0})]
        r.task("coder-1", created=0, result="complete", transcript=transcript, test_runs=runs, workdir=r.clone)
        r.observe(0.5, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")      # the result's time: the edit at 5 came after the last check
        m = r.score()["measures"]["verification"]
        # 1 and 2 ran on the same recorded tree t0: one repeat. 4 ran after an edit (a new generation of the work dir): not.
        self.assertEqual((m["value"], [x["by"] for x in m["repeats"]]), (1, ["tree"]))
        self.assertEqual((m["stale"], m["stale_tasks"], m["final_tree_verified"]), (1, ["coder-1"], False))

    def test_unknown_without_records(self):
        r = self.records()
        r.task("coder-1", created=0)
        self.assertIsNone(r.score()["measures"]["verification"]["value"])


class SafeToolTests(ScorerCase):
    def test_native_reads_counted_safe_and_private_ones_not(self):
        r = self.records()
        transcript = [("tool", 1, "Read", {"file_path": "src/a.ts"}), ("tool", 2, "mcp__tmux-pi-safe__safe_read", {"path": "src/a.ts"}),
                      ("tool", 3, "Bash", {"command": "cat src/a.ts | head -5"}), ("tool", 4, "Bash", {"command": "cat /tmp/x.log"}),
                      ("tool", 5, "pi_tool", {})]
        r.task("coder-1", created=0, transcript=transcript)
        r.task("coder-2", created=0)       # no transcript: the whole count is unknown (round 3 rule 1), not a partial 0
        r.observe(1, "coder-1", "working")
        r.observe(1, "coder-2", "working")
        m = r.score()["measures"]["safe_tools"]
        self.assertIsNone(m["value"])
        self.assertIn("worker_transcripts: transcript of coder-2", m["unknown"])
        r.tasks["coder-2"]["transcript"] = []     # observed working, transcript empty: a known 0 for coder-2
        m = r.score()["measures"]["safe_tools"]
        # `cat src/a.ts` reads a project file (head reads the pipe); /tmp/x.log is outside the clone; Read is native, not shell.
        self.assertEqual((m["workers"]["coder-1"]["value"], m["workers"]["coder-1"]["native_read_search"]), (1, 1))
        self.assertEqual((m["value"], m["workers"]["coder-2"]["value"]), (1, 0))

    def test_all_unknown_is_unknown(self):
        r = self.records()
        r.task("coder-1", created=0)
        self.assertIsNone(r.score()["measures"]["safe_tools"]["value"])


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
        r.task("coder-1", created=0, messages=[(11, "Also add a test.")], transcript=[])
        r.observe(10, "coder-1", "working")
        self.assertEqual(r.score()["measures"]["into_prompt"]["value"], 0)

    def test_unknown_without_observations(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480.")], transcript=[])
        m = r.score()["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("missing records: observations: observations of coder-1", m["unknown"])


class RecoveryTests(ScorerCase):
    def _run(self, main=True, unblocked=True):
        r = self.records()
        r.step(10, "compact", "compact")
        if main:
            r.said(13, "Back after compaction; the coder is still working.")
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
        self.assertIn("main_transcript", m["unknown"])


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
        self.assertIsNone(r.check({"check": "main_said", "pattern": "x"})[0])
        r.said(1, "x")
        ok, detail = r.check({"check": "main_said", "after": "nope", "pattern": "x"})
        self.assertIsNone(ok)
        self.assertIn("never ran", detail)

    def test_test_run_dated_by_mtime_minus_seconds(self):
        r = self.records()
        r.owner(10, "goal2", "goal")
        # Ended at 11 after 3 s: started at 8, before goal2. Without the start date it would count as run after goal2.
        r.task("coder-1", created=0, test_runs=[(8, 3, {"label": "npm run build", "tree": "t", "exit": 0})])
        check = {"check": "command", "by": "role:coder", "after": "goal2", "pattern": "npm run build"}
        ok, detail = r.check(check)
        self.assertIsNone(ok)            # no transcript: an absent command is not proven absent
        self.assertIn("missing records: worker_transcripts: transcript of coder-1", detail)
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
        self.assertTrue(detail.startswith("1 matching command"), detail)


class FinalTreeTests(ScorerCase):
    CHECK = {"check": "command", "by": "any", "pattern": "npm (run )?test", "final_tree": True}

    def test_worker_test_run_on_final_tree(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal"}
        r.main = []
        runs = [(5, 1, {"label": "npm test", "tree": "tfinal", "exit": 0})]
        r.task("coder-1", created=0, transcript=bash_of(runs), test_runs=runs)
        r.observe(1, "coder-1", "working")
        self.assertIs(r.check(self.CHECK)[0], True)

    def test_main_ran_tests_after_last_commit_on_clean_tree(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        runs = [(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})]
        r.task("coder-1", created=0, transcript=bash_of(runs), test_runs=runs)
        r.observe(1, "coder-1", "working")
        r.ran(31, "npm test")
        ok, detail = r.check(self.CHECK)
        self.assertIs(ok, True)
        self.assertIn("after the last commit", detail)

    def test_main_tests_before_last_commit_or_dirty_tree_do_not_count(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        runs = [(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})]
        r.task("coder-1", created=0, transcript=bash_of(runs), test_runs=runs)
        r.observe(1, "coder-1", "working")
        r.main = []
        self.assertIs(r.check(self.CHECK)[0], False)        # the only run has a recorded tree that is not the final one
        r.ran(29, "npm test")
        ok, detail = r.check(self.CHECK)                    # before the commit, with no tree recorded: not proof either way
        self.assertIsNone(ok)
        self.assertIn("no tree recorded", detail)
        r.final = {"exit": 0, "tree": "tfinal", "clean": False, "head_committed": at(30)}
        r.main = [("tool", 31, "Bash", {"command": "npm test"})]
        self.assertIsNone(r.check(self.CHECK)[0])           # after it, on a dirty tree: the tree it tested is not recorded

    def test_unknown_without_fingerprint_or_records(self):
        r = self.records()
        self.assertIsNone(r.check(self.CHECK)[0])
        r.final = {"exit": 0, "tree": "tfinal"}
        r.task("coder-1", created=0)            # no transcript, no test-run: cannot tell
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
        r.owner(11, "pause")
        self.assertIsNone(r.check({"check": "pause_backed", "role": "coder", "after": "pause"})[0])
        r.task("coder-1", created=1)
        r.observe(1, "coder-1", "working")
        ok, detail = r.check({"check": "pause_backed", "role": "coder", "after": "pause"})
        self.assertIsNone(ok)
        self.assertIn("missing records: main_transcript", detail)


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
        r.task("coder-1", created=1, transcript=[])
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
        self.assertIn("missing records: worker_transcripts: transcript of coder-1", detail)
        r.tasks["coder-1"]["transcript"] = []
        self.assertIsNone(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0])  # never seen run
        r.observe(1, "coder-1", "working")
        self.assertIs(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0], True)

    def test_queued_text_without_transcript_is_unknown_and_not_counted_known(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-1", transcript=None)
        r.observe(1, "coder-1", "working")
        r.task("coder-2", transcript=[])
        r.step(2, "draft", "queue_text", ok=True, task="coder-1", text="queued draft")
        result = r.score()
        m = result["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("worker_transcripts: transcript of coder-1", m["unknown"])
        el.write_json(self.tmp / "A" / "1.json", dict(result, run_id="A1"))
        data, _ = score.compare([str(self.tmp / "A" / "1.json")])
        row = data["arms"][str((self.tmp / "A").resolve())]["measures"]["into_prompt"]
        self.assertEqual((row["n"], row["unknown"]), (0, 1))

    def test_message_into_prompt_without_transcript_is_unknown_not_true(self):
        r = self.records()
        r.task("coder-1", created=0, messages=[(11, "Use 480.")])
        r.observe(10, "coder-1", "permission")
        m = r.score()["measures"]["into_prompt"]
        self.assertIsNone(m["value"])
        self.assertIn("worker_transcripts: transcript of coder-1", m["unknown"])


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
    """F5: redundant and stale verification by tree identity, across Main and the workers in time order."""

    def test_main_rerun_after_a_worker_edit_is_not_redundant(self):
        r = self.records()
        r.ran(1, "npm test")
        r.task("coder-1", workdir=r.clone, transcript=[("tool", 2, "Edit", {"file_path": "a.txt"})])
        r.ran(3, "npm test")
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)
        r.tasks["coder-1"]["workdir"] = None       # the edit's dir is unknown: it still separates the two runs
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)

    def test_main_rerun_of_the_workers_tested_tree_is_redundant(self):
        r = self.records()
        r.task("coder-1", workdir=r.clone, result="complete", transcript=[("tool", 2, "Bash", {"command": "npm test"})],
               test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})])
        r.ran(5, "npm test")
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["value"], m["repeats"][0]["who"], m["repeats"][0]["after"]), (1, "main", "coder-1"))

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
        self.assertEqual((m["value"], m["repeats"][0]["by"]), (1, "tree"))

    def test_commit_of_a_tested_tree_is_not_stale(self):
        r = self.records()
        r.main = []
        r.task("coder-1", workdir=r.clone, result="complete", test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})],
               transcript=[("tool", 2, "Bash", {"command": "npm test"}), ("tool", 4, "Bash", {"command": "git add -A && git commit -m change"})])
        r.observe(1, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 0)
        r.tasks["coder-1"]["transcript"].append(("tool", 5, "Bash", {"command": "sed -i '' s/a/b/ a.txt"}))
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 1)

    def test_unknown_when_a_stream_is_missing(self):
        r = self.records()
        r.ran(1, "npm test")
        r.ran(2, "npm test")
        r.task("coder-1", transcript=None)
        m = r.score()["measures"]["verification"]
        self.assertIsNone(m["value"])
        self.assertIn("worker_transcripts: transcript of coder-1", m["unknown"])


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
        r.main = []
        r.step(10, "compact", "compact")
        r.step(20, "end", "teardown")
        m = r.score()["measures"]["recovery"]
        self.assertEqual(m["value"], 1)
        self.assertIsNone(m["seconds"])
        self.assertIn("Main never acted after compact", m["seconds_unknown"])
        self.assertIn("recovery failures | 1 | Main never acted", score.markdown(r.score()))


class ShellReadTests(unittest.TestCase):
    """F8: shell reads count by path inside the project, including absolute paths under a /tmp scratch clone."""
    CLONE = "/tmp/scratch/runs/E1/project"
    PRIVATE = ("/tmp/scratch/runs/E1/project/.tmux-pi", "/tmp/scratch/runs/E1/run", "/dev")

    def reads(self, command, cwd=None):
        return el.shell_readers(command, cwd, (self.CLONE,), self.PRIVATE)

    def test_project_reads_count(self):
        for command, want in [
                ("rg MATCH_SHOT_LIMITS", ["rg"]), ("rg MATCH_SHOT_LIMITS src", ["rg"]), ("cat Makefile", ["cat"]),
                ("cat README.md", ["cat"]), ("sed -n 1,40p src/game/gameState.ts", ["sed"]), ("head -n 20 package.json", ["head"]),
                ("grep -rn quick", ["grep"]), ("grep -n quick src/a.ts", ["grep"]), ("grep -e quick -n src/a.ts", ["grep"]),
                ("cat %s/src/a.ts" % self.CLONE, ["cat"]), ("cat /private%s/src/a.ts" % self.CLONE, ["cat"]),
                ("find . -name '*.ts'", ["find"]), ("sed -n 1p < src/a.ts", ["sed"]), ("tail -n 3 <src/a.ts 2>/dev/null", ["tail"]), ("rg -g '*.ts' quick", ["rg"]), ("cd src && cat a.ts", ["cat"])]:
            self.assertEqual(self.reads(command), want, command)

    def test_private_stdin_home_and_outside_paths_do_not(self):
        for command in ("cat /tmp/scratch/runs/E1/project/.tmux-pi/sessions/x/tasks/c/packet.md", "cat /tmp/scratch/runs/E1/run/score.json",
                        "npm test 2>&1 | tail -20", "echo x | grep -c x", "grep quick", "cat ~/notes.txt", "cat /etc/hosts",
                        "awk '{print $1}'", "sed -n 1p < /dev/null"):
            self.assertEqual(self.reads(command), [], command)

    def test_measure_counts_absolute_scratch_paths_and_names_native_counts(self):
        tmp = Path(tempfile.mkdtemp(prefix="ep-score-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        r = Records(tmp / "r")
        clone = str(r.clone.resolve())
        r.task("coder-1", workdir=r.clone, transcript=[("tool", 1, "Bash", {"command": "rg -n quick %s/src" % clone}),
                                                        ("tool", 2, "Read", {"file_path": clone + "/a.ts"}),
                                                        ("tool", 3, "Grep", {"pattern": "x"})])
        m = r.score()["measures"]["safe_tools"]
        self.assertEqual((m["value"], m["native_read_search"]), (1, 2))
        self.assertNotIn("unsafe", json.dumps(m))


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
        r.tasks["coder-1"].update(messages=[(3, "Use 480.")], transcript=[])
        r.observe(2, "coder-1", "permission")
        result = r.score()
        self.assertEqual(result["measures"]["into_prompt"]["value"], 1)
        self.assertIs(result["expected_met"], False)
        self.assertEqual(result["expected_measures"][0]["ok"], False)
        r.expected = {"measures": {"into_prompt": 1}}
        result = r.score()
        # Every expected check and measure holds, but declared records are missing: expected_met is unknown, not true.
        self.assertIsNone(result["expected_met"])
        self.assertIn("missing records: ", result["expected_unknown"][0])
        self.assertIn("final_check: run/final-check.json", result["expected_unknown"][0])
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
        self.assertIn("main_transcript: Main transcript %s" % (r.root / "missing-pre-restart.jsonl"), detail)

    def test_missing_packet_or_project_file_is_unknown_not_absent(self):
        r = self.records()
        r.task("coder-1", transcript=[])
        r.observe(1, "coder-1", "working")
        r.write()
        (r.state / "tasks" / "coder-1" / "packet.md").unlink()
        run = score.Run(r.root)
        ok, detail = score.evaluate_check(run, {"check": "packet", "role": "coder", "pattern": "npm run build", "expect": False})
        self.assertIsNone(ok)
        self.assertIn("worker_packets: packet of coder-1", detail)
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
        self.assertIn("is not a git work tree", result["manifest"]["clone"][0])
        self.assertIsNone(result["measures"]["completed"]["value"])
        self.assertIn("missing records: clone: clone ", result["measures"]["completed"]["unknown"])
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
        self.assertIn("missing records: worker_packets: packet of coder-1", result["expected_unknown"][0])
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
        r = self.records()
        r.main = []
        r.task("coder-1", transcript=[])
        r.task("coder-2", transcript=[])
        r.observe(1, "coder-1", "working")
        r.step(20, "end", "teardown")
        m = r.score()["measures"]
        for name in ("recovery", "avoidable_waiting"):
            self.assertIsNone(m[name]["value"], name)
            self.assertIn("observations: observations of coder-2", m[name]["unknown"], name)

    def test_empty_transcript_is_evidence_only_when_the_log_shows_it_ran(self):
        check = {"check": "command", "by": "any", "pattern": "npm run build", "expect": False}
        r = self.records()
        r.main = []
        r.bounds = False                     # no main.ready: an empty Main transcript proves nothing
        r.log = [{"at": at(0), "kind": "run.start"}, {"at": at(9), "kind": "run.end"}]
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("Main transcript is empty and the log never shows Main ready", detail)
        r.log.insert(1, {"at": at(1), "kind": "main.ready", "ok": True})
        self.assertIs(r.check(check)[0], True)
        r.task("coder-1", transcript=[])      # never observed: its empty transcript is not a known absence
        ok, detail = r.check(check)
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-1 is empty and the log never saw it run", detail)
        r.observe(2, "coder-1", "starting")
        self.assertIsNone(r.check(check)[0])
        r.observe(3, "coder-1", "working")
        self.assertIs(r.check(check)[0], True)

    def test_an_empty_record_that_contradicts_the_log_is_missing(self):
        r = self.records()
        r.main = []
        r.owner(1, "goal", "goal")
        runs = [(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})]
        r.task("coder-1", result="complete", transcript=bash_of(runs), test_runs=runs, events=[{"kind": "state", "at": at(2)}],
               requests=[{"id": "q1"}])
        r.observe(2, "coder-1", "input")
        r.write()
        main = Path(el.read_json(r.root / "run" / "run.json")["main_transcripts"][0])
        task = r.state / "tasks" / "coder-1"
        self.assertEqual(score.Run(r.root).manifest["main_transcript"], [])
        for path, kind, want in ((main, "main_transcript", "empty but the log shows owner turns sent to Main"),
                                 (task / "transcript.jsonl", "worker_transcripts", "empty but it wrote test-run records"),
                                 (task / "events.jsonl", "events", "empty and the log saw it run"),
                                 (task / "packet.md", "worker_packets", "packet of coder-1")):
            keep = path.read_text()
            path.write_text("")
            self.assertIn(want, "; ".join(score.Run(r.root).manifest[kind]), kind)
            path.write_text(keep)
        for p in (task / "requests").glob("*.json"):
            p.unlink()
        self.assertIn("is empty and the log saw it at a prompt", score.Run(r.root).manifest["requests"][0])
        shutil.rmtree(task)                     # observed in the log with no task dir: the state dir lost it
        run = score.Run(r.root)
        self.assertIn("state_dir: task dir of coder-1 (observed in the log)", run.incomplete())

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
            self.assertIn("incomplete-records: episode_log: run/episode-log.jsonl", m["unknown"], name)
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
            self.assertIn("incomplete-records: state_dir", detail)
        self.assertEqual(score.score(r.root)["records"], "incomplete-records")

    def test_partial_events_requests_or_results_are_unknown(self):
        r = self.records()
        r.main = []
        r.task("coder-1", events=[], requests=[{"id": "req1"}], transcript=[], result="complete")
        r.task("coder-2", events=None, requests=None, transcript=[])
        r.observe(1, "coder-1", "input")
        r.observe(1, "coder-2", "input")
        r.write()
        run = score.Run(r.root)
        for check, want in (({"check": "event", "event": "pause.confirmed", "expect": False}, "events: events.jsonl of coder-2"),
                            ({"check": "requests_distinct", "count": 2}, "requests: requests/ of coder-2"),
                            ({"check": "result_complete", "role": "coder"}, "worker_results: result of coder-2")):
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
        r.task("coder-1")
        result = r.score()
        self.assertEqual(result["records"], "partial")
        self.assertEqual(result["manifest"]["observations"], ["observations of coder-1"])
        self.assertEqual(result["manifest"]["worker_transcripts"], ["transcript of coder-1"])
        self.assertEqual(result["records_declared"], el.required_records(json.loads((r.root / "run" / "episode.json").read_text())))


class TaskIdentityTests(ScorerCase):
    """Rule 2 (F3): tasks are keyed by id; a result counts unless a task names it as its parent (meta.parent)."""

    def _run(self, parent=None):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.task("coder-a", created=1, result="blocked", transcript=[])
        r.task("coder-b", created=2, result="complete", transcript=[], parent=parent)
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
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", transcript=[],
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
        r.task("coder-1", created=1, result="complete", transcript=[], test_runs=[(3, 1, {"label": "npm test", "exit": 0, "tree": "t1"})])
        r.observe(1, "coder-1", "working")
        r.observe(5, "coder-1", "waiting", result="complete")
        r.said(6, "Step one is done.")
        r.task("coder-2", created=10, result="blocked", transcript=[])
        r.observe(10, "coder-2", "working")
        r.observe(15, "coder-2", "waiting", result="blocked")
        r.final = {"exit": 1, "tree": "t2"}
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["value"], m["claims"][0]["false"]), (0, False))

    def test_final_check_judges_a_claim_only_when_nothing_changed_after_it(self):
        r = self.records()
        r.owner(0, "goal", "goal")
        r.said(5, "Done.")
        r.final = {"exit": 1, "tree": "t1"}
        m = r.score()["measures"]["false_claims"]
        self.assertEqual(m["value"], 1)
        self.assertIn("the final check (exit 1", m["claims"][0]["why"])
        r.main.append(("tool", 6, "Edit", {"file_path": "a"}))        # a write after the claim: the final tree is not the claim's
        m = r.score()["measures"]["false_claims"]
        self.assertIsNone(m["value"])
        self.assertIn("no task result or check record by then", m["unknown"])

    def test_undated_execution_cannot_back_a_check(self):
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
        ok, detail = score.evaluate_check(score.Run(r.root), {"check": "command", "by": "main", "after": "goal",
                                                              "pattern": "npm run build"})
        self.assertIsNone(ok)
        self.assertIn("ran with no time", detail)


class CommandIdentityTests(ScorerCase):
    """Rule 4 (F6, F7): one identity per execution; text in arguments is never one; a label never hides the cmd."""

    def test_mention_in_an_argument_is_not_an_execution(self):
        r = self.records()
        r.ran(1, "grep -n 'npm run build' README.md")
        r.ran(2, "echo npm run build && rg 'tsc -b' src")
        self.assertIs(r.check({"check": "command", "by": "any", "pattern": "npm run build|tsc -b", "expect": False})[0], True)
        r.ran(3, "cd sub && FOO=1 npm run build 2>&1 | tail -5")
        self.assertIs(r.check({"check": "command", "by": "any", "pattern": "npm run build", "expect": False})[0], False)

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
        self.assertIs(r.check(check)[0], False)

    def test_distinct_arguments_are_distinct_commands(self):
        r = self.records()
        r.ran(1, "npm test -- --run src/a.test.ts")
        r.ran(2, "npm test -- --run src/b.test.ts")
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)
        r.ran(3, "npm test -- --run src/b.test.ts")
        self.assertEqual(r.score()["measures"]["verification"]["value"], 1)

    def test_write_and_check_in_one_call_records_the_write_first(self):
        r = self.records()
        r.ran(1, "npm test")
        r.ran(3, "sed -i '' s/18/20/ src/a.ts && npm test")
        self.assertEqual(r.score()["measures"]["verification"]["value"], 0)
        self.assertEqual([(kind, text.split()[0]) for kind, text in el.command_events("sed -i '' s/18/20/ src/a.ts && npm test")],
                         [("write", "sed"), ("check", "npm")])

    def test_edit_then_revert_on_the_tested_tree_is_verified_not_stale(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", workdir=r.clone, result="complete", test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})],
               transcript=[("tool", 2, "Bash", {"command": "npm test"}), ("tool", 4, "Edit", {"file_path": "a"}),
                           ("tool", 5, "Edit", {"file_path": "a"})])
        r.observe(1, "coder-1", "working")
        r.observe(6, "coder-1", "waiting", result="complete")
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["stale"], m["final_tree_verified"]), (0, True))
        r.final = {"exit": 0, "tree": "t2"}                 # the final tree is not the tested one: stale
        self.assertEqual(r.score()["measures"]["verification"]["stale"], 1)

    def test_missing_workdir_makes_stale_unknown(self):
        r = self.records()
        r.main = []
        r.task("coder-1", result="complete", transcript=[("tool", 1, "Bash", {"command": "npm test"}), ("tool", 2, "Edit", {"file_path": "a"})])
        r.observe(1, "coder-1", "working")
        r.observe(3, "coder-1", "waiting", result="complete")
        m = r.score()["measures"]["verification"]
        self.assertIsNone(m["stale"])
        self.assertIn("coder-1: no work dir recorded", m["stale_unknown"])

    def test_one_final_tree_record_does_not_cover_an_unrecorded_stream(self):
        r = self.records()
        r.main = []
        r.final = {"exit": 0, "tree": "t1"}
        r.task("coder-1", transcript=[], test_runs=[(2, 1, {"label": "npm test", "tree": "t1", "exit": 0})])
        r.task("coder-2", transcript=None)
        r.observe(1, "coder-1", "working")
        r.observe(1, "coder-2", "working")
        ok, detail = r.check({"check": "command", "by": "any", "pattern": "npm run build", "final_tree": True})
        self.assertIsNone(ok)
        self.assertIn("transcript of coder-2", detail)


class ShellReadDiagnosticTests(unittest.TestCase):
    """Rule 5 (F8): shell-read counts are diagnostic; only file operands (and -f/--file) count, against the tracked cwd."""
    CLONE = "/tmp/runs/E1/project"

    def reads(self, command):
        return el.shell_read_scan(command, self.CLONE, (self.CLONE,), (self.CLONE + "/.tmux-pi", "/tmp/runs/E1/run"))

    def test_operands_against_the_tracked_cwd(self):
        for command, want in [("npm test 2>&1 | rg FAIL", []), ("echo ok | rg ok", []), ("cd /tmp && cat unrelated.log", []),
                              ("cd /tmp && cat runs/E1/project/README.md", ["cat"]),
                              ("cd / && cat tmp/runs/E1/project/README.md", ["cat"]),
                              ("rg -f patterns.txt /etc/hosts", ["rg"]), ("grep --file=pats.txt x /etc/hosts", ["grep"]),
                              ("cat .tmux-pi/worker-preamble.md", [])]:
            self.assertEqual(self.reads(command)[0], want, command)

    def test_unhandled_shapes_are_uncounted(self):
        for command in ("ls src | xargs cat", "cat $(git ls-files)", "for f in src/*; do cat $f; done"):
            self.assertGreaterEqual(self.reads(command)[1], 1, command)
        self.assertIn("uncounted", score.SHELL_READ_LIMITS)

    def test_never_expected(self):
        self.assertNotIn("safe_tools", el.EXPECTABLE_MEASURES)
        episode = {"schema": el.SCHEMA, "id": "x", "row": "E1", "variant": "positive", "title": "t", "project": {},
                   "steps": [{"id": "goal", "do": "owner", "tag": "goal", "text": "Do it."}],
                   "expected": {"records": list(el.BASE_RECORDS), "checks": [{"id": "c", "check": "main_said", "pattern": "ok"}],
                                "measures": {"safe_tools": 0}}}
        with self.assertRaisesRegex(el.EpisodeError, "expected measure safe_tools"):
            el.validate_episode(episode)


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

    def _fake(self, script):
        done = self._run("--fake", "--fake-script", script, "--timeout-scale", "0.2", "--final-timeout", "20")
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
        self.assertEqual((m["completed"]["value"], m["false_claims"]["value"], m["safe_tools"]["value"]), (True, 0, 0))

    def test_fake_fail_wait_fails_still_tears_down_and_scores_the_failure(self):
        run_dir, record, _ = self._fake("fail")
        self.assertEqual(record["outcome"], "wait-failed")
        result = score.score(run_dir)
        m = result["measures"]
        self.assertEqual((m["completed"]["value"], m["false_claims"]["value"]), (False, 1))
        self.assertIs(result["expected_met"], False)

    def test_queue_text_never_targets_a_dead_preserved_pane(self):
        """F1, F2: the coder finished and its process died, its pane kept (remain-on-exit, as tp-spawn sets it) and its last
        state still `waiting`. The draft must not be pasted there: the plant is missed, the run stops, the score says so."""
        dead = json.loads(json.dumps(MINI))
        dead["id"] = "MINI-dead"
        dead["steps"] = dead["steps"][:2] + [
            {"id": "gone", "do": "wait", "timeout": 25, "on_timeout": "fail", "until": {"kind": "main_quiet", "seconds": 2}},
            {"id": "draft", "do": "queue_text", "role": "coder", "text": "a queued draft"},
            {"id": "after", "do": "owner", "tag": "followup", "text": "Anything else?"}]
        dead["expected"] = {"checks": [{"id": "draft-kept", "check": "state_seen", "role": "coder", "state": "waiting",
                                        "needs": ["draft"]}], "measures": {"into_prompt": 0}}
        dead["expected"]["records"] = el.required_records(dead)
        dead["fake"]["pass"]["on"]["goal"].append(
            {"bash": "tmux set-option -w -t coder-mini remain-on-exit on; pkill -f 'worker.py.*tasks/coder-mini/'; sleep 1"})
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
        self.assertEqual(el.read_json(run_dir / "run" / "run.json")["outcome"], "plant-missed")
        self.assertIn("teardown", [e["kind"] for e in log])
        result = score.score(run_dir)
        self.assertTrue(result["plant_missed"])
        self.assertIsNone(result["checks"][0]["ok"])
        self.assertIsNone(result["measures"]["into_prompt"]["value"])
        self.assertIsNot(result["expected_met"], True)


if __name__ == "__main__":
    unittest.main()
