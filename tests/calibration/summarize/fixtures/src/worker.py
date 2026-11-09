"""Runs jobs from a JobQueue with a table of handlers."""
import logging
import time

from settings import load_settings

log = logging.getLogger("worker")

HANDLERS = {}


def handler(name):
    """Decorator: register fn as the handler for jobs called `name`."""
    def register(fn):
        HANDLERS[name] = fn
        return fn
    return register


@handler("echo")
def echo(payload):
    return payload


@handler("sleep")
def sleep_job(payload):
    time.sleep(payload["seconds"])


def run_one(queue, now):
    """Pop one ready job and run it. Return True when a job ran, False when none was ready."""
    job = queue.pop(now)
    if job is None:
        return False
    fn = HANDLERS.get(job.name)
    if fn is None:
        log.error("no handler for %s", job.name)
        queue.dead.append(job)
        return True
    try:
        fn(job.payload)
    except Exception:
        log.exception("job %s failed", job.name)
        queue.fail(job, now)
    return True


def run_forever(queue, settings=None):
    """Run ready jobs one at a time; sleep poll_seconds when none is ready."""
    settings = settings or load_settings()
    while True:
        if not run_one(queue, time.time()):
            time.sleep(settings["poll_seconds"])
