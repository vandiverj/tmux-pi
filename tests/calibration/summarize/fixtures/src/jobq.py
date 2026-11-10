"""In-memory job queue with priorities, retry backoff and a dead-letter list."""
import heapq
import itertools

MAX_RETRIES = 3
BACKOFF_SECONDS = (1, 5, 30)


def backoff_for(attempt):
    """Seconds to wait before retry number `attempt` (1-based); the last backoff repeats."""
    index = min(attempt, len(BACKOFF_SECONDS)) - 1
    return BACKOFF_SECONDS[index]


class Job:
    def __init__(self, name, payload, priority=0):
        self.name = name
        self.payload = payload
        self.priority = priority
        self.attempts = 0


class JobQueue:
    def __init__(self):
        self._heap = []
        self._counter = itertools.count()
        self.dead = []

    def push(self, job, ready_at=0.0):
        """Add a job. Jobs pop by ready_at, then priority (lower first), then push order."""
        heapq.heappush(self._heap, (ready_at, job.priority, next(self._counter), job))

    def pop(self, now):
        """Return the next job whose ready_at has passed, or None."""
        if self._heap and self._heap[0][0] <= now:
            return heapq.heappop(self._heap)[3]
        return None

    def fail(self, job, now):
        """Count a failed attempt; requeue after the backoff, or move to the dead list after MAX_RETRIES."""
        job.attempts += 1
        if job.attempts > MAX_RETRIES:
            self.dead.append(job)
            return False
        self.push(job, ready_at=now + backoff_for(job.attempts))
        return True

    def __len__(self):
        return len(self._heap)
