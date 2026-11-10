# Chore exports-cache

## Task
What does cache.py export, and what is each method's job? Files: src/cache.py. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

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

## Reference facts (what a complete answer states)
F1: cache.py exports LRUCache [src/cache.py:6]
F2: LRUCache has a get method [src/cache.py:13]
F3: LRUCache has a put method [src/cache.py:25]
F4: LRUCache has a clear method [src/cache.py:32]
F5: LRUCache has a __len__ method [src/cache.py:35]
F6: get returns the cached value and marks it as recently used [src/cache.py:14]
F7: put stores a value and evicts the oldest entry when the cache is full [src/cache.py:26]
F8: clear empties the cache [src/cache.py:33]
F9: __len__ gives the number of entries [src/cache.py:36]
F10: ttl, when set, makes old entries expire [src/cache.py:9]
F11: capacity limits how many entries it keeps [src/cache.py:8]

## Not in the files (a reply that says these is wrong)
- regex: `LRUCache\.(delete|remove|keys|items)\b`
- regex: `\bdef (delete|remove)\b`

## Replies
### R1
```
- LRUCache is a class that implements a small LRU cache with an optional time-to-live [src/cache.py:6]
- LRUCache.__init__ initializes the cache with a capacity, an optional time-to-live (ttl), and an optional clock function, defaulting to time.monotonic [src/cache.py:7-11]
- LRUCache.clear removes all items from the cache [src/cache.py:32-33]
- LRUCache.__len__ returns the number of items currently in the cache [src/cache.py:35-36]
```
### R2
```
- LRUCache is a class that keeps records in an OrderedDict [src/cache.py:6, src/cache.py:11]
- LRUCache stores a capacity, ttl, and clock in its attributes [src/cache.py:7, src/cache.py:8, src/cache.py:9]
- get returns the cached value and marks it recently used [src/cache.py:13, src/cache.py:22, src/cache.py:23]
- get returns the default when the key is missing [src/cache.py:15, src/cache.py:17]
- get returns the default when the entry has expired [src/cache.py:19, src/cache.py:21]
- put stores a value along with the current clock time [src/cache.py:25, src/cache.py:27]
- put moves the newly stored key to the end of the order [src/cache.py:28]
- put evicts the least recently used entry when the cache is over capacity [src/cache.py:29, src/cache.py:30]
- clear removes every entry from the cache [src/cache.py:32, src/cache.py:33]
- __len__ returns the number of entries in the cache [src/cache.py:35, src/cache.py:36]
```
### R3
```
- LRUCache is a class that implements a small LRU cache with an optional time-to-live [src/cache.py:6]
- LRUCache.__init__ sets the capacity to the provided value or 128 [src/cache.py:7, src/cache.py:8]
- LRUCache.__init__ sets the ttl to the provided value or None [src/cache.py:7, src/cache.py:9]
- LRUCache.__init__ sets the clock to the provided function or time.monotonic [src/cache.py:7, src/cache.py:10]
- LRUCache.__init__ initializes _items as an empty OrderedDict [src/cache.py:11]
- LRUCache.get returns the cached value for a key and marks it recently used [src/cache.py:13, src/cache.py:23]
- LRUCache.get returns the default value when the key is missing [src/cache.py:15, src/cache.py:17]
- LRUCache.get returns the default value when the entry is expired [src/cache.py:19, src/cache.py:21]
- LRUCache.get deletes expired entries from _items [src/cache.py:20]
- LRUCache.get moves the key to the end of _items to mark it recently used [src/cache.py:22]
- LRUCache.put stores a value associated with a key along with the current clock time [src/cache.py:25, src/cache.py:27]
- LRUCache.put moves the key to the end of _items to mark it recently used [src/cache.py:28]
- LRUCache.put evicts the least recently used entry when the cache exceeds capacity [src/cache.py:29, src/cache.py:30]
- LRUCache.clear clears all entries from _items [src/cache.py:32, src/cache.py:33]
- LRUCache.__len__ returns the number of items in _items [src/cache.py:35, src/cache.py:36]
```
### R4
```
- LRUCache.clear removes all items from the cache [src/cache.py:32-33]
- LRUCache.__len__ returns the number of items in the cache [src/cache.py:35-36]
```
### R5
```
- cache.py defines the class LRUCache [src/cache.py:6]
- cache.py defines the method get [src/cache.py:13]
- cache.py defines the method put [src/cache.py:25]
- cache.py defines the method clear [src/cache.py:32]
- cache.py defines the method __len__ [src/cache.py:35]
- LRUCache is a class that implements a small LRU cache with an optional time-to-live [src/cache.py:1, src/cache.py:6]
- __init__ initializes the cache with a capacity, an optional TTL, and a clock function [src/cache.py:7, src/cache.py:8, src/cache.py:9]
- __init__ stores the capacity as an instance attribute [src/cache.py:8]
- __init__ stores the TTL as an instance attribute [src/cache.py:9]
- __init__ stores the clock function as an instance attribute [src/cache.py:10]
- __init__ initializes an empty ordered dictionary for storing items [src/cache.py:11]
- get returns the cached value for a key and marks it as recently used [src/cache.py:13, src/cache.py:14]
- get returns the default value when the key is missing [src/cache.py:15, src/cache.py:16, src/cache.py:17]
- get returns the default value when the entry has expired based on the TTL [src/cache.py:18, src/cache.py:19, src/cache.py:20]
- get removes expired entries from the cache [src/cache.py:20]
- get moves valid entries to the end of the ordered dictionary to mark them as recently used [src/cache.py:22]
- get returns the value for valid entries [src/cache.py:23]
- put stores a value associated with a key along with the current time [src/cache.py:25, src/cache.py:26]
- put moves the newly stored entry to the end of the ordered dictionary [src/cache.py:28]
- put evicts the least recently used entry when the cache exceeds its capacity [src/cache.py:29, src/cache.py:30]
- clear removes all items from the cache [src/cache.py:32, src/cache.py:33]
- __len__ returns the number of items currently in the cache [src/cache.py:35, src/cache.py:36]
```
### R6
```
Based on `src/cache.py`, here is what it exports and each method's job:

## Exports

**1. `LRUCache` class** — `src/cache.py:7`

A small LRU (Least Recently Used) cache with an optional time-to-live (TTL). It uses an `OrderedDict` internally (`src/cache.py:4`, `src/cache.py:12`).

### Methods

| Method | Line | Job |
|---|---|---|
| `__init__(self, capacity=128, ttl=None, clock=time.monotonic)` | `src/cache.py:9` | Initializes the cache with a max **capacity** (default 128), an optional **TTL** in seconds (default no expiry), and a **clock** function (default `time.monotonic`) used to track when entries are stored. |
| `get(self, key, default=None)` | `src/cache.py:14` | Returns the cached value for `key` and marks it as recently used. If the key is missing or expired (TTL exceeded), it deletes the entry and returns `default` (`src/cache.py:15–23`). |
| `put(self, key, value)` | `src/cache.py:25` | Stores a `(key, value)` pair. Marks it as recently used and evicts the least recently used entry if the cache exceeds its capacity (`src/cache.py:26–29`). |
| `clear(self)` | `src/cache.py:31` | Removes all entries from the cache (`src/cache.py:32`). |
| `__len__(self)` | `src/cache.py:34` | Returns the current number of items in the cache (`src/cache.py:35`). |
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
- LRUCache is a class that implements a small LRU cache with an optional time-to-live [src/cache.py:1, src/cache.py:6]
- __init__ initializes the cache with a capacity, an optional TTL, and a clock function [src/cache.py:7, src/cache.py:8, src/cache.py:9]
- capacity sets the maximum number of items the cache can hold [src/cache.py:8]
- ttl stores the optional time-to-live for cache entries [src/cache.py:9]
- clock provides a function to get the current time, defaulting to time.monotonic [src/cache.py:7, src/cache.py:10]
- get returns the cached value for a key and marks it as recently used [src/cache.py:13, src/cache.py:23]
- key is the identifier used to retrieve or store a value in the cache [src/cache.py:13, src/cache.py:15]
- default is the value returned when the key is missing or expired [src/cache.py:13, src/cache.py:17]
- put stores a value for a key and evicts the least recently used entry if over capacity [src/cache.py:25, src/cache.py:30]
- value is the data stored in the cache associated with a key [src/cache.py:25, src/cache.py:27]
- clear removes all items from the cache [src/cache.py:32, src/cache.py:33]
- __len__ returns the number of items currently in the cache [src/cache.py:35, src/cache.py:36]
```
### R8
```
- LRUCache is a class that initializes with capacity, ttl, and clock parameters, and maintains an OrderedDict of items [src/cache.py:6, src/cache.py:7, src/cache.py:8]
- LRUCache.get returns the cached value and marks it recently used, returning default for expired or missing keys [src/cache.py:13, src/cache.py:14, src/cache.py:15]
- LRUCache.put stores a value and evicts the least recently used entry when over capacity [src/cache.py:25, src/cache.py:26, src/cache.py:27]
- LRUCache.clear removes all items from the cache [src/cache.py:32, src/cache.py:33]
- LRUCache.__len__ returns the number of items in the cache [src/cache.py:35, src/cache.py:36]
```
### R9
```
- LRUCache is a class that keeps records in an OrderedDict [src/cache.py:6, src/cache.py:11]
- __init__ stores the capacity, ttl, and clock arguments [src/cache.py:7, src/cache.py:8, src/cache.py:9]
- __init__ initializes an empty OrderedDict for items [src/cache.py:11]
- get returns the cached value and marks it recently used [src/cache.py:13, src/cache.py:22]
- get returns the default value when the key is missing [src/cache.py:16, src/cache.py:17]
- get returns the default value when the entry has expired [src/cache.py:19, src/cache.py:21]
- put stores a value with the current time [src/cache.py:25, src/cache.py:27]
- put moves the stored key to the end to mark it recently used [src/cache.py:28]
- put evicts the least recently used entry when over capacity [src/cache.py:29, src/cache.py:30]
- clear removes all entries from the cache [src/cache.py:32, src/cache.py:33]
- __len__ returns the number of items in the cache [src/cache.py:35, src/cache.py:36]
```
