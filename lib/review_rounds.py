#!/usr/bin/env python3
"""What a reviewer of a retried task needs to see: the earlier review rounds and the change since the last one.

A task's review rounds are the reviews recorded on it and on its retry ancestors (meta.parent), oldest first.
tp-review records the working tree it reviewed as reviewed-state.json in the reviewer's task directory (a git
tree object made from a throwaway index, so the repository's index and files are untouched); the diff for the
next round is the change from that tree to the tree now.

  review_rounds.py scope <workdir> [<lane> [<list-file> [<budget>]]] [--input <scope-input.json>]
      (--input: the file prepare wrote beside the diffs. The listed paths are exactly the paths of the attached diffs (the main diff
      and every held section), never recomputed against HEAD or anything else; without it the round-1 computation is made. When
      HEAD does not resolve (no commit yet) every printed command names the empty tree instead.)
      (<budget>: most characters the text may have; the inline list shrinks to fit. <lane> may be "" for a non-lane task; the full changed-file list is written to <list-file> (atomically, replacing a link there), which the assignment names, when it is longer than the assignment lists (200 paths or 16 KB); it is not attached to the packet)
      prints {"text": the changed-file list and scoped git commands for the reviewer's assignment, "withheld": [paths]};
      always exits 0 (a workdir that is not a git repository, or a list that cannot be made, is said in one line)
      For a lane task the list and the commands start from the lane's last closed step (the commit lane_steps._previous names)
      when that commit is an ancestor of HEAD, else from the lane base; the text names which ("base: last closed step S at SHA"
      or "base: lane base SHA") and says closed steps are out of scope except where the new lines change or call them.

  review_rounds.py timers <workdir> <lane> <out-file> [--input <scope-input.json>]
      (--input: the file prepare wrote; its paths are used only when it is the round-1 plan, the one timers makes without it.)
      writes the `Timer waits in added test lines` file: the added lines of that same diff, in files whose path matches
      test, spec, e2e or __tests__, that contain setTimeout(, sleep(, delay(, Deno.sleep( or waitForTimeout(, as path:line: text
      (a line carrying `timer-ok:` is marked `(timer-ok)`); facts only. Withheld paths are never read. Every match is kept: the
      first line gives the title with the total and how many carry no text (`# Timer waits in added test lines: 301 matching added
      line(s); 1 listed as path:line without their text`, or `...: none found`); the first 300 carry text (cut at 400 characters with a mark), the rest
      are path:line only.

  review_rounds.py prepare <state-dir> <task-id> <workdir> <out-dir> [<lane>]
      writes the diff and the earlier findings files into out-dir and prints
      {"contexts": [paths, in the order a reviewer reads them: the diff first, then the earlier findings oldest first], "rounds": N, "families": [the Family: names of each attached round, [] for none], "snapshot": {...} | null}
      exits 1 and says why when recorded state exists but cannot be used (nothing is guessed)
      A retry made with --route rule-first or --route design (meta.json `gate` of the new task) starts a fresh findings window: only
      the reviews from the one that triggered the newest such retry on are attached in full; the older ones are one line each in
      earlier-summary.md (round, review id, verdict, families, finding headings) and listed in "summarised" with their result.md paths.
      The diff is always of the tree now: from the last reviewed state when a round recorded one, else (round 1, or a round with
      no usable state) against the review base (diff_now: a lane's last closed step, else the lane base, else HEAD for a task
      outside a lane); it gets the same sensitive-path and redaction checks either way. <lane> is the author's lane ("" for none).

A modified text file whose hunks are longer than the file shown whole (a rewrite) is attached as the file itself, every line
marked +, after a one-line note with both sizes and the scoped git command that prints the real diff (entry_patch); a tie keeps the hunks.

An added or modified file that a frozen-copies.json beside or above it declares a byte-identical copy of `commit:source` is named in a
note and left out of the diff (frozen_copies, in safe_diff, so round 1, later rounds and held sections all get it) only when the
manifest lists it, the commit is an ancestor of the old side (of the HEAD recorded with the last reviewed state when the old side is a
tree, else of the review base), the blob and the regular-file mode equal the source's, and neither path is sensitive. Any failed check
or unusable manifest keeps the full diff and adds a one-line reason; gate() refuses a body with a section for a left-out path.

Nothing written to out-dir carries a credential: a changed path the sensitive-read rule flags (lib/sensitive_read.py,
the rule bin/tp-sensitive-read applies) is named in the diff with its contents withheld, whether it was added, modified,
deleted or renamed; and the diff and the earlier findings then go through the redaction pass (lib/redact.py) as context
files for tp-spawn do not. A path that cannot be checked, or a rule or redactor that cannot be loaded, refuses.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_verdict  # noqa: E402
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


GATES = ("rule-first", "design")  # the routes of a gated retry (tp-agent retry --route); bin/tp-agent writes it as meta.json `gate`


def lineage(state_dir, task_id):
    """[(author, [review ids])] for the task and its retry ancestors, oldest first; an author with no review has an empty list."""
    return [(author, review_ids(meta)) for author, meta in lineage_meta(state_dir, task_id)]


def lineage_meta(state_dir, task_id):
    """[(author, meta)] for the task and its retry ancestors, oldest first (the walk lineage and gated_author share)."""
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
        review_ids(meta)  # a record that lists reviews that are not task ids refuses here, as before
        chain.append((current, meta))
        parent = meta.get("parent")
        if not parent:
            break
        current = parent
    return list(reversed(chain))


def dispatched_parent_sweep(meta, parent):
    """True when the task's own dispatch.json `context` lists the sweep.md of `parent` (…/tasks/<parent>/sweep.md): what tp-agent
    retry attached to a gated retry before it recorded `gate` in meta.json. Reads nothing of the parent's directory."""
    return any(isinstance(spec, str) and Path(spec).name == "sweep.md" and Path(spec).parent.name == parent
               and Path(spec).parent.parent.name == "tasks" for spec in (meta or {}).get("context") or [])


def legacy_trigger(state_dir, parent, parent_meta, created):
    """The parent's newest review with a result.md whose meta.json `created` is earlier than `created` (both are %Y-%m-%dT%H:%M:%SZ
    UTC strings, compared as strings), else None: the review a gated retry made before `gate_review` existed was made after."""
    found = None
    for review in review_ids(parent_meta):
        made = (read_json(task_path(state_dir, review) / "meta.json") or {}).get("created")
        if isinstance(made, str) and isinstance(created, str) and made < created and (task_path(state_dir, review) / "result.md").exists():
            if found is None or made >= found[0]:
                found = (made, review)
    return found[1] if found else None


def gated_author(state_dir, task_id):
    """(author, route, trigger review id or None) for the newest author in the chain of `task_id` that a gated retry started, else
    None. Gated: its meta.json `gate` is rule-first or design (`gate_review` names the parent's review that triggered it), or, with
    no `gate` key at all (a task made before retry wrote it), its own dispatch.json lists the sweep.md of the author it replaced; the
    trigger is then that parent's newest review made before the task was. The parent's sweep.md on disk is never read: a later
    sibling retry or a failed gated dispatch writes one there too. A task without either is not gated."""
    found, before = None, None
    for author, meta in lineage_meta(state_dir, task_id):
        if meta.get("gate") in GATES:
            found = (author, meta["gate"], meta.get("gate_review") or None)
        elif "gate" not in meta and before is not None:
            dispatch = read_json(task_path(state_dir, author) / "dispatch.json")
            if dispatched_parent_sweep(dispatch, before[0]):
                found = (author, "sweep.md", legacy_trigger(state_dir, before[0], before[1], meta.get("created")) or "")
        before = (author, meta)
    return found


def earlier_reviews(state_dir, task_id):
    """Review task ids on the task and its retry ancestors, oldest first (ancestors before the task itself)."""
    return [(author, review) for author, reviews in lineage(state_dir, task_id) for review in reviews]


def chain_labels(state_dir, task_id):
    """[(author, review, verdict, families, round)] for the chain of `task_id`, oldest first. THE source of review round numbers:
    `round` counts the reviews of the chain that have a result.md, from 1 (the number tp-review gives the round and the number the
    missing-family flag uses); None for a review with no result.md. `verdict` is the stated verdict word of the review's result.md,
    None when it cannot be read or is not a verdict; `families` are its Family: names (review_verdict.families), [] then. An author
    in the chain with no review at all gets one row (author, None, None, [], None): a retry nobody reviewed, which breaks a run of
    REQUEST_CHANGES. The one reader of these names for the retry gate, the round text and the flag. Raises Refused as lineage does."""
    rows, number = [], 0
    for author, reviews in lineage(state_dir, task_id):
        if not reviews:
            rows.append((author, None, None, [], None))
        for review in reviews:
            path = task_path(state_dir, review) / "result.md"
            if not path.exists():
                rows.append((author, review, None, [], None))
                continue
            number += 1
            try:
                text = path.read_text(encoding="utf-8")
                families = review_verdict.families(text)
                try:
                    verdict = review_verdict.parse(text)[0]
                except ValueError:
                    verdict = None
            except (OSError, ValueError):
                verdict, families = None, []
            rows.append((author, review, verdict, families, number))
    return rows


def git(workdir, *args, env=None):
    run = subprocess.run(["git", "-C", workdir, *args], capture_output=True, text=True, errors="surrogateescape", env=env)
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
    head = git(top, "rev-parse", "--verify", "-q", "HEAD")  # the commit HEAD was at: what "committed since" is measured from in a later round
    return {"workdir": str(Path(workdir).resolve()), "top": top, "tree": tree.stdout.strip(), "head": head.stdout.strip() if head.returncode == 0 else ""}


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
    return hunks_of(run.stdout.decode("utf-8", "surrogateescape"))


def hunks_of(text):
    """blob_patch's answer for the diff text of one pair: its lines from the first hunk on, None for a binary pair, "" for none."""
    at = text.find("\n@@")
    if text.startswith("@@"):
        at = -1
    if at < 0 and not text.startswith("@@"):
        return None if text.strip() else ""
    return text[at + 1:]


BATCH_MIN = 5  # pairs: below this the batch (two check-attr, one mktree, one diff) starts no fewer processes than one diff per pair


def attributes(top, names, index=True):
    """{name: the attributes git gives it, as a sorted tuple of (attribute, value)} for names at the repository root, or None when
    git cannot say. `index=False` reads them with an empty index (only the work tree's, $GIT_DIR/info/attributes and the global
    and system files): `git diff` of two blobs does not read the index for attributes, `git check-attr` does, so blob_patches asks
    both ways."""
    with tempfile.TemporaryDirectory() as tmp:
        env = None if index else {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "no-index")}
        run = subprocess.run(["git", "-C", top, "check-attr", "-z", "--stdin", "--all"], capture_output=True, env=env,
                             input="".join(f"{name}\0" for name in names).encode())
    fields = run.stdout.split(b"\0")
    if run.returncode or fields.pop() != b"" or len(fields) % 3:
        return None
    found = {name: [] for name in names}
    for name, attribute, value in zip(fields[::3], fields[1::3], fields[2::3]):
        if name.decode() not in found:
            return None
        found[name.decode()].append((attribute, value))
    return {name: tuple(sorted(values)) for name, values in found.items()}


def blob_patches(top, pairs):
    """[blob_patch(top, old, new) for old, new in pairs], the same text, from one `git diff` of two trees for most pairs.

    No path is given to git here either. Pair k becomes the entry named k (its index, a name this code chooses) of two trees made
    by one `git mktree --batch` from the pairs' blob ids alone: the old blob in the first, the new in the second, both mode 100644.
    Git's attributes are matched against a name (blob_patch's diff matches each side's against its blob id: `* -diff`, a funcname
    driver or `diff=x` with `diff.x.binary` change the output), so a pair goes into the trees only when its old id, its new id
    and k have the same attributes, read both with and without the index (attributes); any other pair, every pair when the
    attributes cannot be read, and every pair of a list shorter than BATCH_MIN, is diffed alone by blob_patch.
    The output is split back by structure only: a `diff --git a/k b/k` line counts only outside a hunk body, whose length its
    `@@` header gives (as outside_hunks reads it), and only for a name not yet seen; a file line inside a hunk is never read
    as a boundary. Anything else raises Refused, as does a git call that fails: nothing is guessed or attached in part."""
    if len(pairs) < BATCH_MIN:
        return [blob_patch(top, old, new) for old, new in pairs]
    names = [str(k) for k in range(len(pairs))]
    asked = sorted({blob for pair in pairs for blob in pair} | set(names))
    views = [attributes(top, asked, index) for index in (True, False)]
    batch = [] if None in views else [k for k, (old, new) in enumerate(pairs)
                                      if all(view[old] == view[new] == view[names[k]] for view in views)]
    found = {k: blob_patch(top, *pairs[k]) for k in range(len(pairs)) if k not in set(batch)}
    if batch:
        listing = "".join(f"100644 blob {pairs[k][0]}\t{names[k]}\n" for k in batch) + "\n" + \
                  "".join(f"100644 blob {pairs[k][1]}\t{names[k]}\n" for k in batch)
        made = subprocess.run(["git", "-C", top, "mktree", "--batch"], input=listing.encode(), capture_output=True)
        trees = made.stdout.decode("ascii", "replace").split()
        if made.returncode or len(trees) != 2:
            raise Refused(f"cannot diff blobs {pairs[batch[0]][0]}..{pairs[batch[0]][1]} and {len(batch) - 1} more pairs: "
                          f"the trees holding them cannot be made ({made.stderr.decode('utf-8', 'replace').strip()})")
        run = subprocess.run(["git", "-C", top, "diff", "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames",
                              "--src-prefix=a/", "--dst-prefix=b/", *trees], capture_output=True)
        if run.returncode:
            raise Refused(f"cannot diff blobs {pairs[batch[0]][0]}..{pairs[batch[0]][1]} and {len(batch) - 1} more pairs "
                          f"(trees {trees[0]}..{trees[1]}): {run.stderr.decode('utf-8', 'replace').strip()}")
        sections = split_sections(run.stdout.decode("utf-8", "surrogateescape"), [names[k] for k in batch])
        for k in batch:
            if names[k] not in sections and pairs[k][0] != pairs[k][1]:
                raise Refused(f"the diff of blobs {pairs[k][0]}..{pairs[k][1]} is missing from the batched diff")
            found[k] = hunks_of(sections.get(names[k], ""))
    return [found[k] for k in range(len(pairs))]


def split_sections(text, names):
    """{name: its section of text, from its `diff --git a/name b/name` line to the next}: the diff of two trees whose entries are
    `names`. A header counts only outside a hunk body and only once per name; the body of a hunk is as long as its header says (a
    space line uses one old and one new line, `-` one old, `+` one new, a backslash line neither). Raises Refused for anything else:
    a line before the first header, an `@@` line that is not a hunk header, a body cut short or too long, an unknown or repeated
    header, a text that does not end at a line end."""
    headers = {f"diff --git a/{name} b/{name}": name for name in names}
    lines = text.split("\n")
    if lines.pop() != "":
        raise Refused("the batched diff does not end with a line end")
    sections, current, old, new = {}, None, 0, 0
    for at, line in enumerate(lines):
        if old or new:
            kind = line[:1]
            if kind == " ":
                old, new = old - 1, new - 1
            elif kind == "-":
                old -= 1
            elif kind == "+":
                new -= 1
            elif kind != "\\":
                raise Refused(f"the batched diff has a hunk cut short at line {at + 1}")
            if old < 0 or new < 0:
                raise Refused(f"the batched diff has a hunk longer than its header says at line {at + 1}")
        elif line.startswith("diff --git "):
            current = headers.get(line)
            if current is None or current in sections:
                raise Refused(f"the batched diff has a header this code did not write at line {at + 1}: {line[:60]!r}")
            sections[current] = []
        elif line.startswith("@@"):
            head = HUNK_HEAD.match(line)
            if not head:
                raise Refused(f"the batched diff has an @@ line that is not a hunk header at line {at + 1}")
            old, new = int(head.group(1) or 1), int(head.group(2) or 1)
        if current is None:
            raise Refused("the batched diff does not start with a header")
        sections[current].append(line)
    if old or new:
        raise Refused("the batched diff ends inside a hunk")
    return {name: "\n".join(lines) + "\n" for name, lines in sections.items()}


def blob_text(top, blobid):
    """The text of a blob, decoded the way blob_patch decodes a diff."""
    run = subprocess.run(["git", "-C", top, "cat-file", "blob", blobid], capture_output=True)
    if run.returncode:
        raise Refused(f"cannot read blob {blobid}: {run.stderr.decode('utf-8', 'replace').strip()}")
    return run.stdout.decode("utf-8", "surrogateescape")


def blob_texts(top, blobids):
    """{blob id: blob_text(top, blob id)} for every id, from one `git cat-file --batch`; split by the size git gives for each
    object, never by its content. Raises Refused as blob_text does when git fails or an id is not a blob there."""
    blobids = list(dict.fromkeys(blobids))
    if not blobids:
        return {}
    run = subprocess.run(["git", "-C", top, "cat-file", "--batch"], input="".join(f"{b}\n" for b in blobids).encode(),
                         capture_output=True)
    if run.returncode:
        raise Refused(f"cannot read blob {blobids[0]} and {len(blobids) - 1} more: {run.stderr.decode('utf-8', 'replace').strip()}")
    out, at, found = run.stdout, 0, {}
    for blobid in blobids:
        end = out.find(b"\n", at)
        head = out[at:end].decode("ascii", "replace").split() if end >= 0 else []
        if head[:1] != [blobid] or len(head) != 3 or head[1] != "blob" or not head[2].isdigit():
            raise Refused(f"cannot read blob {blobid}: git says {' '.join(head[1:]) or 'nothing'}")
        size = int(head[2])
        if out[end + 1 + size:end + 2 + size] != b"\n":
            raise Refused(f"cannot read blob {blobid}: git gave fewer bytes than its size")
        found[blobid] = out[end + 1:end + 1 + size].decode("utf-8", "surrogateescape")
        at = end + 2 + size
    return found


def whole_file_section(top, path, label, old, new, hunks, text):
    """The section lines after the `diff --git` header for a modified file shown whole: a one-line note (the sizes of its hunks and of
    the file, and the scoped command that prints the real diff between the trees or commits `old` and `new`), then the file as it is
    now with every line prefixed `+`, the way an added file is shown. A line of the file that begins `diff --git ` is therefore
    `+diff --git ...` and cannot pass for a header (gate), and redact_diff sees the same line shapes as in a diff (the `@@` line
    opens its hunk)."""
    command = f"GIT_LITERAL_PATHSPECS=1 git -C {literal(top)} diff {old} {new} -- {literal(path)}"
    lines = text.split("\n")
    missing_newline = bool(lines[-1])
    if not missing_newline:
        lines.pop()
    out = [f"[Whole file shown instead of the hunks: its hunks are {len(hunks)} chars and the file as it is now is {len(text)} chars. "
           f"Every line below is the file as it is now, marked +; the lines it replaced are not shown. The real diff: `{command}`]\n",
           f"--- a/{label}\n+++ b/{label}\n@@ whole file, {len(lines)} line(s) @@\n"]
    out.extend(f"+{line}\n" for line in lines)
    if missing_newline:
        out.append("\\ No newline at end of file\n")
    return "".join(out)


READ = object()  # entry_patch: the hunks are read here


def entry_pair(empty, entry):
    """The two blob ids an entry's hunks are made from (the empty blob for the missing side of an added or deleted file), or None for
    a submodule, which has no blob in this repository."""
    _, (oldmode, newmode, oldid, newid, status) = entry
    if "160000" in (oldmode, newmode):
        return None
    return empty if status == "A" else oldid, empty if status == "D" else newid


def may_go_whole(entry, hunks):
    """True when entry_patch weighs the entry's hunks against the file shown whole, and so needs the new blob's text."""
    _, (oldmode, newmode, _, _, status) = entry
    return bool(hunks) and status == "M" and oldmode == newmode and "160000" not in (oldmode, newmode)


def entry_patches(top, empty, entries, old, new):
    """[entry_patch(top, empty, entry, old, new) for entry in entries], the same text, with the git work done once for all of them:
    every entry's hunks from blob_patches, then the text of each new blob a whole-file section may need from one blob_texts."""
    pairs = [entry_pair(empty, entry) for entry in entries]
    made = iter(blob_patches(top, [pair for pair in pairs if pair]))
    hunks = [next(made) if pair else None for pair in pairs]
    texts = blob_texts(top, [entry[1][3] for entry, found in zip(entries, hunks) if may_go_whole(entry, found)])
    return [entry_patch(top, empty, entry, old, new, found, texts.__getitem__) for entry, found in zip(entries, hunks)]


def entry_patch(top, empty, entry, old, new, hunks=READ, text_of=None):
    """The diff text of one checked entry, with a header this code writes from the entry's own path and modes. `old` and `new` are
    the trees (or commits) the entry was listed between; the note of a whole-file section names them in the command that prints the
    real diff. `hunks` and `text_of` (a blob id to its text) are what blob_patch and blob_text give, when the caller has them
    (entry_patches); without them they are read here.

    A modified text file whose hunk section (the `---`/`+++` lines and the hunks) is longer than its whole-file section (the note, the
    same two lines, an `@@` line and the file as it is now, marked +; whole_file_section) is shown whole: a rewrite's hunks repeat
    every old line as `-` and every new line as `+`. The two are compared as they would be attached, in characters, and whole needs to
    be strictly shorter (equal keeps the hunks), so the rule never makes a section longer, and a small edit to a small file, where the
    note alone would outweigh the hunks, keeps its hunks and shows the removed lines.
    Every other kind of entry (added, deleted, mode change, type change, binary, submodule) keeps its diff. This is the one place
    that rule is applied: the round diff, the base diff and the held sections all come through here via safe_diff."""
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
    if hunks is READ:
        hunks = blob_patch(top, *entry_pair(empty, entry))
    if hunks is None:
        out.append(f"Binary files a/{label} and b/{label} differ\n")
    elif hunks:
        hunks = hunks if hunks.endswith("\n") else hunks + "\n"
        plain = (f"--- {'/dev/null' if status == 'A' else 'a/' + label}\n+++ {'/dev/null' if status == 'D' else 'b/' + label}\n"
                 + hunks)
        if may_go_whole(entry, hunks):
            whole = whole_file_section(top, path, label, old, new, hunks, (text_of or (lambda blob: blob_text(top, blob)))(newid))
            if len(whole) < len(plain):
                plain = whole
        out.append(plain)
    return "".join(out)


HUNK_HEAD = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")  # git's hunk header: a count that is missing is 1
WHOLE_HEAD = re.compile(r"@@ whole file, (\d+) line\(s\) @@$")  # the header whole_file_section writes: no old lines, N new


def outside_hunks(lines):
    """(index, line) for each line of the diff `lines` (a list, split at newlines) that is not in a hunk body, in order: the file
    header lines (`diff --git`, `---`, `+++`) can only be there. After an `@@ -a,b +c,d @@` line the next lines are the body until its
    old and new counts are used up: a space line uses one of each, `-` one old, `+` one new, a backslash line (`\\ No newline at end of
    file`) neither. A line in a body that begins `--- a/x` is therefore a removed line that reads `-- a/x`, not a header.
    A diff this cannot follow raises Refused (as soon as it is reached, so the lines yielded before it are checked first): an `@@` line
    outside a body that is not a hunk header, a body line that is not a diff line (including a header or the end of the text: a hunk
    cut short), or a line that uses more than the count has left."""
    old = new = 0
    for at, line in enumerate(lines):
        if old or new:
            kind = line[:1]
            if kind == " ":
                old, new = old - 1, new - 1
            elif kind == "-":
                old -= 1
            elif kind == "+":
                new -= 1
            elif kind != "\\":  # (an empty line has no first character, so it is not one either)
                raise Refused(f"the diff has a hunk cut short: line {at + 1} is {line[:60]!r}, which is not a line of a hunk")
            if old < 0 or new < 0:
                raise Refused(f"the diff has a hunk longer than its header says: line {at + 1} is {line[:60]!r}")
            continue
        if line.startswith("@@"):
            head, whole = HUNK_HEAD.match(line), WHOLE_HEAD.match(line)
            if not head and not whole:
                raise Refused(f"the diff has an @@ line that is not a hunk header: {line[:60]!r}")
            old, new = (int(head.group(1) or 1), int(head.group(2) or 1)) if head else (0, int(whole.group(1)))
            continue
        yield at, line
    if old or new:
        raise Refused("the diff ends inside a hunk: it has fewer lines than the hunk header says")


def gate(text, safe_paths, withheld_paths, left_out=()):
    """Last check before attaching: every entry header in text names a checked, unflagged path, once. Anything else refuses.

    `left_out` are the paths whose contents were left out because they are verified copies (frozen_copies): none may have a
    section, by its header or by its ---/+++ lines.
    The lines are read with their hunks (outside_hunks): only a line outside a hunk body can be a header, so a removed line `-- a/x`
    and an added line `++ b/x` inside a hunk (they read `--- a/x` and `+++ b/x`) are not mistaken for the header lines of a section.
    A diff whose hunks cannot be followed refuses."""
    allowed = {f"diff --git a/{shown(p)} b/{shown(p)}": p for p in safe_paths}
    flagged = {shown(p) for p in withheld_paths}
    left = {shown(p) for p in left_out}
    left_headers = {f"a/{p} b/{p}" for p in left}
    seen = []
    lines = text.split("\n")
    for at, line in outside_hunks(lines):
        if line.startswith("diff --git "):
            if line[11:] in left_headers:
                raise Refused(f"the diff has a section for a path whose contents were left out: {line[11:]!r}")
            if line not in allowed:
                raise Refused(f"the diff names a path that was not checked as safe: {line[11:]!r}")
            seen.append(line)
        if line[:4] in ("--- ", "+++ ") and line[4:6] in ("a/", "b/") and line[6:] in flagged \
                and f"diff --git a/{line[6:]} b/{line[6:]}" not in allowed:
            raise Refused(f"the diff has a section for a flagged path: {line[4:]!r}")
        # a left-out path's file header pair outside any hunk
        if line.startswith("--- a/") and line[6:] in left and lines[at + 1:at + 2] == [f"+++ b/{line[6:]}"]:
            raise Refused(f"the diff has a section for a path whose contents were left out: {line[4:]!r}")
    if len(seen) != len(set(seen)) or set(seen) != set(allowed):
        raise Refused("the diff's entries are not the checked safe entries")


# Machine-generated files whose contents are named but left out of a review diff (drizzle-kit schema snapshots).
GENERATED = re.compile(r"(^|/)drizzle/meta/[^/]+_snapshot\.json$")


FROZEN = "frozen-copies.json"  # in a directory D: {"copies": [{"path": relative to D, "source": repo path, "commit": sha}]}
REGULAR = ("100644", "100755")
MAX_MANIFEST = 1 << 20  # bytes of a manifest read; a larger one is not parsed
LS_CHUNK = 256  # paths given to one `git ls-tree` call (one call lists them all unless a commit names more sources than this)


def commit_of(top, name):
    """The full id of the commit `name` names (a commit id, ref or HEAD), or None when it names no commit (a tree, or nothing)."""
    run = subprocess.run(["git", "-C", top, "rev-parse", "--verify", "-q", f"{name}^{{commit}}"], capture_output=True, text=True, errors="surrogateescape")
    return run.stdout.strip() if run.returncode == 0 and run.stdout.strip() else None


def ancestors(path):
    """The directories above the file `path`, nearest first, the repository root ("") last."""
    parts = path.split("/")[:-1]
    return ["/".join(parts[:count]) for count in range(len(parts), -1, -1)]


def plain_path(path):
    """Whether `path` can be given to git as a repository-relative name: no NUL, not absolute, encodable."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate, which JSON allows
        return False
    return bool(path) and not path.startswith("/") and "\0" not in path


def tree_files(top, treeish, paths):
    """{path: (mode, blob id)} of the file entries of the tree or commit `treeish` that are at exactly the given `paths` (a path that
    is a directory, or is not there, is left out). Read from the object database, never from the working files. One git call lists up
    to LS_CHUNK paths."""
    names, found = sorted({p for p in paths if plain_path(p)}), {}
    for at in range(0, len(names), LS_CHUNK):
        run = subprocess.run(["git", "-C", top, "ls-tree", "-z", "--full-tree", treeish, "--", *names[at:at + LS_CHUNK]],
                             capture_output=True, env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"})
        for record in run.stdout.split(b"\0") if run.returncode == 0 else []:
            head, _, name = record.partition(b"\t")
            fields = head.decode("ascii", "replace").split(" ")
            if len(fields) == 3 and fields[1] == "blob":
                found[name.decode("utf-8", "surrogateescape")] = (fields[0], fields[2])
    return found


def read_manifest(top, blobid):
    """({copy path: [entries]}, None) from the manifest blob, or (None, why it cannot be used). Any entry of the wrong shape (not an
    object with text `path`, `source` and `commit`) makes the whole manifest unusable: a manifest is read whole or not at all.
    The size is asked first (`git cat-file -s`), and a blob over MAX_MANIFEST is not read; a read stops at MAX_MANIFEST + 1 bytes."""
    size = subprocess.run(["git", "-C", top, "cat-file", "-s", blobid], capture_output=True, text=True, errors="surrogateescape")
    if size.returncode or not size.stdout.strip().isdigit():
        return None, "it cannot be read"
    if int(size.stdout) > MAX_MANIFEST:
        return None, f"it is larger than {MAX_MANIFEST} bytes"
    with subprocess.Popen(["git", "-C", top, "cat-file", "blob", blobid], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        data = proc.stdout.read(MAX_MANIFEST + 1)
        proc.kill()  # (a no-op when git has finished)
    if len(data) > MAX_MANIFEST:
        return None, f"it is larger than {MAX_MANIFEST} bytes"
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        return None, "it is not valid JSON"
    copies = value.get("copies") if isinstance(value, dict) else None
    if not isinstance(copies, list) or not all(
            isinstance(c, dict) and all(isinstance(c.get(k), str) and c[k] for k in ("path", "source", "commit")) for c in copies):
        return None, 'it is not {"copies": [{"path": ..., "source": ..., "commit": ...}]} with text values'
    found = {}
    for c in copies:
        found.setdefault(c["path"], []).append(c)
    return found, None


class History:
    """What one safe_diff asks git about the commits its manifests name, each asked once. `anchor` is the commit a copy's commit must be an
    ancestor of (None: nothing can be checked). The git calls are `rev-parse` once per distinct commit text, `merge-base` once per distinct
    commit, and `ls-tree` once per distinct commit for all the sources that commit is asked for (prefetch; LS_CHUNK per call), so
    the count follows the commits, not the copies."""
    COMMIT = re.compile(r"[0-9a-f]{7,64}")

    def __init__(self, top, anchor):
        self.top, self.anchor = top, anchor
        self.full, self.ancestry, self.files = {}, {}, {}

    def resolve(self, text):
        """The full id `text` names as a commit, or None."""
        if text not in self.full:
            self.full[text] = commit_of(self.top, text) if self.COMMIT.fullmatch(text) else None
        return self.full[text]

    def is_ancestor(self, full):
        """0 when `full` is an ancestor of the anchor (or the anchor), 1 when it is not, 2 when git cannot tell."""
        if full not in self.ancestry:
            run = subprocess.run(["git", "-C", self.top, "merge-base", "--is-ancestor", full, self.anchor], capture_output=True)
            self.ancestry[full] = run.returncode if run.returncode in (0, 1) else 2
        return self.ancestry[full]

    def prefetch(self, wanted):
        """List the sources in `wanted`, [(commit text, source)], that can be checked: one `ls-tree` call per distinct commit."""
        sources = {}
        for text, source in wanted:
            full = self.resolve(text)
            if full and self.anchor and self.is_ancestor(full) == 0 and plain_path(source):
                sources.setdefault(full, set()).add(source)
        for full, names in sources.items():
            found = tree_files(self.top, full, names)
            self.files.update({(full, name): found.get(name) for name in names})

    def file(self, full, source):
        """(mode, blob id) of `source` in commit `full`, or None when it is not a file there."""
        if (full, source) not in self.files:
            self.files[(full, source)] = tree_files(self.top, full, [source]).get(source)
        return self.files[(full, source)]


def verify_copy(history, path, record, copy, check):
    """(blob id, None) when the added or modified entry `path` is a verified copy, else (None, the reason, one line). The checks, in
    this order: the commit is a commit that is an ancestor of the history's anchor; the source is a file of that commit; neither the
    source nor the path is flagged by `check`; the blob of commit:source is the entry's new blob; the mode is the source's and a regular
    file's. Text taken from the manifest is named in a reason only once it is known to be a commit id or a path of that commit."""
    commit, source = copy["commit"], copy["source"]
    if not History.COMMIT.fullmatch(commit):
        return None, "cannot be left out: its commit is not a commit id (7 to 64 hex digits)"
    full = history.resolve(commit)
    if full is None:
        return None, f"cannot be left out: commit {commit} does not resolve to a commit in this repository"
    if history.anchor is None:
        return None, f"cannot be left out: there is no commit on the old side to check that {commit} is an ancestor of"
    anchor = history.anchor
    ancestry = history.is_ancestor(full)
    if ancestry == 1:
        return None, f"cannot be left out: commit {commit} is not an ancestor of {anchor[:12]}"
    if ancestry:
        return None, f"cannot be left out: commit {commit} cannot be checked against {anchor[:12]}"
    if not plain_path(source):
        return None, "cannot be left out: its source is not a repository-relative path"
    found = history.file(full, source)
    if found is None:
        return None, f"cannot be left out: the source is not a file of commit {commit}"
    where = f"{commit}:{shown(source)}"
    if check(history.top, source):
        return None, f"cannot be left out: {where} is a sensitive path"
    mode, blob = found
    if blob != record[3]:
        return None, f"does not match {where}"
    if mode != record[1] or mode not in REGULAR:
        return None, f"has mode {record[1]} but {where} has mode {mode}"
    return blob, None


def frozen_copies(top, new, kept, listed, anchor, check):
    """(entries still to be shown, [(path, record, commit, source, blob id)] left out, [one-line notes]) for the added or modified
    entries `kept` of a diff whose new side is `new`. `listed` maps the path of each manifest the --raw listing shows as added or
    modified to its record. The manifest that governs an entry is the nearest `frozen-copies.json` in a directory above it; its blob
    id comes from that listing, else from the tree `new` (the reviewed tree, never a path on disk). An entry the manifest does not
    list is an ordinary change. One that it lists and that fails a check, or any entry under a manifest that cannot be used, is
    shown in full with a note saying why; nothing here refuses the packet.

    Git calls, as a function of the diff: none when `kept` has no added or modified entry; else one `ls-tree` for the manifests the
    listing does not show (only the manifest names of the directories above the entries are asked for, never the whole tree: with
    none there, the entries are ordinary and nothing more is asked); two (`cat-file -s`, `cat-file blob`) per manifest that governs
    an entry; and, when an entry is listed, `rev-parse` + `merge-base` + `ls-tree` per distinct commit (History). Not one per copy."""
    candidates = [(path, record) for path, record in kept if record[4] in "AM" and path.rpartition("/")[2] != FROZEN]
    names = {f"{d}/{FROZEN}" if d else FROZEN for path, _ in candidates for d in ancestors(path)}
    governed = {path for path, _ in candidates}
    tree = tree_files(top, new, names - listed.keys()) if candidates else {}
    manifests = {}

    def manifest(directory):
        if directory not in manifests:
            name = f"{directory}/{FROZEN}" if directory else FROZEN
            record = listed.get(name)
            found = ((record[1], record[3]) if record[1] in REGULAR else None) if record is not None else tree.get(name)
            if found is None or found[0] not in REGULAR or check(top, name):
                manifests[directory] = None
            else:
                manifests[directory] = (name, *read_manifest(top, found[1]))
        return manifests[directory]
    rows, said = [], set()  # (path, record, how, text): how is "plain", "note" (a note, then plain) or "copy" (text: the manifest entries)
    for path, record in kept:
        governing, directory = None, ""
        if path in governed:
            for directory in ancestors(path):  # nearest first, the repository root last
                governing = manifest(directory)
                if governing is not None:
                    break
        if governing is None:
            rows.append((path, record, "plain", None))
            continue
        name, copies, why = governing
        if copies is None:
            note = None if name in said else f"{FROZEN}: {json.dumps(name)} cannot be used ({why}); every entry under {json.dumps(directory or '.')} is shown in full\n"
            said.add(name)
            rows.append((path, record, "note", note))
            continue
        listing = copies.get(path[len(directory) + 1 if directory else 0:])
        rows.append((path, record, "copy" if listing else "plain", listing))
    history = History(top, anchor)
    history.prefetch([(c[0]["commit"], c[0]["source"]) for _, _, how, c in rows if how == "copy" and len(c) == 1])
    out, left, notes = [], [], []
    for path, record, how, text in rows:
        if how == "note":
            notes.extend([text] if text else [])
        elif how == "copy":
            blob, why = (None, "is listed more than once") if len(text) > 1 else verify_copy(history, path, record, text[0], check)
            if blob is not None:
                left.append((path, record, text[0]["commit"], text[0]["source"], blob))
                continue
            notes.append(f"{FROZEN}: {json.dumps(path)} {why}; its full diff follows\n")
        out.append((path, record))
    return out, left, notes


def safe_diff(top, old, new, only=None, anchor=None):
    """(text, withheld paths): git diff old..new (just the paths in `only` when it is given) without the contents of any path the sensitive-read rule flags.

    Renames are split into a deletion and an addition (--no-renames) so each side is judged by its own path.
    The listing is of leaf entries (blobs, links, submodules) with their blob ids; each safe entry's patch is made from
    its own two blob ids, never from a path given to git, so a path that turned into a directory (or the reverse) cannot
    pull in entries that were not checked. The pairs are diffed together (blob_patches: one git diff of two trees this code
    makes from the safe entries' blob ids under names it chooses) and the texts a whole-file section needs are read together
    (blob_texts); the text is the same as one call per entry. gate() then verifies the finished text.

    An added or modified file that a `frozen-copies.json` manifest declares a byte-identical copy of a file of an older commit, and
    that passes every check (frozen_copies), is named in a note with its commit, source and blob id and left out of the body. The
    commit must be an ancestor of the old side when that is a commit, else of `anchor`, the commit the caller says the old side
    came from (a later round's old side is a tree: the HEAD its reviewed state recorded). With neither, nothing is left out.
    The Git work for copies depends on the manifests and the distinct commits they name, not on the number of copies: with no
    manifest in play there is none (no whole-tree listing); each manifest commit is resolved once, its ancestry checked once, and
    its sources listed once.
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
    safe, withheld, generated, listed = [], [], [], {}
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
        if path.rpartition("/")[2] == FROZEN and record[4] in "AM":
            listed[path] = record  # a manifest is read from its blob in this listing, whatever `only` selects
        if only is not None and path not in only:
            continue
        if check(top, path):
            withheld.append((path, record))
        elif GENERATED.search(path):
            generated.append((path, record))
        else:
            safe.append((path, record))
    anchor = commit_of(top, old) or (commit_of(top, anchor) if anchor else None)
    safe, left, problems = frozen_copies(top, new, safe, listed, anchor, check)
    body = "".join(entry_patches(top, empty, safe, old, new))
    labels = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}
    notes = "".join(f"{json.dumps(p)} ({labels[r[4]]}): sensitive path, its contents were withheld from this diff\n"
                    for p, r in withheld)
    notes += "".join(f"{json.dumps(p)} ({labels[r[4]]}): generated file, its contents were left out of this diff; open it if needed\n"
                     for p, r in generated)
    notes += "".join(f"{json.dumps(p)} ({labels[r[4]]}): byte-identical to {commit}:{shown(source)} (blob {blob}), contents left out; "
                     f"check with git cat-file -p {blob} or open the file\n" for p, r, commit, source, blob in left)
    notes += "".join(problems)
    gate(body, [p for p, _ in safe], [p for p, _ in withheld], [entry[0] for entry in left])
    return notes + body, [p for p, _ in withheld]


def write(path, text):
    return safe_write.write_atomic(path, text, errors="surrogateescape", root=safe_write.task_root(Path(path).parent))


def diff_now(now, lane, red):
    """(text, withheld paths, redaction count, plan): the diff of the tree as it is now (`now` is snapshot(): the working tree with
    staged, unstaged and untracked files) against the review base (review_base: a lane's last closed step, else the lane base,
    else HEAD), with the same checks the later rounds' diff gets: safe_diff withholds sensitive paths, redact_diff the secrets.
    The text starts with a line saying which base it is measured from. A base that cannot be worked out is said in the text, not
    guessed; a diff that cannot be checked raises Refused.

    When the commits since the base include a merge of an upstream branch (upstream_reduction), the diff is of what the step
    authored: each such merge as its resolution (remerge_diff), the commits around it as their own first-parent diffs, then the
    last merge to the files as they are now; and the plan's base is that last merge. The text and the scope say so."""
    top = now["top"]
    try:
        base, basis = review_base(top, lane)
    except Refused as exc:
        return f"No diff: the base to measure the change from could not be worked out ({' '.join(str(exc).split())[:200]}). Inspect the working tree directly.\n", [], 0, None
    label = basis[len("base: "):] if basis else ("HEAD" if base == "HEAD" else "the empty tree (the repository has no commit yet)")
    reduced = upstream_reduction(top, base)
    if reduced:
        label = f"{label}, with an upstream merge reduced to its resolution"
        prefix, withheld, count = reduced_text(top, reduced, red)
        base = reduced["last"]
    else:
        prefix, withheld, count = "", [], 0
    head = (f"[Change in the working tree against {label} (git diff, {base[:12]} to the files as they are now; untracked files included). "
            f"{SECTIONS_NOTE}]\n\n")
    if reduced:
        head += reduced_note(reduced) + "\n"
        label = f"merge {base[:12]}"
    diff, more = safe_diff(top, base, now["tree"])
    withheld += [p for p in more if p not in withheld]
    diff, found = redact_diff(red, diff)
    count += found
    text = head + prefix + (diff if diff.strip() else f"(empty: the working tree is identical to {label})\n")
    extra, more, found, held = held_sections(top, base, now["tree"], red)
    return (text + extra, withheld + [p for p in more if p not in withheld], count + found,
            make_plan(top, base, basis, now["tree"], held, reduced=reduced))


def upstream_reduction(top, base):
    """None, or {"origin": the review base commit, "last": the last upstream merge, "merges": [{"merge", "first", "upstream",
    "paths"}] oldest first}: the merge commits on the first-parent line from `base` to HEAD that merge an upstream branch, a merge
    of exactly two parents whose second parent is not a descendant of `base` (a branch the step did not start from). `paths` are
    the paths the merge's resolution changes (git show --remerge-diff, empty for a clean auto-merge). None when there is no such
    merge, `base` or HEAD is not a commit, or git cannot answer (no --remerge-diff before git 2.36): the caller then keeps the
    ordinary diff against `base`. An octopus merge, and a merge of a branch that descends from `base`, are ordinary commits."""
    root = commit_of(top, base)
    if root is None or commit_of(top, "HEAD") is None:
        return None
    listed = git(top, "rev-list", "--first-parent", "--merges", "--parents", "--reverse", f"{root}..HEAD")
    if listed.returncode:
        return None
    merges = []
    for line in listed.stdout.splitlines():
        ids = line.split()
        if len(ids) != 3:
            continue
        merge, first, upstream = ids
        descends = git(top, "merge-base", "--is-ancestor", root, upstream).returncode
        if descends == 0:
            continue
        if descends != 1:
            return None
        names = subprocess.run(["git", "-C", top, "show", "--remerge-diff", "--format=", "--name-only", "-z", "--no-renames", merge],
                               capture_output=True)
        if names.returncode:
            return None
        try:
            paths = sorted({p.decode("utf-8") for p in names.stdout.split(b"\0") if p})
        except UnicodeDecodeError:
            return None
        merges.append({"merge": merge, "first": first, "upstream": upstream, "paths": paths})
    return {"origin": root, "last": merges[-1]["merge"], "merges": merges} if merges else None


def reduced_ranges(reduced):
    """[(commit the commits before this merge start from, merge)]: what the lane authored before each merge is that commit to
    the merge's first parent (nothing when they are the same commit); the next merge starts from this one."""
    out, old = [], reduced["origin"]
    for merge in reduced["merges"]:
        out.append((old, merge))
        old = merge["merge"]
    return out


def reduced_note(reduced):
    """The sentence that says an upstream merge was reduced to its resolution, naming each merge and its upstream parent."""
    merges = "; ".join(f"merge commit {m['merge'][:12]} brought in upstream {m['upstream'][:12]}" for m in reduced["merges"])
    return (f"[An upstream merge was reduced to its resolution: {merges}. For each, only the conflict resolutions and edits made in the "
            "merge commit (git show --remerge-diff) are shown, and the commits around it as their own diffs; everything else upstream "
            "brought in is out of scope and not shown. The changed files and scoped commands follow the same reduced set.]\n")


def remerge_diff(top, merge, paths, check):
    """(text, withheld paths): `git show --remerge-diff` of the merge commit for the `paths` of its resolution, with the same rule as
    safe_diff: a sensitive path is named and its contents withheld, a generated file is named and left out, and gate() checks the
    text. A merge whose resolution changes nothing gives ("", [])."""
    withheld = [p for p in paths if check(top, p)]
    generated = [p for p in paths if p not in withheld and GENERATED.search(p)]
    safe = [p for p in paths if p not in withheld and p not in generated]
    body = ""
    for at in range(0, len(safe), LS_CHUNK):
        chunk = safe[at:at + LS_CHUNK]
        run = subprocess.run(["git", "-C", top, "-c", "core.quotepath=false", "show", "--remerge-diff", "--format=", "--no-color",
                              "--no-ext-diff", "--no-textconv", "--no-renames", merge, "--", *chunk],
                             capture_output=True, env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"})
        if run.returncode:
            raise Refused(f"cannot show the resolution of merge {merge[:12]}: {run.stderr.decode('utf-8', 'replace').strip()}")
        part = run.stdout.decode("utf-8", "surrogateescape")
        gate(part, chunk, withheld)
        body += part
    notes = "".join(f"{json.dumps(p)}: sensitive path, its contents were withheld from this diff\n" for p in withheld)
    notes += "".join(f"{json.dumps(p)}: generated file, its contents were left out of this diff; open it if needed\n" for p in generated)
    return notes + body, withheld


def reduced_text(top, reduced, red):
    """(text, withheld paths, redaction count): the sections before the last merge, in commit order: for each upstream merge the
    commits the lane made before it (first-parent diff), then its resolution (remerge_diff). Each goes through safe_diff or
    remerge_diff and redact_diff; an empty section is a line saying so for a resolution, nothing for commits."""
    check = sensitive_checker()
    text, withheld, total = "", [], 0
    for old, merge in reduced_ranges(reduced):
        if old != merge["first"]:
            diff, more = safe_diff(top, old, merge["first"])
            diff, count = redact_diff(red, diff)
            withheld += [p for p in more if p not in withheld]
            total += count
            text += f"\nCommitted before merge {merge['merge'][:12]} ({old[:12]} to {merge['first'][:12]}):\n{diff}" if diff.strip() else ""
        diff, more = remerge_diff(top, merge["merge"], merge["paths"], check)
        diff, count = redact_diff(red, diff)
        withheld += [p for p in more if p not in withheld]
        total += count
        text += (f"\nResolution of merge {merge['merge'][:12]} (upstream parent {merge['upstream'][:12]}; git show --remerge-diff):\n"
                 + (diff if diff.strip() else "(empty: the merge is a clean auto-merge, nothing was resolved or edited in it)\n"))
    return text + f"\nCommitted after the last merge and not committed ({reduced['last'][:12]} to the files as they are now):\n", withheld, total


STAGED_HEAD = "Staged, not in the working files:"
COMMITTED_HEAD = "Committed since the base, not in the working files:"
SECTIONS_NOTE = (f"A change that HEAD or the index holds and the files do not have follows, per path, under '{COMMITTED_HEAD}' "
                 f"(base to HEAD) and '{STAGED_HEAD}' (HEAD to index).")


def changed_paths(top, old, new):
    """The set of paths whose entry differs between the trees (or HEAD) `old` and `new`."""
    run = git(top, "diff", "--name-only", "-z", "--no-renames", old, new, "--")
    if run.returncode:
        raise Refused(f"cannot list the changes between {old[:12]} and {new[:12]}: {run.stderr.strip()}")
    return {p for p in run.stdout.split("\0") if p}


def head_name(top):
    """HEAD, or the empty tree when HEAD does not resolve (no commit yet): every command and comparison that names HEAD uses it."""
    if git(top, "rev-parse", "--verify", "-q", "HEAD").returncode == 0:
        return "HEAD"
    return git(top, "hash-object", "-t", "tree", "/dev/null").stdout.strip()  # sha1 or sha256 per the repository's object format


def held_plan(top, base, tree):
    """(note, [(title, old, new, paths)]): the sections held_sections shows, as the paths each one covers. The one computation of
    which paths a section has: the four states of a path are base, HEAD (the empty tree when there is no commit), the index and
    the working tree `tree`; for every path where HEAD or the index differs from `tree`, COMMITTED_HEAD is base to HEAD and
    STAGED_HEAD is HEAD to index, each only if it changes that path. `note` is a line instead when the index cannot be read."""
    with tempfile.TemporaryDirectory() as tmp:  # a copy, so the author's index file is not rewritten (write-tree stores a cache tree in it)
        real = git(top, "rev-parse", "--path-format=absolute", "--git-path", "index")
        copy = Path(tmp) / "index"
        if real.returncode == 0 and Path(real.stdout.strip()).is_file():
            shutil.copyfile(real.stdout.strip(), copy)
        index = git(top, "write-tree", env={**os.environ, "GIT_INDEX_FILE": str(copy)})
    if index.returncode:  # an unmerged index has no tree
        return f"\n{STAGED_HEAD} not shown: the index cannot be read as a tree ({' '.join(index.stderr.split())[:150]}).\n", []
    index = index.stdout.strip()
    head = head_name(top)
    only = changed_paths(top, head, tree) | changed_paths(top, index, tree)
    found = []
    for title, old, new in ((COMMITTED_HEAD, base, head), (STAGED_HEAD, head, index)):
        paths = only & changed_paths(top, old, new) if only else set()
        if paths:
            found.append((title, old, new, paths))
    return "", found


def held_sections(top, base, tree, red, anchor=None):
    """(text, withheld paths, redaction count, paths): a path has four states, base, HEAD, index and the working tree `tree` (see
    snapshot); the main diff is base to `tree`. For every path where HEAD or the index differs from `tree`, a section for each
    comparison that is not empty: COMMITTED_HEAD (base to HEAD) and STAGED_HEAD (HEAD to index), with the same withholding and
    redaction as the main diff. Without them a change the files no longer have (committed then undone, staged and then
    restored, removed, or edited again) would be missing from the packet. One rule for round 1 and later rounds (`base` is
    then the previous round's tree). "" when there is none. `paths` is every path in a section (held_plan).
    Whether a section appears depends only on those four states: no other condition (not where HEAD was at the previous
    round, not any field of a reviewed-state record) applies. `anchor` is what safe_diff takes: the commit a verified copy's
    commit must be an ancestor of when `base` is not itself a commit (a later round)."""
    note, sections = held_plan(top, base, tree)
    if note:
        return note, [], 0, []
    text, withheld, total, paths = "", [], 0, set()
    for title, old, new, only in sections:
        diff, more = safe_diff(top, old, new, only, anchor)
        diff, count = redact_diff(red, diff)
        withheld += [p for p in more if p not in withheld]
        total += count
        paths |= only
        text += f"\n{title}\n{diff}" if diff.strip() else ""
    return text, withheld, total, sorted(paths)


def make_plan(top, base, basis, tree, held, previous=0, reduced=None):
    """The scope's input, from the same computation that built the attached diffs: `paths` is exactly the union of the paths of
    the main diff (`base` to `tree`) and of every held section (`held`, from held_sections or held_plan). `base` is what the main
    diff compares against (in a later round, `previous` > 0 is that round, the tree it reviewed); `basis` says which review base
    a lane's round 1 used; `head` is what scoped commands name as HEAD (the empty tree when there is no commit);
    `untracked` the paths no commit or index holds. `reduced` (upstream_reduction) is kept in the plan, with `base` the last merge: the
    paths then also include those of every section before it."""
    paths = changed_paths(top, base, tree) | set(held)
    for old, merge in reduced_ranges(reduced) if reduced else ():
        paths |= changed_paths(top, old, merge["first"]) | set(merge["paths"])
    paths = sorted(paths)
    others = git(top, "ls-files", "--others", "--exclude-standard", "-z")
    if others.returncode:
        raise Refused(f"git could not list the untracked files: {others.stderr.strip()}")
    new = {p for p in others.stdout.split("\0") if p}
    return {"top": top, "base": base, "basis": basis, "previous": previous, "head": head_name(top),
            "has_head": git(top, "rev-parse", "--verify", "-q", "HEAD").returncode == 0,
            "paths": paths, "untracked": [p for p in paths if p in new], "reduced": reduced}


def plan_for(now, lane):
    """The plan for the change against the review base without making any diff text (what a round-1 prepare gives); raises Refused."""
    base, basis = review_base(now["top"], lane)
    reduced = upstream_reduction(now["top"], base)
    base = reduced["last"] if reduced else base
    note, sections = held_plan(now["top"], base, now["tree"])
    return make_plan(now["top"], base, basis, now["tree"], set().union(*[s[3] for s in sections]) if sections else set(), reduced=reduced)


def reviewed_history(top, state, lane):
    """The commit a later round's verified copies must descend from: the old side of that round's diff is the tree the last round
    reviewed, not a commit, so it is the HEAD that round's reviewed state recorded (everything up to it was in front of that
    reviewer); None when the state recorded no commit. A state with no `head` field (older records) uses the review base."""
    head = state.get("head")
    if isinstance(head, str):
        return commit_of(top, head) if head else None
    try:
        return commit_of(top, review_base(top, lane)[0])
    except Refused:
        return None


def findings_window(state_dir, task_id, rows):
    """The round number the attached findings start at: the round of the review that triggered the newest gated retry of the chain
    (`gate_review`, or for a legacy task the parent's newest review made before it), so a round before it is an older window
    (summary_line); 0 (all rounds in full) when no author of the chain is gated or nothing was reviewed before the gate. A gate with
    no recorded trigger (made between the first and second round of RR1) starts at the last review before the gated author's rows.
    Raises Refused when `gate_review` is not a review of the chain that has a reply."""
    gated = gated_author(state_dir, task_id)
    if gated is None:
        return 0
    author, _, trigger = gated
    if trigger == "":
        return 0
    if trigger is not None:
        for _, review, _, _, number in rows:
            if review == trigger and number:
                return number
        raise Refused(f"gate_review '{trigger}' of '{author}' is not a review with a reply in the retry chain of '{task_id}'")
    start = 0
    for row_author, _, _, _, number in rows:
        if row_author == author:
            break
        if number:
            start = number
    return start


FINDING_HEADING = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]+)?(?:[-*+][ \t]+)?(?:\*\*|__)?(F\d+)\b[^\n]*")


def finding_headings(text):
    """The heading line of each finding of a reply (`F1 — ...`), outside fenced code, cut at 160 characters."""
    found, fence = [], None
    for line in text.replace("\r\n", "\n").split("\n"):
        match = review_verdict.FENCE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                continue
        else:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not match.group(2).strip():
                fence = None
            continue
        heading = FINDING_HEADING.match(line)
        if heading:
            line = heading.group(0).strip()
            found.append(line if len(line) <= 160 else line[:157] + "...")
    return found


def summary_line(number, review, verdict, families, headings):
    """One line for an older round: round, review task id, verdict, the family names and the finding headings; no bodies."""
    return (f"round {number}; review {review}; {verdict or 'no verdict'}; families: {', '.join(families) or 'none named'}; "
            f"findings: {' | '.join(headings) or 'none found'}")


def prepare(state_dir, task_id, workdir, out_dir, lane=""):
    """Writes the diff then the earlier findings into out_dir; `contexts` lists them in the order a reviewer reads them: the diff
    of the tree against the review base first (diff_now; from the last reviewed state instead when an earlier round recorded one),
    then the earlier findings, oldest first. `lane` is the author's lane name ("" for none) and picks the base of diff_now."""
    out = Path(out_dir)
    rows = chain_labels(state_dir, task_id)
    now = snapshot(workdir)
    contexts, skipped, last_state, last_round = [], [], None, 0
    red, redactions, withheld = redactor(), 0, []
    # a review that never produced a reply has no findings to show; a reply that is not a regular file is refused
    reviews = []
    for author, review, _, names, number in rows:
        if review is None:
            continue
        result = task_path(state_dir, review) / "result.md"
        if result.is_symlink() or (result.exists() and not result.is_file()):
            raise Refused(f"review '{review}' of '{author}' has an unreadable result.md")
        (reviews if number else skipped).append((author, review, names, number))
    earlier = reviews
    window = findings_window(state_dir, task_id, rows)
    older = [item for item in earlier if item[3] < window]
    labels = {row[1]: row[2] for row in rows}
    if older:
        lines = []
        for author, review, names, number in older:
            raw = (task_path(state_dir, review) / "result.md").read_text(encoding="utf-8", errors="surrogateescape")
            lines.append(summary_line(number, review, labels.get(review), names, finding_headings(raw)))
        summary, count = redact_text(red, "\n".join(lines) + "\n", "summary of older review rounds")
        redactions += count
        head = (f"[Older review rounds {older[0][3]}-{older[-1][3]}, one line each (round, review task, verdict, families, finding headings; no bodies): "
                "a retry made with a gated route changed the rule or the design after them, so only the findings from the review that triggered it on are attached "
                "in full. The full text of each is its review task's result.md.]\n\n")
        contexts.append(write(out / "earlier-summary.md", head + summary))
    for author, review, _, number in earlier:
        if number < window:
            continue
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
    diff_file, plan = None, None
    if earlier:
        label = f"diff-since-round-{last_round}.md"
        head = (f"[Change in the working tree since the state review round {last_round} reviewed (git diff, round {last_round} tree to the files as they are now). "
                f"{SECTIONS_NOTE}]\n\n")
        if now is None and last_state is None:
            body = (f"No diff: review round {last_round} recorded no reviewed state (it ran before states were recorded, "
                    "or its working directory was not a git repository). Inspect the working tree directly.\n")
        elif now is None:
            raise Refused(f"round {last_round} recorded a reviewed state but {workdir} is not in a git repository now")
        elif last_state is None or last_state["workdir"] != now["workdir"]:
            # no state to start from: the diff of the tree against the review base instead (diff_now), after the reason
            why = (f"review round {last_round} recorded no reviewed state (it ran before states were recorded, or its working directory "
                   "was not a git repository)" if last_state is None else
                   f"round {last_round} reviewed {last_state['workdir']} and this review is of {now['workdir']}")
            body, withheld, count, plan = diff_now(now, lane, red)
            redactions += count
            head = f"[No diff since the last round: {why}. The change against the review base follows instead.]\n\n"
        else:
            anchor = reviewed_history(now["top"], last_state, lane)
            diff, withheld = safe_diff(now["top"], last_state["tree"], now["tree"], anchor=anchor)
            diff, count = redact_diff(red, diff)
            extra, more, found, held = held_sections(now["top"], last_state["tree"], now["tree"], red, anchor)
            plan = make_plan(now["top"], last_state["tree"], "", now["tree"], held, last_round)
            diff += extra
            withheld = withheld + [p for p in more if p not in withheld]
            redactions += count + found
            body = diff if diff.strip() else "(empty: the working tree is identical to the one round " \
                f"{last_round} reviewed)\n"
        diff_file = write(out / label, head + body)
    elif now is not None:  # round 1: no earlier state, so the tree against the review base
        body, withheld, count, plan = diff_now(now, lane, red)
        redactions += count
        diff_file = write(out / "diff-now.md", body)
    # the scope's input, written beside the diffs it was computed with (scope and timers read it with --input)
    scope_input = write(out / "scope-input.json", json.dumps(plan)) if plan else None
    return {"contexts": ([diff_file] if diff_file else []) + contexts, "rounds": len(earlier), "families": [names for _, _, names, _ in earlier],
            "summarised": [{"round": number, "review": review, "result": str(task_path(state_dir, review) / "result.md")} for _, review, _, number in older],
            "snapshot": now, "scope_input": scope_input,
            "skipped": [review for _, review, _, _ in skipped], "withheld": withheld, "redactions": redactions}


MAX_SCOPE_PATHS = 200  # changed paths named in the assignment (safe and withheld together); the rest go to the list file
MAX_SCOPE_BYTES = 16384  # and at most this many bytes of them, so long names cannot crowd out the packet's author evidence


def review_base(top, lane=""):
    """(base, basis) the change under review is measured from, in the repository at `top`; raises Refused. For a lane task: the
    lane's last closed step when that commit is an ancestor of HEAD, else the lane base (basis says which and why); for a task
    outside a lane: HEAD (the empty tree when there is no commit yet), basis "". The one rule scope_lines (the changed-file list
    and scoped commands), the timer waits and diff_now (the round-1 diff) share, so the three always name the same base."""
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
    return base, basis


def load_plan(source):
    """A plan (make_plan) from a dict or the path of the JSON file prepare wrote; raises Refused when it cannot be used."""
    if isinstance(source, (str, os.PathLike)):
        source = read_json(Path(source))
    keys = {"top": str, "base": str, "basis": str, "previous": int, "head": str, "has_head": bool, "paths": list, "untracked": list}
    if not isinstance(source, dict) or any(not isinstance(source.get(k), t) for k, t in keys.items()) \
            or not isinstance(source.get("reduced"), (dict, type(None))):
        raise Refused("the scope input written with the diffs is unreadable")
    return source


def scope_lines(workdir, lane="", plan=None):
    """(base, top, safe paths [(path, untracked)], withheld paths, basis, plan) for the author's change, or None outside git;
    raises Refused. The paths are the plan's: the paths of the attached diffs (see make_plan), never recomputed here; without a
    `plan` the round-1 plan is made (plan_for). basis says which base a lane review diffs from and why ("" for a task outside a
    lane and in a later round): see review_base."""
    if plan is None:
        top = git(workdir, "rev-parse", "--show-toplevel")
        if top.returncode:
            return None
        plan = plan_for(snapshot(workdir), lane)
    else:
        plan = load_plan(plan)
    base, top, basis = plan["base"], plan["top"], plan["basis"]
    new = set(plan["untracked"])
    paths = list(plan["paths"])
    check = sensitive_checker()
    flagged = {path for path in paths if check(top, path)}
    # git matches even a literal pathspec as a directory prefix, so a path above a withheld one (a file that became a
    # directory, or the reverse) would select it: such a path is withheld too.
    withheld = [path for path in paths if path in flagged or any(f.startswith(path + "/") for f in flagged)]
    held = set(withheld)
    safe = [(path, path in new) for path in paths if path not in held]
    return base, top, safe, withheld, basis, plan


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


def scope_text(workdir, lane="", list_file=None, budget=None, plan=None):
    """(assignment text, withheld paths): the changed files and the scoped git commands a reviewer may use. Never raises:
    when the list cannot be made it says so in one line and the review goes on as it did before. At most MAX_SCOPE_PATHS paths
    and MAX_SCOPE_BYTES bytes of path lines (safe and withheld together) are named in the assignment; when more changed and
    list_file is given, the whole list (one quoted path per line, withheld ones marked) is written there. When budget (characters)
    is given the text never exceeds it: the inline list shrinks to fit, down to the pointer line alone, and when even that does not
    fit (or the list file cannot be written) the one-line unavailable text is returned."""
    try:
        found = scope_lines(workdir, lane, plan)
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
    base, top, safe, withheld, basis, plan = found
    head, previous = plan["head"], plan["previous"]
    since = "since its last closed step" if basis.startswith("base: last closed step") else "since its base"
    kinds = f"committed on the lane {since}, staged, unstaged and untracked" if lane else "staged, unstaged and untracked"
    if previous:  # a later round: the main diff is against the tree that round reviewed
        relative, kinds = f"the tree review round {previous} reviewed ({base[:12]})", "committed, staged, unstaged and untracked"
    else:
        relative = base[:12] if lane else "HEAD"  # with no commit yet HEAD is the empty tree (the commands below name it)
    reduced = plan.get("reduced")
    if reduced:  # the review base, not the last merge the commands below start from
        relative = f"{reduced['origin'][:12]} (an upstream merge reduced to its resolution; the last merge is {base[:12]})"
    out = [f"Diff scope: {len(safe)} file(s) changed relative to {relative} ({kinds}) in the repository at {literal(top)}:"]
    if basis:  # a lane task: which base the list and the commands below use, and what that leaves out of scope
        closed = " Closed steps are out of scope except where the new lines change or call them." if "closed step" in basis else ""
        out.insert(0, f"{basis}.{closed}")
    out.append(f"What the attached diff covers: the main diff compares the working tree (the files as they are now, untracked included) against "
               f"{base[:12] if lane or basis or previous else relative}; a later round compares it against the tree the previous round reviewed. For a path where HEAD "
               f"or the index differs from the files, up to two sections follow: '{COMMITTED_HEAD}' is the base against HEAD, and '{STAGED_HEAD}' is HEAD "
               "against the index (only the staged change, not the base against the index).")
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
    if reduced:
        out.insert(1 if basis else 0, reduced_note(reduced).strip())
    # `head` names HEAD, or the empty tree when the repository has no commit yet, so every printed command runs
    committed = f"`{git} diff {base} {head} -- <path> [<path> ...]` (base against {'HEAD' if plan['has_head'] else 'the empty tree'}), " if base != head else ""
    merge_commands = "".join(f" For merge {m['merge'][:12]}: `{git} show --remerge-diff {m['merge']} -- <path> [<path> ...]` (its resolution)"
                             + (f" and `{git} diff {old} {m['first']} -- <path> [<path> ...]` (the commits before it)" if old != m["first"] else "") + "."
                             for old, m in reduced_ranges(reduced)) if reduced else ""
    out.append(f"Scoped commands (keep the GIT_LITERAL_PATHSPECS=1 prefix: it makes each path a literal file name, never a glob or pathspec magic): "
               f"`{git} diff {base} -- <path> [<path> ...]` (working tree against the base), {committed}"
               f"`{git} diff --cached {head} -- <path> [<path> ...]` (staged: the index against {'HEAD' if plan['has_head'] else 'the empty tree'}), "
               f"`{git} diff --stat {base} -- <path> ...`. Run {'the working-tree, the base-against-HEAD and the staged commands' if committed else 'both the working-tree and the staged command'} for a path: a change "
               f"the working tree undoes shows only in the others.{merge_commands} "
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


def round_one_plan(source):
    """The plan prepare wrote (`source`, its scope-input.json) when it is the one scope_lines makes without a plan, else None.
    That is a plan from diff_now (round 1, or a later round with no usable reviewed state): `previous` 0, made by the same rule as
    plan_for. A later round's plan is of the diff since the reviewed tree (`previous` > 0), which the timer waits do not cover; an
    unreadable one is not used either, so the timer waits are then computed as they were without it."""
    if source is None:
        return None
    try:
        plan = load_plan(source)
    except Refused:
        return None
    return plan if plan["previous"] == 0 else None


def timer_waits(workdir, lane="", plan=None):
    """The text of the packet's `Timer waits in added test lines` file: every added line of the review diff, in a file whose
    path matches a test pattern, that contains one of TIMER_CALLS, as `path:line: text`, a line carrying `timer-ok:` marked
    `(timer-ok)`; one line saying none were found when there are none. Facts only. Withheld (sensitive) paths are never read, a
    listed line goes through the redaction pass, and the text is never an error: what cannot be computed is said in one line.
    The changed paths are the round-1 plan's (scope_lines): `plan` is the scope-input.json prepare wrote, used only when it is
    that plan (round_one_plan), so it is not made a second time."""
    head = f"# {TIMER_TITLE}: not computed\n\n"  # the first line carries the title and the counts together
    try:
        found = scope_lines(workdir, lane, round_one_plan(plan))
        if found is None:
            return head + "Not computed: the working directory is not a git repository.\n"
        base, top, safe = found[:3]
        rows, seen = [], set()
        for path, untracked in safe:
            if not TEST_PATH.search(path):
                continue
            for number, text in added_lines(top, base, path, untracked):
                if any(call in text for call in TIMER_CALLS) and (path, number, text) not in seen:
                    seen.add((path, number, text))
                    full = text.strip()
                    clipped = full[:MAX_TIMER_TEXT] + (f"… (truncated, {len(full)} chars)" if len(full) > MAX_TIMER_TEXT else "")
                    rows.append((path, number, f"{shown(path)}:{number}", f"{clipped}{' (timer-ok)' if 'timer-ok:' in text else ''}"))
        rows.sort(key=lambda row: row[:2])
        if not rows:
            return (f"# {TIMER_TITLE}: none found\n\n"
                    "No added test line contains setTimeout(, sleep(, delay(, Deno.sleep( or waitForTimeout(; none were found.\n")
        # every matching line is kept: the first MAX_TIMER_LINES with their text, the rest as path:line only
        body = "\n".join(f"{where}: {text}" if n < MAX_TIMER_LINES else where for n, (_, _, where, text) in enumerate(rows))
        body, _ = redact_text(redactor(), body, "timer waits in added test lines")
        bare = max(0, len(rows) - MAX_TIMER_LINES)
        return (f"# {TIMER_TITLE}: {len(rows)} matching added line(s); {bare} listed as path:line without their text\n\n"
                "Added lines in files whose path contains test, spec, e2e or __tests__ that contain setTimeout(, sleep(, delay(, "
                "Deno.sleep( or waitForTimeout( (a plain match; the reviewer judges them; `(timer-ok)` marks a line carrying `timer-ok:`):\n\n"
                + body + "\n")
    except (Refused, OSError) as exc:
        return head + f"Not computed: {' '.join(str(exc).split())[:200]}.\n"  # same first line as the git-less case


def main(argv):
    if len(argv) in (5, 7) and argv[1] == "timers" and (len(argv) == 5 or argv[5] == "--input"):
        # timers <workdir> <lane> <out-file> [--input <scope-input.json written by prepare>]
        try:
            write_list_file(argv[4], timer_waits(argv[2], argv[3], argv[6] if len(argv) == 7 else None))
        except OSError as exc:
            print(f"review_rounds: cannot write {argv[4]}: {exc}", file=sys.stderr)
            sys.exit(1)
        return
    plan = None
    if len(argv) > 3 and argv[-2] == "--input" and argv[1] == "scope":  # scope ... --input <scope-input.json written by prepare>
        plan, argv = argv[-1], argv[:-2]
    if len(argv) in (3, 4, 5, 6) and argv[1] == "scope":
        text, withheld = scope_text(*argv[2:5], budget=int(argv[5]) if len(argv) == 6 else None, plan=plan)
        print(json.dumps({"text": text, "withheld": withheld}))
        return
    if len(argv) not in (6, 7) or argv[1] != "prepare":
        sys.exit(__doc__)
    try:
        print(json.dumps(prepare(*argv[2:])))
    except Refused as exc:
        print(f"review_rounds: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main(sys.argv)
