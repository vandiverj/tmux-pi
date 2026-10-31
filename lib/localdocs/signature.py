"""api_signature(package, symbol): the declaration of `symbol` in a pinned package's own type declarations.

Offline and read-only. The package is found in the project's node_modules, or (for `npm:` specifiers and for a
package only deno.lock pins) in Deno's npm cache. Its entry declaration file comes from package.json `types` /
`typings` / `exports["."]` / `main`; relative re-exports are followed inside the package. Every read goes through
guard.py (allowlisted extensions, links resolved, nothing outside the package directory, never .env*).

The declaration scanner is a text scanner, not a TypeScript parser: it splits a declaration file into top-level
statements (comments, strings and brackets skipped) and matches the name. It returns the statement with the
JSDoc comment just above it. `Ns.member` finds a member of a namespace, class, interface or enum.
"""
import json
import os
import re
from pathlib import Path

from . import guard, lock

NAME_RE = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9~._-]*/)?[A-Za-z0-9][A-Za-z0-9~._-]*$")
SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
MAX_FILES = 40
MAX_NEST = 8
KEYWORDS = ("export", "declare", "import", "interface", "type", "function", "class", "const", "let", "var", "enum",
            "namespace", "abstract", "module", "global")
CONTAINERS = ("interface", "class", "enum", "namespace", "module", "global")
CONTAINER_HEAD = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?(?:%s)\b" % "|".join(CONTAINERS))
DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?"
    r"(?P<kind>function|class|interface|type|enum|namespace|module|const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)")
MEMBER = re.compile(
    r"^(?:(?:public|protected|private|static|readonly|abstract|declare|async|override|export)\s+)*"
    r"(?:(?:get|set)\s+(?=[A-Za-z_$]))?(?P<name>[A-Za-z_$][\w$]*|'[^']+'|\"[^\"]+\")\s*[?!]?\s*(?:[(<:=;,]|$)")
OPEN_ENDED = ("=", "|", "&", ",", ":", "<", "(", "?", "{", "[")
OPEN_WORDS = re.compile(r"\b(?:extends|implements|keyof|typeof|in|is|as|infer|readonly|new|unique)$")
LINE_CONTINUES = re.compile(r"[|&?:=.)\]>,{]|extends\b|implements\b|\bin\b|=>")


class Miss(Exception):
    """A valid request with no answer (not found, no pin, not installed). The message is safe to show the model."""

    def __init__(self, message, version=None, no_types=False):
        super().__init__(message)
        self.version = version
        self.no_types = no_types


class Found:
    def __init__(self, text, version, package, files, provenance=None):
        self.text, self.version, self.package, self.files, self.provenance = text, version, package, files, provenance


# ---- statement scanner ---------------------------------------------------------------------------------------------

def _skip_string(text, i):
    quote, i = text[i], i + 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _newline_ends(text, start, i, members):
    """A line break ends a statement when the line is complete and the next one begins something new."""
    before = text[start:i].rstrip()
    if not before or before.endswith(OPEN_ENDED) or OPEN_WORDS.search(before):
        return False
    after = text[i + 1:i + 80].lstrip()
    if members:
        return bool(after) and not LINE_CONTINUES.match(after)
    return after.startswith(("/**", "/*", "//", "@")) or bool(re.match(r"(?:%s)\b" % "|".join(KEYWORDS), after))


def split_statements(text, members=False):
    """[(start, end, doc)] for the statements at the top level of `text`; `text[start:end]` is the statement and `doc`
    the (start, end) of the JSDoc comment right above it, or None. `members` splits a container body: a `,` or a
    complete line also ends a member."""
    out = []
    n = len(text)
    i = 0
    start = doc = pending = None
    depth = angle = 0

    def finish(end):
        nonlocal start, doc, depth, angle
        if start is not None and text[start:end].strip():
            out.append((start, end, doc))
        start = doc = None
        depth = angle = 0

    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            if start is None:
                pending = (i, end) if text.startswith("/**", i) and not text.startswith("/**/", i) else None
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if start is None:
            if c.isspace():
                i += 1
                continue
            if c in ";,":
                pending = None
                i += 1
                continue
            start, doc, pending = i, pending, None
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if c == "}" and depth <= 0 and angle <= 0 and CONTAINER_HEAD.match(text[start:i].lstrip()):
                end = i + 1
                tail = re.match(r"[ \t]*;", text[end:end + 40])
                finish(end + tail.end() if tail else end)
                i = max(i + 1, end)
                continue
        elif c == "<" and depth <= 0:
            angle += 1
        elif c == ">" and depth <= 0 and angle > 0 and text[i - 1] != "=":
            angle -= 1
        elif depth <= 0 and (c == ";" or (members and c == "," and angle <= 0)):
            finish(i + 1)
        elif c == "\n" and depth <= 0 and angle <= 0 and _newline_ends(text, start, i, members):
            finish(i)
        i += 1
    finish(n)
    return out


def _with_doc(text, doc, statement):
    return (text[doc[0]:doc[1]] + "\n" if doc else "") + statement


def _body(statement):
    open_at, close_at = statement.find("{"), statement.rfind("}")
    return statement[open_at + 1:close_at] if 0 <= open_at < close_at else ""


def find_declarations(text, symbol, _nest=0):
    """Declaration texts (doc comment included) for `symbol` in one declaration file's text."""
    head, _, rest = symbol.partition(".")
    out = []
    for start, end, doc in split_statements(text):
        statement = text[start:end].strip()
        match = DECL.match(statement)
        if match and match["name"] == head:
            if not rest:
                out.append(_with_doc(text, doc, statement))
            elif _nest < MAX_NEST and match["kind"] in ("namespace", "module", "class", "interface", "enum"):
                out.extend(find_members(_body(statement), rest, _nest + 1))
        elif _nest < MAX_NEST and re.match(r"^(?:export\s+)?declare\s+(?:module\s+['\"]|global\b)", statement):
            out.extend(find_declarations(_body(statement), symbol, _nest + 1))
    return out


def find_members(body, path, _nest=0):
    """Members of a container body named by the dotted `path`."""
    head, _, rest = path.partition(".")
    out = []
    for start, end, doc in split_statements(body, members=True):
        statement = body[start:end].strip().rstrip(",;")
        decl, member = DECL.match(statement), MEMBER.match(statement)
        name = decl["name"] if decl else member["name"].strip("'\"") if member else None
        if name != head:
            continue
        if not rest:
            out.append(_with_doc(body, doc, statement))
        elif _nest < MAX_NEST and decl and decl["kind"] in ("namespace", "module", "class", "interface", "enum"):
            out.extend(find_members(_body(statement), rest, _nest + 1))
    return out


def _export_items(clause):
    """[(original, exported)] from the inside of `{ A, B as C, type D }`."""
    items = []
    for item in clause.split(","):
        words = [w for w in item.split() if w != "type"]
        if len(words) == 1:
            items.append((words[0], words[0]))
        elif len(words) == 3 and words[1] == "as":
            items.append((words[0], words[2]))
    return items


def local_aliases(text, head):
    """Originals that a local `export { Original as head }` (no `from`) exports as `head`."""
    found = []
    for match in re.finditer(r"export\s+(?:type\s+)?\{([^}]*)\}(?!\s*from\b)", text):
        found.extend(orig for orig, exported in _export_items(match.group(1)) if exported == head and orig != head)
    return found


def reexports(text, head):
    """[(specifier, name to look for there)] for relative re-exports that can supply `head`:
    `export * from './x'` (same name) and `export { A as head } from './x'` (name A)."""
    found = []
    pattern = r"""export\s+(?:type\s+)?(\*|\{[^}]*\})\s*from\s*['"](\.{1,2}(?:/[^'"]*)?)['"]"""
    for match in re.finditer(pattern, text):
        clause, spec = match.groups()
        if clause == "*":
            found.append((spec, head))
        else:
            found.extend((spec, orig) for orig, exported in _export_items(clause[1:-1]) if exported == head)
    return found


# ---- package + entry resolution ------------------------------------------------------------------------------------

def deno_dir(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("DENO_DIR"):
        return Path(os.environ["DENO_DIR"])
    home = Path.home()
    if os.uname().sysname == "Darwin":
        return home / "Library" / "Caches" / "deno"
    return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "deno"


def parse_package(spec):
    """(name, wanted_version, via_deno) from `name`, `@scope/name` or `npm:name[@version]`."""
    spec = str(spec or "").strip()
    if not spec or len(spec) > 214:
        raise guard.Rejected("refused: package name is empty or too long")
    if re.match(r"^(jsr|https?|file|node|data):", spec) or spec.startswith(("/", ".", "~", "\\")):
        raise guard.Rejected(
            f"refused: {spec.split(':', 1)[0]!r} specifiers and paths are not supported; only npm packages are read, "
            "from node_modules or Deno's npm cache")
    via_deno = spec.startswith("npm:")
    if via_deno:
        spec = spec[4:]
    wanted = None
    at = spec.rfind("@")
    if at > 0:
        spec, wanted = spec[:at], spec[at + 1:] or None
    if not NAME_RE.match(spec):
        raise guard.Rejected(f"refused: {spec!r} is not an npm package name")
    return spec, wanted, via_deno


def types_package(name):
    return "@types/" + (name[1:].replace("/", "__") if name.startswith("@") else name)


def installed_version(directory):
    try:
        text = guard.read_text(Path(directory) / "package.json", limit=1_000_000)
        version = json.loads(text).get("version") if text else None
    except (ValueError, guard.Rejected, OSError):
        return None
    return version if isinstance(version, str) else None


def node_package_dir(project, name):
    modules = Path(project) / "node_modules"
    if modules.is_symlink():
        raise guard.Rejected("refused: node_modules is a symlink")
    if not modules.is_dir() or not (modules / name).exists():
        return None
    return guard.contained_dir(modules, modules / name)


def deno_package_dir(deno, name, version):
    cache = deno_dir(deno) / "npm"
    if not cache.is_dir() or cache.is_symlink():
        return None
    for host in sorted(p for p in cache.iterdir() if p.is_dir() and not p.is_symlink()):
        candidate = host / name / version
        if candidate.is_dir():
            return guard.contained_dir(cache, candidate)
    return None


def _types_target(value):
    """A declaration path from package.json: the path itself, or the .d.ts beside a .js/.mjs/.cjs one."""
    if not isinstance(value, str) or not value:
        return None
    value = value[2:] if value.startswith("./") else value
    for js, dts in ((".mjs", ".d.mts"), (".cjs", ".d.cts"), (".js", ".d.ts")):
        if value.endswith(js):
            return value[: -len(js)] + dts
    return value


def _from_exports(exports):
    if isinstance(exports, dict) and "." in exports:
        exports = exports["."]
    if isinstance(exports, str):
        return _types_target(exports)
    if isinstance(exports, list):
        return next(filter(None, (_from_exports(item) for item in exports)), None)
    if isinstance(exports, dict):
        for key in ("types", "import", "require", "default", "node"):
            if key in exports:
                got = _from_exports(exports[key])
                if got:
                    return got
    return None


def entry_candidates(package_dir):
    """Declaration entry paths (relative to the package directory), in the order package.json names them."""
    try:
        text = guard.read_text(Path(package_dir) / "package.json", limit=1_000_000)
        meta = json.loads(text) if text else {}
    except (ValueError, guard.Rejected):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    out = []
    for value in (meta.get("types"), meta.get("typings"), _from_exports(meta.get("exports")), _types_target(meta.get("main"))):
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    out.extend(f for f in ("index.d.ts", "index.d.mts", "index.d.cts") if f not in out)
    return out


def _existing(package_dir, relative, refusals):
    """The package-relative path of an existing allowlisted file, or None (a refusal is noted in `refusals`)."""
    try:
        path = guard.resolve_inside(package_dir, relative)
    except guard.Rejected as exc:
        note = f"{relative}: {exc}"
        if note not in refusals:
            refusals.append(note)
        return None
    return os.path.relpath(path, guard.real_root(package_dir)) if path.is_file() else None


def _module_file(package_dir, base, specifier, refusals):
    joined = os.path.normpath(os.path.join(os.path.dirname(base), specifier))
    stem = re.sub(r"\.(?:mjs|cjs|js)$", "", joined)
    for option in (f"{stem}.d.ts", f"{stem}.d.mts", f"{stem}.d.cts", f"{joined}.d.ts", f"{joined}/index.d.ts", f"{joined}/index.d.mts"):
        found = _existing(package_dir, option, refusals)
        if found:
            return found
    return None


def search_package(package_dir, symbol):
    """(hits, files_read, entry, refusals): hits are (file, declaration text); relative re-exports are followed."""
    refusals = []
    entry = next(filter(None, (_existing(package_dir, c, refusals) for c in entry_candidates(package_dir))), None)
    if entry is None:
        if refusals:
            raise guard.Rejected("refused: " + "; ".join(refusals))
        raise Miss("the package ships no declaration entry (no types/typings/exports/main .d.ts, no index.d.ts)", no_types=True)
    queue, seen, hits = [(entry, symbol, 0)], set(), []
    while queue and len(seen) < MAX_FILES:
        current, wanted, depth = queue.pop(0)
        if (current, wanted) in seen:
            continue
        seen.add((current, wanted))
        text = guard.read_text(guard.resolve_inside(package_dir, current))
        if text is None:
            continue
        head, _, rest = wanted.partition(".")
        found = find_declarations(text, wanted)
        for original in local_aliases(text, head):
            found += find_declarations(text, original + ("." + rest if rest else ""))
        equals = re.search(r"^\s*export\s*=\s*([A-Za-z_$][\w$]*)\s*;?", text, re.M)
        if not found and equals and not rest:
            found = find_declarations(text, f"{equals.group(1)}.{wanted}")
        if found:
            hits.extend((current, hit) for hit in found)
            continue
        if depth < MAX_NEST:
            for specifier, name in reexports(text, head):
                module = _module_file(package_dir, current, specifier, refusals)
                if module:
                    queue.append((module, name + ("." + rest if rest else ""), depth + 1))
    files = sorted({f for f, _ in seen})
    return hits, files, entry, refusals


def _lookup(project, name, wanted, via_deno, symbol, deno):
    node_dir = None if via_deno else node_package_dir(project, name)
    pinned = lock.pin(project, name)
    if not pinned and pinned.candidates and node_dir:  # several versions pinned: only then does the installed one matter
        pinned = lock.pin(project, name, installed_version(node_dir))
    if not pinned:
        raise Miss(pinned.reason)
    if wanted and wanted != pinned.version:
        raise Miss(f"{name}@{wanted} was asked for but {pinned.lockfile} pins {pinned.version}; no result for other versions",
                   pinned.version)
    package_dir = node_dir or deno_package_dir(deno, name, pinned.version)
    if package_dir is None:
        raise Miss(f"{name}@{pinned.version} is pinned but not installed (node_modules"
                   f"{'' if via_deno else ' and'} Deno's npm cache were searched); install dependencies first", pinned.version)
    installed = installed_version(package_dir)
    if installed != pinned.version:
        raise Miss(f"{name} on disk is {installed or 'of unknown version'} but {pinned.lockfile} pins {pinned.version}; "
                   "reinstall to match the lockfile", pinned.version)
    try:
        hits, files, entry, refusals = search_package(package_dir, symbol)
    except Miss as miss:
        miss.version = pinned.version
        raise
    if not hits:
        note = f"; refused: {'; '.join(refusals)}" if refusals else ""
        raise Miss(f"{symbol} not found in the {name}@{pinned.version} declarations (entry {entry}, {len(files)} file(s) read){note}",
                   pinned.version)
    member = f" (member {symbol})" if "." in symbol else ""
    text = "\n\n".join(f"// {name}@{pinned.version} — {file}{member}\n{hit}" for file, hit in hits)
    provenance = {"source": f"{'node_modules' if package_dir is node_dir else 'deno-cache'}:{package_dir}", "version": pinned.version,
                  "exact": installed == pinned.version, "retrieved": guard.now()}
    return Found(text, pinned.version, name, files, provenance)


def api_signature(project, package, symbol, *, deno=None):
    """A Found for the symbol. Raises Miss (a valid request without an answer) or guard.Rejected (refused)."""
    symbol = str(symbol or "").strip()
    if not SYMBOL_RE.match(symbol) or len(symbol) > 200:
        raise guard.Rejected(f"refused: {symbol!r} is not an identifier (use Name or Namespace.member)")
    name, wanted, via_deno = parse_package(package)
    try:
        return _lookup(project, name, wanted, via_deno, symbol, deno)
    except Miss as first:
        # A package with no declarations of its own may have them in @types/<name>, pinned on its own.
        if not first.no_types or name.startswith("@types/"):
            raise
        try:
            return _lookup(project, types_package(name), None, via_deno, symbol, deno)
        except Miss:
            raise first from None
