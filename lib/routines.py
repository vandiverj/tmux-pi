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
import shlex
import subprocess
from datetime import datetime, timezone

STATE_VERSION = 2  # 2: counts carry each role's task count (the denominator for shares)

SECRET = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|bearer|private[_ -]?key|AKIA[0-9A-Z]{8,}|sk-[A-Za-z0-9]{10,}|postgres(?:ql)?://[^ ]*:[^ ]*@)")
SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
TASKID = re.compile(r"\b(?:coder|researcher|tester|review|architect|investigator)-[0-9a-f]{6,}(?:-[0-9a-f]+)*\b")
PATH = re.compile(r"(?<![\w.-])(?:~|\.{0,2})?/[^\s'\";|&)(<>]+")
REL_PATH = re.compile(r"(?<![\w.@<>/:-])[\w.@-]+/[^\s'\";|&)(<>]*")  # src/a.ts, origin/main, tests/
QUOTED = re.compile(r"'[^']{25,}'|\"[^\"]{25,}\"")
NUM = re.compile(r"\b\d+\b")
HEREDOC = re.compile(r"<<(?!<)-?\s*['\"]?(\w+)['\"]?.*", re.S)
ENV_FILE = re.compile(r"(^|/)\.env")
FETCH_PROBE = "git fetch"  # matched on git's parsed subcommand (git_subcommands), never on text
# git's global options that take a value (`git --help`): `-C`/`-c` always as the next token; the long ones as
# `--opt=value` or the next token. `--exec-path` without `=` is a flag.
GIT_VALUE_SHORT = ("-C", "-c")
GIT_VALUE_LONG = ("--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env")

# Probes live in config/routines.json (most are specific to the Airtera/DMP monorepo): "probes" over a Bash
# command (heredoc bodies already cut out), "path_probes" over the path a Read / Edit / Write call targets, and
# "lane_pattern" for lane names. Order is significant (specific before the general probe it is part of).
CONFIG = os.environ.get("TP_ROUTINES_CONFIG") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "routines.json")


def _read_config(path, whole):
    """The config document at PATH, checked. `whole`: lane_pattern, probes and path_probes are all required;
    otherwise (a project override) each is optional. Raises ValueError naming the file and the problem."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as err:
        raise ValueError("cannot read routines config {}: {}".format(path, err))
    try:
        if not isinstance(doc, dict):
            raise TypeError("the top level must be an object")
        if whole:
            doc["lane_pattern"], doc["probes"], doc["path_probes"]
        lane = doc.get("lane_pattern", "")
        tables = [doc.get("probes", {}), doc.get("path_probes", {})]
        if not isinstance(lane, str) or not all(isinstance(t, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in t.items()) for t in tables):
            raise TypeError("lane_pattern must be a string, probes and path_probes objects of name -> regex")
        for rx in (lane, *tables[0].values(), *tables[1].values()):
            re.compile(rx)
    except (KeyError, TypeError, re.error) as err:
        raise ValueError("bad routines config {}: {!r}".format(path, err))
    return doc


def _merged(base, extra):
    """BASE with EXTRA on top: a name BASE has keeps its place with the new regex; new names go first (a project's
    own probes are more specific than the shared ones)."""
    return {**{k: v for k, v in extra.items() if k not in base}, **{k: extra.get(k, v) for k, v in base.items()}}


def override_path(project):
    """Where PROJECT keeps its optional routines override."""
    return os.path.join(project, ".tmux-pi", "routines.json")


def main_project(worktree):
    """The main checkout of WORKTREE's repository: the parent of `git rev-parse --git-common-dir` (WORKTREE itself
    in the main checkout), or None when git cannot say."""
    run = subprocess.run(["git", "-C", worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                         capture_output=True, text=True)
    common = run.stdout.strip()
    if run.returncode != 0 or not common.startswith("/"):
        return None
    return os.path.dirname(os.path.realpath(common))


def project_flag(worktree, key):
    """Whether the project override sets KEY to JSON true. The override is WORKTREE's own file when it has one, else the
    main checkout's (a lane worktree has no .tmux-pi of its own); a file that is there wins whole. A file that cannot
    be read raises ValueError."""
    for project in (worktree, main_project(worktree)):
        if project and os.path.isfile(override_path(project)):
            return _read_config(override_path(project), False).get(key) is True
    return False


def load_config(path=CONFIG, project=None):
    """(lane regex, PROBES, PATH_PROBES) from the routines config, with PROJECT's optional override
    (PROJECT/.tmux-pi/routines.json, same keys, all optional) merged on top. lane_pattern replaces the shared one."""
    doc = _read_config(path, True)
    lane, probes, path_probes = doc["lane_pattern"], doc["probes"], doc["path_probes"]
    if project:
        over = override_path(project)
        if os.path.isfile(over):
            extra = _read_config(over, False)
            lane = extra.get("lane_pattern") or lane
            probes = _merged(probes, extra.get("probes", {}))
            path_probes = _merged(path_probes, extra.get("path_probes", {}))
    return re.compile(lane), probes, path_probes


LANE, PROBES, PATH_PROBES = load_config()

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


def is_env_path(path):
    """True when the path, or the file it really is (symlinks in any component followed), is `.env*`."""
    try:
        path = os.fspath(path)
        real = os.path.realpath(path)
    except (OSError, ValueError, TypeError):
        return True  # cannot say where it leads, so do not open it
    return bool(ENV_FILE.search(path) or ENV_FILE.search(real))


def open_text(path):
    """The one way this module opens a file it reads: text mode, refused (PermissionError) for `.env*`,
    whether named directly or reached through a symlink."""
    if is_env_path(path):
        raise PermissionError("refusing to open a .env file: {}".format(path))
    return open(path, encoding="utf-8", errors="replace")


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
    with open_text(path) as fh:
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


PROBE_ORDER = list(PROBES) + list(PATH_PROBES)  # table order: most specific first


def first_probe(probes):
    """The one probe that claims a call (`exclusive`): the first in table order, or None."""
    return next((p for p in PROBE_ORDER if p in probes), None)


def use_project_config(project):
    """Make PROJECT's merged probes the module's tables; returns what to hand to restore_config."""
    global LANE, PROBES, PATH_PROBES, PROBE_ORDER
    saved = (LANE, PROBES, PATH_PROBES, PROBE_ORDER)
    LANE, PROBES, PATH_PROBES = load_config(project=project)
    PROBE_ORDER = list(PROBES) + list(PATH_PROBES)
    return saved


def restore_config(saved):
    global LANE, PROBES, PATH_PROBES, PROBE_ORDER
    LANE, PROBES, PATH_PROBES, PROBE_ORDER = saved


def call_key(call):
    """Identity of a call for dedupe: the normalised command, or the tool and its target path."""
    inp = call["input"]
    if call["name"] == "Bash":
        return norm_cmd(inp.get("command") or "")
    return "{} {}".format(call["name"], inp.get("file_path") or inp.get("path") or "")


def _simple_commands(cmd):
    """[(start, end, tokens)] for each simple command of `cmd`, split on ; & | and newline outside quotes.
    With an unterminated quote the tokens are a plain whitespace split. Never raises.
    Known limit (accepted 2026-11-07, review-coder-169502cc17-162755 F1): a shell comment is not recognised,
    so `echo ok # x && git fetch` yields a `git fetch` command the shell never runs. Real transcripts showed
    no such commands; the comment rule is carried into E2's brief."""
    spans, start, quote, i = [], 0, None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = None
        elif c == "\\":
            i += 1
        elif c in "'\"":
            quote = c
        elif c in ";&|\n":
            spans.append((start, i))
            start = i + 1
        i += 1
    spans.append((start, len(cmd)))
    out = []
    for a, b in spans:
        try:
            tokens = shlex.split(cmd[a:b], posix=True)
        except ValueError:
            tokens = cmd[a:b].split()
        out.append((a, b, tokens))
    return out


def git_subcommands(cmd):
    """[(subcommand, (start, end))] for each simple command of `cmd` that runs git: the first word after git's
    global options (`-C dir`, `-c k=v`, `--git-dir=..`, `--no-pager`, ...) and the span of that simple command.
    A command that is not git (`echo "git fetch"`) yields nothing."""
    found = []
    for a, b, tokens in _simple_commands(cmd):
        i = 0
        while i < len(tokens) and re.fullmatch(r"[A-Za-z_]\w*=.*", tokens[i]):
            i += 1  # leading VAR=value
        if i >= len(tokens) or not (tokens[i].lstrip("(") == "git" or tokens[i].endswith("/git")):
            continue
        i += 1
        while i < len(tokens) and tokens[i].startswith("-") and len(tokens[i]) > 1:
            if tokens[i] in GIT_VALUE_SHORT or tokens[i] in GIT_VALUE_LONG:
                i += 1  # its value is the next token
            i += 1
        if i < len(tokens):
            found.append((tokens[i], (a, b)))
    return found


def keys_for(call):
    """(probe names matched, lead command or None) for one tool call. Nothing for a call that must be dropped.

    The lead command is given only for a Bash call that matched no probe. Every matching probe is returned
    (`any`); `first_probe` picks the one that claims the call (`exclusive`)."""
    name, inp = call["name"], call["input"]
    if name == "Bash":
        cmd = inp.get("command")
        if not isinstance(cmd, str) or has_credential(cmd):
            return set(), None
        head = HEREDOC.sub("", cmd)  # probes see the command, never a heredoc body (commit text, scripts)
        rest, fetch = head, False
        for sub, (a, b) in reversed(git_subcommands(head)):
            if sub == "fetch":  # a fetch's arguments (refs, flags) belong to the git fetch probe only
                fetch, rest = True, rest[:a] + " " + rest[b:]
        probes = {pn for pn, rx in PROBES.items() if re.search(rx, rest)}
        if fetch:
            probes.add(FETCH_PROBE)
        return probes, (None if probes else lead_shape(norm_cmd(cmd)) or None)
    if name in ("Read", "Edit", "Write"):
        target = inp.get("file_path") or inp.get("path") or ""
        if not isinstance(target, str) or ENV_FILE.search(target):
            return set(), None
        return {pn for pn, rx in PATH_PROBES.items() if re.search(rx, target)}, None
    return set(), None


def find_tasks(sessions_dir, since="", role=None, until=""):
    """[(meta, transcript path)] for every task with a readable meta.json and an existing transcript.

    `since` (YYYY-MM-DD) keeps tasks whose meta `created` date is on or after it; `until` keeps those on or
    before it (a task with no date counts as the oldest). A transcript whose path (or the file a symlink
    leads to) names `.env*` is never opened.
    """
    tasks = []
    for task_dir in sorted(_task_dirs(sessions_dir)):
        try:
            with open_text(os.path.join(task_dir, "meta.json")) as fh:
                meta = json.load(fh)
            with open_text(os.path.join(task_dir, "transcript-path")) as fh:
                tpath = fh.read().strip()
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or not meta.get("id") or not tpath or is_env_path(tpath) or not os.path.isfile(tpath):
            continue
        day = (meta.get("created") or "")[:10]
        if (since and day < since) or (until and day > until):
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


def _acc():
    return {"calls": 0, "result_tokens": 0, "tasks": set(), "distinct": set(), "exclusive": {"calls": 0, "result_tokens": 0}}


def mine(tasks):
    """tasks -> {role: {"tasks": n, "calls": n, "probes": {name: stat}, "leads": {lead: stat}}}.

    stat = {"tasks": distinct tasks, "calls": n, "distinct": distinct (task, normalised command) pairs,
            "result_tokens": bytes // 4, "exclusive": {"calls": n, "result_tokens": n}}.
    A call counts toward every probe it matches (`calls`, `result_tokens`, `tasks`); `exclusive` counts it only
    under the first probe in table order (see first_probe). `leads` holds only the Bash lead commands of calls
    that matched no probe, and those are exclusive by definition.
    """
    roles = {}
    accs = {}
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
            if not probes and not lead:
                continue
            first = first_probe(probes)
            targets = [("probes", p, p == first) for p in probes] + ([("leads", lead, True)] if lead else [])
            tokens, dedupe_key = call["bytes"] // 4, call_key(call)
            for table, key, exclusive in targets:
                acc = accs.setdefault((role, table, key), _acc())
                acc["calls"] += 1
                acc["result_tokens"] += tokens
                acc["tasks"].add(meta["id"])
                acc["distinct"].add((meta["id"], dedupe_key))
                if exclusive:
                    acc["exclusive"]["calls"] += 1
                    acc["exclusive"]["result_tokens"] += tokens
    for (role, table, key), acc in accs.items():
        roles[role][table][key] = {"tasks": len(acc["tasks"]), "calls": acc["calls"], "distinct": len(acc["distinct"]),
                                   "result_tokens": acc["result_tokens"], "exclusive": dict(acc["exclusive"])}
    return roles


def load_state(path):
    """The previous run's counts, or None when there is none or it cannot be used (second value says why)."""
    try:
        with open_text(path) as fh:
            state = json.load(fh)
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError) as exc:
        return None, "previous counts unreadable ({}): {}".format(path, exc)
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION or not isinstance(state.get("roles"), dict):
        return None, "previous counts ignored ({}): not a version {} counts file".format(path, STATE_VERSION)
    return state, ""


def counts_of(roles, min_tasks, previous=None):
    """The part of a mine() result worth keeping for the next run: task counts per probe, and per lead.

    A lead is kept when it is in min_tasks+ tasks now, or was saved by an earlier run: a lead once reported
    stays reported, so a raised --min-tasks cannot make it new again later. A saved lead that this run did not
    see keeps its saved count. (`previous` is the saved roles; a role that is gone from `roles` keeps nothing.)"""
    out = {}
    for role, r in roles.items():
        saved = ((previous or {}).get(role) or {}).get("leads") or {}
        leads = {k: v for k, v in saved.items() if isinstance(v, int)}
        leads.update({k: s["tasks"] for k, s in r["leads"].items() if s["tasks"] >= min_tasks or k in leads})
        out[role] = {"tasks": r["tasks"], "probes": {k: s["tasks"] for k, s in r["probes"].items()}, "leads": leads}
    return out


def merged_state(previous, roles, min_tasks, now, role=""):
    """New state. An unscoped run replaces every role (a role that no longer has tasks is gone, so its probes
    drop to 0 once and are not kept). A run limited to `role` replaces only that role and keeps the others."""
    counts = counts_of(roles, min_tasks, previous["roles"] if previous else None)
    if role:
        kept = dict(previous["roles"]) if previous else {}
        kept.pop(role, None)
        kept.update({k: v for k, v in counts.items() if k == role})
    else:
        kept = counts
    return {"version": STATE_VERSION, "generated": now, "min_tasks": min_tasks, "roles": kept}


EMPTY_ROLE = {"tasks": 0, "calls": 0, "probes": {}, "leads": {}}


def share(count, tasks):
    """The share of a role's tasks that a probe is in. 0 when the role has no tasks."""
    return count / tasks if tasks else 0.0


def compare(previous, roles, min_tasks, role=""):
    """The three lists. An unscoped run compares every role in either run (a role with no tasks now counts 0);
    a run limited to `role` compares only that role. With no previous run every count starts at 0.

    grew      probes whose share of the role's tasks rose (tasks with the probe / tasks in the role)
    new_leads Bash lead commands in >= min_tasks tasks that match no probe and were not reported before
              (the saved counts hold the leads that were at their run's min_tasks or more, so a lead that is
              saved was already reported, whatever threshold this run uses)
    dropped   probes whose share of the role's tasks fell (including to 0)

    Shares, not raw task counts, decide grew and dropped: a role that has more tasks grows its raw counts
    without any routine getting more common. Each entry keeps the raw counts too (`previous`, `tasks`).
    """
    before = (previous or {}).get("roles", {})
    names = [role] if role else sorted(set(roles) | set(before))
    grew, new_leads, dropped = [], [], []
    for name_role in names:
        old = before.get(name_role, {})
        old_probes, old_leads = old.get("probes", {}), old.get("leads", {})
        old_tasks = int(old.get("tasks", 0))
        now = roles.get(name_role, EMPTY_ROLE)
        now_probes = now["probes"]
        for name, stat in now_probes.items():
            prev = int(old_probes.get(name, 0))
            prev_share, now_share = share(prev, old_tasks), share(stat["tasks"], now["tasks"])
            if now_share > prev_share:
                grew.append({"role": name_role, "probe": name, "previous": prev, "tasks": stat["tasks"],
                             "previous_share": prev_share, "share": now_share,
                             "previous_role_tasks": old_tasks, "role_tasks": now["tasks"],
                             "calls": stat["calls"], "distinct": stat["distinct"], "result_tokens": stat["result_tokens"],
                             "exclusive_result_tokens": stat["exclusive"]["result_tokens"]})
        for name, prev in old_probes.items():
            prev = int(prev)
            now_tasks = now_probes[name]["tasks"] if name in now_probes else 0
            prev_share, now_share = share(prev, old_tasks), share(now_tasks, now["tasks"])
            gone = now_probes.get(name) or {"calls": 0, "distinct": 0, "result_tokens": 0, "exclusive": {"result_tokens": 0}}
            if now_share < prev_share:
                dropped.append({"role": name_role, "probe": name, "previous": prev, "tasks": now_tasks,
                                "previous_share": prev_share, "share": now_share,
                                "previous_role_tasks": old_tasks, "role_tasks": now["tasks"],
                                "calls": gone["calls"], "distinct": gone["distinct"], "result_tokens": gone["result_tokens"],
                                "exclusive_result_tokens": gone["exclusive"]["result_tokens"]})
        for name, stat in now["leads"].items():
            if stat["tasks"] >= min_tasks and name not in old_leads:
                new_leads.append({"role": name_role, "lead": name, "tasks": stat["tasks"], "calls": stat["calls"],
                                  "distinct": stat["distinct"], "result_tokens": stat["result_tokens"]})
    grew.sort(key=lambda e: (-(e["share"] - e["previous_share"]), e["role"], e["probe"]))
    new_leads.sort(key=lambda e: (-e["tasks"], e["role"], e["lead"]))
    dropped.sort(key=lambda e: (-(e["previous_share"] - e["share"]), e["role"], e["probe"]))
    return {"grew": grew, "new_leads": new_leads, "dropped": dropped}


def build_report(roles, previous, min_tasks, since="", until="", role="", project="", now=None):
    lists = compare(previous, roles, min_tasks, role=role)
    return {
        "generated": now or utc_now(), "project": project, "since": since, "until": until, "role": role, "min_tasks": min_tasks,
        "baseline": previous is None,
        "summary": {r: {"tasks": v["tasks"], "calls": v["calls"]} for r, v in sorted(roles.items())},
        **lists,
    }


def _pct(x):
    return "{:.1f}%".format(100 * x)


def render_text(report):
    out = ["routines report {}{}".format(report["generated"], " (baseline: no previous counts)" if report["baseline"] else "")]
    for role, s in report["summary"].items():
        out.append("  {}: {} tasks, {} tool calls, {:.1f} calls per task".format(
            role, s["tasks"], s["calls"], s["calls"] / s["tasks"] if s["tasks"] else 0))
    out.append("\n(a) probes in more tasks (share of the role's tasks rose): {}".format(len(report["grew"])))
    out.append("    tasks with routine / tasks in role; tok = result tokens, any (every matching probe) / exclusive (first match)")
    for e in report["grew"]:
        out.append("  {:<10} {:>6} -> {:<6} tasks {}/{} (was {}/{}) calls {:>5} distinct {:>5} tok {:>7} any {:>7} excl  {}".format(
            e["role"], _pct(e["previous_share"]), _pct(e["share"]), e["tasks"], e["role_tasks"], e["previous"],
            e["previous_role_tasks"], e["calls"], e["distinct"], e["result_tokens"], e["exclusive_result_tokens"], e["probe"]))
    out.append("\n(b) new Bash lead commands in {}+ tasks matching no probe: {}".format(report["min_tasks"], len(report["new_leads"])))
    for e in report["new_leads"]:
        out.append("  {:<10} {:>7} tasks {:>4} calls {:>4} distinct {:>7} tok  {}".format(
            e["role"], e["tasks"], e["calls"], e["distinct"], e["result_tokens"], e["lead"]))
    out.append("\n(c) probes in fewer tasks (share of the role's tasks fell): {}".format(len(report["dropped"])))
    for e in report["dropped"]:
        out.append("  {:<10} {:>6} -> {:<6} tasks {}/{} (was {}/{}) calls {:>5} distinct {:>5} tok {:>7} any {:>7} excl  {}".format(
            e["role"], _pct(e["previous_share"]), _pct(e["share"]), e["tasks"], e["role_tasks"], e["previous"],
            e["previous_role_tasks"], e["calls"], e["distinct"], e["result_tokens"], e["exclusive_result_tokens"], e["probe"]))
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


def run(project, role="", since="", min_tasks=3, as_json=False, out="", until=""):
    """Mine PROJECT's tasks and return (report text, warnings). With `out`, write the report there and the
    new counts next to it (OUT.counts.json), after the report is on disk; then the returned text is a one-line note."""
    sessions = os.path.join(project, ".tmux-pi", "sessions")
    if not os.path.isdir(sessions):
        raise ValueError("no sessions directory at {}".format(sessions))
    for flag, value in (("--since", since), ("--until", until)):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("{} must be YYYY-MM-DD, got {!r}".format(flag, value))
    if min_tasks < 1:
        raise ValueError("--min-tasks must be at least 1")
    warnings = []
    previous = None
    if out:
        previous, why = load_state(counts_path(out))
        if why:
            warnings.append(why)
    saved = use_project_config(project)
    try:
        roles = mine(find_tasks(sessions, since=since, role=role or None, until=until))
        report = build_report(roles, previous, min_tasks, since=since, until=until, role=role, project=project)
    finally:
        restore_config(saved)
    body = json.dumps(report, indent=2) + "\n" if as_json else render_text(report)
    if not out:
        return body, warnings
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    write_atomic(out, body)
    write_atomic(counts_path(out), json.dumps(merged_state(previous, roles, min_tasks, report["generated"], role=role), indent=2) + "\n")
    return "wrote {} (counts in {})\n".format(out, counts_path(out)), warnings
