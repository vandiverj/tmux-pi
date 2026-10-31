"""Reverse import index for a Deno project (context engine, CX3).

`deno info --json` gives the forward module graph of one entry point and takes seconds per call, so it cannot answer "who imports
this file?" for every call. This module reads each source file's import specifiers itself (pure Python, no Node, no network, no
subprocess), resolves them the way Deno does (relative paths with their extension, the `imports` table of a Deno config, `npm:`
`jsr:` `node:` and URL specifiers as external), and keeps `file -> imports` per file, keyed by `(mtime_ns, size)`, so a later call
re-reads only the files that changed. The reverse map is derived from it.

What it does not do: type-check, follow a specifier it cannot resolve (those are listed in `unresolved`, never guessed), read a
`scopes` table (reported in `meta["ignored"]`), or open a file larger than MAX_BYTES. Paths in an index are posix paths relative to
`base` (the project root); a path outside `base` is never indexed or returned.

The scanner is a tokenizer, not a parser: it skips comments, strings, template literals and regular-expression literals, so an
`import` inside any of them is not an edge, then matches the import and re-export forms on the tokens."""
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import json
import os
import re
import stat
import time

INDEX_VERSION = 1
SOURCE_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
SKIP_DIRS = frozenset({"node_modules", ".git", ".tmux-pi", ".deno"})
MAX_BYTES = 2_000_000
EXTERNAL_SCHEMES = ("npm:", "jsr:", "node:", "http:", "https:", "data:", "blob:")
_REGEX_AFTER_WORDS = frozenset({"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
                                "yield", "await"})

_PAREN_KEYWORDS = frozenset({"if", "while", "for", "with"})

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<line>//[^\n]*)
  | (?P<block>/\*.*?(?:\*/|\Z))
  | (?P<str>'(?:[^'\\\n]|\\.)*'?|"(?:[^"\\\n]|\\.)*"?)
  | (?P<tpl>`)
  | (?P<id>[^\W\d][\w$]*|\$[\w$]*)
  | (?P<num>\d[\w.]*)
  | (?P<punct>.)
""", re.X | re.S)
_TEMPLATE_BODY = re.compile(r"(?:[^`\\$]|\\.|\$(?!\{))*", re.S)
_REGEX_BODY = re.compile(r"/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\\n]|\\.)*\])*/[a-z]*", re.S)
_REFERENCE = re.compile(r"""^///\s*<reference\s+(path)\s*=\s*["']([^"']+)["']""")


def tokenize(text):
    """[(kind, value, line)] for `text`: kind is `id`, `str` (value without its quotes), `num`, `val` (a template or a regex
    literal), `punct` or `ref` (a `/// <reference path="...">` comment). Comments and whitespace are dropped."""
    out, pos, line, size = [], 0, 1, len(text)
    depth, templates = 0, []  # brace depth, and the depth at which each open `${` began
    in_template = False
    parens, kw_close = [], -1  # open `(`: True when it follows if/while/for/with; index of the last `)` that closed such a one
    while pos < size:
        if in_template:
            body = _TEMPLATE_BODY.match(text, pos)
            pos = body.end()
            line += text.count("\n", body.start(), pos)
            if pos < size and text[pos] == "`":
                pos += 1
                in_template = False
                out.append(("val", "", line))
            elif text.startswith("${", pos):
                pos += 2
                templates.append(depth)
                depth += 1
                in_template = False
            else:
                in_template = False  # unterminated: stop treating the rest as template text
                pos = size if pos >= size else pos + 1
            continue
        match = _TOKEN.match(text, pos)
        kind, value = match.lastgroup, match.group()
        pos = match.end()
        if kind == "ws":
            line += value.count("\n")
        elif kind == "line":
            ref = _REFERENCE.match(value) if value.startswith("///") else None
            if ref:
                out.append(("ref", ref.group(2), line))
        elif kind == "block":
            line += value.count("\n")
        elif kind == "str":
            quote = value[0]
            out.append(("str", value[1:-1] if len(value) > 1 and value[-1] == quote else value[1:], line))
            line += value.count("\n")
        elif kind == "tpl":
            in_template = True
        elif kind == "punct" and value == "/":
            if _regex_allowed(out, kw_close):
                rx = _REGEX_BODY.match(text, pos - 1)
                if rx:
                    pos = rx.end()
                    out.append(("val", "", line))
                    continue
            out.append(("punct", "/", line))
        elif kind == "punct" and value == "(":
            before = out[-1] if out else ("", "", 0)
            parens.append(before[0] == "id" and (before[1] in _PAREN_KEYWORDS or (
                before[1] == "await" and len(out) > 1 and out[-2][0] == "id" and out[-2][1] == "for")))
            out.append(("punct", "(", line))
        elif kind == "punct" and value == ")":
            closes_condition = parens.pop() if parens else False
            out.append(("punct", ")", line))
            if closes_condition:
                kw_close = len(out) - 1
        elif kind == "punct" and value == "{":
            depth += 1
            out.append(("punct", "{", line))
        elif kind == "punct" and value == "}":
            depth -= 1
            if templates and templates[-1] == depth:
                templates.pop()
                in_template = True
            else:
                out.append(("punct", "}", line))
        else:
            out.append((kind, value, line))
    return out


def _regex_allowed(tokens, condition_close=-1):
    """A `/` starts a regular expression unless the token before it ends an expression. A `)` ends an expression, except the one
    that closes the condition of `if`/`while`/`for`/`with` (`condition_close` is its index): a statement follows it."""
    if not tokens:
        return True
    kind, value, _ = tokens[-1]
    if kind in ("str", "num", "val"):
        return False
    if kind == "id":
        return value in _REGEX_AFTER_WORDS
    if value == ")":
        return len(tokens) - 1 == condition_close
    return value not in ("]", "}")


def _skip_braces(tokens, at):
    """Index after the `}` that closes the `{` at `at`; len(tokens) when it never closes."""
    depth = 0
    while at < len(tokens):
        value = tokens[at][1] if tokens[at][0] == "punct" else ""
        depth += value == "{"
        depth -= value == "}"
        at += 1
        if depth <= 0:
            return at
    return at


def _is(tokens, at, value, kind="id"):
    return at < len(tokens) and tokens[at][0] == kind and tokens[at][1] == value


def _from_clause(tokens, at):
    """The specifier of `from "x"` at `at`, as (specifier, line), or None."""
    if _is(tokens, at, "from") and at + 1 < len(tokens) and tokens[at + 1][0] == "str":
        return tokens[at + 1][1], tokens[at + 1][2]
    return None


def scan_specifiers(text):
    """Every module specifier `text` imports or re-exports: [{"spec", "line", "type_only", "dynamic"}] in source order.

    Forms: `import "x"`, `import a, { b } from "x"`, `import * as a from "x"`, `import type ... from "x"`, `import a = require("x")`,
    `import("x")` with a literal, `export ... from "x"`, `export type ... from "x"` and `/// <reference path="x" />`. An `import(...)`
    whose argument is not a string literal is not an edge (it cannot be resolved without running the code)."""
    tokens = tokenize(text)
    found = []

    def add(spec, line, type_only=False, dynamic=False):
        found.append({"spec": spec, "line": line, "type_only": type_only, "dynamic": dynamic})

    for at, (kind, value, line) in enumerate(tokens):
        if kind == "ref":
            add(value, line, type_only=True)
            continue
        if kind != "id" or value not in ("import", "export"):
            continue
        if at and tokens[at - 1][0] == "punct" and tokens[at - 1][1] == ".":
            continue  # a property named import/export
        nxt = tokens[at + 1] if at + 1 < len(tokens) else ("", "", 0)
        if nxt[0] == "punct" and nxt[1] == ":":
            continue  # an object key
        at += 1
        if value == "import":
            if nxt[0] == "str":
                add(nxt[1], nxt[2])
            elif nxt[0] == "punct" and nxt[1] == "(":
                if at + 1 < len(tokens) and tokens[at + 1][0] == "str" and (_is(tokens, at + 2, ")", "punct")
                                                                           or _is(tokens, at + 2, ",", "punct")):
                    add(tokens[at + 1][1], tokens[at + 1][2], dynamic=True)
            elif nxt[0] == "punct" and nxt[1] == ".":
                continue  # import.meta
            else:
                _import_clause(tokens, at, add)
        else:
            _export_clause(tokens, at, add)
    return found


def _named_specifiers(tokens, at):
    """For the `{` at `at`: (index after its `}`, True when it lists at least one specifier and every one is `type`-marked, as in
    `{ type A, type B as C }`). A lone `type`, or `type as name`, names a value called `type`."""
    end = _skip_braces(tokens, at)
    inner, groups, current = tokens[at + 1:end - 1], [], []
    for token in inner:
        if token[0] == "punct" and token[1] == ",":
            groups.append(current)
            current = []
        else:
            current.append(token)
    groups.append(current)
    groups = [g for g in groups if g]

    def marked(group):
        if len(group) < 2 or group[0][0] != "id" or group[0][1] != "type":
            return False
        return group[1][1] != "as" or group[1][0] != "id" or len(group) in (2, 4)

    return end, bool(groups) and all(marked(g) for g in groups)


def _import_clause(tokens, at, add):
    """`import <clause> from "x"` / `import a = require("x")` with `at` just past `import`."""
    type_only = False
    if _is(tokens, at, "type") and at + 1 < len(tokens) and (
            tokens[at + 1][1] in ("{", "*") and tokens[at + 1][0] == "punct"
            or (tokens[at + 1][0] == "id" and tokens[at + 1][1] != "from")):
        type_only, at = True, at + 1
    has_value = False  # a default or namespace binding is a value, whatever the named ones are
    if at < len(tokens) and tokens[at][0] == "id":
        has_value = True
        at += 1  # the default binding
        if _is(tokens, at, "=", "punct") and _is(tokens, at + 1, "require") and _is(tokens, at + 2, "(", "punct") \
                and at + 3 < len(tokens) and tokens[at + 3][0] == "str":
            add(tokens[at + 3][1], tokens[at + 3][2], type_only=type_only)
            return
        if _is(tokens, at, ",", "punct"):
            at += 1
    if _is(tokens, at, "*", "punct"):
        has_value = True
        at += 3  # * as name
    elif _is(tokens, at, "{", "punct"):
        at, all_marked = _named_specifiers(tokens, at)
        type_only = type_only or (all_marked and not has_value)
    clause = _from_clause(tokens, at)
    if clause:
        add(clause[0], clause[1], type_only=type_only)


def _export_clause(tokens, at, add):
    """`export [type] { ... } from "x"` / `export * [as name] from "x"` with `at` just past `export`."""
    type_only = False
    if _is(tokens, at, "type") and at + 1 < len(tokens) and tokens[at + 1][0] == "punct" and tokens[at + 1][1] in ("{", "*"):
        type_only, at = True, at + 1
    if _is(tokens, at, "{", "punct"):
        at, all_marked = _named_specifiers(tokens, at)
        type_only = type_only or all_marked
    elif _is(tokens, at, "*", "punct"):
        at += 1
        if _is(tokens, at, "as"):
            at += 2
    else:
        return
    clause = _from_clause(tokens, at)
    if clause:
        add(clause[0], clause[1], type_only=type_only)


class ImportMap:
    """The `imports` table of a Deno config: exact keys and `prefix/` keys, longest key first. Targets that start with `./`, `../` or
    `/` are paths relative to the config's directory; every other target (`npm:`, `jsr:`, `https:`, ...) is external."""

    def __init__(self, imports, config_dir):
        self.config_dir = config_dir
        self.entries = sorted(((str(k), str(v)) for k, v in (imports or {}).items()), key=lambda kv: -len(kv[0]))

    def lookup(self, spec):
        """(target, matched) for `spec`: target is an absolute path, an external specifier, or None when no key matches."""
        for key, target in self.entries:
            if spec == key or (key.endswith("/") and spec.startswith(key)):
                full = target + (spec[len(key):] if key.endswith("/") else "")
                if full.startswith(("./", "../", "/")):
                    return os.path.normpath(full if os.path.isabs(full) else os.path.join(self.config_dir, full)), key
                return full, key
        package = "/".join(spec.split("/")[: 2 if spec.startswith("@") else 1])
        for key, target in self.entries:  # a package key also covers its subpaths: "drizzle-orm/pg-core" under "drizzle-orm"
            if key == package and spec != package and target.startswith(("npm:", "jsr:")):
                return target + spec[len(package):], key
        return None, None


def load_config(path):
    """(ImportMap, details) from a Deno config file; a missing or unreadable file gives an empty map and says why in `details`."""
    details = {"path": path, "exclude": [], "ignored": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        details["error"] = type(error).__name__
        return ImportMap({}, os.path.dirname(path)), details
    if not isinstance(data, dict):
        details["error"] = "not an object"
        return ImportMap({}, os.path.dirname(path)), details
    imports = data.get("imports") if isinstance(data.get("imports"), dict) else {}
    details["exclude"] = [e for e in data.get("exclude", []) if isinstance(e, str)] if isinstance(data.get("exclude"), list) else []
    if data.get("scopes"):
        details["ignored"].append("scopes")
    if data.get("workspace"):
        details["ignored"].append("workspace")
    return ImportMap(imports, os.path.dirname(path)), details


def resolve(spec, importer, import_map, isfile=os.path.isfile):
    """How Deno resolves `spec` written in `importer` (absolute path): (kind, target, reason).

    kind `file`: target is an absolute path of an existing file. kind `external`: target is the specifier (a package, a builtin, a
    URL). kind `unresolved`: target is None and reason says why (`missing` file, `bare` specifier no key of the import map matches)."""
    if spec.startswith(EXTERNAL_SCHEMES):
        return "external", spec, ""
    spec = re.split(r"[?#]", spec, maxsplit=1)[0]  # "./a.tsx?variant" is the module a.tsx
    if spec.startswith("file://"):
        path = os.path.normpath(spec[len("file://"):])
        return ("file", path, "") if isfile(path) else ("unresolved", None, "missing")
    if spec.startswith(("./", "../", "/")):
        path = os.path.normpath(spec if os.path.isabs(spec) else os.path.join(os.path.dirname(importer), spec))
        return ("file", path, "") if isfile(path) else ("unresolved", None, "missing")
    target, _ = import_map.lookup(spec)
    if target is None:
        return "unresolved", None, "bare"
    if os.path.isabs(target):
        return ("file", target, "") if isfile(target) else ("unresolved", None, "missing")
    return "external", target, ""


def lexical_path(base, path):
    """`path` (absolute, or relative to `base`) as a posix path relative to `base`, judged by its name alone; None when it is
    `base` itself or outside it. `base` must be a resolved path."""
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))
    if full == base or not full.startswith(base + os.sep):
        return None
    return os.path.relpath(full, base).replace(os.sep, "/")


def project_path(base, path):
    """`lexical_path`, and also None when the path, with its symlinks followed, leaves `base`. The relative path returned is the one
    written, not where a symlink inside the project leads. An absolute path may be given in any spelling of the project root
    (`/tmp/x` for `/private/tmp/x`)."""
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))
    real = os.path.realpath(full)
    if real == base or not real.startswith(base + os.sep):
        return None
    return lexical_path(base, full) or lexical_path(base, real)


def read_source(path):
    """The text of `path`, or None when it cannot be read or is larger than MAX_BYTES."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > MAX_BYTES else data.decode("utf-8", "replace")


def read_sources(paths, workers=8):
    """{path: text or None} for each path. Reading is the slow part of a build (many small files, and a cold disk); it is done in
    threads, and the CPU work of scanning stays in the caller."""
    paths = list(paths)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(paths) or 1))) as pool:
        return dict(zip(paths, pool.map(read_source, paths)))


# ------------------------------------------------------------------------------------------------------------ the index

def _excluded(rel, patterns):
    """True when the config-relative posix path `rel` is under, or matches, one of the config's `exclude` entries."""
    for pattern in patterns:
        pattern = pattern.strip("/")
        if any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern + "/*"):
                return True
        elif rel == pattern or rel.startswith(pattern + "/"):
            return True
    return False


def _signature(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_mtime_ns, info.st_size]


class ImportIndex:
    """`file -> imports` for the source files below `root`, and the reverse map derived from it.

    `base` is the project root: every path in and out is a posix path relative to it, and nothing outside it is read, indexed or
    returned. `allow` is the caller's path guard: a file it refuses is never read or indexed, and an import that resolves to one is
    an `unresolved` edge with reason `refused` whose specifier is blanked (the edge must not name the path); a path that leaves the
    project, by its name or through a symlinked directory, is `unresolved` with reason `outside`. `config` is the Deno config whose `imports` table resolves bare specifiers (default `root/deno.json`); `overlays` are
    further configs (for the API, `deno.test.json`, which swaps the Temporal packages for mocks): where one resolves a specifier
    differently from `config`, the extra edge is recorded with `config` set to that file's name, so the importers of a mock module
    are answerable.

    What is cached, and keyed how: per file the scanned specifiers, keyed by `(mtime_ns, size)`. Resolution is not cached: it is
    string work that costs a fraction of a second for a thousand files, and it has to be redone when a file elsewhere appears (an
    importer of a file that did not exist yet becomes an edge without the importer changing). `refresh()` re-reads only the files
    whose key changed, forgets deleted ones, and returns what it did."""

    def __init__(self, base, root=None, config=None, overlays=(), cache_path=None, allow=None):
        self.base = os.path.realpath(base)
        self.allow = allow  # allow(rel) -> bool: may this project file be read? None allows every file inside the project
        self.root = os.path.realpath(root or base)
        self.config = os.path.join(self.root, "deno.json") if config is None else os.path.realpath(config)
        self.overlays = [os.path.realpath(p) for p in overlays]
        self.cache_path = cache_path
        self.entries = {}  # rel -> {"key": [mtime_ns, size], "specs": [[spec, line, type_only, dynamic], ...]}
        self.edges = {}  # rel -> [edge]
        self.meta = {"ignored": [], "config_error": ""}
        self._reverse = {}
        self._known = set()
        self.refreshed_at = None
        self._load()

    # --- paths

    def normalize(self, path):
        """`path` as a base-relative posix path, or None when it is outside the project (see `project_path`)."""
        return project_path(self.base, path)

    def _abs(self, rel):
        return os.path.join(self.base, rel)

    # --- persistence

    def _load(self):
        if not self.cache_path or os.path.islink(self.cache_path):
            return
        try:
            with open(self.cache_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or data.get("base") != self.base \
                or data.get("root") != self.root or not isinstance(data.get("entries"), dict):
            return
        for rel, entry in data["entries"].items():
            if isinstance(entry, dict) and isinstance(entry.get("key"), list) and isinstance(entry.get("specs"), list):
                self.entries[rel] = {"key": entry["key"], "specs": entry["specs"]}

    def save(self):
        """Write the entries to `cache_path` (temp file, then rename). Returns True when written; a symlink at the path, or a
        failed write, leaves the cache as it was and returns False (the index still works, only the next start is cold)."""
        if not self.cache_path or os.path.islink(self.cache_path):
            return False
        data = {"version": INDEX_VERSION, "base": self.base, "root": self.root, "entries": self.entries}
        temp = f"{self.cache_path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            os.replace(temp, self.cache_path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass
            return False
        return True

    # --- building

    def _walk(self, patterns):
        found = {}
        config_dir = os.path.dirname(self.config)
        for here, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not os.path.islink(os.path.join(here, d))
                             and not _excluded(os.path.relpath(os.path.join(here, d), config_dir).replace(os.sep, "/"), patterns))
            for name in sorted(names):
                if not name.endswith(SOURCE_EXTENSIONS):
                    continue
                full = os.path.join(here, name)
                try:
                    info = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue  # a symlink is skipped: it could lead out of the project
                if not _excluded(os.path.relpath(full, config_dir).replace(os.sep, "/"), patterns):
                    rel = os.path.relpath(full, self.base).replace(os.sep, "/")
                    if self._allowed(rel):
                        found[rel] = [info.st_mtime_ns, info.st_size]
        return found

    def refresh(self):
        """Bring the index up to date with the files on disk. Returns {"files", "scanned", "removed", "unchanged", "stale",
        "ms"}: `scanned` is how many files were read this time; `stale` lists files that changed again while they were being read
        (the next call picks them up; the answer should say so)."""
        started = time.perf_counter()
        imap, details = load_config(self.config)
        self.meta = {"ignored": list(details["ignored"]), "config_error": details.get("error", "")}
        overlay_maps = []
        for path in self.overlays:
            omap, odetails = load_config(path)
            overlay_maps.append((os.path.basename(path), omap))
            self.meta["ignored"] += [f"{os.path.basename(path)}:{item}" for item in odetails["ignored"]]
        current = self._walk(details["exclude"])
        changed = sorted(rel for rel, key in current.items() if rel not in self.entries or self.entries[rel]["key"] != key)
        removed = [rel for rel in self.entries if rel not in current]
        for rel in removed:
            del self.entries[rel]
        texts = read_sources([self._abs(rel) for rel in changed])
        stale = []
        for rel in changed:
            text = texts[self._abs(rel)]
            specs = [[s["spec"], s["line"], s["type_only"], s["dynamic"]] for s in scan_specifiers(text)] if text is not None else []
            if _signature(self._abs(rel)) != current[rel]:
                stale.append(rel)  # keyed by the key seen before the read, so the next refresh reads it again
            self.entries[rel] = {"key": current[rel], "specs": specs}
        self._known = set(current)
        self._resolve_all(imap, overlay_maps)
        self.refreshed_at = time.time()
        if changed or removed:
            self.save()
        return {"files": len(current), "scanned": len(changed), "removed": len(removed), "unchanged": len(current) - len(changed),
                "stale": stale, "ms": round((time.perf_counter() - started) * 1000)}

    def _allowed(self, rel):
        if self.allow is None:
            return True
        try:
            return bool(self.allow(rel))
        except Exception:  # a guard that fails refuses
            return False

    def _is_file(self):
        cache = {}

        def check(path):
            if path not in cache:
                cache[path] = os.path.isfile(path)
            return cache[path]
        return check

    def _judge(self, target):
        """(rel, "") for an existing file an import may point at, else (None, reason): `outside` when it, or a directory on its way,
        leads out of the project; `refused` when the guard refuses it (by its name or by where it really is); `missing` when
        a symlink is on the way inside the project (the walk does not follow links, so neither does an edge)."""
        lexical = lexical_path(self.base, target)
        if lexical is None:
            return None, "outside"
        if lexical in self._known:
            return lexical, ""  # walked, so allowed and free of symlinks
        real = os.path.realpath(target)
        inside = lexical_path(self.base, real)
        if inside is None:
            return None, "outside"
        if not (self._allowed(lexical) and self._allowed(inside)):
            return None, "refused"
        if real != target:
            return None, "missing"
        return lexical, ""

    def _resolve_all(self, imap, overlay_maps):
        isfile = self._is_file()
        edges, reverse = {}, {}
        for rel in sorted(self.entries):
            importer = self._abs(rel)
            out = []
            for spec, line, type_only, dynamic in self.entries[rel]["specs"]:
                base = resolve(spec, importer, imap, isfile)
                out.append(self._edge(rel, spec, line, type_only, dynamic, base, None))
                for name, omap in overlay_maps:
                    alt = resolve(spec, importer, omap, isfile)
                    if alt[:2] != base[:2]:
                        out.append(self._edge(rel, spec, line, type_only, dynamic, alt, name))
            edges[rel] = out
            for edge in out:
                if edge["kind"] == "file":
                    reverse.setdefault(edge["to"], []).append(edge)
        for items in reverse.values():
            items.sort(key=lambda e: (e["from"], e["line"], e["config"] or ""))
        self.edges, self._reverse = edges, reverse

    def _edge(self, rel, spec, line, type_only, dynamic, resolved, config):
        kind, target, reason = resolved
        if kind == "file":
            inside, why = self._judge(target)
            if inside is None:
                kind, target, reason = "unresolved", None, why
                if why == "refused":
                    spec = ""
            else:
                target = inside
        return {"from": rel, "line": line, "spec": spec, "kind": kind, "to": target, "reason": reason, "type_only": bool(type_only),
                "dynamic": bool(dynamic), "config": config}

    # --- questions

    def importers(self, rel):
        """Every edge that imports `rel`, ordered by (importer, line). [] for a file nothing imports and for an unknown path."""
        return list(self._reverse.get(rel, []))

    def imports(self, rel):
        """Every import edge written in `rel` (file, external and unresolved), in source order; None when `rel` is not indexed."""
        return list(self.edges[rel]) if rel in self.edges else None

    def unresolved(self):
        """Every edge no rule could resolve: [{"from", "line", "spec", "reason"}]. Resolved as external (a package, a builtin, a
        URL) is not unresolved."""
        return [{"from": e["from"], "line": e["line"], "spec": e["spec"], "reason": e["reason"]}
                for rel in sorted(self.edges) for e in self.edges[rel] if e["kind"] == "unresolved"]

    def indexed(self, rel):
        return rel in self.entries

    def age_s(self):
        return None if self.refreshed_at is None else round(time.time() - self.refreshed_at, 3)
