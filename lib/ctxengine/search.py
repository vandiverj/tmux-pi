"""Finding notes: scope globs first, then tags, then BM25 over title and body (design §4). No embeddings, no model.

Selectors are filters and all given ones must hold (AND):
  paths  a note qualifies when one of its scope globs matches one of the paths
  tags   a note qualifies when it has at least one of the tags
  query  a note qualifies when at least one query term occurs in its title or body
Qualifying notes are ordered by (scope specificity, tag overlap, BM25 score), best first, then by id, so equal inputs and an
equal store give the same list. Scope specificity of a glob for a path: exact (no wildcard) beats a directory-anchored pattern
beats a repo-wide pattern; ties break on more literal leading directories, then on fewer `**`. A note's specificity is its best
glob over all the paths asked about.

The index is a plain JSON file, <project>/.tmux-pi/ctx-cache/knowledge-index.json: per note its search fields and term counts,
stamped with every note file's (name, mtime, size). A query reads the stamp; if any note file changed the index is rebuilt in
memory and written back (a cache that cannot be written is skipped, never an error). Only the notes returned are re-read.
"""
import json
import math
import os
import re
import time
from dataclasses import dataclass, field

import safe_write

from . import store

INDEX_NAME = "knowledge-index.json"
INDEX_VERSION = 1
K1, B = 1.5, 0.75
TITLE_WEIGHT = 2
MAX_TERMS = 64
CACHE_MAX = 64 * 1024 * 1024
WORD = re.compile(r"[a-z0-9]+")


def tokens(text):
    return [w for w in WORD.findall(str(text).lower()) if len(w) > 1]


# ------------------------------------------------------------------------------------------------------------- globs

def _segment_regex(segment):
    return re.compile("".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in segment) + r"\Z")


def glob_matches(glob, path):
    """Whether the relative posix `path` matches the scope `glob` (`*` and `?` within a segment, `**` across directories)."""
    try:
        patterns = store.parse_glob(glob)
    except ValueError:
        return False
    parts = path.split("/")
    compiled = [None if p == "**" else _segment_regex(p) for p in patterns]
    memo = {}

    def match(i, j):
        key = (i, j)
        if key in memo:
            return memo[key]
        if i == len(compiled):
            result = j == len(parts)
        elif compiled[i] is None:
            if i == len(compiled) - 1:  # a trailing `**` is the contents of a directory, so it needs at least one more segment
                result = j < len(parts)
            else:
                result = match(i + 1, j) or (j < len(parts) and match(i, j + 1))
        else:
            result = j < len(parts) and compiled[i].match(parts[j]) is not None and match(i + 1, j + 1)
        memo[key] = result
        return result

    return match(0, 0)


def specificity(glob):
    """A sortable tuple, larger is more specific: (class, literal leading directories, -number of `**`), where class is 3 for a
    glob without wildcards, 2 for a pattern anchored in a literal directory, 1 for a repo-wide pattern."""
    segments = store.parse_glob(glob)
    wildcard = [("*" in s or "?" in s) for s in segments]
    if not any(wildcard):
        return (3, len(segments), 0)
    literal_dirs = 0
    for segment, is_wild in zip(segments[:-1], wildcard):
        if is_wild:
            break
        literal_dirs += 1
    return (2 if literal_dirs else 1, literal_dirs, -sum(1 for s in segments if s == "**"))


def best_specificity(scope, paths):
    """The best specificity among the globs of `scope` that match any of `paths`, or None when none matches."""
    best = None
    for glob in scope:
        if any(glob_matches(glob, p) for p in paths):
            s = specificity(glob)
            best = s if best is None or s > best else best
    return best


# -------------------------------------------------------------------------------------------------------------- index

def _entry(note, stamp):
    counts = {}
    for term in tokens(note.headline) * TITLE_WEIGHT + tokens(note.body):
        counts[term] = counts.get(term, 0) + 1
    return {"id": note.id, "status": note.status, "kind": note.kind, "title": note.headline, "scope": note.scope,
            "tags": note.tags, "stamp": stamp, "len": sum(counts.values()), "tf": counts}


def _stamps(layout, wheres):
    """{(directory, file name): [mtime_ns, size]} for every note-looking file, from lstat alone."""
    out = {}
    for where in wheres:
        directory = layout.directory(where)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if name.endswith(".md") and not name.startswith("."):
                try:
                    info = os.lstat(directory / name)
                except OSError:
                    continue
                out[f"{store.DIRS[where]}/{name}"] = [info.st_mtime_ns, info.st_size]
    return out


@dataclass
class Index:
    entries: dict
    invalid: list
    built: float
    stamps: dict
    cache: str = "built"  # hit (read from the file), built (rebuilt and written), memory (rebuilt, the file could not be written)

    @property
    def age_s(self):
        return max(0, int(time.time() - self.built))


def _cache_path(layout):
    return layout.cache / INDEX_NAME


def _read_cache(layout, stamps):
    path = _cache_path(layout)
    try:
        safe_write.check_below(layout.project, path, "cache path")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            data = json.loads(handle.read(CACHE_MAX + 1).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or data.get("stamps") != stamps:
        return None
    entries = data.get("entries")
    if not isinstance(entries, dict) or not isinstance(data.get("invalid"), list) or not isinstance(data.get("built"), (int, float)):
        return None
    return Index(entries, data["invalid"], float(data["built"]), stamps, "hit")


def _build(layout, stamps):
    notes, invalid = store.scan(layout, ("approved", "candidate"))
    entries = {n.id: _entry(n, stamps.get(f"{store.DIRS[n.where]}/{n.id}.md")) for n in notes}
    return Index(entries, invalid, time.time(), stamps, "memory")


def load_index(layout, use_cache=True):
    """The current index, from the cache file when its stamp still matches the note files, else rebuilt (and written back)."""
    stamps = _stamps(layout, ("approved", "candidate"))
    if use_cache:
        cached = _read_cache(layout, stamps)
        if cached:
            return cached
    index = _build(layout, stamps)
    if use_cache:
        payload = {"version": INDEX_VERSION, "built": index.built, "stamps": stamps, "entries": index.entries, "invalid": index.invalid}
        try:
            safe_write.ensure_dir(layout.cache, root=layout.project, what="cache directory")
            safe_write.write_atomic(_cache_path(layout), json.dumps(payload, ensure_ascii=False, separators=(",", ":")), mode=0o644,
                                    root=layout.project)
            index.cache = "built"
        except (OSError, ValueError):
            index.cache = "memory"
    else:
        index.cache = "memory"
    return index


def _bm25(docs, entries, terms):
    """{id: score} for `entries`, with corpus statistics from `docs` (index entries); only ids with a matching term score."""
    if not docs or not terms:
        return {}
    average = (sum(d["len"] for d in docs) / len(docs)) or 1.0
    frequency = {t: sum(1 for d in docs if t in d["tf"]) for t in set(terms)}
    scores = {}
    for entry in entries:
        total = 0.0
        for term in terms:
            tf = entry["tf"].get(term, 0)
            if not tf:
                continue
            df = frequency[term]
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            total += idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * entry["len"] / average))
        if total > 0:
            scores[entry["id"]] = round(total, 6)
    return scores


# ------------------------------------------------------------------------------------------------------------- query

@dataclass
class Hit:
    note: store.Note
    score: float = 0.0
    specificity: tuple = ()
    tag_overlap: int = 0


@dataclass
class Result:
    hits: list = field(default_factory=list)
    total: int = 0
    index_age_s: int = 0
    cache: str = ""
    invalid: list = field(default_factory=list)
    candidates_counted: int = 0  # candidates that qualified but were not asked for (never counted in a brief)


def query(layout, *, query=None, paths=(), tags=(), include_candidates=False, limit=5, use_cache=True):
    """Notes matching the selectors, best first. `paths` are project-relative posix paths (see redact.check_path). Superseded
    notes are never returned here; ask for one by id with store.get. Candidates only when `include_candidates`."""
    limit = max(1, min(int(limit), 10))
    paths = [p for p in paths]
    tags = [t.lower() for t in tags]
    terms = tokens(query)[:MAX_TERMS] if query else []
    index = load_index(layout, use_cache)
    result = Result(index_age_s=index.age_s, cache=index.cache, invalid=index.invalid)
    pool = []
    for entry in index.entries.values():
        if paths:
            special = best_specificity(entry["scope"], paths)
            if special is None:
                continue
        else:
            special = ()
        overlap = len(set(entry["tags"]) & set(tags))
        if tags and not overlap:
            continue
        if entry["status"] != "approved" and not include_candidates:
            result.candidates_counted += 1
            continue
        pool.append((entry, special, overlap))
    corpus = [e for e in index.entries.values() if include_candidates or e["status"] == "approved"]
    scores = _bm25(corpus, [p[0] for p in pool], terms) if terms else {}
    ranked = []
    for entry, special, overlap in pool:
        if terms and entry["id"] not in scores:
            continue
        ranked.append((entry, special, overlap, scores.get(entry["id"], 0.0)))
    ranked.sort(key=lambda r: (_neg(r[1]), -r[2], -r[3], r[0]["id"]))
    result.total = len(ranked)
    for entry, special, overlap, score in ranked[:limit]:
        try:
            note = store.load(layout, "approved" if entry["status"] == "approved" else "candidate", entry["id"])
        except (store.StoreError, store.NoteError) as exc:
            result.invalid.append({"path": entry["id"], "problems": [f"changed during the query: {exc}"]})
            continue
        result.hits.append(Hit(note, score, special, overlap))
    return result


def _neg(special):
    """A sort key that puts the larger specificity first under an ascending sort; no specificity sorts last."""
    if not special:
        return (1,)
    return (0, -special[0], -special[1], -special[2])
