# E1: make the routines report count honestly

Lane `measure`, step 1. Level: `small` coder (`--level small`): one file plus its test module. Touches: `lib/routines.py`,
`tests/test_routines.py`.

## Why
The 2026-11-06 report's token figures cannot be compared: a call matching several probes adds its whole output to each
(`keys_for` returns every match), the probe labelled "tp-agent request/help" matches any `tp-agent` command, the lane-base probe
includes `git fetch`, identical repeated commands are not deduplicated, and the counts file stores task counts with no
denominator.

## Change
1. Probes become first-match (most specific first; order the table so `pre-commit gate bundle` style aggregates come after
   their parts) and the report prints two columns: `exclusive` (first match) and `any` (every match, today's number).
2. Rename "tp-agent request/help" to "tp-agent (any)" and add exact probes for `tp-agent --help|-h|help` and
   `tp-agent request`. Split `git fetch` out of lane-base into its own probe.
3. Dedupe: per task, identical normalised command strings count once in a `distinct` column; raw `calls` stays.
4. Denominators: the counts file records tasks per role; the report prints `tasks with routine / tasks in role` and
   `calls per task`. `compare` flags growth on the ratio, not the raw count.
5. Keep `--since`; add `--until` so two disjoint windows can be compared.

## Done means
1. A fixture task whose one call matches two probes contributes its tokens once in `exclusive` and twice in `any`.
2. `tp-agent --help` and `tp-agent request --kind ...` land in different probes; `git fetch` is not in lane-base.
3. Two identical `sed -n 1,40p x` calls in one task give calls 2, distinct 1.
4. The text report shows per-role denominators and ratios; `compare` uses the ratio.
5. `--until` excludes tasks created after it.
6. Tests in `tests/test_routines.py` cover 1 to 5 and fail without the change; existing tests still pass.
