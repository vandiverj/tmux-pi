# LS1b round 2 (Main, 2026-11-09)

Main checked the LS1b worktree in four cells while the machine was loaded (load average about 16).
`test_rule_20_a_megabyte_of_bracket_items_on_one_line_is_read_in_linear_time` failed in py3.9 with the session env set:
- `cpu_bound` (`tests/test_qual_summarize.py:2030`) arms `ITIMER_PROF` for 2 CPU seconds;
- under load the 1 MB check went over it.

The margin was already known to be thin: the worst shape takes 1.66–1.73 s. CPU seconds on this Mac grow under load, through
efficiency cores and clock changes. So the pass/fail depends on the machine's state. That is not deterministic, and it
also makes `test_qual_summarize.py` go over the 60 s per-file budget on py3.9 (61 to 70 s).

## Fix
1. **The rule-20 linear-time tests (every `cpu_bound` and `megabyte` user) must not pass or fail on time.** Bound the work
   deterministically instead. For example, count the checker's basic steps with a counter, kept off by default and
   enabled only in tests, and assert the count is at most c × input length, with c fixed from the current code. Or use
   another measure that is the same on every run and every machine.
   - The same 1 MB inputs, every shape, must still be covered.
   - Keep a generous wall-clock backstop only against hangs: 30 s or more, never the pass criterion.
   - Do not change what the checker returns for any input. The existing differential and oracle tests must stay green.
2. **Fail-before.** The new bound must fail on the quadratic code at commit `8560cf6` (LS1 round 13) for the nested family.
   Run it against a copy of that commit's `tools/qual/summarize_check.py` and record the counts. Also show at least one
   other known super-linear shape failing, if the history has one.
3. **Load proof.** Run the rule-20 tests alone with 12 busy-loop CPU burners in the background. Bound the burners with a
   timeout and kill them afterwards. The tests must pass with the same counts as when idle. Record both runs.
4. **Budget.** `tests/test_qual_summarize.py` must be under 60 s in all four cells. If the deterministic bound makes the
   megabyte inputs cheaper, use that; otherwise split slow parts into a second test file. Do not drop any case.

Everything else from ls1b-real-split.md stands, and so does the rest of your round 1 work.

## Done means
- four cells green, each file under 60 s;
- the fail-before and load-proof numbers in the report;
- the scripts you wrote and ran are listed;
- `RESULT_STATUS` is reported.

No tmux, no live model, never read `.env*`.
