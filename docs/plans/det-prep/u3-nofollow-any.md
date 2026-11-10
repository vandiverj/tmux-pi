# U3 (NOFOLLOW-ANY): macOS link guard is weaker under Python 3.9

Lane `nofollow-any`, step U3. Level: `mid` coder. Base: `main` (after U2). Touches: `lib/ctxengine/graph.py`,
`tests/test_ctx_nav_api.py` (only if a test needs it), `tests/test_nofollow_any.py` (new),
`docs/plans/det-prep/baseline-failing.txt` (only lines this lane fixes).

## Why
Found by U2 (coder-e0e9e46dea, 2026-11-08), corrected by Main. `lib/ctxengine/graph.py:490`:
`NOFOLLOW_ANY = getattr(os, "O_NOFOLLOW_ANY", 0) or os.O_NOFOLLOW`. Apple's `/usr/bin/python3` 3.9.6 has no
`os.O_NOFOLLOW_ANY` (Homebrew 3.14.5 has it, value `0x20000000`, the same as `O_NOFOLLOW_ANY` in the macOS SDK's
`sys/fcntl.h`, supported by the kernel since macOS 11). So under 3.9 on macOS a folder on the way swapped for a link is
followed where the module's design says it is refused (ELOOP); only the last name is held. `python3` on PATH on the owner's
machines is that 3.9.6. Two baseline tests show it on 3.9 only:
`PathRuleCase.test_r8_own_scratch_folders_are_made_where_the_parent_was_judged_never_through_a_link` (NotADirectoryError)
and `PathRuleCase.test_r8_a_folder_on_the_way_swapped_for_a_link_is_seen_by_the_look_and_the_read`.

## Change
1. Reproduce first on `/usr/bin/python3`: run the two r8 tests and a direct probe (open `a/link/b` where `link` is a
   symlink to a folder, with `NOFOLLOW_ANY`); save `before.log`. Run the same on `/opt/homebrew/bin/python3`.
2. On macOS (`sys.platform == "darwin"`) when `os` has no `O_NOFOLLOW_ANY`, use `0x20000000`. Prove at import or in a
   test that the kernel honours it (an open through an intermediate link fails with ELOOP); if the kernel rejects the flag
   (EINVAL), fall back as today and say so. Linux and other systems unchanged.
3. Check every other place the "no link on the way" guarantee depends on `NOFOLLOW_ANY` (grep the module and
   `lib/ctxengine/nav_api.py`), and `make_own_folder`'s error mapping (ENOTDIR vs ELOOP). Report; fix only what the
   constant change requires.
4. New `tests/test_nofollow_any.py` (import `session_env` first): on darwin, `graph.NOFOLLOW_ANY` refuses an intermediate
   link on every interpreter; it must fail on main under 3.9 before the fix (show it).
5. Run test_ctx_nav_api, the new test and the ctx modules (test_ctx_calls, cli, nav_web, store, surface, tools_wiring),
   both modes (session keys unset, dummy `TMUX`/`TP_*`), under `/usr/bin/python3` and `/opt/homebrew/bin/python3`.
   Remove from `baseline-failing.txt` only the lines this fixes, and list them.

## Done means
1. On macOS, `NOFOLLOW_ANY` refuses an intermediate link under 3.9 and 3.14, shown by the new test.
2. The two r8 tests pass on 3.9 (or each remaining failure is explained with its cause).
3. No new failure outside the baseline, on either interpreter, in either mode.
