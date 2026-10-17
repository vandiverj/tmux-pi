"""`tp-agent work promote` (Phase B): a private note or workspace becomes a DRAFT context file in a working tree, and nothing else.
Only temp stores and temp git repositories are used; the real engineering-context clone is never named or touched."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402
import work_find  # noqa: E402
import work_promote  # noqa: E402

AWS = "AKIA" + "IOSFODNN7EXAMPLE"
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}


class PromoteBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.tree = self.tmp / "context-clone"
        self.tree.mkdir()
        self.repo = self.tmp / "product-repo"
        self.repo.mkdir()
        self.config = self.tmp / "work-config.json"
        self.config.write_text("{}")
        patch = mock.patch.dict(os.environ, {"TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(self.config)})
        patch.start()
        self.addCleanup(patch.stop)

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", str(cwd or self.tree), *args], check=True, text=True, capture_output=True, env=GIT_ENV).stdout.strip()

    def init_git(self):
        self.git("init", "-q")
        (self.tree / "README.md").write_text("seed\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "seed")

    def configure(self, **projects):
        self.config.write_text(json.dumps({"projects": {str(self.repo): {"target": str(self.tree), "promote_dir": "context/drafts"}}, **projects}))

    def note(self, title="Loader batch limits", body="The batch endpoint rejects more than 500 rows.\n", **meta):
        meta.setdefault("workspaces", [])
        return work.create("note", title, meta=meta, body=body)

    def promote(self, ident, **kw):
        return work_promote.promote(ident, **kw)


class PromoteTests(PromoteBase):
    def test_a_note_becomes_a_draft_file_under_the_configured_directory(self):
        ws = work.create("workspace", "Loader", meta={"repos": [str(self.repo)]})
        note = self.note(workspaces=[ws.id], tickets=["DMP-77"])
        self.configure()
        out = self.promote(note.id)
        path = self.tree / "context" / "drafts" / "loader-batch-limits.context.md"
        text = path.read_text()
        self.assertTrue(text.startswith("---\ncontext_id: loader.batch.limits\nstatus: draft\nconfidence: low\n"))
        self.assertIn("repos: [product-repo]", text)
        self.assertIn("# Context: Loader batch limits", text)
        self.assertIn("### Believed\nThe batch endpoint rejects more than 500 rows.", text)  # not claimed as verified
        self.assertIn("wrote target:context/drafts/loader-batch-limits.context.md", out)
        self.assertNotIn(str(self.tmp), out)  # the configured tree is not printed
        self.assertIn("+++ b/context/drafts/loader-batch-limits.context.md", out)
        self.assertIn("review the diff, then commit it yourself", out)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")
        self.assertIn(f"promoted_to: {path}@", work.load("note", note.id).path.read_text())

    def test_confidence_is_recorded_and_checked(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree), confidence="high")
        self.assertIn("confidence: high", (self.tree / "loader-batch-limits.context.md").read_text())
        with self.assertRaises(work.WorkError):
            self.promote(note.id, target=str(self.tree), confidence="certain")

    def test_a_ticket_puts_the_file_under_tasks_and_a_bad_ticket_is_refused(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree), ticket="DMP-77")
        self.assertTrue((self.tree / "tasks" / "DMP-77" / "loader-batch-limits.context.md").is_file())
        for bad in ("../x", "dmp-77", "DMP-77/../../y", "DMP-"):
            with self.assertRaises(work.WorkError, msg=bad):
                self.promote(note.id, target=str(self.tree), ticket=bad)
        self.assertEqual(sorted(p.name for p in self.tree.iterdir()), ["tasks"])
        # as a task: the note's own ticket is used when none is given, and one is required otherwise
        other = self.note("Other note", tickets=["CM-9"])
        self.promote(other.id, kind_as="task", target=str(self.tree))
        self.assertTrue((self.tree / "tasks" / "CM-9" / "other-note.context.md").is_file())
        with self.assertRaises(work.WorkError):
            self.promote(self.note("No ticket here").id, kind_as="task", target=str(self.tree))

    def test_no_target_configured_says_how_to_set_one_and_writes_nothing(self):
        note = self.note()
        with self.assertRaises(work.WorkError) as raised:
            self.promote(note.id)
        self.assertIn("--target", str(raised.exception))
        self.assertIn("work.local.json", str(raised.exception))
        self.assertEqual(list(self.tree.iterdir()), [])
        with self.assertRaises(work.WorkError):
            self.promote(note.id, target=str(self.tmp / "missing-dir"))

    def test_a_promote_dir_that_climbs_out_of_the_tree_is_refused(self):
        note = self.note()
        self.config.write_text(json.dumps({"shared": {"path": str(self.tree), "promote_dir": "../outside"}}))
        with self.assertRaises(work.WorkError):
            self.promote(note.id)
        self.assertFalse((self.tmp / "outside").exists())

    def test_shared_path_and_promote_dir_are_the_fallback_when_no_project_matches(self):
        note = self.note()
        self.config.write_text(json.dumps({"shared": {"path": str(self.tree), "promote_dir": "drafts"}}))
        self.promote(note.id)
        self.assertTrue((self.tree / "drafts" / "loader-batch-limits.context.md").is_file())

    def test_git_is_never_written_to_only_one_untracked_file_appears(self):
        self.init_git()
        before = (self.git("rev-parse", "HEAD"), self.git("for-each-ref"), (self.tree / ".git" / "index").read_bytes(), self.git("branch", "--list"))
        note = self.note()
        self.promote(note.id, target=str(self.tree), ticket="DMP-77")
        self.assertEqual((self.git("rev-parse", "HEAD"), self.git("for-each-ref"), (self.tree / ".git" / "index").read_bytes(), self.git("branch", "--list")), before)
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), "?? tasks/DMP-77/loader-batch-limits.context.md")
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(self.git("stash", "list"), "")

    def test_promote_runs_no_git_command_at_all(self):
        self.init_git()
        note = self.note()
        with mock.patch("subprocess.run", side_effect=AssertionError("git was run")), mock.patch("subprocess.Popen", side_effect=AssertionError("git was run")), \
                mock.patch("subprocess.check_output", side_effect=AssertionError("git was run")):
            self.promote(note.id, target=str(self.tree))

    def test_an_existing_draft_needs_update_and_update_shows_the_diff_and_writes_a_draft_again(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        with self.assertRaises(work.WorkError) as raised:
            self.promote(note.id, target=str(self.tree))
        self.assertIn("--update", str(raised.exception))
        path.write_text(path.read_text().replace("status: draft", "status: verified").replace("confidence: low", "confidence: high"))
        edited = work.load("note", note.id)
        edited.body = "Now it rejects more than 1000 rows.\n"
        edited.save()
        out = self.promote(note.id, target=str(self.tree), update=True)
        self.assertIn("-The batch endpoint rejects more than 500 rows.", out)
        self.assertIn("+Now it rejects more than 1000 rows.", out)
        text = path.read_text()
        self.assertIn("status: draft", text)  # changed content is never published as verified
        self.assertNotIn("verified", text)
        self.assertIn("confidence: low", text)
        self.assertIn("Now it rejects more than 1000 rows.", text)

    def test_update_does_not_copy_an_edited_status_or_confidence_field_with_a_secret(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        for field in ("status", "confidence"):
            original = path.read_text()
            path.write_text(original.replace(f"{field}: ", f"{field}: {AWS} ", 1))
            edited = work.load("note", note.id)
            edited.body = f"changed for {field}\n"
            edited.save()
            out = self.promote(note.id, target=str(self.tree), update=True)
            self.assertNotIn(AWS, out)  # the diff's removed line is redacted
            self.assertNotIn(AWS, path.read_text())  # and the field is not carried into the new file
            self.assertIn("status: draft", path.read_text())

    def test_update_refuses_when_the_final_text_holds_a_secret_and_leaves_the_file_alone(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        before = path.read_text()
        edited = work.load("note", note.id)
        edited.body = f"key {AWS}\n"
        edited.save()
        with self.assertRaises(work.WorkError) as raised:
            self.promote(note.id, target=str(self.tree), update=True)
        self.assertNotIn(AWS, str(raised.exception))
        self.assertEqual(path.read_text(), before)

    def test_update_diff_does_not_print_a_secret_from_a_removed_line_of_the_old_file(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        path.write_text(path.read_text() + f"reviewer scratch: {AWS}\n")
        out = self.promote(note.id, target=str(self.tree), update=True)
        self.assertNotIn(AWS, out)
        self.assertNotIn(AWS, path.read_text())

    def test_update_with_nothing_new_changes_nothing(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        before = (path.read_text(), path.stat().st_mtime_ns)
        self.assertIn("no change", self.promote(note.id, target=str(self.tree), update=True))
        self.assertEqual((path.read_text(), path.stat().st_mtime_ns), before)

    def test_update_on_an_identical_draft_still_records_promoted_to_in_the_note(self):
        note = self.note()
        path = self.tree / "loader-batch-limits.context.md"
        path.write_text(work_promote.build_text(work.load("note", note.id)))  # the draft is already there, put there by hand
        self.assertNotIn("promoted_to", work.load("note", note.id).meta)
        before = (path.read_text(), path.stat().st_mtime_ns)
        self.assertIn("no change", self.promote(note.id, target=str(self.tree), update=True))
        self.assertEqual((path.read_text(), path.stat().st_mtime_ns), before)
        self.assertTrue(work.load("note", note.id).meta["promoted_to"].startswith(f"{path}@"))
        marked = work.load("note", note.id).path.read_text()
        self.assertIn("no change", self.promote(note.id, target=str(self.tree), update=True))  # idempotent
        self.assertEqual(work.load("note", note.id).path.read_text(), marked)

    def test_update_with_a_new_confidence_and_the_same_body_writes_it(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree), confidence="low")
        path = self.tree / "loader-batch-limits.context.md"
        out = self.promote(note.id, target=str(self.tree), confidence="high", update=True)
        self.assertNotIn("no change", out)
        self.assertIn("-confidence: low", out)
        self.assertIn("+confidence: high", out)
        self.assertIn("confidence: high", path.read_text())
        self.assertNotIn("confidence: low", path.read_text())
        # the same confidence again is a re-run on the same content
        self.assertIn("no change", self.promote(note.id, target=str(self.tree), confidence="high", update=True))

    def test_update_without_a_confidence_keeps_a_reviewed_confidence_and_status_on_unchanged_content(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        path = self.tree / "loader-batch-limits.context.md"
        reviewed = path.read_text().replace("status: draft", "status: verified").replace("confidence: low", "confidence: high")
        path.write_text(reviewed)
        self.assertIn("no change", self.promote(note.id, target=str(self.tree), update=True))
        self.assertEqual(path.read_text(), reviewed)

    def test_a_body_line_that_looks_like_review_state_is_still_compared(self):
        note = self.note(body="status: ok today\n")
        self.promote(note.id, target=str(self.tree))
        edited = work.load("note", note.id)
        edited.body = "status: broken today\n"
        edited.save()
        out = self.promote(note.id, target=str(self.tree), update=True)
        self.assertNotIn("no change", out)
        self.assertIn("status: broken today", (self.tree / "loader-batch-limits.context.md").read_text())

    def test_a_secret_in_the_note_refuses_the_promotion_naming_the_kind_and_never_the_value(self):
        note = self.note(body=f"remember the key {AWS} for later\n")
        with self.assertRaises(work.WorkError) as raised:
            self.promote(note.id, target=str(self.tree))
        message = str(raised.exception)
        self.assertIn("aws_access_key", message)
        self.assertNotIn(AWS, message)
        self.assertEqual(list(self.tree.iterdir()), [])
        self.assertNotIn("promoted_to", work.load("note", note.id).path.read_text())

    def test_a_secret_in_the_title_is_refused_too(self):
        note = self.note(title=f"Key {AWS}")
        with self.assertRaises(work.WorkError):
            self.promote(note.id, target=str(self.tree))
        self.assertEqual(list(self.tree.iterdir()), [])

    def test_private_tag_or_marker_refuses(self):
        tagged = self.note("Tagged", tags=["private"])
        with self.assertRaises(work.WorkError) as raised:
            self.promote(tagged.id, target=str(self.tree))
        self.assertIn("private", str(raised.exception))
        marked = self.note("Marked", body="public line\n<!-- private -->\nhidden line\n")
        with self.assertRaises(work.WorkError) as raised:
            self.promote(marked.id, target=str(self.tree))
        self.assertIn("private section", str(raised.exception))
        self.assertEqual(list(self.tree.iterdir()), [])

    def test_a_workspace_is_promoted_without_its_in_flight_state(self):
        ws = work.create("workspace", "Loader", meta={"repos": [str(self.repo)]}, body="## Objective\nRewrite the loader.\n\n## Scope\nOnly the batch path.\n")
        doc = work.load("workspace", ws.id)
        doc.body = work.append_line(doc.body, "Next actions", "- [ ] NEXT-ACTION-TEXT")
        doc.body = work.append_line(doc.body, "Sessions", "- run r1, tasks: SESSION-REF-TEXT")
        doc.body = work.append_line(doc.body, "Decisions", "- 2026-10-01 Use the batch API.")
        doc.save()
        d = work.load("workspace", ws.id)
        entries = work.decisions_of(d)
        entries.append({"id": f"{d.id}/D1", "priority": "high", "status": "open", "blocking": True, "question": "OPEN-QUESTION-TEXT", "background": "",
                        "options": "", "recommended": "", "affects": ["workspace"], "resolution": "", "by": "", "at": "", "quote": "", "withdrawn": "", "_dirty": True})
        work.put_decisions(d, entries)
        d.save()
        self.configure()
        self.promote(ws.id)
        text = (self.tree / "context" / "drafts" / "loader.context.md").read_text()
        for kept in ("Rewrite the loader.", "Only the batch path.", "Use the batch API."):
            self.assertIn(kept, text)
        for left_out in ("NEXT-ACTION-TEXT", "SESSION-REF-TEXT", "OPEN-QUESTION-TEXT", "Open decisions"):
            self.assertNotIn(left_out, text)

    def test_unknown_and_archived_documents_are_refused(self):
        with self.assertRaises(work.WorkError):
            self.promote("nope", target=str(self.tree))
        note = self.note()
        work.move_to_archive(work.load("note", note.id), "archived")
        with self.assertRaises(work.WorkError):
            self.promote(note.id, target=str(self.tree))

    def test_a_symlinked_destination_directory_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.tree / "tasks").symlink_to(outside, target_is_directory=True)
        note = self.note()
        with self.assertRaises(work.WorkError):
            self.promote(note.id, target=str(self.tree), ticket="DMP-77")
        self.assertEqual(list(outside.iterdir()), [])

    def test_find_marks_a_promoted_note(self):
        note = self.note()
        self.promote(note.id, target=str(self.tree))
        rows, total = work_find.search(["batch"], kind="note")
        self.assertIn("[promoted]", work_find.format_rows(rows, total))


class PromoteCli(PromoteBase):
    def run_cli(self, *args):
        env = {**os.environ, "TP_STATE_DIR": str(self.tmp / "state")}
        env.pop("TMUX", None)
        return subprocess.run([str(ROOT / "bin/tp-agent"), "work", *args], env=env, text=True, capture_output=True, timeout=30)

    def test_cli_promote_prints_the_diff_and_the_path(self):
        self.assertEqual(self.run_cli("note", "add", "CLI note", "--body", "cli body").returncode, 0)
        run = self.run_cli("promote", "cli-note", "--target", str(self.tree), "--confidence", "medium")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("+confidence: medium", run.stdout)
        self.assertIn("wrote target:cli-note.context.md", run.stdout)
        again = self.run_cli("promote", "cli-note", "--target", str(self.tree))
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("--update", again.stderr)
        self.assertNotIn("Traceback", again.stderr)

    def test_cli_update_with_a_new_confidence_and_an_unchanged_note_writes_it(self):
        self.assertEqual(self.run_cli("note", "add", "Conf note", "--body", "same body").returncode, 0)
        first = self.run_cli("promote", "conf-note", "--target", str(self.tree))
        self.assertEqual(first.returncode, 0, first.stderr)
        path = self.tree / "conf-note.context.md"
        self.assertIn("confidence: low", path.read_text())
        run = self.run_cli("promote", "conf-note", "--target", str(self.tree), "--confidence", "high", "--update")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("no change", run.stdout)
        self.assertIn("-confidence: low", run.stdout)
        self.assertIn("+confidence: high", run.stdout)
        self.assertIn("confidence: high", path.read_text())
        plain = self.run_cli("promote", "conf-note", "--target", str(self.tree), "--update")
        self.assertIn("no change", plain.stdout)  # no --confidence: the file's value stands
        self.assertIn("confidence: high", path.read_text())

    def test_cli_refusal_for_a_secret_has_no_traceback_and_no_value(self):
        self.assertEqual(self.run_cli("note", "add", "Leaky", "--body", f"key {AWS}").returncode, 0)
        run = self.run_cli("promote", "leaky", "--target", str(self.tree))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("aws_access_key", run.stderr)
        self.assertNotIn(AWS, run.stdout + run.stderr)
        self.assertEqual(list(self.tree.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
