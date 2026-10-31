"""Qwen-trial transcript scanners: one positive and one negative case per slip rule, gate runs, Claude and pi transcripts."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import trial_scan  # noqa: E402

# rule id -> (a command that breaks it, a command that looks close and does not)
COMMAND_CASES = {
    "heredoc": ("cat <<'EOF'\nx\nEOF", "echo $((1<<3))"),
    "inline_script": ("python3 -c 'print(1)'", "python3 -m unittest tests.test_x"),
    "command_substitution": ("echo $(date)", "echo $((1+2))"),
    "repo_root_search": ("rg -n foo lib", "git -C /w/wt grep -n foo -- lib"),
    "env_read": ("cat apps/api/.env.local", "cat apps/api/.env.example"),
    "npm_install": ("npm ci", "npm run test"),
    "mise_trust": ("mise trust", "mise ls"),
    "git_push": ("git -C /w/wt push origin HEAD", "git -C /w/wt log -1"),
    "git_amend": ("git commit --amend --no-edit", "git commit -m 'DMP-1: add x'"),
    "git_rebase": ("git rebase main", "git rev-parse main"),
    "git_reset": ("git -C /w/wt reset --hard HEAD~1", "git -C /w/wt restore x"),
    "git_cherry_pick": ("git cherry-pick abc123", "git log --oneline --grep=cherry-pick"),
}


def line(entry):
    return json.dumps(entry) + "\n"


def use(ident, name, **tool_input):
    return line({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident, "name": name, "input": tool_input}]}})


def result(ident, text="ok", error=False):
    return line({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": text,
                                                          "is_error": error}]}})


class CommandRuleTests(unittest.TestCase):
    def test_every_command_rule_has_a_positive_and_a_negative_case(self):
        for rule, _, _ in trial_scan.COMMAND_RULES:
            self.assertIn(rule, COMMAND_CASES, rule)

    def test_positive_and_negative_per_rule(self):
        for rule, (bad, fine) in COMMAND_CASES.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, trial_scan.command_slips(bad))
                self.assertNotIn(rule, trial_scan.command_slips(fine))

    def test_a_non_string_command_has_no_slips(self):
        self.assertEqual(trial_scan.command_slips(None), [])
        self.assertEqual(trial_scan.gate_kinds(None), [])

    def test_a_commit_message_heredoc_counts_both_the_heredoc_and_the_substitution(self):
        found = trial_scan.command_slips("git commit -m \"$(cat <<'EOF'\nDMP-1: x\nEOF\n)\"")
        self.assertEqual(sorted(found), ["command_substitution", "heredoc"])


class WriteRuleTests(unittest.TestCase):
    def test_fixed_wait_in_a_test_file_is_a_slip(self):
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "import time\ntime.sleep(2)\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "await new Promise(r => setTimeout(r, 50))\n"), ["fixed_wait"])

    def test_fixed_wait_with_a_timer_ok_mark_or_outside_a_test_file_is_not(self):
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "setTimeout(done, 5) // timer-ok: fake clock\n"), [])
        self.assertEqual(trial_scan.write_slips("lib/retry.py", "time.sleep(2)\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "await event.wait()\n"), [])


    def test_a_timer_inside_a_quoted_literal_is_not_a_wait(self):
        quoted = 'self.assertEqual(write_slips("tests/test_a.py", "time.sleep(2)"), ["fixed_wait"])\n'
        self.assertEqual(trial_scan.write_slips("tests/test_trial_scan.py", quoted), [])
        self.assertEqual(trial_scan.write_slips("a.test.ts", "expect(scan(`setTimeout(r, 5)`)).toBe(1)\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "x = 'it\\'s time.sleep(1)'\n"), [])

    def test_a_timer_inside_a_multi_line_string_is_not_a_wait_but_code_after_it_is(self):
        text = 'FIXTURE = """\nimport time\ntime.sleep(2)\n"""\n'
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", text), [])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", text + "time.sleep(1)\n"), ["fixed_wait"])

    def test_a_real_wait_beside_a_string_is_still_a_wait(self):
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", 'print("start"); time.sleep(2)\n'), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "# don't wait\ntime.sleep(2)\n"), ["fixed_wait"])


class MessageRuleTests(unittest.TestCase):
    GOOD = "DMP-12: add the thing\n\nCo-Authored-By: Qwen <noreply@anthropic.com>\n"

    def test_a_good_message_has_no_slip(self):
        self.assertEqual(trial_scan.commit_message_slips([self.GOOD]), [])

    def test_subject_and_trailer_are_separate_rules(self):
        self.assertEqual(trial_scan.commit_message_slips(["add the thing\n\nCo-Authored-By: Q <q@example.com>\n"]), ["commit_subject"])
        self.assertEqual(trial_scan.commit_message_slips(["DMP-12: add the thing\n"]), ["commit_trailer"])

    def test_two_bad_commits_count_twice(self):
        self.assertEqual(trial_scan.commit_message_slips(["x", "y"]), ["commit_subject", "commit_trailer"] * 2)

    def test_think_leak_in_a_message(self):
        self.assertIn("think_leak", trial_scan.commit_message_slips([self.GOOD + "<think>hm</think>\n"]))
        self.assertNotIn("think_leak", trial_scan.commit_message_slips([self.GOOD]))

    def test_think_leak_helper(self):
        self.assertTrue(trial_scan.think_leak("a </think> b"))
        self.assertFalse(trial_scan.think_leak("we think so"))
        self.assertFalse(trial_scan.think_leak(None))

    def test_every_rule_id_is_listed(self):
        for rule in ("fixed_wait", "commit_subject", "commit_trailer", "guard_denial", "think_leak"):
            self.assertIn(rule, trial_scan.SLIP_RULES)


class GateKindTests(unittest.TestCase):
    def test_each_gate_kind_is_found(self):
        cases = {
            "test": ["python3 -m unittest tests.test_a -v", "python3 tests/run_parallel.py", "npm run test", "deno test -A", "npx vitest run"],
            "lint": ["npx eslint .", "deno lint", "npm run lint"],
            "tsc": ["npx tsc --noEmit", "npm run typecheck", "deno check main.ts"],
            "prettier": ["npx prettier --check .", "npm run format"],
            "gitleaks": ["gitleaks detect --no-git"],
        }
        for kind, commands in cases.items():
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(trial_scan.gate_kinds(command), [kind])

    def test_only_the_head_of_a_segment_counts(self):
        self.assertEqual(trial_scan.gate_kinds("git commit -m 'run pytest and eslint'"), [])
        self.assertEqual(trial_scan.gate_kinds("echo npm test"), [])

    def test_a_gate_inside_quotes_is_not_a_segment(self):
        self.assertEqual(trial_scan.gate_kinds('git commit -m "ok && npm test"'), [])
        self.assertEqual(trial_scan.gate_kinds("echo 'a; npm test' && npm test"), ["test"])

    def test_a_redirect_ampersand_does_not_split(self):
        self.assertEqual(trial_scan.gate_kinds("npm test 2>&1 | tail -3"), ["test"])

    def test_repeated_gates_are_kept(self):
        self.assertEqual(trial_scan.gate_kinds("npm test; npm test"), ["test", "test"])
        self.assertEqual(trial_scan.gate_kinds("npm test && npm run lint && npm test"), ["test", "lint", "test"])

    def test_chained_gates_come_in_order_and_cd_does_not_hide_them(self):
        self.assertEqual(trial_scan.gate_kinds("cd /w && npx tsc --noEmit && FOO=1 python3 -m unittest x | tail -5"), ["tsc", "test"])


class ClaudeScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "t.jsonl"

    def scan(self, *lines):
        self.path.write_text("".join(lines), encoding="utf-8")
        return trial_scan.scan_claude(self.path)

    def test_slips_are_counted_by_rule_and_no_command_text_is_kept(self):
        out = self.scan(use("a", "Bash", command="python3 -c 'x'"), use("b", "Bash", command="python3 -c 'y'"),
                        use("c", "Bash", command="git push"), use("d", "Bash", command="git status"))
        self.assertEqual(out["rule_slips"], {"git_push": 1, "inline_script": 2})
        self.assertEqual(out["commands"], 4)
        self.assertNotIn("python3 -c", json.dumps(out))

    def test_gate_exit_status_comes_from_the_matching_result(self):
        out = self.scan(use("a", "Bash", command="python3 -m unittest x"), result("a", "Exit code 1\nFAILED", error=True),
                        use("b", "Bash", command="npx tsc --noEmit"), result("b", "done"),
                        use("c", "Bash", command="npx eslint ."))
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": 1}, {"kind": "tsc", "exit": 0}, {"kind": "lint", "exit": None}])

    def gates(self, command, text=None, error=False):
        lines = [use("a", "Bash", command=command)] + ([result("a", text, error)] if text is not None else [])
        return self.scan(*lines)["gate_runs"]

    def test_and_chain_that_failed_does_not_say_the_second_gate_ran(self):
        self.assertEqual(self.gates("npm test && npm run lint", "Exit code 1\nFAIL", True),
                         [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}])

    def test_and_chain_that_passed_ran_every_gate(self):
        self.assertEqual(self.gates("npm test && npm run lint", "ok"), [{"kind": "test", "exit": 0}, {"kind": "lint", "exit": 0}])

    def test_the_gate_that_ended_the_chain_gets_the_exit_code(self):
        self.assertEqual(self.gates("cd /w && npm test", "Exit code 2\nFAIL", True), [{"kind": "test", "exit": 2}])

    def test_an_or_branch_may_or_may_not_have_run_when_the_exit_was_zero(self):
        self.assertEqual(self.gates("npm test || npm run lint", "ok"),
                         [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}])

    def test_an_or_branch_never_runs_after_a_pass_that_is_known(self):
        self.assertEqual(self.gates("true || npm test", "ok"), [])

    def test_an_or_branch_after_a_failed_gate_is_known_to_have_run_when_the_exit_was_nonzero(self):
        self.assertEqual(self.gates("npm test || npm run lint", "Exit code 1\nx", True), [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 1}])

    def test_semicolon_runs_both_and_keeps_repeats(self):
        self.assertEqual(self.gates("npm test; npm test", "Exit code 1\nx", True), [{"kind": "test", "exit": None}, {"kind": "test", "exit": 1}])
        self.assertEqual(self.gates("npm test; npm test", "ok"), [{"kind": "test", "exit": None}, {"kind": "test", "exit": 0}])  # `;` shows only the last status

    def test_no_result_leaves_later_branches_uncertain_and_exits_unknown(self):
        self.assertEqual(self.gates("npm test && npm run lint"), [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}])

    def test_denied_chain_counts_no_run(self):
        out = self.scan(use("a", "Bash", command="npm test && npm run lint"),
                        result("a", "PreToolUse:Bash hook error: guard denied this tool call: x", error=True))
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": None, "denied": True}, {"kind": "lint", "exit": None, "denied": True}])

    def test_total_counts_only_gates_known_to_have_run(self):
        out = self.scan(use("a", "Bash", command="npm test && npm run lint"), result("a", "Exit code 1\nx", True),
                        use("b", "Bash", command="npm test"),
                        result("b", "PreToolUse:Bash hook error: guard denied this tool call: x", error=True))
        self.assertEqual(trial_scan.gate_totals(out["gate_runs"]), {"total": 1, "uncertain": 1, "denied": 1})

    def test_a_malformed_line_makes_the_scan_incomplete_not_ok(self):
        out = self.scan("not json\n", use("a", "Bash", command="git push"))
        self.assertEqual((out["status"], out["bad_lines"], out["complete"]), ("incomplete", 1, False))
        self.assertEqual(out["rule_slips"], {"git_push": 1})  # what was seen is kept as a lower bound

    def test_an_oversized_line_makes_the_scan_incomplete(self):
        with mock.patch.object(trial_scan, "MAX_LINE_BYTES", 10):
            out = self.scan(use("a", "Bash", command="git status"))
        self.assertEqual((out["status"], out["complete"]), ("incomplete", False))

    def test_a_clean_scan_is_complete(self):
        out = self.scan(use("a", "Bash", command="git status"))
        self.assertEqual((out["status"], out["complete"]), ("ok", True))

    def test_a_refused_call_is_a_guard_denial_and_has_no_exit(self):
        out = self.scan(use("a", "Bash", command="npm test"),
                        result("a", "PreToolUse:Bash hook error: tmux-pi trust store guard denied this tool call: x", error=True))
        self.assertEqual(out["rule_slips"], {"guard_denial": 1})
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": None, "denied": True}])

    def test_an_ordinary_failure_with_permission_denied_text_is_not_a_guard_denial(self):
        out = self.scan(use("a", "Bash", command="ls /root"), result("a", "Exit code 1\nls: /root: Permission denied", error=True))
        self.assertEqual(out["rule_slips"], {})

    def test_assistant_think_tags_are_a_leak_but_user_text_is_not(self):
        leak = line({"type": "assistant", "message": {"content": [{"type": "text", "text": "<think>x</think> done"}]}})
        quoted = line({"type": "user", "message": {"content": [{"type": "text", "text": "<think>"}]}})
        self.assertEqual(self.scan(leak)["rule_slips"], {"think_leak": 1})
        self.assertEqual(self.scan(quoted)["rule_slips"], {})

    def test_write_and_read_tools_are_scanned(self):
        out = self.scan(use("a", "Write", file_path="tests/test_a.py", content="time.sleep(1)\n"),
                        use("b", "Edit", file_path="tests/test_a.py", old_string="x", new_string="y\n"),
                        use("c", "Read", file_path="/w/apps/api/.env"),
                        use("d", "Read", file_path="/w/apps/api/.env.example"))
        self.assertEqual(out["rule_slips"], {"env_read": 1, "fixed_wait": 1})

    def test_malformed_and_blank_lines_are_counted_not_fatal(self):
        out = self.scan("not json\n", "\n", use("a", "Bash", command="git push"))
        self.assertEqual(out["bad_lines"], 1)
        self.assertEqual(out["rule_slips"], {"git_push": 1})

    def test_a_missing_file_and_a_symlink_are_unknown_not_zero(self):
        out = trial_scan.scan_claude(Path(self.tmp.name) / "nope.jsonl")
        self.assertEqual((out["status"], out["rule_slips"], out["gate_runs"]), ("missing", None, None))
        self.path.write_text("", encoding="utf-8")
        link = Path(self.tmp.name) / "link.jsonl"
        link.symlink_to(self.path)
        self.assertEqual(trial_scan.scan_claude(link)["status"], "missing")

    def test_an_empty_transcript_is_ok_with_nothing_found(self):
        out = self.scan()
        self.assertEqual((out["status"], out["rule_slips"], out["gate_runs"]), ("ok", {}, []))


class FixtureTranscriptTests(unittest.TestCase):
    def test_recorded_claude_transcript(self):
        out = trial_scan.scan_claude(ROOT / "tests" / "fixtures" / "trial" / "claude_transcript.jsonl")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["commands"], 5)
        self.assertEqual(out["rule_slips"], {"git_push": 1, "guard_denial": 1, "inline_script": 1})
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": 1}, {"kind": "tsc", "exit": 0}])


class TaskScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_transcript_path_names_a_claude_transcript(self):
        transcript = self.dir / "elsewhere.jsonl"
        transcript.write_text(use("a", "Bash", command="git push"), encoding="utf-8")
        (self.dir / "transcript-path").write_text(str(transcript) + "\n", encoding="utf-8")
        self.assertEqual(trial_scan.scan_task(self.dir)["rule_slips"], {"git_push": 1})

    def test_a_pi_transcript_is_unknown_until_its_schema_is_pinned(self):
        (self.dir / "transcript.jsonl").write_text('{"anything": 1}\n', encoding="utf-8")
        out = trial_scan.scan_task(self.dir)
        self.assertEqual((out["harness"], out["status"], out["rule_slips"], out["gate_runs"]), ("pi", "unknown", None, None))

    def test_no_transcript_is_missing(self):
        out = trial_scan.scan_task(self.dir)
        self.assertEqual((out["harness"], out["status"], out["rule_slips"]), (None, "missing", None))


if __name__ == "__main__":
    unittest.main()
