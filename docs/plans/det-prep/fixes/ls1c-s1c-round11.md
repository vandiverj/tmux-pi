# LS1c S1c round 11: a module name is credited to a file only when Python's finder must load that file (Main, 2026-11-09)

Review review-coder-56e30e3898-205916 (REQUEST_CHANGES), F1 (family `binding-ambiguity`, tenth round). Repro and report:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-56e30e3898-205916/`.
Run its repro on both interpreters first.

With `pkg/t.py` (defines `class Q`) and `pkg/t/__init__.py` (`Q = lambda: "other"`) both present, every import form, including
`from pkg.t import Q`, is credited to `pkg/t.py`. Python loads the package directory and returns `"other"`.

## Cause, and why the rounds keep coming
`_module_name` maps a path to a dotted name, and the scan assumes that mapping runs backwards: dotted name → that one file.
Python decides the other way round. For each step of a dotted name its path finder checks, in each directory, a package
directory first, then module files by loader suffix (extension modules, then `.py`, then `.pyc`), then a namespace portion; and
a name already in `sys.modules` (builtins, frozen and already-imported standard modules) never reaches the finder. Rounds 7-10
each closed one place where the backward mapping fails. This round closes the mapping as a whole.

## Main's decision: one rule for module identity
- **Rule:** a dotted module name is attributed to a file `F` (as the target's definer, or as a link holder in `_path_proved`)
  only when `F` is the only thing Python's finder can load for that name from the run root. Prove it per step of the name:
  for the directory of that step, no other candidate exists: no `name/` package directory when `F` is `name.py`, no `name.py`
  or `name.<ext>` when `F` is `name/__init__.py`, no `name` with any other import suffix
  (`importlib.machinery.all_suffixes()` on the running interpreter), and no `name/` directory that would make a namespace
  portion compete. The top-level name of the module must not be a builtin or standard-library module name
  (`sys.builtin_module_names`, `sys.stdlib_module_names` where it exists, a fixed list on 3.9).
  When the proof fails, the target is not credited through that module: no creation, call, use or "does not use" lines
  through it (the import line stays a fact where it is today).
- **Where candidates come from:** the supplied files, and, when the run root exists on disk, a listing of each directory on
  the name's path in the run root (names only, nothing read). State in the docstring that a run with no run root on disk sees
  only supplied files, and that the limit is the run root as `sys.path[0]`; another `sys.path` entry or a `.pth` file is
  beyond static proof and stated once, beside the round-9 limit.
- One function owns module identity, and every place that turns a dotted name into a file calls it: definer lookup in
  `use_scan`, the import graph, `_path_links` / `_holder_binds_other` / `_path_proved`. Nothing else may map a name to a file.
- Keep: `_DYNAMIC_ACCESS`, the absence-line wording, rounds 8-10 rules, the use-lrucache lines.

## Attack the rule before you report
Input space, printed by code: the target's module position (top-level, one package deep, two deep) × each competing candidate
at each step (none; `name/` package dir; `name.py`; `name.<ext>` for each suffix of `all_suffixes()`; `name/` without
`__init__`; a stdlib or builtin top-level name such as `json`, `os`, `sys`) × the import form (`import a.b.c`, `as`,
`from a.b import c`, `from a.b.c import T`, relative forms) × class and function targets. For each case run `use_scan`, then
execute it (`-I`, `sys.path[0]` = the case root) and compare. Report mismatch and unsound counts on both interpreters before
and after. Rows whose execution needs a compiled extension may assert the scan side only (say which). Keep the rows worth
keeping in the existing table; fold the reviewer's repro in.

## Report
- Fail-before against the round-10 checkpoint fb4b09c.
- Guard per-split counts and the probe count against fb4b09c, every lost line listed. If any use-lrucache line is lost, stop
  and report it.
- The seven modules (`tests.test_qual_summarize_code_facts tests.test_qual_summarize tests.test_qual_summarize_oracle
  tests.test_qual_summarize_structured tests.test_qual_summarize_structured_v2 tests.test_session_env
  tests.test_qual_summarize_meaning`) in all four cells after your last edit, each file's time per cell.
- F2: for every run you cite, its log file with a start timestamp and the tree hash it ran on, taken before and after. Keep
  them in evidence/; claims without a record are labelled unverified.
- Sweep table for `binding-ambiguity` with current line numbers, including every function that maps a name to a file.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running.
