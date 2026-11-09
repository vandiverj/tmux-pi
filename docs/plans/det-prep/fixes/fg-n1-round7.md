# FG-N1 round 7: every reading of a string option, read-side expectations, sudo shell mode (Main, 2026-11-08)

Same lane `fg-wrappers`, step N1, same touches. Review: tasks/review-coder-acf3f2f10c-223756, REQUEST_CHANGES with defects F1 and F2,
note F3 (Main's deferred sudo gap), and an owed check F4. Family: remote-command-uncertainty.

## F1 (Main reproduced it on the lane, py3.9: `fgn1r7probe.py` / `fgn1r7probe.txt` in this task's main-evidence)
With `unwrap` returning None, each of these reports search=False. The table catches them; round 5 caught them too:
- `su -c'rg --hidden TOKEN . -Sls' nobody`
- `su -c'sh -Sls' nobody`
- `flock L -c'rg --hidden TOKEN . -Sls'`
- `script -qc'rg --hidden TOKEN . -Sls' /dev/null`

`inside` (command_wrappers.py:268-277) returns the first row's reading of a word. When several rows read the same word differently
(su's `-c` text vs env's `-S` text), the first one hides the others.

## Rules
1. **Every reading, not the first.** When a word is a string option for more than one row, the floors look at every reading (the
   union). A reading may add a look; it never removes one. No row order may change a result. Write this as a property: for any word,
   the bag and the uncertainty with every row's reading are a superset of those with any single row's reading.
2. **Generated competing-readings test.** Extend the wrong-table class. Take every pair of rows with string options × each row's
   spelling (rule 1 of round 6) × the payloads, with the other row's option letters placed inside or after the payload (`-Sls`, `-cls`,
   `--command=ls`). With `unwrap` made to fail, every case must still be flagged. Print the count, and assert it is the full product.
3. **F2: no omitted assertions.** Remove the `git grep foo` read skip (test_wrapper_lists.py:452-455). For every cell, compute the
   expected read result independently: the read result of the same command with the correct table. Assert that a wrong table never
   turns a True into a False. Print the True/False counts. Explain any cell where a wrong table gives True and the correct table gives
   False (the review counted 972 True, 1308 False), or list it for Main's ruling.
4. **F3: sudo shell mode.** `sudo -s ARGS`, `sudo --shell ARGS`, `sudo -i ARGS`, `sudo --login ARGS`, and clusters (`-us`, `-Es`):
   the arguments are a command line run by a shell. Today `sudo -s "$X"`, `sudo --shell "$X"` and `sudo -i "$X"` give sensitive=False.
   Treat those arguments as a handed string from the spec (round 6 rule 1), so unresolved expansions are uncertain and a search
   program is exposed, nested too (`sudo -h localhost sudo -s "$X"`). `sudo -s` and `sudo -i` with no arguments stay as they are today.
   Add them to the round 6 generated product and to ReviewReproducers.
5. **Fail-before** against round 6's tree: lane commit `019e07e` (Main checkpointed rounds 1-6 as reviewed). Save that copy before you edit,
   and load it in memory. Show that each F1, F3 and rule-2 case is missed before and caught after.
6. **No new false positives.** Re-run fp41, Main's 38-command list, and probe2.py with ROOT = main and = the lane (as in round 6).
   Show the output and list any difference for Main's ruling. `sudo sort -Srg f` stays as already ruled.
7. **F4: the socket warning.** Run the module that prints the unclosed-socket ResourceWarning on py3.14 against base `dbb101c`
   and against main, and show that the warning is there before this lane. If it is not, say so.

## Verification and claims
- The same 13 modules on both interpreters and in both modes.
- The STRAY_DEPTH_ONE count may change only if you say why.
- kill_guard and network-ask PREFIXES stay out of scope. No live SSH or sudo. Do not create or read any `.env` file outside a temp
  directory your own test makes.
- List every script you wrote and ran, by its path in the task directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).
