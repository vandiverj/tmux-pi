"""Product design package persistence: lock, journaled atomic commit, commit record, external-edit detection.

Control state lives in `<package>/.tp-product/` (docs/product-design.md, "Persistence"):

  commit.json        the commit record: revision, the canonical digest of each package file, the authority map
                     (package.authority_map) and the originals map (package.originals_map). Tracked with the package.
  history/NNNNNN.json one entry per committed revision: actor, evidence, summary, operations, impact, file digests.
                     Tracked. An entry is written once and never replaced.
  lock               flock target for every writer. Not content.
  txn/               the journal of a commit in progress. Not content; exists only during (or after an interrupted) commit.
                     txn/displaced/ holds the live files the commit moved aside until it is complete.

A commit is: take the lock; build the state it publishes and check it whole with the reader's own checks (prospective:
the documents, the commit record, the history with the new entry, and the reader's limits); write every new file into
txn/; check that no live file changed since the change was computed; write txn/txn.json last (the commit
point); place each changed live file (_place: rename the old one into txn/displaced/, check it, link the new one in;
someone else's bytes are never destroyed, only kept and reported), then the history entry and commit.json; remove txn/
and the displaced copies that hold the old content. A crash before txn.json leaves the live package untouched (the partial txn/ is discarded); a crash after it is
rolled forward by `recover`, which first checks the journal's files the same way (prospective) and, if they would
publish a state the reader refuses, refuses and keeps the journal. Readers never write: they report an interrupted commit, an uncommitted external edit and
control records that disagree, and readiness is not trusted until that is resolved.

Control records are believed only after their shape is checked and they agree: every history entry is well formed and
follows the one before it (check_history), the commit record's revision is the highest entry's, that entry records the
same file digests, and an unedited manifest carries the same revision. Digests are of the canonical JSON (package.canonical), so re-indenting a file is not an edit.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from . import package as P
from . import schema as S

sys.path.insert(0, str(P.ROOT / "lib"))
import safe_write  # noqa: E402

CONTROL = ".tp-product"
FILES = {key: name for key, (name, _, _) in P.DOCUMENTS.items()}  # doc key -> file name
LOCK_WAIT = 30.0
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
HISTORY_NAME = re.compile(r"([0-9]{6,})\.json\Z")
DISPLACED = "displaced"  # txn/displaced/: the live files a commit moved aside, until it is complete
UNREADABLE = "unreadable"  # the live digest of a file that exists but cannot be read back


class StoreError(Exception):
    """A refused operation. `code` is stable; `exit` is the CLI exit status; `details` is JSON-able."""

    def __init__(self, code, message, exit=1, details=None):
        super().__init__(message)
        self.code, self.exit, self.details = code, exit, details or {}


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


render = P.render  # the file form of package.encode: the one place a value becomes bytes


def control(package_dir) -> Path:
    return Path(package_dir) / CONTROL


def history_path(package_dir, revision) -> Path:
    return control(package_dir) / "history" / f"{revision:06d}.json"


def _read_json(path: Path, root):
    """P.ABSENT, or the parsed value of the control file at `path` below `root` (package.read_file: the one reader)."""
    safe_write.check_below(root, path, "package control file")
    return P.read_file(path)


def file_digests(docs) -> Dict[str, str]:
    return {FILES[key]: P.digest(docs[key]) for key in FILES if key in docs}


def commit_record(docs) -> dict:
    return {"schema": "tp-product-commit", "schema_version": 1, "revision": docs["manifest"]["revision"],
            "files": file_digests(docs), "authority": P.authority_map(docs), "originals": P.originals_map(docs)}


# ---------------------------------------------------------------- control record shapes

def _get(value, *path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _is_int(v, least=0):
    return type(v) is int and v >= least


def _digest_map(v, keys=None):
    return (isinstance(v, dict) and all(isinstance(k, str) and isinstance(d, str) and DIGEST.match(d)
                                        for k, d in v.items()) and (keys is None or set(v) == set(keys)))


def _fields(rec, required, optional=()):
    out = []
    missing = sorted(set(required) - set(rec))
    extra = sorted(set(rec) - set(required) - set(optional))
    if missing:
        out.append(f"missing {', '.join(missing)}")
    if extra:
        out.append(f"unknown field(s) {', '.join(extra)}")
    return out


def commit_problems(rec) -> list:
    """Why a commit record cannot be believed, as text; empty when its shape is right."""
    if not isinstance(rec, dict):
        return ["is not a JSON object"]
    out = _fields(rec, ("schema", "schema_version", "revision", "files", "authority", "originals"))
    if rec.get("schema") != "tp-product-commit" or type(rec.get("schema_version")) is not int or rec.get("schema_version") != 1:
        out.append("schema must be tp-product-commit version 1")
    if "revision" in rec and not _is_int(rec["revision"], 1):
        out.append("revision is a positive integer")
    if "files" in rec and not _digest_map(rec["files"], FILES.values()):
        out.append(f"files maps {', '.join(FILES.values())} to sha256 digests")
    for field in ("authority", "originals"):
        if field in rec and not _digest_map(rec[field]):
            out.append(f"{field} maps ids to sha256 digests")
    return out


def txn_problems(rec) -> list:
    if not isinstance(rec, dict):
        return ["is not a JSON object"]
    out = _fields(rec, ("revision", "old", "new", "old_commit", "new_commit"))
    if "revision" in rec and not _is_int(rec["revision"], 1):
        out.append("revision is a positive integer")
    if "new" in rec and not _digest_map(rec["new"], FILES.values()):
        out.append("new maps each package file to a digest")
    if rec.get("old") is not None and not _digest_map(rec["old"]):
        out.append("old is null or maps package files to digests")
    for field in ("old_commit", "new_commit"):
        value = rec.get(field)
        if (value is not None or field == "new_commit") and not (isinstance(value, str) and DIGEST.match(value)):
            out.append(f"{field} is a sha256 digest" + (" or null" if field == "old_commit" else ""))
    return out


def history_problems(rec, revision=None) -> list:
    if not isinstance(rec, dict):
        return ["is not a JSON object"]
    out = _fields(rec, ("schema", "schema_version", "revision", "base_revision", "committed", "actor", "summary",
                        "evidence", "operations", "files"), ("impact",))
    if rec.get("schema") != "tp-product-history" or type(rec.get("schema_version")) is not int or rec.get("schema_version") != 1:
        out.append("schema must be tp-product-history version 1")
    if "revision" in rec and not (_is_int(rec["revision"], 1) and (revision is None or rec["revision"] == revision)):
        out.append("revision is a positive integer" if revision is None else f"revision is {revision}, the entry's number")
    if rec.get("base_revision") is not None and not _is_int(rec["base_revision"], 1):
        out.append("base_revision is null or a positive integer")
    for field in ("committed", "actor", "summary"):
        if field in rec and not isinstance(rec[field], str):
            out.append(f"{field} is text")
    if "evidence" in rec and not (isinstance(rec["evidence"], list) and all(isinstance(e, str) for e in rec["evidence"])):
        out.append("evidence is a list of text")
    if "operations" in rec and not isinstance(rec["operations"], list):
        out.append("operations is a list")
    if "files" in rec and not _digest_map(rec["files"], FILES.values()):
        out.append("files maps each package file to a digest")
    if rec.get("impact") is not None and not isinstance(rec["impact"], dict):
        out.append("impact is null or an object")
    return out


def _load(root, rel, check):
    """("missing", None) | ("bad", message) | ("ok", value) for the control file at root/rel."""
    try:
        value = _read_json(root / rel, root)
    except (OSError, ValueError) as exc:
        return "bad", f"unreadable: {exc}"
    if value is P.ABSENT:
        return "missing", None
    problems = check(value)  # whatever JSON value the file holds, null included, is checked as the record
    return ("bad", "; ".join(problems)) if problems else ("ok", value)


def history_numbers(root) -> list:
    """The revision numbers of the history entries, sorted. Other names (a writer's temp file) are ignored."""
    try:
        names = os.listdir(control(root) / "history")
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(int(m.group(1)) for m in map(HISTORY_NAME.match, names) if m)


def check_history(root) -> dict:
    """Every history entry and the chain they form. The one check every reader and writer uses; never only the newest.

    Returns {numbers, entries, invalid, broken}:
      numbers   the revision of every entry file, ascending (a malformed entry included)
      entries   revision -> the entry, for the well-formed ones
      invalid   [(path below .tp-product, message)]: an entry that is unreadable, of the wrong shape or types, whose
                revision is not its file name's, or whose name is not the one tp-product writes (control.invalid)
      broken    [message]: the chain is not whole: a gap between two entries, or a base_revision that is not the
                previous entry's revision (package.control-mismatch)
    History may start after revision 1 (an imported package): the first entry's base_revision is null or the revision
    before it. Adjacency is compared between the entries that exist, so any integer revision is checked without
    building a range over the revision numbers.
    """
    out = {"numbers": [], "entries": {}, "invalid": [], "broken": []}
    try:
        names = sorted(os.listdir(control(root) / "history"))
    except (FileNotFoundError, NotADirectoryError):
        return out
    found = {}
    for name in names:
        m = HISTORY_NAME.match(name)
        if not m:
            continue  # not an entry name: a writer's temp file (".000004.json.<token>")
        rev, rel = int(m.group(1)), Path("history") / name
        if rev < 1 or name != f"{rev:06d}.json":
            out["invalid"].append((rel, f"is not a history entry name: revision {rev} is written as "
                                        f"{max(rev, 1):06d}.json"))
            continue
        status, value = _load(Path(root), Path(CONTROL) / rel, lambda h: history_problems(h, rev))
        found[rev] = value if status == "ok" else None
        if status != "ok":
            out["invalid"].append((rel, value))
    out["numbers"] = sorted(found)
    out["entries"] = {r: e for r, e in found.items() if e is not None}
    out["broken"] = chain_problems(found)
    return out


def chain_problems(found) -> list:
    """[message] for each place the chain of history entries is not whole. `found` maps the revision of every entry to
    the entry, or to None when it is malformed (reported on its own). Used on the history on disk (check_history) and
    on the history a commit would publish (prospective)."""
    out, prev = [], None
    for rev in sorted(found):
        entry = found[rev]
        if prev is not None and rev != prev + 1:
            out.append(f"history has no entry between revisions {prev} and {rev}")
        if entry is not None:
            base = entry["base_revision"]
            if prev is None and base not in (None, rev - 1):
                out.append(f"history entry {rev} is the first, so its base_revision is null or {rev - 1}, not {base}")
            elif prev is not None and base != prev:
                out.append(f"history entry {rev} has base_revision {base}, but the entry before it is {prev}")
        prev = rev
    return out


def agreement_problems(commit, numbers, entries, docs, interrupted=None) -> list:
    """[message] for each way a well-formed commit record disagrees with the history (`numbers`: every entry's revision;
    `entries`: the well-formed ones) and the package documents. `interrupted` is the revision of a journal after its
    commit point, which may already have written its history entry. Used by inspect and by prospective."""
    out, rev = [], commit["revision"]
    if interrupted is not None and interrupted not in (rev, rev + 1):
        return [f"the interrupted commit is to revision {interrupted}, but commit.json is at {rev}"]
    top = numbers[-1] if numbers else None
    if top != rev and not (interrupted == rev + 1 and top == rev + 1):
        out.append(f"commit.json is at revision {rev}, but the highest history entry is {'missing' if top is None else top}")
    entry = entries.get(rev)
    if entry is not None and entry["files"] != commit["files"]:
        out.append(f"history entry {rev} records other file digests than commit.json")
    manifest = docs.get("manifest")
    if (interrupted is None and isinstance(manifest, dict)
            and file_digests(docs).get(FILES["manifest"]) == commit["files"][FILES["manifest"]]
            and manifest.get("revision") != rev):
        out.append(f"commit.json is at revision {rev}, but the committed package.json says {manifest.get('revision')!r}")
    return out


def load_txn(root) -> dict:
    """The journal's txn.json, shape-checked, or StoreError control.invalid."""
    status, value = _load(Path(root), Path(CONTROL) / "txn" / "txn.json", txn_problems)
    if status != "ok":
        raise StoreError("control.invalid", f"{control(root) / 'txn' / 'txn.json'} is " + (
            "missing" if status == "missing" else value) + f": it is not a commit journal tp-product wrote. Move "
            f"{control(root) / 'txn'} aside by hand, then run `tp-product status`", exit=1)
    return value


# ---------------------------------------------------------------- inspection (read-only)

def inspect(package_dir, prove=None) -> dict:
    """What a reader must know before trusting the package. Never writes.

    Returns {docs, issues, commit, commit_state, commit_digest, interrupted, external, authority_changed,
    originals_changed, control, history, displaced}:
      issues             read errors (package.read_docs), else validation issues; plus control.invalid for a control
                         file (commit.json, txn/txn.json, any history entry) that is unreadable or malformed
      history            the revisions of the history entries, ascending (check_history)
      displaced          paths (below the package) of copies a commit displaced and kept, because they are not the
                         content it expected to replace: someone else's bytes
      commit             the commit record when its shape is right, else None
      commit_state       "ok", "missing" or "bad"
      commit_digest      the commit record's canonical digest (what a writer checks before replacing it), or None
      interrupted        the journal's revision when a commit was interrupted after its commit point, else None
      external           file names whose content differs from the commit record (all three when there is no record)
      authority_changed  ids whose acceptance fields differ from the commit record
      originals_changed  original records (notes, inputs, reviews) the commit record holds that are gone or changed
      control            (code, message) for control records that disagree with each other: not trusted
      builds             (file, why) for each build the interrupted commit records whose file is missing or not the
                         recorded bytes: recovery refuses to commit it (build_problems)
      recorded_builds    (file, why) for each build the manifest records whose bytes are not proved
                         (recorded_build_problems): missing, not a regular file, not the recorded digest, or for an
                         entry without a digest not the page `prove(docs, entry)` renders (review.build_prover). An
                         entry without a digest is proved only against committed files: while a package file differs
                         from the commit record (or without `prove`) it is unproved
    """
    root = Path(package_dir)
    docs, issues = P.read_docs(root)
    out = {"docs": docs, "issues": issues, "commit": None, "commit_state": "missing", "commit_digest": None,
           "interrupted": None, "external": [], "authority_changed": [], "originals_changed": [], "control": [],
           "history": [], "displaced": [], "builds": [], "recorded_builds": []}
    if not issues:
        out["issues"] = P.validate_docs(docs)

    def bad(rel, message):
        out["issues"] = out["issues"] + [S.Issue("control.invalid", (CONTROL,) + tuple(Path(rel).parts), message)]

    status, value = _load(root, Path(CONTROL) / "commit.json", commit_problems)
    out["commit_state"] = status
    if status == "bad":
        bad("commit.json", value)
    elif status == "ok":
        out["commit"], out["commit_digest"] = value, P.digest(value)
    status, txn = _load(root, Path(CONTROL) / "txn" / "txn.json", txn_problems)
    if status == "bad":
        bad("txn/txn.json", txn)
    elif status == "ok":
        out["interrupted"] = txn["revision"]
        with contextlib.suppress(OSError, ValueError):  # a malformed staged entry is _staged's refusal, at --recover
            entry = _read_json(control(root) / "txn" / "history.json", root)
            out["builds"] = build_problems(root, entry)
    out["displaced"] = [str(p.relative_to(root)) for p in _kept(root, txn if status == "ok" else None)]
    commit = out["commit"]

    def mismatch(message):
        out["control"].append(("package.control-mismatch", message + "; restore .tp-product/ from version control"))

    hist = check_history(root)  # every entry, every time
    out["history"] = hist["numbers"]
    for rel, message in hist["invalid"]:
        bad(rel, message)
    for message in hist["broken"]:
        mismatch(message)
    if commit:
        for message in agreement_problems(commit, hist["numbers"], hist["entries"], docs, out["interrupted"]):
            mismatch(message)

    committed = commit if commit is not None else {"files": {}, "authority": {}, "originals": {}}
    live = file_digests(docs)
    out["external"] = sorted(name for name in FILES.values() if live.get(name) != committed["files"].get(name))
    if not out["issues"]:  # a valid manifest: every build it records must be there, holding bytes that are proved
        if prove is not None and out["external"]:
            def prove(_docs, _entry, files=", ".join(out["external"])):  # a render of uncommitted files proves nothing
                raise StoreError("package.external-edit", f"{files} differ from the commit record")
        out["recorded_builds"] = recorded_build_problems(root, docs, prove)
    if all(k in docs for k in FILES):
        current, before = P.authority_map(docs), committed["authority"]
        out["authority_changed"] = sorted(i for i in set(current) | set(before) if current.get(i) != before.get(i))
        current, before = P.originals_map(docs), committed["originals"]
        out["originals_changed"] = sorted(i for i in before if current.get(i) != before[i])
    return out


def trust_problems(state) -> list:
    """Why readiness read from this package cannot be trusted yet, as (code, message)."""
    out = []
    if state["interrupted"] is not None:
        out.append(("package.interrupted",
                    f"a commit to revision {state['interrupted']} was interrupted; `tp-product apply DIR --recover` finishes it"))
    malformed = ["/".join(map(str, i.path)) for i in state["issues"] if i.code == "control.invalid"]
    if malformed:  # any malformed control record, whichever it is: never trusted
        out.append(("package.control-invalid", f"{', '.join(malformed)} is malformed (see the control.invalid "
                                               "issue); restore it from version control"))
    if state["displaced"]:
        out.append(("package.displaced",
                    f"{', '.join(state['displaced'])} holds bytes someone else wrote while a commit replaced the file; "
                    "they were kept. Compare them with the live file, move what you need out of .tp-product/txn/, then "
                    "run `tp-product apply DIR --recover`"))
    if state.get("builds"):
        out.append(("package.build-missing",
                    "the interrupted commit records review build(s) whose file is not the recorded build ("
                    + "; ".join(f"{rel} {why}" for rel, why in state["builds"]) + "); `tp-product apply DIR --recover` "
                    "refuses to commit them. Put the build file back, or move .tp-product/txn/ aside by hand"))
    def kind(why):
        return "missing" if why == MISSING else "unproved" if why.startswith(UNPROVED) else "changed"
    for code, verb in (("package.build-missing", "missing"), ("package.build-changed", "changed"),
                       ("package.build-unproved", "unproved")):
        found = [(rel, why) for rel, why in state.get("recorded_builds", ()) if kind(why) == verb]
        if not found:
            continue
        listed = "; ".join(f"{rel} {why}" for rel, why in found)
        if verb == "unproved":
            out.append((code, "the manifest records review build(s) from before digests whose bytes cannot be proved ("
                        + listed + "); a reader cannot rely on them. Check each file against version control, then "
                        "record its sha256 as the entry's digest (or remove the entry) with an adopt-external-edit "
                        "changeset"))
            continue
        out.append((code, "the manifest records review build(s) that are " + verb + " (" + listed + "); a reader "
                    "cannot rely on the recorded build. Put the recorded file back (from version control), or "
                    + ("" if verb == "missing" else "move it aside and ") + "run `tp-product build DIR`, which "
                    "restores the last build when it still renders the recorded bytes"))
    if state["commit"] is None and state["commit_state"] != "bad":  # a malformed one is reported above
        out.append(("package.uncommitted", "no commit record (.tp-product/commit.json): not created by `tp-product init`, "
                                            "or its control state was removed"))
    elif state["commit"] is not None and state["external"] and state["interrupted"] is None:
        out.append(("package.external-edit",
                    f"{', '.join(state['external'])} changed outside `tp-product apply`; adopt the edit with an "
                    "adopt-external-edit changeset, or restore the committed files"))
    return out + list(state["control"])


# ---------------------------------------------------------------- the prospective state

def prospective(root, docs, entry, staged=None) -> dict:
    """Build the state a commit would publish and check it whole, with the checks `status` and `validate` apply to the
    published package (validate_docs, commit_problems, history_problems, check_history, chain_problems,
    agreement_problems) and the reader's limits (file_problem). Every commit (apply, adoption, ingest, init) calls it
    before its commit point, and recovery calls it on the journal's files before it replaces anything: no command
    publishes a state its own reader refuses (docs/product-design.md, "Persistence").

    A commit passes the documents and its history entry without revision and files (`staged` None): the commit record
    is built here and the entry completed. Recovery passes the staged documents and entry, and `staged` = (the staged
    commit record, the journal's revision): each is checked as the value its file holds, whatever JSON value that is
    (null is a malformed record, never "no record"), the record against the one these documents make.

    Returns {revision, record, entry, data, problems}. data holds the rendered bytes by file name (the package files,
    "history.json", "commit.json"); problems lists (code, file, message), empty when the state may be published:
      result.unreadable         a file the reader would refuse: too deep, too large, text that is not UTF-8
      result.invalid            the documents do not validate
      control.invalid           the commit record or the entry is malformed, an existing history entry is malformed,
                                or the journal's revision is not the documents'
      package.control-mismatch  the records disagree: the commit record is not the one the documents make, the history
                                with the new entry is not whole, or the commit record is not its newest entry
    """
    record = None if staged is None else staged[0]
    out = {"revision": None, "record": record, "entry": entry, "data": {}, "problems": []}

    def refuse(code, name, message):
        out["problems"].append((code, name, message))
        return out

    for key, name in FILES.items():  # the reader's limits first: nothing below encodes a value it would refuse
        if key not in docs:
            return refuse("result.invalid", name, "is missing")
        data, problem = P.file_problem(docs[key], render)
        if problem:
            return refuse("result.unreadable", name, f"would be {problem}")
        out["data"][name] = data
    issues = P.validate_docs(docs)
    if issues:
        return refuse("result.invalid", issues[0].path[0] if issues[0].path else "package",
                      "does not validate: " + "; ".join(f"{S.format_path(i.path)}: {i.message} [{i.code}]"
                                                         for i in issues))
    rev = out["revision"] = docs["manifest"]["revision"]
    built = commit_record(docs)
    if staged is None:
        if not isinstance(entry, dict):
            return refuse("control.invalid", f"history entry {rev}", "is not a JSON object")
        record = out["record"] = built
        entry = out["entry"] = dict(entry, revision=rev, files=built["files"])
    elif staged[1] != rev:
        return refuse("control.invalid", "package.json", f"is revision {rev!r}, but the journal commits revision "
                                                         f"{staged[1]}")
    for name, problems in (("commit.json", commit_problems(record)),
                           (f"history entry {rev}", history_problems(entry, rev))):
        if problems:
            return refuse("control.invalid", name, "is malformed: " + "; ".join(problems))
    if record != built:
        return refuse("package.control-mismatch", "commit.json", "is not the commit record of the package files "
                                                                 "committed with it")
    hist = check_history(root)
    if hist["invalid"]:
        rel, message = hist["invalid"][0]
        return refuse("control.invalid", f"{CONTROL}/{rel.as_posix()}", f"is {message}")
    found = {n: hist["entries"].get(n) for n in hist["numbers"]}
    found[rev] = entry
    problems = chain_problems(found) + agreement_problems(record, sorted(found), found, docs)
    if problems:
        return refuse("package.control-mismatch", f"history entry {rev}", "; ".join(problems))
    for name, value in (("history.json", entry), ("commit.json", record)):
        data, problem = P.file_problem(value, render)
        if problem:
            return refuse("result.unreadable", f"history entry {rev}" if name == "history.json" else name,
                          f"would be {problem}")
        out["data"][name] = data
    return out


EXITS = {"package.control-mismatch": 3}


# ---------------------------------------------------------------- lock

@contextlib.contextmanager
def lock(package_dir, wait=LOCK_WAIT):
    """Hold the package's writer lock (flock on .tp-product/lock). Waits up to `wait` seconds, then StoreError lock.busy."""
    root = Path(package_dir)
    safe_write.ensure_dir(control(root), what="package control directory", root=root)
    path = control(root) / "lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | safe_write.NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= deadline:
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise StoreError("lock.busy", f"another writer holds {path} (waited {wait:g}s); retry when it finishes",
                                         exit=5) from None
                    raise
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)


# ---------------------------------------------------------------- commit and recovery

def _noop(point):
    return None


def _write(root, path, data):
    safe_write.write_atomic(path, data, root=root, fsync=True)


def _live_digest(root: Path, rel) -> Optional[str]:
    """The canonical digest of a live file; None when it is absent; UNREADABLE when it cannot be read back."""
    try:
        value = _read_json(root / rel, root)
    except (OSError, ValueError):
        return UNREADABLE
    return None if value is P.ABSENT else P.digest(value)


def _expected(meta, name):
    """(old, new): the digest a live package file or commit.json had when the change was computed, and will have."""
    if name == "commit.json":
        return meta["old_commit"], meta["new_commit"]
    return (None if meta["old"] is None else meta["old"].get(name)), meta["new"][name]


def _live_rel(name) -> Path:
    return Path(CONTROL) / "commit.json" if name == "commit.json" else Path(name)


def _displaced(root: Path, meta) -> list:
    """[(path, file name or None, ours)] for each copy in txn/displaced/. A copy is `ours` when it holds the content
    the commit expected to displace (its file's old content, or the new one: no one else's bytes either way); without a
    journal (`meta` None) none is. The copy of FILE is named FILE~TOKEN."""
    held = control(root) / "txn" / DISPLACED
    safe_write.check_below(root, held, "package journal")
    try:
        names = sorted(os.listdir(held))
    except (FileNotFoundError, NotADirectoryError):
        return []
    out = []
    for n in names:
        name = n.rsplit("~", 1)[0] if "~" in n else None
        name = name if name in ALL else None
        ours = meta is not None and name is not None and _live_digest(held, n) in _expected(meta, name)
        out.append((held / n, name, ours))
    return out


def _kept(root: Path, meta) -> list:
    """The displaced copies no commit may remove: someone else's bytes (see _displaced)."""
    return [p for p, _, ours in _displaced(root, meta) if not ours]


def _changed_under(root: Path, meta: dict, names, copies=()) -> list:
    """The files among `names` (package file names, or "commit.json") that are neither what the change was computed
    from (meta old) nor what it writes (meta new): someone else wrote them. Absent counts as None on both sides; a file
    this commit displaced before an interruption (a copy of it in `copies` is ours) is absent by its own doing."""
    out = []
    for name in names:
        live = _live_digest(root, _live_rel(name))
        if live in _expected(meta, name) or (live is None and any(n == name and ours for _, n, ours in copies)):
            continue
        out.append(name)
    return out


ALL = tuple(FILES.values()) + ("commit.json",)


def _discard(root: Path, txn: Path):
    """Remove a journal that stopped before its commit point. Nothing live was touched, so it holds no displaced copy;
    if it does (a journal edited by hand), it is not removed: those may be someone's only bytes."""
    if _displaced(root, None):
        raise StoreError("control.invalid", f"{txn} has no txn.json but holds displaced copies in {txn / DISPLACED}: "
                                            "move them out of .tp-product/ by hand, then retry", exit=1)
    shutil.rmtree(txn)


def commit(package_dir, docs, entry, fault: Callable = _noop, base_digests=None, base_commit=None):
    """Write `docs` as the next committed state. Call with the lock held and `docs` already validated.

    `entry` is the history entry (revision filled in from docs). `base_digests` are the digests of the live files the
    change was computed from and `base_commit` the digest of the commit record it was computed against (None for init:
    the files and the record must be absent). A file that differs from them at the commit point is someone else's write:
    the commit is refused (commit.external-edit) and their bytes are kept. The existing history must let this revision
    follow it (check_history). `fault(point)` is called at each step so a test can stop the commit there or act as
    another writer.
    """
    root = Path(package_dir)
    txn = control(root) / "txn"
    safe_write.check_below(root, txn, "package journal")
    if txn.exists() and not (txn / "txn.json").exists():
        _discard(root, txn)  # a commit that stopped before its commit point: nothing live was touched
    elif txn.exists():
        raise StoreError("package.interrupted", f"{txn} holds an interrupted commit: run `tp-product apply DIR --recover` "
                                                "first", exit=3)
    revision = _get(docs, "manifest", "revision")
    if _is_int(revision, 1) and os.path.lexists(history_path(root, revision)):  # someone's entry: never replaced
        raise StoreError("history.exists", f"{history_path(root, revision)} already exists: a history entry is never "
                                           "replaced. Nothing was written; check .tp-product/ against version control",
                         exit=3)
    state = prospective(root, docs, entry)  # the whole state this commit publishes, checked as its reader will
    if state["problems"]:
        code, name, message = state["problems"][0]
        raise StoreError(code, f"{name} {message}: the package would not be read back as committed. Nothing was "
                               "written.", exit=EXITS.get(code, 1), details={"file": name, "problem": message})
    revision, record, rendered = state["revision"], state["record"], state["data"]
    meta = {"revision": revision, "old": base_digests, "new": record["files"], "old_commit": base_commit,
            "new_commit": P.digest(record)}
    meta_data, problem = P.file_problem(meta, render)
    if problem:  # base_digests from a caller: digests, so only a malformed argument fails here
        raise StoreError("result.unreadable", f"txn.json would be {problem}. Nothing was written.",
                         details={"file": "txn.json", "problem": problem})
    if txn_problems(meta):
        raise StoreError("control.invalid", f"txn.json would be malformed: {'; '.join(txn_problems(meta))}. Nothing "
                                            "was written.", details={"file": "txn.json"})
    safe_write.ensure_dir(txn, what="package journal", root=root)
    try:
        for name, data in rendered.items():
            _write(root, txn / name, data)
        fault("before-commit-point")
        changed = _changed_under(root, meta, ALL) + _history_taken(root, revision, rendered["history.json"])
        if changed:
            raise StoreError("commit.external-edit",
                             f"{', '.join(changed)} changed while this commit was being prepared (someone edited it "
                             "outside tp-product). Nothing was written and their content is kept: read `tp-product "
                             "status`, then adopt or restore the edit and retry", exit=3, details={"files": changed})
        found = build_problems(root, state["entry"])
        if found:
            raise _build_refusal(found, "nothing was written")
        _write(root, txn / "txn.json", meta_data)
    except BaseException:
        # txn.json is the commit point, whatever raised after it landed (an error or an interrupt during the rename's
        # own cleanup or a later fsync): from then on only recovery decides, so nothing here removes the journal.
        if not os.path.lexists(txn / "txn.json"):
            shutil.rmtree(txn, ignore_errors=True)  # nothing live was touched (nothing is displaced before it)
        raise
    fault("after-commit-point")
    _roll_forward(root, fault)


def _history_taken(root, revision, data) -> list:
    """[the history entry's name] when a file other than `data` already holds it, else []."""
    target = history_path(root, revision)
    if not os.path.lexists(target):
        return []
    safe_write.check_below(root, target, "package history entry")
    return [] if target.read_bytes() == data else [f"{CONTROL}/history/{target.name}"]


def file_digest(path) -> str:
    """sha256 of a file's bytes, as "sha256:HEX" (what a build entry records)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def build_file_problem(root, rel, want=None, render=None) -> Optional[str]:
    """Why root/rel is not the review build a manifest entry records, or None: a regular file below the package, no
    link on the way, whose bytes are proved. With digest `want`, the proof is that digest. Without one (an entry from
    before digests), `render` is how the bytes are proved: called, it returns the page the committed package renders for
    that entry (bytes, which the file must equal), or None when the package no longer renders that build; it may raise
    StoreError (the package cannot render). Without either, only the file's presence is checked (build_problems: a
    journal entry this code wrote, whose operations always carry the digest)."""
    if not isinstance(rel, str) or not rel:
        return "is not a file name"
    path = Path(root) / rel
    try:
        safe_write.check_below(Path(root), path, "build file")
        st = os.lstat(path)
    except FileNotFoundError:
        return MISSING
    except (OSError, ValueError) as exc:
        return f"cannot be read ({getattr(exc, 'strerror', None) or exc})"
    if not stat.S_ISREG(st.st_mode):
        return "is not a regular file"
    if want is None and render is None:
        return None
    if want is None:
        try:
            page = render()
        except StoreError as exc:
            return UNPROVED + f" (the package cannot render it: {exc})"
        if page is None:
            return UNPROVED + " (this package no longer renders that build)"
        want = "sha256:" + hashlib.sha256(page).hexdigest()
    try:
        got = file_digest(path)
    except OSError as exc:
        return f"cannot be read ({exc.strerror or exc})"
    return None if got == want else "holds other bytes than the recorded build"


MISSING = "is missing"
# An entry without a digest whose bytes cannot be compared with anything: never trusted, never reported up to date.
UNPROVED = "has no recorded digest and its bytes cannot be proved"


def recorded_build_problems(root, docs, prove=None) -> list:
    """[(file, why)] for each build the manifest records whose bytes are not proved to be that build: missing, not a
    regular file, other bytes than its digest, or (an entry from before digests) other bytes than the page the
    committed package renders for it, or no such page. prove(docs, entry) returns that page (bytes) or None; without
    `prove`, an entry with no digest is unproved. The view of recorded_build_checks for a reader that only names."""
    return [(c["file"], c["why"]) for c in recorded_build_checks(root, docs, prove)]


def recorded_build_checks(root, docs, prove=None) -> list:
    """recorded_build_problems with each problem's entry kept: [{"index", "entry", "file", "why"}], one per manifest
    build entry whose bytes are not proved, in manifest order. Every entry is its own proof obligation: several entries
    naming one file are each checked against its bytes, and a writer that puts a file back must satisfy all of them
    (review._recorded_builds)."""
    builds = _get(docs, "manifest", "builds")
    out = []
    for i, b in enumerate(builds if isinstance(builds, list) else []):
        if not isinstance(b, dict):
            out.append({"index": i, "entry": b, "file": None, "why": "is not a build entry"})
            continue
        want = b.get("digest") if isinstance(b.get("digest"), str) else None
        why = build_file_problem(root, b.get("file"), want,
                                 (lambda b=b: prove(docs, b)) if prove is not None else (lambda: None))
        if why:
            out.append({"index": i, "entry": b, "file": b.get("file"), "why": why})
    return out


def build_problems(root, entry) -> list:
    """[(file, why)] for each record-build operation of a history entry whose file is not the recorded build. A commit
    checks this just before its commit point and recovery before it publishes anything: a manifest never records a
    build whose file is absent or differs."""
    ops = entry.get("operations") if isinstance(entry, dict) else None
    out = []
    for op in ops if isinstance(ops, list) else []:
        if isinstance(op, dict) and op.get("op") == "record-build":
            why = build_file_problem(root, op.get("file"), op.get("digest") if isinstance(op.get("digest"), str) else None)
            if why:
                out.append((op.get("file"), why))
    return out


def _build_refusal(found, when):
    return StoreError("package.build-missing",
                      "; ".join(f"{rel} {why}" for rel, why in found) + f": the commit records that review build, so "
                      f"{when}. Put the build file back (or move the journal aside by hand) and run `tp-product status`",
                      exit=3, details={"builds": [{"file": rel, "problem": why} for rel, why in found]})


def _write_new(root, target: Path, data):
    """Create `target` with `data`, never replacing a file that is there: a temp file linked into place."""
    temp = safe_write.write_temp(target, data, root=root, fsync=True)
    try:
        os.link(temp, target)
    finally:
        os.unlink(temp)


def _conflict(root, meta, name, why="was changed by someone else", kept=()):
    txn = control(root) / "txn"
    what = ".tp-product/commit.json" if name == "commit.json" else name
    kept = [str(p) for p in kept]
    return StoreError("txn.conflict", f"{what} {why} during the commit to revision {meta['revision']}; it was not "
                                      "overwritten" + (f", and the bytes the commit displaced are kept at "
                                                       f"{', '.join(kept)}" if kept else "") +
                                      f". Compare it with {txn / name} and restore one of them, then run "
                                      "`tp-product apply DIR --recover`", exit=3,
                      details={"file": name, "revision": meta["revision"], "kept": kept})


def _place(root: Path, meta: dict, name, fault: Callable):
    """Make the journal's `name` (a package file or commit.json) the live file without destroying bytes this commit did
    not write (docs/product-design.md, "Persistence").

    The live file is renamed into txn/displaced/ (never unlinked), the displaced bytes are checked against what the
    change was computed from, and the new file is linked into place: a link fails if anything has appeared at the
    target since. After the link the displaced copy is checked again (a writer holding the old file open may have
    written to it). Anything unexpected is kept and named in a txn.conflict; the journal stays, so the package reports
    the interrupted commit, and the kept copy, until `--recover` finishes it."""
    rel = _live_rel(name)
    target = root / rel
    old, new = _expected(meta, name)
    if _live_digest(root, rel) == new:  # already in place: unchanged by this commit, or placed before an interruption
        fault("after-replace:" + name)
        return
    txn = control(root) / "txn"
    held_dir = safe_write.ensure_dir(txn / DISPLACED, what="package journal", root=root)
    temp = safe_write.write_temp(target, (txn / name).read_bytes(), root=root, fsync=True)
    try:
        fault("before-displace:" + name)
        held = held_dir / f"{name}~{secrets.token_hex(8)}"
        try:
            os.rename(target, held)  # the live inode keeps a name: the commit never unlinks it
        except FileNotFoundError:
            held = None
        fault("after-displace:" + name)
        if held is None:
            if old is not None and not any(n == name and ours for _, n, ours in _displaced(root, meta)):
                raise _conflict(root, meta, name, "was removed by someone else")
        elif _live_digest(held_dir, held.name) not in (old, new):
            try:  # put their bytes back, unless the target has reappeared since
                os.link(held, target, follow_symlinks=False)
                why = "was changed by someone else (their bytes are back in place)"
            except FileExistsError:
                why = "was changed by someone else, then saved again (the newer save is in place)"
            raise _conflict(root, meta, name, why, kept=[held])
        fault("after-check:" + name)
        fault("before-link:" + name)
        try:
            os.link(temp, target, follow_symlinks=False)
        except FileExistsError:
            raise _conflict(root, meta, name, "was saved by someone else while it was being replaced (their file is "
                                              "in place)", kept=[held] if held else []) from None
        fault("after-link:" + name)
        if held is not None and _live_digest(held_dir, held.name) not in (old, new):
            raise _conflict(root, meta, name, "was written in place by someone else while it was being replaced (the "
                                              "new content is live)", kept=[held])
        fault("after-replace:" + name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def _finish_txn(root: Path, meta: dict, fault: Callable):
    """Remove the journal of a complete commit. A displaced copy is removed only when it is ours (see _displaced),
    checked just before; any other is kept, and the journal with it (txn.conflict), so status keeps reporting it."""
    txn = control(root) / "txn"
    fault("before-cleanup")
    copies = _displaced(root, meta)
    kept = [p for p, _, ours in copies if not ours]
    if kept:
        raise StoreError("txn.conflict", f"the commit to revision {meta['revision']} is in place, but "
                                         f"{', '.join(map(str, kept))} holds bytes someone else wrote while a file was "
                                         "being replaced; they were kept. Compare them with the live file, move what "
                                         "you need out of .tp-product/txn/, then run `tp-product apply DIR --recover`",
                         exit=3, details={"revision": meta["revision"], "kept": [str(p) for p in kept]})
    for path, _, _ in copies:
        os.unlink(path)
    try:
        os.rmdir(txn / DISPLACED)
    except FileNotFoundError:
        pass
    except OSError as exc:  # a copy appeared after the check: keep it, and the journal
        if exc.errno != errno.ENOTEMPTY:
            raise
        raise StoreError("txn.conflict", f"{txn / DISPLACED} gained a file while the commit was finishing; it was "
                                         "kept. Move it out of .tp-product/txn/, then run `tp-product apply DIR "
                                         "--recover`", exit=3, details={"revision": meta["revision"]}) from None
    shutil.rmtree(txn)


def _roll_forward(root: Path, fault: Callable = _noop):
    """Make the journal's files live (each by _place), then its history entry and commit record."""
    txn = control(root) / "txn"
    meta = load_txn(root)
    target = history_path(root, meta["revision"])
    with contextlib.suppress(OSError):  # someone else's entry in this commit's slot first: it is kept (txn.conflict)
        taken = _history_taken(root, meta["revision"], (txn / "history.json").read_bytes())
        if taken:
            raise _conflict(root, meta, taken[0])
    data = _staged(root, meta)  # then the journal's state, whole, before any live file is replaced
    entry = json.loads(data)
    for name in FILES.values():
        _place(root, meta, name, fault)
    safe_write.ensure_dir(target.parent, what="package history", root=root)
    if not os.path.lexists(target):  # present with these bytes: written by this commit before an interruption
        try:
            _write_new(root, target, data)
        except FileExistsError:
            pass
    if _history_taken(root, meta["revision"], data):  # never replaced, whoever wrote it
        raise _conflict(root, meta, f"{CONTROL}/history/{target.name}")
    found = build_problems(root, entry)  # again, last thing before the commit record: never commit a missing build
    if found:
        raise _build_refusal(found, "the commit record was not written; the journal is kept")
    _place(root, meta, "commit.json", fault)
    fault("after-commit-record")
    _finish_txn(root, meta, fault)


def _staged(root: Path, meta) -> bytes:
    """Check the journal's files before any of them is made live: each is what txn.json says it holds, and together
    they are a state the reader accepts (prospective). Returns the staged history entry's bytes. A refusal is a
    structured control error naming the journal and its staged files, which are kept as they are."""
    txn = control(root) / "txn"
    staged = sorted(str(p) for p in txn.iterdir() if p.is_file())

    def refuse(code, name, message):
        return StoreError(code, f"{name} in the journal {message}. Publishing it would leave a package `tp-product "
                                f"status` refuses, so nothing was replaced; the journal {txn} and its staged files are "
                                "kept. Restore the package from version control, or move the journal aside by hand",
                          exit=EXITS.get(code, 1), details={"file": name, "problem": message, "journal": str(txn),
                                                            "staged": staged})
    values = {}
    for name in ALL + ("history.json",):  # each staged file once, by the one reader: absent only if it is not there
        try:
            values[name] = _read_json(txn / name, txn)
        except (OSError, ValueError) as exc:
            raise refuse("control.invalid", name, f"is unreadable: {exc}") from None
        if values[name] is P.ABSENT:
            raise refuse("control.invalid", name, "is missing")
    for name in ALL:  # the journal must hold what txn.json says it holds
        if P.digest(values[name]) != _expected(meta, name)[1]:
            raise refuse("control.invalid", name, f"does not match {txn / 'txn.json'}")
    docs = {key: values[name] for key, name in FILES.items()}
    state = prospective(root, docs, values["history.json"], staged=(values["commit.json"], meta["revision"]))
    if state["problems"]:
        code, name, message = state["problems"][0]
        raise refuse("control.invalid" if code.startswith("result.") else code, name, message)
    found = build_problems(root, values["history.json"])
    if found:
        raise _build_refusal(found, "nothing was replaced and the journal is kept")
    return (txn / "history.json").read_bytes()


def recover(package_dir, fault: Callable = _noop) -> Optional[dict]:
    """With the lock held: finish a commit interrupted after its commit point, or discard one interrupted before it.
    Returns {"action": "rolled-forward"|"discarded", "revision": N} or None when there was nothing to recover.
    Refuses (StoreError txn.conflict) when a live file is neither the old nor the new committed content (someone edited
    it during the interruption: overwriting it would lose that edit), or when a displaced copy holds someone else's
    bytes (kept until the owner moves it). Roll-forward replaces files the same way a commit does (_place). A
    malformed journal is control.invalid."""
    root = Path(package_dir)
    txn = control(root) / "txn"
    safe_write.check_below(root, txn, "package journal")
    if not txn.exists():
        return None
    if not (txn / "txn.json").exists():
        _discard(root, txn)
        return {"action": "discarded", "revision": None}
    meta = load_txn(root)
    copies = _displaced(root, meta)
    kept = [p for p, _, ours in copies if not ours]
    if kept:
        raise StoreError("txn.conflict", f"{', '.join(map(str, kept))} holds bytes someone else wrote while the commit "
                                         f"to revision {meta['revision']} replaced a file; they are kept. Compare them "
                                         "with the live file, move what you need out of .tp-product/txn/, then run "
                                         "`tp-product apply DIR --recover` again", exit=3,
                         details={"revision": meta["revision"], "kept": [str(p) for p in kept]})
    changed = _changed_under(root, meta, ALL, copies)  # all of them, before any is replaced
    if changed:
        raise _conflict(root, meta, changed[0])
    _roll_forward(root, fault)
    return {"action": "rolled-forward", "revision": meta["revision"]}


# ---------------------------------------------------------------- init

def scaffold(package_id, title, owner, objective, today):
    manifest = {
        "schema": "tp-product-package", "schema_version": 1, "id": package_id, "title": title, "owner": owner,
        "objective": objective, "source_roots": [], "revision": 1, "activity": "frame",
        "next_action": {"text": "Frame the product: actor, problem, desired outcome, scope and known constraints.",
                        "owner": owner, "set": today},
        "builds": [],
    }
    design = {"schema": "tp-product-design", "schema_version": 1, "package": package_id}
    for coll, (doc, _) in P.COLLECTIONS.items():
        if doc == "design":
            design[coll] = []
    reviews = {"schema": "tp-product-reviews", "schema_version": 1, "package": package_id,
               "notes": [], "inputs": [], "reviews": []}
    return {"manifest": manifest, "design": design, "reviews": reviews}


def history_entry(actor, summary, operations, base_revision, evidence=(), impact=None) -> dict:
    return {"schema": "tp-product-history", "schema_version": 1, "base_revision": base_revision, "committed": now(),
            "actor": actor, "summary": summary, "evidence": list(evidence), "operations": operations, "impact": impact}


def init(package_dir, package_id, title, owner="Not set", objective="Not framed yet.", actor="tp-product init",
         fault: Callable = _noop):
    """Create a package at revision 1. Refuses when a package file or a commit record is already there."""
    root = Path(package_dir)
    if root.is_symlink():
        raise StoreError("init.unsafe", f"{root} is a symbolic link")
    def present():  # a history entry counts: init never starts a second history under an old one
        names = [n for n in FILES.values() if os.path.lexists(root / n)]
        names += [f"{CONTROL}/commit.json"] * os.path.lexists(control(root) / "commit.json")
        return names + [f"{CONTROL}/history/{h:06d}.json" for h in history_numbers(root)]
    if present() and not (control(root) / "txn" / "txn.json").exists():
        raise StoreError("init.exists", f"{root} already holds {', '.join(present())}; refusing to overwrite a package")
    today = now()[:10]
    docs = scaffold(package_id, title, owner, objective, today)
    text = P.text_problem(docs)  # the one representability rule (package.text_problem), before the schema walks it
    issues = [S.Issue("file.encoding", (), text)] if text else P.validate_docs(docs)
    if issues:
        raise StoreError("init.invalid", "; ".join(f"{S.format_path(i.path)}: {i.message}" for i in issues),
                         details={"issues": [i._asdict() for i in issues]})
    root.mkdir(parents=True, exist_ok=True)
    # A lock file or journal left by a failed init does not block a retry: under the lock, an init interrupted after its
    # commit point is finished (and then refused as existing), one interrupted before it is discarded.
    with lock(root):
        recover(root)
        if present():  # re-checked under the lock: a racing init lost
            raise StoreError("init.exists", f"{root} was initialised by another writer: {', '.join(present())}")
        safe_write.ensure_dir(control(root), what="package control directory", root=root)
        try:  # created once, never replaced: a .gitignore someone edited is theirs
            _write_new(root, control(root) / ".gitignore", IGNORE)
        except FileExistsError:
            pass
        commit(root, docs, history_entry(actor, f"init {package_id}", [], None), fault=fault)
    return docs


IGNORE = b"# tp-product: the writer lock and an in-progress commit journal are not package content\nlock\ntxn/\n"
