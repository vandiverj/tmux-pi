"""The "# Project Tools" block of a worker's system prompt, rendered from <workdir>/.tmux-pi/agent-tools.json.

The file is the project's own list of helper scripts (the shared ones live in tools/agent/ in this repository). It is a JSON
list, or an object {"tools": [...]}, of entries

    {"name": "web-jest", "path": "$TP_ROOT/tools/agent/web-jest.sh", "roles": ["coder", "tester"],
     "use": "run web jest tests", "usage": "WORKTREE [--repeat N] TEST_PATH...", "output": "last line exit=N log=PATH"}

  name    lowercase letters, digits and dashes; unique.
  path    an absolute path, or $TP_ROOT/... (TP_ROOT is this repository's root, wherever it is checked out). It must exist
          and be executable. The file is checked for every entry whatever the worker's role is, so a broken manifest is
          refused the same way for all roles and never half-applied.
  roles   the roles that get the entry (names of agent/roles/*.md).
  use     what it is for. usage: the arguments after the path (a leading script name is dropped). output: what it prints.
          One line each, no control characters. name + use + usage + output together may not exceed ENTRY_MAX bytes
          (the target is about 120): the whole standing text of a worker has an 8,000 byte cap (lib/preamble.py).

A missing manifest adds nothing. A manifest that exists but cannot be used (unreadable, not UTF-8, not JSON, a bad or
duplicate entry, a path that is missing or not executable) is an error, never a silent skip. Like the preamble, the manifest
of the working tree wins, and `source` (a lane task's preamble_from) is read when the working tree has none.

The block ("# Project Tools") holds only the entries for the worker's role; with none it is empty. The standing rules every
worker gets, manifest or not, are the worker contract (agent/contract/worker.md, rendered by lib/preamble.py). prior_line
gives `Prior result: PATH; review report: PATH` for a task that continues an earlier one; lib/preamble.py delivers it.

    python3 agent_tools.py <workdir> [role [source]]    prints the block (nothing when there is no manifest)
"""

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELATIVE = Path(".tmux-pi") / "agent-tools.json"
MAX_MANIFEST_BYTES = 64 * 1024
ENTRY_MAX = 150
NAME = re.compile(r"[a-z][a-z0-9-]*\Z")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
FIELDS = ("name", "path", "roles", "use", "usage", "output")
MAX_ANCESTORS = 10


def _present(path):
    try:
        return path.exists() or path.is_symlink()
    except OSError as error:
        raise ValueError(f"cannot check {path}: {error.strerror or error}")


def manifest_path(workdir, source=None):
    """The manifest file to use, or None when neither the working tree nor `source` has one."""
    path = Path(workdir) / RELATIVE
    if not _present(path) and source:
        path = Path(source) / RELATIVE
    return path if _present(path) else None


def _text(value, what, where):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: {what} must be a non-empty string")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{where}: {what} must be one line without control characters")
    return value.strip()


def _resolve(raw, where):
    if raw == "$TP_ROOT" or raw.startswith("$TP_ROOT/"):
        path = ROOT / raw[len("$TP_ROOT/"):] if raw != "$TP_ROOT" else ROOT
    elif raw.startswith("/"):
        path = Path(raw)
    else:
        raise ValueError(f"{where}: path must be absolute or start with $TP_ROOT/, got {raw!r}")
    if "$" in str(path):
        raise ValueError(f"{where}: path {raw!r} has a variable other than $TP_ROOT")
    try:
        ok = path.is_file() and os.access(path, os.X_OK)
        exists = path.exists()
    except OSError as error:
        raise ValueError(f"{where}: cannot check {path}: {error.strerror or error}")
    if not exists:
        raise ValueError(f"{where}: {path} does not exist")
    if not ok:
        raise ValueError(f"{where}: {path} is not an executable file")
    return str(path)


def load_entries(path, known_roles):
    """Validate the manifest at `path` and return its entries (dicts with path resolved), in file order."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error.strerror or error}")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(f"{path} is {len(raw)} bytes; the limit is {MAX_MANIFEST_BYTES}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8")
    except ValueError as error:
        raise ValueError(f"{path} is not valid JSON: {error}")
    if isinstance(data, dict):
        if set(data) != {"tools"}:
            raise ValueError(f"{path}: an object manifest has exactly one key, 'tools'")
        data = data["tools"]
    if not isinstance(data, list):
        raise ValueError(f"{path}: the manifest must be a list of entries or an object with a 'tools' list")
    entries, seen = [], set()
    for number, item in enumerate(data, 1):
        where = f"{path} entry {number}"
        if not isinstance(item, dict):
            raise ValueError(f"{where}: must be an object")
        if set(item) != set(FIELDS):
            missing, extra = sorted(set(FIELDS) - set(item)), sorted(set(item) - set(FIELDS))
            raise ValueError(f"{where}: fields must be exactly {', '.join(FIELDS)}"
                             + (f"; missing {', '.join(missing)}" if missing else "") + (f"; unknown {', '.join(extra)}" if extra else ""))
        name = _text(item["name"], "name", where)
        where = f"{path} entry '{name}'"
        if not NAME.match(name):
            raise ValueError(f"{where}: name must be lowercase letters, digits and dashes")
        if name in seen:
            raise ValueError(f"{where}: name appears twice")
        seen.add(name)
        roles = item["roles"]
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
            raise ValueError(f"{where}: roles must be a non-empty list of role names")
        for role in roles:
            if role not in known_roles:
                raise ValueError(f"{where}: unknown role '{role}' (roles: {', '.join(sorted(known_roles))})")
        entry = {"name": name, "roles": list(dict.fromkeys(roles)),
                 "use": _text(item["use"], "use", where), "usage": _text(item["usage"], "usage", where),
                 "output": _text(item["output"], "output", where)}
        entry["path"] = _resolve(_text(item["path"], "path", where), where)
        script = Path(entry["path"]).name
        if entry["usage"].startswith(script + " "):
            entry["usage"] = entry["usage"][len(script) + 1:].lstrip()
        size = sum(len(entry[k].encode("utf-8")) for k in ("name", "use", "usage", "output"))
        if size > ENTRY_MAX:
            raise ValueError(f"{where}: name, use, usage and output are {size} bytes; the limit is {ENTRY_MAX}. Shorten them")
        entries.append(entry)
    return entries


def _task_dir(state_dir, task_id):
    return Path(state_dir) / "tasks" / task_id


def _meta(state_dir, task_id):
    try:
        data = json.loads((_task_dir(state_dir, task_id) / "meta.json").read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prior_line(state_dir, parent):
    """`Prior result: PATH; review report: PATH` for a task that continues `parent`, or "".

    The result is the parent's result.md; the report is report.md of the newest review recorded on the parent or, failing
    that, on its nearest retry ancestor (meta.review / meta.reviews). A file that does not exist is not named. Nothing
    is guessed from an unreadable record: a missing or broken record just leaves its part out.
    """
    if not state_dir or not parent or not TASK_ID.match(parent):
        return ""
    parts = []
    result = _task_dir(state_dir, parent) / "result.md"
    if result.is_file():
        parts.append(f"Prior result: {result}")
    current, seen = parent, set()
    while current and current not in seen and len(seen) < MAX_ANCESTORS and TASK_ID.match(current):
        seen.add(current)
        meta = _meta(state_dir, current)
        if meta is None:
            break
        ids = meta.get("reviews")
        if ids is None:
            ids = [meta["review"]] if meta.get("review") else []
        ids = [i for i in ids if isinstance(i, str) and TASK_ID.match(i)] if isinstance(ids, list) else []
        report = next((p for p in (_task_dir(state_dir, i) / "report.md" for i in reversed(ids)) if p.is_file()), None)
        if report:
            parts.append(f"review report: {report}")
            break
        current = meta.get("parent")
    return "; ".join(parts)


def render(workdir, role="", source=None, known_roles=()):
    """The Project Tools block for `role`: its entries, or "" when there is no manifest or no entry for the role. Every entry
    is validated whatever the role, so a broken manifest refuses for all roles."""
    path = manifest_path(workdir, source)
    if path is None:
        return ""
    entries = load_entries(path, set(known_roles))
    lines = [f"- {entry['name']}: {entry['use']}: {entry['path']} {entry['usage']} -> {entry['output']}"
             for entry in entries if role and role.lower() in entry["roles"]]
    return "\n".join(["# Project Tools", "", *lines]) if lines else ""


if __name__ == "__main__":
    known = {p.stem for p in (ROOT / "agent" / "roles").glob("*.md")}
    try:
        sys.stdout.write(render(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else None, known))
    except (ValueError, IndexError) as error:
        sys.exit(f"agent tools: {error}")
