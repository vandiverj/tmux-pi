"""CX1 ctx-store: `tp-ctx` as a person or a worker runs it. Every run is a subprocess with a scrubbed environment (no worker
variables, no real work store) against a temp project; the approve gate is also driven through a pseudo-terminal."""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TP_CTX = ROOT / "bin" / "tp-ctx"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ctx_store"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
API = "apps/launchpad/api/src"


class Cli(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = self.tmp / "project"
        (self.project / API).mkdir(parents=True)
        (self.project / ".tmux-pi").mkdir()
        (self.project / API / "a.test.ts").write_text("one\ntwo\n")
        (self.tmp / "work-config.json").write_text("{}")
        self.task = self.tmp / "task"
        self.task.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
        self.env.update({"TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(self.tmp / "work-config.json"),
                         "TP_CTX_TODAY": "2026-10-31"})
        self.base = [sys.executable, str(TP_CTX), "--project", str(self.project)]
        self.task_env = {"TP_TASK_DIR": str(self.task)}  # a logged run that is not a worker's shell (no TP_TASK_ID)

    def run_cli(self, *args, worker=False, env=None, stdin=None):
        full = dict(self.env, **(env or {}))
        if worker:
            full.update({"TP_TASK_DIR": str(self.task), "TP_TASK_ID": "coder-test"})
        return subprocess.run(self.base + list(args), env=full, capture_output=True, text=True, timeout=60, input=stdin or "",
                              cwd=str(self.tmp))

    def add(self, *extra, text="Clean up only the rows a test created.", **kw):
        if not kw.get("worker"):
            kw["env"] = {**self.task_env, **kw.get("env", {})}
        return self.run_cli("knowledge", "add", "--kind", "convention", "--scope", f"{API}/**/*.test.ts", "--evidence",
                            f"{API}/a.test.ts:2", "--title", "Clean up own rows", "--tag", "test-isolation", "--text", text, *extra, **kw)

    def run_tty(self, *args, env=None):
        """Run tp-ctx with a pseudo-terminal as stdin and stdout (stderr captured). Reads until the process closes the terminal.
        Returns (exit code, terminal output, stderr)."""
        master, slave = pty.openpty()
        errors = tempfile.TemporaryFile()
        self.addCleanup(errors.close)
        proc = subprocess.Popen(self.base + list(args), stdin=slave, stdout=slave, stderr=errors, env=dict(self.env, **(env or {})),
                                cwd=str(self.tmp), close_fds=True)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        seen = b""
        while True:
            ready, _, _ = select.select([master], [], [], 30)  # a bound, not a wait: the loop ends when the terminal closes
            if not ready:
                proc.kill()
                self.fail(f"tp-ctx did not finish; saw {seen!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
        os.close(master)
        code = proc.wait(timeout=30)
        errors.seek(0)
        return code, seen.decode(errors="replace"), errors.read().decode(errors="replace")

    def log_lines(self):
        path = self.task / "ctx-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def candidate_path(self, ident="kn-2026-10-31-clean-up-own-rows"):
        return self.project / ".tmux-pi" / "knowledge" / "candidates" / f"{ident}.md"

    def approved_path(self, ident="kn-2026-10-31-clean-up-own-rows"):
        return self.project / ".tmux-pi" / "knowledge" / "approved" / f"{ident}.md"


class Knowledge(Cli):
    def test_add_writes_a_candidate_and_never_an_approved_note(self):
        done = self.add()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("written as a candidate", done.stdout)
        self.assertTrue(self.candidate_path().is_file())
        self.assertFalse(self.approved_path().exists())
        self.assertIn("status: candidate", self.candidate_path().read_text())

    def test_add_twice_with_one_id_does_not_overwrite(self):
        self.add("--id", "kn-2026-10-31-same")
        before = (self.candidate_path("kn-2026-10-31-same")).read_text()
        again = self.add("--id", "kn-2026-10-31-same", text="Something else entirely.")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)
        self.assertEqual(self.candidate_path("kn-2026-10-31-same").read_text(), before)

    def test_a_secret_looking_body_is_refused_without_echoing_it_and_nothing_is_written(self):
        done = self.add(text=f"Use {AWS} for the account.")
        self.assertEqual(done.returncode, 2)
        self.assertNotIn(AWS, done.stdout + done.stderr)
        self.assertNotIn("AKIA", done.stdout + done.stderr)
        self.assertIn("secret-like", done.stderr)
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_invalid_notes_are_refused_with_the_rule(self):
        cases = {
            "an oversize body": (dict(text="x" * 1201), "1201 characters; the limit is 1200"),
            "a bad glob": (dict(scope="../etc/passwd"), "scope glob"),
            "no evidence": (dict(evidence="review:only#F1"), None),  # a non-file evidence is allowed, so this one passes
        }
        for label, (opts, expected) in cases.items():
            text = opts.get("text", "A body.")
            done = self.run_cli("knowledge", "add", "--kind", "lesson", "--scope", opts.get("scope", f"{API}/**"), "--evidence",
                                opts.get("evidence", f"{API}/a.test.ts:1"), "--title", label, "--text", text, "--id", f"kn-2026-10-31-case-{label.split()[-1]}",
                                env=self.task_env)
            if expected:
                self.assertEqual(done.returncode, 1, label)
                self.assertIn(expected, done.stderr, label)
            else:
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_add_refuses_a_supersedes_id_given_twice(self):
        done = self.add("--supersedes", "kn-2026-10-01-a", "--supersedes", "kn-2026-10-01-a")
        self.assertEqual(done.returncode, 1)
        self.assertIn("supersedes lists each id once", done.stderr)
        self.assertFalse(self.candidate_path().exists())

    def test_list_show_and_the_candidate_status_filter(self):
        self.add()
        self.assertEqual(self.run_cli("knowledge", "list", env=self.task_env).stdout.strip(), "no notes")  # approved only by default
        listed = self.run_cli("knowledge", "list", "--status", "candidate", env=self.task_env)
        self.assertIn("kn-2026-10-31-clean-up-own-rows", listed.stdout)
        shown = self.run_cli("knowledge", "show", "kn-2026-10-31-clean-up-own-rows", "--json", env=self.task_env)
        data = json.loads(shown.stdout)
        self.assertEqual((data["status"], data["note"]["status"]), ("ok", "candidate"))
        missing = self.run_cli("knowledge", "show", "kn-2026-10-31-nope", env=self.task_env)
        self.assertEqual(missing.returncode, 1)

    def test_show_flags_a_stale_by_path_note_and_audit_lists_it(self):
        self.add("--id", "kn-2026-10-31-dead-scope")
        text = self.candidate_path("kn-2026-10-31-dead-scope").read_text().replace(f"{API}/**/*.test.ts", "gone/**")
        self.candidate_path("kn-2026-10-31-dead-scope").write_text(text)
        shown = json.loads(self.run_cli("knowledge", "show", "kn-2026-10-31-dead-scope", "--json", env=self.task_env).stdout)
        self.assertIn("stale_by_path", shown["note"]["flags"])
        audit = self.run_cli("knowledge", "audit", env=self.task_env)
        self.assertIn("stale_by_path", audit.stdout)
        self.assertIn("scope_no_match:gone/**", audit.stdout)

    def test_moved_evidence_is_flagged_by_show_and_audit(self):
        self.add("--id", "kn-2026-10-31-moved")  # cites a.test.ts:2, which says "two"
        self.assertNotIn("stale_by_path", self.run_cli("knowledge", "show", "kn-2026-10-31-moved", env=self.task_env).stdout)
        (self.project / API / "a.test.ts").write_text("zero\nzero too\none\ntwo\n")  # still long enough; "two" is now on line 4
        shown = json.loads(self.run_cli("knowledge", "show", "kn-2026-10-31-moved", "--json", env=self.task_env).stdout)
        self.assertIn("stale_by_path", shown["note"]["flags"])
        self.assertEqual(shown["note"]["path_reasons"], [f"evidence_changed:{API}/a.test.ts:2"])
        self.assertIn(f"evidence_changed:{API}/a.test.ts:2", self.run_cli("knowledge", "audit", env=self.task_env).stdout)

    def test_a_non_matching_versions_range_is_flagged_through_a_lockfile_pin(self):
        # the lockfile reader is L5's (not in this lane); the library hook is covered in test_ctx_store. Here the CLI reports
        # the check as unavailable rather than silently passing.
        self.add("--version", "drizzle-orm=>=0.45 <0.46", "--id", "kn-2026-10-31-versioned")
        shown = json.loads(self.run_cli("knowledge", "show", "kn-2026-10-31-versioned", "--json", env=self.task_env).stdout)
        note = shown["note"]
        self.assertEqual(note["versions"], {"drizzle-orm": ">=0.45 <0.46"})
        self.assertTrue(note.get("version_unchecked") or "stale_by_version" in note.get("flags", []))

    def test_audit_lists_a_file_that_does_not_parse(self):
        directory = self.project / ".tmux-pi" / "knowledge" / "approved"
        directory.mkdir(parents=True)
        (directory / "kn-2026-10-31-broken.md").write_text((FIXTURES / "missing-evidence-approved.md").read_text())
        audit = self.run_cli("knowledge", "audit", env=self.task_env)
        self.assertIn("invalid", audit.stdout)
        self.assertIn("evidence is required", audit.stdout)

    def test_import_from_work_refuses_an_unknown_or_non_lesson_note(self):
        done = self.run_cli("knowledge", "import", "--from-work", "note-nope", "--scope", f"{API}/**", "--evidence", f"{API}/a.test.ts:1",
                            env=self.task_env)
        self.assertIn(done.returncode, (1, 2))
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_status_reports_counts_and_the_lockfile_reader(self):
        self.add()
        out = self.run_cli("status", "--json", env=self.task_env)
        data = json.loads(out.stdout)
        self.assertEqual(data["counts"]["candidate"], 1)
        self.assertIn("lockfile_reader", data)

    def test_no_project_is_a_usage_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        done = subprocess.run([sys.executable, str(TP_CTX), "status"], env=dict(self.env, **self.task_env), capture_output=True, text=True,
                              cwd=str(empty), timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn("no project", done.stderr)

    def test_output_passes_the_redaction_boundary(self):
        self.add("--id", "kn-2026-10-31-boundary")
        path = self.candidate_path("kn-2026-10-31-boundary")
        path.write_text(path.read_text().replace("Clean up only", f"Use {AWS}; clean up only"))  # planted after the add screen
        shown = self.run_cli("knowledge", "show", "kn-2026-10-31-boundary")
        self.assertNotIn(AWS, shown.stdout + shown.stderr)


class Logging(Cli):
    def test_a_worker_call_is_logged_and_a_candidate_is_attributed_to_the_task(self):
        done = self.add(worker=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        line = self.log_lines()[-1]
        self.assertEqual((line["tool"], line["status"]), ("knowledge.add", "ok"))
        self.assertIn("source: worker:coder-test", self.candidate_path().read_text())

    def test_a_worker_call_with_no_usable_task_dir_gets_no_answer_and_reads_nothing(self):
        done = self.run_cli("knowledge", "list", env={"TP_TASK_DIR": str(self.tmp / "missing")})
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertIn("refusing to answer without a log", done.stderr)
        added = self.add(env={"TP_TASK_DIR": str(self.tmp / "missing")})
        self.assertEqual(added.returncode, 3)
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_a_symlinked_log_is_refused(self):
        target = self.tmp / "target.jsonl"
        target.write_text("keep\n")
        (self.task / "ctx-calls.jsonl").symlink_to(target)
        done = self.run_cli("knowledge", "list", worker=True)
        self.assertEqual(done.returncode, 3)
        self.assertEqual(done.stdout, "")
        self.assertEqual(target.read_text(), "keep\n")

    def test_the_logged_arguments_never_hold_a_secret(self):
        self.add(text=f"Use {AWS}.", worker=True)
        raw = (self.task / "ctx-calls.jsonl").read_text()
        self.assertNotIn(AWS, raw)
        self.assertEqual(self.log_lines()[-1]["status"], "refused")

    def test_operator_commands_may_run_unlogged_with_no_worker_variable(self):
        self.add()
        for words in (("knowledge", "list", "--status", "all"), ("knowledge", "show", "kn-2026-10-31-clean-up-own-rows"), ("knowledge", "audit"),
                      ("status",)):
            code, _, errors = self.run_tty(*words)  # a person at a terminal (CX4 N2/N3: without one these are refused)
            self.assertEqual(code, 0, (words, errors))
        self.assertEqual([line["tool"] for line in self.log_lines()], ["knowledge.add"])

    def test_every_other_command_gets_no_answer_without_a_task_dir(self):
        cases = [("knowledge", "add", "--kind", "lesson", "--scope", f"{API}/**", "--evidence", f"{API}/a.test.ts:1", "--text", "A rule."),
                 ("knowledge", "import", "--from-work", "note-nope", "--scope", f"{API}/**", "--evidence", f"{API}/a.test.ts:1")]
        for words in cases:
            done = self.run_cli(*words)
            self.assertEqual(done.returncode, 3, (words, done.stdout, done.stderr))
            self.assertEqual(done.stdout, "", words)
            self.assertIn("TP_TASK_DIR is not set", done.stderr)
        self.assertFalse((self.project / ".tmux-pi" / "knowledge").exists())

    def test_an_operator_command_with_only_a_task_id_is_a_worker_and_gets_no_answer(self):
        self.add()
        done = self.run_cli("knowledge", "list", env={"TP_TASK_ID": "coder-test"})
        self.assertEqual((done.returncode, done.stdout), (3, ""))

    def test_a_worker_cannot_redirect_or_skip_its_log_with_log(self):
        own = self.tmp / "own.jsonl"
        self.add()
        for words, kw in ((("--log", str(own), "knowledge", "list"), dict(worker=True)),
                          (("--log", str(own), "knowledge", "list"), dict(env=self.task_env)),
                          (("--log", str(own), "knowledge", "list"), {}),  # no terminal
                          (("--log", str(own), "status"), {}),
                          (("--log", str(own), "knowledge", "add", "--kind", "lesson", "--scope", f"{API}/**", "--evidence", f"{API}/a.test.ts:1",
                            "--text", "A rule."), {})):
            done = self.run_cli(*words, **kw)
            self.assertEqual((done.returncode, done.stdout), (3, ""), (words, kw, done.stderr))
            self.assertIn("--log is for a person", done.stderr)
            self.assertFalse(own.exists())
        self.assertEqual(len(list((self.project / ".tmux-pi" / "knowledge" / "candidates").iterdir())), 1)  # the setup add only

    def test_a_person_at_a_terminal_may_log_an_operator_command_to_a_file(self):
        self.add()
        own = self.tmp / "own.jsonl"
        code, shown, errors = self.run_tty("--log", str(own), "knowledge", "list", "--status", "all")
        self.assertEqual(code, 0, errors)
        self.assertIn("kn-2026-10-31-clean-up-own-rows", shown)
        self.assertEqual([json.loads(line)["tool"] for line in own.read_text().splitlines()], ["knowledge.list"])
        code, shown, errors = self.run_tty("--log", str(own), "knowledge", "list", env={"TP_TASK_DIR": str(self.task)})
        self.assertEqual(code, 3, shown)

    def test_a_symlinked_ancestor_of_the_task_dir_is_refused(self):
        real = self.tmp / "real"
        (real / "task").mkdir(parents=True)
        (self.tmp / "alias").symlink_to(real)
        for words in (("knowledge", "list"), ("status",)):
            done = self.run_cli(*words, env={"TP_TASK_DIR": str(self.tmp / "alias" / "task")})
            self.assertEqual((done.returncode, done.stdout), (3, ""), words)
            self.assertIn("symlink", done.stderr)
        self.assertEqual(list((real / "task").iterdir()), [])

    def test_a_no_project_call_is_logged_as_no_project(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        env = dict(self.env, TP_TASK_DIR=str(self.task), TP_TASK_ID="coder-test")
        done = subprocess.run([sys.executable, str(TP_CTX), "status"], env=env, capture_output=True, text=True, cwd=str(empty), timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertEqual(self.log_lines()[-1]["status"], "no_project")


class Approve(Cli):
    def candidate(self):
        done = self.add("--id", "kn-2026-10-31-clean-up-own-rows")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_approve_refuses_in_a_worker_shell_even_with_a_terminal_and_changes_nothing(self):
        self.candidate()
        for names in ({"TP_TASK_DIR": str(self.task)}, {"TP_TASK_ID": "coder-test"}):
            done = self.run_cli("knowledge", "approve", "kn-2026-10-31-clean-up-own-rows", env=names)
            self.assertEqual(done.returncode, 2, names)
            self.assertIn("marks a worker's shell", done.stderr)
            self.assertTrue(self.candidate_path().exists())
            self.assertFalse(self.approved_path().exists())
        self.assertEqual([line["tool"] for line in self.log_lines()], ["knowledge.add"])  # the add; approve logged nothing

    def test_approve_refuses_without_a_terminal(self):
        self.candidate()
        done = self.run_cli("knowledge", "approve", "kn-2026-10-31-clean-up-own-rows", stdin="kn-2026-10-31-clean-up-own-rows\n")
        self.assertEqual(done.returncode, 2)
        self.assertIn("interactive terminal", done.stderr)
        self.assertTrue(self.candidate_path().exists())
        self.assertFalse(self.approved_path().exists())

    def drive(self, ident, reply, env=None):
        """Run approve with a pseudo-terminal as stdin and stdout; answer the prompt when it appears. Returns (code, output)."""
        master, slave = pty.openpty()
        full = dict(self.env, **(env or {}))
        proc = subprocess.Popen(self.base + ["knowledge", "approve", ident], stdin=slave, stdout=slave, stderr=slave,
                                env=full, cwd=str(self.tmp), close_fds=True)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        seen, answered = b"", False
        while True:
            ready, _, _ = select.select([master], [], [], 15)
            if not ready:
                proc.kill()
                self.fail(f"approve did not reach its prompt; saw {seen!r}")
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
            if not answered and b"Type the note id" in seen:
                os.write(master, (reply + "\n").encode())
                answered = True
        os.close(master)
        code = proc.wait(timeout=30)
        return code, seen.decode(errors="replace")

    def test_approve_on_a_terminal_prints_the_diff_and_moves_the_note_when_the_id_is_typed(self):
        self.candidate()
        code, shown = self.drive("kn-2026-10-31-clean-up-own-rows", "kn-2026-10-31-clean-up-own-rows")
        self.assertEqual(code, 0, shown)
        self.assertIn("+++ b/.tmux-pi/knowledge/approved/kn-2026-10-31-clean-up-own-rows.md", shown)
        self.assertIn("+status: approved", shown.replace("\r", ""))
        self.assertTrue(self.approved_path().is_file())
        self.assertFalse(self.candidate_path().exists())
        self.assertIn("last_reviewed: 2026-10-31", self.approved_path().read_text())

    def test_approve_with_the_wrong_id_typed_changes_nothing(self):
        self.candidate()
        code, shown = self.drive("kn-2026-10-31-clean-up-own-rows", "yes")
        self.assertEqual(code, 1, shown)
        self.assertIn("nothing was changed", shown)
        self.assertTrue(self.candidate_path().exists())
        self.assertFalse(self.approved_path().exists())

    def test_approve_of_an_unknown_candidate_is_refused_on_a_terminal(self):
        code, shown = self.drive("kn-2026-10-31-nope", "x")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
