"""Claude Code hook policy for redaction; see docs/sensitive-read-redaction-design.md §4.3-4.4.

PreToolUse denies or asks only for real secret stores, refuses placeholders, and appends
` || echo exit-code: nonzero` to Bash so failing output stays on PostToolUse, the only
event whose output a hook can replace. PostToolUse redacts every string in the result
and keeps its shape. Mode comes from TP_REDACTION or config/local.json "redaction.mode":
off, shadow (default: log what would happen; the sensitive-read ask stays) or on.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import signal
import time

from placeholders import find, rehydrate, resolve_all
from redact import Redactor, exact_values
from redact_llm import LocalClassifier
from redact_rules import DOTENV, PLACEHOLDER, is_secret_name
from redaction_store import Store, default_root, sweep

ROOT = Path(__file__).resolve().parent.parent
MODES = ("off", "shadow", "on")
SUFFIX = " || echo exit-code: nonzero"
# Seconds. The watchdog must stay below the hook timeout in hooks.json (60), which lets raw output through, and
# above the model review budget (config/redaction.json) plus the deterministic pass.
BUDGET = int(os.getenv("TP_REDACTION_WATCHDOG") or 45)
STUB = "[tmux-pi: output withheld because redaction did not finish ({}). Re-run a narrower command.]"
KEEP_KEYS = {"type", "mode", "filePath", "filenames", "persistedOutputPath", "base64"}
LOG_LIMIT = 5_000_000

PROD_ENV = re.compile(r"^\.env\.(?:production|prod|staging|stage)(?:\..*)?$", re.I)
EXAMPLE_ENV = re.compile(r"\.(?:example|sample|template|dist)$", re.I)
KEY_FILE = re.compile(r"^id_(?:rsa|dsa|ecdsa|ed25519)(?:_sk)?$|\.(?:p12|pfx)$", re.I)
MAYBE_KEY_FILE = re.compile(r"\.(?:pem|key)$", re.I)
SSH_PUBLIC = re.compile(r"\.pub$|^known_hosts|^config$|^authorized_keys")
CREDENTIAL_FILE = re.compile(r"^(?:credentials(?:\.json)?|[^/]*\.tfvars|secrets?\.(?:json|ya?ml|toml))$", re.I)
PATHISH = re.compile(r"[~\w./+@-]*[./][~\w./+@-]*")
ENV_REF = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", ";;"}
PREFIXES = {"sudo", "env", "command", "exec", "time", "nohup", "nice", "xargs", "builtin"}
LISTERS = {"ls", "find", "stat", "test", "[", "du", "wc", "file", "realpath", "readlink", "basename", "dirname",
           "touch", "mkdir", "chmod", "rm"}
NETWORK = {"curl", "wget", "nc", "ncat", "netcat", "socat", "ssh", "scp", "sftp", "rsync", "ftp", "telnet", "http",
           "https", "xh", "gh", "aws", "gsutil", "az", "pbcopy", "mail", "sendmail"}
TRANSFORM = {"base64", "xxd", "od", "hexdump", "rev", "strings", "tr", "gzip", "bzip2", "xz", "zstd", "uuencode",
             "iconv", "openssl"}
READ_TOOLS = {"Read": "file_path", "NotebookRead": "notebook_path", "Grep": "path"}
WRITE_TOOLS = {"Bash", "Edit", "MultiEdit", "Write", "NotebookEdit"}
FILE_TOOLS = {"Edit", "MultiEdit", "Write"}
FILE_KEYS = {"file_path", "notebook_path"}
REVEAL_TOOL = "mcp__tmux-pi-safe__reveal"
SECRET_EXEC = ROOT / "bin/tp-secret-exec"


def mode():
    value = os.getenv("TP_REDACTION")
    if not value:
        try:
            config = json.loads(Path(os.getenv("TP_LOCAL_CONFIG") or ROOT / "config/local.json").read_text())
            value = (config.get("redaction") or {}).get("mode")
        except (OSError, ValueError, AttributeError):
            value = None
    return value if value in MODES else "shadow"


def store_paths():
    home = Path.home()
    config = Path(os.getenv("CLAUDE_CONFIG_DIR") or home / ".claude")
    return {"dirs": [home / ".ssh", home / ".gnupg", home / ".config/gcloud", default_root()],
            "files": [home / ".aws/credentials", home / ".kube/config", home / ".docker/config.json", home / ".netrc",
                      home / ".npmrc", home / ".pypirc", home / ".config/gh/hosts.yml", ROOT / "claude/mcp.local.env",
                      config / ".credentials.json"]}


def resolve(value, cwd):
    path = Path(os.path.expanduser(value))
    try:
        return (path if path.is_absolute() else Path(cwd) / path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def path_kind(resolved, stores=None):
    """'blocked' (production or staging env), 'store' (credential store), 'secret' (env file) or None."""
    if resolved is None:
        return None
    stores = stores or store_paths()
    name = resolved.name
    if PROD_ENV.match(name):
        return "blocked"
    for base in stores["dirs"]:
        base = base.expanduser().resolve()
        if resolved == base or base in resolved.parents:
            return None if base.name == ".ssh" and resolved.is_file() and SSH_PUBLIC.search(name) else "store"
    if any(resolved == f.expanduser().resolve() for f in stores["files"]) or KEY_FILE.search(name):
        return "store"
    if MAYBE_KEY_FILE.search(name) and resolved.is_file():
        try:
            with open(resolved, "rb") as handle:
                if b"PRIVATE KEY" in handle.read(8192):
                    return "store"
        except OSError:
            pass
    if resolved.is_file() and ((DOTENV.search(name) and not EXAMPLE_ENV.search(name)) or CREDENTIAL_FILE.match(name)):
        return "secret"
    return None


def words(command):
    """Shell words per line; punctuation splits commands. Unparseable lines fall back to whitespace."""
    out = []
    for line in command.split("\n"):
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
            lex.whitespace_split = True
            out += list(lex) + [";"]
        except ValueError:
            out += line.split() + [";"]
    return out


def simple_commands(command):
    """Yield (command word, args, redirected inputs) for each simple command."""
    current, inputs, after = [], [], None
    for token in words(command) + [";"]:
        if token in SEPARATORS:
            if current:
                while current and (current[0] in PREFIXES or current[0].startswith("-") or re.match(r"^\w+=", current[0])):
                    current.pop(0)
                if current:
                    yield os.path.basename(current[0]), current[1:], inputs
            current, inputs, after = [], [], None
        elif token in {"<", ">", ">>", "<<", "<<<", ">&", "<&"}:
            after = token
        elif after:
            if after == "<":
                inputs.append(token)
            after = None
        else:
            current.append(token)


def candidates(token):
    seen = {token, token.lstrip("@"), token.split("=", 1)[-1].lstrip("@")}
    seen.update(PATHISH.findall(token))
    return [c for c in seen if c and len(c) < 4096]


def bash_reads(command, cwd):
    """Classify what a command reads: {kinds}, network, transform, [(kind, path)]."""
    stores, reads, network, transform = store_paths(), [], False, False
    for word, args, inputs in simple_commands(command):
        network |= word in NETWORK
        transform |= word in TRANSFORM
        lister = word in LISTERS and not (word == "find" and {"-exec", "-execdir", "-ok", "-okdir"} & set(args))
        operands = list(inputs) + ([] if lister else [a for a in args if not a.startswith("-") or "=" in a])
        for token in operands[:200]:
            for value in candidates(token):
                resolved = resolve(value, cwd)
                kind = path_kind(resolved, stores)
                if kind:
                    reads.append((kind, str(resolved)))
    return {k for k, _ in reads}, network, transform, reads


def wrap(command):
    """Command plus SUFFIX so a failing command still exits 0 through PostToolUse; None when unsafe."""
    text = command.rstrip()
    while text.endswith(";") and not text.endswith(";;"):
        text = text[:-1].rstrip()
    if not text:
        return None
    # Claude Code's command checker rejects a heredoc command once anything is appended to it.
    if "<<" in text:
        return None
    lines = text.split("\n")
    line = lines[-1]
    if re.search(r"(?:\\|\|\||&&|\||&|\{|\(|\bdo|\bthen|\belse)$", line.rstrip()):
        return None
    if re.search(r"(?:^|[;&|(\s])exit(?:\s|$|;)", text):
        return None
    try:
        if shlex.split(line, comments=True) != shlex.split(line):
            return None
    except ValueError:
        return None
    if text.endswith(SUFFIX):  # already wrapped (a retry of a rewritten call): wrapping again would stack it
        return text
    lines[-1] = line.rstrip() + SUFFIX
    return "\n".join(lines)


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def store_name(event):
    """Store key: the tp-claude launch id when set (the MCP server shares it), else the Claude session id."""
    return str(os.getenv("TP_REDACTION_LAUNCH") or event.get("session_id") or "default")


def origin(data, name):
    entry = data.get("entries", {}).get(name) or {}
    return ", ".join("{}:{}".format(p["path"], p["line"]) if p.get("line") else p["path"]
                     for p in entry.get("sources", [])[:3]) or "command output"


def describe(data, name):
    """Placeholder, where it came from and its kind; never the value."""
    return "{} ({}, kind {})".format(name, origin(data, name), (data.get("entries", {}).get(name) or {}).get("kind", "unknown"))


def normalize(value, cwd):
    path = Path(os.path.expanduser(str(value)))
    return (path if path.is_absolute() else Path(cwd) / path).resolve()


def map_strings(value, fn, key=None):
    if isinstance(value, dict):
        return {k: map_strings(v, fn, k) for k, v in value.items()}
    if isinstance(value, list):
        return [map_strings(v, fn, key) for v in value]
    return fn(value) if isinstance(value, str) and key not in FILE_KEYS else value


def refuse(result, target, reason):
    return {**result, "decision": "deny", "target": target, "reason": reason, "input": None}


def file_placeholders(event, params, cwd, result):
    """Edit/Write/MultiEdit: placeholders from the file being edited are filled in silently, anything else is refused."""
    names = find("\n".join(strings({k: v for k, v in params.items() if k not in FILE_KEYS})))
    if not names:
        return None
    if any(PLACEHOLDER.search(str(params.get(k) or "")) for k in FILE_KEYS):
        return refuse(result, names[0], "A file path cannot contain a redaction placeholder.")
    data = Store(store_name(event)).peek()
    unknown = [n for n in names if n not in data.get("entries", {})]
    if unknown:
        return refuse(result, unknown[0], f"{unknown[0]} is not a placeholder known in this session. Read the file again "
                      "to get current placeholders, or edit around that line.")
    target = normalize(params.get("file_path") or params.get("notebook_path") or "", cwd)
    foreign = [n for n in names if not any(normalize(p["path"], cwd) == target
                                           for p in data["entries"][n].get("sources", []))]
    if foreign or params.get("notebook_path"):
        found = foreign[0] if foreign else names[0]
        return refuse(result, found, f"{found} does not come from {target.name}. Writing it here would need the value in "
                      "this prompt. Use a Bash command with the placeholder (the user is asked, and the value is "
                      "substituted without being shown), or edit around that line.")
    values = resolve_all(data, names)
    if len(values) != len(names):
        return refuse(result, names[0], "A placeholder's source changed since it was read, so its value cannot be "
                      "verified. Read the file again.")
    return {**result, "decision": "allow", "target": names[0], "placeholders": names, "record": None,
            "reason": "Placeholders from this file were filled in with their original values.",
            "input": map_strings(params, lambda v: rehydrate(v, values))}


def bash_placeholders(event, command, params, result, kinds_info):
    """Bash: run the command through tp-secret-exec, which substitutes values the prompt never shows."""
    names = find(command)
    # Running it needs --session (no abbreviations), so reading, grepping or documenting the script stays allowed.
    if "tp-secret-exec" in command and "--session" in command:
        return refuse(result, "tp-secret-exec", "tp-secret-exec is added by the tmux-pi hook. Write the command with "
                      "placeholders and run it normally.")
    if not names:
        return None
    name = store_name(event)
    data = Store(name).peek()
    unknown = [n for n in names if n not in data.get("entries", {})]
    if unknown:
        return refuse(result, unknown[0], f"{unknown[0]} is not a placeholder known in this session. Read the file "
                      "again to get current placeholders.")
    if "<<" in command:
        return refuse(result, names[0], "A heredoc cannot take a placeholder safely. Use a simpler command, or write "
                      "the file with Edit/Write from the same file's placeholders.")
    kinds, network, transform = kinds_info
    shown = "; ".join(describe(data, n) for n in names)
    ask = (f"This command uses {shown}. Allow the real values to be substituted locally for this command? Later uses "
           f"of the same placeholder in this session will not ask again (this session only).")
    wrapped = "{} --session {} -- {}".format(shlex.quote(str(SECRET_EXEC)), shlex.quote(name), shlex.quote(command))
    granted = all(data.get("grants", {}).get(n) == "granted" for n in names)
    decision = "allow" if granted and not (network or transform or kinds) else "ask"
    if decision == "ask" and granted:
        ask = f"This command uses {shown} (already approved), but it also reads or sends sensitive files. " + ask
    return {**result, "decision": decision, "target": names[0], "placeholders": names, "record": "pending",
            "reason": ask if decision == "ask" else f"Placeholders already approved this session: {shown}.",
            "input": {**params, "command": wrapped}, "scope": "session", "command": command,
            "source": "; ".join(origin(data, n) for n in names)}


def pre_decision(event):
    """Return {decision, reason, target, input, ...} for a PreToolUse event; decision None means no prompt.

    Pure: nothing is written. The handler records grants from `placeholders`/`record`.
    """
    tool, params, cwd = event.get("tool_name", ""), dict(event.get("tool_input") or {}), event.get("cwd") or os.getcwd()
    result = {"decision": None, "reason": "", "target": "", "input": None, "placeholders": [], "record": None,
              "source": "", "scope": ""}
    if tool == REVEAL_TOOL:
        name = str(params.get("placeholder") or "")
        data = Store(store_name(event)).peek()
        if name not in data.get("entries", {}):
            return refuse(result, name, f"{name or 'That'} is not a placeholder known in this session.")
        return {**result, "decision": "ask", "target": name, "placeholders": [name], "source": origin(data, name),
                "scope": "once", "reason": "The model asks to see the real value of {}. Reason: {}. Approving sends the "
                "value to the model provider; it is shown to you nowhere. Approve only if that is acceptable.".format(
                    describe(data, name), str(params.get("reason") or "none given")[:300])}
    if tool in FILE_TOOLS or tool == "NotebookEdit":
        placed = file_placeholders(event, params, cwd, result)
        if placed:
            return placed
    if tool in READ_TOOLS and params.get(READ_TOOLS[tool]):
        resolved = resolve(str(params[READ_TOOLS[tool]]), cwd)
        if path_kind(resolved) in {"blocked", "store"}:
            return {**result, "decision": "deny", "target": str(resolved), "reason":
                    f"tmux-pi blocks reading {resolved}: it is a credential store or a production/staging env file. "
                    "Use mcp__tmux-pi-safe__safe_read (redacted) for its structure, or ask the user if a specific "
                    "value is needed."}
        return result
    if tool != "Bash":
        return result
    command = str(params.get("command") or "")
    kinds, network, transform, reads = bash_reads(command, cwd)
    holds = bash_placeholders(event, command, params, result, (kinds, network, transform))
    if holds and holds["decision"] == "deny":
        return holds
    wrapped = None if params.get("run_in_background") or holds else wrap(command)
    if wrapped and wrapped != command:  # nothing to change: no updated input
        result["input"] = {**params, "command": wrapped}
    if "blocked" in kinds:
        path = next(p for k, p in reads if k == "blocked")
        return {**result, "decision": "deny", "target": path, "reason":
                f"tmux-pi blocks reading production/staging env file {path}. Ask the user if a value is needed."}
    if "store" in kinds:
        path = next(p for k, p in reads if k == "store")
        reason = f"This command reads credential store {path}. Allow only if its contents are needed."
        if holds:
            return {**holds, "decision": "ask", "reason": reason + " " + holds["reason"]}
        return {**result, "decision": "ask", "target": path, "reason": reason}
    if "secret" in kinds and (network or transform):
        path = next(p for k, p in reads if k == "secret")
        how = "sends it over the network" if network else "encodes it so redaction cannot recognize the values"
        reason = f"This command reads {path} and {how}. Allow only if that is intended."
        if holds:
            return {**holds, "decision": "ask", "reason": reason + " " + holds["reason"]}
        return {**result, "decision": "ask", "target": path, "reason": reason}
    if holds:
        return holds
    secret_ref = any(is_secret_name(n) for n in ENV_REF.findall(command))
    if not wrapped and not params.get("run_in_background") and (kinds or secret_ref):
        target = reads[0][1] if reads else "a secret environment variable"
        return {**result, "decision": "ask", "target": target, "reason":
                f"This command reads {target}, and its form (heredoc, exit or trailing comment) means output from a "
                "failure cannot be redacted. Allow only if needed."}
    return result


def bash_source(command, cwd):
    """The single file a simple reader command prints, so config-file rules apply to its output."""
    files = set()
    for word, args, inputs in simple_commands(command):
        if word in LISTERS:
            continue
        for token in list(inputs) + [a for a in args if not a.startswith("-")]:
            resolved = resolve(token, cwd)
            if resolved and resolved.is_file():
                files.add(str(resolved))
    return files.pop() if len(files) == 1 else None


PREFIX = re.compile(r"^([^\s:][^:\n]{0,300}?)[:-](?:(\d+)[:-])?")


class PostRedactor:
    """Redacts every string of one tool result with a single candidate review.

    grep-style `path:line:` lines use that path as their source, so config-file rules apply.
    Pass 1 plans all pieces, the classifier reviews every candidate at once (outside the
    store lock), pass 2 writes placeholders under one lock.
    """

    def __init__(self, redactor, cwd):
        self.redactor, self.cwd, self.files, self.entries, self.findings = redactor, cwd, {}, [], []

    def is_file(self, value):
        if value not in self.files:
            resolved = resolve(value, self.cwd)
            self.files[value] = bool(resolved and resolved.is_file())
        return self.files[value]

    def collect(self, text, source=None):
        """Split text into [prefix, body, source, first line, spans] pieces and plan each."""
        pieces, block, start = [], [], 1
        for number, line in enumerate(text.split("\n"), 1):
            match = PREFIX.match(line)
            if match and self.is_file(match.group(1)):
                if block:
                    pieces.append(["", "\n".join(block), source, start])
                    block = []
                pieces.append([line[:match.end()], line[match.end():], match.group(1),
                               int(match.group(2)) if match.group(2) else None])
                start = number + 1
            else:
                block.append(line)
        if block or not pieces:
            pieces.append(["", "\n".join(block), source, start])
        for piece in pieces:
            piece.append(self.redactor.plan(piece[1], piece[2]))
        self.entries.append(pieces)
        return text

    def resolve(self):
        """Drop candidates the classifier clears. Next to a private key, every candidate stays."""
        refs = [(piece, span) for pieces in self.entries for piece in pieces for span in piece[4] if not span[4]]
        if not refs:
            return
        if any(span[3] == "private_key" for pieces in self.entries for piece in pieces for span in piece[4]):
            return
        keep = self.redactor.review([(piece[1], (span[0], span[1]), piece[2]) for piece, span in refs])
        for i, (piece, span) in enumerate(refs):
            if i not in keep:
                piece[4].remove(span)

    def render(self, pieces):
        lines = []
        for prefix, body, source, first, spans in pieces:
            text, found = self.redactor.apply(body, spans, source)
            lines.append(prefix + text)
            for f in found:
                line = first if prefix else (first or 1) + f["line"] - 1
                self.findings.append({**f, "source": source, "line": line})
        return "\n".join(lines)

    def walk(self, value, fn, key=None):
        if isinstance(value, dict):
            return {k: self.walk(v, fn, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.walk(v, fn, key) for v in value]
        if isinstance(value, str) and key not in KEEP_KEYS and value:
            return fn(value)
        return value


def withheld(value, reason, key=None):
    if isinstance(value, dict):
        return {k: withheld(v, reason, k) for k, v in value.items()}
    if isinstance(value, list):
        return [withheld(v, reason, key) for v in value]
    if isinstance(value, str) and key not in KEEP_KEYS and value:
        return STUB.format(reason)
    return value


def make_redactor(event, persist=True, review=True):
    """Session redactor; in "on" mode ambiguous candidates go to the local model (lib/redact_llm.py)."""
    session = store_name(event)
    classify = None
    if review:
        try:
            classify = LocalClassifier()
        except (OSError, ValueError, KeyError):
            classify = None  # no config: candidates stay redacted
    return Redactor(Store(session, persist=persist), exact_values(files=[ROOT / "claude/mcp.local.env"]), classify)


def post_result(event, redactor, current_mode):
    """Return (hook output or None, log fields) for PostToolUse."""
    tool, response, cwd = event.get("tool_name", ""), event.get("tool_response"), event.get("cwd") or os.getcwd()
    if response is None:
        return None, {}
    if isinstance(response, dict) and (response.get("isImage") or response.get("type") not in (None, "text")):
        return None, {"skipped": "binary"}  # image, PDF or notebook data; text formats don't apply
    params = event.get("tool_input") or {}
    source = None
    if tool == REVEAL_TOOL:
        return None, {"skipped": "reveal"}  # the user approved showing this value; redacting it would defeat that
    if tool in FILE_TOOLS:
        source = params.get("file_path")
    elif tool == "Read" and isinstance(response, dict):
        source = (response.get("file") or {}).get("filePath") or params.get("file_path")
    elif tool == "Bash":
        source = bash_source(str(params.get("command") or ""), cwd)
    post = PostRedactor(redactor, cwd)
    post.walk(response, lambda text: post.collect(text, source))
    persisted = response.get("persistedOutputPath") if isinstance(response, dict) else None
    raw = Path(persisted).read_text(errors="surrogateescape") if persisted and os.path.isfile(persisted) else None
    if raw is not None:
        post.collect(raw, source)
    post.resolve()
    entries = iter(post.entries)
    with redactor.store.locked():
        updated = post.walk(response, lambda text: post.render(next(entries)))
        clean = post.render(next(entries)) if raw is not None else None
    rewrite = clean if clean is not None and clean != raw else None
    fields = {"findings": [{k: f.get(k) for k in ("placeholder", "kind", "line", "source")} for f in post.findings],
              "persisted_rewrite": bool(rewrite),
              "llm": getattr(redactor.classify, "stats", None) or None}
    if current_mode != "on" or not post.findings:
        return None, fields
    if rewrite is not None:
        Path(persisted).write_text(rewrite, errors="surrogateescape")
        os.chmod(persisted, 0o600)
    unique = {}
    for f in post.findings:
        where = f"{f['source']}:{f['line']}" if f.get("source") else f"line {f['line']}"
        unique.setdefault(f["placeholder"], where)
    listed = ", ".join(f"{p} ({w})" for p, w in list(unique.items())[:10])
    note = (f"tmux-pi redacted {len(unique)} value(s). Placeholders stand for values you cannot see. Use one "
            "as-is in a Bash command (the user is asked once; the real value is substituted locally and never shown), or in "
            "Write content or an Edit new_string for the file it came from. Never put a placeholder in an Edit old_string: "
            "it is matched against the file before tmux-pi can fill it in, so it never matches; anchor on a nearby line "
            "or rewrite the file with Write. Do not guess values. Call mcp__tmux-pi-safe__reveal only if the "
            f"literal value itself is required. Redacted: {listed}.")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": updated,
                                   "additionalContext": note[:1500]}}, fields


def log(record):
    try:
        root = default_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "shadow.log"
        if path.is_file() and path.stat().st_size > LOG_LIMIT:
            path.replace(root / "shadow.log.1")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        pass


def save_request(target, reason, status, **extra):
    task = os.getenv("TP_TASK_DIR")
    if not task:
        return
    path = Path(task) / "permission-request.json"
    value = {"status": status, "target": target, "reason": reason,
             "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             **{k: v for k, v in extra.items() if v}}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(value) + "\n")
    tmp.replace(path)


def settle(event, decision):
    path = Path(os.getenv("TP_TASK_DIR", "/nonexistent")) / "permission-request.json"
    try:
        prior = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    call, denied = prior.get("call"), event.get("hook_event_name") == "PermissionDenied"
    if call:
        same = call == event.get("tool_use_id")
    else:
        same = decision["decision"] == "ask" and prior.get("target") == decision["target"]
    if prior.get("status") == "pending" and same:
        save_request(prior["target"], prior.get("reason"), "denied" if denied else "authorized",
                     **{k: prior.get(k) for k in ("placeholder", "source", "scope", "call")})


def used_values(event):
    """Values this tool call substituted for placeholders, so Post can redact them if the output echoes them."""
    call = event.get("tool_use_id")
    data = Store(store_name(event)).peek()
    names = data.get("calls", {}).get(call, []) if call else []
    return resolve_all(data, names)


class Timeout(Exception):
    pass


def on_alarm(*_):
    raise Timeout()


def handle(event):
    """Run the hook for one event; return the JSON to print, or None."""
    name = event.get("hook_event_name")
    if name == "SessionEnd":
        sweep(session=store_name(event))  # runs in every mode: a store should not outlive its session
        return None
    if name in {"Stop", "UserPromptSubmit"}:
        # Nothing awaits approval now, so a pending grant here belongs to a rejected ask; tp-secret-exec must not
        # accept it. A failure only leaves the grant until the next turn; it never blocks the turn.
        try:
            Store(store_name(event)).drop_pending()
        except Exception:
            pass
        return None
    current = mode()
    if current == "off":
        return None
    started = time.monotonic()
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "mode": current, "event": name,
              "tool": event.get("tool_name"), "agent_type": event.get("agent_type"),
              "session": str(event.get("session_id") or "")[:8]}
    output = None
    signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(BUDGET)
    try:
        if name == "PreToolUse":
            decision = pre_decision(event)
            record["pre"] = {"decision": decision["decision"], "target": decision["target"], "wrapped": bool(decision["input"])}
            if event.get("tool_name") == "Bash":
                command = str((event.get("tool_input") or {}).get("command") or "")
                record["command"] = Redactor(Store(persist=False)).redact(command[:300])[0]
            if current == "on":
                out = {"hookEventName": "PreToolUse"}
                if decision["decision"]:
                    out.update(permissionDecision=decision["decision"], permissionDecisionReason=decision["reason"])
                    if decision["decision"] == "ask":
                        save_request(decision["target"], decision["reason"], "pending",
                                     placeholder=", ".join(decision["placeholders"]), source=decision["source"],
                                     scope=decision["scope"], call=event.get("tool_use_id"))
                if decision["input"] and decision["decision"] != "deny":
                    out["updatedInput"] = decision["input"]
                    if decision["placeholders"] and event.get("tool_use_id"):
                        store = Store(store_name(event))
                        with store.locked():
                            store.record_use(event["tool_use_id"], decision["placeholders"], decision["record"],
                                             decision.get("command"))
                output = {"hookSpecificOutput": out} if len(out) > 1 else None
        elif name == "PostToolUse":
            redactor = make_redactor(event, persist=current == "on", review=current == "on")
            used = used_values(event) if current == "on" else {}
            redactor.add_exact(used.values())
            output, fields = post_result(event, redactor, current)
            record.update(fields)
            if current == "on" and event.get("tool_use_id"):
                Store(store_name(event)).settle_use(event["tool_use_id"], True)
            if current == "on":
                settle(event, pre_decision(event))
        elif name in {"PostToolUseFailure", "PermissionDenied"}:
            if name == "PostToolUseFailure":
                _, found = make_redactor(event, persist=False, review=False).redact(str(event.get("error") or ""))
                record["unredactable_failure_findings"] = len(found)
            if current == "on":
                settle(event, pre_decision(event))
                if name == "PermissionDenied" and event.get("tool_use_id"):
                    Store(store_name(event)).settle_use(event["tool_use_id"], False)
        else:
            return None
    except Exception as exc:  # fail closed: never let an error pass raw output or skip a gate
        record["error"] = type(exc).__name__
        if current == "on" and name == "PreToolUse":
            output = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "tmux-pi redaction hook failed; retry the call."}}
        elif current == "on" and name == "PostToolUse" and event.get("tool_response") is not None:
            reason = "timeout" if isinstance(exc, Timeout) else "error"
            output = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                             "updatedToolOutput": withheld(event["tool_response"], reason)}}
    finally:
        signal.alarm(0)
    record["ms"] = round((time.monotonic() - started) * 1000)
    log(record)
    return output
