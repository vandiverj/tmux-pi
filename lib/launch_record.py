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
starts and never claims that it started. A tp-spawn failure before the record (or after it, before the task is registered) removes
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
    packet = (task_dir / "packet.md").read_text(encoding="utf-8").rstrip("\n")  # run.sh passes "$(cat packet.md)"
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
    reasons["preferences"] = "HB3 decision records are not applied at launch: no preference ids reach tp-spawn"
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


def main_record(state_dir, harness, main_id, argv, env=None, root=ROOT):
    """Main's own record (role main, route main) for bin/tp-main: the prompt files it appends, what tp-claude adds and points to
    (the Bash-suffix rule, its settings file with the hooks, the plugin directory), the harness, and the model and effort when the
    arguments name them. `start` is "resume" when the arguments resume a conversation (--resume/-r, --continue/-c), else "start".
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
    binary, version, why = resolve_harness(harness, env)
    if version is None:
        reasons["harness_version"] = why
    commit, dirty, tree_reasons = tmux_pi_tree(root)
    reasons.update(tree_reasons)
    model = _argv_value(argv, "--model")
    if model is None:
        reasons["model"] = "no --model on tp-main's command line: the harness's default or its own setting"
    effort = _argv_value(argv, "--effort" if harness == "claude" else "--thinking")
    if effort is None:
        reasons["effort"] = "no effort on tp-main's command line"
    reasons["preferences"] = "HB3 records reach Main through tp-agent brief, not at launch"
    adapter = preamble.HARNESS_ADAPTER[harness]
    resume = any(a in ("--resume", "-r", "--continue", "-c") or a.startswith("--resume=") for a in argv)
    record = {
        "schema": SCHEMA, "status": "prepared" if binary else "failed", "task": main_id, "role": "main", "harness": harness,
        "harness_version": version, "model": model, "effort": effort, "tmux_pi_commit": commit, "tmux_pi_dirty": dirty,
        "instructions": instructions, "tools": {"exposed": exposed, "denied": []},
        "capabilities": {"supported": list(adapter["supported"]), "unsupported": list(adapter["unsupported"])},
        "preferences": None,
        "context": {"brief": None, "parent": None, "lane": None, "step": None, "base_commit": None, "evidence": []},
        "route": "main", "start": "resume" if resume else "start",
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


def contract_version(record):
    return next((i.get("version") for i in record.get("instructions", []) if i.get("name") == "contract"), None)


def summary(record):
    """One short line for a reader: contract version, role, route, unsupported capabilities, and a failure when there is one."""
    parts = [f"contract {contract_version(record) or 'none'}", f"role {record.get('role') or 'none'}", f"route {record.get('route')}"]
    unsupported = (record.get("capabilities") or {}).get("unsupported") or []
    parts.append("unsupported " + ("+".join(unsupported) if unsupported else "none"))
    if record.get("status") == "failed":
        parts.insert(0, "FAILED (" + str(record.get("failure", "")) + ")")
    return ", ".join(parts)


def _main(argv):
    if argv[:1] == ["worker"]:
        facts = json.loads(sys.stdin.read())
        record = worker(facts)
        write(Path(facts["task_dir"]) / NAME, record, facts["state_dir"])
        return 0
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
        if options.get("harness") not in preamble.HARNESS_ADAPTER:
            raise RecordError("--harness must be pi or claude")
        write(Path(state) / MAIN_NAME, main_record(state, options["harness"], options.get("main-id") or None, rest,
                                                       root=options.get("root") or ROOT), state)
        return 0
    raise RecordError("usage: launch_record.py worker < facts.json | main --state-dir DIR --harness H --main-id ID -- ARGS")


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except (RecordError, ValueError, OSError, KeyError) as error:
        sys.exit(f"launch record: {error}")
