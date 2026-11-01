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

THE PATH RULE, for this module and nav_api.py (a test scans both for any file-system call and holds each to a table): no file-system
call (open, stat, lstat, listing, readlink, realpath, a mkdir, a write or a removal) is made at a path until its name has been shown
inside the boundary and allowed by the guard. Links are followed only by `judged_realpath`, which judges each link's target by name
before it looks at it; bytes are read only by `read_bounded`; a cache lives only in a `CacheDir`, a folder inside the project judged
once like any other path; files and folders are made and removed only relative to a descriptor opened with no link on the way
(`write_new`, `open_folder_below`, `make_own_folder`, `remove_own_folder`). A location not shown allowed is refused, and the refusal
does not depend on whether anything is there. Places outside any project (what the sandbox grants: Deno's cache and executable, the
`PATH` folders `deno` is looked up in, the folder scratch copies are made in) are resolved the same way, by nav_api's
`judged_location`: `judged_realpath` with `/` as the boundary and every sensitive name (`.ssh`, `.git`, ...) refused. The closed list
of calls outside the rule: the realpath of the boundary itself (the project root a caller passes), the check that the fixed
`sandbox-exec` exists, and the existence probes for monorepo markers (`.git`, workspace files) by exact name in the project and the
folders above it (`_guarded_roots`).

The scanner is a tokenizer, not a parser: it skips comments, strings, template literals and regular-expression literals, so an
`import` inside any of them is not an edge, then matches the import and re-export forms on the tokens."""
from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import secrets
import shutil
import stat
import time
from urllib.parse import unquote

INDEX_VERSION = 2  # 2: an entry is only ever made from a read that succeeded (a failed read used to be saved as a file with no imports)
SOURCE_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
SKIP_DIRS = frozenset({"node_modules", ".git", ".tmux-pi", ".deno"})
MAX_BYTES = 2_000_000
MAX_CACHE_BYTES = 64_000_000  # an index or doc cache file; larger is taken as no cache
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


def load_config(path, raw, error=""):
    """(ImportMap, details) from the bytes `raw` of the Deno config file `path` (read by the caller with `read_bounded`; this
    function reads nothing); `raw` None gives an empty map and says why in `details` (`error`, or "unreadable").

    `details`: `exclude`, `ignored` (what the index does not follow), `unmodelled` (why the walk that guards Deno cannot trust the
    map: a list of short reasons, empty when it can), `data` (the parsed config, {} when unreadable) and `error`. `raw` is the
    file's bytes."""
    details = {"path": path, "exclude": [], "ignored": [], "unmodelled": [], "data": {}}
    if raw is None:
        details["error"] = error or "unreadable"
        return ImportMap({}, os.path.dirname(path)), details
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as failure:
        details["error"] = type(failure).__name__
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


def resolve(spec, importer, import_map, isfile):
    """How Deno resolves `spec` written in `importer` (absolute path): (kind, target, reason). `isfile(path)` is the caller's
    judgement of a path (no default: the index passes one that judges the name before it looks, see `ImportIndex._verdict`).

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
    if full == base or not full.startswith(base.rstrip(os.sep) + os.sep):  # `/` as `base`: every other absolute path is inside
        return None
    return os.path.relpath(full, base).replace(os.sep, "/")


NOFOLLOW_ANY = getattr(os, "O_NOFOLLOW_ANY", 0) or os.O_NOFOLLOW  # macOS: no link anywhere on the path; else the last name only
DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)  # a descriptor on a folder, to list it or
# to make and rename in it: no bytes of a file are read through one
MAX_LINK_HOPS = 40


def lstat_nofollow(path):
    """`os.lstat(path)` with no link followed anywhere on the way: the folder holding it is opened with `NOFOLLOW_ANY` and the name
    is looked at relative to it, so a folder on the way swapped for a link gives OSError (ELOOP) and nothing past the link is
    looked at. (Linux has no `O_NOFOLLOW_ANY`: there only the folder's own name is held to it.)"""
    return _look(path)[0]


def _look(path, link=False):
    """(lstat of `path`, its link target when `link` and it is a link, else None), both relative to a descriptor on its folder
    opened with no link on the way (see `lstat_nofollow`)."""
    parent, name = os.path.split(path)
    fd = os.open(parent, DIR_FLAGS | NOFOLLOW_ANY)
    try:
        info = os.lstat(name, dir_fd=fd)
        return info, (os.readlink(name, dir_fd=fd) if link and stat.S_ISLNK(info.st_mode) else None)
    finally:
        os.close(fd)


class CannotJudge(Exception):
    """Raised by an `allow` guard that could not decide about a name because it could not look at the file system (the guard's
    `unreadable_path`): not a verdict on the name. Anything that is not the index treats it as a refusal (`_allows`); the index
    (`ImportIndex._allowed`) records a gap, since a name it cannot judge is a file it may not have indexed."""


def _allows(allow, rel):
    """`allow(rel)` as a bool; a guard that raises refuses."""
    try:
        return bool(allow(rel))
    except Exception:
        return False


def _allow_any(rel):
    return True


def judged_realpath(base, path, allow):
    """(lexical, real): the `base`-relative posix names of `path` (absolute, or relative to `base`, a resolved directory) as
    written and with its links followed; raises Unproven `outside_project`, `reaches_refused_path` or `unreadable_file` (a name
    that cannot be looked at, or a link loop).

    THE ONE WAY these modules follow a link (a test scans both files for `os.path.realpath` outside the few places that resolve a
    boundary itself). Each name is judged before the file system is asked anything about it: the written name must be inside `base`
    and allowed before any of its components is looked at, and each link's target, joined to what is left of the path, must be
    inside `base` and allowed before anything at it is looked at. So resolving never stats, lists or reads a location outside `base`
    or one `allow` refuses, which `os.path.realpath` would do for whatever a link names. Past a name that does not exist, the rest is
    taken as written."""
    return _judge(base, path, allow)[:2]


def _judge(base, path, allow):
    """`judged_realpath`, and a third value: True when the last name was seen to be there (every name on the way was looked at and
    found), False when the walk stopped at a name that was not there. A caller that then finds nothing at the name knows whether
    it was never there or went away after it was judged (a race)."""
    lexical = lexical_path(base, path)
    if lexical is None:
        raise Unproven("outside_project")
    if not _allows(allow, lexical):
        raise Unproven("reaches_refused_path")
    parts, at, hops, seen = lexical.split("/"), 0, 0, True
    while at < len(parts):
        here = os.path.join(base, *parts[:at + 1])
        try:
            info, target = _look(here, link=True)  # the folders before it were just seen to be no links, and still are
        except (FileNotFoundError, NotADirectoryError):
            seen = False
            break  # nothing there: the rest is a name, resolved as written
        except OSError:
            raise Unproven("unreadable_file")  # a link loop, or a folder on the way swapped for a link (ELOOP), or no access
        if not stat.S_ISLNK(info.st_mode):
            at += 1
            continue
        hops += 1
        if target is None:
            raise Unproven("unreadable_file")
        if hops > MAX_LINK_HOPS:
            raise Unproven("unreadable_file")
        rel = lexical_path(base, os.path.join(os.path.join(base, *parts[:at]), target, *parts[at + 1:]))
        if rel is None:
            raise Unproven("outside_project")
        if not _allows(allow, rel):
            raise Unproven("reaches_refused_path")
        parts, at = rel.split("/"), 0  # the parts before `at` held no link, so `..` in the target was resolved by name correctly
    return lexical, "/".join(parts), seen


def project_path(base, path, allow=None):
    """`lexical_path`, and also None when the path, with its symlinks followed, leaves `base` (or names a path `allow` refuses).
    The relative path returned is the one written, not where a symlink inside the project leads. Links are followed by
    `judged_realpath`, so nothing outside `base` is looked at."""
    try:
        return judged_realpath(base, path, allow or _allow_any)[0]
    except Unproven:
        return None


def _opened_path(fd):
    """The path of the file open on `fd` as the kernel names it (macOS `F_GETPATH`, Linux `/proc/self/fd`), or None when that cannot
    be asked: then no read is taken to be of a checked file."""
    try:
        if hasattr(fcntl, "F_GETPATH"):
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


READ_OK = "ok"


def read_bounded(path, admitted, boundary, allow, limit=None, expect=None, take=True):
    """(status, bytes or None, opened): THE ONE WAY this module and `nav_api` read the bytes of a file (a test scans both files
    for any other). `admitted` is where the caller judged `path` to lead (a path with no link on it, from `judged_realpath`).
    In this order: (a) both names are inside `boundary` (a resolved directory) and `allow(rel)` is true for both (a guard that
    raises refuses); nothing is asked of the file system before that; (b) `admitted` is opened, read only, never through a link
    (`NOFOLLOW_ANY`); (c) the kernel is asked which file is open (`_opened_path`), and it must be `admitted`; (d) when `path` is
    another name, `judged_realpath` must still lead from it to `admitted`; (e) it must be a regular file of at most `limit` bytes
    (default MAX_BYTES); (f) only then is it read. Nothing of a file that fails a check is read, and nothing is opened at a name
    that fails (a).

    status: `ok`; `absent` (nothing at `admitted`); `unreadable` (cannot be opened, or is not a regular file, or the kernel cannot
    name it, or is over `limit`); `changed` (a link is now on the way to `admitted`, or `path` no longer leads there: something
    moved after the caller judged the name); `outside` (not inside `boundary`); `refused` (the guard refuses it). `opened` is the
    kernel's path, None when not known.

    `expect` ([mtime_ns, size], the key of the regular file the caller saw): the open file (`fstat`, at step (e), before the type
    and size checks) must be a regular file with this signature, else `changed`: the bytes are those of the version the caller
    keyed, or none, and a folder, FIFO or bigger file in its place is a change, not a file that cannot be read. `take` False: every check is made and nothing is read (`ok` with no bytes):
    the proof that a file is there, readable and unchanged."""
    admitted, path = os.path.normpath(admitted), os.path.normpath(path)
    limit = MAX_BYTES if limit is None else limit
    names = [lexical_path(boundary, admitted), lexical_path(boundary, path)]
    if None in names:
        return "outside", None, None
    for rel in dict.fromkeys(names):
        if not _allows(allow, rel):
            return "refused", None, None
    try:
        fd = os.open(admitted, os.O_RDONLY | os.O_NONBLOCK | NOFOLLOW_ANY | getattr(os, "O_CLOEXEC", 0))
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            return "absent", None, None
        return ("changed" if error.errno == errno.ELOOP else "unreadable"), None, None
    except ValueError:
        return "unreadable", None, None
    try:
        opened = _opened_path(fd)
        if opened is None:
            return "unreadable", None, None
        if opened != admitted:
            return "changed", None, opened
        if path != admitted:
            try:
                _, real = judged_realpath(boundary, path, allow)
            except Unproven:
                return "changed", None, opened
            if os.path.join(boundary, real) != admitted:
                return "changed", None, opened
        try:
            info = os.fstat(fd)
        except OSError:
            return "unreadable", None, opened
        if expect is not None and not (stat.S_ISREG(info.st_mode) and [info.st_mtime_ns, info.st_size] == list(expect)):
            return "changed", None, opened  # not the regular file the caller keyed: it moved (a folder, FIFO, another version)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return "unreadable", None, opened
        if not take:
            return READ_OK, None, opened
        chunks, left = [], limit + 1
        try:
            while left > 0:  # (d): every check above has passed
                chunk = os.read(fd, min(left, 1 << 20))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        except OSError:
            return "unreadable", None, opened
        data = b"".join(chunks)
        if len(data) > limit:
            return "unreadable", None, opened
        return READ_OK, data, opened
    finally:
        os.close(fd)


def write_new(path, data, dir_fd=None):
    """Write `data` to `path` (relative to the directory open on `dir_fd`, when given) as a new regular file (never through a link,
    anywhere on the way: `NOFOLLOW_ANY`; never over an existing file): the one way this module and `nav_api` write a file. Raises
    OSError when `path` exists or a folder on the way is a link (a mirror folder swapped for one after it was made)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW_ANY | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def open_folder_below(base, parts):
    """A descriptor on the folder `base`/`parts`..., each part made (mode 0700) where missing: `base` is opened with no link on the
    way (`NOFOLLOW_ANY`) and each part is made with `mkdir` and opened with `O_NOFOLLOW` relative to the one before, so no link is
    followed anywhere and nothing is made through one. Raises OSError (a link on the way: ELOOP or ENOTDIR)."""
    fd = os.open(base, DIR_FLAGS | NOFOLLOW_ANY)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            inner = os.open(part, DIR_FLAGS | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = inner
    except BaseException:
        os.close(fd)
        raise
    return fd


CACHE_REFUSALS = {"outside_project": "cache_dir_outside_project", "reaches_refused_path": "cache_dir_refused",
                  "unreadable_file": "cache_dir_unreadable"}


class CacheDir:
    """The folder a cache lives in, judged once, like any other project path, before anything in it is read or written.

    The folder must be inside the project `base` (not `base` itself), and `allow` must allow its name and, followed through its
    links by `judged_realpath`, where it leads; it is then kept as that resolved path and never resolved again. Otherwise there is
    no cache, and `refusal` names why: `cache_dir_outside_project`, `cache_dir_refused` (the guard refuses the folder: `.git`, a
    sensitive name, or a link that leads to one), `cache_dir_unreadable`, `cache_dir_unguarded` (no guard was given, so nothing
    shows the folder allowed). A folder outside the project is refused rather than judged some other way: the guard can only
    judge the project.

    Each cache file is a plain `.json` name directly in the folder (no `/`, not hidden), read with `read_bounded` (boundary `base`,
    the guard judging the file's project-relative name as well) and written through a descriptor on the folder, made one component
    at a time below `base` with no link followed and checked to be the judged folder, so a folder swapped for a link after the
    judgement is neither read nor written."""

    def __init__(self, base, folder, allow):
        self.base = base
        self.allow = allow
        self.rel = self.path = self.refusal = None
        if allow is None:
            self.refusal = "cache_dir_unguarded"
            return
        try:
            self.rel = judged_realpath(base, folder, allow)[1]
        except Unproven as error:
            self.refusal = CACHE_REFUSALS.get(error.reason, "cache_dir_refused")
            return
        self.path = os.path.join(base, *self.rel.split("/"))

    @staticmethod
    def _plain(name):
        return isinstance(name, str) and "/" not in name and os.sep not in name and "\0" not in name and not name.startswith(".") \
            and name.endswith((".json", ".tmp"))

    def _allow_file(self, rel):
        return posixpath.dirname(rel) == self.rel and self._plain(posixpath.basename(rel)) and _allows(self.allow, rel)

    def read_json(self, name):
        """The JSON value in the cache file `name`, or None (no cache, refused, absent, a link, not JSON, over MAX_CACHE_BYTES)."""
        if self.refusal or not self._plain(name) or not name.endswith(".json"):
            return None
        full = os.path.join(self.path, name)
        status, data, _ = read_bounded(full, full, self.base, self._allow_file, limit=MAX_CACHE_BYTES)
        if status != READ_OK:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            return None

    def _open_folder(self):
        """A descriptor on the cache folder, made where missing (`open_folder_below`), or None; it must be the judged folder by the
        kernel's name."""
        try:
            fd = open_folder_below(self.base, self.rel.split("/"))
        except OSError:
            return None
        if _opened_path(fd) != self.path:
            os.close(fd)
            return None
        return fd

    def write_json(self, name, value):
        """Write `value` to the cache file `name` (a temporary file, then a rename, both in the folder's descriptor). True when
        written; False when there is no cache, the name is refused, a link is at the name, or the write fails (the cache is left as
        it was, and only the next start is cold)."""
        temp = f"{name}.{os.getpid()}.tmp"
        if self.refusal or not self._plain(name) or not name.endswith(".json") \
                or not all(self._allow_file(posixpath.join(self.rel, item)) for item in (name, temp)):
            return False
        fd = self._open_folder()
        if fd is None:
            return False
        try:
            try:
                if stat.S_ISLNK(os.lstat(name, dir_fd=fd).st_mode):
                    return False  # never replace a link (the rename would not follow it, but it is not ours to remove)
            except FileNotFoundError:
                pass
            try:
                os.unlink(temp, dir_fd=fd)  # left by a crashed run of this process id; unlink never follows a link
            except FileNotFoundError:
                pass
            write_new(temp, json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"), dir_fd=fd)
            os.rename(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            return True
        except OSError:
            try:
                os.unlink(temp, dir_fd=fd)
            except OSError:
                pass
            return False
        finally:
            os.close(fd)


def read_source(path, base, allow, failures=None, expect=None):
    """The text of `path` (a resolved path with no link on the way: the index's walk does not follow links), or None when
    `read_bounded` does not give it (refused, outside `base`, a link swapped in after the walk, over MAX_BYTES, or, with `expect`,
    not the version the walk keyed). When `failures` is a dict, the `read_bounded` status of a read that failed is kept in it under
    `path`: the caller says why a file is missing."""
    status, data, _ = read_bounded(path, path, base, allow, expect=expect)
    if status != READ_OK:
        if failures is not None:
            failures[path] = status
        return None
    return data.decode("utf-8", "replace")


def read_sources(paths, base, allow, workers=8, failures=None, keys=None):
    """{path: text or None} for each path (`failures`: see `read_source`; `keys`: {path: [mtime_ns, size]}, the signature each read
    must find, see `read_bounded`'s `expect`). Reading is the slow part of a build (many small files, and a cold disk); it is done
    in threads, and the CPU work of scanning stays in the caller."""
    paths, keys = list(paths), keys or {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(paths) or 1))) as pool:
        return dict(zip(paths, pool.map(lambda path: read_source(path, base, allow, failures, keys.get(path)), paths)))


def probe_sources(keyed, base, allow, workers=8):
    """{path: `read_bounded` status} for each (path, [mtime_ns, size]) in `keyed`: every check of a read (inside `base`, allowed,
    opened with no link on the way, the kernel's name, a regular file within MAX_BYTES, its open signature the given one) and no
    byte read. `ok` proves the file is there, can be read and is the version keyed; in threads, as `read_sources`."""
    keyed = list(keyed)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(keyed) or 1))) as pool:
        return dict(zip([path for path, _ in keyed], pool.map(
            lambda item: read_bounded(item[0], item[0], base, allow, expect=item[1], take=False)[0], keyed)))


# ------------------------------------------------------------------------------------------------------- the allow-list walk

MAX_REACH_FILES = 3000
_PACKAGE_LOCAL = ("file:", "link:", "portal:", "workspace:")
_SCAN_MEMO = {}


class Unproven(Exception):
    """The walk cannot show that Deno would load only allowed files. `reason` is one of `outside_project`, `reaches_refused_path`,
    `unmodelled_config`, `unmodelled_import`, `unreadable_file`, `too_many_files`, `changed_during_walk` (the file opened was not
    the one the walk had checked: a link moved in between)."""

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
    refuses). Judged by name whether or not a file is there, and through `judged_realpath`, so nothing outside `base` or refused
    is looked at on the way. `unreadable_file` when a name on the way cannot be looked at."""
    return judged_realpath(base, path, allow)


def _fingerprint(data):
    return hashlib.sha256(data).hexdigest()


CONFIG_ABSENT = ("absent",)
CONFIG_UNREADABLE = ("unreadable",)
CONFIG_OUTSIDE = ("outside",)
CONFIG_REFUSED = ("refused",)
CONFIG_CHANGED = ("changed",)


_JUDGED_STATE = {"outside_project": "outside", "reaches_refused_path": "refused", "unreadable_file": "unreadable"}


def _config_read(path, base, allow):
    """(state, bytes or None): the state of the config file `path` (see `_config_state`) and the bytes it was made from. `path` is
    judged now (`judged_realpath`: by name, inside the project `base`, allowed by `allow`, its links followed only inside), and the
    file it leads to is read with one `read_bounded`. A config that leads outside the project, or that the guard refuses, is never
    opened, and no location outside the project is looked at to find that out."""
    try:
        _, real, seen = _judge(base, path, allow)
    except Unproven as error:
        return (_JUDGED_STATE.get(error.reason, "unreadable"),), None
    status, data, opened = read_bounded(path, os.path.join(base, *real.split("/")), base, allow)
    if status == "absent" and seen:
        return CONFIG_CHANGED, None  # it was there when its name was judged and is gone at the read: a race, not "no config"
    if status != READ_OK:
        return (status,), None
    return ("content", opened, _fingerprint(data)), data


def judged_is_file(base, path, allow):
    """True when the judged `path` leads to a regular file (looked at only after `judged_realpath` passes)."""
    try:
        real = judged_realpath(base, path, allow)[1]
        return stat.S_ISREG(lstat_nofollow(os.path.join(base, *real.split("/"))).st_mode)
    except (Unproven, OSError):
        return False


def judged_is_dir(base, path, allow):
    """True when `path` is `base` itself or, judged like `judged_is_file`, leads to a directory inside it."""
    if os.path.normpath(path) == base:
        try:
            return stat.S_ISDIR(os.lstat(base).st_mode)  # the boundary itself, resolved by the caller
        except OSError:
            return False
    try:
        real = judged_realpath(base, path, allow)[1]
        return stat.S_ISDIR(lstat_nofollow(os.path.join(base, *real.split("/"))).st_mode)
    except (Unproven, OSError):
        return False


def _config_state(path, base, allow):
    """What a config file is now: `CONFIG_ABSENT` (nothing is there), `CONFIG_UNREADABLE` (something is there and it cannot be read
    as a file, or is over MAX_BYTES), `CONFIG_OUTSIDE` (it leads out of `base`), `CONFIG_REFUSED` (the guard refuses it),
    `CONFIG_CHANGED` (a link moved while it was opened) or `("content", resolved path, fingerprint)`. Only `content` was read.
    Every writer and reader of a config state goes through here, so "there was none" and "there is one I cannot read" are never
    the same value."""
    return _config_read(path, base, allow)[0]


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

    def __init__(self, base, allow):
        self.base = base
        self.allow = allow
        self.files = {}
        self.contents = {}
        self.entry = None
        self.config_rel = None
        self.config_data = {}
        self.packages = {}
        self.configs = {}
        self.boundary_holds = None  # () -> bool: does the choice of `base` (the monorepo root, its .git and marker) still hold?

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
        if self.boundary_holds is not None and not self.boundary_holds():
            return False
        for rel, (real, fingerprint) in self.files.items():
            status, data, _ = read_bounded(os.path.join(self.base, rel), os.path.join(self.base, real), self.base, self.allow)
            if status != READ_OK or _fingerprint(data) != fingerprint:
                return False
        return all(_config_state(path, self.base, self.allow) == state for path, state in self.configs.items())

    def materialize(self, parent):
        """A `Mirror` holding a copy of each proved file and nothing else, under the same relative paths, with a config made from
        `config_data`: the only tree Deno is given."""
        mirror = Mirror(self.base, parent)
        try:
            package_files = {posixpath.join(folder, "package.json")
                             for folder in {posixpath.dirname(self.config_rel), "", *self.packages}}  # the two Deno looks in, and each above
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


def make_own_folder(parent, prefix):
    """A new, empty folder (mode 0700) directly in `parent` (a resolved folder the caller judged), made relative to a descriptor on
    `parent` opened with no link on the way (`NOFOLLOW_ANY`) and checked by the kernel's name (`_opened_path`) to be `parent`:
    so it is made where `parent` was judged to be, never through a link swapped in since, and nothing is resolved (no `realpath`)
    to find that out. Returns its path; raises Unproven(`scratch_refused`) when `parent` is now not that folder (a link on the way)
    and OSError when it cannot be made (no such folder, no access)."""
    try:
        fd = os.open(parent, DIR_FLAGS | NOFOLLOW_ANY)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise Unproven("scratch_refused")
        raise
    try:
        if _opened_path(fd) != parent:
            raise Unproven("scratch_refused")
        for _ in range(100):
            name = prefix + secrets.token_hex(8)
            try:
                os.mkdir(name, 0o700, dir_fd=fd)
            except FileExistsError:
                continue
            return os.path.join(parent, name)
        raise FileExistsError(errno.EEXIST, "no free scratch folder name", parent)
    finally:
        os.close(fd)


def remove_own_folder(path):
    """Remove a folder `make_own_folder` made, and what is in it, relative to a descriptor on its parent opened with no link on the
    way (`shutil.rmtree` with `dir_fd`, which follows no link inside): a parent swapped for a link since is not removed through
    (the folder is left where it was made). Errors are ignored."""
    parent, name = os.path.split(path)
    try:
        fd = os.open(parent, DIR_FLAGS | NOFOLLOW_ANY)
    except OSError:
        return
    try:
        if _opened_path(fd) == parent:
            shutil.rmtree(name, dir_fd=fd, ignore_errors=True)
    finally:
        os.close(fd)


class Mirror:
    """A temporary directory laid out like the project (same relative paths, so a relative import and the config's `./` targets
    mean the same), holding copies of the files the walk proved and a rewritten config. A file the walk did not prove is not in it,
    so an import the scanner missed, or an import map entry the walk misread, resolves to nothing instead of to a refused file.
    Files are written as new regular files, never as links. `root` is its resolved path; `cleanup()` removes it. `parent` is
    required: a resolved folder the caller has judged (nav_api's `_scratch_parent`), never the system default chosen here; the
    mirror is made directly in it by `make_own_folder`, or not at all (`scratch_refused`)."""

    def __init__(self, base, parent):
        self.base = base
        if not parent:
            raise Unproven("scratch_refused")  # no judged folder to make it in
        self.root = make_own_folder(parent, "tp-ctx-mirror-")  # a folder this process just made, where `parent` was judged
        if self.root == base or self.root.startswith(base + os.sep) or base.startswith(self.root + os.sep):
            self.cleanup()
            raise Unproven("unmodelled_config")  # a mirror inside the project (or around it) would put refused files in Deno's reach

    def path(self, rel):
        return os.path.join(self.root, *rel.split("/")) if rel else self.root

    def write(self, rel, data):
        """A new file at `rel` (a posix path) in the mirror, its folders made where missing, with no link followed on the way
        (`open_folder_below`, `write_new` relative to it): a mirror folder swapped for a link is not written through."""
        parts = rel.split("/")
        fd = open_folder_below(self.root, parts[:-1])
        try:
            write_new(parts[-1], data, dir_fd=fd)
        finally:
            os.close(fd)

    def write_json(self, rel, value):
        self.write(rel, json.dumps(value, sort_keys=True).encode("utf-8"))

    def to_real(self, path):
        """The project-relative posix name of `path` when it is a file of the mirror, else None (Deno named a path outside the mirror)."""
        full = os.path.normpath(path)
        if full == self.root or not full.startswith(self.root + os.sep):
            return None
        return os.path.relpath(full, self.root).replace(os.sep, "/")

    def cleanup(self):
        remove_own_folder(self.root)


_PACKAGE_KEEP = ("name", "version", "type")
_PACKAGE_DEPENDENCIES = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


_CONFIG_REFUSAL = {"outside": "outside_project", "refused": "reaches_refused_path", "changed": "changed_during_walk"}


# Files that define a workspace in a form this module does not model; one in a folder Deno looks in refuses the call. (`turbo.json` and
# `nx.json` define none: they read the `package.json` `workspaces`, which is modelled below.)
UNMODELLED_WORKSPACE_FILES = ("pnpm-workspace.yaml", "lerna.json", "rush.json")
_WORKSPACE_GLOB = frozenset("*?[]{}!()\\\0")


def check_workspaces(base, folder, value, allow):
    """Raises Unproven(`unmodelled_config`) unless the `workspaces` of the `package.json` in `folder` is a form that is modelled: the
    one form this module supports is npm's list of plain folder paths (`["apps/web", "./apps/e2e"]`): non-empty strings, no glob
    character, no `..`, each a folder strictly below `folder` and inside `base`, and allowed by `allow` by its name. An empty or
    absent key is no workspace (the caller does not call this); a key that is present must hold such a list and nothing else. Anything
    else (null, false, 0, an empty string, an empty list, an empty object, an object with `packages`, a mixed list, a glob, a
    negation, a path out of `folder`, a name the guard refuses) is refused, never guessed. Nothing is looked at: members are names; the mirror's `package.json` has
    no `workspaces`, so Deno is given no member, and a bare specifier that names one resolves to nothing there."""
    if not isinstance(value, list) or not value:
        raise Unproven("unmodelled_config")
    for item in value:
        if not isinstance(item, str) or not item.strip() or any(ch in _WORKSPACE_GLOB for ch in item) or item.startswith("/") \
                or ".." in item.split("/"):
            raise Unproven("unmodelled_config")
        member = lexical_path(base, os.path.join(folder, item))
        if member is None or member == (lexical_path(base, folder) or "") or not _allows(allow, member):
            raise Unproven("unmodelled_config")


def _folders_to_base(base, directory):
    """`directory` and every folder above it up to `base` (inclusive), nearest first: the folders Deno looks in for a
    `package.json`. Names only."""
    out, folder = [], os.path.normpath(directory)
    while True:
        out.append(folder)
        if folder == base or lexical_path(base, folder) is None:
            return out
        folder = os.path.dirname(folder)


def _check_package_json(base, directory, configs, allow):
    """Deno reads the `package.json` next to its config, and looks above it. Every folder from the config's up to the boundary `base`
    is looked at (the boundary is the project root, or the monorepo root when the project is in one). A dependency that is a local
    path (`file:`, `link:`, a workspace) loads code from somewhere the walk does not look: refused. A `package.json` that is not
    JSON is refused too, and so is a `workspaces` that `check_workspaces` does not model, and a `pnpm-workspace.yaml`, `lerna.json`
    or `rush.json` in any of the folders. Returns {folder: what the mirror gets} for the folders that have a `package.json` (`""` is
    the boundary, others are boundary-relative posix paths): the package's `name`, `version`, `type` and dependency tables, and
    nothing else (its `imports`, `exports` and `workspaces` name paths). Each file is read once, with `read_bounded` (one that leads
    outside the boundary or that `allow` refuses is never read, and refuses the call); what was read is recorded in `configs` (see
    `Reach`), absent files too."""
    out = {}
    for folder in _folders_to_base(base, directory):
        for name in UNMODELLED_WORKSPACE_FILES:
            path = os.path.join(folder, name)
            configs[path], _ = _config_read(path, base, allow)
            if configs[path] != CONFIG_ABSENT:  # there, whatever it holds: a workspace form that is not modelled (or not readable)
                state = configs[path][0]
                raise Unproven(_CONFIG_REFUSAL.get(state, "unmodelled_config" if state == "content" else "unreadable_file"))
        path = os.path.join(folder, "package.json")
        configs[path], raw = _config_read(path, base, allow)
        if raw is None:
            if configs[path][0] in _CONFIG_REFUSAL:
                raise Unproven(_CONFIG_REFUSAL[configs[path][0]])
            if configs[path] == CONFIG_UNREADABLE:
                raise Unproven("unreadable_file")  # something is there (a FIFO, a folder, too big): Deno would see it, the mirror not
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise Unproven("unmodelled_config")
        if not isinstance(data, dict):
            raise Unproven("unmodelled_config")
        if "workspaces" in data:  # present means present: null, false, 0, "" and {} are forms this module does not model
            check_workspaces(base, folder, data["workspaces"], allow)
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
    (`unreadable_file`); a graph over MAX_REACH_FILES files (`too_many_files`); a file whose open leads somewhere other than the
    path that was judged, because a link moved in between (`changed_during_walk`).
    The walk is a first check, not the only one: a file it misses is not in the mirror, so Deno cannot read it.
    The index is not used: nothing here depends on it being built."""
    base = os.path.realpath(base)  # the boundary itself
    reach = Reach(base, allow)
    config_dir = os.path.normpath(os.path.dirname(config))
    if config_dir != base:
        try:
            config_dir = os.path.join(base, *judged_realpath(base, config_dir, allow)[1].split("/"))
        except Unproven as error:
            raise Unproven("unmodelled_config" if error.reason == "outside_project" else error.reason)  # a folder outside the project
    config = os.path.join(config_dir, os.path.basename(config))  # one spelling: the configs are read and recorded under it
    roots = []  # (specifier, referrer) pairs the config itself makes Deno load
    config_rel = lexical_path(base, config_dir)
    reach.config_rel = posixpath.join(config_rel or "", os.path.basename(config))
    try:
        judged_realpath(base, config, allow)  # a config that leads out of the project is not modelled
    except Unproven as error:
        raise Unproven("unmodelled_config" if error.reason == "outside_project" else error.reason)
    if judged_is_file(base, config, allow):
        reach.configs[config], raw = _config_read(config, base, allow)
        if raw is None:
            state = reach.configs[config][0]
            if state == "outside":
                raise Unproven("changed_during_walk")  # it was inside when checked above: a link moved in between
            raise Unproven(_CONFIG_REFUSAL.get(state, "unreadable_file"))
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
        reach.configs[config] = _config_state(config, base, allow)
        state = reach.configs[config][0]
        if state in ("content", "outside"):
            raise Unproven("changed_during_walk")  # none was there (or it was inside) when checked above, and that moved
        if state != "absent":
            raise Unproven(_CONFIG_REFUSAL.get(state, "unreadable_file"))  # something is there (a FIFO, a folder): fail closed
        imap = ImportMap({}, config_dir)  # the mirror gets a config of its own, so Deno looks for none above it
    reach.packages = _check_package_json(base, config_dir, reach.configs, allow)
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
        try:
            info = lstat_nofollow(os.path.join(base, *real.split("/")))  # where the judged name leads, no link followed
        except (FileNotFoundError, NotADirectoryError):
            continue  # nothing there: Deno finds nothing either
        except OSError:
            raise Unproven("unreadable_file")  # something is there that cannot be looked at: not "nothing", so nothing is proven
        if stat.S_ISLNK(info.st_mode):
            raise Unproven("changed_during_walk")  # judged with no link there, and one is there now
        if not stat.S_ISREG(info.st_mode):
            continue
        if len(reach.files) >= MAX_REACH_FILES:
            raise Unproven("too_many_files")
        status, data, _ = read_bounded(path, os.path.join(base, real), base, allow)
        if status in ("changed", "outside", "refused"):
            raise Unproven("changed_during_walk")  # the file open is not the one `admit` judged; nothing of it was read
        if status != READ_OK:
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


def respell(path, spelled, real):
    """`path` (absolute, or relative to `spelled`) with the prefix `spelled` (a directory as the caller wrote it) replaced by `real`
    (the same directory resolved), by name alone: a path written under another spelling of the project (`/var` for `/private/var`)
    is judged as the path it names. Nothing is looked at."""
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(spelled, path))
    if spelled != real and (full == spelled or full.startswith(spelled + os.sep)):
        return real + full[len(spelled):]
    return full


_JUDGED_EDGE = {"outside_project": "outside", "reaches_refused_path": "refused"}

# Every way an index can be built from part of its input, by code: (kind, reason). `refused`: a refusal (the answer refuses, as the
# Deno side does for the same config); `changed`: a race, the tree moved while it was being looked at (ask again); `incomplete`:
# something that is there could not be read or used. The split between policy and gap: a file or folder the guard refuses BY ITS
# NAME is outside the index by policy (it is not listed, read or counted, and nothing says it exists); a refusal at READ time, a
# path that turns out to be outside the root, and everything below are gaps.
GAPS = {
    "root_refused": ("refused", "reaches_refused_path"), "root_outside": ("refused", "outside_project"),
    "root_unreadable": ("incomplete", "index_incomplete"),
    "config_refused": ("refused", "reaches_refused_path"), "config_outside": ("refused", "outside_project"),
    "config_changed": ("changed", None), "config_unreadable": ("incomplete", "index_incomplete"),
    "config_invalid": ("incomplete", "unmodelled_config"),  # present but not JSON (comments included) or not an object
    "config_unmodelled": ("incomplete", "unmodelled_config"),  # `importMap`, `patch`, `sloppy-imports`...: resolution the index does not model
    "folder_unreadable": ("incomplete", "index_incomplete"), "folder_changed": ("changed", None),
    "entry_unreadable": ("incomplete", "index_incomplete"), "entry_changed": ("changed", None),
    "guard_unjudged": ("incomplete", "index_incomplete"),  # the guard could not look at a name (`CannotJudge`): neither allowed nor refused
    "source_unreadable": ("incomplete", "index_incomplete"),  # cannot be opened, not a regular file, over MAX_BYTES
    "source_refused": ("incomplete", "index_incomplete"),  # allowed when the walk judged the name, refused or outside at the read
    "source_changed": ("changed", None),  # gone, a link in its place, or another version (signature), at the read or the proof
    "target_unreadable": ("incomplete", "index_incomplete"),  # an import's target could not be looked at: neither a file nor missing
    "target_changed": ("changed", None),  # an import's target is not what it was when the edges were made (`prove`)
}
_ROOT_GAP = {"outside": "root_outside", "refused": "root_refused", "unreadable": "root_unreadable"}
_CONFIG_GAP = {"refused": "config_refused", "outside": "config_outside", "changed": "config_changed",
               "unreadable": "config_unreadable"}
_READ_GAP = {"absent": "source_changed", "changed": "source_changed", "refused": "source_refused", "outside": "source_refused"}


class ImportIndex:
    """`file -> imports` for the source files below `root`, and the reverse map derived from it.

    `base` is the project root: every path in and out is a posix path relative to it, and nothing outside it is read, indexed or
    returned. `allow` is the caller's path guard: a file it refuses is never read or indexed, and an import that resolves to one is
    an `unresolved` edge with reason `refused` whose specifier is blanked (the edge must not name the path); a path that leaves the
    project, by its name or through a symlinked directory, is `unresolved` with reason `outside`, whether or not anything is there.
    `config` is the Deno config whose `imports` table resolves bare specifiers (default `root/deno.json`); `overlays` are further
    configs (for the API, `deno.test.json`, which swaps the Temporal packages for mocks): where one resolves a specifier
    differently from `config`, the extra edge is recorded with `config` set to that file's name, so the importers of a mock module
    are answerable.

    Every path is judged by name before the file system is asked anything about it (`judged_realpath`): `root`, the configs, each
    directory and file the walk meets (a directory the guard refuses is not listed; nothing is followed through a link) and each
    file an import names. The cache is a `CacheDir`: `cache` is (CacheDir, file name), or `cache_path` names the file and its folder
    is judged here; either way, a folder outside the project, refused by the guard, or with no guard to judge it (`allow` None)
    means no cache (`cache_refusal` says why), and the index works without one.

    What is cached, and keyed how: per file the scanned specifiers, keyed by `(mtime_ns, size)`. Resolution is not cached: it is
    string work that costs a fraction of a second for a thousand files, and it has to be redone when a file elsewhere appears (an
    importer of a file that did not exist yet becomes an edge without the importer changing). `refresh()` re-reads only the files
    whose key changed, forgets deleted ones, and returns what it did.

    Gaps. The index never turns what it could not get into an empty value. Whatever it could not read or use is recorded in `gaps`
    ({code: count}, see `GAPS`) by `refresh()`, and `incomplete()` says what that means for an answer: (kind, reason, gaps) or None.
    A folder that cannot be opened or listed, a source that cannot be read (it has no entry, so it is read again next time), an
    entry that cannot be looked at, a config or overlay that is refused, outside, unreadable, not JSON or not modelled, an import
    whose target cannot be looked at, and a root that is refused are gaps; so is a race (a file gone, or a link in its place, between
    the walk and the read). A missing config is not (nothing is there, which the kernel said), nor is a missing import target, nor is
    a file or folder the guard refuses by its name (outside the index by policy). An answer that could depend on a gap is not `ok`:
    `NavApi` is the one place that decides (`_index_answer`).

    The proof (`prove`). What the index holds is never taken as still true because a key matched once: `prove()` looks again at
    every input the edges were made from, and returns the gaps it finds ({} when everything holds). Each folder listing: the walk is
    made again and must find the same files with the same keys. Each source: stat and open-for-read must succeed, with its open
    signature the indexed one (`probe_sources`, no byte read). Each config and overlay: read again, its state and fingerprint must be
    the ones the map was made from (gone, or another one, is `config_changed`; now unreadable or refused is that gap). Each import
    target that was looked at: its verdict must be the same. `refresh()` ends with one (a change during the build is found there);
    `NavApi` makes one more after the answer is built and before it is given. Entries loaded from the cache, entries kept by an
    instance and entries just read get the same proof. Its limit is the key's: a file rewritten with the same mtime_ns and size is
    not seen (the design's `(path, mtime, size)` key), and the cache folder is trusted as the project's sources are."""

    def __init__(self, base, root=None, config=None, overlays=(), cache_path=None, allow=None, cache=None):
        spelled = os.path.abspath(base)
        self.base = os.path.realpath(base)  # the boundary itself
        self.allow = allow  # allow(rel) -> bool: may this project file be read? None allows every file inside the project
        self.root, self.root_refusal = self.base, None
        if root is not None and respell(root, spelled, self.base) != self.base:
            try:
                self.root = os.path.join(self.base, *judged_realpath(self.base, respell(root, spelled, self.base),
                                                                     self._allowed)[1].split("/"))
            except Unproven as error:
                self.root, self.root_refusal = None, _JUDGED_STATE.get(error.reason, "unreadable")
        here = self.root or self.base
        self.config = os.path.join(here, "deno.json") if config is None else respell(config, spelled, self.base)
        self.overlays = [respell(p, spelled, self.base) for p in overlays]
        if cache is not None:
            self._cache, self._cache_name = cache
        elif cache_path:
            cache_path = respell(cache_path, spelled, self.base)
            self._cache, self._cache_name = CacheDir(self.base, os.path.dirname(cache_path), allow), os.path.basename(cache_path)
        else:
            self._cache = self._cache_name = None
        self.cache_refusal = self._cache.refusal if self._cache is not None else None
        self.cache_path = os.path.join(self._cache.path, self._cache_name) if self._cache is not None and not self.cache_refusal \
            else None
        self.entries = {}  # rel -> {"key": [mtime_ns, size], "specs": [[spec, line, type_only, dynamic], ...]}
        self.edges = {}  # rel -> [edge]
        self.meta = {"ignored": [], "config_error": ""}
        self.gaps = {}  # code -> how many times, from the last refresh (see GAPS)
        self._sink = self.gaps  # where `_gap` records: `gaps`, or the proof being made
        self.boundary_holds = None  # () -> bool: does the choice of `base` still hold? `prove` asks (a change is `config_changed`)
        self._configs = []  # [(path, state token)]: each config and overlay as the last refresh read it (`prove` reads it again)
        self._exclude = []  # the config's `exclude`, as the last walk used it
        self._targets = {}  # absolute path -> verdict: each import target the edges were made from (`prove` judges it again)
        self._unread = set()  # sources the last walk found and the build could not read (each already a gap)
        self._reverse = {}
        self._known = set()
        self.refreshed_at = None
        self._load()

    # --- paths

    def normalize(self, path):
        """`path` as a base-relative posix path, or None when it is outside the project or refused (see `project_path`)."""
        return project_path(self.base, path, self._allowed)

    def _abs(self, rel):
        return os.path.join(self.base, rel)

    # --- persistence

    def _load(self):
        if self.cache_path is None:
            return
        data = self._cache.read_json(self._cache_name)
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION or data.get("base") != self.base \
                or data.get("root") != self.root or not isinstance(data.get("entries"), dict):
            return
        for rel, entry in data["entries"].items():
            if isinstance(entry, dict) and isinstance(entry.get("key"), list) and isinstance(entry.get("specs"), list):
                self.entries[rel] = {"key": entry["key"], "specs": entry["specs"]}

    def save(self):
        """Write the entries to the cache (`CacheDir.write_json`: a temporary file, then a rename, in the judged folder). Returns True
        when written; no cache, a symlink at the name, or a failed write leaves the cache as it was and returns False (the index
        still works, only the next start is cold)."""
        if self.cache_path is None:
            return False
        data = {"version": INDEX_VERSION, "base": self.base, "root": self.root, "entries": self.entries}
        return self._cache.write_json(self._cache_name, data)

    # --- building

    def _walk(self, patterns):
        """{rel: [mtime_ns, size]} for the source files below `root`. Each entry is judged by its name (the guard, SKIP_DIRS) before
        anything is asked of it, and then looked at with no link followed: a link is neither indexed nor followed, a directory
        the guard refuses is not listed, and each directory is opened with no link on its path (`NOFOLLOW_ANY`) and must be the
        one judged, by the kernel's name, before it is listed."""
        found = {}
        if self.root is None:
            return found
        config_dir = os.path.dirname(self.config)
        stack = [self.root]
        while stack:
            here = stack.pop()
            try:
                fd = os.open(here, DIR_FLAGS | NOFOLLOW_ANY)
            except OSError as error:
                self._gap("folder_changed" if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP) else "folder_unreadable")
                continue
            try:
                opened = _opened_path(fd)
                if opened != here:
                    self._gap("folder_unreadable" if opened is None else "folder_changed")  # a link was swapped in after the judgement
                    continue
                with os.scandir(fd) as listing:
                    names = sorted(entry.name for entry in listing)
                subdirs = []
                for name in names:
                    full = os.path.join(here, name)
                    rel = lexical_path(self.base, full)
                    if rel is None or name in SKIP_DIRS or not self._allowed(rel):
                        continue  # judged by its name: nothing is asked of it
                    try:
                        info = os.lstat(name, dir_fd=fd)
                    except OSError as error:
                        self._gap("entry_changed" if isinstance(error, FileNotFoundError) else "entry_unreadable")
                        continue
                    config_rel = os.path.relpath(full, config_dir).replace(os.sep, "/")
                    if stat.S_ISDIR(info.st_mode):
                        if not _excluded(config_rel, patterns):
                            subdirs.append(full)
                    elif stat.S_ISREG(info.st_mode) and name.endswith(SOURCE_EXTENSIONS) and not _excluded(config_rel, patterns):
                        found[rel] = [info.st_mtime_ns, info.st_size]
                    # anything else (a link, a FIFO, a socket, a device) is neither indexed nor followed
                stack.extend(reversed(subdirs))
            except OSError:
                self._gap("folder_unreadable")
                continue
            finally:
                os.close(fd)
        return found

    def refresh(self, prove=True):
        """Bring the index up to date with the files on disk. Returns {"files", "scanned", "removed", "unchanged", "stale",
        "ms"}: `scanned` is how many files were read this time. With `prove` (the default) the build ends with `prove()`, whose
        gaps are added to `gaps`: `stale` lists the sources it found changed since they were keyed (a change during the build; the
        next refresh reads them again). `NavApi` passes False and proves after the answer is built instead."""
        started = time.perf_counter()
        self.gaps = {}
        self._sink = self.gaps
        self._configs = []
        imap, details = self._load_config(self.config)
        self.meta = {"ignored": list(details["ignored"]), "config_error": details.get("error", "")}
        if self.root_refusal:
            self.meta["root_error"] = self.root_refusal
            self._gap(_ROOT_GAP.get(self.root_refusal, "root_unreadable"))
        overlay_maps = []
        for path in self.overlays:
            omap, odetails = self._load_config(path)
            overlay_maps.append((os.path.basename(path), omap))
            self.meta["ignored"] += [f"{os.path.basename(path)}:{item}" for item in odetails["ignored"]]
        self._exclude = list(details["exclude"])
        current = self._walk(self._exclude)
        changed = sorted(rel for rel, key in current.items() if rel not in self.entries or self.entries[rel]["key"] != key)
        removed = [rel for rel in self.entries if rel not in current]
        for rel in removed:
            del self.entries[rel]
        failures, self._unread = {}, set()
        texts = read_sources([self._abs(rel) for rel in changed], self.base, self._allowed, failures=failures,
                             keys={self._abs(rel): current[rel] for rel in changed})
        for rel in changed:
            text = texts[self._abs(rel)]
            if text is None:  # not read: no entry (an entry with no imports would stand for a file that has none), and a gap
                self._gap(_READ_GAP.get(failures.get(self._abs(rel)), "source_unreadable"))
                self.entries.pop(rel, None)
                self._unread.add(rel)
                continue
            specs = [[s["spec"], s["line"], s["type_only"], s["dynamic"]] for s in scan_specifiers(text)]
            self.entries[rel] = {"key": current[rel], "specs": specs}  # read with its open signature equal to this key
        self._known = set(current)
        self._resolve_all(imap, overlay_maps)
        self.refreshed_at = time.time()
        if changed or removed:
            self.save()
        stale = []
        if prove:
            proof, stale = self._prove()
            for code, count in proof.items():
                self.gaps[code] = self.gaps.get(code, 0) + count
        return {"files": len(current), "scanned": len(changed), "removed": len(removed), "unchanged": len(current) - len(changed),
                "stale": stale, "ms": round((time.perf_counter() - started) * 1000)}

    def prove(self):
        """The gaps found by looking again at every input the index's edges were made from ({} when all of them hold); see the class
        docstring. Nothing is changed: the next `refresh()` brings the index up to date."""
        return self._prove()[0]

    def _prove(self):
        """(gaps, [rel of each source found changed])."""
        proof, stale = {}, []
        self._sink = proof
        try:
            if self.boundary_holds is not None and not self.boundary_holds():
                self._gap("config_changed")
            for path, token in self._configs:
                state = _config_state(path, self.base, self._allowed)
                if _state_token(state) != token:
                    self._gap(_CONFIG_GAP.get(state[0], "config_changed") if state[0] != "absent" else "config_changed")
            current = self._walk(self._exclude)
            indexed = {rel: entry["key"] for rel, entry in self.entries.items()}
            if set(current) - set(indexed) - self._unread or set(indexed) - set(current):
                self._gap("folder_changed")  # a file appeared or went, beyond those the build already counted as gaps
            for rel in sorted(indexed):
                if rel in current and current[rel] != indexed[rel]:
                    stale.append(rel)
                    self._gap("source_changed")
            probed = probe_sources([(self._abs(rel), indexed[rel]) for rel in sorted(indexed) if current.get(rel) == indexed[rel]],
                                   self.base, self._allowed)
            for path, status in probed.items():
                if status != READ_OK:
                    if status in ("absent", "changed"):
                        stale.append(lexical_path(self.base, path))
                    self._gap(_READ_GAP.get(status, "source_unreadable"))
            for path, verdict in list(self._targets.items()):
                if self._verdict(path) != verdict:
                    self._gap("target_changed")
        finally:
            self._sink = self.gaps
        return proof, sorted(set(stale))

    def target_state(self, rel):
        """The index's verdict on the project file `rel` as an import target (see `_verdict`): "" for a regular file an import
        may point at, else `missing`, `unreadable` (a gap is recorded), `outside` or `refused`. Kept, so `prove` looks again."""
        path = self._abs(rel)
        verdict = self._verdict(path)
        self._targets[path] = verdict
        return verdict[1]

    def _load_config(self, path):
        """`load_config` of the bytes `_config_read` gives (inside the project, allowed): a config that leads out of the project or
        that the guard refuses is not read, and is reported in `config_error` by its state (`outside`, `refused`). Its state token
        is kept (`_configs`): `prove` reads it again and holds the map to it."""
        state, raw = _config_read(path, self.base, self._allowed)
        self._configs.append((path, _state_token(state)))
        imap, details = load_config(path, raw, error=state[0])
        if state[0] in _CONFIG_GAP:
            self._gap(_CONFIG_GAP[state[0]])
        elif state[0] == "content":
            if details.get("error"):
                self._gap("config_invalid")
            elif details["unmodelled"]:
                self._gap("config_unmodelled")
        return imap, details  # "absent" is no gap: nothing is there, which the kernel said, so the map is empty and not unknown

    def _gap(self, code):
        self._sink[code] = self._sink.get(code, 0) + 1

    def incomplete(self, gaps=None):
        """None when `gaps` (default: those of the last refresh) is empty; else (kind, reason, {code: count}) for the most serious
        kind: `refused` (a refusal: the config or root the index needs), then `incomplete` (something could not be read or used),
        then `changed` (a race). `reason` is a code a caller may show; `gaps` has counts and never a path."""
        gaps = self.gaps if gaps is None else gaps
        for kind in ("refused", "incomplete", "changed"):
            codes = sorted(code for code in gaps if GAPS[code][0] == kind)
            if codes:
                return kind, GAPS[codes[0]][1], dict(sorted(gaps.items()))
        return None

    def _allowed(self, rel):
        if self.allow is None:
            return True
        try:
            return bool(self.allow(rel))
        except CannotJudge:
            self._gap("guard_unjudged")  # not a refusal by name: the guard could not look, so the file may be missing from the index
            return False
        except Exception:
            return False  # a guard that fails refuses

    def _verdicts(self):
        """judge(path) -> (rel, reason), remembered for one resolution pass (see `_verdict`) in `_targets`, which `prove` judges
        again: every target an edge was made from is an input of the answer."""
        memo = self._targets = {}

        def judge(path):
            if path not in memo:
                memo[path] = self._verdict(path)
            return memo[path]
        return judge

    def _verdict(self, target):
        """(rel, "") for an existing regular file an import may point at, else (None, reason): `outside` when it, or a link on its
        way, leads out of the project; `refused` when the guard refuses it (by its name or by where a link leads); `missing` when
        nothing is there, it is not a regular file, or a symlink is on the way inside the project (the walk does not follow links,
        so neither does an edge). Judged by name first (`judged_realpath`): an outside or refused path gets its reason without
        anything being asked of the file system, so the reason never tells whether a file is there."""
        lexical = lexical_path(self.base, target)
        if lexical is None:
            return None, "outside"
        if lexical in self._known:
            return lexical, ""  # walked, so allowed, a regular file, and free of symlinks
        try:
            lexical, real = judged_realpath(self.base, target, self._allowed)
        except Unproven as error:
            if error.reason == "unreadable_file":  # a name on the way cannot be looked at: not the same as nothing being there
                self._gap("target_unreadable")
                return None, "unreadable"
            return None, _JUDGED_EDGE.get(error.reason, "missing")
        if real != lexical:
            return None, "missing"
        try:
            info = lstat_nofollow(os.path.join(self.base, *real.split("/")))
        except (FileNotFoundError, NotADirectoryError):
            return None, "missing"
        except OSError:
            self._gap("target_unreadable")
            return None, "unreadable"
        return (lexical, "") if stat.S_ISREG(info.st_mode) else (None, "missing")

    def _resolve_all(self, imap, overlay_maps):
        judge = self._verdicts()

        def isfile(path):
            return judge(path)[1] != "missing"  # an outside or refused path is passed on as a file, so `_edge` says why
        edges, reverse = {}, {}
        for rel in sorted(self.entries):
            importer = self._abs(rel)
            out = []
            for spec, line, type_only, dynamic in self.entries[rel]["specs"]:
                base = resolve(spec, importer, imap, isfile)
                out.append(self._edge(rel, spec, line, type_only, dynamic, base, None, imap, judge))
                for name, omap in overlay_maps:
                    alt = resolve(spec, importer, omap, isfile)
                    if alt[:2] != base[:2]:
                        out.append(self._edge(rel, spec, line, type_only, dynamic, alt, name, omap, judge))
            edges[rel] = out
            for edge in out:
                if edge["kind"] == "file":
                    reverse.setdefault(edge["to"], []).append(edge)
        for items in reverse.values():
            items.sort(key=lambda e: (e["from"], e["line"], e["config"] or ""))
        self.edges, self._reverse = edges, reverse

    def _edge(self, rel, spec, line, type_only, dynamic, resolved, config, imap, judge=None):
        kind, target, reason = resolved
        if kind == "file":
            inside, why = (judge or self._verdict)(target)
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
