"""lifecycle_files — the one list of files a task's lifecycle is worked out from.

lib/contract.py task_lifecycle reads exactly these (and refuses any other name); lib/tp_window.py fingerprints exactly
these to know whether a cached verdict is still good. Both take the list from here, so they cannot drift apart. It is a
module of its own because the status pane needs the list on every refresh and should not import contract.py for it.
"""
from request_file import REQUEST_NAMES

LIFECYCLE_FILES = ("state.json", "checkpoints.json", "result.md", "completed", "exit", "escalated", "stopped.json",
                   "collected.json", "pending-capability.json") + REQUEST_NAMES
