# SN1: two review notes left open (lane small-notes, mechanical)

Two notes from approved reviews, too small for lanes of their own.

1. **Bench readers (TR2 review, not fixed there).** TR2 (`tr2-brief.md`, merged 20af558) made the task-record readers tolerate
   non-UTF-8 files:
   - `bin/tp-agent` record/retry;
   - `lib/model_router.py` review_plan;
   - `lib/contract.py` ledger_row.

   `lib/bench/runner.py:381` and `:422` still read a worker's `result.md` with a bare `read_text()`, so one invalid byte in a result raises and ends the bench run. Read them the way TR2's readers do (find them; use the same encoding and errors choice, not a new one). Check the rest of `lib/bench/` for the same strict read of a worker-written file, and list each one in report.md as fixed or not applicable, with the reason.
2. **Stale comment (HB8b review note, review-coder-c1a7ce3072).** `tests/test_episode_harness.py:223` and any nearby comment or report wording still describe the rules HB8b round 7 removed (the mtime window, the stable window, continuous presence). Read `docs/plans/det-prep/hub/hb8/hb8b-decision.md` and the round-7/8 briefs in that dir, then make the comments say what the code does now. Comments only: no behaviour change in this item.

Attack table (in report.md, short) for item 1:
- input: result.md as UTF-8, Latin-1 bytes, a lone surrogate-like byte, an empty file, or missing;
- reader: each fixed site.

For each row, give the expected outcome and the row that proves it. Extend the nearest existing bench test (find it with safe_grep) with one table-driven row set. No new module and no ritual tests. Show the fail-before on main in the evidence-file form: a `.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`.

Run the touched test modules and `tests.test_session_env` once each through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground, one after another.

Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
