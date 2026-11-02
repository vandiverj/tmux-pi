"""Run bin/tp-agent in this process: load it once, call its main, capture the output.

run(argv, env, cwd) -> (rc, out, err) matches what a subprocess of `bin/tp-agent ARGV` gives for rc, stdout and stderr.
Limits: a child process that inherits the real stdout/stderr (no capture) writes there, not into out/err; env replaces
os.environ for the call only (tp-agent does this); cwd is changed with os.chdir for the call, so run one call at a time.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parent.parent / "bin" / "tp-agent"
_module = None


def load():
    global _module
    if _module is None:
        loader = importlib.machinery.SourceFileLoader("tp_agent_inproc_module", str(AGENT))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _module = module
    return _module


def run(argv, env=None, cwd=None):
    """Call tp-agent's command line with argv (a list, without the program name). env: the whole environment (default: a copy of
    os.environ). cwd: the working directory (default: unchanged). Returns (rc, stdout, stderr); rc is 0 on a normal return."""
    agent = load()
    out, err = io.StringIO(), io.StringIO()
    saved_cwd = os.getcwd()
    rc = 0
    try:
        if cwd is not None:
            os.chdir(cwd)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                agent.cli(list(argv), dict(os.environ) if env is None else env)
            except SystemExit as stop:
                code = stop.code
                rc = 0 if code is None else code if isinstance(code, int) else (print(code, file=sys.stderr) or 1)
    finally:
        os.chdir(saved_cwd)
    return rc, out.getvalue(), err.getvalue()
