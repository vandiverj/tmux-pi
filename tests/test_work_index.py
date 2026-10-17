"""The `work` block of the session index (Phase B): candidates, notes, decisions and what they cost, plus `index --no-write`,
`index --bounded` (Pi's task_index) and the atomic objective write. Every store and state directory is a temp directory."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402
import work_index  # noqa: E402

SECRET_QUESTION = "Should we rotate the quokka credentials"
INDEX_KEYS = {"schema", "updated", "objective", "active", "pending", "unstatused", "paused", "recent"}


def stamp(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def aged(doc, days_ago):
    """Write a document with an explicit `updated` (save() always stamps now)."""
    doc.meta["updated"] = stamp(days_ago)
    work.atomic_write(doc.path, work.render(doc.meta, doc.body))
    return doc


def add_decision(ws, question, priority="normal", blocking=False):
    doc = work.load("workspace", ws)
    entries = work.decisions_of(doc)
    entries.append({"id": f"{doc.id}/D{work.next_decision_number(entries)}", "priority": priority, "status": "open", "blocking": blocking,
                    "question": question, "background": "", "options": "", "recommended": "", "affects": ["workspace"], "resolution": "",
                    "by": "", "at": "", "quote": "", "withdrawn": "", "_dirty": True})
    work.put_decisions(doc, entries)
    doc.save()


class IndexBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.store = self.tmp / "work"
        self.state = self.tmp / "project" / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.repo_a = self.tmp / "repo-a"
        self.repo_b = self.tmp / "repo-b"
        for repo in (self.repo_a, self.repo_b, self.repo_a / "sub" / "deep"):
            repo.mkdir(parents=True)
        self.config = self.tmp / "work-config.json"
        self.config.write_text("{}")
        self.env = {"TP_WORK_DIR": str(self.store), "TP_WORK_CONFIG": str(self.config), "TP_STATE_DIR": str(self.state),
                    "TP_WORKDIR": str(self.repo_a)}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def settings(self, **values):
        self.config.write_text(json.dumps(values))

    def block(self, workdir=None):
        return work_index.build(self.state, workdir)

    def cli(self, *args, env=None):
        full = {**os.environ, **self.env, **(env or {})}
        full.pop("TMUX", None)
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=full, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout


class BuildTests(IndexBase):
    def test_no_store_means_no_block_and_the_store_is_not_created(self):
        self.assertIsNone(self.block())
        self.assertFalse(self.store.exists())

    def test_an_empty_store_gives_no_block(self):
        work.ensure_store()
        self.assertIsNone(self.block())

    def test_candidates_are_the_active_workspaces_whose_repo_contains_the_workdir(self):
        here = work.create("workspace", "Here", meta={"repos": [str(self.repo_a)]})
        work.create("workspace", "Elsewhere", meta={"repos": [str(self.repo_b)]})
        work.create("workspace", "Paused here", meta={"repos": [str(self.repo_a)], "status": "paused"})
        for workdir in (self.repo_a, self.repo_a / "sub" / "deep"):
            block = self.block(str(workdir))
            self.assertEqual([c["id"] for c in block["candidates"]], [here.id])
            self.assertEqual(block["attached"], [])
        self.assertEqual(self.block(str(self.repo_a))["others"], 1)  # Elsewhere; the paused one is not "active"

    def test_candidates_are_newest_first_and_capped(self):
        for i, days in enumerate((5, 1, 3, 2)):
            aged(work.create("workspace", f"Ws {i}", meta={"repos": [str(self.repo_a)]}), days)
        self.settings(inject_candidates=2)
        block = self.block()
        self.assertEqual([c["id"] for c in block["candidates"]], ["ws-1", "ws-3"])
        self.assertEqual(block["others"], 2)
        self.settings(inject_candidates=3)
        self.assertEqual(len(self.block()["candidates"]), 3)

    def test_an_attached_workspace_replaces_the_candidates(self):
        a = work.create("workspace", "Attached one", meta={"repos": [str(self.repo_b)]})
        work.create("workspace", "Candidate", meta={"repos": [str(self.repo_a)]})
        b = work.create("workspace", "Attached two")
        (self.state / "session-work.json").write_text(json.dumps({"attached": [a.id, b.id, "missing"]}))
        block = self.block()
        self.assertEqual([x["id"] for x in block["attached"]], [a.id, b.id, "missing"])
        self.assertEqual(block["attached"][2], {"id": "missing", "title": "", "health": "file missing"})  # named, not dropped
        self.assertEqual(block["candidates"], [])
        self.assertEqual(block["others"], 1)

    def test_summary_carries_next_action_and_the_last_three_session_refs(self):
        ws = work.create("workspace", "Rich", meta={"repos": [str(self.repo_a)]}, body="## Objective\nO\n")
        doc = work.load("workspace", ws.id)
        doc.body = work.append_line(doc.body, "Next actions", "- [x] done thing")
        doc.body = work.append_line(doc.body, "Next actions", "- [ ] the real next step")
        for run in ("r1", "r2", "r3"):
            doc.body = work.append_line(doc.body, "Sessions", f"- run {run}, tasks: coder-{run}")
        doc.save()
        item = self.block()["candidates"][0]
        self.assertEqual(item["next"], "the real next step")
        self.assertEqual(item["sessions"], ["r1/coder-r1", "r2/coder-r2", "r3/coder-r3"][-3:])
        self.assertEqual(set(item), {"id", "title", "status", "updated", "next", "sessions"})
        # the old objective text is not part of the block
        self.assertNotIn('"O"', json.dumps(self.block()))

    def test_a_non_coding_workspace_without_repos_is_one_line_only_while_recent(self):
        fresh = work.create("workspace", "Research the vendor")
        block = self.block(str(self.repo_b))
        self.assertEqual([c["id"] for c in block["candidates"]], [fresh.id])
        aged(work.load("workspace", fresh.id), 40)
        block = self.block(str(self.repo_b))
        self.assertEqual(block["candidates"], [])
        self.assertEqual(block["others"], 1)

    def test_an_unrelated_workdir_shows_no_candidates_but_counts_the_rest(self):
        work.create("workspace", "In repo a", meta={"repos": [str(self.repo_a)]})
        block = self.block(str(self.repo_b))
        self.assertEqual((block["candidates"], block["attached"], block["others"]), ([], [], 1))

    def test_notes_surface_recent_workspace_less_and_important_ones_as_id_and_title(self):
        ws = work.create("workspace", "W", meta={"repos": [str(self.repo_a)]})
        yesterday = aged(work.create("note", "Updated yesterday", body="body one"), 1)
        old_important = aged(work.create("note", "Old but important", meta={"important": True}, body="body two"), 90)
        old_plain = aged(work.create("note", "Old plain", body="body three"), 90)
        linked_other = aged(work.create("note", "Linked to something else", meta={"workspaces": ["some-other-ws"]}), 1)
        linked_here = work.create("note", "Linked to the candidate", meta={"workspaces": [ws.id]})
        block = self.block()
        recent = [n["id"] for n in block["notes"]["recent"]]
        self.assertIn(yesterday.id, recent)
        self.assertIn(linked_here.id, recent)
        self.assertNotIn(old_plain.id, recent)
        self.assertNotIn(old_important.id, recent)
        self.assertLess(recent.index(linked_here.id), recent.index(linked_other.id) if linked_other.id in recent else 99)
        self.assertEqual([n["id"] for n in block["notes"]["important"]], [old_important.id])
        self.assertEqual(block["notes"]["important"][0], {"id": old_important.id, "title": "Old but important"})
        self.assertNotIn("body", json.dumps(block["notes"]))

    def test_notes_are_capped(self):
        for i in range(6):
            work.create("note", f"Note {i}")
        self.settings(inject_notes=2)
        self.assertEqual(len(self.block()["notes"]["recent"]), 2)

    def test_notes_alone_are_enough_for_a_block(self):
        work.create("note", "Only a note")
        block = self.block()
        self.assertEqual((block["attached"], block["candidates"], block["others"]), ([], [], 0))
        self.assertEqual(block["notes"]["recent"][0]["id"], "only-a-note")

    def test_archived_workspaces_and_notes_are_not_in_the_block(self):
        ws = work.create("workspace", "Gone", meta={"repos": [str(self.repo_a)]})
        note = work.create("note", "Gone note", meta={"important": True})
        work.move_to_archive(work.load("workspace", ws.id), "archived")
        work.move_to_archive(work.load("note", note.id), "archived")
        self.assertIsNone(self.block())

    def test_checkpoint_is_present_and_null(self):
        work.create("note", "n")
        self.assertIn("checkpoint", self.block())
        self.assertIsNone(self.block()["checkpoint"])


class DecisionTests(IndexBase):
    def make(self, workspaces, per):
        for w in range(workspaces):
            work.create("workspace", f"Ws {w:03d}", wanted=f"ws-{w:03d}")
            for d in range(per):
                add_decision(f"ws-{w:03d}", f"{SECRET_QUESTION} {w}/{d}", priority=("high", "normal", "low")[d % 3], blocking=(w % 10 == 0 and d == 0))

    def test_every_open_decision_is_in_items_with_only_four_fields_and_correct_counts(self):
        self.make(30, 10)
        work.create("workspace", "Done one", wanted="done-one")
        add_decision("done-one", "answered later")
        work.move_to_archive(work.load("workspace", "done-one"), "archived")  # an archived workspace's decisions are not open
        block = self.block()["decisions"]
        self.assertEqual(block["open"], 300)
        self.assertEqual(block["blocking"], 3)
        self.assertEqual(len(block["items"]), 300)  # the JSON is uncapped
        self.assertEqual({tuple(sorted(i)) for i in block["items"]}, {("blocking", "id", "priority", "workspace")})
        self.assertEqual([i["blocking"] for i in block["items"][:3]], [True] * 3)
        self.assertEqual(len({i["id"] for i in block["items"]}), 300)
        self.assertNotIn(SECRET_QUESTION, json.dumps(self.block()))

    def test_an_answered_decision_is_not_counted(self):
        self.make(1, 3)
        self.cli("work", "decision", "answer", "ws-000/D2", "--text", "yes")
        self.assertEqual(self.block()["decisions"]["open"], 2)

    def test_index_refresh_in_repo_x_has_no_question_text_from_a_workspace_not_linked_to_x(self):
        work.create("workspace", "Linked to a", wanted="linked-a", meta={"repos": [str(self.repo_a)]})
        work.create("workspace", "Linked to b", wanted="linked-b", meta={"repos": [str(self.repo_b)]})
        add_decision("linked-a", "ONLY-A-QUESTION", blocking=True)
        add_decision("linked-b", "ONLY-B-QUESTION", blocking=True)
        out = self.cli("index", "--no-write", env={"TP_WORKDIR": str(self.repo_a)})
        self.assertNotIn("ONLY-B-QUESTION", out)
        self.assertNotIn("ONLY-A-QUESTION", out)  # no question text at all, only ids
        index = json.loads(out)["work"]["decisions"]
        self.assertEqual(sorted(i["id"] for i in index["items"]), ["linked-a/D1", "linked-b/D1"])
        self.cli("index", "--no-tick", env={"TP_WORKDIR": str(self.repo_a)})
        self.assertNotIn("ONLY-B-QUESTION", (self.state / "session-index.json").read_text())

    def test_work_decisions_still_lists_every_open_decision_with_its_question(self):
        self.make(3, 2)
        work.create("workspace", "Unattached elsewhere", wanted="far-away", meta={"repos": [str(self.repo_b)]})
        add_decision("far-away", "far away question")
        out = self.cli("work", "decisions", env={"TP_WORKDIR": str(self.repo_a)})
        self.assertEqual(len(out.strip().split("\n")), 7)
        self.assertIn("far-away/D1", out)
        self.assertIn("far away question", out)

    def test_summary_text_has_counts_ids_that_fit_and_the_command(self):
        items = [{"id": f"w/D{i}", "priority": "normal", "workspace": "w", "blocking": i < 2} for i in range(300)]
        decisions = {"open": 300, "blocking": 2, "chars": 400, "items": items}
        text = work_index.decision_summary(decisions)
        lines = text.split("\n")
        self.assertEqual(lines[0], "decisions: 300 open (2 blocking)")
        self.assertLessEqual(len(text), 400)
        shown = [l for l in lines if l.startswith("- ")]
        self.assertTrue(10 < len(shown) < 300)
        self.assertEqual(shown[:2], ["- w/D0 [normal] blocking", "- w/D1 [normal] blocking"])
        self.assertEqual(lines[-1], f"+{300 - len(shown)} more (0 blocking): tp-agent work decisions")

    def test_summary_with_few_decisions_lists_them_all(self):
        items = [{"id": "a/D1", "priority": "low", "workspace": "a", "blocking": False}, {"id": "b/D1", "priority": "high", "workspace": "b", "blocking": False}]
        text = work_index.decision_summary({"open": 2, "blocking": 0, "chars": 1200, "items": items})
        self.assertEqual(text, "decisions: 2 open (0 blocking)\n- b/D1 [high]\n- a/D1 [low]\ntp-agent work decisions (every question and detail)")
        self.assertEqual(work_index.decision_summary({"open": 0, "blocking": 0, "chars": 1200, "items": []}), "")

    def test_summary_trailer_survives_a_tiny_budget(self):
        items = [{"id": f"w/D{i}", "priority": "normal", "workspace": "w", "blocking": False} for i in range(50)]
        text = work_index.decision_summary({"open": 50, "blocking": 0, "chars": 1, "items": items})
        self.assertTrue(text.endswith("tp-agent work decisions"))


class UpdatedFieldTests(IndexBase):
    """One accessor, `work.updated_at`: a malformed or missing `updated` never raises and never looks fresher than the file is."""

    def raw(self, kind, ident, front, body="Body.\n", days_old=None):
        folder = self.store / work.KINDS[kind]
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{ident}.md"
        path.write_text(f"---\nid: {ident}\ntitle: {ident.title()}\n{front}---\n{body}")
        if days_old is not None:
            when = (datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()
            os.utime(path, (when, when))
        return path

    def test_accessor_reads_dates_datetimes_and_survives_junk(self):
        path = self.raw("note", "n1", "")
        doc = lambda value: type("D", (), {"meta": {"updated": value}, "path": path})()
        utc = timezone.utc
        self.assertEqual(work.updated_at(doc("2026-10-16T14:02:00Z")), datetime(2026, 10, 16, 14, 2, tzinfo=utc))
        self.assertEqual(work.updated_at(doc("2026-10-16")), datetime(2026, 10, 16, tzinfo=utc))
        self.assertEqual(work.updated_at(doc("2026-10-16T16:02:00+02:00")), datetime(2026, 10, 16, 14, 2, tzinfo=utc))
        self.assertEqual(work.updated_at(doc(datetime(2026, 10, 16, 1, 2))), datetime(2026, 10, 16, 1, 2, tzinfo=utc))
        mtime = datetime.fromtimestamp(path.stat().st_mtime, utc)
        for junk in (["broken"], [], {}, 7, None, "", "yesterday", "9999-99-99", True):
            self.assertEqual(work.updated_at(doc(junk)), mtime, junk)
        self.assertEqual(work.updated_iso(doc(["broken"])), mtime.strftime("%Y-%m-%dT%H:%M:%SZ"))
        gone = type("D", (), {"meta": {}, "path": self.tmp / "missing.md"})()
        self.assertEqual(work.updated_at(gone), datetime.fromtimestamp(0, utc))

    def test_malformed_updated_beside_a_normal_one_breaks_neither_index_nor_list(self):
        self.raw("workspace", "broken", f"updated: [broken]\nrepos: [{self.repo_a}]\n", days_old=3)
        work.create("workspace", "Normal", meta={"repos": [str(self.repo_a)]})
        block = self.block()
        self.assertEqual([c["id"] for c in block["candidates"]], ["normal", "broken"])  # the broken one ranks by its file time
        value = json.loads(self.cli("index", "--no-write"))
        self.assertEqual({c["id"] for c in value["work"]["candidates"]}, {"normal", "broken"})
        self.assertNotIn("work_error", value)
        listed = json.loads(self.cli("work", "list"))
        self.assertEqual([w["id"] for w in listed], ["normal", "broken"])
        self.assertRegex(next(w["updated"] for w in listed if w["id"] == "broken"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_malformed_updated_does_not_break_decisions_or_note_lists(self):
        self.raw("workspace", "broken", "updated: [broken]\n", body="## Open decisions\n### broken/D1 · high · open\nQuestion: q\n")
        work.create("workspace", "Normal")
        add_decision("normal", "Which?")
        self.assertEqual({r["id"] for r in work.open_decisions()}, {"broken/D1", "normal/D1"})
        self.raw("note", "bad-note", "updated: {oops}\n", days_old=1)
        work.create("note", "Fine note")
        self.assertEqual({n["id"] for n in self.block()["notes"]["recent"]}, {"bad-note", "fine-note"})
        json.loads(self.cli("work", "list", "--kind", "note"))

    def test_missing_updated_with_an_old_file_is_not_a_fresh_candidate(self):
        self.raw("workspace", "undated-old", "", days_old=2000)
        self.assertEqual(self.block(str(self.repo_b))["candidates"], [])
        self.assertEqual(self.block(str(self.repo_b))["others"], 1)

    def test_missing_updated_with_a_recent_file_is_a_candidate(self):
        self.raw("workspace", "undated-new", "", days_old=1)
        self.assertEqual([c["id"] for c in self.block(str(self.repo_b))["candidates"]], ["undated-new"])

    def test_missing_updated_with_yesterdays_file_appears_in_recent_notes(self):
        self.raw("note", "undated-yesterday", "", days_old=1)
        self.raw("note", "undated-ancient", "", days_old=400)
        self.assertEqual([n["id"] for n in self.block()["notes"]["recent"]], ["undated-yesterday"])

    def test_a_missing_updated_is_shown_as_the_file_time_in_the_summary(self):
        self.raw("workspace", "undated", f"repos: [{self.repo_a}]\n", days_old=5)
        shown = self.block()["candidates"][0]["updated"]
        self.assertEqual(shown[:10], (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d"))


class CliIndexTests(IndexBase):
    def read(self):
        return json.loads((self.state / "session-index.json").read_text())

    def test_no_workspace_index_has_no_work_key_and_the_store_is_not_created(self):
        self.cli("index", "--no-tick")
        value = self.read()
        self.assertEqual(set(value), INDEX_KEYS)
        self.assertFalse(self.store.exists())
        # same text as before this feature: only the known keys, written as indent-2 JSON plus a newline
        self.assertEqual((self.state / "session-index.json").read_text(), json.dumps(value, indent=2) + "\n")

    def test_a_new_run_in_the_same_workdir_shows_the_workspace_without_the_old_objective(self):
        work.create("workspace", "Loader", meta={"repos": [str(self.repo_a)]}, body="## Objective\nOLD-OBJECTIVE-TEXT\n")
        self.cli("index", "--no-tick")
        value = self.read()
        self.assertEqual(value["objective"], "")
        self.assertEqual([c["id"] for c in value["work"]["candidates"]], ["loader"])
        self.assertNotIn("OLD-OBJECTIVE-TEXT", json.dumps(value))

    def test_an_unrelated_workdir_with_no_workspaces_shows_nothing(self):
        self.cli("index", "--no-tick", env={"TP_WORKDIR": str(self.repo_b)})
        self.assertNotIn("work", self.read())

    def test_no_write_prints_the_index_and_leaves_session_index_json_untouched(self):
        work.create("workspace", "Loader", meta={"repos": [str(self.repo_a)]})
        self.cli("index", "--no-tick")
        path = self.state / "session-index.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        os.utime(path, ns=(before[1] - 5_000_000_000, before[1] - 5_000_000_000))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        work.create("workspace", "Added later", meta={"repos": [str(self.repo_a)]})
        value = json.loads(self.cli("index", "--no-write"))
        self.assertEqual(len(value["work"]["candidates"]), 2)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_the_injection_reader_calls_index_no_write_and_that_leaves_the_file_alone(self):
        """The Claude plugin module's startup injection is one `readIndex` call (the compaction one is the SessionStart command hook, which
        runs `tp-agent index --no-write --hook-json`); the file it reads must not change."""
        source = (ROOT / "claude" / "plugins" / "tmux-pi" / "hooks" / "register.tsx").read_text()
        called = [m for m in re.findall(r"runTpAgent\(\$, \[([^\]]*)\]\)", source) if "index" in m]
        self.assertEqual(called, ["'index', '--no-write'"])
        argv = [a.strip("' ") for a in called[0].split(",")]
        self.cli("index", "--no-tick")
        path = self.state / "session-index.json"
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        work.create("workspace", "Added later", meta={"repos": [str(self.repo_a)]})
        self.cli(*argv)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_no_write_does_not_create_session_index_json(self):
        self.cli("index", "--no-write")
        self.assertFalse((self.state / "session-index.json").exists())

    def test_bounded_index_replaces_the_list_with_a_short_summary_and_the_file_keeps_the_list(self):
        for w in range(30):
            work.create("workspace", f"Ws {w:03d}", wanted=f"ws-{w:03d}")
            for d in range(10):
                add_decision(f"ws-{w:03d}", f"{SECRET_QUESTION} {w}/{d}", blocking=(d == 0 and w < 4))
        self.settings(inject_decisions_chars=500)
        out = self.cli("index", "--no-tick", "--bounded")
        decisions = json.loads(out)["work"]["decisions"]
        self.assertEqual(set(decisions), {"open", "blocking", "summary"})
        self.assertEqual((decisions["open"], decisions["blocking"]), (300, 4))
        self.assertLessEqual(len(decisions["summary"]), 500)
        self.assertTrue(decisions["summary"].startswith("decisions: 300 open (4 blocking)\n- ws-000/D1 [normal] blocking"))
        self.assertIn("more (0 blocking): tp-agent work decisions", decisions["summary"])
        self.assertNotIn(SECRET_QUESTION, out)
        self.assertLess(len(out), 6000)  # the whole answer Pi's task_index returns stays small with 300 open
        self.assertEqual(len(self.read()["work"]["decisions"]["items"]), 300)  # session-index.json is uncapped
        self.assertLess(len(json.loads(self.cli("index", "--no-write", "--bounded"))["work"]["decisions"]["summary"]), 501)

    def test_bounded_without_a_work_block_is_the_plain_index(self):
        a = json.loads(self.cli("index", "--no-write", "--bounded"))
        self.assertEqual(set(a), INDEX_KEYS)


class ObjectiveAtomicity(IndexBase):
    def load_agent(self):
        loader = SourceFileLoader("tp_agent_under_test", str(ROOT / "bin/tp-agent"))
        agent = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(agent)
        out = contextlib.redirect_stdout(io.StringIO())
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        return agent

    def test_objective_is_replaced_whole_and_a_failed_replace_keeps_the_old_one(self):
        agent = self.load_agent()
        path = self.state / "session-objective.md"
        path.write_text("old objective\n")
        with mock.patch.object(sys, "argv", ["tp-agent", "objective", "  new objective  "]):
            agent.main()
        self.assertEqual(path.read_text(), "new objective\n")
        with mock.patch.object(sys, "argv", ["tp-agent", "objective", "lost objective"]), \
                mock.patch.object(agent.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                agent.main()
        self.assertEqual(path.read_text(), "new objective\n")  # not truncated, not half-written
        self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.name.startswith(".")), [])  # no temp file left behind

    def test_objective_write_goes_through_one_rename_in_the_same_directory(self):
        agent = self.load_agent()
        calls = []
        real = os.replace

        def spy(src, dst):
            calls.append((Path(src).parent, Path(dst)))
            return real(src, dst)

        with mock.patch.object(sys, "argv", ["tp-agent", "objective", "spied"]), mock.patch.object(agent.os, "replace", side_effect=spy):
            agent.main()
        self.assertEqual(calls[0], (self.state, self.state / "session-objective.md"))  # the objective is its own single rename


class MalformedDocumentTests(IndexBase):
    """Per-document isolation: in every loop over store documents (index, `work list`, `work decisions`, `find`, the duplicate
    check) one document that cannot be processed is skipped and counted, and every valid workspace, note and decision still shows."""

    NUL = 'repos: ["/broken\\u0000repo"]\n'
    # name -> (kind, frontmatter lines, body)
    BROKEN = {
        "nul-repos": ("workspace", NUL, "## Objective\nx\n"),
        "scalar-repos": ("workspace", "repos: just-a-string\n", "## Objective\nx\n"),
        "list-title": ("workspace", "title: [a, b]\n", "## Objective\nx\n"),
        "broken-updated": ("workspace", "updated: [broken]\nstatus: active\n", "## Objective\nx\n"),
        "bad-decisions": ("workspace", "", "## Open decisions\n### nul-x/D1 · weird\nQuestion\n### \u0000/D · ·\nQuestion: \x00\n"),
        "nul-body": ("workspace", "", "## Sessions\n- run \x00 tasks: \x00\n## Next actions\n- [ ] \x00\n"),
        "note-nul-links": ("note", 'workspaces: ["a\\u0000b"]\nimportant: [x]\n', "Body.\n"),
        "note-list-title": ("note", "title: [a, b]\nupdated: {oops}\n", "Body.\n"),
    }

    def setUp(self):
        super().setUp()
        work.create("workspace", "Good workspace", "good", {"repos": [str(self.repo_a)]}, "## Objective\ngoodterm lives here\n")
        add_decision("good", "Open question goodterm?", "high", True)
        work.create("note", "Good note", "good-note", {"important": True}, "goodterm note body\n")
        for name, (kind, front, body) in self.BROKEN.items():
            folder = self.store / work.KINDS[kind]
            (folder / f"{name}.md").write_text(f"---\nid: {name}\ntitle: {name}\n{front}---\n{body}", encoding="utf-8")
        (self.store / "workspaces" / "binary.md").write_bytes(b"\xff\xfe\x00---\n\xc3\x28")

    def run_cli(self, *args):
        env = {**os.environ, **self.env}
        env.pop("TMUX", None)
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env, text=True, capture_output=True, timeout=60)

    def test_the_index_keeps_every_valid_item_and_counts_the_unreadable_one(self):
        run = self.run_cli("index", "--no-write")
        self.assertEqual(run.returncode, 0, run.stderr)
        value = json.loads(run.stdout)
        self.assertNotIn("work_error", value)
        block = value["work"]
        self.assertIn("good", [c["id"] for c in block["candidates"]])
        self.assertIn("good/D1", [i["id"] for i in block["decisions"]["items"]])
        self.assertIn("good-note", [n["id"] for n in block["notes"]["important"]])
        self.assertEqual(block["skipped"], {"count": block["skipped"]["count"]})  # the NUL repo cannot be resolved: the document is skipped, not the index
        self.assertGreaterEqual(block["skipped"]["count"], 1)
        self.assertNotIn("nul-repos", run.stdout)  # a count only: no id or file name

    def test_each_malformed_document_alone_leaves_the_valid_ones(self):
        base = {"nul-repos", "scalar-repos", "list-title", "broken-updated", "bad-decisions", "nul-body", "note-nul-links", "note-list-title", "binary"}
        for name in sorted(base):
            keep = {n for n in base if n != name}
            for path in (*(self.store / "workspaces").glob("*.md"), *(self.store / "notes").glob("*.md")):
                if path.stem in keep:
                    path.rename(path.with_suffix(".off"))
            with self.subTest(document=name):
                block = self.block()
                self.assertIn("good/D1", [i["id"] for i in block["decisions"]["items"]])
                self.assertIn("good-note", [n["id"] for n in block["notes"]["important"]])
                self.assertIn("good", [c["id"] for c in block["candidates"]])
            for path in (*(self.store / "workspaces").glob("*.off"), *(self.store / "notes").glob("*.off")):
                path.rename(path.with_suffix(".md"))

    def test_work_list_and_decisions_return_the_valid_items(self):
        listed = json.loads(self.run_cli("work", "list").stdout)
        self.assertIn("good", [w["id"] for w in listed])
        notes = json.loads(self.run_cli("work", "list", "--kind", "note").stdout)
        self.assertIn("good-note", [n["id"] for n in notes])
        run = self.run_cli("work", "decisions", "--json")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("good/D1", [d["id"] for d in json.loads(run.stdout)])
        self.assertIn("good/D1", self.run_cli("work", "decisions").stdout)

    def test_find_returns_the_valid_items(self):
        run = self.run_cli("find", "goodterm", "--json", "--limit", "50")
        self.assertEqual(run.returncode, 0, run.stderr)
        found = {(r["kind"], r["id"]) for r in json.loads(run.stdout)["rows"]}
        self.assertTrue({("workspace", "good"), ("note", "good-note"), ("decision", "good/D1")} <= found, found)

    def test_a_note_add_still_warns_about_a_valid_duplicate(self):
        run = self.run_cli("work", "note", "add", "Good note")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("similar note good-note", run.stderr)

    # A document that parses but then fails while it is processed: forced per surface, since the parser is deliberately tolerant.

    def fail_for(self, *names):
        real = work.updated_at

        def updated_at(doc):
            if doc.id in names:
                raise ValueError("boom")
            return real(doc)

        patch = mock.patch.object(work, "updated_at", updated_at)
        patch.start()
        self.addCleanup(patch.stop)

    def valid_pair(self):
        work.create("workspace", "Doomed", "doomed", {"repos": [str(self.repo_a)]})
        add_decision("doomed", "Doomed question?")
        work.create("note", "Doomed note", "doomed-note", {}, "goodterm too\n")
        self.fail_for("doomed", "doomed-note")

    def test_a_failing_document_is_skipped_and_counted_in_the_index(self):
        before = self.block()["skipped"]["count"]
        self.valid_pair()
        block = self.block()
        self.assertEqual(block["skipped"], {"count": before + 2})  # counts only, never names
        self.assertEqual([c["id"] for c in block["candidates"]], ["good"])
        self.assertEqual([i["id"] for i in block["decisions"]["items"]], ["good/D1"])
        self.assertIn("good-note", [n["id"] for n in block["notes"]["important"]])

    def test_a_failing_document_is_skipped_by_list_decisions_and_find(self):
        import work_commands
        import work_find
        self.valid_pair()
        for kind, good, bad in (("workspace", "good", "doomed"), ("note", "good-note", "doomed-note")):
            err = io.StringIO()
            args = type("A", (), {"work_command": "list", "kind": kind, "status": None, "all": False})()
            with contextlib.redirect_stderr(err):
                rows = work_commands._read_command(args)
            self.assertIn(good, [r["id"] for r in rows])
            self.assertNotIn(bad, [r["id"] for r in rows])
            self.assertIn(f"{work.KINDS[kind]}/{bad}.md", err.getvalue())
        skipped = []
        rows = work.open_decisions(skipped=skipped)
        self.assertEqual([r["id"] for r in rows], ["good/D1"])
        self.assertEqual(work.skipped_files(skipped), ["workspaces/doomed.md"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            text = work_commands._read_command(type("A", (), {"work_command": "decisions", "all": False})())
        self.assertIn("good/D1", text)
        self.assertIn("doomed", err.getvalue())
        incomplete = []
        found, _ = work_find.search(["goodterm"], everything=False, limit=50, incomplete=incomplete)
        self.assertIn(("workspace", "good"), [(r["kind"], r["id"]) for r in found])
        self.assertIn(("note", "good-note"), [(r["kind"], r["id"]) for r in found])
        self.assertNotIn("doomed", [r["id"] for r in found])
        self.assertTrue(any("unreadable work files" in line and "doomed" in line for line in incomplete), incomplete)

    def test_a_failing_note_does_not_stop_the_duplicate_check(self):
        import work_find
        (self.store / "notes" / "boom.md").write_text("---\nid: boom\ntitle: BOOM\n---\nx\n")
        real = work.slugify

        def slugify(title):
            if title == "BOOM":
                raise ValueError("boom")
            return real(title)

        unreadable = []
        with mock.patch.object(work, "slugify", slugify):
            found = work_find.near_duplicates("Good note", skipped=unreadable)
        self.assertEqual([d["id"] for d in found], ["good-note"])
        self.assertEqual(work.skipped_files(unreadable), ["notes/boom.md"])  # counted, not silent

    def test_only_unreadable_documents_still_report_them_and_a_clean_store_reports_none(self):
        for path in (*(self.store / "workspaces").glob("*.md"), *(self.store / "notes").glob("*.md")):
            if path.stem not in {"binary", "nul-repos"}:
                path.unlink()
        block = self.block()
        self.assertIsNotNone(block)
        for path in (self.store / "workspaces").glob("*.md"):
            path.unlink()
        self.assertIsNone(self.block())
        work.create("workspace", "Clean", "clean")
        self.assertNotIn("skipped", self.block())


    # F1: the index never names a skipped file

    def secret_name(self):
        return "AKIA" + "ABCDEFGHIJKLMNOP"  # credential-shaped, built at runtime so no literal sits in the source

    def test_the_index_never_carries_a_skipped_file_name(self):
        name = self.secret_name()
        (self.store / "workspaces" / f"{name}.md").write_text(f'---\nid: {name}\ntitle: t\nrepos: ["/broken\\u0000repo"]\n---\nx\n', encoding="utf-8")
        for args in (("index", "--no-write"), ("index", "--no-write", "--bounded")):
            run = self.run_cli(*args)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertNotIn(name, run.stdout)
            self.assertGreaterEqual(json.loads(run.stdout)["work"]["skipped"]["count"], 1)
            self.assertNotIn("ids", json.loads(run.stdout)["work"]["skipped"])
        run = self.run_cli("index", "--no-tick")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn(name, run.stdout)
        written = (self.state / "session-index.json").read_text()
        self.assertNotIn(name, written)
        self.assertNotIn("ids", json.loads(written)["work"]["skipped"])

    # F2: the duplicate check says when it was incomplete

    def test_note_add_reports_an_unreadable_note_and_still_creates_the_note(self):
        locked = self.store / "notes" / "locked.md"
        locked.write_text("---\nid: locked\ntitle: Duplicate\n---\nx\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if os.access(locked, os.R_OK):
            self.skipTest("file permissions are not enforced here")
        run = self.run_cli("work", "note", "add", "Duplicate", "--body", "b")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("duplicate check incomplete: 1 unreadable notes skipped", run.stderr)
        self.assertNotIn("locked", run.stderr)
        value = json.loads(run.stdout)
        self.assertEqual(value["near_duplicates_incomplete"], 1)
        self.assertTrue((self.store / "notes" / "duplicate.md").exists())

    def test_note_add_with_a_readable_store_has_no_incomplete_marker(self):
        run = self.run_cli("work", "note", "add", "Brand new", "--body", "b")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("incomplete", run.stderr)
        self.assertNotIn("near_duplicates_incomplete", json.loads(run.stdout))

    # F3: skipped files are counted by full path

    def test_two_unreadable_files_with_the_same_stem_count_as_two(self):
        before = self.block()["skipped"]["count"]
        (self.store / "workspaces" / "bad.md").write_text('---\nid: bad\ntitle: bad\nrepos: ["/broken\\u0000repo"]\n---\nx\n', encoding="utf-8")
        (self.store / "notes" / "bad.md").write_text("---\nid: bad\ntitle: bad\n---\nx\n")
        real = work._read

        def read(kind, path, in_archive):
            if kind == "note" and path.stem == "bad":
                raise PermissionError("denied")
            return real(kind, path, in_archive)

        with mock.patch.object(work, "_read", read):
            self.assertEqual(self.block()["skipped"]["count"], before + 2)  # one id, two files
            files = []
            list(work.iter_docs("note", skipped=files))
            work.note_skipped(files, self.store / "workspaces" / "bad.md")
        self.assertEqual(work.skipped_count(files), 2)
        self.assertEqual(work.skipped_files(files), ["notes/bad.md", "workspaces/bad.md"])


if __name__ == "__main__":
    unittest.main()
