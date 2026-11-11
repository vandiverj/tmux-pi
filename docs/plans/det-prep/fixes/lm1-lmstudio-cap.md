# LM1: a cap on LM Studio requests from tests, attacks and workers (Main, 2026-11-11)

## Owner decision
Main proposed, after measuring `qwen/qwen3.6-35b-a3b` (medium 12k-character request ~16 s; full summarizer size 60k characters
35 s; two at once each ~25% slower): opt-in only, a 300 s wait per request, one request at a time, at most 20 requests per run.
Owner, 2026-11-11: "put the cap on lm studio as proposed". Earlier: "we should probably determine the limit but the test would have
to be able to wait for the longer response time and be okay". Why: an SW1 attack once sent ~216-432 requests to LM Studio, which
the owner also uses for their own work. Bar: realistic use.

## Where requests go today
Every request to a local model endpoint goes through `lib/router.py` `local_open` (callers: `lib/summarize_structured.py` `ask`,
`lib/llm_router.py`). Tests reach it only with stub servers on free loopback ports; none reaches 127.0.0.1:1234 today. Production
use (the owner's summarizer runs) must stay unrestricted.

## The rules
1. **One guard at the chokepoint.** `local_open` counts and limits requests to the LM Studio endpoint (the configured local
   endpoint's host and port, 127.0.0.1:1234 / localhost:1234 on this Mac; read it from where the router reads it, do not hard-code
   if a config value exists). Requests to any other loopback port (the tests' stub servers) are not counted or limited.
2. **Off unless a budget is set.** With no budget variable in the environment, behaviour is unchanged (production). A budget is a
   variable naming a cap and a counter file (your names; e.g. `TP_LMSTUDIO_BUDGET=<cap>` and `TP_LMSTUDIO_BUDGET_FILE=<path>`).
   The counter file is shared by every process of one run (subprocesses inherit the variables), updated under an exclusive file
   lock, and the lock is also held for the whole request so only one request is in flight per run (concurrency 1).
3. **Refusal is clear and early.** Request number cap+1 is refused before anything is sent, with an error that names the cap and
   the variable; callers see it as their normal failure (summarize_structured: an AskError), never a hang.
4. **Tests default to zero.** The test package (`tests/__init__.py`, or the place every `python3 -m unittest` run of tests imports)
   sets a budget of 0 with a fresh counter file unless one is already set, so no default test can reach LM Studio. With
   `TP_LIVE_LMSTUDIO=1` the budget is 20 and the per-request timeout used by live tests is 300 s.
5. **Workers default to zero.** `bin/tp-spawn` gives every worker and reviewer a budget of 0 (their own counter file in the task
   dir), so attack scripts and test runs in a worker cannot reach LM Studio. A worker that needs live requests is a Main decision:
   leave a documented way for Main to pass a non-zero budget at dispatch only if it is a one-line addition; otherwise state it as
   a follow-up.
6. **One opt-in live test.** Add (or extend an existing summarizer test module with) one test, skipped unless
   `TP_LIVE_LMSTUDIO=1`, that sends one full-size summarizer request (60k characters, as the default limit) with the 300 s timeout
   and checks a well-formed answer. It is the "determine the limit, wait for the longer response" check. Do NOT run it yourself.
7. **Docs.** One short section where test rules live (docs/agent-tools.md or the testing doc you find with safe_grep): the cap,
   the opt-in, the timeout, concurrency 1, and that stress/attack tests always use the stub server.

## Tests (tests earn their place)
Extend the existing router/summarizer tests (tests/test_router.py, tests/test_qual_summarize_structured.py) rather than new files.
One table-driven test over: no budget (unchanged), budget 0 (refused, nothing sent), budget 2 (third refused), a stub on another
port (never counted), two processes sharing one counter (serialised, total respected), a counter file that is missing, empty, or
not a number (refused, not unlimited). Use a stub HTTP server bound to a free port and point the "LM Studio endpoint" at it
through whatever override the router already reads, so no test touches 127.0.0.1:1234. Fail-before against the lane base. Report
added vs extended, and an attack table (report artifact, not one test per row).

## Verification (the worker tests once)
Run tests.test_router, tests.test_qual_summarize_structured, tests.test_llm_router (if it exists), tests.test_orchestration,
tests.test_session_env once with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u
TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`; the log starts with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery.
Nothing to 127.0.0.1:1234 or any network; no Claude or pi start; private tmux socket only. Do not edit config/ (if a config value
is needed, report it as a proposal). Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*, auth
files or config/local.json. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
