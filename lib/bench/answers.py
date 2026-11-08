"""Hidden answers (tests, planted-defect keys, exact answers) live outside the repo, each with its sha256 recorded in the task spec."""
import hashlib
import json
import os
from pathlib import Path


class AnswerError(RuntimeError):
    def __init__(self, task_id, why):
        super().__init__(f"answer for task {task_id} {why}")
        self.task_id = task_id


def home():
    return Path(os.environ.get("TP_BENCH_HOME") or Path.home() / ".local" / "share" / "tp-bench")


def answers_dir():
    return Path(os.environ.get("TP_BENCH_ANSWERS") or home() / "answers")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def answer_path(spec, directory=None):
    return Path(directory or answers_dir()) / spec["answer"]["file"]


def verify(spec, directory=None):
    """Refuse (AnswerError naming the task id) an answer that is not sealed, missing, or not the file the spec recorded."""
    if spec["kind"] == "orient":
        return None
    recorded = spec["answer"].get("sha256")
    if not recorded:
        raise AnswerError(spec["id"], "is not sealed (the spec has no sha256; run tp-bench seal)")
    path = answer_path(spec, directory)
    try:
        found = sha256_file(path)
    except OSError:
        raise AnswerError(spec["id"], f"is missing ({path})") from None
    if found != recorded:
        raise AnswerError(spec["id"], f"changed: expected sha256 {recorded}, found {found}")
    return path


def read_json(spec, directory=None):
    return json.loads(verify(spec, directory).read_text())


def seal(specs, tasks_dir, source, directory=None):
    """Copy each answer file from source (bench/answers-src/) to the answers directory and write its sha256 into the spec file.
    Returns [(task id, sha256)]. A task whose source file is missing stops the seal before any file is written."""
    target = Path(directory or answers_dir())
    wanted = [s for s in specs if s["kind"] != "orient"]
    for spec in wanted:
        if not (Path(source) / spec["answer"]["file"]).is_file():
            raise AnswerError(spec["id"], f"has no source file {Path(source) / spec['answer']['file']}")
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    sealed = []
    for spec in wanted:
        data = (Path(source) / spec["answer"]["file"]).read_bytes()
        out = target / spec["answer"]["file"]
        out.write_bytes(data)
        os.chmod(out, 0o600)
        spec["answer"]["sha256"] = hashlib.sha256(data).hexdigest()
        (Path(tasks_dir) / f"{spec['id']}.json").write_text(json.dumps(spec, indent=2) + "\n")
        sealed.append((spec["id"], spec["answer"]["sha256"]))
    return sealed
