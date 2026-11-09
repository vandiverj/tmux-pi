# FG-N1 round 5: the uncertainty floor covers the whole tail; splitting keeps the following words (Main, 2026-11-08)

Same lane `fg-wrappers`, step N1, same touches. Review: tasks/review-coder-8b20c9cf68-205249 (F1-F4 defects, F5 owed check).
Main reproduced F1-F3 on Python 3.9 with that review's `probe.py`; the output matches the review.

Round 4 added a bag-of-words floor for SEARCH programs, but the UNCERTAINTY check (rule 2) still ran only where the precise
option table reached. So a wrong table entry (`sudo -h HOST`) hid `su -c '$X'`. Also, splitting a string dropped the words after it.
This round makes both floors table-free.

| Finding | Cause |
|---|---|
| F1 `sudo -h localhost su -c '$X' nobody`, same with `flock /tmp/l -c` | uncertainty is checked only after precise unwrapping; the sudo table treats `-h` as help, but `-h HOST` is the host option |
| F2 `sudo -h localhost env -S 'rg --hidden' TOKEN .` | `lists` splits the string as `(word, [])` and drops `TOKEN .` |
| F3 `ssh host '$1'`, `'$@'`, `'echo $?'` | `_EXPANSION` misses positional and special parameters |
| F4 depth two is a union of partial products; 3,290 read assertions disabled (`read=None`) | generator shape |

## Rules
1. **Flattening keeps order.** The floor's word list is the command's words after the first wrapper word. Each word that a
   wrapper or a shell option hands on as text (or every word, if you cannot tell) is replaced IN PLACE by its shell
   split, recursively to the depth limit. When a search or read program is found at position k, it is judged over
   flattened[k+1:]. So `env -S 'rg --hidden' TOKEN .` is judged as `rg --hidden TOKEN .`. The same holds for
   `'/tmp/a b/rg' --hidden TOKEN .`: the program is the basename and the rest are its arguments.
2. **The uncertainty floor is table-free.** Rule 2 of round 4 (unresolved expansion, shell with no string, shell with an
   option cluster, exec-style dispatcher) is applied to the whole flattened tail of any command that starts with a
   wrapper, whatever the option table says. The table may only add sensitivity. Report every false positive this adds
   to the 41-command and 38-command probes; if any, list them for Main's ruling and do not tune them away silently.
3. **Expansions:** an unresolved expansion is `$` followed by any of: a name start, `{`, `(`, a digit, or one of
   `@ * # ? $ ! -`; also backticks. Literal `$` at the end of a word or before a space is not an expansion. Test each form.
4. **Fix the sudo table:** `-h` takes a value when another word follows it (the host option). `sudo -h` alone stays help.
   Check the other table rows for the same help-versus-valued trap; list each you changed.
5. **Generated test.** First print the full depth-two product count (outer wrapper x option form x string way x inner
   wrapper x option form x string way x payload, including `sh -cx` and the `--command STR` form) and its time on
   Python 3.9. If it is 2,000,000 cases or fewer and runs in 180 s or less there, run the full product. If not, run the
   full product over (outer wrapper x inner wrapper x payload) and cover option form and string way at both levels with
   an all-pairs design: every pair of values across the seven dimensions appears at least once. Assert the all-pairs
   coverage in the test itself. State which branch you took, with the numbers.
   **Read assertions:** a case whose expected read result cannot be computed is a missing expectation, not a skip. Enable all
   3,290. Any case you still cannot enable is listed by category with the reason, as a decision for Main, and counted.
6. **Tests for every reproducer:** all of the review's `probe.py` commands, verbatim, in ReviewReproducers, plus the
   rule 3 forms.
7. **Fail-before on the real base (F5):** a probe that runs every ReviewReproducers command from rounds 1-5 against the
   base commit's `lib/` (a detached `git worktree` at the lane's base; remove it afterwards) and against the lane. Show
   each line was missed on base and caught on the lane. Do not use an approximate rollback.

## Verification and claims
The same 12 modules as round 4, both interpreters, both modes. The 41-command false-positive list again, with main and lane
results side by side. kill_guard and network-ask PREFIXES stay out of scope. No live SSH or sudo.
