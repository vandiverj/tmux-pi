"""The `work` block of the session index (Phase B): what a new session needs to find durable work without being told.

`build(state_dir)` returns the block or None. None means no workspace, no note and no unreadable work file exists, so the index is byte-identical to
one made before this feature. Reading never creates the store. Per open decision the block carries only `id`, `workspace`,
`priority` and `blocking` (amendment R3-F1): question text stays in the workspace file and is shown by `tp-agent work decisions`.
A document that cannot be processed is skipped on its own and counted in `skipped.count`; the rest still appears. The block never names a skipped
file (a file name can hold anything, and this block reaches session-index.json and Pi): `work list`, `work decisions` and `find` name them.
`checkpoint` (Phase C) is the status of the first attached workspace's newest valid checkpoint (ids, times and flags, and the two
sentences the injection shows), None when nothing is attached; `recovery` appears only while a compaction marker is pending and a
workspace is attached. Both are read-only.
`decision_summary` is the bounded text form the Claude injection and Pi's task_index share (R3-F3).
"""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re

import work
import work_checkpoint
from work import skipped_count

CLIP = 120
SESSION_REFS = 3
COMMAND = "tp-agent work decisions"


def _clip(text, size=CLIP):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= size else text[:size - 1] + "…"


def _next_action(doc):
    for line in (work.get_section(doc.body, "Next actions") or "").split("\n"):
        match = re.match(r"^\s*[-*] \[ \]\s*(.*)$", line)
        if match:
            return _clip(match.group(1))
    return ""


def _session_refs(doc):
    """The last three run/task references of a workspace's `Sessions` section, oldest first."""
    refs = []
    for line in (work.get_section(doc.body, "Sessions") or "").split("\n"):
        run = re.search(r"\brun\s+([A-Za-z0-9._-]+?)…?(?=\s|$|,)", line)
        tasks = re.search(r"\btasks?:\s*(.*)$", line)
        names = [t.strip() for t in re.split(r"[,\s]+", tasks.group(1)) if t.strip()] if tasks else []
        if run and names:
            refs += [f"{run.group(1)}/{t}" for t in names]
        elif run:
            refs.append(run.group(1))
    return refs[-SESSION_REFS:]


def _summary(doc, sessions=True):
    value = {"id": doc.id, "title": _clip(doc.meta.get("title", doc.id), 80), "status": doc.meta.get("status", ""),
             "updated": work.updated_iso(doc), "next": _next_action(doc)}
    if sessions:
        value["sessions"] = _session_refs(doc)
    return value


def attached_ids(state_dir):
    try:
        value = json.loads((Path(state_dir) / "session-work.json").read_text()).get("attached", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [i for i in value if isinstance(i, str)] if isinstance(value, list) else []


def _inside(workdir, repo):
    try:
        return workdir == repo or repo in workdir.parents
    except (OSError, ValueError):
        return False


def _repos(doc):
    """The resolved `repos` of a workspace. A malformed entry raises: the caller skips the whole document."""
    repos = doc.meta.get("repos", [])
    if not isinstance(repos, list):
        raise ValueError("repos is not a list")
    return [Path(repo).expanduser().resolve() for repo in repos]


def decision_items(skipped=None):
    rows = work.open_decisions(skipped=skipped)
    return [{"id": r["id"], "priority": r["priority"], "workspace": r["workspace"], "blocking": bool(r["blocking"])} for r in rows]


class _Row:
    """What the block needs from one document, computed inside the document's own try so a malformed file cannot reach the rest."""

    def __init__(self, doc):
        self.doc, self.id = doc, doc.id
        self.updated = work.updated_at(doc)
        self.active = doc.meta.get("status") == "active"
        self.brief = {"id": doc.id, "title": _clip(doc.meta.get("title", doc.id), 80)}


class _Workspace(_Row):
    def __init__(self, doc):
        super().__init__(doc)
        self.repos = _repos(doc)
        self.has_repos = bool(doc.meta.get("repos"))
        self.summary = _summary(doc)


class _Note(_Row):
    def __init__(self, doc):
        super().__init__(doc)
        links = doc.meta.get("workspaces", [])
        self.links = set(links) if isinstance(links, list) else set()
        self.important = bool(doc.meta.get("important"))


def _rows(kind, build_row, skipped):
    rows = []
    for doc in work.iter_docs(kind, skipped=skipped):
        try:
            rows.append(build_row(doc))
        except Exception:  # noqa: BLE001 - per-document isolation: counted in `skipped`, the rest of the block is unaffected
            work.note_skipped(skipped, doc.path)
    return rows


def _opened(ids):
    """{id: workspace_health(id)} for the attached ids; an id whose check itself fails is reported corrupt, never raised."""
    found = {}
    for ws_id in ids:
        try:
            found[ws_id] = work_checkpoint.workspace_health(ws_id)
        except Exception as error:  # noqa: BLE001 - one attached id must not hide the block
            found[ws_id] = (None, f"corrupt: {type(error).__name__}")
    return found


def _attached_rows(ids, opened, by_id):
    """The attached workspaces as rows. One that cannot be used (file missing or corrupt) is still named, with its health, so the
    injected text can say so and `work recover` can be suggested."""
    rows = []
    for ws_id in ids:
        doc, health = opened[ws_id]
        if health == "ok" and ws_id in by_id:
            rows.append(by_id[ws_id].summary)
        elif health == "ok":  # an archived workspace is still attached
            try:
                rows.append(_summary(doc))
                continue
            except Exception:  # noqa: BLE001
                health = "corrupt: unreadable"
        if health != "ok":
            rows.append({"id": ws_id, "title": "", "health": health})
    return rows


def _checkpoint_entry(state_dir, ids, opened, tasks):
    """The checkpoint status of the first attached workspace (the one the injection speaks of), or None; it never raises."""
    if not ids:
        return None
    ws_id = ids[0]
    try:
        return work_checkpoint.status(ws_id, Path(state_dir).name, tasks, opened[ws_id])
    except Exception:  # noqa: BLE001 - the checkpoint line is an extra; a failure here must not hide the rest of the block
        return None


def _recovery(state_dir):
    try:
        value = json.loads((Path(state_dir) / work_checkpoint.RECOVERY_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("pending") is not True:
        return None
    return {"pending": True, "since": _clip(value.get("since"), 40), "reason": _clip(value.get("reason"), 80)}


def build(state_dir, workdir=None, tasks=None):
    """`tasks`: the live task records (for the checkpoint's task diff); None when the caller has none."""
    skipped = []
    workspaces, notes = _rows("workspace", _Workspace, skipped), _rows("note", _Note, skipped)
    items = decision_items(skipped)
    ids = list(dict.fromkeys(attached_ids(state_dir)))
    if not workspaces and not notes and not skipped_count(skipped) and not ids:
        return None
    cap_candidates, cap_notes = work.setting("inject_candidates"), work.setting("inject_notes")
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=work.setting("recent_days"))
    by_id = {w.id: w for w in workspaces}
    opened = _opened(ids)
    attached = _attached_rows(ids, opened, by_id)

    candidates = []
    workdir = os.getenv("TP_WORKDIR") if workdir is None else workdir
    if not attached:
        try:
            here = Path(workdir).expanduser().resolve() if workdir else None
        except (OSError, ValueError, RuntimeError):
            here = None
        active = sorted((w for w in workspaces if w.active), key=lambda w: w.updated, reverse=True)
        if here is not None:
            candidates = [w for w in active if any(_inside(here, r) for r in w.repos)][:cap_candidates]
        if not candidates:  # a non-coding workspace: one line, only when it is recent and nothing else points at this session
            fresh = [w for w in active if not w.has_repos and w.updated >= recent_cutoff]
            candidates = fresh[:1]
    shown = set(ids) | {w.id for w in candidates}
    others = sum(1 for w in workspaces if w.active and w.id not in shown)

    recent = sorted((n for n in notes if n.updated >= recent_cutoff), key=lambda n: n.updated, reverse=True)
    # workspace-less notes and those linked to the attached or candidate workspaces come first (stable: newest first within each)
    recent.sort(key=lambda n: bool(n.links) and not n.links & shown)
    important = sorted((n for n in notes if n.important), key=lambda n: n.updated, reverse=True)
    block = {
        "attached": attached,
        "candidates": [w.summary for w in candidates],
        "others": others,
        "decisions": {"open": len(items), "blocking": sum(1 for i in items if i["blocking"]), "chars": work.setting("inject_decisions_chars"),
                      "items": items},
        "notes": {"recent": [n.brief for n in recent[:cap_notes]],
                  "important": [n.brief for n in important[:cap_notes]]},
        "checkpoint": _checkpoint_entry(state_dir, ids, opened, tasks),
    }
    recovery = _recovery(state_dir) if ids else None  # a marker with nothing attached has nothing to recover
    if recovery:
        block["recovery"] = recovery
    if skipped_count(skipped):  # only when something could not be read, so a healthy store's block is what it was
        block["skipped"] = {"count": skipped_count(skipped)}
    return block


# ------------------------------------------------------------ bounded summary

def _trailer(more, more_blocking):
    return f"+{more} more ({more_blocking} blocking): {COMMAND}" if more > 0 else f"{COMMAND} (every question and detail)"


def decision_summary(decisions, chars=None):
    """Bounded text for the injection and Pi: the open and blocking counts, then the first ids (blocking first, then priority,
    then age; the order `items` already has) that fit `chars` one line each, then the command that lists every one. The trailer
    is reserved first, so it is never the thing that is clipped. Empty when nothing is open."""
    chars = decisions.get("chars") if chars is None else chars
    chars = chars if isinstance(chars, int) and chars > 0 else work.DEFAULTS["inject_decisions_chars"]
    items = decisions.get("items") or []
    opened = decisions.get("open", len(items))
    blocking = decisions.get("blocking", sum(1 for i in items if i.get("blocking")))
    if not opened:
        return ""
    rank = {name: i for i, name in enumerate(work.PRIORITIES)}
    ordered = sorted(items, key=lambda i: (not i.get("blocking"), rank.get(i.get("priority"), 9)))
    header = f"decisions: {opened} open ({blocking} blocking)"
    size = len(header) + 1 + max(len(_trailer(opened, blocking)), len(_trailer(0, 0)))
    lines, shown_blocking = [], 0
    for item in ordered:
        line = f"- {item['id']} [{item.get('priority')}]" + (" blocking" if item.get("blocking") else "")
        if size + len(line) + 1 > chars:
            break
        size += len(line) + 1
        lines.append(line)
        shown_blocking += bool(item.get("blocking"))
    return "\n".join([header, *lines, _trailer(opened - len(lines), blocking - shown_blocking)])


def bounded(index):
    """The index as Pi's `task_index` returns it: `work.decisions.items` replaced by the bounded summary text (R3-F3)."""
    work_block = index.get("work")
    if not isinstance(work_block, dict) or not isinstance(work_block.get("decisions"), dict):
        return index
    d = work_block["decisions"]
    slim = {"open": d.get("open", 0), "blocking": d.get("blocking", 0), "summary": decision_summary(d)}
    return {**index, "work": {**work_block, "decisions": slim}}
