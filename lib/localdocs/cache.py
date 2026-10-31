"""Answer cache for docs_search, kept in the task directory: `<task dir>/docs-cache/<lib>@<version>-<digest>.json`.

An entry answers one question: this library, at this pinned version, from this build of its index, for this query and
result limit. All of them are in the digest and written in the record, and a record whose fields do not match the
question is ignored, so a moved pin, a rebuilt index or a different query never reads another's answer. Misses are not
cached. Staleness against the lockfile is not cached either: the caller judges it on every call. The cache is
best-effort: a directory that is a link, or a write that fails, means "off" and the answer is computed.
"""
import hashlib
import json
from pathlib import Path

import safe_write

from . import guard

CACHE_DIR = "docs-cache"
FIELDS = ("lib", "version", "build_id", "query", "limit")


def entry_path(directory, ident):
    digest = hashlib.sha256(json.dumps([ident[f] for f in FIELDS], ensure_ascii=False).encode()).hexdigest()[:32]
    return Path(directory) / CACHE_DIR / f"{ident['lib'].replace('/', '__')}@{ident['version']}-{digest}.json"


def load(directory, ident):
    """The cached {body, count} for `ident`, or None."""
    try:
        record = json.loads(guard.read_record(entry_path(directory, ident)) or "null")
    except ValueError:
        return None
    if not isinstance(record, dict) or any(record.get(f) != ident[f] for f in FIELDS):
        return None
    return record if isinstance(record.get("body"), str) and isinstance(record.get("count"), int) else None


def store(directory, ident, body, count):
    """True when the entry was written; False when the cache is off for this directory."""
    path = entry_path(directory, ident)
    try:
        if path.parent.is_symlink():
            return False
        safe_write.ensure_dir(path.parent, what="docs cache directory", root=directory)
        safe_write.write_atomic(path, json.dumps({**ident, "body": body, "count": count}, ensure_ascii=False), root=directory)
    except OSError:
        return False
    return True
