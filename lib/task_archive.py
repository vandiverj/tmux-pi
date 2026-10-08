"""Where a task went when it left tasks/. No dependencies, so a shell command can import it alone."""
import json
from pathlib import Path


def find_archived_task(state_dir, ident):
    """The archive directory holding task `ident`, or None. lib/task_record.py archive_task names it for the task, or
    <id>-ended-<stamp>[-N] when that name was taken, so the record's own id decides. The one lookup tp-agent and
    tp-review share."""
    archive = Path(state_dir) / "archive"
    if (archive / ident / "meta.json").is_file():
        return archive / ident
    if not archive.is_dir():
        return None
    for meta in sorted(archive.glob("*/meta.json")):
        try:
            found = json.loads(meta.read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError, AttributeError):
            continue
        if found == ident:
            return meta.parent
    return None
