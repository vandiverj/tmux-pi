#!/usr/bin/env python3
"""What a reviewer of a retried task needs to see: the earlier review rounds and the change since the last one.

A task's review rounds are the reviews recorded on it and on its retry ancestors (meta.parent), oldest first.
tp-review records the working tree it reviewed as reviewed-state.json in the reviewer's task directory (a git
tree object made from a throwaway index, so the repository's index and files are untouched); the diff for the
next round is the change from that tree to the tree now.

  review_rounds.py prepare <state-dir> <task-id> <workdir> <out-dir>
      writes the earlier findings files and the diff into out-dir and prints
      {"contexts": [paths, in order], "rounds": N, "snapshot": {...} | null}
      exits 1 and says why when recorded state exists but cannot be used (nothing is guessed)

Nothing written to out-dir carries a credential: a changed path the sensitive-read rule flags (lib/sensitive_read.py,
the rule bin/tp-sensitive-read applies) is named in the diff with its contents withheld, whether it was added, modified,
deleted or renamed; and the diff and the earlier findings then go through the redaction pass (lib/redact.py) as context
files for tp-spawn do not. A path that cannot be checked, or a rule or redactor that cannot be loaded, refuses.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
MAX_ANCESTORS = 200  # a safety limit on the walk to the root; reaching it refuses the review


class Refused(Exception):
    pass


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def task_path(state_dir, ident):
    if not isinstance(ident, str) or not IDENT.fullmatch(ident):
        raise Refused(f"invalid task id in the retry lineage: {ident!r}")
    return Path(state_dir) / "tasks" / ident


def review_ids(meta):
    ids = meta.get("reviews")
    if ids is None:
        ids = [meta["review"]] if meta.get("review") else []
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise Refused(f"a task record lists reviews that are not task ids: {ids!r}")
    return ids


def earlier_reviews(state_dir, task_id):
    """Review task ids on the task and its retry ancestors, oldest first (ancestors before the task itself)."""
    chain, seen, current = [], set(), task_id
    while True:
        if current in seen:
            raise Refused(f"the retry lineage of '{task_id}' loops back to '{current}'")
        if len(seen) >= MAX_ANCESTORS:
            raise Refused(f"the retry lineage of '{task_id}' is longer than {MAX_ANCESTORS} tasks; "
                          "refusing rather than leaving earlier review rounds out")
        seen.add(current)
        meta = read_json(task_path(state_dir, current) / "meta.json")
        if meta is None:
            if current == task_id:
                raise Refused(f"cannot read the record of '{task_id}'")
            raise Refused(f"cannot read the record of retry ancestor '{current}'")
        chain.append((current, review_ids(meta)))
        parent = meta.get("parent")
        if not parent:
            break
        current = parent
    return [(author, review) for author, reviews in reversed(chain) for review in reviews]


def git(workdir, *args, env=None):
    run = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True, env=env)
    return run


def snapshot(workdir):
    """{"workdir", "tree"} for the working tree as it is now, or None when workdir is not in a git repository."""
    top = git(workdir, "rev-parse", "--show-toplevel")
    if top.returncode:
        return None
    top = top.stdout.strip()
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if git(top, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
            read = git(top, "read-tree", "HEAD", env=env)
            if read.returncode:
                raise Refused(f"git read-tree failed: {read.stderr.strip()}")
        add = git(top, "add", "-A", env=env)
        if add.returncode:
            raise Refused(f"git add failed while recording the reviewed state: {add.stderr.strip()}")
        tree = git(top, "write-tree", env=env)
        if tree.returncode:
            raise Refused(f"git write-tree failed: {tree.stderr.strip()}")
    return {"workdir": str(Path(workdir).resolve()), "top": top, "tree": tree.stdout.strip()}


def sensitive_checker():
    """A function path -> bool from the sensitive-read rule; raises Refused when the rule cannot be loaded or answers oddly."""
    try:
        from sensitive_read import classify
    except Exception as exc:  # any failure to load the rule
        raise Refused(f"cannot load the sensitive-read rule, so the diff cannot be checked: {exc}")

    def check(top, path):
        absolute = str(Path(top) / path)
        try:
            verdict = classify("Read", {"file_path": absolute}, top)
        except Exception as exc:
            raise Refused(f"the sensitive-read rule failed on {path!r}: {exc}")
        flag = verdict.get("sensitive") if isinstance(verdict, dict) else None
        if not isinstance(flag, bool):
            raise Refused(f"the sensitive-read rule gave no answer for {path!r}")
        return flag
    return check


def redactor():
    try:
        from redact import Redactor, exact_values
        from redaction_store import Store
        return Redactor(Store(persist=False), exact_values(files=[ROOT / "claude/mcp.local.env"]))
    except Exception as exc:
        raise Refused(f"cannot load the redaction pass, so the review text cannot be checked: {exc}")


def redact_text(red, text, source):
    try:
        out, findings = red.redact(text, source)
    except Exception as exc:
        raise Refused(f"the redaction pass failed on {source}: {exc}")
    return out, len(findings)


def redact_diff(red, diff):
    """(text, count): the redaction pass over a unified diff. The +/-/space column is taken off before the pass, since
    a line such as `+API_TOKEN=...` is not recognised with it, and put back on every output line afterwards."""
    out, count, run, mark, source, in_hunk = [], 0, [], None, None, False

    def flush():
        nonlocal run, count
        if run:
            text, found = redact_text(red, "".join(run), source)
            count += found
            out.extend(mark + line for line in text.splitlines(keepends=True))
            if text and not text.endswith("\n"):
                out[-1] += "\n"
        run = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            in_hunk, source = False, None
        elif not in_hunk and line[:4] in ("--- ", "+++ "):
            name = line[4:].rstrip("\n").split("\t")[0]
            if name != "/dev/null" and name[:2] in ("a/", "b/"):
                source = name[2:]
        elif line.startswith("@@"):
            flush()
            in_hunk = True
            out.append(line)
            continue
        if in_hunk and line[:1] in (" ", "+", "-"):
            if line[0] != mark:
                flush()
                mark = line[0]
            run.append(line[1:] if line.endswith("\n") else line[1:] + "\n")
            continue
        flush()
        out.append(line)
    flush()
    return "".join(out), count


def shown(path):
    """A path as it appears in a diff header: one line, injective (backslash, quotes and control characters escaped)."""
    return "".join(c if c.isprintable() and c not in '\\"' else json.dumps(c)[1:-1] for c in path)


def blob_patch(top, oldid, newid):
    """The hunks between two blob ids (no path is given to git, so nothing can be selected beyond those two blobs).

    A text diff comes back as its lines from the first hunk on; a binary pair as None."""
    run = subprocess.run(["git", "-C", top, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                          oldid, newid], capture_output=True)
    if run.returncode:
        raise Refused(f"cannot diff blobs {oldid}..{newid}: {run.stderr.decode('utf-8', 'replace').strip()}")
    text = run.stdout.decode("utf-8", "surrogateescape")
    at = text.find("\n@@")
    if text.startswith("@@"):
        at = -1
    if at < 0 and not text.startswith("@@"):
        return None if text.strip() else ""
    return text[at + 1:]


def entry_patch(top, empty, entry):
    """The diff text of one checked entry, with a header this code writes from the entry's own path and modes."""
    path, (oldmode, newmode, oldid, newid, status) = entry
    label = shown(path)
    out = [f"diff --git a/{label} b/{label}\n"]
    if status == "A":
        out.append(f"new file mode {newmode}\n")
    elif status == "D":
        out.append(f"deleted file mode {oldmode}\n")
    elif oldmode != newmode:
        out.append(f"old mode {oldmode}\nnew mode {newmode}\n")
    if "160000" in (oldmode, newmode):  # a submodule commit is not a blob in this repository
        out.append(f"Submodule entry ({oldid[:12]}..{newid[:12]}); no content shown.\n")
        return "".join(out)
    hunks = blob_patch(top, empty if status == "A" else oldid, empty if status == "D" else newid)
    if hunks is None:
        out.append(f"Binary files a/{label} and b/{label} differ\n")
    elif hunks:
        out.append(f"--- {'/dev/null' if status == 'A' else 'a/' + label}\n+++ {'/dev/null' if status == 'D' else 'b/' + label}\n")
        out.append(hunks if hunks.endswith("\n") else hunks + "\n")
    return "".join(out)


def gate(text, safe_paths, withheld_paths):
    """Last check before attaching: every entry header in text names a checked, unflagged path, once. Anything else refuses.

    Hunk lines always begin with a space, +, - or backslash, so a line that begins `diff --git ` is a header."""
    allowed = {f"diff --git a/{shown(p)} b/{shown(p)}": p for p in safe_paths}
    flagged = {shown(p) for p in withheld_paths}
    seen = []
    for line in text.split("\n"):
        if line.startswith("diff --git "):
            if line not in allowed:
                raise Refused(f"the diff names a path that was not checked as safe: {line[11:]!r}")
            seen.append(line)
    if len(seen) != len(set(seen)) or set(seen) != set(allowed):
        raise Refused("the diff's entries are not the checked safe entries")
    for line in text.split("\n"):
        if line[:4] in ("--- ", "+++ ") and line[4:6] in ("a/", "b/") and line[6:] in flagged \
                and f"diff --git a/{line[6:]} b/{line[6:]}" not in allowed:
            raise Refused(f"the diff has a section for a flagged path: {line[4:]!r}")


def safe_diff(top, old, new):
    """(text, withheld paths): git diff old..new without the contents of any path the sensitive-read rule flags.

    Renames are split into a deletion and an addition (--no-renames) so each side is judged by its own path.
    The listing is of leaf entries (blobs, links, submodules) with their blob ids; each safe entry's patch is made from
    its own two blob ids, never from a path given to git, so a path that turned into a directory (or the reverse) cannot
    pull in entries that were not checked. gate() then verifies the finished text.
    """
    listing = subprocess.run(["git", "-C", top, "diff", "--raw", "-z", "--no-renames", "--no-abbrev", old, new],
                             capture_output=True)
    if listing.returncode:
        raise Refused(f"cannot list the changes since tree {old}: {listing.stderr.decode('utf-8', 'replace').strip()}")
    fields = listing.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 2:
        raise Refused("cannot parse the list of changed paths")
    empty = subprocess.run(["git", "-C", top, "hash-object", "-w", "-t", "blob", "--stdin"], input=b"", capture_output=True)
    if empty.returncode or not re.fullmatch(r"[0-9a-f]{40,64}", empty.stdout.decode().strip()):
        raise Refused("cannot work out the empty blob id")
    empty = empty.stdout.decode().strip()
    check = sensitive_checker()
    safe, withheld = [], []
    for info, name in zip(fields[::2], fields[1::2]):
        try:
            path, meta = name.decode("utf-8"), info.decode("ascii")
        except UnicodeDecodeError:
            raise Refused(f"a changed path cannot be decoded, so it cannot be checked: {name!r}")
        match = re.fullmatch(r":(\d{6}) (\d{6}) ([0-9a-f]{40,64}) ([0-9a-f]{40,64}) ([ADMT])", meta)
        if not match:
            raise Refused(f"cannot parse a changed-path record for {path!r}")
        if not path or path.startswith("/") or "\0" in path:
            raise Refused(f"a changed path is not a repository-relative path: {path!r}")
        record = match.groups()
        (withheld if check(top, path) else safe).append((path, record))
    body = "".join(entry_patch(top, empty, entry) for entry in safe)
    labels = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}
    notes = "".join(f"{json.dumps(p)} ({labels[r[4]]}): sensitive path, its contents were withheld from this diff\n"
                    for p, r in withheld)
    gate(body, [p for p, _ in safe], [p for p, _ in withheld])
    return notes + body, [p for p, _ in withheld]


def write(path, text):
    Path(path).write_text(text, encoding="utf-8", errors="surrogateescape")
    return str(path)


def prepare(state_dir, task_id, workdir, out_dir):
    out = Path(out_dir)
    earlier = earlier_reviews(state_dir, task_id)
    now = snapshot(workdir)
    contexts, skipped, last_state, last_round = [], [], None, 0
    red, redactions, withheld = redactor(), 0, []
    # a review that never produced a reply has no findings to show; a reply that is not a regular file is refused
    reviews = []
    for author, review in earlier:
        result = task_path(state_dir, review) / "result.md"
        if result.is_symlink() or (result.exists() and not result.is_file()):
            raise Refused(f"review '{review}' of '{author}' has an unreadable result.md")
        (reviews if result.exists() else skipped).append((author, review))
    earlier = reviews
    for number, (author, review) in enumerate(earlier, 1):
        directory = task_path(state_dir, review)
        text = (directory / "result.md").read_text(encoding="utf-8", errors="surrogateescape")
        text, count = redact_text(red, text, f"earlier findings of {review}")
        redactions += count
        head = (f"[Earlier review, round {number} of {len(earlier)} before this one; review task {review}, "
                f"of author task {author}. Oldest first. Each finding is cited by round and number, e.g. round {number} F1.]\n\n")
        contexts.append(write(out / f"earlier-findings-{number:02d}-{review}.md", head + text))
        state = directory / "reviewed-state.json"
        if state.exists() or state.is_symlink():
            value = read_json(state)
            if value is None or not isinstance(value.get("tree"), str) or not isinstance(value.get("workdir"), str):
                raise Refused(f"round {number}: reviewed-state.json of '{review}' is unreadable")
            last_state, last_round = value, number
        else:
            last_state, last_round = None, number
    if earlier:
        label = f"diff-since-round-{last_round}.md"
        if last_state is None:
            body = (f"No diff: review round {last_round} recorded no reviewed state (it ran before states were recorded, "
                    "or its working directory was not a git repository). Inspect the working tree directly.\n")
        elif now is None:
            raise Refused(f"round {last_round} recorded a reviewed state but {workdir} is not in a git repository now")
        elif last_state["workdir"] != now["workdir"]:
            body = (f"No diff: round {last_round} reviewed {last_state['workdir']} and this review is of {now['workdir']}. "
                    "Inspect the working tree directly.\n")
        else:
            diff, withheld = safe_diff(now["top"], last_state["tree"], now["tree"])
            diff, count = redact_diff(red, diff)
            redactions += count
            body = diff if diff.strip() else "(empty: the working tree is identical to the one round " \
                f"{last_round} reviewed)\n"
        head = f"[Change in the working tree since the state review round {last_round} reviewed (git diff, round {last_round} tree to now).]\n\n"
        contexts.append(write(out / label, head + body))
    return {"contexts": contexts, "rounds": len(earlier), "snapshot": now, "skipped": [review for _, review in skipped],
            "withheld": withheld, "redactions": redactions}


def main(argv):
    if len(argv) != 6 or argv[1] != "prepare":
        sys.exit(__doc__)
    try:
        print(json.dumps(prepare(*argv[2:])))
    except Refused as exc:
        print(f"review_rounds: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
