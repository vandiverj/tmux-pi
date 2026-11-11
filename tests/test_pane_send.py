"""lib/pane_send.py against a fake worker TUI in a private tmux server (no Claude, no pi, no model)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from private_tmux import PrivateTmux  # noqa: E402

TEXT = "Use the local HTML renderer for the evidence"
# The production message (2026-11-10) was 1089 characters; of it and the id typed after it, only the last 83 characters
# reached the worker (MS2). Same length here.
LONG = ("Main: two items are still owed. 1) The four cells: /usr/bin/python3 and $(mktemp -d) 'outside' as env -u TMUX "
        "PY -m unittest tests.test_main_notify. " * 12)[:1089]
FAST = {"settle": 0.3, "confirm": 0.8}  # the retry bound is shortened; the settle time stays the real one


class PaneSendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PrivateTmux()
        cls.saved = dict(os.environ)
        os.environ.update(cls.server.env())
        os.environ.pop("TMUX", None)
        global pane_send
        import pane_send  # noqa: E402  after PATH points at the private tmux

    @classmethod
    def tearDownClass(cls):
        cls.server.close()
        os.environ.clear()
        os.environ.update(cls.saved)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.task = Path(temp.name)

    def claude_worker(self, mode="normal", state=True, **kwargs):
        """A worker whose task says `worker: claude` (meta.json). state=False: the hook has not written state.json yet."""
        (self.task / "meta.json").write_text(json.dumps({"id": "w", "worker": "claude"}))
        if state:
            (self.task / "state.json").write_text(json.dumps({"harness": "claude", "promptSubmits": 4}))
        return self.server.worker(mode, task_dir=self.task, **kwargs)

    def box_text(self, entry):
        """What the worker took in: the text without the delivery id send() puts before it."""
        self.assertRegex(entry, r"^" + pane_send.TOKEN_PATTERN + r" ")
        return re.sub(r"^" + pane_send.TOKEN_PATTERN + r" ", "", entry)

    def ids_typed(self, raw):
        """The delivery ids that arrived at the worker as typed input, in order."""
        return pane_send.TOKEN_RE.findall("".join(json.loads(line) for line in raw.read_text().splitlines()))

    def state(self):
        return json.loads((self.task / "state.json").read_text())

    def wait_for(self, condition):
        deadline = time.monotonic() + 10
        while not condition():  # waits on the event itself, bounded
            self.assertLess(time.monotonic(), deadline, "event never happened")
            time.sleep(0.02)

    def test_immediate_enter_is_a_paste_and_the_helper_gets_a_submit(self):
        old, old_log, _ = self.claude_worker()
        # The old tp-agent sequence: text, then Enter at once. The fake reads the Enter as part of a paste.
        self.server.tmux("send-keys", "-t", old, "-l", "old way")
        self.server.tmux("send-keys", "-t", old, "Enter")
        self.assertEqual(self.state()["promptSubmits"], 4)
        self.assertEqual(self.server.submitted(old_log), [])
        # The helper pauses before Enter, so the fake sees a real submit and the record moves.
        pane, log, raw = self.server.worker("normal", task_dir=self.task)
        result = pane_send.send(pane, "line one\nline two", task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 1})
        self.assertEqual([self.box_text(t) for t in self.server.submitted(log)], ["line one line two"])
        self.assertEqual(self.state()["promptSubmits"], 5)

    def test_never_confirmed_reports_false_after_every_enter_and_types_once(self):
        pane, log, raw = self.claude_worker("never")
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertEqual(self.server.submitted(log), [])
        self.assertEqual(self.server.typed(raw, TEXT), 1)

    def test_second_enter_confirms_on_the_retry_and_the_text_and_its_id_are_typed_once(self):
        pane, log, raw = self.claude_worker("second")
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 2})
        self.assertEqual([self.box_text(t) for t in self.server.submitted(log)], [TEXT])
        self.assertEqual(self.server.typed(raw, TEXT), 1)
        self.assertEqual(len(self.ids_typed(raw)), 1)  # the retry pressed Enter again; it did not make a second id

    def test_a_claude_task_with_no_state_yet_is_never_confirmed_by_the_pane(self):
        # F1: the loading screen never showed the text, so the pane check would read "gone" as submitted. A Claude task
        # (meta.json worker) always needs its own submit record; a missing state.json is no record.
        pane, log, raw = self.claude_worker("blind", state=False)
        self.assertFalse((self.task / "state.json").exists())
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertEqual(self.server.typed(raw, TEXT), 1)

    def test_the_harness_comes_from_meta_then_state(self):
        for meta, state, expected in (({"worker": "claude"}, {}, "claude"), ({"worker": "pi"}, {"harness": "claude"}, "pi"),
                                      ({}, {"harness": "claude"}, "claude"), ({"worker": "other"}, {}, None), ({}, {}, None)):
            with self.subTest(meta=meta, state=state):
                (self.task / "meta.json").write_text(json.dumps(meta))
                (self.task / "state.json").write_text(json.dumps(state))
                self.assertEqual(pane_send.harness(self.task), expected)
        self.assertIsNone(pane_send.harness(None))

    def test_a_record_that_holds_another_sends_id_does_not_confirm(self):
        # A hook record lands after the send began, so the count rises, but its prompt holds another send's id; this
        # Enter is ignored.
        pane, log, raw = self.claude_worker("never", foreign=True)
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertGreater(self.state()["promptSubmits"], 4)  # the counter did rise
        self.assertEqual(self.server.submitted(log), [])

    def test_a_record_with_this_sends_id_confirms_even_after_an_older_one(self):
        pane, log, raw = self.claude_worker("normal", foreign=True)  # the older record lands first, then ours
        result = pane_send.send(pane, "line one\nline two", task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 1})
        entries = self.state()["promptLog"]
        self.assertEqual([e["n"] for e in entries], [5, 6])
        self.assertEqual(entries[0]["tokens"], ["[tp:ffffffffff/16]"])
        self.assertEqual(entries[1]["tokens"], self.ids_typed(raw))
        self.assertEqual([self.box_text(t) for t in self.server.submitted(log)], ["line one line two"])

    def test_the_same_text_sent_twice_gets_a_new_id_and_the_first_record_does_not_confirm_the_second(self):
        first, log1, raw1 = self.claude_worker("normal")
        self.assertEqual(pane_send.send(first, TEXT, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
        # The first record (n=5) is in state.json. The second send's worker never submits.
        second, log2, raw2 = self.server.worker("never", task_dir=self.task)
        self.assertEqual(pane_send.send(second, TEXT, task_dir=self.task, **FAST),
                         {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        # And a real second submit, with a record of its own, is confirmed.
        third, log3, raw3 = self.server.worker("normal", task_dir=self.task)
        self.assertEqual(pane_send.send(third, TEXT, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
        ids = [self.ids_typed(r) for r in (raw1, raw2, raw3)]
        self.assertEqual([len(i) for i in ids], [1, 1, 1])
        self.assertEqual(len({i[0] for i in ids}), 3)  # a new id for every call

    def test_a_delayed_record_of_the_same_text_does_not_confirm_the_next_send(self):
        # F1: send A reaches the worker but its hook is slow. Send B of the same text has its Enter ignored. A's record
        # lands while B waits. It holds A's id, not B's, so B is not submitted.
        first, log1, raw1 = self.claude_worker("normal", hold=True)
        a = pane_send.send(first, TEXT, task_dir=self.task, retries=0, **FAST)
        self.assertEqual(a, {"submitted": False, "attempts": 1})  # the worker took it; no record yet
        self.assertEqual(len(self.server.submitted(log1)), 1)
        second, log2, raw2 = self.server.worker("never", task_dir=self.task)
        result = {}
        sender = threading.Thread(target=lambda: result.update(pane_send.send(second, TEXT, task_dir=self.task, **FAST)))
        sender.start()
        self.wait_for(lambda: self.server.typed(raw2, TEXT) == 1)  # B is typed and waiting for its record
        self.server.release(log1)
        self.wait_for(lambda: len(self.state().get("promptLog", [])) == 1)  # A's record has landed
        sender.join(timeout=30)
        self.assertFalse(sender.is_alive())
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertEqual(self.state()["promptLog"][0]["tokens"], self.ids_typed(raw1))
        self.assertNotEqual(self.ids_typed(raw1), self.ids_typed(raw2))

    def test_a_message_that_did_not_arrive_whole_is_not_submitted_and_says_what_arrived(self):
        # MS2, on a pane. Round 1's production case: the worker got only the last 83 characters of the 1089 (typed keys).
        # The id now goes first, so a lost start takes the id with it: no record of this send, the usual retries, not
        # submitted, and the text was put in once.
        pane, log, raw = self.claude_worker("normal", options=("--drop-first", "1022"))
        result = pane_send.send(pane, LONG, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertLess(len(self.server.submitted(log)[0]), 100)
        self.assertEqual(self.server.typed(raw, LONG), 1)
        # The id and the start arrived, the end did not: not submitted, how much arrived, one Enter.
        pane, log, raw = self.server.worker("normal", task_dir=self.task, options=("--keep-first", "500"))
        result = pane_send.send(pane, LONG, task_dir=self.task, **FAST)
        self.assertFalse(result["submitted"], result)
        token = self.ids_typed(raw)[0]
        arrived = self.server.submitted(log)[0]
        self.assertTrue(arrived.startswith(token + " "))
        self.assertEqual(len(arrived), 500)
        self.assertEqual(result, {"submitted": False, "attempts": 1, "length": len(pane_send.normalize(LONG)),
                                  "received": len(pane_send.normalize(arrived[len(token):]))})
        self.assertEqual(self.server.typed(raw, LONG), 1)
        self.assertEqual("".join(json.loads(line) for line in raw.read_text().splitlines()).count("\r"), 1)
        # The whole text after other text already in the box, arriving twice, or wrapped in <pasted_content> tags the way
        # Claude Code 2.1.322 puts a long paste in the prompt (main-repro), is the whole text: submitted.
        for options in (("--prefill", "words already in the box "), ("--twice",), ("--wrap", "800")):
            with self.subTest(options=options):
                pane, log, raw = self.server.worker("normal", task_dir=self.task, options=options)
                self.assertEqual(pane_send.send(pane, LONG, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
                if options[0] == "--wrap":
                    self.assertRegex(self.server.submitted(log)[0], r'^\n\n<pasted_content id="1f02">\n\[tp:')

    def test_a_message_never_switches_the_workers_input_mode(self):
        # MS2 round 2: a first "!" puts Claude Code's box in bash mode (typed or pasted; main-repro), and pi runs it as
        # bash. The id goes first, so the text can start with anything and still goes in as a prompt, whole.
        for text in ("!rm -rf build and then tell me", "  !echo hi", "/clear the cache", "? what next", "@README.md read it",
                     "# note", "& in the background"):
            with self.subTest(text=text):
                pane, log, raw = self.claude_worker("normal", options=("--bang",))
                self.assertEqual(pane_send.send(pane, text, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
                self.assertEqual(self.server.ran_as_bash(log), [])
                self.assertEqual([self.box_text(t) for t in self.server.submitted(log)], [text])
        pane, log, raw = self.server.worker("normal", options=("--bang",))  # the typed-keys path (pi, no task dir)
        self.assertEqual(pane_send.send(pane, "!echo hi", **FAST), {"submitted": True, "attempts": 1})
        self.assertEqual(self.server.ran_as_bash(log), [])

    def test_whitespace_and_control_characters_do_not_matter_the_id_confirms(self):
        # F2: U+001C..U+001F are whitespace to Python's str.split and \s, not to jq; a no-break space likewise. No text is
        # compared, so none of it can make hook and sender disagree.
        text = "a\x1cb\x1dc\x1ed\x1fe\t\tf   g h"
        pane, log, raw = self.claude_worker("normal")
        self.assertEqual(pane_send.send(pane, text, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
        self.assertEqual([self.box_text(t) for t in self.server.submitted(log)], [re.sub(r"[\x00-\x1f]", " ", text)])  # pasted as spaces

    def test_a_worker_without_a_submit_record_uses_the_pane_check(self):
        # A pi task (meta.json worker "pi"; reporter.ts has no prompt-submit record), or a send with no task dir: the
        # text leaving the box is the signal. Even a state.json that says claude does not move a pi task to records.
        (self.task / "meta.json").write_text(json.dumps({"id": "w", "worker": "pi"}))
        (self.task / "state.json").write_text(json.dumps({"harness": "claude", "promptSubmits": 4}))
        for mode, expected in (("normal", {"submitted": True, "attempts": 1}),
                               ("second", {"submitted": True, "attempts": 2}),
                               ("never", {"submitted": False, "attempts": 1 + pane_send.RETRIES})):
            with self.subTest(mode=mode):
                pane, log, raw = self.server.worker(mode)
                self.assertEqual(pane_send.send(pane, TEXT, task_dir=self.task, **FAST), expected)
                self.assertEqual(self.server.typed(raw, TEXT), 1)
        # PD1: a screen with malformed bytes (0xa0, lone 0xff, truncated 0xc3) beside a valid prompt probe still reads right.
        stubs = self.task / "pd1-stubs"
        stubs.mkdir()
        for name, junk in (("0xa0", r"a \240 b"), ("lone 0xff", r"\377"), ("truncated multibyte", r"caf\303")):
            with self.subTest(malformed=name):
                (stubs / "tmux").write_text(f"#!/bin/sh\nprintf '%b\\n' '{junk}' 'prompt> {TEXT}'\n")
                (stubs / "tmux").chmod(0o755)
                with unittest.mock.patch.dict(os.environ, {"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}):
                    self.assertTrue(pane_send.pane_has_text("%0", TEXT))
                    self.assertFalse(pane_send.pane_has_text("%0", "something never shown"))
        pane, log, raw = self.server.worker("normal")
        self.assertEqual(pane_send.send(pane, TEXT, **FAST), {"submitted": True, "attempts": 1})  # no task dir
        # The stated limit (MS2): the pane check sees only the end, so a long message whose start was lost reads as submitted.
        pane, log, raw = self.server.worker("normal", options=("--drop-first", "1022"))
        self.assertEqual(pane_send.send(pane, LONG, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
        self.assertLess(len(self.server.submitted(log)[0]), 100)

    def attack_send(self, text, box, cut):
        """send() with tmux replaced: the first Enter makes the hook record for `box` + cut(sent, n), n being the length of
        the "[tp:...] " put in before the text; cut returns None for no record at all. Later Enters find an empty box.
        Also checks the Claude send path: one load-buffer from stdin into a named buffer, one paste-buffer -p -d of that
        buffer, no typed keys, and the id first."""
        sent, buffers = [], {}

        def tmux(*args, **kwargs):
            if args[0] == "load-buffer":
                self.assertEqual(args, ("load-buffer", "-b", args[2], "-"))
                buffers[args[2]] = kwargs["input"].decode("utf-8", "surrogatepass")
            elif args[0] == "paste-buffer":
                self.assertEqual(args[:5], ("paste-buffer", "-p", "-d", "-b", args[4]))
                sent.append(buffers.pop(args[4]))
            elif args[-1] == "Enter" and sent:
                whole = sent.pop()
                self.assertRegex(whole, "^" + pane_send.TOKEN_PATTERN + " ")
                self.assertNotRegex(whole, "[\\x00-\\x1f\\x7f]")
                prompt = cut(whole, len(whole) - len(pane_send.flatten(text)))
                if prompt is not None:
                    marks = pane_send.prompt_marks(box + prompt)
                    (self.task / "state.json").write_text(json.dumps({"harness": "claude", "promptLog": [
                        {"n": 1, "at": "t", "tokens": [m["id"] for m in marks], "marks": marks}]}))
            else:
                self.assertEqual(args[0], "send-keys")
                self.assertNotIn("-l", args)
            return subprocess.CompletedProcess(args, 0)
        with unittest.mock.patch.object(pane_send, "_tmux", tmux):
            result = pane_send.send("%1", text, task_dir=self.task, settle=0, confirm=0)
        self.assertEqual(buffers, {})
        return result

    def test_only_the_whole_text_with_its_id_is_submitted(self):
        # MS2 attack table, in-process: content x length x box state x what arrives, against send() with a fake hook record
        # (pane_send.prompt_marks, which test_claude_report keeps equal to report.sh).
        (self.task / "meta.json").write_text(json.dumps({"id": "w", "worker": "claude"}))
        contents = {"plain": "Use the local renderer for the evidence. ", "dollar": "cost $HOME and $(date) ",
                    "quotes": "it's \"quoted\" and 'single' ", "backslash": "back\\slash \\n not a newline ",
                    "bang": "!echo at the start then words ", "slash": "/clear the box then words ",
                    "question": "? help then words ", "at": "@README.md then words ", "hash": "# remember this ",
                    "amp": "& in the background ", "newlines": "line one\nline two\n\nline four ",
                    "unicode": "Grüße – naïve café ✓ 漢字 ", "control": "a\x1cb\x1dc\x1ed\x1fe\tf ",
                    "escape": "\x1b[201~ then words that would be typed keys "}
        wrap = ('\n\n<pasted_content id="1f02">\n', '\n</pasted_content id="1f02">\n')  # Claude Code 2.1.322, main-repro
        arrivals = {  # what reaches the prompt from the sent `t` ("[tp:...] " + text); n is the length of "[tp:...] "
            "all": (lambda t, n: t, "the whole text after its id", True),
            "wrapped": (lambda t, n: wrap[0] + t + wrap[1], "a long paste as Claude Code puts it: tags around id and text", True),
            "twice": (lambda t, n: t + " " + t, "the whole text is there (twice)", True),
            "end lost": (lambda t, n: t[:len(t) - max(1, (len(t) - n) // 10)], "the id and the start arrived", "partial"),
            "middle lost": (lambda t, n: t[:n + 4] + t[n + 16:], "characters missing between id and end", "partial"),
            "start lost": (lambda t, n: t[n + max(1, (len(t) - n) // 10):], "the id went with the start: no record", False),
            "nothing": (lambda t, n: None, "no submit record at all", False)}
        boxes = ("", "words already in the box ", "[tp:ffffffffff/16] an earlier message ")
        counts = {}
        for kind, unit in contents.items():
            for size in (40, 1000, 1105, 6000):
                text = (unit * (size // len(unit) + 1))[:size]
                length = len(pane_send.normalize(pane_send.flatten(text)))
                for box in boxes:
                    for arrive, (cut, reason, want) in arrivals.items():
                        with self.subTest(kind=kind, size=size, box=box[:6], arrive=arrive, why=reason):
                            (self.task / "state.json").write_text(json.dumps({"harness": "claude"}))
                            result = self.attack_send(text, box, cut)
                            if want is True:
                                self.assertEqual(result, {"submitted": True, "attempts": 1})
                            elif want == "partial":  # the record is back: answer at once, say what arrived, no more Enters
                                self.assertEqual((result["submitted"], result["attempts"], result["length"]), (False, 1, length))
                                self.assertLess(result["received"], length)
                            else:  # this send's id never came back: the usual Enter retries; nothing known about arrival
                                self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
                            counts[arrive] = counts.get(arrive, 0) + 1
        self.assertEqual(counts, {a: len(contents) * 4 * len(boxes) for a in arrivals})  # 14 x 4 x 3 x 7 = 1176 rows

    def test_tmux_failure_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            pane_send.send("%99999", TEXT, **FAST)


if __name__ == "__main__":
    unittest.main()
