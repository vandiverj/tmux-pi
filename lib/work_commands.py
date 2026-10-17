"""`tp-agent work ...` (Phase A): the commands over lib/work.py. Every handler returns a JSON-able value or, for
`decisions`, text; bin/tp-agent prints it. Mutations run under the store lock and read the file inside it."""
import json
from pathlib import Path
import re

import sys

import autonomy
import work
import work_checkpoint
import work_find
import work_index
import work_promote
from work import WorkError, append_line, get_section, set_section

TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def add_parsers(sub):
    w = sub.add_parser("work", help="durable work context: workspaces, notes and decisions in ~/.config/tmux-pi/work")
    ws = w.add_subparsers(dest="work_command", required=True)

    def ident(p, name="id", help="workspace id"):
        p.add_argument(name, help=help)

    p = ws.add_parser("new", help="create a workspace")
    p.add_argument("title")
    p.add_argument("--id")
    p.add_argument("--objective", default="")
    p.add_argument("--repo", action="append", default=[])
    p.add_argument("--ticket", action="append", default=[])
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--goal", action="append", default=[], help="a tp-goal id, kept as a reference only")
    p = ws.add_parser("show", help="print a workspace (or one section)")
    ident(p)
    p.add_argument("--section", help="objective|scope|decisions|open-decisions|blockers|next|links|progress|sessions")
    p = ws.add_parser("list", help="list workspaces (or notes with --kind note), newest first")
    p.add_argument("--kind", choices=list(work.KINDS), default="workspace")
    p.add_argument("--status")
    p.add_argument("--all", action="store_true", help="include archived")
    p = ws.add_parser("set", help="change workspace fields")
    ident(p)
    p.add_argument("--title")
    p.add_argument("--status", choices=work.WORKSPACE_STATUS)
    p.add_argument("--objective")
    p.add_argument("--scope")
    p.add_argument("--repo", action="append", default=[])
    p.add_argument("--ticket", action="append", default=[])
    p.add_argument("--tag", action="append", default=[])
    p = ws.add_parser("next", help="add a next action, or mark one done (--done N)")
    ident(p)
    p.add_argument("text", nargs="?")
    p.add_argument("--done", type=int, metavar="N")
    p = ws.add_parser("decide", help="record a decision made (a line in Decisions)")
    ident(p)
    p.add_argument("text")
    p.add_argument("--source", default="")
    p = ws.add_parser("block", help="record a blocker or question, or clear one (--clear N)")
    ident(p)
    p.add_argument("text", nargs="?")
    p.add_argument("--clear", type=int, metavar="N")
    p = ws.add_parser("link", help="link a note, artifact, task or goal to a workspace")
    ident(p)
    p.add_argument("--note", action="append", default=[])
    p.add_argument("--artifact", action="append", default=[])
    p.add_argument("--task", action="append", default=[])
    p.add_argument("--goal", action="append", default=[])
    p = ws.add_parser("archive", help="move a workspace (or note) to the archive; nothing is deleted")
    ident(p, help="workspace or note id")
    p.add_argument("--kind", choices=list(work.KINDS))
    p = ws.add_parser("supersede", help="replace a note with a new one; the old note is archived as superseded")
    ident(p, help="the note being replaced")
    p.add_argument("--title", required=True)
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--new-id")
    p = ws.add_parser("attach", help="attach a workspace to this session")
    ident(p)
    n = ws.add_parser("note", help="notes").add_subparsers(dest="note_command", required=True)
    p = n.add_parser("add", help="create a note")
    p.add_argument("title")
    p.add_argument("--id")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--workspace", action="append", default=[])
    p.add_argument("--ticket", action="append", default=[])
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--source", action="append", default=[])
    p.add_argument("--important", action="store_true")
    p = n.add_parser("show", help="print a note")
    p.add_argument("id")
    d = ws.add_parser("decision", help="decision entries").add_subparsers(dest="decision_command", required=True)
    p = d.add_parser("add", help="add an open decision to a workspace")
    ident(p, "workspace", "workspace id")
    p.add_argument("--question", required=True)
    p.add_argument("--background", default="")
    p.add_argument("--options", default="")
    p.add_argument("--recommended", default="")
    p.add_argument("--affects", action="append", default=[], help="workspace | task:<id> | lane:<name> | next:<n> (repeatable)")
    p.add_argument("--priority", choices=work.PRIORITIES, default="normal")
    p.add_argument("--blocking", action="store_true")
    p = d.add_parser("answer", help="record the resolution and print what can resume")
    p.add_argument("id")
    p.add_argument("--text", required=True)
    p.add_argument("--by", choices=["user", "agent"], default="user")
    p.add_argument("--quote", default="")
    p = d.add_parser("withdraw", help="withdraw a decision; the entry stays")
    p.add_argument("id")
    p.add_argument("--reason", default="")
    p = ws.add_parser("promote", help="write a DRAFT *.context.md from a note or workspace into a working tree (no git is run)")
    p.add_argument("id", help="note or workspace id")
    p.add_argument("--as", dest="kind_as", choices=["context", "task"], default="context")
    p.add_argument("--ticket", help="a ticket id; the file goes under tasks/<ticket>/")
    p.add_argument("--confidence", choices=list(work_promote.CONFIDENCE), default=None,
                   help="default low for a new file; with --update, omitted leaves the file's reviewed confidence")
    p.add_argument("--target", help="the working tree to write into (default: config)")
    p.add_argument("--update", action="store_true", help="replace an existing draft (the diff is shown)")
    p = d.add_parser("list", help="same as `work decisions`")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p = ws.add_parser("decisions", help="every open decision across all workspaces, blocking first")
    p.add_argument("--all", action="store_true", help="include answered and withdrawn")
    p.add_argument("--json", action="store_true")
    p = ws.add_parser("checkpoint", help="write a validated checkpoint of a workspace's judgement state (default: the attached ones)")
    p.add_argument("id", nargs="?", help="workspace id (default: every workspace attached to this session)")
    p.add_argument("--trigger", choices=["manual", "precompact", "session-end"], default="manual")
    p.add_argument("--auto", action="store_true", help="for hooks: a snapshot only (no --summary), and say nothing is attached instead of failing; a failed checkpoint still exits non-zero, which the hook and tp-exit ignore")
    p.add_argument("--summary", help="your own short summary of where the work stands, stored with the checkpoint")
    p.add_argument("--summary-file", help="read that summary from a file")
    p = ws.add_parser("recover", help="print what is known about a workspace after a compaction or restart (reads only)")
    p.add_argument("id", nargs="?", help="workspace id (default: every workspace attached to this session)")
    p = ws.add_parser("recovered", help="clear this session's recovery-pending marker after reconciling (the only way it is cleared)")
    p.add_argument("id", nargs="?", help="one of this run's workspaces; the marker is run-wide, so every attached workspace is reconciled whatever is named")
    p.add_argument("--evidence", required=True, help="what you checked: the live records, the open decisions, any 'done' claim you re-verified")
    p.add_argument("--accept", default="", help="comma-separated task ids that differ from the checkpoint and that you have checked (T1,T2)")
    p.add_argument("--no-checkpoint", action="store_true", help="there is no checkpoint of this run to compare with: the live records and the workspace are what you checked")
    p = ws.add_parser("compacted", help="mark this session's recovery as pending by adding a recovery generation (for the compaction hooks)")
    p.add_argument("--reason", default="compaction")


def _extend(meta, key, values):
    values = [v for v in values if v]
    if values:
        meta[key] = list(dict.fromkeys([*meta.get(key, []), *values]))


def _lines(section):
    return [l for l in (section or "").split("\n") if l.strip()]


def _numbered(section):
    """Checklist/bullet lines of a section, in order."""
    return [l for l in _lines(section) if l.lstrip().startswith(("- ", "* "))]


def _text(value, path):
    if path:
        return Path(path).read_text(encoding="utf-8")
    return value or ""


def _summary(doc):
    d = doc.meta
    return {"id": doc.id, "title": d.get("title"), "status": d.get("status"), "updated": work.updated_iso(doc),
            "tickets": d.get("tickets", []), "repos": d.get("repos", []), "path": work_find.store_path(doc.path)}


SECTION_NAMES = {"objective": "Objective", "scope": "Scope", "decisions": "Decisions", "open-decisions": "Open decisions",
                 "blockers": "Blockers / questions", "next": "Next actions", "links": "Links", "progress": "Progress",
                 "sessions": "Sessions"}


def _checklist(doc, what, number, mark):
    lines = (get_section(doc.body, what) or "").split("\n")
    seen = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("- ", "* ")):
            seen += 1
            if seen == number:
                lines[i] = mark(line)
                doc.body = set_section(doc.body, what, "\n".join(lines))
                return
    raise WorkError(f"{what} has no item {number}")


def resume_targets(entry, task_status=None):
    """What an answered decision unblocks: each Affects target, plus Next actions lines that name the id."""
    out = []
    for target in entry["affects"]:
        row = {"target": target}
        if target.startswith("task:") and task_status:
            row["status"] = task_status(target[5:])
        out.append(row)
    return out


def leaf(args):
    """The subcommand as one word or two ('next', 'decision add', 'note show'), the key of the sets below."""
    cmd = args.work_command
    sub = {"note": "note_command", "decision": "decision_command"}.get(cmd)
    return f"{cmd} {getattr(args, sub)}" if sub else cmd


# Every `work` subcommand that changes nothing in the work store. Anything not listed here is a mutation: `run` calls
# `post_mutation` after it, so a new mutating subcommand gets the stale-checkpoint trigger without anyone remembering to add it.
READ_ONLY = frozenset({"show", "list", "decisions", "note show", "decision list"})
NOT_MUTATIONS = READ_ONLY | {"recover", "compacted", "recovered", "checkpoint"}


def mutating(args):
    return leaf(args) not in NOT_MUTATIONS


def workspace_written(args):
    """The workspace a mutating command changed, else None (a note, a new workspace or an attach has no workspace to checkpoint)."""
    cmd = args.work_command
    if cmd in {"set", "next", "decide", "block", "link"}:
        return args.id
    if cmd == "archive":
        return None if args.kind == "note" else args.id
    if cmd == "decision":
        if args.decision_command == "add":
            return args.workspace
        match = work.DECISION_RE.match(getattr(args, "id", "") or "")
        return match.group(1) if match else None
    return None


def _live(tasks):
    """The live task records (id, status, result_path) as a list, or None when there is no session to read them from."""
    try:
        return list(tasks()) if tasks else None
    except (ValueError, OSError, KeyError):
        return None


def _run_id(session_dir):
    return Path(session_dir).name if session_dir else ""


def run(args, session_dir=None, task_status=None, tasks=None):
    """`tasks`: a callable returning the live task records of this session (for the checkpoint diff), or None."""
    cmd = args.work_command
    if cmd == "compacted":  # touches the session's state directory only, never the work store
        return work_checkpoint.compacted(session_dir, args.reason)
    if cmd == "recover":
        return _recover(args, session_dir, tasks)
    if cmd == "recovered":  # touches the session's state directory only (it reads the work store and the task records)
        return _recovered(args, session_dir, tasks)
    if leaf(args) in READ_ONLY:
        work.ensure_store()
        return _read_command(args)
    with work.locked():
        if cmd == "checkpoint":
            return _checkpoint(args, session_dir, tasks)
        value = _write_command(args, session_dir, task_status)
        post_mutation(args, session_dir, tasks)
        return value


def _targets(args, session_dir):
    if args.id:
        return [args.id]
    return list(dict.fromkeys(work_index.attached_ids(session_dir))) if session_dir else []


def _checkpoint(args, session_dir, tasks):
    """Write a checkpoint of each named (or attached) workspace and print `checkpoint written: <path>` for each one that was read back
    and validated (and only for those). Any failure, `--auto` included, fails the command after the others were tried; the hooks that
    call it ignore the exit code themselves, so a compaction or an exit is never blocked."""
    if args.auto and (args.summary is not None or args.summary_file is not None):
        raise WorkError("usage: --auto records a snapshot only and takes no --summary or --summary-file; "
                        "run `work checkpoint --summary-file FILE` without --auto to store your own summary")
    ids = _targets(args, session_dir)
    if not ids:
        if args.auto:
            return "no workspace is attached to this session; no checkpoint written"
        raise WorkError("no workspace given and none attached to this session; pass a workspace id")
    live, failures = _live(tasks), []
    summary = _text(args.summary, args.summary_file).strip() or None
    for ws_id in ids:
        try:
            doc, health = work_checkpoint.workspace_health(ws_id)
            if doc is None:  # nothing is written from a file that cannot be read: it would push a good checkpoint out
                raise WorkError(f"workspace {health}; no checkpoint written (the earlier ones are kept)")
            path, value = work_checkpoint.write(doc, _run_id(session_dir), args.trigger, live or [], summary)
        except (WorkError, OSError) as error:
            failures.append(f"{ws_id}: {error}")
            continue
        print(f"checkpoint written: {work_find.store_path(path)}")
        if value["truncated"]:
            print(f"  {work_checkpoint.TRUNCATED_LABEL} (left out: {json.dumps(value['truncated']['sections'], sort_keys=True)})")
    if failures:
        raise WorkError("checkpoint failed: " + "; ".join(failures))


def post_mutation(args, session_dir, tasks):
    """Runs after EVERY work mutation (see `NOT_MUTATIONS`), under the store lock, with the run's state directory as bin/tp-agent
    resolved it (`state_dir()`: an exported TP_STATE_DIR or the one the session records). After a change to a workspace this run is
    attached to: a checkpoint when the newest valid one is missing or old. Never fails the command (the change is already saved); a
    problem is a warning."""
    ws_id = workspace_written(args)
    if not ws_id or session_dir is None or ws_id not in work_index.attached_ids(session_dir):
        return
    try:
        doc = work_checkpoint.workspace_health(ws_id)[0]
        if doc is not None and work_checkpoint.due(ws_id):
            work_checkpoint.write(doc, _run_id(session_dir), "work-write", _live(tasks) or [])
    except (WorkError, OSError) as error:
        print(f"warning: checkpoint not written after the change: {error}", file=sys.stderr)


def _recover(args, session_dir, tasks):
    ids = _targets(args, session_dir)
    if not ids:
        raise WorkError("no workspace given and none attached to this session; pass a workspace id")
    live = _live(tasks)
    return "\n\n".join(work_checkpoint.format_recover(work_checkpoint.recover(i, live, _run_id(session_dir))) for i in ids)


def _recovery_targets(args, session_dir):
    """The workspaces whose reconciliation clears this run's marker: every one attached to the run, plus the one its autonomy record
    names. The marker is run-wide, so a workspace id on the command line may select among them but never replaces them."""
    required = list(dict.fromkeys(work_index.attached_ids(session_dir)))
    try:
        named = (autonomy.record(session_dir) or {}).get("workspace")
    except ValueError:
        named = None
    if isinstance(named, str) and named and named not in required:
        required.append(named)
    if args.id and args.id not in required:
        raise WorkError(f"{args.id} is not a workspace of this run (attached: {', '.join(required) or 'none'}); recovery clears the whole "
                        "run's marker, so it can only be checked against this run's own workspaces")
    return required


def _recovered(args, session_dir, tasks):
    """Clear the recovery marker, but only after the same reconciliation `work recover` prints: with a checkpoint of this run, the live
    task diff against it must be empty or every task that differs named in --accept; with none (or none from this run), the caller
    says so with --no-checkpoint. --evidence is required and recorded. Nothing is cleared when any workspace refuses."""
    if session_dir is None:
        raise WorkError("no tmux-pi state directory; start a workspace with tp, or set TP_STATE_DIR")
    evidence = (args.evidence or "").strip()
    if not evidence:
        raise WorkError("--evidence must say what you checked (the live records, the open decisions, any \"done\" claim you re-verified)")
    accepted = {part for part in re.split(r"[,\s]+", args.accept or "") if part}
    run, notes, ids = _run_id(session_dir), [], []

    def check():  # the clear covers only the generations that existed before this ran (work_checkpoint.recovered)
        # Select the workspaces only now, after that snapshot: an attach that published its workspace earlier is checked here, and
        # one that publishes later also arms a newer generation, which this clear does not cover.
        ids[:] = _recovery_targets(args, session_dir)
        live, unreadable = None, None
        try:
            live = list(tasks()) if tasks else None
        except (ValueError, OSError, KeyError) as error:
            unreadable = error
        if live is None and unreadable is None:
            unreadable = "no task records were given"
        problems = _recovery_problems(args, ids, run, live, unreadable, accepted, notes)
        if problems:
            raise WorkError("recovery is not complete, nothing was cleared: " + " | ".join(problems))

    value = work_checkpoint.recovered(session_dir, evidence, accepted, check)
    return {"recovered": True, "changed": value["changed"], "workspaces": ids, "accepted": sorted(accepted),
            **({"notes": notes} if notes else {})}


def _recovery_problems(args, ids, run, live, unreadable, accepted, notes):
    """Why recovery is not complete (sentences; empty when it is), comparing each workspace's live tasks with this run's newest
    valid checkpoint. Appends checkpoint labels worth printing to `notes`."""
    problems = []
    if unreadable is not None:
        # Reconciliation is against the live records, with a checkpoint or (--no-checkpoint) without one: unread, nothing was checked.
        return [f"the live task records cannot be read ({unreadable}), so recovery cannot be checked against them, with or without "
                "a checkpoint; fix that, then run `work recover` and this again"]
    if not ids and not args.no_checkpoint:
        problems.append("no workspace is attached to this session, so there is nothing to compare; check `tp-agent list` and the live "
                        "records, then pass --no-checkpoint")
    for ws_id in ids:
        info = work_checkpoint.recover(ws_id, live, run)
        if info["state"] is None and not args.no_checkpoint:
            problems.append(f"{ws_id}: its workspace file is unavailable ({info['file']}) and there is no valid checkpoint; "
                            "pass --no-checkpoint to say you reconciled from the live records alone")
        elif not info["tasks"]["known"]:
            if not args.no_checkpoint:
                problems.append(f"{ws_id}: no valid checkpoint of this run to compare the live tasks with; run `work recover {ws_id}`, "
                                "check `tp-agent list`, then pass --no-checkpoint")
        else:
            unaccepted = sorted({line.split()[0] for line in info["tasks"]["changes"]} - accepted)
            if unaccepted:
                problems.append(f"{ws_id}: tasks changed since its checkpoint and not accepted: {', '.join(unaccepted)}; "
                                f"check each (`tp-agent inspect ID`), then pass --accept {','.join(unaccepted)}")
        if info["checkpoint"] and info["checkpoint"]["truncated"]:
            notes.append(f"{ws_id}: the checkpoint is {work_checkpoint.TRUNCATED_LABEL}")
    return problems


def _read_command(args):
    cmd = args.work_command
    if cmd == "list":
        skipped, rows = [], []
        for archived in ((False, True) if args.all else (False,)):
            for d in work.iter_docs(args.kind, archived=archived, skipped=skipped):
                try:
                    if not args.status or d.meta.get("status") == args.status:
                        rows.append((work.updated_at(d), _summary(d)))
                except Exception:  # noqa: BLE001 - per-document isolation: one malformed file never hides the others
                    work.note_skipped(skipped, d.path)
        _report_skipped(skipped)
        return [row for _, row in sorted(rows, key=lambda r: r[0], reverse=True)]
    if cmd == "show":
        doc = work.load("workspace", args.id)
        value = _summary(doc)
        if args.section:
            title = SECTION_NAMES.get(args.section)
            if not title:
                raise WorkError("unknown section; one of " + ", ".join(SECTION_NAMES))
            value["section"] = title
            value["text"] = get_section(doc.body, title) or ""
        else:
            value["meta"] = doc.meta
            value["text"] = doc.body
        return value
    if cmd == "note":
        doc = work.load("note", args.id)
        return {**_summary(doc), "meta": doc.meta, "text": doc.body}
    skipped = []
    rows = work.open_decisions(args.all, skipped=skipped)
    _report_skipped(skipped)
    return rows if getattr(args, "json", False) else decisions_text(rows)


def _report_skipped(skipped):
    """Names the work files that could not be read on stderr (stdout, and so --json, stays what it was)."""
    names = work.skipped_files(skipped)
    if names:
        print(f"warning: {len(names)} unreadable work files skipped: {', '.join(names)}", file=sys.stderr)


def decisions_text(rows):
    if not rows:
        return "no open decisions"
    out = []
    for r in rows:
        flags = " blocking" if r["blocking"] else ""
        affects = " ".join(r["affects"]) or "workspace"
        status = "" if r["status"] == "open" else f" ({r['status']})"
        out.append(f"{r['id']} [{r['priority']}{flags}]{status} {r['question'].splitlines()[0] if r['question'] else ''} — affects: {affects}")
    return "\n".join(out)


def _write_command(args, session_dir, task_status):
    cmd = args.work_command
    if cmd == "new":
        tickets = list(dict.fromkeys([*args.ticket, *TICKET.findall(args.title)]))
        meta = {"repos": [str(Path(r).expanduser().resolve()) for r in args.repo], "tickets": tickets, "tags": args.tag, "goals": args.goal}
        body = ""
        for title, text in (("Objective", args.objective),):
            if text:
                body = set_section(body, title, text.strip())
        doc = work.create("workspace", args.title, args.id, meta, body)
        return _summary(doc)
    if cmd == "note" and args.note_command == "add":
        return _note_add(args)
    if cmd == "promote":
        return work_promote.promote(args.id, args.kind_as, args.ticket, args.confidence, args.target, args.update)
    if cmd == "supersede":
        return _supersede(args)
    if cmd == "archive":
        return _archive(args)
    if cmd == "attach":
        doc = work.load("workspace", args.id, include_archived=False)
        if session_dir is None:
            raise WorkError("no tmux-pi state directory; start a workspace with tp, or set TP_STATE_DIR")
        path = Path(session_dir) / "session-work.json"
        try:
            current = json.loads(path.read_text()).get("attached", [])
        except (OSError, ValueError, AttributeError):
            current = []
        attached = list(dict.fromkeys([*current, doc.id]))
        work.atomic_write(path, json.dumps({"attached": attached}, indent=2) + "\n")
        # A run that attaches a workspace is a restart that needs reconciling: recovery is pending (adds a recovery generation).
        work_checkpoint.compacted(session_dir, "attach")
        return {"attached": attached}
    if cmd == "decision":
        return _decision(args, task_status)
    doc = work.load("workspace", args.id, include_archived=False)
    stamp = work.now()[:10]
    if cmd == "set":
        if args.title:
            doc.meta["title"] = args.title
        if args.status:
            if args.status == "archived":
                raise WorkError("use `work archive` to archive")
            doc.meta["status"] = args.status
        for key, values in (("repos", [str(Path(r).expanduser().resolve()) for r in args.repo]), ("tickets", args.ticket), ("tags", args.tag)):
            _extend(doc.meta, key, values)
        if args.objective is not None:
            doc.body = set_section(doc.body, "Objective", args.objective.strip())
        if args.scope is not None:
            doc.body = set_section(doc.body, "Scope", args.scope.strip())
    elif cmd == "next":
        if args.done:
            _checklist(doc, "Next actions", args.done, lambda l: l.replace("- [ ]", "- [x]", 1))
        elif args.text:
            doc.body = append_line(doc.body, "Next actions", f"- [ ] {args.text.strip()}")
        else:
            raise WorkError("give the action text or --done N")
    elif cmd == "decide":
        doc.body = append_line(doc.body, "Decisions", f"- {stamp} {args.text.strip()}" + (f" Source: {args.source}" if args.source else ""))
    elif cmd == "block":
        if args.clear:
            _checklist(doc, "Blockers / questions", args.clear, lambda l: l.replace("- [ ]", "- [x]", 1))
        elif args.text:
            doc.body = append_line(doc.body, "Blockers / questions", f"- [ ] {args.text.strip()}")
        else:
            raise WorkError("give the blocker text or --clear N")
    elif cmd == "link":
        parts = []
        for label, values in (("notes", args.note), ("artifacts", args.artifact), ("tasks", args.task)):
            if values:
                parts.append(f"{label}: " + ", ".join(values))
        if args.goal:
            _extend(doc.meta, "goals", args.goal)
            parts.append("goals: " + ", ".join(args.goal))
        if not parts:
            raise WorkError("nothing to link")
        doc.body = append_line(doc.body, "Links", "- " + " · ".join(parts))
        for note_id in args.note:
            note = work.load("note", note_id, include_archived=False)
            if doc.id not in note.meta.get("workspaces", []):
                _extend(note.meta, "workspaces", [doc.id])
                note.save()
    else:
        raise WorkError("unknown work command")
    doc.save()
    return _summary(doc)


def _note_add(args):
    tickets = list(dict.fromkeys([*args.ticket, *TICKET.findall(args.title)]))
    for ws in args.workspace:
        work.load("workspace", ws, include_archived=False)
    unreadable = []
    similar = work_find.near_duplicates(args.title, tickets, args.tag, unreadable)
    for dup in similar:  # a warning, not a refusal: the note is still created. Screened values only, then the output boundary.
        what = (f"its id or path is withheld; locate it with `tp-agent find --kind note`" if dup["withheld"] else
                f"read it first, or replace it with `work supersede {dup['id']} --title \"<new title>\" --body-file <file>`")
        print(work_find.guard(f"warning: similar note {dup['id']} ({dup['why']}): {dup['title']} — {dup['path']}; {what}"),
              file=sys.stderr)
    skipped = work.skipped_count(unreadable)
    if skipped:  # the note is still created: say only that the check was incomplete
        print(work_find.guard(f"warning: duplicate check incomplete: {skipped} unreadable notes skipped"), file=sys.stderr)
    meta = {"workspaces": args.workspace, "tickets": tickets, "tags": args.tag, "sources": args.source, "important": args.important}
    doc = work.create("note", args.title, args.id, meta, _text(args.body, args.body_file))
    value = _summary(doc)
    if similar:
        value["near_duplicates"] = [d["id"] for d in similar]
    if skipped:
        value["near_duplicates_incomplete"] = skipped
    return value


def _supersede(args):
    """Create the replacement first (exclusive create, carrying `supersedes: <old>`), then archive the old note. A retry finds
    the replacement by that field; nothing is reserved on the old note beforehand, so no other note can lose or take an id."""
    old = work.load("note", args.id)
    if old.in_archive:
        raise WorkError(f"note {old.id} is already archived")
    if old.meta.get("status") == "superseded" and old.meta.get("superseded_by"):  # an interrupted supersede: finish the move
        new = work.load("note", old.meta["superseded_by"])
    else:
        new = next((d for state in (False, True) for d in work.iter_docs("note", archived=state)
                    if d.meta.get("supersedes") == old.id), None)
        if new is None:
            if old.archived:
                raise WorkError(f"note {old.id} is archived and read-only")
            inherited = {k: old.meta[k] for k in ("workspaces", "tickets", "tags", "important") if old.meta.get(k)}
            new = work.create("note", args.title, args.new_id, {**inherited, "supersedes": old.id}, _text(args.body, args.body_file))
    work.move_to_archive(old, "superseded", {"superseded_by": new.id})
    return {"superseded": old.id, "by": new.id, "archived_path": work_find.store_path(old.path),
            "path": work_find.store_path(new.path)}


def _archive(args):
    found = []
    for kind in ([args.kind] if args.kind else list(work.KINDS)):
        try:
            doc = work.load(kind, args.id)
        except WorkError:
            continue
        found.append(doc)
    if len(found) > 1:
        raise WorkError(f"{args.id} is both a workspace and a note; pass --kind")
    if not found:
        raise WorkError(f"unknown workspace or note: {args.id}")
    doc = found[0]
    work.move_to_archive(doc, "archived")
    return {**_summary(doc), "kind": doc.kind}


def _decision(args, task_status):
    sub = args.decision_command
    if sub == "add":
        doc = work.load("workspace", args.workspace, include_archived=False)
        entries = work.decisions_of(doc)
        entry = {"id": f"{doc.id}/D{work.next_decision_number(entries)}", "priority": args.priority, "status": "open",
                 "blocking": args.blocking, "question": args.question, "background": args.background, "options": args.options,
                 "recommended": args.recommended, "affects": args.affects or ["workspace"], "resolution": "", "by": "", "at": "",
                 "quote": "", "withdrawn": ""}
        entry["_dirty"] = True
        entries.append(entry)
        work.put_decisions(doc, entries)
        doc.save()
        return entry
    doc, entries, entry = work.find_decision(args.id)
    if entry["status"] != "open":
        raise WorkError(f"{entry['id']} is already {entry['status']}")
    entry["_dirty"] = True
    if sub == "answer":
        cleared = not (entry["blocking"] and args.by != "user")
        # An agent's answer is stored but never clears a blocking entry: it stays open, in the queue, until the user answers or it is withdrawn.
        entry.update(status="answered" if cleared else "open", resolution=args.text, by=args.by, at=work.now(), quote=args.quote)
        work.put_decisions(doc, entries)
        mentions = [l.strip() for l in _lines(get_section(doc.body, "Next actions")) if entry["id"] in l]
        doc.save()
        value = {"id": entry["id"], "status": entry["status"], "by": args.by, "clears_blocking": cleared,
                 "can_resume": resume_targets(entry, task_status) if cleared else [],
                 "next_actions_mentioning": mentions if cleared else []}
        if not cleared:
            value["note"] = "recorded by agent: the blocking decision stays open until the user answers it (or it is withdrawn)"
        return value
    entry.update(status="withdrawn", withdrawn=args.reason or "withdrawn", at=work.now())
    work.put_decisions(doc, entries)
    doc.save()
    return {"id": entry["id"], "status": "withdrawn"}
