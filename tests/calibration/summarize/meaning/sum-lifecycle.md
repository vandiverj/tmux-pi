# Chore sum-lifecycle

## Task
Summarize docs/lifecycle.md in 3 to 5 bullets. Files: docs/lifecycle.md. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

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
F1: A job is always in one of five states [docs/lifecycle.md:3]
F2: A handler that raises sends the job back with a backoff delay [docs/lifecycle.md:8]
F3: A job is dead after the third retry or when no handler is registered [docs/lifecycle.md:9]
F4: Dead jobs are never run again [docs/lifecycle.md:9]
F5: Dead jobs live in memory only, so a restart drops them [docs/lifecycle.md:13]

## Not in the files (a reply that says these is wrong)
- regex: `persist(ed|s)? to (disk|a file|the database)`

## Replies
### R1
```
- docs/lifecycle.md describes the five states a job can be in [docs/lifecycle.md:3, docs/lifecycle.md:5, docs/lifecycle.md:6]
- docs/lifecycle.md lists pending as the first state, where a job waits in the queue [docs/lifecycle.md:5]
- docs/lifecycle.md lists running as the second state, where a worker pops and executes the job [docs/lifecycle.md:6]
- docs/lifecycle.md lists succeeded as the third state, reached when the handler returns without raising [docs/lifecycle.md:7]
- docs/lifecycle.md lists retrying as the fourth state, where a job returns to the queue with a backoff delay after raising [docs/lifecycle.md:8]
- docs/lifecycle.md lists dead as the fifth state, where a job sits in queue.dead after failing three retries or having no handler [docs/lifecycle.md:9]
- docs/lifecycle.md outlines the transitions between states [docs/lifecycle.md:11]
- docs/lifecycle.md states that dead jobs are kept in memory only and are dropped on restart [docs/lifecycle.md:13]
```
### R2
```
- A job is always in one of five states [docs/lifecycle.md:3]
- pending is pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- running is popped by a worker, handler executing [docs/lifecycle.md:6]
- succeeded is the handler returned without raising [docs/lifecycle.md:7]
- retrying is the handler raised; the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is the handler raised after the third retry, or no handler is registered. The job sits in queue.dead and is never run again [docs/lifecycle.md:9]
- Transitions are pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- Dead jobs are kept in memory only [docs/lifecycle.md:13]
- Restarting the process drops dead jobs [docs/lifecycle.md:13]
```
### R3
```
- Job lifecycle describes five states a job can be in [docs/lifecycle.md:3]
- Job lifecycle lists pending, running, succeeded, retrying, and dead as the five states [docs/lifecycle.md:5, docs/lifecycle.md:6, docs/lifecycle.md:7]
- Job lifecycle defines pending as a job pushed to the queue waiting for its ready time [docs/lifecycle.md:5]
- Job lifecycle defines running as a job popped by a worker with its handler executing [docs/lifecycle.md:6]
- Job lifecycle defines succeeded as a job whose handler returned without raising [docs/lifecycle.md:7]
- Job lifecycle defines retrying as a job that raised and is back in the queue with a backoff delay [docs/lifecycle.md:8]
- Job lifecycle defines dead as a job that raised after the third retry or has no handler, sitting in queue.dead [docs/lifecycle.md:9]
- Job lifecycle outlines transitions from pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- Job lifecycle states that dead jobs are kept in memory only and dropped upon process restart [docs/lifecycle.md:13]
```
### R4
```
- A job is always in one of five states [docs/lifecycle.md:3]
- pending is pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- running is popped by a worker, handler executing [docs/lifecycle.md:6]
- succeeded is the handler returned without raising [docs/lifecycle.md:7]
- retrying is the handler raised; the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- dead is the handler raised after the third retry, or no handler is registered. The job sits in queue.dead and is never run again [docs/lifecycle.md:9]
- Transitions are pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- Dead jobs are kept in memory only [docs/lifecycle.md:13]
- Restarting the process drops dead jobs [docs/lifecycle.md:13]
```
### R5
```
- docs/lifecycle.md describes the five states of a job lifecycle [docs/lifecycle.md:3, docs/lifecycle.md:5, docs/lifecycle.md:6]
- docs/lifecycle.md lists pending as the first state, where a job is pushed to the queue waiting for its ready time [docs/lifecycle.md:5]
- docs/lifecycle.md lists running as the second state, where a job is popped by a worker and the handler is executing [docs/lifecycle.md:6]
- docs/lifecycle.md lists succeeded as the third state, where the handler returned without raising [docs/lifecycle.md:7]
- docs/lifecycle.md lists retrying as the fourth state, where the handler raised and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- docs/lifecycle.md lists dead as the fifth state, where the handler raised after the third retry or no handler is registered, and the job sits in queue.dead [docs/lifecycle.md:9]
- docs/lifecycle.md outlines the transitions between states: pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- docs/lifecycle.md states that dead jobs are kept in memory only and restarting the process drops them [docs/lifecycle.md:13]
```
### R6
```
- docs/lifecycle.md states that a job is always in one of five states [docs/lifecycle.md:3]
- docs/lifecycle.md states that the pending state means the job is pushed to the queue, waiting for its ready time [docs/lifecycle.md:5]
- docs/lifecycle.md states that the running state means the job is popped by a worker, with the handler executing [docs/lifecycle.md:6]
- docs/lifecycle.md states that the succeeded state means the handler returned without raising [docs/lifecycle.md:7]
- docs/lifecycle.md states that the retrying state means the handler raised, and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- docs/lifecycle.md states that the dead state means the handler raised after the third retry, or no handler is registered, and the job sits in queue.dead and is never run again [docs/lifecycle.md:9]
- docs/lifecycle.md states the transitions: pending -> running; running -> succeeded; running -> retrying; retrying -> running; running -> dead [docs/lifecycle.md:11]
- docs/lifecycle.md states that dead jobs are kept in memory only, and restarting the process drops them [docs/lifecycle.md:13]
```
### R7
```
Here's a summary of `docs/lifecycle.md`:

- A job exists in one of five states: **pending** (waiting in the queue), **running** (executing on a worker), **succeeded** (handler completed without error), **retrying** (handler raised an exception, job requeued with backoff delay), and **dead** (handler failed after 3 retries or no handler is registered) — [docs/lifecycle.md:3-7]
- The state transitions follow: pending → running → succeeded / retrying / dead, with retrying looping back to running — [docs/lifecycle.md:9]
- Dead jobs are stored only in memory, so restarting the process causes them to be dropped — [docs/lifecycle.md:11]
```
### R8
```
- Job lifecycle is a document that describes the five states a job can be in [docs/lifecycle.md:1, docs/lifecycle.md:3]
- Job lifecycle lists pending as the state where a job is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- Job lifecycle lists running as the state where a worker pops the job and the handler executes [docs/lifecycle.md:6]
- Job lifecycle lists succeeded as the state reached when the handler returns without raising [docs/lifecycle.md:7]
- Job lifecycle lists retrying as the state where the handler raised and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- Job lifecycle lists dead as the state where the handler raised after the third retry or no handler is registered, keeping the job in queue.dead [docs/lifecycle.md:9]
- Job lifecycle describes the transitions between states as pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- Job lifecycle states that dead jobs are kept in memory only and restarting the process drops them [docs/lifecycle.md:13]
```
### R9
```
- Job lifecycle is a document that describes the five states a job can be in [docs/lifecycle.md:1, docs/lifecycle.md:3]
- Job lifecycle lists pending as the state where a job is pushed to the queue and waits for its ready time [docs/lifecycle.md:5]
- Job lifecycle lists running as the state where a worker pops the job and the handler executes [docs/lifecycle.md:6]
- Job lifecycle lists succeeded as the state where the handler returns without raising [docs/lifecycle.md:7]
- Job lifecycle lists retrying as the state where the handler raised and the job is back in the queue with a backoff delay [docs/lifecycle.md:8]
- Job lifecycle lists dead as the state where the handler raised after the third retry or no handler is registered, keeping the job in queue.dead [docs/lifecycle.md:9]
- Job lifecycle describes the transitions between states as pending to running, running to succeeded, running to retrying, retrying to running, and running to dead [docs/lifecycle.md:11]
- Job lifecycle states that dead jobs are kept in memory only and restarting the process drops them [docs/lifecycle.md:13]
```
