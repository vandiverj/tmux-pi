# Chore exports-worker

## Task
What does worker.py export, and what is each name's job? Files: src/worker.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
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

## Reference facts (what a complete answer states)
F1: worker.py exports HANDLERS [src/worker.py:9]
F2: worker.py exports handler [src/worker.py:12]
F3: worker.py exports echo [src/worker.py:21]
F4: worker.py exports sleep_job [src/worker.py:26]
F5: worker.py exports run_one [src/worker.py:30]
F6: worker.py exports run_forever [src/worker.py:48]
F7: HANDLERS maps job names to the functions that run them [src/worker.py:15]
F8: handler(name) is a decorator that registers a function under a job name [src/worker.py:13]
F9: echo returns its payload unchanged [src/worker.py:22]
F10: sleep_job sleeps for the number of seconds in its payload [src/worker.py:27]
F11: run_one pops and runs one ready job [src/worker.py:31]
F12: run_forever loops and sleeps poll_seconds when idle [src/worker.py:53]

## Not in the files (a reply that says these is wrong)
- regex: `\b(shutdown|graceful)\b`

## Replies
### R1
```
- The module src/worker.py runs jobs from a JobQueue with a table of handlers [src/worker.py:1]
- The module src/worker.py imports the logging module [src/worker.py:2]
- The module src/worker.py imports the time module [src/worker.py:3]
- The module src/worker.py imports the load_settings function from the settings module [src/worker.py:5]
- The variable log is a logger named 'worker' created using logging.getLogger [src/worker.py:7]
- The variable HANDLERS is an empty dictionary [src/worker.py:9]
- The function register is a nested function inside handler that adds the function to the HANDLERS dictionary using the name as the key and returns the function [src/worker.py:14-16]
- The function echo is a handler for jobs named 'echo' that returns the payload [src/worker.py:20-22]
- The function sleep_job is a handler for jobs named 'sleep' that sleeps for a specified number of seconds from the payload [src/worker.py:25-27]
```
### R2
```
- log is a logger created with the name 'worker' [src/worker.py:7]
- HANDLERS is a dictionary that stores registered job handlers [src/worker.py:9]
- handler is a decorator that registers a function as the handler for a specific job name [src/worker.py:12, src/worker.py:14]
- handler stores the function in the HANDLERS dictionary using the job name as the key [src/worker.py:15]
- handler returns the original function [src/worker.py:16]
- echo is a handler for 'echo' jobs that returns the payload [src/worker.py:20, src/worker.py:21, src/worker.py:22]
- sleep_job is a handler for 'sleep' jobs that pauses execution for a specified number of seconds [src/worker.py:25, src/worker.py:26, src/worker.py:27]
- run_one pops one ready job from the queue [src/worker.py:30, src/worker.py:32]
- run_one returns False when no job is ready [src/worker.py:33, src/worker.py:34]
- run_one looks up the handler for the job name [src/worker.py:35]
- run_one logs an error and moves the job to the dead queue when no handler is found [src/worker.py:36, src/worker.py:37, src/worker.py:38]
- run_one returns True when a job ran [src/worker.py:39, src/worker.py:45]
- run_one calls the handler function with the job payload [src/worker.py:41]
- run_one logs the exception and marks the job as failed when the handler raises an error [src/worker.py:42, src/worker.py:43, src/worker.py:44]
- run_forever loads settings if none are provided [src/worker.py:48, src/worker.py:50]
- run_forever runs jobs one at a time in an infinite loop [src/worker.py:51, src/worker.py:52]
- run_forever sleeps for the configured poll_seconds when no job is ready [src/worker.py:52, src/worker.py:53]
```
### R3
```
- worker.py defines the constant log [src/worker.py:7]
- worker.py defines the constant HANDLERS [src/worker.py:9]
- worker.py defines the function handler [src/worker.py:12]
- worker.py defines the function echo [src/worker.py:21]
- worker.py defines the function sleep_job [src/worker.py:26]
- worker.py defines the function run_one [src/worker.py:30]
- worker.py defines the function run_forever [src/worker.py:48]
- log is a logger created with the name 'worker' [src/worker.py:7]
- HANDLERS is a dictionary that stores registered job handlers [src/worker.py:9]
- handler is a decorator that registers a function as the handler for a specific job name [src/worker.py:12, src/worker.py:14]
- handler returns the original function after registering it [src/worker.py:16]
- echo is a handler for jobs named 'echo' [src/worker.py:20, src/worker.py:21]
- echo returns the payload it receives [src/worker.py:22]
- sleep_job is a handler for jobs named 'sleep' [src/worker.py:25, src/worker.py:26]
- sleep_job pauses execution for a number of seconds specified in the payload [src/worker.py:27]
- run_one pops one ready job from a queue and runs it [src/worker.py:30, src/worker.py:32]
- run_one returns False when no job is ready [src/worker.py:34]
- run_one logs an error and moves the job to the dead queue when no handler is found [src/worker.py:36, src/worker.py:38]
- run_one calls the handler function with the job's payload [src/worker.py:41]
- run_one logs the exception and marks the job as failed when the handler raises an error [src/worker.py:42, src/worker.py:44]
- run_one returns True when a job runs successfully [src/worker.py:45]
- run_forever runs ready jobs one at a time in an infinite loop [src/worker.py:48, src/worker.py:51]
- run_forever loads settings if none are provided [src/worker.py:50]
- run_forever sleeps for a configured duration when no job is ready [src/worker.py:52]
```
### R4
```
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
- log is a logger created with the name 'worker' [src/worker.py:7]
- HANDLERS is a dictionary that stores registered job handlers [src/worker.py:9]
- handler is a decorator that registers a function as the handler for a specific job name [src/worker.py:12, src/worker.py:14, src/worker.py:15]
- handler takes a job name as its argument [src/worker.py:12]
- handler returns a register function [src/worker.py:17]
- register stores the provided function in the HANDLERS dictionary under the job name [src/worker.py:15]
- register returns the provided function [src/worker.py:16]
- echo is a handler for jobs named 'echo' [src/worker.py:20, src/worker.py:21]
- echo returns the payload it receives [src/worker.py:22]
- sleep_job is a handler for jobs named 'sleep' [src/worker.py:25, src/worker.py:26]
- sleep_job pauses execution for a number of seconds specified in the payload [src/worker.py:27]
- run_one pops one ready job from the queue [src/worker.py:30, src/worker.py:32]
- run_one returns False when no job is ready [src/worker.py:33, src/worker.py:34]
- run_one looks up the handler for the job's name [src/worker.py:35]
- run_one logs an error and moves the job to the dead queue when no handler is found [src/worker.py:36, src/worker.py:37, src/worker.py:38]
- run_one returns True when a job ran [src/worker.py:39, src/worker.py:45]
- run_one executes the handler function with the job's payload [src/worker.py:41]
- run_one logs the exception and marks the job as failed when the handler raises an error [src/worker.py:42, src/worker.py:43, src/worker.py:44]
- run_forever runs ready jobs one at a time in an infinite loop [src/worker.py:48, src/worker.py:51]
- run_forever loads settings if none are provided [src/worker.py:50]
- run_forever sleeps for a configured number of seconds when no job is ready [src/worker.py:52, src/worker.py:53]
```
### R5
```
- HANDLERS is an empty dictionary [src/worker.py:9]
- echo is a handler for jobs called `echo` [src/worker.py:20-22]
- echo returns its payload [src/worker.py:22]
- sleep_job is a handler for jobs called `sleep` [src/worker.py:25-27]
- sleep_job sleeps for the number of seconds specified in the job's payload [src/worker.py:27]
- run_one logs an error and appends the job to the queue's dead list when no handler is found for the job's name [src/worker.py:36-39]
- run_one logs the exception and calls queue.fail with the job and current time when a handler raises an exception [src/worker.py:42-44]
- run_forever loads settings using load_settings if settings is not provided [src/worker.py:50]
- run_forever calls run_one with the queue and current time, and sleeps for poll_seconds if run_one returns False [src/worker.py:52-53]
```
### R6
```
- worker.py imports logging [src/worker.py:2]
- worker.py imports time [src/worker.py:3]
- worker.py imports load_settings from settings [src/worker.py:5]
- worker.py creates a logger named 'worker' [src/worker.py:7]
- worker.py defines a HANDLERS dict [src/worker.py:9]
- handler is a decorator that registers a function as the handler for a given job name [src/worker.py:12, src/worker.py:14]
- handler stores the function in the HANDLERS dict under the job name [src/worker.py:15]
- handler returns the original function [src/worker.py:16]
- echo is a handler for 'echo' jobs [src/worker.py:20, src/worker.py:21]
- echo returns the payload [src/worker.py:22]
- sleep_job is a handler for 'sleep' jobs [src/worker.py:25, src/worker.py:26]
- sleep_job sleeps for the number of seconds specified in the payload [src/worker.py:27]
- run_one pops one ready job from the queue [src/worker.py:30, src/worker.py:32]
- run_one returns False when no job is ready [src/worker.py:33, src/worker.py:34]
- run_one logs an error and appends the job to the queue's dead list when no handler is found [src/worker.py:35, src/worker.py:37, src/worker.py:38]
- run_one calls the handler function with the job's payload [src/worker.py:41]
- run_one logs an exception and calls queue.fail when the handler raises an error [src/worker.py:42, src/worker.py:44]
- run_one returns True when a job ran [src/worker.py:45]
- run_forever loads settings or uses the provided settings [src/worker.py:48, src/worker.py:50]
- run_forever runs ready jobs one at a time in a loop [src/worker.py:51, src/worker.py:52]
- run_forever sleeps for poll_seconds when no job is ready [src/worker.py:53]
```
### R7
```
Here is what `src/worker.py` exports, along with each name's job:

| Name | Type | Job | Source |
|---|---|---|---|
| `HANDLERS` | Module-level dict | Stores the mapping of job names → handler functions. Registered handlers are added here by the `@handler` decorator. | `src/worker.py:9` |
| `handler(name)` | Function (decorator) | Registers a function as the handler for jobs of a given `name`. It adds the function to `HANDLERS[name]` and returns the original function. | `src/worker.py:11-15` |
| `echo(payload)` | Function (decorated) | A handler for `"echo"` jobs (registered via `@handler("echo")`). Simply returns the payload unchanged. | `src/worker.py:17-19` |
| `sleep_job(payload)` | Function (decorated) | A handler for `"sleep"` jobs (registered via `@handler("sleep")`). Sleeps for the number of seconds specified in `payload["seconds"]`. | `src/worker.py:21-23` |
| `run_one(queue, now)` | Function | Pops one ready job from the queue at the given timestamp (`now`). If no handler exists for the job name, logs an error and moves the job to `queue.dead`. If the handler raises, logs the exception and calls `queue.fail()`. Returns `True` if a job was processed, `False` if none was ready. | `src/worker.py:25-37` |
| `run_forever(queue, settings=None)` | Function | Continuously runs jobs one at a time in an infinite loop. When no job is ready, it sleeps for `settings["poll_seconds"]` before polling again. Loads settings via `load_settings()` if not provided. | `src/worker.py:39-44` |
| `log` | Module-level object | A `logging.Logger` instance named `"worker"`, used for logging error and failure messages. | `src/worker.py:7` |
| `load_settings` | Imported name | Imported from `settings` (line 5) — used by `run_forever()` to load configuration. | `src/worker.py:5` |

**Not in the files:** The module does **not** define or export a `JobQueue` class, `queue` class, or any `Job` class — these are assumed to be passed in as arguments (`queue` and `job`). The `settings` module (providing `load_settings`) is also not shown in this file.
```
### R8
```
- HANDLERS is an empty dictionary [src/worker.py:9]
- handler registers a function as the handler for jobs called name [src/worker.py:12, src/worker.py:15]
- echo is registered as the handler for jobs called echo [src/worker.py:20, src/worker.py:21, src/worker.py:22]
- sleep_job is registered as the handler for jobs called sleep [src/worker.py:25, src/worker.py:26, src/worker.py:27]
- run_one pops one ready job and runs it, returning True when a job ran and False when none was ready [src/worker.py:30, src/worker.py:32, src/worker.py:34]
- run_forever runs ready jobs one at a time and sleeps poll_seconds when none is ready [src/worker.py:48, src/worker.py:50, src/worker.py:52]
```
### R9
```
- worker.py imports the load_settings function from the settings module [src/worker.py:5]
- worker.py creates a logger named 'worker' [src/worker.py:7]
- worker.py defines an empty dictionary named HANDLERS [src/worker.py:9]
- handler is a decorator that registers a function as the handler for a specific job name [src/worker.py:12, src/worker.py:14]
- handler stores the function in the HANDLERS dictionary using the job name as the key [src/worker.py:15]
- handler returns the original function [src/worker.py:16]
- echo is a handler registered for the 'echo' job name [src/worker.py:20, src/worker.py:21]
- echo returns the payload it receives [src/worker.py:22]
- sleep_job is a handler registered for the 'sleep' job name [src/worker.py:25, src/worker.py:26]
- sleep_job pauses execution for a number of seconds specified in the payload [src/worker.py:27]
- run_one pops one ready job from the queue and runs it [src/worker.py:30, src/worker.py:32]
- run_one returns False when no job is ready [src/worker.py:33, src/worker.py:34]
- run_one returns True when a job ran [src/worker.py:39, src/worker.py:45]
- run_one logs an error and moves the job to the dead queue when no handler is found [src/worker.py:36, src/worker.py:38]
- run_one calls the handler function with the job's payload [src/worker.py:41]
- run_one logs the exception and marks the job as failed when the handler raises an error [src/worker.py:42, src/worker.py:44]
- run_forever runs ready jobs one at a time in an infinite loop [src/worker.py:48, src/worker.py:51]
- run_forever loads settings if none are provided [src/worker.py:50]
- run_forever sleeps for a specified poll duration when no job is ready [src/worker.py:52, src/worker.py:53]
```
