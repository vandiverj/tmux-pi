"""summarize_files without a tool loop (docs/plans/det-prep/ls1c-structured-summarize.md).

tmux-pi reads the named files and numbers their lines (prepare), makes one structured-output call to a local
OpenAI-compatible endpoint (ask), drops every fact whose citation does not check out (validate) and writes the rest as
fact lines (render). summarize() ties the four together. No line number is ever invented or repaired.

The system prompt and the JSON schema are not here: they are the "summarize_structured" key of config/local.json (the
template is config/local.example.json), read by load_settings through lib/router.load_config.
"""
import ast
import collections
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import router
import sensitive_read
import trust

KEY = "summarize_structured"
MAX_SPAN = 5
DEFAULT_BUDGET_CHARS = 60000
_SUFFIX = re.compile(r":\d+(?:-\d+)?$")
MAX_LINES = 3
_DOTTED = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)+(\(\))?")


class PrepareError(Exception):
    """A named file may not be sent, or the files are too large."""


class AskError(Exception):
    """The endpoint call failed. Carries the endpoint and the HTTP status (None when there was none), never a body."""

    def __init__(self, message, endpoint, status=None):
        super().__init__(message)
        self.endpoint, self.status = endpoint, status


def _within(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _refuse_name(path):
    name = os.path.basename(path)
    if name.startswith(".env"):
        return "an environment file"
    if sensitive_read.sensitive_path(path):
        return "withheld by the safe-read rules"
    if _within(path, os.path.realpath(trust.trust_dir())):
        return "the trust store"
    return None


def prepare(root, paths, budget_chars=DEFAULT_BUDGET_CHARS):
    """Number the lines of each named file: a list of {path, lines, chars, text}, text being `{n:>4}\\t{line}` per line.

    Refuses (PrepareError) a path outside root, a symlink leaving root, a .env* file, the trust store, anything the safe-read
    rules withhold, a file that is not readable text, and a total over budget_chars (the message names every size)."""
    root_real = os.path.realpath(root)
    files = []
    for given in paths:
        name = normalize_path(given)
        if not name or "\0" in name:
            raise PrepareError("empty or invalid path: {!r}".format(given))
        full = os.path.normpath(os.path.join(root_real, name))
        if not _within(full, root_real):
            raise PrepareError("{}: outside the root".format(given))
        why = _refuse_name(full)
        if why is None:
            real = os.path.realpath(full)
            if not _within(real, root_real):
                raise PrepareError("{}: a symlink that leaves the root".format(given))
            why = _refuse_name(real)
        if why:
            raise PrepareError("{}: refused, {}".format(given, why))
        try:
            lines = Path(full).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            raise PrepareError("{}: cannot read as text ({})".format(given, type(exc).__name__)) from exc
        text = "\n".join("{:>4}\t{}".format(n, line) for n, line in enumerate(lines, 1))
        files.append({"path": name, "lines": len(lines), "chars": len(text), "text": text})
    total = sum(f["chars"] for f in files)
    if total > budget_chars:
        sizes = ", ".join("{} {} chars".format(f["path"], f["chars"]) for f in files)
        raise PrepareError("files total {} chars, over the budget of {}: {}".format(total, budget_chars, sizes))
    return files


def _completions(endpoint):
    base = endpoint.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def ask(endpoint, model, system, user, schema, timeout):
    """One POST to <endpoint>/chat/completions with a json_schema response format at temperature 0; the reply's `content`,
    else its `reasoning_content` (LM Studio puts the JSON there while reasoning is on), as text.
    endpoint is the base URL (http://127.0.0.1:1234/v1). Raises AskError; its message never holds a response body."""
    url = _completions(endpoint)
    body = {"model": model, "temperature": 0,
            "response_format": {"type": "json_schema", "json_schema": {"name": "facts", "strict": True, "schema": schema}},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with router.local_open(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise AskError("HTTP {} from {}".format(exc.code, url), url, exc.code) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise AskError("timeout after {}s from {}".format(timeout, url), url) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AskError("cannot reach {} ({})".format(url, type(exc).__name__), url) from exc
    try:
        message = json.loads(raw)["choices"][0]["message"]
        text = (message.get("content") or "").strip() or (message.get("reasoning_content") or "").strip()
        json.loads(text)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AskError("reply from {} is not JSON".format(url), url, 200) from exc
    return text


def normalize_path(path):
    """A fact's path as the named file: no surrounding space or backticks, no `:N[-M]` suffix, no leading `./`."""
    path = path.strip().strip("`").strip()
    path = _SUFFIX.sub("", path)
    while path.startswith("./"):
        path = path[2:]
    return path.strip()


def _subject(subject, files):
    """A fact's subject as a bare name: no space or backticks; a dotted name (`A.b`, `A.b()`) that is not a named file's path or
    base name keeps only its last part."""
    subject = subject.strip().strip("`").strip()
    names = set(files) | {name.rsplit("/", 1)[-1] for name in files}
    if subject not in names and _DOTTED.fullmatch(subject):
        subject = subject.rstrip("()").rsplit(".", 1)[-1]
    return subject


# The agreed phrases for "this name is absent" (case-insensitive). A claim that says one of them about a name a named file
# defines is false_absence. "not in <path or base>" is added per named file in _absence_scope. Code decides this, never a model.
ABSENCE_PHRASES = (r"\bnot in the files\b", r"\bis not defined\b", r"\bdoes not define\b", r"\bdoes not exist\b",
                   r"\bis absent\b")
_WORD = re.compile(r"[A-Za-z_]\w*")


def _file_mentions(text, paths):
    """(paths named in text by path or base name, text with those mentions removed)."""
    hit = []
    for path in sorted(paths, key=len, reverse=True):
        for form in {path, path.rsplit("/", 1)[-1]}:
            pattern = re.compile(r"(?<![\w/.])" + re.escape(form) + r"(?![\w])")
            if pattern.search(text):
                hit.append(path)
                text = pattern.sub(" ", text)
    return hit, text


# Ordinary words that are never taken for a defined name unless the claim backticks them. With them, a bare lowercase word of one or
# two characters is not a name either. A bare word that is a defined name and in neither group (`Q`, `attempt`) is.
_STOP_WORDS = frozenset("""a an and any anywhere are as at be by define defined defines do does exist exists file files for found from has
have here in is it its missing name names never no not of on or present absent that the there this to use used uses was with""".split())


def _is_name_word(token):
    return token not in _STOP_WORDS and not (token.islower() and len(token) < 3)


def _false_absence(subject, claim, defined):
    """True when claim says a name is absent and a named file defines that name. defined maps each named path to the set of names
    outline() finds in it. The files that count: the subject's when it names a file (`cli.py` does not define `pop` is false only if
    cli.py defines pop), else those the claim names (`not in cli.py`), else all of them. A name is a backticked word, a one-word
    subject, or a bare word of the claim that passes _is_name_word."""
    named, rest = _file_mentions(claim, defined)
    absent = any(re.search(p, claim, re.I) for p in ABSENCE_PHRASES)
    if not absent:
        absent = any(re.search(r"\bnot in\s+[`'\"(]*" + re.escape(form) + r"(?![\w])", claim, re.I)
                     for path in named for form in {path, path.rsplit("/", 1)[-1]})
    if not absent:
        return False
    subject_files, subject_rest = _file_mentions(subject, defined) if subject else ([], "")
    scope = set().union(*(defined[p] for p in (subject_files or named or defined)))
    candidates = set()
    words = _WORD.findall(subject_rest)
    if len(words) == 1 and subject_rest.strip() == words[0]:
        candidates.update(words)
    else:
        candidates.update(w for w in words if _is_name_word(w))
    for span in re.findall(r"`([^`]+)`", rest):
        candidates.update(_WORD.findall(span))
    candidates.update(w for w in _WORD.findall(rest) if _is_name_word(w))
    return bool(candidates & scope)


def _validate_v2(fact, files, defined=None):
    """(kept fact, None) or (None, reason) for a fact {"subject", "claim", "path", "lines"}."""
    subject, claim, path, lines = (fact.get(k) for k in ("subject", "claim", "path", "lines"))
    if not (isinstance(subject, str) and isinstance(claim, str) and isinstance(path, str) and isinstance(lines, list)
            and 1 <= len(lines) <= MAX_LINES and all(isinstance(n, int) and not isinstance(n, bool) for n in lines)):
        return None, "malformed"
    path = normalize_path(path)
    if path not in files:
        return None, "unnamed_file"
    if any(not 1 <= n <= files[path] for n in lines):
        return None, "range_outside_file"
    if not claim.strip():
        return None, "empty_claim"
    subject = _subject(subject, files)
    if defined and _false_absence(subject, claim, defined):
        return None, "false_absence"
    return {"claim": "{} {}".format(subject, claim.strip()), "path": path, "lines": sorted(set(lines))}, None


def validate(facts, files, defined=None):
    """(kept, drops). files maps each named path to its line count. A fact is {subject, claim, path, lines}, lines being 1 to
    MAX_LINES separate line numbers; or the older {claim, path, line_start, line_end}. It is dropped when it is malformed (a lines
    list that is empty, longer than MAX_LINES or holds a non-integer or a bool), its path is not a named file, a line is outside the
    file, or it has no claim; an older fact also when its range is reversed or spans more than MAX_SPAN lines. A line number is
    never changed (a v2 fact's lines are only de-duplicated and sorted). With defined (path -> set of names the file defines), a fact whose
    claim says a name is absent (ABSENCE_PHRASES) that a named file defines is dropped as false_absence. Each drop is {"fact": <the fact as given>, "reason": ...}."""
    kept, drops = [], []
    for fact in facts:
        if isinstance(fact, dict) and "lines" in fact:
            good, reason = _validate_v2(fact, files, defined)
            if good:
                kept.append(good)
            else:
                drops.append({"fact": fact, "reason": reason})
            continue
        try:
            claim, path = fact["claim"], fact["path"]
            a, b = fact["line_start"], fact["line_end"]
            if not (isinstance(claim, str) and isinstance(path, str)) or any(
                    isinstance(x, bool) or not isinstance(x, int) for x in (a, b)):
                raise TypeError
        except (KeyError, TypeError):
            drops.append({"fact": fact, "reason": "malformed"})
            continue
        path = normalize_path(path)
        if path not in files:
            reason = "unnamed_file"
        elif not 1 <= a <= b <= files[path]:
            reason = "range_outside_file" if min(a, b) < 1 or max(a, b) > files[path] else "range_reversed"
        elif b - a + 1 > MAX_SPAN:
            reason = "range_too_long"
        elif not claim.strip():
            reason = "empty_claim"
        elif defined and _false_absence("", claim, defined):
            reason = "false_absence"
        else:
            kept.append({"claim": claim, "path": path, "line_start": a, "line_end": b})
            continue
        drops.append({"fact": fact, "reason": reason})
    return kept, drops


def render(facts):
    """One `- <claim> [path:a]` or `- <claim> [path:a-b]` per older fact, `- <claim> [path:n, path:m]` per v2 fact (the forms
    tools/qual/summarize_check.FACT_LINE parses)."""
    lines = []
    for fact in facts:
        claim = " ".join(fact["claim"].split()).rstrip(".").rstrip()
        if "lines" in fact:
            cites = ", ".join("{}:{}".format(fact["path"], n) for n in fact["lines"])
        else:
            a, b = fact["line_start"], fact["line_end"]
            cites = "{}:{}".format(fact["path"], a if a == b else "{}-{}".format(a, b))
        lines.append("- {} [{}]".format(claim, cites))
    return "\n".join(lines)


def _source(numbered_text):
    """The file's text from prepare's numbered text (`{n:>4}\\t{line}`)."""
    return "\n".join(line.split("\t", 1)[1] if "\t" in line else "" for line in numbered_text.split("\n"))


def _parse(path, numbered_text):
    """The ast of a .py file's numbered text, or None (not .py, or it does not parse)."""
    if not path.endswith(".py"):
        return None
    try:
        return ast.parse(_source(numbered_text))
    except (SyntaxError, ValueError):
        return None


def outline(path, numbered_text):
    """The names a Python file defines, read with ast from its numbered text (prepare's `{n:>4}\\t{line}`): a list of
    (line, kind, name, owner) in source order. kind is class, function, method, constant (a module-level assignment to a plain
    name) or parameter (every parameter of a function or method in signature order, *args and **kwargs included, except self and cls; its owner is that function). A method's owner is its class.
    Functions inside functions are not walked. A file that is not .py, or does not parse, gives []."""
    if not path.endswith(".py"):
        return []
    tree = _parse(path, numbered_text)
    if tree is None:
        return []
    found = []

    def walk(body, owner):
        for node in body:
            if isinstance(node, ast.ClassDef):
                found.append((node.lineno, "class", node.name, None))
                walk(node.body, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((node.lineno, "method" if owner else "function", node.name, owner))
                args = node.args
                for arg in args.posonlyargs + args.args + [args.vararg] + args.kwonlyargs + [args.kwarg]:
                    if arg and arg.arg not in ("self", "cls"):
                        found.append((node.lineno, "parameter", arg.arg, node.name))
            elif owner is None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
                found.extend((node.lineno, "constant", t.id, None) for t in targets if isinstance(t, ast.Name))

    walk(tree.body, None)
    return found


def name_index(files):
    """Code-made fact lines `- <base name> defines the <kind> <name> [<path>:<line>]`, one per class, function, method and constant
    of outline(), in file then line order; not parameters, __init__ or a name that starts with a single underscore."""
    lines = []
    for f in files:
        base = f["path"].rsplit("/", 1)[-1]
        for line, kind, name, _owner in outline(f["path"], f["text"]):
            if kind == "parameter" or name == "__init__" or (name.startswith("_") and not name.startswith("__")):
                continue
            lines.append("- {} defines the {} {} [{}:{}]".format(base, kind, name, f["path"], line))
    return lines


_VERSION_SUFFIX = (re.compile(r"_v(\d+)$"), re.compile(r"(\d+)$"))
# A directory named like one of these tells the version of the files in it: (family, rank). Only ranks of one family are compared.
_VERSION_DIRS = {"old": ("rel", 0), "before": ("rel", 0), "new": ("rel", 1), "after": ("rel", 1)}


def _version_key(path):
    """(group key, (directory rank, file number), suffixed) of a .py path. The group key holds the directories (the first one that is
    a version directory replaced by its family), the stem without its version suffix and that family. The file number is that of a
    trailing `_v<digits>` or `<digits>`, else 0, and suffixed says there was one."""
    parts = path.split("/")
    directory, stem = parts[:-1], parts[-1][:-3]
    family, rank = None, 0
    for i, part in enumerate(directory):
        marker = _VERSION_DIRS.get(part) or (("v", int(part[1:])) if re.fullmatch(r"v\d+", part) else None)
        if marker:
            family, rank = marker
            directory = directory[:i] + [""] + directory[i + 1:]
            break
    number, suffixed = 0, False
    for pattern in _VERSION_SUFFIX:
        found = pattern.search(stem)
        if found and found.start() > 0:
            stem, number, suffixed = stem[:found.start()], int(found.group(1)), True
            break
    return (tuple(directory), stem, family), (rank, number), suffixed


def version_pairs(files):
    """(old, new) pairs of the named .py files that are two versions of one file. Two files are versions when, in the same directory
    or in directories that differ only by a version directory (`old/` and `new/`, `before/` and `after/`, `v1/` and `v2/`), their
    stems are equal once one trailing `_v<digits>` or `<digits>` is removed (settings.py and settings_v2.py; a.py and a2.py;
    old/a.py and new/a2.py). The one with the higher directory rank and file number is the new one; a file without a suffix has
    number 0. A pair whose two orders disagree (old/a2.py and new/a.py) is not a pair. Equal orders pair only in one reading: a group
    of exactly an unsuffixed file and one suffixed with number 0 (a.py and a_v0.py), the unsuffixed one being the older. Three
    versions pair neighbours. A file that pairs with nothing gives no pair."""
    groups = {}
    for f in files:
        if f["path"].endswith(".py"):
            key, order, suffixed = _version_key(f["path"])
            groups.setdefault(key, []).append((order, suffixed, f))
    pairs = []
    for versions in groups.values():
        versions.sort(key=lambda item: item[:2])
        for a, b in zip(versions, versions[1:]):
            if a[0] == b[0]:
                ok = len(versions) == 2 and not a[1] and b[1]
            else:
                ok = a[0][0] <= b[0][0] and a[0][1] <= b[0][1]
            if ok:
                pairs.append((a[2], b[2]))
    return pairs


def _base(f):
    return f["path"].rsplit("/", 1)[-1]


_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# A file that names one of these anywhere can bind, rewrite or delete a name by a path the source does not show (globals()["X"] = 1,
# setattr(module, "X", 1), exec("X = 1"), module.__dict__.update(...), importlib.import_module): it states nothing about its names.
_DYNAMIC = frozenset(("globals", "vars", "locals", "setattr", "delattr", "exec", "eval", "__import__", "importlib", "__dict__"))


def _root_name(node):
    """The name at the root of `a.b[c].d` (a), or None when the chain starts with something else."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


_Index = collections.namedtuple("_Index", "events star dynamic")


def _binding_index(tree):
    """_Index(events, star, dynamic): every way the file binds or writes a name, read once and kept on the tree.
    events is {name: [(kind, node, statement, scope)]}.
    kind is "name" (a Store or Del target of any form, an import alias, an except name, a match capture), "def" (a def, async def or
    class), "arg" (a parameter), "global" (global or nonlocal) or "write" (an attribute or subscript write that names it, as `x.name = v`
    or `name.x = v` or `name[k] = v`). scope is the def, lambda or class the node sits in, None for the module. star: some
    `from m import *` is there. dynamic: the file uses one of _DYNAMIC (as a name, an attribute, an import)."""
    cached = getattr(tree, "_binding_index", None)
    if cached:
        return cached
    events, star, dynamic = {}, False, False
    stack = [(tree, None, None)]
    while stack:
        node, scope, stmt = stack.pop()
        if isinstance(node, ast.stmt):
            stmt = node
        found = []  # (name, kind)
        if isinstance(node, ast.Name) and node.id in _DYNAMIC or isinstance(node, ast.Attribute) and node.attr in _DYNAMIC:
            dynamic = True
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.id, "name"))
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.extend((n, "write") for n in {getattr(node, "attr", None), _root_name(node.value)} - {None})
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append((node.name, "def"))
        elif isinstance(node, ast.arg):
            found.append((node.arg, "arg"))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.append((node.name, "name"))
        elif isinstance(node, ast.alias):
            dynamic = dynamic or node.name.split(".")[0] in _DYNAMIC
            if node.name == "*":
                star = True
            else:
                found.append((node.asname or node.name.split(".")[0], "name"))
        elif isinstance(node, ast.ImportFrom):
            dynamic = dynamic or (node.module or "").split(".")[0] in _DYNAMIC
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend((n, "global") for n in node.names)
        elif type(node).__name__ in ("MatchAs", "MatchStar") and node.name:  # absent from ast before Python 3.10
            found.append((node.name, "name"))
        elif type(node).__name__ == "MatchMapping" and node.rest:
            found.append((node.rest, "name"))
        for name, kind in found:
            events.setdefault(name, []).append((kind, node, stmt, scope))
        inner = node if isinstance(node, _SCOPE_NODES) else scope
        stack.extend((child, inner, stmt) for child in ast.iter_child_nodes(node))
    tree._binding_index = _Index(events, star, dynamic)
    return tree._binding_index


def _star(tree):
    """True when the file has a `from m import *`: it then binds names no statement shows. The one test of it, used by _binds, the
    use scan (_use_lines) and the absence check (_no_path_to)."""
    return _binding_index(tree).star


def _binds(tree, name, owner, shadow):
    """The events of _binding_index that can change what name is in the scope of owner (a ClassDef of the module, or None for the
    module); None when the file uses a dynamic form (_DYNAMIC) or, in the module scope, has a star import. In the module scope every Store of the name counts
    wherever it sits in the file (a function's `global` or a local of the same name too); a def or class counts when it is in that
    scope, or anywhere when shadow. A parameter counts only when shadow. Attribute and subscript writes always count."""
    index = _binding_index(tree)
    if index.dynamic or (owner is None and _star(tree)):
        return None
    counted = []
    for event in index.events.get(name, ()):
        kind, scope = event[0], event[3]
        if kind == "write" or (kind == "arg" and shadow) or (kind == "def" and (shadow or scope is owner)) \
                or (kind in ("name", "global") and (owner is None or scope is owner)):
            counted.append(event)
    return counted


def _single_binding(tree, name, cls=None, *, shadow=False, nested=False):
    """THE one predicate for "is this name provably bound once, simply": the statement that binds name, or None when the source does
    not prove it. Proven means: in the module scope (or in the body of the module-level class cls, which must itself pass this test)
    exactly one thing binds or writes the name (see _binds), and it is a `def`, `async def` or `class`, a plain `NAME = value`
    (one Name target; `NAME: type = value` too), or an import alias, standing directly in that scope's statements and not under an if, try, with, for, while or
    match; a file that uses a dynamic form (_DYNAMIC) proves nothing. nested: the statement may be anywhere in the file (a variable bound inside a function). shadow: a parameter or a def in
    another scope of the same name counts as a binding too (for a name a use scan reads anywhere in the file)."""
    owner = None
    if cls is not None:
        owner = _single_binding(tree, cls)
        if not isinstance(owner, ast.ClassDef):
            return None
    found = _binds(tree, name, owner, shadow)
    if not found or len(found) != 1:
        return None
    kind, node, stmt, _scope = found[0]
    if kind == "def":
        result = node
    elif kind == "name" and isinstance(stmt, ast.Assign) and stmt.targets == [node]:
        result = stmt
    elif kind == "name" and isinstance(stmt, ast.AnnAssign) and stmt.target is node and stmt.value is not None:
        result = stmt
    elif kind == "name" and isinstance(node, ast.alias):
        result = stmt
    else:
        return None
    body = owner.body if owner else tree.body
    return result if nested or any(s is result for s in body) else None


def _unbound(tree, name, cls=None):
    """True when the source proves the module (or the body of the module-level class cls) does not bind name at all: no binding or
    write of any kind, no star import, and the class, if cls is given, is absent or itself proven."""
    owner = None
    if cls is not None:
        if _unbound(tree, cls):
            return True
        owner = _single_binding(tree, cls)
        if not isinstance(owner, ast.ClassDef):
            return False
    found = _binds(tree, name, owner, False)
    return found is not None and not found


def _defs(tree):
    """{(owner, name): node} of the functions at module level and the methods of module-level classes whose binding is proven single
    (_single_binding); a name bound more than once, under an if or try, rewritten, a property getter and setter, is left out."""
    found = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _single_binding(tree, node.name) is node:
            found[(None, node.name)] = node
        elif isinstance(node, ast.ClassDef) and _single_binding(tree, node.name) is node:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and _single_binding(tree, sub.name, node.name) is sub:
                    found[(node.name, sub.name)] = sub
    return found


_IMMUTABLE_TYPES = (int, float, complex, str, bytes, bool, type(None))


def _immutable(node):
    """True when a value node is a literal nothing can change afterwards: a number (a leading sign too), a string, bytes, True, False
    or None, or a tuple of only those. A dict, list or set is mutable (`D.update(...)`, `D.append(...)`, `D[k] = v` in a function would
    change it without any binding the source shows); a tuple that holds one is mutable too."""
    if isinstance(node, ast.Constant):
        return type(node.value) in _IMMUTABLE_TYPES
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float, complex)
    return isinstance(node, ast.Tuple) and all(_immutable(e) for e in node.elts)


def _literal_constants(tree):
    """{name: (literal value, line)} of the module-level constants that are a plain `NAME = <value>` or `NAME: type = <value>`, whose
    binding is proven single (_single_binding) and whose value is an immutable literal (_immutable)."""
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target = node.target
        else:
            continue
        if not isinstance(target, ast.Name) or _single_binding(tree, target.id) is not node or not _immutable(node.value):
            continue
        found[target.id] = (ast.literal_eval(node.value), node.lineno)
    return found


def _same(a, b):
    """True when two literal values are equal and of the same types all the way down: 1 and True, or 1 and 1.0, differ."""
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _scope_header(node):
    """The parts of a nested def, lambda or class that run in the enclosing scope: decorators, default values, bases."""
    if isinstance(node, ast.ClassDef):
        return node.decorator_list + node.bases + [k.value for k in node.keywords]
    head = node.args.defaults + [d for d in node.args.kw_defaults if d]
    return head if isinstance(node, ast.Lambda) else head + node.decorator_list


def _calls(fn):
    """{callee name: first line} of the plain calls (a Name or an Attribute func) that run in the function's own scope. A call in a
    nested def, lambda or class body belongs to that inner scope and is left out."""
    found = {}

    def visit(node):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else None
            if name and (name not in found or node.lineno < found[name]):
                found[name] = node.lineno
        for child in (_scope_header(node) if isinstance(node, _SCOPES) else ast.iter_child_nodes(node)):
            visit(child)

    for statement in fn.body:
        visit(statement)
    return found


def _keyed_names(f):
    skipped = lambda kind, name: kind == "parameter" or name == "__init__" or (name.startswith("_") and not name.startswith("__"))  # noqa: E731
    return {(kind, owner, name): line for line, kind, name, owner in outline(f["path"], f["text"]) if not skipped(kind, name)}


def version_diff(old, new):
    """Code-made fact lines `- <claim> [path:n, ...]` for what the new file adds, drops or changes against the old one, each citing
    the line that shows it: names (class, function, method, constant, as name_index skips them), constants whose value is an immutable
    literal (a number, string, bytes, True, False, None, or a tuple of those; never a dict, list or set), then the plain calls of a function or method both define. A line is stated only on proof: a name is compared
    only when each file binds it once, simply and directly in its scope (_single_binding), and is added or dropped only when one file
    proves that and the other proves it does not bind the name at all (_unbound). A file that uses globals, vars, locals, setattr, delattr,
    exec, eval, __import__, importlib or __dict__ proves nothing, so it gives no line. Order: names, constants, calls, each in line order."""
    old_tree, new_tree = _parse(old["path"], old["text"]), _parse(new["path"], new["text"])
    if old_tree is None or new_tree is None:
        return []
    base, op, np = _base(new), old["path"], new["path"]
    obase = _base(old)
    lines = []
    old_names, new_names = _keyed_names(old), _keyed_names(new)
    # a name is added or dropped only when one file proves it bound once and the other proves it not bound at all (no star import,
    # no write under an if or try)
    for key, line in sorted(new_names.items(), key=lambda item: item[1]):
        if key not in old_names and _unbound(old_tree, key[2], key[1]) and _single_binding(new_tree, key[2], key[1]):
            lines.append("- {} adds the {} {} [{}:{}]".format(base, key[0], key[2], np, line))
    for key, line in sorted(old_names.items(), key=lambda item: item[1]):
        if key not in new_names and _unbound(new_tree, key[2], key[1]) and _single_binding(old_tree, key[2], key[1]):
            lines.append("- {} drops the {} {} that {} defines [{}:{}]".format(base, key[0], key[2], obase, op, line))
    old_const, new_const = _literal_constants(old_tree), _literal_constants(new_tree)
    for name, (value, line) in sorted(new_const.items(), key=lambda item: item[1][1]):
        if name in old_const and not _same(old_const[name][0], value):
            lines.append("- {} changes {} from {!r} to {!r} [{}:{}, {}:{}]".format(
                base, name, old_const[name][0], value, op, old_const[name][1], np, line))
    old_defs, new_defs = _defs(old_tree), _defs(new_tree)
    added, gone = [], []
    for key, fn in new_defs.items():
        if key in old_defs:
            was, now = _calls(old_defs[key]), _calls(fn)
            added.extend((line, "- in {}, {} now calls {} [{}:{}]".format(base, key[1], callee, np, line)) for callee, line in now.items() if callee not in was)
            gone.extend((line, "- in {}, {} no longer calls {} [{}:{}]".format(base, key[1], callee, op, line)) for callee, line in was.items() if callee not in now)
    return lines + [text for _, text in sorted(added, key=lambda item: item[0])] + [text for _, text in sorted(gone, key=lambda item: item[0])]


def _dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return head + "." + node.attr if head else None
    return None


def _members(tree, cls):
    """The names a class defines: its methods (dunder ones too), its class-level assignments and the self.<name> attributes its methods
    assign."""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls:
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(sub.name)
                    names.update(n.attr for n in ast.walk(sub) if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store)
                                 and isinstance(n.value, ast.Name) and n.value.id == "self")
                else:
                    names.update(t.id for t in (sub.targets if isinstance(sub, ast.Assign) else [sub.target] if isinstance(sub, ast.AnnAssign) else [])
                                 if isinstance(t, ast.Name))
    return names


def _module_name(path):
    """The dotted module a .py path names from the run root: pkg/t.py is pkg.t, pkg/__init__.py is pkg."""
    parts = path[:-3].split("/") if path.endswith(".py") else path.split("/")
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_module(path, node):
    """The full dotted module a `from <module> import` statement names: a relative one is resolved from the importing file's
    package, as Python does (level 1 is that package, each further level its parent); None when it has no such package (a module at
    the run root) or climbs above the run root."""
    if not node.level:
        return node.module or ""
    package = path.split("/")[:-1]
    if node.level > len(package):  # Python: no level may climb above the importer's package, and a top-level module has none
        return None
    package = package[:len(package) - (node.level - 1)]
    return ".".join(package + ([node.module] if node.module else []))


def _inside(dotted, module):
    return dotted == module or dotted.startswith(module + ".")


# A file whose text holds one of these words can reach a name it does not spell (getattr(m, chr(81)), vars(m), importlib.import_module,
# inspect.getmembers, a class from object.__subclasses__()): "does not use" is not said of it. The words are matched in the raw text,
# comments and strings too, on identifier boundaries; a false hit only loses a line. The last six are beyond the brief's list.
_DYNAMIC_ACCESS = ("getattr", "__getattribute__", "__import__", "importlib", "import_module", "sys.modules", "eval", "exec", "compile",
                   "globals", "locals", "vars", "__dict__", "attrgetter", "methodcaller", "runpy", "builtins", "__builtins__",
                   "pkgutil", "pickle", "marshal",
                   "setattr", "delattr", "inspect", "getmembers", "__subclasses__", "gc")
_DYNAMIC_ACCESS_RE = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(w) for w in _DYNAMIC_ACCESS) + r")(?![A-Za-z0-9_])")


def _imports_of(path, tree):
    """(dotted names the file imports, whether every import resolved): `import a.b` names a.b; `from a.b import c` names a.b and a.b.c
    (c may be a module); a relative import is resolved as in _imported_module, and one that does not resolve makes the second False."""
    names, resolved = set(), True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            source = _imported_module(path, node)
            if source is None:
                resolved = False
                continue
            names.add(source)
            names.update(source + "." + a.name for a in node.names if a.name != "*")
    return names, resolved


def _import_graph(files, parsed):
    """{dotted module: (imports, resolved) or None} for every .py file of the run root (_imports_of); None for one that does not parse
    (or when one of two files of the same module name does not)."""
    graph = {}
    for f in files:
        if f["path"].endswith(".py"):
            module, tree = _module_name(f["path"]), parsed[f["path"]]
            found = _imports_of(f["path"], tree) if tree is not None else None
            if module in graph:  # pkg.py and pkg/__init__.py: either may run, so the module holds the imports of both
                found = None if found is None or graph[module] is None else (found[0] | graph[module][0], found[1] and graph[module][1])
            graph[module] = found
    return graph


def _reaches(imports, resolved, module, graph):
    """True when code that imports the dotted names `imports` may reach the dotted module `module` through the run root's import graph.
    An import counts when it can name the target: it is equal to it, an ancestor package of it or a descendant of it. A name may name a
    run-root module by its full path or by the path below a directory on sys.path (`worker` for src/worker.py). Importing a module runs
    the modules of its ancestor packages, and each module it loads is followed through its own imports (transitively). An import that
    cannot be resolved (a relative one above the run root), a file that does not parse, and a name that is not a run-root module or
    package but whose top-level name is one all count as a path."""
    if not resolved:
        return True
    tops = {m.split(".")[0] for m in graph}
    seen, todo = set(), list(imports)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        named = {name} | {m for m in graph if m.endswith("." + name)}
        if any(_inside(n, module) or _inside(module, n) for n in named):
            return True
        if len(named) == 1 and not any(_inside(m, name) or _inside(name, m) for m in graph) and name.split(".")[0] in tops:
            return True
        for m in {m for m in graph if any(_inside(n, m) for n in named)}:
            if graph[m] is None or not graph[m][1]:
                return True
            todo.extend(graph[m][0])
    return False


def _no_path_to(source, tree, path, name, module, graph):
    """True when the file's text and the run root's import graph prove the file has no path to the name defined in the dotted module
    `module`, so that "does not use" may be said. All of these hold; none is a list of syntax cases:
    - the bare name occurs nowhere in the raw text (code, strings, bytes, comments, docstrings), on identifier boundaries;
    - the text holds none of _DYNAMIC_ACCESS;
    - no star import (_star);
    - no path through the import graph to the module (_reaches): none of its imports, nor those of any run-root module it loads,
      is the target module, an ancestor package or a submodule of it, or cannot be resolved.
    Anything else gives no absence line."""
    if _star(tree) or _DYNAMIC_ACCESS_RE.search(source) or re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", source):
        return False
    return not _reaches(*_imports_of(path, tree), module, graph)


def _use_lines(f, tree, target, kind, module, others, members, dup):
    """(imports, creates, uses, undecided) for one target in one other file: lists of (line, text), and whether the file refers to
    the target name in a way this scan cannot attribute (an import of it from a module that is not the target's by its full dotted
    path, `from m import *`, a dynamic form such as globals() or setattr (_DYNAMIC; the file then gives no line at all), an import of the target that is rebound or may be overwritten, a use of the bare name or of `x.<name>`
    that no import of the target explains, the name as a string) or binds it from two of the definers (ambiguous). module is the
    target's dotted module name from the run root, others those of the other files that define the same name. An import counts as
    the target's only when its module resolves to `module` by full dotted path (a basename alone never does). A call, a creation or
    a use is attributed only through a name that _single_binding proves bound once, by that import: a name that is bound again,
    written (`t.Q = x`), shadowed, or in a file with a star import (_star: a parameter attribution too) gives the import line at most. dup (the name is defined in more
    than one file) names the target `<module>.<T>` in each line and takes a use through a parameter only from a file that imports
    this module."""
    if _binding_index(tree).dynamic:
        return [], [], [], True  # a dynamic form (_DYNAMIC) anywhere: the source does not show what any name in this file is
    base, path, source = _base(f), f["path"], _source(f["text"]).split("\n")
    label = "{}.{}".format(module, target) if dup else target
    direct, modules, imports = {}, {}, []  # local name -> import statement; the dotted name used in code -> (bound name, statement)
    other_direct, other_modules, unexplained = set(), set(), False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            source_module = _imported_module(path, node)
            for alias in node.names:
                local = alias.asname or alias.name
                joined = (source_module + "." + alias.name if source_module else alias.name) if source_module is not None else None
                if alias.name == "*":
                    continue
                if source_module == module and alias.name == target:
                    direct[local] = node
                    shown = next((n for n in range(node.lineno, (node.end_lineno or node.lineno) + 1)
                                  if re.search(r"\b" + re.escape(target) + r"\b", source[n - 1])), node.lineno)
                    imports.append((shown, "- {} imports {} [{}:{}]".format(base, label, path, shown)))
                elif joined == module:
                    modules[local] = (local, node)
                elif source_module in others and alias.name == target:
                    other_direct.add(local)
                elif joined in others:
                    other_modules.add(local)
                elif alias.name == target:
                    unexplained = True
        elif isinstance(node, ast.Import):
            for a in node.names:
                code, bound = a.asname or a.name, a.asname or a.name.split(".")[0]
                if a.name == module:
                    modules[code] = (bound, node)
                elif a.name in others:
                    other_modules.add(code)
    if set(direct) & other_direct or set(modules) & other_modules:
        return [], [], [], True
    if _star(tree):
        # a star import binds names no statement shows: the import line is a fact, no call, creation or attribute use is (a parameter
        # included: whatever `q` is, it may be a name the star import brought)
        return sorted(imports, key=lambda item: item[0]), [], [], True
    valid_direct = {name for name, stmt in direct.items() if _single_binding(tree, name, shadow=True) is stmt}
    valid_modules = {code for code, (bound, stmt) in modules.items() if _single_binding(tree, bound, shadow=True) is stmt}
    if valid_direct != set(direct) or valid_modules != set(modules):
        # the import is a fact; what the name then is, is not shown. Nothing is attributed through a name that may not be the import.
        return sorted(imports, key=lambda item: item[0]), [], [], True
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == target:
            unexplained = unexplained or node.id not in other_direct  # a use of this module's name is a line, or else (a star import) undecided
        elif isinstance(node, ast.Attribute) and node.attr == target:
            unexplained = unexplained or _dotted(node.value) not in set(modules) | other_modules
        elif isinstance(node, ast.Constant) and node.value == target:
            unexplained = True

    def is_ref(func):
        if isinstance(func, ast.Name):
            return func.id in valid_direct
        return isinstance(func, ast.Attribute) and func.attr == target and _dotted(func.value) in valid_modules

    first_module_ref = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and is_ref(node) and (first_module_ref is None or node.lineno < first_module_ref):
            first_module_ref = node.lineno
    if first_module_ref:
        imports.append((first_module_ref, "- {} imports {} [{}:{}]".format(base, label, path, first_module_ref)))
    verb = "creates a" if kind == "class" else "calls"
    creates, bound = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and is_ref(node.func):
            creates.append((node.lineno, "- {} {} {} [{}:{}]".format(base, verb, label, path, node.lineno)))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and is_ref(node.value.func):
            bound.update(t.id for t in node.targets
                         if isinstance(t, ast.Name) and _single_binding(tree, t.id, shadow=True, nested=True) is node)
    uses = []
    if kind == "class" and members:
        def visit(node, params):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                a = node.args
                params = {x.arg for x in a.posonlyargs + a.args + [a.vararg] + a.kwonlyargs + [a.kwarg] if x} - {"self", "cls"}
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr in members \
                    and (node.value.id in bound or node.value.id in params):
                uses.append((node.lineno, "- {} uses {}.{} on {} [{}:{}]".format(base, label, node.attr, node.value.id, path, node.lineno)))
            for child in ast.iter_child_nodes(node):
                visit(child, params)
        visit(tree, set())
    if dup and uses and not (valid_direct or valid_modules):
        uses, unexplained = [], True
    key = lambda item: item[0]  # noqa: E731
    return sorted(imports, key=key), sorted(creates, key=key), sorted(uses, key=key), unexplained


def use_scan(task, files):
    """Code-made fact lines about who uses a class or top-level function (the target) that the task names as a whole word, that a
    named .py file defines, and whose binding there is proven single (_single_binding). Per other .py file: its import, creation or
    call, and the attribute uses through a variable bound once from T(...) or a parameter (only of a name the class defines, dunder
    methods too), each only through an import of the target by its full dotted path whose name nothing else binds, writes or
    shadows (see _use_lines); `from m import *` attributes nothing. A file with none of these gives `<base> does not use <T> [path:1]`,
    unless the file's text and the run root's import graph do not prove it has no path to the name (_no_path_to: a star import, a word of
    _DYNAMIC_ACCESS, the name anywhere in the text, a path through the imports to the target's module): then it gives nothing. When more than one file
    defines the name, each definer is scanned as its own module and its lines say `<module>.<T>`; a file that defines the name itself,
    or whose binding of the name is ambiguous, gives nothing for it."""
    out = []
    parsed = {f["path"]: _parse(f["path"], f["text"]) for f in files}
    graph = _import_graph(files, parsed)
    for f in files:
        for line, kind, name, owner in outline(f["path"], f["text"]):
            if kind not in ("class", "function") or owner or not re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", task):
                continue
            if not isinstance(_single_binding(parsed[f["path"]], name), (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            definers = {g["path"] for g in files for _l, k, n, o in outline(g["path"], g["text"]) if k in ("class", "function") and n == name and not o}
            module = _module_name(f["path"])
            others = {_module_name(p) for p in definers if p != f["path"]}
            if module in others:
                continue
            members = _members(parsed[f["path"]], name) if kind == "class" else set()
            for g in files:
                if g["path"] in definers or parsed[g["path"]] is None:
                    continue
                imports, creates, uses, undecided = _use_lines(g, parsed[g["path"]], name, kind, module, others, members, len(definers) > 1)
                found = [text for _n, text in imports + creates + uses]
                if found:
                    out.extend(found)
                elif not undecided and _no_path_to(_source(g["text"]), parsed[g["path"]], g["path"], name, module, graph):
                    out.append("- {} does not use {} [{}:1]".format(_base(g), "{}.{}".format(module, name) if len(definers) > 1 else name, g["path"]))
    return list(dict.fromkeys(out))


def code_facts(task, files):
    """(diff lines, use lines) made by code for this task and these files."""
    diff = [line for old, new in version_pairs(files) for line in version_diff(old, new)]
    return list(dict.fromkeys(diff)), use_scan(task, files)


def user_message(task, files):
    parts = []
    for f in files:
        part = "## {}\n```\n{}\n```".format(f["path"], f["text"])
        names = outline(f["path"], f["text"])
        if names:
            part += "\nNames defined in {} (cover each one the task is about):\n{}".format(f["path"], "\n".join(
                "- line {}: {}".format(line, "parameter {} of {}".format(name, owner) if kind == "parameter" else "{} {}".format(kind, name))
                for line, kind, name, owner in names))
        parts.append(part)
    message = "# Task\n\n{}\n\n# Files (numbered; take line numbers from here)\n\n{}".format(task, "\n\n".join(parts))
    diff, uses = code_facts(task, files)
    if diff:
        message += "\n\nDifferences found by code:\n" + "\n".join(diff)
    if uses:
        message += "\n\nUses found by code:\n" + "\n".join(uses)
    if diff or uses:
        message += "\n\nExplain what each difference or use means for the task; these lines are added to your answer for you."
    return message


def summarize(root, paths, task, *, endpoint, model, settings, budget_chars=DEFAULT_BUDGET_CHARS, timeout=600):
    """(text, record). settings is {"system": ..., "schema": ...} (see load_settings). The text is name_index(files), version_diff and use_scan lines (all made
    by code), then the rendered facts, deduplicated by exact text. Raises PrepareError or AskError; a reply whose JSON lacks a facts list is an AskError too. The record:
    files (path, lines, chars), chars, drops, kept, indexed (the index lines), code_facts (the diff and use lines), seconds, model."""
    started = time.monotonic()
    files = prepare(root, paths, budget_chars)
    reply = ask(endpoint, model, settings["system"], user_message(task, files), settings["schema"], timeout)
    facts = json.loads(reply).get("facts") if isinstance(json.loads(reply), dict) else None
    if not isinstance(facts, list):
        raise AskError("reply has no facts list", _completions(endpoint), 200)
    defined = {f["path"]: {name for _l, _k, name, _o in outline(f["path"], f["text"])} for f in files}
    kept, drops = validate(facts, {f["path"]: f["lines"] for f in files}, defined)
    index = name_index(files)
    diff, uses = code_facts(task, files)
    record = {"files": [{k: f[k] for k in ("path", "lines", "chars")} for f in files],
              "chars": sum(f["chars"] for f in files), "kept": len(kept), "drops": drops, "indexed": len(index), "code_facts": len(diff) + len(uses),
              "seconds": round(time.monotonic() - started, 2), "model": model}
    answer = index + diff + uses + (render(kept).split("\n") if kept else [])
    return "\n".join(dict.fromkeys(answer)), record


def load_settings(root, local_path=None):
    """The summarize_structured key ({"system", "schema"}) of this machine's local config, read through router.load_config; the
    template's (config/local.example.json) when the machine's has none."""
    root = Path(root)
    candidates = [local_path or os.environ.get("TP_LOCAL_CONFIG") or root / "config" / "local.json",
                  root / "config" / "local.example.json"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            local = router.load_config(root / "config" / "router.json", local_path=str(candidate), laya_model=False)["local"]
            if local.get(KEY):
                return local[KEY]
    raise PrepareError("no {} key in the local config or config/local.example.json".format(KEY))


def default_endpoint(root, local_path=None):
    """The LM Studio base URL the router config already names: the follow-up judge's base_url."""
    candidates = [local_path or os.environ.get("TP_LOCAL_CONFIG") or Path(root) / "config" / "local.json",
                  Path(root) / "config" / "local.example.json"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            local = router.load_config(Path(root) / "config" / "router.json", local_path=str(candidate), laya_model=False)["local"]
            if local.get("followup"):
                return local["followup"]["base_url"]
    raise PrepareError("no followup base_url in the local config; pass --endpoint")
