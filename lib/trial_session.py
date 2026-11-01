"""The trial's own tmux session and its capacity guard (design §3, §8, §10).

A shadow or replay run lives in a detached tmux session `tp-trial-<run>` whose `@tp_state_dir` is
<project>/.tmux-pi/trial/<run>/state. lib/tp.sh `tp_session` refuses a state dir that no session owns, so a separate session is
the only way to a separate state dir: Main's task index, `max_active_workers`, collect notices and lane gate never see a trial
task, and a trial never writes into Main's state dir.

`start_tp` is the one gate every tp-* command of the trial goes through (its table `TP` says what each can do); a command
that can end anything runs only after the run's own check passes, and one the table does not name is never started.

`env_for` is the one place that builds the environment every trial subprocess (tp-spawn, tp-agent, git) gets: TP_STATE_DIR and
TP_SESSION name the trial's state and session, TMUX and TMUX_PANE are removed (inside tmux, `tp_session` would otherwise pick
the caller's own session), and nothing that points at Main's state survives.

The guard (`guard`) is §8/§10.7: a trial local run starts only while Main holds fewer than its `max_local_workers` local pi
tasks, and while the trial's own local runs (running tasks, and runs that are queued or starting) are under
`trial.max_local_workers`. Main's count is read with `tp-agent list`, the
same classifier Main's own capacity check uses; nothing in Main's state dir is written.
"""

import errno
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PREFIX = "tp-trial-"
# Same prefixes as bin/tp-agent's LOCAL (the models that run on this machine); bin/tp-agent may not be imported or edited.
LOCAL = ("slotstream/", "lmstudio/", "mlx/", "ollama/")
ACTIVE = {"starting", "working", "needs_input"}
CONFIG = ROOT / "config" / "orchestration.json"
TRIAL_DEFAULTS = {"max_local_workers": 1, "attempt_timeout_s": 2700, "model": "lmstudio/qwen/qwen3.6-35b-a3b", "thinking": "medium"}
THINKING = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
TMUX_TIMEOUT = 30


class SessionError(ValueError):
    """The trial session or its guard refuses; the message says what and how to clear it."""


def valid_run(run):
    if not isinstance(run, str) or not RUN.fullmatch(run):
        raise SessionError("a trial run id is 1-40 characters of a-z, 0-9 and '-', starting with a letter or digit: {!r}".format(run))
    return run


def session_name(run):
    return PREFIX + valid_run(run)


def trial_root(project):
    return Path(project) / ".tmux-pi" / "trial"


def run_dir(project, run):
    return trial_root(project) / valid_run(run)


def state_dir(project, run):
    return run_dir(project, run) / "state"


def _positive(value, key, shown):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SessionError("{} {!r} must be a whole number of at least 1".format(shown, value))
    return value


def load_config(path=None):
    """(trial settings, Main's max_local_workers) from config/orchestration.json. A missing `trial` block gives the defaults; a
    malformed one refuses and names the key."""
    path = Path(path or CONFIG)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except (OSError, ValueError) as error:
        raise SessionError("cannot read {}: {}".format(path, error))
    if not isinstance(value, dict):
        raise SessionError("{} is not an object".format(path))
    block = value.get("trial", {})
    if not isinstance(block, dict):
        raise SessionError("{}: trial must be an object".format(path))
    unknown = sorted(set(block) - set(TRIAL_DEFAULTS))
    if unknown:
        raise SessionError("{}: unknown trial key(s): {}".format(path, ", ".join(unknown)))
    trial = {**TRIAL_DEFAULTS, **block}
    shown = "{}: trial.".format(path)
    _positive(trial["max_local_workers"], "max_local_workers", shown + "max_local_workers")
    _positive(trial["attempt_timeout_s"], "attempt_timeout_s", shown + "attempt_timeout_s")
    if not isinstance(trial["model"], str) or not trial["model"]:
        raise SessionError(shown + "model must be a model name")
    if trial["thinking"] not in THINKING:
        raise SessionError("{}thinking must be one of {}".format(shown, ", ".join(THINKING)))
    main = value.get("max_local_workers", 2)
    _positive(main, "max_local_workers", "{}: max_local_workers".format(path))
    return trial, main


def env_for(state, name, base=None, extra=None):
    """The environment for a command run against the trial's state dir `state` and session `name`."""
    env = dict(os.environ if base is None else base)
    # TP_DOCS_TOOLS and TP_TRIAL_ID reach a worker only through tp-spawn --worker-env (written into that worker's run.sh), never
    # through an inherited environment: a value in the caller's would otherwise follow into the session and its other panes.
    for key in ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_WORK_DIR", "TP_DOCS_TOOLS", "TP_TRIAL_ID"):
        env.pop(key, None)
    env["TP_STATE_DIR"] = str(state)
    env["TP_SESSION"] = name
    env.update(extra or {})
    return env


def _tmux(args, env=None, check=True):
    run = subprocess.run(["tmux"] + list(args), text=True, capture_output=True, env=env, timeout=TMUX_TIMEOUT)
    if check and run.returncode:
        raise SessionError("tmux {} failed: {}".format(args[0], (run.stderr or run.stdout).strip() or "exit {}".format(run.returncode)))
    return run


def has_session(name, env=None):
    return _tmux(["has-session", "-t", "=" + name], env, check=False).returncode == 0


def session_option(name, option, env=None):
    """The value of the session option `option` of the session now called `name` ("" when unset or no such session)."""
    run = _tmux(["show-options", "-qv", "-t", "=" + name + ":", option], env, check=False)
    return run.stdout.strip() if run.returncode == 0 else ""


def session_state(name, env=None):
    return session_option(name, "@tp_state_dir", env)


SEP = "|"
FIELDS = "#{session_id}" + SEP + "#{session_created}" + SEP + "#{session_name}" + SEP + "#{@tp_state_dir}"


def ensure(project, run, env=None, made=None):
    """Create or attach `tp-trial-<run>` with its own `@tp_state_dir` (<project>/.tmux-pi/trial/<run>/state). An existing session
    is reused only when it already names this state dir; one that names another is refused, never taken over. Returns the
    session name and the state dir. When this call itself creates the session and `made` is a list, (session id, created time)
    of that session (what tmux printed when it made it) is appended: the only proof a caller has that the session is its own."""
    name = session_name(run)
    base = run_dir(project, run)
    state = state_dir(project, run)
    for directory in (base, state, state / "tasks"):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise SessionError("{} is a symbolic link; the trial's own directories are never links".format(directory))
    env = env_for(state, name, env)
    if not has_session(name, env):
        created = _tmux(["new-session", "-d", "-P", "-F", FIELDS, "-s", name, "-n", "trial", "-c", str(base), "tail -f /dev/null"], env, check=False)
        if created.returncode and not has_session(name, env):  # lost a race to another start of the same run, or a real failure
            raise SessionError("cannot create tmux session {}: {}".format(name, created.stderr.strip() or "exit {}".format(created.returncode)))
        parts = created.stdout.strip().split(SEP, 3)
        if created.returncode == 0 and made is not None and len(parts) == 4 and parts[0].startswith("$") and parts[1].isdigit():
            made.append((parts[0], parts[1]))
    owned = session_state(name, env)
    if owned and Path(owned).resolve() != state.resolve():
        raise SessionError("tmux session {} keeps its state in {}, not {}; it is not this run's".format(name, owned, state))
    if not owned:
        _tmux(["set-option", "-t", name, "@tp_state_dir", str(state)], env)
        _tmux(["set-option", "-t", name, "@tp_root", str(ROOT)], env)
        _tmux(["set-option", "-t", name, "@tp_workdir", str(Path(project))], env)
        _tmux(["set-option", "-t", name, "@tp_trial", run], env)
        _tmux(["set-environment", "-t", name, "TP_STATE_DIR", str(state)], env)
        _tmux(["set-environment", "-t", name, "PATH", "{}:{}".format(ROOT / "bin", os.environ.get("PATH", ""))], env)
        _tmux(["set-environment", "-t", name, "PI_CODING_AGENT_DIR", str(ROOT / "agent")], env)
    return name, state




def identity_of(target, env=None):
    """(session id, created time, session name, @tp_state_dir) of the session that holds tmux `target` (a session `$id`, a `=name:` or
    a pane `%id`), or None when tmux does not know it. A session id is not given to a second session while the tmux server lives;
    the created time tells a session of the same id apart after a server restart. The name and state dir are only informative: a
    worker can rename a session, so they are never what decides whether it is the run's."""
    run = _tmux(["display-message", "-p", "-t", target, FIELDS], env, check=False)
    if run.returncode:
        return None
    parts = run.stdout.rstrip("\n").split(SEP, 3)  # the state dir is last: a path may hold the separator
    return tuple(parts) if len(parts) == 4 and parts[0].startswith("$") and parts[1].isdigit() else None


def named(name, env=None):
    """The identity (see `identity_of`) of the session now called `name`, or None."""
    return identity_of("=" + name + ":", env)


def is_session(sid, created, env=None):
    """Whether `sid` is still the session this run created: the same id and the same created time."""
    found = identity_of(sid, env)
    return found is not None and found[0] == sid and found[1] == str(created)


def end_exact(sid, created, env=None):
    """Kill the session `sid` only when it still is the session recorded for this run (`is_session`). Returns True when it was ended,
    False when no session has that id any more; raises SessionError when the id now belongs to a session that is not this run's,
    which is left alone."""
    found = identity_of(sid, env)
    if found is None:
        return False
    if not is_session(sid, created, env):
        raise SessionError("tmux session {} ({}) is not the session this run created ({} created {}); it was not ended".format(found[0], found[2], sid, created))
    return _tmux(["kill-session", "-t", sid], env, check=False).returncode == 0


def panes_in(sid, env=None):
    """Ids of every pane in the session `sid`."""
    return _tmux(["list-panes", "-s", "-t", sid, "-F", "#{pane_id}"], env).stdout.split()


def live_panes_in(sid, env=None):
    """{pane id: pid} for every pane of session `sid` whose process is still running. A dead pane (kept by remain-on-exit) is left out:
    its pid may already belong to another process."""
    out = {}
    for line in _tmux(["list-panes", "-s", "-t", sid, "-F", "#{pane_id} #{pane_dead} #{pane_pid}"], env).stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "0" and parts[2].isdigit():
            out[parts[0]] = int(parts[2])
    return out


def process_start(pid):
    """The start time `ps` gives process `pid` (whitespace collapsed, the form lib/worker_process.py records), or None when no such
    process runs. A pid that is reused has another start time."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return None
    run = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], text=True, capture_output=True, timeout=TMUX_TIMEOUT)
    start = " ".join(run.stdout.split())
    return start if run.returncode == 0 and start else None


# ------------------------------------------------------------------------------------------- the tp-* commands a trial starts
#
# The one gate (`start_tp`) every tp-* command of the trial goes through, and its table. What each command can do, through
# everything it runs (bin/tp-agent, bin/tp-review, bin/tp-spawn, bin/tp-kill, bin/tp-view, lib/worker_process.py):
#
#   reads   tp-agent inspect, list         read task dirs (tp-agent's record / all_records); no signal, kill or removal
#   ends    tp-agent end-author <task>     worker_process.end_or_refuse: signals the process of the pane meta.json names
#           tp-agent stop <task>           a queue entry queue/<task>.json is unlinked (through a link at queue/ too); otherwise
#                                          tp-kill: process.json and the meta.json pane are signalled and killed, and when the
#                                          session's @tp_viewing is the task, tp-view swaps the pane with @tp_viewer_pane first
#           tp-agent review [--force] <t>  tp-review: tp-agent end-author <t> (as above), then tp-spawn of a reviewer
#           tp-spawn ...                   a start that fails kills the pane it made and removes the task dir it made
#   runner  tp-trial run --run-dir DIR     the run itself (`launch`); everything it starts comes back through this gate
#
# Every file named there is the worker's to write (it runs as the same user), and every command resolves its session by the name
# TP_SESSION. So an `ends` command is started only after the caller's `check` (the run's `Owner.check_stop`) passes, right
# before it starts; a caller that gives none is refused. Anything else is refused and never started: another tool, another
# subcommand (collect, reap, retry, message, dispatch, tick, index, ...), a flag (review --force-alive, spawn --view) or an
# argument the table does not name.

AGENT, SPAWN, TRIAL = (str(ROOT / "bin" / n) for n in ("tp-agent", "tp-spawn", "tp-trial"))
TASK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # tp-agent's task id
TP = {"tp-agent inspect": "reads", "tp-agent list": "reads", "tp-agent end-author": "ends", "tp-agent stop": "ends",
      "tp-agent review": "ends", "tp-spawn": "ends", "tp-trial run": "runner"}
SPAWN_FLAGS = frozenset(("--task-id", "--worker", "--model", "--thinking", "--workdir", "--preamble-from", "--worker-env", "--role",
                         "--tests", "--deliverable", "--other-lane-paths", "--context", "--note", "--file"))  # each takes a value


def tp_command(argv):
    """(name, task) of a tp-* command line the trial may start: `name` is its key in TP, `task` the task id it acts on (None for
    list and run). Raises SessionError for anything the table does not name."""
    args = [str(a) for a in argv]
    if len(args) >= 2 and args[0] == sys.executable and args[1] == TRIAL:
        args = args[1:]
    tool, rest = (args[0], args[1:]) if args else ("", [])
    if tool == AGENT:
        if rest == ["list"]:
            return "tp-agent list", None
        if len(rest) == 2 and rest[0] in ("inspect", "end-author", "stop") and TASK.fullmatch(rest[1]):
            return "tp-agent " + rest[0], rest[1]
        if len(rest) in (2, 3) and rest[0] == "review" and rest[1:-1] in ([], ["--force"]) and TASK.fullmatch(rest[-1]):
            return "tp-agent review", rest[-1]
    elif tool == SPAWN:
        pairs = list(zip(rest[0::2], rest[1::2]))
        ids = [value for flag, value in pairs if flag == "--task-id"]
        if len(rest) % 2 == 0 and all(flag in SPAWN_FLAGS for flag, _ in pairs) and len(ids) == 1 and TASK.fullmatch(ids[0]):
            return "tp-spawn", ids[0]
    elif tool == TRIAL:
        if len(rest) == 3 and rest[:2] == ["run", "--run-dir"]:
            return "tp-trial run", None
    raise SessionError("refused: {!r} is not a command the trial may start (lib/trial_session.py TP); nothing was started".format(" ".join(args)[:300]))


def start_tp(argv, env, cwd=None, timeout=None, check=None, detach=None):
    """Start the tp-* command `argv` through the table (see TP). A `reads` command runs; an `ends` command runs only after
    `check(name, task)` returned (it raises to refuse), and is refused when no check is given; the `runner` is started detached
    (its output to the open file `detach`) and its Popen returned. Returns the CompletedProcess otherwise."""
    name, task = tp_command(argv)
    kind = TP[name]
    if (kind == "runner") != (detach is not None):
        raise SessionError("refused: {} is {}started detached; nothing was started".format(name, "" if kind == "runner" else "not "))
    if kind == "ends":
        if check is None:
            raise SessionError("refused: {} can end a process or remove a file, and no ownership check was given; nothing was started".format(name))
        check(name, task)
    args = [str(a) for a in argv]
    if detach is not None:
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=detach, stderr=subprocess.STDOUT, start_new_session=True, cwd=cwd, env=env)
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env, cwd=cwd)


def local_active(state, env=None):
    """Ids of the running local pi tasks in the tmux-pi state dir `state`, by `tp-agent list` (the classifier Main's own capacity
    check reads). A state dir that cannot be listed raises: an unknown count is never read as zero."""
    state = Path(state)
    if not (state / "tasks").is_dir():
        return []
    run = start_tp([AGENT, "list"], env_for(state, "", env), timeout=120)
    if run.returncode:
        raise SessionError("cannot count the local workers in {}: {}".format(state, run.stderr.strip() or "tp-agent list exited {}".format(run.returncode)))
    try:
        rows = json.loads(run.stdout)
    except ValueError as error:
        raise SessionError("cannot read the task list of {}: {}".format(state, error))
    if not isinstance(rows, list):
        raise SessionError("the task list of {} is not a list".format(state))
    return [str(r.get("id")) for r in rows
            if isinstance(r, dict) and r.get("status") in ACTIVE and r.get("harness") == "pi" and str(r.get("model") or "").startswith(LOCAL)]


def read_plain(path, dir_fd=None):
    """The bytes of the plain file at `path`, read without waiting and never through a link. The worker can write every file of the
    run (run.json, its task files, the owner record, its worktree's .git): one made a FIFO would hold a read until a writer came,
    and with it the runner (its attempt clock, its session kill) or a `tp-trial` command. Raises OSError for anything but a plain
    file (FileNotFoundError when there is none), so a caller's `except OSError` covers it."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a plain file", str(path))
        with os.fdopen(os.dup(fd), "rb") as handle:
            return handle.read()
    finally:
        os.close(fd)


def _run_info(directory):
    try:
        value = json.loads(read_plain(directory / "run.json").decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # EPERM: the process exists and is somebody else's
        return True
    return True


def trial_runs(project):
    """[(run name, directory)] of every run under the project, oldest name first."""
    root = trial_root(project)
    try:
        return sorted((p.name, p) for p in root.iterdir() if p.is_dir() and RUN.fullmatch(p.name))
    except OSError:
        return []


def trial_local(project, exclude_run=None, ahead_of=None, env=None):
    """What the trial's own local runs hold now, as a list of names. A run counts by its running local pi tasks, and, when it has
    none yet, once by itself while its run.json says it is not done: a run that is queued or starting holds a slot before tp-spawn
    has made its task (without this, two quick starts both pass the guard). A run whose runner has died does not hold one (`tp-trial
    stop` also ends it); a run not yet launched has no pid and does.

    `exclude_run` leaves one run out (the caller itself). `ahead_of` = (created, run name) is the recheck at spawn time: a run that
    is only queued counts only when it was created before the caller (or ties and sorts first), so of two runs that were prepared
    together exactly one goes on; a run that already has a running task always counts."""
    out = []
    for name, directory in trial_runs(project):
        if name == exclude_run:
            continue
        info = _run_info(directory)
        tasks = local_active(directory / "state", env) if (directory / "state" / "tasks").is_dir() else []
        if tasks:
            out.extend(tasks)
            continue
        if info.get("phase") in (None, "done") or not str(info.get("model") or "").startswith(LOCAL):
            continue
        if info.get("pid") is not None and not _alive(info.get("pid")):
            continue
        if ahead_of is not None and (str(info.get("created") or ""), name) >= tuple(ahead_of):
            continue
        out.append("run " + name)
    return out


def guard(main_state, project, trial, main_max, model, env=None, exclude_run=None, ahead_of=None):
    """(ok, reason): may a trial run of `model` start now? A model that does not run on this machine is not limited by the local
    slots. Otherwise Main must hold fewer than `main_max` local workers (a trial yields to Main, §10.7) and the trial's own local
    runs (`trial_local`) must be fewer than `trial["max_local_workers"]`."""
    if not model.startswith(LOCAL):
        return True, ""
    main = local_active(main_state, env)
    if len(main) >= main_max:
        return False, "Main holds {} of its {} local worker slots ({}); a trial run waits for Main".format(len(main), main_max, ", ".join(main))
    own = trial_local(project, exclude_run, ahead_of, env)
    if len(own) >= trial["max_local_workers"]:
        return False, "the trial already runs {} local worker(s) ({}); its limit is {}".format(len(own), ", ".join(own), trial["max_local_workers"])
    return True, ""


if __name__ == "__main__":
    sys.exit("lib/trial_session.py is a module; use bin/tp-trial")
