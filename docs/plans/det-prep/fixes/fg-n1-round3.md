# FG-N1 round 3 (review-coder-50e09c1158-143214: REQUEST_CHANGES)

Same lane `fg-wrappers`, same touches. Read that review's result.md and report.md. Two rounds have fixed the exact
reproducers and missed the family. This round fixes the method, not the cases.

1. **No "first wrapper-looking word" heuristic (F1).** Unwrapping a plain wrapper (`env`, `timeout`, `sudo`, `nice`, ...)
   uses that wrapper's real option table, including which options take a value (`env -u NAME`, `env -C DIR`,
   `sudo -u USER`, `sudo -n` takes none, `timeout -s SIG`, `nice -n N`). Give each wrapper its own row in
   lib/command_wrappers.py; remove the shared `_WRAPPER_VALUES` guess. If a word cannot be classified (unknown option),
   fail closed: treat the command as a search (search side) and sensitive (read side). This also closes the `sudo -n`
   gap the worker reported; include it.
2. **String forms:** `-cSTR`, `-c'STR'`, `--command=STR`, `--command STR`, for every wrapper that takes a command
   string, split with the shell tokenizer (spaces, tabs, newlines).
3. **Provenance through nesting (F2).** A line handed on by a remote/string wrapper (ssh, su -c, flock -c, script -c,
   sg, runuser -c) stays "wrapper-supplied" through any nested `sh -c` / `bash -c` / `zsh -c` / `eval` inside it, to the
   existing depth limit. An unresolved expansion anywhere in that chain fails closed. A user's own top-level
   `sh -c '$X'` keeps its current behaviour.
4. **Generated test (the proof).** In tests/test_wrapper_lists.py, build the cases from tables rather than listing
   them: every wrapper in the table x its option forms (none, flag, valued option, `--`) x stack depth 1-3 (any wrapper
   inside any) x payload (direct `rg --hidden TOKEN .`, each string form, nested `sh -c`, unresolved `$VAR`). Every
   case must be a search and sensitive on both detectors; a matching set with a harmless payload (`echo hi`, no
   expansion) must not be. Report the case count. Include both reviewers' reproducers verbatim.
5. F3 follow-ups (kill_guard, network-ask PREFIXES) stay out of scope.
6. Same verification: the 12 modules, both interpreters, both modes. Show the new generated test failing on the
   round-2 tree first.
