"""The one place tp-bench starts a process, and the one function that builds the environment of every child.

The bench is often started from inside a tmux-pi pane. That pane's TMUX / TMUX_PANE / TP_* keys name the OWNER's session;
`lib/tp.sh` `tp_session` reads TMUX before TP_SESSION, so a leaked key makes tp-exit or tp-reset act on the owner's workspace.
`child_env` therefore starts from the process environment, removes every session key, and adds only what the runner sets on
purpose. Tests hand the runner a fake with the same `run`; a fake that records `environment(env)` sees exactly what a child gets."""
import os
import subprocess
from collections import namedtuple
from pathlib import Path

Result = namedtuple("Result", "returncode stdout stderr")
TIMED_OUT = 124  # the exit code `timeout(1)` uses

# Every environment key bin/ and lib/ read to find a tmux-pi session, task or main. tests/test_bench_shell.py compares this
# list with a scan of bin/ and lib/, so a new key there fails the test until it is added here (or listed there as not a session key).
SESSION_KEYS = (
    "TMUX", "TMUX_PANE", "TP_SESSION", "TP_STATE_DIR", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD",
    "TP_TRIAL_ID", "CLAUDE_CODE_SESSION_ID", "TP_MODE",
)


def child_env(base, extra=None):
    """`base` without the session keys, plus `extra` (what the runner sets on purpose, values as strings)."""
    env = {k: v for k, v in base.items() if k not in SESSION_KEYS}
    env.update({k: str(v) for k, v in (extra or {}).items()})
    return env


class ShellError(RuntimeError):
    """A child exited nonzero (127: not found, 124: timed out) and the caller did not opt out of checking."""

    def __init__(self, argv, result):
        self.argv, self.result = [str(a) for a in argv], result
        text = (result.stderr or result.stdout or "").strip()[:300]
        super().__init__(f"{Path(self.argv[0]).name} {' '.join(self.argv[1:3])} exited {result.returncode}: {text}")


class Shell:
    def environment(self, extra=None):
        return child_env(os.environ, extra)

    def run(self, argv, env=None, cwd=None, timeout=None, tolerate=None):
        """Run argv and return its Result. A nonzero exit (127 not found, 124 timed out) raises ShellError, so no result is ignored.
        A caller whose logic reads the exit code itself passes `tolerate="<why a failure is not an error here>"`; a reason string is
        required (tests/test_bench_shell.py scans every `tolerate=` for one)."""
        if tolerate is not None and not (isinstance(tolerate, str) and tolerate.strip()):
            raise TypeError("tolerate= wants a reason string")
        result = self.execute(argv, env, cwd, timeout)
        if result.returncode != 0 and tolerate is None:
            raise ShellError(argv, result)
        return result

    def execute(self, argv, env=None, cwd=None, timeout=None):
        """Start the process; never raises for a nonzero exit, a missing program or a timeout."""
        try:
            done = subprocess.run([str(a) for a in argv], env=self.environment(env), cwd=cwd, text=True, errors="replace",
                                  capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            return Result(TIMED_OUT, error.stdout or "", error.stderr or "")
        except OSError as error:
            return Result(127, "", str(error))
        return Result(done.returncode, done.stdout, done.stderr)
