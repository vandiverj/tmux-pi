"""Resolve `<REDACTED:...>` placeholders back to values, and substitute them into text or shell commands.

Only local code on the user's machine calls this (tp-secret-exec, the Edit/Write rehydration in the
PreToolUse hook, and the reveal tool). A value is recovered from the store when it was kept there, or by
re-reading its source file and checking the hash, so a file that changed since it was read yields None.
"""
import hashlib
from pathlib import Path
import re

from redact import Redactor, exact_values
from redact_rules import PLACEHOLDER
from redaction_store import Store

ROOT = Path(__file__).resolve().parent.parent
SAFE = re.compile(r"^[A-Za-z0-9_@%+=:,./~-]*$")


def find(text):
    """Distinct placeholders in text, in order of first use."""
    return list(dict.fromkeys(PLACEHOLDER.findall(text)))


def resolve_value(entry):
    """The value an entry stands for, or None when it cannot be recovered and verified."""
    if not entry:
        return None
    if "value" in entry:
        return entry["value"]
    redactor = Redactor(Store(persist=False), exact_values(files=[ROOT / "claude/mcp.local.env"]))
    for pointer in entry.get("sources", []):
        try:
            text = Path(pointer["path"]).read_text(errors="surrogateescape")
        except OSError:
            continue
        for start, end, *_ in redactor.plan(text, pointer["path"]):
            value = text[start:end]
            if hashlib.sha256(value.encode()).hexdigest() == entry.get("sha256"):
                return value
    return None


def resolve_all(data, names):
    """{placeholder: value} for names in a store's data dict; a name that cannot be resolved is left out."""
    values = {}
    for name in names:
        value = resolve_value(data.get("entries", {}).get(name))
        if value is not None:
            values[name] = value
    return values


def rehydrate(text, values):
    """Plain substitution, for file contents."""
    return PLACEHOLDER.sub(lambda m: values.get(m.group(0), m.group(0)), text)


def shell_quote_for(value, state):
    """Quote value for the shell context it lands in: None (bare), `'` or `"`."""
    if state == "'":
        return value.replace("'", "'\\''")
    if state == '"':
        return re.sub(r'([\\"$`])', r"\\\1", value)
    if SAFE.match(value) and value:
        return value
    return "'" + value.replace("'", "'\\''") + "'" if value else "''"


def substitute_command(command, values):
    """Replace placeholders in a shell command, quoting each value for its quote context.

    Returns None when the command is too complex to do that safely: a value with shell
    metacharacters inside command substitution or backticks, or a heredoc.
    """
    if "<<" in command:
        return None
    nested = "$(" in command or "`" in command
    out, state, i = [], None, 0
    while i < len(command):
        match = PLACEHOLDER.match(command, i)
        if match:
            value = values.get(match.group(0))
            if value is None:
                return None
            if nested and not SAFE.match(value):
                return None
            out.append(shell_quote_for(value, state))
            i = match.end()
            continue
        c = command[i]
        if c == "\\" and state != "'" and i + 1 < len(command):
            out.append(command[i:i + 2])
            i += 2
            continue
        if state is None and c in "'\"":
            state = c
        elif state == c:
            state = None
        out.append(c)
        i += 1
    return "".join(out)
