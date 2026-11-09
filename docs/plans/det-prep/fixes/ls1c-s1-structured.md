# LS1c step S1: structured summarize_files module (Main, 2026-11-09)

Read docs/plans/det-prep/ls1c-structured-summarize.md (the design and why) and
/Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/s2-2026-11-09/structured_probe.py (Main's probe; reuse its ideas, not its code paths). Build steps 1-4 and 6
of the design. Step 5 (routing `summarize_files` dispatches here) is a later step: do not change tp-route, tp-spawn or the
router in this step.

## Build
1. `lib/summarize_structured.py`:
   - `prepare(root, paths, budget_chars)` numbers each named file (`{n:>4}\t{text}`). It refuses a path outside root, a
     symlink leaving root, `.env*`, the trust store or a path the existing safe-read checks withhold (reuse those
     functions; do not copy rules). It refuses when the total exceeds the budget, naming the sizes.
   - `ask(endpoint, model, system, user, schema, timeout)` makes one POST to `/v1/chat/completions` with `response_format`
     json_schema and temperature 0. It reads `content`, else `reasoning_content`. On HTTP or JSON errors it raises a
     typed error with the endpoint and status, never the body of a file.
   - `validate(facts, files)` normalizes `path` (strip a `:N[-M]` suffix, a leading `./`, backticks, whitespace). It drops a
     fact whose path is not named, whose range is outside the file or reversed, or whose range spans more than 5 lines.
     It returns kept facts plus drops with a reason each. It never changes a line number.
   - `render(facts)` writes one `- <claim> [path:a]` or `- <claim> [path:a-b]` per fact. It collapses whitespace in the
     claim and strips a trailing period.
   - `summarize(...)` ties the four together and returns the text plus a record: files, sizes, drops, seconds, model.
2. The system prompt and schema live in one place, `config/local.example.json` under a new key, and are read through the
   existing config loader. Main tunes the text later.
3. `tools/qual/summarize_run.py --path structured` calls `summarize` (the same function, not a copy), with `--endpoint`
   (default from the router config's LM Studio base URL). It scores with the existing checker, and the record carries
   `drops`. The default `--path pi` is unchanged.

## Tests (stub the endpoint with a local HTTP server on 127.0.0.1, port 0; no live model)
- prepare: numbering, every refusal, the budget.
- ask: content vs reasoning_content, HTTP 500, non-JSON, a timeout.
- validate: each normalization and each drop reason. A range of exactly 5 is kept and 6 is dropped. An unnamed file is
  dropped.
- render: exact text, which the existing `summarize_check.FACT_LINE` parses.
- runner `--path structured` with the stub: records and summary written, no temp dir left, no tmux or git call (the
  existing stub-PATH pattern).
- Each new test fails before the change (keep the script and log under the task dir's `evidence/`).

## Done means
1. The module, the config key and the runner path, with tests.
2. The new tests plus test_qual_summarize* pass in both interpreters, both modes, with each file's time.
3. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search `bin/`
as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively (name config files).
No live model and no request to 127.0.0.1:1234. Any test that calls tmux: set `TMUX_TMPDIR` to a temp dir and use a private
`-L` socket.
