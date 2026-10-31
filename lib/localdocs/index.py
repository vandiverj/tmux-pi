"""docs_search(lib, query) over a local index, and the build/refresh/status that make it.

An index is `<project>/.tmux-pi/docs-index/<lib>@<version>/` holding meta.json and index.json: markdown split at headings
into chunks, with an inverted index (term -> [chunk, count]) scored with BM25 at query time. A plain JSON file, not SQLite
FTS5: it needs nothing from the Python build, and for a package's docs (hundreds of KB) it stays small (see the lane report).

The version in the directory name is the one the lockfile pins. A query never builds, never fetches and never falls back to
another version: with no pin, or no index for the pin, it says so. `refresh` rebuilds an index when the lockfile hash it
was built from changed, and leaves it alone otherwise.
"""
import json
import math
import os
import re
import shutil
import uuid
from pathlib import Path

import safe_write

from . import cache, guard, lock, signature

INDEX_ROOT = Path(".tmux-pi") / "docs-index"
SOURCE_ROOT = Path(".tmux-pi") / "docs-src"
MAX_INDEX = 64_000_000
MAX_DOC_FILES = 2000
CHUNK_CHARS = 1800
TOKEN = re.compile(r"[A-Za-z0-9_$]+")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
SKIP_DIRS = {"node_modules", ".git"}


class Miss(Exception):
    def __init__(self, message, version=None):
        super().__init__(message)
        self.version = version


class Hit:
    """`provenance` is {source, version, exact, retrieved}; `cache` is "hit", "miss" or "off" (no cache directory in use)."""

    def __init__(self, text, version, provenance=None, cache=None):
        self.text, self.version, self.provenance, self.cache = text, version, provenance, cache


def key(lib, version):
    return f"{lib.replace('/', '__')}@{version}"


def index_root(project):
    return Path(project) / INDEX_ROOT


def tokens(text):
    out = []
    for word in TOKEN.findall(text):
        low = word.lower()
        if len(low) > 1:
            out.append(low)
        parts = [p.lower() for p in CAMEL.findall(word)]
        if len(parts) > 1:
            out.extend(p for p in parts if len(p) > 1)
    return out


def chunk_markdown(text, file):
    """[{f, h, l, t}]: sections at headings (a # inside a code fence is not one), long ones cut at blank lines."""
    chunks, heading, start, buf, fence = [], "", 1, [], False

    def flush():
        body = "\n".join(buf).strip()
        while body:
            piece = body[:CHUNK_CHARS]
            if len(body) > CHUNK_CHARS and "\n\n" in piece:
                piece = piece[: piece.rindex("\n\n")]
            chunks.append({"f": file, "h": heading, "l": start, "t": piece.strip()})
            body = body[len(piece):].strip()

    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not fence and re.match(r"#{1,4}\s", line):
            flush()
            heading, start, buf = line.lstrip("# ").strip(), number, [line]
        else:
            if not buf:
                start = number
            buf.append(line)
    flush()
    return chunks


def collect_docs(root):
    """[(relative path, text)] for the markdown under `root`: no links followed, no .env*, no node_modules/.git."""
    root = guard.real_root(root)
    out = []
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".env") and not os.path.islink(os.path.join(current, d)))
        for name in sorted(files):
            if not name.lower().endswith(".md") or name.startswith(".env"):
                continue
            full = Path(current) / name
            if full.is_symlink():
                continue
            relative = os.path.relpath(full, root)
            try:
                text = guard.read_text(guard.resolve_inside(root, relative))
            except guard.Rejected:
                continue
            if text:
                out.append((relative, text))
            if len(out) >= MAX_DOC_FILES:
                return out
    return out


def _inverted(chunks):
    postings, lengths = {}, []
    for number, chunk in enumerate(chunks):
        counts = {}
        words = tokens(chunk["h"] + " " + chunk["t"])
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        lengths.append(len(words))
        for word, count in counts.items():
            postings.setdefault(word, []).append([number, count])
    return postings, lengths


def source_retrieved(source, kind):
    """When the docs the index is built from were obtained: the fetch time for fetched docs, else now (an installed
    package or a directory is read at build time)."""
    if kind == "fetched":
        try:
            record = json.loads(guard.read_record(Path(source) / ".fetched.json") or "null")
        except ValueError:
            record = None
        if isinstance(record, dict) and isinstance(record.get("retrieved"), str):
            return record["retrieved"]
    return guard.now()


def build(project, lib, version, source, lock_hash, lock_name, source_kind):
    """Write the index for lib@version from the markdown under `source`; returns its meta. Replaces any index of the same
    key. Built beside the final directory and renamed into place, so a reader sees the old index or the whole new one."""
    docs = collect_docs(source)
    if not docs:
        raise Miss(f"no markdown files under {source}; nothing to index", version)
    chunks = [c for file, text in docs for c in chunk_markdown(text, file)]
    postings, lengths = _inverted(chunks)
    meta = {"lib": lib, "version": version, "lock_hash": lock_hash, "lockfile": lock_name, "built": guard.now(),
            "build_id": uuid.uuid4().hex, "files": len(docs), "chunks": len(chunks),
            "source": {"kind": source_kind, "path": str(source), "retrieved": source_retrieved(source, source_kind)}}
    root = index_root(project)
    safe_write.ensure_dir(root, what="docs index directory", root=project)
    final = root / key(lib, version)
    stage = root / f".build-{uuid.uuid4().hex}"
    stage.mkdir()
    try:
        safe_write.write_atomic(stage / "index.json", json.dumps({"chunks": chunks, "postings": postings, "lengths": lengths},
                                                                 separators=(",", ":"), ensure_ascii=False), root=stage)
        safe_write.write_atomic(stage / "meta.json", json.dumps(meta, indent=1), root=stage)
        if final.is_symlink():
            raise guard.Rejected(f"refused: {final} is a symlink")
        old = None
        if final.exists():
            old = root / f".old-{uuid.uuid4().hex}"
            final.rename(old)
        stage.rename(final)
        if old:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return meta


def read_meta(directory):
    text = guard.read_text(Path(directory) / "meta.json", limit=1_000_000)
    meta = json.loads(text) if text else None
    return meta if isinstance(meta, dict) else None


def list_indexes(project):
    root = index_root(project)
    out = []
    if root.is_dir() and not root.is_symlink():
        for directory in sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")):
            try:
                meta = read_meta(directory)
            except (ValueError, guard.Rejected, OSError):
                meta = None
            if meta:
                out.append((directory, meta))
    return out


def default_source(project, lib, version, deno=None):
    """Where a build reads docs from: docs fetched for exactly this version, else the installed package when it is that
    version. (kind, path) or None."""
    fetched = Path(project) / SOURCE_ROOT / key(lib, version)
    if fetched.is_dir() and not fetched.is_symlink():
        return "fetched", fetched
    installed = signature.node_package_dir(project, lib) or signature.deno_package_dir(deno, lib, version)
    if installed and signature.installed_version(installed) == version:
        return "package", installed
    return None


def build_for(project, lib, *, source=None, version=None, deno=None):
    """`tp-docs build`: index lib at the lockfile's pin from `source` (a directory) or the default source."""
    if not signature.NAME_RE.match(str(lib or "")):
        raise guard.Rejected(f"refused: {lib!r} is not an npm package name")
    installed = None
    node_dir = signature.node_package_dir(project, lib)
    if node_dir:
        installed = signature.installed_version(node_dir)
    pinned = lock.pin(project, lib, installed)
    if not pinned and pinned.candidates and version in pinned.candidates:
        pinned = lock.Pin(version, "lockfile")
    if not pinned:
        raise Miss(pinned.reason)
    if version and version != pinned.version:
        raise Miss(f"{lib}@{version} was asked for but the lockfile pins {pinned.version}", pinned.version)
    if source:
        chosen = ("dir", Path(source))
    else:
        chosen = default_source(project, lib, pinned.version, deno)
        if not chosen:
            raise Miss(f"no docs source for {lib}@{pinned.version}: it is not installed at that version and nothing was fetched; "
                       f"run `tp-docs fetch {lib}` or pass --from DIR", pinned.version)
    return build(project, lib, pinned.version, chosen[1], lock.lock_hash(project), pinned.lockfile, chosen[0])


def refresh(project, deno=None):
    """`tp-docs refresh`: rebuild every index whose lockfile hash changed. [(lib, action, detail)]."""
    current = lock.lock_hash(project)
    done = []
    for directory, meta in list_indexes(project):
        lib = meta.get("lib", "")
        if current is None:
            done.append((lib, "skipped", "no lockfile"))
        elif meta.get("lock_hash") == current:
            done.append((lib, "unchanged", meta.get("version", "")))
        else:
            try:
                recorded = meta.get("source") or {}
                source = recorded.get("path") if recorded.get("kind") == "dir" and os.path.isdir(recorded.get("path", "")) else None
                fresh = build_for(project, lib, source=source, deno=deno)
            except (Miss, guard.Rejected) as exc:
                done.append((lib, "failed", str(exc)))
                continue
            if fresh["version"] != meta.get("version") and directory.exists():
                shutil.rmtree(directory, ignore_errors=True)
            done.append((lib, "rebuilt", fresh["version"]))
    return done


def status(project):
    current = lock.lock_hash(project)
    rows = []
    for directory, meta in list_indexes(project):
        pinned = lock.pin(project, meta.get("lib", ""))
        rows.append({"lib": meta.get("lib"), "version": meta.get("version"), "chunks": meta.get("chunks"), "built": meta.get("built"),
                     "pinned_version": pinned.version, "lock_changed": meta.get("lock_hash") != current,
                     "matches_pin": pinned.version == meta.get("version")})
    return {"project": str(project), "lock_hash": current, "indexes": rows}


def _load(directory):
    path = Path(directory) / "index.json"
    text = guard.read_text(path, limit=MAX_INDEX)
    return json.loads(text) if text else None


def _rank(directory, lib, version, query, limit):
    """The formatted top results from the index in `directory`."""
    data = _load(directory)
    if not data:
        raise Miss(f"the docs index for {lib}@{version} is empty or unreadable; run `tp-docs build {lib}`", version)
    chunks, postings, lengths = data["chunks"], data["postings"], data["lengths"]
    avg = (sum(lengths) / len(lengths)) or 1
    scores = {}
    for term in set(tokens(query)):
        entries = postings.get(term, [])
        idf = math.log(1 + (len(chunks) - len(entries) + 0.5) / (len(entries) + 0.5))
        for number, count in entries:
            norm = count * 2.2 / (count + 1.2 * (0.25 + 0.75 * lengths[number] / avg))
            scores[number] = scores.get(number, 0) + idf * norm
    ranked = sorted(scores, key=lambda n: (-scores[n], n))[:limit]
    return [f"[{chunks[n]['f']}:{chunks[n]['l']}] {chunks[n]['h']}\n{chunks[n]['t'][:1500]}" for n in ranked]


def docs_search(project, lib, query, limit=None, cache_dir=None):
    """Top chunks for `query` from the index built for lib's pinned version. Raises Miss with the reason when there is none.

    With `cache_dir`, an answer is kept there and served again for the same lib, pinned version, index build, query and
    limit (see cache.py). The Hit carries its provenance: the source the index was built from, the pinned version, whether
    that is exactly the version the lockfile pins and the index is current with it, and when the docs were retrieved."""
    lib, query = str(lib or "").strip(), str(query or "").strip()
    if not signature.NAME_RE.match(lib):
        raise guard.Rejected(f"refused: {lib!r} is not an npm package name")
    if not query or len(query) > 500:
        raise guard.Rejected("refused: query is empty or longer than 500 characters")
    try:
        limit = max(1, min(10, int(limit))) if limit not in (None, "") else 5
    except (TypeError, ValueError):
        raise guard.Rejected("refused: limit must be a number") from None
    installed = None
    node_dir = signature.node_package_dir(project, lib)
    if node_dir:
        installed = signature.installed_version(node_dir)
    pinned = lock.pin(project, lib, installed)
    if not pinned:
        raise Miss(pinned.reason)
    directory = index_root(project) / key(lib, pinned.version)
    if directory.is_symlink() or not (directory / "index.json").is_file():
        raise Miss(f"no docs index for {lib}@{pinned.version} (the version the lockfile pins); run `tp-docs build {lib}`", pinned.version)
    guard.contained_dir(index_root(project), directory)
    meta = read_meta(directory) or {}
    current = lock.lock_hash(project)
    ident = {"lib": lib, "version": pinned.version, "build_id": meta.get("build_id") or "", "query": query, "limit": limit}
    stale = "" if meta.get("lock_hash") == current else " (built from an older lockfile; run `tp-docs refresh`)"
    cached = cache.load(cache_dir, ident) if cache_dir and ident["build_id"] else None
    if cached:
        body, count, state = cached["body"], cached["count"], "hit"
    else:
        results = _rank(directory, lib, pinned.version, query, limit)
        if not results:
            raise Miss(f"no match for {query!r} in the {lib}@{pinned.version} docs index{stale}", pinned.version)
        body, count, state = "\n\n".join(results), len(results), "off"
        if cache_dir and ident["build_id"]:
            state = "miss" if cache.store(cache_dir, ident, body, count) else "off"
    source = meta.get("source") or {}
    kind = source.get("kind") or "unknown"
    provenance = {"source": f"{kind}:{source.get('path', '')}", "version": pinned.version,
                  "exact": kind in ("package", "fetched") and meta.get("version") == pinned.version and not stale,
                  "retrieved": source.get("retrieved") or meta.get("built") or ""}
    return Hit(f"{lib}@{pinned.version} docs, {count} result(s){stale}\n\n{body}", pinned.version, provenance, state if cache_dir else None)
