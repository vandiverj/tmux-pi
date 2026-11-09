# LS1c step MG1: a repeatable meaning grader (Main, 2026-11-09)

The LS1c bar is graded on meaning (ls1c-structured-summarize.md, "Bar"). It needs zero wrong lines, zero unsupported
lines, and recall of at least 90% overall and 80% per chore, judged by a judge that is not the model under test. The judge
must be calibrated against the 108 blind grades in /Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/ls1c-2026-11-09/meaning/ before its numbers count. Those
grades were made by hand today (12 chores x 9 replies, Claude graders, RUBRIC.md with amendment 1). This step makes the
same grading a tool, so every later run (S1c tuning, holdout, real) is graded the same way.

`bench-results/` is untracked and exists only in the main checkout, at /Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/.
Copy what the tool and its tests need into the repo (below); never point a test at
`bench-results/`.

## Build: `tools/qual/summarize_meaning.py`
Python 3.9+, standard library only, the same CLI style as `tools/qual/summarize_run.py`.

1. **`packet`**: build one packet per chore from a set of runs.
   - Inputs: one or more `NAME=FILE.jsonl` run files (summarize_run records: `chore`, `reply`; use a chore's first record
     per run), the chores and answers (`tests/calibration/summarize/chores.jsonl`, `answers.jsonl`), and the fixtures.
   - The packet is exactly the shape of /Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/ls1c-2026-11-09/meaning/diff-order.md:
     - `## Task`;
     - `## Files (numbered)`, numbered like `cat -n`;
     - `## Reference facts`: F1..Fn from the answer's `reference[].text` without the leading `- `;
     - `## Not in the files`: one `- regex: \`...\`` per `forbidden` entry;
     - `## Replies`: R1..Rk.
   - Reply ids are shuffled with a seed (`--seed`; exactly one of `--seed` or `--key` is required). The run-name key is written to a separate `key.json` that
     the judge never sees.
   - Packets are deterministic: the same inputs and seed give byte-identical files.
2. **`judge`**: grade each packet with a judge model through `bin/tp-pi -p`, headless, the way `summarize_run.run_pi`
   runs a worker. That means the same child-env scrub, a fresh temp dir, a timeout and a process-group kill.
   - `--model` is required, with no default. The tool refuses any model id whose provider is `local` in
     config/model-catalog.json, or any whose id contains `qwen`. The judge must never be the model under test.
   - The judge prompt is the rubric text (copy RUBRIC.md, amendment 1 included, to
     `tests/calibration/summarize/meaning/RUBRIC.md` and read it from there) plus the packet. The judge answers with the
     grade JSON on stdout.
   - Parse it strictly into the RUBRIC.md shape. Every reply must be present, with every Fi of the packet and every
     cited reply line (1-based position among cited lines). Anything else counts as `judge_error` for that chore, never
     as a pass. One retry per chore on `judge_error`, then record it.
   - Write `<chore>.grade.json` per chore, plus `judge.jsonl` with one line per call: the model, seconds, rc and error.
3. **`score`**: the aggregate, from grade files plus the key (generalize /Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/ls1c-2026-11-09/meaning/aggregate.py).
   - Per run: strict pass if run records are given, meaning pass (every fact met, zero wrong or unsupported), meaning
     >= 80%, recall, wrong, unsupported.
   - The bar verdict: zero wrong, zero unsupported, zero unparsed or `judge_error`, recall >= 90% overall, and every chore
     >= `ceil(0.8 n)`.
   - Output as a text table and `--json`.
4. **`calibrate`**: compare one judge's grade files with a reference set of grade files over the same packets and key.
   - Report the fact agreement (met vs not_met, over every reply x fact) and the line agreement (ok vs wrong vs
     unsupported, over every graded line).
   - Report the precision failures the reference marked (wrong + unsupported) that the judge also marked, and the
     ones the judge added.
   - Report the per-run bar verdict and per-run recall from each set.
   - **Pass rule** (constants in one named block, with a comment):
     - fact agreement >= 90%;
     - the judge finds >= 90% of the reference's wrong + unsupported lines;
     - the judge adds no more than 5 wrong/unsupported lines the reference did not mark, across the set;
     - per-run recall within 5 points of the reference for every run.
   - Print every disagreement (chore, reply, run, fact or line, both verdicts, the judge's note) so Main can read them.

## Reference data to copy in
`tests/calibration/summarize/meaning/`:
- RUBRIC.md;
- the 12 packets (`*.md`);
- the 12 grade files (`*.grade.json`);
- `key.json`.

These are the calibration set; record in a README there that they came from the 2026-11-09 hand grading (key seed
20261109). The shuffle that made that key is not recorded, so `packet` also takes `--key FILE`, which reuses an existing
key's reply order instead of shuffling. Packets made by `packet --key` from the same runs must equal the committed packets
byte for byte. If they cannot (whitespace, order), explain in the report exactly what differs and why. The run name to
file mapping is the `runs` dict in /Users/jerrickvandiver/Documents/Projects/tmux-pi/bench-results/ls1c-2026-11-09/meaning/aggregate.py
(base and t1-t7 under that ls1c-2026-11-09/ folder; pi is ../s2-2026-11-09/tune-live.jsonl). Copy only the `chore` and `reply`
fields of their first record per chore into
`tests/calibration/summarize/meaning/runs/<name>.jsonl`, so the packet test can rebuild the packets.

## Tests (stub judge; no live model)
- **packet**: the rebuilt packets equal the committed ones; the same seed gives the same output; a different seed changes
  only the reply order and the key; `--key` with the committed key reproduces the committed packets.
- **judge** with a stub `--pi-command` script:
  - a good grade JSON is written;
  - a missing reply, a missing Fi, a bad line index, non-JSON output and a timeout each give `judge_error` (with one
    retry);
  - a `local` provider or `qwen` model id is refused before any call;
  - the child env has no TMUX or TP_* except as summarize_run allows.
- **score**: on the committed grades and key, the table equals the run-log's meaning table
  (docs/plans/det-prep/run-log.md, "meaning re-score" entry): base 4/12 meaning, 64% recall; t3 7/12, 85%; pi 1/12, 45%;
  and the rest. The bar verdict is false for every run.
- **calibrate**: the reference grades against themselves pass with 100%. A copy with three facts flipped and one wrong
  line removed reports exactly those disagreements, and passes or fails per the rule as computed.
- **fail-before**: each new test fails on the lane base. Keep the script and log under `evidence/` in the task dir, and
  name logs by absolute path.

## Not in this step
No live judge run. Do not start pi or Claude against a real model, and send no request to 127.0.0.1:1234. Main runs the
calibration live after merge, with `--model openai-codex/gpt-6-sol`, and decides from `calibrate`'s output whether the
judge counts.

## Done means
1. Items 1-4, the reference data and the tests.
2. The new tests, plus tests.test_qual_summarize* and tests.test_session_env, pass with both interpreters
   (`/usr/bin/python3`, `/opt/homebrew/bin/python3`), in both modes:
   - outside tmux: `env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`;
   - inside tmux: `TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`.

   List each file's time.
3. `RESULT_STATUS` reported on its own line.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search
`bin/` as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name config files.
Any test that calls tmux sets `TMUX_TMPDIR` to a fresh short dir and uses a private `-L` socket.
