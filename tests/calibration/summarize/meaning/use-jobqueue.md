# Chore use-jobqueue

## Task
Which of these files use JobQueue, and what do they use it for? Files: src/jobq.py, src/worker.py, src/cli.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
### src/jobq.py
```
   1	"""In-memory job queue with priorities, retry backoff and a dead-letter list."""
   2	import heapq
   3	import itertools
   4	
   5	MAX_RETRIES = 3
   6	BACKOFF_SECONDS = (1, 5, 30)
   7	
   8	
   9	def backoff_for(attempt):
  10	    """Seconds to wait before retry number `attempt` (1-based); the last backoff repeats."""
  11	    index = min(attempt, len(BACKOFF_SECONDS)) - 1
  12	    return BACKOFF_SECONDS[index]
  13	
  14	
  15	class Job:
  16	    def __init__(self, name, payload, priority=0):
  17	        self.name = name
  18	        self.payload = payload
  19	        self.priority = priority
  20	        self.attempts = 0
  21	
  22	
  23	class JobQueue:
  24	    def __init__(self):
  25	        self._heap = []
  26	        self._counter = itertools.count()
  27	        self.dead = []
  28	
  29	    def push(self, job, ready_at=0.0):
  30	        """Add a job. Jobs pop by ready_at, then priority (lower first), then push order."""
  31	        heapq.heappush(self._heap, (ready_at, job.priority, next(self._counter), job))
  32	
  33	    def pop(self, now):
  34	        """Return the next job whose ready_at has passed, or None."""
  35	        if self._heap and self._heap[0][0] <= now:
  36	            return heapq.heappop(self._heap)[3]
  37	        return None
  38	
  39	    def fail(self, job, now):
  40	        """Count a failed attempt; requeue after the backoff, or move to the dead list after MAX_RETRIES."""
  41	        job.attempts += 1
  42	        if job.attempts > MAX_RETRIES:
  43	            self.dead.append(job)
  44	            return False
  45	        self.push(job, ready_at=now + backoff_for(job.attempts))
  46	        return True
  47	
  48	    def __len__(self):
  49	        return len(self._heap)
```
### src/worker.py
```
   1	"""Runs jobs from a JobQueue with a table of handlers."""
   2	import logging
   3	import time
   4	
   5	from settings import load_settings
   6	
   7	log = logging.getLogger("worker")
   8	
   9	HANDLERS = {}
  10	
  11	
  12	def handler(name):
  13	    """Decorator: register fn as the handler for jobs called `name`."""
  14	    def register(fn):
  15	        HANDLERS[name] = fn
  16	        return fn
  17	    return register
  18	
  19	
  20	@handler("echo")
  21	def echo(payload):
  22	    return payload
  23	
  24	
  25	@handler("sleep")
  26	def sleep_job(payload):
  27	    time.sleep(payload["seconds"])
  28	
  29	
  30	def run_one(queue, now):
  31	    """Pop one ready job and run it. Return True when a job ran, False when none was ready."""
  32	    job = queue.pop(now)
  33	    if job is None:
  34	        return False
  35	    fn = HANDLERS.get(job.name)
  36	    if fn is None:
  37	        log.error("no handler for %s", job.name)
  38	        queue.dead.append(job)
  39	        return True
  40	    try:
  41	        fn(job.payload)
  42	    except Exception:
  43	        log.exception("job %s failed", job.name)
  44	        queue.fail(job, now)
  45	    return True
  46	
  47	
  48	def run_forever(queue, settings=None):
  49	    """Run ready jobs one at a time; sleep poll_seconds when none is ready."""
  50	    settings = settings or load_settings()
  51	    while True:
  52	        if not run_one(queue, time.time()):
  53	            time.sleep(settings["poll_seconds"])
```
### src/cli.py
```
   1	"""Command line: `jobq run` starts a worker, `jobq enqueue NAME JSON` queues one job in this process only."""
   2	import argparse
   3	import json
   4	import sys
   5	
   6	from jobq import Job, JobQueue
   7	from settings import load_settings
   8	from worker import HANDLERS, run_forever
   9	
  10	
  11	def build_parser():
  12	    parser = argparse.ArgumentParser(prog="jobq")
  13	    parser.add_argument("--config", help="path to a settings JSON file")
  14	    sub = parser.add_subparsers(dest="command", required=True)
  15	    sub.add_parser("run", help="run jobs until interrupted")
  16	    enqueue = sub.add_parser("enqueue", help="add one job")
  17	    enqueue.add_argument("name")
  18	    enqueue.add_argument("payload", help="JSON object")
  19	    sub.add_parser("handlers", help="list registered handler names")
  20	    return parser
  21	
  22	
  23	def main(argv=None):
  24	    args = build_parser().parse_args(argv)
  25	    settings = load_settings(args.config)
  26	    if args.command == "handlers":
  27	        print("\n".join(sorted(HANDLERS)))
  28	        return 0
  29	    queue = JobQueue()
  30	    if args.command == "enqueue":
  31	        queue.push(Job(args.name, json.loads(args.payload)))
  32	        print("queued", args.name)
  33	        return 0
  34	    run_forever(queue, settings)
  35	    return 0
  36	
  37	
  38	if __name__ == "__main__":
  39	    sys.exit(main())
```

## Reference facts (what a complete answer states)
F1: cli.py creates a JobQueue [src/cli.py:29]
F2: cli.py then pushes a new Job onto that queue for the enqueue command [src/cli.py:31]
F3: jobq.py defines the JobQueue class [src/jobq.py:23]
F4: worker.py receives the queue as an argument from its caller [src/worker.py:30]
F5: worker.py pops the next ready job from the queue in run_one [src/worker.py:32]
F6: worker.py fails the job on the queue so it is retried when a handler raises [src/worker.py:44]

## Not in the files (a reply that says these is wrong)
- regex: `worker\.py[^\n]{0,60}\b(creates?|instantiates?|imports?)\b[^\n]{0,30}JobQueue`

## Replies
### R1
```
- src/jobq.py defines the JobQueue class [src/jobq.py:23]
- src/worker.py uses JobQueue by calling queue.pop [src/worker.py:32]
- src/worker.py uses JobQueue by calling queue.fail [src/worker.py:44]
- src/worker.py uses JobQueue by appending to queue.dead [src/worker.py:38]
- src/cli.py uses JobQueue by creating a JobQueue instance [src/cli.py:29]
- src/cli.py uses JobQueue by calling queue.push [src/cli.py:31]
```
### R2
```
(empty reply)
```
### R3
```
- src/jobq.py defines the JobQueue class [src/jobq.py:23]
- src/worker.py uses JobQueue by passing it to run_one [src/worker.py:30, src/worker.py:32]
- src/worker.py uses JobQueue by passing it to run_forever [src/worker.py:48, src/worker.py:52]
- src/cli.py uses JobQueue by creating an instance [src/cli.py:6, src/cli.py:29]
```
### R4
```
- jobq.py defines the constant MAX_RETRIES [src/jobq.py:5]
- jobq.py defines the constant BACKOFF_SECONDS [src/jobq.py:6]
- jobq.py defines the function backoff_for [src/jobq.py:9]
- jobq.py defines the parameter attempt of backoff_for [src/jobq.py:9]
- jobq.py defines the class Job [src/jobq.py:15]
- jobq.py defines the parameter name of __init__ [src/jobq.py:16]
- jobq.py defines the parameter payload of __init__ [src/jobq.py:16]
- jobq.py defines the parameter priority of __init__ [src/jobq.py:16]
- jobq.py defines the class JobQueue [src/jobq.py:23]
- jobq.py defines the method push [src/jobq.py:29]
- jobq.py defines the parameter job of push [src/jobq.py:29]
- jobq.py defines the parameter ready_at of push [src/jobq.py:29]
- jobq.py defines the method pop [src/jobq.py:33]
- jobq.py defines the parameter now of pop [src/jobq.py:33]
- jobq.py defines the method fail [src/jobq.py:39]
- jobq.py defines the parameter job of fail [src/jobq.py:39]
- jobq.py defines the parameter now of fail [src/jobq.py:39]
- jobq.py defines the method __len__ [src/jobq.py:48]
- worker.py defines the constant log [src/worker.py:7]
- worker.py defines the constant HANDLERS [src/worker.py:9]
- worker.py defines the function handler [src/worker.py:12]
- worker.py defines the parameter name of handler [src/worker.py:12]
- worker.py defines the function echo [src/worker.py:21]
- worker.py defines the parameter payload of echo [src/worker.py:21]
- worker.py defines the function sleep_job [src/worker.py:26]
- worker.py defines the parameter payload of sleep_job [src/worker.py:26]
- worker.py defines the function run_one [src/worker.py:30]
- worker.py defines the parameter queue of run_one [src/worker.py:30]
- worker.py defines the parameter now of run_one [src/worker.py:30]
- worker.py defines the function run_forever [src/worker.py:48]
- worker.py defines the parameter queue of run_forever [src/worker.py:48]
- worker.py defines the parameter settings of run_forever [src/worker.py:48]
- cli.py defines the function build_parser [src/cli.py:11]
- cli.py defines the function main [src/cli.py:23]
- cli.py defines the parameter argv of main [src/cli.py:23]
- src/worker.py uses JobQueue as the queue parameter of run_one [src/worker.py:30, src/worker.py:32]
- src/worker.py uses JobQueue as the queue parameter of run_forever [src/worker.py:48, src/worker.py:52]
- src/cli.py uses JobQueue to create a queue instance [src/cli.py:6, src/cli.py:29]
```
### R5
```
Here is a summary of JobQueue usage across the three files:

---

### `src/jobq.py` — Definition and core methods

| Line(s) | Usage |
|---|---|
| `src/jobq.py:22` | `JobQueue.__init__` creates the internal heap (`self._heap`) and dead-letter list (`self.dead`). |
| `src/jobq.py:25-27` | `push()` — adds a `Job` onto the priority queue (heapq). |
| `src/jobq.py:30-33` | `pop()` — returns the next ready job, or `None`. |
| `src/jobq.py:35-41` | `fail()` — counts a failure, re-queues with backoff, or moves to the dead list after `MAX_RETRIES`. |

**Purpose:** This file *defines* the `JobQueue` class — it is the source implementation (in-memory, priority-based, with retry/dead-letter support).

---

### `src/worker.py` — Consumes `JobQueue`

| Line(s) | Usage |
|---|---|
| `src/worker.py:19` | `run_one(queue, now)` calls `queue.pop(now)` to fetch the next job. |
| `src/worker.py:24` | When there is no handler, appends the job to `queue.dead`. |
| `src/worker.py:28` | On exception, calls `queue.fail(job, now)` to re-queue with backoff. |
| `src/worker.py:33` | `run_forever(queue, settings)` loops calling `run_one`. |

**Purpose:** The worker *consumes* jobs from a `JobQueue` instance, dispatching them to registered handlers and handling failures via `fail()`.

---

### `src/cli.py` — Creates and pushes to `JobQueue`

| Line(s) | Usage |
|---|---|
| `src/cli.py:6` | Imports `JobQueue` from `jobq`. |
| `src/cli.py:25` | `queue = JobQueue()` — creates a new queue. |
| `src/cli.py:27` | `queue.push(Job(args.name, json.loads(args.payload)))` — enqueues a job from the CLI `enqueue` sub-command. |
| `src/cli.py:29` | Passes the queue to `run_forever(queue, settings)` for the `run` sub-command. |

**Purpose:** The CLI *instantiates* a `JobQueue` and either pushes a single job (`enqueue`) or hands the queue to the worker loop (`run`).

---

### Summary

| File | Uses `JobQueue`? | For what? |
|---|---|---|
| `src/jobq.py` | **Yes** | Defines the class and its methods (`push`, `pop`, `fail`). |
| `src/worker.py` | **Yes** | Pops jobs, handles failures via `queue.fail()`, dead-lists unhandled jobs. |
| `src/cli.py` | **Yes** | Creates a `JobQueue`, pushes jobs via `enqueue`, passes it to `run_forever`. |
```
### R6
```
- src/jobq.py defines the JobQueue class [src/jobq.py:23]
- src/worker.py uses JobQueue as the type for the `queue` parameter in `run_one` [src/worker.py:30]
- src/worker.py uses JobQueue as the type for the `queue` parameter in `run_forever` [src/worker.py:48]
- src/cli.py imports JobQueue from jobq [src/cli.py:6]
- src/cli.py creates a JobQueue instance in `main` [src/cli.py:29]
```
### R7
```
- src/jobq.py uses JobQueue to provide an in-memory job queue with priorities, retry backoff and a dead-letter list [src/jobq.py:1]
- src/jobq.py uses JobQueue to store jobs in a heap ordered by ready_at, priority, and push order [src/jobq.py:31]
- src/jobq.py uses JobQueue to return the next job whose ready_at has passed, or None [src/jobq.py:34]
- src/jobq.py uses JobQueue to count a failed attempt, requeue after the backoff, or move to the dead list after MAX_RETRIES [src/jobq.py:40]
- src/worker.py uses JobQueue to pop one ready job and run it, returning True when a job ran, False when none was ready [src/worker.py:31]
- src/worker.py uses JobQueue to append a job to the dead list when there is no handler for the job [src/worker.py:38]
- src/worker.py uses JobQueue to fail a job and handle the backoff when a job fails [src/worker.py:44]
- src/worker.py uses JobQueue to run ready jobs one at a time, sleeping poll_seconds when none is ready [src/worker.py:49]
- src/cli.py uses JobQueue to create a queue for jobs [src/cli.py:29]
- src/cli.py uses JobQueue to push a job to the queue when the enqueue command is used [src/cli.py:31]
- src/cli.py uses JobQueue to run jobs forever using the run_forever function [src/cli.py:34]
```
### R8
```
- jobq.py defines the constant MAX_RETRIES [src/jobq.py:5]
- jobq.py defines the constant BACKOFF_SECONDS [src/jobq.py:6]
- jobq.py defines the function backoff_for [src/jobq.py:9]
- jobq.py defines the class Job [src/jobq.py:15]
- jobq.py defines the class JobQueue [src/jobq.py:23]
- jobq.py defines the method push [src/jobq.py:29]
- jobq.py defines the method pop [src/jobq.py:33]
- jobq.py defines the method fail [src/jobq.py:39]
- jobq.py defines the method __len__ [src/jobq.py:48]
- worker.py defines the constant log [src/worker.py:7]
- worker.py defines the constant HANDLERS [src/worker.py:9]
- worker.py defines the function handler [src/worker.py:12]
- worker.py defines the function echo [src/worker.py:21]
- worker.py defines the function sleep_job [src/worker.py:26]
- worker.py defines the function run_one [src/worker.py:30]
- worker.py defines the function run_forever [src/worker.py:48]
- cli.py defines the function build_parser [src/cli.py:11]
- cli.py defines the function main [src/cli.py:23]
- src/jobq.py defines the JobQueue class [src/jobq.py:23]
- src/worker.py uses JobQueue as the type of the `queue` argument in `run_one` [src/worker.py:30]
- src/worker.py uses JobQueue as the type of the `queue` argument in `run_forever` [src/worker.py:48]
- src/cli.py imports JobQueue from jobq [src/cli.py:6]
- src/cli.py creates a JobQueue instance in `main` [src/cli.py:29]
```
### R9
```
(empty reply)
```
