"""Settings: defaults, a JSON file, then environment variables."""
import json
import os

DEFAULTS = {
    "workers": 2,
    "poll_seconds": 0.5,
    "log_level": "info",
    "state_file": "state.json",
}

ENV_PREFIX = "JOBQ_"


def load_settings(path=None):
    """Return DEFAULTS, updated from the JSON file at path (if it exists), then from JOBQ_* variables."""
    settings = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            settings.update(json.load(handle))
    for key in DEFAULTS:
        value = os.environ.get(ENV_PREFIX + key.upper())
        if value is not None:
            settings[key] = type(DEFAULTS[key])(value)
    return settings
