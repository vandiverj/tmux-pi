# LS1c step S1c: facts that code can state (version diff, use scan, false-absence drop) (Main, 2026-11-09)

The meaning re-score (run-log 2026-11-09, bench-results/ls1c-2026-11-09/meaning/scores.txt) moved the LS1c bar to
meaning: zero wrong lines, zero unsupported lines, recall >= 90% overall and >= 80% per chore. The best tune run got
85% recall with zero wrong and zero unsupported. Its misses are cross-file facts:
- **diff chores:** what the new version adds, drops or changes;
- **use chores:** who uses a class, for what, and who does not;
- **false absences:** "X is not in the files" when a named file defines X.

Code can find all three exactly. This step makes `lib/summarize_structured.py` state them, so the model is no longer
relied on for them. It builds on S1b (fact form v2, `outline`, `name_index`); the lane base is main after S1b merges.

Build every rule for any Python files. Never special-case the calibration fixtures (`tests/calibration/summarize/
fixtures/src/`), their names or their tasks: the holdout and real splits use other files.

## Build
1. **`version_pairs(files)`**: pairs of named `.py` files whose stems are equal after removing one trailing `_v<digits>`
   or `<digits>`, such as `settings.py` and `settings_v2.py`. The file with the suffix (or the higher number) is the
   new one. Files that pair with nothing give no pairs.
2. **`version_diff(old, new)`**: lines `- <claim> [path:n, ...]` in the v2 render form, each citing the line that
   shows it.
   - **Names**: each class, function, method or constant (`outline` kinds, with the same skips as `name_index`) that is
     in new but not old, written as `<new base> adds the <kind> <name> [new:line]`. Each one in old but not new, written
     as `<new base> drops the <kind> <name> that <old base> defines [old:line]`.
   - **Literal constants**: for a module constant in both files whose value `ast.literal_eval`s, compare the values.
     - A scalar that changed: `<new base> changes <name> from <old> to <new> [old:l, new:l]`.
     - A dict: compare it key by key. Write the same "changes" line for a key whose value changed (`changes
       DEFAULTS["workers"] from 2 to 4`), "adds the key"/"drops the key" for added and dropped keys, and cite each key's
       own line.
     - A value that does not `literal_eval`: compare nothing.
   - **Calls**: for a function or method defined in both, list the plain names called (`ast.Call` with a `Name` or
     `Attribute` func) in new and not in old, written as `in <new base>, <func> now calls <callee> [new:call line]`.
     Write the reverse as "no longer calls".
   - **Order**: file-pair order, then names, constants, calls, each in line order.
3. **`use_scan(task, files)`**: runs when the task text contains, as a whole word, the name of a class or top-level
   function that one named `.py` file defines (the "target"). For each other named `.py` file:
   - **Imports** of the target (`from m import T`, `import m` then `m.T`): `<base> imports <T> [path:line]`.
   - **Calls** `T(...)`: `<base> creates a <T> [path:line]` for a class, `<base> calls <T> [path:line]` for a function.
   - **Uses through a variable or parameter**, for a class: attribute access `x.m` where `m` is a method or attribute
     the target class defines and `x` is a name bound in that file from `T(...)`, or a parameter of the enclosing
     function. Write `<base> uses <T>.<m> on <x> [path:line]`, one line per site.
   - **No use**: a file with no import, call or attribute use of the target gives `<base> does not use <T> [path:1]`.
     Citing line 1 is the agreed form for an absence (rubric amendment 1).

   A guessed parameter use (only the method name matches) is still a fact about the code: "uses X.pop on queue" says
   `queue.pop` is called. The line says `uses <T>.<m> on <x>` only when the target defines `m`.
4. **The false-absence drop**: in `validate`, drop a model fact whose claim says a name is absent from the files or
   from a named file, and whose name a named file defines (any `outline` entry, parameters included). The drop reason
   is `false_absence`.
   - **Absent**: the claim matches the agreed absence phrases, case-insensitive: "not in the files", "not in <path or
     base>", "is not defined", "does not define", "does not exist", "is absent".
   - **Name**: the fact's subject, or a backticked or identifier-like token in the claim.
   - Keep the phrase list in one named constant with a comment. Do not use a model to judge it.
5. **`user_message`**: after the names block, add `Differences found by code:` and `Uses found by code:` blocks with the
   lines from 2 and 3. Add `Explain what each difference or use means for the task; these lines are added to your answer
   for you.` Add nothing when there are none.
6. **`summarize`**: the answer is `index lines + diff lines + use lines + rendered facts`, deduplicated by exact text.
   The record gets `"code_facts": <count of diff + use lines>`, and drops list `false_absence` with the rest.

## Tests (stub endpoint; no live model)
- **version_pairs**: settings/settings_v2, a.py/a2.py, and no pair for unrelated stems or for a non-.py file.
- **version_diff**, on a new fixture written for the test (not the calibration files):
  - added and dropped functions and constants;
  - scalar and dict-key changes, plus added and dropped keys, with exact line numbers;
  - a non-literal constant that is not compared;
  - "now calls" and "no longer calls".
- **use_scan**, on a new fixture:
  - an import, a create, a parameter use and a no-use file, each with an exact line;
  - a task naming no defined name gives nothing;
  - a method name the target does not define gives no line.
- **The false-absence drop**: each phrase drops a fact naming a defined name, keeps a true absence (a name that is not
  defined) and keeps a fact without an absence phrase.
- **The guard**: on the calibration fixtures, every diff and use line passes `summarize_check` rule 3 (its citation is
  the line that shows it) against the calibration answers' file numbering. An absence line's citation of line 1 is
  exempt from that check.
- **summarize with the stub**: the order index, diff, use, facts; deduplication; `code_facts`; `false_absence` in drops.
- **fail-before**: each new test fails on the lane base. Keep the script and log under `evidence/` in the task dir, and
  name every log by absolute path.

## Done means
1. Items 1-6, with tests.
2. The new tests pass in both interpreters (`/usr/bin/python3`, `/opt/homebrew/bin/python3`), in both modes:
   - outside tmux: `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`;
   - inside tmux: `TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`.

   The same goes for tests.test_qual_summarize*, tests.test_router and tests.test_session_env. List each file's time.
3. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search
`bin/` as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name config files.
No live model, and no request to 127.0.0.1:1234. Any test that calls tmux sets `TMUX_TMPDIR` to a fresh short dir
(`mktemp -d /tmp/s1c.XXXX`) and uses a private `-L` socket.
