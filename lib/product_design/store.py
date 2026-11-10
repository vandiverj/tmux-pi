"""Product design package persistence: lock, journaled atomic commit, commit record, external-edit detection.

Control state lives in `<package>/.tp-product/` (docs/product-design.md, "Persistence"):

  commit.json        the commit record: revision, the canonical digest of each package file, the authority map
                     (package.authority_map) and the originals map (package.originals_map). Tracked with the package.
  history/NNNNNN.json one entry per committed revision: actor, evidence, summary, operations, impact, file digests.
                     Tracked. An entry is written once and never replaced.
  lock               flock target for every writer. Not content.
  txn/               the journal of a commit in progress. Not content; exists only during (or after an interrupted) commit.

A commit is: take the lock; check that every file it writes is readable back (package.file_problem); write every new
file into txn/; check that no live file changed since the change was computed; write txn/txn.json last (the commit
point); replace the live files (each re-checked just before it is replaced), the history entry and commit.json; remove
txn/. A crash before txn.json leaves the live package untouched (the partial txn/ is discarded); a crash after it is
rolled forward by `recover`. Readers never write: they report an interrupted commit, an uncommitted external edit and
control records that disagree, and readiness is not trusted until that is resolved.

Control records are believed only after their shape is checked and they agree: the commit record's revision is the
highest history entry's, that entry records the same file digests, history has no gap, and an unedited manifest carries
the same revision. Digests are of the canonical JSON (package.canonical), so re-indenting a file is not an edit.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
import re
import shutil
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
UNREADABLE = "unreadable"  # the live digest of a file that exists but cannot be read back


class StoreError(Exception):
    """A refused operation. `code` is stable; `exit` is the CLI exit status; `details` is JSON-able."""

    def __init__(self, code, message, exit=1, details=None):
        super().__init__(message)
        self.code, self.exit, self.details = code, exit, details or {}


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def render(doc) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def control(package_dir) -> Path:
    return Path(package_dir) / CONTROL


def history_path(package_dir, revision) -> Path:
    return control(package_dir) / "history" / f"{revision:06d}.json"


def _read_json(path: Path, root):
    safe_write.check_below(root, path, "package control file")
    return P.read_json_file(path)


def file_digests(docs) -> Dict[str, str]:
    return {FILES[key]: P.digest(docs[key]) for key in FILES if key in docs}


def commit_record(docs) -> dict:
    return {"schema": "tp-product-commit", "schema_version": 1, "revision": docs["manifest"]["revision"],
            "files": file_digests(docs), "authority": P.authority_map(docs), "originals": P.originals_map(docs)}


# ---------------------------------------------------------------- control record shapes

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
    if "revision" in rec and not (_is_int(rec["revision"], 1) and rec["revision"] == (revision or rec["revision"])):
        out.append(f"revision is {revision}, the entry's number" if revision else "revision is a positive integer")
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
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError) as exc:
        return "bad", f"unreadable: {exc}"
    problems = check(value)
    return ("bad", "; ".join(problems)) if problems else ("ok", value)


def history_numbers(root) -> list:
    """The revision numbers of the history entries, sorted. Other names (a writer's temp file) are ignored."""
    try:
        names = os.listdir(control(root) / "history")
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(int(m.group(1)) for m in map(HISTORY_NAME.match, names) if m)


def load_txn(root) -> dict:
    """The journal's txn.json, shape-checked, or StoreError control.invalid."""
    status, value = _load(Path(root), Path(CONTROL) / "txn" / "txn.json", txn_problems)
    if status != "ok":
        raise StoreError("control.invalid", f"{control(root) / 'txn' / 'txn.json'} is " + (
            "missing" if status == "missing" else value) + f": it is not a commit journal tp-product wrote. Move "
            f"{control(root) / 'txn'} aside by hand, then run `tp-product status`", exit=1)
    return value


# ---------------------------------------------------------------- inspection (read-only)

def inspect(package_dir) -> dict:
    """What a reader must know before trusting the package. Never writes.

    Returns {docs, issues, commit, commit_state, commit_digest, interrupted, external, authority_changed,
    originals_changed, control}:
      issues             read errors (package.read_docs), else validation issues; plus control.invalid for a control
                         file (commit.json, txn/txn.json, the top history entry) that is unreadable or malformed
      commit             the commit record when its shape is right, else None
      commit_state       "ok", "missing" or "bad"
      commit_digest      the commit record's canonical digest (what a writer checks before replacing it), or None
      interrupted        the journal's revision when a commit was interrupted after its commit point, else None
      external           file names whose content differs from the commit record (all three when there is no record)
      authority_changed  ids whose acceptance fields differ from the commit record
      originals_changed  original records (notes, inputs, reviews) the commit record holds that are gone or changed
      control            (code, message) for control records that disagree with each other: not trusted
    """
    root = Path(package_dir)
    docs, issues = P.read_docs(root)
    out = {"docs": docs, "issues": issues, "commit": None, "commit_state": "missing", "commit_digest": None,
           "interrupted": None, "external": [], "authority_changed": [], "originals_changed": [], "control": []}
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
    commit = out["commit"]

    def mismatch(message):
        out["control"].append(("package.control-mismatch", message + "; restore .tp-product/ from version control"))

    if commit and txn and status == "ok" and txn["revision"] not in (commit["revision"], commit["revision"] + 1):
        mismatch(f"the interrupted commit is to revision {txn['revision']}, but commit.json is at {commit['revision']}")
    elif commit and out["interrupted"] is None:
        numbers = history_numbers(root)
        rev = commit["revision"]
        if not numbers or numbers[-1] != rev:
            mismatch(f"commit.json is at revision {rev}, but the highest history entry is "
                     f"{numbers[-1] if numbers else 'missing'}")
        elif numbers != list(range(numbers[0], rev + 1)):
            gaps = sorted(set(range(numbers[0], rev + 1)) - set(numbers))
            mismatch(f"history has no entry for revision(s) {', '.join(map(str, gaps))}")
        else:
            rel = Path(CONTROL) / "history" / history_path(root, rev).name
            hstatus, entry = _load(root, rel, lambda h: history_problems(h, rev))
            if hstatus == "bad":
                bad(rel.relative_to(CONTROL), entry)
            elif hstatus == "ok" and entry["files"] != commit["files"]:
                mismatch(f"history entry {rev} records other file digests than commit.json")
        manifest = docs.get("manifest")
        if (isinstance(manifest, dict) and file_digests(docs).get(FILES["manifest"]) == commit["files"][FILES["manifest"]]
                and manifest.get("revision") != rev):
            mismatch(f"commit.json is at revision {rev}, but the committed package.json says {manifest.get('revision')!r}")

    committed = commit or {}
    live = file_digests(docs)
    out["external"] = sorted(name for name in FILES.values() if live.get(name) != committed.get("files", {}).get(name))
    if all(k in docs for k in FILES):
        current, before = P.authority_map(docs), committed.get("authority", {})
        out["authority_changed"] = sorted(i for i in set(current) | set(before) if current.get(i) != before.get(i))
        current, before = P.originals_map(docs), committed.get("originals", {})
        out["originals_changed"] = sorted(i for i in before if current.get(i) != before[i])
    return out


def trust_problems(state) -> list:
    """Why readiness read from this package cannot be trusted yet, as (code, message)."""
    out = []
    if state["interrupted"] is not None:
        out.append(("package.interrupted",
                    f"a commit to revision {state['interrupted']} was interrupted; `tp-product apply DIR --recover` finishes it"))
    if state["commit_state"] == "bad":
        out.append(("package.control-invalid", ".tp-product/commit.json is malformed (see the control.invalid issue); "
                                               "restore it from version control"))
    elif state["commit"] is None:
        out.append(("package.uncommitted", "no commit record (.tp-product/commit.json): not created by `tp-product init`, "
                                            "or its control state was removed"))
    elif state["external"] and state["interrupted"] is None:
        out.append(("package.external-edit",
                    f"{', '.join(state['external'])} changed outside `tp-product apply`; adopt the edit with an "
                    "adopt-external-edit changeset, or restore the committed files"))
    return out + list(state["control"])


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
    path = root / rel
    if not os.path.lexists(path):
        return None
    try:
        return P.digest(_read_json(path, root))
    except (OSError, ValueError):
        return UNREADABLE


def _changed_under(root: Path, meta: dict, names) -> list:
    """The files among `names` (package file names, or "commit.json") that are neither what the change was computed
    from (meta old) nor what it writes (meta new): someone else wrote them. Absent counts as None on both sides."""
    out = []
    for name in names:
        if name == "commit.json":
            live, allowed = _live_digest(root, Path(CONTROL) / name), (meta["old_commit"], meta["new_commit"])
        else:
            live, allowed = _live_digest(root, name), ((meta["old"] or {}).get(name), meta["new"][name])
        if live not in allowed:
            out.append(name)
    return out


ALL = tuple(FILES.values()) + ("commit.json",)


def commit(package_dir, docs, entry, fault: Callable = _noop, base_digests=None, base_commit=None):
    """Write `docs` as the next committed state. Call with the lock held and `docs` already validated.

    `entry` is the history entry (revision filled in from docs). `base_digests` are the digests of the live files the
    change was computed from and `base_commit` the digest of the commit record it was computed against (None for init:
    the files and the record must be absent). A file that differs from them at the commit point is someone else's write:
    the commit is refused (commit.external-edit) and their bytes are kept. `fault(point)` is called at each step so a
    test can stop the commit there.
    """
    root = Path(package_dir)
    txn = control(root) / "txn"
    safe_write.check_below(root, txn, "package journal")
    if txn.exists() and not (txn / "txn.json").exists():
        shutil.rmtree(txn)  # a commit that stopped before its commit point: nothing live was touched
    elif txn.exists():
        raise StoreError("package.interrupted", f"{txn} holds an interrupted commit: run `tp-product apply DIR --recover` "
                                                "first", exit=3)
    revision = docs["manifest"]["revision"]
    record = commit_record(docs)
    entry = dict(entry, revision=revision, files=record["files"])
    meta = {"revision": revision, "old": base_digests, "new": record["files"], "old_commit": base_commit,
            "new_commit": P.digest(record)}
    blobs = [(name, docs[key]) for key, name in FILES.items()] + [
        (f"history entry {revision}", entry), ("commit.json", record), ("txn.json", meta)]
    rendered = []
    for name, value in blobs:  # what is committed can always be read back: the reader's limits, checked here
        data = render(value)
        problem = P.file_problem(data, value)
        if problem:
            raise StoreError("result.unreadable", f"{name} would be {problem}: the package could not be read back. "
                                                  "Nothing was written.", details={"file": name, "problem": problem})
        rendered.append(data)
    if os.path.lexists(history_path(root, revision)):
        raise StoreError("history.exists", f"{history_path(root, revision)} already exists: a history entry is never "
                                           "replaced. Nothing was written; check .tp-product/ against version control",
                         exit=3)
    safe_write.ensure_dir(txn, what="package journal", root=root)
    try:
        for name, data in zip(list(FILES.values()) + ["history.json", "commit.json"], rendered):
            _write(root, txn / name, data)
        fault("before-commit-point")
        changed = _changed_under(root, meta, ALL) + _history_taken(root, revision, rendered[3])
        if changed:
            raise StoreError("commit.external-edit",
                             f"{', '.join(changed)} changed while this commit was being prepared (someone edited it "
                             "outside tp-product). Nothing was written and their content is kept: read `tp-product "
                             "status`, then adopt or restore the edit and retry", exit=3, details={"files": changed})
        _write(root, txn / "txn.json", rendered[-1])
    except BaseException:
        shutil.rmtree(txn, ignore_errors=True)  # nothing live was touched; the package keeps its committed state
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


def _write_new(root, target: Path, data):
    """Create `target` with `data`, never replacing a file that is there: a temp file linked into place."""
    temp = safe_write.write_temp(target, data, root=root, fsync=True)
    try:
        os.link(temp, target)
    finally:
        os.unlink(temp)


def _conflict(root, meta, name):
    txn = control(root) / "txn"
    what = ".tp-product/commit.json" if name == "commit.json" else name
    return StoreError("txn.conflict", f"{what} was changed by someone else during the commit to revision "
                                      f"{meta['revision']}; it was not overwritten. Compare it with {txn / name} and "
                                      "restore one of them, then run `tp-product apply DIR --recover`", exit=3,
                      details={"file": name, "revision": meta["revision"]})


def _roll_forward(root: Path, fault: Callable = _noop):
    """Replace the live files with the journal's, each only while it is still the old or the new content."""
    txn = control(root) / "txn"
    meta = load_txn(root)
    for name in FILES.values():  # the journal must hold what txn.json says it holds
        if _live_digest(txn, name) != meta["new"][name]:
            raise StoreError("control.invalid", f"{txn / name} does not match {txn / 'txn.json'}", exit=1)
    if _live_digest(txn, "commit.json") != meta["new_commit"]:
        raise StoreError("control.invalid", f"{txn / 'commit.json'} does not match {txn / 'txn.json'}", exit=1)
    try:
        hist = _read_json(txn / "history.json", txn)
    except (OSError, ValueError) as exc:
        raise StoreError("control.invalid", f"{txn / 'history.json'} is unreadable: {exc}", exit=1) from None
    if history_problems(hist, meta["revision"]) or hist["files"] != meta["new"]:
        raise StoreError("control.invalid", f"{txn / 'history.json'} does not match {txn / 'txn.json'}", exit=1)
    data = (txn / "history.json").read_bytes()
    target = history_path(root, meta["revision"])
    taken = _history_taken(root, meta["revision"], data)  # before any live file is replaced
    if taken:
        raise _conflict(root, meta, taken[0])
    for name in FILES.values():
        if _changed_under(root, meta, [name]):  # checked again just before its replacement
            raise _conflict(root, meta, name)
        _write(root, root / name, (txn / name).read_bytes())
        fault("after-replace:" + name)
    safe_write.ensure_dir(target.parent, what="package history", root=root)
    if not os.path.lexists(target):  # present with these bytes: written by this commit before an interruption
        try:
            _write_new(root, target, data)
        except FileExistsError:
            pass
    if _history_taken(root, meta["revision"], data):  # never replaced, whoever wrote it
        raise _conflict(root, meta, f"{CONTROL}/history/{target.name}")
    if _changed_under(root, meta, ["commit.json"]):
        raise _conflict(root, meta, "commit.json")
    _write(root, control(root) / "commit.json", (txn / "commit.json").read_bytes())
    fault("after-commit-record")
    shutil.rmtree(txn)


def recover(package_dir) -> Optional[dict]:
    """With the lock held: finish a commit interrupted after its commit point, or discard one interrupted before it.
    Returns {"action": "rolled-forward"|"discarded", "revision": N} or None when there was nothing to recover.
    Refuses (StoreError txn.conflict) when a live file is neither the old nor the new committed content: someone edited
    it during the interruption, and overwriting it would lose that edit. A malformed journal is control.invalid."""
    root = Path(package_dir)
    txn = control(root) / "txn"
    safe_write.check_below(root, txn, "package journal")
    if not txn.exists():
        return None
    if not (txn / "txn.json").exists():
        shutil.rmtree(txn)
        return {"action": "discarded", "revision": None}
    meta = load_txn(root)
    changed = _changed_under(root, meta, ALL)  # all of them, before any is replaced
    if changed:
        raise _conflict(root, meta, changed[0])
    _roll_forward(root)
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
    def present():
        names = [n for n in FILES.values() if os.path.lexists(root / n)]
        return names + [f"{CONTROL}/commit.json"] * os.path.lexists(control(root) / "commit.json")
    if present() and not (control(root) / "txn" / "txn.json").exists():
        raise StoreError("init.exists", f"{root} already holds {', '.join(present())}; refusing to overwrite a package")
    today = now()[:10]
    docs = scaffold(package_id, title, owner, objective, today)
    issues = P.validate_docs(docs)
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
        _write(root, control(root) / ".gitignore", IGNORE)
        commit(root, docs, history_entry(actor, f"init {package_id}", [], None), fault=fault)
    return docs


IGNORE = b"# tp-product: the writer lock and an in-progress commit journal are not package content\nlock\ntxn/\n"
