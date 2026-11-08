r"""T12: a failing test a worker says it does not own, and the investigation that follows.

A worker's report (report.md, else its final reply result.md) may carry a block:

    FAILING_TESTS:
    - test: tests.test_x.TestY.test_z
      command: python3 -m unittest tests.test_x
      unmodified_base: yes, failed on a clean checkout at abc1234 (output below)   |   not checked
      note: anything else worth knowing (optional)

Reading is lenient and never raises: an entry missing a field is kept as text with the problems named, so a report is never
failed or hidden for how it wrote the block. Nothing here blocks a commit. A block with no entry is no block (parse returns
None), whatever words it uses: no word list. Each line after the header is classified into one LINE KIND (`_classify`) and
handled by the transition table in `parse`'s docstring; nothing else decides what a line does. An entry counts only if its
final id matches TEST_ID. The header-line text is always a note. Notes stay in `text`: collect shows them, and they never
trigger anything.

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
LABELLED = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):(\s|$)")  # a bare `label:` (BULLET_TEST/BULLET_LABEL, FIELD_TEST/FIELD_OTHER)
# A test identifier, defined once: dotted unittest ids, test_x / x_test / TestX names, paths and pytest node ids.
TEST_ID = re.compile(
    r"\btests?\.\w+|\b\w*test_\w+|\b\w+_test\b|\bTest[A-Z]\w*"
    r"|\btests?/\S+|[\w/.-]+\.py\b|[\w/.-]+\.(?:test|spec)\.[jt]sx?\b|[\w/.-]+::\w+")
FIELDS = ("test", "command", "unmodified_base")


def _plain(text):
    """`text` without `*`/backtick markers and without `_` emphasis (an underscore at a word edge, not inside a name)."""
    return re.sub(r"[*`]|(?<![A-Za-z0-9])_+|_+(?![A-Za-z0-9])", "", text)


def _names_test(value):
    return bool(TEST_ID.search(_plain(value)))


# Line kinds (`_classify`). Each line is matched after stripping `*`, `_` emphasis and backticks. A "label" is a bare
# ^([A-Za-z][A-Za-z0-9_]*):(\s|$), no dots, slashes or ::. So `- test_case: tests.x` and `- test_foo: Error` are
# BULLET_LABEL notes: a bare identifier and a colon is ambiguous, and ambiguous text never triggers an investigation.
BLANK = "BLANK"                    # an empty line
END = "END"                        # a heading (`#`) or an unindented `NAME:` status line
BULLET_TEST = "BULLET_TEST"        # `- test: v` (or `* `)
BULLET_LABEL = "BULLET_LABEL"      # `- other: v`
BULLET_TEXT_ID = "BULLET_TEXT_ID"  # BULLET_TEXT whose first token (trailing `:,.`, wrapping `()` removed) matches TEST_ID
BULLET_TEXT = "BULLET_TEXT"        # any other bullet
FIELD_TEST = "FIELD_TEST"          # unbulleted `test: v`
FIELD_OTHER = "FIELD_OTHER"        # unbulleted `label: v`
CONTINUATION = "CONTINUATION"      # indented, and neither a bullet nor `label:`
OTHER = "OTHER"                    # anything else
START, HEADER_TEXT = "START", "HEADER_TEXT"  # the last line kind before the first line: a header without / with text
ANY = "any"
# (entry, last line kind, line kind, action), read top to bottom: the first row that matches decides. `entry` is none,
# provisional (opened by BULLET_TEXT_ID) or explicit (its id came from a `test:` line). parse's docstring is this table.
TABLE = (
    (ANY, START, BLANK, "skip"),
    (ANY, ANY, BLANK, "end"),
    (ANY, ANY, END, "end"),
    (ANY, ANY, BULLET_TEST, "open"),
    (ANY, ANY, BULLET_TEXT_ID, "open_provisional"),
    (ANY, ANY, BULLET_LABEL, "note"),
    (ANY, ANY, BULLET_TEXT, "note"),
    ("none", ANY, FIELD_TEST, "note"),
    ("provisional", ANY, FIELD_TEST, "set_id"),
    ("explicit", ANY, FIELD_TEST, "double"),
    ("none", ANY, FIELD_OTHER, "note"),
    (ANY, ANY, FIELD_OTHER, "add_field"),
    ("none", ANY, CONTINUATION, "note"),
    (ANY, FIELD_OTHER, CONTINUATION, "extend"),
    (ANY, CONTINUATION, CONTINUATION, "extend"),
    (ANY, ANY, CONTINUATION, "note"),
    (ANY, ANY, OTHER, "note"),
)


def _classify(line):
    """(kind, label, value) of one line after the header; label and value are None where the kind has none."""
    if not line.strip():
        return BLANK, None, None
    if line.lstrip().startswith("#") or OTHER_FIELD.match(line):
        return END, None, None
    bullet = ENTRY.match(line)
    plain = _plain(bullet.group(1) if bullet else line).strip()
    label = LABELLED.match(plain)
    if label:
        name, value = label.group(1).lower(), plain[label.end():].strip()
        if bullet:
            return (BULLET_TEST if name == "test" else BULLET_LABEL), name, value
        return (FIELD_TEST if name == "test" else FIELD_OTHER), name, value
    if bullet:
        token = plain.split(None, 1)[0].strip(":,.()") if plain else ""
        if token and _names_test(token):
            return BULLET_TEXT_ID, None, bullet.group(1).strip()
        return BULLET_TEXT, None, None
    if line.startswith((" ", "\t")):
        return CONTINUATION, None, line.strip()
    return OTHER, None, None


def _action(entry_state, last_kind, kind):
    for row_entry, row_last, row_kind, action in TABLE:
        if row_kind == kind and row_entry in (ANY, entry_state) and row_last in (ANY, last_kind):
            return action
    raise AssertionError(f"no TABLE row for {(entry_state, last_kind, kind)}")  # every kind ends in an (any, any) row


def parse(text):
    """The block in `text` as {"entries": [...], "text": raw block, "malformed": [reasons]}, or None when there is none.

    State: (entry, last line kind, last field label), updated on every line read, notes included. A note closes the entry
    and stays in `text`. Each line's kind is `_classify`'s. Rows are read top to bottom; the first match decides.

    | entry       | last line kind | line kind      | action                                             | entry after | last field |
    |-------------|----------------|----------------|----------------------------------------------------|-------------|------------|
    | any         | START          | BLANK          | skip: not in `text`, state unchanged               | none        | none       |
    | any         | any            | BLANK          | end of block                                       | -           | -          |
    | any         | any            | END            | end of block                                       | -           | -          |
    | any         | any            | BULLET_TEST    | open a new entry, id = the value                   | explicit    | none       |
    | any         | any            | BULLET_TEXT_ID | open a new entry, id = the bullet's text           | provisional | none       |
    | any         | any            | BULLET_LABEL   | note                                               | none        | none       |
    | any         | any            | BULLET_TEXT    | note                                               | none        | none       |
    | none        | any            | FIELD_TEST     | note                                               | none        | none       |
    | provisional | any            | FIELD_TEST     | id = the value                                     | explicit    | none       |
    | explicit    | any            | FIELD_TEST     | mark "more than one test: field", id unchanged     | explicit    | none       |
    | none        | any            | FIELD_OTHER    | note                                               | none        | none       |
    | any         | any            | FIELD_OTHER    | field label = value (after "; " when already set)  | unchanged   | label      |
    | none        | any            | CONTINUATION   | note                                               | none        | none       |
    | any         | FIELD_OTHER    | CONTINUATION   | append the line to the last field                  | unchanged   | unchanged  |
    | any         | CONTINUATION   | CONTINUATION   | append the line to the last field                  | unchanged   | unchanged  |
    | any         | any            | CONTINUATION   | note                                               | none        | none       |
    | any         | any            | OTHER          | note                                               | none        | none       |
    """
    lines = text.splitlines()
    for start, line in enumerate(lines):
        match = HEADER.match(line)
        if match:
            break
    else:
        return None
    inline = match.group(1)
    entries, doubled, read = [], [], []
    entry, entry_state, last_kind, last_field = None, "none", (HEADER_TEXT if inline else START), None
    for line in lines[start + 1:]:
        kind, label, value = _classify(line)
        action = _action(entry_state, last_kind, kind)
        if action == "end":
            break
        if action == "skip":
            continue
        read.append(line)
        if action in ("open", "open_provisional"):
            entry = {"test": value}
            entries.append(entry)
            entry_state = "explicit" if action == "open" else "provisional"
        elif action == "set_id":
            entry["test"], entry_state = value, "explicit"
        elif action == "double":
            doubled.append(entry)
        elif action == "add_field":
            entry[label] = (entry[label] + "; " + value).strip("; ") if label in entry else value
        elif action == "extend":
            entry[last_field] = (entry[last_field] + " " + value).strip()
        elif action == "note":
            entry, entry_state = None, "none"
        last_field = label if action == "add_field" else last_field if action == "extend" else None
        last_kind = kind
    entries = [entry for entry in entries if _names_test(entry["test"])]  # an entry counts only if its final id names a test
    if not entries:
        return None
    malformed = []
    for number, entry in enumerate(entries, 1):
        missing = [name for name in FIELDS if not entry.get(name)]
        problems = ([f"no {', '.join(missing)}"] if missing else []) + (["more than one test: field"] if any(entry is d for d in doubled) else [])
        if problems:
            malformed.append(f"entry {number} has " + " and ".join(problems))
    return {"entries": entries, "text": "\n".join(([inline] if inline else []) + read), "malformed": malformed}


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
