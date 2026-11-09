"""A test never inherits the session of whoever runs the suite. The first statement of every tests/test_*.py is
`import session_env`, which removes the session keys from this process (tests/__init__.py does the same for package runners), so
in-process code and every subprocess, whatever env-copy form it uses, sees none of them. The checks below run each way of
starting the suite, load every module alone, and read each module's first statement from its syntax tree."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import ast
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tokenize
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lib"))
import session_env  # noqa: E402
from session_env import SESSION_KEYS, isolate_session, outside_session  # noqa: E402

# Every TP_*/CLAUDE_CODE_*/TMUX* name that bin/ and lib/ mention, classified. A new name fails the parity test until it is added
# here, as identity (and then to SESSION_KEYS) or as configuration.
CONFIG = {
    "TP_ROOT", "TP_STATE_DIR", "TP_LIB_DIR", "TP_MODEL_CATALOG", "TP_ROUTING_EVALS_TEXT", "TP_ROUTING_EVALS", "TP_ROUTING_EVAL_DIR",
    "TP_WORKDIR", "TP_WORK_DIR", "TP_WORK_CONFIG", "TP_WORK_LOCK_TIMEOUT", "TP_TICKET_PREFIXES", "TP_DOCS_TOOLS", "TP_DOCS_PROJECT",
    "TP_CTX_TOOLS", "TP_CTX_PROJECT", "TP_CTX_TODAY", "TP_CTX_NAV_MODULES", "TP_LOCAL_CONFIG", "TP_PROXY", "TP_TRUST",
    "TP_TRUST_GUARD_LOG_DIR", "TP_CLAUDE_CONFIG_DIR", "TP_PI_NODE", "TP_NO_ATTACH", "TP_ROUTER_LLM_URL", "TP_ROUTER_BASE_URL",
    "TP_ROUTER_MODEL", "TP_REDACTION", "TP_REDACTION_DIR", "TP_REDACTION_LLM_URL", "TP_REDACTION_WATCHDOG",
    "TP_REDACTION_SECRET_FILES", "TP_LMS", "TP_AGENT", "TP_PS", "TP_LAYA_DEVICE", "TP_STATUS_WINDOW", "TP_STATUS_CLOCK",
    "TP_STATUS_POLL", "TP_MAIN_HARNESS",  # launcher choice of harness, not who the caller is
    "TP_MAIN",  # TP_MAIN is the stem of shell variable names in bin/tp
    "TP_BENCH_ANSWERS",  # lib/bench/answers.py:19: answer-directory override, not caller identity
    "TP_BENCH_HOME",  # lib/bench/answers.py:15: benchmark data-home override, not caller identity
    "TP_BENCH_PROJECT",  # bin/tp-bench:155: --project default, not caller identity
    "TP_BENCH_RESULTS",  # bin/tp-bench:156: --results-dir default, not caller identity
    "TP_BENCH_WORK",  # bin/tp-bench:157: --work-root default, not caller identity
    "TP_HL1",  # lib/bench/compare.py:5,37: arm-gate configuration allowed to differ between records
    "TP_X1_ARM",  # lib/bench/compare.py:5,37: arm-gate configuration allowed to differ between records
    "TP_TRUST_CAPTURE_TIMEOUT",  # lib/trust_dialog.py:22,37: capture-timeout test hook, read at each call, not caller identity
    "TP_TRUST_SCAN_BUDGET",  # lib/trust_dialog.py:24,95: scan-budget test hook, read when a Scanner starts, not caller identity
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_DISABLE_POLICY_SKILLS", "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
}
NAME = re.compile(r"TP_[A-Z0-9_]+|CLAUDE_CODE_[A-Z_]+|TMUX_PANE|TMUX")
SCRUB = ("session_env", None)  # `import session_env`, alone and unrenamed
# Loads one module the way a filtered discover does (`discover -s tests -p <module>.py`, with or without -t ., which is how
# tests/run_parallel.py starts each module), in a process whose caller set the session keys, and records which keys the module's
# import left set. session_env, the tests package and the module are dropped from sys.modules before each load, so each module
# runs and scrubs (or not) on its own, as it would in a fresh process.
LOAD_EACH = r"""
import json, os, sys, unittest
keys, dummy, top, names = json.loads(sys.argv[1])
left = {}
for name in names:
    for cached in ("session_env", "tests.session_env", "tests", name, "tests." + name):  # a module another one imported is cached
        sys.modules.pop(cached, None)
    os.environ.update(dummy)
    loader = unittest.TestLoader()
    if not loader.discover("tests", pattern=name + ".py", top_level_dir=top).countTestCases() and not loader.errors:
        left[name] = "not found"
    elif any(key in os.environ for key in keys):
        left[name] = sorted(key for key in keys if key in os.environ)
print(json.dumps(left))
"""


def _code_starts(body):
    """Index of the first statement that runs code: past the module docstring and `from __future__` imports."""
    index = 1 if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str) else 0
    while index < len(body) and isinstance(body[index], ast.ImportFrom) and body[index].module == "__future__":
        index += 1
    return index


def first_statement(source):
    """The first statement of a module that runs code (an AST node), or None. A module written for a newer Python than this one
    cannot be parsed whole; then the shortest head of it, cut at a logical line end, that parses and holds such a statement is used."""
    try:
        body = ast.parse(source).body
        return body[_code_starts(body)] if _code_starts(body) < len(body) else None
    except SyntaxError:
        pass
    lines = source.splitlines(True)
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.NEWLINE:
                continue
            try:
                head = ast.parse("".join(lines[:token.end[0]])).body
            except SyntaxError:
                continue
            if _code_starts(head) < len(head):
                return head[_code_starts(head)]
    except (tokenize.TokenError, SyntaxError):
        pass
    return None


def starts_with_scrub(source):
    node = first_statement(source)
    return isinstance(node, ast.Import) and [(alias.name, alias.asname) for alias in node.names] == [SCRUB]


def all_test_modules():
    return sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))


PROBE = "SESSION_ENV_PROBE_OUT"  # not a session key: where the probe test below writes what pane_table() did


def child_env(**extra):
    return {**outside_session(), **extra}


class SessionEnvTests(unittest.TestCase):
    SESSION = {key: f"caller-{key}" for key in SESSION_KEYS}

    def test_importing_the_tests_package_removed_every_session_key_from_this_process(self):
        self.assertEqual([key for key in SESSION_KEYS if key in os.environ], [])
        if "tests.session_env" in sys.modules:  # package runners: the two names are one module, so one CALLER_SESSION
            self.assertIs(sys.modules["session_env"], sys.modules["tests.session_env"])

    def test_every_way_of_running_the_suite_scrubs_before_any_test_code(self):
        """One probe module, started the five ways the suite is run, with a dummy session in the caller's shell."""
        runners = {
            "-m unittest tests.X": ["-m", "unittest", "tests.test_session_probe"],
            "single test id": ["-m", "unittest", "tests.test_session_probe.Probe.test_probe"],
            "discover -s tests -t .": ["-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_session_probe.py"],
            "discover -s tests": ["-m", "unittest", "discover", "-s", "tests", "-p", "test_session_probe.py"],
            "python3 tests/test_x.py": ["tests/test_session_probe.py"],
        }
        for name, argv in runners.items():
            with self.subTest(runner=name), tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "seen.json"
                env = {**outside_session(), "SESSION_PROBE_OUT": str(out), **{key: f"dummy-{key}" for key in SESSION_KEYS}}
                env.update(TMUX="/tmp/x,1,0", TMUX_PANE="%999")
                run = subprocess.run([sys.executable, *argv], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(out.read_text()), [], "the probe saw the caller's session")

    def test_the_reviewers_repro_passes_under_discover_without_t(self):
        env = {**outside_session(), "TMUX": "/tmp/x,1,0", "TMUX_PANE": "%999"}
        run = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_lane_writer.py", "-k",
                              "test_alive_on_tree_is_alive_on_workdir_with_the_same_answer"],
                             cwd=ROOT, env=env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stderr, r"Ran 1 test")

    def test_the_first_statement_that_runs_code_must_be_the_scrub_import(self):
        rows = [  # (source, accepted)
            ('import session_env\nimport os\n', True),
            ('"""Docstring."""\nimport session_env\n', True),
            ('"""Docstring."""\nfrom __future__ import annotations\nimport session_env\n', True),
            ('#!/usr/bin/env python3\n# a comment\n\nimport session_env  # noqa\n', True),
            ('import session_env\nthis line is not Python\n', True),  # parsed from the head: written for a newer Python
            ('SEEN = __import__("os").environ.get("TP_MAIN_ID")\nimport session_env\n', False),  # assignment first
            ('print("hi")\nimport session_env\n', False),  # expression first
            ('"""Docstring."""\n"""A second string runs as an expression."""\nimport session_env\n', False),
            ('import os\nimport session_env\n', False),  # another import first
            ('import os, session_env\n', False),
            ('import session_env as env\n', False),
            ('from session_env import outside_session\n', False),
            ('from tests import session_env\n', False),
            ('if True:\n    import session_env\n', False),  # scrub inside if
            ('try:\n    import session_env\nexcept ImportError:\n    pass\n', False),  # scrub inside try
            ('def setup():\n    import session_env\n', False),
            ('x = 1\nthis line is not Python\n', False),
            ('"""Only a docstring."""\n', False),
        ]
        for source, accepted in rows:
            with self.subTest(source=source):
                self.assertEqual(starts_with_scrub(source), accepted)

    def test_every_test_module_runs_the_scrub_import_first(self):
        lacking = [name for name in all_test_modules() if not starts_with_scrub((ROOT / "tests" / f"{name}.py").read_text())]
        self.assertEqual(lacking, [], "the first statement that runs code must be `import session_env`")

    def test_a_filtered_discover_of_any_one_module_leaves_no_session_key(self):
        """Every tests/test_*.py, loaded alone by a filtered discover with and without a top-level dir, in a process whose caller
        set the whole session."""
        dummy = {key: f"dummy-{key}" for key in SESSION_KEYS}
        dummy.update(TMUX="/tmp/x,1,0", TMUX_PANE="%999")
        self.maxDiff = None
        names = []
        for name in all_test_modules():  # one this Python cannot compile runs no statement here; its first statement is checked above
            try:
                compile((ROOT / "tests" / f"{name}.py").read_text(), name, "exec")
                names.append(name)
            except SyntaxError:
                pass
        for top in (None, "."):
            with self.subTest(top_level_dir=top):
                run = subprocess.run([sys.executable, "-c", LOAD_EACH, json.dumps([SESSION_KEYS, dummy, top, names])], cwd=ROOT,
                                     env={**outside_session(), **dummy}, text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr[-3000:])
                self.assertEqual(json.loads(run.stdout.strip().splitlines()[-1]), {})

    def test_a_child_with_a_plain_copy_or_no_env_argument_sees_no_session_key(self):
        code = "import os, json; print(json.dumps([k for k in %r if k in os.environ]))" % (SESSION_KEYS,)
        for env in ({"env": os.environ.copy()}, {"env": {**os.environ}}, {"env": dict(os.environ)}, {}):
            run = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, **env)
            self.assertEqual(json.loads(run.stdout), [], env.keys())

    def test_what_was_removed_is_kept_for_reading_and_never_goes_back_into_the_environment(self):
        with mock.patch.dict(os.environ, self.SESSION), mock.patch.dict(session_env.CALLER_SESSION, clear=True):
            session_env.drop_caller_session()
            self.assertEqual(session_env.CALLER_SESSION, self.SESSION)
            self.assertEqual([key for key in SESSION_KEYS if key in os.environ], [])

    def test_outside_session_drops_exactly_the_session_keys_and_copies(self):
        source = {**self.SESSION, "PATH": "/bin", "TP_STATE_DIR": "/state", "TP_WORK_DIR": "/work"}
        self.assertEqual(outside_session(source), {"PATH": "/bin", "TP_STATE_DIR": "/state", "TP_WORK_DIR": "/work"})
        self.assertIn("TP_MAIN_ID", source)  # the argument is not changed
        with mock.patch.dict(os.environ, self.SESSION):
            self.assertFalse(set(SESSION_KEYS) & set(outside_session()))
            self.assertIn("TP_MAIN_ID", os.environ)

    def test_isolate_session_hides_the_keys_for_one_test_and_restores_them(self):
        class Probe(unittest.TestCase):
            seen = None

            def runTest(self):
                isolate_session(self)
                Probe.seen = {key: os.environ.get(key) for key in SESSION_KEYS}
                os.environ["TP_MAIN_ID"] = "set-by-the-test"

        with mock.patch.dict(os.environ, self.SESSION):
            result = unittest.TestResult()
            Probe().run(result)
            self.assertTrue(result.wasSuccessful(), result.errors)
            self.assertEqual(set(Probe.seen.values()), {None})
            self.assertEqual({key: os.environ[key] for key in SESSION_KEYS}, self.SESSION)

    def test_pane_table_in_process_is_the_same_with_a_dummy_tmux_in_the_callers_shell_as_without(self):
        """The runner is a subprocess started with `python3 -m unittest` and the dummy session env, so the package import is under test."""
        results = {}
        for mode, session in (("dummy", {"TMUX": "/tmp/x,1,0", "TMUX_PANE": "%999"}), ("unset", {})):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out.json"
                run = subprocess.run([sys.executable, "-m", "unittest", "tests.test_session_env.PaneTableProbe.test_probe"],
                                     cwd=ROOT, env=child_env(**{PROBE: str(out)}, **session), text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                results[mode] = json.loads(out.read_text())
        self.assertEqual(results["dummy"], results["unset"])

    def test_every_session_name_bin_and_lib_read_is_in_session_keys_or_classified_as_configuration(self):
        run = subprocess.run(["git", "grep", "-h", "-o", "-E", NAME.pattern, "--", "bin", "lib"], cwd=ROOT, text=True, capture_output=True)
        names = set(run.stdout.split())
        self.assertTrue(names, run.stderr)
        self.assertEqual(sorted(names - set(SESSION_KEYS) - CONFIG), [], "classify each as identity (add to SESSION_KEYS) or config (CONFIG)")
        self.assertEqual(sorted(set(SESSION_KEYS) - names), [], "a SESSION_KEYS entry no code reads")
        self.assertFalse(set(SESSION_KEYS) & CONFIG)


@unittest.skipUnless(os.environ.get(PROBE), "run by the pane_table test above, in a child process")
class PaneTableProbe(unittest.TestCase):
    def test_probe(self):
        import worker_process
        try:
            result = ["table", sorted(worker_process.pane_table())]
        except RuntimeError as error:
            result = ["refused", str(error)]
        Path(os.environ[PROBE]).write_text(json.dumps(result))


if __name__ == "__main__":
    unittest.main()
