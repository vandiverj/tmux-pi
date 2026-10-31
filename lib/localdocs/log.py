"""Call log: one JSON line per docs call in $TP_TASK_DIR/docs-calls.jsonl.

The log is not optional. With no usable task directory a call fails closed (LogUnavailable) before it reads anything,
and a line that cannot be written withholds the result. The file is opened O_APPEND | O_NOFOLLOW through safe_write, so a
link planted at docs-calls.jsonl or at the task directory is refused, not followed.
"""
import json
import os
import time
from pathlib import Path

import safe_write

LOG_NAME = "docs-calls.jsonl"
ARG_LIMIT = 500


class LogUnavailable(Exception):
    pass


def log_path(override=None, env=None):
    """Where this call is logged. `override` is an explicit file; otherwise $TP_TASK_DIR/docs-calls.jsonl."""
    env = os.environ if env is None else env
    if override:
        return Path(override)
    task_dir = env.get("TP_TASK_DIR", "")
    if not task_dir:
        raise LogUnavailable("TP_TASK_DIR is not set, so the call cannot be logged; refusing to answer without a log")
    if not os.path.isdir(task_dir) or os.path.islink(task_dir):
        raise LogUnavailable(f"TP_TASK_DIR {task_dir!r} is not a directory; refusing to answer without a log")
    return Path(task_dir) / LOG_NAME


def _arg(value):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= ARG_LIMIT else text[:ARG_LIMIT] + "…"


def record(path, tool, args, result, size, version=None, reason=None, provenance=None, cache=None):
    """Append one line. Raises LogUnavailable if it cannot be written."""
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool": tool,
            "args": {key: _arg(value) for key, value in args.items()}, "result": result, "bytes": size,
            "index_version": version, "provenance": provenance}
    if cache:
        line["cache"] = cache
    if reason:
        line["reason"] = _arg(reason)
    try:
        safe_write.append_text(path, json.dumps(line, ensure_ascii=False) + "\n", root=Path(path).parent)
    except OSError as exc:
        raise LogUnavailable(f"cannot write {path}: {exc}; refusing to answer without a log") from exc
