# LS1b: a real-work split for the summarize_files qualification

Lane `ls1-real`, step S1b. Level: `mid` coder. Base: `main`.

Touches:
- `tests/calibration/summarize/` (chores, answers, fixtures);
- `tests/test_qual_summarize.py`;
- `tools/qual/` (only if the runner or checker needs to accept the new split, or larger files).

Not touched: `config/`, `lib/`, `bin/`.

## Why
The owner decided J1-D1 on 2026-11-09: local summarization must be "built well and fully tested with best chance of
success to be used on real world work". The 24 S1 chores run over small synthetic fixtures. Before Main runs the live
qualification (S2), the set needs chores over real, full-size files. Read ls1-local-summarizer.md, including "S2
additions".

## Change
1. **Fixtures.** Freeze copies of real tmux-pi files from commit `109948a` under
   `tests/calibration/summarize/fixtures/real/`, keeping their relative paths:
   - Pick 8 to 12 files that span the real mix: large Python modules (for example lib/lanes.py, lib/lane_remove.py,
     lib/lane_commands.py), a script from bin/ (store it without the executable bit), a det-prep plan, the README, and
     one test module.
   - Never copy `.env*`, anything under `config/` or `agent/`, credentials, or files that hold private data.
   - Put each file's source commit and path in `fixtures/real/SOURCES.md`.
   - Make sure no repo-wide test, closure check or tool treats these copies as live code. If one does, exclude the
     directory in the narrowest way, inside this lane's touches, and say where.
2. **Chores.** Add 12 chores with `split: real`, in the existing format. Cover the same kinds as S1: which files use X
   and what for; what a module exports and each export's job; the states or steps a doc lists; how two files differ; a
   3-5 bullet summary of one file. At least 4 chores must need facts from more than one file. At least 3 must need facts
   from deep inside a large file (past line 400).
3. **Answers.** Write `answers.jsonl` entries in the existing format (required facts as accepted phrasings or regexes,
   forbidden plausible fabrications, support lines, anchors). Each required fact must be checkable from the frozen
   fixture text alone. Give each chore at least 2 forbidden claims that a model might plausibly invent.
4. **Tests.** Extend `tests/test_qual_summarize.py`:
   - the real split has 12 chores and they pair with their answers;
   - every named fixture exists;
   - every anchor's line contains its text;
   - for each chore, a hand-written reference reply passes the checker, and a reply with one fact removed or one
     forbidden claim added fails it. Commit the reference replies as samples.
5. **Runner.** If `summarize_run.py --split` needs `real`, add it, and test it with the stub worker. Do not run the
   runner against a live model.
6. Run `tests/test_qual_summarize.py`, `test_router_catalog` and `test_route_resolve` in four cells: `/usr/bin/python3`
   and `/opt/homebrew/bin/python3`, each with the session env set and unset. Each file must take under 60 s.

## Rules
- Never read `.env*`.
- Never search config/ or agent/ recursively.
- No live model or bench run.
- Any test that calls tmux must set `TMUX_TMPDIR` to a temp dir first and use a private `-L` socket.

## Done means
1. 12 real chores whose answers can be checked, and whose reference replies pass the checker.
2. The negative cases fail.
3. Four cells green, no new failure.
4. The report lists the files frozen, the commit, the scripts written and run, and `RESULT_STATUS`.
