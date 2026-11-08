"""The call log: one JSON line per engine call in $TP_TASK_DIR/ctx-calls.jsonl (design §5), fail-closed like L5's docs log.

A call with no usable task directory is refused before anything is read (LogUnavailable), and a line that cannot be written
withholds the answer. The file is opened O_APPEND | O_NOFOLLOW through safe_write, so a link planted at ctx-calls.jsonl is
refused, not followed; a link anywhere in the task directory's path is refused too (below). Arguments are screened
(redact.guard) before they are logged and before they are hashed; the hash is over the screened arguments.

Two kinds of call. A query operation (the default) always needs a usable $TP_TASK_DIR and has no other log. An operator command
(`operator=True`: approve, audit, show, list, status) is a person's: with neither TP_TASK_DIR nor TP_TASK_ID set, and only at
a terminal (stdin and stdout), it may run unlogged or log to an explicit file; with either set, or with no terminal, it is
logged like any call (so needs a usable task directory). An explicit log file is accepted only for an operator command on a
terminal with no worker variable, so a worker cannot point its log elsewhere or skip it, and a worker that clears its
variables but has no terminal is still refused.

TP_TASK_DIR must be spelled as its own normalized absolute path (no `..`, `.`, repeated or trailing slash): a `..` after a
symlink would name a different directory than the one the checks looked at.

Links in the task directory's path: for `<state>/tasks/<id>` (or `archive/<id>`) every part below the sessions directory
(`<state>`, `tasks`, `<id>`) must be a real directory. For any other spelling nothing marks where tmux-pi's state begins, so
every ancestor that is a symlink owned by the calling user is refused (a link the user's own processes could have planted; the
system's links, such as /tmp -> /private/tmp, are root's and stay allowed).

A line: {ts, tool, args_hash, args, status, bytes, ms, notes_returned, index_age_s} and, when there is one, `reason`.
Statuses: ok, invalid (bad arguments), refused (a screen said no), no_project, stale_index, unavailable (a navigator or
reader is missing), error (a bug; the text says so without a traceback).
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import safe_write

from . import redact

LOG_NAME = "ctx-calls.jsonl"
LOCK_NAME = "ctx-calls.lock"
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
    undo: object = None  # for a call that wrote something: takes the write back when the call cannot be logged


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
    task_dir = str(task_dir)
    if not os.path.isabs(task_dir) or os.path.normpath(task_dir) != task_dir:
        raise LogUnavailable("TP_TASK_DIR is not a normalized absolute path; refusing to answer without a log")
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
    command on a terminal with neither TP_TASK_DIR nor TP_TASK_ID set. None only for an operator command on a terminal with
    neither set and no override. Everything else, with no usable task directory, is LogUnavailable."""
    env = os.environ if env is None else env
    workers = _worker_vars(env)
    if override:
        if not operator or workers or not on_terminal():
            raise LogUnavailable("--log is for a person running approve, audit, show, list or status at a terminal with no "
                                 "TP_TASK_DIR or TP_TASK_ID; refusing to answer without a log")
        return Path(override)
    task_dir = env.get("TP_TASK_DIR", "")
    if not task_dir:
        if operator and not workers and on_terminal():
            return None
        if operator and not workers:
            raise LogUnavailable("TP_TASK_DIR is not set and there is no terminal, so the call cannot run unlogged; refusing to "
                                 "answer without a log")
        raise LogUnavailable("TP_TASK_DIR is not set, so the call cannot be logged; refusing to answer without a log")
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


class _Lock:
    """An exclusive lock on <task dir>/ctx-calls.lock for a call that counts, writes and logs as one step."""

    def __init__(self, task_dir):
        self.task_dir, self.fd = task_dir, None

    def __enter__(self):
        try:
            self.fd = os.open(os.path.join(self.task_dir, LOCK_NAME), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) |
                              getattr(os, "O_CLOEXEC", 0), 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except OSError as exc:
            if self.fd is not None:
                os.close(self.fd)
            raise LogUnavailable(f"cannot lock the call log ({exc.strerror or type(exc).__name__}); refusing to answer without a log") from exc
        return self

    def __exit__(self, *exc):
        os.close(self.fd)  # closing the descriptor releases the lock


def check_writable(path, task_dir=None):
    """LogUnavailable unless a line could be appended to `path` now (nothing is written). A link, a directory or a refused parent is
    found here, before a write that could not be logged is made."""
    try:
        root = check_task_dir(task_dir) if task_dir else None
        safe_write.open_append(path, root=root or Path(path).parent).close()
    except OSError as exc:
        raise LogUnavailable(f"cannot write {path}: {exc}; refusing to answer without a log") from exc


def call(tool, args, run, *, log_file=None, env=None, operator=False, exclusive=False):
    """One logged engine call: resolve the log first (LogUnavailable before anything is read), run `run()` -> Answer, log the
    line, then hand the answer back. A raised exception becomes status `error` with a text that carries no traceback.
    `operator` marks a person's command (see the module note): the only kind that may run unlogged or use `log_file`.

    `exclusive` is for a call that writes (a candidate note) or counts earlier calls (the per-task note limit): the task's lock is
    held from before `run()` until the line is written, so two such calls never interleave, and the log is checked to be writable
    first. If the line still cannot be written, the answer's `undo` takes the write back before LogUnavailable goes up: a write
    is never left behind without its line."""
    environ = os.environ if env is None else env
    path = log_path(log_file, environ, operator)
    task_dir = environ.get("TP_TASK_DIR") if path is not None and not log_file else None
    if exclusive and path is not None and task_dir:
        with _Lock(task_dir):
            check_writable(path, task_dir)
            return _run_logged(tool, args, run, path, task_dir)
    if exclusive and path is not None:
        check_writable(path)
    return _run_logged(tool, args, run, path, task_dir)


def _run_logged(tool, args, run, path, task_dir):
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
        try:
            record(path, tool, args, answer, int((time.monotonic() - started) * 1000), task_dir=task_dir)
        except LogUnavailable:
            if callable(answer.undo):
                try:
                    answer.undo()
                except Exception:
                    pass
            raise
    return answer
