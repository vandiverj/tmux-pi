"""Small LRU cache with an optional time-to-live."""
import time
from collections import OrderedDict


class LRUCache:
    def __init__(self, capacity=128, ttl=None, clock=time.monotonic):
        self.capacity = capacity
        self.ttl = ttl
        self._clock = clock
        self._items = OrderedDict()

    def get(self, key, default=None):
        """Return the cached value and mark it recently used; expired or missing keys give default."""
        entry = self._items.get(key)
        if entry is None:
            return default
        value, stored_at = entry
        if self.ttl is not None and self._clock() - stored_at > self.ttl:
            del self._items[key]
            return default
        self._items.move_to_end(key)
        return value

    def put(self, key, value):
        """Store a value; evict the least recently used entry when over capacity."""
        self._items[key] = (value, self._clock())
        self._items.move_to_end(key)
        if len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def clear(self):
        self._items.clear()

    def __len__(self):
        return len(self._items)
