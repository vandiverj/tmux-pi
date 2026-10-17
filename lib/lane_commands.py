"""Lanes (T10, slice L2): create a lane and list lanes, on top of lib/lanes.py. bin/tp-agent calls these (`lane create`,
`lanes`); nothing here dispatches a worker.

`create_lane` changes exactly three things: a new git worktree, a new branch and the lane record. It also creates the
lane lock file and the `tmux-pi/lanes` directory in the common git directory (empty bookkeeping that every later lane
operation needs). It never touches the main checkout's files, HEAD, index or `info/exclude`, never reuses a path or a
branch that exists, and on any failure undoes what it made (see `_roll_back`; what cannot be undone is named). Rollback
never forces: a worktree a hook dirtied stays, with the command that discards it. The default base is the integration branch.

`list_lanes` is read-only: it takes no lock, runs git with `--no-optional-locks`, and writes nothing.

What later slices must not assume:
- `open_step`, `occupant`, `queued` and `waiting_on` come from a session's tasks and the lane's lease, which this module does
  not read: `list_lanes(..., steps=)` takes the function that gives them (lib/lane_steps.py row_fields; `tp-agent lanes`
  passes it). Without it they are null, and an open lane may in fact have a writer.
- `overlaps` compares declared `touches` with each other and with the other lane's measured paths
  (`git diff --name-only base..tip` plus uncommitted paths); it is a warning, never a refusal.
- Creation holds the lane lock while it creates, so two creations in one repository queue; it does not hold git's own
  locks, so a hand-run `git worktree add` or `git branch` for the same name in between is caught by git, not by us."""
import os
import subprocess
from datetime import datetime, timezone

import lanes

DU_TIMEOUT = 5
OPEN_STATES = ("open", "integrating")


def _main_repo(repo_dir):
    """(common git dir, main working tree) of the repository `repo_dir` belongs to."""
    if not isinstance(repo_dir, (str, os.PathLike)) or not os.path.isdir(repo_dir):
        raise ValueError(f"{repo_dir} is not a directory")
    common = lanes.git_common_dir(repo_dir)
    return common, lanes.main_worktree(common)


def _resolve_base(repo, base, integration_branch):
    """(ref text, full commit id). Without `base`, the configured integration branch (design section 5); when that branch
    does not exist yet nothing is guessed: the refusal names the two ways out."""
    if base is None:
        code, out = lanes._git(["rev-parse", "--verify", "-q", f"refs/heads/{integration_branch}^{{commit}}"], repo, ok=(0, 1))
        if code or not lanes.HEX.fullmatch(out.strip()):
            raise ValueError(f"the integration branch {integration_branch} does not exist in {repo}, so there is no default base. "
                             f"Pass --base REF, or create the integration branch (git -C {repo} branch {integration_branch} REF)")
        return integration_branch, out.strip()
    if not isinstance(base, str) or not base or base.startswith("-") or any(ord(c) < 32 for c in base):
        raise ValueError(f"base {base!r} is not a usable ref")
    code, out = lanes._git(["rev-parse", "--verify", "-q", "--end-of-options", f"{base}^{{commit}}"], repo, ok=(0, 1))
    commit = out.strip()
    if code or not lanes.HEX.fullmatch(commit):
        raise ValueError(f"base {base!r} is not a commit in {repo}")
    return base, commit


def _status_entries(status):
    """Entries of `git status --porcelain -z` as (status code, [paths]): a rename or copy carries its old path in the next
    field, which belongs to the same entry."""
    fields, entries, index = status.split("\0"), [], 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) <= 3:
            continue
        paths = [entry[3:]]
        if entry[0] in "RC" and index < len(fields) and fields[index]:
            paths.append(fields[index])
            index += 1
        entries.append((entry[:2], paths))
    return entries


def _measured(record):
    """Paths the lane changed: committed beyond its base plus uncommitted (including untracked). Empty when git cannot say."""
    try:
        _, diff = lanes._git(["diff", "--name-only", "-z", f"{record['base']['commit']}..refs/heads/{record['branch']}"],
                             record["git_common_dir"])
        _, status = lanes._git(["--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"], record["worktree"])
    except ValueError:
        return []
    paths = {p for p in diff.split("\0") if p}
    for _, entry_paths in _status_entries(status):
        paths.update(entry_paths)
    return sorted(paths)


def _overlaps(declared, record, measured_cache):
    """Overlap entries between a lane's declared paths and another lane's record; basis names the kinds compared."""
    if record["name"] not in measured_cache:
        measured_cache[record["name"]] = _measured(record)
    measured = measured_cache[record["name"]]
    found = {}
    for basis, mine, theirs in (("declared/declared", declared, record["touches"]), ("declared/measured", declared, measured)):
        for path in lanes.overlapping_paths(mine, theirs):
            found.setdefault(path, []).append(basis)
    return found


def _warnings(name, domain, touches, others):
    """One lane-overlap warning per other open lane that overlaps `touches`. `others` are validated records."""
    cache, out = {}, []
    for other in others:
        if other["name"] == name or other["state"] not in OPEN_STATES:
            continue
        found = _overlaps(touches, other, cache)
        if found:
            out.append({"kind": "lane-overlap", "lanes": sorted([name, other["name"]]), "paths": sorted(found),
                        "basis": sorted({b for bases in found.values() for b in bases}), "claims": {name: domain, other["name"]: other["domain"]}})
    return out


def _all_records(common):
    return [lanes.read_lane(common, n) for n in lanes.lane_names(common)]


def _roll_back(common, repo, name, slot, base_commit, made):
    """Undo what a failed creation made, newest first: record, worktree, branch. Returns the list of things that could not
    be undone, each with the command that does it. `made` says which of the three may exist."""
    left = []
    if made["record"]:
        try:
            lanes.discard_lane_record(common, name)
        except ValueError as error:
            left.append(f"the record for lane {name} in {lanes.lanes_dir(common) / name} ({error}); delete that directory")
    if made["worktree"]:
        try:
            if slot["worktree"] in lanes.registered_worktrees(common):
                lanes._git(["worktree", "remove", slot["worktree"]], repo)  # never --force: a hook's edits are not discarded
            elif os.path.lexists(slot["worktree"]):
                raise ValueError("it exists but git does not list it, so it is not deleted")
        except ValueError as error:
            left.append(f"the worktree {slot['worktree']} ({error}); to discard it and anything in it run: "
                        f"git -C {repo} worktree remove --force {slot['worktree']}")
            if made["branch"]:  # a branch checked out in a worktree that stays cannot go either
                left.append(f"the branch {slot['branch']} (its worktree is left); after removing the worktree run: "
                            f"git -C {repo} branch -d {slot['branch']}")
            made = dict(made, branch=False)
    if made["branch"]:
        try:
            code, tip = lanes._git(["rev-parse", "--verify", "-q", f"refs/heads/{slot['branch']}"], common, ok=(0, 1))
            if code == 0 and tip.strip() == base_commit:
                try:
                    lanes._git(["branch", "-d", slot["branch"]], repo)
                except ValueError:
                    # -d also wants the branch merged into HEAD; ours holds no commit of its own (tip == base), so delete
                    # it only if it still points at the base, which loses nothing.
                    lanes._git(["update-ref", "-d", f"refs/heads/{slot['branch']}", base_commit], repo)
            elif code == 0:
                raise ValueError(f"it moved to {tip.strip()} since it was created, so it is not deleted")
        except ValueError as error:
            left.append(f"the branch {slot['branch']} ({error}); run: git -C {repo} branch -d {slot['branch']}")
    return left


def check_lane_name(name):
    if not lanes.valid_lane_name(name):
        raise ValueError(f"invalid lane name {name!r}: use 1-24 characters of a-z, 0-9 and -, starting with a letter or digit"
                         if name != "archive" else "the lane name 'archive' is reserved")


def warning_text(warning):
    """The stderr line for one warning from `warnings` (lane-overlap or unrecorded-worktree)."""
    if warning["kind"] == "lane-overlap":
        return (f"tp-agent: warning: lanes {' and '.join(warning['lanes'])} overlap on {', '.join(warning['paths'])} "
                f"({', '.join(warning['basis'])}). They were claimed independent ("
                + "; ".join(f"{k}: {v!r}" for k, v in warning["claims"].items()) + ").")
    return f"tp-agent: warning: {warning['path']} is {warning['note']}"


def validate_declaration(repo_config, name, domain, touches):
    """The checked (touches, domain) of a new lane: the name, the touches (none may overlap an owned path, check 14) and the
    domain text. Raises before anything is read from git or created."""
    check_lane_name(name)
    touches = lanes.path_prefixes(list(touches or []), "touches", minimum=1)
    if not isinstance(domain, str) or not domain.strip() or len(domain) > lanes.MAX_DOMAIN or any(ord(c) < 32 for c in domain):
        raise ValueError(f"--domain must be 1-{lanes.MAX_DOMAIN} characters of text on one line")
    for path in touches:
        for owned in repo_config.get("owned", []):
            if lanes.paths_overlap(path, owned):
                raise ValueError(f"--touches {path} is owned by the orchestrator (repo lanes.json \"owned\": {owned}); "
                                 "it cannot be a lane's area")
    return touches


def create_lane_locked(common, repo, name, domain, touches, base_ref, base_commit, machine, repo_config, branch=None):
    """Create the lane with the lane lock ALREADY held by this thread (create_lane and dispatch --lane take it): the slot
    and the active-lane limit are checked, then the worktree and branch are made with one `git worktree add -b`, the record
    is written and checked with lane_reality. Returns (record, warnings). A failure rolls back and raises with the original
    reason; if something cannot be undone the error says what, and the command that does it."""
    slot = lanes.precheck_lane_slot(common, repo_config, name, branch=branch)
    lanes.check_lane_capacity(common, machine["max_active_lanes"])
    others = _all_records(common)
    warnings = _warnings(name, domain, touches, others)
    made = {"record": False, "worktree": False, "branch": False}
    try:
        try:
            lanes._git(["worktree", "add", "--quiet", "-b", slot["branch"], slot["worktree"], base_commit], repo)
        finally:
            # The slot was proven free above, so whatever exists now was made by this command (even if git failed halfway).
            made["branch"] = lanes._branch_in_use(common, slot["branch"]) == slot["branch"]
            made["worktree"] = slot["worktree"] in lanes.registered_worktrees(common) or os.path.lexists(slot["worktree"])
        made["record"] = True
        record = lanes.write_lane(common, {
            "schema": lanes.SCHEMA, "name": name, "kind": "work", "state": "open", "repo": repo, "git_common_dir": common,
            "branch": slot["branch"], "worktree": slot["worktree"], "base": {"ref": base_ref, "commit": base_commit},
            "created": lanes.utc_now(), "domain": domain, "touches": touches, "closed": [], "integrated": None,
            "defaulted": lanes.defaulted_values(machine, repo_config)})
        lanes.lane_reality(record)
    except BaseException as error:
        left = _roll_back(common, repo, name, slot, base_commit, made)
        if left:
            raise ValueError(f"lane {name} was not created: {error}. These could not be undone and are left: " + "; ".join(left))
        if isinstance(error, ValueError):
            raise ValueError(f"lane {name} was not created: {error}. Nothing was left behind.")
        raise
    return record, warnings


def undo_created_lane(record):
    """Undo a lane this same call just created (dispatch --lane, when the task could not be started or queued afterwards), with
    the lane lock held. Returns nothing; raises naming what could not be undone, with the commands that do it."""
    slot = {"branch": record["branch"], "worktree": record["worktree"]}
    left = _roll_back(record["git_common_dir"], record["repo"], record["name"], slot, record["base"]["commit"],
                      {"record": True, "worktree": True, "branch": True})
    if left:
        raise ValueError("these could not be undone and are left: " + "; ".join(left))


def create_lane(repo_dir, name, domain, touches, base=None, machine=None, repo_config=None, branch=None):
    """Create work lane `name`: a branch from `base`, a worktree for it and the record. Returns {"lane": record,
    "warnings": [...]}. Order: validate every input; load configuration (absent values default, recorded in "defaulted");
    resolve the base; then under the lane lock create_lane_locked. `machine` / `repo_config` are the loaded configurations
    (loaded here when None). `branch` names the lane's git branch explicitly instead of branch_prefix + name; the record stores it."""
    common, repo = _main_repo(repo_dir)
    machine = machine if machine is not None else lanes.load_machine_config()
    repo_config = repo_config if repo_config is not None else lanes.load_repo_config(repo)
    touches = validate_declaration(repo_config, name, domain, touches)
    base_ref, base_commit = _resolve_base(repo, base, repo_config["integration_branch"])
    with lanes.lane_lock(common):
        record, warnings = create_lane_locked(common, repo, name, domain, touches, base_ref, base_commit, machine, repo_config, branch)
    return {"lane": record, "warnings": warnings}


def _disk_kb(worktree):
    try:
        run = subprocess.run(["du", "-sk", worktree], text=True, capture_output=True, timeout=DU_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, f"du took longer than {DU_TIMEOUT}s"
    except OSError as error:
        return None, f"du could not run ({error.__class__.__name__})"
    if run.returncode != 0:  # a du that failed part-way still prints a number; it is not a measurement
        return None, run.stderr.strip().splitlines()[-1] if run.stderr.strip() else f"du exited {run.returncode}"
    first = run.stdout.split("\t", 1)[0].strip()
    if not first.isdigit():
        return None, "du printed no size"
    return int(first), None


def age_text(created, now=None):
    """How long ago `created` (a record's UTC time) was: the largest whole unit of d, h, m, s ("3d", "5h", "12m", "40s")."""
    seconds = int(((now or datetime.now(timezone.utc)) - datetime.fromisoformat(created)).total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{max(seconds, 0)}s"


def _row(record, others, steps=None):
    """The `lanes` row of one validated, reality-checked record. `steps(record)` gives the fields that come from a session's
    tasks (open_step, occupant, queued, waiting_on: lane_steps.row_fields); without it they are null, because tasks are not known."""
    tip = lanes.lane_reality(record)["tip"]
    _, ahead = lanes._git(["rev-list", "--count", f"{record['base']['commit']}..{tip}"], record["git_common_dir"])
    _, status = lanes._git(["--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"], record["worktree"])
    disk, disk_error = _disk_kb(record["worktree"])
    overlaps = _warnings(record["name"], record["domain"], record["touches"], others) if record["state"] in OPEN_STATES else []
    row = {"name": record["name"], "kind": record["kind"], "state": record["state"], "repo": record["repo"],
           "branch": record["branch"], "worktree": record["worktree"], "base": record["base"], "domain": record["domain"],
           "touches": record["touches"], "created": record["created"], "age": age_text(record["created"]), "open_step": None, "steps_closed": len(record["closed"]), "occupant": None,
           "queued": None, "waiting_on": None,
           "ahead": int(ahead.strip()), "dirty": len(_status_entries(status)),
           "last_closed": record["closed"][-1]["step"] if record["closed"] else None, "overlaps": overlaps, "disk_kb": disk}
    if disk_error:
        row["disk_error"] = disk_error
    if steps:
        row.update(steps(record))
    return row


def list_lanes(repo_dir, name=None, steps=None):
    """One row per recorded lane of the repository, then one per unrecorded linked worktree (only when the repository has a
    recorded lane; only its path and directory name are read). A record that cannot be read or does not match git gives a
    row with `error` and its reason, and the other rows are still listed. With `name`, only that lane (refuses when it has
    no directory). Read-only."""
    common, repo = _main_repo(repo_dir)
    names = lanes.lane_names(common)
    if name is not None and name not in names:
        raise ValueError(f"there is no lane {name} in {lanes.lanes_dir(common)}")
    records, rows, errors = {}, [], {}
    for lane in names:
        try:
            records[lane] = lanes.read_lane(common, lane)
        except ValueError as error:
            errors[lane] = str(error)
    for lane in names:
        if name is not None and lane != name:
            continue
        if lane in errors:
            rows.append({"name": lane, "state": "error", "error": errors[lane]})
            continue
        record = records[lane]
        try:
            rows.append(_row(record, [r for r in records.values() if r["name"] != lane], steps))
        except ValueError as error:
            rows.append({"name": lane, "kind": record["kind"], "state": record["state"], "worktree": record["worktree"],
                         "error": str(error)})
    if name is None and names:
        recorded = {r["worktree"] for r in records.values()}
        for path in lanes.registered_worktrees(common):
            if path != repo and path not in recorded:
                row = {"name": os.path.basename(path), "state": "unrecorded", "repo": repo, "worktree": path}
                if errors:  # an unreadable record may be this worktree's; say so rather than call it hand-made
                    row["note"] = f"lane record(s) {', '.join(sorted(errors))} could not be read; this worktree may belong to one"
                rows.append(row)
    return rows
