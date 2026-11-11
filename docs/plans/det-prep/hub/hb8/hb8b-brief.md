# HB8b: record dependencies reach every reader; raw stamps stay raw (Main, 2026-11-11)

Source: `hb8b-known-limits.md` (this dir) K1-K3 and F5, from review-coder-a38acc0415-134517 (`result.md`, `report.md` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-a38acc0415-134517/`).
HB8a's brief and rounds 2-9 (this dir) describe the scorer; records-only stays. Bar: realistic use. Lane hub-episodes2, base
main. Route rule-first.

## The rule (one dependency mechanism, K1 and K2)
Every value the scorer derives carries the set of sources it was derived from (transcripts, event journals, request journals,
record files). A derived source (the record inventory from transcripts' `record:` pointers; request ids from journals) inherits
the sources it was read from. A measure is `unknown`, with the missing source named, whenever any source in its closure is
missing or unreadable. There is one tracking function; `record_pointers` and `request_ids` go through it; no reader enumerates
pointers or ids around it. K1: deleting a reviewer's transcript that pointed at a missing record makes verification unknown,
not verified. K2: a lost journal behind an event naming req-b keeps messages-into-prompt unknown, never 0.

## Time (K3)
Keep the raw stamp of each observation apart from any poll clamp. `before_result` compares raw stamps at full precision; a
clamp may only widen an unknown, never move a stamp. Result at 9.2, pass ending 9.25 observed at clamp 9.3: the pass is after the
result (stale 1).

## Attack (report artifact; oracle from this text first)
{transcript, event journal, request journal, record file, final-check.json} x {present, missing, unreadable, empty} x every
measure that reads it (list them from the code). Expected: unknown iff a source in the closure is missing/unreadable; empty
behaves as present-with-nothing. Times: result/pass stamps at equal, 0.05 s apart either way, and with clamps on each side.

## Tests (tests earn their place)
Extend the existing tables (no new files): K1, K2, K3 reproductions above, fail-before on main. Rerun the 14 fake episodes;
table: episode, before, after, cause of any change. F5: correct the sweep claim (comparison reads `COMPARE`).

## Verification (the worker tests once)
`tests.test_episode_harness` and `tests.test_session_env` once through `tools/agent/test-run.sh`, `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), fresh short `TMUX_TMPDIR` under /tmp,
log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`. No `--live` runs; no Claude
or pi start; private tmux socket only; nothing to 127.0.0.1:1234 or any network. Edit only tools/episodes/ and
tests/test_episode_harness.py (and docs for the scorer). Search with safe_grep, read with Read/safe_read; never read
claude/mcp.json, .env* or auth files. Do not edit config/. **Guard refusals:** if a guard hook refuses a shell or inline-script
edit, make the same edit with the Edit/Write tool and list it in the report; any other refusal: stop and report. Approve no
prompt. Commit nothing. No background shells. Report: sweep table `Site | Disposition | Evidence`; tests by name.
`RESULT_STATUS: complete` as the last line.
