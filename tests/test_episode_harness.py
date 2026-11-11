import session_env  # noqa: F401  (first: drops the caller's tmux/task session before anything else runs)

"""HB8a episode harness: runner plan and teardown (fake mode, private tmux server), episode validation, and the scorer on
hand-made records for every measure, including the negative variants and missing telemetry (unknown, never 0).

No model is started and nothing goes to the network. The runner tests start tmux only through tools/episodes/run.py, which
uses a fresh TMUX_TMPDIR and its own `-L` socket and kills only that server."""
import json
import os
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


class Records:
    """A run directory written by hand: run/{run.json, episode.json, episode-log.jsonl, final-check.json}, a session state dir
    with task records, an optional Main transcript, and a clone directory (not a git repository)."""

    def __init__(self, root, variant="positive"):
        self.root = Path(root)
        self.state = self.root / "state"
        self.clone = self.root / "proj"
        self.clone.mkdir(parents=True)
        self.checks, self.log, self.tasks = [], [], {}
        self.main = None            # None: no Main transcript listed; [] listed and empty
        self.final = None
        self.variant = variant
        self.session_events = None

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

    def task(self, ident, role="coder", lane=None, created=0, packet="", messages=(), test_runs=(), transcript=None,
             events=None, requests=None, result=None):
        self.tasks[ident] = {"role": role, "lane": lane, "created": created, "packet": packet, "messages": list(messages),
                             "test_runs": list(test_runs), "transcript": transcript, "events": events, "requests": requests,
                             "result": result}

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
            el.write_json(d / "meta.json", {"role": t["role"], "lane": t["lane"], "created": whole(t["created"]), "worker": "claude"})
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
            self._transcript(path, self.main)
            transcripts = [str(path)]
        episode = {"schema": el.SCHEMA, "id": "T", "row": "T", "variant": self.variant, "title": "t", "project": {},
                   "steps": [], "expected": {"checks": self.checks}}
        el.write_json(run / "episode.json", episode)
        el.write_json(run / "run.json", {"schema": el.RUN_SCHEMA, "run_id": "T-1", "episode": "T", "row": "T",
                                         "variant": self.variant, "mode": "fake", "fake_script": "pass", "outcome": "played",
                                         "clone": str(self.clone), "state_dir": str(self.state), "main_transcripts": transcripts})
        if self.final is not None:
            el.write_json(run / "final-check.json", self.final)
        elif (run / "final-check.json").exists():
            (run / "final-check.json").unlink()      # written again after a change: the record is gone, not stale
        (run / "episode-log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in self.log), encoding="utf-8")
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
        self.assertEqual(m["unknown"], "no final check")
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
        self.assertEqual(m["unknown"], "no Main transcript")


class FalseClaimTests(ScorerCase):
    def test_main_done_claim_on_incomplete_work_and_worker_failed_tests(self):
        r = self.records()
        r.checks = [{"id": "landed", "check": "change_present", "path": "a.txt", "pattern": "quick: 20", "measure": "evidence"}]
        r.final = {"exit": 0, "tree": "t1"}
        r.owner(0, "goal", "goal")
        r.task("coder-1", created=1, result="complete", test_runs=[(5, 1, {"label": "npm test", "tree": "t0", "exit": 1})])
        r.said(10, "Done: quick matches are 20 shots.")
        r.said(11, "The README is not done yet.")
        m = r.score()["measures"]["false_claims"]
        self.assertEqual((m["value"], m["main_false"], m["worker_false"]), (2, 1, 1))
        self.assertEqual(m["claims"], ["Done: quick matches are 20 shots."])

    def test_unknown_without_main_transcript(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        self.assertIsNone(r.score()["measures"]["false_claims"]["value"])


class VerificationTests(ScorerCase):
    def test_repeat_without_edit_counted_stale_and_exact_duplicates(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "t1"}
        transcript = [("tool", 1, "Bash", {"command": "npm test"}), ("tool", 2, "Bash", {"command": "npm run test"}),
                      ("tool", 3, "Edit", {"file_path": "a"}), ("tool", 4, "Bash", {"command": "npm test"}),
                      ("tool", 5, "Edit", {"file_path": "b"})]
        runs = [(1, 0.5, {"label": "npm test", "tree": "t0", "exit": 0}), (2, 0.5, {"label": "npm test", "tree": "t0", "exit": 0})]
        r.task("coder-1", created=0, result="complete", transcript=transcript, test_runs=runs)
        m = r.score()["measures"]["verification"]
        self.assertEqual((m["redundant_derived"], m["redundant_exact"], m["value"]), (1, 1, 1))
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
        r.task("coder-2", created=0)       # no transcript: unknown for this task, not 0
        m = r.score()["measures"]["safe_tools"]
        self.assertEqual(m["value"], 2)
        self.assertIsNone(m["workers"]["coder-2"]["value"])
        self.assertIn("coder-2", m["unknown"])

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
        self.assertEqual(m["unknown"], "no observations")


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
        self.assertIsNone(m["events"][0]["main_acted"])


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
        self.assertIn("no matching test-run", detail)
        r.tasks["coder-1"]["test_runs"].append((12, 3, {"label": "npm run build", "tree": "t", "exit": 0}))
        self.assertIs(r.check(check)[0], True)
        r.tasks["coder-1"]["transcript"] = []    # transcript known and empty, the only test-run before goal2: not run after it
        r.tasks["coder-1"]["test_runs"].pop()
        self.assertIs(r.check(check)[0], False)

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
        r.task("coder-1", created=0, test_runs=[(5, 1, {"label": "npm test", "tree": "tfinal", "exit": 0})])
        self.assertIs(r.check(self.CHECK)[0], True)

    def test_main_ran_tests_after_last_commit_on_clean_tree(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        r.task("coder-1", created=0, test_runs=[(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})])
        r.ran(31, "npm test")
        ok, detail = r.check(self.CHECK)
        self.assertIs(ok, True)
        self.assertIn("after the last commit", detail)

    def test_main_tests_before_last_commit_or_dirty_tree_do_not_count(self):
        r = self.records()
        r.final = {"exit": 0, "tree": "tfinal", "clean": True, "head_committed": at(30)}
        r.task("coder-1", created=0, test_runs=[(5, 1, {"label": "npm test", "tree": "tlane", "exit": 0})])
        r.ran(29, "npm test")
        self.assertIs(r.check(self.CHECK)[0], False)
        r.final = {"exit": 0, "tree": "tfinal", "clean": False, "head_committed": at(30)}
        r.main = [("tool", 31, "Bash", {"command": "npm test"})]
        self.assertIs(r.check(self.CHECK)[0], False)

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
        self.assertIn("no Main transcript", detail)


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
                "expected": {"checks": [{"id": "c", "check": "main_said", "pattern": "ok"}]}}

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
        {"id": "tests-final", "check": "command", "by": "any", "pattern": "run-tests", "final_tree": True}]},
    "fake": {
        "pass": {"start": [{"say": "Ready."}], "on": {"goal": [
            {"spawn": {"id": "coder-mini", "role": "coder", "packet": "Set quick to 20 in value.txt. Run {test}.",
                       "steps": [{"edit": {"path": "value.txt", "old": "quick: 18", "new": "quick: 20"}},
                                 {"test_run": "{test}"}, {"result": "complete", "text": "quick is 20; {test} passed."}]}},
            {"wait_task": {"id": "coder-mini", "state": "waiting", "timeout": 30}},
            {"say": "Done: quick is 20 and the test passed."}]}},
        "fail": {"start": [{"say": "Ready."}], "on": {"goal": [{"say": "Done."}]}}},
}


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


if __name__ == "__main__":
    unittest.main()
