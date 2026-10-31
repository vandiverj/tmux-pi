"""Lockfile pins for the local docs tools: which version of a package does this project's lockfile say it uses.

Reads package-lock.json, pnpm-lock.yaml and deno.lock (text only, no network, no YAML library) and answers one
question: `pin(project, name)`. The answer is a version the lockfile names, or nothing. There is no fallback to
"latest" and no guess from node_modules: a package the lockfile does not name has no pinned version.

`lock_hash(project)` is the sha256 of every lockfile present (name and bytes); an index built from the lockfiles
records it, and `tp-docs refresh` rebuilds when it changes.
"""
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

LOCKFILES = ("package-lock.json", "pnpm-lock.yaml", "deno.lock")
MAX_LOCK = 64_000_000
SEMVERISH = re.compile(r"^\d[0-9A-Za-z.+-]*$")


@dataclass
class Pin:
    version: str | None = None
    lockfile: str | None = None
    integrity: str | None = None
    # Distinct versions the lockfiles name for this package when no single one is the project's own.
    candidates: list = field(default_factory=list)
    reason: str = ""

    def __bool__(self):
        return self.version is not None


def find_project(start):
    """The directory a docs call is about: the nearest ancestor of `start` with a lockfile, else with package.json,
    deno.json or node_modules, else `start`."""
    start = Path(start).resolve()
    chain = [start, *start.parents]
    for markers in (LOCKFILES, ("package.json", "deno.json", "deno.jsonc", "node_modules")):
        for directory in chain:
            if any((directory / name).exists() for name in markers):
                return directory
    return start


def lockfiles(project):
    return [Path(project) / name for name in LOCKFILES if (Path(project) / name).is_file()]


def _read(path):
    path = Path(path)
    if path.stat().st_size > MAX_LOCK:
        raise ValueError(f"{path.name} is larger than {MAX_LOCK} bytes")
    return path.read_bytes()


def lock_hash(project):
    """sha256 over the lockfiles present, or None when the project has none."""
    files = lockfiles(project)
    if not files:
        return None
    digest = hashlib.sha256()
    for path in files:
        data = _read(path)
        digest.update(path.name.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return digest.hexdigest()


def _clean(version):
    """1.2.3(react@18) and 1.2.3_react@18 are 1.2.3."""
    version = str(version).strip().strip("'\"")
    version = version.split("(", 1)[0].split("_", 1)[0]
    return version if SEMVERISH.match(version) else None


# ---- package-lock.json -------------------------------------------------------------------------------------------

def _npm_lock(data, name):
    direct, other = {}, {}
    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            if not isinstance(entry, dict) or entry.get("link") or not isinstance(entry.get("version"), str):
                continue
            tail = key.rsplit("node_modules/", 1)[-1] if "node_modules/" in key else None
            if tail != name:
                continue
            version = _clean(entry["version"])
            if version:
                (direct if key == f"node_modules/{name}" else other)[version] = entry.get("integrity")
    elif isinstance(data.get("dependencies"), dict):  # lockfileVersion 1
        top = data["dependencies"].get(name)
        if isinstance(top, dict) and _clean(top.get("version", "")):
            direct[_clean(top["version"])] = top.get("integrity")

        def walk(deps):
            for dep_name, entry in (deps or {}).items():
                if isinstance(entry, dict):
                    if dep_name == name and _clean(entry.get("version", "")):
                        other.setdefault(_clean(entry["version"]), entry.get("integrity"))
                    walk(entry.get("dependencies"))
        walk(data["dependencies"])
    return direct, other


# ---- pnpm-lock.yaml ----------------------------------------------------------------------------------------------

def _yaml_key(text):
    text = text.strip()
    if text[:1] in "'\"":
        end = text.find(text[0], 1)
        if end < 0:
            return None, ""
        rest = text[end + 1:].lstrip()
        return text[1:end], rest[1:].strip() if rest.startswith(":") else ""
    head, sep, rest = text.partition(":")
    # A key may hold ':' only inside quotes; "a: b" or a trailing "a:" ends the key.
    while sep and rest and not rest.startswith(" "):
        more, sep, rest = rest.partition(":")
        head += ":" + more
    return (head.strip(), rest.strip()) if sep else (None, "")


def parse_yaml_mapping(text):
    """The block-mapping subset pnpm-lock.yaml uses, as nested dicts with string leaves. Lists and flow
    collections are kept as their text; anything this does not understand is skipped, never guessed."""
    root = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("- "):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, rest = _yaml_key(stripped)
        if key is None:
            continue
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]
        if rest and not rest.startswith("#"):
            parent[key] = rest
        else:
            child = {}
            parent[key] = child
            stack.append((indent, child))
    return root


_PNPM_KEY = re.compile(r"^/?(?P<name>(?:@[^/@]+/)?[^/@()]+)(?:@|/)(?P<version>[^()@/]+)")


def _pnpm_name_version(key):
    match = _PNPM_KEY.match(key)
    return (match["name"], _clean(match["version"])) if match else (None, None)


def _pnpm_lock(text, name):
    doc = parse_yaml_mapping(text)
    direct, other = {}, {}
    scopes = []
    importers = doc.get("importers")
    if isinstance(importers, dict) and isinstance(importers.get("."), dict):
        scopes.append(importers["."])
    scopes.append(doc)  # lockfileVersion 5 keeps dependencies at the top level
    for scope in scopes:
        for section in ("dependencies", "devDependencies", "optionalDependencies"):
            entries = scope.get(section)
            if not isinstance(entries, dict) or name not in entries:
                continue
            entry = entries[name]
            version = _clean(entry.get("version", "")) if isinstance(entry, dict) else _clean(entry)
            if version:
                direct[version] = None
    packages = doc.get("packages")
    if isinstance(packages, dict):
        for key, entry in packages.items():
            pkg, version = _pnpm_name_version(key)
            if pkg == name and version:
                integrity = None
                resolution = entry.get("resolution") if isinstance(entry, dict) else None
                found = re.search(r"integrity:\s*([A-Za-z0-9+/=-]+)", resolution) if isinstance(resolution, str) else None
                if found:
                    integrity = found.group(1)
                other[version] = integrity
    return direct, other


# ---- deno.lock ---------------------------------------------------------------------------------------------------

def _deno_sections(data):
    """(specifiers, {scheme: packages}, remote) from a deno.lock of version 4 (top level) or 3 (under "packages")."""
    inner = data.get("packages") if isinstance(data.get("packages"), dict) else {}
    pick = lambda key: data.get(key) if isinstance(data.get(key), dict) else inner.get(key) if isinstance(inner.get(key), dict) else {}
    return pick("specifiers"), {"npm": pick("npm"), "jsr": pick("jsr")}, pick("remote")


def _deno_lock(data, name, scheme="npm"):
    """Pins for `name` from deno.lock under `scheme` ("npm" or "jsr"). Version 3 values read "npm:name@1.2.3"."""
    direct, other = {}, {}
    specifiers, packages, _ = _deno_sections(data)
    wanted = f"{scheme}:{name}@"
    for spec, resolved in specifiers.items():
        if isinstance(spec, str) and spec.startswith(wanted) and isinstance(resolved, str):
            version = _clean(resolved[len(wanted):] if resolved.startswith(wanted) else resolved)
            if version:
                direct[version] = None
    for key, entry in packages[scheme].items():
        if not isinstance(key, str) or not key.startswith(name + "@"):
            continue
        version = _clean(key[len(name) + 1:])
        if version:
            other[version] = entry.get("integrity") if isinstance(entry, dict) else None
    return direct, other


def remote_hash(project, url):
    """The sha256 deno.lock records for the remote module `url`, or None when the lockfile does not list it."""
    path = Path(project) / "deno.lock"
    if not path.is_file():
        return None
    try:
        value = _deno_sections(json.loads(_read(path)))[2].get(url)
    except (ValueError, OSError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def _per_file(project, name, scheme="npm"):
    for path in lockfiles(project):
        if scheme != "npm" and path.name != "deno.lock":  # only deno.lock knows jsr packages
            continue
        try:
            if path.name == "package-lock.json":
                yield path.name, _npm_lock(json.loads(_read(path)), name)
            elif path.name == "pnpm-lock.yaml":
                yield path.name, _pnpm_lock(_read(path).decode("utf-8", "replace"), name)
            else:
                yield path.name, _deno_lock(json.loads(_read(path)), name, scheme)
        except (ValueError, OSError):
            yield path.name, None


def pin(project, name, installed=None, scheme="npm"):
    """The version the project's lockfiles pin for `name`.

    The project's own entry wins (the root node_modules entry, an importer dependency, a deno specifier). With none,
    a single version anywhere in the lockfiles is the pin. With several, `installed` (the version found on disk)
    picks one if it is among them; otherwise the pin is ambiguous and says so. Nothing found is a Pin with a reason."""
    files = list(lockfiles(project))
    if not files:
        return Pin(reason=f"no lockfile ({', '.join(LOCKFILES)}) in {project}")
    direct, other, broken = {}, {}, []
    for lockname, found in _per_file(project, name, scheme):
        if found is None:
            broken.append(lockname)
            continue
        for version, integrity in found[0].items():
            direct.setdefault(version, (lockname, integrity))
        for version, integrity in found[1].items():
            other.setdefault(version, (lockname, integrity))
    for version, (lockname, integrity) in direct.items():  # an importer or specifier entry carries no hash; the package entry does
        if integrity is None and version in other:
            direct[version] = (lockname, other[version][1])
    for pool in (direct, other):
        if len(pool) == 1:
            version, (lockname, integrity) = next(iter(pool.items()))
            return Pin(version, lockname, integrity)
        if len(pool) > 1:
            if installed in pool:
                return Pin(installed, pool[installed][0], pool[installed][1], sorted(pool))
            return Pin(candidates=sorted(pool), reason=f"{name} is pinned at more than one version: {', '.join(sorted(pool))}")
    where = "deno.lock" if scheme != "npm" else "the lockfile"
    why = f"no pinned version: {name} has no {scheme + ' ' if scheme != 'npm' else ''}entry in {where}"
    if broken:
        why += f" (could not read {', '.join(broken)})"
    return Pin(reason=why)
