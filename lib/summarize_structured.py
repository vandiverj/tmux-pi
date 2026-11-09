"""summarize_files without a tool loop (docs/plans/det-prep/ls1c-structured-summarize.md).

tmux-pi reads the named files and numbers their lines (prepare), makes one structured-output call to a local
OpenAI-compatible endpoint (ask), drops every fact whose citation does not check out (validate) and writes the rest as
fact lines (render). summarize() ties the four together. No line number is ever invented or repaired.

The system prompt and the JSON schema are not here: they are the "summarize_structured" key of config/local.json (the
template is config/local.example.json), read by load_settings through lib/router.load_config.
"""
import ast
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


def _validate_v2(fact, files):
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
    return {"claim": "{} {}".format(_subject(subject, files), claim.strip()), "path": path, "lines": sorted(set(lines))}, None


def validate(facts, files):
    """(kept, drops). files maps each named path to its line count. A fact is {subject, claim, path, lines}, lines being 1 to
    MAX_LINES separate line numbers; or the older {claim, path, line_start, line_end}. It is dropped when it is malformed (a lines
    list that is empty, longer than MAX_LINES or holds a non-integer or a bool), its path is not a named file, a line is outside the
    file, or it has no claim; an older fact also when its range is reversed or spans more than MAX_SPAN lines. A line number is
    never changed (a v2 fact's lines are only de-duplicated and sorted). Each drop is {"fact": <the fact as given>, "reason": ...}."""
    kept, drops = [], []
    for fact in facts:
        if isinstance(fact, dict) and "lines" in fact:
            good, reason = _validate_v2(fact, files)
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


def outline(path, numbered_text):
    """The names a Python file defines, read with ast from its numbered text (prepare's `{n:>4}\\t{line}`): a list of
    (line, kind, name, owner) in source order. kind is class, function, method, constant (a module-level assignment to a plain
    name) or parameter (of a function or method, except self and cls; its owner is that function). A method's owner is its class.
    Functions inside functions are not walked. A file that is not .py, or does not parse, gives []."""
    if not path.endswith(".py"):
        return []
    source = "\n".join(line.split("\t", 1)[1] if "\t" in line else "" for line in numbered_text.split("\n"))
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
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
                for arg in args.posonlyargs + args.args + args.kwonlyargs:
                    if arg.arg not in ("self", "cls"):
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
    return "# Task\n\n{}\n\n# Files (numbered; take line numbers from here)\n\n{}".format(task, "\n\n".join(parts))


def summarize(root, paths, task, *, endpoint, model, settings, budget_chars=DEFAULT_BUDGET_CHARS, timeout=600):
    """(text, record). settings is {"system": ..., "schema": ...} (see load_settings). The text is name_index(files), made by code,
    then the rendered facts. Raises PrepareError or AskError; a reply whose JSON lacks a facts list is an AskError too. The record:
    files (path, lines, chars), chars, drops, kept, indexed (the index lines), seconds, model."""
    started = time.monotonic()
    files = prepare(root, paths, budget_chars)
    reply = ask(endpoint, model, settings["system"], user_message(task, files), settings["schema"], timeout)
    facts = json.loads(reply).get("facts") if isinstance(json.loads(reply), dict) else None
    if not isinstance(facts, list):
        raise AskError("reply has no facts list", _completions(endpoint), 200)
    kept, drops = validate(facts, {f["path"]: f["lines"] for f in files})
    index = name_index(files)
    record = {"files": [{k: f[k] for k in ("path", "lines", "chars")} for f in files],
              "chars": sum(f["chars"] for f in files), "kept": len(kept), "drops": drops, "indexed": len(index),
              "seconds": round(time.monotonic() - started, 2), "model": model}
    return "\n".join(index + ([render(kept)] if kept else [])), record


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
