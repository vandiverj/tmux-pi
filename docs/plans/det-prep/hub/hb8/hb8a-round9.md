# HB8a round 9 (last): one owner for each record fact (Main, 2026-11-11)

Round 8's review (review-coder-c24113fdaa-130232, `result.md`, `report.md` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-c24113fdaa-130232/`)
keeps the records-only design (Bash parsing is gone) but finds verification-tree-identity (F1, F3), command-evidence-identity
(F2, F5) and unknown-telemetry (F4) again at new sites. **This is the last round.** The owner's pattern for the same stop
(HB3a, HB4a): one rule-first last round, then merge as is with the findings as known limits (HB8b). The HB8a brief and round 8
still apply except where a rule below replaces them. Bar: realistic use. Keep the change in tools/episodes and its tests.

## Main's diagnosis
Round 8's rule 2 said the scorer computes the final tree's fingerprint itself; the code reads `final-check.json.tree`, taken
before the runner's final tests (F1). The other sites each re-derive a fact in place: who made a record (F2: a mention of
`record: PATH` in any tool result), when a pass counts (F3: `result_time + 1s` here, an exact boundary in false-claim
scoring), which journals a count depends on (F4), what a record path may contain (F5). This round gives each fact one function
that every reader calls, the review's own direction ("centralize record provenance, dependencies and time bounds").

## The rules
1. **Tree (F1).** One scorer function `final_tree(clone)` computes `tp_tree_fingerprint` of the scratch clone at scoring time
   (after every runner step). Every final-tree check, claim standing and staleness reads only it. `final-check.json.tree` is
   not read for any verdict (it may be shown as "tree at final check" if it differs).
2. **Producer (F2).** A record's owner is where it was written: the record directory it was found in (Main's stated directory,
   or a task dir) and its own fields. A tool result that names a record path is a reference, never ownership; reading,
   catting or quoting a record changes nothing. One function `record_owner(record)`; every actor-scoped measure uses it.
3. **Time (F3).** One function `before_result(record, result)` with the exact boundary false-claim scoring already uses
   (record end <= result.md mtime, full precision, no +1 s). Staleness, claim standing and every "before the reply" check call it.
4. **Dependencies (F4).** Every measure that reads event journals (task or session) declares them through the same tracking
   the transcripts use; a missing journal it depends on makes the measure `unknown` with its reason. Request ids are
   enumerated only through that tracked reader.
5. **Paths (F5).** A record path is data: whatever follows the `record:` marker to the end of that line (or the JSON field),
   spaces included. The lost-record check works the same for `Episode Runs/...` as for a plain path.

## Tests (tests earn their place)
Extend the existing tables; no new files. Fail-before against 492650f for each of F1-F5 using the review's reproductions
(report.md): (F1) a change to the clone after final-check.json; (F2) a reviewer Read of Main's wrapper output keeps `by: main`;
(F3) result at 9.2 s, pass ending 9.5 s is stale; (F4) a missing journal naming req-b gives unknown; (F5) a lost-pointer row
with a space in the directory. Where round-8 tests duplicate FinalTreeTests/TreeIdentityTests/numbering-gap rows (review F7),
fold them into the existing table instead of adding; list folds by name. Rerun the 14 fake episodes; table: episode, round-8
verdict, round-9 verdict, cause of any change.

## Report (review F8)
Baseline count 152 -> 126; the sweep table `Site | Disposition | Evidence` (dispositions `fixed`, `already correct`,
`out of scope`, `unresolved`) covering every site the review names plus each reader of the five functions; no claim about
gate_record validation unless the code does it; tests counted as added / extended / folded / removed, by name.

## Verification (the worker tests once)
Run `tests.test_episode_harness` and `tests.test_session_env` once through `tools/agent/test-run.sh`, with `/usr/bin/python3`
outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short
`TMUX_TMPDIR` under /tmp, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then
`git reset -q`. Main runs the four cells and the full discovery. No `--live` runs; no Claude or pi start; private tmux socket
only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep, read with Read/safe_read; never read claude/mcp.json,
.env* or auth files. Do not edit config/. If a guard hook refuses a command, stop and report it; do not work around it.
Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
