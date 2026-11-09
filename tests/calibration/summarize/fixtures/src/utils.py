"""Helpers with no dependencies outside the standard library."""
import re

_DURATION = re.compile(r"^(\d+)([smh])$")
_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text):
    """'90s' -> 90, '5m' -> 300, '2h' -> 7200. Raise ValueError for anything else."""
    match = _DURATION.match(text.strip())
    if not match:
        raise ValueError("bad duration: " + text)
    return int(match.group(1)) * _UNITS[match.group(2)]


def chunked(items, size):
    """Yield lists of at most `size` items."""
    for start in range(0, len(items), size):
        yield items[start:start + size]


def slugify(text):
    """Lowercase, replace runs of non-alphanumerics with one hyphen, trim hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
