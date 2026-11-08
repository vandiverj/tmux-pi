import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_fixture  # noqa: E402,F401
from bench import shell  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOOKUP = re.compile(r"\b(TMUX_PANE|TMUX|TP_SESSION|TP_STATE_DIR|TP_TASK_[A-Z_]+|TP_MAIN_[A-Z_]+|TP_TRIAL_[A-Z_]+|TP_MODE|CLAUDE_CODE_SESSION_ID)\b")
NOT_SESSION_LOOKUP = {"TP_MAIN_HARNESS"}  # which harness main runs; it names no session, task or main


class EnvBuilder(unittest.TestCase):
    def test_session_keys_cover_every_key_bin_and_lib_read_for_session_lookup(self):
        found = set()
        for base in ("bin", "lib"):
            for path in (ROOT / base).rglob("*"):
                if not path.is_file() or "bench" in path.parts or path.name == "tp-bench" or path.suffix in {".pyc", ".png"}:
                    continue
                try:
                    found.update(LOOKUP.findall(path.read_text()))
                except (UnicodeDecodeError, OSError):
                    continue
        self.assertEqual(sorted(found - NOT_SESSION_LOOKUP - set(shell.SESSION_KEYS)), [],
                         "a session key read in bin/ or lib/ is missing from lib/bench/shell.py SESSION_KEYS (or add it to NOT_SESSION_LOOKUP)")
        self.assertEqual(sorted(set(shell.SESSION_KEYS) - found), [], "SESSION_KEYS names a key nothing in bin/ or lib/ reads")

    def test_child_env_drops_session_keys_keeps_the_rest_and_adds_extras(self):
        base = {**{k: "x" for k in shell.SESSION_KEYS}, "PATH": "/bin", "TP_PI_NODE": ""}
        self.assertEqual(shell.child_env(base, {"TP_SESSION": "bench-a", "N": 1}), {"PATH": "/bin", "TP_PI_NODE": "", "TP_SESSION": "bench-a", "N": "1"})

    def test_a_real_child_process_gets_the_scrubbed_environment(self):
        leaks = {k: "LEAK" for k in shell.SESSION_KEYS}
        with mock.patch.dict(os.environ, leaks):
            done = shell.Shell().run([sys.executable, "-c", "import os,json;print(json.dumps(dict(os.environ)))"], env={"TP_SESSION": "bench-a"})
        got = json.loads(done.stdout)
        self.assertEqual({k: v for k, v in got.items() if k in shell.SESSION_KEYS}, {"TP_SESSION": "bench-a"})

    def test_only_shell_py_starts_processes(self):
        starters = re.compile(r"\bsubprocess\b|os\.(system|popen|exec\w*|spawn\w*)\(|\bPopen\b")
        for path in [*(ROOT / "lib" / "bench").glob("*.py"), ROOT / "bin" / "tp-bench"]:
            if path.name != "shell.py":
                self.assertIsNone(starters.search(path.read_text()), f"{path.name} starts a process outside lib/bench/shell.py")

    def test_session_ending_argv_is_built_only_in_the_ownership_checked_helper(self):
        import ast
        destructive = {"kill-session", "kill-window", "kill-pane", "tp-exit", "tp-reset"}
        where = set()
        for path in [*(ROOT / "lib" / "bench").glob("*.py"), ROOT / "bin" / "tp-bench"]:
            tree = ast.parse(path.read_text())
            for func in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for node in ast.walk(func):
                    if isinstance(node, ast.Constant) and node.value in destructive:
                        where.add((path.name, func.name))
        # nested walks list an outer function too; only stop_step may name them
        self.assertEqual({w for w in where if w[1] != "stop_workspace"}, {("runner.py", "stop_step")}, where)

    def test_missing_program_is_127_and_is_raised_unless_tolerated(self):
        self.assertEqual(shell.Shell().run(["/nonexistent/program"], tolerate="the test reads the code").returncode, 127)
        with self.assertRaises(shell.ShellError) as caught:
            shell.Shell().run(["/nonexistent/program"])
        self.assertEqual(caught.exception.result.returncode, 127)

    def test_a_nonzero_exit_and_a_timeout_raise_by_default_and_an_opt_out_needs_a_reason(self):
        sh = shell.Shell()
        with self.assertRaises(shell.ShellError):
            sh.run([sys.executable, "-c", "raise SystemExit(3)"])
        with self.assertRaises(shell.ShellError) as timed:
            # the child blocks on a read from a pipe nobody writes to: it ends only when the 0.2 s bound kills it, and no wait is timed
            sh.run([sys.executable, "-c", "import os; read_end, write_end = os.pipe(); os.read(read_end, 1)"], timeout=0.2)
        self.assertEqual(timed.exception.result.returncode, shell.TIMED_OUT)
        self.assertEqual(sh.run([sys.executable, "-c", "raise SystemExit(3)"], tolerate="exit code read by the test").returncode, 3)
        for bad in ("", "  ", True):
            with self.assertRaises(TypeError):
                sh.run([sys.executable, "-c", "pass"], tolerate=bad)

    def test_every_opt_out_carries_a_reason_and_only_functions_that_opt_out_read_an_exit_code(self):
        import ast
        sites = 0
        for path in [*(ROOT / "lib" / "bench").glob("*.py"), ROOT / "bin" / "tp-bench"]:
            if path.name == "shell.py":
                continue
            tree = ast.parse(path.read_text())
            strings = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                       and isinstance(n.value.value, str) for t in n.targets if isinstance(t, ast.Name)}
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "tolerate":
                    sites += 1
                    value = node.value
                    reason = value.value if isinstance(value, ast.Constant) else strings.get(getattr(value, "id", None))
                    if isinstance(value, ast.Name) and value.id == "tolerate":
                        continue  # a wrapper passing its own parameter through (the caller supplies the reason)
                    self.assertTrue(isinstance(reason, str) and len(reason.split()) >= 4, f"{path.name}:{node.value.lineno}: tolerate= needs a reason")
            for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
                reads = [n for n in ast.walk(func) if isinstance(n, ast.Attribute) and n.attr == "returncode"]
                opts = [n for n in ast.walk(func) if isinstance(n, ast.keyword) and n.arg == "tolerate"]
                takes_result = any(a.arg == "result" for a in func.args.args)  # a reader of a Result its caller already opted out for
                self.assertTrue(not reads or opts or takes_result, f"{path.name}:{func.name} reads an exit code without opting out of the check")
        self.assertGreaterEqual(sites, 15)  # the scan found the opt-outs it is meant to police


if __name__ == "__main__":
    unittest.main()
