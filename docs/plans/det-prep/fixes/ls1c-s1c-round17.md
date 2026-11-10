# LS1c S1c round 17: a module reference must name a module that exists (Main, 2026-11-10)

Review review-coder-15ec199bde-025847 (REQUEST_CHANGES): F1 (family `binding-ambiguity`, sixteenth round), F2 (report accounting),
F3 (attack breadth). The report, `repro.py`, `check-spellings.py` and both interpreters' logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-15ec199bde-025847/`.
Run `repro.py` on both interpreters first and record each failing case.

The review confirmed these from round 16; keep them:
- the one resolver;
- `_spellings`;
- the 16 allow-list entries;
- guard 9/3/0 and probe 0;
- the full-files and use-lrucache lines;
- import attack 136 -> 0 and spellings 840 -> 0.

The decisions of rounds 15b and 16 stand.

## The defect (F1)
`_file_member` treats a member as a module reference when `_same_module(al.qualified, sub, graph)` holds. `_same_module` accepts two
equal strings without proving that either names a run-root module (lib/summarize_structured.py:1367-1375, 1379-1383, 1402).

`h.py: from h import x` with no `h/x.py` therefore resolves `x` to a made-up module `h.x`. The file stays non-opaque, and `use_scan`
writes absence lines for `h.py` and for every importer (`u.py: from h import x`). Python itself raises ImportError on a partly
initialized `h`. The review reproduces five forms on both interpreters:
- absolute;
- an explicit alias;
- `hp/__init__.py: from . import x`;
- `hp/h.py: from .h import x`;
- an importer using `import h`.

## Main's decision
1. **A module reference must name a module file the graph has.** A name resolves to a run-root module only when the graph holds that
   module, as a `.py` file or a package `__init__.py`. Equal strings prove nothing. Every other case is "unknown", and the file is
   opaque:
   - a member the scan cannot find;
   - a module importing from itself, at any depth of chain;
   - a cycle.

   The real submodule case (`from pkg import sub` where `pkg/sub.py` exists) keeps resolving as today.
2. **Fix it where the resolver decides.** Change `_same_module` / `_file_member` (or replace the shortcut), still inside `_resolve` and
   its helpers. No new per-file test, and no special case to win lines back. `_opaque` stays the one per-file test for both walks.
3. Sweep for every other place that turns a name into `_Ref('module', ...)` or compares module names. Each must prove existence the
   same way. List each site in the report with its line.

## Attack the rule before you report
Build each input space by code, print it, and run it at f957a20 (before) and after, on both interpreters:
- **Self and cycle imports:** a module importing a member from itself, by every import form round 16 covered:
  - absolute;
  - relative at levels 1 to 3;
  - `from . import x`;
  - `import h` then `h.x`;
  - aliases;
  - re-export chains of depth 1 to 3 that end in a self-import or a two- and three-module cycle.

  Cross these with:
  - member kinds: missing; a real submodule file; a name bound earlier in the module; a name bound later;
  - importers: none; one importer; an importer through a package `__init__`;
  - targets: a class and a function;
  - locations: the root, the outer initializer, the middle `app/sub/__init__.py` initializer and the innermost initializer.

  Execute every row (`-I`, `sys.path[0]` = case root) with credit and absence variants. Expected: unsound 0. A real submodule keeps
  its lines; report every line lost or won, both ways.
- **F3, the missing breadth of round 16's attacks:**
  - Rerun the round-16 import attack with function targets added and the middle initializer location included.
  - Rerun the spelling attack with every identifier-valid code point whose NFKC form is an ASCII letter, found by code. That is
    what the reviewer's `check-spellings.py` does (814 or 818 candidates per Unicode version); reuse it or match it.

  Report unsound and mismatch counts before and after on both interpreters. Do not add refusal rules to pass this.

## F2 (correct the report from existing logs; no rerun needed)
- Map each sweep row to the mechanism the earlier review actually cited, not to the old line range. The review lists the corrected
  rows. Row 19 also changes: the old `_use_lines` already handled relative imports.
- List every timing measurement that exists instead of ranges. That includes:
  - the 8.648 s and 7.151 s whole-file runs;
  - the six base and six after measurements on system Python;
  - the three of each on Homebrew.

  Either qualify "did not get slower" or drop it.

## Report
- Fail-before against f957a20 for each new test.
- Guard per-split counts and the real probe count against f957a20. List every lost line with the reference that made its file opaque.
  If any use-lrucache line is lost, or a full-files row loses a line, stop and report it.
- The seven modules (tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
  tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2, tests.test_session_env,
  tests.test_qual_summarize_meaning) in all four cells, run one after another:
  - each log starts with `date -u` and `git add -A && git write-tree`;
  - record each file's time per cell;
  - run `git reset -q` afterwards, so nothing stays staged.
- The sweep: every place that makes or compares a module reference, found by code.
- Commit nothing. Make `RESULT_STATUS: complete` the last line, exactly. Leave no background shells running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script. Use a private `TMUX_TMPDIR` for anything that touches tmux.
