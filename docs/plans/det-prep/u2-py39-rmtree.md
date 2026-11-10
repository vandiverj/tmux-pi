# U2 (PY39-RMTREE): test_ctx_nav_api on Python 3.9

Lane `py39-rmtree`, step U2. Level: `mid` coder. Base: `main` (073e541). Touches: `lib/ctxengine/graph.py`,
`tests/test_ctx_nav_api.py` (only 3.9 compatibility of the test itself), `tests/test_remove_own_folder.py` (new),
`docs/plans/det-prep/baseline-failing.txt` (only lines this lane fixes).

## Why
Found after U1 (2026-11-08). Under `/usr/bin/python3` (3.9.6) test_ctx_nav_api has 358 failing tests (all in the
baseline). 347 of them are one error: `TypeError: rmtree() got an unexpected keyword argument 'dir_fd'` from
`remove_own_folder` (`lib/ctxengine/graph.py:1398-1411`). `shutil.rmtree(..., dir_fd=)` exists only from Python 3.11.
Main's logs of the run on the U1 lane, both modes, are in
`.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-b41c9f8728/main-evidence/{out,in}.test_ctx_nav_api.log`.

`remove_own_folder` is a security boundary: it removes a folder `make_own_folder` made, relative to a descriptor on
the parent opened with no link on the way, so a parent or child swapped for a symlink is never followed. The 3.9 path
must keep exactly that guarantee. Today on 3.9 the folder is never removed: the TypeError is raised, not ignored.

## Change
1. Reproduce first: show the TypeError under `/usr/bin/python3` with one small direct call; save `before.log`.
2. In `remove_own_folder`, keep `shutil.rmtree(name, dir_fd=fd, ignore_errors=True)` where it is supported
   (`shutil.rmtree.avoids_symlink_attacks` and Python >= 3.11, or feature-detect `dir_fd` support), and otherwise use
   a small descriptor-relative removal: open each child folder with `os.open(name, DIR_FLAGS | os.O_NOFOLLOW,
   dir_fd=parent_fd)`, list it with `os.scandir(fd)`, `os.unlink(entry, dir_fd=fd)` for anything that is not a real
   folder (symlinks are unlinked, never followed), recurse into real folders, then `os.rmdir(name, dir_fd=parent_fd)`.
   Errors ignored, as now. No path-string join anywhere in the fallback. Bounded depth or iteration, not unbounded
   recursion on a hostile tree (say which and why).
3. New `tests/test_remove_own_folder.py` (import `session_env` first): runs on whichever interpreter runs the tests and
   checks (a) a nested tree with files is removed; (b) a symlink inside the folder pointing outside is removed as a link
   and its target (a folder with a file) survives; (c) a parent swapped for a symlink after `make_own_folder` is not
   removed through; (d) the fallback path itself is exercised on every interpreter (call it directly or force it), so
   3.14 runs cover it too. It must fail on main before the fix under 3.9 (show it).
4. Investigate the other 11 failures (see the logs): `assertNoLogs` (3.10+) in the test, a NotADirectoryError on
   `scratch`, one `Lists differ ... != []` open-list assertion. Fix the test-only 3.9 incompatibilities in
   test_ctx_nav_api; for anything that is a lib behaviour difference, report it with the cause, do not fix it here.
5. Run test_ctx_nav_api (slow: ~16 minutes per mode on 3.9; give it a 2400s bound) plus the new test and any module that
   imports graph's folder helpers, in both modes (session keys unset, and dummy `TMUX`/`TP_*` values), under
   `/usr/bin/python3` and the newest python3. Remove from `baseline-failing.txt` only the lines your fix makes pass,
   and list them.

Out of scope: behaviour changes outside `remove_own_folder`; any other module's baseline lines.

## Done means
1. `remove_own_folder` removes the folder on 3.9 and on 3.14, with the link guarantees of (b) and (c) shown by tests.
2. The new test fails before the fix (3.9) and passes after, both modes, both interpreters.
3. test_ctx_nav_api on 3.9: the failures left are listed with their cause; nothing outside the baseline.
