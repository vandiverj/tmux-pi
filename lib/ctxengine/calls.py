"""The call log: one JSON line per engine call in $TP_TASK_DIR/ctx-calls.jsonl (design §5), fail-closed like L5's docs log.

A call with no usable task directory is refused before anything is read (LogUnavailable), and a line that cannot be written
withholds the answer. The file is opened O_APPEND | O_NOFOLLOW through safe_write, so a link planted at ctx-calls.jsonl is
refused, not followed; a link anywhere in the task directory's path is refused too (below). Arguments are screened
(redact.guard) before they are logged and before they are hashed; the hash is over the screened arguments.

Two kinds of call. A query operation (the default) always needs a usable $TP_TASK_DIR and has no other log. An operator command
(`operator=True`: approve, audit, show, list) is a person's: with neither TP_TASK_DIR nor TP_TASK_ID set it may run unlogged,
or log to an explicit file when run on a terminal; with either set it is a worker's shell and is logged like any call. An
explicit log file is accepted only for an operator command on a terminal with no worker variable, so a worker cannot point its
log elsewhere or skip it.

Links in the task directory's path: for `<state>/tasks/<id>` (or `archive/<id>`) every part below the sessions directory
(`<state>`, `tasks`, `<id>`) must be a real directory. For any other spelling nothing marks where tmux-pi's state begins, so
every ancestor that is a symlink owned by the calling user is refused (a link the user's own processes could have planted; the
system's links, such as /tmp -> /private/tmp, are root's and stay allowed).

A line: {ts, tool, args_hash, args, status, bytes, ms, notes_returned, index_age_s} and, when there is one, `reason`.
Statuses: ok, invalid (bad arguments), refused (a screen said no), no_project, stale_index, unavailable (a navigator or
reader is missing), error (a bug; the text says so without a traceback).
"""
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import safe_write

from . import redact

LOG_NAME = "ctx-calls.jsonl"
ARG_LIMIT = 500
NOTES_LIMIT = 50
STATUSES = ("ok", "invalid", "refused", "no_project", "stale_index", "unavailable", "error")


class LogUnavailable(Exception):
    pass


@dataclass
class Answer:
    status: str
    text: str
    notes_returned: list = field(default_factory=list)
    index_age_s: int | None = None
    reason: str | None = None
    data: object = None  # whatever structured result the caller wants back; not logged


WORKER_ENV = ("TP_TASK_DIR", "TP_TASK_ID")


def on_terminal():
    """Whether stdin and stdout are a terminal (a person is at the keyboard)."""
    try:
        return os.isatty(0) and os.isatty(1)
    except (OSError, ValueError):
        return False


def _worker_vars(env):
    return [name for name in WORKER_ENV if env.get(name)]


def check_task_dir(task_dir):
    """The directory `safe_write` should treat as the root for writes in `task_dir` (None when no part of the path marks one),
    after refusing a task directory that is, or sits behind, a symlink. LogUnavailable otherwise."""
    task_dir = os.path.abspath(str(task_dir))
    if os.path.islink(task_dir) or not os.path.isdir(task_dir):
        raise LogUnavailable("TP_TASK_DIR is not a real absolute directory; refusing to answer without a log")
    state = safe_write.task_root(task_dir)
    try:
        if state is not None:
            root = os.path.dirname(state)
            safe_write.check_below(root, task_dir, "task directory")  # <state>, tasks and <id>
            return root
        here = os.path.dirname(task_dir)
        while here != os.path.dirname(here):
            if os.path.islink(here) and os.lstat(here).st_uid == os.geteuid():
                raise safe_write.UnsafePath(here, "task directory")
            here = os.path.dirname(here)
    except safe_write.UnsafePath:
        raise LogUnavailable("the task directory is, or is inside, a symlink; refusing to answer without a log") from None
    except OSError:
        raise LogUnavailable("the task directory cannot be examined; refusing to answer without a log") from None
    return None


def log_path(override=None, env=None, operator=False):
    """Where this call is logged, or None when it may run unlogged. LogUnavailable when it cannot be logged.

    $TP_TASK_DIR/ctx-calls.jsonl when a task directory is set and usable. `override` (an explicit file) only for an operator
    command on a terminal with neither TP_TASK_DIR nor TP_TASK_ID set. None only for an operator command with neither set and
    no override. Everything else, with no usable task directory, is LogUnavailable."""
    env = os.environ if env is None else env
    workers = _worker_vars(env)
    if override:
        if not operator or workers or not on_terminal():
            raise LogUnavailable("--log is for a person running approve, audit, show or list at a terminal with no TP_TASK_DIR or "
                                 "TP_TASK_ID; refusing to answer without a log")
        return Path(override)
    task_dir = env.get("TP_TASK_DIR", "")
    if not task_dir:
        if operator and not workers:
            return None
        raise LogUnavailable("TP_TASK_DIR is not set, so the call cannot be logged; refusing to answer without a log")
    if not os.path.isabs(task_dir):
        raise LogUnavailable("TP_TASK_DIR is not a real absolute directory; refusing to answer without a log")
    check_task_dir(task_dir)
    return Path(task_dir) / LOG_NAME


def _arg(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = redact.guard(text)
    return text if len(text) <= ARG_LIMIT else text[:ARG_LIMIT] + "…"


def _line(tool, args, answer, ms):
    shown = {key: _arg(value) for key, value in sorted(args.items())}
    digest = hashlib.sha256(json.dumps(shown, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool, "args_hash": digest, "args": shown,
            "status": answer.status, "bytes": len(answer.text.encode("utf-8")), "ms": ms,
            "notes_returned": list(answer.notes_returned)[:NOTES_LIMIT], "index_age_s": answer.index_age_s}
    if answer.reason:
        line["reason"] = _arg(answer.reason)
    return line


def record(path, tool, args, answer, ms=0, task_dir=None):
    """Append one line for `answer`. LogUnavailable if it cannot be written. With `task_dir` (the log is its ctx-calls.jsonl)
    the directory's path is examined again at write time, so a link planted after the first check is still refused."""
    if answer.status not in STATUSES:
        raise ValueError(f"unknown status {answer.status!r}")
    text = json.dumps(_line(tool, args, answer, ms), ensure_ascii=False) + "\n"
    try:
        root = check_task_dir(task_dir) if task_dir else None
        safe_write.append_text(path, text, root=root or Path(path).parent)
    except OSError as exc:
        raise LogUnavailable(f"cannot write {path}: {exc}; refusing to answer without a log") from exc


def call(tool, args, run, *, log_file=None, env=None, operator=False):
    """One logged engine call: resolve the log first (LogUnavailable before anything is read), run `run()` -> Answer, log the
    line, then hand the answer back. A raised exception becomes status `error` with a text that carries no traceback.
    `operator` marks a person's command (see the module note): the only kind that may run unlogged or use `log_file`."""
    environ = os.environ if env is None else env
    path = log_path(log_file, environ, operator)
    task_dir = environ.get("TP_TASK_DIR") if path is not None and not log_file else None
    started = time.monotonic()
    try:
        answer = run()
        if not isinstance(answer, Answer):
            raise TypeError("a call returns an Answer")
        if answer.status not in STATUSES:
            raise ValueError("a call returns a known status")
    except redact.Refused as refusal:
        answer = Answer("refused", str(refusal), reason=str(refusal))
    except Exception as exc:  # a bug must not escape as a traceback into a worker's context, nor go unlogged
        answer = Answer("error", f"internal error ({type(exc).__name__}); see the call log", reason=repr(exc))
    if path is not None:
        record(path, tool, args, answer, int((time.monotonic() - started) * 1000), task_dir=task_dir)
    return answer
