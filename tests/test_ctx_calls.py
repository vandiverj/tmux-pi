"""CX1 ctx-store: the fail-closed call log (ctx-calls.jsonl). No usable task directory means no answer; a symlinked log is
refused; arguments are redacted before they are logged or hashed."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import calls, redact  # noqa: E402

AWS = "AKIA" + "IOSFODNN7EXAMPLE"


class CallLog(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.task = self.tmp / "task"
        self.task.mkdir()
        config = self.tmp / "work-config.json"
        config.write_text("{}")
        env = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_TASK_ID")}
        env.update({"TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(config)})
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.log = self.task / calls.LOG_NAME

    def lines(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def ok(self, text="three notes", notes=("kn-1",)):
        return lambda: calls.Answer("ok", text, list(notes), index_age_s=4)

    def test_no_task_dir_means_no_answer_and_run_is_never_called(self):
        ran = []
        with self.assertRaisesRegex(calls.LogUnavailable, "TP_TASK_DIR is not set"):
            calls.call("knowledge.list", {}, lambda: ran.append(1))
        self.assertEqual(ran, [])

    def test_an_unusable_task_dir_means_no_answer(self):
        link = self.tmp / "link"
        link.symlink_to(self.task)
        for value in ("relative/dir", str(self.tmp / "missing"), str(link), str(self.tmp / "work-config.json")):
            ran = []
            with self.assertRaises(calls.LogUnavailable, msg=value):
                calls.call("t", {}, lambda: ran.append(1), env={"TP_TASK_DIR": value})
            self.assertEqual(ran, [], value)

    def test_one_line_per_call_with_the_documented_fields(self):
        env = {"TP_TASK_DIR": str(self.task)}
        calls.call("knowledge.list", {"status": "approved"}, self.ok(), env=env)
        calls.call("knowledge.show", {"id": "kn-1"}, lambda: calls.Answer("invalid", "no such note", reason="no such note"), env=env)
        first, second = self.lines()
        self.assertEqual(set(first), {"ts", "tool", "args_hash", "args", "status", "bytes", "ms", "notes_returned", "index_age_s"})
        self.assertEqual((first["tool"], first["status"], first["bytes"], first["notes_returned"], first["index_age_s"]),
                         ("knowledge.list", "ok", len("three notes"), ["kn-1"], 4))
        self.assertEqual(first["args"], {"status": "approved"})
        self.assertEqual((second["status"], second["reason"]), ("invalid", "no such note"))
        self.assertEqual(len(first["args_hash"]), 16)
        self.assertNotEqual(first["args_hash"], second["args_hash"])

    def test_the_same_arguments_hash_the_same_regardless_of_order(self):
        env = {"TP_TASK_DIR": str(self.task)}
        calls.call("t", {"a": "1", "b": "2"}, self.ok(), env=env)
        calls.call("t", {"b": "2", "a": "1"}, self.ok(), env=env)
        one, two = self.lines()
        self.assertEqual(one["args_hash"], two["args_hash"])

    def test_a_symlinked_log_is_refused_and_the_target_is_untouched(self):
        target = self.tmp / "elsewhere.jsonl"
        target.write_text("keep\n")
        self.log.symlink_to(target)
        ran = []
        with self.assertRaisesRegex(calls.LogUnavailable, "refusing to answer without a log"):
            calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(target.read_text(), "keep\n")

    def test_a_log_that_cannot_be_written_withholds_the_answer(self):
        self.log.mkdir()  # a directory where the log file should be
        with self.assertRaises(calls.LogUnavailable):
            calls.call("t", {}, self.ok(), env={"TP_TASK_DIR": str(self.task)})
        os.rmdir(self.log)
        self.task.chmod(0o500)
        self.addCleanup(self.task.chmod, 0o700)
        if os.access(self.task, os.W_OK):
            self.skipTest("this account can write to a read-only directory")
        with self.assertRaises(calls.LogUnavailable):
            calls.call("t", {}, self.ok(), env={"TP_TASK_DIR": str(self.task)})

    def test_arguments_are_redacted_before_they_are_logged_and_hashed(self):
        env = {"TP_TASK_DIR": str(self.task)}
        calls.call("knowledge.add", {"text": f"key {AWS}"}, self.ok(), env=env)
        calls.call("knowledge.add", {"text": f"key {AWS.lower()}"}, self.ok(), env=env)
        raw = self.log.read_text()
        self.assertNotIn(AWS, raw)
        self.assertNotIn("AKIA", raw)
        self.assertEqual(self.lines()[0]["args"]["text"], redact.guard(f"key {AWS}"))

    def test_a_reason_is_redacted_too(self):
        env = {"TP_TASK_DIR": str(self.task)}
        calls.call("t", {}, lambda: calls.Answer("invalid", "x", reason=f"bad {AWS}"), env=env)
        self.assertNotIn(AWS, self.log.read_text())

    def test_a_long_argument_is_cut(self):
        calls.call("t", {"text": "x" * 5000}, self.ok(), env={"TP_TASK_DIR": str(self.task)})
        self.assertLessEqual(len(self.lines()[0]["args"]["text"]), calls.ARG_LIMIT + 1)

    def test_a_non_string_argument_is_logged_as_json(self):
        calls.call("t", {"paths": ["a", "b"]}, self.ok(), env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(self.lines()[0]["args"]["paths"], '["a", "b"]')

    def test_a_refusal_inside_a_call_is_logged_as_refused_with_the_rule_only(self):
        def run():
            raise redact.Refused("refused: the path is outside the project")
        answer = calls.call("t", {}, run, env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual((answer.status, self.lines()[0]["status"]), ("refused", "refused"))

    def test_a_bug_inside_a_call_is_logged_as_error_without_a_traceback_in_the_answer(self):
        def run():
            raise KeyError("boom")
        answer = calls.call("t", {}, run, env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(answer.status, "error")
        self.assertNotIn("Traceback", answer.text)
        self.assertIn("KeyError", self.lines()[0]["reason"])

    def test_a_call_that_returns_something_else_is_an_error(self):
        answer = calls.call("t", {}, lambda: "plain text", env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(answer.status, "error")

    def test_an_unknown_status_is_never_written(self):
        with self.assertRaises(ValueError):
            calls.record(self.log, "t", {}, calls.Answer("fine", "x"))
        self.assertFalse(self.log.exists())

    def test_an_explicit_log_file_is_for_an_operator_at_a_terminal_with_no_task_dir(self):
        other = self.tmp / "own.jsonl"
        with mock.patch.object(calls, "on_terminal", return_value=True):
            calls.call("t", {}, self.ok(), log_file=other, operator=True)
        self.assertTrue(other.is_file())
        self.assertFalse(self.log.exists())

    def test_an_explicit_log_file_is_refused_everywhere_else_and_the_call_is_never_run(self):
        other = self.tmp / "own.jsonl"
        cases = {
            "a query operation": dict(operator=False, env={}, tty=True),
            "no terminal": dict(operator=True, env={}, tty=False),
            "a task dir in the environment": dict(operator=True, env={"TP_TASK_DIR": str(self.task)}, tty=True),
            "a task id in the environment": dict(operator=True, env={"TP_TASK_ID": "coder-x"}, tty=True),
        }
        for label, case in cases.items():
            ran = []
            with mock.patch.object(calls, "on_terminal", return_value=case["tty"]):
                with self.assertRaises(calls.LogUnavailable, msg=label):
                    calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), log_file=other,
                               operator=case["operator"], env=case["env"])
            self.assertEqual(ran, [], label)
            self.assertFalse(other.exists(), label)
            self.assertFalse(self.log.exists(), label)

    def test_an_operator_command_may_run_unlogged_only_with_no_worker_variables(self):
        ran = []
        with mock.patch.object(calls, "on_terminal", return_value=True):  # a person at a terminal (CX4 N2: without one it is refused)
            answer = calls.call("t", {}, lambda: ran.append(1) or calls.Answer("ok", "x"), operator=True, env={})
            self.assertEqual((answer.status, ran), ("ok", [1]))
            self.assertFalse(self.log.exists())
            for env in ({"TP_TASK_ID": "coder-x"}, {"TP_TASK_DIR": str(self.tmp / "missing")}):
                with self.assertRaises(calls.LogUnavailable, msg=str(env)):
                    calls.call("t", {}, lambda: ran.append(2), operator=True, env=env)
        self.assertEqual(ran, [1])

    def test_a_query_operation_never_runs_unlogged(self):
        ran = []
        with self.assertRaises(calls.LogUnavailable):
            calls.call("t", {}, lambda: ran.append(1), operator=False, env={})
        self.assertEqual(ran, [])

    def test_an_operator_command_with_a_task_dir_is_logged_there(self):
        calls.call("t", {}, self.ok(), operator=True, env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(len(self.lines()), 1)

    def test_a_symlinked_ancestor_of_the_task_dir_is_refused_and_nothing_is_written_through_it(self):
        real = self.tmp / "real"
        (real / "task").mkdir(parents=True)
        (self.tmp / "alias").symlink_to(real)
        ran = []
        with self.assertRaisesRegex(calls.LogUnavailable, "symlink"):
            calls.call("t", {}, lambda: ran.append(1), env={"TP_TASK_DIR": str(self.tmp / "alias" / "task")})
        self.assertEqual((ran, list((real / "task").iterdir())), ([], []))

    def test_a_symlinked_part_of_a_session_task_path_is_refused(self):
        other = self.tmp / "other"
        (other / "tasks" / "t1").mkdir(parents=True)
        (self.tmp / "sessions").mkdir()
        (self.tmp / "sessions" / "sid").symlink_to(other)  # <sessions>/<sid> is a link
        with self.assertRaisesRegex(calls.LogUnavailable, "symlink"):
            calls.call("t", {}, self.ok(), env={"TP_TASK_DIR": str(self.tmp / "sessions" / "sid" / "tasks" / "t1")})
        self.assertEqual(list((other / "tasks" / "t1").iterdir()), [])
        state = self.tmp / "sessions" / "sid2"
        state.mkdir()
        (state / "tasks").symlink_to(other / "tasks")  # <sid>/tasks is a link
        with self.assertRaisesRegex(calls.LogUnavailable, "symlink"):
            calls.call("t", {}, self.ok(), env={"TP_TASK_DIR": str(state / "tasks" / "t1")})
        self.assertEqual(list((other / "tasks" / "t1").iterdir()), [])

    def test_a_real_session_task_path_is_logged(self):
        task = self.tmp / "sessions" / "sid" / "tasks" / "t1"
        task.mkdir(parents=True)
        calls.call("t", {}, self.ok(), env={"TP_TASK_DIR": str(task)})
        self.assertEqual(len((task / calls.LOG_NAME).read_text().splitlines()), 1)

    def test_a_link_that_appears_after_the_check_is_refused_at_write_time(self):
        real = self.tmp / "real"
        (real / "task").mkdir(parents=True)
        (self.tmp / "alias").symlink_to(real)
        with self.assertRaises(calls.LogUnavailable):
            calls.record(self.tmp / "alias" / "task" / calls.LOG_NAME, "t", {}, calls.Answer("ok", "x"),
                         task_dir=self.tmp / "alias" / "task")
        self.assertEqual(list((real / "task").iterdir()), [])

    def test_notes_returned_is_capped(self):
        calls.call("t", {}, self.ok(notes=[f"kn-{i}" for i in range(80)]), env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(len(self.lines()[0]["notes_returned"]), calls.NOTES_LIMIT)


if __name__ == "__main__":
    unittest.main()
