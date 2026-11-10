# Chore exports-jobq

## Task
What does jobq.py export, and what is each export's job? Files: src/jobq.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

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

## Reference facts (what a complete answer states)
F1: jobq.py exports the Job class [src/jobq.py:15]
F2: jobq.py exports the JobQueue class [src/jobq.py:23]
F3: jobq.py exports MAX_RETRIES [src/jobq.py:5]
F4: jobq.py exports BACKOFF_SECONDS [src/jobq.py:6]
F5: jobq.py exports backoff_for [src/jobq.py:9]
F6: Job holds a name, a payload, a priority and an attempts counter [src/jobq.py:16]
F7: JobQueue holds the pending jobs in a heap, hands out the next ready one, and moves a job to a dead list after too many failures [src/jobq.py:27]
F8: MAX_RETRIES is the limit of 3 retries [src/jobq.py:42]
F9: BACKOFF_SECONDS is the table of 1, 5 and 30 that each retry reads from [src/jobq.py:12]
F10: backoff_for(attempt) returns how many seconds to wait for a retry [src/jobq.py:10]
F11: backoff_for repeats the last backoff past the table [src/jobq.py:10]

## Not in the files (a reply that says these is wrong)
- regex: `\bJobQueue\.(peek|cancel|remove|clear|size)\b`
- regex: `\b(peek|cancel)\(`

## Replies
### R1
```
- MAX_RETRIES is 3 [src/jobq.py:5]
- BACKOFF_SECONDS is (1, 5, 30) [src/jobq.py:6]
- backoff_for returns the seconds to wait before retry number attempt (1-based), using the last backoff if the attempt exceeds the length of BACKOFF_SECONDS [src/jobq.py:9-12]
- JobQueue has _heap, _counter, and dead attributes [src/jobq.py:23-27]
- push adds a job to the queue, ordering by ready_at, then priority (lower first), then push order [src/jobq.py:29-31]
- pop returns the next job whose ready_at has passed, or None [src/jobq.py:33-37]
- __len__ returns the length of the heap [src/jobq.py:48-49]
```
### R2
```
- jobq.py exports MAX_RETRIES, BACKOFF_SECONDS, backoff_for, Job, and JobQueue [src/jobq.py:1, src/jobq.py:5, src/jobq.py:6]
- MAX_RETRIES is 3 [src/jobq.py:5]
- BACKOFF_SECONDS is (1, 5, 30) [src/jobq.py:6]
- backoff_for returns the seconds to wait before retry number attempt (1-based), using the last backoff if the attempt exceeds the list length [src/jobq.py:9, src/jobq.py:11, src/jobq.py:12]
- Job has name, payload, priority, and attempts attributes [src/jobq.py:16, src/jobq.py:17, src/jobq.py:18]
- JobQueue has _heap, _counter, and dead attributes [src/jobq.py:24, src/jobq.py:25, src/jobq.py:26]
- push adds a job to the queue, ordering by ready_at, then priority (lower first), then push order [src/jobq.py:29, src/jobq.py:31]
- pop returns the next job whose ready_at has passed, or None [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- fail counts a failed attempt, requeues the job after the backoff, or moves it to the dead list after MAX_RETRIES [src/jobq.py:39, src/jobq.py:41, src/jobq.py:42]
- __len__ returns the length of the internal heap [src/jobq.py:48, src/jobq.py:49]
```
### R3
```
- MAX_RETRIES sets the maximum number of retries to 3 [src/jobq.py:5]
- BACKOFF_SECONDS sets the backoff delays to 1, 5, and 30 seconds [src/jobq.py:6]
- backoff_for returns the backoff delay for a given attempt number [src/jobq.py:9, src/jobq.py:11, src/jobq.py:12]
- Job is a class that stores a job's name, payload, priority, and attempt count [src/jobq.py:15, src/jobq.py:16, src/jobq.py:17]
- __init__ initializes the job's name, payload, priority, and attempts [src/jobq.py:16, src/jobq.py:17, src/jobq.py:18]
- JobQueue is a class that manages an in-memory job queue with priorities, retry backoff, and a dead-letter list [src/jobq.py:23, src/jobq.py:24, src/jobq.py:25]
- __init__ initializes the queue's heap, counter, and dead list [src/jobq.py:24, src/jobq.py:25, src/jobq.py:26]
- push adds a job to the queue, ordering by ready_at, priority, and push order [src/jobq.py:29, src/jobq.py:31]
- pop returns the next job whose ready_at has passed, or None [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- fail increments the job's attempt count, requeues it after a backoff, or moves it to the dead list if retries are exceeded [src/jobq.py:39, src/jobq.py:41, src/jobq.py:42]
- __len__ returns the number of jobs currently in the heap [src/jobq.py:48, src/jobq.py:49]
```
### R4
```
- jobq.py exports the constant MAX_RETRIES, which is set to 3 [src/jobq.py:5]
- jobq.py exports the constant BACKOFF_SECONDS, which is a tuple containing (1, 5, 30) [src/jobq.py:6]
- jobq.py exports the function backoff_for, which calculates the seconds to wait before retry number `attempt` (1-based), repeating the last backoff [src/jobq.py:9-12]
- JobQueue.__init__ initializes the queue with an empty heap, a counter for push order, and an empty dead list [src/jobq.py:24-27]
- JobQueue.push adds a job to the queue, ordering by ready_at, then priority (lower first), then push order [src/jobq.py:29-31]
- JobQueue.pop returns the next job whose ready_at has passed, or None [src/jobq.py:33-37]
- JobQueue.__len__ returns the length of the heap [src/jobq.py:48-49]
```
### R5
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
- MAX_RETRIES sets the maximum number of retries to 3 [src/jobq.py:5]
- BACKOFF_SECONDS sets the backoff delays to 1, 5, and 30 seconds [src/jobq.py:6]
- backoff_for returns the backoff delay for a given attempt number [src/jobq.py:9, src/jobq.py:11, src/jobq.py:12]
- Job is a class that stores a job's name, payload, priority, and attempt count [src/jobq.py:15, src/jobq.py:16, src/jobq.py:17]
- JobQueue is a class that manages an in-memory priority queue of jobs [src/jobq.py:23, src/jobq.py:24, src/jobq.py:25]
- push adds a job to the priority queue with a ready time and priority [src/jobq.py:29, src/jobq.py:31]
- pop returns the next ready job or None if no jobs are ready [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- fail increments the job's attempt count and either requeues it with a backoff or moves it to the dead list [src/jobq.py:39, src/jobq.py:41, src/jobq.py:42]
- __len__ returns the number of jobs currently in the queue [src/jobq.py:48, src/jobq.py:49]
```
### R6
```
Here is every export from `src/jobq.py` and what each does:

### Module-level constants
| Export | Line | Job |
|---|---|---|
| `MAX_RETRIES` | [src/jobq.py:5](src/jobq.py:5) | Defines the maximum number of retry attempts (3) before a job is dead-lettered. |
| `BACKOFF_SECONDS` | [src/jobq.py:6](src/jobq.py:6) | Tuple of backoff intervals (1, 5, 30 seconds) used for retry delays. |

### Functions
| Export | Line | Job |
|---|---|---|
| `backoff_for(attempt)` | [src/jobq.py:9](src/jobq.py:9) | Returns the number of seconds to wait before retry number `attempt` (1-based). It indexes into `BACKOFF_SECONDS`, repeating the last value for attempts beyond the tuple length. |

### Classes
| Export | Line | Job |
|---|---|---|
| `Job` | [src/jobq.py:14](src/jobq.py:14) | Represents a single work item with a `name`, `payload`, `priority`, and `attempts` counter. |
| `JobQueue` | [src/jobq.py:20](src/jobq.py:20) | An in-memory priority queue backed by a min-heap. Supports `push` (insert with a `ready_at` time), `pop` (retrieve the next ready job, ordered by `ready_at` then priority then FIFO), and `fail` (increment attempts, requeue with backoff, or move to `dead` list after `MAX_RETRIES`). Also exposes `__len__` for the heap size. |
```
### R7
```
- jobq.py exports the constant MAX_RETRIES [src/jobq.py:5]
- jobq.py exports the constant BACKOFF_SECONDS [src/jobq.py:6]
- jobq.py exports the function backoff_for [src/jobq.py:9]
- jobq.py exports the class Job [src/jobq.py:15]
- jobq.py exports the class JobQueue [src/jobq.py:23]
- backoff_for returns the backoff seconds for a given attempt number [src/jobq.py:9, src/jobq.py:11, src/jobq.py:12]
- Job stores a name, payload, priority, and attempts count [src/jobq.py:16, src/jobq.py:17, src/jobq.py:18]
- JobQueue maintains a heap, a counter, and a dead list [src/jobq.py:24, src/jobq.py:25, src/jobq.py:26]
- JobQueue pushes a job into the heap with a ready time, priority, and counter [src/jobq.py:29, src/jobq.py:31]
- JobQueue pops the next job if its ready time has passed, otherwise returns None [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- JobQueue increments the job's attempts and requeues it after a backoff or moves it to the dead list [src/jobq.py:39, src/jobq.py:41, src/jobq.py:42]
- JobQueue returns the length of the heap [src/jobq.py:48, src/jobq.py:49]
```
### R8
```
- jobq.py exports the constant MAX_RETRIES set to 3 [src/jobq.py:5]
- jobq.py exports the constant BACKOFF_SECONDS set to (1, 5, 30) [src/jobq.py:6]
- jobq.py exports the function backoff_for [src/jobq.py:9]
- backoff_for returns the backoff seconds for a given attempt number [src/jobq.py:11, src/jobq.py:12]
- jobq.py exports the class Job [src/jobq.py:15]
- Job stores a name, payload, priority, and attempts count [src/jobq.py:16, src/jobq.py:17, src/jobq.py:18]
- jobq.py exports the class JobQueue [src/jobq.py:23]
- JobQueue maintains an internal heap, a counter, and a dead list [src/jobq.py:24, src/jobq.py:25, src/jobq.py:26]
- JobQueue provides a push method to add jobs to the queue [src/jobq.py:29]
- JobQueue provides a pop method to retrieve the next ready job [src/jobq.py:33]
- JobQueue provides a fail method to handle job failures and retries [src/jobq.py:39]
- JobQueue provides a __len__ method to return the number of jobs in the heap [src/jobq.py:48]
```
### R9
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
- MAX_RETRIES sets the maximum number of retries to 3 [src/jobq.py:5]
- BACKOFF_SECONDS sets the backoff delays to 1, 5, and 30 seconds [src/jobq.py:6]
- backoff_for returns the backoff delay for a given attempt number [src/jobq.py:9, src/jobq.py:11, src/jobq.py:12]
- Job is a class that stores a job's name, payload, priority, and attempt count [src/jobq.py:15, src/jobq.py:16, src/jobq.py:17]
- __init__ initializes a job with a name, payload, priority, and zero attempts [src/jobq.py:16, src/jobq.py:17, src/jobq.py:18]
- JobQueue is a class that manages an in-memory priority queue of jobs [src/jobq.py:23, src/jobq.py:24, src/jobq.py:25]
- __init__ initializes the queue with an empty heap, a counter, and an empty dead list [src/jobq.py:24, src/jobq.py:25, src/jobq.py:26]
- push adds a job to the priority queue based on ready time, priority, and insertion order [src/jobq.py:29, src/jobq.py:31]
- pop returns the next job whose ready time has passed, or None [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- fail increments the job's attempt count and either requeues it with a backoff or moves it to the dead list [src/jobq.py:39, src/jobq.py:41, src/jobq.py:42]
- fail returns False if the job exceeds the maximum retries, otherwise True [src/jobq.py:42, src/jobq.py:44]
- __len__ returns the number of jobs currently in the heap [src/jobq.py:48, src/jobq.py:49]
```
