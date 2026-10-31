"""Mechanical miner for repeated worker routines. No model reads transcripts.

Ported from the qwen-trial prototype (tools/mine_routines.py). Reads tmux-pi task records
(<project>/.tmux-pi/sessions/*/tasks/<id>/meta.json + transcript-path) and the linked worker transcripts:
Claude (`tool_use` / `tool_result`) and pi (`toolCall` / `toolResult`). Per role it counts, in how many
tasks, each known probe (a routine worth turning into a tool or a rule) and each Bash lead command that
matches no probe. `compare` turns two such counts into the three report lists.

Safety rules, kept from the prototype:
- a Bash command with any line that looks like a credential is dropped whole, before anything is derived from it;
- `.env*` files are never opened (neither as a transcript nor as a transcript's Read/Edit/Write target);
- heredoc bodies are cut out before any probe or normalisation looks at the command.
Transcript text is never stored: only keys (probe names, normalised lead commands) and counts leave this module.
"""
import collections
import json
import os
import re
from datetime import datetime, timezone

STATE_VERSION = 1

SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|bearer|private[_ -]?key|AKIA[0-9A-Z]{8,}|sk-[A-Za-z0-9]{10,}|postgres(?:ql)?://[^ ]*:[^ ]*@)")
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
TASKID = re.compile(r"\b(?:coder|researcher|tester|review|architect|investigator)-[0-9a-f]{6,}(?:-[0-9a-f]+)*\b")
PATH = re.compile(r"(?<![\w.-])(?:~|\.{0,2})?/[^\s'\";|&)(<>]+")
REL_PATH = re.compile(r"(?<![\w.@<>/:-])[\w.@-]+/[^\s'\";|&)(<>]*")  # src/a.ts, origin/main, tests/
LANE = re.compile(r"\bdmp-\d+[\w-]*")
QUOTED = re.compile(r"'[^']{25,}'|\"[^\"]{25,}\"")
NUM = re.compile(r"\b\d+\b")
HEREDOC = re.compile(r"<<(?!<)-?\s*['\"]?(\w+)['\"]?.*", re.S)
ENV_FILE = re.compile(r"(^|/)\.env")

# Probes over a Bash command (heredoc bodies already cut out).
PROBES = {
    "deno binary path discovery (mise path/which deno)": r"mise/installs/deno|DENO=|which deno|command -v deno|deno --version",
    "gitleaks run": r"gitleaks",
    "semgrep run": r"semgrep",
    "prettier --check/--write": r"prettier",
    "eslint": r"\beslint\b",
    "node_modules/.bin lookup (ls/which)": r"ls [^;|&]*node_modules/\.bin|which (prettier|jest|tsc)|ls [^;|&]*node_modules( |$)",
    "web jest (any)": r"\bjest\b",
    "web jest zod4 scratch config": r"jest\.zod4|zod4",
    "path with (protected) parens in a command": r"\(protected\)",
    "jest --runTestsByPath": r"runTestsByPath",
    "api DB test wrapper api-db-test*.sh": r"api-db-test",
    "direct deno task test": r"deno task test|\$DENO test|deno test",
    "revert deno.lock/deno.test.json after tests": r"checkout -- [^;&]*deno\.(lock|test\.json)|git checkout [^;&]*deno\.lock",
    "typecheck (tsc / deno check)": r"\btsc\b|deno check|npm run typecheck",
    "gate: npm run check:*": r"npm run check:",
    "gate: npm run smoke:*": r"npm run smoke:",
    "npm run build": r"npm run build",
    "drizzle migration/journal/snapshot": r"drizzle|_journal|snapshot\.json|prevId|meta/\d{4}",
    "lane base / merge-base / origin base": r"merge-base|sprint2-base|origin/DMP|origin/develop|git fetch",
    "git status/log orientation": r"git (-C \S+ )?(status|log --oneline|branch --show-current)",
    "git add+commit": r"git (-C \S+ )?commit",
    "read review report": r"review-[\w-]+/report\.md|tasks/review",
    "tp-agent request/help": r"tp-agent",
    "test-runs log dir (campaign logs)": r"test-runs/",
    "pre-commit gate bundle (prettier|eslint|gitleaks|tsc|deno check)": r"prettier|\beslint\b|gitleaks|\btsc\b|deno check",
    "find prior task dir (ls .../tasks)": r"ls [^|;&]*tasks(/| |$)|ls -t[^|;&]*tasks|find [^|;&]*tasks",
    "find worktree (worktree list / ls Projects)": r"git (-C \S+ )?worktree list|ls [^|;&]*airtera-monorepo-wt|ls [^|;&]*Projects( |$)",
    "serialized 5-run loop": r"for \w+ in (1 2 3 4 5|\$\(seq 1 5\)|\{1\.\.5\})|seq 1 5",
    "check-requirements prototype script": r"check:requirements|check-requirements",
}

# Probes over the path a Read / Edit / Write tool call targets.
PATH_PROBES = {
    "Read: lane/brief common brief (lane-common/brief-common)": r"lane-common|brief-common",
    "Read: campaign research/decision doc": r"campaigns/[^/]+/(research|briefs/sprint|cases)/",
    "Read: review report.md": r"review-[\w-]+/report\.md|tasks/review",
    "Read: prototype (engineering-context damp-prototype)": r"engineering-context/domains/drug/damp-prototype",
    "Write/Edit: scratch jest zod4 config": r"jest\.zod4",
    "Read: prior task result/report": r"tasks/(coder|researcher|tester)-[0-9a-f]+/(result|report)\.md",
}

# pi names its tools in lower case; fold them onto the Claude names the probes are written for.
PI_TOOLS = {"bash": "Bash", "read": "Read", "edit": "Edit", "write": "Write", "grep": "Grep", "find": "Glob"}


def norm_cmd(cmd):
    """A command with its variable parts (paths, shas, ids, long strings, numbers) replaced by placeholders."""
    cmd = HEREDOC.sub("<<HEREDOC", cmd)
    cmd = re.sub(r"^\s*cd\s+[^;&|]+(?:&&|;)\s*", "", cmd)
    cmd = UUID.sub("<UUID>", cmd)
    cmd = TASKID.sub("<TASK>", cmd)
    cmd = QUOTED.sub("'<STR>'", cmd)
    cmd = PATH.sub("<P>", cmd)
    cmd = REL_PATH.sub("<P>", cmd)
    cmd = LANE.sub("<LANE>", cmd)
    cmd = SHA.sub(lambda m: "<SHA>" if re.search(r"\d", m.group(0)) and re.search(r"[a-f]", m.group(0)) else m.group(0), cmd)
    cmd = NUM.sub("<N>", cmd)
    return re.sub(r"\s+", " ", cmd).strip()[:300]


def lead_shape(cmd):
    """Leading command of a compound (normalised) command, after cd/env; npm run X, git X, deno X aware."""
    for seg in re.split(r"\s*(?:&&|;)\s*", cmd):
        seg = seg.split("|")[0].strip()
        w = seg.split()
        while w and ("=" in w[0] and not w[0].startswith("-")):
            w = w[1:]
        if not w or w[0] in ("cd", "export", "set", "echo", "true", "do", "done", "then", "fi", "for", "while", "if"):
            continue
        if w[0] in ("npm", "pnpm", "yarn") and len(w) > 2 and w[1] == "run":
            return " ".join(w[:3])
        if w[0] == "npx":
            w = [x for x in w if not x.startswith("--")]
            return " ".join(w[:2])
        if w[0] == "git":
            w = [x for x in w if x != "-C" and not x.startswith("<P>")]
            return " ".join(w[:3])
        if w[0] == "deno":
            return " ".join(w[:3])
        if w[0].startswith("<P>"):
            return "<P> " + " ".join(w[1:2])
        return " ".join(w[:2])
    return ""


def has_credential(text):
    """True when any line of the text looks like a credential (SECRET has no pattern that spans lines)."""
    return bool(SECRET.search(text))


def _text_size(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(p.get("text") or "") if isinstance(p, dict) else len(str(p)) for p in content)
    return 0


def parse_transcript(path):
    """A Claude or pi transcript -> [{"name", "input", "bytes"}] in call order. Names are Claude names.

    Claude: assistant `tool_use` parts, answered by user `tool_result` parts (tool_use_id).
    pi: assistant `toolCall` parts (id, name, arguments), answered by `toolResult` messages (toolCallId).
    Lines that are not JSON are skipped. Result text is measured, not kept.
    """
    calls = []
    pending = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if msg.get("role") == "toolResult":  # pi
                call = pending.pop(msg.get("toolCallId"), None)
                if call:
                    call["bytes"] = _text_size(content)
                continue
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "tool_use":
                    call = {"name": part.get("name"), "input": part.get("input") or {}, "bytes": 0}
                elif kind == "toolCall":
                    call = {"name": PI_TOOLS.get(part.get("name"), part.get("name")), "input": part.get("arguments") or {}, "bytes": 0}
                elif kind == "tool_result":
                    done = pending.pop(part.get("tool_use_id"), None)
                    if done:
                        done["bytes"] = _text_size(part.get("content"))
                    continue
                else:
                    continue
                if not isinstance(call["input"], dict):
                    call["input"] = {}
                calls.append(call)
                pending[part.get("id")] = call
    return calls


def keys_for(call):
    """(probe names matched, lead command or None) for one tool call. Nothing for a call that must be dropped.

    The lead command is given only for a Bash call that matched no probe."""
    name, inp = call["name"], call["input"]
    if name == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or has_credential(cmd):
            return set(), None
        head = HEREDOC.sub("", cmd)  # probes see the command, never a heredoc body (commit text, scripts)
        probes = {pn for pn, rx in PROBES.items() if re.search(rx, head)}
        return probes, (None if probes else lead_shape(norm_cmd(cmd)) or None)
    if name in ("Read", "Edit", "Write"):
        target = inp.get("file_path") or inp.get("path") or ""
        if not isinstance(target, str) or ENV_FILE.search(target):
            return set(), None
        return {pn for pn, rx in PATH_PROBES.items() if re.search(rx, target)}, None
    return set(), None


def find_tasks(sessions_dir, since="", role=None):
    """[(meta, transcript path)] for every task with a readable meta.json and an existing transcript.

    `since` (YYYY-MM-DD) keeps tasks whose meta `created` date is on or after it. A transcript whose
    file name is `.env*` is never opened.
    """
    tasks = []
    for task_dir in sorted(_task_dirs(sessions_dir)):
        try:
            with open(os.path.join(task_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            with open(os.path.join(task_dir, "transcript-path"), encoding="utf-8") as fh:
                tpath = fh.read().strip()
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not meta.get("id") or not tpath or ENV_FILE.search(tpath) or not os.path.isfile(tpath):
            continue
        if since and (meta.get("created") or "")[:10] < since:
            continue
        if role and (meta.get("role") or "other") != role:
            continue
        tasks.append((meta, tpath))
    return tasks


def _task_dirs(sessions_dir):
    try:
        sessions = os.listdir(sessions_dir)
    except OSError:
        return
    for session in sessions:
        tasks_dir = os.path.join(sessions_dir, session, "tasks")
        try:
            names = os.listdir(tasks_dir)
        except OSError:
            continue
        for name in names:
            yield os.path.join(tasks_dir, name)


def mine(tasks):
    """tasks -> {role: {"tasks": n, "calls": n, "probes": {name: stat}, "leads": {lead: stat}}}.

    stat = {"tasks": distinct tasks, "calls": n, "result_tokens": bytes // 4}. `leads` holds only the
    Bash lead commands of calls that matched no probe.
    """
    roles = {}
    seen = collections.defaultdict(lambda: {"probes": collections.defaultdict(set), "leads": collections.defaultdict(set)})
    for meta, tpath in tasks:
        role = meta.get("role") or "other"
        r = roles.setdefault(role, {"tasks": 0, "calls": 0, "probes": {}, "leads": {}})
        try:
            calls = parse_transcript(tpath)
        except OSError:
            continue
        r["tasks"] += 1
        r["calls"] += len(calls)
        for call in calls:
            probes, lead = keys_for(call)
            for table, key in [("probes", p) for p in probes] + ([("leads", lead)] if lead else []):
                stat = r[table].setdefault(key, {"tasks": 0, "calls": 0, "result_tokens": 0})
                stat["calls"] += 1
                stat["result_tokens"] += call["bytes"] // 4
                seen[role][table][key].add(meta["id"])
    for role, r in roles.items():
        for table in ("probes", "leads"):
            for key, stat in r[table].items():
                stat["tasks"] = len(seen[role][table][key])
    return roles


def load_state(path):
    """The previous run's counts, or None when there is none or it cannot be used (second value says why)."""
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, "previous counts unreadable ({}): {}".format(path, exc)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(state.get("roles"), dict):
        return None, "previous counts ignored ({}): not a version {} counts file".format(path, STATE_VERSION)
    return state, ""


def counts_of(roles, min_tasks):
    """The part of a mine() result worth keeping for the next run: task counts per probe, and per lead seen min_tasks+ times."""
    return {role: {"probes": {k: s["tasks"] for k, s in r["probes"].items()},
                   "leads": {k: s["tasks"] for k, s in r["leads"].items() if s["tasks"] >= min_tasks}}
            for role, r in roles.items()}


def merged_state(previous, roles, min_tasks, now):
    """New state: the roles analysed now replace their old counts; roles not analysed this run keep theirs."""
    kept = dict(previous["roles"]) if previous else {}
    kept.update(counts_of(roles, min_tasks))
    return {"version": STATE_VERSION, "generated": now, "min_tasks": min_tasks, "roles": kept}


def compare(previous, roles, min_tasks):
    """The three lists. Only roles analysed this run are compared; with no previous run every count starts at 0.

    grew      probes whose task count is above the previous run's
    new_leads Bash lead commands in >= min_tasks tasks that match no probe and were not already at min_tasks before
              (the saved counts hold only leads at min_tasks or more, so a lead below it counts as 0 before)
    dropped   probes whose task count is below the previous run's (including to 0)
    """
    before = (previous or {}).get("roles", {})
    grew, new_leads, dropped = [], [], []
    for role in sorted(roles):
        old = before.get(role, {})
        old_probes, old_leads = old.get("probes", {}), old.get("leads", {})
        now_probes = roles[role]["probes"]
        for name, stat in now_probes.items():
            prev = int(old_probes.get(name, 0))
            if stat["tasks"] > prev:
                grew.append({"role": role, "probe": name, "previous": prev, "tasks": stat["tasks"],
                             "calls": stat["calls"], "result_tokens": stat["result_tokens"]})
        for name, prev in old_probes.items():
            if name not in now_probes and prev > 0:
                dropped.append({"role": role, "probe": name, "previous": int(prev), "tasks": 0})
            elif name in now_probes and now_probes[name]["tasks"] < prev:
                dropped.append({"role": role, "probe": name, "previous": int(prev), "tasks": now_probes[name]["tasks"]})
        for name, stat in roles[role]["leads"].items():
            prev = int(old_leads.get(name, 0))
            if stat["tasks"] >= min_tasks and prev < min_tasks:
                new_leads.append({"role": role, "lead": name, "tasks": stat["tasks"],
                                  "calls": stat["calls"], "result_tokens": stat["result_tokens"]})
    grew.sort(key=lambda e: (-(e["tasks"] - e["previous"]), e["role"], e["probe"]))
    new_leads.sort(key=lambda e: (-e["tasks"], e["role"], e["lead"]))
    dropped.sort(key=lambda e: (-(e["previous"] - e["tasks"]), e["role"], e["probe"]))
    return {"grew": grew, "new_leads": new_leads, "dropped": dropped}


def build_report(roles, previous, min_tasks, since="", role="", project="", now=None):
    lists = compare(previous, roles, min_tasks)
    return {
        "generated": now or utc_now(), "project": project, "since": since, "role": role, "min_tasks": min_tasks,
        "baseline": previous is None,
        "summary": {r: {"tasks": v["tasks"], "calls": v["calls"]} for r, v in sorted(roles.items())},
        **lists,
    }


def render_text(report):
    out = ["routines report {}{}".format(report["generated"], " (baseline: no previous counts)" if report["baseline"] else "")]
    for role, s in report["summary"].items():
        out.append("  {}: {} tasks, {} tool calls".format(role, s["tasks"], s["calls"]))
    out.append("\n(a) probes in more tasks than last report: {}".format(len(report["grew"])))
    for e in report["grew"]:
        out.append("  {:<10} {:>3} -> {:<3} tasks {:>4} calls {:>7} tok  {}".format(e["role"], e["previous"], e["tasks"], e["calls"], e["result_tokens"], e["probe"]))
    out.append("\n(b) new Bash lead commands in {}+ tasks matching no probe: {}".format(report["min_tasks"], len(report["new_leads"])))
    for e in report["new_leads"]:
        out.append("  {:<10} {:>7} tasks {:>4} calls {:>7} tok  {}".format(e["role"], e["tasks"], e["calls"], e["result_tokens"], e["lead"]))
    out.append("\n(c) probes in fewer tasks than last report: {}".format(len(report["dropped"])))
    for e in report["dropped"]:
        out.append("  {:<10} {:>3} -> {:<3} tasks  {}".format(e["role"], e["previous"], e["tasks"], e["probe"]))
    return "\n".join(out) + "\n"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomic(path, text):
    tmp = "{}.tmp{}".format(path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def counts_path(out):
    return out + ".counts.json"


def run(project, role="", since="", min_tasks=3, as_json=False, out=""):
    """Mine PROJECT's tasks and return (report text, warnings). With `out`, write the report there and the
    new counts next to it (OUT.counts.json), after the report is on disk; then the returned text is a one-line note."""
    sessions = os.path.join(project, ".tmux-pi", "sessions")
    if not os.path.isdir(sessions):
        raise ValueError("no sessions directory at {}".format(sessions))
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ValueError("--since must be YYYY-MM-DD, got {!r}".format(since))
    if min_tasks < 1:
        raise ValueError("--min-tasks must be at least 1")
    warnings = []
    previous = None
    if out:
        previous, why = load_state(counts_path(out))
        if why:
            warnings.append(why)
    roles = mine(find_tasks(sessions, since=since, role=role or None))
    report = build_report(roles, previous, min_tasks, since=since, role=role, project=project)
    body = json.dumps(report, indent=2) + "\n" if as_json else render_text(report)
    if not out:
        return body, warnings
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    write_atomic(out, body)
    write_atomic(counts_path(out), json.dumps(merged_state(previous, roles, min_tasks, report["generated"]), indent=2) + "\n")
    return "wrote {} (counts in {})\n".format(out, counts_path(out)), warnings
