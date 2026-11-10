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

    # --- RO1: an unprompted turn must not replace a report that carries RESULT_STATUS ------------------------------
    REPORT = "full report\nRESULT_STATUS: complete\n"

    def turn(self, via, kind, reply, **extra):
        """One Claude turn whose origin reaches the hook `via` the hook input ("input"), the prompt text ("prompt"), the
        transcript read at Stop ("transcript"), or nowhere ("none"). kind is origin.kind: human, task-notification, hook."""
        self.entries = getattr(self, "entries", [])
        self.entries.append({"type": "user", "promptSource": "typed" if kind == "human" else "system", "origin": {"kind": kind}})
        fields = dict(extra)
        if via == "input":
            fields["origin"] = {"kind": kind}
        if via == "prompt" and kind == "task-notification":
            fields["prompt"] = "<task-notification>\n<task-id>b1</task-id>\n</task-notification>"
        stop = {}
        if via == "transcript":
            stop["transcript_path"] = self.transcript(self.entries)
        self.hook("UserPromptSubmit", **fields)
        self.hook("Stop", last_assistant_message=reply, **stop)

    def kept(self):
        return sorted(p.read_text() for p in (self.task / "results").glob("*.md")) if (self.task / "results").is_dir() else []

    def test_task_notification_turn_leaves_the_report_and_records_its_reply(self):
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        for n, via in enumerate(("input", "prompt", "transcript"), 1):
            self.turn(via, "task-notification", f"nothing new {n}")
            self.assertEqual((self.task / "result.md").read_text(), self.REPORT, via)
            self.assertEqual((self.state()["laterReplies"], self.state()["laterReplyPath"]), (n, f"results/{n + 1}.md"), via)
            self.assertEqual((self.task / self.state()["laterReplyPath"]).read_text(), f"nothing new {n}\n")
        self.assertEqual(self.state()["state"], "waiting")
        self.assertIn(self.REPORT, self.kept())  # the report is also kept apart from result.md
        self.review(accepted=True)

    def test_inspect_and_list_rows_show_a_later_reply(self):
        import tp_agent_inproc
        agent = tp_agent_inproc.load()
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        self.assertNotIn("later_replies", agent.record(self.task))
        self.turn("input", "task-notification", "nothing new")
        row = agent.record(self.task)
        self.assertEqual((row["later_replies"], row["later_reply_path"]), (1, str(self.task / "results/2.md")))
        self.assertEqual(row["result_status"], "complete")
        index = [r for k in ("active", "pending", "paused", "unstatused", "recent") for r in agent.build_index(self.state_dir)[k]
                 if r["id"] == "w1"]
        self.assertTrue(index and all(r.get("later_replies") == 1 for r in index), index)

    def test_a_message_turn_replaces_the_report_and_keeps_the_old_one(self):
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        self.turn("transcript", "human", "second\nRESULT_STATUS: blocked")
        self.assertEqual((self.task / "result.md").read_text(), "second\nRESULT_STATUS: blocked\n")
        self.assertIn(self.REPORT, self.kept())
        self.assertEqual((self.state()["laterReplies"], self.state()["laterReplyPath"]), (0, ""))
        # a human turn with a short reply replaces too (origin known), and the old report is still kept
        self.turn("transcript", "human", "short")
        self.assertEqual((self.task / "result.md").read_text(), "short\n")
        self.assertIn("second\nRESULT_STATUS: blocked\n", self.kept())

    def test_unprompted_turn_with_a_status_replaces_and_two_quick_turns_hold_the_report(self):
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        self.turn("input", "task-notification", "new\nRESULT_STATUS: incomplete")
        self.assertEqual((self.task / "result.md").read_text(), "new\nRESULT_STATUS: incomplete\n")
        self.assertIn(self.REPORT, self.kept())
        # human submit, then a notification submit before any Stop: the one Stop belongs to the newest origin
        self.entries = []
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        self.hook("UserPromptSubmit", prompt="msg")
        self.assertFalse((self.task / "result.md").exists())  # a human prompt withdraws the old reply while it works
        self.hook("UserPromptSubmit", origin={"kind": "task-notification"})
        self.hook("Stop", last_assistant_message="meh")
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual(self.state()["laterReplies"], 1)

    def test_origin_attack_table(self):
        # origin x prior result.md x reply. A reply without RESULT_STATUS replaces a report with one only when the turn
        # came from outside the worker (human); every other origin, and an origin nobody can tell, keeps it.
        prior_text = {"none": None, "status": self.REPORT, "plain": "plain old reply\n"}
        replies = {"status": "r\nRESULT_STATUS: complete", "short": "short", "empty": ""}
        for kind, via in (("human", "transcript"), ("task-notification", "input"), ("hook", "transcript"), ("human", "none")):
            for prior, reply in ((p, r) for p in prior_text for r in replies):
                with self.subTest(kind=kind, via=via, prior=prior, reply=reply):
                    self.tearDown_task()
                    self.entries = []
                    if prior_text[prior] is not None:
                        self.turn("none", "human", prior_text[prior].rstrip("\n"))
                    tell = via != "none"
                    outside = kind == "human" and tell
                    protected = prior == "status" and not outside and reply != "status"
                    self.turn(via, kind, replies[reply])
                    want = (prior_text[prior] if protected else None if replies[reply] == "" else replies[reply] + "\n")
                    got = (self.task / "result.md").read_text() if (self.task / "result.md").exists() else None
                    self.assertEqual(got, want)
                    if prior_text[prior] is not None:
                        self.assertIn(prior_text[prior], self.kept())  # the old reply is never lost
                    self.assertEqual(self.state().get("laterReplies", 0), 1 if protected and reply == "short" else 0)

    def tearDown_task(self):
        for p in list(self.task.iterdir()):
            if p.name not in ("meta.json", "task.md", "packet.md"):
                if p.is_dir():
                    for q in p.iterdir():
                        q.unlink()
                    p.rmdir()
                else:
                    p.unlink()

    def test_stop_without_an_open_prompt_and_duplicate_stop_change_nothing(self):
        self.hook("Stop", last_assistant_message="orphan\nRESULT_STATUS: complete")
        self.assertFalse((self.task / "result.md").exists())
        self.turn("none", "human", self.REPORT.rstrip("\n"))
        before = (self.state(), self.kept())
        self.hook("Stop", last_assistant_message="late short")
        self.assertEqual((self.task / "result.md").read_text(), self.REPORT)
        self.assertEqual((self.state()["laterReplies"] if "laterReplies" in self.state() else 0), 0)
        self.assertEqual(self.kept(), before[1])


if __name__ == "__main__":
    unittest.main()
