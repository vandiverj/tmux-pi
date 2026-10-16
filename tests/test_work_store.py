"""Durable work store (Phase A): ids, frontmatter, atomic writes, lock, archive, permissions, isolation, trust guard."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402
import trust_guard  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "work"
        patch = mock.patch.dict(os.environ, {"TP_WORK_DIR": str(self.root)})
        patch.start()
        self.addCleanup(patch.stop)


class StoreTests(Store):
    def test_create_and_duplicate_ids(self):
        a = work.create("workspace", "CI flakiness investigation")
        self.assertEqual(a.id, "ci-flakiness-investigation")
        b = work.create("workspace", "CI flakiness investigation")
        self.assertNotEqual(a.id, b.id)
        self.assertTrue(b.id.startswith("ci-flakiness-investigation"))
        with self.assertRaises(work.WorkError):
            work.create("workspace", "x", wanted=a.id)
        with self.assertRaises(work.WorkError):
            work.create("note", "x", wanted=a.id)  # ids are unique across kinds
        with self.assertRaises(work.WorkError):
            work.create("workspace", "x", wanted="../evil")

    def test_archived_ids_are_never_reused(self):
        a = work.create("workspace", "Same title")
        work.move_to_archive(a, "archived")
        self.assertNotEqual(work.create("workspace", "Same title").id, a.id)

    def test_frontmatter_round_trip(self):
        meta = {"id": "t", "title": 'Odd: "title", with # marks', "status": "active", "updated": "2026-10-16T14:02:00Z",
                "tickets": ["DMP-76", "CM-67"], "repos": ["/a b/c"], "tags": [], "important": True}
        text = work.render(meta, "## Objective\nGo\n")
        parsed, body = work.parse(text)
        self.assertEqual(parsed["title"], meta["title"])
        self.assertEqual(parsed["tickets"], meta["tickets"])
        self.assertEqual(parsed["repos"], ["/a b/c"])
        self.assertIs(parsed["important"], True)
        self.assertEqual(body, "## Objective\nGo\n")

    def test_tolerant_parse_of_hand_edits(self):
        work.ensure_store()
        path = self.root / "workspaces" / "hand.md"
        path.write_text("---\ntitle: Hand made   # note\nstatus: paused\ntickets: [A-1,  B-2 ]\nweird line\n---\nfree text\n## Mystery\nkeep me\n")
        doc = work.load("workspace", "hand")
        self.assertEqual(doc.meta["id"], "hand")
        self.assertEqual(doc.meta["title"], "Hand made")
        self.assertEqual(doc.meta["tickets"], ["A-1", "B-2"])
        doc.body = work.append_line(doc.body, "Next actions", "- [ ] x")
        doc.save()
        text = path.read_text()
        self.assertIn("free text", text)
        self.assertIn("## Mystery\nkeep me", text)
        # no frontmatter at all, and an unterminated one: listed, not an error
        (self.root / "workspaces" / "bare.md").write_text("just text\n")
        (self.root / "workspaces" / "open.md").write_text("---\ntitle: x\nno end\n")
        self.assertEqual({d.id for d in work.iter_docs("workspace")}, {"hand", "bare", "open"})

    def test_atomic_write_failure_keeps_old_file(self):
        doc = work.create("workspace", "Keep")
        before = doc.path.read_text()
        with mock.patch("work.os.replace", side_effect=OSError("disk")):
            doc.body = "changed\n"
            with self.assertRaises(OSError):
                doc.save()
        self.assertEqual(doc.path.read_text(), before)
        self.assertEqual([p.name for p in doc.path.parent.iterdir()], ["keep.md"])  # no temp file left

    def test_symlinked_target_is_refused(self):
        doc = work.create("workspace", "Link")
        outside = self.root.parent / "outside.md"
        outside.write_text("x")
        doc.path.unlink()
        doc.path.symlink_to(outside)
        with self.assertRaises(work.WorkError):
            work.atomic_write(doc.path, "y")
        self.assertEqual(outside.read_text(), "x")

    def test_lock_contention(self):
        with work.locked():
            code = ("import sys; sys.path.insert(0, %r); import work\n"
                    "try:\n    work.locked(timeout=0.3).__enter__()\nexcept work.WorkError as e:\n    print('refused', e)\n" % str(ROOT / "lib"))
            run = subprocess.run([sys.executable, "-c", code], env=os.environ, text=True, capture_output=True)
            self.assertIn("refused", run.stdout, run.stderr)
        start = time.monotonic()
        with work.locked(timeout=0.3):  # free again once released
            pass
        self.assertLess(time.monotonic() - start, 2)

    def test_archive_and_supersede_keep_history(self):
        n = work.create("note", "Old fact", body="body\n")
        w = work.create("workspace", "Work")
        work.move_to_archive(w, "archived")
        self.assertFalse((self.root / "workspaces" / f"{w.id}.md").exists())
        self.assertEqual(work.load("workspace", w.id).meta["status"], "archived")
        with self.assertRaises(work.WorkError):
            work.load("workspace", w.id, include_archived=False)
        with self.assertRaises(work.WorkError):
            work.move_to_archive(work.load("workspace", w.id), "archived")
        work.move_to_archive(n, "superseded")
        self.assertTrue((self.root / "archive" / "notes" / "old-fact.md").is_file())

    def _fail_rename(self, name):
        real = os.rename
        def boom(src, dst, *a, **k):
            if os.path.basename(str(src)) == name:
                raise OSError("simulated")
            return real(src, dst, *a, **k)
        return mock.patch.object(work.os, "rename", boom)

    def _archive_args(self, ident):
        import argparse
        return argparse.Namespace(work_command="archive", id=ident, kind=None)

    def test_interrupted_archive_is_archived_everywhere_and_retry_finishes_the_rename(self):
        import argparse
        import work_commands
        doc = work.create("workspace", "Crashy")
        with self._fail_rename("crashy.md"):
            with self.assertRaises(OSError):
                work.move_to_archive(work.load("workspace", doc.id, include_archived=False), "archived")
        self.assertTrue((self.root / "workspaces" / "crashy.md").exists())  # the marked file has not moved yet
        self.assertEqual([d.id for d in work.iter_docs("workspace")], [])
        self.assertEqual([d.id for d in work.iter_docs("workspace", archived=True)], ["crashy"])
        # Every mutator refuses it, so nothing can be written to the leftover.
        before = (self.root / "workspaces" / "crashy.md").read_text()
        cases = [dict(work_command="set", id="crashy", title="x", status=None, objective=None, scope=None, repo=[], ticket=[], tag=[]),
                 dict(work_command="next", id="crashy", text="a", done=None),
                 dict(work_command="decide", id="crashy", text="a", source=""),
                 dict(work_command="block", id="crashy", text="a", clear=None),
                 dict(work_command="link", id="crashy", note=[], artifact=["p"], task=[], goal=[]),
                 dict(work_command="attach", id="crashy"),
                 dict(work_command="decision", decision_command="add", workspace="crashy", question="Approve?", background="",
                      options="", recommended="", affects=[], priority="normal", blocking=True),
                 dict(work_command="decision", decision_command="answer", id="crashy/D1", text="y", by="user", quote=""),
                 dict(work_command="note", note_command="add", title="n", id=None, body="", body_file=None, workspace=["crashy"],
                      ticket=[], tag=[], source=[], important=False)]
        for case in cases:
            with self.assertRaises(work.WorkError, msg=case["work_command"]):
                work_commands.run(argparse.Namespace(**case), session_dir=str(self.root))
        self.assertEqual((self.root / "workspaces" / "crashy.md").read_text(), before)
        self.assertEqual(work.open_decisions(True), [])
        work_commands.run(self._archive_args("crashy"))  # retry: only the rename remains
        self.assertFalse((self.root / "workspaces" / "crashy.md").exists())
        self.assertTrue((self.root / "archive" / "workspaces" / "crashy.md").is_file())
        self.assertEqual(work.load("workspace", "crashy").meta["status"], "archived")

    def test_supersede_interrupted_before_replacement_reserves_nothing(self):
        import argparse
        import work_commands
        old = work.create("note", "Old fact", body="old body")
        args = argparse.Namespace(work_command="supersede", id=old.id, title="New fact", body="intended", body_file=None, new_id=None)
        real = work.create
        def fail(kind, *a, **k):
            raise OSError("simulated")
        with mock.patch.object(work, "create", fail):
            with self.assertRaises(OSError):
                work_commands.run(args)
        self.assertNotIn("superseded_by", work.load("note", "old-fact").meta)  # nothing reserved on the old note
        other = work.create("note", "New fact", body="UNRELATED")  # a competing note takes the id
        self.assertEqual(other.id, "new-fact")
        out = work_commands.run(args)
        self.assertNotEqual(out["by"], "new-fact")
        self.assertEqual(work.load("note", "new-fact").body.strip(), "UNRELATED")
        self.assertEqual(work.load("note", out["by"]).body.strip(), "intended")
        self.assertEqual(work.load("note", out["by"]).meta["supersedes"], "old-fact")
        self.assertEqual(work.load("note", "old-fact").meta["superseded_by"], out["by"])

    def test_supersede_interrupted_at_the_move_is_finished_by_retry(self):
        import argparse
        import work_commands
        old = work.create("note", "Old fact", body="old body")
        args = argparse.Namespace(work_command="supersede", id=old.id, title="New fact", body="new body", body_file=None, new_id=None)
        with self._fail_rename("old-fact.md"):
            with self.assertRaises(OSError):
                work_commands.run(args)
        self.assertEqual([d.id for d in work.iter_docs("note")], ["new-fact"])  # old is already out of the live set
        self.assertEqual([d.id for d in work.iter_docs("note", archived=True)], ["old-fact"])
        with self.assertRaises(work.WorkError):  # not mutable in between
            work_commands.run(argparse.Namespace(work_command="link", id="x", note=["old-fact"], artifact=[], task=[], goal=[]))
        other = work.create("note", "Another")  # a command in the window cannot take either id
        self.assertNotIn(other.id, ("old-fact", "new-fact"))
        different = argparse.Namespace(**{**vars(args), "title": "Changed title", "body": "different"})
        out = work_commands.run(different)  # the retry reuses the existing replacement
        self.assertEqual(out["by"], "new-fact")
        self.assertEqual(sorted(d.id for d in work.iter_docs("note")), ["another", "new-fact"])
        self.assertEqual(work.load("note", "new-fact").body.strip(), "new body")
        self.assertEqual(work.load("note", "old-fact").meta["status"], "superseded")
        self.assertFalse((self.root / "notes" / "old-fact.md").exists())

    def test_create_is_exclusive(self):
        work.ensure_store()
        path = self.root / "notes" / "taken.md"
        work.atomic_write(path, "keep")
        with self.assertRaises(work.WorkError):
            work.atomic_write(path, "overwrite", exclusive=True)
        self.assertEqual(path.read_text(), "keep")

    def test_permissions(self):
        doc = work.create("workspace", "Perm")
        for d in (self.root, self.root / "workspaces", self.root / "archive" / "notes"):
            self.assertEqual(stat.S_IMODE(d.stat().st_mode), 0o700, d)
        self.assertEqual(stat.S_IMODE(doc.path.stat().st_mode), 0o600)
        with work.locked():
            pass
        self.assertEqual(stat.S_IMODE((self.root / ".lock").stat().st_mode), 0o600)

    def test_tp_work_dir_isolation(self):
        self.assertEqual(work.store_root(), self.root.resolve())
        work.create("workspace", "Isolated")
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": str(self.root.parent / "other")}):
            self.assertEqual(list(work.iter_docs("workspace")), [])
        self.assertNotEqual(work.store_root(), Path.home() / ".config" / "tmux-pi" / "work")

    def test_default_store_is_user_config(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("TP_WORK_DIR")
            self.assertEqual(work.store_root(), (Path.home() / ".config" / "tmux-pi" / "work").resolve())

    def test_trust_guard_does_not_block_work(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(home, True))
        store = home / ".config" / "tmux-pi" / "work" / "workspaces"
        store.mkdir(parents=True)
        (home / ".config" / "tmux-pi" / "trust").mkdir()
        file = str(store / "a.md")
        calls = [("Bash", {"command": "tp-agent work list"}),
                 ("Bash", {"command": "tp-agent work new 'Title' --objective 'x'"}),
                 ("Read", {"file_path": file}),
                 ("Write", {"file_path": file, "content": "x"}),
                 ("Edit", {"file_path": file, "old_string": "a", "new_string": "b"}),
                 ("Bash", {"command": f"cat {file}"}),
                 ("Bash", {"command": f"ls {store}"})]
        for tool, params in calls:
            answer = trust_guard.judge(tool, params, cwd=str(home), home=str(home))
            self.assertEqual(answer.kind, trust_guard.ALLOW, (tool, params, answer))
        # and the guard still protects its own sibling
        denied = trust_guard.judge("Read", {"file_path": str(home / ".config" / "tmux-pi" / "trust" / "x")}, cwd=str(home), home=str(home))
        self.assertEqual(denied.kind, trust_guard.DENY)


if __name__ == "__main__":
    unittest.main()
