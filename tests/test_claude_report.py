"""Simulated Claude hooks and the real tp-review settlement guard (no live session)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "lib"))
import pane_send  # noqa: E402
from fake_tools import fake_tmux  # noqa: E402
from session_env import outside_session  # noqa: E402
HOOK = ROOT / "claude/plugins/tmux-pi/hooks/report.sh"


class ClaudeReportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.state_dir = self.base / ".tmux-pi"
        self.task = self.state_dir / "tasks/w1"
        self.task.mkdir(parents=True)
        (self.state_dir / "archive").mkdir()
        (self.task / "meta.json").write_text(json.dumps({"id": "w1", "worker": "claude", "workdir": str(self.base)}))
        (self.task / "task.md").write_text("Read fixture")
        (self.task / "packet.md").write_text("Read fixture")
        fake = self.base / "fake-bin"
        fake.mkdir()
        fake_tmux(fake, 'case "$1" in\n has-session) exit 0;;\n show-options) echo "$TP_STATE_DIR";;\n new-window) echo "%fake";;\n *) exit 0;;\nesac\n')
        self.env = {**outside_session(), "TP_TASK_DIR": str(self.task), "TP_TASK_ID": "w1",
                    "TP_STATE_DIR": str(self.state_dir), "TP_SESSION": "fixture",
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)

    def hook(self, name, **fields):
        run = subprocess.run(["bash", str(HOOK)], input=json.dumps({"hook_event_name": name, **fields}),
                             text=True, capture_output=True, env=self.env, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)

    def state(self):
        return json.loads((self.task / "state.json").read_text())

    def review(self, accepted=False):
        run = subprocess.run([str(ROOT / "bin/tp-review"), "--force", "w1"], text=True,
                             capture_output=True, env=self.env, timeout=15)
        if accepted:
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue((self.state_dir / "tasks" / run.stdout.strip() / "review-of").exists())
        else:
            self.assertNotEqual(run.returncode, 0)
            self.assertTrue("has no result yet" in run.stderr or "has not completed successfully" in run.stderr, run.stderr)

    def test_submit_log_records_every_delivery_id_in_the_prompt_and_keeps_the_last_records(self):
        # The hook finds the delivery ids lib/pane_send.py send() types; it stores no text and compares none.
        one, two = pane_send.new_token(), pane_send.new_token()
        self.hook("UserPromptSubmit", prompt="  Use the local renderer \t\x1f for\n the {} evidence {}\n".format(one, two))
        self.assertEqual(self.state()["promptLog"][-1]["tokens"], [one, two])  # all of them, in order
        self.hook("UserPromptSubmit", prompt="no id here, only [tp:123] [tp:0123456789a] [tp:0123456789A]")
        self.assertEqual(self.state()["promptLog"][-1]["tokens"], [])  # near misses are not ids
        self.hook("UserPromptSubmit")  # no prompt at all
        self.assertEqual(self.state()["promptLog"][-1]["tokens"], [])
        for i in range(4, 11):
            self.hook("UserPromptSubmit", prompt="p{} {}".format(i, "[tp:00000000{:02x}]".format(i)))
        log = self.state()["promptLog"]
        self.assertEqual([r["n"] for r in log], list(range(3, 11)))  # the last 8 of 10, oldest first
        self.assertEqual(log[-1]["tokens"], ["[tp:000000000a]"])
        self.assertEqual(self.state()["promptSubmits"], 10)
        self.assertNotIn("promptHash", self.state())  # no text hash: the id is the whole rule

    def test_the_hook_and_the_sender_use_the_same_id_pattern(self):
        self.assertIn("marker_re='{}'".format(pane_send.TOKEN_PATTERN), HOOK.read_text())
        self.assertRegex(pane_send.new_token(), "^" + pane_send.TOKEN_PATTERN + "$")

    def test_late_tool_does_not_reopen_stop_but_new_prompt_does(self):
        self.hook("SessionStart", model="claude-opus-5-5")
        self.assertNotIn("promptSubmits", self.state())  # SessionStart is not a submit
        self.hook("UserPromptSubmit", prompt="first")
        submit = self.state()  # the record lib/pane_send.py waits on: count, and a log entry with the ids in the prompt
        self.assertEqual(submit["promptSubmits"], 1)
        self.assertEqual(submit["promptLog"], [{"n": 1, "at": submit["updated"], "tokens": []}])
        self.hook("PreToolUse", tool_name="Read", tool_input={"file_path": "fixture.txt"})
        self.assertEqual({k: self.state()[k] for k in ("promptSubmits", "promptLog")},
                         {k: submit[k] for k in ("promptSubmits", "promptLog")})  # other events leave it
        self.hook("Stop", last_assistant_message="first reply\nRESULT_STATUS: complete")
        self.assertEqual(self.state()["state"], "waiting")
        self.review(accepted=True)
        self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "printf ''"})  # concurrency: see parallel test
        self.hook("PostToolUse", tool_name="Bash")
        self.hook("Notification", message="permission needed")
        self.hook("Stop", last_assistant_message="stale stop")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.state()["tools"], 1)
        self.assertEqual((self.task / "result.md").read_text(), "first reply\nRESULT_STATUS: complete\n")
        self.assertFalse(list(self.task.glob(".state.*")) + list(self.task.glob(".result.*")))

        self.hook("UserPromptSubmit", prompt="confirm [tp:abcdef0123]")
        self.assertEqual(self.state()["state"], "working")
        self.assertEqual(self.state()["promptTurn"], 2)
        self.assertEqual(self.state()["promptSubmits"], 2)
        self.assertEqual([(r["n"], r["tokens"]) for r in self.state()["promptLog"]],
                         [(1, []), (2, ["[tp:abcdef0123]"])])  # in order
        self.assertFalse((self.task / "result.md").exists())
        self.review()  # old answer cannot be reviewed for the new prompt
        self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "printf ''"})
        self.assertEqual(self.state()["state"], "tool")
        self.review()  # even a stray old result cannot authorize an active tool
        (self.task / "result.md").write_text("old reply")
        self.review()
        self.hook("PostToolUse", tool_name="Bash")
        self.assertEqual(self.state()["state"], "working")
        self.hook("Stop", last_assistant_message="new reply\nRESULT_STATUS: complete")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.state()["settledTurn"], 2)
        self.assertEqual((self.task / "result.md").read_text(), "new reply\nRESULT_STATUS: complete\n")
        self.review(accepted=True)

    def test_parallel_active_hooks_do_not_lose_counts_or_clobber_stop(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "printf ''"}), range(12)))
        self.assertEqual(self.state()["tools"], 12)
        self.assertEqual(self.state()["state"], "tool")
        self.hook("Stop", last_assistant_message="settled")
        self.hook("PreToolUse", tool_name="Bash", tool_input={"command": "printf ''"})
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.state()["tools"], 12)
        self.assertEqual((self.task / "result.md").read_text(), "settled\n")

    def test_empty_stop_cannot_reuse_old_result(self):
        self.hook("SessionStart")
        self.hook("UserPromptSubmit")
        self.hook("Stop", last_assistant_message="reply")
        self.hook("UserPromptSubmit")
        self.hook("Stop", last_assistant_message="")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertFalse((self.task / "result.md").exists())
        self.review()

    def test_prompts_and_stops_never_touch_report_md(self):
        (self.task / "report.md").write_text("worker's report\n")
        for event, fields in (("UserPromptSubmit", {"prompt": "go"}), ("Stop", {"last_assistant_message": "RESULT_STATUS: complete\nDone."}),
                              ("UserPromptSubmit", {"prompt": "again"}), ("Stop", {"last_assistant_message": ""})):
            self.hook(event, **fields)
        self.assertEqual((self.task / "report.md").read_text(), "worker's report\n")

    def transcript(self, lines):
        path = self.base / "t.jsonl"
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return str(path)

    @staticmethod
    def msg(mid, model, out, inp=1, cr=10, cw=2):
        return {"type": "assistant", "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": cr,
            "cache_creation_input_tokens": cw}}}

    def test_usage_by_model_dedupes_and_ignores_synthetic(self):
        path = self.transcript([
            self.msg("a", "m-one", 5), self.msg("a", "m-one", 20),  # streaming duplicate: last wins
            self.msg("b", "m-two", 7),
            self.msg("c", "m-one", 3),
            self.msg("s", "<synthetic>", 0, inp=0, cr=0, cw=0),
        ])
        self.hook("SessionStart", model="m-one")
        self.hook("UserPromptSubmit", transcript_path=path)
        self.hook("PostToolUse", tool_name="Bash", transcript_path=path)
        self.hook("Stop", last_assistant_message="r", transcript_path=path)
        s = self.state()
        self.assertEqual(s["model"], "m-one")  # last real model, not <synthetic>
        self.assertEqual(s["turns"], 4)
        self.assertEqual(s["tokens"], {"input": 3, "output": 30, "cacheRead": 30, "cacheWrite": 6})
        self.assertEqual(s["byModel"], {
            "m-one": {"input": 2, "output": 23, "cacheRead": 20, "cacheWrite": 4},
            "m-two": {"input": 1, "output": 7, "cacheRead": 10, "cacheWrite": 2}})
        self.assertEqual(s["cost"], 0)
        self.assertIs(s["costKnown"], False)

    def test_tool_events_do_not_read_transcript(self):
        path = self.transcript([self.msg("a", "m-one", 5)])
        self.hook("UserPromptSubmit", transcript_path=path)
        self.hook("Stop", last_assistant_message="r", transcript_path=path)
        before = self.state()["tokens"]
        self.hook("UserPromptSubmit", transcript_path=self.transcript([self.msg("a", "m-one", 5), self.msg("b", "m-one", 9)]))
        self.hook("PostToolUse", tool_name="Bash", transcript_path=path)
        self.assertNotEqual(self.state()["tokens"], before)
        self.assertEqual(self.state()["turns"], 2)
        self.transcript([self.msg("a", "m-one", 5), self.msg("b", "m-one", 9), self.msg("c", "m-one", 9)])
        self.hook("PostToolUse", tool_name="Bash", transcript_path=str(self.base / "t.jsonl"))
        self.assertEqual(self.state()["turns"], 2)

    # --- RO1: the report is kept by what the reply says, never by who started the turn -------------------------------
    REPORT = "full report\nRESULT_STATUS: complete\n"
    REPLIES = {"complete": "r\nRESULT_STATUS: complete", "empty-status": "r\nRESULT_STATUS: empty",
               "blocked": "r\nRESULT_STATUS: blocked", "incomplete": "r\nRESULT_STATUS: incomplete",
               "bold": "r\n**RESULT_STATUS: complete**", "malformed": "r\nRESULT_STATUS: done", "partial": "r\nRESULT_STATUS:",
               "prefixed": "r\nNote: RESULT_STATUS: complete", "none": "nothing new", "empty": ""}
    VALID = ("complete", "empty-status", "blocked", "incomplete", "bold")  # what lib/result_status.py (tp-review's gate) accepts

    def turn(self, origin, reply):
        """One Claude turn, its origin shown to the hook the ways the engine can show it. The hook must not read any of
        them: none, human, task-notification, source-user (UserPromptSubmit source "user"), composer, unknown, lagging (a
        transcript that still shows only earlier human entries while this turn is a task notification)."""
        self.entries = getattr(self, "entries", [])
        fields = {}
        if origin == "human":
            fields["origin"] = {"kind": "human"}
            self.entries.append({"type": "user", "promptSource": "typed", "turnOrigin": "human", "origin": {"kind": "human"}})
        elif origin == "task-notification":
            fields.update(origin={"kind": "task-notification"}, prompt="<task-notification>\n<task-id>b1</task-id>\n</task-notification>")
            self.entries.append({"type": "user", "promptSource": "system", "origin": {"kind": "task-notification"}})
        elif origin in ("source-user", "composer"):
            fields.update({"source": "user", "prompt": "Please confirm."} if origin == "source-user" else {"origin": {"kind": "composer"}})
            self.entries.append({"type": "user", "origin": {"kind": "composer"}})
        elif origin == "unknown":
            fields["origin"] = {"kind": "something-new"}
        elif origin == "lagging":
            fields["origin"] = {"kind": "task-notification"}
            self.entries = [{"type": "user", "origin": {"kind": "human"}}] * 3
        self.hook("UserPromptSubmit", **fields)
        self.hook("Stop", last_assistant_message=reply,
                  **({"transcript_path": self.transcript(self.entries)} if self.entries else {}))

    def kept(self):
        return sorted(p.read_text() for p in (self.task / "results").glob("*.md")) if (self.task / "results").is_dir() else []

    def later(self):
        state = self.state()
        return state.get("laterReplies", 0), state.get("laterReplyPath", "")

    def test_task_notification_turn_leaves_the_report_and_records_its_reply(self):
        self.turn("none", self.REPORT.rstrip("\n"))
        for n in (1, 2, 3):
            self.turn("task-notification", f"nothing new {n}")
            self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
            self.assertEqual(self.later(), (n, f"results/{n + 1}.md"))
            self.assertEqual((self.task / self.state()["laterReplyPath"]).read_text(), f"nothing new {n}\n")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual(self.kept().count(self.REPORT), 1)  # kept apart from result.md once, not once per turn
        self.review(accepted=True)

    def test_inspect_and_list_rows_show_a_later_reply(self):
        import tp_agent_inproc
        agent = tp_agent_inproc.load()
        self.turn("none", self.REPORT.rstrip("\n"))
        self.assertNotIn("later_replies", agent.record(self.task))
        self.turn("task-notification", "nothing new")
        row = agent.record(self.task)
        self.assertEqual((row["later_replies"], row["later_reply_path"]), (1, str(self.task / "results/2.md")))
        self.assertEqual(row["result_status"], "complete")
        index = [r for k in ("active", "pending", "paused", "unstatused", "recent") for r in agent.build_index(self.state_dir)[k]
                 if r["id"] == "w1"]
        self.assertTrue(index and all(r.get("later_replies") == 1 for r in index), index)

    def test_a_statused_reply_replaces_the_report_and_an_unstatused_one_never_does(self):
        self.turn("none", self.REPORT.rstrip("\n"))
        self.turn("human", "second\nRESULT_STATUS: blocked")
        self.assertEqual((self.task / "result.md").read_text(), "second\nRESULT_STATUS: blocked\n")
        self.assertIn(self.REPORT, self.kept())
        self.assertEqual(self.later(), (0, ""))
        self.hook("UserPromptSubmit", prompt="msg", origin={"kind": "human"})
        self.assertFalse((self.task / "result.md").exists())  # the next prompt withdraws the old reply while it works
        self.hook("Stop", last_assistant_message="short")  # a human-asked turn without a status does not replace it either
        self.assertEqual((self.task / "result.md").read_text(), "second\nRESULT_STATUS: blocked\n")
        self.assertEqual((self.task / self.later()[1]).read_text(), "short\n")

    def test_overlapping_turns_keep_the_report_and_both_replies(self):
        self.turn("none", self.REPORT.rstrip("\n"))
        self.hook("UserPromptSubmit", prompt="msg")
        self.hook("UserPromptSubmit", origin={"kind": "task-notification"})  # before the first turn's Stop
        self.hook("Stop", last_assistant_message="meh")
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual(self.later()[0], 1)
        self.hook("Stop", last_assistant_message="second")  # the other turn's Stop: no open prompt, settles nothing
        self.hook("Stop", last_assistant_message="second")  # a duplicate of it adds nothing
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual(self.later()[0], 2)
        self.assertEqual((self.task / self.later()[1]).read_text(), "second\n")
        self.assertEqual(sorted(self.kept()), sorted([self.REPORT, "meh\n", "second\n"]))

    def test_reply_decides_attack_table(self):
        # prior result.md x reply x turn origin (rows from evidence/attack.py; the origin never changes the answer).
        # The 'lagging' row was F1, 'source-user' F3 of review-coder-c28747a7ad-220316.
        rows = (("status", "task-notification", "none"), ("status", "lagging", "none"), ("status", "source-user", "none"),
                ("status", "composer", "complete"), ("status", "human", "partial"), ("status", "unknown", "malformed"),
                ("status", "none", "prefixed"), ("status", "human", "bold"), ("status", "task-notification", "incomplete"),
                ("status", "none", "empty"), ("plain", "task-notification", "none"), ("plain", "human", "empty"),
                ("none", "unknown", "blocked"), ("none", "composer", "empty-status"))
        prior_text = {"none": None, "status": self.REPORT, "plain": "plain old reply\n"}
        for prior, origin, reply in rows:
            with self.subTest(prior=prior, origin=origin, reply=reply):
                self.tearDown_task()
                self.entries = []
                if prior_text[prior] is not None:
                    self.turn("none", prior_text[prior].rstrip("\n"))
                self.turn(origin, self.REPLIES[reply])
                protected = prior == "status" and reply not in self.VALID
                want = prior_text[prior] if protected else (self.REPLIES[reply] + "\n" if self.REPLIES[reply] else None)
                got = (self.task / "result.md").read_text() if (self.task / "result.md").exists() else None
                self.assertEqual(got, want)
                if prior_text[prior] is not None:
                    self.assertIn(prior_text[prior], self.kept())  # the old reply is never lost
                self.assertEqual(self.later()[0], 1 if protected and reply != "empty" else 0)
                if self.later()[0]:
                    self.assertEqual((self.task / self.later()[1]).read_text(), self.REPLIES[reply] + "\n")

    def tearDown_task(self):
        for p in list(self.task.iterdir()):
            if p.name not in ("meta.json", "task.md", "packet.md"):
                if p.is_dir():
                    for q in p.iterdir():
                        q.unlink()
                    p.rmdir()
                else:
                    p.unlink()

    def test_an_archive_write_never_overwrites(self):
        # state written before RO1 (no resultTurn), with results/ already holding the next numbers (F2)
        (self.task / "state.json").write_text(json.dumps({"state": "waiting", "promptTurn": 2, "settledTurn": 2}))
        (self.task / "result.md").write_text(self.REPORT)
        (self.task / "results").mkdir()
        before = {n: f"kept before {n}\n" for n in (2, 3, 4)}
        for n, text in before.items():
            (self.task / f"results/{n}.md").write_text(text)
        for reply in ("short A", "short B", "short C"):
            self.turn("task-notification", reply)
        self.assertEqual({n: (self.task / f"results/{n}.md").read_text() for n in before}, before)
        self.assertEqual(sorted(self.kept()), sorted([*before.values(), self.REPORT, "short A\n", "short B\n", "short C\n"]))
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual(self.later()[0], 3)
        self.assertEqual((self.task / self.later()[1]).read_text(), "short C\n")
        self.assertFalse(list((self.task / "results").glob(".keep.*")))
        # no hard links (ln always fails): a noclobber write takes over, never over a name that exists, and loses nothing
        fake_ln = Path(self.env["PATH"].split(os.pathsep)[0]) / "ln"
        fake_ln.write_text("#!/bin/sh\nexit 1\n")
        fake_ln.chmod(0o755)
        fresh = "not archived yet\nRESULT_STATUS: complete\n"
        (self.task / "result.md").write_text(fresh)
        self.turn("task-notification", "short D")
        self.assertEqual((self.task / "result.md").read_text(), fresh)
        self.assertEqual((self.task / self.later()[1]).read_text(), "short D\n")
        self.assertEqual({n: (self.task / f"results/{n}.md").read_text() for n in before}, before)
        self.assertTrue({fresh, "short D\n"} <= set(self.kept()))
        self.assertFalse(list((self.task / "results").glob(".keep.*")))
        # nothing can be created under results/: result.md and every reply still stay on disk, each one recorded
        (self.task / "results").chmod(0o555)
        self.addCleanup((self.task / "results").chmod, 0o755)
        unkept = "no copy anywhere\nRESULT_STATUS: complete\n"
        (self.task / "result.md").write_text(unkept)
        for reply in ("short E", "final\nRESULT_STATUS: complete"):
            self.turn("task-notification", reply)
            self.assertEqual((self.task / "result.md").read_text(), unkept)
            self.assertEqual((self.task / self.later()[1]).read_text(), reply + "\n")
        self.assertEqual(sorted(p.read_text() for p in self.task.glob(".reply.*")),
                         ["final\nRESULT_STATUS: complete\n", "short E\n"])

    def test_stop_without_an_open_prompt_keeps_its_reply_and_changes_nothing_else(self):
        self.hook("Stop", last_assistant_message="orphan\nRESULT_STATUS: complete")
        self.assertFalse((self.task / "result.md").exists())
        self.assertEqual(self.kept(), ["orphan\nRESULT_STATUS: complete\n"])
        self.turn("none", self.REPORT.rstrip("\n"))
        before = self.kept()
        self.hook("Stop", last_assistant_message=self.REPORT.rstrip("\n"))  # a duplicate Stop: nothing new to keep
        self.assertEqual((self.kept(), self.later()), (before, (0, "")))
        self.hook("Stop", last_assistant_message="late short")
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual(self.state()["state"], "waiting")
        self.assertEqual((self.task / self.later()[1]).read_text(), "late short\n")


if __name__ == "__main__":
    unittest.main()
