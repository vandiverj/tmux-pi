"""Qwen-trial transcript scanners: one positive and one negative case per slip rule, gate runs, Claude and pi transcripts."""

import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
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

    def test_a_shell_comment_breaks_no_rule(self):
        self.assertEqual(trial_scan.command_slips("git status  # never git push here"), [])
        self.assertEqual(trial_scan.command_slips("# python3 -c 'x'\ngit status"), [])

    def test_a_hash_that_is_not_a_comment_keeps_the_rest_of_the_command(self):
        self.assertEqual(trial_scan.command_slips("echo a#b; git push"), ["git_push"])
        self.assertEqual(trial_scan.command_slips("echo '# x'; git push"), ["git_push"])
        self.assertEqual(trial_scan.command_slips('echo "$#" ${#a[@]}; git push'), ["git_push"])
        self.assertEqual(trial_scan.command_slips("git status # x\ngit push"), ["git_push"])

    def test_a_commit_message_heredoc_counts_both_the_heredoc_and_the_substitution(self):
        found = trial_scan.command_slips("git commit -m \"$(cat <<'EOF'\nDMP-1: x\nEOF\n)\"")
        self.assertEqual(sorted(found), ["command_substitution", "heredoc"])


class WriteRuleTests(unittest.TestCase):
    def test_fixed_wait_in_a_test_file_is_a_slip(self):
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "import time\ntime.sleep(2)\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "setTimeout(r, 50)\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "await sleep(50)\n"), ["fixed_wait"])

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

    def test_a_shell_wait_inside_an_interpolation_runs_and_is_a_wait(self):
        self.assertEqual(trial_scan.write_slips("tests/run.sh", 'echo "$(sleep 2)"\n'), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "x=`sleep 2`\n"), ["fixed_wait"])

    def test_a_py_or_js_call_inside_an_interpolation_does_not_start_its_statement(self):
        for path, text in (("tests/test_wait.py", 'f"{time.sleep(2)}"\n'), ("tests/test_wait.py", "x = rf'a {time.sleep(2)!r} b'\n"),
                           ("tests/test_wait.py", "x = f\"{f'{time.sleep(1)}'}\"\n"),
                           ("src/a.test.ts", "const s = `x ${setTimeout(done, 50)} y`\n")):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), [])

    def test_literal_text_around_an_interpolation_is_still_not_a_wait(self):
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", 'f"time.sleep(2) {x}"\n'), [])
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", 'f"{{time.sleep(2)}}"\n'), [])
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", 'f"{x!r:>{width}} time.sleep(2)"\n'), [])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "const s = `setTimeout(r, 5) ${x}`\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "echo 'sleep 2' \"sleep 3\"\n"), [])

    def test_a_wait_in_a_comment_does_not_run(self):
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", "# time.sleep(2)\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", "x = 1  # then time.sleep(2)\n"), [])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "// setTimeout(done, 5)\n"), [])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "/* old:\n setTimeout(done, 5)\n*/ ok()\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "# sleep 2\n"), [])

    def test_a_comment_marker_inside_a_string_does_not_hide_the_wait_after_it(self):
        self.assertEqual(trial_scan.write_slips("tests/test_wait.py", 'x = "#"; time.sleep(1)\n'), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", 'const u = "http://x"; setTimeout(r, 5)\n'), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "this.#t; setTimeout(r, 5)\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "echo a#b; sleep 2\n"), ["fixed_wait"])

    def test_deeply_nested_interpolations_do_not_crash(self):
        for path, unit, wait, flagged in (("tests/test_a.py", 'f"{', "time.sleep(1)", []), ("src/a.test.ts", "`${", "time.sleep(1)", []),
                                          ("tests/run.sh", '"$(', "sleep 1", ["fixed_wait"])):
            with self.subTest(path=path):  # past MAX_NESTING the text is read as written: a py/js call there starts no statement
                self.assertEqual(trial_scan.write_slips(path, unit * 5000 + wait + "\n"), flagged)
                self.assertEqual(trial_scan.write_slips(path, unit * 5000 + "\n"), [])

    def test_code_keeps_its_lines(self):
        text = 'a = """x\ny"""\n/* p\nq */ b = f"{\nc}"  # z\n`m\n${n\n}`\n'
        for path in ("tests/test_a.py", "src/a.test.ts", "tests/run.sh"):
            with self.subTest(path=path):
                self.assertEqual(trial_scan.executable_code(path, text).count("\n"), text.count("\n"))

    def test_the_code_after_a_block_comment_is_scanned(self):
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "/* x */ setTimeout(r, 5)\n"), ["fixed_wait"])

    # Rule: a wait is flagged only when it is positively a call token in code that runs.
    def test_a_regex_literal_is_not_code_that_waits(self):
        for text in ("const matcher = /setTimeout(done, 50)/;\n", "if (/sleep\\(/.test(src)) run()\n",
                     "const r = /[/]setTimeout(/g;\n", "return /setTimeout(/.test(s)\n", "x = s.split(/a\\/setTimeout(/)\n",
                     "const m = cond ? /setTimeout(/ : null\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("src/a.test.ts", text), [])

    def test_a_wait_beside_a_regex_or_a_division_is_still_a_wait(self):
        for text in ("const m = /x/g; setTimeout(r, 5)\n", "const u = /https?:\\/\\//; setTimeout(r, 5)\n",
                     "const q = a / b; setTimeout(r, 5)\n", "x = total / 2 / 3; setTimeout(r, 5)\n",
                     "const q = arr[0] / 2; setTimeout(r, 5)\n", "const c = /[//]/; setTimeout(r, 5)\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("src/a.test.ts", text), ["fixed_wait"])

    def test_a_slash_that_may_be_a_division_or_a_regex_flags_nothing_on_its_line_only(self):
        for text in ("f(a) / 2; setTimeout(r, 5)\n", "x = /abc setTimeout(r, 5)\n", "const q = (a) /b/ 2; setTimeout(r, 5)\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("src/a.test.ts", text), [])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "f(a) / 2\nsetTimeout(r, 5)\n"), ["fixed_wait"])

    def test_timer_ok_counts_only_in_a_comment(self):
        for path, text in (("tests/test_a.py", 'marker = "timer-ok"; time.sleep(2)\n'),
                           ("src/a.test.ts", "const m = 'timer-ok'; setTimeout(r, 5)\n"),
                           ("src/a.test.ts", "const m = `timer-ok`; setTimeout(r, 5)\n"),
                           ("src/a.test.ts", "const m = /timer-ok/; setTimeout(r, 5)\n"),
                           ("tests/run.sh", "echo 'timer-ok'; sleep 2\n"),
                           ("tests/test_a.py", "# timer-ok\ntime.sleep(2)\n"),
                           ("src/a.test.ts", "/* timer-ok */\nsetTimeout(r, 5)\n")):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), ["fixed_wait"])

    def test_a_timer_ok_comment_on_the_line_of_the_wait_clears_it(self):
        for path, text in (("tests/test_a.py", "time.sleep(2)  # timer-ok: fake clock\n"),
                           ("src/a.test.ts", "setTimeout(r, 5) // timer-ok\n"),
                           ("src/a.test.ts", "setTimeout(r, 5) /* timer-ok */\n"),
                           ("src/a.test.ts", "/* why:\n timer-ok */ setTimeout(r, 5)\n"),
                           ("tests/run.sh", "sleep 2  # timer-ok\n"),
                           ("tests/test_a.py", 'x = "a"; time.sleep(2)  # timer-ok\n')):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), [])

    def test_a_definition_or_a_file_of_unknown_kind_is_not_a_wait_call(self):
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "async def sleep(n):\n    pass\n"), [])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "function sleep(ms) { return ms }\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "await sleep(1)\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "await sleep(100)\n"), ["fixed_wait"])
        for path in ("tests/notes.md", "tests/fixtures/a.json", "tests/page.html", "tests/a.rb"):
            self.assertEqual(trial_scan.write_slips(path, "setTimeout(done, 5)\ntime.sleep(2)\nsleep 2\n"), [], path)
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "sleep 2\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "sleep 2\n"), [])

    def test_a_declaration_is_never_a_wait_whatever_its_whitespace(self):
        for path, text in (("tests/test_a.py", "def   sleep(n):\n    pass\n"), ("tests/test_a.py", "def\tsleep(n):\n    pass\n"),
                           ("tests/test_a.py", "async  def  sleep(n):\n    pass\n"),
                           ("src/a.test.ts", "function  sleep(ms) { return ms }\n"), ("src/a.test.ts", "function\tsleep(ms) { return ms }\n"),
                           ("src/a.test.ts", "async function   sleep(ms) {\n  return ms\n}\n"),
                           ("src/a.test.ts", "export const sleep = (ms) => ms\n"),
                           ("src/a.test.ts", "class T {\n  sleep(ms) {\n    return ms\n  }\n}\n")):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), [])

    def test_only_a_call_statement_starting_with_a_wait_token_is_flagged(self):
        flagged = (("tests/test_a.py", "sleep(1)\n"), ("tests/test_a.py", "    time.sleep(0.5)\n"), ("tests/test_a.py", "await asyncio.sleep(1)\n"),
                   ("tests/test_a.py", "x = 1; asyncio.sleep(1)\n"), ("src/a.test.ts", "  setTimeout(() => done(), 5);\n"),
                   ("src/a.test.ts", "await sleep(5)\n"), ("src/a.test.ts", "a(); setTimeout(\n  r,\n  5)\n"),
                   ("tests/test_a.py", "time.sleep(1).x\n"))
        for path, text in flagged:
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), ["fixed_wait"])
        not_flagged = (("src/a.test.ts", "await new Promise(r => setTimeout(r, 50))\n"), ("tests/test_a.py", "x = time.sleep(1)\n"),
                       ("tests/test_a.py", "if ready: time.sleep(1)\n"), ("tests/test_a.py", "run(sleep(1), 2)\n"),
                       ("src/a.test.ts", "const w = () => sleep(1)\n"), ("src/a.test.ts", "page.waitForTimeout(500)\n"),
                       ("tests/test_a.py", "page.wait_for_timeout(500)\n"), ("src/a.test.ts", "window.setTimeout(r, 5)\n"),
                       ("tests/test_a.py", "helper.sleep(1)\n"), ("tests/test_a.py", "setTimeout(r, 5)\n"),
                       ("src/a.test.ts", "time.sleep(5)\n"), ("src/a.test.ts", "class T {\n  sleep(ms) { return ms }\n}\n"),
                       ("tests/test_a.py", "x = \\\n    time.sleep(1)\n"), ("tests/test_a.py", "time.sleep(\n"))
        for path, text in not_flagged:
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips(path, text), [])

    def test_a_declaration_with_the_brace_on_the_next_line_or_in_an_interface_is_not_a_wait(self):  # review 5 N2
        for text in ("class T {\n  sleep(ms)\n  {\n    return ms\n  }\n}\n", "const o = {\n  sleep(ms)\n  {\n    return ms\n  }\n}\n",
                     "interface Clock {\n  sleep(ms: number);\n}\n", "interface Clock {\n  sleep(ms: number): Promise<void>;\n}\n",
                     "interface Clock {\n  sleep(ms: number)\n}\n", "export interface Clock extends Base {\n  sleep(ms: number);\n  now();\n}\n",
                     "abstract class C {\n  sleep(ms: number);\n  sleep(ms: any) { return ms }\n}\n",
                     "declare class C {\n  sleep(ms: number);\n}\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("src/a.test.ts", text), [])

    def test_a_call_after_or_beside_an_interface_or_class_is_still_a_wait(self):
        for text in ("interface A {\n  sleep(ms: number);\n}\nsleep(5);\n", "class T {\n  run() {\n    sleep(5);\n  }\n}\n",
                     "class T {\n  static {\n    sleep(5);\n  }\n}\n", "interface A { b: { c: 1 } }\nsleep(1)\n",
                     "describe('x', () => {\n  sleep(5);\n})\n", "class T {\n  sleep(ms) { return ms }\n  run() {\n    sleep(5);\n  }\n}\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("src/a.test.ts", text), ["fixed_wait"])

    def test_declaration_limits_a_call_followed_by_a_block_on_the_next_line_is_not_a_wait(self):
        self.assertEqual(trial_scan.write_slips("src/a.test.ts", "sleep(5)\n{\n  x()\n}\n"), [])  # the other direction
        self.assertEqual(trial_scan.write_slips("tests/test_a.py", "sleep(5)\n{1: 2}\n"), ["fixed_wait"])  # py is not read this way

    def test_a_shell_wait_after_a_quoted_heredoc_marker_is_still_a_wait(self):
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "echo '<<EOF'\nsleep 2\nEOF\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", 'echo "<<EOF" # x\nsleep 2\n'), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<EOF\nsleep 2\nEOF\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<EOF\nsleep 2\n"), [])  # no end line: the body runs to the end
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "x=$(cat <<EOF\nsleep 2\nEOF\n)\nsleep 3\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "git commit -m \"$(cat <<'EOF'\nit's\nsleep 2\nEOF\n)\"\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "git commit -m \"$(cat <<'EOF'\nit's\nEOF\n)\"\nsleep 3\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<-EOF\n\tsleep 2\n\tEOF\nsleep 3\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<A <<B\nsleep 1\nA\nsleep 2\nB\n"), [])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<<sleep\nsleep 2\n"), ["fixed_wait"])
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "echo $((1<<3))\nsleep 2\n"), ["fixed_wait"])

    def test_a_shell_wait_is_only_a_command(self):
        for text in ("echo sleep 5\n", "cat <<EOF\nsleep 2\nEOF\n", "cat <<-'EOF' > t.sh\n\tsleep 2\n\tEOF\n",
                     "rsync a b \\\n  sleep 2\n", "grep sleep 2 file\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("tests/run.sh", text), [])
        for text in ("if x; then sleep 2; fi\n", "while true; do\n  sleep 1\ndone\n", "make && sleep 2\n", "sleep 2 &\n",
                     "cat <<EOF >x\nbody\nEOF\nsleep 2\n", "echo $((1<<3)); sleep 2\n"):
            with self.subTest(text=text):
                self.assertEqual(trial_scan.write_slips("tests/run.sh", text), ["fixed_wait"])


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

    def test_a_gate_in_a_shell_comment_did_not_run(self):
        self.assertEqual(trial_scan.gate_kinds("npm run build # then ; npm test"), [])
        self.assertEqual(trial_scan.gate_runs("npm run build # then && npm test", 0), [])
        self.assertEqual(trial_scan.gate_kinds("echo a#b && npm test"), ["test"])
        self.assertEqual(trial_scan.gate_kinds("git status # x\nnpm test"), ["test"])

    def test_a_redirect_ampersand_does_not_split(self):
        self.assertEqual(trial_scan.gate_kinds("npm test 2>&1 | tail -3"), ["test"])

    def test_a_gate_only_inside_a_substitution_a_group_or_a_string_is_not_found(self):
        for command in ('echo "$(npm test)"', "echo `npm test`", "x=$(npm test)", "diff <(npm test) b", 'echo "$(echo "$(npm test)")"',
                        "echo $(npm test | tail -3)", "x=$(cd a && npm test)", "echo '$(npm test)'", "echo \\$(npm test)",
                        "echo $((1+2))", "echo '`npm test`'", 'bash -c "npm test"', "sh -c 'npm run lint'", "echo \"x\"; echo 'npm test'"):
            with self.subTest(command=command):
                self.assertEqual(trial_scan.gate_kinds(command), [])
                for code in (0, 1, None):
                    self.assertEqual(trial_scan.gate_runs(command, code), [])

    def test_a_gate_beside_a_substitution_is_found_and_the_substitution_is_not(self):
        self.assertEqual(trial_scan.gate_runs("FOO=$(cat a) npm test", 1), [{"kind": "test", "exit": 1}])
        self.assertEqual(trial_scan.gate_runs("npm run lint && echo $(npm test) && npx tsc --noEmit", 0),
                         [{"kind": "lint", "exit": None}, {"kind": "tsc", "exit": 0}])
        self.assertEqual(trial_scan.gate_kinds("x=$(cd a && npm test) && npx tsc"), ["tsc"])

    def test_an_outer_redirection_failure_is_not_given_to_an_unrun_gate(self):  # review F1
        self.assertEqual(trial_scan.gate_runs("echo $(npm test) > /no/dir/f && npm run lint", 1),
                         [{"kind": "lint", "exit": None, "uncertain": True}])
        self.assertEqual(trial_scan.gate_runs("echo $(npm test) > /no/dir/f && npm run lint", 0),
                         [{"kind": "lint", "exit": 0}])

    def test_a_heredoc_body_is_data_not_commands(self):
        message = 'git commit -m "$(cat <<\'EOF\'\nnpm test\nEOF\n)"'
        self.assertEqual(trial_scan.gate_kinds(message), [])
        self.assertEqual(trial_scan.gate_runs("cat <<EOF\nnpm test\nEOF\nnpm run lint", 0), [{"kind": "lint", "exit": 0}])
        self.assertEqual(trial_scan.gate_runs("cat <<-EOF\n\tnpm test\n\tEOF\nnpm run lint", 0), [{"kind": "lint", "exit": 0}])
        self.assertEqual(trial_scan.gate_runs("cat <<A <<'B'\nnpm test\nA\nnpm run lint\nB\nnpx tsc", 0), [{"kind": "tsc", "exit": 0}])
        self.assertEqual(trial_scan.gate_kinds("echo $((1<<3)) && npm test"), ["test"])
        self.assertEqual(trial_scan.gate_kinds("cat <<<npm\nnpm test"), ["test"])  # a here-string has no body
        self.assertEqual(trial_scan.gate_kinds("cat <<EOF\nnpm test\n"), [])  # no end line: the body runs to the end

    def test_a_heredoc_marker_inside_quotes_is_not_a_heredoc(self):  # review F2
        for command in ("echo '<<EOF'\nnpm test\nEOF\ntrue", 'echo "<<EOF"\nnpm test\nEOF\ntrue', "echo \\<<EOF\nnpm test\nEOF\ntrue",
                        "echo '<<EOF' # x\nnpm test\nEOF\ntrue"):
            with self.subTest(command=command):
                self.assertEqual(trial_scan.gate_kinds(command), ["test"])
                self.assertEqual(trial_scan.gate_runs(command, 0), [{"kind": "test", "exit": None}])

    def test_a_heredoc_body_with_a_quote_or_hash_does_not_hide_what_follows(self):
        self.assertEqual(trial_scan.gate_runs("cat <<EOF\nit's # no\nEOF\nnpm test # then ; npm run lint", 1), [{"kind": "test", "exit": 1}])

    def test_deeply_nested_substitutions_do_not_crash(self):
        self.assertEqual(trial_scan.gate_runs("echo " + "$(" * 5000 + "npm test", 0), PARSE_FAILED)  # too deep to read: visible
        self.assertEqual(trial_scan.gate_runs("echo " + '"$(' * 5000 + "npm test", 0), PARSE_FAILED)
        self.assertEqual(trial_scan.gate_runs("echo " + "$(" * 5000 + "npm test" + ")" * 5000, 0), PARSE_FAILED)

    def test_repeated_gates_are_kept(self):
        self.assertEqual(trial_scan.gate_kinds("npm test; npm test"), ["test", "test"])
        self.assertEqual(trial_scan.gate_kinds("npm test && npm run lint && npm test"), ["test", "lint", "test"])

    def test_chained_gates_come_in_order_and_cd_does_not_hide_them(self):
        self.assertEqual(trial_scan.gate_kinds("cd /w && npx tsc --noEmit && FOO=1 python3 -m unittest x | tail -5"), ["tsc", "test"])


class GateRunsTests(unittest.TestCase):
    """The exit is the call's status for the last command run only; every other gate has none, and nothing is inferred."""

    def check(self, table):
        for command, code, expected in table:
            with self.subTest(command=command, code=code):
                self.assertEqual(trial_scan.gate_runs(command, code), expected)

    def test_a_simple_command_owns_the_exit(self):
        self.check([("npm test", 0, [{"kind": "test", "exit": 0}]), ("npm test", 1, [{"kind": "test", "exit": 1}]),
                    ("npm test", 2, [{"kind": "test", "exit": 2}]), ("npm test", None, [{"kind": "test", "exit": None}]),
                    ("FOO=1 npx tsc --noEmit", 0, [{"kind": "tsc", "exit": 0}])])

    def test_only_the_last_command_run_gets_the_exit(self):
        self.check([
            ("npm test && npm run lint", 0, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 0}]),
            ("npm test; npm run lint", 1, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 1}]),
            ("npm test\nnpm run lint", 0, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 0}]),
            ("npm test; npm test", 1, [{"kind": "test", "exit": None}, {"kind": "test", "exit": 1}]),
            ("npm test; echo done", 0, [{"kind": "test", "exit": None}]),
            ("npm test || true", 0, [{"kind": "test", "exit": None}]),
        ])

    def test_a_failed_chain_does_not_say_which_gate_failed_or_that_the_second_ran(self):
        failed = [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}]
        self.check([("npm test && npm run lint", 1, failed), ("npm test && npm run lint", None, failed),
                    ("echo start && npm test", 2, [{"kind": "test", "exit": None, "uncertain": True}]),
                    ("cd /does-not-exist && npm test", 1, [{"kind": "test", "exit": None, "uncertain": True}]),
                    ("cd /w && npm test", 0, [{"kind": "test", "exit": 0}])])

    def test_an_or_branch_is_never_known_to_have_run(self):
        both = [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}]
        self.check([("npm test || npm run lint", 0, both), ("npm test || npm run lint", 1, both), ("true || npm test", 0, [{"kind": "test", "exit": None, "uncertain": True}])])

    def test_a_semicolon_list_after_a_chain_does_not_prove_the_chain_ran(self):
        self.check([("git status && npm test; npm run lint", 0, [{"kind": "test", "exit": None, "uncertain": True}, {"kind": "lint", "exit": 0}]),
                    ("npm test; git status && npm run lint", 0, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 0}]),
                    ("npm test; git status && npm run lint", 1, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}]),
                    ("git status && npm test || npm run lint", 0, [{"kind": "test", "exit": None, "uncertain": True}, {"kind": "lint", "exit": None, "uncertain": True}])])

    def test_pipes(self):
        self.check([("npm test | tail -5", 1, [{"kind": "test", "exit": None}]), ("npm test 2>&1 | tail -5", 0, [{"kind": "test", "exit": None}]),
                    ("cat x | npm test", 3, [{"kind": "test", "exit": 3}]),
                    ("set -o pipefail; cat x | npm test", 1, [{"kind": "test", "exit": None}]),
                    ("set -o pipefail; npm test | tail -5", 0, [{"kind": "test", "exit": None}])])

    def test_a_background_job_has_no_exit(self):
        self.check([("npm test &", 0, [{"kind": "test", "exit": None}]),
                    ("npm test & npm run lint", 1, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 1}]),
                    ("npm test 2>&1 >/dev/null", 1, [{"kind": "test", "exit": 1}])])

    def test_exit_return_and_set_e_make_later_gates_uncertain(self):
        self.check([("npm test; exit 0", 0, [{"kind": "test", "exit": None}]),
                    ("exit 0; npm test", 0, [{"kind": "test", "exit": None, "uncertain": True}]),
                    ("return 1; npm test", 1, [{"kind": "test", "exit": None, "uncertain": True}]),
                    ("set -euo pipefail\nnpm test", 0, [{"kind": "test", "exit": 0}]),
                    ("set -e; npm test", 1, [{"kind": "test", "exit": None, "uncertain": True}]),
                    ("set -o errexit; npm run lint; npm test", 3, [{"kind": "lint", "exit": None, "uncertain": True}, {"kind": "test", "exit": None, "uncertain": True}]),
                    ("set -x\nnpm test", 1, [{"kind": "test", "exit": 1}])])

    def test_a_gate_inside_a_compound_command_is_uncertain(self):
        maybe = [{"kind": "test", "exit": None, "uncertain": True}]
        self.check([("if x; then npm test; fi", 0, []), ("for f in a; do\n  npm test\ndone", 0, maybe),
                    ("if [ -f x ]; then\n  npm test\nfi", 0, maybe), ("f() {\n  npm test\n}", 0, maybe),
                    ("(cd x && npm test)", 0, maybe), ("{ npm test; }", 0, []), ("while x; do\n npm test\ndone\nnpm run lint", 1,
                     [{"kind": "test", "exit": None, "uncertain": True}, {"kind": "lint", "exit": None, "uncertain": True}]),
                    ("docker run x && npm test", 0, [{"kind": "test", "exit": 0}])])

    def test_a_long_list_has_no_cap_and_no_guess(self):
        command = "; ".join(["git status"] * 40) + "; npm test"
        self.check([(command, 0, [{"kind": "test", "exit": 0}]), (command, 1, [{"kind": "test", "exit": 1}])])
        chain = " && ".join(["git status"] * 60 + ["npm test", "npm run lint"])
        self.check([(chain, 0, [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 0}]),
                    (chain, 1, [{"kind": "test", "exit": None, "uncertain": True}, {"kind": "lint", "exit": None, "uncertain": True}])])

    def test_totals_split_attributed_unattributed_uncertain_and_denied(self):
        runs = trial_scan.gate_runs("npm test && npm run lint && npx tsc --noEmit", 1) + trial_scan.gate_runs("npm test; npm run lint", 0)
        runs.append({"kind": "test", "exit": None, "denied": True})
        self.assertEqual(trial_scan.gate_totals(runs), {"total": 3, "uncertain": 2, "denied": 1, "unattributed": 2, "parse_failed": 0})
        self.assertEqual(trial_scan.gate_totals(None), {"total": 0, "uncertain": 0, "denied": 0, "unattributed": 0, "parse_failed": 0})


PARSE_FAILED = [{"kind": None, "exit": None, "parse_failed": True}]


class ShellTokenizerTests(unittest.TestCase):
    """Review 5 F1: one pass skips a heredoc body as data wherever it opens, so an apostrophe in it opens no quote."""

    def check_found(self, command, code, expected):
        with self.subTest(command=command, code=code):
            self.assertEqual(trial_scan.gate_runs(command, code), expected)

    def test_a_heredoc_in_a_substitution_with_an_apostrophe_does_not_hide_the_gate_after_it(self):  # review 5 F1
        commit = 'git commit -m "$(cat <<\'EOF\'\nfix: don\'t crash\nEOF\n)"'
        self.check_found(commit + " && npm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found(commit + "; npm test", 1, [{"kind": "test", "exit": 1}])
        self.check_found(commit + "\nnpm test", 1, [{"kind": "test", "exit": 1}])
        self.check_found("x=\"$(cat <<'EOF'\nit's\nEOF\n)\"\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found("x=$(cat <<'EOF'\nit's\nEOF\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found("x=`cat <<'EOF'\nit's\nEOF\n`\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found("x=$(cat <<-'EOF'\n\tit's\n\tEOF\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.assertEqual(trial_scan.gate_kinds(commit + " && npm test"), ["test"])

    def test_a_heredoc_body_in_a_group_is_data_whatever_it_holds(self):
        for body in ("it's", 'say "hi', "a ) b", "a ( b", "`tick", "$(unclosed", "npm test", "# it's"):
            self.check_found('x="$(cat <<\'EOF\'\n%s\nEOF\n)"\nnpm run lint' % body, 0, [{"kind": "lint", "exit": 0}])
        self.check_found("x=$(cat <<A <<'B'\nit's\nA\nthat's\nB\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found('echo "$(echo "$(cat <<\'EOF\'\nit\'s\nEOF\n)")" && npm test', 0, [{"kind": "test", "exit": 0}])
        self.check_found("x=$(cat <<EOF\nit's\nEOF\n); y=$(cat <<EOF\nthat's\nEOF\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])

    def test_a_marker_that_is_not_an_opener_in_a_group_opens_no_body(self):
        for command in ("x=$(echo '<<EOF')\nnpm test", 'x=$(echo "<<EOF")\nnpm test', "x=$(echo $((1<<3)))\nnpm test",
                        'x="$((1<<3))"\nnpm test', "x=$(cat <<<npm)\nnpm test", "x=$(echo \\<<EOF)\nnpm test"):
            self.check_found(command, 0, [{"kind": "test", "exit": 0}])

    def test_tab_stripping_and_a_delimiter_line_with_other_text(self):
        self.check_found("x=$(cat <<-EOF\n\tit's\n\tEOF\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])
        self.check_found("x=$(cat <<EOF\nit's\n EOF\nEOF\n)\nnpm test", 0, [{"kind": "test", "exit": 0}])  # ' EOF' is body

    def test_ordinary_calls_are_not_parse_failed(self):
        for command in ("npm test", "echo \"it's\"", "echo it\\'s", "echo 'say \"hi'", 'echo "$(date)"', "echo `date`", "cat <<EOF\nx\nEOF",
                        "cat <<EOF\nx\nEOF\n", "echo $((1<<3))", "echo '<<EOF'", "", "   ", "# only a comment's apostrophe", "ls | wc",
                        "a && b || c; d &"):
            with self.subTest(command=command):
                self.assertFalse(any(r.get("parse_failed") for r in trial_scan.gate_runs(command, 0)))

    def test_an_unclosed_quote_group_or_heredoc_is_parse_failed_not_empty(self):  # review 5: fail visible
        unreadable = ("echo 'abc\nnpm test", 'echo "abc\nnpm test', "echo $(npm test", "echo `npm test", 'echo "$(npm test',
                      "cat <<EOF\nnpm test\n", "cat <<EOF", "cat <<EOF\n", "cat <<'EOF'\nit's\nnpm test",
                      'x="$(cat <<\'EOF\'\nit\'s\nnpm test\n)"', "x=$(cat <<EOF)\nnpm test", "echo $(echo 'a)\nnpm test",
                      "echo $(echo \"a)\nnpm test", "echo `echo 'a`\nnpm test", "cat <<A <<B\nx\nA\n", "echo $((1+2)",
                      "echo " + "$(" * 5000 + "npm test", "echo " + '"$(' * 5000 + "npm test")
        for command in unreadable:
            for code in (0, 1, None):
                with self.subTest(command=command[:40], code=code):
                    self.assertEqual(trial_scan.gate_runs(command, code), PARSE_FAILED)
            self.assertEqual(trial_scan.gate_kinds(command), [])

    def test_parse_failed_is_counted_by_gate_totals_and_not_as_a_gate(self):
        runs = trial_scan.gate_runs("echo 'x\nnpm test", 0) + trial_scan.gate_runs("npm test", 0) + trial_scan.gate_runs("cat <<EOF", 1)
        self.assertEqual(trial_scan.gate_totals(runs), {"total": 1, "uncertain": 0, "denied": 0, "unattributed": 0, "parse_failed": 2})
        self.assertEqual(trial_scan.gate_totals(None), {"total": 0, "uncertain": 0, "denied": 0, "unattributed": 0, "parse_failed": 0})
        self.assertEqual(trial_scan.gate_totals(PARSE_FAILED), {"total": 0, "uncertain": 0, "denied": 0, "unattributed": 0, "parse_failed": 1})

    def test_a_parse_failed_call_in_a_transcript_shows_in_the_totals(self):
        lines = (use("a", "Bash", command="git commit -m 'don't' && npm test") + result("a", "Exit code 1\nx", True)
                 + use("b", "Bash", command="npm test") + result("b", "ok"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.jsonl"
            path.write_text(lines, encoding="utf-8")
            out = trial_scan.scan_claude(path)
        self.assertEqual(out["gate_runs"], PARSE_FAILED + [{"kind": "test", "exit": 0}])
        self.assertEqual(trial_scan.gate_totals(out["gate_runs"])["parse_failed"], 1)


class KnownLimitsTests(unittest.TestCase):
    """One test per bullet of the Known limits in lib/trial_scan.py, written against what the code does."""

    def kinds(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(trial_scan.gate_runs(command, 0), [], command)

    def test_gate_bullet_a_gate_is_found_only_where_a_pipeline_stage_starts(self):
        for command in ("npm test", "cd w && FOO=1 npx tsc --noEmit", "env X=1 timeout 60 npm run lint", "time command pytest -q",
                        "a | npm test", "a; b\nnpm test"):
            with self.subTest(command=command):
                self.assertTrue(trial_scan.gate_kinds(command), command)
        self.kinds(['bash -c "npm test"', "echo \"$(npm test)\"", "cat <<EOF\nnpm test\nEOF", "echo $((1+2))", "xargs npm test",
                    "echo a | xargs npm test", "find . -exec npm test {} +", "sudo npm test", "! npm test", "nice npm test"])

    def test_gate_bullet_groups_and_branch_keywords_head_a_stage_and_hide_the_gate(self):  # review 5 F2
        self.kinds(["(npm test)", "{ npm test; }", "cd a && (npm test)", "x && { npm test; }", "if x; then npm test; fi",
                    "if x; then npm test; else npm run lint; fi", "for f in a; do npm test; done", "while x; do npm test; done",
                    "case x in a) npm test;; esac", "if x; then :; elif y; then npm test; fi"])

    def test_gate_bullet_a_gate_on_its_own_line_in_a_compound_is_found_and_uncertain(self):  # review 5 F2
        maybe = [{"kind": "test", "exit": None, "uncertain": True}]
        for command in ("for f in a; do\n  npm test\ndone", "if x; then\n  npm test\nfi", "f() {\n  npm test\n}",
                        "(cd x && npm test)", "(cd x; npm test)", "while x; do\n npm test\ndone"):
            with self.subTest(command=command):
                self.assertEqual(trial_scan.gate_runs(command, 0), maybe)

    def test_uncertain_bullet_exactly_when(self):
        un = {"kind": "test", "exit": None, "uncertain": True}
        table = [
            # follows && or || and the call did not pass as a plain && list
            ("x && npm test", 0, [{"kind": "test", "exit": 0}]), ("x && npm test", 1, [un]), ("x && npm test", None, [un]),
            ("x || npm test", 0, [un]), ("x && y; z && npm test", 0, [{"kind": "test", "exit": 0}]),
            ("x && npm test; z", 0, [un]),
            # a compound command anywhere in the call
            ("if x; then :; fi\nnpm test", 0, [un]), ("npm test\nfor f in a; do :; done", 0, [un]), ("f() { :; }\nnpm test", 0, [un]),
            ("(cd x)\nnpm test", 0, [un]), ("{ :; }\nnpm test", 0, [un]),
            # exit / return, and set -e in a call that did not pass
            ("exit 0; npm test", 0, [un]), ("return 1; npm test", 1, [un]), ("set -e; npm test", 1, [un]),
            ("set -e; npm test", None, [un]), ("set -e; npm test", 0, [{"kind": "test", "exit": 0}]),
            ("set -e\nnpm test", 2, [un]),
            # not triggers: trap, bash -e -c, set +e, a plain list
            ("trap 'x' ERR; npm test", 1, [{"kind": "test", "exit": 1}]), ("set +e; npm test", 1, [{"kind": "test", "exit": 1}]),
            ("bash -e -c 'x'; npm test", 1, [{"kind": "test", "exit": 1}]),
            ("a; b; npm test", 3, [{"kind": "test", "exit": 3}])]
        for command, code, expected in table:
            with self.subTest(command=command, code=code):
                self.assertEqual(trial_scan.gate_runs(command, code), expected)

    def test_totals_bullet(self):
        runs = [{"kind": "test", "exit": 0}, {"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True},
                {"kind": "tsc", "exit": None, "denied": True}] + PARSE_FAILED
        self.assertEqual(trial_scan.gate_totals(runs), {"total": 2, "uncertain": 1, "denied": 1, "unattributed": 1, "parse_failed": 1})

    def test_parse_failed_bullet_a_call_the_tokenizer_cannot_close(self):
        for command in ("echo 'x", 'echo "x', "echo $(x", "echo `x", "cat <<EOF\nx"):
            self.assertEqual(trial_scan.gate_runs(command, 0), PARSE_FAILED, command)
        # an apostrophe in a `#` comment inside $( ) is not recognised as a comment: it opens a quote and the call fails
        self.assertEqual(trial_scan.gate_runs("x=$(echo a # it's\n)\nnpm test", 0), PARSE_FAILED)
        self.assertEqual(trial_scan.gate_runs("x=$(echo a # its\n)\nnpm test", 0), [{"kind": "test", "exit": 0}])
        # outside a substitution the comment is dropped, so the apostrophe is harmless
        self.assertEqual(trial_scan.gate_runs("echo a # it's\nnpm test", 0), [{"kind": "test", "exit": 0}])

    def test_heredoc_bullet_shell_heredocs_in_a_command_and_in_a_written_file(self):
        # a command: a heredoc with no end line is parse_failed; in a written shell file the body runs to the end of the text
        self.assertEqual(trial_scan.gate_runs("cat <<EOF\nnpm test\n", 0), PARSE_FAILED)
        self.assertEqual(trial_scan.write_slips("tests/run.sh", "cat <<EOF\nsleep 2\n"), [])
        # `<<` in a (( )) command is read as an opener: with no matching line the call is parse_failed (not counted)
        self.assertEqual(trial_scan.gate_runs("(( x = 1 << 2 ))\nnpm test", 0), PARSE_FAILED)
        self.assertEqual(trial_scan.gate_runs("[[ a << b ]]\nnpm test", 0), PARSE_FAILED)
        self.assertEqual(trial_scan.gate_runs("(( x = 1 << 2 ))\n2\nnpm test", 0), [dict(kind="test", exit=None, uncertain=True)])  # "2" ends the body

    def test_pipe_ampersand_bullet(self):  # review 5 N1: |& is read as a pipe then a background &
        self.assertEqual(trial_scan.gate_runs("false && echo |& npm test", 1), [{"kind": "test", "exit": 1}])
        self.assertEqual(trial_scan.gate_runs("npm test |& tail -3", 0), [{"kind": "test", "exit": None}])

    def test_command_slips_heredoc_bullet(self):  # review 5 N3: the rule is a text match, a quoted << still counts
        self.assertEqual(trial_scan.command_slips("echo '<<EOF'"), ["heredoc"])
        self.assertEqual(trial_scan.command_slips('echo "<<EOF"'), ["heredoc"])
        self.assertEqual(trial_scan.command_slips('echo "git push"'), ["git_push"])  # every rule is a text match
        self.assertEqual(trial_scan.command_slips("echo $((1<<3))"), [])


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

    def test_and_chain_that_passed_gives_the_exit_to_the_last_gate_only(self):
        self.assertEqual(self.gates("npm test && npm run lint", "ok"), [{"kind": "test", "exit": None}, {"kind": "lint", "exit": 0}])

    def test_a_failure_behind_an_unproven_prefix_is_not_given_to_the_gate(self):
        self.assertEqual(self.gates("echo start && npm test", "Exit code 2\nFAIL", True),
                         [{"kind": "test", "exit": None, "uncertain": True}])

    def test_an_or_branch_may_or_may_not_have_run(self):
        both = [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}]
        self.assertEqual(self.gates("npm test || npm run lint", "ok"), both)
        self.assertEqual(self.gates("npm test || npm run lint", "Exit code 1\nx", True), both)

    def test_semicolon_runs_both_and_keeps_repeats(self):
        self.assertEqual(self.gates("npm test; npm test", "Exit code 1\nx", True), [{"kind": "test", "exit": None}, {"kind": "test", "exit": 1}])
        self.assertEqual(self.gates("npm test; npm test", "ok"), [{"kind": "test", "exit": None}, {"kind": "test", "exit": 0}])

    def test_a_failure_with_no_code_in_the_result_gives_no_exit(self):
        self.assertEqual(self.gates("npm test", "boom", True), [{"kind": "test", "exit": None}])

    def test_no_result_leaves_later_branches_uncertain_and_exits_unknown(self):
        self.assertEqual(self.gates("npm test && npm run lint"), [{"kind": "test", "exit": None}, {"kind": "lint", "exit": None, "uncertain": True}])

    def test_a_gate_only_in_a_substitution_is_not_counted_even_when_the_call_is_denied(self):
        self.assertEqual(self.gates('echo "$(npm test)"', "done"), [])
        self.assertEqual(self.gates('echo "$(npm test)" > /no/dir/f', "Exit code 1\nboom", error=True), [])
        self.assertEqual(self.gates('echo "$(npm test)"', "The user denied this tool call", error=True), [])

    def test_denied_chain_counts_no_run(self):
        out = self.scan(use("a", "Bash", command="npm test && npm run lint"),
                        result("a", "PreToolUse:Bash hook error: guard denied this tool call: x", error=True))
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": None, "denied": True}, {"kind": "lint", "exit": None, "denied": True}])

    def test_total_counts_only_gates_known_to_have_run(self):
        out = self.scan(use("a", "Bash", command="npm test && npm run lint"), result("a", "Exit code 1\nx", True),
                        use("b", "Bash", command="npm test"),
                        result("b", "PreToolUse:Bash hook error: guard denied this tool call: x", error=True))
        self.assertEqual(trial_scan.gate_totals(out["gate_runs"]), {"total": 1, "uncertain": 1, "denied": 1, "unattributed": 1, "parse_failed": 0})

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
        # `cd /w/wt && python3 -m unittest ... | tail -5` exited 1: the cd may have failed, and tail owns the pipe's status
        self.assertEqual(out["gate_runs"], [{"kind": "test", "exit": None, "uncertain": True}, {"kind": "tsc", "exit": 0}])


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
