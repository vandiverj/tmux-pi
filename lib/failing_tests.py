"""T12: a failing test a worker says it does not own, and the investigation that follows.

A worker's report (report.md, else its final reply result.md) may carry a block:

    FAILING_TESTS:
    - test: tests.test_x.TestY.test_z
      command: python3 -m unittest tests.test_x
      unmodified_base: yes, failed on a clean checkout at abc1234 (output below)   |   not checked
      note: anything else worth knowing (optional)

The block runs to the first blank line, heading, or other `NAME:` line. `FAILING_TESTS: none` is no block. Reading is
lenient and never raises: an entry missing a field, or a block that is not a list of entries at all, is kept as text with
the problems named, so a report is never failed or hidden for how it wrote the block. Nothing here blocks a commit.

`investigate_plan` prepares the investigation (`tp-agent dispatch --investigate-failure`): the task text for a tester, the
diff of the reporting task, and a clean worktree at the commit the task started from.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

HEADER = re.compile(r"^[ \t>*_`#-]*FAILING_TESTS[ \t]*:[ \t*_`]*(.*?)\s*$")
OTHER_FIELD = re.compile(r"^[A-Z][A-Z0-9_]{2,}:(\s|$)")
ENTRY = re.compile(r"^\s*[-*]\s+(.*)$")
FIELD = re.compile(r"^\s*(?:[-*]\s+)?(test|command|unmodified_base|note)\s*:\s*(.*)$", re.I)
NONE = {"none", "n/a", "no", "nothing", "-", "[]"}
FIELDS = ("test", "command", "unmodified_base")


def parse(text):
    """The block in `text` as {"entries": [...], "text": raw block, "malformed": [reasons]}, or None when there is none."""
    lines = text.splitlines()
    for start, line in enumerate(lines):
        match = HEADER.match(line)
        if match:
            break
    else:
        return None
    inline = match.group(1)
    if inline.lower() in NONE:
        return None
    body = [inline] if inline else []
    for line in lines[start + 1:]:
        if not line.strip():
            if body:
                break
            continue
        if line.lstrip().startswith("#") or OTHER_FIELD.match(line):
            break
        body.append(line)
    if not body:
        return None
    entries, current = [], None
    for line in body:
        field = FIELD.match(line)
        bullet = ENTRY.match(line)
        if field:
            name = field.group(1).lower()
            if current is None or bullet or (name in current and name != "note"):
                current = {}
                entries.append(current)
            current[name] = field.group(2).strip()
        elif current is not None and current and line.startswith((" ", "\t")) and not bullet:
            last = list(current)[-1]  # a wrapped value belongs to the field before it
            current[last] = (current[last] + " " + line.strip()).strip()
        else:  # free text: kept as the test's name, and reported as incomplete
            current = {"test": (bullet.group(1) if bullet else line).strip()}
            entries.append(current)
    malformed = []
    for number, entry in enumerate(entries, 1):
        missing = [name for name in FIELDS if not entry.get(name)]
        if missing:
            malformed.append(f"entry {number} has no {', '.join(missing)}")
    if not entries:
        malformed.append("the block holds no '- test: ...' entries")
    return {"entries": entries, "text": "\n".join(body), "malformed": malformed}


def read(task_path):
    """The block a task reported, from report.md else result.md (lossy decode: display text), with `source`; or None."""
    for name in ("report.md", "result.md"):
        try:
            text = (Path(task_path) / name).read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        found = parse(text)
        if found:
            return dict(found, source=str(Path(task_path) / name))
    return None


def notice(task_id, found):
    """The one line collect says, and the notification's wording."""
    count = len(found["entries"]) or 1
    return (f"{task_id} reports failing tests it does not own ({count}): a worker's word that they predate its change is not "
            f"evidence. Start the investigation with: tp-agent dispatch --investigate-failure {task_id}")


def _git(workdir, *args):
    run = subprocess.run(["git", "-C", str(workdir), *args], text=True, capture_output=True)
    if run.returncode:
        raise ValueError(f"git {' '.join(args)} failed in {workdir}: {run.stderr.strip() or run.stdout.strip()}")
    return run.stdout


def head_commit(workdir):
    """The commit a task starts from (recorded in dispatch.json as base_commit), or None outside a git repository."""
    run = subprocess.run(["git", "-C", str(workdir), "rev-parse", "--verify", "HEAD^{commit}"], text=True, capture_output=True)
    return run.stdout.strip() if run.returncode == 0 else None


def repo_top(workdir):
    return _git(workdir, "rev-parse", "--show-toplevel").strip()


def _git_env(workdir, index, *args):
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    run = subprocess.run(["git", "-C", str(workdir), *args], text=True, capture_output=True, env=env)
    if run.returncode:
        raise ValueError(f"git {' '.join(args)} failed in {workdir}: {run.stderr.strip() or run.stdout.strip()}")
    return run.stdout


def diff_of(workdir, base_commit):
    """The workdir's tree now against `base_commit` as one patch `git apply` takes, new (untracked, unignored) files included.
    It is staged in a throwaway index, so the workdir's own index and files are not touched."""
    with tempfile.TemporaryDirectory() as folder:
        index = Path(folder) / "index"
        _git_env(workdir, index, "add", "-A")
        return _git_env(workdir, index, "diff", "--cached", "--binary", base_commit)


def moved_commits(workdir, base_commit):
    """One line per commit the workdir's HEAD has gained since `base_commit` (empty when it has not moved)."""
    return [line for line in _git(workdir, "log", "--oneline", f"{base_commit}..HEAD").splitlines() if line]


def clean_worktree(workdir, base_commit, target):
    """A detached worktree of the task's repository at `base_commit`, at `target` (which must not exist)."""
    _git(workdir, "cat-file", "-e", f"{base_commit}^{{commit}}")
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    _git(workdir, "worktree", "add", "--detach", str(target), base_commit)
    return str(target)


def remove_worktree(workdir, target):
    subprocess.run(["git", "-C", str(workdir), "worktree", "remove", "--force", str(target)], text=True, capture_output=True)


PATCH_NOTE = ("The patch is the state of {workdir} when this investigation was started, compared with the commit the task started "
              "from. In a folder others share it can include later or concurrent changes that are not the task's.")


def task_text(task_id, block_text, base_commit, workdir, tree, patch_path, patch_lines, moved=()):
    caveat = PATCH_NOTE.format(workdir=workdir)
    if moved:
        caveat += (f" HEAD of {workdir} has moved since the task started; these commits came after it and are in the patch:\n"
                   + "\n".join(f"  {line}" for line in moved))
    return (
        f"Investigate failing tests that task {task_id} reported as not its own. A worker's belief that a failure predates "
        f"its change is a claim to examine: it may have caused the failure through something its change affected, or the "
        f"test may be outdated.\n\n"
        f"Reported (verbatim):\n{block_text}\n\n"
        f"Your working directory is a clean worktree at {base_commit}, the commit {task_id} started from. {task_id}'s "
        f"own tree, with its change, is {workdir}; its diff is {patch_path} ({patch_lines} lines; apply it here with "
        f"`git apply` to see the change). {caveat}\n\n"
        f"Do not edit the test to make it pass and do not skip it until you have answered, "
        f"with evidence (command and output):\n"
        f"1. Does the test fail on the unmodified base (this worktree, before applying the diff)?\n"
        f"2. Does it fail only with the change, directly or through something the change affected?\n"
        f"3. Is it order- or environment-dependent (run it alone and with its module; say what the environment needs)?\n"
        f"4. Is the test outdated: what it asserts no longer matches the intended behaviour, and who decided that behaviour?\n"
        f"Then fix the cause, or fix or retire the test under the repository's test policy, leaving the fix uncommitted in "
        f"this worktree and saying so with `git diff` as the patch, or report why the owner must decide. "
        f"Your reply states which answer holds for each question.")
