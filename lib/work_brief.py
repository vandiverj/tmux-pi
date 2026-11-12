"""`tp-agent brief` (HB3a): Main's working brief, built from records only (no model call), at most 60 lines.

Parts (docs/plans/det-prep/hub/interfaces.md section 5): the workspace and its outcome and acceptance; the active records whose
scope covers this Main (global, project:<repo> holding the session's project, and workspace for an attached workspace); the active
work from the session's task records (tasks, reviews with their verdict, lanes); the open decisions (ids only, never question text,
as the index); obligations (HB5) as a named empty slot until those records exist; the launch records (HB2b, lib/launch_record.py:
each listed task's contract version, role, route and unsupported capabilities, and Main's own record); the last approved
review and the evidence it was given; pointers. Each part states its record time, and a part older than the newest compaction is marked stale.

Size: at most MAX_LINES lines, by section budgets (`assemble`), never by a cut of the whole text. The fixed parts are never cut; the
rules, work and decisions sections share the lines left, and one over its budget keeps its first lines and ends `+N more: COMMAND`.

With `attach`, a session with no attached workspace attaches the newest active workspace whose `repos` covers the project (the
others that match are listed); with none, the first line says so and how to make one. Nothing is ever created. The text that
reaches Main is owner-confirmed (only `active` records are shown, and only the owner makes a record active), so worker text never
lands in Main's context through it.
"""
import json
import os
from pathlib import Path
import re
import sys

import launch_record
import work
import work_checkpoint
import review_verdict
import work_index

MAX_LINES = 60
LINE_CHARS = 240
MORE = {"rules": "tp-agent work records",  # every status the rules section counts (active, pending, provisional) comes back
         "work": "tp-agent list", "decisions": "tp-agent work decisions"}
MAIN_SCOPES = ("global", "project", "workspace")
NO_WORKSPACE = "no workspace attached: `tp-agent work new TITLE --repo {project} --objective TEXT && tp-agent work attach ID`"  # a Whole
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def add_parser(sub):
    p = sub.add_parser("brief", help="print Main's working brief from records only (outcome, acceptance, rules in force, active work, "
                                      "open decisions, evidence, pointers; no model call)")
    p.add_argument("--attach", action="store_true", help="first attach the newest active workspace for this project when none is "
                                                          "attached (Claude Main's SessionStart hook; never creates one)")
    p.add_argument("--workdir", help="the session's project (default TP_WORKDIR, else the current directory)")
    p.add_argument("--hook-json", action="store_true", help="print the SessionStart hook output, one JSON line")
    p.add_argument("--source", choices=["startup", "resume", "compact"], default=None,
                   help="the SessionStart source; with compact the task index text follows the brief (register.tsx injects it only at startup)")


class Whole(str):
    """A command, id, path or name Main must be able to use: never clipped, never split. Everything that is not a Whole is prose."""


def _clip(text, size=LINE_CHARS):
    """Clip PROSE. A command, id, path or list of them is never passed here: it is a Whole (Brief.line, add_whole, add_list)."""
    text = re.sub(r"\s+", " ", _CONTROL.sub("", str(text or ""))).strip()
    return text if len(text) <= size else text[:size - 1] + "…"


def _when(value):
    return work._parse_when(value)


def _stamp(when):
    return when.strftime(work.STAMP) if when else "unknown"


def project_dir(workdir=None):
    raw = workdir or os.getenv("TP_WORKDIR") or os.getcwd()
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def covers(record, project, attached, worker=None):
    """Whether an active record applies: global; project:<repo> holding the project (`project` is one directory or a tuple of them: a
    worker's working directory and its repository's main checkout); workspace of an attached one. lane:, task: and role: records are
    for the workers they name: they match only with `worker` ({role, lane, tasks}: the worker packet, HB3b), never in Main's brief."""
    scope = record["scope"]
    if scope == "global":
        return True
    if scope == "workspace":
        return record["workspace"] in attached
    if scope.startswith("project:"):
        repo = Path(scope[len("project:"):])
        return any(p == repo or repo in p.parents for p in (project if isinstance(project, tuple) else (project,)) if p is not None)
    kind, _, name = scope.partition(":")
    if worker and name and kind in ("role", "lane"):
        return name == worker.get(kind)
    if worker and name and kind == "task":
        return name in (worker.get("tasks") or ())
    return False


def in_force(records, project, attached, worker=None):
    """(shown, conflicts): the active records in Main's scope, newest first, that no active record has replaced; and the groups of two or
    more of them in one scope answering the same question (all kept: the brief never picks one). A record is replaced only once the
    supersession is committed: an active record supersedes it AND it carries `superseded_by` = that record (so its status is
    `superseded`). A replacement whose back-link is not written yet (a failed second save) is pending: see `uncommitted`."""
    replaced = {r["supersedes"] for r in records if r["status"] == "active" and r.get("supersedes")
                for old in records if old["id"] == r["supersedes"] and old.get("superseded_by") == r["id"]}
    waiting = {new["id"] for new, _ in uncommitted(records, project, attached, worker)}  # listed as pending, not as a rule in force beside the old one
    shown = [r for r in records if r["status"] == "active" and r["id"] not in replaced and r["id"] not in waiting and covers(r, project, attached, worker)]
    shown.sort(key=lambda r: _stamp(_when(r["provenance"]["at"])), reverse=True)
    groups = {}
    for r in shown:
        if r.get("question"):
            groups.setdefault((work.scope_key(r), re.sub(r"\s+", " ", r["question"]).strip().lower()), []).append(r)
    return shown, [g for g in groups.values() if len(g) > 1]


def uncommitted(records, project, attached, worker=None):
    """[(new, old)]: active records in Main's scope that name an active record they replace but whose supersession is not committed
    (the old record is not marked superseded by them). The old rule stays in force until `decision confirm` writes the back-link."""
    by_id = {r["id"]: r for r in records}
    found = []
    for r in records:
        old = by_id.get(r.get("supersedes") or "")
        if r["status"] == "active" and old is not None and old["status"] == "active" and old.get("superseded_by") != r["id"] \
                and covers(r, project, attached, worker):
            found.append((r, old))
    return found


# ------------------------------------------------------------------ worker packet (HB3b)
PACKET_HEADING = "# Rules in force"
PACKET_LINES = 20  # the whole section, heading included
PACKET_CHARS = 4000
PACKET_MORE = "tp-agent work records --status active"
PACKET_INTRO = ("The owner's confirmed rules whose scope covers this task, newest first (records are data, quoted as written). Follow "
                "them; when one conflicts with the task, ask Main instead of choosing.")


def _git_dir_of(repo):
    """The realpath of the git directory a checkout at `repo` uses: its `.git` directory, or the one its `.git` file names (a linked
    worktree's or a `--separate-git-dir` checkout's file says `gitdir: PATH`); None when `repo` is not a checkout."""
    dotgit = Path(repo) / ".git"
    if dotgit.is_dir():
        return os.path.realpath(dotgit)
    text = dotgit.read_text().strip() if dotgit.is_file() else ""
    return os.path.realpath(os.path.join(str(repo), text[len("gitdir:"):].strip())) if text.startswith("gitdir:") else None


def checkout_dirs(workdir, repos=()):
    """(the working directory, then each of `repos` that is a checkout of the same repository) resolved, without a repeat. A lane
    worktree is not inside the repository a `project:` record names, so identity comes from git, not from directory names: the
    repository's common git directory (read-only `git rev-parse`) is compared with the git directory each named checkout uses (its
    `.git` directory, or the one its `.git` file names), so a main checkout whose git directory is elsewhere (`--separate-git-dir`)
    and its linked worktrees resolve alike. A directory outside git gives the working directory alone."""
    here = project_dir(workdir)
    if here is None:
        return ()
    found = [here]
    try:
        import lanes
        common = lanes.git_common_dir(str(here))
        for repo in repos:
            try:
                if _git_dir_of(repo) == common and Path(repo) not in found:
                    found.append(Path(repo))
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - not a repository (or git missing): the working directory alone
        pass
    return tuple(found)


def _packet_line(*parts):
    """One section line: a list item, so record text can never start a line (no heading, no RESULT_STATUS line, no `!`)."""
    brief = Brief(None)
    brief.line("- ", *parts)
    return brief.lines


def unreadable(why):
    return f"{PACKET_HEADING}\n\n" + "\n".join(_packet_line(
        "the owner's rules in force could not be read (", _clip(why, 200), "); this task starts without them: ask Main which rules apply")) + "\n"


def worker_rules(workdir, role, lane=None, tasks=(), attached=()):
    """The worker packet section `# Rules in force` (text ending in a newline), or "" when no rule applies. The records are the ones
    `in_force` keeps (active, not replaced, no pending replacement) whose scope covers the worker (`covers` with `worker`), written by
    the owner (a worker-written record is never shown, whatever its status says). Conflicts in one scope are one line each, above the
    rules, never resolved. At most PACKET_LINES lines and PACKET_CHARS characters: whole lines, newest first, then one line naming how
    many were left out and the command that lists them. Never raises: a store that cannot be read is one line saying so and why."""
    try:
        problem = store_problem()
        if problem is None and not work.store_root().exists():  # never written: said, calmly (a machine with no store is normal)
            return f"{PACKET_HEADING}\n\n" + "\n".join(_packet_line("no work store at ", Whole(str(work.store_root())),
                                                                   ", so no owner rules are recorded for this task")) + "\n"
        if problem:
            return unreadable(f"work store {work.store_root()} unusable: {problem}")
        skipped = []
        records = work.all_records(skipped)
        target = {"role": role or None, "lane": lane or None, "tasks": {t for t in tasks if t}}
        shown, conflicts = in_force(records, checkout_dirs(workdir, [Path(r["scope"][len("project:"):]) for r in records if r["scope"].startswith("project:")]), list(attached), target)
        shown = [r for r in shown if r["provenance"]["by"] == "owner"]
        ids = {r["id"] for r in shown}
        entries = []
        for group in conflicts:
            group = [r["id"] for r in group if r["id"] in ids]
            if len(group) > 1:
                entries.append((0, _packet_line("CONFLICT: ", *[Whole(i if not n else ", " + i) for n, i in enumerate(group)],
                                            " are active in one scope and answer the same question; the owner has not said which "
                                            "stands, so ask Main before relying on either")))
        for r in shown:
            # the rule text is never clipped (a clipped rule is a different rule); only the owner's quote is
            entries.append((1, [f"- [{r['kind']} {_scope_label(r['scope'])}] {_clip(r['text'], 10 ** 9)} — owner: \"" +
                                f"{_clip(r['provenance']['quote'], 120)}\" ({r['id']})"]))
        fixed = [PACKET_HEADING, "", PACKET_INTRO]
        if work.skipped_count(skipped):
            fixed += _packet_line(f"{work.skipped_count(skipped)} work file(s) could not be read, so rules in them are missing: ",
                                  Whole("tp-agent work list"))
        elif not entries:
            return ""
        return "\n".join(fixed + _fit(fixed, entries)) + "\n"
    except Exception as error:  # noqa: BLE001 - fail open: a dispatch never waits on the store
        return unreadable(f"{type(error).__name__}: {error}")


def rules_for_task(base, task_id, parent, workdir, role, lane=None):
    """`worker_rules` for the task `task_id` (state directory `base`) that continues `parent`: the worker is its working directory, role,
    lane, its own id and the tasks it continues (the parent it retries, reviews or investigates, and theirs in turn, in tasks/ or
    archive/), and the workspaces this session has attached. The one builder every start shares: bin/tp-agent launch (rules_for) and
    bin/tp-review (`python3 lib/work_brief.py reviewer-rules`). Never raises: what cannot be read is one line saying so."""
    from task_archive import find_archived_task
    tasks, ident = [task_id], parent
    try:
        while isinstance(ident, str) and ident not in tasks and len(tasks) < 20 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ident):
            tasks.append(ident)
            path = Path(base) / "tasks" / ident if (Path(base) / "tasks" / ident / "meta.json").is_file() else find_archived_task(base, ident)
            try:
                meta = json.loads((path / "meta.json").read_text()) if path else {}
            except (OSError, ValueError):
                meta = {}
            ident = meta.get("parent") if isinstance(meta, dict) else None
        attached = work_index.attached_ids(Path(base))
    except Exception as error:  # noqa: BLE001 - the rules section never stops a start; it says what it could not read
        return unreadable(f"the task's ancestry or attached workspaces: {type(error).__name__}: {error}")
    return worker_rules(workdir, role, lane, tasks, attached)


def _main(argv):
    """`work_brief.py reviewer-rules STATE_DIR AUTHOR_TASK_ID AUTHOR_WORKDIR [AUTHOR_LANE]`: the section for the reviewer of that task
    (role code-reviewer; it continues the reviewed task, so its ancestry is the author's and the author's), on stdout. The reviewer has
    no id yet, so its own id is not among the tasks; the reviewed task stands in as its parent."""
    if len(argv) not in (4, 5) or argv[0] != "reviewer-rules":
        print("usage: work_brief.py reviewer-rules STATE_DIR AUTHOR_TASK_ID AUTHOR_WORKDIR [AUTHOR_LANE]", file=sys.stderr)
        return 2
    _, base, author, workdir = argv[:4]
    sys.stdout.write(rules_for_task(base, "", author, workdir, "code-reviewer", argv[4] if len(argv) == 5 else None))
    return 0


def _fit(fixed, entries):
    """The entries ((records, lines): kept or dropped whole) that fit beside `fixed` in PACKET_LINES and PACKET_CHARS, in order; when not
    all fit, the kept ones and one closing line naming how many RECORDS are left out (a conflict line is not a record). An entry that
    alone does not fit is left out (counted); shorter ones after it still fit."""
    def size(lines):
        return sum(len(line) + 1 for line in lines)
    if len(fixed) + sum(len(e[1]) for e in entries) <= PACKET_LINES and size(fixed) + sum(size(e[1]) for e in entries) <= PACKET_CHARS:
        return [line for _, lines in entries for line in lines]
    kept, left = [], 0
    reserve = _packet_line(f"+{sum(r for r, _ in entries)} more left out (newest shown first); all of them: ", Whole(PACKET_MORE))
    for records, lines in entries:
        if len(fixed) + len(kept) + len(lines) + len(reserve) > PACKET_LINES or size(fixed) + size(kept) + size(lines) + size(reserve) > PACKET_CHARS:
            left += records
        else:
            kept += lines
    return kept + _packet_line(f"+{left} more left out (newest shown first); all of them: ", Whole(PACKET_MORE))


def matching(project, skipped):
    """The active workspaces whose `repos` hold the project, newest first (the index's candidate rule)."""
    if project is None:
        return []
    rows = [w for w in work_index._rows("workspace", work_index._Workspace, skipped) if w.active]
    found = [w for w in rows if any(work_index._inside(project, r) for r in w.repos)]
    return sorted(found, key=lambda w: w.updated, reverse=True)


def last_compaction(state_dir):
    """The time of the newest compaction event of this run (PreCompact or SessionStart(compact)), or None."""
    if state_dir is None:
        return None
    try:
        generations = work_checkpoint._generations(state_dir) or []
    except OSError:
        return None
    for generation in reversed(generations):
        event = work_checkpoint._event(state_dir, generation)
        if "compact" in event["reason"]:
            return _when(event["since"])
    return None


def attach_newest(state_dir, ws_id):
    """Attach `ws_id` to this run the way `work attach` does (session-work.json, recovery generation `attach`)."""
    import argparse
    import work_commands
    work_commands.run(argparse.Namespace(work_command="attach", id=ws_id), state_dir)  # takes the store lock itself


def store_problem():
    """Why the work store cannot be read, or None. A store that does not exist yet is not a problem (nothing was ever written); one that
    exists but is not a readable directory, or whose listing fails, is."""
    try:
        root = work.store_root()
        if not root.exists():
            return None
        if not root.is_dir():
            return "it is not a directory"
        for folder in (root, *(root / sub for sub in work.KINDS.values())):
            if folder.exists():
                if not folder.is_dir():
                    return f"{folder.name} is not a directory"
                with os.scandir(folder):
                    pass
    except (OSError, RuntimeError, ValueError) as error:
        return _clip(getattr(error, "strerror", None) or error, 120)
    return None


def _utc(timestamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _approved_review(state_dir):
    """The newest APPROVED review of this run: (review id, reviewed task, verdict time, evidence files it was given) or None. Read from the
    session's reviewer task records (`tasks/*/review-of`, `result.md`) by the one verdict reader (review_verdict); a review that is
    incomplete or asks for changes approves nothing. The evidence is what the review's packet carries under `# Evidence supplied by Main:`."""
    if state_dir is None:
        return None
    best = None
    for marker in sorted((Path(state_dir) / "tasks").glob("*/review-of")):
        task = marker.parent
        try:
            result = task / "result.md"
            if marker.is_symlink() or result.is_symlink() or not result.is_file():
                continue
            verdict, _, incomplete = review_verdict.assess(review_verdict.read_review(result))
            if verdict not in ("APPROVED", "APPROVED_WITH_NOTES") or incomplete is not None:
                continue
            when = result.stat().st_mtime
            reviewed = marker.read_text(encoding="utf-8", errors="replace").strip()
        except (OSError, ValueError):
            continue
        if best is None or when > best[0]:
            best = (when, task, reviewed)
    if best is None:
        return None
    when, task, reviewed = best
    given = []
    try:
        with open(task / "packet.md", encoding="utf-8", errors="replace") as packet:
            for line in packet:
                match = re.match(r"^# Evidence supplied by Main: (\S.*?)\s*$", line)
                if match and match.group(1) not in given:
                    given.append(match.group(1))
    except OSError:
        pass
    return task.name, reviewed, _utc(when), given


class Brief:
    """The brief's lines. Prose is clipped; a Whole (command, id, path, lane name) is not, and there is no call that clips one:
    `add` takes prose only; `line` composes prose with Wholes (the prose shrinks to leave them room); `add_whole` puts Wholes on their own
    lines; `add_list` packs whole items on as many lines as they need. Only a single Whole longer than LINE_CHARS makes a longer line."""

    def __init__(self, compaction):
        self.lines, self.compaction = [], compaction
        self.units = []  # [(first line index, keys)]: the pieces a section budget keeps or drops whole (see fit)

    def unit(self, *keys):
        """Start a piece of the section: the lines added from here to the next `unit` are kept or dropped together. `keys` name what
        it shows (record, task, lane, decision ids) so a cut can say how many are left out."""
        self.units.append((len(self.lines), set(keys)))

    def fit(self, budget, command):
        """The section in at most `budget` lines (at least 1): all of it when it fits; else its first pieces and one last line
        `- +N more: COMMAND`, N = the keys that no kept piece shows, COMMAND (whole) prints all of them."""
        if len(self.lines) <= budget:
            return list(self.lines)
        bounds = [start for start, _ in self.units] + [len(self.lines)]
        pieces = [(self.lines[:bounds[0]], set())] + [(self.lines[a:b], keys) for (a, keys), b in zip(self.units, bounds[1:])]
        kept, shown, every, full = [], set(), set(), False
        for lines, keys in pieces:
            every |= keys
            if not full and len(kept) + len(lines) <= budget - 1:
                kept += lines
                shown |= keys
            else:
                full = True  # a section keeps its FIRST lines: nothing after the first piece that does not fit
        more = Brief(None)
        more.line(f"- +{len(every - shown)} more: ", Whole(command))
        return kept + more.lines

    def stale(self, when):
        """STALE when the part's own time is before the newest compaction; a part with no time cannot be called fresh."""
        if self.compaction is None:
            return ""
        if when is None:
            return " STALE (no record time; cannot be called fresh)"
        return " STALE (recorded before the last compaction at " + _stamp(self.compaction) + ")" if when < self.compaction else ""

    def head(self, name, when, *parts, unknown=False):
        """`unknown`: the part exists but nothing says when it was written (shown as `time unknown`); otherwise no time = no record.
        `parts`: prose and Wholes, as for `line`."""
        label = _stamp(when) if when else ("time unknown" if unknown else "no record")
        stale = self.stale(when) if (when is not None or unknown) else ""
        self.line(f"{name} [{label}]{stale}:" + (" " if parts else ""), *parts)

    def add(self, text):
        """One line of prose (clipped). Never a command, id, path or list: those are Wholes."""
        assert not isinstance(text, Whole), "a Whole is added with line/add_whole, never clipped"
        self.lines.append(_clip(text))

    def line(self, *parts):
        """One line from prose (str, clipped to what the Wholes leave) and Wholes (kept as they are). When the Wholes leave the prose
        less than 20 characters a piece, the prose is one line and each Whole follows on its own indented line."""
        wholes = [p for p in parts if isinstance(p, Whole)]
        prose = [(i, re.sub(r"\s+", " ", _CONTROL.sub("", str(p)))) for i, p in enumerate(parts) if not isinstance(p, Whole)]
        budget = LINE_CHARS - sum(len(w) for w in wholes)
        if not prose:
            if len(wholes) <= 1:
                self.lines.append("".join(wholes))
            else:
                self.lines.extend(("" if i == 0 else "  ") + w for i, w in enumerate(wholes))
            return
        caps = {i: len(t) for i, t in prose}
        if budget >= 20 * len(prose):
            while sum(caps.values()) > budget:
                caps[max(caps, key=caps.get)] -= 1
            out = list(parts)
            for i, t in prose:
                out[i] = t if caps[i] >= len(t) else t[:max(caps[i] - 1, 0)] + "…"
            self.lines.append("".join(out).strip())
            return
        self.lines.append(_clip("".join(t for _, t in prose)))
        self.lines.extend("  " + w for w in wholes)

    def add_whole(self, prose, *whole):
        """Prose line, then each command or id list Main is told to use whole on its own line (a list is joined by the caller only when
        it is short and fixed; otherwise use add_list)."""
        self.lines.append(_clip(prose))
        self.lines.extend("  " + w for w in map(Whole, whole))

    def add_value(self, text):
        """One indented line that is a single command: whole."""
        self.lines.append("  " + Whole(text))

    def add_list(self, label, items, sep=", ", below=False, keys=None):
        """`label` (prose) then whole `items` packed onto as many lines as they need: an item is never split or dropped. The length
        check counts everything on the line (label, indentation, separators) before an item joins it; an item that does not fit
        starts the next line (indented), and one that alone is longer than LINE_CHARS is that line, whole. The separator goes only
        between items on one line; a line end separates the rest. `keys`: what each item shows, for the section's units (default the
        item itself): the label with the first line of items is one unit, each further line its own."""
        items = list(map(str, items))
        rows = [[_clip(label), []]] + ([["", []]] if below else [])  # [text, keys of the items on it]
        for item, key in zip(items, items if keys is None else keys):
            text, on = rows[-1]
            joined = (text + (sep if on else " ") + item) if text else "  " + item
            if text and len(joined) > LINE_CHARS:
                rows.append(["  " + item, [key]])
            else:
                rows[-1] = [joined, on + [key]]
        self.unit()
        before = False  # whether the unit so far already holds items
        for text, on in (r for r in rows if r[0]):
            if before:
                self.unit()
            self.units[-1][1].update(on)
            before = before or bool(on)
            self.lines.append(text)


def build(state_dir, workdir=None, attach=False, index=None):
    """(text, info). `index`: a callable giving the session index value (bin/tp-agent build_index), or None when there is no session;
    it is called after the attach, so it sees it. Every part fails on its own: a broken part says it is unavailable and why; the rest
    is still built."""
    project = project_dir(workdir)
    problem = store_problem()
    if problem:  # not "none recorded": the store is there and cannot be read, so nothing below could say what is recorded
        root = work.store_root()
        head, tail, sections = Brief(last_compaction(state_dir)), Brief(None), {"work": Brief(last_compaction(state_dir))}
        head.line("working brief unavailable: work store ", Whole(root), " unusable: ", problem)
        _work(sections["work"], index() if callable(index) else index)
        tail.add_whole("pointers (the work store must be fixed first):", "tp-agent work show ID | tp-agent work records | tp-agent list")
        return "\n".join(assemble(head, sections, tail)), {"attached": [], "auto_attached": None, "matching": [], "store_problem": problem}
    skipped, notes = [], []
    attached = list(dict.fromkeys(work_index.attached_ids(state_dir))) if state_dir else []
    try:
        found = matching(project, skipped)
    except Exception as error:  # noqa: BLE001 - one part
        found, notes = [], [("workspace match unavailable: ", _clip(error, 120))]
    auto = None
    if attach and not attached and found and state_dir is not None:
        try:
            attach_newest(state_dir, found[0].id)
            attached, auto = [found[0].id], found[0].id
        except Exception as error:  # noqa: BLE001 - attaching is an extra; the brief still says what it found
            notes.append(("auto-attach of ", Whole(found[0].id), " failed: ", _clip(error, 160)))
    brief = Brief(last_compaction(state_dir))
    if not attached:
        brief.line(Whole(NO_WORKSPACE.format(project=project or "PATH")))
    brief.add(f"working brief (tp-agent brief; from records only; built {work.now()}; last compaction "
              f"{_stamp(brief.compaction) if brief.compaction else 'none recorded'})")
    for note in notes:
        brief.line(*note)
    others = [w.id for w in found if w.id not in attached]
    if not attached and others:
        brief.add_list("matching workspaces (newest first; attach one with `tp-agent work attach ID`):", others, below=True)
    docs = {}
    for ws_id in attached:
        try:
            doc, health = work_checkpoint.workspace_health(ws_id)
        except Exception as error:  # noqa: BLE001
            doc, health = None, f"corrupt: {type(error).__name__}"
        if doc is None:
            brief.line("workspace ", Whole(ws_id), f": unavailable ({_clip(health, 80)}); its last checkpoint is printed by:")
            brief.add_value(f"tp-agent work recover {ws_id}")
            continue
        docs[ws_id] = doc
        how = "attached automatically: the newest active workspace for this project" if ws_id == auto else "attached"
        brief.line("workspace ", Whole(ws_id), f" \"{_clip(doc.meta.get('title', ws_id), 80)}\" ({how}; status {_clip(doc.meta.get('status'), 20)})")
        if ws_id == auto and others:
            brief.add_list("  also matching:", others)
        objective = (work.get_section(doc.body, "Objective") or "").strip()  # each part's own write time (`work set` stores it); never the file's
        brief.head("outcome", _when(doc.meta.get("objective_at")),
                   *([_clip(objective, 10 ** 6)] if objective else ["not recorded (", Whole(f"tp-agent work set {ws_id} --objective TEXT"), ")"]),
                   unknown=bool(objective))
        acceptance = (work.get_section(doc.body, "Acceptance") or "").strip()
        brief.head("acceptance", _when(doc.meta.get("acceptance_at")),
                   *([_clip(acceptance, 10 ** 6)] if acceptance else ["not recorded (", Whole(f"tp-agent work set {ws_id} --acceptance TEXT"), ")"]),
                   unknown=bool(acceptance))
    sections = {name: Brief(brief.compaction) for name in MORE}
    _rules(sections["rules"], project, attached, skipped)
    _work(sections["work"], index() if callable(index) else index, state_dir)
    _decisions(sections["decisions"], docs)
    tail = Brief(brief.compaction)
    tail.add("obligations (HB5): no obligation records exist yet")
    _main_launch(tail, state_dir)
    _evidence_part(tail, state_dir, attached)
    if work.skipped_count(skipped):
        tail.add(work.skipped_line(skipped))
    ws = attached[0] if attached else "ID"
    tail.add_whole("pointers:", f"tp-agent work show {ws} | tp-agent work records | tp-agent work decisions | tp-agent work recover | tp-agent list")
    return "\n".join(assemble(brief, sections, tail)), {"attached": attached, "auto_attached": auto, "matching": [w.id for w in found]}


def budgets(left, needs):
    """{section: lines}. The lines left under MAX_LINES after the fixed ones: each section first gets what it needs up to a third of
    them; the lines no section used then go to the others in order (rules, work, decisions). A section with lines gets at least 1 (its
    `+N more` line), set aside before the spare lines are shared."""
    left = max(left, 0)
    give = {name: max(min(need, left // 3), 1 if need else 0) for name, need in needs.items()}
    spare = left - sum(give.values())
    for name, need in needs.items():
        extra = max(min(need - give[name], spare), 0)
        give[name] += extra
        spare -= extra
    return give


def assemble(head, sections, tail):
    """The brief's lines: `head` and `tail` whole (header, workspace, outcome, acceptance, placeholders, evidence, checkpoint,
    skipped, pointers: never cut), each variable section in its budget (Brief.fit). No global cut: the total is at most MAX_LINES
    unless the fixed lines alone leave fewer than one line per section that has any."""
    given = budgets(MAX_LINES - len(head.lines) - len(tail.lines), {name: len(b.lines) for name, b in sections.items()})
    return head.lines + [line for name, b in sections.items() for line in b.fit(given[name], MORE[name])] + tail.lines


def _rules(brief, project, attached, skipped):
    try:
        records = work.all_records(skipped)
    except Exception as error:  # noqa: BLE001
        brief.add(f"rules in force: unavailable ({_clip(error, 160)})")
        return
    shown, conflicts = in_force(records, project, attached)
    newest = max((_when(r["provenance"]["at"]) for r in shown if _when(r["provenance"]["at"])), default=None)
    brief.head("rules in force (active, owner-confirmed; global, project, workspace)", newest, "" if shown else "none recorded")
    for group in conflicts:  # above the other rule lines: a budget cut keeps them first
        brief.add_list("CONFLICT: these are active in one scope and answer the same question; ask the owner which stands "
                        "(`work decide ... --supersedes ID --by owner --quote ...`):", [r["id"] for r in group], below=True)
    for r in shown:
        quote = r["provenance"]["quote"] or ""
        at = _when(r["provenance"]["at"])
        tail = Whole(f" ({r['id']}, {_stamp(at)}{', STALE' if brief.stale(at) else ''})")  # whole: only the prose before it is clipped
        brief.unit(r["id"])
        brief.line(f"- [{r['kind']} ", Whole(_scope_label(r['scope'])), "] ", _clip(r["text"], 120), " — owner: \"", _clip(quote, 60), "\"", tail)
    for new, old in uncommitted(records, project, attached):
        brief.unit(new["id"], old["id"])
        brief.line("pending: ", Whole(new["id"]), " replaces ", Whole(old["id"]), ", not yet committed (", Whole(old["id"]),
                   " stays in force); finish with:")
        brief.add_value(f"tp-agent work decision confirm {new['id']} --quote \"their exact words\"")
    waiting = [r["id"] for r in records if r["status"] == "provisional" and covers(r, project, attached)]
    if waiting:
        brief.add_list("provisional (not in force until the owner confirms; tp-agent work decision confirm ID --quote ...):", waiting, below=True)


def _scope_label(scope):
    """The scope as Main reads it: `project:<directory name>` (a name, so a Whole: never clipped), else the scope itself."""
    return "project:" + Path(scope[len("project:"):]).name if scope.startswith("project:") else scope


def _task_launch(state_dir, task_id):
    """A task's launch.json (HB2b) as one short phrase: contract version, role, route, unsupported capabilities; `no record` for a
    task started before launch records existed (or by a launcher that writes none); never the record's paths or hashes."""
    if state_dir is None or not task_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", str(task_id)):
        return None
    try:
        record = launch_record.read(Path(state_dir) / "tasks" / str(task_id) / launch_record.NAME)
    except launch_record.RecordError as error:
        return f"unusable: {error}"
    return launch_record.summary(record) if record else "no record"


def _main_launch(brief, state_dir):
    """Main's own launch record (bin/tp-main writes <state dir>/main-launch.json at each start and recovery)."""
    name = "launch record (HB2; tasks: [launch: ...] in active work; Main)"
    if state_dir is None:
        brief.add(f"{name}: no session state directory, so no launch record was read")
        return
    try:
        record = launch_record.read(Path(state_dir) / launch_record.MAIN_NAME)
    except launch_record.RecordError as error:
        brief.add(f"{name}: unavailable ({_clip(error, 160)})")
        return
    if record is None:
        brief.head(name, None, "none (Main was started before launch records, or not by tp-main)")
        return
    brief.head(name, _when(record.get("recorded")), f"{record.get('harness')} {record.get('start')}, model "
               f"{record.get('model') or 'harness default'}, " + launch_record.summary(record), unknown=True)


def _work(brief, index, state_dir=None):
    if index is None:
        brief.add("active work: no session state directory, so no task records were read")
        return
    if "error" in index:
        brief.add(f"active work: unavailable ({_clip(index['error'], 160)})")
        return
    rows = [*index.get("active", []), *index.get("paused", []), *index.get("unstatused", []), *index.get("pending", [])]
    newest = max((_when(r.get("updated")) for r in rows if _when(r.get("updated"))), default=None)
    brief.head("active work (session task records)", newest, "" if rows else "no active, paused or unreviewed tasks")
    for r in rows:
        extra = "".join(f" {k}: {r[k]}" for k in ("verdict", "result_status") if r.get(k))
        lane = f" lane {r['lane']}/{r['lane_step']}" if r.get("lane") else ""
        brief.unit(f"task {r.get('id')}")
        row = Whole(f"- {r.get('id')} {r.get('role')} {r.get('status')}{extra}{lane}")
        launch = _task_launch(state_dir, r.get("id"))
        # The launch note is prose after the row; a row that leaves it too little room is shown alone, as before.
        brief.line(row, f" [launch: {launch}]") if launch and LINE_CHARS - len(row) >= 20 else brief.line(row)
    lanes = sorted((index.get("lanes") or {}).items())
    if lanes:
        brief.add_list("lanes:", [f"{name} " + ", ".join(f"{k} {v}" for k, v in sorted(e.get("by_status", {}).items())) for name, e in lanes],
                       sep="; ", keys=[f"lane {name}" for name, _ in lanes])


def _entry_time(entry):
    """When an open decision was written: the time its `Record:` field stored at `decision add` (or the answer attempt's `At:`); None
    for an entry that carries neither (written before HB3a)."""
    fields = work._loads(entry.get("record") or "") or {}
    return _when(fields.get("written")) or _when(entry.get("at"))


def _decisions(brief, docs):
    """Open decisions of the attached workspaces: id, priority, blocking and the decision's own time (question text stays in the file,
    as in the index)."""
    rows, newest = [], None
    for ws_id, doc in docs.items():
        try:
            entries = [e for e in work.decisions_of(doc) if e["status"] == "open"]
        except Exception as error:  # noqa: BLE001
            brief.line("open decisions of ", Whole(ws_id), f": unavailable ({_clip(error, 120)})")
            continue
        rows += [(e, _entry_time(e)) for e in entries]
    if not docs:
        return
    rows.sort(key=lambda row: (not row[0]["blocking"], work.PRIORITY_RANK.get(row[0]["priority"], 9)))
    known = [when for _, when in rows if when]
    brief.head("open decisions", max(known, default=None), *(["none"] if not rows else [Whole("tp-agent work decisions"), " shows each question"]),
               unknown=bool(rows) and not known)
    for e, when in rows:
        brief.unit(e["id"])
        brief.line(Whole(f"- {e['id']} [{e['priority']}{' blocking' if e['blocking'] else ''}] "
                         f"{_stamp(when) if when else 'time unknown'}{brief.stale(when)}"))


def _evidence_part(brief, state_dir, attached):
    found = _approved_review(state_dir)
    if found:
        review, reviewed, when, given = found
        names = []
        for name in given:
            path = Path(state_dir) / "tasks" / reviewed / "main-evidence" / name
            names.append(path.relative_to(state_dir).as_posix() if path.is_file() else name)
        shown = [part for i, name in enumerate(names) for part in ((", ",) if i else ()) + (Whole(name),)]
        brief.head("last approved review", when, Whole(f"{review} of {reviewed}"), f", approved {_stamp(when)}; evidence it was given: ",
                   *(shown or ["none attached"]))
    else:
        brief.add("last approved review: none")
    if attached:
        try:
            latest = work_checkpoint.latest(attached[0])["valid"]
        except Exception:  # noqa: BLE001
            latest = None
        if latest:
            brief.head("last checkpoint", _when(latest[1].get("at")), Whole(f"{attached[0]} {latest[0].name}"), f" ({_clip(latest[1].get('trigger'), 40)})")
        else:
            brief.line("last checkpoint: none valid for ", Whole(attached[0]))


def text_or_unavailable(build_text):
    """The brief, or one line saying it is unavailable and why: a broken store never blocks a start or a compaction."""
    try:
        return build_text()
    except Exception as error:  # noqa: BLE001 - fail open
        return f"working brief unavailable: {type(error).__name__}: {_clip(error, 200)}"


def run(args, state_dir_fn, build_index, render_index, guard=None):
    """The `brief` command. `state_dir_fn` raises without a session; then the brief is built from the store alone."""
    try:
        state = state_dir_fn()
    except Exception:  # noqa: BLE001 - no session: the brief still reads the store
        state = None

    memo = []

    def index():  # once, after any attach, and shared by the brief and the compact index text
        if not memo:
            try:
                memo.append(None if state is None else build_index(state))
            except Exception as error:  # noqa: BLE001
                memo.append({"error": str(error)})
        return memo[0]

    text = text_or_unavailable(lambda: build(state, args.workdir, args.attach and not work.in_worker(), index)[0])
    if guard is not None:
        text = text_or_unavailable(lambda: guard(text))
    if args.source == "compact":  # register.tsx appends the index at startup only; after a compaction this is its one delivery
        try:
            value = index()
            if value is None or "error" in value:
                raise ValueError(value["error"] if value else "no session state directory")
            text += "\n\n" + render_index(value)
        except Exception as error:  # noqa: BLE001
            text += f"\n\ntask index unavailable: {_clip(error, 200)}"
    if args.hook_json:
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    return text


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
