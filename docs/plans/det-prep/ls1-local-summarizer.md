# LS1 (LOCAL-SUMMARIZER): qualify summarize_files for local Qwen

Lane `local-summarizer`, step S1. Level: `mid` coder. Base: `main`. Touches: `tests/calibration/summarize/` (new),
`tools/qual/` (new), `tests/test_qual_summarize.py` (new), `config/local.example.json` (the `prompts` text and its
`_prompts` note only), `tests/calibration/experiments.md` (a new section only).
Not touched here: `config/model-catalog.json` (lane strong-shown holds it; the status flip is Main's step S2).

## Why
Owner 2026-11-08: "can't we make a summarizer for local models so it can work". The J1 live run (run-log 2026-11-08)
refused every local bench task. `summarize_files` is `candidate` in the catalog ("needs about 20 chores"), so a
summarize task never routes local, and the local router also failed to tag two count questions with a qualified shape.
The other shapes were qualified by chores run against Qwen3.6-35B-A3B in LM Studio (tests/calibration/experiments.md:
locate 117/120; counting 77/80 with prompt E, which lives in `config/local.json` `local.prompts` and is added to the
worker's system prompt by bin/tp-spawn:737). That harness was ad hoc (/tmp/tp-qual-*) and is not in the repo.

## Change (S1, the worker)
1. **Chores.** `tests/calibration/summarize/chores.jsonl`: 24 chores, `split` tune (12) / holdout (12), over frozen
   fixture files committed under `tests/calibration/summarize/fixtures/` (small, real-looking source and docs; no secrets,
   no copies of private projects). Mix: which files use X and what for; what a module exports and each export's job;
   the states/steps a doc lists; how two files differ; a 3-5 bullet summary of one file. Each chore names its files.
   Each answer in `answers.jsonl` holds `required` facts (each a small set of accepted phrasings or regexes),
   `forbidden` claims (plausible fabrications), and the files a citation may point at.
2. **Checker.** `tools/qual/summarize_check.py`: pure function reply -> {pass, missing, forbidden_hit, bad_citations}.
   A reply passes when every required fact is present, no forbidden claim appears, and every `path:line` citation it
   makes exists and that line contains the quoted text (if it quotes). Deterministic; no model in the checker.
3. **Runner.** `tools/qual/summarize_run.py --model M --split S --runs N --out FILE [--prompt-file P]`: for each chore,
   copy its fixtures to a fresh temp dir, run one Pi worker headless on the local model with the same system prompt
   tp-spawn would build (reuse tp-spawn's prompt source, do not copy text), collect the reply, score it, write one JSON
   line per run plus a summary (pass rate, median seconds). Strip TMUX/TMUX_PANE/TP_* from the child env; never create
   tmux sessions or worktrees. Also `--route-only`: run `tp-route --no-log --local-only` on each chore text and record
   the shape and level the router gives (tagging accuracy).
4. **Prompt draft.** In `config/local.example.json` add a summarize rule to the Qwen3.6 prompt (keep prompt E), e.g.
   read every named file in full before answering; state only what the files say; cite `path:line` for each fact; say
   "not in the files" instead of guessing. Explain it in `_prompts`.
5. **Tests** (`tests/test_qual_summarize.py`, import `session_env` first): chores and answers parse and pair up; splits are
   12/12; every fixture a chore names exists; checker cases (pass, missing fact, forbidden hit, bad citation, quote
   mismatch); runner with a stubbed worker writes the record and summary and leaves no temp dir, no tmux call
   (stub `tmux`/`git` on PATH and assert they were not called).
6. Do not run the runner against the live model from your pane. Main runs it (S2).
7. Run the new test and test_router_catalog, test_route_resolve, both modes, `/usr/bin/python3` and
   `/opt/homebrew/bin/python3`.

## S2 (Main, after S1 merges)
From a plain shell (TMUX unset), LM Studio up: `--route-only` on all 24; then tune split x3 without and with the prompt
draft, pick the prompt on tune only; then holdout x5 with the chosen prompt. Bar (same as the qualified shapes):
holdout >= 57/60 (95%) with zero fabricated citations. If met: Main copies the prompt into `config/local.json`, and a
small lane flips `summarize_files` to `qualified` with that evidence and adjusts the router's shape text if tagging
was weak. If not met: record the numbers, keep `candidate`, report the failure classes.

## Done means (S1)
1. 24 chores with deterministic answers; the checker and runner exist and are tested with stubs.
2. The runner leaves no temp dir, tmux session or worktree behind (tested).
3. No new failure outside the baseline, both interpreters, both modes.
