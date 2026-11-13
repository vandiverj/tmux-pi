"""Launch records (HB2b; docs/plans/det-prep/hub/interfaces.md section 2): what a worker or Main was started with.

    TASK_DIR/launch.json                        written by bin/tp-spawn for every worker it starts (every route), after the system
                                                prompt, packet and run.sh are written and before the tmux window that runs the harness
    <state dir>/main-launch.json                written by bin/tp-main at each start and recovery of Main (rewritten, not appended)

Schema `tp-launch:v1`. A record holds paths, sha256 hashes, names and versions only: never prompt text, environment values, tokens or
file contents (config/local.json is named by path, never read here). Each instruction piece's sha256 is the hash of the text the
harness was given, checked against the delivered system prompt or packet; a piece that cannot be found there gets sha256 null and a
reason. Every field a source cannot supply is null, with its reason under `null_reasons`, never a guess.

`status`: "prepared" when the harness binary resolves the way bin/tp-claude / bin/tp-pi would resolve it; "failed" (with `failure`)
when it does not, so a launch whose harness cannot start is never recorded as a good one. The record is written before the harness
starts and never claims that it started. A record error never stops a launch: status "error" with `error` (or one stderr line). A tp-spawn failure before the record (or after it, before the task is registered) removes
the task directory, so it leaves no record.

    python3 launch_record.py worker < facts.json      (bin/tp-spawn; facts below in `worker`)
    python3 launch_record.py main --state-dir DIR --harness pi|claude --main-id ID [--root DIR] -- HARNESS_ARGS...   (bin/tp-main)
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preamble  # noqa: E402
import safe_write  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = "tp-launch:v1"
MAJOR = re.compile(r"tp-launch:v([0-9]+)\Z")
NAME = "launch.json"
MAIN_NAME = "main-launch.json"
ROUTES = ("spawn", "retry", "review", "lane", "contract", "trial", "bench", "main", "subagent")
BENCH_SESSION_PREFIX = "bench-"  # lib/bench/runner.py SESSION_PREFIX: a bench session's name (the runner refuses any other)


class RecordError(ValueError):
    pass


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _shell_assignment(path, name):
    """The value of a one-line `name='...'` or `name=...` assignment in a shell script, or None (the text is the script's own)."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            match = re.match(r"^%s=(?:'([^']*)'|\"([^\"]*)\"|(\S*))\s*$" % re.escape(name), line)
            if match:
                return next(g for g in match.groups() if g is not None)
    except OSError:
        return None
    return None


def claude_suffix(root=ROOT):
    """The Bash-suffix rule bin/tp-claude appends to every Claude system prompt (its `suffix_instruction`)."""
    return _shell_assignment(Path(root) / "bin" / "tp-claude", "suffix_instruction")


def resolve_harness(harness, env):
    """(binary path or None, version or None, reason for a null version). Resolved as bin/tp-claude and bin/tp-pi resolve it, without
    running anything: claude from PATH; pi from TP_PI_NODE (or bin/tp-pi's pinned default) with pi beside it, or from PATH when
    TP_PI_NODE is set empty. The version is read from the install layout only (a Claude versions/<v> target, pi's package.json)."""
    path = env.get("PATH", "")
    if harness == "claude":
        found = shutil.which("claude", path=path)
        if not found:
            return None, None, "claude is not on PATH"
        real = os.path.realpath(found)
        if Path(real).parent.name == "versions":
            return found, Path(real).name, None
        return found, None, f"{real} is not a versions/<version> install; reading the version would mean running it"
    if "TP_PI_NODE" in env:
        node = env["TP_PI_NODE"]
    else:
        default = _shell_assignment(ROOT / "bin" / "tp-pi", "PI_NODE")
        match = re.fullmatch(r"\$\{TP_PI_NODE-(.*)\}", default or "")
        if not match:
            return None, None, "bin/tp-pi has no PI_NODE default this reader knows"
        node = match.group(1).replace("$HOME", env.get("HOME", ""))
    if not node:
        found = shutil.which("pi", path=path)
        if not found:
            return None, None, "pi is not on PATH (TP_PI_NODE is empty)"
        package = None
        for parent in list(Path(os.path.realpath(found)).parents)[:4]:
            candidate = parent / "package.json"
            if candidate.is_file():
                package = candidate
                break
    else:
        beside = Path(node).parent / "pi"
        if not (os.access(node, os.X_OK) and beside.exists()):
            return None, None, f"pi is pinned to {node} with pi beside it, and that is not installed"
        found = str(beside)
        package = Path(node).parent.parent / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "package.json"
    try:
        data = json.loads(package.read_text(encoding="utf-8")) if package else {}
        if data.get("name") == "@earendil-works/pi-coding-agent" and isinstance(data.get("version"), str):
            return found, data["version"], None
    except (OSError, ValueError):
        pass
    return found, None, f"no pi package.json beside {found}"


def tmux_pi_tree(root=ROOT):
    """(commit, dirty, reasons) of this tmux-pi checkout; dirty counts tracked files only (untracked files are not delivered)."""
    reasons = {}
    try:
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        status = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
                                capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        reasons["tmux_pi_commit"] = reasons["tmux_pi_dirty"] = f"git did not run: {error}"
        return None, None, reasons
    head = commit.stdout.strip() if commit.returncode == 0 else None
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    if head is None:
        reasons["tmux_pi_commit"] = f"{root} is not a git checkout git can read"
    if dirty is None:
        reasons["tmux_pi_dirty"] = "git status failed"
    return head, dirty, reasons


def _rel(path):
    """A path under tmux-pi as a repository path, else absolute."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _piece(name, path, text, delivered, version=None, reasons=None, where="system prompt"):
    """One instruction piece. sha256 is of `text`, but only when that text is in what the harness was given."""
    entry = {"name": name, "path": path, "sha256": sha256(text) if text and text in delivered else None, "version": version}
    if entry["sha256"] is None and reasons is not None:
        reasons[f"instructions[{name}].sha256"] = f"the piece is not in the delivered {where}"
    return entry


def route_of(facts):
    """The launch route, from what tp-spawn was given (no caller names it): the review of a task (--review-of), a continuation of a
    parent (--parent: tp-agent retry, dispatch --parent and dispatch --investigate-failure all reach tp-spawn this way), a shadow trial
    (--worker-env TP_TRIAL_ID), a contract task (-k), a lane step (--lane), a start in a bench session (lib/bench/runner.py names
    the session bench-*), else a plain spawn. The first that applies wins; parent, lane and step are kept in `context`."""
    env = dict(pair.split("=", 1) for pair in facts.get("worker_env", []) if "=" in pair)
    if facts.get("review_of"):
        return "review"
    if facts.get("parent"):
        return "retry"
    if env.get("TP_TRIAL_ID"):
        return "trial"
    if facts.get("kind"):
        return "contract"
    if facts.get("lane"):
        return "lane"
    if str(facts.get("session", "")).startswith(BENCH_SESSION_PREFIX):
        return "bench"
    return "spawn"


def _argv_value(argv, flag):
    for i, arg in enumerate(argv):
        if arg == flag and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith(flag + "="):
            return arg[len(flag) + 1:]
    return None


def _pane_env(env, line):
    """`env` with TP_PI_NODE as the pane will have it: `line` is tmux show-environment's answer from tp-spawn (`TP_PI_NODE=value`,
    `-TP_PI_NODE` removed, `unset` when tmux has neither), or "" when tmux did not say (then tp-spawn's own environment stands)."""
    if line.startswith("TP_PI_NODE="):
        return {**env, "TP_PI_NODE": line[len("TP_PI_NODE="):]}
    if line in ("-TP_PI_NODE", "unset"):
        return {k: v for k, v in env.items() if k != "TP_PI_NODE"}
    return env


def worker(facts, env=None):
    """The launch record of a worker from tp-spawn's facts: id, task_dir, state_dir, workdir, harness, role, preamble_role, model,
    launch_model, parent, review_of, kind, lane, lane_step, preamble_from, session, worker_env (KEY=VAL list), ctx_tools (bool),
    argv (the harness command without the packet), evidence (the --context files the packet holds)."""
    env = os.environ if env is None else env
    reasons = {}
    harness = facts["harness"]
    task_dir = Path(facts["task_dir"])
    system = (task_dir / "system-prompt.md").read_text(encoding="utf-8") if (task_dir / "system-prompt.md").exists() else ""
    packet = (task_dir / "packet.md").read_text(encoding="utf-8").rstrip("\n")  # run.sh passes "$(cat packet.md)" (claude) or @packet.md (pi): the same text
    role_text = facts.get("role") or facts.get("preamble_role") or ""
    source = facts.get("preamble_from") or None
    workdir = facts["workdir"]

    instructions = []
    contract = preamble.contract(harness)
    first = preamble.CONTRACT.read_text(encoding="utf-8").strip().splitlines()[0]
    version = "v" + preamble.CONTRACT_VERSION.match(first).group(1)  # preamble.contract checked the line above
    instructions.append(_piece("contract", _rel(preamble.CONTRACT), contract, system, version, reasons))
    text, tools_block = preamble._parts(workdir, role_text, source)
    if text:
        path = Path(workdir) / preamble.RELATIVE
        path = path if preamble._present(path) or not source else Path(source) / preamble.RELATIVE
        instructions.append(_piece("preamble", str(path), "# Repository Instructions\n\n" + text, system, None, reasons))
    if tools_block:
        import agent_tools
        instructions.append(_piece("manifest", str(agent_tools.manifest_path(workdir, source)), tools_block, system, None, reasons))
    prior = preamble._prior_line(facts.get("state_dir"), facts.get("parent"))
    if prior:
        instructions.append(_piece("prior-task", str(Path(facts["state_dir"]) / "tasks" / facts["parent"]), "# Prior Task\n\n" + prior,
                                   system, None, reasons))
    standing = preamble.system_text(workdir, role_text, source, harness, facts.get("state_dir"), facts.get("parent"))
    if system.startswith(standing) and len(system) > len(standing) + 2:
        instructions.append(_piece("model-prompt", "config/local.json", system[len(standing) + 2:], system, None, reasons))
    if harness == "claude":
        suffix = claude_suffix()
        entry = {"name": "bash-suffix-rule", "path": "bin/tp-claude", "sha256": sha256(suffix) if suffix else None, "version": None}
        if suffix is None:
            reasons["instructions[bash-suffix-rule].sha256"] = "bin/tp-claude has no one-line suffix_instruction"
        instructions.append(entry)
    if role_text:
        role_path = ROOT / "agent" / "roles" / f"{role_text}.md"
        body = role_path.read_text(encoding="utf-8").rstrip("\n") if role_path.is_file() else ""
        instructions.append(_piece("role", _rel(role_path), "# Worker Role\n\n" + body, packet, None, reasons, "packet"))
        if instructions[-1]["sha256"]:
            instructions[-1]["sha256"] = sha256(body)
    instructions.append({"name": "system-prompt", "path": str(task_dir / "system-prompt.md"), "sha256": sha256(system) if system else None,
                         "version": None})
    if not system:
        reasons["instructions[system-prompt].sha256"] = "no system prompt was given"
    instructions.append({"name": "packet", "path": str(task_dir / "packet.md"), "sha256": sha256(packet), "version": None})

    argv = list(facts.get("argv", []))
    adapter = preamble.HARNESS_ADAPTER[harness]
    exposed = []
    passed = _argv_value(argv, "--tools")
    if passed:
        exposed += [t for t in passed.split(",") if t]
    elif harness == "pi":
        reasons["tools.exposed"] = "pi starts with its built-in default tools (tp-spawn passes no --tools); only adapter tools are listed"
    exposed += adapter["tools"]
    worker_env = dict(pair.split("=", 1) for pair in facts.get("worker_env", []) if "=" in pair)
    if worker_env.get("TP_DOCS_TOOLS") == "1":
        exposed += adapter["docs_tools"]
    if facts.get("ctx_tools"):
        exposed += adapter["ctx_tools"]
    if harness == "claude":
        reasons["tools.exposed_mcp"] = "claude/mcp.json may name more MCP servers; it is not read here"

    binary, version, why = resolve_harness(harness, _pane_env(env, facts.get("pane_pi_node") or ""))
    if version is None:
        reasons["harness_version"] = why
    commit, dirty, tree_reasons = tmux_pi_tree()
    reasons.update(tree_reasons)
    model = facts.get("model") or None
    if model is None:
        reasons["model"] = "no --model: the harness's own default"
    effort = _argv_value(argv, "--effort" if harness == "claude" else "--thinking")
    if effort is None:
        reasons["effort"] = "no effort was passed: the harness's own default"
    if not role_text:
        reasons["role"] = "started with no role (no --role or --preamble-role)"
    reasons["preferences"] = "the owner's rules reach the worker as the packet section `# Rules in force` (HB3b), not as preference ids, so there are none to record here"
    base = worker_env.get("LANE_BASE") or None
    if base is None:
        reasons["context.base_commit"] = "only a lane task is given its base (LANE_BASE)"
    record = {
        "schema": SCHEMA, "status": "prepared" if binary else "failed", "task": facts["id"], "role": role_text or None,
        "harness": harness, "harness_version": version, "model": model,
        **({"launch_model": facts["launch_model"]} if facts.get("launch_model") and facts.get("launch_model") != model else {}),
        "effort": effort, "tmux_pi_commit": commit, "tmux_pi_dirty": dirty,
        "instructions": instructions,
        "tools": {"exposed": exposed, "denied": []},
        "capabilities": {"supported": list(adapter["supported"]), "unsupported": list(adapter["unsupported"])},
        "preferences": None,
        "context": {"brief": str(task_dir / "request.md"), "parent": facts.get("parent") or facts.get("review_of") or None,
                    "lane": facts.get("lane") or None, "step": facts.get("lane_step") or None, "base_commit": base,
                    "evidence": list(facts.get("evidence", []))},
        "route": route_of(facts),
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "null_reasons": reasons,
    }
    if not binary:
        record["failure"] = f"the {harness} harness binary is missing: {why}"
    return record


def _options(arity, cls, *names):
    return {name: (arity, cls) for name in names}


# The options of each harness this record knows: name -> (arity, class). Arity: 0 a switch; 1 the next token whatever it starts with;
# "?" the next token when it does not start with "-"; "+" the next token whatever it starts with, then each following token up to
# one that starts with "-" (claude's `<x...>` options; their `--x=v` form is one value). Class: what the record makes of the value (None: kept in caller_args only).
# Sources: bin/tp-main, bin/tp-claude, bin/tp-pi, bin/tp-spawn and lib/llm_router.py (the options tmux-pi itself passes), claude's
# option definitions in its install (read, not run), and for pi the HB2b round 4 brief (pi is not started and is not installed here).
# Any other option is unknown: its name is kept, never classified, and its value (it may be a secret) only as sha256 and length.
HARNESS_OPTIONS = {
    "claude": {
        **_options(1, "instruction-file", "--append-system-prompt-file", "--system-prompt-file"),
        **_options(1, "instruction-text", "--append-system-prompt", "--system-prompt"),
        **_options(1, "settings", "--settings", "--agents"),
        **_options("+", "settings", "--mcp-config"),
        **_options(1, "dir", "--plugin-dir"),
        **_options("+", "tools-exposed", "--tools"),
        **_options("+", "tools-allowed", "--allowedTools", "--allowed-tools"),
        **_options("+", "tools-denied", "--disallowedTools", "--disallowed-tools"),
        **_options(1, "model", "--model"),
        **_options(1, "effort", "--effort"),
        **_options("?", "resume", "-r", "--resume"),
        **_options(0, "resume", "-c", "--continue"),
        **_options(1, None, "--setting-sources", "--agent", "--fallback-model", "--permission-mode", "--session-id", "-n", "--name",
                   "--output-format", "--input-format", "--max-turns", "--max-budget-usd", "--json-schema", "--debug-file",
                   "--permission-prompt-tool"),
        **_options("+", None, "--add-dir", "--betas", "--file"),
        **_options("?", None, "-d", "--debug", "--from-pr"),
        **_options(0, None, "-p", "--print", "--strict-mcp-config", "--verbose", "--dangerously-skip-permissions",
                   "--allow-dangerously-skip-permissions", "--no-session-persistence", "--fork-session", "--ide", "--chrome",
                   "--no-chrome", "--bare", "--disable-slash-commands", "--include-partial-messages", "--replay-user-messages"),
    },
    "pi": {
        **_options(1, "instruction-text", "--append-system-prompt", "--system-prompt"),
        **_options(1, "extension", "-e", "--extension"),
        **_options(1, "skill", "--skill"),
        **_options(1, "prompt-template", "--prompt-template"),
        **_options(1, "tools-exposed", "--tools"),
        **_options(1, "model", "--model"),
        **_options(1, "effort", "--thinking"),
        **_options(0, "resume", "-r", "--resume", "-c", "--continue"),
        **_options(1, "secret", "--api-key"),
        **_options(1, None, "--provider", "--mode", "--session", "--session-dir", "--models", "--export", "--name", "--theme"),
        **_options("?", None, "--list-models"),
        **_options(0, None, "-p", "--print", "--no-session", "--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
                   "--no-context-files", "--no-mcp", "--no-themes", "--verbose", "--offline"),
    },
}
# Classes whose value is instruction text or a secret: never stored as given (text by sha256, a secret not at all).
TEXT_CLASSES = ("instruction-text",)
LONG_VALUE = 200


def _parse_caller(argv, harness):
    """One left-to-right pass over the caller's arguments: [(option or None for a positional, [values], known, value_guess)]. A known
    option consumes its values (by its arity) before the next token is looked at, so a value is never read as an option; `--` ends the
    options (what follows is positional); an unknown `--x=v` is one entry, an unknown `--x` takes the next token only when that token
    does not start with "-", and marks it a guess."""
    table = HARNESS_OPTIONS[harness]
    parsed, i = [], 0
    while i < len(argv):
        arg = argv[i]
        i += 1
        if arg == "--":
            parsed += [(None, [rest], True, False) for rest in argv[i:]]
            break
        if not arg.startswith("-") or arg == "-":
            parsed.append((None, [arg], True, False))
            continue
        flag, eq, inline = arg.partition("=") if arg.startswith("--") else (arg, "", "")
        if flag not in table:
            if eq:
                parsed.append((flag, [inline], False, False))
            elif i < len(argv) and not argv[i].startswith("-"):
                parsed.append((arg, [argv[i]], False, True))
                i += 1
            else:
                parsed.append((arg, [], False, False))
            continue
        arity = table[flag][0]
        if eq:
            values = [inline]
        elif arity == 1:
            values = argv[i:i + 1]
        elif arity == "?":
            values = argv[i:i + 1] if i < len(argv) and not argv[i].startswith("-") else []
        elif arity == "+":
            values = argv[i:i + 1]
            while values and i + len(values) < len(argv) and not argv[i + len(values)].startswith("-"):
                values.append(argv[i + len(values)])
        else:
            values = []
        if not eq:
            i += len(values)
        parsed.append((flag, values, True, False))
    return parsed


def _stored(value, cls):
    """A value as caller_args keeps it. Known options: as given, or by sha256 only when it is instruction text, a positional (a prompt),
    longer than LONG_VALUE or JSON-looking; a secret is not kept at all. An unknown option (cls "unknown"): never as given, only
    sha256 and length."""
    if cls == "secret":
        return {"value": None, "redacted": True}
    if cls == "unknown":
        return {"value_sha256": sha256(value), "value_length": len(value)}
    if cls in TEXT_CLASSES or cls == "positional" or len(value) > LONG_VALUE or value.lstrip().startswith(("{", "[")):
        return {"value_sha256": sha256(value)}
    return {"value": value}


def main_caller_args(argv, harness):
    """(caller_args, parsed): the record's ordered list of what the caller passed, one entry per option or positional, and the parse it
    is made from (main_caller_inputs classifies that same parse)."""
    table = HARNESS_OPTIONS[harness]
    parsed = _parse_caller(argv, harness)
    entries = []
    for flag, values, known, guess in parsed:
        if flag is None:
            entries.append({"positional": True, **_stored(values[0], "positional")})
            continue
        cls = table[flag][1] if known else "unknown"
        entry = {"option": flag}
        if not known:
            entry["known"] = False
        if known and table[flag][0] == "+" and len(values) > 1:
            entry["values"] = [_stored(v, cls) for v in values]
        elif values:
            entry.update(_stored(values[0], cls))
        if guess:
            entry["value_guess"] = True
        entries.append(entry)
    return entries, parsed


def _sha256_file(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest(), None
    except OSError as error:
        return None, f"{type(error).__name__}: cannot be read"


def main_caller_inputs(argv, harness, reasons):
    """What the caller of tp-main passed, from one parse of its arguments (main_caller_args): (caller_args, instruction entries,
    {"exposed": list|None, "allowed": [...], "denied": [...], "model", "effort", "resume"}). Only known options are classified: files,
    extensions, skills and prompt templates by path and sha256 of a file (an mcp config or settings file is hashed, never printed),
    directories by path, text and inline JSON by sha256 only."""
    table = HARNESS_OPTIONS[harness]
    caller_args, parsed = main_caller_args(argv, harness)
    entries = []
    found = {"exposed": None, "allowed": [], "denied": [], "model": None, "effort": None, "resume": False}
    for flag, values, known, _guess in parsed:
        if flag is None or not known:
            continue
        cls = table[flag][1]
        if cls == "resume":
            found["resume"] = True
        for value in values:
            name, key = "caller:" + flag, f"instructions[caller:{flag}#{len(entries)}]"
            if cls == "model" or cls == "effort":
                found[cls] = value   # the last one given is the one the harness keeps
            elif cls == "tools-exposed":
                found["exposed"] = (found["exposed"] or []) + [t for t in re.split(r"[,\s]+", value) if t]
            elif cls in ("tools-allowed", "tools-denied"):
                found[cls[len("tools-"):]].append(value)
            elif cls in TEXT_CLASSES or (cls == "settings" and value.lstrip().startswith(("{", "["))):
                entries.append({"name": name, "path": None, "sha256": sha256(value), "version": None})
                reasons[key + ".path"] = "given as text on the command line; no file holds it"
            elif cls == "extension" and value.startswith("builtin:"):
                entries.append({"name": name, "path": value, "sha256": None, "version": None})
                reasons[key + ".sha256"] = "a built-in extension of the harness, named, not a file"
            elif cls in ("instruction-file", "settings", "dir", "extension", "skill", "prompt-template"):
                if Path(value).is_dir():
                    entries.append({"name": name, "path": value, "sha256": None, "version": None})
                    reasons[key + ".sha256"] = "a directory"
                    continue
                digest, why = _sha256_file(value)
                entries.append({"name": name, "path": value, "sha256": digest, "version": None})
                if why:
                    reasons[key + ".sha256"] = why
    if any(not known for _flag, _values, known, _guess in parsed if _flag is not None):
        reasons["caller_args.unknown"] = ("an option this record does not know is kept by name and not classified, its value only as sha256 and length; a value after it "
                                          "is marked value_guess (its arity is not known)")
    return caller_args, entries, found


def main_record(state_dir, harness, main_id, argv, env=None, root=ROOT):
    """Main's own record (role main, route main) for bin/tp-main: the prompt files it appends, what tp-claude adds and points to
    (the Bash-suffix rule, its settings file with the hooks, the plugin directory), the harness, and the model and effort when the
    arguments name them. `start` is "resume" when the arguments resume a conversation (--resume/-r, --continue/-c), else "start".
    `caller_args`: every argument the caller passed (main_caller_args); instructions, tools, model, effort and start come from that parse.
    `root`: the tmux-pi checkout of the tp-main that is starting (its prompt files, launchers and git state)."""
    env = os.environ if env is None else env
    root = Path(root)
    reasons = {}
    core = (root / "agent" / "orchestrator.md").read_text(encoding="utf-8").rstrip("\n")
    own = (root / "agent" / f"orchestrator-{harness}.md").read_text(encoding="utf-8").rstrip("\n")
    instructions = [{"name": "orchestrator", "path": "agent/orchestrator.md", "sha256": sha256(core), "version": None},
                    {"name": f"orchestrator-{harness}", "path": f"agent/orchestrator-{harness}.md", "sha256": sha256(own), "version": None},
                    {"name": "system-prompt", "path": None, "sha256": sha256(core + "\n\n" + own), "version": None}]
    reasons["instructions[system-prompt].path"] = "bin/tp-main passes the text on the command line; no file holds it"
    exposed = None
    if harness == "claude":
        suffix = claude_suffix(root)
        instructions.append({"name": "bash-suffix-rule", "path": "bin/tp-claude", "sha256": sha256(suffix) if suffix else None,
                             "version": None})
        if suffix is None:
            reasons["instructions[bash-suffix-rule].sha256"] = "bin/tp-claude has no one-line suffix_instruction"
        instructions.append({"name": "hooks-settings", "path": "claude/settings.json", "sha256": None, "version": None})
        reasons["instructions[hooks-settings].sha256"] = "bin/tp-claude composes Main's settings from this file (bashOutputMaxChars)"
        instructions.append({"name": "plugins", "path": "claude/plugins", "sha256": None, "version": None})
        reasons["instructions[plugins].sha256"] = "a directory"
        reasons["tools.exposed"] = "Claude's default tool set (tp-main passes no --tools; --allowedTools Bash(tp-agent:*) only skips prompts)"
    else:
        listed = _shell_assignment(root / "bin" / "tp-pi", "MAIN_TOOLS")
        exposed = listed.split(",") if listed else None
        if exposed is None:
            reasons["tools.exposed"] = "bin/tp-pi has no one-line MAIN_TOOLS"
    caller_args, extra, found = main_caller_inputs(argv, harness, reasons)
    instructions += extra
    if found["exposed"] is not None:
        exposed = found["exposed"]
        reasons.pop("tools.exposed", None)
    tools = {"exposed": exposed, "denied": found["denied"]}
    if found["allowed"]:
        tools["allowed"] = found["allowed"]
    binary, version, why = resolve_harness(harness, env)
    if version is None:
        reasons["harness_version"] = why
    commit, dirty, tree_reasons = tmux_pi_tree(root)
    reasons.update(tree_reasons)
    model = found["model"]
    if model is None:
        reasons["model"] = "no --model on tp-main's command line: the harness's default or its own setting"
    effort = found["effort"]
    if effort is None:
        reasons["effort"] = "no effort on tp-main's command line"
    reasons["preferences"] = "HB3 records reach Main through tp-agent brief, not at launch"
    adapter = preamble.HARNESS_ADAPTER[harness]
    record = {
        "schema": SCHEMA, "status": "prepared" if binary else "failed", "task": main_id, "role": "main", "harness": harness,
        "harness_version": version, "model": model, "effort": effort, "tmux_pi_commit": commit, "tmux_pi_dirty": dirty,
        "instructions": instructions, "tools": tools, "caller_args": caller_args,
        "capabilities": {"supported": list(adapter["supported"]), "unsupported": list(adapter["unsupported"])},
        "preferences": None,
        "context": {"brief": None, "parent": None, "lane": None, "step": None, "base_commit": None, "evidence": []},
        "route": "main", "start": "resume" if found["resume"] else "start",
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "null_reasons": reasons,
    }
    if not binary:
        record["failure"] = f"the {harness} harness binary is missing: {why}"
    return record


def write(path, record, root):
    """Atomic: a reader sees the old record or the new one, never a part."""
    safe_write.write_atomic(path, json.dumps(record, indent=2) + "\n", root=root)


def read(path):
    """The record at `path`, None when there is none; RecordError for one this reader cannot use (unreadable, another major)."""
    path = Path(path)
    try:
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecordError(f"{path.name} unreadable: {error}")
    match = MAJOR.match(str(record.get("schema", ""))) if isinstance(record, dict) else None
    if not match:
        raise RecordError(f"{path.name} has no tp-launch schema")
    if match.group(1) != "1":
        raise RecordError(f"{path.name} is {record['schema']}; this reader knows tp-launch:v1 only")
    return record


def summary(record):
    """One short line for a reader: contract version, role, route, unsupported capabilities, and a failure when there is one."""
    # each field is a value (the record holds it), "none" (it holds the field, empty) or "unknown" (it does not hold the field)
    def scalar(key):
        return "unknown" if key not in record else (str(record[key]) if record[key] else "none")
    instructions = record.get("instructions")
    if not isinstance(instructions, list):
        contract = "unknown"
    else:
        entry = next((i for i in instructions if isinstance(i, dict) and i.get("name") == "contract"), None)
        if entry is None:
            contract = "none"
        else:
            contract = "unknown" if "version" not in entry else (str(entry["version"]) if entry["version"] else "none")
    capabilities = record.get("capabilities")
    unsupported = capabilities.get("unsupported") if isinstance(capabilities, dict) else None
    if not isinstance(unsupported, list):
        unsupported = "unknown"
    else:
        unsupported = "+".join(str(u) for u in unsupported) or "none"
    parts = [f"contract {contract}", f"role {scalar('role')}", f"route {scalar('route')}", f"unsupported {unsupported}"]
    if record.get("status") == "failed":
        parts.insert(0, "FAILED (" + str(record.get("failure", "")) + ")")
    if record.get("status") == "error":
        parts.insert(0, "ERROR (" + str(record.get("error", "")) + ")")
    return ", ".join(parts)


def _run(argv, facts_text=None):
    """Build and write the record the arguments name. Returns (path, known) as soon as they are known, so a failure after that point
    can still leave an error record at the same path."""
    if argv[:1] == ["worker"]:
        facts = json.loads(facts_text if facts_text is not None else sys.stdin.read())
        known = {"task": facts.get("id"), "role": facts.get("role") or facts.get("preamble_role") or None,
                 "harness": facts.get("harness")}
        return Path(facts["task_dir"]) / NAME, facts["state_dir"], known, lambda: worker(facts)
    if argv[:1] == ["main"]:
        options, rest = {}, []
        i = 1
        while i < len(argv):
            if argv[i] == "--":
                rest = argv[i + 1:]
                break
            if argv[i] in ("--state-dir", "--harness", "--main-id", "--root") and i + 1 < len(argv):
                options[argv[i][2:]] = argv[i + 1]
                i += 2
                continue
            raise RecordError(f"unknown argument {argv[i]}")
        state = options.get("state-dir")
        if not state or not Path(state).is_dir():
            raise RecordError("--state-dir must be an existing directory")
        known = {"task": options.get("main-id") or None, "role": "main", "harness": options.get("harness"), "route": "main"}
        if options.get("harness") not in preamble.HARNESS_ADAPTER:
            raise RecordError("--harness must be pi or claude")
        return (Path(state) / MAIN_NAME, state, known,
                lambda: main_record(state, options["harness"], options.get("main-id") or None, rest, root=options.get("root") or ROOT))
    raise RecordError("usage: launch_record.py worker < facts.json | main --state-dir DIR --harness H --main-id ID -- ARGS")


def _main(argv):
    """Always 0: a launch record never stops a launch. Any error leaves an error record at the same path (atomic) with what is known,
    or, when even that cannot be written, one line on stderr."""
    path = state = None
    known = {}
    try:
        path, state, known, build = _run(argv)
        write(path, build(), state)
    except Exception as error:  # noqa: BLE001 -- nothing here may change the caller's exit code
        message = f"{type(error).__name__}: {error}"
        try:
            if path is None:
                raise RecordError("no record path is known")
            write(path, {"schema": SCHEMA, "status": "error", "error": message, **{k: v for k, v in known.items() if v is not None},
                         "recorded": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, state)
        except Exception:  # noqa: BLE001
            print(f"launch record: {message}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
