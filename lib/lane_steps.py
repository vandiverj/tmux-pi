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
a non-empty Coverage section. Reviews of another tree are stale and ignored. There is no skip and no override.

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
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_commands  # noqa: E402
import lane_lease  # noqa: E402
import lanes  # noqa: E402
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
    review_verdict.assess (the one reader); a REQUEST_CHANGES verdict rejects even when its Coverage section is missing."""
    life = life_of(task)
    if life in AT_WORK:
        return "running", life
    if life not in FINISHED_REVIEW:
        return "unusable", f"it ended {life}"
    try:
        verdict, _, incomplete = review_verdict.assess(review_verdict.read_review(task["path"] / "result.md"))
    except review_verdict.ReviewMissing:
        return "unusable", "it has no result"
    except ValueError as error:
        return "unusable", str(error)
    if verdict == "REQUEST_CHANGES":
        return "rejects", verdict
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


def evaluate(session, record, step):
    """The facts of the review gate for `step` of `record`'s lane: {ok, why, wait, review, verdict, tree, dirty, head}.
    `wait` is who the step is waiting for (the `lanes` column waiting_on): worker | review | orchestrator-dispatch |
    orchestrator-commit | orchestrator-close. `why` is empty when ok. Reads only; raises ValueError when git cannot say."""
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
    if rejecting:
        review = rejecting[0][0]
        fix = (f"Change the work, for example `tp-agent retry {author['id']} --reason \"address the findings of {review['id']}\"`, and review the new tree"
               if author else f"Change the work with a task in the step ({_new_task(name, step, writers)}) and review the new tree")
        return refuse("orchestrator-dispatch", f"{review['id']} asked for changes to this tree ({where}) and a review of the same tree cannot undo that. {fix}")
    running = [j for j in judged if j[1] == "running"]
    if running:
        review, life = running[0][0], running[0][2]
        how = "Wait for it to finish, or end it" if finishes_alone(life) else _answer(review["id"]) + ", or end it"
        return refuse("review", f"{review['id']} is reviewing this tree and is {state_words(life)}. {how} with {_stop(review['id'])}")
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
    if dirty:
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


def close_step(state, common, name, step):
    """Record that `step` of lane `name` is finished and bound to the commit the reviewed work is committed as. Every refusal
    is raised before anything is written; the one write is the new entry in `closed`, under the lane lock. Returns the record."""
    lane_commands.check_lane_name(name)
    if not lanes.STEP_ID.fullmatch(step):
        raise ValueError(f"invalid step id {step!r}: use up to 40 characters of letters, digits, . _ -")
    if name not in lanes.lane_names(common):
        raise ValueError(f"there is no lane {name} in {lanes.lanes_dir(common)}; `tp-agent lanes` lists the lanes")
    with lanes.lane_lock(common):
        record = lanes.read_lane(common, name)
        lane_use_checks(record)
        session = Session(state)
        seen = session.steps(name)
        entry = _closed_entry(record, step)
        if step not in seen and not entry:
            here = f"its steps in this session are {', '.join(seen)}" if seen else "no task of this session belongs to the lane"
            raise ValueError(f"lane {name} has no step {step} in this session ({state}): {here}. Check the id with `tp-agent lanes {name}`; if the "
                             f"step ran in another session, run this from that session's workspace (or TP_STATE_DIR=<its state directory> tp-agent lane close {name} {step})")
        if entry:
            raise ValueError(f"step {step} is already closed at {entry['commit']}; it is recorded, so there is nothing to do. The next step takes a "
                             f"new id: `tp-agent dispatch --lane {name} --step NEW-ID --role ROLE --task \"...\"`")
        current = open_step(session, record)  # a step with tasks that is not closed is open, so `current` is set whenever `step` is
        if current != step:
            raise ValueError(f"step {step} is not lane {name}'s open step ({current}, the oldest that is not closed); close that one first with "
                             f"`tp-agent lane close {name} {current}`")
        facts = evaluate(session, record, step)
        if not facts["ok"]:
            raise ValueError(f"step {step} of lane {name} cannot be closed: {facts['why']}")
        head, head_tree = _head(record["worktree"])  # the clean tree evaluate judged: its commit is what the step is recorded as
        updated = dict(record, closed=record["closed"] + [{"step": step, "commit": head, "tree": head_tree, "review": facts["review"],
                                                            "verdict": facts["verdict"], "closed_at": lanes.utc_now()}])
        lanes.lane_reality(updated)  # the new entry is checked against git before it is written, never after
        return lanes.write_lane(common, updated)


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
