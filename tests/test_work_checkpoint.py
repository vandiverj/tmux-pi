"""Checkpoints, recovery and the compaction marker (Phase C): lib/work_checkpoint.py, `tp-agent work checkpoint|recover|compacted`,
the checkpoint entry of the index, the PreCompact hook script and the tp-exit call. Every store and state directory is a temp
directory; nothing here touches the real user-level work store."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402
import work_checkpoint as cp  # noqa: E402
import work_index  # noqa: E402

HOOK = ROOT / "claude" / "plugins" / "tmux-pi" / "hooks" / "precompact.sh"


def decision(ws, number, question, status="open", blocking=False, **fields):
    entry = {"id": f"{ws}/D{number}", "priority": "normal", "status": status, "blocking": blocking, "question": question,
             "background": "", "options": "", "recommended": "", "affects": ["workspace"], "resolution": "", "by": "", "at": "",
             "quote": "", "withdrawn": "", "_dirty": True}
    entry.update(fields)
    return entry


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.store = self.tmp / "work"
        self.state = self.tmp / "project" / ".tmux-pi" / "run-one"
        (self.state / "tasks").mkdir(parents=True)
        config = self.tmp / "work-config.json"
        config.write_text("{}")
        self.config = config
        self.env = {"TP_WORK_DIR": str(self.store), "TP_WORK_CONFIG": str(config), "TP_STATE_DIR": str(self.state)}
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)

    def settings(self, **values):
        self.config.write_text(json.dumps(values))

    def workspace(self, ident="demo", decisions=(), blockers=(), nexts=(), links=(), decided=(), objective="Ship the thing"):
        (self.store / "workspaces" / f"{ident}.md").unlink(missing_ok=True)  # a test may rebuild the same workspace
        doc = work.create("workspace", ident, ident, {}, "")
        doc.body = work.set_section(doc.body, "Objective", objective)
        for section, lines in (("Decisions", decided), ("Blockers / questions", blockers), ("Next actions", nexts), ("Links", links)):
            for line in lines:
                doc.body = work.append_line(doc.body, section, line)
        if decisions:
            work.put_decisions(doc, list(decisions))
        doc.save()
        return work.load("workspace", ident)

    def attach(self, *ids):
        (self.state / "session-work.json").write_text(json.dumps({"attached": list(ids)}))

    def files(self, ident="demo"):
        return sorted(p.name for p in (self.store / "checkpoints" / ident).glob("*") if p.is_file())

    def cli(self, *args, check=True, env=None, stdin=None):
        full = {**os.environ, **self.env, **(env or {})}
        full.pop("TMUX", None)
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=full, text=True, capture_output=True, timeout=60, input=stdin)
        if check:
            self.assertEqual(run.returncode, 0, run.stderr)
        return run


class Snapshot(Base):
    def full(self):
        return self.workspace(
            decided=["- 2026-10-01 Use sqlite", "- 2026-10-02 Keep two reviewers"],
            blockers=["- [ ] need API key", "- [x] old blocker"], nexts=["- [ ] write tests", "- [x] write docs"], links=["- notes: n1"],
            decisions=[decision("demo", 1, "Which db?", blocking=True, background="two candidates", options="a | b",
                                recommended="a", affects=["task:t1", "lane:x"]),
                       decision("demo", 2, "Which colour?", status="answered", resolution="blue", by="user", at="2026-10-03T00:00:00Z")])

    def test_it_copies_every_judgement_item_whole_and_validates(self):
        doc = self.full()
        body = cp.snapshot(doc, "run-one", "manual", [{"id": "t1", "status": "working", "result_path": None}], "short summary")
        self.assertEqual(body["state"]["decisions"], ["- 2026-10-01 Use sqlite", "- 2026-10-02 Keep two reviewers"])
        first = body["state"]["open_decisions"][0]
        self.assertEqual((first["id"], first["blocking"], first["options"], first["affects"]), ("demo/D1", True, "a | b", ["task:t1", "lane:x"]))
        self.assertEqual(body["state"]["open_decisions"][1]["resolution"], "blue")
        self.assertEqual(body["counts"], {"decisions": 2, "open_decisions": 2, "blockers": 2, "next_actions": 2, "sources": 1})
        self.assertIs(body["truncated"], False)
        self.assertEqual(body["summary"], {"authored_by": "agent", "text": "short summary"})
        self.assertEqual(cp.check(cp.seal(body), "demo"), "")

    def test_a_changed_body_fails_its_own_sha256(self):
        sealed = cp.seal(cp.snapshot(self.full(), "r", "manual"))
        sealed["state"]["decisions"].append("- injected")
        self.assertIn("sha256", cp.check(sealed))
        sealed = cp.seal(cp.snapshot(self.full(), "r", "manual"))
        self.assertIn("another workspace", cp.check(sealed, "other"))
        sealed["schema"] = "nope"
        self.assertEqual(cp.check(sealed), "unknown schema")
        self.assertEqual(cp.check([]), "not a JSON object")

    def test_counts_must_agree_with_what_was_copied(self):
        body = cp.snapshot(self.full(), "r", "manual")
        body["counts"]["blockers"] = 5  # a sealed lie: the sha matches but the copy is short
        self.assertIn("blockers", cp.check(cp.seal(body)))

    def test_summary_text_is_bounded_and_empty_means_no_author(self):
        body = cp.snapshot(self.full(), "r", "manual", summary="x" * 5000)
        self.assertEqual(len(body["summary"]["text"]), cp.SUMMARY_CHARS)
        self.assertEqual(cp.snapshot(self.full(), "r", "manual")["summary"], {"authored_by": None, "text": ""})


class Truncation(Base):
    def big(self, decisions=30, nexts=60):
        return self.workspace(
            decided=[f"- 2026-10-01 decided thing number {i} " + "w" * 80 for i in range(decisions)],
            nexts=[f"- [ ] next action number {i} " + "n" * 80 for i in range(nexts)],
            blockers=[f"- [ ] blocker {i} " + "b" * 80 for i in range(10)], links=[f"- link {i}" for i in range(10)],
            decisions=[decision("demo", i + 1, f"Question {i}?", blocking=(i == 0), status="open" if i < 3 else "answered", resolution="r")
                       for i in range(6)])

    def test_over_the_bound_whole_items_go_and_the_rest_says_so(self):
        doc = self.big()
        for limit in (20000, 9000, 4000, 1500):
            body = cp.snapshot(doc, "r", "manual", limit=limit)
            sealed = cp.seal(body)
            self.assertLessEqual(len(cp._dump(sealed).encode()), limit, limit)
            self.assertEqual(cp.check(sealed, "demo"), "", limit)
            self.assertEqual(body["counts"]["decisions"], 30)  # what the workspace held is still said
            for key, left_out in (body["truncated"] or {"sections": {}})["sections"].items():
                if key not in ("objective", "tasks", "summary"):
                    self.assertEqual(len(body["state"][key]) + left_out, body["counts"][key])
        self.assertIs(cp.snapshot(doc, "r", "manual", limit=10 ** 7)["truncated"], False)

    def test_the_cut_keeps_open_decisions_ahead_of_notes_and_closed_ones_go_first(self):
        body = cp.snapshot(self.big(), "r", "manual", limit=6000)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["truncated"]["sections"].get("sources"), 10)  # least valuable first
        kept = body["state"]["open_decisions"]
        if len(kept) < 6:
            self.assertTrue(all(e["status"] == "open" for e in kept[:3]) and all(e["id"] for e in kept))
            self.assertEqual([e["id"] for e in kept[:3]], ["demo/D1", "demo/D2", "demo/D3"])

    def test_sources_are_cut_oldest_first_so_the_newest_is_kept(self):
        doc = self.workspace(links=["- oldest " + "o" * 200, "- newest " + "n" * 200])
        full = len(cp._dump(cp.seal(cp.snapshot(doc, "r", "manual"))).encode())
        for limit in range(full, 0, -1):  # the first bound that forces exactly one source out
            body = cp.snapshot(doc, "r", "manual", limit=limit)
            if body["truncated"] and body["truncated"]["sections"].get("sources") == 1:
                break
        else:
            self.fail("no bound cut exactly one source")
        self.assertEqual(body["truncated"]["sections"]["sources"], 1)
        self.assertEqual(len(body["state"]["sources"]), 1)
        self.assertIn("newest", body["state"]["sources"][0])
        self.assertNotIn("oldest", json.dumps(body["state"]))

    def test_a_truncated_recovery_is_labelled_and_never_complete(self):
        doc = self.big()
        self.settings(checkpoint_max_bytes=3000)
        cp.write(doc, "r", "manual")
        found = cp.latest("demo")["valid"][1]
        self.assertTrue(found["truncated"])
        text = cp.format_recover(cp.recover("demo"))
        self.assertIn(cp.TRUNCATED_LABEL, text)
        self.assertNotIn("complete", text.lower().replace("incomplete", ""))
        self.assertIn(cp.TRUNCATED_LABEL, cp.status("demo")["line"])


class SizeBound(Base):
    def tasks(self, n, path_chars=100):
        return [{"id": f"coder-{i:05d}", "status": "settled", "result_path": "/r/" + "p" * path_chars + str(i)} for i in range(n)]

    def test_the_written_size_is_at_or_under_the_limit_for_large_task_lists(self):
        doc = self.workspace(decided=["- 2026-10-01 kept"])
        for limit in (1500, 3000, 20000):
            self.settings(checkpoint_max_bytes=limit)
            path, value = cp.write(doc, "r", "manual", self.tasks(500))
            self.assertLessEqual(path.stat().st_size, limit, limit)
            self.assertEqual(cp.read_checkpoint(path)[1], "")
            cut = value["truncated"]["sections"]["tasks"]
            self.assertEqual(len(value["tasks"]) + cut, 500)

    def test_task_rows_go_oldest_first_and_each_cut_section_is_recorded(self):
        doc = self.workspace(decided=["- 2026-10-01 kept"], nexts=["- [ ] n"])
        rows = self.tasks(200)
        body = cp.snapshot(doc, "r", "manual", rows, "an agent summary " * 20, limit=6000)
        kept = [t["id"] for t in body["tasks"]]
        self.assertEqual(kept, [t["id"] for t in rows][len(rows) - len(kept):])  # the newest rows survive, in order
        self.assertEqual(body["truncated"]["sections"]["tasks"], 200 - len(kept))
        self.assertEqual(body["truncated"]["bytes_limit"], 6000)
        # the judgement state is the last thing to go: it is whole while tasks were cut
        self.assertEqual(body["state"]["decisions"], ["- 2026-10-01 kept"])
        self.assertLessEqual(cp._size(body), 6000)

    def test_the_summary_is_cut_whole_after_the_tasks_and_says_so(self):
        doc = self.workspace(decided=["- 2026-10-01 kept"])
        base = cp._size(cp.snapshot(doc, "r", "manual"))
        body = cp.snapshot(doc, "r", "manual", [], "s" * 1400, limit=base + 600)
        self.assertEqual(body["summary"], {"authored_by": None, "text": ""})
        self.assertEqual(body["truncated"]["sections"], {"summary": 1})
        self.assertEqual(cp.check(cp.seal(body), "demo"), "")

    def test_a_cut_keeps_the_most_whole_items_that_fit(self):
        doc = self.workspace(decided=[f"- 2026-10-0{i} decided {i} " + "d" * (40 + 9 * i) for i in range(1, 6)], objective="")
        full = cp.snapshot(doc, "r", "manual", limit=10 ** 7)
        lines = full["state"]["decisions"]

        def size_keeping(k, limit):
            state = {**full["state"], "decisions": lines[len(lines) - k:]}
            return cp._size({**full, "state": state, "truncated": {"sections": {"decisions": len(lines) - k}, "bytes_limit": limit}})

        kept_seen = set()
        for limit in range(size_keeping(2, 0) - 5, cp._size(full) + 5):
            body = cp.snapshot(doc, "r", "manual", limit=limit)
            expected = max((k for k in range(len(lines) + 1) if size_keeping(k, limit) <= limit), default=None)
            if cp._size(full) <= limit:
                expected = len(lines)
            if expected is None or expected < 2:
                continue
            self.assertEqual(len(body["state"]["decisions"]), expected, limit)
            self.assertEqual(body["state"]["decisions"], lines[len(lines) - expected:], limit)  # the newest are kept
            self.assertLessEqual(cp._size(body), limit)
            kept_seen.add(expected)
        self.assertTrue({3, 4, 5} <= kept_seen, kept_seen)

    def test_a_limit_below_the_smallest_checkpoint_is_an_error_and_writes_nothing(self):
        doc = self.workspace()
        self.settings(checkpoint_max_bytes=100)
        with self.assertRaises(work.WorkError) as raised:
            cp.write(doc, "r", "manual", self.tasks(3))
        self.assertIn("checkpoint_max_bytes", str(raised.exception))
        self.assertEqual(self.files(), [])


class Writing(Base):
    def test_a_written_checkpoint_is_private_validated_and_leaves_no_temp_file(self):
        doc = self.workspace()
        path, value = cp.write(doc, "run-one", "manual")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(path.parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(json.loads(path.read_text())["sha256"], value["sha256"])
        self.assertEqual(cp.read_checkpoint(path)[1], "")
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name])

    def test_sequence_numbers_grow_and_old_files_are_pruned(self):
        self.settings(checkpoints_keep=2)
        doc = self.workspace()
        for _ in range(4):
            cp.write(doc, "r", "manual")
        names = self.files()
        self.assertEqual(len(names), 2)
        self.assertEqual([n.split("-")[2] for n in names], ["0003.json", "0004.json"])

    def test_pruning_counts_valid_checkpoints_and_never_removes_the_last_valid_one(self):
        self.settings(checkpoints_keep=2)
        doc = self.workspace()
        for _ in range(3):
            cp.write(doc, "r", "manual")
        folder = self.store / "checkpoints" / "demo"
        # damaged files newer than every valid one must not push the valid ones out
        for seq in (5, 6, 7):
            (folder / f"20261016-000000-{seq:04d}.json").write_text("{ not json")
        cp.write(doc, "r", "manual")  # this is seq 8; the valid ones are 8 and 3 (2 and 1 are beyond the newest two valid)
        names = self.files()
        self.assertIn("0008.json", names[-1])
        valid = [n for n in names if cp.read_checkpoint(folder / n)[0] is not None]
        self.assertEqual([n.split("-")[2] for n in valid], ["0003.json", "0008.json"])
        self.assertFalse(any(n.split("-")[2] in ("0001.json", "0002.json") for n in names))

    def test_a_pruning_pass_with_one_valid_file_keeps_it_whatever_keep_says(self):
        self.settings(checkpoints_keep=1)
        doc = self.workspace()
        cp.write(doc, "r", "manual")
        folder = self.store / "checkpoints" / "demo"
        for seq in (2, 3):
            (folder / f"20261016-000000-{seq:04d}.json").write_text("garbage")
        cp._prune("demo")
        self.assertIsNotNone(cp.latest("demo")["valid"])

    def test_failed_validation_after_writing_is_not_reported_as_written(self):
        doc = self.workspace()
        cp.write(doc, "r", "manual")
        before = self.files()
        with mock.patch.object(cp, "read_checkpoint", return_value=(None, "forced")):
            with self.assertRaises(work.WorkError) as raised:
                cp.write(doc, "r", "manual")
        self.assertIn("validation", str(raised.exception))
        self.assertEqual(self.files(), before)  # the failed file is removed, the earlier one untouched
        self.assertEqual([p for p in (self.store / "checkpoints" / "demo").iterdir() if p.name.startswith(".")], [])

    def test_a_failed_rename_leaves_no_file_and_no_temp(self):
        doc = self.workspace()
        with mock.patch("work_checkpoint.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(work.WorkError):
                cp.write(doc, "r", "manual")
        self.assertEqual(list((self.store / "checkpoints" / "demo").iterdir()), [])

    def test_a_symlinked_checkpoint_directory_is_refused(self):
        doc = self.workspace()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.store / "checkpoints").mkdir(parents=True)
        (self.store / "checkpoints" / "demo").symlink_to(elsewhere)
        with self.assertRaises(work.WorkError):
            cp.write(doc, "r", "manual")
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_unknown_trigger_and_bad_id_are_refused(self):
        with self.assertRaises(work.WorkError):
            cp.write(self.workspace(), "r", "whenever")
        with self.assertRaises(work.WorkError):
            cp.directory("../x")

    def test_wrong_typed_settings_fall_back_to_the_defaults(self):
        self.settings(checkpoints_keep="many", checkpoint_max_bytes=True)
        self.assertEqual((cp._keep(), cp._limit()), (5, 262144))


class Fallback(Base):
    def test_the_newest_valid_checkpoint_wins_over_a_corrupt_newer_one(self):
        doc = self.workspace(decided=["- one"])
        cp.write(doc, "r", "manual")
        newest, _ = cp.write(doc, "r", "manual")
        newest.write_text(newest.read_text()[:40])  # a torn file
        found = cp.latest("demo")
        self.assertTrue(found["newest_failed"])
        self.assertTrue(found["valid"][0].name.endswith("0001.json"))
        status = cp.status("demo")
        self.assertEqual(status["state"], "older-valid")
        self.assertIn("older", status["line"])
        self.assertIn("failed validation", status["line"])

    def test_when_every_file_is_invalid_there_is_no_valid_checkpoint_and_it_says_so(self):
        doc = self.workspace()
        path, _ = cp.write(doc, "r", "manual")
        path.write_text('{"schema": "tp-work-checkpoint:v1"}')
        status = cp.status("demo")
        self.assertEqual(status["state"], "none-valid")
        self.assertIn("none valid", status["line"])
        self.assertIn("none valid", cp.format_recover(cp.recover("demo")))

    def test_a_tampered_checkpoint_is_not_used(self):
        doc = self.workspace(decided=["- one"])
        path, _ = cp.write(doc, "r", "manual")
        value = json.loads(path.read_text())
        value["state"]["decisions"] = ["- forged"]
        path.write_text(json.dumps(value))
        self.assertIsNone(cp.latest("demo")["valid"])

    def test_no_checkpoint_at_all(self):
        doc = self.workspace()
        self.assertEqual(cp.status("demo")["state"], "none")
        self.assertTrue(cp.due("demo"))
        self.assertFalse((self.store / "checkpoints").exists())  # reading creates nothing


class Malformed(Base):
    """A checkpoint with a correct sha256 but a field of the wrong shape is invalid, never an exception, and the previous valid
    checkpoint is used."""
    SHAPES = {
        "truncated sections count is text": lambda b: b.update(truncated={"sections": {"decisions": "not-an-int"}, "bytes_limit": 5}),
        "truncated is a list": lambda b: b.update(truncated=[]),
        "truncated is true": lambda b: b.update(truncated=True),
        "truncated sections is a list": lambda b: b.update(truncated={"sections": [], "bytes_limit": 5}),
        "truncated sections names an unknown part": lambda b: b.update(truncated={"sections": {"nonsense": 1}, "bytes_limit": 5}),
        "truncated sections count is negative": lambda b: b.update(truncated={"sections": {"decisions": -1}, "bytes_limit": 5}),
        "truncated bytes_limit is text": lambda b: b.update(truncated={"sections": {}, "bytes_limit": "x"}),
        "counts is a list": lambda b: b.update(counts=[]),
        "a count is text": lambda b: b["counts"].update(decisions="1"),
        "a count is a bool": lambda b: b["counts"].update(decisions=True),
        "a count is missing": lambda b: b["counts"].pop("sources"),
        "state is a list": lambda b: b.update(state=[]),
        "objective is a number": lambda b: b["state"].update(objective=5),
        "a section is text": lambda b: b["state"].update(decisions="text"),
        "a decision line is a number": lambda b: b["state"]["decisions"].append(1),
        "an open decision is text": lambda b: b["state"]["open_decisions"].append("x"),
        "an open decision field is a list": lambda b: b["state"]["open_decisions"][0].update(question=["q"]),
        "an open decision field is missing": lambda b: b["state"]["open_decisions"][0].pop("resolution"),
        "an open decision affects is text": lambda b: b["state"]["open_decisions"][0].update(affects="a"),
        "an open decision blocking is text": lambda b: b["state"]["open_decisions"][0].update(blocking="yes"),
        "tasks is a dict": lambda b: b.update(tasks={}),
        "a task is text": lambda b: b["tasks"].append("t1"),
        "a task id is a number": lambda b: b["tasks"].append({"id": 1, "status": "x", "result_path": None}),
        "a task status is a list": lambda b: b["tasks"].append({"id": "t", "status": [], "result_path": None}),
        "summary is text": lambda b: b.update(summary="s"),
        "summary text is a number": lambda b: b["summary"].update(text=5),
        "summary author is unknown": lambda b: b["summary"].update(authored_by="model"),
        "at is missing": lambda b: b.pop("at"),
        "at is not a time": lambda b: b.update(at="yesterday"),
        "run is a number": lambda b: b.update(run=1),
        "trigger is a list": lambda b: b.update(trigger=[]),
        "workspace is a number": lambda b: b.update(workspace=1),
        "workspace_updated is null": lambda b: b.update(workspace_updated=None),
        "workspace_sha256 is a number": lambda b: b.update(workspace_sha256=7),
        "state is missing": lambda b: b.pop("state"),
        "schema is another": lambda b: b.update(schema="tp-work-checkpoint:v2"),
    }

    def two(self):
        doc = self.workspace(decided=["- 2026-10-01 kept"], decisions=[decision("demo", 1, "Which db?")])
        old, _ = cp.write(doc, "r", "manual", [{"id": "t1", "status": "working", "result_path": None}])
        new, value = cp.write(doc, "r", "manual", [{"id": "t1", "status": "working", "result_path": None}])
        return doc, old, new, value

    def plant(self, new, mutate):
        body = {k: v for k, v in json.loads(new.read_text()).items() if k != "sha256"}
        mutate(body)
        new.write_text(json.dumps(cp.seal(body)))  # the digest is right; only the shape is wrong

    def test_each_malformed_shape_is_invalid_and_the_older_checkpoint_is_used(self):
        for name, mutate in self.SHAPES.items():
            with self.subTest(name):
                doc, old, new, _ = self.two()
                self.plant(new, mutate)
                value, problem = cp.read_checkpoint(new)
                self.assertIsNone(value)
                self.assertNotEqual(problem, "")
                found = cp.latest("demo")
                self.assertEqual(found["valid"][0], old)
                self.assertTrue(found["newest_failed"])
                info = cp.recover("demo")  # none of these raises
                self.assertEqual(info["checkpoint"]["path"], old)
                self.assertTrue(info["newest_failed"])
                self.assertEqual(cp.status("demo")["state"], "older-valid")
                cp.format_recover(info)
                cp.due("demo")
                shutil.rmtree(self.store / "checkpoints")

    def test_a_malformed_checkpoint_with_nothing_older_means_none_valid_not_a_crash(self):
        doc, old, new, _ = self.two()
        old.unlink()
        self.plant(new, lambda b: b.update(truncated={"sections": {"decisions": "x"}, "bytes_limit": 1}))
        self.assertIsNone(cp.latest("demo")["valid"])
        self.assertEqual(cp.status("demo")["state"], "none-valid")
        self.assertIn("none valid", cp.format_recover(cp.recover("demo")))

    def test_unreadable_json_shapes_are_invalid_too(self):
        doc, old, new, _ = self.two()
        for text in ("[" * 100000, "null", "3", '"s"', "[]", "\x00", ""):
            new.write_text(text)
            value, problem = cp.read_checkpoint(new)
            self.assertIsNone(value, text[:10])
            self.assertEqual(cp.latest("demo")["valid"][0], old)

    def test_check_never_raises_on_arbitrary_values(self):
        for value in (None, 5, "s", [], {}, {"schema": cp.SCHEMA}, {"schema": cp.SCHEMA, "sha256": 5}):
            self.assertNotEqual(cp.check(value, "demo"), "")


class Freshness(Base):
    def test_due_only_when_missing_or_old(self):
        doc = self.workspace()
        cp.write(doc, "r", "manual")
        self.assertFalse(cp.due("demo"))
        with mock.patch.object(cp, "STALE_SECONDS", -1):
            self.assertTrue(cp.due("demo"))

    def test_a_workspace_changed_after_the_checkpoint_is_stale(self):
        doc = self.workspace()
        cp.write(doc, "r", "manual")
        self.assertFalse(cp.status("demo")["stale"])
        changed = work.load("workspace", "demo")
        changed.body = work.append_line(changed.body, "Decisions", "- later")
        changed.save()
        status = cp.status("demo")
        self.assertTrue(status["stale"])
        self.assertIn("stale", status["line"])

    def test_task_diff(self):
        old = [{"id": "a", "status": "working", "result_path": None}, {"id": "b", "status": "working", "result_path": None},
               {"id": "c", "status": "settled", "result_path": "/p"}]
        live = [{"id": "a", "status": "settled", "result_path": "/r/a"}, {"id": "b", "status": "working", "result_path": None},
                {"id": "d", "status": "queued", "result_path": None}]
        self.assertEqual(cp.diff_tasks(old, live), ["a was working, now settled with result path /r/a", "c missing (was settled)", "d is new (queued)"])
        self.assertEqual(cp.diff_tasks(old, old), [])

    def test_the_task_diff_is_only_made_against_a_checkpoint_of_this_run(self):
        doc = self.workspace()
        cp.write(doc, "run-one", "manual", [{"id": "a", "status": "working", "result_path": None}])
        live = [{"id": "a", "status": "settled", "result_path": None}]
        self.assertEqual(cp.recover("demo", live, "run-one")["tasks"], {"known": True, "changes": ["a was working, now settled"], "partial": 0})
        self.assertFalse(cp.recover("demo", live, "run-two")["tasks"]["known"])
        self.assertIsNone(cp.status("demo", "run-two", live)["tasks_changed"])
        self.assertEqual(cp.status("demo", "run-one", live)["tasks_changed"], 1)

    def test_age_text_is_coarse(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 10, tzinfo=timezone.utc)
        at = lambda **k: (now - timedelta(**k)).strftime(work.STAMP)  # noqa: E731
        self.assertEqual([cp.age_text(at(seconds=5), now), cp.age_text(at(minutes=41, seconds=20), now), cp.age_text(at(hours=3), now),
                          cp.age_text(at(days=2), now), cp.age_text("junk", now)], ["<1 min", "41 min", "3h", "2d", "an unknown time"])


class Recover(Base):
    def lost(self):
        doc = self.workspace(decided=["- 2026-10-01 Use sqlite"], blockers=["- [ ] need key"], nexts=["- [ ] a", "- [x] b"], links=["- n1"],
                             decisions=[decision("demo", 1, "Which db?", blocking=True, options="a | b")])
        cp.write(doc, "r", "manual")
        return doc

    def test_a_missing_workspace_file_is_recovered_from_the_checkpoint_and_labelled(self):
        self.lost()
        (self.store / "workspaces" / "demo.md").unlink()
        info = cp.recover("demo")
        self.assertEqual((info["file"], info["source"]), ("file missing", "checkpoint"))
        text = cp.format_recover(info)
        for needle in ("workspace file missing", "recover demo: workspace file missing", "the latest valid checkpoint (the workspace file is unavailable)", "Use sqlite", "demo/D1",
                       "need key", "Options: a | b", "(2, 1 unfinished", "- [x] b"):
            self.assertIn(needle, text)
        self.assertIn("workspace file missing", cp.status("demo")["line"])

    def test_a_corrupt_workspace_file_is_recovered_from_the_checkpoint(self):
        self.lost()
        path = self.store / "workspaces" / "demo.md"
        path.write_text("garbage with no frontmatter")
        info = cp.recover("demo")
        self.assertTrue(info["file"].startswith("corrupt"))
        self.assertEqual(info["source"], "checkpoint")
        self.assertEqual(info["state"]["decisions"], ["- 2026-10-01 Use sqlite"])

    def test_the_live_workspace_is_the_source_when_it_is_readable(self):
        self.lost()
        info = cp.recover("demo")
        self.assertEqual(info["source"], "workspace")
        self.assertEqual(info["shortfall"], {})
        self.assertIn("re-confirm with the user", cp.format_recover(info))

    def test_a_workspace_that_lost_items_since_a_complete_checkpoint_is_flagged(self):
        self.lost()
        doc = work.load("workspace", "demo")
        doc.body = work.set_section(doc.body, "Decisions", "")
        doc.body = work.set_section(doc.body, "Open decisions", "")
        doc.save()
        info = cp.recover("demo")
        self.assertEqual(sorted(info["shortfall"]), ["decisions", "open_decisions"])
        text = cp.format_recover(info)
        self.assertIn("warning: the workspace file holds 0 decisions but the checkpoint copied 1", text)
        self.assertIn("Use sqlite", text)
        self.assertIn("demo/D1", text)

    def test_no_state_anywhere(self):
        info = cp.recover("ghost")
        self.assertIsNone(info["state"])
        self.assertIn("no workspace state to show", cp.format_recover(info))

    def test_recovery_reads_only(self):
        self.lost()
        snapshot = sorted(str(p) for p in self.store.rglob("*"))
        mtimes = {p: p.stat().st_mtime_ns for p in self.store.rglob("*") if p.is_file()}
        cp.recover("demo")
        cp.status("demo")
        cp.latest("demo")
        self.assertEqual(sorted(str(p) for p in self.store.rglob("*")), snapshot)
        self.assertEqual({p: p.stat().st_mtime_ns for p in self.store.rglob("*") if p.is_file()}, mtimes)


class RoundTrip(Base):
    def twelve(self):
        return self.workspace(
            decided=[f"- 2026-10-{i + 1:02d} decided {i} with a long reason that must not be clipped " + "z" * 200 for i in range(12)],
            blockers=[f"- [ ] blocker {i}" for i in range(3)], nexts=[f"- [ ] next {i}" for i in range(4)], links=["- notes: a, b", "- tasks: run/t1"],
            decisions=[decision("demo", i + 1, f"Question number {i}?\nsecond line of {i}", blocking=(i % 4 == 0), priority=("high", "normal", "low")[i % 3],
                                background=f"background {i}\nmore {i}", options=f"opt a{i} | opt b{i}", recommended=f"a{i}",
                                affects=[f"task:t{i}", "workspace"], status="open" if i % 2 == 0 else "answered", resolution=f"resolved {i}",
                                by="user", at="2026-10-04T00:00:00Z", quote=f"quote {i}", _extra=[f"hand-written note {i}"]) for i in range(12)])

    def test_twelve_decisions_and_twelve_open_decisions_survive_a_corrupted_workspace_file(self):
        doc = self.twelve()
        cp.write(doc, "r", "manual")
        (self.store / "workspaces" / "demo.md").write_text("\x00 corrupted \x00")
        info = cp.recover("demo")
        self.assertEqual(info["source"], "checkpoint")
        self.assertEqual(info["counts"], {"decisions": 12, "open_decisions": 12, "blockers": 3, "next_actions": 4, "sources": 2})
        text = cp.format_recover(info)
        for i in range(12):
            self.assertIn(f"decided {i} with a long reason that must not be clipped " + "z" * 200, text)
            self.assertIn(f"demo/D{i + 1} · ", text)
        for i in range(12):  # every entry, open or answered, shows every field
            for needle in (f"second line of {i}", f"Background: background {i}", f"more {i}", f"Options: opt a{i} | opt b{i}", f"Recommended: a{i}",
                           f"Affects: task:t{i} | workspace", f"hand-written note {i}", f"Resolution: resolved {i}", "By: user",
                           "At: 2026-10-04T00:00:00Z", f"Quote: quote {i}"):
                self.assertIn(needle, text)
        kept = cp.latest("demo")["valid"][1]["state"]["open_decisions"]
        self.assertEqual([e["resolution"] for e in kept], [f"resolved {i}" for i in range(12)])
        self.assertEqual([e["quote"] for e in kept], [f"quote {i}" for i in range(12)])

    def test_recover_prints_decisions_with_the_renderer_that_writes_the_workspace_file(self):
        # One renderer: the lines recover prints for the checkpointed entries are what work.render_decisions makes of the
        # workspace's own entries, for an answered and a withdrawn entry as much as an open one.
        doc = self.workspace(decisions=[
            decision("demo", 1, "Which DB?", status="answered", background="My critical context", options="A or B", affects=["task:coder-1"],
                     resolution="B", by="user", at="2026-10-03T00:00:00Z", quote="use B"),
            decision("demo", 2, "Ship?", status="withdrawn", withdrawn="no longer needed", priority="high", blocking=True)])
        cp.write(doc, "r", "manual")
        (self.store / "workspaces" / "demo.md").unlink()
        text = cp.format_recover(cp.recover("demo"))
        entries = cp.latest("demo")["valid"][1]["state"]["open_decisions"]
        for entry in entries:
            for line in cp.entry_lines(entry):
                self.assertIn("  " + line, text)
        for needle in ("Background: My critical context", "Options: A or B", "Affects: task:coder-1", "Resolution: B", "By: user",
                       "At: 2026-10-03T00:00:00Z", "Quote: use B", "Withdrawn: no longer needed", "### demo/D2 · high · withdrawn · blocking"):
            self.assertIn(needle, text)
        parsed = work.parse_decisions(work.render_decisions([{**e, "_extra": [], "_dirty": True} for e in entries]))
        self.assertEqual([(e["id"], e["resolution"], e["quote"], e["withdrawn"]) for e in parsed],
                         [(e["id"], e["resolution"], e["quote"], e["withdrawn"]) for e in entries])

    def test_the_copy_is_verbatim_for_text_that_was_never_a_field(self):
        doc = self.workspace(decisions=[decision("demo", 1, "q", _extra=["a hand-written line"])], decided=["- odd   spacing  kept"])
        body = cp.snapshot(doc, "r", "manual")
        self.assertEqual(body["state"]["open_decisions"][0]["extra"], "a hand-written line")
        self.assertEqual(body["state"]["decisions"], ["- odd   spacing  kept"])

    def test_the_summary_file_is_stored_as_the_agents_own(self):
        self.workspace()
        self.attach("demo")
        note = self.tmp / "summary.txt"
        note.write_text("we are choosing a database\n")
        self.cli("work", "checkpoint", "--summary-file", str(note))
        value = cp.latest("demo")["valid"][1]
        self.assertEqual(value["summary"], {"authored_by": "agent", "text": "we are choosing a database"})
        self.assertIn("with agent summary", cp.status("demo")["line"])
        self.assertIn("agent summary: we are choosing a database", cp.format_recover(cp.recover("demo")))

    def test_no_model_call_and_no_network_is_made(self):
        import socket
        doc = self.twelve()
        refuse = mock.Mock(side_effect=AssertionError("a process or a socket was used"))
        with mock.patch.object(subprocess, "Popen", refuse), mock.patch.object(socket, "socket", refuse), mock.patch.object(socket, "create_connection", refuse):
            cp.write(doc, "r", "manual")
            cp.recover("demo")
            cp.status("demo")
            cp.compacted(self.state)
        refuse.assert_not_called()
        for name in ("socket", "urllib", "http", "subprocess", "anthropic", "requests"):
            self.assertNotIn(f"import {name}", (ROOT / "lib" / "work_checkpoint.py").read_text())


class Replay(Base):
    def test_recover_names_the_tasks_that_changed_since_the_checkpoint(self):
        self.workspace()
        self.attach("demo")
        task = self.state / "tasks" / "coder-1"
        task.mkdir()
        (task / "meta.json").write_text(json.dumps({"id": "coder-1", "role": "coder", "created": "2026-10-16T00:00:00+00:00"}))
        self.cli("work", "checkpoint")
        quiet = self.cli("work", "recover").stdout
        self.assertIn("tasks since the checkpoint: none changed", quiet)
        (task / "result.md").write_text("RESULT_STATUS: complete\n")
        (self.state / "tasks" / "coder-2").mkdir()
        (self.state / "tasks" / "coder-2" / "meta.json").write_text(json.dumps({"id": "coder-2", "role": "coder", "created": "2026-10-16T00:01:00+00:00"}))
        text = self.cli("work", "recover").stdout
        self.assertIn("coder-1 was", text)
        self.assertIn("with result path", text)
        self.assertIn("coder-2 is new", text)
        line = json.loads(self.cli("index", "--no-write").stdout)["work"]["checkpoint"]
        self.assertEqual(line["tasks_changed"], 2)
        self.assertIn("2 tasks changed since", line["line"])

    def test_a_recovery_from_another_run_makes_no_task_claim(self):
        self.workspace()
        self.attach("demo")
        self.cli("work", "checkpoint")
        other = self.tmp / "project" / ".tmux-pi" / "run-two"
        (other / "tasks").mkdir(parents=True)
        (other / "session-work.json").write_text(json.dumps({"attached": ["demo"]}))
        text = self.cli("work", "recover", env={"TP_STATE_DIR": str(other)}).stdout
        self.assertIn("tasks: no comparison", text)


class Marker(Base):
    def test_compacted_sets_the_marker_once_and_never_changes_it(self):
        first = cp.compacted(self.state, "precompact:auto")
        self.assertTrue(first["changed"])
        stored = json.loads((self.state / cp.RECOVERY_FILE).read_text())
        self.assertEqual((stored["pending"], stored["reason"]), (True, "precompact:auto"))
        again = cp.compacted(self.state, "session-start:compact")
        self.assertFalse(again["changed"])
        self.assertEqual(json.loads((self.state / cp.RECOVERY_FILE).read_text()), stored)  # first since and reason stay

    def test_the_marker_needs_no_work_store(self):
        cp.compacted(self.state)
        self.assertFalse(self.store.exists())

    def test_without_a_state_directory_it_is_an_error(self):
        with self.assertRaises(work.WorkError):
            cp.compacted(None)
        with self.assertRaises(work.WorkError):
            cp.compacted(self.tmp / "nope")

    def test_a_long_reason_is_bounded_and_one_line(self):
        cp.compacted(self.state, "a\nb " + "x" * 1000)
        reason = json.loads((self.state / cp.RECOVERY_FILE).read_text())["reason"]
        self.assertNotIn("\n", reason)
        self.assertLessEqual(len(reason), cp.MARKER_REASON_CHARS)

    def test_a_marker_that_is_not_pending_is_set(self):
        (self.state / cp.RECOVERY_FILE).write_text(json.dumps({"pending": False, "since": "x", "reason": "old"}))
        self.assertTrue(cp.compacted(self.state, "again")["changed"])


class Cli(Base):
    def prepared(self):
        self.cli("work", "new", "Demo", "--id", "demo", "--objective", "Ship")
        self.cli("work", "decide", "demo", "Use sqlite")
        self.cli("work", "decision", "add", "demo", "--question", "Which db?", "--blocking")
        self.cli("work", "attach", "demo")

    def test_checkpoint_writes_the_attached_workspaces_and_says_where_it_stands(self):
        self.prepared()
        out = self.cli("work", "checkpoint", "--summary", "halfway").stdout
        name = self.files()[0]
        self.assertEqual(out.strip(), f"checkpoint written: store:checkpoints/demo/{name}")
        found = cp.latest("demo")["valid"][1]
        self.assertEqual((found["trigger"], found["run"], found["summary"]["text"]), ("manual", "run-one", "halfway"))
        self.assertEqual(found["counts"]["open_decisions"], 1)

    def test_the_written_path_keeps_its_store_label_through_the_output_boundary(self):
        # The real directory never appears, the label does, and the line is the same whatever the store is called.
        self.prepared()
        for text in (self.cli("work", "checkpoint").stdout, self.cli("work", "checkpoint", "demo", "--trigger", "session-end").stdout):
            self.assertRegex(text.strip(), r"^checkpoint written: store:checkpoints/demo/\d{8}-\d{6}-\d{4}\.json$")
            self.assertNotIn(str(self.store), text)
            self.assertNotIn("REDACTED", text)

    def test_checkpoint_of_a_named_workspace_needs_no_session(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        env = {k: v for k, v in os.environ.items() if k not in ("TP_STATE_DIR", "TMUX")}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "work", "checkpoint", "demo"], env={**env, **{k: v for k, v in self.env.items() if k != "TP_STATE_DIR"}},
                             text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("checkpoint written: store:checkpoints/demo/", run.stdout)

    def test_checkpoint_with_nothing_attached(self):
        auto = self.cli("work", "checkpoint", "--auto")
        self.assertIn("no checkpoint written", auto.stdout)
        self.assertNotIn("checkpoint written:", auto.stdout)
        self.assertNotEqual(self.cli("work", "checkpoint", check=False).returncode, 0)

    def test_any_failed_checkpoint_fails_the_command_auto_included_and_prints_no_success(self):
        self.attach("ghost")
        for extra in ((), ("--auto",)):
            run = self.cli("work", "checkpoint", *extra, check=False)
            self.assertNotEqual(run.returncode, 0, extra)
            self.assertIn("ghost", run.stderr)
            self.assertNotIn("checkpoint written:", run.stdout + run.stderr)

    def test_one_failure_among_several_still_reports_the_written_ones_and_fails(self):
        self.prepared()
        self.attach("demo", "ghost")
        run = self.cli("work", "checkpoint", "--auto", check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("checkpoint written: store:checkpoints/demo/", run.stdout)
        self.assertIn("ghost", run.stderr)

    def test_work_attach_sets_recovery_pending_once_in_a_run_with_no_marker(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        self.assertFalse((self.state / cp.RECOVERY_FILE).exists())
        self.cli("work", "attach", "demo")
        first = json.loads((self.state / cp.RECOVERY_FILE).read_text())
        self.assertEqual((first["pending"], first["reason"]), (True, "attach"))
        cp.compacted(self.state, "precompact:auto")  # already pending: nothing changes
        self.cli("work", "attach", "demo")
        self.assertEqual(json.loads((self.state / cp.RECOVERY_FILE).read_text()), first)

    def test_a_corrupt_workspace_pushes_no_good_checkpoint_out(self):
        self.prepared()
        self.cli("work", "checkpoint")
        (self.store / "workspaces" / "demo.md").write_text("junk")
        run = self.cli("work", "checkpoint", "demo", check=False)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(len(self.files()), 1)

    def test_recover_prints_the_judgement_state_and_tells_what_to_do(self):
        self.prepared()
        self.cli("work", "checkpoint")
        text = self.cli("work", "recover").stdout
        for needle in ("recover demo: workspace ok", "checkpoint: valid snapshot", "Use sqlite", "### demo/D1 · normal · open · blocking", "re-verify"):
            self.assertIn(needle, text)

    def test_recover_goes_through_the_output_boundary(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        self.cli("work", "decision", "add", "demo", "--question", f"Is {self.store} the place?")
        self.cli("work", "checkpoint", "demo")
        text = self.cli("work", "recover", "demo").stdout
        self.assertNotIn(str(self.store), text)
        self.assertIn("store:", text)

    def test_compacted_through_the_cli_is_idempotent(self):
        first = json.loads(self.cli("work", "compacted", "--reason", "precompact:auto").stdout)
        second = json.loads(self.cli("work", "compacted", "--reason", "session-start:compact").stdout)
        self.assertEqual((first["changed"], second["changed"], second["reason"]), (True, False, "precompact:auto"))
        env = {k: v for k, v in os.environ.items() if k not in ("TP_STATE_DIR", "TMUX")}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "work", "compacted"], env={**env, "TP_WORK_DIR": str(self.store), "TP_SESSION": "no-such-session-x"},
                             text=True, capture_output=True, timeout=60)
        self.assertNotEqual(run.returncode, 0)

    def test_a_change_to_an_attached_workspace_checkpoints_when_none_is_fresh(self):
        self.prepared()
        self.assertEqual(self.files(), [])
        self.cli("work", "next", "demo", "write tests")
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(cp.latest("demo")["valid"][1]["trigger"], "work-write")
        self.cli("work", "next", "demo", "more")  # fresh enough: no second file
        self.assertEqual(len(self.files()), 1)

    def test_a_change_to_a_workspace_that_is_not_attached_writes_no_checkpoint(self):
        self.cli("work", "new", "Other", "--id", "other")
        self.cli("work", "next", "other", "x")
        self.assertFalse((self.store / "checkpoints").exists())

    def test_a_failing_automatic_checkpoint_does_not_fail_the_change(self):
        self.prepared()
        (self.store / "checkpoints").write_text("not a directory")
        run = self.cli("work", "next", "demo", "still saved")
        self.assertIn("warning: checkpoint not written", run.stderr)
        self.assertIn("still saved", json.loads(self.cli("work", "show", "demo", "--section", "next").stdout)["text"])


class IndexEntry(Base):
    def index(self, *extra):
        return json.loads(self.cli("index", "--no-write", *extra).stdout)

    def test_no_attached_workspace_means_no_checkpoint_entry(self):
        self.workspace()
        self.assertIsNone(self.index()["work"]["checkpoint"])
        self.assertNotIn("recovery", self.index()["work"])

    def test_the_checkpoint_entry_of_the_first_attached_workspace(self):
        doc = self.workspace(decisions=[decision("demo", 1, "Which db?")])
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        entry = self.index()["work"]["checkpoint"]
        self.assertEqual((entry["state"], entry["workspace_file"], entry["stale"], entry["tasks_changed"]), ("valid", "ok", False, 0))
        self.assertTrue(entry["line"].startswith("checkpoint: valid snapshot"))
        self.assertNotIn("Which db?", json.dumps(self.index()["work"]))  # status only: no question text

    def test_a_missing_attached_workspace_file_is_said_in_the_line(self):
        doc = self.workspace()
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        (self.store / "workspaces" / "demo.md").unlink()
        entry = self.index()["work"]["checkpoint"]
        self.assertEqual(entry["workspace_file"], "file missing")
        self.assertIn("workspace file missing", entry["line"])
        self.assertIn("work recover", entry["line"])

    def test_a_deleted_attached_workspace_is_still_named_with_file_missing(self):
        doc = self.workspace()
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        (self.store / "workspaces" / "demo.md").unlink()
        block = self.index()["work"]
        self.assertEqual(block["attached"], [{"id": "demo", "title": "", "health": "file missing"}])
        self.assertIn("workspace file missing", block["checkpoint"]["line"])

    def test_a_corrupt_attached_workspace_is_called_corrupt_by_the_index_and_by_recover_alike(self):
        doc = self.workspace()
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        (self.store / "workspaces" / "demo.md").write_text("junk without frontmatter")
        block = self.index()["work"]
        self.assertEqual(block["attached"], [{"id": "demo", "title": "", "health": "corrupt: no frontmatter"}])
        self.assertEqual(block["checkpoint"]["workspace_file"], "corrupt: no frontmatter")
        self.assertIn("workspace corrupt: no frontmatter", block["checkpoint"]["line"])
        self.assertEqual(cp.recover("demo")["file"], block["checkpoint"]["workspace_file"])

    def test_one_health_function_serves_index_and_recover(self):
        self.workspace()
        self.attach("demo")
        with mock.patch.object(cp, "workspace_health", wraps=cp.workspace_health) as health:
            work_index.build(self.state)
            cp.recover("demo")
        self.assertGreaterEqual(health.call_count, 2)
        for kind, write in (("file missing", lambda p: p.unlink()), ("corrupt: no frontmatter", lambda p: p.write_text("x"))):
            write(self.store / "workspaces" / "demo.md")
            self.assertEqual(cp.workspace_health("demo")[1], kind)
            self.assertEqual(cp.recover("demo")["file"], kind)
            self.assertEqual(self.index()["work"]["checkpoint"]["workspace_file"], kind)
            self.workspace()

    def test_recovery_shows_only_while_pending_and_attached(self):
        self.workspace()
        cp.compacted(self.state, "precompact:auto")
        self.assertNotIn("recovery", self.index()["work"])  # nothing attached
        self.attach("demo")
        self.assertEqual(self.index()["work"]["recovery"]["reason"], "precompact:auto")

    def test_reinjection_is_read_only_and_idempotent(self):
        doc = self.workspace(decisions=[decision("demo", 1, "Which db?")])
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        cp.compacted(self.state, "precompact:auto")
        watched = [*self.store.rglob("*"), *self.state.rglob("*")]
        before = {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in watched if p.is_file()}
        names = sorted(str(p) for p in watched)
        first, second = self.index(), self.index()
        first.pop("updated"), second.pop("updated")
        self.assertEqual(first, second)
        after = [*self.store.rglob("*"), *self.state.rglob("*")]
        self.assertEqual(sorted(str(p) for p in after), names)
        self.assertEqual({str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in after if p.is_file()}, before)
        self.assertFalse((self.state / "session-index.json").exists())

    def test_the_pi_form_carries_the_same_checkpoint_entry(self):
        doc = self.workspace()
        self.attach("demo")
        cp.write(doc, "run-one", "manual")
        whole, bounded = self.index()["work"], self.index("--bounded")["work"]
        self.assertEqual(whole["checkpoint"], bounded["checkpoint"])
        self.assertIn("summary", bounded["decisions"])

    def test_an_unreadable_checkpoint_entry_cannot_hide_the_block(self):
        self.workspace()
        self.attach("demo")
        with mock.patch.object(cp, "status", side_effect=RuntimeError("boom")):
            block = work_index.build(self.state)
        self.assertIsNone(block["checkpoint"])
        self.assertEqual([a["id"] for a in block["attached"]], ["demo"])

    def test_an_attached_workspace_with_an_empty_store_still_gives_a_block(self):
        self.attach("demo")
        block = work_index.build(self.state)
        self.assertEqual(block["attached"], [{"id": "demo", "title": "", "health": "file missing"}])
        self.assertIn("workspace file missing", block["checkpoint"]["line"])


class HookScript(Base):
    def run_hook(self, stdin='{"hook_event_name":"PreCompact","trigger":"auto"}', **env):
        base = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_MODE", "TMUX")}
        base.update(self.env)
        base.update(env)
        return subprocess.run([str(HOOK)], input=stdin, env=base, text=True, capture_output=True, timeout=60)

    def marker(self):
        path = self.state / cp.RECOVERY_FILE
        return json.loads(path.read_text()) if path.exists() else None

    def test_the_orchestrator_main_gets_the_marker_and_a_checkpoint(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        self.attach("demo")
        run = self.run_hook(TP_MODE="orchestrator")
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertEqual(self.marker()["reason"], "precompact:auto")
        self.assertEqual(cp.latest("demo")["valid"][1]["trigger"], "precompact")

    def test_manual_and_unknown_triggers_are_named_in_the_reason(self):
        self.run_hook('{"trigger":"manual"}', TP_MODE="orchestrator")
        self.assertEqual(self.marker()["reason"], "precompact:manual")
        (self.state / cp.RECOVERY_FILE).unlink()
        self.run_hook("not json at all", TP_MODE="orchestrator")
        self.assertEqual(self.marker()["reason"], "precompact:unknown")

    def test_the_marker_is_set_even_when_no_checkpoint_can_be_written(self):
        self.attach("ghost")
        run = self.run_hook(TP_MODE="orchestrator")
        self.assertEqual(run.returncode, 0)
        self.assertTrue(self.marker()["pending"])
        self.assertFalse((self.store / "checkpoints").exists())

    def test_it_never_fails_the_compaction(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        self.attach("demo")
        for stdin in ("", "\x00\xff", "{"):
            self.assertEqual(self.run_hook(stdin, TP_MODE="orchestrator").returncode, 0)
        shutil.rmtree(self.store / "checkpoints", ignore_errors=True)
        (self.store / "checkpoints").write_text("blocked")
        self.assertEqual(self.run_hook(TP_MODE="orchestrator").returncode, 0)
        self.assertEqual(self.run_hook(TP_MODE="orchestrator", TP_STATE_DIR=str(self.tmp / "gone")).returncode, 0)

    def test_workers_and_other_sessions_are_left_alone(self):
        self.cli("work", "new", "Demo", "--id", "demo")
        self.attach("demo")
        for env in ({}, {"TP_MODE": "worker"}, {"TP_MODE": "orchestrator", "TP_TASK_DIR": str(self.tmp)}):
            run = self.run_hook(**env)
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        self.assertIsNone(self.marker())
        self.assertFalse((self.store / "checkpoints").exists())


class SessionEnd(unittest.TestCase):
    def test_tp_exit_checkpoints_before_it_ends_anything_and_never_blocks(self):
        text = (ROOT / "bin" / "tp-exit").read_text()
        call = "work checkpoint --auto --trigger session-end"
        self.assertIn(call, text)
        self.assertLess(text.index(call), text.index("bin/tp-reset"))
        line = next(l for l in text.splitlines() if call in l)
        self.assertTrue(line.rstrip().endswith("|| true"))
        self.assertIn("TP_STATE_DIR=", line)

    def test_the_session_end_checkpoint_command_works_from_a_state_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            tmp = Path(temp).resolve()
            state = tmp / "run-x"
            state.mkdir()
            env = {**{k: v for k, v in os.environ.items() if k != "TMUX"}, "TP_WORK_DIR": str(tmp / "w"), "TP_WORK_CONFIG": str(tmp / "none.json"),
                   "TP_STATE_DIR": str(state)}
            run_cli = lambda *a: subprocess.run([str(ROOT / "bin/tp-agent"), *a], env=env, text=True, capture_output=True, timeout=60)  # noqa: E731
            self.assertEqual(run_cli("work", "new", "Demo", "--id", "demo").returncode, 0)
            (state / "session-work.json").write_text('{"attached": ["demo"]}')
            run = run_cli("work", "checkpoint", "--auto", "--trigger", "session-end")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(len(list((tmp / "w" / "checkpoints" / "demo").glob("*.json"))), 1)


if __name__ == "__main__":
    unittest.main()


class PromptSentences(unittest.TestCase):
    """The two Phase C prompt sentences say what the design says (a prompt that leaves out the step loses the capture)."""

    def test_claude_compact_instructions_name_the_workspace_decisions_and_the_manual_checkpoint(self):
        text = (ROOT / "agent" / "orchestrator-claude.md").read_text()
        section = text.split("## Compact Instructions", 1)[1]
        self.assertIn("keep the attached workspace id and any open decision ids", section)
        self.assertIn("before a manual /compact run `tp-agent work checkpoint --summary-file", section)
        self.assertIn("tp-agent work recover", section)

    def test_pi_runs_work_recover_after_compaction_with_or_without_a_marker(self):
        text = (ROOT / "agent" / "orchestrator-pi.md").read_text()
        line = next(l for l in text.splitlines() if "after compaction" in l.lower() and "work recover" in l)
        self.assertIn("whether or not `task_index` shows a pending recovery", line)
        self.assertNotIn("If it shows a pending recovery", line)


class CompletedItemsAreRecovered(Snapshot):
    def test_recover_prints_every_checkpointed_blocker_and_next_action_completed_ones_included(self):
        doc = self.full()
        cp.write(doc, "r", "manual")
        (self.store / "workspaces" / "demo.md").unlink()
        text = cp.format_recover(cp.recover("demo"))
        for line in ("- [ ] need API key", "- [x] old blocker", "- [ ] write tests", "- [x] write docs"):
            self.assertIn(f"  {line}", text)
        self.assertNotIn("(none open)", text)


class CarriedSummary(Base):
    def test_an_automatic_checkpoint_carries_the_newest_agent_summary_and_says_so(self):
        self.workspace()
        self.attach("demo")
        note = self.tmp / "summary.txt"
        note.write_text("agent was weighing option B")
        self.cli("work", "checkpoint", "--summary-file", str(note))
        manual = cp._seq(cp.latest("demo")["valid"][0])
        self.cli("work", "checkpoint", "--auto", "--trigger", "precompact")
        self.cli("work", "checkpoint", "--auto", "--trigger", "precompact")  # carried again: the origin stays the manual one
        newest = cp.latest("demo")["valid"][1]
        self.assertEqual(newest["trigger"], "precompact")
        self.assertEqual(newest["summary"]["text"], "agent was weighing option B")
        self.assertEqual(newest["summary"]["carried_from"]["seq"], manual)
        text = self.cli("work", "recover").stdout
        self.assertIn(f"agent summary (carried forward from checkpoint seq {manual}", text)
        self.assertIn("agent was weighing option B", text)
        self.assertIn(f"carried from seq {manual}", cp.status("demo")["line"])

    def test_an_own_summary_is_not_marked_as_carried_and_no_summary_means_nothing_to_carry(self):
        self.workspace()
        cp.write(work.load("workspace", "demo"), "r", "precompact")
        self.assertEqual(cp.latest("demo")["valid"][1]["summary"], {"authored_by": None, "text": ""})
        cp.write(work.load("workspace", "demo"), "r", "manual", summary="mine")
        self.assertNotIn("carried_from", cp.latest("demo")["valid"][1]["summary"])
        cp.write(work.load("workspace", "demo"), "r", "manual", summary="newer")
        self.assertEqual(cp.latest("demo")["valid"][1]["summary"]["text"], "newer")

    def test_a_malformed_carried_from_is_invalid(self):
        doc = self.workspace()
        body = cp.snapshot(doc, "r", "manual", carried={"text": "x", "seq": 1, "at": "t"})
        self.assertEqual(cp.check(cp.seal(body)), "")
        for bad in ({"seq": "1", "at": "t"}, "x", {"seq": 1}):
            broken = {**body, "summary": {**body["summary"], "carried_from": bad}}
            self.assertNotEqual(cp.check(cp.seal(broken)), "")
        broken = {**body, "summary": {"authored_by": None, "text": "", "carried_from": {"seq": 1, "at": "t"}}}
        self.assertNotEqual(cp.check(cp.seal(broken)), "")


class PartialTaskDiff(Base):
    def test_a_truncated_task_snapshot_never_calls_absent_rows_new_or_removed(self):
        self.settings(checkpoint_max_bytes=1500)
        doc = self.workspace()
        tasks = [{"id": f"coder-{i:02d}", "status": "settled", "result_path": f"/r/{i:02d}"} for i in range(50)]
        cp.write(doc, "run", "manual", tasks)
        value = cp.latest("demo")["valid"][1]
        left_out = value["truncated"]["sections"]["tasks"]
        self.assertGreater(left_out, 0)
        self.assertEqual(len(value["tasks"]) + left_out, 50)
        info = cp.recover("demo", tasks, "run")
        self.assertEqual(info["tasks"]["changes"], [])
        text = cp.format_recover(info)
        self.assertIn(f"task diff partial: {left_out} task rows were not checkpointed", text)
        self.assertNotIn("is new", text)
        status = cp.status("demo", "run", tasks)
        self.assertEqual(status["tasks_changed"], 0)
        self.assertIn(f"task diff partial: {left_out} task rows were not checkpointed", status["line"])
        # A checkpointed row that changed is still reported; a task with no row, and one that is brand new, are not called new.
        kept = value["tasks"][-1]["id"]
        live = [{**t, "status": "working"} if t["id"] == kept else t for t in tasks] + [{"id": "coder-99", "status": "queued", "result_path": None}]
        changes = cp.recover("demo", live, "run")["tasks"]["changes"]
        self.assertEqual(changes, [f"{kept} was settled, now working with result path {tasks[-1]['result_path']}"])

    def test_a_complete_snapshot_keeps_calling_new_tasks_new(self):
        doc = self.workspace()
        cp.write(doc, "run", "manual", [])
        info = cp.recover("demo", [{"id": "a", "status": "queued", "result_path": None}], "run")
        self.assertEqual((info["tasks"]["changes"], info["tasks"]["partial"]), (["a is new (queued)"], 0))
        self.assertNotIn("task diff partial", cp.format_recover(info))


class AutoTakesNoSummary(Base):
    def test_auto_with_a_summary_is_a_usage_error_and_writes_nothing(self):
        self.workspace()
        self.attach("demo")
        note = self.tmp / "s.txt"
        note.write_text("pretend")
        empty = self.tmp / "empty.txt"
        empty.write_text("")
        for extra in (("--summary", "agent says"), ("--summary", ""), ("--summary-file", str(note)), ("--summary-file", str(empty)),
                      ("--summary-file", "")):
            run = self.cli("work", "checkpoint", "--auto", *extra, check=False)
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("--auto", run.stderr)
            self.assertNotIn("checkpoint written:", run.stdout)
        self.assertEqual(self.files(), [])


FAKE_TMUX = """#!/bin/sh
case "$1" in
  display-message) echo run-session ;;
  show-options) echo "$FAKE_STATE_DIR" ;;
esac
"""

# Every `work` subcommand and how to call it: ("workspace", argv) changes an attached workspace, ("other", argv) changes the store
# without changing a workspace, ("read", argv) changes nothing. A new subcommand must be added here, so it is decided on purpose.
LEAVES = {
    "new": ("other", ["new", "T"]), "show": ("read", ["show", "demo"]), "list": ("read", ["list"]),
    "set": ("workspace", ["set", "demo", "--status", "paused"]), "next": ("workspace", ["next", "demo", "do it"]),
    "decide": ("workspace", ["decide", "demo", "chose a"]), "block": ("workspace", ["block", "demo", "stuck"]),
    "link": ("workspace", ["link", "demo", "--goal", "g1"]), "archive": ("workspace", ["archive", "demo"]),
    "supersede": ("other", ["supersede", "n1", "--title", "N", "--body", "b"]), "attach": ("other", ["attach", "demo"]),
    "note add": ("other", ["note", "add", "N"]), "note show": ("read", ["note", "show", "n1"]),
    "decision add": ("workspace", ["decision", "add", "demo", "--question", "Q?"]),
    "decision answer": ("workspace", ["decision", "answer", "demo/D1", "--text", "yes"]),
    "decision withdraw": ("workspace", ["decision", "withdraw", "demo/D2"]),
    "decision list": ("read", ["decision", "list"]), "promote": ("other", ["promote", "demo"]), "decisions": ("read", ["decisions"]),
    "checkpoint": ("read", ["checkpoint"]), "recover": ("read", ["recover"]), "compacted": ("read", ["compacted"]),
}


def _leaves(parser):
    import argparse
    out = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                below = _leaves(child)
                out += [f"{name} {leaf}" for leaf in below] if below else [name]
    return out


class EveryMutationCheckpoints(Base):
    def parse(self, argv):
        import argparse
        import work_commands
        parser = argparse.ArgumentParser()
        work_commands.add_parsers(parser.add_subparsers(dest="command", required=True))
        return parser, parser.parse_args(["work", *argv])

    def test_every_work_subcommand_is_classified_here(self):
        parser, _ = self.parse(["list"])
        found = sorted(leaf for leaf in _leaves(parser._subparsers._group_actions[0].choices["work"]))
        self.assertEqual(found, sorted(LEAVES), "a work subcommand was added or removed: classify it in LEAVES")

    def test_work_commands_agrees_with_the_classification(self):
        import work_commands
        for leaf, (kind, argv) in LEAVES.items():
            args = self.parse(argv)[1]
            self.assertEqual(work_commands.leaf(args), leaf)
            self.assertEqual(work_commands.mutating(args), kind in ("workspace", "other"), leaf)
            self.assertEqual(bool(work_commands.workspace_written(args)) if work_commands.mutating(args) else None,
                             True if kind == "workspace" else (False if kind == "other" else None), leaf)

    def test_run_calls_the_post_mutation_function_after_every_mutating_subcommand_and_after_no_other(self):
        import work_commands
        for leaf, (kind, argv) in LEAVES.items():
            if kind == "read":
                continue
            args = self.parse(argv)[1]
            with mock.patch.object(work_commands, "_write_command", return_value={}), \
                    mock.patch.object(work_commands, "post_mutation") as post:
                work_commands.run(args, self.state, None, None)
            post.assert_called_once()
            self.assertIs(post.call_args.args[0], args, leaf)
            self.assertEqual(post.call_args.args[1], self.state, leaf)

    def fake_tmux_env(self):
        bin_dir = self.tmp / "fake-bin"
        bin_dir.mkdir(exist_ok=True)
        script = bin_dir / "tmux"
        script.write_text(FAKE_TMUX)
        script.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "TP_STATE_DIR"}
        env.update({k: v for k, v in self.env.items() if k != "TP_STATE_DIR"})
        env.update(PATH=f"{bin_dir}:{env['PATH']}", TMUX="/fake/socket,1,0", FAKE_STATE_DIR=str(self.state))
        env.pop("TMUX_PANE", None)
        return env

    def test_a_run_found_only_through_tmux_is_checkpointed_after_each_workspace_mutation(self):
        env = self.fake_tmux_env()

        def tp(*argv):
            run = subprocess.run([str(ROOT / "bin/tp-agent"), *argv], env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(run.returncode, 0, f"{argv}: {run.stderr}")
            return run

        tp("work", "new", "Demo", "--id", "demo")
        tp("work", "decision", "add", "demo", "--question", "Q1?")
        tp("work", "decision", "add", "demo", "--question", "Q2?")
        tp("work", "attach", "demo")
        tp("work", "note", "add", "N", "--id", "n1")
        self.assertEqual(self.files(), [], "nothing so far changed an attached workspace")
        for leaf, (kind, argv) in sorted(LEAVES.items(), key=lambda item: item[0] == "archive"):  # archiving ends the workspace's edits
            if kind != "workspace":
                continue
            shutil.rmtree(self.store / "checkpoints", ignore_errors=True)
            tp("work", *argv)
            self.assertEqual(len(self.files()), 1, f"{leaf} wrote no checkpoint")
            self.assertEqual(cp.latest("demo")["valid"][1]["trigger"], "work-write", leaf)
            self.assertEqual(cp.latest("demo")["valid"][1]["run"], "run-one", leaf)
