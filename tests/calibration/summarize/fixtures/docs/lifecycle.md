# Job lifecycle

A job is always in one of five states.

1. **pending**: pushed to the queue, waiting for its ready time.
2. **running**: popped by a worker, handler executing.
3. **succeeded**: the handler returned without raising.
4. **retrying**: the handler raised; the job is back in the queue with a backoff delay.
5. **dead**: the handler raised after the third retry, or no handler is registered. The job sits in `queue.dead` and is never run again.

Transitions: pending -> running; running -> succeeded; running -> retrying; retrying -> running; running -> dead.

Dead jobs are kept in memory only. Restarting the process drops them.
