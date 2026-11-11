"""The probe the runner matrix of test_session_env starts under each way of running the suite. It records which session keys the
test process holds and the LM Studio budget and counter it got; it does nothing unless the matrix asks (SESSION_PROBE_OUT)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import unittest

OUT = "SESSION_PROBE_OUT"
KEYS = ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_TRIAL_ID", "CLAUDE_CODE_SESSION_ID",
        "TP_MODE", "TP_SESSION", "TP_REDACTION_LAUNCH", "TP_REDACTION_SESSION")  # a copy, so a wrong SESSION_KEYS cannot hide here


@unittest.skipUnless(os.environ.get(OUT), "started by the runner matrix in test_session_env")
class Probe(unittest.TestCase):
    def test_probe(self):
        counter = os.environ.get("TP_LMSTUDIO_BUDGET_FILE")
        count = None
        if counter and os.path.exists(counter):
            with open(counter) as handle:
                count = handle.read()
        with open(os.environ[OUT], "w") as handle:
            json.dump({"session": sorted(key for key in KEYS if key in os.environ), "budget": os.environ.get("TP_LMSTUDIO_BUDGET"),
                       "counter": counter, "count": count, "pid": os.getpid()}, handle)


if __name__ == "__main__":
    unittest.main()
