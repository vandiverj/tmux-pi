"""`tp-agent work ...` (Phase A): the commands over lib/work.py. Every handler returns a JSON-able value or, for
`decisions`, text; bin/tp-agent prints it. Mutations run under the store lock and read the file inside it."""
import json
from pathlib import Path
import re

import work
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
    p = ws.add_parser("list", help="list workspaces")
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
    p = d.add_parser("list", help="same as `work decisions`")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p = ws.add_parser("decisions", help="every open decision across all workspaces, blocking first")
    p.add_argument("--all", action="store_true", help="include answered and withdrawn")
    p.add_argument("--json", action="store_true")


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
    return {"id": doc.id, "title": d.get("title"), "status": d.get("status"), "updated": d.get("updated"),
            "tickets": d.get("tickets", []), "repos": d.get("repos", []), "path": str(doc.path)}


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


def run(args, session_dir=None, task_status=None):
    cmd = args.work_command
    if cmd in {"show", "list", "decisions"} or (cmd == "note" and args.note_command == "show") or \
            (cmd == "decision" and args.decision_command == "list"):
        work.ensure_store()
        return _read_command(args)
    with work.locked():
        return _write_command(args, session_dir, task_status)


def _read_command(args):
    cmd = args.work_command
    if cmd == "list":
        rows = [_summary(d) for d in work.iter_docs("workspace") if not args.status or d.meta.get("status") == args.status]
        if args.all:
            rows += [_summary(d) for d in work.iter_docs("workspace", archived=True) if not args.status or d.meta.get("status") == args.status]
        return rows
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
    if getattr(args, "json", False):
        return work.open_decisions(args.all)
    return decisions_text(work.open_decisions(args.all))


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
    similar = [d.id for d in work.iter_docs("note") if d.id == work.slugify(args.title)
               or (tickets and set(tickets) & set(d.meta.get("tickets", [])) and set(args.tag) & set(d.meta.get("tags", [])))]
    meta = {"workspaces": args.workspace, "tickets": tickets, "tags": args.tag, "sources": args.source, "important": args.important}
    doc = work.create("note", args.title, args.id, meta, _text(args.body, args.body_file))
    value = _summary(doc)
    if similar:
        value["near_duplicates"] = similar
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
    return {"superseded": old.id, "by": new.id, "archived_path": str(old.path), "path": str(new.path)}


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
