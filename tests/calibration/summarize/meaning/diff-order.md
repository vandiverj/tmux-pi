# Chore diff-order

## Task
cache.py and jobq.py both keep items in an ordered structure. How do their ordering and removal rules differ? Files: src/cache.py, src/jobq.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

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
F1: LRUCache keeps entries in recency order, the most recently used last [src/cache.py:11]
F2: put evicts the entry at the front once the capacity is exceeded [src/cache.py:30]
F3: get also drops expired entries when a ttl is set [src/cache.py:19]
F4: JobQueue orders jobs by ready_at first [src/jobq.py:31]
F5: JobQueue breaks ties by the lower priority number [src/jobq.py:30]
F6: JobQueue releases otherwise equal jobs in the sequence they were pushed [src/jobq.py:30]
F7: pop removes the next job once it is due [src/jobq.py:34]
F8: JobQueue never evicts: a job that keeps failing goes to the dead list [src/jobq.py:43]

## Not in the files (a reply that says these is wrong)
- regex: `JobQueue[^\n]{0,40}\b(evicts?|drops?) (the )?(oldest|least)`

## Replies
### R1
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
- LRUCache keeps items in an OrderedDict [src/cache.py:6, src/cache.py:11]
- LRUCache orders items by last access time [src/cache.py:22]
- LRUCache removes the least recently used item when capacity is exceeded [src/cache.py:29, src/cache.py:30]
- LRUCache removes expired items on access [src/cache.py:19, src/cache.py:20]
- JobQueue keeps items in a heap [src/jobq.py:23, src/jobq.py:25]
- JobQueue orders items by ready_at time, then priority, then insertion order [src/jobq.py:30, src/jobq.py:31]
- JobQueue removes the next ready job by priority [src/jobq.py:33, src/jobq.py:36]
- JobQueue moves failed jobs to a dead list after exceeding retries [src/jobq.py:39, src/jobq.py:43]
```
### R2
```
- LRUCache keeps items in an OrderedDict [src/cache.py:6, src/cache.py:11]
- LRUCache orders items by last access time [src/cache.py:22]
- LRUCache removes the least recently used item when capacity is exceeded [src/cache.py:29, src/cache.py:30]
- LRUCache removes items that have exceeded their time-to-live [src/cache.py:19, src/cache.py:20]
- JobQueue keeps items in a heap [src/jobq.py:23, src/jobq.py:25]
- JobQueue orders items by ready_at time, then priority, then insertion order [src/jobq.py:30, src/jobq.py:31]
- JobQueue removes the next ready job from the heap [src/jobq.py:35, src/jobq.py:36]
- JobQueue moves failed jobs to a dead list after exceeding MAX_RETRIES [src/jobq.py:42, src/jobq.py:43]
```
### R3
```
- cache.py defines the class LRUCache [src/cache.py:6]
- cache.py defines the method get [src/cache.py:13]
- cache.py defines the method put [src/cache.py:25]
- cache.py defines the method clear [src/cache.py:32]
- cache.py defines the method __len__ [src/cache.py:35]
- jobq.py defines the constant MAX_RETRIES [src/jobq.py:5]
- jobq.py defines the constant BACKOFF_SECONDS [src/jobq.py:6]
- jobq.py defines the function backoff_for [src/jobq.py:9]
- jobq.py defines the class Job [src/jobq.py:15]
- jobq.py defines the class JobQueue [src/jobq.py:23]
- jobq.py defines the method push [src/jobq.py:29]
- jobq.py defines the method pop [src/jobq.py:33]
- jobq.py defines the method fail [src/jobq.py:39]
- jobq.py defines the method __len__ [src/jobq.py:48]
- LRUCache keeps items in an OrderedDict [src/cache.py:6, src/cache.py:11]
- LRUCache evicts the least recently used entry when over capacity [src/cache.py:25, src/cache.py:30]
- LRUCache marks accessed items as recently used [src/cache.py:13, src/cache.py:22]
- LRUCache removes expired entries on access [src/cache.py:13, src/cache.py:19]
- JobQueue keeps items in a heap [src/jobq.py:23, src/jobq.py:25]
- JobQueue pops jobs by ready_at, then priority, then push order [src/jobq.py:29, src/jobq.py:31]
- JobQueue moves failed jobs to a dead list after MAX_RETRIES [src/jobq.py:39, src/jobq.py:43]
- LRUCache is not in src/jobq.py [src/jobq.py:1]
- JobQueue is not in src/cache.py [src/cache.py:1]
```
### R4
```
- LRUCache uses an OrderedDict to store items [src/cache.py:11]
- LRUCache.get marks accessed keys as recently used by moving them to the end of the OrderedDict [src/cache.py:22]
- LRUCache.get removes expired keys based on a time-to-live (ttl) and returns a default value [src/cache.py:19-21]
- LRUCache.put evicts the least recently used entry when the cache exceeds its capacity [src/cache.py:29-30]
- LRUCache stores a timestamp alongside each value to track expiration [src/cache.py:27]
- JobQueue uses a heap to store jobs [src/jobq.py:25]
- JobQueue orders jobs by ready_at time, then priority, then push order [src/jobq.py:31]
- JobQueue.pop returns the next job whose ready_at time has passed [src/jobq.py:35-36]
- JobQueue.fail requeues a job after a calculated backoff delay [src/jobq.py:45]
- JobQueue.fail moves a job to the dead list after it exceeds MAX_RETRIES [src/jobq.py:42-43]
```
### R5
```
- LRUCache keeps items in an OrderedDict [src/cache.py:6, src/cache.py:11]
- get moves accessed keys to the end to mark them recently used [src/cache.py:13, src/cache.py:22]
- get removes expired keys based on a time-to-live [src/cache.py:19, src/cache.py:20]
- put moves newly added keys to the end [src/cache.py:25, src/cache.py:28]
- put evicts the least recently used entry when capacity is exceeded [src/cache.py:29, src/cache.py:30]
- JobQueue keeps items in a heap [src/jobq.py:23, src/jobq.py:25]
- push orders jobs by ready_at, then priority, then push order [src/jobq.py:29, src/jobq.py:31]
- pop removes the next job whose ready_at has passed [src/jobq.py:33, src/jobq.py:36]
- fail requeues failed jobs after a backoff delay [src/jobq.py:39, src/jobq.py:45]
- fail moves jobs to a dead list after exceeding maximum retries [src/jobq.py:42, src/jobq.py:43]
```
### R6
```
Here is a comparison of how ordering and removal differ between the two modules:

---

### Ordering

| | `src/cache.py` | `src/jobq.py` |
|---|---|---|
| **Data structure** | `OrderedDict` | `heapq` (binary heap) |
| **Ordering key** | Insertion / last-access time — items are ordered by when they were most recently touched (`move_to_end` on `get` and `put`) | Tuples of `(ready_at, priority, counter)` — lowest `ready_at` first, then lowest `priority` (lower = higher urgency), then FIFO push order |
| **Access changes order** | Yes — `get` and `put` call `move_to_end(key)` to mark the item as recently used (`cache.py`:17, `cache.py`:24) | No — `push` places the job at the correct heap position; order is fixed by the tuple comparison |

### Removal

| | `src/cache.py` | `src/jobq.py` |
|---|---|---|
| **Eviction trigger** | **Capacity** — evicted when `len(self._items) > self.capacity` (`cache.py`:26) | **None explicit** — the heap is unbounded; items stay until popped or failed |
| **Eviction policy** | **LRU** — `popitem(last=False)` removes the *least recently used* entry (`cache.py`:27) | **Earliest ready_at** — `heapq.heappop` removes the job with the smallest `(ready_at, priority, counter)` tuple (`jobq.py`:20) |
| **Time-based expiry** | **TTL** — `get` checks `self._clock() - stored_at > self.ttl` and deletes expired entries on read (`cache.py`:14-15) | Not present — no TTL or time-based expiry is implemented |
| **Failure-based removal** | Not present | **`fail`** — after `MAX_RETRIES` (3) failed attempts, the job is moved to `self.dead` (`jobq.py`:29-30); otherwise it is re-queued with a backoff delay (`jobq.py`:31) |
| **Clear** | `clear()` empties all items (`cache.py`:29) | No `clear` method exists — "not in the files" |

### Summary of key differences

1. **Ordering basis**: The cache orders by **recency of access** (LRU via `OrderedDict`), while the job queue orders by **`(ready_at, priority, insertion_order)`** via a min-heap.
2. **Removal by capacity vs. none**: The cache enforces a hard `capacity` and evicts the **least recently used** item (`cache.py`:26-27). The job queue has no capacity limit and never evicts items proactively.
3. **Time-based behavior**: The cache supports a **TTL** that expires items on read (`cache.py`:14-15). The job queue uses `ready_at` as a **delayed-release** mechanism but has no expiry — a job whose `ready_at` never arrives simply stays in the heap forever.
4. **Failure handling**: Only the job queue has a **failure/retry/dead-letter** path (`jobq.py`:28-31). The cache has no concept of failure.
```
### R7
```
- LRUCache keeps items in an OrderedDict [src/cache.py:11]
- LRUCache orders items by last access time using move_to_end [src/cache.py:22]
- LRUCache orders items by insertion time using move_to_end [src/cache.py:28]
- LRUCache removes expired entries when they are accessed [src/cache.py:19, src/cache.py:20]
- LRUCache removes the least recently used entry when capacity is exceeded [src/cache.py:29, src/cache.py:30]
- JobQueue keeps items in a heap [src/jobq.py:25]
- JobQueue orders items by ready_at time [src/jobq.py:31]
- JobQueue orders items by priority (lower first) [src/jobq.py:31]
- JobQueue orders items by push order using a counter [src/jobq.py:31]
- JobQueue removes items when they are popped [src/jobq.py:36]
- JobQueue moves items to a dead list after exceeding MAX_RETRIES [src/jobq.py:42, src/jobq.py:43]
```
### R8
```
- LRUCache uses an OrderedDict to store items, maintaining insertion order and allowing move-to-end operations [src/cache.py:11]
- LRUCache.get marks accessed keys as recently used by moving them to the end of the OrderedDict [src/cache.py:22]
- LRUCache.evicts the least recently used entry by popping the first item from the OrderedDict when capacity is exceeded [src/cache.py:29-30]
- LRUCache removes expired entries during get operations if the current time exceeds the stored timestamp plus the TTL [src/cache.py:19-21]
- JobQueue uses a heap to store jobs, ordering them by ready_at time, then priority, then push order [src/jobq.py:31]
- JobQueue.pop returns the next job whose ready_at time has passed, removing it from the heap [src/jobq.py:35-36]
- JobQueue.fail requeues failed jobs after a backoff period or moves them to a dead list after exceeding MAX_RETRIES [src/jobq.py:41-45]
```
### R9
```
- LRUCache uses an OrderedDict to store items [src/cache.py:11]
- LRUCache.get returns the cached value and marks the key as recently used [src/cache.py:13, src/cache.py:22]
- LRUCache.get returns the default value when a key is missing [src/cache.py:16, src/cache.py:17]
- LRUCache.get removes expired keys and returns the default value [src/cache.py:19, src/cache.py:20, src/cache.py:21]
- LRUCache.put stores a value and records the current time [src/cache.py:25, src/cache.py:27]
- LRUCache.put moves the key to the end of the ordered dictionary [src/cache.py:28]
- LRUCache.popitem removes the least recently used entry when the cache exceeds capacity [src/cache.py:29, src/cache.py:30]
- JobQueue uses a heap to store jobs [src/jobq.py:25]
- JobQueue.push adds a job to the heap with ready_at, priority, and a counter [src/jobq.py:29, src/jobq.py:31]
- JobQueue.pop returns the next job whose ready_at has passed [src/jobq.py:33, src/jobq.py:35, src/jobq.py:36]
- JobQueue.fail increments the job's attempt count [src/jobq.py:39, src/jobq.py:41]
- JobQueue.fail moves a job to the dead list when attempts exceed MAX_RETRIES [src/jobq.py:42, src/jobq.py:43, src/jobq.py:44]
- JobQueue.fail requeues a job after a backoff delay when attempts do not exceed MAX_RETRIES [src/jobq.py:45, src/jobq.py:46]
```
