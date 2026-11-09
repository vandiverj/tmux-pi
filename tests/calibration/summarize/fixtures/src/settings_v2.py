"""Settings: defaults, a JSON file, then environment variables; the result is validated."""
import json
import os

DEFAULTS = {
    "workers": 4,
    "poll_seconds": 0.5,
    "log_level": "info",
    "max_payload_bytes": 65536,
}

ENV_PREFIX = "JOBQ_"


def validate_settings(settings):
    """Raise ValueError when workers is below 1 or poll_seconds is not positive."""
    if settings["workers"] < 1:
        raise ValueError("workers must be at least 1")
    if settings["poll_seconds"] <= 0:
        raise ValueError("poll_seconds must be positive")


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
    validate_settings(settings)
    return settings
