"""A repository's standing instructions for workers: <workdir>/.tmux-pi/worker-preamble.md.

Text before the first "## Role: <name>" heading goes to every worker; a role section goes only to that role.
A missing file adds nothing. A file that exists but cannot be used (unreadable, not UTF-8, over the size cap, a
section naming a role tmux-pi does not have) is an error, never a silent skip, so a worker never runs without
instructions the repository meant it to have. The cap is checked on the whole file, so a refusal does not
depend on which role is running.

    python3 preamble.py <workdir> [role [source]]    prints the text for the worker system prompt (nothing when there is none)

A lane's worktree is a fresh checkout and .tmux-pi/ is git-ignored, so it has no preamble of its own. `source` (a lane
task's preamble_from, the repository's main working tree) is read when the working tree has no such file; a file in the
working tree wins (a repository that tracks its preamble versions it with the branch).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELATIVE = Path(".tmux-pi") / "worker-preamble.md"
MAX_BYTES = 8000
SECTION = re.compile(r"^##[ \t]+Role:[ \t]*(\S.*?)[ \t]*$", re.I)


def _present(path):
    try:
        return path.exists() or path.is_symlink()
    except OSError as error:  # e.g. a directory we may not search: not the same as "no file"
        raise ValueError(f"cannot check {path}: {error.strerror or error}")


def load(workdir, role="", source=None):
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
    known = {p.stem for p in (ROOT / "agent" / "roles").glob("*.md")}
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


if __name__ == "__main__":
    try:
        sys.stdout.write(load(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else None))
    except (ValueError, IndexError) as error:
        sys.exit(f"worker preamble: {error}")
