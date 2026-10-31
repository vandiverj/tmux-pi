"""The trial's own tmux session and its capacity guard (design §3, §8, §10).

A shadow or replay run lives in a detached tmux session `tp-trial-<run>` whose `@tp_state_dir` is
<project>/.tmux-pi/trial/<run>/state. lib/tp.sh `tp_session` refuses a state dir that no session owns, so a separate session is
the only way to a separate state dir: Main's task index, `max_active_workers`, collect notices and lane gate never see a trial
task, and a trial never writes into Main's state dir.

`env_for` is the one place that builds the environment every trial subprocess (tp-spawn, tp-agent, git) gets: TP_STATE_DIR and
TP_SESSION name the trial's state and session, TMUX and TMUX_PANE are removed (inside tmux, `tp_session` would otherwise pick
the caller's own session), and nothing that points at Main's state survives.

The guard (`guard`) is §8/§10.7: a trial local run starts only while Main holds fewer than its `max_local_workers` local pi
tasks, and while the trial's own local runs (running tasks, and runs that are queued or starting) are under
`trial.max_local_workers`. Main's count is read with `tp-agent list`, the
same classifier Main's own capacity check uses; nothing in Main's state dir is written.
"""

import json
import os
import re
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


def session_state(name, env=None):
    run = _tmux(["show-options", "-qv", "-t", "=" + name + ":", "@tp_state_dir"], env, check=False)
    return run.stdout.strip() if run.returncode == 0 else ""


def ensure(project, run, env=None):
    """Create or attach `tp-trial-<run>` with its own `@tp_state_dir` (<project>/.tmux-pi/trial/<run>/state). An existing session
    is reused only when it already names this state dir; one that names another is refused, never taken over. Returns the
    session name and the state dir."""
    name = session_name(run)
    base = run_dir(project, run)
    state = state_dir(project, run)
    for directory in (base, state, state / "tasks"):
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink():
            raise SessionError("{} is a symbolic link; the trial's own directories are never links".format(directory))
    env = env_for(state, name, env)
    if not has_session(name, env):
        created = _tmux(["new-session", "-d", "-s", name, "-n", "trial", "-c", str(base), "tail -f /dev/null"], env, check=False)
        if created.returncode and not has_session(name, env):  # lost a race to another start of the same run, or a real failure
            raise SessionError("cannot create tmux session {}: {}".format(name, created.stderr.strip() or "exit {}".format(created.returncode)))
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


def end(run, env=None):
    """Kill the run's session. A session that is already gone is fine. Returns whether one was ended."""
    name = session_name(run)
    if not has_session(name, env):
        return False
    return _tmux(["kill-session", "-t", "=" + name], env, check=False).returncode == 0


def local_active(state, env=None):
    """Ids of the running local pi tasks in the tmux-pi state dir `state`, by `tp-agent list` (the classifier Main's own capacity
    check reads). A state dir that cannot be listed raises: an unknown count is never read as zero."""
    state = Path(state)
    if not (state / "tasks").is_dir():
        return []
    run = subprocess.run([str(ROOT / "bin" / "tp-agent"), "list"], text=True, capture_output=True, timeout=120,
                         env=env_for(state, "", env))
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


def _run_info(directory):
    try:
        value = json.loads((directory / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
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
