"""Task families: the tasks of one piece of work, linked through the data already in each task's meta.json.

A family is one root task and everything that follows from it:
  - retries: a task whose `parent` is the root (or a later retry) and an escalated retry (`escalated_from` / `escalated_to`);
  - reviews: the tasks a task names in `review` / `reviews` (or whose `review-of` file names it);
  - the preparation tasks (researcher, architect) of the step, which are their own roots unless something links them.
The family id is `fam-<root task id>`: the topmost task the chain names. When that task's record is gone (archived away,
removed) the id still comes from its name, so it stays the same if the record turns up later, and `root_missing` says so.
Two coders given the same lane step with no link between them are two families that carry the same lane and lane_step.

Per family (see `family_record`): `attempts` and `reviews` in order, `first_review_verdict`, `review_rounds`, `accepted`
(the last attempt whose latest review approves; None for a family with no approving review) and `status` (accepted,
open: reviewed but not approved, unreviewed), frontier and local tokens (input, output, cache_read, cache_write, cache),
and wall time. A token or time that was not recorded is None, never 0, and `*_unknown_tasks` counts them.

Family labels: `families` is the `Family:` names of each review round (review_verdict.families on that review's result.md), a list
with one list per round, [] for an unlabelled round; a review whose result.md is gone or cannot be read is unlabelled. `labelled_rounds`
counts the rounds with a name and `repeated_families` the names that appear in 2+ rounds (a name repeated inside one round counts once).
`tp-evals families` ends with a one-line summary of them.

Facts only: a verdict here is read from the reviewer's reply the way `tp-evals export` reads it (routing_evals.review_outcome).

Wired into `tp-evals families [--project DIR] [--json]`: bin/tp-evals calls `task_family.add_subcommand(sub)` and, for that
command, `args.func(args)`.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import review_verdict  # noqa: E402
import routing_evals as re_  # noqa: E402

FAMILY_SCHEMA = "tmux-pi-task-family:v1"
PREP_ROLES = ("researcher", "architect", "investigator", "planner")  # read-only work done before a coder starts
LOCAL_MODELS = ("slotstream/", "lmstudio/", "mlx/", "ollama/")  # the same prefixes bin/tp-agent counts as local
APPROVING = ("APPROVED", "APPROVED_WITH_NOTES")
MAX_CHAIN = 500  # a safety limit on a parent walk; a loop or a longer chain ends at the task reached
TOKEN_KEYS = (("input", "input"), ("output", "output"), ("cache_read", "cacheRead"), ("cache_write", "cacheWrite"))


def _text_id(value):
    return value if isinstance(value, str) and re_.TASK_ID.match(value) else None


def _load(state_dir):
    """{task id: node} for the readable tasks of one state dir (tasks/ and archive/)."""
    nodes = {}
    for d in re_._task_dirs(state_dir):
        meta = re_._read_json(d / "meta.json") or {}
        ident = _text_id(meta.get("id")) or d.name
        state = re_._read_json(d / "state.json") or {}
        review_of = re_._read_line(d / "review-of")
        reviews = meta.get("reviews") if isinstance(meta.get("reviews"), list) else [meta.get("review")]
        escalated_from = meta.get("escalated_from") if isinstance(meta.get("escalated_from"), dict) else {}
        escalated_to = meta.get("escalated_to") if isinstance(meta.get("escalated_to"), dict) else {}
        nodes[ident] = {
            "id": ident, "dir": d, "meta": meta, "state": state, "role": meta.get("role") or ("code-reviewer" if ident.startswith("review-") else "generic"),
            "parent": _text_id(meta.get("parent")), "escalated_from": _text_id(escalated_from.get("id")),
            "escalated_to": _text_id(escalated_to.get("id")),
            "review_ids": [r for r in (_text_id(r) for r in reviews) if r], "review_of": _text_id(review_of),
            "created": re_._parse_time(meta.get("created")), "updated": re_._parse_time(state.get("updated")),
        }
    return nodes


def _reviewers(nodes):
    """{reviewer id: author id}: from the author's review/reviews; a review-of file covers the reviewer a record does not name."""
    owner = {}
    for node in nodes.values():
        for r in node["review_ids"]:
            owner.setdefault(r, node["id"])
    for node in nodes.values():
        if node["review_of"] and node["id"] not in owner:
            owner[node["id"]] = node["review_of"]
    return owner


def _before(nodes):
    """{task id: the task it continues}: its parent, else the task it was escalated from (either side may name the link)."""
    before = {}
    for node in nodes.values():
        link = node["parent"] or node["escalated_from"]
        if link:
            before[node["id"]] = link
    for node in nodes.values():
        target = node["escalated_to"]
        if target and target in nodes and target not in before and target != node["id"]:
            before[target] = node["id"]
    return before


def _root(ident, before):
    seen = [ident]
    while ident in before and len(seen) < MAX_CHAIN:
        nxt = before[ident]
        if nxt in seen:  # a loop: every task in it gets the same root, the smallest id
            return min(seen[seen.index(nxt):])
        ident = nxt
        seen.append(ident)
    return ident


def _kind(node):
    return "prep" if node["role"] in PREP_ROLES else "attempt"


def _order(node):
    return (node["created"] is None, node["created"].timestamp() if node["created"] else 0, node["id"])


def _local(node):
    return str(node["state"].get("model") or node["meta"].get("model") or "").startswith(LOCAL_MODELS)


def _tokens(nodes):
    """{input, output, cache_read, cache_write, cache, unknown_tasks} summed over the tasks that recorded them."""
    out = {}
    for name, key in TOKEN_KEYS:
        total, missing = re_.measured_total([(n["state"].get("tokens") or {}).get(key)
                                             if isinstance(n["state"].get("tokens"), dict) else None for n in nodes])
        out[name] = total
        out[name + "_unknown_tasks"] = missing
    cache = [out["cache_read"], out["cache_write"]]
    out["cache"] = sum(c for c in cache if c is not None) if any(c is not None for c in cache) else None
    return out


def _round_families(reviewer_dir):
    """The distinct Family: names in one review's result.md; [] when it has none or cannot be read."""
    try:
        return review_verdict.families(review_verdict.read_review(Path(reviewer_dir) / "result.md"))
    except (ValueError, OSError):  # ReviewMissing is a ValueError; an unreadable or undecodable file is unlabelled
        return []


def _entry(node):
    meta = node["meta"]
    return {"id": node["id"], "role": node["role"], "worker": meta.get("worker"), "model": node["state"].get("model") or meta.get("model"),
            "local": _local(node), "state": node["state"].get("state"), "created": meta.get("created"),
            "parent": node["parent"] or node["escalated_from"], "escalated": bool(node["escalated_from"] or node["escalated_to"])}


def family_record(root, members, owner, session=None):
    """One family from its member nodes (author side, then the reviewers named for them)."""
    members = sorted(members, key=_order)
    authors = [n for n in members if n["id"] not in owner]
    reviewers = [n for n in members if n["id"] in owner]
    attempts = []
    for n in authors:
        entry = dict(_entry(n), kind=_kind(n))
        attempts.append(entry)
    # Rounds come from the review ids the authors name (meta.reviews), not from the review dirs that survive: a pruned
    # review still was a round, and when the first one is gone the first verdict is unknown, not the next one's.
    declared = []
    for n in authors:
        declared += [r for r in n["review_ids"] if r not in declared]
    present = {n["id"]: n for n in reviewers}
    extra = [n["id"] for n in reviewers if n["id"] not in declared]
    sequence = declared + extra
    missing_reviews = [r for r in sequence if r not in present]
    reviewers = [present[r] for r in sequence if r in present]
    reviews, latest = [], {}
    for rn in reviewers:
        outcome = re_.review_outcome(rn["dir"])
        entry = {"id": rn["id"], "of": owner[rn["id"]], "round": sequence.index(rn["id"]) + 1, "verdict": outcome["verdict"],
                 "model": rn["state"].get("model") or rn["meta"].get("model"), "created": rn["meta"].get("created")}
        for key in ("stated_verdict", "incomplete_reason", "unusable_reason"):
            if key in outcome:
                entry[key] = outcome[key]
        entry["families"] = _round_families(rn["dir"])
        reviews.append(entry)
        latest[owner[rn["id"]]] = entry["verdict"]
    by_round = {r["id"]: r["families"] for r in reviews}
    round_families = [by_round.get(r, []) for r in sequence]  # a pruned review is a round with no label
    seen_in = {}
    for names in round_families:
        for name in names:
            seen_in[name] = seen_in.get(name, 0) + 1
    accepted = next((a["id"] for a in reversed(attempts) if a["kind"] == "attempt" and latest.get(a["id"]) in APPROVING), None)
    status = "accepted" if accepted else ("open" if sequence else "unreviewed")
    timeline = [{"id": n["id"], "kind": "review" if n["id"] in owner else _kind(n)} for n in members]
    created = [n["created"] for n in members if n["created"]]
    updated = [n["updated"] for n in members if n["updated"]]
    wall = int((max(updated) - min(created)).total_seconds()) if created and updated and max(updated) >= min(created) else None
    each = [int((n["updated"] - n["created"]).total_seconds()) if n["created"] and n["updated"] and n["updated"] >= n["created"] else None
            for n in members]
    task_seconds, task_seconds_missing = re_.measured_total(each)
    root_node = next((n for n in members if n["id"] == root), None)
    lane_node = next((n for n in members if n["meta"].get("lane")), None)
    lane_meta = lane_node["meta"] if lane_node else {}
    return {
        "schema": FAMILY_SCHEMA, "family_id": "fam-" + root, "root": root, "root_missing": root_node is None,
        "session": session, "lane": lane_meta.get("lane"), "lane_step": lane_meta.get("lane_step"),
        "attempts": attempts, "reviews": reviews, "timeline": timeline,
        "first_review_verdict": reviews[0]["verdict"] if reviews and sequence[0] in present else None,
        "first_review_known": bool(sequence) and sequence[0] in present, "review_rounds": len(sequence),
        "missing_reviews": missing_reviews,
        "families": round_families, "labelled_rounds": sum(1 for names in round_families if names),
        "repeated_families": [n for n, c in seen_in.items() if c > 1],
        "accepted": accepted, "status": status,
        "tokens": {"frontier": _tokens([n for n in members if not _local(n)]), "local": _tokens([n for n in members if _local(n)])},
        "wall_seconds": wall, "task_seconds": task_seconds, "task_seconds_unknown_tasks": task_seconds_missing,
    }


def families(state_dir, session=None):
    """Family records for one state dir, ordered by their first task."""
    nodes = _load(state_dir)
    owner = _reviewers(nodes)
    before = _before({k: v for k, v in nodes.items() if k not in owner})
    groups = {}
    for ident, node in nodes.items():
        author = ident
        seen = {ident}
        while author in owner and owner[author] in nodes and owner[author] not in seen:  # a review of a review follows to the work
            author = owner[author]
            seen.add(author)
        groups.setdefault(_root(author, before), []).append(node)
    out = [family_record(root, members, owner, session) for root, members in groups.items()]
    firsts = {r["family_id"]: min(_order(n) for n in groups[r["root"]]) for r in out}
    return sorted(out, key=lambda r: firsts[r["family_id"]])


def state_dirs(project):
    """State dirs under a project: the dir itself or its .tmux-pi when it holds tasks, else each .tmux-pi/sessions/<id>."""
    root = Path(project)
    found = []
    for cand in (root, root / ".tmux-pi"):
        if (cand / "tasks").is_dir() or (cand / "archive").is_dir():
            found.append(cand)
    sessions = root / ".tmux-pi" / "sessions"
    if sessions.is_dir() and not sessions.is_symlink():
        found += [d for d in sorted(sessions.iterdir()) if d.is_dir() and not d.is_symlink()]
    return found


def project_families(project):
    out = []
    for d in state_dirs(project):
        out += families(d, session=d.name)
    return out


def _count(value):
    return "-" if value is None else str(value)


def render(records):
    """One line per family, then its attempts and reviews in order."""
    lines = []
    for r in records:
        tok, loc = r["tokens"]["frontier"], r["tokens"]["local"]
        lines.append("{}  {}  lane={}/{}  rounds={} first={} accepted={}  wall={}s  frontier in/out/cache={}/{}/{}  local in/out/cache={}/{}/{}".format(
            r["family_id"], r["status"], r["lane"] or "-", r["lane_step"] or "-", r["review_rounds"],
            r["first_review_verdict"] or "-", r["accepted"] or "-", _count(r["wall_seconds"]), _count(tok["input"]),
            _count(tok["output"]), _count(tok["cache"]), _count(loc["input"]), _count(loc["output"]), _count(loc["cache"])))
        by_id = {a["id"]: a for a in r["attempts"]}
        by_id.update({v["id"]: v for v in r["reviews"]})
        for item in r["timeline"]:
            e = by_id[item["id"]]
            lines.append("    {:<8} {}  {}".format(item["kind"], e["id"], e.get("verdict") or e.get("model") or "-"))
    return "\n".join(lines)


def summary(records):
    """One line: rounds with a label / all rounds, families with a repeated name, families with 3+ REQUEST_CHANGES and no repeated name."""
    rounds = sum(r["review_rounds"] for r in records)
    labelled = sum(r["labelled_rounds"] for r in records)
    repeated = sum(1 for r in records if r["repeated_families"])
    unnamed = sum(1 for r in records if not r["repeated_families"]
                  and sum(1 for v in r["reviews"] if v["verdict"] == "REQUEST_CHANGES") >= 3)
    return ("family labels: rounds with a label {}/{}; families with a repeated name {}; "
            "families with 3+ REQUEST_CHANGES and no repeated name {}".format(labelled, rounds, repeated, unnamed))


def run(project=None, as_json=False, out=sys.stdout):
    """`tp-evals families`: print the families under `project` (default: $TP_STATE_DIR, else the current directory)."""
    import os
    records = project_families(project or os.environ.get("TP_STATE_DIR") or ".")
    if as_json:
        out.write(json.dumps(records, indent=2, sort_keys=True) + "\n")
    elif records:
        out.write(render(records) + "\n" + summary(records) + "\n")
    return records


def add_subcommand(sub):
    """Add `families [--project DIR] [--json]` to an argparse subparsers object; its handler is args.func(args)."""
    parser = sub.add_parser("families", help="task families: attempts, reviews, accepted attempt, tokens and wall time")
    parser.add_argument("--project", help="project dir (default: $TP_STATE_DIR, else the current directory)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(func=lambda args: run(args.project, args.json) and None)
    return parser
