"""Read, validate and compare the records of tools/agent/pre-commit-gate.sh (`tp-gate:v1`) and test-run.sh (`tp-test-run:v1`).

A record names the tree it saw: `head` (the commit) and `tree` (git write-tree of a temporary index that holds tracked
changes and untracked, non-ignored files). `status` says whether the working tree still is that tree.
"""
import json
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

GATE_SCHEMA = "tp-gate:v1"
TEST_RUN_SCHEMA = "tp-test-run:v1"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_base(value):
    return isinstance(value, dict) and isinstance(value.get("ref"), str) and isinstance(value.get("sha"), str)


# The one type table. A field is (kind, allowed): the kind names the JSON type it must have; `allowed`, when not None, is
# the only values a string of that field may take. validate() checks every field of a record against this table, and
# nothing else decides what a valid record is.
KINDS = {
    "str": (lambda v: isinstance(v, str), "a string"),
    "int": (_is_int, "an int"),
    "number": (_is_number, "a number"),
    "bool": (lambda v: isinstance(v, bool), "a bool"),
    "null": (lambda v: v is None, "null"),
    "strings": (_is_strings, "a list of strings"),
    "checks": (lambda v: isinstance(v, list), "a list"),
    "base": (_is_base, "an object with string ref and sha"),
}
STR = ("str", None)
RECORD_FIELDS = {
    GATE_SCHEMA: {
        "schema": ("str", (GATE_SCHEMA,)), "worktree": STR, "base": ("base", None), "head": STR, "tree": STR,
        "files": ("strings", None), "checks": ("checks", None), "exit": ("int", None), "started": STR, "finished": STR, "log": STR,
    },
    TEST_RUN_SCHEMA: {
        "schema": ("str", (TEST_RUN_SCHEMA,)), "label": STR, "cmd": ("strings", None), "cwd": STR, "head": STR, "tree": STR,
        "exit": ("int", None), "timed_out": ("bool", None), "seconds": ("number", None), "log": STR, "summary": STR,
    },
}

# The fields a check of the gate record must hold, by its status. A check that ran has an exit code; one that did not
# run (tool missing, nothing to check) has exit null. Every status has name and seconds.
CHECK_STATUSES = ("passed", "failed", "missing", "not_applicable")
_RAN = {"name": STR, "status": ("str", CHECK_STATUSES), "exit": ("int", None), "seconds": ("number", None)}
_NOT_RAN = {"name": STR, "status": ("str", CHECK_STATUSES), "exit": ("null", None), "seconds": ("number", None)}
CHECK_FIELDS = {"passed": _RAN, "failed": _RAN, "missing": _NOT_RAN, "not_applicable": _NOT_RAN}


def load(path) -> dict:
    """The record in the file. Raises ValueError when it is not a JSON object, OSError when it cannot be read."""
    with open(path, encoding="utf-8") as handle:
        try:
            record = json.load(handle)
        except json.JSONDecodeError as exc:
            raise ValueError("%s: not JSON: %s" % (path, exc))
    if not isinstance(record, dict):
        raise ValueError("%s: not a JSON object" % path)
    return record


def as_bytes(value: str) -> bytes:
    """The exact bytes behind a string of a record (a file name, a command argument). The writer decodes with
    os.fsdecode, so a byte that is not UTF-8 is stored as the surrogate \\udcXX; os.fsencode turns it back."""
    return os.fsencode(value)


def _field_problems(owner, obj, fields):
    problems = []
    for key, (kind, allowed) in fields.items():
        test, what = KINDS[kind]
        if key not in obj:
            problems.append("%smissing key: %s" % (owner, key))
        elif not test(obj[key]):
            problems.append("%s%s is not %s" % (owner, key, what))
        elif allowed is not None and obj[key] not in allowed:
            problems.append("%s%s is not one of %s" % (owner, key, ", ".join(allowed)))
    return problems


def validate(record) -> List[str]:
    """The problems of a record; an empty list means it is valid. It checks the record against the tables above and never
    raises, whatever JSON it is given."""
    if not isinstance(record, dict):
        return ["not an object"]
    schema = record.get("schema")
    if not isinstance(schema, str) or schema not in RECORD_FIELDS:
        return ["unknown schema: %r" % (schema,)]
    problems = _field_problems("", record, RECORD_FIELDS[schema])
    if schema == GATE_SCHEMA and isinstance(record.get("checks"), list):
        for index, check in enumerate(record["checks"]):
            if not isinstance(check, dict) or not isinstance(check.get("name"), str):
                problems.append("checks[%d] has no name" % index)
            elif not isinstance(check.get("status"), str) or check["status"] not in CHECK_FIELDS:
                problems.append("check %s has status %r" % (check["name"], check.get("status")))
            else:
                problems += _field_problems("check %s: " % check["name"], check, CHECK_FIELDS[check["status"]])
    return problems


def _git_raw(worktree, *args, env=None, data=None) -> bytes:
    out = subprocess.run(["git", "-C", str(worktree), *args], env=env, input=data, capture_output=True)
    if out.returncode != 0:
        raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), worktree, out.stderr.decode(errors="replace").strip()))
    return out.stdout


def _git(worktree, *args, env=None) -> str:
    out = subprocess.run(["git", "-C", str(worktree), *args], env=env, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), worktree, out.stderr.strip()))
    return out.stdout.strip()


def fingerprint(worktree) -> Tuple[str, str]:
    """(head, tree) of the working tree. The steps of tp_tree_fingerprint in tools/agent/_common.sh: a temporary index
    seeded with HEAD's tree and every path of the real index (tracked-but-ignored files stay in; the real index's
    assume-unchanged and skip-worktree flags are not inherited), then `git add -A`, then `git write-tree`. head is ""
    in a repository without a commit."""
    head = subprocess.run(["git", "-C", str(worktree), "rev-parse", "--verify", "--quiet", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    scratch = tempfile.mkdtemp(prefix="tp-fp.")
    try:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(scratch, "index"))
        if head:
            _git(worktree, "read-tree", "HEAD", env=env)
        paths = _git_raw(worktree, "ls-files", "-z")
        _git_raw(worktree, "update-index", "--add", "--remove", "-z", "--stdin", env=env, data=paths)
        _git(worktree, "add", "-A", env=env)
        return head, _git(worktree, "write-tree", env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def current(worktree) -> str:
    """The tree fingerprint of the working tree now."""
    return fingerprint(worktree)[1]


def status(record, worktree) -> str:
    """`invalid` when the record fails validate(); `fresh` when the working tree is the one it saw (same head and same
    tree); else `stale`. A worktree that cannot be fingerprinted is `stale`: nothing proves the record still holds."""
    if validate(record):
        return "invalid"
    try:
        head, tree = fingerprint(worktree)
    except (RuntimeError, OSError):
        return "stale"
    return "fresh" if (record["head"], record["tree"]) == (head, tree) else "stale"


def _matches(name: str, required: str) -> bool:
    return name == required or name.startswith(required + ":")


def missing_required(record, names) -> List[str]:
    """The checks of a gate record whose name is one of `names` (or `NAME:...`, so `typecheck` covers `typecheck:web`)
    and whose status is `missing`. The gate script fails on exactly these when it is given --require."""
    if not isinstance(record, dict) or record.get("schema") != GATE_SCHEMA or not isinstance(record.get("checks"), list):
        return []
    found = []  # type: List[str]
    for check in record["checks"]:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            continue
        if check.get("status") == "missing" and any(_matches(check["name"], r) for r in names):
            found.append(check["name"])
    return found
