"""A repository's standing instructions for workers: <workdir>/.tmux-pi/worker-preamble.md.

Text before the first "## Role: <name>" heading goes to every worker; a role section goes only to that role.
A missing file adds nothing. A file that exists but cannot be used (unreadable, not UTF-8, over the size cap, a
section naming a role tmux-pi does not have) is an error, never a silent skip, so a worker never runs without
instructions the repository meant it to have. The cap is checked on the whole file, so a refusal does not
depend on which role is running.

    python3 preamble.py <workdir> [role [source]] [--state-dir DIR --parent ID]
        prints the text for the worker system prompt (nothing when there is none)

A lane's worktree is a fresh checkout and .tmux-pi/ is git-ignored, so it has no preamble of its own. `source` (a lane
task's preamble_from, the repository's main working tree) is read when the working tree has no such file; a file in the
working tree wins (a repository that tracks its preamble versions it with the branch).

A repository that also has <workdir>/.tmux-pi/agent-tools.json (same fallback to `source`; see lib/agent_tools.py) gets a
"Tools" block after the text above: two standing lines, the helper scripts listed for the worker's role and, when the caller
passes --state-dir and --parent, the prior result and review report of the task being continued. The whole text, as rendered
for every role, must stay within MAX_BYTES too. With no such manifest the text is exactly what it was before the block existed.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELATIVE = Path(".tmux-pi") / "worker-preamble.md"
TOOLS_RELATIVE = Path(".tmux-pi") / "agent-tools.json"
MAX_BYTES = 8000
SECTION = re.compile(r"^##[ \t]+Role:[ \t]*(\S.*?)[ \t]*$", re.I)


def _present(path):
    try:
        return path.exists() or path.is_symlink()
    except OSError as error:  # e.g. a directory we may not search: not the same as "no file"
        raise ValueError(f"cannot check {path}: {error.strerror or error}")


def _known_roles():
    return {p.stem for p in (ROOT / "agent" / "roles").glob("*.md")}


def _preamble_text(workdir, role, source):
    path = Path(workdir) / RELATIVE
    if not _present(path) and source:
        path = Path(source) / RELATIVE
    if not _present(path):
        return ""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError(f"cannot read {path}: {error.strerror or error}")
    if len(raw) > MAX_BYTES:
        raise ValueError(f"{path} is {len(raw)} bytes; the limit is {MAX_BYTES}. Shorten it")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8")
    known = _known_roles()
    sections = {None: []}
    current = None
    for line in text.splitlines():
        match = SECTION.match(line)
        if match:
            current = match.group(1).lower()
            if current not in known:
                raise ValueError(f"{path} has a section for unknown role '{match.group(1)}' (roles: {', '.join(sorted(known))})")
            sections.setdefault(current, [])
        else:
            sections[current].append(line)
    parts = ["\n".join(sections[None]).strip()]
    if role and role.lower() in sections:
        parts.append("\n".join(sections[role.lower()]).strip())
    return "\n\n".join(p for p in parts if p)


def _agent_tools(workdir, source):
    """lib/agent_tools.py when the repository has a tools manifest, else None. The module is imported only then, so a
    manifest with no module beside this file fails loudly, and a repository without a manifest never depends on it."""
    if not any(_present(Path(base) / TOOLS_RELATIVE) for base in (workdir, source) if base):
        return None
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import agent_tools
    return agent_tools


def _join(*parts):
    return "\n\n".join(p for p in parts if p)


def load(workdir, role="", source=None, state_dir=None, parent=None):
    text = _preamble_text(workdir, role, source)
    agent_tools = _agent_tools(workdir, source)
    if agent_tools is None or agent_tools.manifest_path(workdir, source) is None:
        return text
    known = _known_roles()
    # The cap must not depend on which role runs, so every role's text is checked (without the per-task prior-result line).
    for each in sorted(known):
        size = len(_join(_preamble_text(workdir, each, source), agent_tools.render(workdir, each, source, known)).encode("utf-8"))
        if size > MAX_BYTES:
            raise ValueError(f"the worker text for role '{each}' is {size} bytes with the tools block; the limit is {MAX_BYTES}. "
                             f"Shorten {agent_tools.RELATIVE} or the preamble")
    text = _join(text, agent_tools.render(workdir, role, source, known, state_dir, parent))
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError(f"the worker text is {len(text.encode('utf-8'))} bytes with the tools block; the limit is {MAX_BYTES}")
    return text


def _args(argv):
    positional, options, i = [], {}, 0
    while i < len(argv):
        if argv[i] in ("--state-dir", "--parent"):
            if i + 1 >= len(argv):
                raise ValueError(f"{argv[i]} needs a value")
            options[argv[i][2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            positional.append(argv[i])
            i += 1
    return positional, options


if __name__ == "__main__":
    try:
        args, options = _args(sys.argv[1:])
        sys.stdout.write(load(args[0], args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else None, **options))
    except (ValueError, IndexError) as error:
        sys.exit(f"worker preamble: {error}")
