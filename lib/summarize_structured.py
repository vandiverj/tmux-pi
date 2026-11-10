"""summarize_files without a tool loop (docs/plans/det-prep/ls1c-structured-summarize.md).

tmux-pi reads the named files and numbers their lines (prepare), makes one structured-output call to a local
OpenAI-compatible endpoint (ask), drops every fact whose citation does not check out (validate) and writes the rest as
fact lines (render). summarize() ties the four together. No line number is ever invented or repaired.

The system prompt and the JSON schema are not here: they are the "summarize_structured" key of config/local.json (the
template is config/local.example.json), read by load_settings through lib/router.load_config.
"""
import ast
import collections
import importlib.machinery
import json
import os
import re
import socket
import sys
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
    events is {name: [(kind, node, statement, scope, role)]}.
    kind is "name" (a Store or Del target of any form, an import alias, an except name, a match capture), "def" (a def, async def or
    class), "arg" (a parameter, or a type parameter `def f[t]()`), "global" (global or nonlocal) or "write" (an attribute or subscript write that names it, as `x.name = v`
    or `name.x = v` or `name[k] = v`). scope is the def, lambda or class the node sits in, None for the module. role (round 7b) says
    which part of a write names it: "attr" when the name is the attribute written (`x.name = v`: x may be the module), "root" when it is
    the root of the target (`name.x = v`, `name[k] = v`, `name[k] += v`: a write into the object the name holds, which cannot unbind or
    rebind the name); None for every other kind. The context of the write (Store or Del) is that of the node (node.ctx). star: some
    `from m import *` is there. dynamic: the file uses one of _DYNAMIC (as a name, an attribute, an import) or `sys.modules` (an attribute `modules`, or
    `from sys import modules`), by which it can rebind or delete any name of any module."""
    cached = getattr(tree, "_binding_index", None)
    if cached:
        return cached
    events, star, dynamic = {}, False, False
    stack = [(tree, None, None)]
    while stack:
        node, scope, stmt = stack.pop()
        if isinstance(node, ast.stmt):
            stmt = node
        found = []  # (name, kind[, role])
        if isinstance(node, ast.Name) and node.id in _DYNAMIC or isinstance(node, ast.Attribute) and (node.attr in _DYNAMIC or node.attr == "modules"):
            dynamic = True  # (`sys.modules[name] = m` replaces a module for every later importer)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append((node.id, "name"))
        elif isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            roles = {n: "root" for n in (_root_name(node.value),) if n}
            roles.update({node.attr: "attr"} if isinstance(node, ast.Attribute) else {})  # `x.x = v`: the attribute role wins
            found.extend((n, "write", role) for n, role in roles.items())
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
            dynamic = dynamic or (node.module or "").split(".")[0] in _DYNAMIC or node.module == "sys" and any(a.name == "modules" for a in node.names)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.extend((n, "global") for n in node.names)
        elif type(node).__name__ in ("MatchAs", "MatchStar") and node.name:  # absent from ast before Python 3.10
            found.append((node.name, "name"))
        elif type(node).__name__ in ("TypeVar", "ParamSpec", "TypeVarTuple"):  # `def f[t]()`, `class C[t]`, `type X[t] = ...` (Python 3.12+): binds t like a parameter
            found.append((node.name, "arg"))
        elif type(node).__name__ == "MatchMapping" and node.rest:
            found.append((node.rest, "name"))
        for name, kind, *role in found:
            events.setdefault(name, []).append((kind, node, stmt, scope, role[0] if role else None))
        inner = node if isinstance(node, _SCOPE_NODES) else scope
        stack.extend((child, inner, stmt) for child in ast.iter_child_nodes(node))
    tree._binding_index = _Index(events, star, dynamic)
    return tree._binding_index


def _star(tree):
    """True when the file has a `from m import *`: it then binds names no statement shows. The one test of it, used by _binds, the
    use scan (_use_lines) and the absence check (_no_path_to)."""
    return _binding_index(tree).star


def _binds(tree, name, owner, shadow, existence=False):
    """The events of _binding_index that can change what name is in the scope of owner (a ClassDef of the module, or None for the
    module); None when the file uses a dynamic form (_DYNAMIC) or, in the module scope, has a star import. In the module scope every Store of the name counts
    wherever it sits in the file (a function's `global` or a local of the same name too); a def or class counts when it is in that
    scope, or anywhere when shadow. A parameter counts only when shadow. Attribute and subscript writes always count, except under
    existence (round 7b: the question is whether the name exists, not what it holds): a Store whose target is rooted at the name
    (`name[k] = v`, `name.x = v`) changes the object, not the name, and is not counted. A Del of any form, and a write whose attribute is
    the name (`x.name = v`), still count."""
    index = _binding_index(tree)
    if index.dynamic or (owner is None and _star(tree)):
        return None
    counted = []
    for event in index.events.get(name, ()):
        kind, scope = event[0], event[3]
        if kind == "write" and existence and event[4] == "root" and isinstance(event[1].ctx, ast.Store):
            continue
        if kind == "write" or (kind == "arg" and shadow) or (kind == "def" and (shadow or scope is owner)) \
                or (kind in ("name", "global") and (owner is None or scope is owner)):
            counted.append(event)
    return counted


def _single_binding(tree, name, cls=None, *, shadow=False, nested=False, existence=False):
    """THE one predicate for "is this name provably bound once, simply": the statement that binds name, or None when the source does
    not prove it. Proven means: in the module scope (or in the body of the module-level class cls, which must itself pass this test)
    exactly one thing binds or writes the name (see _binds), and it is a `def`, `async def` or `class`, a plain `NAME = value`
    (one Name target; `NAME: type = value` too), or an import alias, standing directly in that scope's statements and not under an if, try, with, for, while or
    match; a file that uses a dynamic form (_DYNAMIC) proves nothing. nested: the statement may be anywhere in the file (a variable bound inside a function). shadow: a parameter or a def in
    another scope of the same name counts as a binding too (for a name a use scan reads anywhere in the file). existence (round 7b, only
    for the proof that a member import finds the name): a write into the object the name holds (`name[k] = v`, `name.x = v`) is not a
    binding of the name (_binds); every other caller asks about the value and keeps the default."""
    owner = None
    if cls is not None:
        owner = _single_binding(tree, cls, existence=existence)
        if not isinstance(owner, ast.ClassDef):
            return None
    found = _binds(tree, name, owner, shadow, existence)
    if not found or len(found) != 1:
        return None
    kind, node, stmt = found[0][:3]
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


# The top-level names of the standard library, for an interpreter without sys.stdlib_module_names (3.9): the 3.14 list and the 3.9
# stdlib listing, fixed here (test-only and sysconfigdata modules left out). A name too many only withholds lines.
_STDLIB_FIXED = frozenset("""
331
    __future__ _abc _aix_support _android_support _apple_support _ast _ast_unparse _asyncio _bisect _blake2 _bootlocale
    _bootsubprocess _bz2 _codecs _codecs_cn _codecs_hk _codecs_iso2022 _codecs_jp _codecs_kr _codecs_tw _collections
    _collections_abc _colorize _compat_pickle _compression _contextvars _crypt _csv _ctypes _curses _curses_panel _datetime _dbm
    _decimal _elementtree _frozen_importlib _frozen_importlib_external _functools _gdbm _hashlib _heapq _hmac _imp
    _interpchannels _interpqueues _interpreters _io _ios_support _json _locale _lsprof _lzma _markupbase _md5 _multibytecodec
    _multiprocessing _opcode _opcode_metadata _operator _osx_support _overlapped _peg_parser _pickle _posixshmem
    _posixsubprocess _py_abc _py_warnings _pydatetime _pydecimal _pyio _pylong _pyrepl _queue _random _remote_debugging _scproxy
    _sha1 _sha2 _sha256 _sha3 _sha512 _signal _sitebuiltins _socket _sqlite3 _sre _ssl _stat _statistics _string _strptime
    _struct _suggestions _symtable _sysconfig _thread _threading_local _tkinter _tokenize _tracemalloc _types _typing _uuid
    _warnings _weakref _weakrefset _winapi _wmi _xxsubinterpreters _zoneinfo _zstd abc aifc annotationlib antigravity argparse
    array ast asynchat asyncio asyncore atexit audioop base64 bdb binascii binhex bisect builtins bz2 cProfile calendar cgi
    cgitb chunk cmath cmd code codecs codeop collections colorsys compileall compression concurrent configparser contextlib
    contextvars copy copyreg crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils doctest email
    encodings ensurepip enum errno faulthandler fcntl filecmp fileinput fnmatch formatter fractions ftplib functools gc
    genericpath getopt getpass gettext glob graphlib grp gzip hashlib heapq hmac html http idlelib imaplib imghdr imp importlib
    inspect io ipaddress itertools json keyword lib2to3 linecache locale logging lzma mailbox mailcap marshal math mimetypes
    mmap modulefinder msvcrt multiprocessing netrc nis nntplib nt ntpath nturl2path numbers opcode operator optparse os parser
    pathlib pdb pickle pickletools pipes pkgutil platform plistlib poplib posix posixpath pprint profile pstats pty pwd
    py_compile pyclbr pydoc pydoc_data pyexpat queue quopri random re readline reprlib resource rlcompleter runpy sched secrets
    select selectors shelve shlex shutil signal site smtpd smtplib sndhdr socket socketserver sqlite3 sre_compile sre_constants
    sre_parse ssl stat statistics string stringprep struct subprocess sunau symbol symtable sys sysconfig syslog tabnanny
    tarfile telnetlib tempfile termios textwrap this threading time timeit tkinter token tokenize tomllib trace traceback
    tracemalloc tty turtle turtledemo types typing unicodedata unittest urllib uu uuid venv warnings wave weakref webbrowser
    winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport zlib zoneinfo
""".split())


def _reserved_names():
    """Top-level module names Python may answer without the path finder: builtins, and the standard library (already imported, frozen,
    or found in the stdlib by a later sys.path entry when the run root's candidate is only a namespace directory)."""
    return frozenset(sys.builtin_module_names) | frozenset(getattr(sys, "stdlib_module_names", _STDLIB_FIXED))


class _RunRoot:
    """The run root as Python's path finder sees it, with the run root as sys.path[0]: the supplied paths (and the directories they sit
    in), and, when root is a directory on disk, the names in each directory the finder is asked about (a listing: names, and whether
    each is a directory; nothing is read). A run with no run root on disk sees only the supplied files."""

    def __init__(self, paths, root=None):
        self.supplied = frozenset(paths)
        self.root = root if root is not None and os.path.isdir(root) else None
        self._listings = {}

    def listing(self, directory):
        """(file names, directory names) in directory ("" is the run root, else a/b)."""
        if directory not in self._listings:
            files, dirs, prefix = set(), set(), directory + "/" if directory else ""
            for path in self.supplied:
                if path.startswith(prefix):
                    head, sep, _rest = path[len(prefix):].partition("/")
                    (dirs if sep else files).add(head)
            if self.root is not None:
                try:
                    with os.scandir(os.path.join(self.root, *directory.split("/")) if directory else self.root) as entries:
                        for entry in entries:
                            (dirs if entry.is_dir() else files).add(entry.name)
                except OSError:
                    pass
            self._listings[directory] = (files, dirs)
        return self._listings[directory]


_Identity = collections.namedtuple("_Identity", "owners candidates")


def _module_identity(dotted, run):
    """THE one function that turns a dotted module name into files (round 11, Main): nothing else maps a name to a file. Python's path
    finder, for each step of the name, looks in that step's directory for a package directory (name/ with an __init__ of any import
    suffix), then a module file name.<suffix> in the order of importlib.machinery.all_suffixes() (extension modules first on this
    interpreter's loaders, then .py, then .pyc), then a namespace portion (name/ without an __init__); a name already in sys.modules
    (a builtin, a frozen or already imported standard module) never reaches the finder.
    candidates: for each step, from the first, that the run root (_RunRoot) has anything for, the set of what the finder could load
    there: "a/b.py", "a/b.<ext>", "a/b/__init__.<suffix>", or "a/b/" for a directory with no __init__; the list stops at a step with
    nothing (the name then names nothing in the run root from that step on).
    owners: the one path of each step, or None when the run root does not prove it. A step is proved only when its candidate set holds
    exactly one entry: a module file name.py and no name/ directory of any kind and no name with another suffix; a package
    name/__init__.py and no other __init__ suffix and no name.<suffix> beside it; or, for a step that is not the last, a namespace
    directory name/ and no name.<suffix> beside it. The last step must be a .py file (a module or a package __init__.py). The top-level
    name must not be a builtin or standard-library name (_reserved_names). A module is credited to a file only through owners. What
    the finder sees beyond the run root is a limit, stated in _no_path_to."""
    parts, suffixes = dotted.split("."), importlib.machinery.all_suffixes()
    candidates, owners = [], []
    for i, name in enumerate(parts):
        directory = "/".join(parts[:i])
        files, dirs = run.listing(directory)
        base = directory + "/" + name if directory else name
        found = {base + s for s in suffixes if name + s in files}
        if name in dirs:
            inits = {base + "/__init__" + s for s in suffixes if "__init__" + s in run.listing(base)[0]}
            found |= inits or {base + "/"}
        if not found:
            break
        candidates.append(found)
        one = next(iter(found)) if len(found) == 1 else ""
        # a step before the last must be a package (an __init__.py, or a namespace directory); the last must be a .py file
        proved = (i or name not in _reserved_names()) and (one.endswith(".py") if i == len(parts) - 1 else one.endswith(("/__init__.py", "/")))
        owners = owners + [one] if owners is not None and proved else None
    if owners is not None and len(owners) < len(parts):
        owners = None
    return _Identity(owners, candidates)


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
    """(names, members, whether every import resolved): `import a.b` puts a.b in names; `from a.b import c` puts a.b in names and
    (a.b, c) in members (c may be a module of a.b or an attribute a.b provably binds: _unresolved says which). A relative import is resolved
    as in _imported_module, and one that does not resolve makes the third False."""
    names, members, resolved = set(), set(), True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            source = _imported_module(path, node)
            if source is None:
                resolved = False
                continue
            names.add(source)
            members.update((source, a.name) for a in node.names if a.name != "*")
    return names, members, resolved


def _single_names(tree):
    """The names the module provably holds as attributes once it has run: those _single_binding proves bound once, in its module scope
    (a def, class, an assignment of any value, or an import alias, not under an if or try, nothing else binding, deleting it or writing
    it as an attribute, no star import, no dynamic form). The one place a member import (`from m import name`) is proved, through the
    one predicate, and what it proves is EXISTENCE, not value (round 8, Main). The question is only whether `from m import name` takes
    an attribute of m or falls back to importing a submodule m.name that may come from outside the run root; Python takes the attribute
    when m binds the name at module scope. One unconditional binding proves that, whatever the right-hand side: if it raises, m fails to
    import and so does the importer. What the name holds is m's business: m is in the import graph (_reaches), every module it imports
    is walked and every module the walk visits is held to the file's own tests (_refers), so a value that reaches the target makes a path. The proof does
    not judge the value. Because it asks whether the name exists, a write into the object it holds (`name[k] = v`, `name.x = v`) does
    not count (_single_binding, existence). A name that is only annotated, deleted, bound under a condition, written as `obj.name = v`,
    or bound twice is not in it."""
    return frozenset(name for name in _binding_index(tree).events if _single_binding(tree, name, existence=True) is not None)


_ANY = "*"  # not an identifier: the member _written_names puts in its set when the file may write any attribute


def _written_names(tree):
    """The names the file may unbind or rebind on another object, which may be a module: `h.x = v` and `del h.x` name x (_binding_index
    kind "write", role "attr"), and a Del of any form rooted at a name (`del x[k]`) names x. A Store into the object a name holds
    (`x[k] = v`) does not: it cannot remove the name from a module. A file that uses a dynamic form (_binding_index dynamic: setattr,
    delattr, vars, __dict__, globals, exec, eval, importlib, sys.modules...) may write or delete any attribute of any module, which no
    source line shows (round 8 attack): _ANY is then in the set, and _may_write is the one test of it."""
    index = _binding_index(tree)
    names = frozenset(name for name, events in index.events.items()
                      if any(e[0] == "write" and (e[4] == "attr" or isinstance(e[1].ctx, ast.Del)) for e in events))
    return names | {_ANY} if index.dynamic else names


def _may_write(written, name):
    """True when a set from _written_names (of one file, or the union over the run root) may write or delete an attribute called name."""
    return name in written or _ANY in written


def _path_links(module, target, through_package):
    """The links a name of the target is resolved through, in order: (holder, name) pairs, holder the dotted package whose attribute
    name is read, or None for the target itself (its binding in the defining file is proved by use_scan). through_package (the
    `modules` map of _use_lines: `import a.b.c`, `import a.b.c as x`, `from a.b import c`, and their relative forms): the code reaches
    the module object through package attributes, so every component after the first is a link (b held by a, c held by a.b), then the
    target. Else (`from a.b.c import T`, which reads sys.modules['a.b.c']): the target only."""
    parts = module.split(".")
    steps = [(".".join(parts[:i]), parts[i]) for i in range(1, len(parts))] if through_package else []
    return steps + [(None, target)]


def _holder_binds_other(holder, name, parsed, run):
    """True when the run root does not prove that the package holder's attribute name is only ever its submodule holder.name. The
    package is the one _module_identity proves for holder on the way to holder.name (round 11: a holder.py beside the package, an
    __init__ of another suffix, a standard-library top-level name all leave it unproved, and so True). Then: the package's __init__.py
    binds name by any statement other than an import of exactly that submodule (`from . import name`, `from <holder> import name`,
    `import <holder>.<name> as name`), that is an `as` import of another module, an assignment, a def or class, a for or with target,
    a global, a delete, a star import or a dynamic form (_binds); or it defines a module `__getattr__`, which can answer any name; or
    the __init__.py was not supplied (it is on disk only) or does not parse. A namespace directory (no __init__) binds nothing."""
    owners = _module_identity(holder + "." + name, run).owners
    if owners is None:
        return True
    init = owners[-2]
    if init.endswith("/"):
        return False
    tree = parsed.get(init)
    if tree is None or _binds(tree, "__getattr__", None, False, existence=True) != []:
        return True  # (None: a dynamic form or a star import, which binds names no statement shows)
    found = _binds(tree, name, None, False, existence=True)
    for kind, node, stmt, _scope, _role in found:
        if not (kind == "name" and isinstance(node, ast.alias)):
            return True
        if isinstance(stmt, ast.ImportFrom) and _imported_module(init, stmt) == holder and node.name == name:
            continue
        if isinstance(stmt, ast.Import) and node.name == holder + "." + name and node.asname == name:
            continue
        return True
    return False


def _path_proved(module, target, through_package, parsed, written, run):
    """THE one proof of a name the use scan attributes through (round 10, Main): every link the name is resolved through (_path_links)
    is proved. A link is unproved when some run-root file writes or deletes an attribute of the link's name on any object (_written_names,
    _may_write: no alias analysis, as round 8), or, for a link held by a package, when that package's __init__ may bind the name to
    something other than its submodule, or the package itself is not proved (_holder_binds_other, _module_identity). Every form of
    import is proved here, so no form can skip a link. (That the module is the definer's file at all is proved once per target, in
    use_scan, by _module_identity.)"""
    return not any(_may_write(written, name) or holder is not None and _holder_binds_other(holder, name, parsed, run)
                   for holder, name in _path_links(module, target, through_package))


# Round 12 (Main): the import state the scan models, one fixed tuple. Source: Python's language reference, "The import system"
# (5.4.4 import-related module attributes __name__, __loader__, __package__, __spec__, __path__, __file__, __cached__; 5.5 sys.path,
# sys.path_hooks, sys.path_importer_cache; 5.3 sys.meta_path, sys.modules; site, which builds sys.path) and __builtins__, which holds
# the __import__ every import statement calls; plus the _DYNAMIC_ACCESS words.
_STATE_ATTRS = ("__path__", "__package__", "__spec__", "__loader__", "__name__", "__file__", "__cached__", "__builtins__")
_STATE_SYS = ("path", "meta_path", "path_hooks", "path_importer_cache", "modules")
_IMPORT_STATE = _STATE_ATTRS + tuple("sys." + m for m in _STATE_SYS) + ("site",) + _DYNAMIC_ACCESS
# referenced at all (a read too) changes the state: these attributes, and the sys members only the import system has
_STATE_WORDS_RE = re.compile(r"(?<![A-Za-z0-9_])(?:__path__|__package__|__spec__|__loader__|meta_path|path_hooks|path_importer_cache)(?![A-Za-z0-9_])")


def _sys_node(node):
    """True for an expression that is the sys module by its spelling: the name sys, or an attribute .sys of anything (os.sys)."""
    return isinstance(node, ast.Name) and node.id == "sys" or isinstance(node, ast.Attribute) and node.attr == "sys"


def _changes_import_state(source, tree):
    """THE one test of whether running a file may change the import state the scan models (round 12, Main; _IMPORT_STATE). True when
    the file:
    - binds a module attribute of _STATE_ATTRS in any form, at any scope (assignment, augmented or annotated assignment, del, global or
      nonlocal, a for, with, walrus or match target, an import as, a def or class name), or writes or deletes it on any object
      (`m.__name__ = v`);
    - references __path__, __package__, __spec__, __loader__, sys.meta_path, sys.path_hooks or sys.path_importer_cache at all: its
      raw text holds the word (_STATE_WORDS_RE: comments and strings too, on identifier boundaries; a false hit only loses a line);
    - references sys.path or sys.modules (any `<sys>.path` or `<sys>.modules`, `<sys>` the name sys or an attribute .sys), imports one
      of _STATE_SYS from sys (or `*`), or holds sys other than as the object of an attribute read (`s = sys`, `f(sys)`,
      `import sys as s`), since no alias analysis is made;
    - imports the site module (which adds to sys.path), in any form;
    - holds a word of _DYNAMIC_ACCESS (its raw text).
    Reading __name__ and __file__ (`if __name__ == "__main__"`, `os.path.dirname(__file__)`) does not change it. _opaque calls this, and
    _runs_clean calls it on every file Python runs on the way to an import; nothing else checks import state."""
    if tree is None or _DYNAMIC_ACCESS_RE.search(source) or _STATE_WORDS_RE.search(source):
        return True
    objects = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}  # nodes read as the object of an attribute
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and (node.id in _STATE_ATTRS and isinstance(node.ctx, (ast.Store, ast.Del))
                                           or node.id == "sys" and id(node) not in objects and not isinstance(node.ctx, ast.Store)):
            return True
        if isinstance(node, ast.Attribute) and (node.attr in _STATE_ATTRS and isinstance(node.ctx, (ast.Store, ast.Del))
                                                or node.attr in ("path", "modules") and _sys_node(node.value)
                                                or node.attr == "sys" and id(node) not in objects):
            return True
        if isinstance(node, (ast.Global, ast.Nonlocal)) and not set(node.names).isdisjoint(_STATE_ATTRS):
            return True
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in _STATE_ATTRS:
            return True
        if isinstance(node, ast.alias) and ((node.asname or node.name.split(".")[0]) in _STATE_ATTRS
                                            or node.name == "sys" and node.asname not in (None, "sys")):
            return True
        if isinstance(node, ast.ImportFrom) and not node.level and (node.module == "sys" and any(a.name in _STATE_SYS + ("*",) for a in node.names)
                                                                  or (node.module or "").split(".")[0] == "site"):
            return True
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "site" for a in node.names):
            return True
        if type(node).__name__ in ("MatchAs", "MatchStar", "MatchMapping") \
                and (getattr(node, "name", None) or getattr(node, "rest", None)) in _STATE_ATTRS:
            return True
    return False


def _opaque(source, tree):
    """True when the file changes import state (_changes_import_state: which includes a word of _DYNAMIC_ACCESS) or has a star import:
    what it loads is then not shown by its imports. One of the per-file tests of _refers."""
    return _star(tree) or _changes_import_state(source, tree)


def _package_inits(path, run):
    """The package __init__ files Python runs before the file at path when it is imported by its dotted name from the run root, outer
    first: for a/b/u.py, the __init__ of a and of a/b. (init path, supplied): a supplied a/__init__.py, or the first __init__ of another
    suffix (or one on disk only) that the finder could load instead, which runs unseen. A namespace directory runs nothing."""
    dirs, out = path.split("/")[:-1], []
    for i in range(1, len(dirs) + 1):
        d = "/".join(dirs[:i])
        inits = sorted(d + "/__init__" + s for s in importlib.machinery.all_suffixes() if "__init__" + s in run.listing(d)[0])
        if d + "/__init__.py" in run.supplied and inits in ([d + "/__init__.py"], []):
            out.append((d + "/__init__.py", True))
        elif inits:
            out.append((inits[0], False))
    return out


def _suffixes(dotted):
    """Every dotted suffix of a dotted path: a.b.c is a.b.c, b.c and c. Any directory of the run root may be on sys.path, so a module
    is known by each of them."""
    parts = dotted.split(".")
    return {".".join(parts[i:]) for i in range(len(parts))}


class _Graph(dict):
    """{dotted module: (names, members, resolved, single, texts, written) or None} for the run root (see _import_graph), and the names the
    run root answers to: by_name {a name: [the modules it can be]} (every dotted suffix of each module's path) and packages (every
    dotted suffix of each directory a module sits in, whether or not it has an __init__.py), run, the _RunRoot the finder sees, and files
    {path: (source, tree), or None when it does not parse} of every supplied .py file."""

    def __init__(self, entries, run, files=None):
        super().__init__(entries)
        self.run, self.by_name, self.packages, self.files = run, {}, set(), files or {}
        for module in self:
            for name in _suffixes(module):
                self.by_name.setdefault(name, []).append(module)
            self.packages.update(_ancestor_names(module))

    def loaded(self, dotted):
        """The modules that importing `dotted` runs: those with a name that is `dotted` or one of its ancestor packages."""
        parts = dotted.split(".")
        return {m for i in range(1, len(parts) + 1) for m in self.by_name.get(".".join(parts[:i]), ())}


def _ancestor_names(module):
    """The names of the directories above a module path: for a.b.c, a and a.b and b (every suffix of each proper prefix)."""
    parts = module.split(".")
    return {name for i in range(1, len(parts)) for name in _suffixes(".".join(parts[:i]))}


def _import_graph(files, parsed, run):
    """_Graph of {dotted module: (names, members, resolved, single, texts, written) or None} for every .py file of the run root
    (_imports_of, _single_names, ((source, tree), ...) of its files for _refers, _written_names); None for one that does not parse (or
    when one of two files of the same module name does not)."""
    graph, texts = {}, {}
    for f in files:
        if f["path"].endswith(".py"):
            module, tree = _module_name(f["path"]), parsed[f["path"]]
            texts[f["path"]] = (_source(f["text"]), tree) if tree is not None else None
            found = (*_imports_of(f["path"], tree), _single_names(tree), ((_source(f["text"]), tree),), _written_names(tree)) \
                if tree is not None else None
            if module in graph:  # pkg.py and pkg/__init__.py: either may run, so the module holds the imports of both and proves only what both prove
                other = graph[module]
                if found is not None and other is not None:
                    found = (found[0] | other[0], found[1] | other[1], found[2] and other[2], found[3] & other[3], found[4] + other[4],
                             found[5] | other[5])
                else:
                    found = None
            graph[module] = found
    return _Graph(graph, run, texts)


def _unresolved(name, graph, attribute=False):
    """THE one rule for what a dotted import name is against the run root: True when it cannot be resolved. A name is resolved when it is
    a name of a run-root module or directory (_Graph: any dotted suffix of the path, a directory a package whether or not it has an
    __init__.py). The name of a `from m import c` member (attribute) is resolved too when m names run-root modules and every one of them
    proves c exists (_single_names: bound once at module scope, whatever the value) and no file of the run root writes or deletes an
    attribute c (_written_names). A name that is not resolved but has a proper prefix that is a name of the run root (`import h.missing` with a
    run-root h, `from h import missing` when h does not provably bind missing, `from .missing import x` in a package) is unresolved: it
    names no module of the run root and may be one nobody gave us, so it counts as a path to every module. A name none of whose
    prefixes is a name of the run root is outside it and is not unresolved.
    Round 11: before any of that, the name is unresolved when Python's finder may load, for some step of it, a file of the run root
    the scan was not given (_module_identity's candidates: an __init__.py or module on disk only, a .pyc, an extension module): what it
    runs is not shown. A namespace directory runs nothing."""
    def known(dotted):
        return dotted in graph.by_name or dotted in graph.packages

    if any(c not in graph.run.supplied and not c.endswith("/") for step in _module_identity(name, graph.run).candidates for c in step):
        return True
    if known(name):
        return False
    parent, _dot, last = name.rpartition(".")
    if attribute and parent:
        owners = [graph[m] for m in graph.by_name.get(parent, ())]
        if owners and all(o is not None and last in o[3] and not any(g is not None and _may_write(g[5], last) for g in graph.values()) for o in owners):
            return False
    parts = name.split(".")
    return any(known(".".join(parts[:i])) for i in range(1, len(parts)))


def _walk_blocked(texts, names, members, resolved, graph, test, stop=lambda dotted: False):
    """THE walk of what code runs (round 9; round 12 shares it): True when code whose own files are `texts` ((source, tree) pairs) and
    that imports the dotted names `names` and the (module, member) pairs `members` may run a file that fails the per-file test `test`
    (source, tree) or an import that `stop` (dotted) refuses. Importing a module runs the modules of its ancestor packages (_Graph.loaded),
    and each module it loads is followed through its own imports (transitively). An import that cannot be resolved (`resolved` False: a
    relative one above the run root; or _unresolved, made on every import, the file's own and those of each module it loads), and a
    loaded file that does not parse, count as blocked: what runs there is not shown."""
    if not resolved or any(test(source, tree) for source, tree in texts):
        return True
    seen, todo = set(), [(n, False) for n in names] + [(s + "." + c, True) for s, c in members]
    while todo:
        item = todo.pop()
        if item in seen:
            continue
        seen.add(item)
        dotted = item[0]
        if _unresolved(dotted, graph, item[1]) or stop(dotted):
            return True
        for m in graph.loaded(dotted):
            entry = graph[m]
            if entry is None or not entry[2] or any(test(source, tree) for source, tree in entry[4]):
                return True
            todo.extend([(n, False) for n in entry[0]] + [(s + "." + c, True) for s, c in entry[1]])
    return False


def _reaches(texts, names, members, resolved, name, module, graph):
    """True when code whose own files are `texts`, that imports `names` and `members`, may reach the name defined in the dotted module
    `module` (round 9: the file and every module it can load are one closure, and every per-file test is made on each of them;
    _walk_blocked). An import counts when it can name the target: it is equal to one of the target's names (every dotted suffix of its
    path: src.h.t is also h.t and t, as any directory may be on sys.path), a descendant of one, or the name of a directory above it (an
    ancestor package: src, src.h, h). The code's own files and every module the walk visits are held to the same per-file tests, _refers,
    with the same target tokens: one that refers to the target counts as a path. An unresolved import, a file that does not parse, all
    count as a path."""
    own, above = _suffixes(module), _ancestor_names(module)
    return _walk_blocked(texts, names, members, resolved, graph, lambda source, tree: _refers(source, tree, name, module),
                         lambda dotted: dotted in above or any(_inside(dotted, n) for n in own))


def _own_files(path, source, tree, graph):
    """((source, tree), ...) of the file at path and of the package __init__s Python runs before it (_package_inits), or None when one
    of those __init__s is not supplied (it runs unseen) or does not parse."""
    texts = [(source, tree)]
    for init, supplied in _package_inits(path, graph.run):
        if not supplied or graph.files.get(init) is None:
            return None
        texts.insert(-1, graph.files[init])
    return tuple(texts)


def _runs_clean(path, source, tree, graph):
    """THE set of files that must not change import state for an import in the file at path to be credited (round 12, Main): the file
    itself, every package __init__ on its own package path (they run first, and the package decides __package__), and every run-root
    module it can load, transitively (_walk_blocked), which takes in the target module T and every package __init__ on T's dotted
    path. True when none of them changes import state (_changes_import_state), every one was supplied and parses, and every import on the
    way resolves. A file that does not import T does not run T's path, so the test is per importing file: a T whose path changes import
    state is credited through no import, and a file with no path to T keeps its "does not use" line (_no_path_to)."""
    texts = _own_files(path, source, tree, graph)
    return texts is not None and not _walk_blocked(texts, *_imports_of(path, tree), graph, _changes_import_state)


def _string_text(node):
    """The text a string expression is made of, or None when it is not one: a str or bytes constant; the `+` of two such; an f-string,
    whose constant parts are joined with a NUL where a value is formatted in (a name split by a value is two tokens)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
        return node.value if isinstance(node.value, str) else node.value.decode("latin-1")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _string_text(node.left), _string_text(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "\0".join(_string_text(part) or "" for part in node.values)
    return None


def _string_mentions(tree, tokens):
    """True when some string of the file, split on `.`, `:` and `/`, has a token that is in tokens (round 8b: a name in a string is a
    reference). Strings are read as written (_string_text: one constant, a `+` of constants, the constant parts of an f-string), and
    every constant on its own as well, so `"pkg.sub." + "t"`, `f"{pkg}.t.Q"`, `"t:Q"` and `"pkg/sub/t.py"` all mention t. A token must
    be equal: `"QUEUE"` does not mention Q."""
    for node in ast.walk(tree):
        text = _string_text(node)
        if text is not None and not tokens.isdisjoint(re.split(r"[.:/\0]", text)):
            return True
    return False


def _refers(source, tree, name, module):
    """THE per-file tests of the absence path (round 9: one function, made on the file scanned and on every module _reaches visits, so a
    test cannot be added to one and not the other). True when the file's source or tree may refer to the name defined in the dotted
    module `module`; none of these is a list of syntax cases:
    - the bare name occurs in the raw text (code, strings, bytes, comments, docstrings), on identifier boundaries;
    - a string of the file is a reference to the target (_string_mentions, round 8b): split on `.`, `:` and `/`, it has a token equal
      to the name or to the last component of the module's names, whether the string is one constant, a `+` of constants, or the
      constant parts of an f-string. Every string-built loader (pydoc.locate, unittest.mock.patch, logging.config, an entry point
      "t:Q", a path "pkg/sub/t.py", pkgutil.resolve_name, ...) needs the target's name as text, and the set of loaders is open while
      the text is not, so this is a rule over strings and not a list of loaders;
    - the file is opaque (_opaque): its text holds a word of _DYNAMIC_ACCESS, or it has a star import."""
    return (_opaque(source, tree) or bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", source))
            or _string_mentions(tree, {name, module.split(".")[-1]}))


def _no_path_to(source, tree, path, name, module, graph):
    """True when the file's text and the run root's import graph prove the file has no path to the name defined in the dotted module
    `module`, so that "does not use" may be said: no path through _reaches, which makes the per-file tests (_refers: the bare name in
    the raw text, a string reference, a _DYNAMIC_ACCESS word, a star import) on the file and, with the same target tokens, on every
    run-root module it can load (directly, through any number of imports, an ancestor package's __init__, a `from pkg import sub`
    member, an import cycle), and finds none of its imports, nor those of any module it loads, to be the target module, an ancestor
    package or a submodule of it, or unresolvable. A module the file can load is part of what the file runs. Anything else gives no
    absence line.
    The limit, stated once: a name that reaches the code from outside its source (a configuration file, the environment, the network),
    or that the source computes without writing any token of it anywhere in the closure (every character from chr, say), is beyond
    static proof; the absence line is a statement about the source it was given. A finding that needs such a name is closed by this
    limit; a finding where a token of the target is written anywhere in the closure is not.
    The limit of module identity (round 11, _module_identity), stated once: the run root is taken as sys.path[0], and a run with no
    run root on disk sees only the supplied files. Another sys.path entry (which may add a namespace portion, or answer a top-level
    name the run root has only as a namespace directory) or a .pth file is beyond static proof.
    The limit of import state (round 12, _changes_import_state, _runs_clean), stated once: the scan takes each file as imported by its
    dotted name from the run root, in a fresh interpreter under Python's default import state; a file Python runs on the way that may
    change that state is opaque. Code outside the run (site-packages, .pth files, sitecustomize and usercustomize, PYTHONPATH, the
    interpreter's own startup) and whatever ran in the process before the file was imported (the entry script, an importer of the file)
    are beyond static proof.
    The file's own package __init__s (_package_inits) run before it and are held to the same tests; one that was not supplied is a
    path."""
    texts = _own_files(path, source, tree, graph)
    return texts is not None and not _reaches(texts, *_imports_of(path, tree), name, module, graph)


def _use_lines(f, tree, target, kind, module, others, members, dup, parsed=None, written=frozenset(), run=None, credited=True):
    """(imports, creates, uses, undecided) for one target in one other file: lists of (line, text), and whether the file refers to
    the target name in a way this scan cannot attribute (an import of it from a module that is not the target's by its full dotted
    path, `from m import *`, a dynamic form such as globals() or setattr (_DYNAMIC; the file then gives no line at all), an import of the target that is rebound or may be overwritten, a use of the bare name or of `x.<name>`
    that no import of the target explains; a name in a string is decided by _no_path_to, _string_mentions) or binds it from two of the definers (ambiguous). module is the
    target's dotted module name from the run root, others those of the other files that define the same name. An import counts as
    the target's only when its module resolves to `module` by full dotted path (a basename alone never does). A call, a creation or
    a use is attributed only through a name that _single_binding proves bound once, by that import: a name that is bound again,
    written (`t.Q = x`), shadowed, or in a file with a star import (_star: a parameter attribution too) gives the import line at most. dup (the name is defined in more
    than one file) names the target `<module>.<T>` in each line and takes a use through a parameter only from a file that imports
    this module.
    The rule over links (round 10, Main): a call, creation or use is attributed through a name only when every link the name is
    resolved through is proved (_path_proved, the one function every form calls; parsed is {path: tree} of the run root, written the
    union of _written_names over it). A direct member import (`from a.b.c import T`) reads sys.modules['a.b.c'] and has one link, T:
    when some file of the run root writes or deletes an attribute named T (`x.T = v`, `x.T += v`, `del x.T`, any x; round 8) the import
    lines are the only facts of the file, and it is undecided. A module-path name (`import a.b.c`, `import a.b.c as x`,
    `from a.b import c`, relative forms too) also goes through each attribute step after the first component (b, c): when one of those
    is written or deleted anywhere, or the __init__ of its package binds it by anything but an import of exactly that submodule
    (_holder_binds_other), nothing is attributed through the module-path names and the file is undecided; direct member imports of the
    same file keep their facts. No alias analysis: the write of the attribute name is the block.
    Round 11 (Main): credited is whether _module_identity proves module is the definer's file, every package __init__ on the way
    supplied (use_scan); when it is not, the import
    lines of `from <module> import <T>` are the only facts of the file and it is undecided. run is the _RunRoot (default: the parsed
    paths only)."""
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
    parsed = parsed or {}
    run = run or _RunRoot(parsed)
    if not credited:
        return sorted(imports, key=lambda item: item[0]), [], [], True
    if (direct or modules) and not _path_proved(module, target, False, parsed, written, run):
        return sorted(imports, key=lambda item: item[0]), [], [], True
    valid_direct = {name for name, stmt in direct.items() if _single_binding(tree, name, shadow=True) is stmt}
    valid_modules = {code for code, (bound, stmt) in modules.items() if _single_binding(tree, bound, shadow=True) is stmt}
    if valid_direct != set(direct) or valid_modules != set(modules):
        # the import is a fact; what the name then is, is not shown. Nothing is attributed through a name that may not be the import.
        return sorted(imports, key=lambda item: item[0]), [], [], True
    if modules and not _path_proved(module, target, True, parsed, written, run):
        # a package link may hold another object: no line through a module-path name, nor through a parameter when no direct import
        # of the target is left to explain it
        if not valid_direct:
            return sorted(imports, key=lambda item: item[0]), [], [], True
        valid_modules, unexplained = set(), True
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == target:
            unexplained = unexplained or node.id not in other_direct  # a use of this module's name is a line, or else (a star import) undecided
        elif isinstance(node, ast.Attribute) and node.attr == target:
            unexplained = unexplained or _dotted(node.value) not in set(modules) | other_modules

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


def use_scan(task, files, root=None):
    """Code-made fact lines about who uses a class or top-level function (the target) that the task names as a whole word, that a
    named .py file defines, and whose binding there is proven single (_single_binding). Per other .py file: its import, creation or
    call, and the attribute uses through a variable bound once from T(...) or a parameter (only of a name the class defines, dunder
    methods too), each only through an import of the target by its full dotted path whose name nothing else binds, writes or
    shadows (see _use_lines); `from m import *` attributes nothing. Round 10 (Main): a line is attributed through a name only when
    every link the name is resolved through is proved (_path_proved): for a module-path name (`import a.b.c`, `import a.b.c as x`,
    `from a.b import c`) each attribute step after the first component and the target; for `from a.b.c import T` the target only. A
    link is unproved when a run-root file writes or deletes an attribute of its name, or the __init__ of the package that holds it
    binds it by anything but an import of exactly that submodule. Round 11 (Main): a target is credited through its module only when
    _module_identity proves the module's name is that file and nothing else Python's finder could load from the run root (root, the
    run root on disk, as sys.path[0]; with no root, only the supplied files are seen), and every package __init__ on the way was
    supplied (an __init__ on disk only runs unseen); else every other file gives its
    `from <module> import <T>` import lines at most, and no "does not use" line. A file with none of these gives `<base> does not use <T> [path:1]`,
    unless the file's text and the run root's import graph do not prove it has no path to the name (_no_path_to: a star import, a word of
    _DYNAMIC_ACCESS, the name anywhere in the text, a string that names it or its module, in the file or in any run-root module it can
load (_refers), or a path through the imports to the target's module): then it gives nothing. When more than one file
    defines the name, each definer is scanned as its own module and its lines say `<module>.<T>`; a file that defines the name itself,
    or whose binding of the name is ambiguous, gives nothing for it."""
    out = []
    parsed = {f["path"]: _parse(f["path"], f["text"]) for f in files}
    run = _RunRoot(parsed, root)
    graph = _import_graph(files, parsed, run)
    # round 8: every attribute name some file of the run root writes or deletes (_written_names), on any object; a file that does not
    # parse cannot run, so it writes nothing
    written = frozenset().union(*(_written_names(t) for t in parsed.values() if t is not None))
    clean = {}
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
            owners = _module_identity(module, run).owners if f["path"].endswith(".py") else None
            # the module is the definer's file, and every package __init__ the import runs on the way was supplied (one on disk only
            # runs unseen, and may rebind what the scan attributes through)
            credited = owners is not None and owners[-1] == f["path"] and all(o.endswith("/") or o in run.supplied for o in owners)
            members = _members(parsed[f["path"]], name) if kind == "class" else set()
            for g in files:
                if g["path"] in definers or parsed[g["path"]] is None:
                    continue
                if g["path"] not in clean:  # round 12: nothing Python runs on the way to g's imports changes import state
                    clean[g["path"]] = g["path"].endswith(".py") and _runs_clean(g["path"], _source(g["text"]), parsed[g["path"]], graph)
                imports, creates, uses, undecided = _use_lines(g, parsed[g["path"]], name, kind, module, others, members, len(definers) > 1, parsed, written,
                                                        run, credited and clean[g["path"]])
                found = [text for _n, text in imports + creates + uses]
                if found:
                    out.extend(found)
                elif not undecided and _no_path_to(_source(g["text"]), parsed[g["path"]], g["path"], name, module, graph):
                    out.append("- {} does not use {} [{}:1]".format(_base(g), "{}.{}".format(module, name) if len(definers) > 1 else name, g["path"]))
    return list(dict.fromkeys(out))


def code_facts(task, files, root=None):
    """(diff lines, use lines) made by code for this task and these files; root is the run root on disk, if any (use_scan)."""
    diff = [line for old, new in version_pairs(files) for line in version_diff(old, new)]
    return list(dict.fromkeys(diff)), use_scan(task, files, root)


def user_message(task, files, root=None):
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
    diff, uses = code_facts(task, files, root)
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
    reply = ask(endpoint, model, settings["system"], user_message(task, files, root), settings["schema"], timeout)
    facts = json.loads(reply).get("facts") if isinstance(json.loads(reply), dict) else None
    if not isinstance(facts, list):
        raise AskError("reply has no facts list", _completions(endpoint), 200)
    defined = {f["path"]: {name for _l, _k, name, _o in outline(f["path"], f["text"])} for f in files}
    kept, drops = validate(facts, {f["path"]: f["lines"] for f in files}, defined)
    index = name_index(files)
    diff, uses = code_facts(task, files, root)
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
