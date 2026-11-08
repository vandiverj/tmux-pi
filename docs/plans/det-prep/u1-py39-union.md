# U1 (PY39-UNION): lib modules that do not import on Python 3.9

Lane `py39-union`, step 1. Level: `mid` coder. Base: `lane/integrate` (0cb5400). Touches: `lib/ctxengine/calls.py`,
`lib/ctxengine/surface.py`, `lib/localdocs/lock.py`, `lib/localdocs/calls.py`, any further site the sweep finds,
`tests/test_lib_imports_py39.py` (new), `docs/plans/det-prep/baseline-failing.txt` (only lines this lane fixes).

## Why
Found during T1 round 3 (2026-11-08). The owner's machines run tests with `/usr/bin/python3` (3.9.6). Dataclass fields
annotated `X | None` are evaluated at class creation and raise `TypeError` on 3.9. Main's scan found 9 lib modules that
fail to import, all through two roots:
- `lib/ctxengine/calls.py:55-56` (`index_age_s: int | None`, `reason: str | None`)
- `lib/localdocs/lock.py:23-25`
Further sites that would fail once the roots are fixed: `lib/ctxengine/surface.py:472`, `lib/localdocs/calls.py:18-20`.
None of these files has `from __future__ import annotations`. Test modules importing them by dotted name crash with no
`Ran` line (they appear in the baseline as `_FailedTest`).

## Change
1. Reproduce first: under `/usr/bin/python3`, import every module under `lib/` (package-qualified, with `lib` on the
   path the way the tests set it up) and record which fail and why. Save the log as `before.log` in the task dir.
2. Fix every failing site with `from __future__ import annotations` at the top of the file (preferred; check nothing in
   that file reads `__annotations__` or `dataclasses.fields(...).type` at runtime expecting a real type — if one does,
   use `Optional[X]` there instead and say why).
3. Sweep: every `|` used in an annotation, `isinstance`, or `typing` call anywhere under `lib/` and `bin/` (Python files,
   including extensionless scripts with a python shebang). One row per site: file:line, evaluated at runtime on 3.9 yes/no,
   fixed / not needed.
4. New `tests/test_lib_imports_py39.py`: imports every `lib` module in a subprocess under the interpreter running the
   tests, and fails naming each module that does not import. It must fail on `lane/integrate` before the fix (show it).
5. Run the test modules that import the fixed packages (find them with grep) in both modes (session keys unset, and
   dummy `TMUX`/`TP_*` values). Remove from `baseline-failing.txt` only the lines your fix makes pass, and list them.

Out of scope: `tests/test_retry_packet.py` (f-string backslash, owned by the tests lane); behaviour changes.

## Done means
1. Every `lib` module imports under `/usr/bin/python3` 3.9.6 and under the newest python3 on PATH.
2. The new test fails before the fix and passes after, in both modes.
3. Sweep table complete; baseline lines removed are each shown passing.
