"""Reverse import index for a Deno project (context engine, CX3).

`deno info --json` gives the forward module graph of one entry point and takes seconds per call, so it cannot answer "who imports
this file?" for every call. This module reads each source file's import specifiers itself (pure Python, no Node, no network, no
subprocess), resolves them the way Deno does (relative paths with their extension, the `imports` table of a Deno config, `npm:`
`jsr:` `node:` and URL specifiers as external), and keeps `file -> imports` per file, keyed by `(mtime_ns, size)`, so a later call
re-reads only the files that changed. The reverse map is derived from it.

What it does not do: type-check, follow a specifier it cannot resolve (those are listed in `unresolved`, never guessed), or open a
file larger than MAX_BYTES. `scopes` of the config are followed (a malformed table is reported in `meta["ignored"]`). Paths in an
index are posix paths relative to `base` (the project root); a path outside `base` is never indexed or returned, and a specifier
that names one (or a path the caller's guard refuses) is blanked in the edge.

`prove_reach` is separate from the index: the allow-list walk that must succeed before Deno is run (see nav_api.py). It reads the
files itself and shares only the scanner, the import map and the path helpers with the index. What it proves is also what Deno is
given: `Reach.materialize` copies exactly the proved files, with a rewritten config, into a temporary `Mirror`, and Deno is run
only there, so a file the walk missed is a file Deno cannot open.

The scanner is a tokenizer, not a parser: it skips comments, strings, template literals and regular-expression literals, so an
`import` inside any of them is not an edge, then matches the import and re-export forms on the tokens."""
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
import time
from urllib.parse import unquote

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
            closing_tag = bool(out) and out[-1][:2] == ("punct", "<") and pos >= 2 and text[pos - 2] == "<"  # `</div>`
            rx = _REGEX_BODY.match(text, pos - 1) if not closing_tag and _regex_allowed(out, kw_close) else None
            if rx:
                pos = rx.end()
                out.append(("val", "", line))
            else:
                out.append(("punct", "/", line))
        elif kind == "punct" and value == "(":
            before = out[-1] if out else ("", "", 0)
            parens.append(before[0] == "id" and not _after_dot(out, len(out) - 1) and (before[1] in _PAREN_KEYWORDS or (
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
        return value in _REGEX_AFTER_WORDS and not _after_dot(tokens, len(tokens) - 1)  # `a.in / 2`: a property, not the keyword
    if value == ")":
        return len(tokens) - 1 == condition_close
    if value in ("+", "-") and len(tokens) > 1 and tokens[-2][:2] == ("punct", value):
        return False  # `a++ / 2`: a regex cannot follow a postfix operator, and `+ +/x/` is not code
    return value not in ("]", "}")


def _after_dot(tokens, at):
    """True when the token at `at` is the name after a single `.` (a property, `a.if`), not after a spread's `...`."""
    return (at > 0 and tokens[at - 1][:2] == ("punct", ".")
            and not (at > 1 and tokens[at - 2][:2] == ("punct", ".")))


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
    whose argument is not a string literal is not an edge (it cannot be resolved without running the code); `scan_module` reports it."""
    return scan_module(text)["specs"]


_PRAGMA = re.compile(r"""(?:@deno-types|@ts-types)\s*=\s*["']([^"']+)["']|<reference\s+(?:path|types)\s*=\s*["']([^"']+)["']"""
                     r"""|@jsxImportSource(?:Types)?\s+([^\s*"']+)""")


def scan_module(text):
    """What `text` makes Deno load: {"specs": as `scan_specifiers`, "pragmas": [specifier], "unmodelled": [str]}.

    `pragmas` are the specifiers named in comments that Deno reads (`// @ts-types="x"`, `// @deno-types="x"`,
    `/// <reference types="x" />`, `/** @jsxImportSource x */`), found on the raw text, so a match inside a string is a false
    positive that can only make a check stricter. `unmodelled` names a construct that loads a module the scanner cannot name:
    `dynamic_import` (`import(expr)` with an argument that is not a string literal) and `import_meta_resolve`."""
    tokens = tokenize(text)
    found, unmodelled = [], []

    def add(spec, line, type_only=False, dynamic=False):
        found.append({"spec": spec, "line": line, "type_only": type_only, "dynamic": dynamic})

    for at, (kind, value, line) in enumerate(tokens):
        if kind == "ref":
            add(value, line, type_only=True)
            continue
        if kind != "id" or value not in ("import", "export"):
            continue
        if (at and tokens[at - 1][0] == "punct" and tokens[at - 1][1] == "."
                and not (at > 1 and tokens[at - 2][0] == "punct" and tokens[at - 2][1] == ".")):
            continue  # a property named import/export; `...import(x)` is a spread of a real import, so it is kept
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
                elif "dynamic_import" not in unmodelled:
                    unmodelled.append("dynamic_import")
            elif nxt[0] == "punct" and nxt[1] == ".":
                if _is(tokens, at + 1, "meta") and _is(tokens, at + 2, ".", "punct") and _is(tokens, at + 3, "resolve") \
                        and "import_meta_resolve" not in unmodelled:
                    unmodelled.append("import_meta_resolve")
                continue  # import.meta
            else:
                _import_clause(tokens, at, add)
        else:
            _export_clause(tokens, at, add)
    pragmas = [next(group for group in match.groups() if group) for match in _PRAGMA.finditer(text)]
    return {"specs": found, "pragmas": pragmas, "unmodelled": unmodelled}


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


def path_like_key(key):
    """True for an import-map key that is a path or a file URL (`./src/a.ts`, `../a`, `/a`, `file:///a`, a backslash form), not a
    specifier prefix. Deno's URL parser resolves such a key against the config and matches it to the importer's *resolved*
    address, so it can redirect a relative import; `resolve` does not model that."""
    return key in (".", "..") or key.startswith(("./", "../", "/")) or key.lower().startswith("file:") or "\\" in key


def unmodelled_target(target):
    """True for an import-map target the walk does not model: a `file:` URL, an absolute path or a backslash form. A target that
    starts `./` or `../` is a path below the config, which is modelled and, in the mirror, stays below the mirror."""
    return target.lower().startswith("file:") or target.startswith("/") or "\\" in target


class ImportMap:
    """The `imports` and `scopes` tables of a Deno config. Exact keys and `prefix/` keys, longest key first. Targets that start with
    `./`, `../` or `/` are paths relative to the config's directory (also inside a scope); every other target (`npm:`, `jsr:`,
    `https:`, ...) is external.

    Scopes follow the import-map rule: for a referrer (the importing file), the scopes whose key is that file or a directory above it
    are tried longest first, each as its own table; a scope that has no key for the specifier gives way to the next, and the
    top-level table is last. A table that is not what Deno reads (a scope that is not a table of strings, an `imports` that is not
    a table, a key that is a path or a `file:` URL, a target that is a `file:` URL or an absolute path) sets `unmodelled`; the walk
    that guards Deno refuses on it, because it cannot say what Deno would load."""

    def __init__(self, imports, config_dir, scopes=None):
        self.config_dir = config_dir
        self.unmodelled = False
        self.entries = self._table(imports)
        self.scopes = []
        if scopes:
            if not isinstance(scopes, dict):
                self.unmodelled = True
            else:
                for key, table in scopes.items():
                    prefix = self._scope_prefix(str(key))
                    if not isinstance(table, dict):
                        self.unmodelled = True
                    elif prefix is not None:  # a scope for a URL no local file has as its address cannot apply here
                        self.scopes.append((prefix, self._table(table)))
                self.scopes.sort(key=lambda item: -len(item[0]))

    def _table(self, table):
        if table is None:
            return []
        if not isinstance(table, dict):
            self.unmodelled = True
            return []
        if not all(isinstance(v, str) for v in table.values()):
            self.unmodelled = True
        if any(path_like_key(str(k)) for k in table) or any(isinstance(v, str) and unmodelled_target(v) for v in table.values()):
            self.unmodelled = True
        return sorted(((str(k), v) for k, v in table.items() if isinstance(v, str)), key=lambda kv: -len(kv[0]))

    def _scope_prefix(self, key):
        if key.startswith("file://"):
            key = key[len("file://"):]
        if not key.startswith(("./", "../", "/")):
            return None
        full = os.path.normpath(key if os.path.isabs(key) else os.path.join(self.config_dir, key))
        return full + "/" if key.endswith("/") else full

    def _match(self, entries, spec):
        for key, target in entries:
            if spec == key or (key.endswith("/") and spec.startswith(key)):
                full = target + (spec[len(key):] if key.endswith("/") else "")
                if full.startswith(("./", "../", "/")):
                    return os.path.normpath(full if os.path.isabs(full) else os.path.join(self.config_dir, full)), key
                return full, key
        package = "/".join(spec.split("/")[: 2 if spec.startswith("@") else 1])
        for key, target in entries:  # a package key also covers its subpaths: "drizzle-orm/pg-core" under "drizzle-orm"
            if key == package and spec != package and target.startswith(("npm:", "jsr:")):
                return target + spec[len(package):], key
        return None, None

    def lookup(self, spec, referrer=None):
        """(target, matched) for `spec` written in the file `referrer` (an absolute path; None skips the scopes): target is an
        absolute path, an external specifier, or None when no key matches."""
        if referrer:
            for prefix, entries in self.scopes:
                if referrer == prefix or (prefix.endswith("/") and referrer.startswith(prefix)):
                    got = self._match(entries, spec)
                    if got[0] is not None:
                        return got
        return self._match(self.entries, spec)


# A key of the config that changes how Deno resolves a module, which the walk does not follow: Deno would read files it never saw.
_UNMODELLED_KEYS = ("importMap", "workspace", "links", "patch", "vendor")


def load_config(path, raw=None):
    """(ImportMap, details) from a Deno config file; a missing or unreadable file gives an empty map and says why in `details`.

    `details`: `exclude`, `ignored` (what the index does not follow), `unmodelled` (why the walk that guards Deno cannot trust the
    map: a list of short reasons, empty when it can), `data` (the parsed config, {} when unreadable) and `error`. `raw` is the
    file's bytes when the caller has read them already (the parse is then of those bytes, not of a second read)."""
    details = {"path": path, "exclude": [], "ignored": [], "unmodelled": [], "data": {}}
    try:
        if raw is None:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        else:
            data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as error:
        details["error"] = type(error).__name__
        return ImportMap({}, os.path.dirname(path)), details
    if not isinstance(data, dict):
        details["error"] = "not an object"
        return ImportMap({}, os.path.dirname(path)), details
    details["data"] = data
    imap = ImportMap(data.get("imports"), os.path.dirname(path), data.get("scopes"))
    details["exclude"] = [e for e in data.get("exclude", []) if isinstance(e, str)] if isinstance(data.get("exclude"), list) else []
    if imap.unmodelled:
        details["unmodelled"].append("import_map")
        if data.get("scopes"):
            details["ignored"].append("scopes")
    for key in _UNMODELLED_KEYS:
        if data.get(key):
            details["unmodelled"].append(key)
    if data.get("workspace"):
        details["ignored"].append("workspace")
    unstable = data.get("unstable")
    if "sloppy-imports" in (unstable if isinstance(unstable, list) else [unstable]):
        details["unmodelled"].append("sloppy-imports")  # Deno would try extensions and index files the walk does not
    return imap, details


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
    target, _ = import_map.lookup(spec, importer)
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


# ------------------------------------------------------------------------------------------------------- the allow-list walk

MAX_REACH_FILES = 3000
_PACKAGE_LOCAL = ("file:", "link:", "portal:", "workspace:")
_SCAN_MEMO = {}


class Unproven(Exception):
    """The walk cannot show that Deno would load only allowed files. `reason` is one of `outside_project`, `reaches_refused_path`,
    `unmodelled_config`, `unmodelled_import`, `unreadable_file`, `too_many_files`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def spec_variants(spec):
    """The spellings of `spec` that Deno's URL parser may read as different paths: as written, percent-decoded (`%2e%2e` is `..`),
    and with backslashes as slashes. A check applies to every one of them: the file Deno reads is whichever it is, and a check on
    the spelling alone would pass a `./%2e%2e/.env`."""
    out = [spec]
    for variant in (unquote(spec), spec.replace("\\", "/"), unquote(spec).replace("\\", "/")):
        if variant not in out:
            out.append(variant)
    return out


def named_paths(spec, importer, import_map):
    """Every absolute path `spec`, written in the file `importer`, may name once the URL parser and the import map have had their
    say: [] for a package, a builtin, a URL or a bare specifier no key matches. It names a path whether or not a file is there."""
    found = []
    for variant in spec_variants(spec):
        variant = re.split(r"[?#]", variant, maxsplit=1)[0]
        if variant.startswith(EXTERNAL_SCHEMES):
            continue
        if variant.startswith("file://"):
            found.append(os.path.normpath(variant[len("file://"):]))
        elif variant.startswith(("./", "../", "/")):
            found.append(os.path.normpath(variant if os.path.isabs(variant) else os.path.join(os.path.dirname(importer), variant)))
        else:
            target, _ = import_map.lookup(variant, importer)
            if target is not None and os.path.isabs(target):
                found.append(target)
                twin = os.path.normpath(unquote(target).replace("\\", "/"))  # a map target is parsed as a URL as well
                if twin != target:
                    found.append(twin)
    return list(dict.fromkeys(found))


def admit(base, path, allow):
    """(lexical, real) project-relative names of `path` when Deno may read it, else raises Unproven: `outside_project` when it, or a
    directory on its way, is not inside `base`; `reaches_refused_path` when `allow(rel)` refuses either name (a guard that raises
    refuses). Judged by name whether or not a file is there."""
    lexical = lexical_path(base, path)
    inside = lexical_path(base, os.path.realpath(path))
    if lexical is None or inside is None:
        raise Unproven("outside_project")
    for rel in dict.fromkeys((lexical, inside)):
        try:
            allowed = bool(allow(rel))
        except Exception:
            allowed = False
        if not allowed:
            raise Unproven("reaches_refused_path")
    return lexical, inside


def _fingerprint(data):
    return hashlib.sha256(data).hexdigest()


CONFIG_ABSENT = ("absent",)
CONFIG_UNREADABLE = ("unreadable",)


def _config_read(path):
    """(state, bytes or None): the state of a config file (see `_config_state`) and the bytes it was made from, from one read."""
    if not os.path.lexists(path):
        return CONFIG_ABSENT, None
    data = _read_capped(path) if os.path.isfile(path) else None
    if data is None:
        return CONFIG_UNREADABLE, None
    return ("content", os.path.realpath(path), _fingerprint(data)), data


def _config_state(path):
    """What a config file is now, one of three states: `CONFIG_ABSENT` (nothing is there), `CONFIG_UNREADABLE` (something is there
    and it cannot be read as a file, or is over MAX_BYTES) or `("content", resolved path, fingerprint)`. Every writer and reader of
    a config state goes through here, so "there was none" and "there is one I cannot read" are never the same value."""
    return _config_read(path)[0]


def _state_token(state):
    return state[0] if len(state) == 1 else f"{state[0]}:{state[2]}"


def _scan_cached(data):
    key = hashlib.sha256(data).hexdigest()
    got = _SCAN_MEMO.get(key)
    if got is None:
        if len(_SCAN_MEMO) >= 4096:
            _SCAN_MEMO.clear()
        got = _SCAN_MEMO[key] = scan_module(data.decode("utf-8", "replace"))
    return got


class Reach:
    """What `prove_reach` proved: every file Deno would load from the entry, as {rel: (real rel, fingerprint)}, and the bytes that
    were read (`contents`: the snapshot a mirror is built from). `names` holds both spellings of each (a symlink inside the project
    is a file under two names); `digest()` stands for their content; `holds()` is False once any of them has changed, or points
    somewhere else, since the proof. `config_rel` is the config's project-relative path, `config_data` what the mirror's config
    is made from and `packages` the sanitised `package.json` files (`rel -> dict`). `configs` is every file those two were made
    from, the config and the `package.json` of each folder Deno looks in, as {absolute path: `_config_state`} (absent, unreadable
    or content). `holds()` checks them as well, since an answer made with an old config is an answer for a tree that has moved,
    and a change between any two of the three states counts."""

    def __init__(self, base):
        self.base = base
        self.files = {}
        self.contents = {}
        self.entry = None
        self.config_rel = None
        self.config_data = {}
        self.packages = {}
        self.configs = {}

    @property
    def names(self):
        return {name for rel, (real, _) in self.files.items() for name in (rel, real)}

    def digest(self):
        h = hashlib.sha256()
        for rel in sorted(self.files):
            h.update(f"{rel}\0{self.files[rel][1]}\0".encode("utf-8"))
        for path in sorted(self.configs):
            h.update(f"config\0{lexical_path(self.base, path)}\0{_state_token(self.configs[path])}\0".encode("utf-8"))
        return h.hexdigest()

    def holds(self):
        for rel, (real, fingerprint) in self.files.items():
            path = os.path.join(self.base, rel)
            if lexical_path(self.base, os.path.realpath(path)) != real:
                return False
            data = _read_capped(path)
            if data is None or _fingerprint(data) != fingerprint:
                return False
        return all(_config_state(path) == state for path, state in self.configs.items())

    def materialize(self, parent=None):
        """A `Mirror` holding a copy of each proved file and nothing else, under the same relative paths, with a config made from
        `config_data`: the only tree Deno is given."""
        mirror = Mirror(self.base, parent)
        try:
            package_files = {posixpath.join(folder, "package.json") for folder in (posixpath.dirname(self.config_rel), "")}
            for rel in sorted(self.files):
                if rel not in package_files and rel != self.config_rel:  # those are written once, below, in their cut-down form
                    mirror.write(rel, self.contents[rel])
            mirror.write_json(self.config_rel, self._mirror_config())
            for rel in sorted(package_files):
                mirror.write_json(rel, self.packages.get(posixpath.dirname(rel), {}))
        except BaseException:
            mirror.cleanup()
            raise
        return mirror

    def _mirror_config(self):
        """The config Deno gets in the mirror: what changes how a module resolves or what a file is (`imports`, `scopes`,
        `compilerOptions`, `unstable`), nothing that names a path of its own (`exclude`, `lock`, `tasks`, a vendor or
        node_modules directory). The npm cache is the global one."""
        data = self.config_data
        out = {"lock": False, "nodeModulesDir": "none"}
        for key in ("imports", "scopes", "compilerOptions", "unstable"):
            if key in data:
                out[key] = data[key]
        return out


class Mirror:
    """A temporary directory laid out like the project (same relative paths, so a relative import and the config's `./` targets
    mean the same), holding copies of the files the walk proved and a rewritten config. A file the walk did not prove is not in it,
    so an import the scanner missed, or an import map entry the walk misread, resolves to nothing instead of to a refused file.
    Files are written as new regular files, never as links. `root` is its resolved path; `cleanup()` removes it."""

    def __init__(self, base, parent=None):
        self.base = base
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="tp-ctx-mirror-", dir=parent))
        if self.root == base or self.root.startswith(base + os.sep) or base.startswith(self.root + os.sep):
            self.cleanup()
            raise Unproven("unmodelled_config")  # a mirror inside the project (or around it) would put refused files in Deno's reach

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def write(self, rel, data):
        target = self.path(rel)
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        with open(target, "xb") as handle:
            handle.write(data)

    def write_json(self, rel, value):
        self.write(rel, json.dumps(value, sort_keys=True).encode("utf-8"))

    def to_real(self, path):
        """The project-relative posix name of `path` when it is a file of the mirror, else None (Deno named a path outside the mirror)."""
        full = os.path.normpath(path)
        if full == self.root or not full.startswith(self.root + os.sep):
            return None
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def _read_capped(path):
    """The bytes of `path` when it can be read and is not larger than MAX_BYTES, else None."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > MAX_BYTES else data


_PACKAGE_KEEP = ("name", "version", "type")
_PACKAGE_DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _check_package_json(base, directory, configs):
    """Deno reads the `package.json` next to its config. A dependency that is a local path (`file:`, `link:`, a workspace) loads
    code from somewhere the walk does not look: refused. A `package.json` that is not JSON is refused too. Returns {folder: what
    the mirror gets} for the folders that have one (`""` is the project root, others are project-relative posix paths): the
    package's `name`, `version`, `type` and dependency tables, and nothing else (its `imports` and `exports` name paths). Each
    file is read once; what was read is recorded in `configs` (see `Reach`), absent files too."""
    out = {}
    for folder in dict.fromkeys((directory, base)):
        path = os.path.join(folder, "package.json")
        configs[path], raw = _config_read(path)
        if raw is None:
            if configs[path] == CONFIG_UNREADABLE and os.path.isfile(path):
                raise Unproven("unreadable_file")
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise Unproven("unmodelled_config")
        if not isinstance(data, dict) or data.get("workspaces"):
            raise Unproven("unmodelled_config")
        kept = {key: data[key] for key in _PACKAGE_KEEP if isinstance(data.get(key), str)}
        for field in _PACKAGE_DEPENDENCIES:
            table = data.get(field)
            if table is None:
                continue
            if not isinstance(table, dict) or any(not isinstance(v, str) or v.startswith(_PACKAGE_LOCAL) for v in table.values()):
                raise Unproven("unmodelled_config")
            kept[field] = table
        rel = lexical_path(base, folder)
        out["" if folder == base else (rel if rel is not None else "")] = kept
    return out


def prove_reach(base, entry, config, allow):
    """Walk every module Deno would load from `entry` (an absolute path), following the imports of each local file whether or not
    an index holds it, and return a `Reach`; raise `Unproven` when that cannot be shown. Deno is to be run only on the mirror
    (`Reach.materialize`) this proves, never on the project.

    Allowed means: inside `base` (by name and with symlinks followed), `allow(rel)` true for both names, readable, and small enough
    to scan. Followed: static imports and re-exports, `import("literal")`, `require` in `import x = require("y")`, the pragmas of
    `scan_module`, the `types` and `jsxImportSource` of the config's `compilerOptions`, the import map and its scopes, every
    spelling of a specifier (`spec_variants`). A specifier that names a refused or outside path is refused even when no file is
    there. Refused outright, because the walk cannot say what Deno would load: a config with `importMap`, `workspace`, `links`,
    `patch`, `vendor`, `sloppy-imports`, a `nodeModulesDir` of `manual`, or a map it cannot read or whose keys are paths or
    whose targets are `file:` URLs (`unmodelled_config`); a local `package.json` dependency (`unmodelled_config`); an `import(expr)`
    or `import.meta.resolve` anywhere in the graph (`unmodelled_import`); a source file that cannot be read or is over MAX_BYTES
    (`unreadable_file`); a graph over MAX_REACH_FILES files (`too_many_files`).
    The walk is a first check, not the only one: a file it misses is not in the mirror, so Deno cannot read it.
    The index is not used: nothing here depends on it being built."""
    base = os.path.realpath(base)
    reach = Reach(base)
    config_dir = os.path.dirname(config)
    roots = []  # (specifier, referrer) pairs the config itself makes Deno load
    config_rel = lexical_path(base, os.path.realpath(config_dir))
    if config_rel is None and os.path.realpath(config_dir) != base:
        raise Unproven("unmodelled_config")  # a config directory outside the project
    reach.config_rel = posixpath.join(config_rel or "", os.path.basename(config))
    if os.path.isfile(config):
        if lexical_path(base, os.path.realpath(config)) is None:
            raise Unproven("unmodelled_config")
        reach.configs[config], raw = _config_read(config)
        if raw is None:
            raise Unproven("unreadable_file")
        imap, details = load_config(config, raw)
        if details.get("error") or details["unmodelled"]:
            raise Unproven("unmodelled_config")
        reach.config_data = details["data"]
        if reach.config_data.get("nodeModulesDir") not in (None, "auto", "none", True, False):
            raise Unproven("unmodelled_config")  # "manual" reads a node_modules directory the mirror does not have
        options = details["data"].get("compilerOptions")
        if options is not None and not isinstance(options, dict):
            raise Unproven("unmodelled_config")
        options = options or {}
        types = options.get("types", [])
        if not isinstance(types, list) or not all(isinstance(item, str) for item in types):
            raise Unproven("unmodelled_config")
        roots += [(item, config) for item in types]
        for field in ("jsxImportSource", "jsxImportSourceTypes"):
            source = options.get(field)
            if source is not None and not isinstance(source, str):
                raise Unproven("unmodelled_config")
            if source:
                roots += [(source + suffix, config) for suffix in ("", "/jsx-runtime", "/jsx-dev-runtime")]
    else:
        reach.configs[config] = _config_state(config)
        imap = ImportMap({}, config_dir)  # the mirror gets a config of its own, so Deno looks for none above it
    reach.packages = _check_package_json(base, config_dir, reach.configs)
    reach.entry = lexical_path(base, os.path.normpath(entry))
    queue, seen = [os.path.normpath(entry)], set()
    for spec, referrer in roots:
        queue += _local_targets(spec, referrer, imap, base, allow)
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        lexical, real = admit(base, path, allow)
        if not os.path.isfile(path):
            continue
        if len(reach.files) >= MAX_REACH_FILES:
            raise Unproven("too_many_files")
        data = _read_capped(path)
        if data is None:
            raise Unproven("unreadable_file")
        reach.files[lexical] = (real, _fingerprint(data))
        reach.contents[lexical] = data
        if not lexical.endswith(SOURCE_EXTENSIONS):
            continue
        scanned = _scan_cached(data)
        if scanned["unmodelled"]:
            raise Unproven("unmodelled_import")
        specs = [item["spec"] for item in scanned["specs"]]
        for pragma in scanned["pragmas"]:
            specs += [pragma, pragma + "/jsx-runtime", pragma + "/jsx-dev-runtime"]
        for spec in specs:
            queue += _local_targets(spec, path, imap, base, allow)
    return reach


def _local_targets(spec, importer, imap, base, allow):
    """The paths `spec` may name, each shown allowed (raises Unproven otherwise), for the walk to follow."""
    out = named_paths(spec, importer, imap)
    for path in out:
        admit(base, path, allow)
    return out


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
                out.append(self._edge(rel, spec, line, type_only, dynamic, base, None, imap))
                for name, omap in overlay_maps:
                    alt = resolve(spec, importer, omap, isfile)
                    if alt[:2] != base[:2]:
                        out.append(self._edge(rel, spec, line, type_only, dynamic, alt, name, omap))
            edges[rel] = out
            for edge in out:
                if edge["kind"] == "file":
                    reverse.setdefault(edge["to"], []).append(edge)
        for items in reverse.values():
            items.sort(key=lambda e: (e["from"], e["line"], e["config"] or ""))
        self.edges, self._reverse = edges, reverse

    def _edge(self, rel, spec, line, type_only, dynamic, resolved, config, imap):
        kind, target, reason = resolved
        if kind == "file":
            inside, why = self._judge(target)
            if inside is None:
                kind, target, reason = "unresolved", None, why
            else:
                target = inside
        if kind == "unresolved" and self._names_forbidden(spec, self._abs(rel), imap):
            spec = ""  # the specifier is a path the guard refuses (or one outside the project): the answer must not say which
        return {"from": rel, "line": line, "spec": spec, "kind": kind, "to": target, "reason": reason, "type_only": bool(type_only),
                "dynamic": bool(dynamic), "config": config}

    def _names_forbidden(self, spec, importer, imap):
        """True when `spec` names, in any spelling Deno's URL parser accepts, a path outside the project or one `allow` refuses,
        whether or not a file is there."""
        for path in named_paths(spec, importer, imap):
            try:
                admit(self.base, path, self._allowed)
            except Unproven:
                return True
        return False

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
