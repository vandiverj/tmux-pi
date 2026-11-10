"""Product design package persistence: lock, journaled atomic commit, commit record, external-edit detection.

Control state lives in `<package>/.tp-product/` (docs/product-design.md, "Persistence"):

  commit.json        the commit record: revision, the canonical digest of each package file, the authority map
                     (package.authority_map). Tracked with the package.
  history/NNNNNN.json one entry per committed revision: actor, evidence, summary, operations, impact. Tracked.
  lock               flock target for every writer. Not content.
  txn/               the journal of a commit in progress. Not content; exists only during (or after an interrupted) commit.

A commit is: take the lock; write every new file into txn/; write txn/txn.json last (the commit point); replace the live
files, the history entry and commit.json; remove txn/. A crash before txn.json leaves the live package untouched (the
partial txn/ is discarded); a crash after it is rolled forward by the next writer (`recover`). Readers never write: they
report an interrupted commit and an uncommitted external edit, and readiness is not trusted until it is resolved.

Digests are of the canonical JSON (package.canonical), so re-indenting a file is not an edit; any change of content is.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
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


def _read_json(path: Path, root):
    safe_write.check_below(root, path, "package control file")
    return json.loads(path.read_text(encoding="utf-8"))


def file_digests(docs) -> Dict[str, str]:
    return {FILES[key]: P.digest(docs[key]) for key in FILES if key in docs}


def commit_record(docs) -> dict:
    return {"schema": "tp-product-commit", "schema_version": 1, "revision": docs["manifest"]["revision"],
            "files": file_digests(docs), "authority": P.authority_map(docs)}


# ---------------------------------------------------------------- inspection (read-only)

def inspect(package_dir) -> dict:
    """What a reader must know before trusting the package. Never writes.

    Returns {docs, issues, commit, interrupted, external, authority_changed}:
      issues             read errors (package.read_docs), else validation issues
      commit             the commit record, or None when there is none
      interrupted        the journal's revision when a commit was interrupted after its commit point, else None
      external           file names whose content differs from the commit record (all three when there is no record)
      authority_changed  ids whose acceptance fields differ from the commit record
    """
    root = Path(package_dir)
    docs, issues = P.read_docs(root)
    out = {"docs": docs, "issues": issues, "commit": None, "interrupted": None, "external": [], "authority_changed": []}
    if not issues:
        out["issues"] = P.validate_docs(docs)
    try:
        out["commit"] = _read_json(control(root) / "commit.json", root)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        out["issues"] = out["issues"] + [S.Issue("control.unreadable", (CONTROL, "commit.json"), str(exc))]
    try:
        out["interrupted"] = _read_json(control(root) / "txn" / "txn.json", root)["revision"]
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError) as exc:
        out["issues"] = out["issues"] + [S.Issue("control.unreadable", (CONTROL, "txn", "txn.json"), str(exc))]
    committed = out["commit"] or {}
    live = file_digests(docs)
    out["external"] = sorted(name for name in FILES.values() if live.get(name) != committed.get("files", {}).get(name))
    if all(k in docs for k in FILES):
        current, before = P.authority_map(docs), committed.get("authority", {})
        out["authority_changed"] = sorted(i for i in set(current) | set(before) if current.get(i) != before.get(i))
    return out


def trust_problems(state) -> list:
    """Why readiness read from this package cannot be trusted yet, as (code, message)."""
    out = []
    if state["interrupted"] is not None:
        out.append(("package.interrupted",
                    f"a commit to revision {state['interrupted']} was interrupted; `tp-product apply DIR --recover` finishes it"))
    if state["commit"] is None:
        out.append(("package.uncommitted", "no commit record (.tp-product/commit.json): not created by `tp-product init`, "
                                            "or its control state was removed"))
    elif state["external"] and state["interrupted"] is None:
        out.append(("package.external-edit",
                    f"{', '.join(state['external'])} changed outside `tp-product apply`; adopt the edit with an "
                    "adopt-external-edit changeset, or restore the committed files"))
    return out


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


def commit(package_dir, docs, entry, fault: Callable = _noop, base_digests=None):
    """Write `docs` as the next committed state. Call with the lock held and `docs` already validated.

    `entry` is the history entry (revision filled in from docs). `base_digests` are the committed digests the live files
    are expected to have (None for init). `fault(point)` is called at each step so a test can stop the commit there.
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
    safe_write.ensure_dir(txn, what="package journal", root=root)
    try:
        for key, name in FILES.items():
            _write(root, txn / name, render(docs[key]))
        _write(root, txn / "history.json", render(entry))
        _write(root, txn / "commit.json", render(record))
        fault("before-commit-point")
        _write(root, txn / "txn.json", render({"revision": revision, "old": base_digests, "new": record["files"]}))
    except BaseException:
        shutil.rmtree(txn, ignore_errors=True)  # nothing live was touched; the package keeps its committed state
        raise
    fault("after-commit-point")
    _roll_forward(root, fault)


def _roll_forward(root: Path, fault: Callable = _noop):
    txn = control(root) / "txn"
    meta = _read_json(txn / "txn.json", root)
    for name in FILES.values():
        _write(root, root / name, (txn / name).read_bytes())
        fault("after-replace:" + name)
    history = control(root) / "history"
    safe_write.ensure_dir(history, what="package history", root=root)
    _write(root, history / f"{meta['revision']:06d}.json", (txn / "history.json").read_bytes())
    _write(root, control(root) / "commit.json", (txn / "commit.json").read_bytes())
    fault("after-commit-record")
    shutil.rmtree(txn)


def recover(package_dir) -> Optional[dict]:
    """With the lock held: finish a commit interrupted after its commit point, or discard one interrupted before it.
    Returns {"action": "rolled-forward"|"discarded", "revision": N} or None when there was nothing to recover.
    Refuses (StoreError txn.conflict) when a live file is neither the old nor the new committed content: someone edited
    it during the interruption, and overwriting it would lose that edit."""
    root = Path(package_dir)
    txn = control(root) / "txn"
    safe_write.check_below(root, txn, "package journal")
    if not txn.exists():
        return None
    if not (txn / "txn.json").exists():
        shutil.rmtree(txn)
        return {"action": "discarded", "revision": None}
    meta = _read_json(txn / "txn.json", root)
    docs, _ = P.read_docs(root)
    live = file_digests(docs)
    for name, new in meta["new"].items():
        old = (meta.get("old") or {}).get(name)
        if name in live and live[name] not in (old, new):
            raise StoreError("txn.conflict", f"{name} was edited during an interrupted commit to revision {meta['revision']}; "
                                             f"compare it with {txn / name} and restore one of them", exit=3)
        if name not in live and old is not None:
            raise StoreError("txn.conflict", f"{name} is missing or unreadable during an interrupted commit", exit=3)
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


def init(package_dir, package_id, title, owner="Not set", objective="Not framed yet.", actor="tp-product init",
         fault: Callable = _noop):
    """Create a package at revision 1. Refuses when a package file or a commit record is already there."""
    root = Path(package_dir)
    if root.is_symlink():
        raise StoreError("init.unsafe", f"{root} is a symbolic link")
    def present():
        names = [n for n in FILES.values() if (root / n).exists() or (root / n).is_symlink()]
        return names + [f"{CONTROL}/commit.json"] * (control(root) / "commit.json").exists()
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
        commit(root, docs, {"actor": actor, "summary": f"init {package_id}", "evidence": [], "operations": [],
                            "base_revision": None, "committed": now()}, fault=fault)
    return docs


IGNORE = b"# tp-product: the writer lock and an in-progress commit journal are not package content\nlock\ntxn/\n"
