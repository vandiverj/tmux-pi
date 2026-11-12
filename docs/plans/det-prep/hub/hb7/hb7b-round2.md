# HB7b round 2 (lane hub-prune, on f273a24)

Review review-coder-5108f0e785-094722: REQUEST_CHANGES F1-F3 (read its result.md and report.md first). Main's diagnosis: all three
are code against rules the brief stated (rule 7 "neither counts", rule 9 problem counts, rule 3 "every pointer is resolved"); the
brief stands. Fix the cause the reviewer names for each, in one pass over the ledger path:

1. **F1, identity before validation.** Count (`file`, `sha`) multiplicity over every entry that has a usable `file` and `sha`,
   *before* dropping faulty entries. Any identity seen twice is a duplicate problem and no entry with it counts, whether the others
   are valid or faulty. Extend the existing FAULTS rows with a valid companion and assert no justification or evidence credit.
2. **F2, one accounting for every ledger problem.** Every listed ledger problem (load, schema, entry field/type, missing or
   unreadable file, outside population, duplicate, stale-match, git) increments the per-file count when it names a file and the
   total always. Text and JSON totals equal the number of listed ledger problems. Extend the existing fault, ledger-level and
   permission assertions with the counts.
3. **F3, pointer diagnostics apart from matching.** Resolve `reasons` and `removal_reasons` of every structurally valid entry,
   including stale, duplicate and missing-file entries; list unresolved/unchecked pointers for them; they still never count toward
   a line. Extend those matrix rows.
4. **F5 (folded):** a stale entry's text says "no current line that needs a reason has this sha" (a heading match is not a needed
   line).
5. **F6:** correct report.md's FAULTS indices (unknown by/status 15/16; outside population 18/19) and the attack-script shape counts
   (28 heading/fence shapes, 10 population-path checks). Rerun the attack script and state its new total.

Already decided (do not raise again): F4 dropped (the lane fixture helper lacks the hook/signing overrides this test needs); worker
reasons never justify; withdrawn records resolve (known limit); wrong-schema ledger refused; missing `removal_reasons` is a listed
field problem.

Tests: extend the existing method and rows; no new methods. Fail-before: run the extended rows against f273a24 in the evidence-file
form (`.py` in your evidence/ dir, `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`). Run `tests.test_friction_report` under
python3.9 and python3.14 and `tests.test_session_env tests.test_docs_wiring` once, through `tools/agent/test-run.sh` with a fresh
TMUX_TMPDIR. Do not commit. Do not add git notes.
