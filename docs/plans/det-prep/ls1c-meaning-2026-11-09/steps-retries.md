# Chore steps-retries

## Task
List the limits docs/limits.md states about retries. Files: docs/limits.md. Answer only from these files, cite path:line for each fact, and say "not in the files" for anything they do not show.

## Files (numbered)
### docs/limits.md
```
   1	# Limits
   2	
   3	- Retries: at most 3 (`MAX_RETRIES` in `src/jobq.py`), waiting 1 s, 5 s and 30 s.
   4	- The queue is in memory. Nothing survives a restart.
   5	- Workers: the default is 2; set `JOBQ_WORKERS` to change it.
   6	- Log level: `info` by default; `JOBQ_LOG_LEVEL` changes it.
   7	- There is no authentication. Run it only on a trusted host.
```

## Reference facts (what a complete answer states)
F1: Retries are limited to at most 3 [docs/limits.md:3]
F2: The first wait is 1 s [docs/limits.md:3]
F3: The second wait is 5 s [docs/limits.md:3]
F4: The last wait is 30 s [docs/limits.md:3]

## Not in the files (a reply that says these is wrong)
- regex: `\bexponential\b|\bjitter\b`

## Replies
### R1
```
- Retries allows at most 3 retries [docs/limits.md:3]
- Retries waits 1 s, 5 s and 30 s between retries [docs/limits.md:3]
```
### R2
```
- Retries allows at most 3 retries [docs/limits.md:3]
- Retries waits 1 s, 5 s and 30 s between retries [docs/limits.md:3]
```
### R3
```
- Retries allows at most 3 retries [docs/limits.md:3]
- Retries waits 1 s, 5 s and 30 s between retries [docs/limits.md:3]
```
### R4
```
According to `docs/limits.md`, here are the facts about retries:

- **Line 2**: There are at most **3 retries** (`MAX_RETRIES` in `src/jobq.py`), with wait intervals of **1 s, 5 s, and 30 s**.

That is all the document says about retries.
```
### R5
```
- Retries: at most 3 (`MAX_RETRIES` in `src/jobq.py`), waiting 1 s, 5 s and 30 s [docs/limits.md:3]
- Retries: waiting 1 s [docs/limits.md:3]
- Retries: waiting 5 s [docs/limits.md:3]
- Retries: waiting 30 s [docs/limits.md:3]
```
### R6
```
- Retries allows at most 3 retries [docs/limits.md:3]
- Retries waits 1 second, 5 seconds, and 30 seconds between retries [docs/limits.md:3]
```
### R7
```
- Retries allows at most 3 retries [docs/limits.md:3]
- Retries waits 1 second, 5 seconds, and 30 seconds between retries [docs/limits.md:3]
```
### R8
```
- Retries: at most 3 (`MAX_RETRIES` in `src/jobq.py`), waiting 1 s, 5 s and 30 s [docs/limits.md:3]
- Retries: at most 3 [docs/limits.md:3]
- Retries: waiting 1 s [docs/limits.md:3]
- Retries: waiting 5 s [docs/limits.md:3]
- Retries: waiting 30 s [docs/limits.md:3]
```
### R9
```
- Retries: at most 3 (MAX_RETRIES in src/jobq.py), waiting 1 s, 5 s and 30 s [docs/limits.md:3]
- The queue is in memory. Nothing survives a restart [docs/limits.md:4]
- Workers: the default is 2; set JOBQ_WORKERS to change it [docs/limits.md:5]
- Log level: info by default; JOBQ_LOG_LEVEL changes it [docs/limits.md:6]
- There is no authentication. Run it only on a trusted host [docs/limits.md:7]
```
