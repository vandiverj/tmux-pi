# Limits

- Retries: at most 3 (`MAX_RETRIES` in `src/jobq.py`), waiting 1 s, 5 s and 30 s.
- The queue is in memory. Nothing survives a restart.
- Workers: the default is 2; set `JOBQ_WORKERS` to change it.
- Log level: `info` by default; `JOBQ_LOG_LEVEL` changes it.
- There is no authentication. Run it only on a trusted host.
