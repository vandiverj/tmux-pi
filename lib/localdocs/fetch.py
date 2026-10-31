"""The one network step: download a package's docs for its pinned version. Run by hand with `tp-docs fetch`; nothing else
imports this module.

Fetches the npm registry tarball for exactly lib@version (checked against the lockfile's integrity when it has one) and keeps
only its markdown files, written to <project>/.tmux-pi/docs-src/<lib>@<version>/ for `tp-docs build`. Members are taken from
the archive by name only: no links, no devices, no absolute or ../ paths, no .env*, no node_modules.
"""
import base64
import hashlib
import io
import json
import os
import posixpath
import shutil
import tarfile
import urllib.request
import uuid
from pathlib import Path

import safe_write

from . import guard, index, lock

REGISTRY = "https://registry.npmjs.org"
MAX_TARBALL = 60_000_000
MAX_MEMBER = 1_000_000


class FetchError(Exception):
    pass


def tarball_url(lib, version, registry=REGISTRY):
    base = lib.rsplit("/", 1)[-1]
    return f"{registry.rstrip('/')}/{lib}/-/{base}-{version}.tgz"


def download(url, limit=MAX_TARBALL):
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 - a fixed https registry URL the user chose to fetch
        data = response.read(limit + 1)
    if len(data) > limit:
        raise FetchError(f"{url} is larger than {limit} bytes")
    return data


def check_integrity(data, integrity):
    if not integrity:
        return "not checked (the lockfile has no integrity for it)"
    algo, _, expected = integrity.partition("-")
    if algo not in ("sha512", "sha256", "sha1"):
        return f"not checked (unknown integrity algorithm {algo!r})"
    actual = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if actual != expected:
        raise FetchError(f"integrity mismatch: the tarball does not match the lockfile's {algo}")
    return f"verified {algo}"


def docs_from_tarball(data):
    """[(relative path, bytes)] of the markdown files in an npm tarball (its top directory stripped)."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isreg() or member.size > MAX_MEMBER:
                continue
            parts = posixpath.normpath(member.name).split("/")[1:]
            if not parts or ".." in parts or member.name.startswith("/") or "node_modules" in parts:
                continue
            if parts[-1].startswith(".env") or any(p.startswith(".env") for p in parts):
                continue
            if not parts[-1].lower().endswith(".md"):
                continue
            out.append(("/".join(parts), archive.extractfile(member).read()))
            if len(out) >= index.MAX_DOC_FILES:
                break
    return out


def fetch(project, lib, *, version=None, registry=REGISTRY, opener=download):
    """Fetch the docs of lib at the lockfile's pin into docs-src. Returns {version, files, integrity, url, retrieved}."""
    installed = None
    from . import signature
    node_dir = signature.node_package_dir(project, lib)
    if node_dir:
        installed = signature.installed_version(node_dir)
    pinned = lock.pin(project, lib, installed)
    if not pinned:
        raise FetchError(pinned.reason)
    if version and version != pinned.version:
        raise FetchError(f"{lib}@{version} was asked for but the lockfile pins {pinned.version}")
    url = tarball_url(lib, pinned.version, registry)
    data = opener(url)
    retrieved = guard.now()
    integrity = check_integrity(data, pinned.integrity)
    docs = docs_from_tarball(data)
    if not docs:
        raise FetchError(f"{lib}@{pinned.version} ships no markdown files")
    root = Path(project) / index.SOURCE_ROOT
    safe_write.ensure_dir(root, what="docs source directory", root=project)
    final = root / index.key(lib, pinned.version)
    stage = root / f".fetch-{uuid.uuid4().hex}"
    try:
        for relative, content in docs:
            target = stage / relative
            safe_write.ensure_dir(target.parent, what="docs source directory")
            safe_write.write_atomic(target, content.decode("utf-8", "replace"), root=stage)
        safe_write.write_atomic(stage / ".fetched.json", json.dumps({"url": url, "version": pinned.version, "integrity": integrity, "retrieved": retrieved}), root=stage)
        if final.is_symlink():
            raise FetchError(f"{final} is a symlink")
        shutil.rmtree(final, ignore_errors=True)
        stage.rename(final)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"version": pinned.version, "files": len(docs), "integrity": integrity, "url": url, "retrieved": retrieved}
