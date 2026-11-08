"""Task specs: one JSON file per task in bench/tasks/ (schema tp-bench-task:v1). Answers are never in a spec, only their sha256."""
import fnmatch
import hashlib
import json
import re
from pathlib import Path

SCHEMA = "tp-bench-task:v1"
KINDS = ("code", "review", "local", "orient")
ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_LEVEL = "small"  # both arms use the same model
ROLE = {"code": "coder", "orient": "coder", "local": "researcher"}


class SpecError(ValueError):
    pass


def _need(spec, name, kind, path):
    value = spec.get(name)
    ok = isinstance(value, kind) and not isinstance(value, bool)
    if ok and kind is str:
        ok = bool(value.strip())
    if not ok:
        raise SpecError(f"{path}: '{name}' must be {'a non-empty string' if kind is str else 'an integer'}")


def validate(spec, path="spec"):
    if not isinstance(spec, dict) or spec.get("schema") != SCHEMA:
        raise SpecError(f"{path}: schema must be {SCHEMA}")
    _need(spec, "id", str, path)
    if not ID.fullmatch(spec["id"]):
        raise SpecError(f"{path}: id '{spec['id']}' must be lower-case letters, digits and dashes")
    if spec.get("kind") not in KINDS:
        raise SpecError(f"{path}: kind must be one of {', '.join(KINDS)}")
    _need(spec, "title", str, path)
    _need(spec, "budget_s", int, path)
    if spec["budget_s"] <= 0:
        raise SpecError(f"{path}: budget_s must be a positive number of seconds")
    kind = spec["kind"]
    required = {"code": ("brief", "test_path"), "orient": ("brief",), "local": ("question",), "review": ("request", "diff")}[kind]
    for name in required:
        _need(spec, name, str, path)
    if kind != "orient":
        answer = spec.get("answer")
        if not isinstance(answer, dict) or not isinstance(answer.get("file"), str) or not re.fullmatch(r"[\w.-]+", answer["file"]):
            raise SpecError(f"{path}: answer.file must be a plain file name in the answers directory")
        if "sha256" in answer and not re.fullmatch(r"[0-9a-f]{64}", str(answer["sha256"])):
            raise SpecError(f"{path}: answer.sha256 must be 64 hex digits")
    elif "answer" in spec:
        raise SpecError(f"{path}: an orient task is scored by its trace and has no answer")
    if kind == "review" and not isinstance(spec.get("clean", False), bool):
        raise SpecError(f"{path}: clean must be true or false")
    if kind == "code" and not spec["test_path"].startswith("src/"):
        raise SpecError(f"{path}: test_path must be under src/")
    return spec


def load_specs(tasks_dir):
    specs, seen = [], {}
    for path in sorted(Path(tasks_dir).glob("*.json")):
        try:
            spec = validate(json.loads(path.read_text()), path.name)
        except ValueError as error:  # SpecError and bad JSON
            raise SpecError(f"{path.name}: {error}" if not isinstance(error, SpecError) else str(error)) from error
        if spec["id"] in seen:
            raise SpecError(f"{path.name}: id {spec['id']} is also in {seen[spec['id']]}")
        if path.stem != spec["id"]:
            raise SpecError(f"{path.name}: the file name must be the task id ({spec['id']}.json)")
        seen[spec["id"]] = path.name
        specs.append(spec)
    return specs


def select(specs, globs):
    """The specs whose id matches any comma-separated glob, in id order. A glob that matches nothing is an error."""
    patterns = [g.strip() for g in (globs or "*").split(",") if g.strip()]
    chosen = [s for s in specs if any(fnmatch.fnmatchcase(s["id"], p) for p in patterns)]
    if not chosen:
        raise SpecError(f"no task matches {globs!r}; known: {', '.join(s['id'] for s in specs) or 'none'}")
    return sorted(chosen, key=lambda s: s["id"])


def task_set_hash(specs, bench_dir):
    """sha256 over the selected specs (their answer hashes included) and the diff files they name."""
    parts = []
    for spec in sorted(specs, key=lambda s: s["id"]):
        entry = {"spec": spec}
        if spec.get("diff"):
            entry["diff_sha256"] = hashlib.sha256((Path(bench_dir) / spec["diff"]).read_bytes()).hexdigest()
        parts.append(entry)
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
