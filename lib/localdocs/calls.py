"""One docs call end to end: log first-or-fail, run the tool, cap the answer, log it, hand it back.

Result states, as logged: hit (an answer), miss (a valid request with no answer: not found, no pinned version, no index),
rejected (the read-only rules refused the request), error (a bug; the text says so without a traceback).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import guard, index, log, signature

MAX_RESPONSE = 8192
MARK = "\n[truncated: {} more bytes]"


@dataclass
class Reply:
    status: str
    text: str
    version: str | None = None
    provenance: dict | None = None
    cache: str | None = None


def cap(text, limit=MAX_RESPONSE):
    """The text cut to `limit` bytes (at a character boundary) with a marker saying how much was dropped."""
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    marker_room = len(MARK.format(len(raw)).encode())
    keep = raw[: max(0, limit - marker_room)].decode("utf-8", "ignore")
    return keep + MARK.format(len(raw) - len(keep.encode()))


def header(provenance):
    """The first line of every answer: where it came from."""
    return (f"[provenance] source={provenance['source']} version={provenance['version']} "
            f"exact={'yes' if provenance['exact'] else 'no'} retrieved={provenance['retrieved']}\n")


def call(tool, args, *, project, deno=None, log_file=None, env=None):
    """Run `tool` ('api_signature' or 'docs_search'). Raises log.LogUnavailable instead of answering unlogged."""
    path = log.log_path(log_file, env)  # before anything is read
    version = provenance = cached = None
    try:
        if tool == "api_signature":
            found = signature.api_signature(project, args.get("package"), args.get("symbol"), deno=deno)
            status, text, version, provenance = "hit", found.text, found.version, found.provenance
        elif tool == "docs_search":
            found = index.docs_search(project, args.get("lib"), args.get("query"), limit=args.get("limit"), cache_dir=path.parent)
            status, text, version, provenance, cached = "hit", found.text, found.version, found.provenance, found.cache
        else:
            raise guard.Rejected(f"refused: unknown tool {tool!r}")
    except signature.Miss as miss:
        status, text, version = "miss", str(miss), miss.version
    except index.Miss as miss:
        status, text, version = "miss", str(miss), miss.version
    except guard.Rejected as refusal:
        status, text = "rejected", str(refusal)
    except Exception as exc:  # a bug must not escape as a traceback into a worker's context, nor go unlogged
        status, text = "error", f"internal error ({type(exc).__name__}); see the call log"
        reason = repr(exc)
    else:
        reason = None
    if status in ("miss", "rejected"):
        reason = text
    if status == "hit" and provenance:
        # The line goes first and the cap counts it, so truncation takes the end of the answer and never the provenance.
        text = header(provenance) + text
    text = cap(text)
    log.record(path, tool, args, status, len(text.encode("utf-8")), version, None if status == "hit" else reason,
               provenance if status == "hit" else None, cached)
    return Reply(status, text, version, provenance if status == "hit" else None, cached)
