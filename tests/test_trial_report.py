"""Qwen-trial report: rates, the n<5 noise flag, medians, the paired 2x2, empty input, and the tp-evals trial command."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import trial_record  # noqa: E402
import trial_report  # noqa: E402

TP_EVALS = ROOT / "bin" / "tp-evals"


def rec(pair, arm, outcome, role="coder", size="small", **extra):
    base = {"pair_id": pair, "arm": arm, "role": role, "size": size, "first_review_outcome": outcome,
            "first_review_verdict": {"approved": "APPROVED", "request_changes": "REQUEST_CHANGES"}.get(outcome)}
    return {**base, **extra}


class RateTests(unittest.TestCase):
    def test_rate_counts_approved_over_conclusive_and_keeps_the_rest_apart(self):
        rows = [rec("1", "qwen", "approved"), rec("2", "qwen", "request_changes"), rec("3", "qwen", "inconclusive"),
                rec("4", "qwen", None), rec("5", "qwen", "approved")]
        cell = trial_report.summarize(rows)["by_arm"]["qwen"]
        self.assertEqual((cell["n"], cell["approved"], cell["request_changes"], cell["inconclusive"], cell["no_review"], cell["rate"]),
                         (3, 2, 1, 1, 1, 0.6667))

    def test_noise_flag_is_below_five_conclusive_attempts(self):
        four = trial_report.summarize([rec(str(i), "qwen", "approved") for i in range(4)])["by_arm"]["qwen"]
        five = trial_report.summarize([rec(str(i), "qwen", "approved") for i in range(5)])["by_arm"]["qwen"]
        self.assertEqual((four["n"], four["noise"], five["n"], five["noise"]), (4, True, 5, False))

    def test_inconclusive_reviews_do_not_lift_n_out_of_noise(self):
        rows = [rec(str(i), "qwen", "approved") for i in range(4)] + [rec("x", "qwen", "inconclusive")]
        self.assertTrue(trial_report.summarize(rows)["by_arm"]["qwen"]["noise"])

    def test_an_arm_with_no_conclusive_review_has_no_rate(self):
        cell = trial_report.summarize([rec("1", "qwen", "approved")])["by_arm"]["normal"]
        self.assertEqual((cell["n"], cell["rate"]), (0, None))

    def test_cells_split_by_role_size_and_arm(self):
        rows = [rec("1", "qwen", "approved", role="coder", size="small"), rec("1", "normal", "request_changes", role="coder", size="small"),
                rec("2", "qwen", "request_changes", role="coder", size="large"), rec("3", "qwen", "approved", role="tester", size="small"),
                rec("4", "qwen", "approved", role=None, size=None)]
        cells = {(c["role"], c["size"]): c for c in trial_report.summarize(rows)["cells"]}
        self.assertEqual(set(cells), {("coder", "small"), ("coder", "large"), ("tester", "small"), ("unknown", "unknown")})
        self.assertEqual((cells[("coder", "small")]["qwen"]["approved"], cells[("coder", "small")]["normal"]["request_changes"]), (1, 1))
        self.assertEqual(cells[("coder", "large")]["normal"]["n"], 0)

    def test_records_with_an_unknown_arm_are_ignored(self):
        out = trial_report.summarize([rec("1", "gpt", "approved"), {"nope": 1}])
        self.assertEqual(out["records"], 0)


class CategoryAndMedianTests(unittest.TestCase):
    def test_top_categories_per_arm_ranked_by_count_then_name(self):
        rows = [rec("1", "qwen", "request_changes", finding_categories=["b", "a"]),
                rec("2", "qwen", "request_changes", finding_categories=["b"]),
                rec("3", "normal", "request_changes", finding_categories=["z"])]
        cats = trial_report.summarize(rows)["categories"]
        self.assertEqual(cats["qwen"], [{"family": "b", "count": 2}, {"family": "a", "count": 1}])
        self.assertEqual(cats["normal"], [{"family": "z", "count": 1}])

    def test_medians_skip_unknowns_and_report_how_many_were_known(self):
        rows = [rec("1", "qwen", "approved", wall_time_s=100, tokens={"input": 10, "output": 5}, review_rounds=1,
                    rule_slips={"complete": True, "total": 2}, gate_runs={"complete": True, "total": 3}, docs_calls={"count": 4}),
                rec("2", "qwen", "approved", wall_time_s=300, tokens={"input": 30, "output": 5, "cacheRead": 999}, review_rounds=3,
                    rule_slips={"complete": True, "total": 4}, gate_runs={"complete": True, "total": 5}, docs_calls={"count": 0}),
                rec("3", "qwen", "approved", wall_time_s=None, tokens=None, rule_slips={"complete": False, "total": 9},
                    gate_runs=None, docs_calls=None),
                rec("4", "qwen", "approved", gate_runs={"complete": False, "total": 99})]
        med = trial_report.summarize(rows)["medians"]["qwen"]
        self.assertEqual(med["wall_time_s"], {"n": 2, "median": 200})
        self.assertEqual(med["tokens"], {"n": 2, "median": 25})  # (15 + 35) / 2: cache tokens are left out
        self.assertEqual(med["review_rounds"], {"n": 2, "median": 2})
        self.assertEqual(med["rule_slips"], {"n": 2, "median": 3})  # the incomplete record is not counted
        self.assertEqual(med["gate_runs"], {"n": 2, "median": 4})  # the incomplete scan is not counted
        self.assertEqual(med["docs_calls"], {"n": 2, "median": 2})

    def test_unread_bash_calls_are_counted_per_arm_and_their_records_skip_the_gate_median(self):  # review 5: fail visible
        rows = [rec("1", "qwen", "approved", gate_runs={"complete": True, "total": 3, "parse_failed": 0}),
                rec("2", "qwen", "approved", gate_runs={"complete": True, "total": 9, "parse_failed": 2}),
                rec("3", "normal", "approved", gate_runs={"complete": True, "total": 4}),  # a record from before the key
                rec("4", "normal", "approved", gate_runs={"complete": True, "total": 6, "parse_failed": 1})]
        out = trial_report.summarize(rows)
        self.assertEqual(out["gate_parse_failed"], {"qwen": 2, "normal": 1})
        self.assertEqual(out["medians"]["qwen"]["gate_runs"], {"n": 1, "median": 3})
        self.assertEqual(out["medians"]["normal"]["gate_runs"], {"n": 1, "median": 4})
        text = trial_report.render(out)
        self.assertIn("Bash calls not read (parse_failed): qwen 2, normal 1", text)
        self.assertIn("lower bound", text)
        clean = trial_report.render(trial_report.summarize([rec("1", "qwen", "approved", gate_runs={"complete": True, "total": 1})]))
        self.assertIn("Bash calls not read (parse_failed): qwen 0, normal 0", clean)
        self.assertNotIn("lower bound", clean)
        self.assertEqual(json.loads(json.dumps(out))["gate_parse_failed"], {"qwen": 2, "normal": 1})

    def test_a_median_with_nothing_known_is_none(self):
        med = trial_report.summarize([rec("1", "qwen", "approved")])["medians"]["normal"]
        self.assertEqual(med["wall_time_s"], {"n": 0, "median": None})


class LatestRecordTests(unittest.TestCase):
    """One policy for every aggregate: the last record of a (pair_id, arm) replaces the earlier tries."""
    ROWS = [rec("a", "qwen", "request_changes", finding_categories=["old"], wall_time_s=100, review_rounds=1),
            rec("a", "qwen", "approved", finding_categories=["new"], wall_time_s=300, review_rounds=3),
            rec("a", "normal", "approved")]

    def test_rates_and_cells_use_the_last_record_so_they_agree_with_the_paired_view(self):
        out = trial_report.summarize(self.ROWS)
        self.assertEqual((out["by_arm"]["qwen"]["n"], out["by_arm"]["qwen"]["rate"]), (1, 1.0))
        self.assertEqual(out["cells"][0]["qwen"]["request_changes"], 0)
        self.assertEqual(out["paired"]["table"]["qwen_approved"]["normal_approved"], 1)

    def test_categories_and_medians_use_the_last_record(self):
        out = trial_report.summarize(self.ROWS)
        self.assertEqual(out["categories"]["qwen"], [{"family": "new", "count": 1}])
        self.assertEqual(out["medians"]["qwen"]["wall_time_s"], {"n": 1, "median": 300})
        self.assertEqual(out["medians"]["qwen"]["review_rounds"], {"n": 1, "median": 3})

    def test_the_count_says_how_many_records_were_replaced(self):
        out = trial_report.summarize(self.ROWS)
        self.assertEqual((out["records"], out["superseded"]), (2, 1))
        self.assertIn("1 superseded", trial_report.render(out))

    def test_the_same_pair_id_on_the_two_arms_is_two_attempts(self):
        self.assertEqual(trial_report.summarize([rec("a", "qwen", "approved"), rec("a", "normal", "approved")])["superseded"], 0)

    def test_a_record_without_a_pair_id_is_its_own_attempt_and_never_paired(self):
        rows = [rec(None, "qwen", "approved"), rec(None, "qwen", "request_changes")]
        out = trial_report.summarize(rows)
        self.assertEqual((out["by_arm"]["qwen"]["n"], out["superseded"], out["paired"]["pairs"]), (2, 0, 0))


class PairedTests(unittest.TestCase):
    def paired(self, rows):
        return trial_report.summarize(rows)["paired"]

    def test_two_by_two(self):
        rows = [rec("1", "qwen", "approved"), rec("1", "normal", "approved"),
                rec("2", "qwen", "approved"), rec("2", "normal", "request_changes"),
                rec("3", "qwen", "request_changes"), rec("3", "normal", "approved"),
                rec("4", "qwen", "request_changes"), rec("4", "normal", "request_changes"),
                rec("5", "qwen", "request_changes"), rec("5", "normal", "approved")]
        out = self.paired(rows)
        self.assertEqual(out["pairs"], 5)
        self.assertEqual(out["table"], {"qwen_approved": {"normal_approved": 1, "normal_request_changes": 1},
                                        "qwen_request_changes": {"normal_approved": 2, "normal_request_changes": 1}})

    def test_a_pair_needs_a_conclusive_first_review_on_both_arms(self):
        rows = [rec("1", "qwen", "approved"),                                  # no normal arm
                rec("2", "qwen", "approved"), rec("2", "normal", "inconclusive"),
                rec("3", "qwen", None), rec("3", "normal", "approved")]
        self.assertEqual(self.paired(rows)["pairs"], 0)

    def test_the_last_record_of_an_arm_replaces_an_earlier_try(self):
        rows = [rec("1", "qwen", "request_changes"), rec("1", "qwen", "approved"), rec("1", "normal", "approved")]
        out = self.paired(rows)
        self.assertEqual(out["table"]["qwen_approved"]["normal_approved"], 1)
        self.assertEqual(out["pairs"], 1)


class RenderTests(unittest.TestCase):
    def test_empty_input_gives_one_clear_line(self):
        text = trial_report.render(trial_report.summarize([]), source="/p/records.jsonl")
        self.assertEqual(text, "No trial records in /p/records.jsonl. Nothing to report yet.")

    def test_empty_input_mentions_skipped_lines(self):
        self.assertIn("2 malformed", trial_report.render(trial_report.summarize([], 2)))

    def test_table_flags_noise_and_shows_n(self):
        rows = [rec("1", "qwen", "approved"), rec("1", "normal", "request_changes")]
        text = trial_report.render(trial_report.summarize(rows))
        self.assertIn("1/1 100% noise", text)
        self.assertIn("0/1 0% noise", text)
        self.assertIn("Paired (1 pair(s)", text)

    def test_a_cell_with_no_conclusive_review_still_shows_n_and_the_noise_flag(self):
        rows = [rec("1", "qwen", "approved", role="coder", size="small")]
        text = trial_report.render(trial_report.summarize(rows))
        row = next(line for line in text.splitlines() if line.strip().startswith("coder"))
        self.assertIn("1/1 100% noise", row)
        self.assertIn("0/0 noise", row)
        by_arm = next(line for line in text.splitlines() if line.strip().startswith("normal"))
        self.assertIn("0/0 noise", by_arm)

    def test_no_pairs_prints_no_table(self):
        text = trial_report.render(trial_report.summarize([rec("1", "qwen", "approved")]))
        self.assertIn("Paired (0 pair(s)", text)
        self.assertNotIn("normal approved", text)


class CommandTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(TP_EVALS), "trial", *args], capture_output=True, text=True, timeout=60,
                              env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})

    def test_report_over_a_project_prints_the_table_and_json_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(5):
                trial_record.append(tmp, rec(str(i), "qwen", "approved" if i % 2 else "request_changes", finding_categories=["x"]))
                trial_record.append(tmp, rec(str(i), "normal", "approved"))
            text = self.run_cli("--project", tmp)
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertIn("By role and size:", text.stdout)
            self.assertIn("2/5 40%", text.stdout)
            self.assertIn("5/5 100%", text.stdout)
            data = self.run_cli("--project", tmp, "--json")
            self.assertEqual(data.returncode, 0, data.stderr)
            parsed = json.loads(data.stdout)
            self.assertEqual(parsed["records"], 10)
            self.assertEqual(parsed["paired"]["pairs"], 5)
            records, bad = trial_report.load(Path(tmp) / ".tmux-pi" / "trial" / "records.jsonl")
            self.assertEqual(parsed, trial_report.summarize(records, bad))

    def test_empty_or_missing_file_exits_zero_with_a_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = self.run_cli("--project", tmp)
            self.assertEqual((missing.returncode, missing.stdout.startswith("No trial records")), (0, True), missing.stderr)
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            again = self.run_cli("--file", str(empty))
            self.assertEqual((again.returncode, again.stdout.startswith("No trial records")), (0, True), again.stderr)
            as_json = json.loads(self.run_cli("--file", str(empty), "--json").stdout)
            self.assertEqual(as_json["records"], 0)

    def test_needs_no_tmux_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_cli("--project", tmp)  # env has no TMUX and no TP_STATE_DIR
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_a_directory_in_place_of_the_file_is_an_error_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_cli("--file", tmp)
            self.assertEqual(out.returncode, 1)
            self.assertIn("tp-evals:", out.stderr)
            self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
