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

    def claude_worker(self, mode="normal"):
        (self.task / "state.json").write_text(json.dumps({"harness": "claude", "promptSubmits": 4}))
        return self.server.worker(mode, task_dir=self.task)

    def test_immediate_enter_is_a_paste_and_the_helper_gets_a_submit(self):
        pane, log, raw = self.claude_worker()
        # The old tp-agent sequence: text, then Enter at once. The fake reads the Enter as part of a paste.
        self.server.tmux("send-keys", "-t", pane, "-l", "old way")
        self.server.tmux("send-keys", "-t", pane, "Enter")
        self.assertEqual(pane_send.submits(self.task), 4)
        self.assertEqual(self.server.submitted(log), [])
        # The helper pauses before Enter, so the fake sees a real submit and the record moves.
        result = pane_send.send(pane, "line one\nline two", task_dir=self.task, **FAST)
        self.assertEqual(result, {"submitted": True, "attempts": 1})
        self.assertEqual(self.server.submitted(log), ["old way line one line two"])  # the pasted Enter is a space in the box
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

    def test_a_worker_without_a_submit_record_uses_the_pane_check(self):
        # No state.json harness "claude" (pi has no prompt-submit record): the text leaving the box is the signal.
        for mode, expected in (("normal", {"submitted": True, "attempts": 1}),
                               ("second", {"submitted": True, "attempts": 2}),
                               ("never", {"submitted": False, "attempts": 1 + pane_send.RETRIES})):
            with self.subTest(mode=mode):
                pane, log, raw = self.server.worker(mode)
                self.assertEqual(pane_send.send(pane, TEXT, task_dir=self.task, **FAST), expected)
                self.assertEqual(self.server.typed(raw, TEXT), 1)

    def test_tmux_failure_raises(self):
        with self.assertRaises(subprocess.CalledProcessError):
            pane_send.send("%99999", TEXT, **FAST)


if __name__ == "__main__":
    unittest.main()
