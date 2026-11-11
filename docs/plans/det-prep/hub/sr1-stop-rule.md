# SR1: the owner authorizes a stop rule once; Main closes under it (Main, 2026-11-11)

**Owner request, 2026-11-11:** "we need a better way to handle the review override when there are not changes so that you can
handle it yourself, propose a solution that does not allow you to skip necessary reviews just these ones"; on Main's proposal:
"go with your recommendations".

## Why
`tp-agent lane close NAME STEP --override-review --reason TEXT` (bin/tp-agent ~1531-1545, lib/lane_steps.py `close_step`) needs
the owner at a terminal with a typed-back code (lib/owner_confirm.py `confirm`). For a last round (HB3a, HB4a, HB8a) the owner
already decided the outcome when choosing it: "one rule-first last round; if the families recur, merge as is with the findings
as known limits". The later override adds no judgment, only the owner's presence. SR1 moves the owner's confirmation to the
moment of that decision and lets Main close only when the tool proves the case is the one the owner authorized.

## The rule
1. **Authorize (owner, typed-back code).** New `tp-agent lane authorize-stop NAME STEP --reason TEXT`. It calls
   `owner_confirm.confirm` exactly as `--override-review` does (same refusals with no terminal). It binds to the step's
   **gate review**: the newest review of the step that asked for changes at the time of authorizing. It records, in the lane
   record (validated by `lanes.lane_record` like every other field): an id, `authorized_at`, `reason`, `step`, `gate_review`,
   and `families`, the family names of that review's findings that close by code (read with the existing parser:
   lib/review_verdict.py / lib/family_sweep.py / lib/review_rounds.py, whichever already reads `Family:` and `Closes by:`; do
   not write a new parser). The confirm text shows the lane, step, gate review id, the families and the reason. One
   authorization per step; authorizing again replaces it (also confirmed). Refuse when the step is closed or has no review that
   asked for changes.
2. **Close (Main, no code).** New `tp-agent lane close NAME STEP --stop-rule --known-limits PATH`. Allowed only when all hold,
   each refusal naming which and why, before anything is written:
   a. the step has an authorization;
   b. the closing review (what `evaluate` judges today) asked for changes, on the exact clean HEAD tree being closed (the
      checks `evaluate` already makes for the override path stay);
   c. the closing review's author is a retry whose recorded `gate_review` (meta.json, written by `tp-agent retry` since RR1)
      equals the authorization's `gate_review`. A further retry from a later review breaks this, so an authorization is used
      for one last round only;
   d. every finding of the closing review that closes by code has a family, and that family is in the authorization's
      `families`. A finding with no family, or a new family, refuses: it goes back to the owner;
   e. PATH is a file committed at the main repository's HEAD or the lane's HEAD; it names the closing review id and every
      finding id (`F1`, `F2`, ...) of the closing review that closes by code;
   f. every other close check is unchanged.
   The closed entry has verdict `owner-override` (the existing value; readers stay as they are), `override_reason` = the
   authorization's reason, and a `stop_rule` object: authorization id, `authorized_at`, `gate_review`, `families`,
   `known_limits` path and its blob id. `--stop-rule` and `--override-review` are exclusive. The authorization is removed in
   the same write.
3. **Nothing else changes.** `--override-review` stays as it is for every other case. An approved review closes as today
   (`--stop-rule` on a step that needs no override refuses, as `--override-review` does). No path lets Main create or edit an
   authorization without the typed-back code.

## Tests (tests earn their place)
Extend tests/test_lane_steps.py (and test_lane_records.py for the schema) through their existing fixtures
(tests/lane_fixture.py); no new test files. Use the existing way those tests satisfy or stub `owner_confirm` for
`--override-review`. One table-driven attack test over the close: authorized and every condition holds (closes, entry fields);
no authorization; review approved; tree changed after the review; closing review's author has a different `gate_review` (a
second retry); finding with a new family; code finding with no family; known-limits file missing, uncommitted, or missing one
finding id; `--stop-rule` with `--override-review`; authorization for another step. Plus: authorize refuses without a terminal
and on a step with no changes-requested review; a lane record with a malformed authorization or `stop_rule` refuses to load.
Fail-before is the new commands' absence; say so rather than forcing it.

## Report
Changed files with what each change does; the attack table `Case | Expected | Got`; tests added / extended by name; the exact
help text of both new options; a dry statement of what HB8a would need to use it now (gate review
review-coder-c24113fdaa-130232, round-9 retry coder-a38acc0415).

## Verification (the worker tests once)
Run tests.test_lane_steps, tests.test_lane_records, tests.test_lane_create, tests.test_tp_review and tests.test_session_env once
through `tools/agent/test-run.sh`, with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID
-u TP_TASK_DIR -u TP_STATE_DIR`), a short fresh `TMUX_TMPDIR` under /tmp, the log starting with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the discovery. No Claude or pi start; nothing to
127.0.0.1:1234 or any network; private tmux `-L` sockets only. Do not touch the real lanes or this session's state: tests use
fixtures only, and never run the new commands against /Users/jerrickvandiver/Documents/Projects/tmux-pi's own lanes. Do not edit
config/, interfaces.md or release-gates.md. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json, .env*,
auth files or config/local.json. If a guard hook refuses a command, stop and report it; do not work around it. Commit nothing.
No background shells. `RESULT_STATUS: complete` as the last line.
