# LS1c S1c round 15: a file is clean only if every outside callable it reaches is on an allow-list (Main, 2026-11-10)

Review review-coder-009bf65abf-003741 (REQUEST_CHANGES), F1 (family `binding-ambiguity`, fourteenth round). Report, `repro.py`
and both interpreters' logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-009bf65abf-003741/`.
Run `repro.py` on both interpreters first (144 false credits and 15 false absence claims on each). Round 14's change (`_runs_clean`
uses `_opaque`; a star import is opaque), the start set, the binding enumeration, the guard counts (12/8/0), the real probe (0) and
the three use-lrucache lines were confirmed; keep them.

## The defect
- **F1:** a visited helper runs `pydoc.locate('helperpkg.redirect')`. That loads a supplied module that redirects `pkg.__path__`, but
  no import statement names it, so neither walk visits it: false credit for U, and a false "does not use Q" when the hidden module
  creates Q. `_DYNAMIC_ACCESS` is a deny-list of words; `pydoc.locate` is not on it, nor are `unittest.TestLoader().loadTestsFromName`,
  `unittest.mock.patch`, `logging.config.dictConfig`, `pkg_resources`, `importlib.resources`-style helpers, and many more.

## Why the rounds keep coming
Every round since 10 has added one more way to load a module the import list does not show, and the fix each time named that way.
A deny-list of loaders cannot be complete: any outside callable may import by string. The rule has to be inverted.

## Main's decision
1. **Allow-list, not deny-list.** A file is opaque (`_opaque`) unless every reference it makes to a module outside the run root (a
   name bound by `import m` / `import m as x` / `from m import f` where m is not a supplied or discovered module) is one of:
   - a call whose callee resolves, through those import bindings, to a qualified name on `_SAFE_CALLABLES` (for example
     `re.compile`, `logging.getLogger`, `functools.lru_cache`, `functools.wraps`, `dataclasses.dataclass`, `dataclasses.field`,
     `typing.TypeVar`, `collections.namedtuple`, `os.path.join`, `os.environ.get`), or
   - an attribute read of a qualified name on `_SAFE_VALUES` (constants and classes used only as annotations or bases, such as
     `typing.Any`, `typing.Optional`, `enum.Enum`, `abc.ABC`), or a decorator / base class on one of the two lists.
   Any other use of such a name (a call to anything not listed, passing the module or a function from it as a value, aliasing it,
   subscripting it into a call) makes the file opaque. This holds at every scope of the file, as the current word test does.
   Calls on objects returned by a listed callable, on run-root objects, and on local values are not outside references and are not
   restricted (the run-root code they reach is walked, as today).
2. **Every list entry is proved by execution, not by reading.** For each entry, an attack row calls it under a `sys.meta_path`
   recorder (and with `sys.modules` snapshotted) with arguments that name a supplied module (string, bytes, a dotted path, a
   `module:attr` form, a path); the row passes only if no supplied module is imported or looked up. An entry that cannot be called
   that way, or that imports, is not listed. Keep the lists short: list only what the guard files, the real probe files and the
   lrucache cases need, and justify each by those uses.
3. **The deny-list words stay** (`_DYNAMIC_ACCESS`, `_STATE_WORDS_RE`); the allow-list is added to them in `_opaque`. Builtins are not
   module references; the builtins that load by string (`__import__`, `eval`, `exec`, `compile`, `getattr`, ...) are already words.
   If you find a builtin that loads a module by string and is not a word, add it to the words and list it.
4. One per-file test, as round 14: both walks use `_opaque`; no traversal gets a narrower test. Do not add string edges to win lines
   back (round 12's rule: do not loosen to recover lines).

## Attack the rule before you report
- The reviewer's 144 credit and 15 absence cases are rows.
- Loader axis: every way you can find in the standard library of both interpreters to load a module named by a run-time value, found
  by code (search the stdlib sources for calls to `__import__`, `import_module`, `find_spec`, `exec_module`, `load_module`,
  `pkgutil`, `runpy`, `pydoc.locate`-style resolvers, `pickle` loads, entry-point loading), at least 30 distinct callables, each
  printed with where it was found × the round-13/14 locations (U, each initializer on U's and T's path, T, a helper imported only by
  an initializer, a helper two deep, an unrelated file) × class/function targets, plus the absence variant. Execute each case (`-I`,
  `sys.path[0]` = case root). Expected: unsound 0 for every row on both interpreters.
- List rows: every `_SAFE_CALLABLES` / `_SAFE_VALUES` entry run under the import recorder as in decision 2.
- Report unsound and mismatch counts on both interpreters at 493bc23 (before) and after.

## Report
- Fail-before against 493bc23 for each new test.
- Guard per-split counts and the real probe count against 493bc23, every lost line listed with the outside reference that made its
  file opaque. If any use-lrucache line is lost, stop and report it with the reference that caused it.
- The seven modules in all four cells, one after another, as round 14; each log starting with `date -u` and
  `git add -A && git write-tree`; each file's time per cell.
- The sweep: every place that decides whether a file's code may load something its imports do not show, by code.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script.
