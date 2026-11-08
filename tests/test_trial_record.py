"""Qwen-trial record: built from a fixture task dir; the first review decides, not the last."""

import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import trial_record  # noqa: E402

COVERAGE = "## Coverage\nReviewed lib/trial_record.py and tests/test_trial_record.py in full.\n"
APPROVED = "APPROVED\nRESULT_STATUS: complete\n" + COVERAGE
WITH_NOTES = "APPROVED_WITH_NOTES\nRESULT_STATUS: complete\nN1 a naming note.\n" + COVERAGE
CHANGES = ("REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 [defect] wrong.\nFamily: state-matrix\nCloses by: code\n"
           "F2 [defect] also wrong.\nFamily: error-path\nCloses by: code\n" + COVERAGE)
LABEL_ONLY = "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 a note.\nFamily: docs\nCloses by: check\n" + COVERAGE
NO_COVERAGE = "APPROVED\nRESULT_STATUS: complete\n"


class Fixture:
    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        (self.state / "tasks").mkdir(parents=True)

    def task(self, ident, meta=None, state=None, **files):
        d = self.state / "tasks" / ident
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"id": ident, "created": "2026-10-31T10:00:00Z", **(meta or {})}), encoding="utf-8")
        if state is not None:
            (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
        for name, content in files.items():
            (d / name.replace("__", ".")).write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return d

    def review(self, ident, text, model="gpt-5"):
        return self.task(ident, {"worker": "pi", "model": model, "thinking": "high", "role": "code-reviewer"}, result__md=text)

    def author(self, reviews, **extra):
        meta = {"worker": "pi", "model": "lmstudio/qwen/qwen3.6-35b-a3b", "thinking": "off", "role": "coder", "reviews": reviews,
                "review": reviews[-1] if reviews else None, "reviewer": {"worker": "pi", "model": "gpt-5", "thinking": "high"}, **extra}
        return self.task("author", meta,
                         {"state": "completed", "updated": "2026-10-31T10:12:30Z", "turns": 14, "toolErrors": 2,
                          "tokens": {"input": 1000, "output": 400, "cacheRead": 5, "cacheWrite": 0}},
                         request__md="x" * 321, dispatch__json={"context": ["a.py", "b.py:1-9"], "notes": [], "tests": "targeted",
                                                               "base_commit": "abc1234"}, exit="0\n")

    def build(self, **kw):
        return trial_record.build(self.state / "tasks" / "author", kw.pop("arm", "qwen"), kw.pop("mode", "shadow"),
                                  kw.pop("pair_id", "p1"), now="2026-10-31T11:00:00Z", **kw)


class VerdictTests(unittest.TestCase):
    def outcome(self, text):
        fx = Fixture(self)
        fx.review("rev1", text)
        fx.author(["rev1"])
        return fx.build()

    def test_approved(self):
        r = self.outcome(APPROVED)
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"], r["review_rounds"]), ("APPROVED", "approved", 1))

    def test_approved_with_notes(self):
        r = self.outcome(WITH_NOTES)
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"]), ("APPROVED_WITH_NOTES", "approved"))

    def test_request_changes_gives_its_families(self):
        r = self.outcome(CHANGES)
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"]), ("REQUEST_CHANGES", "request_changes"))
        self.assertEqual(r["finding_categories"], ["state-matrix", "error-path"])
        self.assertEqual(r["finding_closes_by"], ["code", "code"])

    def test_a_review_without_coverage_is_inconclusive_not_an_approval(self):
        r = self.outcome(NO_COVERAGE)
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"]), ("incomplete", "inconclusive"))

    def test_request_changes_without_a_code_finding_is_inconclusive(self):
        r = self.outcome(LABEL_ONLY)
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"]), ("UNUSABLE", "inconclusive"))

    def test_a_reply_that_is_no_verdict_is_unparsed(self):
        r = self.outcome("I looked at it and it seems fine.\n")
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"]), ("unparsed", "inconclusive"))

    def test_a_review_with_no_result_yet_has_no_outcome_but_counts_as_a_round(self):
        fx = Fixture(self)
        fx.task("rev1", {"worker": "pi", "model": "gpt-5", "thinking": "high"})
        fx.author(["rev1"])
        r = fx.build()
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"], r["review_rounds"]), (None, None, 1))

    def test_no_review_at_all(self):
        fx = Fixture(self)
        fx.author([])
        r = fx.build()
        self.assertEqual((r["first_review_verdict"], r["first_review_outcome"], r["review_rounds"], r["finding_categories"]),
                         (None, None, 0, []))

    def test_the_first_review_is_used_and_not_the_last(self):
        fx = Fixture(self)
        fx.review("rev1", CHANGES, model="gpt-5")
        fx.review("rev2", APPROVED, model="claude-opus")
        fx.author(["rev1", "rev2"])
        r = fx.build()
        self.assertEqual((r["first_review_verdict"], r["review_rounds"], r["reviewer_model"]), ("REQUEST_CHANGES", 2, "gpt-5"))
        self.assertEqual(r["finding_categories"], ["state-matrix", "error-path"])

    def test_a_single_review_field_without_a_reviews_list_is_one_round(self):
        fx = Fixture(self)
        fx.review("rev1", APPROVED)
        fx.author(["rev1"])
        meta_path = fx.state / "tasks" / "author" / "meta.json"
        meta = json.loads(meta_path.read_text())
        del meta["reviews"]
        meta_path.write_text(json.dumps(meta))
        self.assertEqual(fx.build()["review_rounds"], 1)


class FieldTests(unittest.TestCase):
    def test_summary_fields(self):
        fx = Fixture(self)
        fx.review("rev1", APPROVED)
        fx.author(["rev1"])
        r = fx.build(diff_files=3, diff_lines=120)
        self.assertEqual((r["pair_id"], r["arm"], r["mode"], r["role"], r["task_id"]), ("p1", "qwen", "shadow", "coder", "author"))
        self.assertEqual((r["model"], r["effort"]), ("lmstudio/qwen/qwen3.6-35b-a3b", "off"))
        self.assertEqual((r["state"], r["exit"], r["turns"], r["tool_errors"], r["wall_time_s"]), ("completed", 0, 14, 2, 750))
        self.assertEqual(r["tokens"], {"input": 1000, "output": 400, "cacheRead": 5, "cacheWrite": 0})
        self.assertEqual((r["brief_size_bytes"], r["context_files"], r["base_commit"]), (321, 2, "abc1234"))
        self.assertEqual((r["size"], r["diff"]), ("medium", {"files": 3, "lines": 120}))
        self.assertEqual(r["created"], "2026-10-31T11:00:00Z")

    def test_size_buckets(self):
        cases = [((1, 10), "small"), ((2, 50), "small"), ((3, 50), "medium"), ((2, 51), "medium"), ((5, 200), "medium"),
                 ((6, 10), "large"), ((1, 201), "large"), ((None, 5), None), ((1, None), None), ((-1, 3), None), ((True, 3), None)]
        for (files, lines), want in cases:
            with self.subTest(files=files, lines=lines):
                self.assertEqual(trial_record.size_bucket(files, lines), want)

    def test_the_record_holds_no_transcript_or_command_text(self):
        fx = Fixture(self)
        fx.author([])
        transcript = Path(fx.tmp.name) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "python3 -c 'SECRET-MARKER'"}}]}}) + "\n", encoding="utf-8")
        (fx.state / "tasks" / "author" / "transcript-path").write_text(str(transcript), encoding="utf-8")
        text = json.dumps(fx.build())
        self.assertNotIn("SECRET-MARKER", text)
        self.assertIn("inline_script", text)

    def test_replay_keeps_brief_drift_and_shadow_drops_it(self):
        fx = Fixture(self)
        fx.author([])
        self.assertEqual(fx.build(mode="replay", brief_drift={"changed": True})["brief_drift"], {"changed": True})
        self.assertIsNone(fx.build(mode="shadow", brief_drift={"changed": True})["brief_drift"])

    def test_bad_arguments_are_refused(self):
        fx = Fixture(self)
        fx.author([])
        for kw in ({"arm": "gpt"}, {"mode": "live"}, {"pair_id": " "}):
            with self.subTest(kw=kw), self.assertRaises(trial_record.RecordError):
                fx.build(**kw)
        with self.assertRaises(trial_record.RecordError):
            trial_record.build(fx.state / "tasks" / "nope", "qwen", "shadow", "p1")

    def test_a_task_record_without_a_worker_is_a_record_error_not_a_key_error(self):
        fx = Fixture(self)
        fx.task("author", {"model": "m"})
        with self.assertRaises(trial_record.RecordError):
            fx.build()

    def test_the_normal_arm_is_the_same_shape(self):
        fx = Fixture(self)
        fx.author([])
        self.assertEqual(set(fx.build(arm="normal")), set(fx.build(arm="qwen")))


class SlipFieldTests(unittest.TestCase):
    def test_unscanned_transcript_makes_slips_incomplete_but_shows_a_leak(self):
        fx = Fixture(self)
        d = fx.author([])
        (d / "transcript.jsonl").write_text("{}\n", encoding="utf-8")  # pi: schema not pinned
        (d / "result.md").write_text("<think>hm</think> done", encoding="utf-8")
        r = fx.build()
        self.assertEqual(r["rule_slips"], {"complete": False, "total": 1, "by_rule": {"think_leak": 1}})
        self.assertIsNone(r["gate_runs"])
        self.assertEqual(r["transcript"]["status"], "unknown")

    def test_commit_message_slips_join_the_transcript_slips(self):
        fx = Fixture(self)
        d = fx.author([])
        transcript = Path(fx.tmp.name) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "git push"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "python3 -m unittest x"}}]}}) + "\n", encoding="utf-8")
        (d / "transcript-path").write_text(str(transcript), encoding="utf-8")
        r = fx.build(commit_messages=["fix it\n"])
        self.assertEqual(r["rule_slips"], {"complete": True, "total": 3,
                                           "by_rule": {"commit_subject": 1, "commit_trailer": 1, "git_push": 1}})
        self.assertEqual(r["gate_runs"], {"complete": True, "total": 1, "uncertain": 0, "denied": 0,
                                          "unattributed": 1, "parse_failed": 0, "runs": [{"kind": "test", "exit": None}]})

    def test_a_transcript_with_a_skipped_line_is_incomplete_for_slips_and_gates(self):
        fx = Fixture(self)
        d = fx.author([])
        transcript = Path(fx.tmp.name) / "t.jsonl"
        transcript.write_text("{broken\n" + json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "git push"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm test && npm run lint"}}]}}) + "\n", encoding="utf-8")
        (d / "transcript-path").write_text(str(transcript), encoding="utf-8")
        r = fx.build()
        self.assertEqual(r["rule_slips"], {"complete": False, "total": 1, "by_rule": {"git_push": 1}})
        self.assertEqual((r["gate_runs"]["complete"], r["gate_runs"]["total"], r["gate_runs"]["uncertain"], r["gate_runs"]["unattributed"]),
                         (False, 1, 1, 1))
        self.assertEqual((r["transcript"]["status"], r["transcript"]["bad_lines"]), ("incomplete", 1))

    def test_an_unreadable_bash_call_is_counted_in_the_record_not_dropped(self):  # review 5: fail visible
        fx = Fixture(self)
        d = fx.author([])
        transcript = Path(fx.tmp.name) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "git commit -m 'don't' && npm test"}},
            {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "npm run lint"}}]}}) + "\n", encoding="utf-8")
        (d / "transcript-path").write_text(str(transcript), encoding="utf-8")
        gates = fx.build()["gate_runs"]
        self.assertEqual((gates["total"], gates["parse_failed"], gates["complete"]), (1, 1, True))
        self.assertEqual(gates["runs"][0], {"kind": None, "exit": None, "parse_failed": True})

    def test_no_transcript_means_unknown_slips(self):
        fx = Fixture(self)
        fx.author([])
        self.assertEqual(fx.build()["rule_slips"], {"complete": False, "total": 0, "by_rule": {}})


class DocsCallsTests(unittest.TestCase):
    def read(self, text):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "docs-calls.jsonl"
        path.write_text(text, encoding="utf-8")
        return trial_record.read_docs_calls(path)

    def test_calls_are_counted_with_target_symbol_hit_and_bytes(self):
        out = self.read(json.dumps({"ts": "t", "tool": "api_signature", "args": {"package": "zod", "symbol": "z.object"}, "hit": True, "bytes": 900}) + "\n"
                        + json.dumps({"tool": "docs_search", "args": {"lib": "hono", "query": "cors"}, "result": "miss", "bytes": 40}) + "\n")
        self.assertEqual((out["count"], out["hits"], out["bytes"], out["bad_lines"]), (2, 1, 940, 0))
        self.assertEqual(out["calls"][0], {"tool": "api_signature", "target": "zod", "symbol": "z.object", "hit": True, "bytes": 900})
        self.assertEqual(out["calls"][1], {"tool": "docs_search", "target": "hono", "symbol": "cors", "hit": False, "bytes": 40})

    def test_torn_and_non_object_lines_are_bad_not_calls(self):
        out = self.read('{"tool": "api_signature"}\n{"tool": "docs_se\n[1]\n\n')
        self.assertEqual((out["count"], out["bad_lines"]), (1, 2))
        self.assertEqual(out["calls"][0]["hit"], None)

    def test_missing_file_is_zero_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = trial_record.read_docs_calls(Path(tmp) / "none.jsonl")
        self.assertEqual((out["count"], out["calls"], out["bad_lines"]), (0, [], 0))

    def test_per_call_rows_are_capped_but_the_count_is_not(self):
        total = trial_record.MAX_DOCS_CALLS + 7
        out = self.read(("{\"tool\": \"docs_search\", \"bytes\": 1}\n") * total)
        self.assertEqual((out["count"], len(out["calls"]), out["bytes"]), (total, trial_record.MAX_DOCS_CALLS, total))

    def test_build_reads_docs_calls_from_the_task_dir(self):
        fx = Fixture(self)
        d = fx.author([])
        (d / "docs-calls.jsonl").write_text('{"tool": "api_signature", "hit": true, "bytes": 5}\n', encoding="utf-8")
        self.assertEqual(fx.build()["docs_calls"]["count"], 1)


class AppendTests(unittest.TestCase):
    def test_two_writers_lose_and_tear_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            per = 40
            start = threading.Barrier(2)
            errors = []

            def write(name):
                try:
                    start.wait(timeout=10)
                    for i in range(per):
                        trial_record.append(tmp, {"pair_id": "{}-{}".format(name, i), "arm": "qwen", "pad": "x" * 2000})
                except Exception as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(n,)) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            self.assertEqual(errors, [])
            rows, bad = trial_record.read_records(trial_record.trial_dir(tmp) / trial_record.RECORDS)
            self.assertEqual((len(rows), bad), (2 * per, 0))
            self.assertEqual(len({r["pair_id"] for r in rows}), 2 * per)

    def test_records_land_under_the_project_trial_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial_record.append(tmp, {"pair_id": "p", "arm": "normal"})
            self.assertTrue((Path(tmp) / ".tmux-pi" / "trial" / "records.jsonl").is_file())

    def test_read_records_skips_non_objects_and_torn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.jsonl"
            path.write_text('{"arm": "qwen"}\n[1]\n{"arm"\n', encoding="utf-8")
            rows, bad = trial_record.read_records(path)
            self.assertEqual((rows, bad), ([{"arm": "qwen"}], 2))
            self.assertEqual(trial_record.read_records(Path(tmp) / "none.jsonl"), ([], 0))


if __name__ == "__main__":
    unittest.main()
