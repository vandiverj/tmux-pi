# LS1c S1c round 16: every import form gets the same outside check, and words are checked as Python reads them (Main, 2026-11-10)

Review review-coder-2feae8a8fc-014545 (REQUEST_CHANGES), F1 and F2 (family `binding-ambiguity`, fifteenth round), F3 (report
accounting). Report, `repro.py`, `repro-more.py` and both interpreters' logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-2feae8a8fc-014545/`.
Run both scripts on both interpreters first and record each failing case. Round 15's allow-list, the 16 proved entries, the loader
axis (0 unsound), guard 3/9/0, real probe 0 and `test_full_src_rows_keep_every_line` were confirmed; keep them. Main's 15b decisions
stand.

## The defects
- **F1:** `_opaque` checks `from M import X` against the allow-list only when the import is absolute (`not node.level`,
  lib/summarize_structured.py:1063). `from .h import argparse as a` binds an outside module through a run-root module and is never
  checked, so `a._sys.path.insert(...)` passes. The absolute form is blocked.
- **F2:** the forbidden-word checks read raw source text, but Python NFKC-normalizes identifiers: `__ｉｍｐｏｒｔ__`, `__ｇｌｏｂａｌｓ__`
  and the like are the same names to the interpreter and are not seen by the words.

## Why the rounds keep coming
Each check is written for one spelling of a thing: absolute imports but not relative ones, ASCII names but not their normalized
forms. The fix must be one place that sees every form.

## Main's decision
1. **One resolver for every import statement.** Every `import` and `from ... import` (absolute, relative at any level, `from . import h`,
   `from .h import X`, chains through several run-root modules) goes through one function that returns what each bound name refers to:
   an outside module or outside qualified name, a run-root module or member, or "unknown". A run-root member that is itself bound in
   its module to an outside name (any import form there, any depth of re-export) resolves to that outside name and is held to the
   allow-list exactly as a direct import is. "Unknown" (a star import, a module `__getattr__`, a name bound more than once, a member
   the scan cannot find, a cycle) makes the file opaque. Attribute access on a run-root module (`h.argparse._sys`) resolves the same
   way. No other code path may decide what an imported name refers to.
2. **Words are checked as Python reads names.** Every word test (`_DYNAMIC_ACCESS`, `_DYNAMIC`, `_STATE_WORDS_RE`, `_LOADER_NAMES`, and
   any other list of names) runs on NFKC-normalized identifiers, from the AST (which already holds normalized names), or on
   `unicodedata.normalize("NFKC", source)` where it reads text. Strings and comments are checked as today. If two word sets
   (`_DYNAMIC` and `_DYNAMIC_ACCESS`) mean the same thing, make them one; if they differ on purpose, say why in the code.
3. Keep one per-file test (`_opaque`) for both walks. Do not add edges or special cases to win lines back.

## Attack the rule before you report
Build each input space by code and print it:
- **Import forms:** absolute, relative level 1..3, `from . import h` then `h.X`, `import pkg.h` then `pkg.h.X`, `from .h import X as Y`,
  re-export chains of depth 1..3 mixing relative and absolute, a member bound twice, a star in the chain, a module `__getattr__`, a cycle
  × outside targets (a listed callable, a listed value, an unlisted module, an unlisted callable, a submodule attribute such as `_sys`)
  × uses (call, attribute read, attribute call, pass as a value, decorator, base) × the round-13/14 locations. Every row executed
  (`-I`, `sys.path[0]` = case root) with credit and absence variants. Expected: unsound 0, and an allow-listed use through a
  re-export is treated the same as the direct import (report the mismatches both ways).
- **Spellings:** every word in every word set × NFKC-equivalent forms found by code (for each ASCII letter, every code point whose NFKC
  form is that letter; use at least fullwidth, mathematical alphanumerics and one ligature where it applies) × identifier, attribute,
  keyword argument, string and comment positions. Execute the identifier rows to confirm Python treats them as the word.
- Report unsound and mismatch counts on both interpreters at 9240ee8 (before) and after.

## F3 (correct the report from existing logs; no rerun needed)
SLOW_TESTS per cell and per run, including every qualifying run; the system/Homebrew probe timings the right way round; the word
sets named exactly as the code names them; review-round labels corrected (rows 11/12).

## Report
- Fail-before against 9240ee8 for each new test.
- Guard per-split counts and the real probe count against 9240ee8, every lost line listed with the reference that made its file opaque.
  If any use-lrucache line is lost, or a full-files row loses a line, stop and report it.
- The seven modules in all four cells, one after another; each log starting with `date -u` and `git add -A && git write-tree`; each
  file's time per cell.
- The sweep: every place that decides what an imported name refers to, and every place that tests a name against a word list, found
  by code.
- Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells left running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script.
