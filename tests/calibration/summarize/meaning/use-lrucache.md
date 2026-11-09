# Chore use-lrucache

## Task
Which of these files use LRUCache? Files: src/cache.py, src/worker.py, src/cli.py, src/jobq.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
### src/cache.py
```
   1	"""Small LRU cache with an optional time-to-live."""
   2	import time
   3	from collections import OrderedDict
   4	
   5	
   6	class LRUCache:
   7	    def __init__(self, capacity=128, ttl=None, clock=time.monotonic):
   8	        self.capacity = capacity
   9	        self.ttl = ttl
  10	        self._clock = clock
  11	        self._items = OrderedDict()
  12	
  13	    def get(self, key, default=None):
  14	        """Return the cached value and mark it recently used; expired or missing keys give default."""
  15	        entry = self._items.get(key)
  16	        if entry is None:
  17	            return default
  18	        value, stored_at = entry
  19	        if self.ttl is not None and self._clock() - stored_at > self.ttl:
  20	            del self._items[key]
  21	            return default
  22	        self._items.move_to_end(key)
  23	        return value
  24	
  25	    def put(self, key, value):
  26	        """Store a value; evict the least recently used entry when over capacity."""
  27	        self._items[key] = (value, self._clock())
  28	        self._items.move_to_end(key)
  29	        if len(self._items) > self.capacity:
  30	            self._items.popitem(last=False)
  31	
  32	    def clear(self):
  33	        self._items.clear()
  34	
  35	    def __len__(self):
  36	        return len(self._items)
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

## Reference facts (what a complete answer states)
F1: None of the other files use LRUCache; their imports do not include it [src/cli.py:6-8]
F2: cache.py defines LRUCache [src/cache.py:6]

## Not in the files (a reply that says these is wrong)
- regex: `\b(worker|cli|jobq)\.py[^\n]{0,60}\b(uses|imports|creates|calls)\b[^\n]{0,30}LRUCache`

## Replies
### R1
```
- src/cache.py defines a class named LRUCache [src/cache.py:6]
- src/cache.py defines a method named __init__ that initializes the cache with a capacity, an optional time-to-live (ttl), and a clock function [src/cache.py:7-11]
- src/cache.py defines a method named clear that clears all items in the cache [src/cache.py:32-33]
- src/cache.py defines a method named __len__ that returns the number of items in the cache [src/cache.py:35-36]
```
### R2
```
**`src/cache.py`** — defines `LRUCache` at `src/cache.py:6`.

None of the other files (`src/worker.py`, `src/cli.py`, `src/jobq.py`) reference or use `LRUCache` — **not in the files** for those.
```
### R3
```
- LRUCache is not in the files [src/cache.py:1]
- LRUCache is not in the files [src/worker.py:1]
- LRUCache is not in the files [src/cli.py:1]
- LRUCache is not in the files [src/jobq.py:1]
```
### R4
```
(empty reply)
```
### R5
```
- src/cache.py defines the LRUCache class [src/cache.py:6]
- src/worker.py does not use LRUCache [src/worker.py:1]
- src/cli.py does not use LRUCache [src/cli.py:1]
- src/jobq.py does not use LRUCache [src/jobq.py:1]
```
### R6
```
- src/cache.py defines the LRUCache class [src/cache.py:6]
- src/worker.py does not use LRUCache [src/worker.py:1]
- src/cli.py does not use LRUCache [src/cli.py:1]
- src/jobq.py does not use LRUCache [src/jobq.py:1]
```
### R7
```
- cache.py defines the class LRUCache [src/cache.py:6]
- cache.py defines the parameter capacity of __init__ [src/cache.py:7]
- cache.py defines the parameter ttl of __init__ [src/cache.py:7]
- cache.py defines the parameter clock of __init__ [src/cache.py:7]
- cache.py defines the method get [src/cache.py:13]
- cache.py defines the parameter key of get [src/cache.py:13]
- cache.py defines the parameter default of get [src/cache.py:13]
- cache.py defines the method put [src/cache.py:25]
- cache.py defines the parameter key of put [src/cache.py:25]
- cache.py defines the parameter value of put [src/cache.py:25]
- cache.py defines the method clear [src/cache.py:32]
- cache.py defines the method __len__ [src/cache.py:35]
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
```
### R8
```
- LRUCache is not in src/worker.py [src/worker.py:1]
- LRUCache is not in src/cli.py [src/cli.py:1]
- LRUCache is not in src/jobq.py [src/jobq.py:1]
```
### R9
```
- cache.py defines the class LRUCache [src/cache.py:6]
- cache.py defines the method get [src/cache.py:13]
- cache.py defines the method put [src/cache.py:25]
- cache.py defines the method clear [src/cache.py:32]
- cache.py defines the method __len__ [src/cache.py:35]
- worker.py defines the constant log [src/worker.py:7]
- worker.py defines the constant HANDLERS [src/worker.py:9]
- worker.py defines the function handler [src/worker.py:12]
- worker.py defines the function echo [src/worker.py:21]
- worker.py defines the function sleep_job [src/worker.py:26]
- worker.py defines the function run_one [src/worker.py:30]
- worker.py defines the function run_forever [src/worker.py:48]
- cli.py defines the function build_parser [src/cli.py:11]
- cli.py defines the function main [src/cli.py:23]
- jobq.py defines the constant MAX_RETRIES [src/jobq.py:5]
- jobq.py defines the constant BACKOFF_SECONDS [src/jobq.py:6]
- jobq.py defines the function backoff_for [src/jobq.py:9]
- jobq.py defines the class Job [src/jobq.py:15]
- jobq.py defines the class JobQueue [src/jobq.py:23]
- jobq.py defines the method push [src/jobq.py:29]
- jobq.py defines the method pop [src/jobq.py:33]
- jobq.py defines the method fail [src/jobq.py:39]
- jobq.py defines the method __len__ [src/jobq.py:48]
```
