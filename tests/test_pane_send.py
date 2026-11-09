"""lib/pane_send.py against a fake worker TUI in a private tmux server (no Claude, no pi, no model)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from private_tmux import PrivateTmux  # noqa: E402

TEXT = "Use the local HTML renderer for the evidence"
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

    def test_immediate_enter_is_a_paste_and_the_helper_gets_a_submit(self):
        old, old_log, _ = self.claude_worker()
        # The old tp-agent sequence: text, then Enter at once. The fake reads the Enter as part of a paste.
        self.server.tmux("send-keys", "-t", old, "-l", "old way")
        self.server.tmux("send-keys", "-t", old, "Enter")
        self.assertEqual(pane_send.submits(self.task), 4)
        self.assertEqual(self.server.submitted(old_log), [])
        # The helper pauses before Enter, so the fake sees a real submit and the record moves.
        pane, log, raw = self.server.worker("normal", task_dir=self.task)
        result = pane_send.send(pane, "line one\nline two", task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 1})
        self.assertEqual(self.server.submitted(log), ["line one line two"])
        self.assertEqual(pane_send.submits(self.task), 5)

    def test_never_confirmed_reports_false_after_every_enter_and_types_once(self):
        pane, log, raw = self.claude_worker("never")
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertEqual(self.server.submitted(log), [])
        self.assertEqual(self.server.typed(raw, TEXT), 1)

    def test_second_enter_confirms_on_the_retry_and_the_text_is_typed_once(self):
        pane, log, raw = self.claude_worker("second")
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 2})
        self.assertEqual(self.server.submitted(log), [TEXT])
        self.assertEqual(self.server.typed(raw, TEXT), 1)

    def test_a_claude_task_with_no_state_yet_is_never_confirmed_by_the_pane(self):
        # F1: the loading screen never showed the text, so the pane check would read "gone" as submitted. A Claude task
        # (meta.json worker) always needs its own submit record; a missing state.json is count 0 and no record.
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

    def test_a_delayed_older_record_does_not_confirm_this_message(self):
        # F2: the count rises after the snapshot, with another prompt's hash, and this Enter is ignored.
        pane, log, raw = self.claude_worker("never", foreign=True)
        result = pane_send.send(pane, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        self.assertGreater(pane_send.submits(self.task), 4)  # the counter did rise
        self.assertEqual(self.server.submitted(log), [])

    def test_a_record_with_this_messages_hash_confirms_even_after_an_older_one(self):
        pane, log, raw = self.claude_worker("normal", foreign=True)  # the older record lands first, then ours
        result = pane_send.send(pane, "line one\nline two", task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 1})
        log_entries = json.loads((self.task / "state.json").read_text())["promptLog"]
        self.assertEqual([e["n"] for e in log_entries], [5, 6])
        self.assertEqual(log_entries[1]["hash"], pane_send.prompt_hash("line one line two"))
        self.assertNotEqual(log_entries[0]["hash"], log_entries[1]["hash"])

    def test_the_same_text_sent_twice_is_not_confirmed_by_the_first_record(self):
        first, log1, raw1 = self.claude_worker("normal")
        self.assertEqual(pane_send.send(first, TEXT, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})
        # The first record (same hash, n=5) is in state.json. The second send's worker never submits.
        second, log2, raw2 = self.server.worker("never", task_dir=self.task)
        result = pane_send.send(second, TEXT, task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": False, "attempts": 1 + pane_send.RETRIES})
        # And a real second submit, with a record of its own, is confirmed.
        third, log3, raw3 = self.server.worker("normal", task_dir=self.task)
        self.assertEqual(pane_send.send(third, TEXT, task_dir=self.task, **FAST), {"submitted": True, "attempts": 1})

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
        pane, log, raw = self.server.worker("normal")
        self.assertEqual(pane_send.send(pane, TEXT, **FAST), {"submitted": True, "attempts": 1})  # no task dir

    def test_tmux_failure_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            pane_send.send("%99999", TEXT, **FAST)


if __name__ == "__main__":
    unittest.main()
