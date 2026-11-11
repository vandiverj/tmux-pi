"""Steps of a lane (T10, slice L4): which step is open, whether its work may be closed, `lane close`, and the next-step rule.

A step is the `lane_step` tag of a lane's tasks. Nothing about a step is stored except its closing: lane.json's `closed` list
({step, commit, tree, review, verdict, closed_at}, append-only, checked against git by lanes.lane_reality on every read; an
empty step has review null and verdict EMPTY).
Everything else is derived on each use from this session's `tasks/` and `archive/` (task_record.iter_task_dirs) and `queue/`:

  open steps   every step that any task or queued item of the lane carries and that is not in `closed`; never the step of the
               newest task, because the newest task can be a late review of a closed step. Check 16 keeps a second step from
               starting, so with the rule enforced there is one; a lane with more (from before the rule, or from a record that
               could not be read) has them all open, `open_step` is the oldest, and a task for any of them is never held back.
  writers      the step's tasks that carry no `review-of` and have no read-only role.
  reviews      the tasks whose `review-of` file names one of the step's writers. A review counts for a tree only when its
               reviewed-state.json (written by tp-review) names that tree and the lane's working tree.
  unreadable   a task directory whose meta.json cannot be read cannot say which lane it is in, so a lane names it in two other
               places: the task's dispatch.json (lane, lane_step; written once the worker has started) and the lane's lease (task
               and session of a holder). A task so named is a writer of its step (or of every open step, when only the lease
               names it) that cannot be shown to be finished: it holds the step open until it is removed. A task directory that
               no lane names stays what it was, an orphan with a warning.

`evaluate` is the one derivation of the review gate (design 4.2); `step_gate` is its (ok, why) form; `lane close`, check 16 and the
`lanes` column waiting_on read the facts. A step is approved when
  1. no writer of it is starting, working, waiting for an answer or paused (an unreadable one counts as working), and nothing
     of it is queued;
  2. the working tree is clean, so the tree on disk is HEAD's tree, and
  3. a review of exactly that tree exists, no review of that tree asks for changes or is still running, and one approves.
Or it is EMPTY: after 1, the working tree is clean and HEAD's tree is the tree of the last closed commit (the base for a first
step), so there is nothing to review. It is closed without a review and recorded with verdict EMPTY and review null. A review
of that unchanged tree, whatever it said, is not read: it judged nothing that was changed.
A review approves when its lifecycle is settled or completed and review_verdict.assess reads APPROVED or APPROVED_WITH_NOTES with
a non-empty Coverage section. A REQUEST_CHANGES rejects, unless every finding is labelled `Closes by:` and none is `code`
(a missing report item or an unrun command, which no change to the tree closes): that review is unusable, a second review of
the same tree may approve, and it is named in `why` like the other unusable ones. A REQUEST_CHANGES with a `code` label, or with
no labels at all (older reviewers, other templates), rejects as before. Reviews of another tree are stale and ignored. There is no skip. The one override is the owner's:
`lane close --override-review --reason TEXT`, typed back at a terminal (owner_confirm), lifts only the refusal of a review that asked
for changes to this tree. Every other part of 1 to 3 still holds (no running review, clean and committed), and the entry is recorded
with verdict OWNER_OVERRIDE, the newest such review and the owner's reason.

The stop rule (SR1) moves the owner's confirmation of a last round to the moment the owner chooses it. `lane authorize-stop NAME
STEP --reason TEXT` (owner, typed-back code) records, in the lane record, an authorization bound to the step's gate review (its
newest review that asked for changes) and the Family: names of that review's code findings. `lane close NAME STEP --stop-rule
--known-limits PATH` (Main, no code) then closes as the override does, only when: the step has an authorization; a review of the
clean HEAD tree asked for changes (the override's checks); the closing review's author is the retry whose meta.json gate_review is
the authorization's; every code finding of each review of this tree that asked for changes has a family the authorization lists;
and PATH is committed at the main repository's or the lane's HEAD and names each such review and its code findings (the reviewer's
own F<n> ids). The findings are counted from every line of a review that starts one (review_findings), never taken from a
quoting splitter; a review whose findings cannot all be counted and classified refuses (authorize and close alike), and the owner
uses --override-review. The entry is an OWNER_OVERRIDE with the authorization's reason and a stop_rule object; the authorization
leaves the record in the same write (any close of the step removes it).

Reading chosen on purpose (it is the least strict one that keeps the rule): a review that FINISHED without a usable verdict
(unreadable, unparseable, no Coverage section, or failed, killed or stopped) is not a vote. It never approves, and it is named
when nothing approves, but a second review of the same tree that approves is enough. Counting it as a rejection would leave a
lane no way out except editing the work, because a review of the same tree can only be added, never removed.

Every `why` names the command that clears it in the state it is printed in. Nothing here writes except close_step, which
appends one entry to `closed` under the lane lock and only after every refusal has been passed.

Limit (design delta): lane records are in the repository's git directory, shared by every session; tasks are in a session. A
session sees only its own tasks, so another session's steps are not seen here (the lease, which is shared, still keeps its
workers out of the lane)."""
import json
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_commands  # noqa: E402
import lane_lease  # noqa: E402
import lanes  # noqa: E402
import family_sweep  # noqa: E402
import review_rounds  # noqa: E402
import review_verdict  # noqa: E402
import worker_process  # noqa: E402
from contract import task_lifecycle  # noqa: E402
from task_record import iter_task_dirs  # noqa: E402
from worker_process import READ_ONLY_ROLES, effective_role, finishes_alone, state_words  # noqa: E402

AT_WORK = ("starting", "working", "needs_input", "paused")  # a writer in one of these is not finished; a review in one has no result yet
FINISHED_REVIEW = ("settled", "completed")
RETRYABLE = ("settled", "completed")  # an author that tp-review and tp-agent retry accept


def _json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _stamp(text):
    """Seconds since the epoch of a task's `created` (tp-spawn writes ...Z, the queue +00:00); 0 when unreadable."""
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class Session:
    """What one session's task directories and queue say about lanes. Read once; every question below is asked of this snapshot.
    A task whose meta.json cannot be read carries no lane tag that can be read, so it is in `unreadable` (not in `tasks`): it is
    a task of a lane only when its dispatch.json or the lane's lease says so (see `unreadable_of`)."""

    def __init__(self, state):
        self.state = Path(state)
        self.tasks, self.queued, self.unreadable = [], [], []
        readable = set()
        for path, where in iter_task_dirs(self.state, True):
            meta = _json(path / "meta.json")
            if meta is None:
                continue
            readable.add(path)
            ident = meta["id"] if isinstance(meta.get("id"), str) and lanes.TASK_ID.fullmatch(meta["id"]) else path.name
            try:
                review_of = (path / "review-of").read_text().strip() or None
            except OSError:
                review_of = None
            lane, step = meta.get("lane"), meta.get("lane_step")
            role = meta.get("role") if isinstance(meta.get("role"), str) else None
            self.tasks.append({"id": ident, "path": path, "where": where, "meta": meta, "review_of": review_of,
                               "lane": lane if isinstance(lane, str) else None, "step": step if isinstance(step, str) else None,
                               "role": effective_role(role, ident), "when": (_stamp(meta.get("created")), _mtime(path / "meta.json"))})
        for where in ("tasks", "archive"):
            base = self.state / where
            for path in sorted(base.iterdir()) if base.is_dir() else []:
                if path.is_dir() and not path.name.startswith(".") and path not in readable:
                    tag = _json(path / "dispatch.json") or {}
                    lane, step = tag.get("lane"), tag.get("lane_step")
                    self.unreadable.append({"id": path.name, "path": path, "where": where,
                                            "lane": lane if isinstance(lane, str) and isinstance(step, str) else None,
                                            "step": step if isinstance(lane, str) and isinstance(step, str) else None,
                                            "when": (_mtime(path / "dispatch.json") / 1e9, _mtime(path / "dispatch.json"))})
        queue = self.state / "queue"
        for file in sorted(queue.glob("*.json")) if queue.is_dir() else []:
            item = _json(file)
            if item and isinstance(item.get("lane"), str) and isinstance(item.get("lane_step"), str):
                self.queued.append({"id": file.stem, "lane": item["lane"], "step": item["lane_step"],
                                    "role": item.get("role") if isinstance(item.get("role"), str) else None,
                                    "when": (_stamp(item.get("created")), _mtime(file))})

    def of_lane(self, name):
        return [t for t in self.tasks if t["lane"] == name] + [q for q in self.queued if q["lane"] == name]

    def writers(self, name, step):
        return [t for t in self.tasks if t["lane"] == name and t["step"] == step and not t["review_of"] and t["role"] not in READ_ONLY_ROLES]

    def queued_for(self, name, step):
        return [q for q in self.queued if q["lane"] == name and q["step"] == step]

    def steps(self, name):
        """The steps of lane `name` that any task, queued item or unreadable task whose dispatch.json says so carries, ordered by
        the oldest item of each."""
        first = {}
        for item in self.of_lane(name) + [u for u in self.unreadable if u["lane"] == name]:
            if item.get("step") and (item["step"] not in first or item["when"] < first[item["step"]]):
                first[item["step"]] = item["when"]
        return sorted(first, key=first.get)

    def unreadable_of(self, record):
        """The unreadable task directories that lane `record` names: by their dispatch.json, else by a holder of the lane's lease
        in this session. Each is {id, path, where, lane, step} with step None when only the lease names it. The lease is read
        only when a directory is left that dispatch.json does not place (a damaged lease refuses, as it does everywhere)."""
        name = record["name"]
        found = [u for u in self.unreadable if u["lane"] == name]
        loose = [u for u in self.unreadable if u["lane"] is None]
        if loose:
            here = os.path.realpath(str(self.state))
            held = {h["task"] for h in lane_lease.read(record["git_common_dir"], name) if os.path.realpath(h["session"]) == here}
            found += [dict(u, lane=name) for u in loose if u["id"] in held]
        return found


def _mtime(path):
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def life_of(task):
    """A task's lifecycle word, derived without a pane look. An unreadable record counts as working: it cannot be
    shown to be finished."""
    try:
        return task_lifecycle(task["path"])[0]
    except Exception:
        return "working"


def open_steps(session, record):
    """Every step of the lane that is not closed, oldest first. A step is open because something of it exists (a task of any role,
    a queued item, an unreadable task its dispatch.json places in it) and the lane's `closed` list does not have it; how new
    how new the lane's newest task is says nothing (it may be a review of a closed step)."""
    closed = {entry["step"] for entry in record["closed"]}
    return [step for step in session.steps(record["name"]) if step not in closed]


def open_step(session, record):
    """The open step of the lane, or None: the oldest step that is not closed (the one `lane close` takes first)."""
    steps = open_steps(session, record)
    return steps[0] if steps else None


def _closed_entry(record, step):
    return next((entry for entry in record["closed"] if entry["step"] == step), None)


def _short(sha):
    return sha[:12]


# --- the git facts --------------------------------------------------------------------------------------------------

def _head(worktree):
    _, out = lanes._git(["rev-parse", "HEAD", "HEAD^{tree}"], worktree)
    commit, tree = out.split()
    return commit, tree


def _dirty(worktree):
    _, out = lanes._git(["--no-optional-locks", "status", "--porcelain", "-z", "--untracked-files=all"], worktree)
    return len(lane_commands._status_entries(out))


def _tree_now(worktree, dirty, head_tree):
    """The tree of the working tree as tp-review records it (a throwaway index: the repository's index and files are not
    touched; git does write the tree's objects, which nothing refers to)."""
    if not dirty:
        return head_tree
    try:
        return review_rounds.snapshot(worktree)["tree"]
    except review_rounds.Refused as error:
        raise ValueError(f"cannot read the tree of {worktree} ({error})")


# --- the review gate ------------------------------------------------------------------------------------------------

def _reviewed_state(task):
    state = _json(task["path"] / "reviewed-state.json")
    if state and isinstance(state.get("tree"), str) and isinstance(state.get("workdir"), str):
        return state
    return None


def _judge(task):
    """('approves' | 'rejects' | 'running' | 'unusable', detail) for a review task. Settled or completed ones are read through
    review_verdict.assess (the one reader); a REQUEST_CHANGES verdict rejects even when its Coverage section is missing, unless its
    findings are labelled `Closes by:` and none is `code` (review_verdict.request_changes_problem): that asks for no change to the
    tree, so it is unusable like any review that is not a vote."""
    life = life_of(task)
    if life in AT_WORK:
        return "running", life
    if life not in FINISHED_REVIEW:
        return "unusable", f"it ended {life}"
    try:
        text = review_verdict.read_review(task["path"] / "result.md")
        verdict, _, incomplete = review_verdict.assess(text)
    except review_verdict.ReviewMissing:
        return "unusable", "it has no result"
    except ValueError as error:
        return "unusable", str(error)
    if verdict == "REQUEST_CHANGES":
        problem = review_verdict.request_changes_problem(text)
        return ("unusable", problem) if problem else ("rejects", verdict)
    if incomplete:
        return "unusable", incomplete
    return "approves", verdict


def _stop(ident):
    return f"`tp-agent stop {ident}`"


def _answer(ident):
    return f"It will not finish by itself: answer it with `tp-agent message {ident} --text \"...\"`"


def _author(session, writers):
    """The newest writer of `writers` that tp-review and tp-agent retry can work on (active, settled or collected), or None."""
    ready = [w for w in writers if w["where"] == "tasks" and life_of(w) in RETRYABLE]
    return max(ready, key=lambda w: w["when"]) if ready else None


def unreadable_refusal(task, step=None):
    """The `why` for an unreadable task of the lane that the step cannot get past: its directory, and the command that clears it
    in the state it is printed in (a process still running from it is ended first; a directory nothing runs from is removed)."""
    path = task["path"]
    try:
        running = worker_process.task_processes(path)
    except (RuntimeError, OSError):
        running = []
    if running:
        try:
            table = worker_process.pane_table()
        except RuntimeError:
            table = {}
        fix = f"`{worker_process.end_command(running, table)}` (a process of it is running), then `rm -r {path}`"
    else:
        fix = f"`rm -r {path}`"
    placed = f"step {task['step']}" if task["step"] else "an open step of this lane"
    return (f"{task['id']} ({path}) belongs to {placed} but its meta.json cannot be read, so it cannot be shown to be finished or "
            f"to have changed nothing. Remove it with {fix} (copy it elsewhere first if you "
            f"need its files), or repair {path}/meta.json by hand")


def _new_task(name, step, writers):
    role = next((w["role"] for w in sorted(writers, key=lambda w: w["when"], reverse=True)), "coder")
    return f"`tp-agent dispatch --lane {name} --step {step} --role {role} --task \"...\"`"


def evaluate(session, record, step, override=False):
    """The facts of the review gate for `step` of `record`'s lane: {ok, why, wait, review, verdict, tree, dirty, head}.
    `wait` is who the step is waiting for (the `lanes` column waiting_on): worker | review | orchestrator-dispatch |
    orchestrator-commit | orchestrator-close. `why` is empty when ok. `override` is the owner's override of a review that asked
    for changes (close_step checks who asked); it lifts that refusal and nothing else. Reads only; raises ValueError when git cannot say."""
    name, worktree = record["name"], record["worktree"]
    writers, queued = session.writers(name, step), session.queued_for(name, step)
    facts = {"ok": False, "why": "", "wait": None, "review": None, "verdict": None, "tree": None, "dirty": None, "head": None}

    def refuse(wait, why):
        facts.update(wait=wait, why=why)
        return facts

    if queued:
        item = queued[0]
        return refuse("worker", f"{item['id']} is queued for step {step} and has not started; it starts when the lane and a worker slot are "
                                f"free. To take it out of the queue: {_stop(item['id'])}")
    for writer in writers:
        life = life_of(writer)
        if life in AT_WORK:
            how = "Wait for it to finish, or end it" if finishes_alone(life) else _answer(writer["id"]) + ", or end it"
            return refuse("worker", f"{writer['id']} is {state_words(life)}, so the step's work is not finished. {how} with {_stop(writer['id'])}")
    for task in session.unreadable_of(record):
        if task["step"] in (step, None):
            return refuse("worker", unreadable_refusal(task))
    head, head_tree = _head(worktree)
    dirty = _dirty(worktree)
    tree = _tree_now(worktree, dirty, head_tree)
    facts.update(tree=tree, dirty=dirty, head=head)
    _, previous_tree = lanes._git(["rev-parse", f"{_previous(record)}^{{tree}}"], record["git_common_dir"])
    if not dirty and head_tree == previous_tree.strip():  # nothing to review: the step left the tree as the last closed commit had it
        facts.update(ok=True, wait="orchestrator-close", verdict=lanes.EMPTY)
        return facts
    author = _author(session, writers)
    writer_ids = {w["id"] for w in writers}
    counting, stale = [], []
    for review in (t for t in session.tasks if t["review_of"] in writer_ids):
        state = _reviewed_state(review)
        if state is None:
            stale.append((review, "no reviewed-state.json"))  # never counts: tp-review writes it after the reviewer starts
        elif state["tree"] == tree and os.path.realpath(state["workdir"]) == os.path.realpath(worktree):
            counting.append(review)
        else:
            stale.append((review, f"it reviewed tree {state['tree'][:12]}"))
    judged = [(review, *_judge(review)) for review in counting]
    where = f"{dirty} uncommitted path(s) in {worktree}" if dirty else f"HEAD {_short(head)}"
    do_review = (f"Start one with `tp-agent review {author['id']} --force` (--force reviews even when the policy says none is needed)"
                 if author else f"No writer of the step can be reviewed (none is settled in this session's active tasks). Dispatch a task into "
                                f"the step with {_new_task(name, step, writers)}, and review that one")
    owner = "review" if author else "orchestrator-dispatch"  # with no writer to review, the next move is a task
    rejecting = [j for j in judged if j[1] == "rejects"]
    if rejecting and not override:
        review = rejecting[0][0]
        fix = (f"Change the work, for example `tp-agent retry {author['id']} --reason \"address the findings of {review['id']}\"`, and review the new tree"
               if author else f"Change the work with a task in the step ({_new_task(name, step, writers)}) and review the new tree")
        return refuse("orchestrator-dispatch", f"{review['id']} asked for changes to this tree ({where}) and a review of the same tree cannot undo that. {fix}")
    running = [j for j in judged if j[1] == "running"]
    if running:
        review, life = running[0][0], running[0][2]
        how = "Wait for it to finish, or end it" if finishes_alone(life) else _answer(review["id"]) + ", or end it"
        return refuse("review", f"{review['id']} is reviewing this tree and is {state_words(life)}. {how} with {_stop(review['id'])}")
    if override:
        if not rejecting:
            return refuse(owner, f"no review of this tree ({where}) asked for changes, so there is nothing to override; "
                                 f"close the step without --override-review or --stop-rule once a review of it approves")
        facts.update(review=max(rejecting, key=lambda j: j[0]["when"])[0]["id"], verdict=lanes.OWNER_OVERRIDE,
                     rejecting=[j[0]["id"] for j in rejecting])
        return _committed(facts, refuse, where, worktree, name, step)
    approving = [j for j in judged if j[1] == "approves"]
    if not approving:
        if judged:
            bad = "; ".join(f"{j[0]['id']}: {j[2]}" for j in judged)
            return refuse(owner, f"no review of this tree ({where}) is usable and complete ({bad}). {do_review}")
        if stale:
            old = "; ".join(f"{review['id']}: {why}" for review, why in stale[:3])
            return refuse(owner, f"no review is of this tree ({where}); the earlier ones judged other work ({old}). {do_review}")
        return refuse(owner, f"step {step} has no review yet. {do_review}")
    chosen = next((j for j in sorted(approving, key=lambda j: j[0]["when"], reverse=True) if j[2] == "APPROVED_WITH_NOTES"),
                  max(approving, key=lambda j: j[0]["when"]))
    facts.update(review=chosen[0]["id"], verdict=chosen[2])
    return _committed(facts, refuse, where, worktree, name, step)


def _committed(facts, refuse, where, worktree, name, step):
    if facts["dirty"]:
        return refuse("orchestrator-commit", f"the approved work is not committed: {where}. Commit it as reviewed with "
                                             f"`git -C {worktree} add -A && git -C {worktree} commit -m \"...\"`, then run `tp-agent lane close {name} {step}`")
    facts.update(ok=True, wait="orchestrator-close")
    return facts


def step_gate(session, record, step):
    """(ok, why): may step `step` of the lane be closed? `why` is empty when it may, else the reason and the command that clears it."""
    facts = evaluate(session, record, step)
    return facts["ok"], facts["why"]


# --- lane close -----------------------------------------------------------------------------------------------------

def _previous(record):
    return record["closed"][-1]["commit"] if record["closed"] else record["base"]["commit"]


def _named(common, name, step):
    lane_commands.check_lane_name(name)
    if not lanes.STEP_ID.fullmatch(step):
        raise ValueError(f"invalid step id {step!r}: use up to 40 characters of letters, digits, . _ -")
    if name not in lanes.lane_names(common):
        raise ValueError(f"there is no lane {name} in {lanes.lanes_dir(common)}; `tp-agent lanes` lists the lanes")


def _step_here(session, record, step, state, command):
    """Refuse a step this session has no task of, or one that is closed (`command` is what to run again from elsewhere)."""
    name = record["name"]
    seen = session.steps(name)
    entry = _closed_entry(record, step)
    if step not in seen and not entry:
        here = f"its steps in this session are {', '.join(seen)}" if seen else "no task of this session belongs to the lane"
        raise ValueError(f"lane {name} has no step {step} in this session ({state}): {here}. Check the id with `tp-agent lanes {name}`; if the "
                         f"step ran in another session, run this from that session's workspace (or TP_STATE_DIR=<its state directory> tp-agent {command})")
    if entry:
        raise ValueError(f"step {step} is already closed at {entry['commit']}; it is recorded, so there is nothing to do. The next step takes a "
                         f"new id: `tp-agent dispatch --lane {name} --step NEW-ID --role ROLE --task \"...\"`")


def close_step(state, common, name, step, override_reason=None, known_limits=None):
    """Record that `step` of lane `name` is finished and bound to the commit the reviewed work is committed as. Every refusal
    is raised before anything is written; the one write is the new entry in `closed`, under the lane lock. Returns the record.
    `override_reason` (the owner's, confirmed at a terminal by the caller) closes over a review that asked for changes.
    `known_limits` (a path; `--stop-rule`) closes over it under the owner's stop-rule authorization of the step (stop_rule_checks)."""
    if override_reason is not None and known_limits is not None:
        raise ValueError("--stop-rule and --override-review are exclusive: the stop rule closes under the owner's earlier authorization, "
                         "the override is the owner at a terminal now")
    if override_reason is not None and not override_reason.strip():
        raise ValueError("--override-review needs a --reason that says why the owner closes over the review")
    if known_limits is not None and not str(known_limits).strip():
        raise ValueError("--stop-rule needs --known-limits PATH: the committed file that names the closing review and its code findings")
    _named(common, name, step)
    with lanes.lane_lock(common):
        record = lanes.read_lane(common, name)
        lane_use_checks(record)
        session = Session(state)
        _step_here(session, record, step, state, f"lane close {name} {step}")
        current = open_step(session, record)  # a step with tasks that is not closed is open, so `current` is set whenever `step` is
        if current != step:
            raise ValueError(f"step {step} is not lane {name}'s open step ({current}, the oldest that is not closed); close that one first with "
                             f"`tp-agent lane close {name} {current}`")
        stop = None
        if known_limits is not None:
            stop = _authorization(record, step)
            if stop is None:  # (a)
                held = ", ".join(a["step"] for a in record.get("stop_authorizations", []))
                raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (a): it has no authorization"
                                 f"{f' (the lane holds one for step(s) {held} only)' if held else ''}. Only the owner gives one, at a terminal: "
                                 f"`tp-agent lane authorize-stop {name} {step} --reason \"...\"`")
        override = override_reason is not None or stop is not None
        facts = evaluate(session, record, step, override=override)
        if not facts["ok"]:
            raise ValueError(f"step {step} of lane {name} cannot be closed: {facts['why']}")
        if override and facts["verdict"] != lanes.OWNER_OVERRIDE:
            raise ValueError(f"step {step} of lane {name} needs no override ({facts['verdict']}); close it without --override-review or --stop-rule")
        stop_rule = stop_rule_checks(session, record, step, facts, stop, known_limits) if stop else None
        head, head_tree = _head(record["worktree"])  # the clean tree evaluate judged: its commit is what the step is recorded as
        entry = {"step": step, "commit": head, "tree": head_tree, "review": facts["review"], "verdict": facts["verdict"], "closed_at": lanes.utc_now()}
        if override_reason is not None:
            entry["override_reason"] = override_reason.strip()
        if stop_rule:
            entry["override_reason"] = stop["reason"]
            entry["stop_rule"] = stop_rule
        updated = dict(record, closed=record["closed"] + [entry])
        _without_authorization(updated, step)  # a closed step needs none: it leaves in the same write
        lanes.lane_reality(updated)  # the new entry is checked against git before it is written, never after
        return lanes.write_lane(common, updated)


# --- the stop rule (SR1) --------------------------------------------------------------------------------------------

def _authorization(record, step):
    return next((a for a in record.get("stop_authorizations", []) if a["step"] == step), None)


def _without_authorization(record, step):
    kept = [a for a in record.get("stop_authorizations", []) if a["step"] != step]
    if kept:
        record["stop_authorizations"] = kept
    else:
        record.pop("stop_authorizations", None)


# A line that starts a finding, as this project's reviewers write one: `F<n>` led by a Markdown heading (`### F1 — ...`), emphasis
# (`**F1 — ...**`, `**F1 [defect]**`), a list marker (`- **F1 — ...**`) or nothing, at most three spaces in (a `>` quote is not one).
# Behind a heading or emphasis any text may follow; a bare `F<n>` starts a finding only when a dash, bold, `[` or `(` follows it, so
# a back-reference such as `F1: qualify the claim` or `F2's evidence` is not counted as a second F1 or F2.
FINDING_LEAD = re.compile(r"^[ \t]{0,3}(?:(?:[-*+]|\d{1,3}[.)])[ \t]+)?(?P<heading>#{1,6}[ \t]+)?(?P<em>(?:\*\*|__|\*|_)[ \t]*)?\[?"
                          r"F(?P<n>\d{1,4})(?![A-Za-z0-9'’]|_[A-Za-z0-9])(?P<rest>.*)$")
BARE_LEAD_REST = re.compile(r"^\]?(?:[ \t]*$|[ \t]*[—–]|[ \t]+(?:-|\*\*|__|\[|\())")


def finding_lead(line):
    """The id `F<n>` when `line` starts a finding (FINDING_LEAD), else None."""
    match = FINDING_LEAD.match(line)
    if not match or not (match.group("heading") or match.group("em") or BARE_LEAD_REST.match(match.group("rest"))):
        return None
    return f"F{int(match.group('n'))}"


def review_findings(text):
    """[(id, closes, [families])] of every finding of a REQUEST_CHANGES reply, in order, or ValueError naming what cannot be
    accounted for. Completeness is proved, not taken from the quoting splitter (family_sweep.findings stops at a Coverage or
    Verification heading and yields nothing for a malformed reply): every line of the whole reply that starts a finding
    (finding_lead) is counted, wherever it is, and its section runs to the next one. Each section must hold exactly one `Closes by:`
    label and its own `Family:` lines (both read by review_verdict's label patterns from the lines family_sweep.visible_lines shows:
    outside fenced code and comments, not quoted, not in a `>` quote). The first `Closes by:` of a section is the finding's; its
    `Family:` names are the ones above it. After it, and before the first finding, only items that close by something other than
    code may follow (the `Closes by: check` lines of an "Owed before close" list): a `Closes by: code` or a `Family:` label there is
    a finding with no lead line and refuses. Refused too: a reply that is not a REQUEST_CHANGES verdict, a fence or comment never
    closed, no counted finding (a reason-only REQUEST_CHANGES), two findings with one id, and a finding with no `Closes by:` label."""
    if review_verdict.parse(text)[0] != "REQUEST_CHANGES":
        raise ValueError("it is not a REQUEST_CHANGES reply")
    review_verdict.normalise(text.strip())  # raises for a fence or comment never closed: the lines after it cannot be told
    raw, visible = family_sweep.visible_lines(text)
    leads = [(index, finding_lead(line)) for index, line in enumerate(raw) if index and finding_lead(line)]
    if not leads:
        raise ValueError("no line starts a finding (a heading or bold lead `F<n>`, or a line `F<n> — ...`), so its findings cannot be "
                         "counted (a reason-only REQUEST_CHANGES)")
    ids = [ident for _, ident in leads]
    twice = sorted({ident for ident in ids if ids.count(ident) > 1}, key=ids.index)
    if twice:
        raise ValueError(f"two findings have the id {', '.join(twice)} (lines {', '.join(str(i + 1) for i, ident in leads if ident in twice)})")

    def labels(start, end, pattern):
        found = []
        for index in range(start, end):
            shown = visible[index]
            if shown is None or shown.lstrip().startswith(">"):
                continue
            match = pattern.search(shown.rstrip())
            if match and not review_verdict._quoted(shown[:match.start()]):
                found.append(((match.group(1) or match.group(2)).lower(), index + 1))
        return found
    def unled(start, end, where):
        stray = sorted([(line, f"Closes by: {value}") for value, line in labels(start, end, review_verdict.CLOSES_BY) if value == "code"]
                       + [(line, f"Family: {value}") for value, line in labels(start, end, review_verdict.FAMILY)])
        if stray:
            raise ValueError(f"line {stray[0][0]} ({stray[0][1]}) is {where}: a finding with no `F<n>` lead line, which cannot be counted")
    unled(1, leads[0][0], f"before the first finding ({leads[0][1]}, line {leads[0][0] + 1})")
    out = []
    for number, (start, ident) in enumerate(leads):
        end = leads[number + 1][0] if number + 1 < len(leads) else len(raw)
        closes = labels(start, end, review_verdict.CLOSES_BY)
        if not closes:
            raise ValueError(f"finding {ident} (line {start + 1}) has no Closes by: line, so it cannot be classified")
        own = closes[0][1]  # 1-based line of the finding's own label
        families = list(dict.fromkeys(name for name, _ in labels(start, own, review_verdict.FAMILY)))
        unled(own, end, f"after finding {ident}'s own Closes by: (line {own})")
        out.append((ident, closes[0][0], families))
    return out


def code_findings(task):
    """[(finding id, [Family: names])] of the findings of review `task` that close by code, in order, from review_findings (the
    complete count; the id is the reviewer's own `F<n>`). Raises ValueError when the reply cannot be read or not every finding of it
    can be accounted for."""
    text = review_verdict.read_review(task["path"] / "result.md")
    return [(ident, families) for ident, closes, families in review_findings(text) if closes == "code"]


INCOMPLETE = ("The stop rule acts only on a review whose every finding it can count and classify; the owner closes this one with "
              "`tp-agent lane close {name} {step} --override-review --reason \"...\"` at their terminal")


def _task(session, ident):
    return next((t for t in session.tasks if t["id"] == ident), None)


def stop_plan(session, record, step, state="this session"):
    """What `lane authorize-stop` binds to: {step, gate_review, families, replaces}. The gate review is the newest review of the
    step (of any tree) that asks for changes (_judge: rejects); families are the Family: names of its code findings. Reads only;
    refuses a closed step, a step this session has no task of, and a step no review of which asked for changes."""
    name = record["name"]
    _step_here(session, record, step, state, f"lane authorize-stop {name} {step} --reason \"...\"")
    writer_ids = {w["id"] for w in session.writers(name, step)}
    rejecting = [t for t in session.tasks if t["review_of"] in writer_ids and _judge(t)[0] == "rejects"]
    if not rejecting:
        raise ValueError(f"step {step} of lane {name} has no review that asked for changes, so there is no last round to authorize; "
                         f"a step whose review approves closes with `tp-agent lane close {name} {step}`")
    gate = max(rejecting, key=lambda t: t["when"])
    try:
        found = code_findings(gate)
    except ValueError as error:
        raise ValueError(f"step {step} of lane {name} cannot be authorized under the stop rule: the findings of gate review {gate['id']} "
                         f"cannot all be accounted for ({error}). {INCOMPLETE.format(name=name, step=step)}")
    families = list(dict.fromkeys(family for _, names in found for family in names))
    held = _authorization(record, step)
    return {"step": step, "gate_review": gate["id"], "families": families, "replaces": held["id"] if held else None}


def stop_confirm_text(name, plan, reason):
    """What the owner reads above the code: the lane, the step, the gate review, the families and the reason."""
    families = ", ".join(plan["families"]) or "none (no close can pass: a code finding needs an authorized family)"
    again = f"\nIt replaces the step's authorization {plan['replaces']}." if plan["replaces"] else ""
    return (f"Authorize Main to close step {plan['step']} of lane {name} under the stop rule, for one last round.\n"
            f"Gate review: {plan['gate_review']} (the retry it triggered is the last round)\n"
            f"Families: {families}\nReason: {reason}{again}\n"
            f"Main may then close over that retry's review only if it asks for changes on the committed tree, its code findings are all in "
            f"these families, and a committed known-limits file names them.")


def authorize_stop(state, common, name, step, reason, confirm):
    """Record the owner's stop-rule authorization of `step` (one per step; a new one replaces the old). `confirm(text)` is the
    owner's typed-back code (owner_confirm.confirm); it is asked after the plan is read and before the lock is taken, and the plan
    is read again under the lock: a gate review or families that changed meanwhile refuse, so what is written is what was shown."""
    if not (reason or "").strip():
        raise ValueError("authorize-stop needs a --reason that says why the owner stops after this round")
    if len(reason) > lanes.MAX_REASON:
        raise ValueError(f"the --reason is longer than {lanes.MAX_REASON} characters")
    _named(common, name, step)
    record = lanes.read_lane(common, name)
    lane_use_checks(record)
    plan = stop_plan(Session(state), record, step, state)
    confirm(stop_confirm_text(name, plan, reason.strip()))
    with lanes.lane_lock(common):
        record = lanes.read_lane(common, name)
        lane_use_checks(record)
        now = stop_plan(Session(state), record, step, state)
        if now != plan:
            raise ValueError(f"step {step} of lane {name} changed while the owner confirmed (gate review {plan['gate_review']} -> "
                             f"{now['gate_review']}, families {plan['families']} -> {now['families']}, held authorization "
                             f"{plan['replaces']} -> {now['replaces']}); nothing was written, run it again")
        auth = {"id": f"stop-{secrets.token_hex(6)}", "authorized_at": lanes.utc_now(), "reason": reason.strip(), "step": step,
                "gate_review": plan["gate_review"], "families": plan["families"]}
        updated = dict(record)
        _without_authorization(updated, step)
        updated["stop_authorizations"] = updated.get("stop_authorizations", []) + [auth]
        return lanes.write_lane(common, updated)


MAX_KNOWN_LIMITS = 1 << 20


def _mentions(text, word):
    return re.search(r"(?<![A-Za-z0-9._-])" + re.escape(word) + r"(?![A-Za-z0-9_])", text) is not None


def stop_rule_checks(session, record, step, facts, stop, known_limits):
    """Checks c, d and e of `lane close --stop-rule` for the facts evaluate judged (a and b are close_step's). Returns the entry's
    stop_rule object; refuses naming the check and why. Reads only."""
    name = record["name"]
    back = (f"It goes back to the owner: a new authorization (`tp-agent lane authorize-stop {name} {step} --reason \"...\"`) or "
            f"`tp-agent lane close {name} {step} --override-review --reason \"...\"`, both at their terminal")
    closing = _task(session, facts["review"])
    author = _task(session, closing["review_of"]) if closing else None
    gate = author["meta"].get("gate_review") if author else None
    if gate != stop["gate_review"]:  # (c)
        whose = (f"its author {author['id']} records gate_review {gate}" if gate else
                 f"its author {author['id'] if author else closing and closing['review_of']} is not a retry triggered by a review (no gate_review)")
        raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (c): the closing review {facts['review']} reviewed "
                         f"work that is not the last round the owner authorized ({whose}; the authorization {stop['id']} is for the retry of "
                         f"gate review {stop['gate_review']}). An authorization covers one last round. {back}")
    needed = {}
    for ident in facts["rejecting"]:
        try:
            found = code_findings(_task(session, ident))
        except ValueError as error:
            raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (d): the findings of {ident} cannot all be "
                             f"accounted for ({error}). {INCOMPLETE.format(name=name, step=step)}")
        for finding, names in found:  # (d)
            if not names:
                raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (d): finding {finding} of {ident} closes by code "
                                 f"and names no Family:, so it cannot be shown to be one the owner authorized. {back}")
            new = [family for family in names if family not in stop["families"]]
            if new:
                raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (d): finding {finding} of {ident} is in "
                                 f"famil{'ies' if len(new) > 1 else 'y'} {', '.join(new)}, which the authorization {stop['id']} does not list "
                                 f"({', '.join(stop['families']) or 'none'}). {back}")
        needed[ident] = [finding for finding, _ in found]
    path, blob = _known_limits(record, known_limits, needed, step)
    return {"id": stop["id"], "authorized_at": stop["authorized_at"], "gate_review": stop["gate_review"], "families": stop["families"],
            "known_limits": path, "known_limits_blob": blob}


def _known_limits(record, given, needed, step):
    """(repo-relative path, blob id) of the known-limits file (e): a regular file committed at the main repository's HEAD or the
    lane's HEAD (relative to the repository, or absolute inside one of the two worktrees) whose committed text names every review
    in `needed` and each of its code finding ids. The committed blob is read, never the file on disk."""
    name = record["name"]
    main = lanes.main_worktree(record["git_common_dir"])
    tops = (("the main repository", main), ("the lane", record["worktree"]))
    raw = str(given)
    if raw.startswith("/"):
        real = os.path.realpath(raw)
        inside = [os.path.relpath(real, top) for _, top in tops if lanes._inside(real, top)]
        if not inside:
            raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (e): --known-limits {raw} is not inside {main} or "
                             f"{record['worktree']}; give the path relative to the repository")
        raw = inside[0]
    try:
        path = lanes.path_prefix(raw)
    except ValueError as error:
        raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (e): --known-limits {error}")
    words = [(word, word if word == ident else f"{word} (a code finding of {ident})")
             for ident, findings in needed.items() for word in [ident, *findings]]
    tried = []
    for where, top in tops:
        _, out = lanes._git(["ls-tree", "-z", "--full-tree", "HEAD", "--", path], top)
        rows = [row for row in out.split("\0") if row]
        meta, _, listed = rows[0].partition("\t") if len(rows) == 1 else ("", "", "")
        mode, kind, blob = (meta.split() + ["", "", ""])[:3]
        if listed != path or kind != "blob" or mode not in ("100644", "100755"):
            continue
        _, size = lanes._git(["cat-file", "-s", blob], top)
        if int(size.strip() or 0) > MAX_KNOWN_LIMITS:
            tried.append(f"at {where}'s HEAD it is over {MAX_KNOWN_LIMITS} bytes")
            continue
        _, text = lanes._git(["cat-file", "blob", blob], top)
        missing = [shown for word, shown in words if not _mentions(text, word)]
        if not missing:
            return path, blob
        tried.append(f"at {where}'s HEAD it does not name {', '.join(missing)}")
    if not tried:
        names = "; ".join(f"{ident}: {', '.join(findings)}" for ident, findings in needed.items())
        raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (e): {path} is not a file committed at the HEAD of "
                         f"the main repository ({main}) or of the lane ({record['worktree']}). Write it, naming {names}, and commit it")
    raise ValueError(f"step {step} of lane {name} cannot be closed under the stop rule (e): the known-limits file {path} must name every review "
                     f"of this tree that asked for changes and each of its code findings ({'; '.join(tried)}). Add them and commit it")


# --- the next-step rule (design checks 15-17) -----------------------------------------------------------------------

def lane_use_checks(record):
    """Checks 11 and 12 for a lane that has a record: it is open, and git agrees with the record."""
    if record["state"] != "open":
        raise ValueError(f"lane {record['name']} is {record['state']}; start a new lane from the integration branch")
    lanes.lane_reality(record)


def next_step_checks(session, record, step):
    """Checks 15, 16 and 17 of `dispatch --lane NAME --step STEP` for a lane that has a record, in that order. A task for the
    lane's open step (a retry, or a fix after REQUEST_CHANGES) passes 16 and 17: only a different step has to wait."""
    name = record["name"]
    entry = _closed_entry(record, step)
    if entry:  # 15
        raise ValueError(f"step {step} of lane {name} was closed at {entry['commit']}; start a new step id: "
                         f"`tp-agent dispatch --lane {name} --step NEW-ID --role ROLE --task \"...\"`")
    steps = open_steps(session, record)
    if step in steps:  # a task for a step that is already open starts no step
        return
    if steps:  # 16
        current = steps[0]
        facts = evaluate(session, record, current)
        why = facts["why"] or ("it changed nothing, so it needs no review, only the close" if facts["verdict"] == lanes.EMPTY
                               else "it is approved and committed but not recorded")
        after = "after" if facts["verdict"] == lanes.EMPTY else f"after the review of {current} finishes and"
        raise ValueError(f"lane {name}: step {current} is not closed ({why}). The next step starts {after} "
                         f"`tp-agent lane close {name} {current}` has recorded it")
    loose = [task for task in session.unreadable_of(record) if task["step"] is None]
    if loose:  # 16, for a task that only the lease places: its step is unknown, so no new step may start beside it
        raise ValueError(f"lane {name}: {unreadable_refusal(loose[0])}. The next step starts after that is cleared")
    # 17: no step is open, so nothing may be on the tree that no closed step accounts for
    worktree = record["worktree"]
    last = _previous(record)
    head, _ = _head(worktree)
    dirty = _dirty(worktree)
    if head == last and not dirty:
        return
    since = f"step {record['closed'][-1]['step']} closed" if record["closed"] else "the lane's base"
    at = f"HEAD {_short(head)} (closed at {_short(last)})" if record["closed"] else f"HEAD {_short(head)} (base {_short(last)})"
    ahead = int(lanes._git(["rev-list", "--count", f"{last}..{head}"], worktree)[1].strip() or 0) if head != last else 0
    moves = []
    if dirty:
        moves.append(f"git -C {worktree} stash push -u -m \"lane {name}: before step {step}\"")
    if ahead:
        save = f"saved/{name}-{head[:8]}"
        moves.append(f"git -C {worktree} branch {save} HEAD && git -C {worktree} reset --hard {last}")
    raise ValueError(f"lane {name} has changes since {since}: {at} / {dirty} uncommitted path(s). No step is open, so they belong to none"
                     f"{f' ({ahead} commit(s) after the closed one)' if ahead else ''}. Set them aside without losing them: "
                     f"{', then '.join(f'`{move}`' for move in moves)}; then dispatch again")


# --- the `lanes` row ------------------------------------------------------------------------------------------------

def row_fields(session, record):
    """open_step, occupant, queued and waiting_on of one lane's `lanes` row (design 3.3)."""
    name = record["name"]
    current = open_step(session, record)
    holders = lane_lease.live(lane_lease.read(record["git_common_dir"], name))
    occupant = None
    if holders:
        first = holders[0]
        occupant = {"task": first["task"], "role": first["role"], "lifecycle": lane_lease.holder_state(first) or "starting"}
    queue = [q for q in session.queued if q["lane"] == name]
    queued = {"task": min(queue, key=lambda q: q["when"])["id"]} if queue else None
    if record["state"] != "open":
        wait = "none"
    elif current:
        wait = evaluate(session, record, current)["wait"]
    else:
        wait = "orchestrator-dispatch"
    return {"open_step": current, "occupant": occupant, "queued": queued, "waiting_on": wait}
