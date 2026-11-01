"""The records the shadow writes, against lane L2's record builder and report (lib/trial_record.py, lib/trial_report.py).

The shadow writes two kinds of qwen record: the builder's, from the task the worker left, and, when a spawn failed before it made a
task dir, `trial_record.no_task`. Both must carry the keys the builder gives (size, first_review_outcome, gate_runs, the transcript
and docs fields), because the report reads them by name. Fixtures: trial_shadow_case.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

sys.path.insert(0, str(ROOT / "lib"))
import trial_record  # noqa: E402
import trial_report  # noqa: E402

RUN_KEYS = {"run", "source_task", "outcome", "workdir", "normal_reviewer_model", "reviewer_mismatch"}  # what the shadow adds to a record
APPROVED = "APPROVED\nRESULT_STATUS: complete\n## Coverage\nReviewed lib/x.py in full.\n"
TRANSCRIPT = ROOT / "tests" / "fixtures" / "trial" / "claude_transcript.jsonl"


class RecordShapeTests(ShadowCase):
    def built(self):
        """A record from the real builder, over the source task of this fixture."""
        return trial_record.build(self.state / "tasks" / self.source_id, "normal", "shadow", "p-1", state_dir=self.state)

    def failed_spawn_record(self):
        rdir, info = self.prepare()
        self.run_stub(rdir, spawn_error="tp-spawn refused: nope")
        return {r["arm"]: r for r in self.records()}["qwen"]

    def settled_run(self):
        rdir, info = self.prepare()

        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.settle_worker(run)
            elif n == 2:
                for p in (run.state / "tasks").iterdir():
                    if (p / "review-of").exists():
                        self.settle(p, "waiting", APPROVED)
        self.run_stub(rdir, poll)
        return {r["arm"]: r for r in self.records()}

    def test_a_spawn_failure_record_has_every_key_the_builders_record_has(self):
        failed = self.failed_spawn_record()
        built = self.built()
        self.assertEqual(sorted(set(built) - set(failed)), [])
        self.assertEqual(sorted(set(failed) - set(built)), sorted(RUN_KEYS | {"no_task", "error"}))
        self.assertIs(failed["no_task"], True)
        self.assertEqual(failed["schema"], trial_record.SCHEMA)

    def test_a_spawn_failure_record_says_nothing_it_does_not_know(self):
        failed = self.failed_spawn_record()
        for key in ("size", "state", "exit", "first_review_verdict", "first_review_outcome", "review_rounds", "tokens", "wall_time_s", "turns",
                    "tool_errors", "brief_size_bytes", "context_files", "gate_runs", "docs_calls", "diff"):
            self.assertIsNone(failed[key], key)
        self.assertEqual((failed["transcript"]["status"], failed["rule_slips"]["complete"]), ("missing", False))
        self.assertEqual((failed["finding_categories"], failed["finding_closes_by"]), ([], []))

    def test_the_report_counts_a_spawn_failure_as_a_qwen_attempt_with_no_review(self):
        self.failed_spawn_record()
        records, bad = trial_report.load(self.repo / ".tmux-pi" / "trial" / "records.jsonl")
        summary = trial_report.summarize(records, bad)
        self.assertEqual(summary["records"], 2)
        self.assertEqual((summary["by_arm"]["qwen"]["no_review"], summary["by_arm"]["qwen"]["n"]), (1, 0))
        self.assertEqual([summary["medians"]["qwen"][k]["n"] for k in ("wall_time_s", "tokens", "review_rounds", "rule_slips", "gate_runs", "docs_calls")],
                         [0] * 6, "a failed spawn adds nothing to a median")
        self.assertIn("qwen", trial_report.render(summary))
        trial_report.report(self.repo / ".tmux-pi" / "trial" / "records.jsonl", as_json=True)

    def test_a_completed_run_writes_the_builders_record_for_both_arms_and_the_report_reads_it(self):
        by_arm = self.settled_run()
        for arm in ("qwen", "normal"):
            with self.subTest(arm=arm):
                self.assertEqual(by_arm[arm]["schema"], trial_record.SCHEMA)
                self.assertNotIn("stub", by_arm[arm])
                self.assertEqual(sorted(set(self.built()) - set(by_arm[arm])), [])
        self.assertEqual((by_arm["qwen"]["first_review_outcome"], by_arm["qwen"]["diff"]), ("approved", {"files": 2, "lines": 3}))
        records, bad = trial_report.load(self.repo / ".tmux-pi" / "trial" / "records.jsonl")
        summary = trial_report.summarize(records, bad)
        self.assertEqual((summary["records"], summary["malformed"], summary["by_arm"]["qwen"]["approved"]), (2, 0, 1))

    def test_a_normal_arm_task_with_a_claude_transcript_carries_gate_runs_the_report_uses(self):
        (self.state / "tasks" / self.source_id / "transcript-path").write_text(str(TRANSCRIPT))
        by_arm = self.settled_run()
        gates = by_arm["normal"]["gate_runs"]
        self.assertEqual(sorted(gates), ["complete", "denied", "parse_failed", "runs", "total", "unattributed", "uncertain"])
        self.assertIsNone(by_arm["qwen"]["gate_runs"], "a pi transcript is not scanned until its schema is pinned")
        records, _ = trial_report.load(self.repo / ".tmux-pi" / "trial" / "records.jsonl")
        summary = trial_report.summarize(records)
        self.assertEqual((summary["medians"]["normal"]["gate_runs"]["n"], summary["medians"]["qwen"]["gate_runs"]["n"]), (1 if gates["complete"] else 0, 0))
        self.assertEqual(summary["gate_parse_failed"]["normal"], gates["parse_failed"])

    def test_the_report_tolerates_a_record_with_no_gate_runs_a_null_kind_and_a_non_object_gate_runs(self):
        by_arm = self.settled_run()
        rows = [dict(by_arm["qwen"], gate_runs=None, pair_id="a"), dict(by_arm["qwen"], gate_runs=[], pair_id="b"),
                dict(by_arm["qwen"], gate_runs={"complete": True, "total": 2, "runs": [{"kind": None}]}, pair_id="c")]
        summary = trial_report.summarize(rows)
        self.assertEqual(summary["records"], 3)
        self.assertEqual(summary["medians"]["qwen"]["gate_runs"], {"n": 1, "median": 2})
        trial_report.render(summary)

    def test_a_record_from_the_stub_of_before_is_a_row_the_report_still_reads_as_no_review(self):
        row = {"schema": 0, "stub": True, "pair_id": "old", "arm": "qwen", "mode": "shadow", "first_review_verdict": "APPROVED", "review_rounds": 1}
        summary = trial_report.summarize([row])
        self.assertEqual((summary["records"], summary["by_arm"]["qwen"]["no_review"]), (1, 1))


class NoTaskBuilderTests(ShadowCase):
    def test_no_task_checks_its_arguments_as_build_does(self):
        for args in (("x", "shadow", "p"), ("qwen", "x", "p"), ("qwen", "shadow", " ")):
            with self.subTest(args=args):
                with self.assertRaises(trial_record.RecordError):
                    trial_record.no_task(*args)

    def test_no_task_is_json_safe_and_appendable(self):
        record = trial_record.no_task("qwen", "shadow", "p-1", task_id="t", role="coder", model="m", effort="medium", base_commit="abc1234", now="2026-10-31T10:00:00Z")
        trial_record.append(self.repo, record)
        rows, bad = trial_record.read_records(trial_record.trial_dir(self.repo) / "records.jsonl")
        self.assertEqual((rows, bad), ([record], 0))
        self.assertEqual((record["created"], record["task_id"], record["model"], record["effort"], record["base_commit"]),
                         ("2026-10-31T10:00:00Z", "t", "m", "medium", "abc1234"))
