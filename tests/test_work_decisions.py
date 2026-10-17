"""Decision entries (Phase A): every brief-section-9 field, store-global ids, cross-workspace listing, answer/withdraw."""
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


class Decisions(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.state = self.tmp / "p" / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        self.env = {**os.environ, "TP_WORK_DIR": str(self.tmp / "work"), "TP_STATE_DIR": str(self.state)}
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


if __name__ == "__main__":
    unittest.main()
