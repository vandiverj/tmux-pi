"""request_file — read permission-request.json / capability-request.json the one strict way, for every reader.

A request file is exactly one JSON object. Anything else that exists (empty, cut short, two objects, `null` then an object,
an array, a directory, bytes that are not UTF-8) is *unreadable*: a reader must say so and treat the task as needing
attention, never skip the file or let it abort a list. A file that is not there is simply no request.
"""
import errno
import json
import os

ABSENT, UNREADABLE, OBJECT = "absent", "unreadable", "object"
WORDS = "unreadable request"   # what a task shows when one of its request files is unreadable
REQUEST_NAMES = ("permission-request.json", "capability-request.json")


def _entry_exists(path):
    """Is there a directory entry of this name, whatever it points to? (lstat does not follow a link.)"""
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def read_request(path):
    """(kind, value): (ABSENT, None), (UNREADABLE, None) or (OBJECT, dict)."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        # Not found by name is not the same as not there: a dangling symlink is an entry that exists and reads as nothing.
        return (UNREADABLE, None) if _entry_exists(path) else (ABSENT, None)
    except OSError as error:
        # A missing parent (ENOTDIR on a path through a file) is no request either; everything else exists and cannot be read.
        return (ABSENT, None) if error.errno == errno.ENOTDIR else (UNREADABLE, None)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return UNREADABLE, None
    return (OBJECT, value) if isinstance(value, dict) else (UNREADABLE, None)


def is_pending(path):
    """Does this request file ask for something (status pending), or can it not be read at all?"""
    kind, value = read_request(path)
    return kind == UNREADABLE or (kind == OBJECT and value.get("status") == "pending")


def unreadable_names(directory):
    """The request files in a task directory that exist but are not exactly one JSON object."""
    return [name for name in REQUEST_NAMES if read_request(os.path.join(directory, name))[0] == UNREADABLE]
