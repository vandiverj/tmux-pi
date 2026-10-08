"""Worker contracts (M9): bounded file-artifact work or no-edit task replies.

A contract lives in a task's meta.json under "contract":

    {"kind": "research" | "task", "artifact": "/abs/path.md",
     "budget": {"seconds": 1200, "turns": 60 | null, "tokens": 400000 | null},
     "checkpoints": {"outline": 180, "draft": 600}, "stall": 360, "grace": 180,
     "tool_errors": 4}

File-contract workers keep a `Status: outline|draft|final|partial` line at the top
of their artifact. Reply-only no-edit tasks use reporter result.md, not file checkpoints.
`evaluate` compares file status and last write (when applicable) with the clock and
the worker's reported turns, tokens and failed tool calls; `enforce` turns a missed
checkpoint, a stall or a run of failed tool calls into one message to the worker, and
a missed first checkpoint, a stall or failures that go on after the reminder, or an
exceeded budget into a pause (Escape). A stopped or paused worker that settles is
"halted": main offers to continue (one budget extension), narrow, escalate or
discard it. Every threshold is configuration (config/research.json for research),
not model behavior.

`lifecycle` names a task's state for the status surfaces: starting, working,
needs_input, paused, settled, completed, failed, killed or escalated.
"""

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

import request_file
from lifecycle_files import LIFECYCLE_FILES
from task_record import request_text

KINDS = ("research", "task")
STATUSES = ("outline", "draft", "final", "partial")
STATUS_LINE = re.compile(r"^\W*status\W*:?\W*(outline|draft|final|partial)\b", re.I)
URL = re.compile(r"https?://[^\s)>\]]+")
# Providers served from this machine; everything else is a frontier (cloud) model.
LOCAL_PROVIDERS = ("lmstudio", "mlx", "slotstream")
RUNNING = ("starting", "working", "needs_input")
DONE = ("settled", "completed", "paused", "failed", "killed", "stopped", "escalated")
# A dependency (--after) is met by an ended task, never a paused one: paused work is not finished,
# whether or not its reply carries a RESULT_STATUS line.
DEPENDENCY_DONE = tuple(s for s in DONE if s != "paused")
RATINGS = {"sources": ("ok", "weak", "missing"), "cleanup": ("none", "minor", "major"),
           "useful": ("yes", "partly", "no")}
THINKING_RANK = {"off": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}
# Heuristic strength ladders for Slice 4 escalation. This is intentionally a
# small route helper, not the Slice 5 capability registry.
MODEL_STRENGTH = (
    (re.compile(r"claude.*haiku", re.I), 20), (re.compile(r"claude.*sonnet", re.I), 40),
    (re.compile(r"claude.*opus", re.I), 60), (re.compile(r"gpt-5\.3.*spark", re.I), 15),
    (re.compile(r"gpt-5\.5", re.I), 25), (re.compile(r"gpt-5\.6.*luna", re.I), 30),
    (re.compile(r"gpt-5\.6.*terra", re.I), 35), (re.compile(r"gpt-5\.6.*sol", re.I), 40),
    (re.compile(r"gpt-6.*luna", re.I), 45), (re.compile(r"gpt-6.*sol", re.I), 55),
    (re.compile(r"gpt-6.*astra", re.I), 65), (re.compile(r"qwen", re.I), 10),
)
SCOPE_PROFILES = {
    "brief": {"time": "10m", "turns": 30, "outline": "2m", "draft": "6m", "stall": "4m"},
    "sustained": {"time": "25m", "turns": 80, "outline": "5m", "draft": "15m", "stall": "8m"},
    "multi_step": {"time": "45m", "turns": 140, "outline": "8m", "draft": "25m", "stall": "10m"},
}


class ContractError(Exception):
    pass


def duration(text):
    """Seconds in "90s", "20m", "1h" or a bare number of seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(text))
    if not m or int(m.group(1)) <= 0:
        raise ContractError("bad duration: {!r} (use 90s, 20m or 1h)".format(text))
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def quantity(text):
    """An integer from "60", "400k" or "2M"."""
    m = re.fullmatch(r"\s*(\d+)\s*([kKmM]?)\s*", str(text))
    if not m or int(m.group(1)) <= 0:
        raise ContractError("bad number: {!r}".format(text))
    return int(m.group(1)) * {"": 1, "k": 1000, "K": 1000, "m": 1000000, "M": 1000000}[m.group(2)]


def pairs(spec, name):
    """{"time": "20m", ...} from "time=20m,turns=60" (or a dict already)."""
    if isinstance(spec, dict):
        return spec
    out = {}
    for part in filter(None, (p.strip() for p in str(spec).split(","))):
        key, sep, value = part.partition("=")
        if not sep:
            raise ContractError("{} must be key=value pairs: {!r}".format(name, spec))
        out[key.strip()] = value.strip()
    return out


def build(kind, artifact, budget, checkpoints, stall, grace, tool_errors=4, profile=None, no_edits=False):
    """A validated contract; artifact must already be absolute."""
    if not isinstance(no_edits, bool) or (no_edits and kind != "task"):
        raise ContractError("no_edits must be boolean and is only valid for task")
    if kind not in KINDS:
        raise ContractError("contract kind must be research or task")
    if not os.path.isabs(artifact):
        raise ContractError("artifact must be an absolute path")
    if no_edits and os.path.basename(artifact) != "result.md":
        raise ContractError("reply-only task artifact must be reporter result.md")
    budget = pairs(budget, "budget")
    if not set(budget) <= {"time", "turns", "tokens"} or "time" not in budget:
        raise ContractError("budget needs time and may have turns and tokens")
    checkpoints = pairs(checkpoints, "checkpoints")
    if not set(checkpoints) <= {"outline", "draft"}:
        raise ContractError("checkpoints are outline and draft")
    if no_edits and checkpoints:
        raise ContractError("reply-only task cannot have file checkpoints")
    out = {"kind": kind, "artifact": artifact,
           "budget": {"seconds": duration(budget["time"]),
                      "turns": quantity(budget["turns"]) if budget.get("turns") else None,
                      "tokens": quantity(budget["tokens"]) if budget.get("tokens") else None},
           "checkpoints": {k: duration(v) for k, v in checkpoints.items()},
           "stall": duration(stall), "grace": duration(grace), "tool_errors": _count(tool_errors, "tool_errors"),
           "warning_thresholds": {"time": 0.8, "turns": 0.8, "tokens": 0.8}}
    if no_edits:
        out["no_edits"] = True
    if profile:
        out["profile"] = {k: v for k, v in profile.items() if v not in (None, "")}
    times = [out["checkpoints"].get("outline"), out["checkpoints"].get("draft"), out["budget"]["seconds"]]
    times = [t for t in times if t]
    if times != sorted(times) or len(times) != len(set(times)):
        raise ContractError("checkpoints must come in order: outline < draft < time budget")
    return out


def dynamic_terms(kind, decision=None, task_class=None, model=None):
    """Route/task-informed defaults for a contract when the caller did not set stricter ones.

    This is Slice 4's light-weight budget profile, deliberately smaller than a
    capability registry. Explicit contract fields still win.
    """
    decision = decision or {}
    route = decision.get("route") or task_class or "standard"
    scope = decision.get("scope") or ("multi_step" if route in ("complex", "expert") else "sustained")
    if kind == "research":
        scope = "sustained" if scope == "brief" else scope
    base = dict(SCOPE_PROFILES.get(scope, SCOPE_PROFILES["sustained"]))
    m = model or decision.get("model") or ""
    # Local models often need more wall-clock/tool time for bounded work, but
    # they still get hard caps; stronger routes get a little more room for larger tasks.
    multiplier = 1.35 if is_local(m) else 1.15 if route in ("complex", "expert") else 1.0
    max_seconds = 3600 if kind == "research" else 2700
    seconds = min(max_seconds, max(300, round(duration(base["time"]) * multiplier)))
    turns = max(12, round(int(base["turns"]) * multiplier))
    outline = max(60, min(seconds - 120, round(duration(base["outline"]) * multiplier)))
    draft = max(outline + 60, min(seconds - 60, round(duration(base["draft"]) * multiplier)))
    return {"budget": {"time": "{}s".format(seconds), "turns": turns},
            "checkpoints": {"outline": "{}s".format(outline), "draft": "{}s".format(draft)},
            "stall": "{}s".format(min(max(180, round(duration(base["stall"]) * multiplier)), seconds - 60)),
            "grace": "180s", "profile": {"source": "dynamic", "route": route, "scope": scope,
                                           "intent": decision.get("intent"), "model": m}}


def _count(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractError("{} must be a positive integer".format(name))
    return value


def is_local(model):
    return str(model or "").split("/")[0] in LOCAL_PROVIDERS


def load_research(path, roster=None):
    """config/research.json: the research route's worker, contract defaults and caps.

    roster is router.json's models list; when given, worker, model and thinking must match an entry.
    """
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("cannot read research config: {}".format(exc)) from exc
    config = {k: v for k, v in config.items() if not k.startswith("_")}
    keys = {"worker", "model", "thinking", "escalate", "budget", "checkpoints", "stall", "grace", "tool_errors",
            "max_frontier", "max_local", "max_active"}
    if set(config) != keys:
        raise ContractError("research config needs exactly {}".format(", ".join(sorted(keys))))
    build("research", "/x", config["budget"], config["checkpoints"], config["stall"], config["grace"],
          config["tool_errors"])
    for key in ("max_frontier", "max_local", "max_active"):
        _count(config[key], key)
    escalate = config["escalate"]
    if not isinstance(escalate, dict) or set(escalate) != {"worker", "model", "thinking"}:
        raise ContractError("escalate needs worker, model and thinking")
    for route in (config, escalate):
        if roster is not None and not any(item["worker"] == route["worker"] and item["model"] == route["model"] and
                                          route["thinking"] in item["thinking"] for item in roster):
            raise ContractError("research worker, model and thinking ({}) must match the router roster".format(
                route["model"]))
    return config


def _clock(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "{}s".format(seconds)
    if seconds < 3600:
        return "{}m{:02d}s".format(seconds // 60, seconds % 60)
    return "{}h{:02d}m".format(seconds // 3600, seconds % 3600 // 60)


def _minutes(seconds):
    return "{} minute{}".format(round(seconds / 60), "" if round(seconds / 60) == 1 else "s") if seconds >= 60 \
        else "{} seconds".format(seconds)


def note(contract, started):
    """The packet note that states the contract to the worker."""
    at = lambda s: datetime.datetime.fromtimestamp(started + s).strftime("%H:%M")
    c, b = contract["checkpoints"], contract["budget"]
    limit = "within {} (by {})".format(_minutes(b["seconds"]), at(b["seconds"]))
    extra = [x for x in ("{} turns".format(b["turns"]) if b["turns"] else "",
                         "{} tokens".format(b["tokens"]) if b["tokens"] else "") if x]
    if contract.get("no_edits") and contract["kind"] == "task":
        return ("Contract: reply-only; do not write an artifact or edit files. Answer in your reply; "
                "the worker reporter records result.md. Finish {l}{x}. If you reach the limit, stop: "
                "reply with what you have, say what is missing, and ask whether to continue, escalate, "
                "narrow the question, or discard it.").format(
            l=limit, x=" and within {}".format(" and ".join(extra)) if extra else "")
    steps = []
    if "outline" in c:
        steps.append("by {} an outline and the first sources (`Status: outline`)".format(at(c["outline"])))
    if "draft" in c:
        steps.append("by {} a usable draft (`Status: draft`)".format(at(c["draft"])))
    text = ("Contract: write your output to {a} and keep a status line at its top: `Status: outline`, then "
            "`Status: draft`, then `Status: final`. Checkpoints: {s}. Finish {l}{x}. "
            "If you reach the limit, stop: save what you have with `Status: partial`, say what is missing, and ask "
            "whether to continue, escalate, narrow the question, or discard it. tmux-pi checks the file at each "
            "checkpoint and pauses a worker that misses the first one or runs over.").format(
        a=contract["artifact"], s="; ".join(steps) or "none", l=limit,
        x=" and within {}".format(" and ".join(extra)) if extra else "")
    if contract["kind"] == "research":
        text += (" Research: answer the one question in the task. Write the outline/initial source table in the "
                 "artifact before exploring broadly or running long searches. "
                 "Prefer primary sources (official documentation, papers, source repositories, standards), then "
                 "reputable secondary ones. Label each claim as a source fact (with its URL or path and the date you "
                 "accessed it), an inference, or a hypothesis. Only a source you fetched or read in this session is a "
                 "source fact; a claim you know from memory is background knowledge, labeled so, with no access "
                 "date. A report without source URLs or paths and access dates is not complete.")
    return text


def artifact_info(path):
    """(exists, size, mtime, status, urls) for an artifact file."""
    try:
        stat = os.stat(path)
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(400000)
    except OSError:
        return {"exists": False, "size": 0, "mtime": None, "status": None, "urls": 0}
    status = None
    for line in text.splitlines()[:8]:
        m = STATUS_LINE.match(line)
        if m:
            status = m.group(1).lower()
            break
    return {"exists": stat.st_size > 0, "size": stat.st_size, "mtime": stat.st_mtime, "status": status,
            "urls": len({u.rstrip(".,;:!?'\"") for u in URL.findall(text)})}


def _epoch(stamp):
    try:
        return datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def evaluate(meta, state, art, now, alive, events=()):
    """Checkpoint state and the one action due now (or None) for a task with a contract.

    Actions while the worker is working: nudge-outline, nudge-draft, nudge-stall,
    nudge-errors (one reminder each), stop (budget reached: ask the worker to save and
    ask), pause (Escape: first checkpoint missed by the grace period; still working the
    grace period after the stop message, a stall reminder or a failed-tools reminder).
    Once stopped or paused and no longer working: halted, for main to offer what next.
    Nothing is typed into a paused worker, and nothing but the pause after a stop. A
    Claude worker reports no Stop when interrupted, so a paused worker whose tool count
    has not moved since the pause counts as not working.
    Each is taken once (events holds the ones already taken); an `extend` event (main's
    "continue") replaces the budget and lets stop, pause and halted happen again.
    """
    c = meta["contract"]
    events = [e if isinstance(e, dict) else {"action": e, "at": 0} for e in events]
    extends = [e for e in events if e["action"] == "extend"]
    since_extend = events[events.index(extends[-1]) + 1:] if extends else events
    created = _epoch(meta.get("created")) or now
    pause = next((e for e in reversed(since_extend) if e["action"] == "pause"), None)
    # A Claude worker reports no Stop when interrupted: paused and idle if its tool count has not moved.
    idle = bool(pause) and "tools" in pause and state.get("state") in ("working", "tool") and \
        (state.get("tools") or 0) <= pause["tools"]
    settled = not alive or state.get("state") in ("waiting", "error")
    end = pause["at"] if idle else (_epoch(state.get("updated")) or now) if settled else now
    elapsed = max(0, end - created)
    turns = state.get("turns") or 0
    tokens = sum((state.get("tokens") or {}).get(k, 0) for k in ("input", "output", "cacheRead", "cacheWrite"))
    b, cp = (extends[-1]["budget"] if extends else c["budget"]), c["checkpoints"]
    reply_only = c.get("kind") == "task" and c.get("no_edits") is True
    status = "reply-only" if reply_only else art["status"] if art["exists"] else None
    has_outline = art["exists"] if not reply_only else False
    has_draft = status in ("draft", "final", "partial")
    over = [what for what, hit in (("time", elapsed >= b["seconds"]),
                                   ("turns", bool(b["turns"]) and turns >= b["turns"]),
                                   ("tokens", bool(b["tokens"]) and tokens >= b["tokens"])) if hit]
    warn_at = c.get("warning_thresholds") or {}
    warnings = [what for what, value, limit in (("time", elapsed, b["seconds"]), ("turns", turns, b["turns"]),
                                                ("tokens", tokens, b["tokens"]))
                if limit and what not in over and value >= float(warn_at.get(what, 0.8)) * limit]
    # The stall clock restarts at main's "continue".
    write_age = now - max(art["mtime"], extends[-1]["at"] if extends else 0) if art["mtime"] and not reply_only else None

    if reply_only:
        phase = "reply-only; " + ("over budget ({})".format(", ".join(over)) if over else
                                 "limit in {}".format(_clock(b["seconds"] - elapsed)))
    elif status == "final":
        phase = "final"
    elif over:
        phase = "over budget ({})".format(", ".join(over))
    elif "outline" in cp and not has_outline and elapsed >= cp["outline"]:
        phase = "outline missed"
    elif "draft" in cp and not has_draft and elapsed >= cp["draft"]:
        phase = "draft missed"
    elif has_outline and write_age is not None and write_age >= c["stall"] and not settled:
        phase = "no new writes for {}".format(_clock(write_age))
    elif "outline" in cp and not has_outline:
        phase = "outline due in {}".format(_clock(cp["outline"] - elapsed))
    elif "draft" in cp and not has_draft:
        phase = "draft due in {}".format(_clock(cp["draft"] - elapsed))
    else:
        phase = "limit in {}".format(_clock(b["seconds"] - elapsed))

    action = None
    working = alive and state.get("state") in ("working", "tool") and not idle
    taken = {e["action"] for e in events}
    recent = {e["action"] for e in since_extend}
    errors, error_limit = state.get("toolErrors") or 0, c.get("tool_errors", 4)
    stall_key = "nudge-stall@{}".format(int(art["mtime"])) if art["mtime"] and not reply_only else None
    if working and status != "final" and "pause" not in recent:
        if over and "stop" not in recent:
            action = ("stop", "{} budget used up".format(" and ".join(over)))
        elif over and "pause" not in recent and _since(since_extend, "stop", now) >= c["grace"]:
            action = ("pause", "still working {} after the stop message".format(_clock(c["grace"])))
        elif not reply_only and "outline" in cp and not has_outline and elapsed >= cp["outline"] + c["grace"] and "pause" not in taken:
            action = ("pause", "no outline in {} after its checkpoint".format(_clock(elapsed - cp["outline"])))
        elif errors >= error_limit and "nudge-errors" in recent and "pause" not in recent and \
                _since(since_extend, "nudge-errors", now) >= c["grace"]:
            action = ("pause", "{} failed tool calls in a row after a reminder".format(errors))
        elif not reply_only and stall_key in recent and "pause" not in recent and write_age >= c["stall"] + c["grace"] and \
                _since(since_extend, stall_key, now) >= c["grace"]:
            action = ("pause", "no new writes for {} after a reminder".format(_clock(write_age)))
        elif "stop" in recent:
            pass
        elif errors >= error_limit and "nudge-errors" not in recent:
            action = ("nudge-errors", "{} failed tool calls in a row".format(errors))
        elif not reply_only and "outline" in cp and not has_outline and elapsed >= cp["outline"] and "nudge-outline" not in taken:
            action = ("nudge-outline", "outline checkpoint reached with nothing written")
        elif not reply_only and "draft" in cp and not has_draft and elapsed >= cp["draft"] and "nudge-draft" not in taken:
            action = ("nudge-draft", "draft checkpoint reached")
        elif not reply_only and has_outline and write_age is not None and write_age >= c["stall"] and stall_key not in taken:
            action = (stall_key, "no new writes for {}".format(_clock(write_age)))
    elif alive and not working and state.get("state") != "permission" and "halted" not in recent:
        why = next((e for e in reversed(since_extend) if e["action"] in ("stop", "pause")), None)
        if why:
            saved = status in ("partial", "final")
            action = ("halted", "{} ({}); {}".format(
                "paused" if why["action"] == "pause" else "stopped", why.get("reason", ""),
                "reply-only; reporter result.md is the record" if reply_only else
                "report saved with Status: {}".format(status) if saved else
                "no partial report saved ({})".format("left at Status: {}".format(status) if status else
                                                      "file has no status line" if art["exists"] else "nothing written")))
    return {"elapsed": elapsed, "turns": turns, "tokens": tokens, "status": status, "phase": phase,
            "write_age": write_age, "over": over, "warnings": warnings, "action": action, "budget": b}


def _since(events, action, now):
    times = [e.get("at", 0) for e in events if isinstance(e, dict) and e.get("action") == action]
    return now - max(times) if times else -1


def extension(meta, state, now, events):
    """main's "continue": a new budget of the original size, counted from now. Once per task."""
    c = meta["contract"]
    if any(e.get("action") == "extend" for e in events):
        raise ContractError("{} was already given more budget once; narrow, escalate or discard it".format(meta.get("id")))
    r = evaluate(meta, state, {"exists": False, "size": 0, "mtime": None, "status": None, "urls": 0}, now, False, events)
    b = c["budget"]
    return {"action": "extend", "at": now,
            "budget": {"seconds": int(max(r["elapsed"], 0) + b["seconds"]),
                       "turns": r["turns"] + b["turns"] if b["turns"] else None,
                       "tokens": r["tokens"] + b["tokens"] if b["tokens"] else None}}


RESULT_STATUS_RE = re.compile(r"(?m)^RESULT_STATUS:\s*(complete|empty|blocked|incomplete)\b", re.I)


def recorded_pause(state, events=()):
    """Is a pause on record for this task and not yet cleared by a resume?

    `lifecycle` consults this before it looks at an exit code, a pane or a result status.
    A pause is on record when state.json says "paused" or the last
    pause/extend checkpoint event is a pause. It is cleared by a later `extend` (main's
    "continue"), or by the worker visibly working again: state "working"/"tool" with a tool
    count past the one the pause recorded. Nothing else clears it, in particular not an exit
    code, a dead or missing pane, or a RESULT_STATUS line: a paused task is not finished."""
    state = state if isinstance(state, dict) else {}
    if state.get("state") == "paused":
        return True
    events = [e for e in events if isinstance(e, dict)] if isinstance(events, (list, tuple)) else []
    last = next((e for e in reversed(events) if e.get("action") in ("pause", "extend")), None)
    if not last or last["action"] != "pause":
        return False
    tools = last.get("tools")
    working_again = state.get("state") in ("working", "tool") and isinstance(tools, (int, float)) and \
        (state.get("tools") or 0) > tools
    return not working_again


def lifecycle(state, pane=None, events=(), reply="", completed_at=None, exit_code=None, escalated=False, replied_at=None,
              stopped=False, collected=False, asking=False, capability_pending=False):
    """A task's state for people, decided in this one place for tp-agent, tp-contract and tp-review:
    starting, working, needs_input, paused, settled, completed, failed, killed, stopped or escalated.

    Plain inputs only. pane is "alive", "dead" (process exited), "gone", or None when the caller
    did not look at panes (tp-agent), which is read as present. exit_code is the exit file's text,
    None when there is none. reply is result.md's text; replied_at its write time (state.json can
    be touched later by an idle notification, so completed compares against the reply).
    escalated and stopped are main's explicit hand-off markers; collected and completed_at are the
    two hand-off records; asking is a pending permission or capability request, capability_pending
    a capability waiting for human approval.

    Order: main's own markers (escalated, stopped) first. Then paused, before any pane or exit
    code is read: a recorded pause (recorded_pause), or a reply without a RESULT_STATUS line from
    a worker that is not visibly at work. A paused task is not finished, so no exit code, dead or
    missing pane or hand-off record turns it into a state that satisfies --after."""
    state = state if isinstance(state, dict) else {}
    life = _lifecycle(state, pane, [e for e in events if isinstance(e, dict)] if isinstance(events, (list, tuple)) else [],
                      reply or "", completed_at, exit_code, escalated, replied_at, stopped, collected)
    # A paused task stays paused when a request coexists with the pause: the request is noted on its row
    # (permission, capability_request), but the state, and so the index tier, is decided by the pause alone.
    if capability_pending and life not in ("failed", "killed", "stopped", "escalated", "paused"):
        return "needs_input"
    if asking and life in RUNNING:
        return "needs_input"
    return life


def satisfies_dependency(life, state=None, events=(), reply=""):
    """May a task in lifecycle state `life` be the predecessor of a --after dependent? The one answer, used by
    tp-agent (queue admission) and tp-contract (slot).

    Never when a pause is on record or the reply lacks a RESULT_STATUS line, whatever state is reported:
    main's own markers (stopped, escalated) outrank the reported state but not this. Otherwise the ended
    states in DEPENDENCY_DONE satisfy it. A task with no reply at all (killed, failed) has nothing to be
    unstatused about and keeps satisfying."""
    if life not in DEPENDENCY_DONE:
        return False
    if recorded_pause(state, events):
        return False
    reply = reply if isinstance(reply, str) else ""
    return not (reply.strip() and not RESULT_STATUS_RE.search(reply))


def _lifecycle(state, pane, events, reply, completed_at, exit_code, escalated, replied_at, stopped, collected):
    if escalated:
        return "escalated"
    if stopped:
        return "stopped"
    s = state.get("state")
    live = pane not in ("dead", "gone") and exit_code is None
    if recorded_pause(state, events):
        return "paused"
    statused = bool(RESULT_STATUS_RE.search(reply))
    at_work = live and s in (None, "starting", "working", "tool", "permission")
    if reply.strip() and not statused and not at_work:
        # Replied without a result status: paused, whatever the pane, the exit code or a trailing question say.
        return "paused"
    if pane == "gone":
        return "killed"
    updated = _epoch(state.get("updated")) or 0
    done = collected or bool(completed_at and completed_at >= (replied_at or updated) - 1)
    if not live:
        # The process ended. Only a clean exit that left a reply is a result.
        if str(exit_code) != "0" or not reply.strip():
            return "failed"
        return "completed" if done else "settled"
    if s in (None, "starting"):
        return "starting"
    if s in ("working", "tool"):
        return "working"
    if s == "permission":
        return "needs_input"
    if s == "error":
        return "failed"
    if reply.rstrip().endswith("?"):
        return "needs_input"
    if not statused:
        return "paused"
    return "completed" if done else "settled"


# Every file task_lifecycle reads is in lifecycle_files.LIFECYCLE_FILES, which it refuses to go beyond.
REQUEST_FILES = request_file.REQUEST_NAMES


def task_lifecycle(d, pane=None):
    """(lifecycle state, state.json, checkpoint events, reply) for a task directory.

    The one reader of a task's files for `lifecycle`, used by tp-agent and tp-contract alike, so
    both hand it the same facts; pane is the only fact not on disk. The files it reads are LIFECYCLE_FILES and no others.
    A request file that cannot be read as one JSON object counts as a pending request (lib/request_file.py)."""
    d = Path(d)

    def path(name):
        if name not in LIFECYCLE_FILES:
            raise ValueError("task_lifecycle read {!r}, which is not in LIFECYCLE_FILES".format(name))
        return d / name

    def obj(name):
        try:
            value = json.loads(path(name).read_text())
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def text(name):
        try:
            return path(name).read_text().strip()
        except (OSError, UnicodeDecodeError):  # unreadable counts as absent here; a review's verdict never comes from this reader
            return ""

    state, events, reply = obj("state.json"), obj("checkpoints.json").get("events", []), text("result.md")
    try:
        completed_at = float(text("completed"))
    except ValueError:
        completed_at = None
    try:
        replied_at = path("result.md").stat().st_mtime
    except OSError:
        replied_at = None
    life = lifecycle(state, pane, events, reply, completed_at, text("exit") or None, path("escalated").exists(), replied_at,
                     stopped=path("stopped.json").exists(), collected=path("collected.json").exists(),
                     asking=any(request_file.is_pending(path(name)) for name in REQUEST_FILES),
                     capability_pending=path("pending-capability.json").exists())
    return life, state, events, reply


def summary_line(reply, question=False):
    """One line of a reply: its question when it asks one, else its first line."""
    lines = [l.strip() for l in reply.splitlines() if l.strip()]
    if not lines:
        return ""
    return lines[-1] if question else lines[0]


def message(action, contract):
    """What tmux-pi types into the worker for an action (None for a bare pause)."""
    a = contract["artifact"]
    if action == "nudge-outline":
        return ("tmux-pi checkpoint: the outline is due and {} is empty. Write your outline and the sources you have "
                "so far there now, with `Status: outline` on top, then continue.".format(a))
    if action == "nudge-draft":
        return ("tmux-pi checkpoint: the draft is due. Bring {} to a usable draft now and set `Status: draft`, "
                "then continue.".format(a))
    if action.startswith("nudge-stall"):
        return ("tmux-pi checkpoint: {} has not changed for a while. Add what you have found since, or if you are "
                "not finding evidence, narrow the question and say so in the file.".format(a))
    if action == "stop" and contract.get("kind") == "task" and contract.get("no_edits") is True:
        return ("tmux-pi: your budget is used up. Stop now: reply with what you have, list what is "
                "missing, and ask whether to continue, escalate, narrow the question, or discard it. "
                "Do not write files; the reporter records result.md.")
    if action == "stop":
        return ("tmux-pi: your budget is used up. Stop now: save {} with `Status: partial` (or `Status: final` if it "
                "is complete), list the evidence still missing, and ask whether to continue, escalate, narrow the "
                "question, or discard it.".format(a))
    return None


def _provider(worker, model):
    return str(model or "").split("/")[0] if "/" in str(model or "") else ("claude" if worker == "claude" or str(model).startswith("claude") else "")


def _family(model):
    m = str(model or "").lower()
    if "claude" in m:
        return "claude"
    if "gpt-6" in m:
        return "gpt-6"
    if "qwen" in m:
        return "qwen"
    return re.sub(r"[-_/]?(luna|terra|sol|astra|haiku|sonnet|opus|spark)\b.*", "", m)


def _model_score(model):
    return next((score for pattern, score in MODEL_STRENGTH if pattern.search(str(model or ""))), None)


def _strength(model, thinking):
    base = _model_score(model)
    rank = THINKING_RANK.get(str(thinking or ""))
    if base is None or rank is None:
        return None
    return base * 10 + rank


def _local_escalation_qualified(item):
    q = item.get("qualification") if isinstance(item.get("qualification"), dict) else {}
    return bool(item.get("qualified_local_escalation") or item.get("local_escalation") == "qualified" or
                q.get("state") in ("qualified", "fixture-qualified"))


def select_escalation(source, roster, preferred=None):
    """A qualified one-step escalation target from the existing route roster.

    Unknown source strength is fail-closed. Prefer the least stronger compatible
    same-provider/family profile, then same provider, then qualified cross-provider.
    Thinking must not be downgraded to off/minimal, and local-to-local escalation
    requires an explicit roster qualification flag.
    """
    roster = [r for r in roster if isinstance(r, dict)]
    src = {"worker": source.get("worker"), "model": source.get("model"), "thinking": source.get("thinking")}
    src_provider, src_family = _provider(src["worker"], src["model"]), _family(src["model"])
    src_strength = _strength(src["model"], src["thinking"])
    src_rank = THINKING_RANK.get(str(src["thinking"] or ""))
    if src_strength is None or src_rank is None:
        return None
    min_target_rank = max(src_rank, THINKING_RANK["low"])
    src_local = is_local(src["model"])

    def entries():
        for item in roster:
            target_local = is_local(item.get("model"))
            if src_local and target_local and not _local_escalation_qualified(item):
                continue
            for thinking in item.get("thinking", []):
                target = {"worker": item.get("worker"), "model": item.get("model"), "thinking": thinking}
                if target["model"] == src["model"]:
                    continue
                target_rank = THINKING_RANK.get(str(thinking or ""))
                target_strength = _strength(target["model"], target["thinking"])
                if target_rank is None or target_strength is None:
                    continue
                if target_rank < min_target_rank:
                    continue
                if target_strength > src_strength:
                    yield target

    candidates = list(entries())
    pref = None
    if preferred:
        pref = next((c for c in candidates if c["worker"] == preferred.get("worker") and
                     c["model"] == preferred.get("model") and c["thinking"] == preferred.get("thinking")), None)
    buckets = [
        [c for c in candidates if _provider(c["worker"], c["model"]) == src_provider and _family(c["model"]) == src_family],
        [c for c in candidates if _provider(c["worker"], c["model"]) == src_provider],
        [c for c in candidates if _provider(c["worker"], c["model"]) != src_provider],
    ]
    for bucket in buckets:
        if not bucket:
            continue
        if pref and any(c == pref for c in bucket):
            return pref
        return sorted(bucket, key=lambda c: (_strength(c["model"], c["thinking"]), THINKING_RANK[c["thinking"]]))[0]
    return None


def escalation_record(meta, trigger, reason, evidence, target, now, outcome="planned"):
    source = {"id": meta.get("id"), "worker": meta.get("worker"), "model": meta.get("model"),
              "thinking": meta.get("thinking"), "contract_kind": (meta.get("contract") or {}).get("kind"),
              "task_class": (meta.get("preflight") or {}).get("mode") or (meta.get("contract") or {}).get("kind") or "task"}
    return {"at": now, "trigger": trigger, "reason": reason, "evidence": evidence, "source": source,
            "target": target, "outcome": outcome, "next_action": "start target once; if it halts, pause/needs-input"}


def read_escalations(task_dir):
    path = Path(task_dir) / "escalation.json"
    try:
        data = json.loads(path.read_text())
        return data.get("records", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def write_escalations(task_dir, records):
    path = Path(task_dir) / "escalation.json"
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps({"records": records}, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def ledger_row(task_dir, now):
    """One ledger row for a task: what ran, for how long, what it cost and what it produced."""
    d = Path(task_dir)
    read = lambda name: json.loads((d / name).read_text()) if (d / name).exists() else {}
    meta, state = read("meta.json"), read("state.json")
    contract = meta.get("contract")
    art = artifact_info(contract["artifact"]) if contract else None
    reply_only = bool(contract and contract.get("kind") == "task" and contract.get("no_edits") is True)
    alive = not (d / "exit").exists() and state.get("state") not in ("waiting", "error")
    events = read("checkpoints.json").get("events", [])
    result = evaluate(meta, state, art, now, alive, events) if contract else None
    tokens = state.get("tokens") or {}
    model = state.get("model") or meta.get("model") or ""
    task = request_text(d).strip()
    decided = next((e for e in reversed(events) if e["action"] in ("discard", "narrow", "escalate", "auto-escalate")), None)
    halted = next((e for e in reversed(events) if e["action"] in ("pause", "stop")), None)
    escalations = read_escalations(d)
    stop = "escalated" if (d / "escalated").exists() else \
        "{}{}".format(decided["action"], " to " + decided["to"] if decided.get("to") else "") if decided else \
        "exit {}".format((d / "exit").read_text().strip()) if (d / "exit").exists() and not halted else \
        state.get("state", "starting") if not halted else \
        "{}: {}; {}".format(halted["action"], halted.get("reason", ""),
                            "reply-only" if reply_only else
                            "saved {}".format(art["status"]) if art and art["status"] in ("partial", "final")
                            else "no partial saved")
    return {"id": meta.get("id", d.name), "kind": contract["kind"] if contract else "task",
            "task": task.splitlines()[0][:200] if task else "", "worker": meta.get("worker"),
            "model": model, "thinking": meta.get("thinking"),
            "provider": model.split("/")[0] if "/" in model else ("anthropic" if model.startswith("claude") else ""),
            "started": meta.get("created"),
            "elapsed_s": round(result["elapsed"]) if result else None,
            "turns": state.get("turns"), "tokens": {k: tokens.get(k, 0) for k in ("input", "output", "cacheRead", "cacheWrite")},
            "cost": state.get("cost"),
            "artifact": contract["artifact"] if contract else None,
            "artifact_status": "reply-only" if reply_only else art["status"] if art else None,
            "artifact_chars": art["size"] if art else None,
            "source_urls": art["urls"] if art else None,
            "checkpoints": [e["action"] for e in events], "stop": stop,
            "escalated_to": (d / "escalated").read_text().strip() if (d / "escalated").exists() else None,
            "recorded": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "escalation": escalations[-1] if escalations else None}


def upsert(ledger, row):
    """Replace the row with the same id (keeping its ratings) or append it."""
    rows = read_ledger(ledger)
    old = next((r for r in rows if r["id"] == row["id"]), None)
    if old:
        row = dict(row, **{k: old[k] for k in ("rating",) if k in old})
        rows = [row if r["id"] == row["id"] else r for r in rows]
    else:
        rows.append(row)
    _write_ledger(ledger, rows)
    return row


def rate(ledger, task_id, values):
    """Add the human judgments (sources, cleanup, useful, note) to a ledger row."""
    rows = read_ledger(ledger)
    row = next((r for r in rows if r["id"] == task_id), None)
    if not row:
        raise ContractError("no ledger row for {}; run tp-ledger record {} first".format(task_id, task_id))
    rating = dict(row.get("rating") or {})
    for key, value in values.items():
        if key == "note":
            rating["note"] = value
        elif key in RATINGS and value in RATINGS[key]:
            rating[key] = value
        else:
            raise ContractError("rating {}={!r}: use {}".format(
                key, value, "; ".join("{}={}".format(k, "|".join(v)) for k, v in RATINGS.items()) + "; note=text"))
    row["rating"] = rating
    _write_ledger(ledger, rows)
    return row


def read_ledger(ledger):
    try:
        return [json.loads(line) for line in Path(ledger).read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def _write_ledger(ledger, rows):
    tmp = Path(str(ledger) + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, ledger)


def state_dir(runner=subprocess.run):
    """The session's .tmux-pi dir: $TP_STATE_DIR, else the current (or $TP_SESSION) tmux-pi session's."""
    if os.environ.get("TP_STATE_DIR"):
        return os.environ["TP_STATE_DIR"]
    if os.environ.get("TMUX"):
        cmd = ["tmux", "display-message", "-p", "#{@tp_state_dir}"]
    else:
        cmd = ["tmux", "show-options", "-qv", "-t", "={}:".format(os.environ.get("TP_SESSION", "tmux-pi")), "@tp_state_dir"]
    out = runner(cmd, capture_output=True, text=True).stdout.strip()
    if not out:
        raise ContractError("no tmux-pi session (start one with: agent)")
    return out
