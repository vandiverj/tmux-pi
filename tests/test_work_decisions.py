"""Decision entries (Phase A): every brief-section-9 field, store-global ids, cross-workspace listing, answer/withdraw."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # tests/ holds session_env
from session_env import outside_session  # noqa: E402


class Decisions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.state = self.tmp / "p" / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.env = {**outside_session(), "TP_WORK_DIR": str(self.tmp / "work"), "TP_STATE_DIR": str(self.state)}
        self.env.pop("TMUX", None)

    def raw(self, *args):
        return subprocess.run([str(ROOT / "bin/tp-agent"), "work", *args], env=self.env, text=True, capture_output=True, timeout=30)

    def ok(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        try:
            return json.loads(run.stdout)
        except ValueError:
            return run.stdout.strip()

    def add(self, ws, question="Re-add !1144 now?", **kw):
        args = ["decision", "add", ws, "--question", question]
        for key, value in kw.items():
            if value is True:
                args.append(f"--{key}")
            elif isinstance(value, list):
                for v in value:
                    args += [f"--{key}", v]
            else:
                args += [f"--{key}", value]
        return self.ok(*args)

    def test_full_field_round_trip(self):
        ws = self.ok("new", "CI flakiness")["id"]
        d = self.add(ws, "Line one\nline two: with colon", background="Why\n\n### not a heading\nQuestion: not a field",
                     options="(a) now  (b) after", recommended="(b)", affects=["task:r-1", "lane:foo", "next:2"], priority="high", blocking=True)
        self.assertEqual(d["id"], f"{ws}/D1")
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"]}):
            got = work.decisions_of(work.load("workspace", ws))
        self.assertEqual(len(got), 1)
        e = got[0]
        self.assertEqual((e["priority"], e["status"], e["blocking"]), ("high", "open", True))
        self.assertEqual(e["question"], "Line one\nline two: with colon")
        self.assertEqual(e["background"], "Why\n\n### not a heading\nQuestion: not a field")
        self.assertEqual(e["options"], "(a) now  (b) after")
        self.assertEqual(e["recommended"], "(b)")
        self.assertEqual(e["affects"], ["task:r-1", "lane:foo", "next:2"])

    def test_ids_unique_across_workspaces_and_never_reused(self):
        a, b = self.ok("new", "Alpha")["id"], self.ok("new", "Beta")["id"]
        self.assertEqual(self.add(a)["id"], f"{a}/D1")
        self.assertEqual(self.add(b)["id"], f"{b}/D1")
        self.assertEqual(self.add(a)["id"], f"{a}/D2")
        self.ok("decision", "withdraw", f"{a}/D2")
        self.assertEqual(self.add(a)["id"], f"{a}/D3")  # a withdrawn number is not reused

    def test_cross_workspace_list_regardless_of_attachment(self):
        a, b = self.ok("new", "Alpha")["id"], self.ok("new", "Beta")["id"]
        self.ok("attach", a)  # only a is attached; b must still be listed
        self.add(a, "low one", priority="low")
        self.add(b, "blocking one", priority="normal", blocking=True)
        self.add(b, "high one", priority="high")
        text = self.ok("decisions")
        lines = text.strip().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(f"{b}/D1 [normal blocking]"), lines)  # blocking first, then priority
        self.assertTrue(lines[1].startswith(f"{b}/D2 [high]"))
        self.assertTrue(lines[2].startswith(f"{a}/D1 [low]"))
        rows = self.ok("decisions", "--json")
        self.assertEqual([r["id"] for r in rows], [f"{b}/D1", f"{b}/D2", f"{a}/D1"])
        self.assertEqual(self.ok("decision", "list"), text)
        self.assertEqual(self.ok("decisions").count("\n"), 2)

    def test_many_decisions_uncapped(self):
        ws = self.ok("new", "Big")["id"]
        for i in range(40):
            self.add(ws, f"q{i}")
        self.assertEqual(len(self.ok("decisions", "--json")), 40)

    def test_answer_records_and_prints_resume(self):
        ws = self.ok("new", "CI")["id"]
        self.ok("next", ws, "re-add MR once CI/D1 is answered")
        d = self.add(ws, "Wait?", affects=["task:nope-1", "next:1", "lane:x"], blocking=True)
        out = self.ok("decision", "answer", d["id"], "--text", "wait", "--by", "user", "--quote", "wait until DMP-77")
        self.assertEqual(out["status"], "answered")
        self.assertTrue(out["clears_blocking"])
        self.assertEqual([t["target"] for t in out["can_resume"]], ["task:nope-1", "next:1", "lane:x"])
        self.assertEqual(out["can_resume"][0]["status"], "unknown")  # unknown task: reported, no crash
        self.assertEqual(out["next_actions_mentioning"], [])  # the line names "CI/D1", not this id
        shown = self.ok("show", ws, "--section", "open-decisions")["text"]
        self.assertIn("Resolution: wait", shown)
        self.assertIn("By: user", shown)
        self.assertIn("Quote: wait until DMP-77", shown)
        self.assertIn(f"### {d['id']} · normal · answered · blocking", shown)  # the entry stays
        self.assertEqual(self.ok("decisions"), "no open decisions")
        self.assertIn(d["id"], self.ok("decisions", "--all"))
        self.assertNotEqual(self.raw("decision", "answer", d["id"], "--text", "again").returncode, 0)

    def test_answer_names_next_actions_that_mention_the_id(self):
        ws = self.ok("new", "Mention")["id"]
        self.ok("next", ws, f"resume after {ws}/D1")
        self.add(ws)
        out = self.ok("decision", "answer", f"{ws}/D1", "--text", "go")
        self.assertEqual(len(out["next_actions_mentioning"]), 1)

    def test_agent_answer_does_not_clear_blocking(self):
        ws = self.ok("new", "Guard")["id"]
        d = self.add(ws, blocking=True)
        out = self.ok("decision", "answer", d["id"], "--text", "guess", "--by", "agent")
        self.assertFalse(out["clears_blocking"])
        self.assertIn("note", out)

    def test_agent_answer_on_blocking_offers_nothing_to_resume(self):
        ws = self.ok("new", "Gate")["id"]
        self.ok("next", ws, f"ship it after {ws}/D1")
        d = self.run_add(ws)
        out = self.ok("decision", "answer", d["id"], "--text", "guess", "--by", "agent")
        self.assertEqual(out["can_resume"], [])
        self.assertEqual(out["next_actions_mentioning"], [])
        self.assertIn("blocking", out["note"])
        user = self.ok("decision", "answer", d["id"], "--text", "go", "--by", "user", "--quote", "go ahead")
        self.assertEqual([t["target"] for t in user["can_resume"]], ["task:t-1", "next:1"])
        self.assertEqual(len(user["next_actions_mentioning"]), 1)

    def run_add(self, ws):
        return self.ok("decision", "add", ws, "--question", "Ship?", "--blocking", "--affects", "task:t-1", "--affects", "next:1")

    def test_agent_answer_on_blocking_stays_open_and_user_can_still_answer(self):
        ws = self.ok("new", "Queue")["id"]
        d = self.add(ws, "Deploy?", blocking=True)
        out = self.ok("decision", "answer", d["id"], "--text", "guess", "--by", "agent")
        self.assertEqual(out["status"], "open")
        self.assertEqual([r["id"] for r in self.ok("decisions", "--json")], [d["id"]])  # still in the approval queue
        self.assertIn(d["id"], self.ok("decisions"))
        self.ok("decision", "answer", d["id"], "--text", "go", "--by", "user", "--quote", "go ahead")
        self.assertEqual(self.ok("decisions"), "no open decisions")
        shown = self.ok("show", ws, "--section", "open-decisions")["text"]
        self.assertIn("Resolution: go", shown)
        self.assertNotIn("guess", shown)
        d2 = self.add(ws, "Other?", blocking=True)  # an open one can also be withdrawn after an agent answer
        self.ok("decision", "answer", d2["id"], "--text", "guess", "--by", "agent")
        self.ok("decision", "withdraw", d2["id"])

    def test_decision_writes_preserve_hand_written_text(self):
        store = self.tmp / "work" / "workspaces"
        self.ok("new", "Seed")
        (store / "hand.md").write_text(
            "---\ntitle: Hand\n---\n## Open decisions\nHandwritten pending review: ask user before deployment\n\n"
            "### hand/D1 · high · open · blocking\nSee the thread with Sam\nQuestion: First?\nNote: keep this line\n\n"
            "### hand/D2 · normal · open\nQuestion: Second?\n\nTrailing free text\n")
        def section():
            return self.ok("show", "hand", "--section", "open-decisions")["text"]
        def check(text):
            for keep in ("Handwritten pending review: ask user before deployment", "See the thread with Sam", "Note: keep this line", "Trailing free text"):
                self.assertIn(keep, text)
        self.add("hand", "Third?")
        check(section())
        self.ok("decision", "answer", "hand/D1", "--text", "yes")
        check(section())
        self.ok("decision", "withdraw", "hand/D2")
        check(section())
        self.assertEqual(sorted(r["id"] for r in self.ok("decisions", "--all", "--json")), ["hand/D1", "hand/D2", "hand/D3"])

    def test_task_status_in_resume(self):
        ws = self.ok("new", "T")["id"]
        task = self.state / "tasks" / "coder-1"
        task.mkdir()
        (task / "meta.json").write_text(json.dumps({"id": "coder-1", "role": "coder", "created": "2026-10-16T00:00:00+00:00"}))
        d = self.add(ws, affects=["task:coder-1"])
        out = self.ok("decision", "answer", d["id"], "--text", "go")
        self.assertNotEqual(out["can_resume"][0]["status"], "unknown")

    def test_withdraw_keeps_entry_and_deferral_leaves_other_work(self):
        ws = self.ok("new", "Keep")["id"]
        self.ok("next", ws, "unrelated action")
        self.ok("decide", ws, "earlier decision")
        before = self.ok("show", ws, "--section", "next")["text"]
        d = self.add(ws, affects=["next:9"])
        self.ok("decision", "withdraw", d["id"], "--reason", "moot")
        shown = self.ok("show", ws, "--section", "open-decisions")["text"]
        self.assertIn(f"### {d['id']} · normal · withdrawn", shown)
        self.assertIn("Withdrawn: moot", shown)
        self.assertEqual(self.ok("show", ws, "--section", "next")["text"], before)
        self.assertIn("earlier decision", self.ok("show", ws, "--section", "decisions")["text"])
        self.assertEqual(self.ok("decisions"), "no open decisions")

    def test_archived_workspace_decisions_not_listed_or_answerable(self):
        ws = self.ok("new", "Gone")["id"]
        d = self.add(ws)
        self.ok("archive", ws)
        self.assertEqual(self.ok("decisions"), "no open decisions")
        self.assertNotEqual(self.raw("decision", "answer", d["id"], "--text", "x").returncode, 0)

    def test_bad_ids_refused(self):
        self.assertNotEqual(self.raw("decision", "answer", "nonsense", "--text", "x").returncode, 0)
        self.assertNotEqual(self.raw("decision", "add", "missing", "--question", "q").returncode, 0)

    def test_hand_edited_workspace_decisions_parse(self):
        store = self.tmp / "work" / "workspaces"
        self.ok("new", "Seed")
        (store / "hand.md").write_text("---\ntitle: Hand\n---\n## Open decisions\n### hand/D4 · high · open · blocking\nQuestion: Hand q?\nOptions: a | b\nAffects: workspace | task:t\n")
        rows = self.ok("decisions", "--json")
        self.assertEqual([r["id"] for r in rows], ["hand/D4"])
        self.assertEqual(rows[0]["affects"], ["workspace", "task:t"])
        self.assertEqual(self.add("hand")["id"], "hand/D5")


class Records(unittest.TestCase):
    """HB3a decision records (kind, scope, provenance, status, supersession), in process: bin/tp-agent's main(argv, env)."""

    def setUp(self):
        from importlib.machinery import SourceFileLoader
        self.agent = SourceFileLoader("tp_agent_records", str(ROOT / "bin/tp-agent")).load_module()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.env = {**outside_session(), "TP_WORK_DIR": str(self.tmp / "work")}
        for key in ("TP_TASK_ID", "TP_TASK_DIR", "TP_STATE_DIR"):
            self.env.pop(key, None)

    def call(self, *argv, worker=False):
        import contextlib
        import io
        out, err, code = io.StringIO(), io.StringIO(), 0
        env = {**self.env, **({"TP_TASK_ID": "coder-1"} if worker else {})}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                self.agent.main(["work", *argv], env)
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, worker=False):
        code, out, err = self.call(*argv, worker=worker)
        self.assertEqual(code, 0, err)
        return json.loads(out) if out.strip().startswith(("{", "[")) else out.strip()

    def refused(self, *argv, worker=False, says=""):
        code, out, err = self.call(*argv, worker=worker)
        self.assertNotEqual(code, 0, f"{argv} was accepted: {out}")
        self.assertIn(says, err)
        return err

    def records(self):
        return {r["id"]: r for r in self.ok("records", "--json")}

    def test_every_old_form_maps_to_design_workspace_and_only_an_owner_quote_is_active(self):
        store = self.tmp / "work" / "workspaces"
        store.mkdir(parents=True)
        (store / "old.md").write_text(
            "---\ntitle: Old\nstatus: active\n---\n## Decisions\n- 2026-10-01 plain old line\n- 2026-10-02 sourced line Source: chat\n"
            "- undated line\n\n## Open decisions\n"
            "### old/D1 · normal · answered\nQuestion: Q1?\nResolution: yes\nBy: user\nAt: 2026-10-03T00:00:00Z\nQuote: do it\n\n"
            "### old/D2 · normal · answered\nQuestion: Q2?\nResolution: yes\nBy: user\nAt: 2026-10-03T00:00:00Z\n\n"
            "### old/D3 · normal · answered\nQuestion: Q3?\nResolution: guess\nBy: agent\nAt: 2026-10-03T00:00:00Z\nQuote: owner words\n\n"
            "### old/D4 · normal · withdrawn\nQuestion: Q4?\nWithdrawn: moot\n\n"
            "### old/D5 · normal · open\nQuestion: Q5?\n")
        got = self.records()
        # row: (id, form, status, by, quote, at, source)
        rows = [("old/L1", "line:old", "provisional", None, None, "2026-10-01", None),
                ("old/L2", "line:old", "provisional", None, None, "2026-10-02", "chat"),
                ("old/L3", "line:old", "provisional", None, None, None, None),
                ("old/D1", "entry:old", "active", "owner", "do it", "2026-10-03T00:00:00Z", None),  # old `--by user --quote`
                ("old/D2", "entry:old", "provisional", "owner", None, "2026-10-03T00:00:00Z", None),  # user, no quote
                ("old/D3", "entry:old", "provisional", "main", "owner words", "2026-10-03T00:00:00Z", None),  # agent with a quote
                ("old/D4", "entry:old", "withdrawn", None, None, None, None),
                ("old/D5", "entry:old", None, None, None, None, None)]  # an open question is never in force
        self.assertEqual(sorted(got), sorted(r[0] for r in rows))
        for ident, form, status, by, quote, at, source in rows:
            r = got[ident]
            with self.subTest(row=ident):
                self.assertEqual((r["kind"], r["scope"], r["form"], r["status"]), ("design", "workspace", form, status), ident)
                self.assertEqual((r["provenance"]["by"], r["provenance"]["quote"], r["provenance"]["at"], r["provenance"]["source"]),
                                 (by, quote, at, source), ident)
        # The visible text of old lines is unchanged by reading them, and the file is not rewritten.
        self.assertEqual((got["old/L2"]["text"], got["old/D1"]["text"]), ("sourced line", "yes"))
        self.assertIn("- 2026-10-02 sourced line Source: chat\n", (store / "old.md").read_text())

    def test_provenance_rules_by_writer(self):
        ws = self.ok("new", "Rules", "--id", "rules")["id"]
        # row: (name, argv, worker, expected status or refusal text)
        rows = [("owner with quote", ["--by", "owner", "--quote", "use safe_grep"], False, "active"),
                ("user alias with quote", ["--by", "user", "--quote", "yes"], False, "active"),
                ("owner, empty quote", ["--by", "owner", "--quote", "  "], False, "provisional"),
                ("owner, no quote", ["--by", "owner"], False, "provisional"),
                ("main", ["--by", "main", "--quote", "a quote does not make main the owner"], False, "provisional"),
                ("default writer", [], False, "provisional"),
                ("worker plain", [], True, "provisional"),
                ("worker as owner", ["--by", "owner", "--quote", "q"], True, "refused: a worker process"),
                ("worker as user", ["--by", "user", "--quote", "q"], True, "refused: a worker process")]
        for name, extra, worker, want in rows:
            with self.subTest(row=name):
                if want.startswith("refused"):
                    self.refused("decide", ws, f"rule {name}", *extra, worker=worker, says=want)
                    self.assertNotIn(f"rule {name}", self.ok("show", ws, "--section", "decisions")["text"], name)
                    continue
                record = self.ok("decide", ws, f"rule {name}", *extra, worker=worker)["record"]
                self.assertEqual(record["status"], want, name)
                if worker:
                    self.assertEqual(record["provenance"]["by"], "worker", name)  # never silently recorded as main or owner
        worker_rule = [r for r in self.records().values() if r["text"] == "rule worker plain"][0]
        # Only the owner, outside a worker, with words, makes a record active.
        self.refused("decision", "confirm", worker_rule["id"], "--quote", "ok", worker=True, says="refused: a worker process")
        self.refused("decision", "confirm", worker_rule["id"], "--quote", " ", says="--quote must hold the owner's exact words")
        self.assertEqual(self.ok("decision", "confirm", worker_rule["id"], "--quote", "fine, keep it")["status"], "active")
        confirmed = self.records()[worker_rule["id"]]
        self.assertEqual((confirmed["provenance"]["by"], confirmed["provenance"]["quote"]), ("owner", "fine, keep it"))
        # An answered entry: a worker answer is stored as provisional (never as the user's); the owner then confirms it.
        d = self.ok("decision", "add", ws, "--question", "Ship it?", "--scope", "global", "--kind", "preference")["id"]
        self.refused("decision", "answer", d, "--text", "yes", "--by", "user", "--quote", "y", worker=True, says="refused: a worker process")
        self.assertEqual(self.ok("decision", "answer", d, "--text", "yes", worker=True)["record_status"], "provisional")
        self.assertEqual((self.records()[d]["provenance"]["by"], self.records()[d]["scope"]), ("worker", "global"))
        self.ok("decision", "confirm", d, "--quote", "yes ship")
        self.assertEqual((self.records()[d]["status"], self.records()[d]["kind"]), ("active", "preference"))

    def test_supersession_is_explicit_two_way_and_same_scope_only(self):
        a = self.ok("new", "A", "--id", "a")["id"]
        self.ok("new", "B", "--id", "b")
        owner = ["--by", "owner", "--quote", "owner said"]
        old = self.ok("decide", a, "tabs", "--scope", "global", *owner)["record"]["id"]
        # A replacement in another workspace but the same (global) scope; pointers both ways, the old one in its own file.
        new = self.ok("decide", "b", "spaces", "--scope", "global", "--supersedes", old, *owner)["record"]["id"]
        got = self.records()
        self.assertEqual((got[new]["supersedes"], got[old]["superseded_by"], got[old]["status"]), (old, new, "superseded"))
        local = self.ok("decide", a, "local rule", *owner)["record"]["id"]
        # row: (name, argv, refusal text)
        rows = [("other scope", ["decide", a, "x", "--scope", "workspace", "--supersedes", new, *owner], "a record supersedes only one in the same scope"),
                ("already superseded", ["decide", "b", "x", "--scope", "global", "--supersedes", old, *owner], "already superseded"),
                ("project vs global", ["decide", a, "x", "--scope", f"project:{self.tmp}", "--supersedes", new, *owner], "same scope"),
                ("workspace a vs workspace b", ["decide", "b", "x", "--supersedes", local, *owner], "same scope")]
        for name, argv, says in rows:
            with self.subTest(row=name):
                self.refused(*argv, says=says)
        # A provisional replacement of an active record waits for the owner; the active one stays in force meanwhile.
        pending = self.ok("decide", a, "local rule v2", "--supersedes", local)
        self.assertIn("stays active until the owner confirms", pending["supersession"])
        self.assertEqual(self.records()[local]["status"], "active")
        self.ok("decision", "confirm", pending["record"]["id"], "--quote", "v2 it is")
        self.assertEqual((self.records()[local]["status"], self.records()[local]["superseded_by"]), ("superseded", pending["record"]["id"]))
        # Withdrawing an active record needs the owner's words; a worker cannot.
        self.refused("decision", "withdraw", new, says="needs the owner's exact words")
        self.refused("decision", "withdraw", new, "--quote", "drop it", worker=True, says="refused: a worker process")
        self.ok("decision", "withdraw", new, "--quote", "drop it")
        self.assertEqual(self.records()[new]["status"], "withdrawn")
        self.refused("decision", "confirm", new, "--quote", "back", says="withdrawn; it cannot be confirmed")


if __name__ == "__main__":
    unittest.main()
