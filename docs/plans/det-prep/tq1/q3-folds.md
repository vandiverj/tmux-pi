# Q3: fold tests that differ only in input into table rows (Main, 2026-11-11)

Owner (TQ1, "all as recommended"; `decisions.md` this dir): Q3 = S1(b) identical-coverage groups to table-driven tests and S5(c)
review_rounds groups; every assertion kept; covered lines the same or larger; time and count before and after; no deletions;
cross-provider review. Reports: `S1-lanes.md` (Redundancy) and `S5-review-retry.md` (Redundancy), this dir. Owner's rule: no
ritual tests; extend or correct before adding. Lane tq1-folds, base main. Kind: mechanical (Sonnet).

## Groups
1. `tests/test_lane_other_paths.py` `OtherLanePathsTests` (the ~6 path-quoting tests) and `LaneFactsTests` (~7 lane-section tests).
2. `tests/test_lane_create.py` `CreateTests` / `CommandTests` (the ~8 that S1 found reach identical lines).
3. `tests/test_lane_lease.py` stale-holder group (~3).
4. `tests/test_review_rounds.py` table rows written as separate tests over one rule (S5: 33 groups / 73 tests across the section,
   mostly here). Leave `FourStateTests` and every "alone holds coverage" test named in S5 as they are.
To find the exact members, use the pinned coverage install outside the repo, `/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/covenv/bin/coverage`
(run with `--context` per test, data under /tmp), or read the tests: a member qualifies only if it has the same setup and the same
assertions as its siblings and differs in input alone.

## For each fold
- Before folding: `git log -L` on each member; if a member was added or changed for a bug, its case becomes a named row and the
  report names the commit.
- One test, one setup, `subTest` per row; the row name says what the old test name said. Every old assertion appears once per row.
- A member whose assertions differ from its siblings stays a separate test (list it as "kept, differs: <why>").
- Table in the report: old test -> new row, assertions carried (by line), commit if bug-linked.

## Proof
Per module: test count (methods and rows), time before and after (one run each, same machine, outside tmux). Mutation check per
fold: a scratch edit to the source rule each folded group covers makes the folded test fail (revert it after). Main measures
covered lines on the preview.

## Verification (the worker tests once)
The changed modules plus tests.test_session_env once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION
-u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`, through
`tools/agent/test-run.sh`, log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`.
Runs under 10 minutes, foreground. Edit only the four test files above. No product code changes. No Claude or pi start; nothing
to 127.0.0.1:1234 or any network; never the default tmux server. Do not edit config/. Search with safe_grep, read with
Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. **Guard refusals:** a refused shell or
inline-script edit may be redone with Edit/Write and listed; any other refusal: stop and report. Approve no prompt. Commit
nothing. No background shells. `RESULT_STATUS: complete` as the last line.
