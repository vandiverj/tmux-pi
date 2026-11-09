# FG-N1 round 6: every spelling of a handed string reaches the table-free floors (Main, 2026-11-08)

Same lane `fg-wrappers`, step N1, same touches. Review: tasks/review-coder-d68351c236-215057, REQUEST_CHANGES with one defect, F1.
Family: remote-command-uncertainty.

## F1 (Main reproduced it on the lane, py3.9)
- `flatten(['-Srg', '--hidden', 'TOKEN', '.'])` returns the words unchanged, so `rg` is never exposed.
- `floor_uncertain('env', ['-Ssh'])` is False, while `floor_uncertain('env', ['-S', 'sh'])` is True.
- `_inside` (command_wrappers.py:244-252) only knows an attached short `c`. `handed_text` (:342-370) uses it to decide whether a value is attached,
  so for `-Ssh` it judges the following word instead of `sh`.

Today's exact `env` table row happens to catch these commands. The point of round 5 was that the floors must not depend on the table,
so this is a defect of that round.

## Rules
1. **One source for string options.** The way each text-handing wrapper takes its string is taken from its spec, the same `Spec` that
   `unwrap` uses (the `string` field, and any long form). Covered: env -S / --split-string, su, runuser, sg, flock, script, the shells' -c,
   ssh's remote text, and any other row with a string option. Every spelling that spec allows is recognised by `_inside` and `handed_text`:
   - separate: `-S STR`;
   - attached short: `-SSTR`;
   - inside a cluster, where the string option is the last letter: `-xc STR`, `-iS STR`, `-qc STR`, `-vSSTR` (only where the wrapper accepts such clusters);
   - long: `--split-string=STR`, `--split-string STR`, `--command=STR`, `--command STR`.

   Do not keep a second hand-written list of letters in `_inside`. If an option takes a value and is not a string option, it is not
   treated as one.
2. **Wrong-table invariant, generated.** Extend the existing wrong-table class (tests/test_wrapper_lists.py:339-351). Do not add an
   unrelated suite. The test covers every text-handing wrapper × every spelling in rule 1 × the payloads: an unresolved expansion
   (`$X`, `$1`, `$@`, `$(cmd)`, backticks), a bare shell (`sh`, `bash -s`), and a search program (`rg --hidden TOKEN .`, `git grep foo`).
   With `unwrap` made to fail, or the wrapper's table row emptied, `floor_uncertain` or the bag floor must still flag each case. Print
   the count and assert it is the full product (no sampling).
3. **The review's reproducers verbatim:** `env -Ssh`, `env -Srg --hidden TOKEN .`, and the probe.py beside the review. Add them to
   ReviewReproducers, along with one attached-form case for each other text-handing wrapper.
4. **Fail-before** against round 5's tree: the lane's HEAD plus the uncommitted round-5 files as reviewed. Save that copy before you
   edit and load it in memory, like round 5's failbefore.py. Show that each new reproducer is missed before and caught after. Also re-run round 5's 25 against
   base `dbb101c` and the lane.
5. **No new false positives.** Re-run the 41-command list (fp41) and Main's 38-command list. Main's probe2.py is in the round-5 task's
   main-evidence directory: run `python3 probe2.py ROOT TREE` with ROOT = main and = the lane. Show identical output. List any
   difference for Main's ruling.

## Verification and claims
- The same 13 modules on both interpreters and in both modes.
- The STRAY_DEPTH_ONE count may change only if you say why.
- kill_guard and network-ask PREFIXES stay out of scope. No live SSH or sudo.
- List every script you wrote and ran, by its path in the task directory.
