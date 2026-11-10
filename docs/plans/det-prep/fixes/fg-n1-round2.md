# FG-N1 round 2 (review-coder-443775e4e5-140238: REQUEST_CHANGES)

Same lane `fg-wrappers`, same touches (lib/command_wrappers.py accepted). Read the review result.md and report.md.

Fix:
1. **F1 `--`.** A wrapper's `--` ends its options; the operands after it (flock's lock file, chroot's dir, ssh's host)
   are still consumed, then the command starts. `flock -- lock rg --hidden TOKEN .`, `chroot -- / rg ...`,
   `ssh -- host rg ...` must be sensitive on both detectors. Test every wrapper with `--` where it applies.
2. **F2 stacked wrappers and string forms.** Unwrap repeatedly (wrapper inside wrapper, up to the existing nesting
   limit), and accept `--command=STR` / `-cSTR` long and attached forms. A command string is split with the same shell
   tokenizer as `sh -c` (tabs and newlines are separators), not a literal-space split. Add the reviewer's
   `setsid su --command='grep<TAB>-r<TAB>TOKEN<TAB>.' nobody` reproducer plus a 3-deep stack.
3. **F3 fail closed on unknown remote/string commands.** If an ssh remote command or a `-c` string contains an
   expansion the detector cannot resolve (`$VAR`, `$(...)`, backticks, `eval`), treat it as sensitive on the read side
   and as a search on the search side (fail closed), as the brief required. Test it.
4. F4: no change (follow-ups). F5 is Main's (the 723 count was 12 modules, Main's evidence listed 10).
5. Same verification: the 10 modules plus test_orchestration and test_route_resolve, both interpreters, both modes.
