# Chore steps-states

## Task
List the states a job can be in, in the order docs/lifecycle.md lists them. Files: docs/lifecycle.md. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
### docs/lifecycle.md
```
   1	# Job lifecycle
   2	
   3	A job is always in one of five states.
   4	
   5	1. **pending**: pushed to the queue, waiting for its ready time.
   6	2. **running**: popped by a worker, handler executing.
   7	3. **succeeded**: the handler returned without raising.
   8	4. **retrying**: the handler raised; the job is back in the queue with a backoff delay.
   9	5. **dead**: the handler raised after the third retry, or no handler is registered. The job sits in `queue.dead` and is never run again.
  10	
  11	Transitions: pending -> running; running -> succeeded; running -> retrying; retrying -> running; running -> dead.
  12	
  13	Dead jobs are kept in memory only. Restarting the process drops them.
```

## Reference facts (what a complete answer states)
F1: 1. pending: pushed to the queue [docs/lifecycle.md:5]
F2: 2. running: popped by a worker [docs/lifecycle.md:6]
F3: 3. succeeded: the handler returned [docs/lifecycle.md:7]
F4: 4. retrying: the handler raised and the job went back [docs/lifecycle.md:8]
F5: 5. dead: retries are used up [docs/lifecycle.md:9]

## Not in the files (a reply that says these is wrong)
- regex: `\b(cancell?ed|paused|expired|timed.?out|archived)\b`

## Replies
### R1
```
- The first state a job can be in is pending, where it is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- The second state a job can be in is running, where it is popped by a worker and the handler is executing [docs/lifecycle.md:6]
- The third state a job can be in is succeeded, where the handler returned without raising [docs/lifecycle.md:7]
- The fourth state a job can be in is retrying, where the handler raised and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- The fifth state a job can be in is dead, where the handler raised after the third retry or no handler is registered, and the job sits in queue.dead and is never run again [docs/lifecycle.md:9]
```
### R2
```
Based on `docs/lifecycle.md`, the states a job can be in, in order, are:

1. **pending** — pushed to the queue, waiting for its ready time. (docs/lifecycle.md:5)
2. **running** — popped by a worker, handler executing. (docs/lifecycle.md:6)
3. **succeeded** — the handler returned without raising. (docs/lifecycle.md:7)
4. **retrying** — the handler raised; the job is back in the queue with a backoff delay. (docs/lifecycle.md:8)
5. **dead** — the handler raised after the third retry, or no handler is registered. The job sits in `queue.dead` and is never run again. (docs/lifecycle.md:9)

The file does **not** show any other states or additional state-related details beyond these five.
```
### R3
```
- pending is a state a job can be in, pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- running is a state a job can be in, popped by a worker, handler executing [docs/lifecycle.md:6]
- succeeded is a state a job can be in, the handler returned without raising [docs/lifecycle.md:7]
- retrying is a state a job can be in, the handler raised; the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state a job can be in, the handler raised after the third retry, or no handler is registered. The job sits in `queue.dead` and is never run again [docs/lifecycle.md:9]
```
### R4
```
- pending is a state a job can be in, pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- running is a state a job can be in, popped by a worker, handler executing [docs/lifecycle.md:6]
- succeeded is a state a job can be in, the handler returned without raising [docs/lifecycle.md:7]
- retrying is a state a job can be in, the handler raised; the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state a job can be in, the handler raised after the third retry, or no handler is registered. The job sits in `queue.dead` and is never run again [docs/lifecycle.md:9]
```
### R5
```
- pending is the state when a job is pushed to the queue and waiting for its ready time [docs/lifecycle.md:5]
- running is the state when a job is popped by a worker and its handler is executing [docs/lifecycle.md:6]
- succeeded is the state when a job's handler returns without raising an error [docs/lifecycle.md:7]
- retrying is the state when a job's handler raises an error and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is the state when a job's handler raises an error after the third retry or no handler is registered, and the job sits in queue.dead [docs/lifecycle.md:9]
```
### R6
```
- pending is a state where a job is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- running is a state where a job is popped by a worker and its handler is executing [docs/lifecycle.md:6]
- succeeded is a state where the handler returns without raising an error [docs/lifecycle.md:7]
- retrying is a state where the handler raised an error and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state where the handler raised after the third retry or no handler is registered, and the job sits in queue.dead and is never run again [docs/lifecycle.md:9]
```
### R7
```
- pending is a state where a job is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- running is a state where a job is popped by a worker and its handler is executing [docs/lifecycle.md:6]
- succeeded is a state where a job's handler returns without raising an error [docs/lifecycle.md:7]
- retrying is a state where a job's handler raised an error and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state where a job's handler raised an error after the third retry or no handler is registered, and the job sits in queue.dead without being run again [docs/lifecycle.md:9]
```
### R8
```
- pending is a state where a job is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- running is a state where a job is popped by a worker and its handler is executing [docs/lifecycle.md:6]
- succeeded is a state where the handler returns without raising an error [docs/lifecycle.md:7]
- retrying is a state where the handler raised an error and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state where the handler raised after the third retry or no handler is registered, and the job sits in queue.dead and is never run again [docs/lifecycle.md:9]
```
### R9
```
- pending is a state where a job is pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- running is a state where a job is popped by a worker and the handler is executing [docs/lifecycle.md:6]
- succeeded is a state where the handler returned without raising [docs/lifecycle.md:7]
- retrying is a state where the handler raised and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is a state where the handler raised after the third retry or no handler is registered, and the job sits in queue.dead and is never run again [docs/lifecycle.md:9]
```
