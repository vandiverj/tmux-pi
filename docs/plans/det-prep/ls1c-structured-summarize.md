# LS1c: summarize_files as deterministic prep plus structured output (Main, 2026-11-09)

S2 ran on 2026-11-09 and missed the bar on the tune split by a wide margin. Following S2 additions item 5 ("record the
numbers and the failure classes, keep `candidate`, and write a fix round"), this is that fix round. The bar is unchanged.

## What S2 found (bench-results/s2-2026-11-09/, LM Studio qwen/qwen3.6-35b-a3b)
| arm | tune pass | notes |
|---|---|---|
| Pi worker, live prompt, thinking off, x3 | 0/36 | prose, headings, `path:line` in sentences; 43 fabricated citations |
| Pi worker, draft prompt, thinking off, x3 | 0/36 | fewer noncanonical citations (145 -> 44), still no reply that is fact lines only |
| draft, thinking low / medium, x1 | 0/12, 0/12 | thinking does not change format compliance |
| draft + "read with cat -n", x1 | 0/12 | mostly fact lines now, but several facts per line and 10-25 line ranges |
| files inlined with line numbers + prompt, x1 | 1/12 | line numbers now right; still prose, "(line N)", bold |
| **structured: numbered files + JSON schema, rendered by code, x1** | **2/12** | **0 bad citations, 0 format failures**; remaining: missing facts, shared_line, wrong_subject |

Root causes:
1. **Pi's `read` tool shows no line numbers.** Citations were guesses: across 330 citations only 116 named a support
   line exactly; the rest scattered at -1 (75), +1 (43), -2 (37), +2 (22).
2. **A free-text agent does not keep a strict format.** No prompt, thinking level or instruction got even one reply that
   was fact lines only.
3. Remaining content gaps (structured arm): facts merged into one item, claims not led by their subject, facts left out.

Routing (`--route-only` x3, all 24): identical every run. The 5 `sum-*` chores tag `summarize_files` (stop while
candidate); the 19 use/exports/steps/diff chores tag the qualified locate-and-quote shape and route local.

## Design (recommended under J1-D1)
`summarize_files` stops being a Pi tool loop. The files are named in the task, so tmux-pi does the reading:
1. **Prep (deterministic).** Resolve the named files (repo-relative, inside the worktree, no `.env*`, no trust-store or
   withheld paths; the same safe-read rules as the review packet). Number each line. Refuse when the total exceeds the
   model's context budget from the catalog, or split per file (chunking: a later step if a real chore needs it).
2. **Ask (one call).** POST to the local OpenAI-compatible endpoint with `response_format` json_schema
   `{facts: [{claim, path, line_start, line_end}]}`, temperature 0. Read `content`, else `reasoning_content` (LM Studio
   puts the JSON there while reasoning is on).
3. **Validate (deterministic).** Normalize `path` (strip a `:N[-M]` suffix, a leading `./`, backticks). Drop a fact whose
   path is not a named file, whose range is outside the file or spans more than 5 lines; count the drops in the result.
   Never invent or repair a line number.
4. **Render.** `- <claim> [path:a]` or `[path:a-b]`, one per fact. The result says how many facts were dropped and why.
5. **Route.** `tp-route`/`tp-spawn` send a `summarize_files` task here instead of a Pi worker; the output lands in the task
   dir as a normal result so `tp-agent collect` works unchanged.
6. **Runner.** `tools/qual/summarize_run.py` gains `--path structured`, which calls the same module (not a copy), so S2
   measures the product path.

## Tune work (inside the lane, tune split only)
Iterate the system prompt and schema on tune only: one fact per item (the checker's `shared_line`), the claim starts with
its subject (`wrong_subject`), cover every part the task asks (`missing`). A schema field that helps (for example a
`subject` field rendered first) is allowed. Record each iteration's tune numbers. Holdout and real are not looked at
until the prompt is chosen.

## Bar (meaning bar, owner direction 2026-11-09: "re-score the runs both ways, then grade on meaning")
The strict checker (`tools/qual/summarize_check.py`) grades format as well as content; on the tune split it passed 1-3
of 12 where a blind meaning grade passed 4-7 (bench-results/ls1c-2026-11-09/meaning/scores.txt). The bar is now graded
on meaning, with the rubric in bench-results/ls1c-2026-11-09/meaning/RUBRIC.md (amendment 1 included):
- **Precision (hard):** zero `wrong` lines and zero `unsupported` lines across every reply (no false claim, no citation
  that does not show its claim), and zero unparsed replies.
- **Recall:** overall >= 90% of reference facts met, and every chore >= 80% (`ceil(0.8 n)`) in every run.
- **Splits:** holdout x5 and real x5 both meet the above; Main hand-inspects real replies.
- **Grader:** a judge independent of the model under test (never qwen grading qwen), calibrated against the 108 blind
  grades in bench-results/ls1c-2026-11-09/meaning/ before its numbers count.
- The strict checker stays as a diagnostic (it finds format drift cheaply) but no longer gates.
- Routing: >= 34/36 tagged summarize_files and identical across 3 runs (the locate-and-quote tagging of the 19 chores
  is settled in this lane: either the router's shape text sends them to summarize_files, or the bar's routing item is
  rewritten by the owner, never silently).

## Probe artifacts
bench-results/s2-2026-11-09/: run.sh, run2.sh, run3.sh, run4.sh, pi-prep.py, structured_probe.py, prompt-*.txt, every
`*.jsonl` and `*.log`; failed-no-node/ holds the first tune runs, which never started a worker (my run script left
`TP_PI_NODE` unset).
