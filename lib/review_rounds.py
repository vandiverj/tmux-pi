#!/usr/bin/env python3
"""What a reviewer of a retried task needs to see: the earlier review rounds and the change since the last one.

A task's review rounds are the reviews recorded on it and on its retry ancestors (meta.parent), oldest first.
tp-review records the working tree it reviewed as reviewed-state.json in the reviewer's task directory (a git
tree object made from a throwaway index, so the repository's index and files are untouched); the diff for the
next round is the change from that tree to the tree now.

  review_rounds.py scope <workdir> [<lane> [<list-file> [<budget>]]]
      (<budget>: most characters the text may have; the inline list shrinks to fit. <lane> may be "" for a non-lane task; the full changed-file list is written to <list-file> (atomically, replacing a link there), which the assignment names, when it is longer than the assignment lists (200 paths or 16 KB); it is not attached to the packet)
      prints {"text": the changed-file list and scoped git commands for the reviewer's assignment, "withheld": [paths]};
      always exits 0 (a workdir that is not a git repository, or a list that cannot be made, is said in one line)
      For a lane task the list and the commands start from the lane's last closed step (the commit lane_steps._previous names)
      when that commit is an ancestor of HEAD, else from the lane base; the text names which ("base: last closed step S at SHA"
      or "base: lane base SHA") and says closed steps are out of scope except where the new lines change or call them.

  review_rounds.py timers <workdir> <lane> <out-file>
      writes the `Timer waits in added test lines` file: the added lines of that same diff, in files whose path matches
      test, spec, e2e or __tests__, that contain setTimeout(, sleep(, delay(, Deno.sleep( or waitForTimeout(, as path:line: text
      (a line carrying `timer-ok:` is marked `(timer-ok)`); facts only. Withheld paths are never read.

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
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_write  # noqa: E402
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


# Machine-generated files whose contents are named but left out of a review diff (drizzle-kit schema snapshots).
GENERATED = re.compile(r"(^|/)drizzle/meta/[^/]+_snapshot\.json$")


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
    safe, withheld, generated = [], [], []
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
        if check(top, path):
            withheld.append((path, record))
        elif GENERATED.search(path):
            generated.append((path, record))
        else:
            safe.append((path, record))
    body = "".join(entry_patch(top, empty, entry) for entry in safe)
    labels = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}
    notes = "".join(f"{json.dumps(p)} ({labels[r[4]]}): sensitive path, its contents were withheld from this diff\n"
                    for p, r in withheld)
    notes += "".join(f"{json.dumps(p)} ({labels[r[4]]}): generated file, its contents were left out of this diff; open it if needed\n"
                     for p, r in generated)
    gate(body, [p for p, _ in safe], [p for p, _ in withheld])
    return notes + body, [p for p, _ in withheld]


def write(path, text):
    return safe_write.write_atomic(path, text, errors="surrogateescape", root=safe_write.task_root(Path(path).parent))


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


MAX_SCOPE_PATHS = 200  # changed paths named in the assignment (safe and withheld together); the rest go to the list file
MAX_SCOPE_BYTES = 16384  # and at most this many bytes of them, so long names cannot crowd out the packet's author evidence


def scope_lines(workdir, lane=""):
    """(base, top, safe paths [(path, untracked)], withheld paths, basis) for the author's change, or None outside git; raises
    Refused. basis says which base a lane review diffs from and why ("" for a task outside a lane): the last closed step's
    commit when the lane has closed steps and that commit is an ancestor of HEAD, else the lane base."""
    top = git(workdir, "rev-parse", "--show-toplevel")
    if top.returncode:
        return None
    top = top.stdout.strip()
    basis = ""
    if git(top, "rev-parse", "--verify", "-q", "HEAD").returncode:
        base = git(top, "hash-object", "-t", "tree", "/dev/null").stdout.strip()  # no commit yet: everything is new
    elif lane:
        try:
            import lanes
            import lane_steps
            record = lanes.read_lane(lanes.git_common_dir(top), lane)
            base = record["base"]["commit"]
            closed = record["closed"][-1]["step"] if record["closed"] else None
            last = lane_steps._previous(record) if closed else None
        except Exception as exc:
            raise Refused(f"cannot read the base of lane {lane}: {exc}")
        merged = git(top, "merge-base", base, "HEAD")
        base = merged.stdout.strip() if merged.returncode == 0 and merged.stdout.strip() else base
        basis = f"base: lane base {base[:12]}"
        if closed and git(top, "merge-base", "--is-ancestor", last, "HEAD").returncode == 0:
            base, basis = last, f"base: last closed step {closed} at {last[:12]}"
        elif closed:
            basis += f" (closed step {closed} at {last[:12]} is not an ancestor of HEAD)"
    else:
        base = "HEAD"
    # The working tree and the index each against the base: a staged change that the working tree undoes (MM) is in the
    # second only, and any path where the two differ from each other is in one of them.
    working = git(top, "diff", "--name-only", "-z", "--no-renames", base, "--")
    staged = git(top, "diff", "--cached", "--name-only", "-z", "--no-renames", base, "--")
    untracked = git(top, "ls-files", "--others", "--exclude-standard", "-z")
    if working.returncode or staged.returncode or untracked.returncode:
        raise Refused(f"git could not list the changed files: {(working.stderr or staged.stderr or untracked.stderr).strip()}")
    new = {p for p in untracked.stdout.split("\0") if p}
    paths = sorted({p for run in (working, staged) for p in run.stdout.split("\0") if p} | new)
    check = sensitive_checker()
    flagged = {path for path in paths if check(top, path)}
    # git matches even a literal pathspec as a directory prefix, so a path above a withheld one (a file that became a
    # directory, or the reverse) would select it: such a path is withheld too.
    withheld = [path for path in paths if path in flagged or any(f.startswith(path + "/") for f in flagged)]
    held = set(withheld)
    safe = [(path, path in new) for path in paths if path not in held]
    return base, top, safe, withheld, basis


def literal(text):
    """One shell word for text, on one line: shlex.quote when every character prints, else bash $'...' with escapes."""
    if text and all(c.isprintable() for c in text):
        return shlex.quote(text)
    out = []
    for c in text:
        if c in "\\'":
            out.append("\\" + c)
        elif c.isprintable():
            out.append(c)
        else:
            out.append("".join(f"\\x{b:02x}" for b in c.encode("utf-8", "surrogateescape")))
    return "$'" + "".join(out) + "'"


def write_list_file(path, text):
    """Write text to path atomically without following a link there: a temp file in the same directory replaces whatever
    is at path (a symlink is replaced, never written through). Raises OSError when it cannot."""
    safe_write.write_atomic(path, text, mode=0o644, root=safe_write.task_root(Path(path).parent))


def scope_text(workdir, lane="", list_file=None, budget=None):
    """(assignment text, withheld paths): the changed files and the scoped git commands a reviewer may use. Never raises:
    when the list cannot be made it says so in one line and the review goes on as it did before. At most MAX_SCOPE_PATHS paths
    and MAX_SCOPE_BYTES bytes of path lines (safe and withheld together) are named in the assignment; when more changed and
    list_file is given, the whole list (one quoted path per line, withheld ones marked) is written there. When budget (characters)
    is given the text never exceeds it: the inline list shrinks to fit, down to the pointer line alone, and when even that does not
    fit (or the list file cannot be written) the one-line unavailable text is returned."""
    try:
        found = scope_lines(workdir, lane)
    except Refused as exc:
        return f"Diff scope: the changed-file list could not be computed ({' '.join(str(exc).split())[:200]}); inspect the files you need directly.", []
    if found is None:
        return "Diff scope: the working directory is not a git repository, so there is no changed-file list.", []
    limit = MAX_SCOPE_BYTES if budget is None else max(0, min(MAX_SCOPE_BYTES, budget))
    while True:
        text, used, usable = render_scope(found, lane, list_file, limit)
        if not usable:
            return text, []
        excess = 0 if budget is None else len(text) - budget
        if excess <= 0 or not used and limit == 0:
            break
        limit = max(0, used - excess - 1) if used else 0
    if budget is not None and len(text) > budget:
        text = "Diff scope: the changed-file list could not be computed (no room left in the review packet); inspect the files you need directly."
        return text, []
    return text, found[3]


def render_scope(found, lane, list_file, max_bytes):
    """(text, bytes of path lines used, False when the text is the unavailable line) for found = scope_lines(...) with at most max_bytes of path lines inline."""
    base, top, safe, withheld, basis = found
    since = "since its last closed step" if basis.startswith("base: last closed step") else "since its base"
    kinds = f"committed on the lane {since}, staged, unstaged and untracked" if lane else "staged, unstaged and untracked"
    out = [f"Diff scope: {len(safe)} file(s) changed relative to {base[:12] if lane else 'HEAD'} ({kinds}) in the repository at {literal(top)}:"]
    if basis:  # a lane task: which base the list and the commands below use, and what that leaves out of scope
        closed = " Closed steps are out of scope except where the new lines change or call them." if "closed step" in basis else ""
        out.insert(0, f"{basis}.{closed}")
    count, size = 0, 0

    def room(line):
        nonlocal count, size
        cost = len(line.encode("utf-8", "surrogateescape")) + 1
        if count >= MAX_SCOPE_PATHS or size + cost > max_bytes:
            return False
        count, size = count + 1, size + cost
        return True
    listed = 0
    for path, is_new in safe:
        line = f"- {literal(path)}{' (untracked: read the file)' if is_new else ''}"
        if not room(line):
            break
        out.append(line)
        listed += 1
    if not safe:
        out.append("- (none)")
    shown_withheld = []
    for path in withheld:
        if not room(literal(path) + ","):
            break
        shown_withheld.append(path)
    more_safe, more_withheld = len(safe) - listed, len(withheld) - len(shown_withheld)
    if more_safe or more_withheld:
        what = " and ".join(part for part in (f"{more_safe} more changed file(s)" if more_safe else "",
                                              f"{more_withheld} more withheld path(s)" if more_withheld else "") if part)
        try:
            if not list_file:
                raise OSError("no list file was given")
            write_list_file(list_file, "".join(f"{literal(p)}{' (untracked)' if n else ''}\n" for p, n in safe)
                            + "".join(f"withheld: {literal(p)}\n" for p in withheld))
        except OSError as exc:  # a partial list would leave changed files unnamed: say the list is unavailable instead
            reason = " ".join(f"the complete list of {len(safe) + len(withheld)} paths could not be written: {exc}".split())[:200]
            return f"Diff scope: the changed-file list could not be computed ({reason}); inspect the files you need directly.", 0, False
        out.append(f"- and {what}: the complete list is in {literal(str(Path(list_file).resolve()))} (one quoted path per line; withheld paths marked); read it with your file-read tool, not a git command")
    if shown_withheld or more_withheld:
        out.append(f"Withheld (sensitive paths; never diff, show or read them): {', '.join(literal(p) for p in shown_withheld) or '(see the list file)'}"
                   + (f" and {more_withheld} more" if shown_withheld and more_withheld else ""))
    git = f"GIT_LITERAL_PATHSPECS=1 git -C {literal(top)}"
    out.append(f"Scoped commands (keep the GIT_LITERAL_PATHSPECS=1 prefix: it makes each path a literal file name, never a glob or pathspec magic): "
               f"`{git} diff {base} -- <path> [<path> ...]` (working tree), `{git} diff --cached {base} -- <path> [<path> ...]` (staged), "
               f"`{git} diff --stat {base} -- <path> ...`. Run both the working-tree and the staged command for a path: a staged change "
               "the working tree undoes shows only in the staged one. "
               "Every `git diff`, `git show` and `git log -p` you run must keep that prefix and must name explicit file paths from the list above; never run them "
               "unscoped, with a bare directory, or on a path that is withheld. Write each path literally in the command; never put paths in a "
               "shell variable or array (`$p`, `\"${p[@]}\"`): the sensitive-read guard cannot see them and stops for permission. Split a long list "
               "over several commands instead. Untracked files do not appear in `git diff`: read them directly.")
    return "\n".join(out), size, True


TIMER_CALLS = ("setTimeout(", "sleep(", "delay(", "Deno.sleep(", "waitForTimeout(")  # a plain substring match; the reviewer judges
TEST_PATH = re.compile(r"test|spec|e2e|__tests__", re.I)
TIMER_TITLE = "Timer waits in added test lines"
MAX_TIMER_LINES = 300  # listed lines; the rest are counted, so a huge list cannot crowd out the packet's author evidence
MAX_TIMER_TEXT = 400


def added_lines(top, base, path, untracked):
    """[(line number, text)]: the lines of `path` that are added relative to `base` (the working tree and the index each; a
    path that is untracked is all added). The path is given to git as a literal name."""
    if untracked:
        target = Path(top) / path
        if target.is_symlink() or not target.is_file():
            return []
        data = target.read_bytes()
        return [] if b"\0" in data else list(enumerate(data.decode("utf-8", "replace").splitlines(), 1))
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    found = []
    for flags in ((), ("--cached",)):
        run = subprocess.run(["git", "-C", top, "-c", "core.quotepath=false", "diff", *flags, "--unified=0", "--no-color", "--no-ext-diff",
                              "--no-textconv", "--no-renames", base, "--", path], capture_output=True, env=env)
        if run.returncode:
            raise Refused(f"cannot read the added lines of {shown(path)}: {run.stderr.decode('utf-8', 'replace').strip()}")
        number = None
        for line in run.stdout.decode("utf-8", "replace").split("\n"):
            hunk = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
            if hunk:
                number = int(hunk.group(1))
            elif number is not None and line.startswith("+"):
                found.append((number, line[1:]))
                number += 1
    return found


def timer_waits(workdir, lane=""):
    """The text of the packet's `Timer waits in added test lines` file: every added line of the review diff, in a file whose
    path matches a test pattern, that contains one of TIMER_CALLS, as `path:line: text`, a line carrying `timer-ok:` marked
    `(timer-ok)`; one line saying none were found when there are none. Facts only. Withheld (sensitive) paths are never read, a
    listed line goes through the redaction pass, and the text is never an error: what cannot be computed is said in one line."""
    head = f"# {TIMER_TITLE}\n\n"
    try:
        found = scope_lines(workdir, lane)
        if found is None:
            return head + "Not computed: the working directory is not a git repository.\n"
        base, top, safe, _, _ = found
        rows, seen = [], set()
        for path, untracked in safe:
            if not TEST_PATH.search(path):
                continue
            for number, text in added_lines(top, base, path, untracked):
                if any(call in text for call in TIMER_CALLS) and (path, number, text) not in seen:
                    seen.add((path, number, text))
                    clipped = text.strip()[:MAX_TIMER_TEXT] + ("…" if len(text.strip()) > MAX_TIMER_TEXT else "")
                    rows.append((path, number, f"{shown(path)}:{number}: {clipped}{' (timer-ok)' if 'timer-ok:' in text else ''}"))
        rows.sort(key=lambda row: row[:2])
        if not rows:
            return head + "No added test line contains setTimeout(, sleep(, delay(, Deno.sleep( or waitForTimeout(; none were found.\n"
        body = "\n".join(row[2] for row in rows[:MAX_TIMER_LINES])
        body, _ = redact_text(redactor(), body, "timer waits in added test lines")
        more = len(rows) - MAX_TIMER_LINES
        return (head + "Added lines in files whose path contains test, spec, e2e or __tests__ that contain setTimeout(, sleep(, delay(, "
                "Deno.sleep( or waitForTimeout( (a plain match; the reviewer judges them; `(timer-ok)` marks a line carrying `timer-ok:`):\n\n"
                + body + "\n" + (f"\nand {more} more such line(s) not listed here.\n" if more > 0 else ""))
    except (Refused, OSError) as exc:
        return head + f"Not computed: {' '.join(str(exc).split())[:200]}.\n"


def main(argv):
    if len(argv) == 5 and argv[1] == "timers":  # timers <workdir> <lane> <out-file>
        try:
            write_list_file(argv[4], timer_waits(argv[2], argv[3]))
        except OSError as exc:
            print(f"review_rounds: cannot write {argv[4]}: {exc}", file=sys.stderr)
            sys.exit(1)
        return
    if len(argv) in (3, 4, 5, 6) and argv[1] == "scope":
        text, withheld = scope_text(*argv[2:5], budget=int(argv[5]) if len(argv) == 6 else None)
        print(json.dumps({"text": text, "withheld": withheld}))
        return
    if len(argv) != 6 or argv[1] != "prepare":
        sys.exit(__doc__)
    try:
        print(json.dumps(prepare(*argv[2:])))
    except Refused as exc:
        print(f"review_rounds: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
