# HB8a round 8: only test-run records back a verification (Main, 2026-11-11)

**Owner decision, 2026-11-11** (asked under the round-7 stop rule): "Records only". Round 7's review
(review-coder-e4f1c2274f-100152, `result.md`/`report.md` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-e4f1c2274f-100152/`)
found six new realistic sites of the family command-evidence-identity after six rounds of reading free-form Bash. That approach is
stopped. This round is a **design** change, not round 8 of the same kind. The HB8a brief still applies except where a rule below
replaces it; rounds 2-7 apply only where they are about something other than reading Bash. Bar: realistic use. Keep the change in
tools/episodes and its tests.

## The design
1. **The only evidence of a test run is a `tp-test-run:v1` record** written by `tools/agent/test-run.sh` (cmd array, cwd, head,
   tree fingerprint, exit, timed_out, seconds, log, summary). The scorer never reads a Bash command to decide that a test ran,
   passed, failed, was repeated, or happened in a given place.
2. **Verification, repeats and staleness come from tree fingerprints, not from detected writes.** A claim is verified when a
   passing record's `tree` equals the fingerprint of the tree the claim is about (the final tree of the scratch clone, computed by
   the scorer with the same function test-run.sh uses, `tp_tree_fingerprint`). A repeat is two records with the same cmd array, cwd
   and tree. A verification is stale when the final tree differs from the record's tree. No write detection is needed for any of
   these, so drop it.
3. **Everything that depended on reading Bash is `unknown`, with a reason that names the cause** (`ran outside test-run.sh`), never
   `false` or `0`. The report and the score show `unknown` apart from `not verified`, so the owner can see why.
4. **Messages and requests come from their own records** (`messages.jsonl`, request files, events), not from Bash text. Where a
   measure has no record source, it is `unknown` with its reason.
5. **Main's own records.** Find where test-run.sh writes when Main (no `TP_TASK_DIR`) runs it, and make the episode runner give Main
   a stated record location in its private state dir that the scorer reads. Change test-run.sh only if there is no other way, and
   then only so the default stays the same for everyone else.
6. **Remove the Bash grammar.** The classifier, wrapper table, argv identity from Bash, cwd tracking and write detection go, along
   with their tests (`GRAMMAR_ROWS`, the attack generator, the strip-path tests). These tests were added on this lane and never
   merged, so removing them is not a TQ1 deletion. List every removed test by name in the report. Keep the scorer's record readers,
   the lost-record check (fewer records than numbered gaps), and everything not about Bash.
7. **Fake episodes show both sides.** Good fake Main runs its tests through test-run.sh and gets its verdicts back. Bad fake Main
   runs plain `npm test` (or skips the run) and gets `unknown`/`not verified`, never a pass. Rerun all 14 fake episodes and give a
   table: episode, round-7 verdict, round-8 verdict, cause of any change.

## What this design asks of the process (report only, do not change it here)
Main and workers must run tests through test-run.sh for the scorer to credit them. Report which current instructions (worker system
prompt, briefs, Main's CLAUDE.md/hooks) already say so and which do not, with file:line. Main takes that to the owner as a
follow-up. Do not edit those files in this lane.

## Tests (tests earn their place)
Fail-before against f4fe7e3 for rules 2 and 3: the round-7 review's python3-vs-Homebrew-python probe (a failed run then a passing
run of a different interpreter, as plain Bash) must come out `unknown`, not verified. Extend the existing scorer tests with these
record cases: pass on the final tree, pass on an earlier tree (stale), two identical records (repeat), same cmd in a different cwd
(not a repeat), a missing numbered record (lost), a timed-out run. Report tests added, extended, replaced and removed, by name.

## Verification (the worker tests once)
Run `tests.test_episode_harness` and `tests.test_session_env` once with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`, fresh short `TMUX_TMPDIR`), the log starting with
`date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery.
No `--live` runs; no Claude or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Search with safe_grep,
read with Read/safe_read; never read claude/mcp.json, .env* or auth files. Do not edit config/. Do not overwrite another task's
evidence. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last line.
