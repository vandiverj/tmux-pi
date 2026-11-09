# LS1c S1c round 3: state a code fact only on positive proof (Main, 2026-11-09)

The check task (coder-0203b5d05f) was checkpoint-committed by Main as the lane tip after b1f06fc. It was previewed with main:
209 tests OK in all four cells. The review is REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-0203b5d05f-160034/result.md
(read it, and `report.md`, `repro.py` and `repro.log` beside it).

## Why this round changes the approach
Three rounds in a row found more binding cases that give a wrong line: a star import, a dotted import, an attribute or
subscript write, a match capture, class-level `if`, and a basename that matches an unrelated module. Each round added one more
case to rule out, and the next review found another. The fix is to flip the rule. A line is emitted only when the code
**proves** the binding is single and simple. Anything not on the allow-list gives no line. Losing a line costs recall; a
wrong line costs correctness, and correctness comes first.

## Build
1. **Constants and definitions (version diff).** Compare a name across two versions only if, in **each** file, every one of
   these holds:
   - The name is bound exactly once in that scope (module, or one class body). The binding is a plain `NAME = <literal>`, a
     `def`, an `async def` or a `class`, and it is not nested under any compound statement (`if`, `try`, `with`, `for`,
     `while`, `match`).
   - Nothing else in the file can change it. There is no star import, and the name does not appear as an assignment target
     of any kind anywhere in the file: attribute, subscript, augmented, annotated, tuple, starred, walrus, `for`, `with ... as`,
     `except ... as`, a `match` capture or pattern name, `import`, `del` or `global`/`nonlocal`. That includes subscript or
     attribute writes to it.
   - For class members, the same test applies to the class body, and the class itself must pass the module-level test.
2. **Use scan.** Attribute a call or an import to the target file only if all of these hold:
   - The importing module resolves to the target by its full dotted path from the run root. For a relative import, use the
     importer's package. A basename match alone never counts.
   - The imported name (or the module alias, for `import t` / `t.Q()`) is bound once in the file, by that import, and passes
     the "nothing else can change it" test from item 1. Writes to `t.Q` count too.
   - The file has no star import.
   - Otherwise keep only what is proven. The import line may still be stated if the import itself resolves by full path.
     Drop every call attribution.
3. **One predicate.** Build one function that answers "is this name provably single-bound in this scope?", and use it for
   both item 1 and item 2. Remove the case-by-case suppressions it replaces.
4. **Report** the guard's per-split line counts before and after, and the real-files probe count (441 at the lane tip). The
   loss in recall must be visible.

## Tests
- Every reproduction in the review's `repro.py` (F1-F4) and the round-1 and check-task reproductions give no wrong line.
- **A table-driven test.** For each binding form listed in item 1, insert one extra binding of the name into an otherwise
  clean pair and assert the diff and the use scan say nothing about it. Insert at module scope, and for class members inside
  the class body. The table must name each form, so a missing form is visible.
- The clean cases still give their lines (a single `X = 1` vs `X = 2`, a single `def`, a full-path `from pkg.t import Q;
  Q()`).
- Fail-before for each new or changed test against the lane tip. Put the script and log under `evidence/` in the task dir and
  name them by absolute path.

## Done means
- The six modules of the round-2 brief plus `tests.test_qual_summarize_meaning`.
- They pass with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Rerun them after your last edit and list each file's time.
- Include a Self-review section and SLOW_TESTS (review F5).
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background watchers that outlive your work. A later turn overwrites the report Main reads.

No live model and no request to 127.0.0.1:1234. No special-casing of fixtures. Never read `.env*`, and never write that
string in a shell command. Never search `bin/` as a directory, or `config/`, `agent/` or `claude/` recursively.
