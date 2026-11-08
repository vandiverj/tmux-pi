import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_fixture as fx  # noqa: E402
import test_bench_score as score  # noqa: E402
from unittest import mock  # noqa: E402

from bench import record, specs  # noqa: E402

BIN = Path(__file__).resolve().parent.parent / "bin" / "tp-bench"
loader = importlib.machinery.SourceFileLoader("tp_bench_cli", str(BIN))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("tp_bench_cli", loader))
loader.exec_module(cli)


def call(*argv, home):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--results-dir", str(home / "results"), *argv])
    return code, out.getvalue(), err.getvalue()


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name)

    def test_compare_prints_the_table_and_verdict(self):
        record.write(record.record_path(self.home / "results", "ctl", 1), score.run_record("ctl", 1, score.arm(2)))
        record.write(record.record_path(self.home / "results", "new", 1), score.run_record("new", 1, score.arm(4)))
        code, out, _ = call("compare", "ctl", "new", home=self.home)
        self.assertEqual(code, 0)
        self.assertIn("ctl mean", out)
        self.assertIn("verdict: keep", out)

    def test_compare_of_an_unknown_label_exits_2(self):
        code, _, err = call("compare", "nope", "nada", home=self.home)
        self.assertEqual((code, "nope" in err), (2, True))

    def test_verify_refuses_a_changed_answer_with_the_task_id_and_exit_3(self):
        bench, ans, _ = fx.make_bench(self.home)
        (ans / "code-f.test.ts").write_text("tampered")
        with mock.patch.dict(os.environ, {"TP_BENCH_ANSWERS": str(ans)}), \
                mock.patch.object(cli, "load", lambda args: (bench, specs.load_specs(bench / "tasks"))):
            code, _, err = call("verify", home=self.home)
        self.assertEqual(code, 3)
        self.assertIn("code-f", err)

    def test_run_refuses_something_that_is_not_a_checkout_and_a_bad_label(self):
        self.assertEqual(call("run", "--arm", str(self.home), "--label", "x", home=self.home)[0], 2)
        self.assertEqual(call("run", "--arm", str(self.home), "--label", "../x", home=self.home)[0], 2)

    def test_a_failed_tolerated_call_is_null_in_the_record_never_a_value_made_from_its_output(self):
        good = {("git", "rev-parse"): fx.Result(0, "abc123\n", ""), ("git", "status"): fx.Result(0, " M a.txt\n", ""),
                ("which", "claude"): fx.OK, ("which", "pi"): fx.OK, ("claude", "--version"): fx.Result(0, "2.1.0 (Claude Code)\n", ""),
                ("pi", "--version"): fx.Result(0, "0.9\n", ""), ("node", "--version"): fx.Result(0, "v22\n", ""),
                ("curl", "-s"): fx.Result(0, '{"data": [{"id": "qwen", "state": "loaded", "loaded_context_length": 4096}]}', "")}
        # (call that fails, record field, value when all is well, value when it fails); the failing call prints on stdout AND stderr
        rows = [(("git", "status"), ("arm", "dirty"), True, None), (("git", "rev-parse"), ("arm", "commit"), "abc123", None),
                (("git", "rev-parse"), ("versions", "arm_commit"), "abc123", None),
                (("claude", "--version"), ("versions", "claude"), "2.1.0 (Claude Code)", None), (("pi", "--version"), ("versions", "pi"), "0.9", None),
                (("node", "--version"), ("versions", "node"), "v22", None),
                (("curl", "-s"), ("versions", "lmstudio_context"), {"qwen": 4096}, None)]
        for failing, (group, field), well, broken in rows:
            for fails in (False, True):
                with self.subTest(call=failing, field=field, fails=fails):
                    def inject(shell, argv):
                        key = (Path(argv[0]).name, argv[1] if Path(argv[0]).name != "git" else argv[argv.index("-C") + 2])
                        if key == failing and fails:
                            return fx.Result(1, "boom out", "boom err")
                        return good.get(key, fx.OK)
                    shell = fx.FakeShell(self.home, inject=inject)
                    got = {"arm": cli.arm_info(shell, "/arm"), "versions": cli.versions_of(shell, "/arm")}[group][field]
                    self.assertEqual(got, broken if fails else well)

    def test_lmstudio_context_is_null_for_any_answer_that_is_not_a_model_list_and_never_raises(self):
        loaded = '{"id": "qwen", "state": "loaded", "loaded_context_length": 4096}'
        rows = [('{"data":{"error":"not ready"}}', None), ('{"data":"x"}', None), ('{"data":[1]}', None), ('{"data":[null]}', None),
                ('{"data":[{"state":"loaded","loaded_context_length":4096}]}', None), ('{"data":[{"state":"loaded"}]}', None),
                ('{"data": []}', {}), ("[]", None), ('"text"', None), ("null", None), ("", None),
                ("not json", None), ('{"data":[{"id":"q","state":"loaded","loaded_context_length":"big"}]}', None),
                ('{"data":[{"id":"q","state":"loaded","loaded_context_length":true}]}', None),
                ('{"data":[%s,{"id":"q","state":"loaded","loaded_context_length":"big"}]}' % loaded, None),
                ('{"data":[{"id":"q","state":"not-loaded","loaded_context_length":"junk"},{"state":"not-loaded","id":7}]}', {}),
                ('{"data": [%s, {"id": "idle", "state": "not-loaded"}]}' % loaded, {"qwen": 4096})]
        for text, want in rows:
            with self.subTest(text=text):
                shell = fx.FakeShell(self.home, inject=lambda s, argv, text=text: fx.Result(0, text, "") if argv[0] == "curl" else None)
                self.assertEqual(cli.lmstudio_context(shell), want)

    def test_run_refuses_a_session_key_in_env(self):
        arm = self.home / "arm"
        for tool in ("tp", "tp-agent", "tp-exit"):
            (arm / "bin").mkdir(parents=True, exist_ok=True)
            (arm / "bin" / tool).write_text("")
        code, _, err = call("run", "--arm", str(arm), "--label", "x", "--env", "TMUX=/x", home=self.home)
        self.assertEqual(code, 2)
        self.assertIn("TMUX", err)


if __name__ == "__main__":
    unittest.main()
