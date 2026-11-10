# LS1c S1c round 18: proved import order or opaque (design route) (Main, 2026-11-10)

Review review-coder-998aeaaa51-035059 (REQUEST_CHANGES): F1 and F2 (family `binding-ambiguity`, seventeenth round), F3 and F4
(report). The report, `repro.py`, `repro-submodule.py` and both interpreters' logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-998aeaaa51-035059/`.
Run both repros on both interpreters first and record each failing case.

Keep what the review confirmed from round 17:
- the one resolver and `_opaque` as the one per-file test;
- `_spellings` and the 16 allow-list entries;
- guard 9/3/0 and probe 0; the full-files and use-lrucache lines;
- the round-17 self/cycle, import and spelling attacks at 0 unsound.

## Why this round changes the design
Rounds 1-17 each closed one more path by which the scan credits a name whose binding depends on import order. The family keeps
recurring because the resolver tries to reason about partly run modules site by site. Round 18 replaces that with two whole-graph rules,
decided once before any name is resolved.

## Main's decision
1. **Import cycles make every module in them opaque.** Build the module import graph of the run's supplied modules from every import
   statement of any form (`import a`, `import a.b`, `import a as b`, `from a import b`, relative forms at every level, imports inside
   functions or conditionals included). A package's `__init__` counts as imported by any import of a submodule. Every module in a
   strongly connected component of size above one, or with a self-edge, is opaque, and so is every file that reads a name such a module
   binds. This replaces the running-file tracking from round 17 if it is no longer needed. Remove it then; do not keep both.
2. **A submodule is an attribute only when an import loads it.** `pkg.sub` read as an attribute (after `import pkg`, through an alias, in
   a namespace package, or inside `pkg/__init__.py` itself) resolves to the submodule only when one of these holds:
   - an import in the reading file loads it before the read (`import pkg.sub`, `from pkg import sub`, `from . import sub`);
   - `pkg/__init__.py` loads it (`from . import sub`, `import pkg.sub`) and the graph is acyclic for it (rule 1).

   Otherwise the name is unknown and the reading file is opaque. File existence alone proves only that `from pkg import sub` can import
   the child, which stays as today.
3. Both rules live in the graph build and `_resolve` and its helpers. No new per-file test and no special case to win lines back.
4. Sweep every place that makes or reads a module reference or follows an import edge, and list each with its line. Say which round-1
   to round-17 sites the two rules now cover, so the reviewer can see the family is closed by rule, not by site.

## Attack the rules before you report
Build each input space by code, print it, and run it at c8f13c7 (before) and after, on both interpreters:
- **Rule 1:** every import form above as the edge type, × cycles of length 1 to 4 (self-import included) × back-read of the same name,
  another name or a submodule × class and function targets × the four package locations of round 17 × entry from a plain module, a
  package `__init__` or a third importer. Include acyclic controls of each shape.
- **Rule 2:** every way to reach `pkg.sub` as an attribute (plain, aliased, chained `a.b.c`, namespace package, inside the initializer)
  × loaded by the reader / by `__init__` / by a third module only / not at all × before and after the read.
- Rerun the round-17 self/cycle, import-form and spelling attacks and the reviewer's two repros.

Execute every row (`-I`, `sys.path[0]` = case root) with credit and absence variants. Expected: unsound 0. Report every line lost or
won against c8f13c7, by cause. Do not add refusal rules beyond the two above to pass this.

## F3 and F4 (report)
- F3: the round-17 report said no attack hit a runtime error. Its logs record 2,338 runtime errors per interpreter (mostly intended
  failing inputs) and 8 `resolve_member_error` cases outside `resolve_unsound`. Report both counts this round, for every attack, and say
  what each is.
- F4: start every matrix log by printing `TMUX_TMPDIR`, as Main's verify logs do.

## Report
- Fail-before against c8f13c7 for each new test.
- Guard per-split counts and the real probe count against c8f13c7. List every lost line with the reference that made its file opaque.
  If any use-lrucache line is lost, or a full-files row loses a line, stop and report it.
- The seven modules (tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
  tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2, tests.test_session_env,
  tests.test_qual_summarize_meaning) in all four cells, run one after another:
  - each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`;
  - record each file's time per cell;
  - run `git reset -q` afterwards, so nothing stays staged.
- Commit nothing. Make `RESULT_STATUS: complete` the last line, exactly. Leave no background shells running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script. Use a private `TMUX_TMPDIR` for anything that touches tmux.
