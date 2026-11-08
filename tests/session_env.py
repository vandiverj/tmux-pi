"""The session of whoever runs the suite must not reach a test. One list of the keys that name the caller's session, and the two
ways to drop them: a copy for a subprocess env, and a patch of os.environ for a test that loads a bin/ script in this process.

A test that wants a session id sets it explicitly, after calling these."""
import os
from unittest import mock

# Keys that identify the caller's own session (tmux, task, main, trial, Claude Code). Configuration such as TP_STATE_DIR,
# TP_WORK_DIR or TP_WORKDIR is not here: a fixture sets those itself. Checked against bin/ and lib/: TP_TASK_ID/TP_TASK_DIR/TP_MODE
# make the caller a worker (trust_guard.is_worker, owner_confirm), TP_MAIN_ID/CLAUDE_CODE_SESSION_ID name the main (tp-agent main_id),
# TP_MAIN_CWD/TP_TRIAL_ID come from the launchers, TP_SESSION is the tmux session name tp-agent and tp-trial read.
SESSION_KEYS = ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_TRIAL_ID",
                "CLAUDE_CODE_SESSION_ID", "TP_MODE", "TP_SESSION", "TP_REDACTION_LAUNCH", "TP_REDACTION_SESSION")


# What the caller's environment held, removed when the tests package was imported. Tests may read it; never put it back in os.environ.
CALLER_SESSION = {}


def drop_caller_session():
    """Delete every session key from os.environ and record what was there in CALLER_SESSION."""
    for key in SESSION_KEYS:
        if key in os.environ:
            CALLER_SESSION[key] = os.environ.pop(key)


def scrub():
    """Called when this module is first imported, so `import session_env` as the first line of a test module is the whole
    mechanism: whichever way the suite is started, it runs before any test code or tested module. Idempotent."""
    drop_caller_session()


def outside_session(env=None):
    """A copy of env (default os.environ) without the caller's session keys."""
    return {key: value for key, value in (os.environ if env is None else env).items() if key not in SESSION_KEYS}


def isolate_session(testcase):
    """Remove the session keys from os.environ for this test only; the undo is registered with addCleanup."""
    patch = mock.patch.dict(os.environ)
    patch.start()
    testcase.addCleanup(patch.stop)
    for key in SESSION_KEYS:
        os.environ.pop(key, None)


scrub()
