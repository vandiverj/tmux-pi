"""Worker contracts (M9): a destination artifact, a budget and checkpoints for bounded work.

A contract lives in a task's meta.json under "contract":

    {"kind": "research" | "task", "artifact": "/abs/path.md",
     "budget": {"seconds": 1200, "turns": 60 | null, "tokens": 400000 | null},
     "checkpoints": {"outline": 180, "draft": 600}, "stall": 360, "grace": 180}

The worker keeps a `Status: outline|draft|final|partial` line at the top of its
artifact. `evaluate` compares that and the artifact's last write with the clock and
the worker's reported turns and tokens; `enforce` turns a missed checkpoint into one
message to the worker, and a missed first checkpoint or an exceeded budget into a
pause (Escape), each at most once. Every threshold is configuration
(config/research.json for research), not model behavior.
"""

import datetime
import json
import os
import re
import subprocess
from pathlib import Path

KINDS = ("research", "task")
STATUSES = ("outline", "draft", "final", "partial")
STATUS_LINE = re.compile(r"^\W*status\W*:?\W*(outline|draft|final|partial)\b", re.I)
URL = re.compile(r"https?://[^\s)>\]]+")
RATINGS = {"sources": ("ok", "weak", "missing"), "cleanup": ("none", "minor", "major"),
           "useful": ("yes", "partly", "no")}


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


def build(kind, artifact, budget, checkpoints, stall, grace):
    """A validated contract; artifact must already be absolute."""
    if kind not in KINDS:
        raise ContractError("contract kind must be research or task")
    if not os.path.isabs(artifact):
        raise ContractError("artifact must be an absolute path")
    budget = pairs(budget, "budget")
    if not set(budget) <= {"time", "turns", "tokens"} or "time" not in budget:
        raise ContractError("budget needs time and may have turns and tokens")
    checkpoints = pairs(checkpoints, "checkpoints")
    if not set(checkpoints) <= {"outline", "draft"}:
        raise ContractError("checkpoints are outline and draft")
    out = {"kind": kind, "artifact": artifact,
           "budget": {"seconds": duration(budget["time"]),
                      "turns": quantity(budget["turns"]) if budget.get("turns") else None,
                      "tokens": quantity(budget["tokens"]) if budget.get("tokens") else None},
           "checkpoints": {k: duration(v) for k, v in checkpoints.items()},
           "stall": duration(stall), "grace": duration(grace)}
    times = [out["checkpoints"].get("outline"), out["checkpoints"].get("draft"), out["budget"]["seconds"]]
    times = [t for t in times if t]
    if times != sorted(times) or len(times) != len(set(times)):
        raise ContractError("checkpoints must come in order: outline < draft < time budget")
    return out


def load_research(path, roster=None):
    """config/research.json: the research route's worker, contract defaults and caps.

    roster is router.json's models list; when given, worker, model and thinking must match an entry.
    """
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("cannot read research config: {}".format(exc)) from exc
    config = {k: v for k, v in config.items() if not k.startswith("_")}
    keys = {"worker", "model", "thinking", "budget", "checkpoints", "stall", "grace", "max_research", "max_active"}
    if set(config) != keys:
        raise ContractError("research config needs exactly {}".format(", ".join(sorted(keys))))
    build("research", "/x", config["budget"], config["checkpoints"], config["stall"], config["grace"])
    for key in ("max_research", "max_active"):
        if isinstance(config[key], bool) or not isinstance(config[key], int) or config[key] < 1:
            raise ContractError("{} must be a positive integer".format(key))
    if roster is not None and not any(item["worker"] == config["worker"] and item["model"] == config["model"] and
                                      config["thinking"] in item["thinking"] for item in roster):
        raise ContractError("research worker, model and thinking must match the router roster")
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
        text += (" Research: answer the one question in the task. Write the outline before exploring broadly. "
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

    Actions: nudge-outline, nudge-draft, nudge-stall, stop (budget reached: ask the
    worker to save and ask), pause (Escape: first checkpoint missed after grace, or
    still working after the stop message plus grace). An action is due only while the
    worker is working and only once (events holds the ones already taken).
    """
    c = meta["contract"]
    created = _epoch(meta.get("created")) or now
    settled = not alive or state.get("state") in ("waiting", "error")
    end = (_epoch(state.get("updated")) or now) if settled else now
    elapsed = max(0, end - created)
    turns = state.get("turns") or 0
    tokens = sum((state.get("tokens") or {}).get(k, 0) for k in ("input", "output", "cacheRead", "cacheWrite"))
    b, cp = c["budget"], c["checkpoints"]
    status = art["status"] if art["exists"] else None
    has_outline = art["exists"]
    has_draft = status in ("draft", "final", "partial")
    over = [what for what, hit in (("time", elapsed >= b["seconds"]),
                                   ("turns", bool(b["turns"]) and turns >= b["turns"]),
                                   ("tokens", bool(b["tokens"]) and tokens >= b["tokens"])) if hit]
    write_age = now - art["mtime"] if art["mtime"] else None

    if status == "final":
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
    working = alive and state.get("state") in ("working", "tool")
    taken = {e["action"] if isinstance(e, dict) else e for e in events}
    if working and status != "final":
        if over and "stop" not in taken:
            action = ("stop", "{} budget used up".format(" and ".join(over)))
        elif over and "pause" not in taken and _since(events, "stop", now) >= c["grace"]:
            action = ("pause", "still working {} after the stop message".format(_clock(c["grace"])))
        elif "outline" in cp and not has_outline and elapsed >= cp["outline"] + c["grace"] and "pause" not in taken:
            action = ("pause", "no outline in {} after its checkpoint".format(_clock(elapsed - cp["outline"])))
        elif "outline" in cp and not has_outline and elapsed >= cp["outline"] and "nudge-outline" not in taken:
            action = ("nudge-outline", "outline checkpoint reached with nothing written")
        elif "draft" in cp and not has_draft and elapsed >= cp["draft"] and "nudge-draft" not in taken:
            action = ("nudge-draft", "draft checkpoint reached")
        elif has_outline and write_age is not None and write_age >= c["stall"] and \
                "nudge-stall@{}".format(int(art["mtime"])) not in taken:
            action = ("nudge-stall@{}".format(int(art["mtime"])), "no new writes for {}".format(_clock(write_age)))
    return {"elapsed": elapsed, "turns": turns, "tokens": tokens, "status": status, "phase": phase,
            "write_age": write_age, "over": over, "action": action}


def _since(events, action, now):
    times = [e.get("at", 0) for e in events if isinstance(e, dict) and e.get("action") == action]
    return now - max(times) if times else -1


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
    if action == "stop":
        return ("tmux-pi: your budget is used up. Stop now: save {} with `Status: partial` (or `Status: final` if it "
                "is complete), list the evidence still missing, and ask whether to continue, escalate, narrow the "
                "question, or discard it.".format(a))
    return None


def ledger_row(task_dir, now):
    """One ledger row for a task: what ran, for how long, what it cost and what it produced."""
    d = Path(task_dir)
    read = lambda name: json.loads((d / name).read_text()) if (d / name).exists() else {}
    meta, state = read("meta.json"), read("state.json")
    contract = meta.get("contract")
    art = artifact_info(contract["artifact"]) if contract else None
    alive = not (d / "exit").exists() and state.get("state") not in ("waiting", "error")
    events = read("checkpoints.json").get("events", [])
    result = evaluate(meta, state, art, now, alive, events) if contract else None
    tokens = state.get("tokens") or {}
    model = state.get("model") or meta.get("model") or ""
    task = (d / "task.md").read_text().strip() if (d / "task.md").exists() else ""
    stop = "escalated" if (d / "escalated").exists() else next(
        (e["action"] + ": " + e.get("reason", "") for e in reversed(events) if e["action"] in ("pause", "stop")),
        "exit {}".format((d / "exit").read_text().strip()) if (d / "exit").exists() else state.get("state", "starting"))
    return {"id": meta.get("id", d.name), "kind": contract["kind"] if contract else "task",
            "task": task.splitlines()[0][:200] if task else "", "worker": meta.get("worker"),
            "model": model, "thinking": meta.get("thinking"),
            "provider": model.split("/")[0] if "/" in model else ("anthropic" if model.startswith("claude") else ""),
            "started": meta.get("created"),
            "elapsed_s": round(result["elapsed"]) if result else None,
            "turns": state.get("turns"), "tokens": {k: tokens.get(k, 0) for k in ("input", "output", "cacheRead", "cacheWrite")},
            "cost": state.get("cost"),
            "artifact": contract["artifact"] if contract else None,
            "artifact_status": art["status"] if art else None, "artifact_chars": art["size"] if art else None,
            "source_urls": art["urls"] if art else None,
            "checkpoints": [e["action"] for e in events], "stop": stop,
            "escalated_to": (d / "escalated").read_text().strip() if (d / "escalated").exists() else None,
            "recorded": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


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
