"""Canonical human task records for tmux-pi task directories.

Slice 1 keeps the existing runtime files (`meta.json`, `state.json`, `result.md`,
`packet.md`) and makes `task.md` the human-readable current record. The original
verbatim request is kept in `request.md`; legacy task directories whose `task.md`
was the raw request are migrated additively when a record is written.
"""

import datetime as _dt
import json
import os
import re
import subprocess
import tempfile
import textwrap
import sys
from pathlib import Path

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_verdict  # noqa: E402

MARKER = "<!-- tmux-pi-task-record:v1 -->"
ACCEPTED_STATES = {"starting", "working", "needs_input", "settled", "completed", "paused", "failed", "killed", "stopped", "escalated"}
RESUMABLE_STATES = {"starting", "working", "needs_input", "settled", "paused"}
STOP_RECORD_MODES = {"plan", "prototype", "investigate"}
TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,12}-\d+)\b")
EXPLICIT_TICKET_RE = re.compile(r"\b(?:youtrack|ticket|issue)\s*[:=#]\s*([A-Z][A-Z0-9]{1,12}-\d+)\b", re.I)
VOLATILE_RECORD_LINES = ("- **Updated:**", "- Status detail:")
VOLATILE_RETENTION_FILES = {"task.md", "retention.json"}


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_canonical(text):
    return MARKER in (text or "")


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def _write_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     prefix=".{}.".format(path.name), suffix=".tmp", delete=False) as f:
        tmp = Path(f.name)
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _fence(text):
    fence = "```"
    while fence in (text or ""):
        fence += "`"
    return fence


def _extract_fenced_request(record):
    lines = record.splitlines()
    in_summary = False
    fence = ""
    body = []
    for line in lines:
        if line.startswith("## "):
            in_summary = line.strip().lower() == "## request summary"
            if body:
                break
            continue
        if not in_summary:
            continue
        if not fence:
            m = re.match(r"^(`{3,})(?:\w+)?\s*$", line.strip())
            if m:
                fence = m.group(1)
            continue
        if line.strip() == fence:
            break
        body.append(line)
    return "\n".join(body).strip()


def request_text(task_dir):
    """Verbatim request for a task dir, supporting canonical and legacy layouts."""
    d = Path(task_dir)
    req = _read(d / "request.md").strip()
    if req:
        return req
    task = _read(d / "task.md").strip()
    if is_canonical(task):
        return _extract_fenced_request(task)
    return task


def _configured_ticket_prefixes():
    prefixes = set()
    raw = os.environ.get("TP_TICKET_PREFIXES", "")
    for part in re.split(r"[,\s]+", raw):
        if part:
            prefixes.add(part.upper())
    try:
        local = json.loads((Path(__file__).resolve().parent.parent / "config" / "local.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = {}
    for key in ("ticket_prefixes", "youtrack_prefixes"):
        value = local.get(key)
        if isinstance(value, str):
            prefixes.update(p.upper() for p in re.split(r"[,\s]+", value) if p)
        elif isinstance(value, list):
            prefixes.update(str(p).upper() for p in value if str(p).strip())
    return prefixes


def _valid_ticket(candidate, configured=None, explicit=False):
    if not candidate:
        return ""
    ticket = candidate.upper()
    prefix = ticket.split("-", 1)[0]
    configured = configured if configured is not None else _configured_ticket_prefixes()
    if not explicit and (not configured or prefix not in configured):
        return ""
    return ticket


def tickets_in(text):
    configured = _configured_ticket_prefixes()
    out = []
    for m in EXPLICIT_TICKET_RE.finditer(text or ""):
        ticket = _valid_ticket(m.group(1), configured=configured, explicit=True)
        if ticket and ticket not in out:
            out.append(ticket)
    if configured:
        for m in TICKET_RE.finditer(text or ""):
            ticket = _valid_ticket(m.group(1), configured=configured, explicit=False)
            if ticket and ticket not in out:
                out.append(ticket)
    return out


def ticket_in(text):
    found = tickets_in(text)
    return found[0] if found else ""


def title_for(request, fallback_id="task"):
    lines = [l.strip(" #\t") for l in (request or "").splitlines() if l.strip()]
    base = lines[0] if lines else fallback_id
    base = re.sub(r"\s+", " ", base).strip()
    ticket = ticket_in(request)
    if ticket:
        if not EXPLICIT_TICKET_RE.search(base):
            base = re.sub(r"\b{}\b".format(re.escape(ticket)), "", base).strip(" -:—[]()")
        if not base:
            base = "Task {}".format(ticket)
        return "[{}] {}".format(ticket, _clip(base, 96)), ticket
    return _clip(base, 112), ""


def _clip(text, limit):
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def retention(task_dir, request=None):
    d = Path(task_dir)
    data = read_json(d / "retention.json")
    ticket = ticket_in(request if request is not None else request_text(d))
    reason = data.get("reason") or "pinned"
    auto_ticket_pin = bool(data.get("pinned")) and str(reason).startswith("YouTrack ticket ")
    explicit = (bool(data.get("pinned")) and not auto_ticket_pin) or (d / "pinned").exists()
    reasons = []
    if explicit:
        reasons.append(reason)
    if ticket:
        # Until a later slice can ask YouTrack whether the ticket is closed, treat
        # linked tickets as open/protected. This is deliberately conservative for
        # dry-run candidate reporting.
        yt = "YouTrack ticket {}".format(ticket)
        if not any(str(r).startswith(yt) for r in reasons):
            reasons.append(yt + " (treated open)")
    return {"pinned": explicit or bool(ticket), "explicit": explicit, "ticket": ticket, "reasons": reasons}


def set_pin(task_dir, pinned=True, reason="manual"):
    d = Path(task_dir)
    data = read_json(d / "retention.json")
    data.update({"pinned": bool(pinned), "reason": reason, "updated": _now()})
    _write_atomic(d / "retention.json", json.dumps(data, indent=2, sort_keys=True) + "\n")
    return data


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_ts(ts):
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else ""


def _mtime_iso(path):
    try:
        return _iso_from_ts(Path(path).stat().st_mtime)
    except OSError:
        return ""


def _rel(path, workdir=None):
    if not path:
        return ""
    try:
        p = Path(path)
        if workdir:
            return str(p.resolve().relative_to(Path(workdir).resolve()))
        return str(p)
    except (OSError, ValueError):
        return str(path)


def _summary(reply):
    lines = [l.strip() for l in (reply or "").splitlines() if l.strip()]
    return lines[0] if lines else ""


def _artifact_status(path):
    text = _read(path)
    if not text:
        return "missing"
    for line in text.splitlines()[:8]:
        m = re.match(r"^\W*status\W*:?\W*(outline|draft|final|partial)\b", line, re.I)
        if m:
            return m.group(1).lower()
    return "present"


def _links(task_dir, meta):
    d = Path(task_dir)
    links = []
    for name, label in (("request.md", "request"), ("packet.md", "context packet"), ("main-contract.json", "main Orient/Frame contract"),
                        ("policy-decision.json", "post-contract policy decision"), ("context-manifest.json", "context manifest"),
                        ("result.md", "result"), ("state.json", "runtime state"), ("meta.json", "metadata")):
        if (d / name).exists():
            links.append((label, str(d / name)))
    contract = meta.get("contract") or {}
    if contract.get("artifact"):
        links.append(("contract artifact", contract["artifact"]))
    if meta.get("review"):
        links.append(("review task", meta["review"]))
    if (d / "escalated").exists():
        links.append(("escalated to", _read(d / "escalated").strip()))
    return links


def next_action(state, task_id):
    if state == "working":
        return "Watch with `tp-view {}` or wait for the worker to settle.".format(task_id)
    if state == "needs_input":
        return "Answer the worker in its pane or with `send_to_subagent`; then collect again."
    if state == "settled":
        return "Read/collect `result.md`; this marks the task completed when handed off."
    if state == "completed":
        return "Verify/close out, or resume only if follow-up work is intentional."
    if state == "paused":
        return "Choose continue, narrow, escalate, or discard; do not silently retry."
    if state == "failed":
        return "Inspect `result.md`, `state.json`, and the pane/exit code before retrying."
    if state in ("killed", "stopped"):
        return "Task was {}; start a new task only if the work is still needed.".format(state)
    if state == "escalated":
        return "Continue from the escalated task recorded in this directory."
    return "Check task state before continuing."


def _material_record_text(text):
    return "\n".join(line for line in (text or "").splitlines() if not line.startswith(VOLATILE_RECORD_LINES))


def write_record(task_dir, lifecycle_state="starting", detail="", meta=None, force=False):
    """Write/update canonical task.md and return its structured fields.

    If this is a legacy task dir, preserve the old raw task text as request.md
    before replacing task.md with the canonical record.
    """
    d = Path(task_dir)
    d.mkdir(parents=True, exist_ok=True)
    meta = meta or read_json(d / "meta.json")
    old_task = _read(d / "task.md")
    if old_task.strip() and not is_canonical(old_task) and not (d / "request.md").exists():
        _write_atomic(d / "request.md", old_task if old_task.endswith("\n") else old_task + "\n")
    req = request_text(d)
    title, ticket = title_for(req, meta.get("id") or d.name)
    ret = retention(d, req)
    # Record auto-protection in retention.json only for explicit visibility; the
    # dry-run code also protects ticket-linked tasks even if this file is absent.
    if ticket and not (d / "retention.json").exists() and not (d / "pinned").exists():
        set_pin(d, True, "YouTrack ticket {} (auto-protected)".format(ticket))
        ret = retention(d, req)

    state = lifecycle_state or "starting"
    reply = _read(d / "result.md").strip()
    contract = meta.get("contract") or {}
    artifact = contract.get("artifact")
    preflight = meta.get("preflight") or {}
    fence = _fence(req)
    parts = ["# {}".format(title), "", MARKER, ""]
    parts += [
        "- **Task ID:** `{}`".format(meta.get("id") or d.name),
        "- **Lifecycle State:** `{}`".format(state),
        "- **Created:** {}".format(meta.get("created") or "unknown"),
        "- **Updated:** {}".format(_now()),
        "- **Worker:** {}{}{}".format(meta.get("worker") or "unknown",
                                      " / " + meta.get("model") if meta.get("model") else "",
                                      " / " + meta.get("thinking") if meta.get("thinking") else ""),
        "- **YouTrack Ticket:** {}".format(ticket or "none"),
        "- **Pinned:** {}".format("yes ({})".format("; ".join(ret["reasons"])) if ret["pinned"] else "no"),
        "",
        "## Request Summary",
        "",
        "Verbatim request captured for faithful resume/review:",
        "",
        fence + "text",
        req.strip(),
        fence,
        "",
        "## Current Result",
        "",
    ]
    if reply:
        parts.append("- Result file: `{}` (updated {})".format(d / "result.md", _mtime_iso(d / "result.md")))
        parts.append("- Summary: {}".format(_clip(_summary(reply), 220)))
        if meta.get("worker") == "main" and preflight.get("stop"):
            out_fence = _fence(reply)
            parts += ["", "Captured main-owned stop-mode output:", "", out_fence + "text", reply, out_fence]
    else:
        parts.append("- No `result.md` yet.")
    if artifact:
        parts.append("- Contract artifact: `{}` ({})".format(artifact, _artifact_status(artifact)))
    relocation = meta.get("archive_relocation") or {}
    if isinstance(relocation, dict) and relocation.get("artifact_from"):
        parts.append("- Archived artifact origin: `{}`".format(relocation["artifact_from"]))
    if preflight:
        parts.append("- Task mode: `{}`; artifact tier: `Tier {}`{}".format(
            preflight.get("mode", "unknown"), preflight.get("artifact_tier", "?"),
            "; stop/no-edits" if preflight.get("stop") or preflight.get("no_edits") else ""))
        if preflight.get("promotion_notice"):
            parts.append("- Promotion notice: {}".format(preflight["promotion_notice"]))
    if detail:
        parts.append("- Status detail: {}".format(detail))
    main_contract = meta.get("main_contract") or read_json(d / "main-contract.json")
    if main_contract:
        material = main_contract.get("material_ambiguity") or {}
        parts += ["", "## Main Orient/Frame", "",
                  "- **Handshake ID:** `{}`".format(main_contract.get("handshake_id", "")),
                  "- **Action:** `{}`".format(main_contract.get("action", "unknown")),
                  "- **Mode:** `{}`".format(main_contract.get("mode", "unknown")),
                  "- **Scope:** `{}`".format(main_contract.get("scope", "unknown")),
                  "- **No Edits:** {}".format("yes" if main_contract.get("no_edits") else "no"),
                  "- **Task Summary:** {}".format(main_contract.get("task_summary", "")),
                  "- **Frame:** {}".format(main_contract.get("frame", "")),
                  "- **Assumptions:** {}".format("; ".join(main_contract.get("assumptions") or []) or "none"),
                  "- **Risk Notes:** {}".format("; ".join(main_contract.get("risk_notes") or []) or "none"),
                  "- **Proof Path:** {}".format("; ".join(main_contract.get("proof_path") or []) or "none"),
                  "- **Done When:** {}".format("; ".join(main_contract.get("done_when") or []) or "none"),
                  "- **Material Ambiguity:** {}".format((material.get("question") or "none") if isinstance(material, dict) else "none"),
                  ""]
    if preflight:
        parts += ["", "## Task Contract", "",
                  "- **Mode:** `{}`".format(preflight.get("mode", "unknown")),
                  "- **Artifact Tier:** `Tier {}`".format(preflight.get("artifact_tier", "?")),
                  "- **Stop Mode:** {}".format("yes" if preflight.get("stop") else "no"),
                  "- **No Edits:** {}".format("yes" if preflight.get("no_edits") else "no"),
                  "- **No-Edit Enforcement Limitation:** {}".format("instruction/authority policy only; tmux-pi does not hard-disable every main-agent edit tool" if preflight.get("no_edits") else "not applicable"),
                  "- **Frame:** {}".format(preflight.get("frame", "")),
                  "- **Decision Boundary:** {}".format(preflight.get("decision_boundary", "")),
                  "- **Allowed Paths:** {}".format("; ".join((preflight.get("allowed") or {}).get("paths") or [])),
                  "- **Allowed Tools:** {}".format("; ".join((preflight.get("allowed") or {}).get("tools") or [])),
                  "- **Allowed MCPs:** {}".format("; ".join((preflight.get("allowed") or {}).get("mcps") or [])),
                  "- **Proof Path:** {}".format("; ".join(preflight.get("proof_path") or [])),
                  "- **Assumptions:** {}".format("; ".join(preflight.get("assumptions") or [])),
                  "- **Triggers:** {}".format("; ".join(preflight.get("triggers") or []) or "none"),
                  ""]
    manifest = meta.get("context_manifest") or read_json(d / "context-manifest.json")
    if manifest:
        sources = manifest.get("sources") or []
        counts = {}
        for s in sources:
            counts[s.get("source", "unknown")] = counts.get(s.get("source", "unknown"), 0) + 1
        refs = []
        for s in sources[:6]:
            ref = s.get("reference") or s.get("path") or s.get("id") or ""
            rng = (":" + str(s.get("range"))) if s.get("range") else ""
            status = " ({})".format(s.get("retrieval_status")) if s.get("retrieval_status") else ""
            refs.append("{}:{}{}{}".format(s.get("source", "unknown"), ref, rng, status))
        parts += ["", "## Context Manifest", "",
                  "- **Schema:** `{}`".format(manifest.get("schema", "unknown")),
                  "- **Selected Sources:** {}".format("; ".join("{} x{}".format(k, v) for k, v in sorted(counts.items())) or "none"),
                  "- **Source Refs:** {}".format("; ".join(refs) or "none"),
                  "- **Untrusted Content:** {}".format(manifest.get("untrusted_content_instruction", "")),
                  ""]
    parts += ["", "## Next Action", "", next_action(state, meta.get("id") or d.name), "", "## Artifact Links", ""]
    for label, path in _links(d, meta):
        parts.append("- {}: `{}`".format(label, path))
    parts += ["", "## Compatibility", "",
              "Legacy runtime files remain authoritative for automation: `meta.json`, `state.json`, `result.md`, `packet.md`, `run.sh`. The original request is retained in `request.md`.", ""]
    rendered = "\n".join(parts)
    if force or not (is_canonical(old_task) and _material_record_text(old_task) == _material_record_text(rendered)):
        _write_atomic(d / "task.md", rendered)
    return {"id": meta.get("id") or d.name, "title": title, "ticket": ticket, "state": state,
            "pinned": ret["pinned"], "request": req}


def refresh_record(state_dir, task_id):
    """Bring one task's canonical record (task.md) up to date after something changed its lifecycle.

    What a writer of a state change calls (collect, stop, message, a capability request), so the record is never left saying
    the old state until some pane happens to look at the task. `tp-contract status ID` is the one place that works the state
    out (with the pane's state) and writes the record. Returns "" or why it did not work; the change itself is already made,
    so a caller reports this rather than failing."""
    root = Path(__file__).resolve().parent.parent
    try:
        run = subprocess.run([str(root / "bin" / "tp-contract"), "status", task_id], capture_output=True, text=True, timeout=60,
                             env=dict(os.environ, TP_STATE_DIR=str(state_dir)))
    except (OSError, subprocess.SubprocessError) as error:
        return "task.md of {} was not refreshed: {}".format(task_id, error)
    if run.returncode:
        return "task.md of {} was not refreshed: {}".format(task_id, (run.stderr.strip() or "tp-contract status failed")[:200])
    if not run.stdout.strip():
        return "task.md of {} was not refreshed: tp-contract status listed no such task".format(task_id)
    return ""


def parse_record(task_dir):
    d = Path(task_dir)
    text = _read(d / "task.md")
    req = request_text(d)
    title = ""
    state = ""
    ticket = ticket_in(req)
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("- **Lifecycle State:**"):
            m = re.search(r"`([^`]+)`", line)
            state = m.group(1) if m else line.split(":", 1)[-1].strip()
        elif line.startswith("- **YouTrack Ticket:**") and not ticket:
            value = line.split(":", 1)[-1].strip()
            ticket = "" if value == "none" else value
    if not title:
        title = title_for(req, d.name)[0]
    return {"id": d.name, "title": title, "state": state or "unknown", "ticket": ticket,
            "request": req, "canonical": is_canonical(text), "path": str(d / "task.md")}


def iter_task_dirs(state_dir, include_archive=True):
    root = Path(state_dir)
    for base_name in (["tasks", "archive"] if include_archive else ["tasks"]):
        base = root / base_name
        if not base.is_dir():
            continue
        for meta in sorted(base.glob("*/meta.json"), key=lambda p: p.stat().st_mtime):
            yield meta.parent, base_name


def find_task(state_dir, query, include_archive=True):
    q = (query or "").lower()
    matches = []
    for d, where in iter_task_dirs(state_dir, include_archive):
        rec = parse_record(d)
        hay = "\n".join([rec["id"], rec["title"], rec["ticket"], rec["request"]]).lower()
        if rec["id"].lower() == q or rec["ticket"].lower() == q or q in hay:
            matches.append((d, where, rec))
    return matches


def newest_mtime(task_dir):
    newest = 0.0
    for p in Path(task_dir).rglob("*"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    try:
        newest = max(newest, Path(task_dir).stat().st_mtime)
    except OSError:
        pass
    return newest


def stable_task_mtime(task_dir):
    """Last meaningful task timestamp for retention, excluding record/pin churn."""
    d = Path(task_dir)
    for name in ("completed", "result.md"):
        try:
            return (d / name).stat().st_mtime
        except OSError:
            pass
    newest = 0.0
    for p in d.rglob("*"):
        if p.name in VOLATILE_RETENTION_FILES:
            continue
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            pass
    if newest:
        return newest
    try:
        return d.stat().st_mtime
    except OSError:
        return 0.0


def _artifact_protects(task_dir, artifact):
    if not artifact:
        return False
    try:
        art = Path(artifact).resolve()
        d = Path(task_dir).resolve()
    except OSError:
        return False
    if not art.exists():
        return False
    return art != d and d not in art.parents


def _identity_index(state):
    """One scan per inventory; never cache authority across inputs or filesystem changes."""
    index = {}
    for base in (Path(state) / "tasks", Path(state) / "archive"):
        if base.is_symlink():
            return {}  # No evidence can be trusted through a symlinked root.
        if not base.is_dir():
            continue
        for d in base.iterdir():
            meta = d / "meta.json"
            if d.is_symlink() or not d.is_dir() or meta.is_symlink() or not meta.is_file():
                continue
            identity = read_json(meta).get("id")
            if isinstance(identity, str):
                index.setdefault(identity, []).append(d)
    return index


def locate(state, identity, index=None):
    """Unique task identity across active and archived records; never follow evidence symlinks."""
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", identity):
        return None
    matches = (index if index is not None else _identity_index(state)).get(identity, [])
    return matches[0] if len(matches) == 1 else None


def author_result_hash(task_dir):
    """None means absent output only; a symlink or unreadable output is invalid."""
    import hashlib
    path = Path(task_dir) / "result.md"
    if path.is_symlink():
        raise OSError("symlinked author result")
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approved_delegated_review(task_dir, meta, marker, index=None):
    """Verify recorded approval against the independent review task and its report."""
    import hashlib
    import re
    if ((Path(task_dir) / "meta.json").is_symlink() or not isinstance(meta, dict) or not isinstance(marker, dict) or marker.get("required") is not True or marker.get("status") != "approved" or marker.get("task_id") != meta.get("id")):
        return False
    approval = marker.get("approval")
    if not isinstance(approval, dict) or not isinstance(marker.get("round_nonce"), str) or not marker["round_nonce"] or approval.get("round_nonce") != marker["round_nonce"] or not isinstance(approval.get("reviewer_id"), str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", approval["reviewer_id"]):
        return False
    reviewer = locate(Path(task_dir).parent.parent, approval["reviewer_id"], index)
    if reviewer is None or reviewer == Path(task_dir) or any((reviewer / name).is_symlink() for name in ("meta.json", "review-of", "result.md")):
        return False
    review_meta = read_json(reviewer / "meta.json")
    author_worker = meta.get("worker")
    if author_worker not in ("pi", "claude") or not isinstance(review_meta, dict) or review_meta.get("worker") != {"pi": "claude", "claude": "pi"}[author_worker]:
        return False
    try:
        if (reviewer / "review-of").read_text().strip() != meta["id"]:
            return False
        report = review_verdict.read_review(reviewer / "result.md")
    except (OSError, KeyError, ValueError):
        return False
    verdict = report.strip().strip("\ufeff").strip()
    try:
        author_hash = author_result_hash(task_dir)
        if author_hash is None:
            return False
    except OSError:
        return False
    return (approval.get("evidence_sha256") == hashlib.sha256(verdict.encode()).hexdigest() and
            review_verdict.approves(verdict) and
            bool(verdict) and approval.get("reviewer_id") == meta.get("review") and
            isinstance(approval.get("author_sha256"), str) and approval["author_sha256"] == author_hash)


def unresolved_review(path, identity=None, id_key="task_id", index=None):
    """A present marker is blocking unless explicitly and validly resolved.

    Unreadable/malformed markers must not make collection or retention fail open.
    """
    import re
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file() or (id_key == "task_id" and (path.parent / "meta.json").is_symlink()):
        return True
    data = read_json(path)
    if isinstance(data, dict) and data.get("status") == "approved" and not (path.parent / "meta.json").is_symlink() and approved_delegated_review(path.parent, read_json(path.parent / "meta.json"), data, index):
        return False
    if isinstance(identity, str) and identity and isinstance(data, dict) and data.get("required") is True and data.get("status") == "pending_review" and data.get(id_key) == identity:
        target_id = data.get("review_target")
        if isinstance(target_id, str) and target_id != identity:
            target = locate(path.parent.parent.parent, target_id, index)
            if target is not None and not (target / "review-required.json").is_symlink():
                target_meta = read_json(target / "meta.json")
                target_marker = read_json(target / "review-required.json")
                if (isinstance(target_meta, dict) and isinstance(target_meta.get("escalated_from"), dict) and
                        target_meta["escalated_from"].get("id") == identity and approved_delegated_review(target, target_meta, target_marker, index)):
                    return False
    resolved = (isinstance(data, dict) and data.get("required") is True and
                isinstance(identity, str) and bool(identity) and data.get(id_key) == identity and
                data.get("status") == "resolved" and isinstance(data.get("resolution"), dict) and
                data["resolution"].get("actor") == "human_cli")
    if resolved and id_key == "task_id":
        nonce = data.get("round_nonce")
        resolution = data["resolution"]
        recorded_hash = resolution.get("author_sha256")
        if (not isinstance(nonce, str) or not nonce or resolution.get("round_nonce") != nonce or
                not (recorded_hash is None and "author_sha256" in resolution or
                     isinstance(recorded_hash, str) and bool(re.fullmatch(r"[0-9a-f]{64}", recorded_hash)))):
            return True
        try:
            return resolution["author_sha256"] != author_result_hash(path.parent)
        except OSError:
            return True
    return not resolved


def review_inventory(state_dir, watching=()):
    """List pending markers and inherited references, including archived tasks."""
    root = Path(state_dir)
    blockers, references = [], []
    watching = set(watching)
    index = _identity_index(root)
    for kind, base in (("handoff", root / "handoffs"), ("task", root / "tasks"), ("task", root / "archive")):
        if base.is_symlink():
            raise ValueError("symlinked review inventory root: {}".format(base))
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir() or d.is_symlink():
                continue
            path = d / "review-required.json"
            task_meta = read_json(d / "meta.json") if kind == "task" else {}
            id_key = "handshake_id" if kind == "handoff" or (isinstance(task_meta, dict) and task_meta.get("worker") == "main") else "task_id"
            identity = d.name if kind == "handoff" else (task_meta.get("handshake_id") if id_key == "handshake_id" else task_meta.get("id"))
            if path.is_file() and not path.is_symlink():
                # A corrupt marker must not silently turn into an ordinary blocker:
                # main loses all edit authority until it is repaired.
                try:
                    json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise ValueError("malformed review marker {}: {}".format(path, exc)) from exc
            if unresolved_review(path, identity, id_key, index):
                marker = read_json(path) if not path.is_symlink() else {}
                marker = marker if isinstance(marker, dict) else {}
                # Watchers relax main's edit gate, never collect. Only live, valid
                # pending rounds (or a source linked to one) may be exempt.
                watched = False
                if kind == "task" and base == root / "tasks" and id_key == "task_id" and isinstance(identity, str) and bool(identity) and marker.get("required") is True and marker.get("task_id") == identity and marker.get("status") == "pending_review":
                    if identity in watching and isinstance(marker.get("round_nonce"), str) and marker["round_nonce"]:
                        watched = locate(root, identity, index) == d
                    else:
                        target_id = marker.get("review_target")
                        target = locate(root, target_id, index)
                        if target is not None and target.parent == root / "tasks" and target_id in watching and not (target / "review-required.json").is_symlink():
                            tm, tr = read_json(target / "meta.json"), read_json(target / "review-required.json")
                            watched = (isinstance(tm.get("escalated_from"), dict) and tm["escalated_from"].get("id") == identity and
                                       tr.get("required") is True and tr.get("task_id") == target_id and tr.get("status") == "pending_review" and
                                       isinstance(tr.get("round_nonce"), str) and bool(tr["round_nonce"]))
                reasons = marker.get("reasons")
                blockers.append({"kind": kind, "id": identity if isinstance(identity, str) and identity else d.name, "status": str(marker.get("status", "invalid")),
                                 "reasons": [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else [],
                                 "review_reference": str(marker.get("review_reference") or marker.get("reviewer") or marker.get("review") or ""),
                                 "path": str(path), "watched": watched})
            source = read_json(d / ("scaffold.json" if kind == "handoff" else "meta.json"))
            blocked_by = source.get("blocked_by") if isinstance(source, dict) else None
            if isinstance(blocked_by, list) and blocked_by:
                references.append({"kind": kind, "id": identity if isinstance(identity, str) and identity else d.name, "blocked_by": [str(ref) for ref in blocked_by],
                                   "path": str(d / ("scaffold.json" if kind == "handoff" else "meta.json"))})
    pending = {b["id"] if b["kind"] == "handoff" else "task:" + b["id"] for b in blockers if not b["watched"]}
    references = [{**row, "blocked_by": [ref for ref in row["blocked_by"] if ref in pending]}
                  for row in references]
    return {"blockers": blockers, "references": [row for row in references if row["blocked_by"]]}


def retention_report(state_dir, days=90, now=None):
    now = now if now is not None else _dt.datetime.now(_dt.timezone.utc).timestamp()
    cutoff = now - days * 86400
    rows = []
    for d, where in iter_task_dirs(state_dir, include_archive=True):
        meta = read_json(d / "meta.json")
        rec = parse_record(d)
        req = rec["request"]
        ret = retention(d, req)
        mtime = stable_task_mtime(d)
        age_days = int((now - mtime) // 86400) if mtime else None
        protected = []
        if ret["pinned"]:
            protected.extend(ret["reasons"] or ["pinned"])
        contract = meta.get("contract") or {}
        artifact = contract.get("artifact")
        if _artifact_protects(d, artifact):
            protected.append("externally durable contract artifact exists")
        preflight = meta.get("preflight") or {}
        if preflight.get("mode") in {"plan", "prototype"}:
            protected.append("main-owned {} stop-mode record".format(preflight.get("mode")))
        if int(preflight.get("artifact_tier") or 0) >= 2:
            protected.append("promoted durable task record")
        if rec["state"] == "paused":
            protected.append("paused resumable task")
        if (d / "pending-capability.json").exists() or (d / "pending-capability.json").is_symlink():
            protected.append("pending human capability resolution")
        if unresolved_review(d / "review-required.json", meta.get("handshake_id") if meta.get("worker") == "main" else meta.get("id"),
                             "handshake_id" if meta.get("worker") == "main" else "task_id"):
            protected.append("pending human review")
        handshake = meta.get("handshake_id")
        if isinstance(handshake, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", handshake):
            if unresolved_review(Path(state_dir) / "handoffs" / handshake / "review-required.json", handshake, "handshake_id"):
                protected.append("pending handoff review")
        blocked_by = meta.get("blocked_by")
        if isinstance(blocked_by, list) and any(
            isinstance(ref, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", ref) and
            unresolved_review(Path(state_dir) / "handoffs" / ref / "review-required.json", ref, "handshake_id") for ref in blocked_by
        ):
            protected.append("pending inherited review")
        eligible = bool(mtime and mtime < cutoff and not protected)
        resolved = str(Path(d).resolve())
        review_key = "{}\x1f{}".format(resolved, d.name)
        rows.append({"id": d.name, "where": where, "title": rec["title"], "state": rec["state"],
                     "ticket": ret["ticket"], "age_days": age_days, "last_modified": _iso_from_ts(mtime),
                     "eligible": eligible, "protected": protected, "path": str(d),
                     "resolved_path": resolved, "review_key": review_key})
    return rows


def capture_main_output(task_dir, output, lifecycle_state="completed"):
    """Persist main-owned stop-mode output and refresh the canonical record."""
    d = Path(task_dir)
    meta = read_json(d / "meta.json")
    if meta.get("worker") != "main":
        raise ValueError("{} is not a main-owned task record".format(d.name))
    state = lifecycle_state if lifecycle_state in ACCEPTED_STATES else "completed"
    _write_atomic(d / "result.md", (output or "").rstrip() + "\n")
    _write_atomic(d / "state.json", json.dumps({"state": state, "updated": _now()}, sort_keys=True) + "\n")
    if state == "completed":
        _write_atomic(d / "completed", str(_dt.datetime.now(_dt.timezone.utc).timestamp()) + "\n")
    return write_record(d, lifecycle_state=state, meta=meta, force=True)


def _archive_destination(archive_dir, task_id):
    dest = Path(archive_dir) / task_id
    if not os.path.lexists(dest):
        return dest
    stamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    n = 0
    while True:
        candidate = Path(archive_dir) / "{}-ended-{}{}".format(task_id, stamp, "-{}".format(n) if n else "")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


def _reconcile_archived_record(task_dir):
    """Repair relocated local artifact provenance, then render paths from the new home."""
    d = Path(task_dir)
    meta = read_json(d / "meta.json")
    contract = meta.get("contract") or {}
    artifact = contract.get("artifact")
    if isinstance(artifact, str) and artifact:
        # Only an absolute artifact inside the original task directory can have
        # moved with it. Never rewrite external research paths (or relative paths).
        old = d.parent.parent / "tasks" / (meta.get("id") or d.name)
        try:
            relative = Path(artifact).relative_to(old)
        except ValueError:
            relative = None
        if relative is not None and relative.parts and ".." not in relative.parts:
            new_artifact = str(d / relative)
            meta["contract"] = {**contract, "artifact": new_artifact}
            meta.setdefault("archive_relocation", {"artifact_from": artifact, "artifact_to": new_artifact})
            meta_path = d / "meta.json"
            stamp = meta_path.stat()
            _write_atomic(meta_path, json.dumps(meta, indent=2, sort_keys=True) + "\n")
            os.utime(meta_path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    write_record(d, lifecycle_state=parse_record(d)["state"], meta=meta)


def archive_task(state_dir, task_dir):
    """Move one active task to a distinct archive name and reconcile its record."""
    root = Path(state_dir)
    d = Path(task_dir)
    if d.parent.resolve() != (root / "tasks").resolve() or d.is_symlink() or not d.is_dir():
        raise ValueError("not an active task directory: {}".format(d))
    archive_dir = root / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = _archive_destination(archive_dir, d.name)
    # An exclusive empty-directory reservation prevents nesting into an existing
    # archive; rename replaces only that empty reservation on the same filesystem.
    while True:
        try:
            dest.mkdir()
            break
        except FileExistsError:
            dest = _archive_destination(archive_dir, d.name)
    try:
        d.rename(dest)
    except Exception:
        dest.rmdir()  # only our empty reservation; leave the source untouched
        raise
    _reconcile_archived_record(dest)
    return dest


def _session_end_reasons(meta, rec, protected):
    reasons = list(protected)
    preflight = meta.get("preflight") or {}
    if meta.get("worker") == "main":
        reasons.append("main-owned record")
    if preflight.get("mode") in STOP_RECORD_MODES:
        reasons.append("stop-mode {}".format(preflight.get("mode")))
    if rec.get("state") in RESUMABLE_STATES:
        reasons.append("resumable state {}".format(rec.get("state")))
    return reasons


def end_session_retention(state_dir, archive_workers=True):
    """Retain/archive task records at /end without deleting task state.

    Worker records are archived so a reopened workspace does not redisplay killed
    panes. Main-owned durable records stay in tasks/. All actions are logged to
    cleanup.log. Returns a summary dict for CLI/tests.
    """
    root = Path(state_dir)
    tasks_dir = root / "tasks"
    archive_dir = root / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    rows_by_path = {r["resolved_path"]: r for r in retention_report(root, days=0)}
    actions = []
    if tasks_dir.is_dir():
        for meta_path in sorted(tasks_dir.glob("*/meta.json")):
            d = meta_path.parent
            meta = read_json(meta_path)
            rec = parse_record(d)
            if not rec["canonical"]:
                write_record(d, lifecycle_state=rec["state"], meta=meta)
                rec = parse_record(d)
            row = rows_by_path.get(str(d.resolve()), {})
            reasons = _session_end_reasons(meta, rec, row.get("protected") or [])
            keep_active = meta.get("worker") == "main"
            if archive_workers and not keep_active:
                dest = archive_task(root, d)
                actions.append({"id": d.name, "action": "archive", "from": str(d), "to": str(dest), "reasons": reasons or ["ordinary session-end retention"]})
            else:
                actions.append({"id": d.name, "action": "retain", "path": str(d), "reasons": reasons or ["ordinary session-end retention"]})
    handoffs = [{"id": d.name, "action": "retain", "path": str(d),
                 "reason": "interrupted handshake may need original request for resume"}
                for d in sorted((root / "handoffs").iterdir()) if d.is_dir()] if (root / "handoffs").is_dir() else []
    event = {"event": "session-end-retention", "at": _now(), "archive_workers": archive_workers,
             "actions": actions, "handoffs": handoffs}
    with (root / "cleanup.log").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")
    return {"archived": [a for a in actions if a["action"] == "archive"],
            "retained": [a for a in actions if a["action"] == "retain"],
            "handoffs": handoffs, "log": str(root / "cleanup.log")}


def format_record_for_resume(task_dir):
    d = Path(task_dir)
    text = _read(d / "task.md")
    if d.parent.name == "archive":
        # Old archives may have canonical text with paths from before the move.
        _reconcile_archived_record(d)
        text = _read(d / "task.md")
    elif not is_canonical(text):
        write_record(d, lifecycle_state=parse_record(d)["state"])
        text = _read(d / "task.md")
    output = _read(d / "result.md").strip()
    output_section = ""
    if output:
        fence = _fence(output)
        output_section = "\n\nResume output from result.md:\n\n{}text\n{}\n{}".format(fence, output, fence)
    return textwrap.dedent("""\
    Task dir: {dir}
    Record: {record}

    {body}{output}
    """).format(dir=d, record=d / "task.md", body=text, output=output_section).rstrip() + "\n"
