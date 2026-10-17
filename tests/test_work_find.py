"""`tp-agent find` (Phase B): the store, the task records of several sessions, filters, redaction, and the read-only shared clone.
Every store, project and git repository here is a temp directory; the real work store and engineering-context clone are never read."""
import hashlib
import io
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

AWS = "AKIA" + "IOSFODNN7EXAMPLE"  # assembled so this file holds no key-shaped literal
KEY = "AKIA" + "X" * 16


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def task(session, name, request, result="", archived=False, created="2026-09-01T10:00:00Z", extra=None):
    folder = session / ("archive" if archived else "tasks") / name
    write(folder / "meta.json", json.dumps({"id": name, "role": "coder", "created": created}))
    write(folder / "request.md", request + "\n")
    if result:
        write(folder / "result.md", result + "\n")
    for file, text in (extra or {}).items():
        write(folder / file, text)
    return folder


class FindBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.store = self.tmp / "work"
        self.project = (self.tmp / "project").resolve()
        self.project.mkdir()
        self.config = self.tmp / "work-config.json"
        self.config.write_text("{}")
        patch = mock.patch.dict(os.environ, {"TP_WORK_DIR": str(self.store), "TP_WORKDIR": str(self.project), "TP_WORK_CONFIG": str(self.config)})
        patch.start()
        self.addCleanup(patch.stop)

    def search(self, *terms, **kw):
        return work_find.search(terms, **kw)

    def ids(self, *terms, **kw):
        return [(r["kind"], r["id"]) for r in self.search(*terms, **kw)[0]]

    def real(self, shown):
        """The file a labelled, root-relative display path names in these fixtures."""
        label, rel = shown.split(":", 1)
        return {"store": self.store, "task": self.project / ".tmux-pi" / "sessions",
                "session": self.project / ".tmux-pi" / "sessions"}[label] / rel


class StoreFind(FindBase):
    def setUp(self):
        super().setUp()
        self.ws = work.create("workspace", "DMP-77 loader rewrite", meta={"tickets": ["DMP-77"], "tags": ["loader"], "repos": [str(self.project)]},
                              body="## Objective\nRewrite the loader around the batch API.\n\n## Next actions\n- [ ] benchmark the zebra path\n")
        self.note = work.create("note", "Loader batch limits", meta={"tickets": ["DMP-77"], "tags": ["loader"], "workspaces": [self.ws.id]},
                                body="The batch endpoint rejects more than 500 rows per call.\n")
        self.other = work.create("note", "Unrelated gardening", meta={"tickets": ["ZZ-1"], "tags": ["garden"]}, body="Water on tuesdays.\n")

    def test_text_search_finds_workspace_note_and_skips_the_rest(self):
        self.assertEqual(sorted(self.ids("batch")), sorted([("note", self.note.id), ("workspace", self.ws.id)]))
        self.assertEqual(self.ids("tuesdays"), [("note", self.other.id)])
        self.assertEqual(self.ids("no-such-word"), [])

    def test_all_terms_must_match_case_insensitively(self):
        self.assertEqual(self.ids("BATCH", "rows"), [("note", self.note.id)])
        self.assertEqual(self.ids("batch", "tuesdays"), [])

    def test_a_malformed_or_missing_updated_does_not_break_find_or_since(self):
        folder = self.store / "workspaces"
        (folder / "hand-edited.md").write_text("---\nid: hand-edited\ntitle: Hand edited\nupdated: [broken]\n---\nzebra notes\n")
        (self.store / "notes" / "undated.md").write_text("---\nid: undated\ntitle: Undated\n---\nzebra evidence\n")
        found = self.ids("zebra")
        self.assertIn(("workspace", "hand-edited"), found)
        self.assertIn(("note", "undated"), found)
        self.assertEqual(self.ids("zebra", since="2000-01-01"), found)  # both fall back to their file time, which is today
        self.assertNotIn(("workspace", "hand-edited"), self.ids("zebra", since="2999-01-01"))
        rows = self.search("zebra")[0]
        self.assertTrue(all(len(r["updated"]) == 20 for r in rows if r["id"] in {"hand-edited", "undated"}))

    def test_ticket_tag_and_status_filters(self):
        self.assertEqual({k for k, _ in self.ids(ticket="DMP-77")}, {"workspace", "note"})
        self.assertEqual(self.ids(ticket="ZZ-1"), [("note", self.other.id)])
        self.assertEqual(self.ids(tag="garden"), [("note", self.other.id)])
        self.assertEqual(self.ids(tag="LOADER", kind="note"), [("note", self.note.id)])
        self.assertEqual(self.ids(ticket="DMP-77", status="paused"), [])
        self.assertEqual(self.ids(status="active", kind="workspace"), [("workspace", self.ws.id)])

    def test_workspace_filter_limits_to_that_workspace_and_its_notes(self):
        self.assertEqual(sorted(self.ids(workspace=self.ws.id)), sorted([("workspace", self.ws.id), ("note", self.note.id)]))
        self.assertEqual(self.ids(workspace="nope"), [])

    def test_since_filters_on_updated_and_rejects_a_bad_date(self):
        self.assertEqual(self.ids(since="2999-01-01"), [])
        self.assertTrue(self.ids(since="2000-01-01"))
        with self.assertRaises(work.WorkError):
            self.search(since="yesterday")

    def test_unknown_kind_is_an_error(self):
        with self.assertRaises(work.WorkError):
            self.search(kind="everything")

    def test_snippet_is_at_most_160_characters_and_names_a_path_and_line(self):
        long = "intro " + "filler " * 80 + "needle in the middle " + "tail " * 80
        note = work.create("note", "Long line", body=long + "\n")
        rows, _ = self.search("needle")
        row = next(r for r in rows if r["id"] == note.id)
        self.assertLessEqual(len(row["snippet"]), 160)
        self.assertIn("needle", row["snippet"])
        self.assertEqual(row["path"], f"store:notes/{note.id}.md")
        self.assertEqual(self.real(row["path"]).resolve(), note.path.resolve())
        self.assertEqual(note.path.read_text().split("\n")[row["line"] - 1].split()[0], "intro")

    def test_default_snippet_is_prose_not_a_heading(self):
        row = self.search(ticket="DMP-77", kind="workspace")[0][0]
        self.assertFalse(row["snippet"].startswith("#"))
        self.assertIn("Rewrite the loader", row["snippet"])

    def test_superseded_and_archived_are_hidden_unless_all(self):
        work.move_to_archive(work.load("note", self.other.id), "archived")
        self.assertEqual(self.ids("tuesdays"), [])
        rows, _ = self.search("tuesdays", everything=True)
        self.assertEqual([r["id"] for r in rows], [self.other.id])
        self.assertIn("[archived]", work_find.format_rows(rows, 1))
        old = work.load("note", self.note.id)
        work.move_to_archive(old, "superseded", {"superseded_by": "x"})
        self.assertNotIn(("note", self.note.id), self.ids("batch"))
        rows, _ = self.search("batch", kind="note", everything=True)
        self.assertIn("[superseded]", work_find.format_rows(rows, 1))

    def test_limit_and_the_count_of_the_rest(self):
        for i in range(5):
            work.create("note", f"Bulk {i}", body="common-term\n")
        rows, total = self.search("common-term", limit=2)
        self.assertEqual((len(rows), total), (2, 5))
        self.assertIn("+3 more; narrow the search or raise --limit", work_find.format_rows(rows, total))

    def test_planted_secret_is_redacted_in_snippet_title_and_json(self):
        work.create("note", f"Credentials {AWS}", body=f"the key is {AWS} keep it\n", meta={"tags": ["creds"]})
        rows, _ = self.search("credentials")
        self.assertTrue(rows)
        blob = json.dumps(rows) + work_find.format_rows(rows, len(rows))
        self.assertNotIn(AWS, blob)
        self.assertIn("REDACTED", blob)
        rows, _ = self.search("keep", tag="creds")
        self.assertNotIn(AWS, json.dumps(rows))

    def test_secret_cut_by_the_snippet_window_is_still_redacted(self):
        line = "x" * 100 + " marker " + AWS + " " + "y" * 30
        work.create("note", "Window", body=line + "\n")
        rows, _ = self.search("marker")
        self.assertNotIn(AWS, json.dumps(rows))
        self.assertNotIn(AWS[:10], json.dumps(rows))

    def test_a_term_far_beyond_64_kib_in_a_note_or_workspace_is_found(self):
        tail = "a" * 70000 + " rare-tail-term\n"
        note = work.create("note", "Huge note", body=tail)
        ws = work.create("workspace", "Huge workspace", body="## Objective\n" + tail)
        found = self.search("rare-tail-term")[0]
        self.assertEqual({(r["kind"], r["id"]) for r in found}, {("note", note.id), ("workspace", ws.id)})
        for row in found:  # a 70,000-character line is over the screen's cap: found, but shown only as where to open it
            self.assertEqual(row["snippet"], f"[line of {len(tail) - 1} chars not shown; open {row['path']}:{row['line']}]")

    def test_a_decision_entry_beyond_64_kib_is_found(self):
        pad = "- [ ] filler\n" * 6000
        ws = work.create("workspace", "Big decisions", body=f"## Next actions\n{pad}\n## Open decisions\n"
                         "### big-decisions/D1 · normal · open\nQuestion: Use the far-away quokka?\n")
        self.assertEqual(self.ids("quokka", kind="decision"), [("decision", f"{ws.id}/D1")])

    def test_snippet_is_windowed_on_a_match_beyond_column_2000(self):
        note = work.create("note", "Wide line", body="a" * 2100 + " NEEDLE " + "b" * 1500 + "\n")
        row = next(r for r in self.search("needle")[0] if r["id"] == note.id)
        self.assertIn("NEEDLE", row["snippet"])
        self.assertLessEqual(len(row["snippet"]), 160)

    def test_snippet_far_down_a_wide_line_is_still_screened_for_secrets(self):
        line = "a" * 2500 + " marker " + AWS + " " + "b" * 1000
        work.create("note", "Wide secret", body=line + "\n")
        rows, _ = self.search("marker")
        self.assertIn("marker", json.dumps(rows))
        self.assertNotIn(AWS, json.dumps(rows))
        self.assertNotIn(AWS[:10], json.dumps(rows))

    def test_a_secret_cut_by_the_window_on_a_wide_line_is_screened(self):
        line = "a" * 2500 + " marker " + "c" * 100 + " " + AWS + " " + "b" * 1000
        work.create("note", "Wide cut", body=line + "\n")
        rows, _ = self.search("marker")
        self.assertIn("marker", json.dumps(rows))
        self.assertNotIn(AWS[:8], json.dumps(rows))

    def test_a_secret_named_far_from_the_term_is_screened_with_its_whole_line(self):
        # review reproduction: the `API_KEY=` that makes the run a secret is 1,100 characters before the term, so a screen of
        # only the window's neighbourhood saw a bare run of letters and showed the credential's value
        line = "API_KEY=" + "alpha" * 220 + "marker" + "beta" * 220
        note = work.create("note", "Key line", body=line + "\n")
        rows, total = self.search("marker", kind="note")
        self.assertEqual([r["id"] for r in rows], [note.id])
        shown = [rows[0]["snippet"], rows[0]["title"], work_find.format_rows(rows, total).replace(str(self.tmp), "")]
        for text in shown:
            self.assertNotIn("alph", text)
            self.assertNotIn("beta", text)
        self.assertIn("API_KEY=<REDACTED", rows[0]["snippet"])

    def test_a_line_over_the_cap_shows_only_where_to_open_it(self):
        line = "API_KEY=" + "q" * 4200 + " marker " + "z" * 300
        note = work.create("note", "Over cap", body="intro line\n" + line + "\n")
        rows, total = self.search("marker")
        row = next(r for r in rows if r["id"] == note.id)
        self.assertEqual(row["line"], note.path.read_text().split("\n").index(line) + 1)
        self.assertEqual(row["snippet"], f"[line of {len(line)} chars not shown; open store:notes/{note.id}.md:{row['line']}]")
        for text in (json.dumps(rows), work_find.format_rows(rows, total)):
            self.assertNotIn("qqqq", text)
            self.assertNotIn("zzzz", text)
            self.assertIn(f"[line of {len(line)} chars not shown; open store:notes/{note.id}.md:{row['line']}]", text)

    def test_the_cap_comes_from_config_and_also_covers_titles(self):
        self.config.write_text(json.dumps({"find_line_max": 100}))
        note = work.create("note", "Cap " + "t" * 120, body="short capword line\n" + "w" * 150 + " capword\n")
        row = next(r for r in self.search("capword")[0] if r["id"] == note.id)
        self.assertEqual(row["snippet"], "short capword line")
        self.assertEqual(row["title"], "[title of 124 chars not shown]")
        row = next(r for r in self.search("w" * 10, "capword")[0] if r["id"] == note.id)
        self.assertTrue(row["snippet"].startswith("[line of 158 chars not shown; open "))
        self.assertNotIn("wwww", row["snippet"])

    def test_a_placeholder_too_long_for_the_snippet_names_path_line_generically(self):
        query = work_find.Query(["marker"])
        text = query.snippet("x" * 5000, "/" + "p" * 200, 7)
        self.assertEqual(text, "[line of 5000 chars not shown; open it at path:line]")
        self.assertLessEqual(len(text), work_find.SNIPPET)

    def test_nothing_is_written_by_a_search(self):
        before = sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*"))
        self.search("batch")
        self.search(ticket="DMP-77", everything=True)
        self.assertEqual(sorted((str(p), p.stat().st_mtime_ns) for p in self.store.rglob("*")), before)


class TaskFind(FindBase):
    def setUp(self):
        super().setUp()
        sessions = self.project / ".tmux-pi" / "sessions"
        self.new = sessions / "20261015-aaaa"
        self.old = sessions / "20260820-bbbb"
        self.ws = work.create("workspace", "DMP-77 loader rewrite", meta={"tickets": ["DMP-77"], "repos": [str(self.project)]})
        self.note = work.create("note", "Importer retry facts", meta={"tickets": ["DMP-77"]}, body="Retries are capped at three.\n")
        task(self.new, "coder-new", "Fix the importer for DMP-77 retries", "Done: retries are capped.", created="2026-10-15T09:00:00Z")
        task(self.new, "coder-other", "Unrelated spelling pass", "typos fixed")
        task(self.old, "coder-old", "Earlier attempt at DMP-77 importer", "Reverted: wrong approach (DMP-77).", archived=True, created="2026-08-20T09:00:00Z")
        write(self.old / "session-objective.md", "Land DMP-77 importer\n")

    def test_ticket_search_finds_workspace_and_an_archived_task_from_an_older_session(self):
        rows, total = self.search(ticket="DMP-77")
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r["kind"], []).append(r)
        self.assertEqual([r["id"] for r in by_kind["workspace"]], [self.ws.id])
        self.assertEqual([r["id"] for r in by_kind["note"]], [self.note.id])
        self.assertEqual({r["id"] for r in by_kind["task"]}, {"coder-new", "coder-old"})
        old = next(r for r in by_kind["task"] if r["id"] == "coder-old")
        self.assertEqual(old["status"], "archived")
        self.assertEqual(old["session"], "20260820-bbbb")
        self.assertTrue(old["path"].startswith("task:20260820-bbbb/archive/coder-old/"), old["path"])
        self.assertTrue(self.real(old["path"]).is_file())
        for r in rows:
            self.assertTrue(self.real(r["path"]).exists(), r)
            self.assertNotIn(str(self.tmp), json.dumps(r))  # no configured root directory is shown
            self.assertLessEqual(len(r["snippet"]), 160)
            self.assertTrue(r["snippet"])
        self.assertEqual([r["id"] for r in by_kind["session"]], ["20260820-bbbb"])

    def test_the_ticket_filter_does_not_match_a_longer_ticket_number(self):
        task(self.new, "coder-77x", "Work on DMP-777 instead", "n/a")
        ids = {r["id"] for r in self.search(ticket="DMP-77", kind="task")[0]}
        self.assertNotIn("coder-77x", ids)

    def test_task_status_filter_separates_live_and_archived(self):
        self.assertEqual({r["id"] for r in self.search("importer", kind="task", status="archived")[0]}, {"coder-old"})
        self.assertEqual({r["id"] for r in self.search("importer", kind="task", status="active")[0]}, {"coder-new"})

    def test_a_task_is_found_from_another_working_directory_through_the_workspace_repos(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        with mock.patch.dict(os.environ, {"TP_WORKDIR": str(elsewhere)}):
            self.assertIn(("task", "coder-old"), self.ids("earlier", kind="task"))
        unlinked = work.load("workspace", self.ws.id)
        unlinked.meta["repos"] = []
        unlinked.save()
        with mock.patch.dict(os.environ, {"TP_WORKDIR": str(elsewhere)}):
            self.assertEqual(self.ids("earlier", kind="task"), [])

    def test_since_and_workspace_filters_apply_to_tasks(self):
        self.assertEqual(self.ids("importer", kind="task", since="2026-10-01"), [("task", "coder-new")])
        link = work.load("workspace", self.ws.id)
        link.body = work.append_line(link.body, "Sessions", "- run 20260820-bbbb, tasks: coder-old")
        link.save()
        self.assertEqual(self.ids(kind="task", workspace=self.ws.id), [("task", "coder-old")])

    def test_packet_is_never_searched_and_omitted_sensitive_context_is_not_surfaced(self):
        task(self.new, "coder-sens", "Check the config", "Looked at it.",
             extra={"packet.md": "# Packet\npacket-only-word\n- Sensitive context /srv/secret/prod.env was omitted from the packet.\n",
                    "task.md": "# Record\nSee /srv/secret/prod.env for the values hidden-value-line\nplain line kept\n"})
        self.assertEqual(self.ids("packet-only-word", kind="task"), [])
        self.assertEqual(self.ids("hidden-value-line", kind="task"), [])
        self.assertEqual(self.ids("plain", "line", kind="task"), [("task", "coder-sens")])

    def test_a_metadata_hit_points_at_the_real_line_and_text_of_meta_json(self):
        fields = {"created": "2026-09-01T10:00:00Z", "parent": "researcher-p1", "role": "metarole", "lane": "metalane", "id": "coder-meta",
                  "deliverable": "metadeliv", "note": "unsearched"}
        for layout, indent in (("pretty", 2), ("pretty4", 4), ("compact", None)):
            folder = task(self.new, f"coder-meta-{layout}", "Plain request")
            record = {**fields, "id": f"coder-meta-{layout}"}
            write(folder / "meta.json", json.dumps(record, indent=indent) + "\n")
            lines = (folder / "meta.json").read_text().splitlines()
            for word in ("metarole", "metalane", "metadeliv", "researcher-p1", f"coder-meta-{layout}"):
                rows = [r for r in self.search(word, kind="task")[0] if r["id"] == f"coder-meta-{layout}"]
                self.assertEqual(len(rows), 1, (layout, word))
                row = rows[0]
                if row["path"].endswith("meta.json"):
                    self.assertIn(word, lines[row["line"] - 1], (layout, word, row))
                    self.assertIn(row["snippet"].strip(), lines[row["line"] - 1], (layout, word, row))
            self.assertEqual([r["id"] for r in self.search("unsearched", kind="task")[0] if r["id"].startswith("coder-meta")], [])

    def test_lines_after_an_omitted_line_keep_their_numbers(self):
        folder = task(self.new, "coder-num", "Check", extra={"task.md": "# Record\nSee /srv/secret/prod.env here\nthird-line-word\n",
                                                            "packet.md": "- Sensitive context /srv/secret/prod.env was omitted from the packet.\n"})
        row = next(r for r in self.search("third-line-word", kind="task")[0] if r["id"] == "coder-num")
        self.assertEqual((folder / "task.md").read_text().splitlines()[row["line"] - 1], "third-line-word")

    def test_secret_in_a_task_result_is_redacted_in_the_snippet(self):
        task(self.new, "coder-leak", "Rotate credentials", f"The old key {AWS} was found in the log.")
        rows, _ = self.search("old key", kind="task")
        self.assertEqual([r["id"] for r in rows], ["coder-leak"])
        self.assertNotIn(AWS, json.dumps(rows))

    def test_a_symlinked_task_directory_is_not_followed(self):
        outside = self.tmp / "outside"
        task(outside, "coder-away", "outside-word in request")
        link = self.new / "tasks" / "linked"
        link.symlink_to(outside / "tasks" / "coder-away", target_is_directory=True)
        self.assertEqual(self.ids("outside-word", kind="task"), [])


class SharedFind(FindBase):
    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.shared), *args], check=True, text=True, capture_output=True,
                              env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}).stdout.strip()

    def snapshot(self):
        """Working files (content and mtime), HEAD, the refs and the index file, read without running anything that could refresh them."""
        digest = hashlib.sha256()
        for path in sorted(self.shared.rglob("*")):
            if path.is_file() and ".git/objects" not in path.as_posix():
                digest.update(path.as_posix().encode() + path.read_bytes())
                digest.update(str(path.stat().st_mtime_ns).encode())
        return digest.hexdigest(), (self.shared / ".git" / "HEAD").read_text(), (self.shared / ".git" / "index").read_bytes()

    def setUp(self):
        super().setUp()
        self.shared = self.tmp / "engineering-context"
        self.shared.mkdir()
        self.git("init", "-q")
        write(self.shared / "context" / "loader.context.md",
              "---\ncontext_id: loader.batch\nstatus: verified\nlast_reviewed: 2026-10-01\n---\n\n# Context: Loader batching\n\nBatches cap at 500 rows. DMP-77 relies on it.\n")
        write(self.shared / "context" / "draft.context.md", "---\ncontext_id: x.draft\nstatus: draft\nlast_reviewed: 2026-10-10\n---\n\n# Context: Draft\n\nbatch draft text\n")
        write(self.shared / ".git-ignored-dir" / "n.md", "unrelated\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "seed")
        self.head = self.git("rev-parse", "HEAD")
        self.config.write_text(json.dumps({"shared": {"path": str(self.shared), "search": True}}))

    def test_shared_search_finds_files_and_leaves_the_clone_untouched(self):
        before = self.snapshot()
        rows, _ = self.search("batch", kind="shared")
        self.assertEqual({r["id"] for r in rows}, {"loader.batch", "x.draft"})
        row = next(r for r in rows if r["id"] == "loader.batch")
        self.assertEqual(row["title"], "Context: Loader batching")
        self.assertEqual(row["path"], "shared:context/loader.context.md")
        self.assertLessEqual(len(row["snippet"]), 160)
        self.assertEqual(self.ids(ticket="DMP-77", kind="shared"), [("shared", "loader.batch")])
        self.assertEqual(self.ids("batch", kind="shared", status="verified"), [("shared", "loader.batch")])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(subprocess.run(["git", "-C", str(self.shared), "status", "--porcelain"], text=True, capture_output=True,
                                        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout, "")

    def test_a_match_in_the_5001st_shared_file_is_found(self):
        for i in range(5002):
            write(self.shared / "many" / f"f{i:05d}.md", "filler\n")
        write(self.shared / "many" / "zz-last.md", "# Last\n\nfar-end-needle\n")
        incomplete = []
        rows, _ = work_find.search(["far-end-needle"], kind="shared", incomplete=incomplete)
        self.assertEqual([r["title"] for r in rows], ["Last"])
        self.assertEqual(incomplete, [])

    def test_an_oversized_shared_file_is_reported_not_silently_cut(self):
        write(self.shared / "context" / "big.md", "x" * 3000 + "\n\nbeyond-the-cut\n")
        with mock.patch.object(work_find, "SHARED_MAX", 1000):
            incomplete = []
            rows, _ = work_find.search(["beyond-the-cut"], kind="shared", incomplete=incomplete)
            self.assertEqual(rows, [])  # past the cut, so not found ...
            self.assertEqual(len(incomplete), 1)  # ... and the search says it could not look
            self.assertIn("big.md", incomplete[0])
            self.assertIn("incomplete: ", work_find.format_rows(rows, 0, incomplete))
            self.assertIn("no matches", work_find.format_rows(rows, 0, incomplete))
            incomplete = []
            work_find.search(["batch"], kind="shared", incomplete=incomplete)
            self.assertEqual(len(incomplete), 1)
        incomplete = []
        work_find.search(["batch"], kind="shared", incomplete=incomplete)
        self.assertEqual(incomplete, [])

    def test_shared_is_off_unless_switched_on_and_a_default_search_does_not_include_it(self):
        self.config.write_text(json.dumps({"shared": {"path": str(self.shared), "search": False}}))
        self.assertEqual(self.ids("batch", kind="shared"), [])
        self.assertNotIn("shared", {k for k, _ in self.ids("batch")})
        self.config.write_text("{}")
        self.assertEqual(self.ids("batch", kind="shared"), [])

    def test_shared_secret_is_redacted(self):
        write(self.shared / "context" / "leak.context.md", f"# Context: Leak\n\nkey {AWS} here\n")
        rows, _ = self.search("key", kind="shared")
        self.assertTrue(rows)
        self.assertNotIn(AWS, json.dumps(rows))

    def test_shared_does_not_follow_symlinks_out_of_the_clone(self):
        outside = self.tmp / "outside.md"
        outside.write_text("outside-secret-word\n")
        (self.shared / "context" / "link.md").symlink_to(outside)
        (self.shared / "dirlink").symlink_to(self.tmp, target_is_directory=True)
        self.assertEqual(self.ids("outside-secret-word", kind="shared"), [])


def strings(value):
    """Every string in a row, however nested, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


class DisplayScreen(FindBase):
    """Every field `find` and `near_duplicates` return for display is screened, not only the snippet."""

    def assertClean(self, value, where):
        for text in strings(value):
            self.assertNotIn(KEY.lower(), text.lower(), where)

    def test_every_display_field_of_every_kind_of_hit_is_screened(self):
        title = f"Stored {KEY} leak"
        note = work.create("note", title, meta={"tickets": ["DMP-77"], "tags": ["loader"], "sources": [KEY]}, body=f"needle {KEY}\n")
        work.create("workspace", title, meta={"tickets": ["DMP-77", KEY], "tags": ["loader"], "repos": [str(self.project)]},
                    body=f"## Objective\nneedle in {KEY}\n\n## Open decisions\n### {work.slugify(title)}/D1 · normal · open\n"
                         f"Question: needle {KEY}?\n")
        session = self.project / ".tmux-pi" / "sessions" / f"run-{KEY}"
        write(session / "session-objective.md", f"needle objective {KEY}\n")
        task(session, f"task-{KEY}", f"needle request {KEY}", result=f"needle result {KEY}")
        shared = self.tmp / "shared"
        write(shared / "context" / f"{KEY}.context.md", f"---\ncontext_id: ctx-{KEY}\nstatus: draft\n---\n# Shared {KEY}\nneedle {KEY}\n")
        self.config.write_text(json.dumps({"shared": {"path": str(shared), "promote_dir": "", "search": True}}))
        rows, _ = self.search("needle", everything=True)
        self.assertEqual({r["kind"] for r in rows}, {"note", "workspace", "decision", "task", "session", "shared"})
        for row in rows:
            self.assertClean(row, row["kind"])
        self.assertClean(work_find.format_rows(rows, len(rows)), "text")
        self.assertClean(json.dumps(rows), "json")
        by_kind = {r["kind"]: r for r in rows}
        self.assertEqual((by_kind["note"]["id"], by_kind["note"]["path"]), ("[id withheld]", "[path withheld]"))
        self.assertIn("REDACTED", by_kind["note"]["title"])
        self.assertEqual(by_kind["workspace"]["tickets"], ["DMP-77", "[tickets withheld]"])
        self.assertTrue(note.path.exists())

    def test_clean_rows_keep_their_id_and_path(self):
        note = work.create("note", "Plain note", body="needle\n")
        row = self.search("needle")[0][0]
        self.assertEqual((row["id"], row["path"]), (note.id, f"store:notes/{note.id}.md"))

    def test_near_duplicates_screens_title_id_and_path(self):
        work.create("note", f"Stored {KEY}", meta={"tickets": ["DMP-77"], "tags": ["loader"]}, body="x\n")
        (dup,) = work_find.near_duplicates("New note", ["DMP-77"], ["loader"])
        self.assertClean(dup, "near_duplicates")
        self.assertTrue(dup["withheld"])
        self.assertEqual((dup["id"], dup["path"]), ("[id withheld]", "[path withheld]"))
        self.assertIn("REDACTED", dup["title"])

    def test_near_duplicates_of_a_clean_note_keeps_its_pointer(self):
        note = work.create("note", "Loader batch limits", meta={"tickets": ["DMP-77"]}, body="x\n")
        (dup,) = work_find.near_duplicates("Loader batch limits")
        self.assertEqual((dup["id"], dup["path"], dup["withheld"]), (note.id, f"store:notes/{note.id}.md", False))


class FindCli(FindBase):
    def run_cli(self, *args):
        env = {**os.environ, "TP_STATE_DIR": str(self.project / ".tmux-pi")}
        env.pop("TMUX", None)
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env, text=True, capture_output=True, timeout=30)

    def test_find_needs_no_session_and_prints_a_line_per_hit(self):
        work.create("note", "CLI visible", meta={"tickets": ["DMP-77"]}, body="cli-needle text\n")
        env = {k: v for k, v in os.environ.items() if k not in ("TP_STATE_DIR", "TMUX")}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "find", "cli-needle"], env=env, text=True, capture_output=True, timeout=30, cwd=self.tmp)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout, r"(?m)^note  cli-visible  CLI visible  \S+  store:notes/cli-visible\.md:\d+  cli-needle text$")
        self.assertIn(work_find.PATHS_LEGEND, run.stdout)
        self.assertNotIn(str(self.tmp), run.stdout)
        self.assertEqual(self.run_cli("find", "absent-term").stdout.strip(), "no matches")

    def test_find_json_and_decision_rows_through_the_cli(self):
        self.assertEqual(self.run_cli("work", "new", "Decide things", "--id", "decide-things").returncode, 0)
        run = self.run_cli("work", "decision", "add", "decide-things", "--question", "Use the quokka queue?", "--priority", "high", "--blocking")
        self.assertEqual(run.returncode, 0, run.stderr)
        run = self.run_cli("find", "quokka", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertEqual([(r["kind"], r["id"]) for r in value["rows"]], [("decision", "decide-things/D1")])
        self.assertEqual(self.run_cli("find", "quokka", "--kind", "workspace").stdout.strip(), "no matches")

    def test_cli_json_carries_the_incomplete_flag(self):
        write(self.tmp / "shared" / "big.md", "x" * (work_find.SHARED_MAX + 10) + "\n")
        self.config.write_text(json.dumps({"shared": {"path": str(self.tmp / "shared"), "search": True}}))
        run = self.run_cli("find", "nothing-here", "--kind", "shared", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertTrue(value["incomplete"])
        self.assertIn("big.md", value["incomplete_reasons"][0])
        self.assertIn("incomplete: ", self.run_cli("find", "nothing-here", "--kind", "shared").stdout)
        clean = json.loads(self.run_cli("find", "x", "--kind", "note", "--json").stdout)
        self.assertEqual((clean["incomplete"], clean["incomplete_reasons"]), (False, []))

    def test_cli_text_and_json_never_show_a_fragment_of_a_split_or_over_cap_secret(self):
        work.create("note", "Key line", body="API_KEY=" + "alpha" * 220 + "marker" + "beta" * 220 + "\n")
        wide = work.create("note", "Wide line", body="API_KEY=" + "q" * 4200 + " marker\n")
        text = self.run_cli("find", "marker", "--kind", "note")
        value = self.run_cli("find", "marker", "--kind", "note", "--json")
        self.assertEqual((text.returncode, value.returncode), (0, 0), text.stderr + value.stderr)
        rows = json.loads(value.stdout)["rows"]
        self.assertEqual(len(rows), 2)
        row = next(r for r in rows if r["id"] == wide.id)
        placeholder = f"[line of 4215 chars not shown; open store:notes/{wide.id}.md:{row['line']}]"
        self.assertEqual(row["snippet"], placeholder)
        self.assertIn(placeholder, text.stdout)
        for out in (text.stdout.replace(str(self.tmp), ""), value.stdout.replace(str(self.tmp), "")):
            for fragment in ("alph", "beta", "qqqq"):
                self.assertNotIn(fragment, out)

    def test_duplicate_note_warning_and_json_never_carry_the_stored_secret(self):
        work.create("note", f"Stored {KEY}", meta={"tickets": ["DMP-77"], "tags": ["loader"]}, body="x\n")
        first = self.run_cli("work", "note", "add", "New note", "--ticket", "DMP-77", "--tag", "loader")
        value = self.run_cli("work", "note", "add", "Another note", "--ticket", "DMP-77", "--tag", "loader")
        for run in (first, value):
            self.assertEqual(run.returncode, 0, run.stderr)  # creation still succeeds
            self.assertNotIn(KEY.lower(), (run.stdout + run.stderr).lower())
            self.assertIn("warning: similar note", run.stderr)
            self.assertIn("withheld", run.stderr)
            self.assertIn("tp-agent find --kind note", run.stderr)
        # the first note added above is a clean control: shown by id; the stored one is withheld
        self.assertEqual(json.loads(value.stdout)["near_duplicates"], ["new-note", "[id withheld]"])
        self.assertTrue((work.store_root() / "notes" / "new-note.md").exists())

    def test_bad_arguments_are_errors_not_tracebacks(self):
        for args in (("find", "--since", "soon"), ("find", "--kind", "nope")):
            run = self.run_cli(*args)
            self.assertNotEqual(run.returncode, 0)
            self.assertNotIn("Traceback", run.stderr)


class OutputBoundary(unittest.TestCase):
    """One table: a key planted in each place a value can come from never reaches stdout or stderr of any command in the family
    (find text, JSON and no-hit; the duplicate-note warning; a promotion refusal), in any case."""

    PLANTS = {
        "store root directory": {"store": KEY},
        "shared file name (oversized)": {"shared": f"{KEY}.md"},
        "note title and slug": {"title": f"Needle {KEY} note"},
        "ticket": {"ticket": f"{KEY}-77"},
        "tag": {"tag": KEY},
    }

    def run_cli(self, env, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env, text=True, capture_output=True, timeout=60)

    def test_a_planted_key_never_reaches_any_output(self):
        for case, plant in self.PLANTS.items():
            with self.subTest(case), tempfile.TemporaryDirectory() as temp:
                tmp = Path(temp)
                store, project, tree = tmp / plant.get("store", "plain") / "work", tmp / "project", tmp / "tree"
                project.mkdir()
                tree.mkdir()
                config = {}
                if "shared" in plant:
                    write(tmp / "shared" / "docs" / plant["shared"], "needle\n" + "x" * (work_find.SHARED_MAX + 10) + "\n")
                    config["shared"] = {"path": str(tmp / "shared"), "search": True}
                (tmp / "work-config.json").write_text(json.dumps(config))
                env = {**os.environ, "TP_WORK_DIR": str(store), "TP_WORKDIR": str(project), "TP_WORK_CONFIG": str(tmp / "work-config.json"),
                       "TP_STATE_DIR": str(tmp / "state")}
                env.pop("TMUX", None)
                title, ticket, tag = plant.get("title", "Needle plain note"), plant.get("ticket", "DMP-77"), plant.get("tag", "loader")
                created = self.run_cli(env, "work", "note", "add", title, "--ticket", ticket, "--tag", tag, "--tag", "private",
                                       "--body", "needle body")
                self.assertEqual(created.returncode, 0, created.stderr)
                ident = next(p.stem for p in (store / "notes").glob("*.md"))
                runs = {
                    "note add": created,
                    "find text": self.run_cli(env, "find", "needle"),
                    "find json": self.run_cli(env, "find", "needle", "--json"),
                    "find no-hit": self.run_cli(env, "find", "absent-term-zz"),
                    "find no-hit json": self.run_cli(env, "find", "absent-term-zz", "--json"),
                    "find by ticket": self.run_cli(env, "find", "--ticket", ticket, "--json"),
                    "note add warning": self.run_cli(env, "work", "note", "add", title, "--ticket", ticket, "--tag", tag),
                    "promote refusal": self.run_cli(env, "work", "promote", ident, "--target", str(tree)),
                }
                for name, run in runs.items():
                    out = run.stdout + run.stderr
                    self.assertNotIn(KEY, out, f"{case}: {name}")
                    self.assertNotIn(KEY.lower(), out.lower(), f"{case}: {name}")
                    self.assertNotIn("Traceback", out, f"{case}: {name}")
                self.assertEqual(runs["find text"].returncode, 0, runs["find text"].stderr)
                self.assertIn("needle", runs["find text"].stdout)
                self.assertGreaterEqual(json.loads(runs["find json"].stdout)["total"], 1)
                self.assertIn("warning: similar note", runs["note add warning"].stderr)
                self.assertEqual(runs["note add warning"].returncode, 0)
                refusal = runs["promote refusal"]
                self.assertNotEqual(refusal.returncode, 0)
                self.assertIn("refused", refusal.stderr)
                self.assertIn("tagged private", refusal.stderr)
                self.assertEqual(list(tree.iterdir()), [])  # nothing was written
                if "shared" in plant:  # the cut is still reported, with the file withheld, also when nothing matched
                    for name in ("find text", "find no-hit"):
                        self.assertIn("incomplete: shared file [path withheld]", runs[name].stdout)
                    self.assertTrue(json.loads(runs["find no-hit json"].stdout)["incomplete"])


class WorkBoundary(unittest.TestCase):
    """The boundary is the dispatcher, not each command: every `work` subcommand, success and failure, with a key built at run time
    in each place a caller can put one (a title, a ticket, a body-file name that does not exist, an id), prints no key and no
    configured directory on stdout or stderr, and every path field of its JSON is labelled root-relative."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.store = self.tmp / "private-root-zz" / "work"
        self.tree = self.tmp / "private-tree-zz"
        self.tree.mkdir()
        write(self.tmp / "work-config.json", "{}")
        (self.tmp / "state").mkdir()
        self.env = {**os.environ, "TP_WORK_DIR": str(self.store), "TP_WORKDIR": str(self.tmp), "TP_WORK_CONFIG": str(self.tmp / "work-config.json"),
                    "TP_STATE_DIR": str(self.tmp / "state")}
        self.env.pop("TMUX", None)

    def cli(self, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True, capture_output=True, timeout=60)

    def test_every_work_subcommand_stays_inside_the_boundary(self):
        missing = str(self.tmp / f"{KEY}-missing.txt")
        a_directory = str(self.tmp / f"{KEY}-dir")
        (self.tmp / f"{KEY}-dir").mkdir()
        for setup in (("work", "new", "Base workspace", "--id", "ws1", "--ticket", "DMP-77", "--repo", str(self.tmp)),
                      ("work", "new", "Other workspace", "--id", "ws2"),
                      ("work", "note", "add", "Base note", "--id", "n1", "--body", "needle"),
                      ("work", "note", "add", "Second note", "--id", "n2", "--body", "needle"),
                      ("work", "decision", "add", "ws1", "--question", "first?"),
                      ("work", "decision", "add", "ws1", "--question", "second?")):
            self.assertEqual(self.cli(*setup).returncode, 0, setup)
        # (arguments, succeeds); each command of the family appears at least once, most of them as a failure too
        table = [
            (("work", "new", f"Plant {KEY} workspace", "--ticket", f"{KEY}-1", "--tag", KEY, "--repo", missing), True),
            (("work", "new", "Base workspace", "--id", "ws1"), False),
            (("work", "show", "ws1"), True),
            (("work", "show", "ws1", "--section", "next"), True),
            (("work", "show", "ws1", "--section", KEY), False),
            (("work", "show", f"{KEY.lower()}-gone"), False),
            (("work", "list"), True),
            (("work", "list", "--kind", "note", "--all"), True),
            (("work", "set", "ws1", "--title", f"{KEY} title", "--ticket", f"{KEY}-2", "--scope", KEY), True),
            (("work", "set", f"{KEY.lower()}-gone", "--title", "x"), False),
            (("work", "next", "ws1", f"do {KEY}"), True),
            (("work", "next", "ws1", "--done", "99"), False),
            (("work", "decide", "ws1", f"decided {KEY}", "--source", KEY), True),
            (("work", "block", "ws1", f"blocked {KEY}"), True),
            (("work", "block", "ws1", "--clear", "99"), False),
            (("work", "link", "ws1", "--note", "n1", "--artifact", missing, "--task", KEY), True),
            (("work", "link", "ws1", "--note", f"{KEY.lower()}-gone"), False),
            (("work", "attach", "ws1"), True),
            (("work", "attach", f"{KEY.lower()}-gone"), False),
            (("work", "note", "add", f"Needle {KEY} note", "--ticket", f"{KEY}-3", "--tag", KEY, "--body", "needle"), True),
            (("work", "note", "add", "Safe title", "--body-file", missing), False),
            (("work", "note", "add", "Safe title", "--body-file", a_directory), False),
            (("work", "note", "add", "Safe title", "--workspace", f"{KEY.lower()}-gone"), False),
            (("work", "note", "show", "n1"), True),
            (("work", "note", "show", f"{KEY.lower()}-gone"), False),
            (("work", "supersede", "n2", "--title", f"{KEY} replacement", "--body-file", missing), False),
            (("work", "supersede", "n2", "--title", "Replacement", "--new-id", "n3", "--body", "needle"), True),
            (("work", "supersede", f"{KEY.lower()}-gone", "--title", "x"), False),
            (("work", "decision", "add", "ws1", "--question", f"third {KEY}?", "--background", KEY, "--affects", f"task:{KEY}"), True),
            (("work", "decision", "add", f"{KEY.lower()}-gone", "--question", "q"), False),
            (("work", "decision", "answer", "ws1/D1", "--text", f"yes {KEY}", "--quote", KEY), True),
            (("work", "decision", "answer", "ws1/D1", "--text", "again"), False),
            (("work", "decision", "withdraw", "ws1/D2", "--reason", KEY), True),
            (("work", "decision", "withdraw", f"{KEY.lower()}/D9"), False),
            (("work", "decision", "list"), True),
            (("work", "decisions", "--all"), True),
            (("work", "decisions", "--json"), True),
            (("work", "promote", "n1", "--target", str(self.tree)), True),
            (("work", "promote", "n1", "--target", str(self.tree)), False),
            (("work", "promote", "n1", "--target", str(self.tree), "--ticket", f"{KEY}-9"), True),  # a well-formed ticket: its directory is written, its name redacted
            (("work", "promote", "n1", "--target", str(self.tmp / KEY / "absent")), False),
            (("work", "promote", f"{KEY.lower()}-gone", "--target", str(self.tree)), False),
            (("work", "archive", "ws2"), True),
            (("work", "archive", f"{KEY.lower()}-gone"), False),
            (("find", "needle"), True),
            (("find", "--ticket", f"{KEY}-3", "--json"), True),
            (("find", "--since", KEY), False),
        ]
        hidden = [str(self.store), str(self.store.resolve()), str(self.tree), str(self.tree.resolve()), "private-root-zz", "private-tree-zz"]
        for args, succeeds in table:
            run = self.cli(*args)
            out = run.stdout + run.stderr
            with self.subTest(args=" ".join(args[:3])):
                self.assertEqual(run.returncode == 0, succeeds, out)
                self.assertNotIn("Traceback", out)
                self.assertNotIn(KEY, out)
                self.assertNotIn(KEY.lower(), out.lower())
                for secret in hidden:
                    self.assertNotIn(secret, out)
                try:
                    shown = json.loads(run.stdout) if succeeds else None
                except ValueError:  # text output
                    shown = None
                for value in self.paths(shown):
                    self.assertRegex(value, r"^((store|shared|target|task|session):|\[path withheld\]$)", value)

    def paths(self, value):
        """Every string under a key named `path` or ending in `_path`, wherever it is in the JSON."""
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and (key == "path" or key.endswith("_path")):
                    yield item
                else:
                    yield from self.paths(item)
        elif isinstance(value, list):
            for item in value:
                yield from self.paths(item)

    def test_path_fields_of_work_json_are_labelled(self):
        self.assertEqual(self.cli("work", "new", "One", "--id", "one").returncode, 0)
        self.assertEqual(self.cli("work", "note", "add", "Old note", "--id", "old", "--body", "x").returncode, 0)
        shown = {
            "new": json.loads(self.cli("work", "new", "Two", "--id", "two").stdout),
            "list": json.loads(self.cli("work", "list").stdout)[0],
            "note add": json.loads(self.cli("work", "note", "add", "Fresh note", "--id", "fresh", "--body", "y").stdout),
            "supersede": json.loads(self.cli("work", "supersede", "old", "--title", "New one", "--new-id", "newer", "--body", "z").stdout),
        }
        self.assertEqual(shown["new"]["path"], "store:workspaces/two.md")
        self.assertTrue(shown["list"]["path"].startswith("store:workspaces/"))
        self.assertEqual(shown["note add"]["path"], "store:notes/fresh.md")
        self.assertEqual(shown["supersede"]["path"], "store:notes/newer.md")
        self.assertEqual(shown["supersede"]["archived_path"], "store:archive/notes/old.md")

    def test_any_exception_type_is_one_guarded_line(self):
        for error in (KeyError(KEY), RuntimeError(f"boom {KEY}"), OSError(2, "No such file", f"{self.tmp}/{KEY}-x"), ZeroDivisionError(KEY)):
            def run(error=error):
                print(f"partial {KEY}")
                print(f"warn {KEY}", file=sys.stderr)
                raise error
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.dict(os.environ, {"TP_WORK_DIR": str(self.store), "TP_WORK_CONFIG": str(self.tmp / "work-config.json")}), \
                    mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
                code = work_find.bounded(run)
            with self.subTest(type(error).__name__):
                self.assertEqual(code, 1)
                self.assertNotIn(KEY, out.getvalue() + err.getvalue())
                self.assertNotIn(str(self.tmp), out.getvalue() + err.getvalue())
                self.assertIn("tp-agent: ", err.getvalue())


if __name__ == "__main__":
    unittest.main()
