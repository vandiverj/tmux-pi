"""Run records (schema tp-bench-run:v1): per-task results, the metrics derived from them, atomic write, numbering."""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

SCHEMA = "tp-bench-run:v1"
TOKEN_KEYS = (("input", "input"), ("output", "output"), ("cache_read", "cacheRead"), ("cache_write", "cacheWrite"))  # lib/task_family.py
METRICS = ("code_pass", "review_catch", "review_fp", "local_exact", "tokens_per_accepted", "orient_waste", "wall_s")
CORRECTNESS = ("code_pass", "review_catch", "local_exact")
LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def token_figures(raw):
    """A task record's `tokens` ({input, output, cacheRead, cacheWrite} or None) -> {name: int|None}.
    A figure that is not a number is None: unknown is never written as 0."""
    out = {}
    for name, key in TOKEN_KEYS:
        value = raw.get(key) if isinstance(raw, dict) else None
        out[name] = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return out


def add_tokens(parts):
    """Sum token figures over several records; a figure unknown in any part is None in the total."""
    total = {}
    for name, _ in TOKEN_KEYS:
        values = [p.get(name) for p in parts]
        total[name] = None if not values or any(v is None for v in values) else sum(values)
    return total


def accepted(task):
    return task["status"] == "pass" and task["kind"] in ("code", "local")


def _ratio(num, den):
    return None if not den else num / den


def metrics(tasks):
    """The seven PROTOCOL metrics from per-task results. A metric with nothing to measure is None.

    Denominators come from the task entries as the runner made them from the specs (`kind`, `clean`, and `planted`, the defect
    count of the sealed key), never from what a run printed: a task that errored, timed out, was stopped or never ran still counts,
    and contributes only misses. Only a scored review contributes caught defects."""
    code = [t for t in tasks if t["kind"] == "code"]
    local = [t for t in tasks if t["kind"] == "local"]
    reviews = [t for t in tasks if t["kind"] == "review"]
    defect_tasks = [t for t in reviews if not t.get("clean")]
    clean_tasks = [t for t in reviews if t.get("clean")]
    orient = [t for t in tasks if t["kind"] == "orient"]
    defects = sum(t["planted"] for t in defect_tasks)
    caught = sum(t["detail"].get("caught", 0) for t in defect_tasks if t["status"] == "scored")
    waste = [t["detail"].get("orient_waste") for t in orient]
    n_accepted = sum(1 for t in tasks if accepted(t))
    spent = [t["tokens"] for t in tasks]
    per_accepted = None
    if n_accepted and spent and all(s and s.get("input") is not None and s.get("output") is not None for s in spent):
        per_accepted = sum(s["input"] + s["output"] for s in spent) / n_accepted
    return {
        "code_pass": _ratio(sum(1 for t in code if t["status"] == "pass"), len(code)),
        "review_catch": _ratio(caught, defects),
        "review_fp": _ratio(sum(t["detail"].get("false_positives", 0) for t in clean_tasks if t["status"] == "scored"), len(clean_tasks)),
        "local_exact": _ratio(sum(1 for t in local if t["status"] == "pass"), len(local)),
        "tokens_per_accepted": per_accepted,
        "orient_waste": None if not orient or any(w is None for w in waste) else sum(waste) / len(orient),
        "wall_s": sum(t.get("wall_s") or 0 for t in tasks),
    }


def run_tokens(tasks):
    """(totals, token_sources): the run's token sum and a note for each figure that could not be summed."""
    total = add_tokens([t["tokens"] or {} for t in tasks])
    notes = {}
    for name, _ in TOKEN_KEYS:
        if total[name] is None:
            missing = [t["id"] for t in tasks if not t["tokens"] or t["tokens"].get(name) is None]
            notes[name] = "unavailable for task(s) " + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "") \
                if missing else "no tasks"
    notes["main"] = "not recorded: the Main session's tokens are not in task records (E2 adds them)"
    return total, notes


def _number_of(path):
    """The repeat number a results entry belongs to: `<n>.json`, `<n>.cleanup.json` or the directory `<n>/`; None for anything else."""
    name = path.name
    if path.is_dir():
        stem = name
    elif name.endswith(".cleanup.json"):
        stem = name[:-len(".cleanup.json")]
    elif name.endswith(".json"):
        stem = name[:-len(".json")]
    else:
        return None
    return int(stem) if stem.isdigit() else None


def next_number(results_dir, label):
    """One more than the highest number any kind of record of this label uses: run records, cleanup incident files, artifacts dirs."""
    folder = Path(results_dir) / label
    numbers = [n for n in map(_number_of, folder.iterdir()) if n is not None] if folder.is_dir() else []
    return max(numbers, default=0) + 1


def reserve(results_dir, label):
    """Claim the next number by creating `<label>/<n>/` exclusively; on EEXIST (another run took it) try the next one. Returns n.
    Every later file of that run (`<n>.json`, `<n>.cleanup.json`, `<n>/artifacts/`) is created under the claimed number."""
    folder = Path(results_dir) / label
    folder.mkdir(parents=True, exist_ok=True)
    taken = 0
    while True:
        n = max(next_number(results_dir, label), taken + 1)  # always moves past a number that just failed with EEXIST
        try:
            (folder / str(n)).mkdir()
        except FileExistsError:
            taken = n
            continue
        return n


def record_path(results_dir, label, n):
    return Path(results_dir) / label / f"{n}.json"


def cleanup_path(results_dir, label, n):
    return Path(results_dir) / label / f"{n}.cleanup.json"


def write(path, data):
    """Create `path` with `data`, whole or not at all, and never replace a file: the JSON goes to a temp file that is hard-linked to
    `path` (os.link raises FileExistsError when it exists), so a record already there is left untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".run-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.link(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def copy_file_exclusive(src, dest):
    """Copy src to dest; FileExistsError instead of overwriting."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as source, open(dest, "xb") as target:
        shutil.copyfileobj(source, target)


def load(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} record")
    return data


def load_arm(results_dir, label):
    """All run records of a label, in repeat order."""
    folder = Path(results_dir) / label
    files = sorted((p for p in folder.glob("*.json") if p.stem.isdigit()), key=lambda p: int(p.stem)) if folder.is_dir() else []
    if not files:
        raise ValueError(f"no run records for label {label!r} in {folder}")
    return [load(p) for p in files]
