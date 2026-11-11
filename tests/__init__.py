"""Importing the tests package removes the caller's session from this process's environment, before any test module runs.

The scrub itself lives in session_env, which scrubs when imported. Every tests/test_*.py starts with `import session_env`, so the
runners that never import this package (`discover -s tests` without `-t .`, `python3 tests/test_x.py`) scrub too. This file covers
`python3 -m unittest tests.X`, a single test id and `discover -s tests -t .`, and makes the top-level name `session_env` the same
module (one CALLER_SESSION) and importable, whichever way the module is reached."""
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)
from . import session_env  # noqa: E402  (scrubs at import)

sys.modules.setdefault("session_env", session_env)


def _lmstudio_budget():
    """No default test reaches LM Studio: a budget of 0 with a fresh counter file unless the caller set one (lib/router.py
    local_open reads both; subprocesses inherit them). TP_LIVE_LMSTUDIO=1 opens a budget of 20 for the one live test."""
    import atexit
    import tempfile
    if "TP_LMSTUDIO_BUDGET" in os.environ:
        return
    os.environ["TP_LMSTUDIO_BUDGET"] = "20" if os.environ.get("TP_LIVE_LMSTUDIO") == "1" else "0"
    fd, path = tempfile.mkstemp(prefix="tp-lmstudio-budget-")
    os.write(fd, b"0")
    os.close(fd)
    os.environ.setdefault("TP_LMSTUDIO_BUDGET_FILE", path)
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))


_lmstudio_budget()
