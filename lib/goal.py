"""Cooperative, project-local adaptive goal turns. A goal is never a tool/dispatch grant.

Only the dispatcher can authorize edits or workers. This module admits bounded main
turns, accounts reporter events, and fails closed when turn/task ownership is lost.
"""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import uuid

from task_record import approved_delegated_review, locate, review_inventory


class GoalError(Exception):
    pass


IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
STATUSES = {"proposed", "active", "waiting", "paused", "blocked", "budget_limited", "suspended", "complete", "stopped"}
MAX_TEXT = 12000


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def keys(obj, required):
    if not isinstance(obj, dict) or set(obj) != set(required):
        raise GoalError("unknown/missing fields: " + ", ".join(sorted(required)))


def text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        raise GoalError("expected nonempty bounded text")
    return value


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def validate(spec, project):
    mandatory = {"id", "objective", "done", "scope", "workdir", "budget"}
    if not isinstance(spec, dict) or not mandatory <= set(spec) or set(spec) - mandatory - {"allowed_paths"}:
        raise GoalError("unknown/missing goal spec fields")
    if not isinstance(spec["id"], str) or not IDENT.fullmatch(spec["id"]):
        raise GoalError("invalid goal id")
    for name in ("objective", "done", "scope"):
        text(spec[name])
    if not isinstance(spec["workdir"], str) or spec["workdir"] != str(project):
        raise GoalError("approved absolute workdir must match project")
    paths = spec.get("allowed_paths", [])
    if not isinstance(paths, list) or len(paths) > 30 or any(not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or any(part in ("", ".", "..") for part in p.split("/")) or p.startswith(".tmux-pi/") for p in paths):
        raise GoalError("allowed_paths must be bounded project-relative path prefixes")
    keys(spec["budget"], {"seconds", "turns", "tokens", "cost_micros", "attempts"})
    ceilings = {"seconds": 86400, "turns": 1000, "tokens": 10_000_000, "cost_micros": 10_000_000, "attempts": 100}
    for name, v in spec["budget"].items():
        if type(v) is not int or not 0 < v <= ceilings[name]:
            raise GoalError("budget must be positive and at most " + str(ceilings[name]) + " for " + name)
    return spec


def project_root(project):
    p = Path(project).resolve(strict=True)
    if not p.is_dir():
        raise GoalError("project is not a directory")
    return p


def state_path(project, ident):
    if not isinstance(ident, str) or not IDENT.fullmatch(ident):
        raise GoalError("invalid goal id")
    base = project / ".tmux-pi"
    goals = base / "goals"
    for p in (base, goals):
        if not p.exists() and not p.is_symlink():
            p.mkdir(mode=0o700)
        if p.is_symlink() or not p.is_dir():
            raise GoalError("unsafe state directory")
    return goals / (ident + ".json")


def task_state(project):
    """Where this workspace keeps task records. The goal ledger stays project-level.

    bin/tp gives every run <project>/.tmux-pi/sessions/<run> and exports it as
    TP_STATE_DIR; without it only the legacy <project>/.tmux-pi can be meant.
    """
    base = project / ".tmux-pi"
    raw = os.environ.get("TP_STATE_DIR")
    if not raw:
        return base
    state = Path(raw).resolve()
    if state != base and state.parent != base / "sessions":
        raise GoalError("TP_STATE_DIR is not a tmux-pi state root of this project")
    return state


def unique_pairs(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate JSON key: " + k)
        result[k] = v
    return result


def read(path):
    if path.is_symlink() or not path.is_file():
        raise GoalError("missing or unsafe evidence: " + str(path))
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f, object_pairs_hook=unique_pairs)
    except (OSError, ValueError) as exc:
        raise GoalError("invalid evidence: " + str(exc)) from exc


def atomic(path, data):
    if path.is_symlink():
        raise GoalError("unsafe goal file")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".goal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def locked(project, ident):
    path = state_path(project, ident)
    lock = path.with_suffix(".lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
            raise GoalError("unsafe goal lock")
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        os.close(fd)


def load(path, project, ident):
    s = read(path)
    keys(s, {"version", "spec", "spec_sha256", "status", "started", "usage", "attempts", "inflight", "wait_for", "events", "last_event", "blocker", "blocker_count", "reason", "evidence", "tasks"})
    if type(s["version"]) is not int or s["version"] != 1 or validate(s["spec"], project)["id"] != ident or s["spec_sha256"] != digest(s["spec"]):
        raise GoalError("unknown version or changed approved scope")
    if not isinstance(s["status"], str) or s["status"] not in STATUSES or type(s["attempts"]) is not int or s["attempts"] < 0:
        raise GoalError("unknown goal state")
    keys(s["usage"], {"turns", "tokens", "cost_micros"})
    if any(type(v) is not int or v < 0 for v in s["usage"].values()):
        raise GoalError("invalid usage")
    if not isinstance(s["events"], dict) or not isinstance(s["tasks"], dict) or not isinstance(s["evidence"], list) or type(s["last_event"]) is not int or s["last_event"] < 0:
        raise GoalError("invalid ledger")
    if s["wait_for"] is not None and (not isinstance(s["wait_for"], str) or not IDENT.fullmatch(s["wait_for"]) or s["status"] not in ("waiting", "paused", "budget_limited", "suspended", "stopped")):
        raise GoalError("unknown wait handle")
    if s["inflight"] is not None and (not isinstance(s["inflight"], dict) or set(s["inflight"]) != {"ticket", "at"} or s["status"] not in ("active", "paused", "suspended", "budget_limited", "stopped")):
        raise GoalError("unknown in-flight turn")
    if s["status"] == "proposed" and s["started"] is not None:
        raise GoalError("unapproved goal start")
    if s["started"] is not None:
        try:
            start = dt.datetime.fromisoformat(s["started"])
            if start.tzinfo is None or start.utcoffset() is None:
                raise ValueError("naive time")
        except (TypeError, ValueError) as exc:
            raise GoalError("invalid start") from exc
    return s


def log(s, action, detail):
    s["last_event"] += 1
    s["evidence"].append({"seq": s["last_event"], "at": utc(), "action": action, "detail": detail})


def limit(s):
    if s["started"] is None:
        return None
    b = s["spec"]["budget"]
    if (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s["started"])).total_seconds() >= b["seconds"]:
        return "wall-time budget exhausted"
    if s["attempts"] >= b["attempts"] or any(s["usage"][k] >= b[k] for k in s["usage"]):
        return "turn/token/cost/attempt budget exhausted"
    return None


def plan(project, spec):
    project = project_root(project)
    validate(spec, project)
    with locked(project, spec["id"]) as path:
        if path.exists() or path.is_symlink():
            s = load(path, project, spec["id"])
            if s["status"] == "proposed" and s["spec"] == spec:
                return s
            raise GoalError("goal exists; no implicit replacement")
        s = {"version": 1, "spec": spec, "spec_sha256": digest(spec), "status": "proposed", "started": None,
             "usage": {"turns": 0, "tokens": 0, "cost_micros": 0}, "attempts": 0,
             "inflight": None, "wait_for": None, "events": {}, "last_event": 0, "blocker": None, "blocker_count": 0,
             "reason": "Explicit human start approval pending", "evidence": [], "tasks": {}}
        atomic(path, s)
        return s


def _reconcile(s, project):
    # A saved ticket is NOT proof of a live Pi turn. Never restart it after reload.
    if s["status"] == "waiting" and s["wait_for"] and not _live_task(project, s["wait_for"]):
        s["status"] = "suspended"
        s["reason"] = "unknown former wait handle after restart; human reconciliation required"
        log(s, "suspend", s["reason"])
    if s["status"] == "active" and s["inflight"] is not None:
        s["status"] = "suspended"
        s["reason"] = "unknown in-flight main turn after restart; human reconciliation required"
        log(s, "suspend", s["reason"])
    for task_id in s["tasks"]:
        if locate(task_state(project), task_id) is None:
            s["status"] = "suspended"
            s["reason"] = "missing or ambiguous linked task; human reconciliation required"
            log(s, "suspend", s["reason"])
            break


def _unlinked(s, project):
    # The dispatcher may have delegated during this turn. Without its durable
    # task-id/usage/review handoff we MUST NOT run the next turn or claim success.
    for root in (task_state(project) / "tasks", task_state(project) / "archive"):
        if root.is_symlink():
            return "unsafe task root"
        if not root.is_dir():
            continue
        for d in root.iterdir():
            if d.is_symlink() or not d.is_dir() or (d / "meta.json").is_symlink():
                return "unsafe task evidence"
            if not (d / "meta.json").exists():
                return "task/archive directory without metadata; unknown handle"
            try:
                meta = read(d / "meta.json")
                if meta.get("workdir") == str(project) and s["started"]:
                    if not isinstance(meta.get("created"), str):
                        return "task creation timestamp unavailable"
                    created = dt.datetime.fromisoformat(meta["created"].replace("Z", "+00:00"))
                    if created >= dt.datetime.fromisoformat(s["started"]).replace(microsecond=0) and meta.get("id") not in s["tasks"]:
                        return "new task not linked/metered/reviewed by goal: " + str(meta.get("id"))
            except (GoalError, TypeError, ValueError):
                return "unreadable task metadata"
    return None


def _unsafe_followup(project, excluded=()):
    # Dispatcher follow-up routing can consume extension-sourced input. Never send
    # a goal turn while any live worker in this session could claim that input.
    base = task_state(project) / "tasks"
    if base.is_symlink():
        return "unsafe active task root"
    if not base.is_dir():
        return None
    pending = []
    for d in base.iterdir():
        if d.is_symlink() or not d.is_dir():
            return "unsafe active task directory"
        if not (d / "meta.json").exists() or (d / "meta.json").is_symlink():
            return "unknown active task metadata"
        if (d / "exit").exists() and not (d / "exit").is_symlink():
            continue
        try:
            meta = read(d / "meta.json")
            if meta.get("id") in excluded:
                continue
            state = read(d / "state.json")
            if state.get("state") not in ("starting", "working", "tool", "waiting", "error", "failed", "killed", "done", "completed"):
                return "unknown worker lifecycle; goal input could become a follow-up"
            pane = meta.get("pane")
            if not isinstance(pane, str) or not pane.startswith("%"):
                return "unknown worker pane; goal input could become a follow-up"
            pending.append(pane)
        except (GoalError, TypeError, ValueError):
            return "unknown worker evidence; goal input could become a follow-up"
    if not pending:
        return None
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"], capture_output=True, text=True, timeout=3, check=True)
        alive = {line.split()[0] for line in result.stdout.splitlines() if len(line.split()) == 2 and line.split()[1] == "0"}
        if alive.intersection(pending):
            return "live worker could claim goal continuation as follow-up"
    except (OSError, subprocess.SubprocessError):
        return "worker pane liveness unavailable"
    return None


def _meter_tasks(s, project):
    for task_id, link in s["tasks"].items():
        d = locate(task_state(project), task_id)
        if d is None or (d / "state.json").is_symlink():
            return "missing or unsafe descendant reporter: " + task_id
        try:
            state = read(d / "state.json")
            tokens = state.get("tokens")
            if not isinstance(tokens, dict):
                return "unmetered descendant tokens: " + task_id
            turns = state.get("turns")
            cost = state.get("cost")
            if type(turns) is not int or turns < 0 or type(cost) not in (float, int) or not math.isfinite(cost) or cost < 0:
                return "unmetered descendant turns/cost: " + task_id
            counts = [tokens.get(k) for k in ("input", "output")]
            if any(type(n) is not int or n < 0 for n in counts):
                return "unmetered descendant input/output: " + task_id
            measured = {"turns": turns, "tokens": sum(counts), "cost_micros": math.ceil(cost * 1_000_000)}
            old = link.get("metered", {k: 0 for k in measured})
            if not isinstance(old, dict) or set(old) != set(measured) or any(type(old[k]) is not int or measured[k] < old[k] for k in measured):
                return "descendant usage reset or malformed: " + task_id
            for k in measured:
                s["usage"][k] += measured[k] - old[k]
            link["metered"] = measured
        except (GoalError, OSError, TypeError, ValueError, OverflowError):
            return "descendant reporter unavailable: " + task_id
    return None


def _live_task(project, task_id):
    d = locate(task_state(project), task_id)
    if d is None:
        return False
    try:
        meta = read(d / "meta.json")
        state = read(d / "state.json")
        pane = meta.get("pane")
        if (d / "exit").exists() or (d / "pending-capability.json").exists() or state.get("state") not in ("starting", "working", "tool", "waiting") or not isinstance(pane, str) or not pane.startswith("%"):
            return False
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"], capture_output=True, text=True, timeout=3, check=True)
        return any(line.split() == [pane, "0"] for line in result.stdout.splitlines())
    except (GoalError, OSError, ValueError, TypeError, subprocess.SubprocessError):
        return False


def _stale_review_wait(s, project):
    reviewers = [rid for rid, link in s["tasks"].items() if link.get("author_id") == s["wait_for"]]
    if any(_live_task(project, rid) and read(locate(task_state(project), rid) / "state.json").get("state") in ("starting", "working", "tool") for rid in reviewers):
        return False
    for task_id in [s["wait_for"]] + reviewers:
        d = locate(task_state(project), task_id)
        if d is None:
            return True
        try:
            state = read(d / "state.json")
            if state.get("state") not in ("waiting", "error", "failed") and not (d / "exit").exists():
                continue
            updated = dt.datetime.fromisoformat(state["updated"].replace("Z", "+00:00"))
            if (dt.datetime.now(dt.timezone.utc) - updated).total_seconds() > 10:
                return True
        except (GoalError, OSError, KeyError, TypeError, ValueError):
            return True
    return False


def _finalize_reviews(s, project):
    """Collect an already approved linked review, never mint watcher approval.

    A lost watcher cannot be reattached here: pending/unknown review needs a
    human, rather than a concurrent approval writer that could clear a gate.
    """
    for author_id, link in s["tasks"].items():
        if "author_id" in link:
            continue
        author = locate(task_state(project), author_id)
        if author is None:
            return "author missing before review recovery"
        reviewers = [rid for rid, row in s["tasks"].items() if row.get("author_id") == author_id]
        marker = read(author / "review-required.json")
        reviewer_id = (marker.get("approval") or {}).get("reviewer_id") if marker.get("status") == "approved" else None
        if reviewer_id not in reviewers:
            return "required opposite-provider approval not linked"
        reviewer = locate(task_state(project), reviewer_id)
        if reviewer is None or any((reviewer / name).is_symlink() for name in ("meta.json", "result.md", "review-of")):
            return "reviewer evidence unavailable or unsafe"
        try:
            a, r = read(author / "meta.json"), read(reviewer / "meta.json")
            marker = read(author / "review-required.json")
            if a.get("id") != author_id or r.get("id") != reviewer_id or a.get("worker") not in ("pi", "claude") or r.get("worker") != {"pi": "claude", "claude": "pi"}[a["worker"]] or (reviewer / "review-of").read_text().strip() != author_id:
                return "reviewer provenance mismatch"
            if (author / "pending-capability.json").exists() or (reviewer / "pending-capability.json").exists():
                return "human capability or trust decision pending"
            if marker.get("status") != "approved":
                return "required review not approved by the owning watcher; human reconciliation needed after reload"
            if not approved_delegated_review(author, read(author / "meta.json"), read(author / "review-required.json")):
                return "independent review missing or stale"
            if not (author / "completed").exists():
                root = Path(__file__).resolve().parent.parent
                result = subprocess.run([str(root / "bin" / "tp-contract"), "collect", author_id],
                                        env={**os.environ, "TP_STATE_DIR": str(task_state(project))},
                                        capture_output=True, text=True, timeout=20)
                if result.returncode != 0 or result.stdout.strip() != "completed":
                    return "canonical collect unavailable: " + (result.stderr or result.stdout).strip()[:300]
        except (GoalError, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
            return "review recovery unavailable: " + str(exc)
    return None


def _reviews(s, project):
    for task_id in s["tasks"]:
        d = locate(task_state(project), task_id)
        if d is None or any((d / name).is_symlink() for name in ("meta.json", "review-required.json", "result.md", "pending-capability.json")):
            return "linked task missing or unsafe"
        if (d / "pending-capability.json").exists():
            return "human capability/trust decision pending"
        try:
            if (d / "review-of").exists():
                if (d / "review-of").is_symlink() or (d / "review-of").read_text().strip() not in s["tasks"]:
                    return "orphaned or unsafe reviewer"
                # Author approval below validates the opposite-provider reviewer,
                # its report hash, round nonce and current author-result hash.
                continue
            meta = read(d / "meta.json")
            marker = read(d / "review-required.json")
            if not approved_delegated_review(d, meta, marker):
                return "required independent task review unavailable or stale"
            if (d / "completed").is_symlink() or not (d / "completed").is_file():
                return "canonical tp-contract collect pending for reviewed task"
        except (GoalError, OSError, ValueError, TypeError):
            return "task review evidence unavailable"
    return None


# High-consequence and external actions are never included in goal continuation.
_FORBIDDEN = re.compile(r"\b(?:push|deploy|release|publish|production|migrate|drop\s+(?:table|database)|delete\s+(?:data|backups?)|rm\s+-rf|new\s+mcp|install\s+(?:packages?|dependencies)|grant\s+(?:trust|permission)|external\s+write)\b", re.I)


def gate_input(project, ident, ticket, spec_sha256):
    project = project_root(project)
    with locked(project, ident) as path:
        s = load(path, project, ident)
        if (s["status"] != "active" or s["spec_sha256"] != spec_sha256 or
                not isinstance(ticket, str) or s["inflight"] is None or s["inflight"]["ticket"] != ticket or
                (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(s["started"])).total_seconds() >= s["spec"]["budget"]["seconds"] or
                any(s["usage"][k] >= s["spec"]["budget"][k] for k in s["usage"])):
            raise GoalError("goal ticket, approved scope or remaining budget invalid")
        if _unlinked(s, project) or _unsafe_followup(project, s["tasks"]):
            raise GoalError("goal has an unknown task or live follow-up candidate")
        return {"id": ident, "ticket": ticket, "spec_sha256": s["spec_sha256"], "workdir": str(project),
                "scope": s["spec"]["scope"], "allowed_paths": s["spec"].get("allowed_paths", []), "done": s["spec"]["done"]}


def pending_task(project, ident, ticket):
    project = project_root(project)
    with locked(project, ident) as path:
        s = load(path, project, ident)
        if not s["inflight"] or s["inflight"]["ticket"] != ticket:
            raise GoalError("goal turn does not own task wait")
        authors = [task_id for task_id, row in s["tasks"].items() if row.get("ticket") == ticket and "author_id" not in row]
        if len(authors) > 1:
            raise GoalError("multiple goal authors for one turn; human decision required")
        return {"task_id": authors[0] if authors and _live_task(project, authors[0]) else None}


def gate_attempt(project, ident, ticket, spec_sha256, main, decision, preflight, policy):
    authority = gate_input(project, ident, ticket, spec_sha256)
    from automation import destructive_or_high, sensitive, review_requirement
    if not all(isinstance(v, dict) for v in (main, decision, preflight, policy)):
        raise GoalError("unknown contract/route/policy")
    if main.get("action") != "delegate" or main.get("mode") != "delegated" or decision.get("placement") != "subagent" or decision.get("worker") not in ("pi", "claude") or not isinstance(decision.get("model"), str) or not decision["model"]:
        raise GoalError("goal edit attempts require an R5 delegated main contract")
    if main.get("no_edits") not in (True, False) or preflight.get("stop") is not False:
        raise GoalError("ambiguous edit/stop boundary")
    for value in (decision.get("task"), main.get("task_summary")):
        text(value)
        if _FORBIDDEN.search(value):
            raise GoalError("external/destructive/trust action requires a separate human decision")
    if destructive_or_high(main["task_summary"], decision, preflight) or sensitive(main["task_summary"], decision, preflight):
        raise GoalError("sensitive or consequential attempt needs a human")
    if policy.get("mode") in ("policy_unavailable", "router_unavailable") or policy.get("confirmation_required") or policy.get("review", {}).get("risk") == "high":
        raise GoalError("R5 policy uncertain or confirmation required")
    if not main["no_edits"]:
        if not authority["allowed_paths"] or preflight.get("no_edits") is True:
            raise GoalError("edit task lacks approved paths or preflight disallows edits")
        if ".." in main["task_summary"] or re.search(r"(?:^|\s)/[A-Za-z0-9_.-]+", main["task_summary"]):
            raise GoalError("absolute or parent-relative path in edit task")
        mentioned = re.findall(r"(?<![\w./])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,12})", main["task_summary"])
        if not mentioned or any(not any(p == allowed or p.startswith(allowed.rstrip("/") + "/") for allowed in authority["allowed_paths"]) for p in mentioned):
            raise GoalError("edit task must name only paths within approved path prefixes")
    # Laya selects provider/effort; it NEVER supplies scope or review authority.
    review = review_requirement(main["task_summary"], {**decision, "review": "required"}, preflight)
    if review["risk"] == "high":
        raise GoalError("high-risk route requires a human")
    return {**authority, "review": "required", "risk": review["risk"], "no_edits": main["no_edits"],
            "notice": "Explicit approved goal continuation, not an enabled automatic routing cell; R5 contract and opposite-provider review remain mandatory"}


def command(project, ident, action, **kw):
    project = project_root(project)
    with locked(project, ident) as path:
        s = load(path, project, ident)
        if action == "status":
            before_usage = dict(s["usage"])
            before_status = s["status"]
            meter_error = _meter_tasks(s, project)
            if (s["status"] == "active" and s["inflight"] is None) or s["status"] == "complete":
                problem = meter_error or _unlinked(s, project) or (s["status"] == "complete" and _reviews(s, project))
                if problem:
                    s["status"] = "suspended"
                    s["reason"] = str(problem)
                    log(s, "suspend", s["reason"])
            if s["status"] == "active" and s["inflight"] is None and limit(s):
                s["status"] = "budget_limited"
                s["reason"] = limit(s)
                log(s, "limit", s["reason"])
            if before_usage != s["usage"] or before_status != s["status"]:
                atomic(path, s)
            return s
        if action == "start":
            if s["status"] != "proposed" or kw.get("confirm") != "START-GOAL" or kw.get("approve_sha256") != s["spec_sha256"]:
                raise GoalError("explicit human approval bound to spec digest required")
            s["started"] = utc()
            s["status"] = "active"
            s["reason"] = "Awaiting first adaptive main turn"
        elif action == "reconcile":
            _reconcile(s, project)
        elif action == "pause":
            if s["status"] not in ("active", "waiting", "paused"):
                raise GoalError("cannot pause")
            s["status"] = "paused"
            s["reason"] = text(kw.get("reason"))
        elif action == "stop":
            if s["status"] in ("complete", "stopped"):
                return s
            s["status"] = "stopped"
            s["reason"] = "Human stopped future admissions; any in-flight work needs separate cancellation"
        elif action == "resolve":
            if s["status"] not in ("suspended", "paused") or s["inflight"] is None or kw.get("confirm") != "RESOLVE-GOAL-TURN":
                raise GoalError("human resolution of an unknown in-flight turn required")
            evidence = text(kw.get("evidence"))
            if _unlinked(s, project) or _unsafe_followup(project):
                raise GoalError("worker/handle remains possible; resolve it through normal task review first")
            log(s, "human-turn-resolution", {"ticket": s["inflight"]["ticket"], "evidence": evidence})
            s["inflight"] = None
            s["reason"] = "Human resolved unknown turn: " + evidence
        elif action == "resume":
            if s["status"] not in ("paused", "blocked", "suspended") or s["inflight"] is not None or s["wait_for"] is not None or kw.get("confirm") != "RESUME-GOAL":
                raise GoalError("human resume and reconciliation of in-flight work required")
            text(kw.get("diagnosis"))
            if limit(s):
                raise GoalError("budget exhausted; no automatic expansion")
            s["status"] = "active"
            s["blocker"] = None
            s["blocker_count"] = 0
            s["reason"] = "Human resumed: " + kw["diagnosis"]
        elif action == "poll":
            if s["status"] not in ("waiting", "paused", "budget_limited") or not s["wait_for"]:
                raise GoalError("no owned wait handle")
            error = _meter_tasks(s, project)
            if error:
                s["status"] = "suspended"
                s["reason"] = error
            else:
                recovery = _finalize_reviews(s, project) if any(row.get("author_id") == s["wait_for"] for row in s["tasks"].values()) else None
                problem = _unlinked(s, project) or recovery or _reviews(s, project)
                if not problem:
                    s["wait_for"] = None
                    if s["status"] == "waiting":
                        s["status"] = "active"
                    s["reason"] = "Wait handle settled with required independent review and canonical collect"
                elif (_live_task(project, s["wait_for"]) or any(_live_task(project, rid) for rid, row in s["tasks"].items() if row.get("author_id") == s["wait_for"])) and not _stale_review_wait(s, project):
                    s["reason"] = "Verified live task/reviewer handle; waiting for binding review: " + problem
                else:
                    s["status"] = "suspended"
                    s["reason"] = "Wait handle or review unavailable: " + problem
            if s["status"] == "waiting" and limit(s):
                s["status"] = "budget_limited"
                s["reason"] = limit(s)
        elif action == "admit":
            if s["status"] != "active" or s["inflight"] is not None:
                raise GoalError("not idle/active; unknown live handle must not be restarted")
            review_problem = _unlinked(s, project) or _meter_tasks(s, project) or _reviews(s, project) or _unsafe_followup(project, s["tasks"])
            if review_problem:
                s["status"] = "suspended"
                s["reason"] = review_problem + "; no new turn until human reconciliation"
            elif limit(s):
                s["status"] = "budget_limited"
                s["reason"] = limit(s)
            else:
                s["attempts"] += 1
                s["inflight"] = {"ticket": uuid.uuid4().hex, "at": utc()}
                s["reason"] = "Main Orient/Frame turn admitted; dispatch still requires R5 policy"
        elif action == "usage":
            ticket = kw.get("ticket")
            event_id = kw.get("event_id")
            if not isinstance(ticket, str) or not isinstance(event_id, str) or not IDENT.fullmatch(event_id):
                raise GoalError("invalid usage identity")
            if event_id in s["events"]:
                if s["events"][event_id] != {k: kw[k] for k in ("ticket", "turns", "tokens", "cost_micros")}:
                    raise GoalError("usage event replay mismatch")
                return s
            if not s["inflight"] or s["inflight"]["ticket"] != ticket or s["status"] not in ("active", "paused", "budget_limited", "stopped"):
                raise GoalError("usage without owned in-flight turn")
            data = {k: kw[k] for k in ("ticket", "turns", "tokens", "cost_micros")}
            if any(type(data[k]) is not int or not 0 <= data[k] <= 10**9 for k in ("turns", "tokens", "cost_micros")):
                raise GoalError("invalid reporter usage")
            for k in ("turns", "tokens", "cost_micros"):
                s["usage"][k] += data[k]
            s["events"][event_id] = data
            if limit(s) and s["status"] == "active":
                s["status"] = "budget_limited"
                s["reason"] = limit(s)
        elif action == "link-task":
            if s["inflight"] is None or s["inflight"]["ticket"] != kw.get("ticket") or s["status"] not in ("active", "paused", "budget_limited"):
                raise GoalError("only current turn can link a policy-authorized task")
            task_id = kw.get("task_id")
            if not isinstance(task_id, str) or not IDENT.fullmatch(task_id):
                raise GoalError("invalid task id")
            if task_id in s["tasks"]:
                if s["tasks"][task_id].get("ticket") == kw["ticket"]:
                    return s
                raise GoalError("task already linked to a different turn")
            d = locate(task_state(project), task_id)
            if d is None:
                raise GoalError("task not uniquely present in approved workdir")
            meta = read(d / "meta.json")
            if meta.get("workdir") != str(project):
                raise GoalError("task outside approved workdir")
            if (d / "review-of").exists():
                if (d / "review-of").is_symlink() or (d / "review-of").read_text().strip() not in s["tasks"]:
                    raise GoalError("reviewer must reference an already linked author")
            elif (not all(isinstance(meta.get(k), dict) for k in ("contract", "main_contract", "policy_decision", "preflight")) or
                  meta["contract"].get("kind") != "task" or not isinstance(meta.get("decision"), dict) or meta["decision"].get("review") != "required"):
                raise GoalError("task lacks R5 provenance and required review; goal cannot supply it")
            s["tasks"][task_id] = {"ticket": kw["ticket"], "at": utc(), "metered": {"turns": 0, "tokens": 0, "cost_micros": 0}}
        elif action == "link-reviewer":
            author_id, reviewer_id = kw.get("author_id"), kw.get("reviewer_id")
            if (not isinstance(author_id, str) or author_id not in s["tasks"] or
                    not isinstance(reviewer_id, str) or not IDENT.fullmatch(reviewer_id)):
                raise GoalError("reviewer must reference a linked author")
            if reviewer_id in s["tasks"]:
                if s["tasks"][reviewer_id].get("author_id") == author_id:
                    return s
                raise GoalError("reviewer already linked to another author")
            author = locate(task_state(project), author_id)
            reviewer = locate(task_state(project), reviewer_id)
            if author is None or reviewer is None or (reviewer / "review-of").is_symlink() or (reviewer / "review-of").read_text().strip() != author_id:
                raise GoalError("reviewer/author linkage missing or unsafe")
            a, r = read(author / "meta.json"), read(reviewer / "meta.json")
            if a.get("worker") not in ("pi", "claude") or r.get("worker") != {"pi": "claude", "claude": "pi"}[a["worker"]] or r.get("workdir") != str(project):
                raise GoalError("opposite-provider review in approved cwd required")
            s["tasks"][reviewer_id] = {"ticket": s["tasks"][author_id]["ticket"], "author_id": author_id, "at": utc(),
                                       "metered": {"turns": 0, "tokens": 0, "cost_micros": 0}}
        elif action == "settle":
            if s["inflight"] is None or s["inflight"]["ticket"] != kw.get("ticket"):
                raise GoalError("stale/unknown turn settlement")
            outcome = kw.get("outcome")
            if outcome not in ("progress", "wait", "no_progress", "safety_block", "complete"):
                raise GoalError("unknown outcome")
            evidence = text(kw.get("evidence"))
            blocker = kw.get("blocker")
            if outcome == "wait":
                if not isinstance(blocker, str) or not IDENT.fullmatch(blocker) or blocker not in s["tasks"] or not _live_task(project, blocker):
                    outcome = "safety_block"
                    evidence = "Wait handle missing/ambiguous/unlinked: " + evidence
                elif s["status"] == "active":
                    s["status"] = "waiting"
                    s["wait_for"] = blocker
                    s["reason"] = "Verified live task handle; wait without restarting"
            if outcome == "complete" and s["status"] in ("paused", "stopped"):
                outcome = "progress"  # Human pause/stop outranks completion proposals.
            if outcome == "complete":
                # Codex-style full-scope audit is model evidence, not authority.
                # Durable R5 task/review/collect and inherited handoff gates decide
                # whether that evidence may complete this cooperative goal.
                try:
                    pending_reviews = review_inventory(task_state(project))["blockers"]
                except (OSError, ValueError, TypeError) as exc:
                    pending_reviews = [{"error": str(exc)}]
                review_problem = _unlinked(s, project) or _meter_tasks(s, project) or _reviews(s, project)
                if pending_reviews or review_problem or len(evidence) < 80:
                    outcome = "safety_block"
                    evidence = (review_problem or "required review unresolved or full-scope audit evidence insufficient") + "; " + evidence
                else:
                    s["status"] = "complete"
                    s["reason"] = "Full-scope main audit recorded; all linked work independently reviewed and canonically collected"
            if outcome == "safety_block":
                if s["status"] != "stopped":
                    s["status"] = "suspended"
                s["reason"] = evidence
            elif outcome == "no_progress":
                text(blocker)
                s["blocker_count"] = s["blocker_count"] + 1 if s["blocker"] == blocker else 1
                s["blocker"] = blocker
                if s["blocker_count"] >= 3 and s["status"] == "active":
                    s["status"] = "blocked"
                    s["reason"] = "same ordinary blocker recurred 3 times: " + blocker
            else:
                s["blocker"] = None
                s["blocker_count"] = 0
            if s["status"] == "active":
                task_problem = _unlinked(s, project) or _meter_tasks(s, project) or _reviews(s, project)
                if task_problem:
                    s["status"] = "suspended"
                    s["reason"] = task_problem
            if s["status"] == "active" and limit(s):
                s["status"] = "budget_limited"
                s["reason"] = limit(s)
            log(s, "settle", {"outcome": outcome, "evidence": evidence, "blocker": blocker, "ticket": kw["ticket"]})
            s["inflight"] = None
            kw = {}
        else:
            raise GoalError("unknown action")
        log(s, action, {k: v for k, v in kw.items() if k not in ("confirm", "approve_sha256")})
        atomic(path, s)
        return s
