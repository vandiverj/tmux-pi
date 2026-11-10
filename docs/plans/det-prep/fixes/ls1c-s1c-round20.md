# LS1c S1c round 20: one binding per name, every read resolved (Main, 2026-11-10)

Review review-coder-ee6d7b3348-054434 (REQUEST_CHANGES): F1 and F2 (family `binding-ambiguity`, nineteenth round), F3 note, F4 and F5
report checks. The report, `repro.py` and both interpreters' logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-ee6d7b3348-054434/`.
Run `repro.py` on both interpreters first and record each failing case (26 false credits per interpreter).

Keep what the review confirmed from round 19:
- rules 1-3 and where they live; `_opaque` as the one per-file test; `_spellings` and the 16 allow-list entries;
- guard 9/3/0 and probe 0, zero inventory lines lost; the full-files and use-lrucache lines;
- every earlier attack at 0 unsound.

F3 (the class-body and `rebound` checks are conservative) is accepted as is: do not narrow or tighten them this round.

## The defects
- **F1:** `import hp.x; from vals import hp; y = hp.x.v` credits Q. `kept` drops the member result of the second import, and `rebound`
  skips every `ast.alias` event, so a name bound by two imports keeps only the first's module.
- **F2:** `import hp; hp.x += 1` credits Q. An augmented assignment's target is a write that also reads `hp.x`, but the checker treats
  it as a write only and drops the last attribute, so rule 2 never sees the read.

## Main's decision
1. **One binding per name, import bindings included.** A name with more than one binding in a scope (any binding form, two or more
   imports included) resolves only when every binding resolves to the same module; otherwise it is unknown and a read through it makes
   the file opaque. No resolution result (member, module, unknown) is discarded before this decision.
2. **Every read of an attribute chain is resolved, whatever the syntax.** Classify each attribute node by what Python evaluates: an
   augmented-assignment target reads its full chain before writing; a `del` of an attribute needs the object chain; any other node that
   both reads and writes counts as a read. Do this in one place where the checker decides read or write, for every node type (list
   them from the `ast` module by code), not by adding an `AugAssign` case alone.
3. No new per-file test, no special case to win lines back.
4. Sweep every place that drops, merges or filters a name's resolution results, and every place that decides read or write for an
   attribute node; list each with its line.

## Attack the rules before you report
Build each input space by code, print it, and run it at d207b11 (before) and after, on both interpreters:
- **Rule 1:** two and three bindings of one name × every pair of binding kinds (import of a module, import of a member, `import a.b`,
  `import ... as`, relative levels 1-3, assignment, def, class, parameter, `for`, `with`, walrus, `global`) × order (module first,
  member first) × same module / different module / member × conditional or unconditional × direct and in an imported helper × class
  and function targets.
- **Rule 2:** every `ast` node type with a `Store` or `Del` context or an implicit read (found by code) × chain depth 1-3 × submodule
  loaded / not loaded × `import hp`, `import hp as h`, relative imports × direct and in a helper.
- The reviewer's repro, round 19's attacks and every earlier attack.

Execute every row (`-I`, `sys.path[0]` = case root) with credit and absence variants. Expected: unsound 0. Report every line lost or
won against d207b11, by cause. Do not add refusal rules beyond the two above to pass this.

## F4 and F5 (correct the report from existing logs; no rerun needed)
- F4: the repro19 errors are 12 credit + 12 absence = 24 per interpreter; the 14,856 lost alias lines are 8,156 credit losses on programs
  that run plus 6,700 sound absence losses on programs that fail. List each SLOW_TESTS measurement individually, every run, no ranges.
- F5: correct sweep rows 6, 10, 11 and 12 to the mechanisms the review cites (parameter attribution, `_changes_import_state`,
  `_runs_clean`, attribute-write events), using its current sites.

## Report
- Fail-before against d207b11 for each new test.
- Guard per-split counts and the real probe count against d207b11. List every lost line with the reference that made its file opaque.
  If any use-lrucache line is lost, or a full-files row loses a line, stop and report it.
- The seven modules (tests.test_qual_summarize_code_facts, tests.test_qual_summarize, tests.test_qual_summarize_oracle,
  tests.test_qual_summarize_structured, tests.test_qual_summarize_structured_v2, tests.test_session_env,
  tests.test_qual_summarize_meaning) in all four cells, run one after another:
  - each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`;
  - record each file's time per cell;
  - run `git reset -q` afterwards, so nothing stays staged.
- Runtime-error counts for every attack, credit and absence, and what they are.
- Commit nothing. Make `RESULT_STATUS: complete` the last line, exactly. Leave no background shells running. Never send a request to
  127.0.0.1:1234 or any other address from a test or attack script. Use a private `TMUX_TMPDIR` for anything that touches tmux.
