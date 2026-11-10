# LS1c step S1b: fact form v2, a name outline and a name index (Main, 2026-11-09)

S1 (merged 711cd79) built `lib/summarize_structured.py`. Main's live tuning on the tune split (12 chores, qwen3.6-35b,
temperature 0) found:
- **line ranges**: the S1 `line_start`/`line_end` form led to whole-function ranges, and validate dropped 60 of them.
  A list of 1-3 separate lines left no drops.
- **a separate subject field**: this field, plus code that normalizes it, cut wrong-subject failures.
- **a name list in the prompt**: listing the names each Python file defines (from `ast`, not the model) cut missing facts.
- **a name index in the output**: writing that list into the answer as fact lines took the score to 3/12 and cut
  failures from 41 to 28.

The prototype lives in `/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/expwt/lib/summarize_structured.py`. The diff
against main is `/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/expwt.diff`. Results are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/ls1c-2026-11-09/` (tune-t2, t3, t6, t7-params). The
prototype hangs its new behaviour on `EXP_*` env switches. Build it properly: no env switches, and no "EXPERIMENT" left in
the code.

## Build
1. **Fact form v2.**
   - **Shape**: `{"subject", "claim", "path", "lines"}`. `lines` is a list of 1 to 3 integers.
   - **What validate does**:
     - Normalize `path` as now.
     - Drop a fact whose `lines` is empty or longer than 3, or holds a non-integer or a bool (`malformed`).
     - Drop a fact with a line outside the file (`range_outside_file`).
     - Drop a fact for an unnamed file or an empty claim, with the S1 reasons.
     - Never change a line number. Remove duplicate lines and sort them.
   - **The subject**:
     - Strip whitespace and backticks.
     - When a dotted name (`A.b` or `A.b()`) is not a named file's path or base name, keep only its last part (`b`).
     - The rendered claim is `<subject> <claim>`.
   - **The old form**: keep accepting `line_start`/`line_end` exactly as S1 does. A user's local config may still use it.
2. **render**: write v2 as `- <claim> [path:n, path:m]`, the form `summarize_check.FACT_LINE` parses, with one citation
   per line. The S1 form renders as before.
3. **`outline(path, numbered_text)`**: a list of `(line, kind, name, owner)` read with `ast` from a `.py` file's text.
   - **What it lists**:
     - classes;
     - top-level functions;
     - methods, with their class as owner;
     - module-level constants: `Assign`/`AnnAssign` to a plain name;
     - the parameters of each function and method, except `self`/`cls`, with the function as owner.
   - **What it does not walk**: nested functions inside functions.
   - **No list**: a file that is not `.py`, or a `SyntaxError`, gives `[]`. Do not raise.
4. **`user_message`**: after each file's block, add `Names defined in <path> (cover each one the task is about):` and
   one `- line N: <kind> <name>` per outline entry (`parameter ttl of __init__` for a parameter). If the outline is
   empty, add nothing.
5. **`name_index(files)`**:
   - **What it writes**: one line per class, function, method and constant in the outline, before the model's facts:
     `- <base name> defines the <kind> <name> [<path>:<line>]`.
   - **What it skips**: parameters, `__init__`, and names that start with a single underscore.
   - **The order**: file order, then line order.
   - **The return value**: `summarize` returns `index lines + rendered facts`. The record gets `"indexed": <count>`.
   - **The guard**: these lines come from code, not the model. A test checks that each one passes `summarize_check`
     rule 3, meaning its citation is the def or assignment line.
6. **Config**: in `config/local.example.json`, switch the `summarize_structured` schema to v2:
   - required: `subject`, `claim`, `path`, `lines`;
   - `lines` has minItems 1 and maxItems 3;
   - `additionalProperties` is false.

   Set `system` to the text of `bench-results/ls1c-2026-11-09/prompt-t6.txt`. Update the key's `_summarize_structured`
   note.

## Tests (stub endpoint as in S1; no live model)
- **validate v2**: each drop reason, 3 lines kept, 4 dropped, subject normalization, and the old form still accepted.
- **render v2**: the exact text, which `FACT_LINE` parses into one citation per line.
- **outline**: a fixture covering every kind (with AnnAssign), a nested function that is not listed, a non-.py file,
  and a SyntaxError.
- **name_index**:
  - the exact lines, with the skips;
  - run on `tests/calibration/summarize/fixtures/src/cache.py`, every index line passes rule 3 of `summarize_check`
    against the calibration answers' file numbering.
- **summarize with the stub**: the index comes first, then the facts. The record carries `indexed` and `drops`.
- **the config key**: it loads through `router.load_config`, and its schema is v2.
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
(`mktemp -d /tmp/s1b.XXXX`) and uses a private `-L` socket.
