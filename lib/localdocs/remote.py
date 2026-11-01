"""Deno's remote module cache as a source of declarations: `jsr:` packages and `https:` modules, offline.

Deno keeps a fetched module at `<DENO_DIR>/remote/https/<host>/<sha256 of the URL path>`. Deno 2.x appends its metadata to that
file as a last line, `\n// denoCacheMetadata={"headers":...,"url":...,"time":...}` (no newline after it); older Deno wrote the
metadata to a `.metadata.json` beside it. Both are read: the trailer is cut off before the content is parsed or hashed (deno.lock's
hashes are of the content without it), and either record, when present, must name the URL asked for. Nothing here fetches: a module
that is not in the cache is a miss that says to cache it.

What is read is bounded the way package files are (guard.py): the cache file is resolved through links and must stay under
the host's cache directory, a URL path with a `.env*` component is refused, and the extension is judged on the URL path (the
cache file has none). Unlike node_modules, the extensions here include TypeScript source (`.ts`, `.mts`, `.cts`), because
jsr publishes source and no declarations. That source is only ever read to extract exported declarations: signature.py cuts
function and method bodies and initializers before anything is returned.

`https:` modules are pinned by deno.lock's "remote" section (URL -> sha256 of the content); a module the lock does not list is
unpinned, and cached content that does not hash to the lock's value is not served. `jsr:` packages are pinned by deno.lock's
specifiers; their files live under `https://jsr.io/<@scope/name>/<version>/`. When deno.lock records an `integrity` for the
jsr package (the sha256 of the version's `<version>_meta.json`), that meta file must hash to it and every file read must match
the checksum its `manifest` lists; a mismatch or an unlisted file is not served, and the answer is marked `exact`.

Known limits:
- A jsr entry whose deno.lock record has no `integrity` (older or hand-edited locks) cannot be checked. Its cached files are
  still read, the version still comes from the lock, and the answer says `exact=no` so a caller knows the content was not
  vouched for by the lockfile.
- The cache layout and the meta/manifest/integrity formats were checked offline against a real Deno 2.9.7 cache (a jsr meta file
  hashes to deno.lock's integrity once the trailer is cut) and against fixtures in both layouts. Other Deno versions are untested.
"""
import hashlib
import json
import posixpath
import re
from pathlib import Path
from urllib.parse import urlsplit

from . import guard, lock

SOURCE_SUFFIXES = (".ts", ".mts", ".cts")
SUFFIXES = guard.ALLOWED_SUFFIXES + SOURCE_SUFFIXES
JSR_HOST = "jsr.io"
JSR_RE = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9~._-]*/[A-Za-z0-9][A-Za-z0-9~._-]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.@~+%=,-]+$")
VERSION_IN_URL = re.compile(r"@(\d[0-9A-Za-z.+-]*)(?=/|$)")


TRAILER = b"\n// denoCacheMetadata="


def split_trailer(raw):
    """(content, record) of a cache file's bytes. Deno 2.x ends the file with TRAILER and a JSON object; that last line is cut off
    and its object returned. Without a well-formed trailer the bytes are the content and the record is None."""
    at = raw.rfind(TRAILER)
    if at < 0:
        return raw, None
    try:
        record = json.loads(raw[at + len(TRAILER):].decode("utf-8"))
    except ValueError:
        return raw, None
    return (raw[:at], record) if isinstance(record, dict) else (raw, None)


class Unavailable(Exception):
    """The module is not in Deno's cache (or not at the pinned content). The message is safe to show the model."""


def is_source(name):
    low = name.lower()
    return low.endswith(SOURCE_SUFFIXES) and not low.endswith((".d.ts", ".d.mts", ".d.cts"))


def parse_https(spec):
    """(host, path) of an `https://host/path` specifier, or Rejected. No credentials, port, query or fragment."""
    parts = urlsplit(spec)
    if parts.scheme != "https" or not parts.netloc:
        raise guard.Rejected("refused: only https: URLs are read (http: is not)")
    if parts.username is not None or parts.password is not None or parts.port is not None or ":" in parts.netloc or "@" in parts.netloc:
        raise guard.Rejected("refused: an https specifier with credentials or a port is not read")
    if parts.query or parts.fragment or "?" in spec or "#" in spec:
        raise guard.Rejected("refused: an https specifier with a query or fragment is not read")
    if not HOST_RE.match(parts.netloc):
        raise guard.Rejected("refused: not a host name")
    path = parts.path
    if not path.startswith("/") or path == "/":
        raise guard.Rejected("refused: an https specifier must name a file")
    check_path(path)
    return parts.netloc.lower(), path


def check_path(path):
    """Refuse a URL path that is not a plain file path to an allowed kind of file."""
    segments = path.split("/")[1:]
    if any(not s or s in (".", "..") or not SEGMENT_RE.match(s) for s in segments):
        raise guard.Rejected(f"refused: {path!r} is not a plain file path")
    guard.reject_names(segments)
    if not segments[-1].lower().endswith(SUFFIXES):
        raise guard.Rejected(f"refused: {segments[-1]!r} is not a declaration, markdown or TypeScript file ({', '.join(SUFFIXES)})")


class RemoteSource:
    """Files of one module tree in Deno's remote cache. Keys are URL paths. `prefix` is what a followed import may not leave."""

    def __init__(self, deno, host, prefix):
        self.host, self.prefix = host, prefix
        self.directory = Path(deno) / "remote" / "https" / host

    # -- cache access
    def _file(self, path, *, checked=True):
        """The resolved cache file for the URL path (None when not cached)."""
        if checked:
            check_path(path)
        if not self.directory.is_dir():
            return None
        name = hashlib.sha256(path.encode()).hexdigest()
        target = guard.resolve_inside(self.directory, name, suffix=False)
        if not target.is_file():
            return None
        meta = self.directory / (name + ".metadata.json")
        if meta.exists() or meta.is_symlink():
            try:
                record = json.loads(guard.read_text(guard.resolve_inside(self.directory, meta.name, suffix=False), limit=1_000_000) or "null")
            except ValueError:
                record = None
            url = record.get("url") if isinstance(record, dict) else None
            if url is not None and url != f"https://{self.host}{path}":
                raise guard.Rejected(f"refused: the cache entry for {path} is recorded for another URL")
        return target

    def _content(self, path, target, *, limit=guard.MAX_FILE):
        """(text, bytes) of a cache file without Deno's trailer, or None when the file is missing. The file is opened once, through
        guard.read_bytes (regular file, size, not binary, no link at the last component), and the trailer is cut from those same
        bytes: the path is never reopened, so a file swapped for a link between a check and a read cannot lead out of the cache. An
        embedded record that names another URL is refused."""
        raw = guard.read_bytes(target, limit=limit)
        if raw is None:
            return None
        body, record = split_trailer(raw)
        url = record.get("url") if record else None
        if url is not None and url != f"https://{self.host}{path}":
            raise guard.Rejected(f"refused: the cache entry for {path} is recorded for another URL")
        return body.decode("utf-8", "replace"), body

    def read_json(self, path):
        """A registry metadata file (jsr's `<version>_meta.json`) from the cache, as an object or None."""
        return self._json(self._raw(path))

    def _raw(self, path):
        """The content bytes of a cached registry metadata file, or None. The file is resolved through links like any cache file."""
        if not self.directory.is_dir():
            return None
        target = guard.resolve_inside(self.directory, hashlib.sha256(path.encode()).hexdigest(), suffix=False)
        found = self._content(path, target, limit=1_000_000)
        return None if found is None else found[1]

    @staticmethod
    def _json(raw):
        try:
            value = json.loads(raw.decode("utf-8", "replace") or "null") if raw is not None else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    # -- the source interface used by signature.search_package
    def existing(self, key, refusals):
        try:
            return key if self._file(self._path(key)) else None
        except guard.Rejected as exc:
            note = f"{key}: {exc}"
            if note not in refusals:
                refusals.append(note)
            return None

    def read(self, key):
        path = self._path(key)
        target = self._file(path)
        found = self._content(path, target) if target else None
        if found is None:
            return None
        self.verify(path, found[1])
        return found[0]

    def verify(self, path, content):
        """Hook: check the cached content bytes (Deno's trailer already cut off) against the lockfile."""

    def is_source(self, key):
        return is_source(key)

    def module(self, base, specifier, refusals):
        """The key of the module a relative import/re-export reaches, or None. Never leaves `prefix`."""
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(self._path(base)), specifier))
        stem = re.sub(r"\.(?:mjs|cjs|js|mts|cts|ts)$", "", joined)
        options = [joined] if joined.lower().endswith(SUFFIXES) else []
        options += [f"{stem}.d.ts", f"{stem}.ts", f"{stem}.mts", f"{joined}.d.ts", f"{joined}.ts", f"{joined}/index.d.ts", f"{joined}/index.ts",
                    f"{joined}/mod.ts"]
        for option in options:
            if not option.startswith(self.prefix):
                continue
            found = self.existing(self._key(option), refusals)
            if found:
                return found
        return None

    def _path(self, key):
        return key

    def _key(self, path):
        return path


class JsrSource(RemoteSource):
    """`jsr:@scope/name` at a pinned version: files under https://jsr.io/@scope/name/<version>/. Keys are relative to that."""

    def __init__(self, deno, name, version, integrity=None):
        super().__init__(deno, JSR_HOST, f"/{name}/{version}/")
        self.name, self.version = name, version
        self.integrity = integrity.lower() if isinstance(integrity, str) and integrity else None
        self._manifest = None

    @property
    def meta_path(self):
        return f"/{self.name}/{self.version}_meta.json"

    def vouch(self):
        """Check the cached meta file against deno.lock's integrity (when it records one); the manifest it lists is then what each
        file is checked against. Raises Unavailable when the cache does not match the lock. True when verified."""
        if self.integrity is None:
            return False
        raw = self._raw(self.meta_path)
        if raw is None:
            raise Unavailable(f"jsr:{self.name}@{self.version}: its meta file is not in Deno's cache, so the files cannot be checked "
                              f"against deno.lock; run `deno cache jsr:{self.name}@{self.version}` (this tool never fetches)")
        if hashlib.sha256(raw).hexdigest() != self.integrity:
            raise Unavailable(f"the cached meta file of jsr:{self.name}@{self.version} does not match the integrity deno.lock records; "
                              "run `deno cache --reload` (nothing is read from content the lockfile does not vouch for)")
        manifest = (self._json(raw) or {}).get("manifest")
        self._manifest = manifest if isinstance(manifest, dict) else {}
        return True

    def verify(self, path, content):
        if self.integrity is None:
            return
        if self._manifest is None:
            self.vouch()
        key = "/" + path[len(self.prefix):]
        entry = self._manifest.get(key)
        checksum = entry.get("checksum") if isinstance(entry, dict) else None
        if not isinstance(checksum, str) or not checksum.startswith("sha256-"):
            raise Unavailable(f"jsr:{self.name}@{self.version}: {key} is not listed in the package manifest that deno.lock vouches for")
        if hashlib.sha256(content).hexdigest() != checksum[len("sha256-"):].lower():
            raise Unavailable(f"the cached {key} of jsr:{self.name}@{self.version} does not match the checksum in its manifest "
                              "(deno.lock vouches for that manifest); run `deno cache --reload`")

    def _path(self, key):
        return self.prefix + key

    def _key(self, path):
        return path[len(self.prefix):]

    def cached(self):
        return self.directory.is_dir()

    def entry_candidates(self):
        meta = self.read_json(self.meta_path)
        out = []
        exports = meta.get("exports") if meta else None
        entry = exports.get(".") if isinstance(exports, dict) else exports
        if isinstance(entry, str):
            out.append(posixpath.normpath(entry))
        out.extend(f for f in ("mod.ts", "mod.d.ts", "index.ts", "index.d.ts") if f not in out)
        return out


class HttpsSource(RemoteSource):
    """One `https://host/path` module and the relative imports it follows, each of which must be pinned in deno.lock."""

    def __init__(self, deno, project, host, path):
        super().__init__(deno, host, "/")
        self.project, self.path = project, path
        self.url = f"https://{host}{path}"

    def locked(self, path):
        return lock.remote_hash(self.project, f"https://{self.host}{path}")

    def existing(self, key, refusals):
        if not self.locked(key):
            return None
        return super().existing(key, refusals)

    def verify(self, path, content):
        wanted = self.locked(path)
        if wanted and hashlib.sha256(content).hexdigest() != wanted:
            raise Unavailable(f"the cached https://{self.host}{path} does not match the hash deno.lock records; run `deno cache --reload` "
                              "(nothing is read from content the lockfile does not vouch for)")

    def entry_candidates(self):
        return [self.path]

    def version(self):
        found = VERSION_IN_URL.search(self.path)
        return found.group(1) if found else "sha256:" + (self.locked(self.path) or "")[:12]
