"""The call log: one JSON line per engine call in $TP_TASK_DIR/ctx-calls.jsonl (design §5), fail-closed like L5's docs log.

A call with no usable task directory is refused before anything is read (LogUnavailable), and a line that cannot be written
withholds the answer. The file is opened O_APPEND | O_NOFOLLOW through safe_write, so a link planted at ctx-calls.jsonl or at
the task directory is refused, not followed. Arguments are screened (redact.guard) before they are logged and before they are
hashed; the hash is over the screened arguments.

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


def log_path(override=None, env=None):
    """Where this call is logged: `override` if given, else $TP_TASK_DIR/ctx-calls.jsonl. LogUnavailable when there is no
    usable task directory (unset, relative, not a directory, or a symlink)."""
    env = os.environ if env is None else env
    if override:
        return Path(override)
    task_dir = env.get("TP_TASK_DIR", "")
    if not task_dir:
        raise LogUnavailable("TP_TASK_DIR is not set, so the call cannot be logged; refusing to answer without a log")
    if not os.path.isabs(task_dir) or os.path.islink(task_dir) or not os.path.isdir(task_dir):
        raise LogUnavailable("TP_TASK_DIR is not a real absolute directory; refusing to answer without a log")
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


def record(path, tool, args, answer, ms=0):
    """Append one line for `answer`. LogUnavailable if it cannot be written."""
    if answer.status not in STATUSES:
        raise ValueError(f"unknown status {answer.status!r}")
    text = json.dumps(_line(tool, args, answer, ms), ensure_ascii=False) + "\n"
    try:
        safe_write.append_text(path, text, root=Path(path).parent)
    except OSError as exc:
        raise LogUnavailable(f"cannot write {path}: {exc}; refusing to answer without a log") from exc


def call(tool, args, run, *, log_file=None, env=None):
    """One logged engine call: resolve the log first (LogUnavailable before anything is read), run `run()` -> Answer, log the
    line, then hand the answer back. A raised exception becomes status `error` with a text that carries no traceback."""
    path = log_path(log_file, env)
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
    record(path, tool, args, answer, int((time.monotonic() - started) * 1000))
    return answer
