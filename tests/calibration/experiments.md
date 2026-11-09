# Laya calibration experiments

Score S = mean of intent, route and review accuracy. Route accuracy compares
targets, so `expert` and `escalated` (both Opus/high) count as the same.
`u` counts tasks reviewed less strictly than labeled. A change is kept only
when the tune score improves, the holdout score does not drop, and `u` does not
rise. T = tune (90 tasks), H = holdout (60 tasks).

| # | change | T score | H score | u (T/H) | kept |
|---|---|---|---|---|---|
| 0 | baseline (M7 questions and thresholds) | 0.556 | 0.544 | 1/0 | — |
| 1a | state as a plain string | 0.478 | 0.489 | 5/0 | no |
| 1b | state as `"Task: ..."` string | 0.519 | 0.511 | 5/1 | no |
| 1c | state key `task` | 0.552 | 0.550 | 1/0 | no (tune not better) |
| 1d | state key `user_request` | 0.574 | 0.539 | 2/0 | no (holdout drops, u rises) |
| 1e | state adds `domain` | 0.556 | 0.544 | 0/0 | no (no change) |
| 1f | state adds `source` | 0.481 | 0.444 | 0/0 | no |
| 2a | difficulty levels described (simple…expert), old thresholds | 0.563 | 0.544 | 1/0 | no (thresholds never reached) |
| 2b | difficulty levels with examples, old thresholds | 0.548 | 0.539 | 1/0 | no |
| 2c | 2a/2b with thresholds searched on tune | 0.693 / 0.696 | 0.633 / 0.667 | 4/4, 2/1 | no (not better than tuned original wording on holdout; u rises) |
| 3a | thresholds searched on tune, uncertainty fallback off | 0.685 | 0.672 | 3/3 | no (u rises: complex plans reviewed as optional) |
| 3b | 3a plus planning thresholds, under-review weight 0.02 | 0.685 | 0.667 | 2/1 | no (u rises) |
| 3c | thresholds searched with under-review weight 0.1: no route uncertainty fallback; review uncertainty band 0.4–0.6; sensitive ≥ 0.4, risk ≥ 0.3; difficulty coding ≥ 1.3, complex ≥ 1.6, expert ≥ 1.9; planning required ≥ 1.0, none < 1.0 | **0.659** | **0.650** | 1/0 | **yes** |
| 4a | `is_sensitive` reworded with yes/no meanings (threshold re-searched) | 0.648 | 0.639 | 0/0 | no (AUC for escalated drops 0.94→0.85 tune, 0.97→0.80 holdout) |
| 4b | `is_high_risk` reworded with yes/no meanings | 0.567 | 0.567 | 0/0 | no |
| 4c | `needs_tools` reworded with yes/no meanings | 0.667 | 0.644 | 2/1 | no (holdout drops, u rises) |
| 4d | `needs_private_context` reworded with yes/no meanings | 0.652 | 0.656 | 1/0 | no (tune not better) |
| 5a | routine-difficulty implementation/review intents route to coding | 0.678 | 0.639 | 1/0 | no (holdout drops) |
| 5b | new `writes_code` question routes to coding (AUC 0.71 tune / 0.79 holdout) | 0.667 | 0.650 | 1/0 | no (below minimum effect: +0.008 tune, holdout flat; adds a question) |

Rule added after 5b: a change must gain at least 0.01 on tune, or gain on holdout,
to justify added complexity.
| 6a | intent: configuration_or_operations described as operating systems | 0.656 | 0.644 | 3/0 | no |
| 6b | intent: implementation described as source-code work | 0.652 | 0.683 | 3/0 | no (tune drops, u rises) |
| 6c | 6a + 6b | 0.644 | 0.683 | 4/0 | no (tune drops, u rises) |
| 6d | intent instruction "What does the user want as the end result…" | 0.670 | 0.639 | 1/0 | no (holdout drops) |
| 6e | intent: review described as judging without changing | 0.630 | 0.650 | 1/0 | no |
| 6f | intent: research described as reporting without changing | 0.630 | 0.644 | 0/0 | no |

Wording variants move tune and holdout in opposite directions by about ±0.03:
that is the measurement noise at this corpus size.
| 7a | learned layer: logistic regression on all Laya probabilities (regularization by 5-fold CV on tune) for route and review; Laya's own intent | 0.800 | 0.722 | 4/4 | no (u rises; all optional→none) |
| 7b | as 7a, intent also learned | 0.822 | 0.711 | 5/4 | no (u rises; intent holdout drops) |
| 7c | as 7a, review chosen by least expected cost with under-review costing w=3 per level (w picked by CV on tune: w=2 obj 0.704, w=3 0.715) | **0.778** | **0.711** | 1/0 | **yes** |
| 8a | + `writes_code` question as a feature | 0.774 | 0.700 | 1/0 | no |
| 8b | + reworded sensitivity question as a second feature | 0.759 | 0.700 | 1/0 | no |
| 8c | + reworded risk question as a second feature | 0.778 | 0.694 | 1/1 | no |
| 8d | + reworded tools question as a second feature | 0.756 | 0.689 | 1/0 | no |
| 8e | + described difficulty levels as a second score | 0.752 | 0.689 | 1/1 | no |
| 8f | + a five-way "which worker" choice | 0.781 | 0.683 | 1/3 | no (holdout drops, u rises) |
| 8g | + `changes`: "Will doing this task change files, systems or data, rather than only producing an answer or a document?" | **0.796** | **0.728** | 1/0 | **yes** |
| 8h | − `needs_private_context` | 0.781 | 0.717 | 1/0 | superseded by 9 (tested on 7c base) |
| 8i | − `needs_tools` | 0.752 | 0.683 | 1/0 | no |
| 9a | 8g − `needs_private_context` | **0.807** | **0.733** | 1/0 | **yes** (also one question fewer) |
| 9b | 9a base − `is_high_risk` (tested on 8g) | 0.789 | 0.717 | 1/1 | no |
| 9c | 8g + `writes_code` | 0.774 | 0.711 | 1/1 | no |
| 9d | `changes` reworded as "asks for something to be changed, created, deployed, deleted or fixed" | 0.774 | 0.678 | 1/1 | no |
| 9e | 8g + 9d wording as a second question | 0.785 | 0.717 | 1/1 | no |
| 9f | 8g + `production` (touches production, real data, credentials, irreversible) | 0.770 | 0.694 | 1/1 | no |
| 10a | regularization by CV repeated over 3 shuffles | 0.770 | 0.728 | 1/0 | no |
| 10b | 10a with the one-standard-error rule | 0.700 | 0.633 | 0/0 | no |
| 10c | 10-fold CV with the one-standard-error rule | 0.733 | 0.683 | 1/1 | no |
| 11a | 9a + eight probe questions (quick fact, specialist field, multi-component, five minutes, small edit, new design, production ops, reads code) | 0.826 | 0.694 | 0/2 | no (fits tune, holdout drops) |
| 11b–i | 9a + each probe alone | 0.767–0.811 | 0.694–0.733 | — | no (none beats 9a on holdout) |

Learning curve on 9a (holdout accuracy, mean of 5 subsamples): route stays at
0.55–0.58 from 36 to 90 training tasks (limited by Laya's features); review
rises 0.72 → 0.83 (limited by data). Next: more labeled data.

## Corpus expanded to 300 (180 tune, 120 holdout)

Re-scored on the larger corpus (holdout S): baseline 0.569, 3c 0.647, 7c 0.672,
8g 0.669, 9a 0.686. The gains hold but are smaller than the 60-task holdout
suggested.

Review errors, re-examined against how `tp-review` uses the level (`none` skips
a review, `optional` and `required` both run it). Acceptance rule from here on:
no `required` task may drop in CV on tune; then maximize accuracy of the
review/no-review decision. This replaces the raw under-review count.

| # | change | T score | H score | u (T/H) | kept |
|---|---|---|---|---|---|
| 12a | 9a with under-review cost re-picked by CV objective accuracy − under-rate: w=1.5 | 0.728 | 0.694 | 10/7 | no (optional→none 5% in CV) |
| 12b | 9a with w=4 (the smallest w with no required downgrade in CV; CV: optional→none 1.9%, unneeded reviews 16.1% vs baseline 1.1% / 20.6%) | **0.702** | **0.683** | 2/2 | **yes**: holdout required→none 0 (baseline 0), optional→none 1 (1), required→optional 1 (0), unneeded reviews 18 (24), review/no-review accuracy 0.842 (0.792), route 0.55 (0.31) |
| 13a | review as a binary review/no-review classifier (threshold 0.5 by CV on tune: required skipped 0, review-or-not 0.894) plus an optional/required head (threshold 0.1: the highest with no required→optional in CV) | 0.856 review | 0.808 review | — | no: holdout skips 1 required review ("Show a progress bar…", Laya intent implementation) |
| 13b | 13a + guardrail: Laya intent implementation or configuration_or_operations always reviews | 0.811 review | 0.800 review | — | superseded by 13c (tie on tune review-or-not 0.889; 13c skips fewer) |
| 13c | 13b + guardrail: is_sensitive ≥ 0.5 always reviews (0.4 ties on tune; 0.5 is Laya's yes boundary) | **0.806 review, 0.889 review-or-not** | **0.800 review, 0.892 review-or-not** | — | **yes**: holdout required skipped 0 (12b: 0), required→optional 0 (1), unneeded reviews 9 (18), optional skipped 4 (1) |
| 14a | intent from a learned head over Laya's answers (tune CV 0.794 vs Laya's own 0.722; holdout research precision 0.88 vs 0.71), driving the research exemption and the change guardrail | **0.856 review, 0.922 review-or-not** | **0.833 review, 0.917 review-or-not** | — | **yes**: holdout required skipped 0, optional skipped 4, unneeded reviews 6 (13c: 0, 4, 9); intent 0.783 (0.742) |
| 15a | route with the eight probe questions again, now on 180 tune tasks | CV 0.526 | 0.542 | — | no (current: CV 0.544, holdout 0.550) |
| 15b | ordinal route model (cumulative binary heads) | CV 0.448 | 0.408 | — | no |
| 15c | route with learned intent probabilities stacked in | CV 0.543 | 0.550 | — | no (no change) |
| 16 | cost-aware routing: least expected cost over model tiers, under-tiering costing w per tier | CV at w=1/1.5/2/3: under-tier 23/15/13/10%, over-tier 14/24/33/44% | — | — | no change: it trades under- for over-tiering one for one; w is a quality-versus-spend preference, left for the user |
| 17a | route from Laya's encoder embedding (PCA 16/32/64) with or without the answers | CV 0.426–0.533 | 0.425–0.625 | — | no (tune CV below current 0.544) |
| 17b | intent from embedding (PCA 64) + answers | CV 0.826 | 0.875 intent | — | no: better intent (tune CV +0.03) but holdout review decisions identical to 14a; needs an embedding service Laya does not provide |
| 17c | review gate from embedding + answers | CV 0.861–0.880 | 0.883–0.900 | — | no (tune CV below current 0.885) |

## Result after cycle 17 (holdout, 120 tasks, 95% bootstrap CI of the paired difference)

| metric | M7 baseline | learned layer | difference |
|---|---|---|---|
| intent | 0.742 | 0.783 | +0.042 [−0.033, +0.117] |
| route tier | 0.308 | 0.550 | +0.242 [+0.125, +0.350] |
| route model | 0.358 | 0.658 | +0.300 [+0.200, +0.392] |
| review level | 0.658 | 0.833 | +0.175 [+0.108, +0.250] |
| review or not | 0.792 | 0.917 | +0.125 [+0.058, +0.200] |
| required reviews skipped | 0 | 0 | 0 |
| unneeded reviews | 24 | 6 | −18 |

Open levers: route is limited by what Laya's answers carry (learning curve flat,
probes and embeddings did not help on tune); an embedding service improves
intent but not review decisions; route cost weight is a quality-versus-spend
choice; the best next data is real tmux-pi tasks labeled by the user.

## Worker models for tool chores (2026-09-26)

Tool-calling agents on 40 read-only repository chores in 8 categories (answers
computed from the repository), with a minimal tool loop (grep, read, find,
bash) on a copy of the repo. Local models on an M5 Max, 4-bit MLX, thinking off.

| model | correct | median per chore | notes |
|---|---|---|---|
| Slotstream Qwen3.8 Flash (prefix cache on) | 40/40 | 13.0 s | first 10-chore run without prefix cache: 32.2 s median |
| Qwen3.5-4B (mlx-lm, 3 runs) | 111/120 | 1.5 s | counting 18/21 + 2/3 line counts; git "first added" 1/3 |
| MiniCPM5-2B (10-chore set, 3 runs) | 26/30 | 1.6 s | needs its own tool-call parser |
| Granite 4.2-3B (10-chore set, 3 runs) | 23/30 | 3.8 s | invented tools; confident wrong answers |

Qwen3.5-4B's recurring mistake is exact counting: it reports a number without
computing it (46 functions for 25, 20 models for 13). A local rule sends tasks
matching `how many|count|number of|total number` that were routed to Qwen to
Slotstream (10/10 on counting). Replaying the Qwen runs with the rule leaves
5 of 120 wrong (95.8%). Almost every failure was a confident wrong answer, not
a tool-error streak: only 2 of 120 runs would have escalated.

Counting prompt for Qwen3.5-4B (10 counting chores × 5 runs; appended to the
system prompt):

| prompt | correct |
|---|---|
| none | 38/50 |
| A: "…compute the number with a command… never count by reading output yourself" | 40/50 |
| B: "For any count, run a command that counts… and report its output exactly" | 44/50 |
| C: "Only answer a count with a number printed by a counting command you have run (grep -c, wc -l, or jq length for JSON); if you have not run one yet, run it before answering" | 45/50 |

With C, 4 of the 5 failures were loops without an answer (visible; one would
escalate) and 1 a wrong number, against mostly wrong numbers without it. On all
40 chores C scored 111/120, the same as without it. 76% → 90% is suggestive,
not conclusive (Fisher exact p ≈ 0.07). Kept: C as a standing prompt for Qwen
workers, and the counting rule to Slotstream for tasks that ask for a count.

## Placement (M8, 2026-09-26)

Placement labels (main or subagent, `LABELS.md`) were added to all 300 tasks, and
Laya was given an `execution_scope` question (brief, sustained, multi_step).
Change-making intents and required reviews always delegate; the question is
what else to keep in main.

| # | change | T placement | H placement | kept in main that should delegate (T/H) | kept |
|---|---|---|---|---|---|
| 18 | rule: delegate unless Laya's scope is brief | 0.761 | 0.792 | 2/1 | baseline (Laya says brief for 20 of 180) |
| 18a | brief when P(brief) ≥ threshold (0.35 by tune objective) | 0.767 | 0.783 | 2/1 | no (holdout drops) |
| 18b | learned placement head over the answers + scope probabilities; threshold 0.2 by 3×5-fold CV on tune (objective: accuracy − 2 × rate of delegable work kept in main; 0.2 ties 0.35 with fewer kept in main) | **0.811** | **0.808** | **0/0** | **yes** |

## Follow-up routing (M8 phase 2, 2026-09-26)

Does a new message belong to a running delegated task? `followups.jsonl`: 56
messages against 12 task/last-reply pairs; 28 follow-ups (answers, corrections,
extensions, questions about the work) and 28 separate requests, many in the same
project; 34 tune, 22 holdout.

| judge | separation | auto-decided (T/H) | wrong when auto (T/H) | asked (T/H) | median |
|---|---|---|---|---|---|
| Laya noul "is it about the active task" | true 0.45–0.67, false 0.10–0.62 on the first 10 | — | — | — | — |
| Laya choice same_task/new_task | AUC 0.85 | — | — | — | — |
| Laya score unrelated/loosely/same (0–2); unsure band from tune | AUC 0.89 | 8/8 | 0/0 at band (0.61, 1.46) | 26/14 | ~0.1 s |
| **Qwen3.5-4B (MLX), "worker or separate", P(worker) from top logprobs; ask in (0.25, 0.9)** | 55/56 greedy | **28/19** | **0/0** | **6/3** | **0.18 s** |

Kept: Qwen as the judge where `config/local.json` names one (`followup`), Laya's
score otherwise (the plan named Laya; it separates too poorly to decide most
messages on its own). With two running tasks (each message paired with an
unrelated second task), context-free replies such as "yes go ahead" claim both,
so the leading task must be at least 0.15 ahead of the next: 39 right, 0 wrong,
17 asked, of 56.


## Packet file selection (M8, 2026-09-27)

Should a worker's packet include files picked for the task, and who should pick?
30 chores on a snapshot of this repo that do not say where the answer is (24
questions checked by answer regex, 6 edits checked by a command). Each worker
got a tp-spawn-style packet (the task, then up to 3 picked files, each cut to
16,000 characters) and a grep/read/find/bash/edit tool loop in a fresh copy.
The picker saw the task and the file list with sizes, and answered with paths.

| picker | picked the needed file | time per pick |
|---|---|---|
| Qwen3.5-4B, file list only | 18/30 | ~0.5 s |
| Qwen3.5-4B, list with one-line descriptions | 17/30 | ~0.5 s |
| Qwen3.5-4B, descriptions + a grep of the task's key terms | 20/30 | ~0.5 s |
| Haiku 4.5 (`claude -p`, no tools) | 24/30 | 7.4 s median |
| **Sonnet 5 (`claude -p`, no tools)** | **26/30** | **~1.9 s** |

| worker | packet | passed | read / edit | median | tool calls | prompt tokens |
|---|---|---|---|---|---|---|
| Qwen3.5-4B (2 runs) | core only | 44/60 | 36/48, 8/12 | 33.2 s | 5.9 | 31.6k |
| Qwen3.5-4B (2 runs) | + Qwen-picked files | 50/60 | — | 30.8 s | 4.3 | 69.9k |
| Qwen3.5-4B (2 runs) | **+ Sonnet-picked files** | **53/60** | **44/48, 9/12** | **27.8 s** | **3.2** | 50.4k |
| Slotstream (1 run) | core only | 28/30 | — | 101 s | 6.7 | 42.7k |
| Slotstream (1 run) | + picked files (either) | 28/30 | — | 101–113 s | ~4.2 | 55–57k |

On the chores where Sonnet picked the needed file, Qwen went from 39/52 to 49/52.
Kept: picked files for Qwen3.5-4B only (`packet_files` in `config/local.json`),
picked by Sonnet 5; Slotstream and stronger workers get the core only, since
they find files themselves at no loss. The plan assumed the local model would
pick well from its locate-chore results; with only a file list it does not.

## Standard-tier worker: Qwen3.6-35B-A3B replaces Qwen3.5-4B (2026-09-28)

Candidates on the same chores, thinking off unless noted. Counting chores are the
10 counting chores of the 40, 5 runs each; the held-out set adds 6 counting chores
not used to write the prompts (16 × 5).

| worker | counting (no prompt / prompt C) | 40 chores × 3 | median | packet chores (core / + Sonnet files) |
|---|---|---|---|---|
| Qwen3.5-4B (mlx-lm) | 38/50 / 45/50 | 111/120 | 1.5 s | 44/60 / 53/60 (33 s / 28 s) |
| **Qwen3.6-35B-A3B MLX 4-bit (LM Studio)** | **41/50 / 46/50** | **117/120** | **1.1 s** | **52/60 / 56/60 (5.3 s / 5.1 s)** |
| Qwen3.6-35B-A3B, thinking on | 48/50 | — | 7.5 s | — |
| Qwen3.6-35B-A3B (mlx-lm) | — | 39/40 | 1.3 s | 28/30 (6.2 s) |
| Bonsai 27B (LM Studio) | 21/50 off; 10/10 on (22 s) | — | — | — |
| Hammer2.1-3B | — | 0/40 (ends with `[]`, no answer) | — | — |
| Slotstream (Qwen3.8 Flash) | 10/10 | — | 13 s | 28/30 (101 s) |

Counting prompts on Qwen3.6, held-out set: C (answer counts only from a counting
command) 73/80, D 73/80, **E (the grep tool lists lines and never counts them;
run `grep -c`, `wc -l` or `jq length` and report its number) 77/80**, 30/30 on
the held-out chores. Qwen3.6 takes about 19 GB idle and 25 GB peak (long-prompt
prefill); hybrid attention keeps the KV cache small. Slotstream cannot serve it
(its checkpoint validation is specific to Qwen3.8 Flash).

Follow-up judge, `followups.jsonl`, same prompt and band (0.25, 0.9),
`reasoning_effort: "none"`: 54/56 greedy; auto-decided 30/20 (T/H), 0 wrong,
asked 4/2, median 0.14 s (Qwen3.5-4B: 28/19, 0 wrong, 0.18 s). With a second
unrelated running task: 36 right, 0 wrong, 20 asked.

Kept: Qwen3.6-35B-A3B from LM Studio for the `standard` route, escalation,
packet files and the follow-up judge, with prompt E; the counting rule to
Slotstream is dropped (prompt E gets counts right at about 1 s against 13 s).
The mlx-lm server for Qwen3.5-4B is no longer run.

## Summarize chores

`summarize_files` is `candidate` in the model catalog until chores show Qwen3.6-35B-A3B (LM Studio) does it as reliably as the
qualified shapes (holdout at least 57/60, 95%, with zero fabricated citations). The chores are in
`tests/calibration/summarize/`: 24 over 10 frozen fixture files (12 tune, 12 holdout), each with required facts, forbidden
fabrications and the files a `path:line` citation may name. `tools/qual/summarize_check.py` scores a reply with no model;
`tools/qual/summarize_run.py` runs one headless Pi worker per chore and run on a temp copy of its files, with the system
prompt tp-spawn would build (`--prompt-file` replaces the model's `config/local.json` prompt). `--route-only` runs
`tp-route --no-log --local-only` on each chore text and records what the tool really prints: exit code, level, model, source and
reason of a route, or the stderr of a stop, with an outcome (`routed`, `shape_not_qualified` for "outside the qualified local
shapes", `stop`, `error`, `timeout`). `tp-route` does not print or log the router's shape tag, so tagging accuracy is read from
the outcome: routed local once the shape is qualified, `shape_not_qualified` while it is a candidate.

Protocol (run from a plain shell with LM Studio up): `--route-only` on all 24; tune split x3 without and with the draft rule in
`config/local.example.json`; choose the prompt on tune only; holdout x5 with the chosen prompt. Results: not run yet.

The checker is lenient on wording (a fact is any of a few regexes for one thing) and strict on form, completeness and evidence.

Round 6 (the format is fixed and parsed exactly; rounds 2 to 5 graded free prose with regexes and each round's review found a
new prose shape that slipped through the same three gaps: required-fact completeness, citation-support coverage, citation-file
validation). A reply is a list of FACT LINES and only those count:

    - <claim> [<path>:<line>[-<line>]][, <path>:<line>...]

1. One fact per line: it starts with `- `, the claim is words, and it ends with one bracketed citation list. The path is a chore
   file written in full. No heading, bold line, earlier bullet or `Line N` supplies a file, and a line with two bracketed lists
   is not a fact line (it is `two_citation_lists`, rule 1).
2. Any other line (heading, sentence, blank, an indented `  - ` line) is ignored, for facts and for citations. A `- ` line with no
   valid trailing citation list is `uncited` and fails the reply: no partial credit, nothing is borrowed from its neighbours.
3. A citation is valid only if its file is a chore file, the range is inside the file and at most 5 lines (`range_too_wide`
   otherwise), and the range covers one of that fact's `support_lines`. Not valid: `unknown_file`, `range`, `range_too_wide`
   (a bad citation); on a line that states the fact but cites other lines: `unsupported`; no good citation at all:
   `missing_citation`.
4. A required fact is met only by ONE fact line whose claim matches AND whose citations support it (same line). Facts are met by
   different lines (bipartite matching), so a one-line list of everything satisfies one fact (`shared_line`, rule 1).
5. A behaviour fact is matched against the claim with every backticked span, every `path:line`, and every identifier of the
   cited files' symbol table removed. The symbol table is computed from the fixture with `ast` (functions, classes, parameters,
   names, attributes, keyword arguments, imports and aliases, `except` names, identifier-like strings), so it is a superset of
   "names defined or parameters"; no list is kept by hand. A claim of only names and signature text has nothing left to match
   and, when no fact needs it, is `identifier_only` (rule 5). Python keywords are stripped for that test only, never for matching.

The result also lists `failures`: one `{rule, kind, line, text}` per failed line, so a failed reply says which rule 1 to 5 it
broke. The runner keeps the raw reply. The prompt in `config/local.example.json` states the format with one example.

Completeness: every item a chore asks for (each using file, export, step, state, difference) is its own required fact, never one
alternation over the items. Where the task asks for an order (the states, the deploy steps) `order` lists the facts and the
position of the first match of each must be greater than the one before. Each answer carries a hand-written `reference` reply, one
fact line per fact (`facts` says which); the tests check that it passes (24 of 24), that dropping the line for one fact fails with
that fact among the failed ones, and that every line of every reference shifted by one line passes none.

`support_lines` (one entry per fact) is `{"src/x.py": ["14", "20-22"]}`: the fixture lines that are evidence of the fact. For what
code does it is the lines that do it (body, return, raise, a docstring that states it), the signature only if it alone shows it;
for a name, its definition line. `anchors` records a short exact substring of each accepted line, and
`test_support_is_a_set_of_lines_and_every_anchor_is_on_its_line` fails when a fixture edit moves one. The set accepts
`src/worker.py:22` for echo and the docstring `src/cache.py:14` for get, and refuses the signatures next to them. A good citation
that no fact's support vouched for (on a line that states no fact, or overlapping no support set there) is `unchecked`: counted
per run and not a failure; S2 reports the count. A correct reply that merely mentions a forbidden claim to deny it can be scored
as a hit; read the failing replies before changing a prompt for them.

Main's saved smoke reply (use-jobqueue; "### src/<file>" headings, "- **Line 29** -- ..." bullets) now fails by rule 2: its 9 bullets
have no trailing citation list (`uncited`), the headings and prose count for nothing, and no fact is stated (rule 4). Round 5
scored it as 11 bad citations; the model is now told the format by the prompt, and the live runs show whether it follows it.


### Audit of the required facts (round 6)

Each chore against its text and fixtures: what the chore asks for, the required fact that covers each (F1 is the first in
`answers.jsonl`), the lines of the fixtures that are evidence of it (`file:lines`, from `support_lines`), and which facts are
behaviour facts, matched on words that say what the code does (the others are names, steps, states or values).
`tests/test_qual_summarize.py` checks that every chore has one row that names each of its facts and its support sets.

| Chore | What the chore asks for | Required facts | Support lines | Behaviour facts |
|---|---|---|---|---|
| use-jobqueue | which files use JobQueue (cli.py, jobq.py, worker.py) and what each uses it for | F1 cli.py creates a JobQueue; F2 cli.py pushes a job; F3 jobq.py defines JobQueue; F4 worker.py receives the queue as an argument; F5 worker.py pops jobs; F6 worker.py fails or retries a job | F1 cli.py:29; F2 cli.py:31; F3 jobq.py:23; F4 worker.py:30,48; F5 worker.py:31-32; F6 worker.py:38,42-44 | F1, F2, F4, F5, F6 |
| use-load-settings | which files call load_settings (cli.py, worker.py) and what each does with the result | F1 cli.py calls load_settings; F2 cli.py passes the result to run_forever; F3 worker.py calls load_settings; F4 worker.py reads poll_seconds | F1 cli.py:25; F2 cli.py:34; F3 worker.py:50; F4 worker.py:49,53 | F1, F2, F3, F4 |
| use-lrucache | which files use LRUCache (none) and where it lives | F1 no other file uses it; F2 cache.py defines LRUCache | F1 cli.py:6-8, worker.py:2-3,5, jobq.py:2-3; F2 cache.py:6 | none |
| use-utils | which files use parse_duration, chunked, slugify (none) and where they live | F1 no file uses the helpers; F2 utils.py defines the helpers | F1 cli.py:6-8, worker.py:2-3,5, settings.py:2-3; F2 utils.py:1,8,16,22 | none |
| use-worker-imports | each import of worker.py (logging, time, load_settings) and what it is used for | F1 imports logging; F2 logging is used to log errors; F3 imports time; F4 time is used to sleep; F5 imports load_settings; F6 load_settings gives run_forever its default settings | F1 worker.py:2; F2 worker.py:7,37,43; F3 worker.py:3; F4 worker.py:27,52-53; F5 worker.py:5; F6 worker.py:50 | F2, F4, F6 |
| exports-jobq | each export of jobq.py (Job, JobQueue, MAX_RETRIES, BACKOFF_SECONDS, backoff_for): its name and its job | F1 exports Job; F2 exports JobQueue; F3 exports MAX_RETRIES; F4 exports BACKOFF_SECONDS; F5 exports backoff_for; F6 Job holds name, payload, priority, attempts; F7 JobQueue has push, pop, fail and a dead list; F8 MAX_RETRIES is the retry limit; F9 BACKOFF_SECONDS are the waits; F10 backoff_for returns the wait; F11 the last backoff repeats | F1 jobq.py:15; F2 jobq.py:23; F3 jobq.py:5; F4 jobq.py:6; F5 jobq.py:9; F6 jobq.py:16-20; F7 jobq.py:25,27,29-30,33-34,39-40; F8 jobq.py:5,40,42; F9 jobq.py:6,12; F10 jobq.py:10-12; F11 jobq.py:10-11 | F6, F7, F8, F9, F10, F11 |
| exports-utils | each function of utils.py (parse_duration, chunked, slugify): its name and what it does, and the units and error of parse_duration | F1 exports parse_duration; F2 exports chunked; F3 exports slugify; F4 parse_duration converts a duration string to seconds; F5 chunked yields lists of at most size items; F6 slugify lowercases and joins with hyphens; F7 the accepted suffixes are s, m, h; F8 anything else raises ValueError | F1 utils.py:8; F2 utils.py:16; F3 utils.py:22; F4 utils.py:9-10,13; F5 utils.py:17-19; F6 utils.py:23-24; F7 utils.py:4-5,9; F8 utils.py:9,11-12 | F4, F5, F6, F7, F8 |
| exports-cache | each method of cache.py (LRUCache.get, put, clear, __len__): its name and its job, and the ttl and capacity | F1 exports LRUCache; F2 has get; F3 has put; F4 has clear; F5 has __len__; F6 get returns the cached value and marks it used; F7 put stores a value and evicts when over capacity; F8 clear empties the cache; F9 len gives the number of entries; F10 an optional ttl makes entries expire; F11 the capacity limits how many entries it keeps | F1 cache.py:6; F2 cache.py:13; F3 cache.py:25; F4 cache.py:32; F5 cache.py:35; F6 cache.py:14,17,21-23; F7 cache.py:26-30; F8 cache.py:33; F9 cache.py:36; F10 cache.py:1,9,19-20; F11 cache.py:8,26,29-30 | F6, F7, F8, F9, F10, F11 |
| exports-settings | the exports of settings.py (DEFAULTS, ENV_PREFIX, load_settings) and the prefix value | F1 exports DEFAULTS; F2 exports ENV_PREFIX; F3 exports load_settings; F4 the prefix is JOBQ_ | F1 settings.py:5; F2 settings.py:12; F3 settings.py:15; F4 settings.py:12 | none |
| exports-worker | each name worker.py exports (HANDLERS, handler, echo, sleep_job, run_one, run_forever): its name and its job | F1 exports HANDLERS; F2 exports handler; F3 exports echo; F4 exports sleep_job; F5 exports run_one; F6 exports run_forever; F7 HANDLERS maps job names to functions; F8 handler is the registering decorator; F9 echo returns its payload; F10 sleep_job sleeps for the seconds in its payload; F11 run_one pops and runs one ready job; F12 run_forever loops and sleeps when idle | F1 worker.py:9; F2 worker.py:12; F3 worker.py:21; F4 worker.py:26; F5 worker.py:30; F6 worker.py:48; F7 worker.py:9,13,15,35; F8 worker.py:13,15; F9 worker.py:22; F10 worker.py:27; F11 worker.py:31-32,41; F12 worker.py:49,51-53 | F7, F8, F9, F10, F11, F12 |
| steps-states | the five states, in the order the doc lists them | F1 state 1 pending; F2 state 2 running; F3 state 3 succeeded; F4 state 4 retrying; F5 state 5 dead | F1 lifecycle.md:5; F2 lifecycle.md:6; F3 lifecycle.md:7; F4 lifecycle.md:8; F5 lifecycle.md:9 | none |
| steps-dead | what happens to a dead job (sits in queue.dead, never run again) and whether it survives a restart (memory only, dropped) | F1 it sits in queue.dead; F2 it is never run again; F3 dead jobs are kept in memory only; F4 a restart drops them | F1 lifecycle.md:9; F2 lifecycle.md:9; F3 lifecycle.md:13; F4 lifecycle.md:13 | none |
| steps-deploy | the six deploy steps, in order | F1 step 1 run the unit tests; F2 step 2 tag the release; F3 step 3 build the image; F4 step 4 push the image; F5 step 5 roll the workers; F6 step 6 roll back if the dead count rises | F1 deploy.md:3; F2 deploy.md:4; F3 deploy.md:5; F4 deploy.md:6; F5 deploy.md:7; F6 deploy.md:8 | none |
| steps-watch | what to watch after rolling the workers (the dead count), for how long (ten minutes), and what to do if it rises (roll back) | F1 watch the dead count; F2 for ten minutes; F3 roll back to the previous tag | F1 deploy.md:7; F2 deploy.md:7; F3 deploy.md:8 | none |
| steps-retries | the retry limit (at most 3) and the three waits (1 s, 5 s, 30 s) | F1 at most 3 retries; F2 wait of 1 s; F3 wait of 5 s; F4 wait of 30 s | F1 limits.md:3; F2 limits.md:3; F3 limits.md:3; F4 limits.md:3 | none |
| diff-settings | how settings.py and settings_v2.py differ (workers 2 -> 4, state_file dropped, max_payload_bytes and validate_settings added, load_settings validates) | F1 v2 default workers is 4 (v1: 2); F2 v2 drops state_file; F3 v2 adds max_payload_bytes; F4 v2 adds validate_settings; F5 v2 load_settings validates | F1 settings_v2.py:6, settings.py:6; F2 settings.py:9, settings_v2.py:5,10; F3 settings_v2.py:9; F4 settings_v2.py:15; F5 settings_v2.py:33 | F5 |
| diff-load | whether load_settings behaves differently between settings.py and settings_v2.py, and how (validates, workers below 1, poll_seconds not positive, ValueError; v1 unchecked) | F1 yes, it differs; F2 v2 validates; F3 a workers value below 1 is rejected; F4 a poll_seconds that is not positive is rejected; F5 it raises ValueError; F6 v1 returns the settings unchecked | F1 settings.py:15,25, settings_v2.py:23,33; F2 settings_v2.py:33; F3 settings_v2.py:16-18; F4 settings_v2.py:16,19-20; F5 settings_v2.py:16,18,20; F6 settings.py:16-25 | F2, F3, F4, F5, F6 |
| diff-order | how the ordering and the removal rules of cache.py and jobq.py differ (cache: recency, eviction, ttl; queue: ready_at, priority, push order, pop, dead list) | F1 the cache orders by recency of use; F2 the cache evicts over capacity; F3 cache entries expire by ttl; F4 the queue orders by ready_at first; F5 then by priority, lower first; F6 then by push order; F7 pop removes the next due job; F8 the queue never evicts; failures go to the dead list | F1 cache.py:11,22,28; F2 cache.py:26,29-30; F3 cache.py:19-21; F4 jobq.py:30-31; F5 jobq.py:30-31; F6 jobq.py:26,30-31; F7 jobq.py:34-36; F8 jobq.py:40,42-43 | F1, F2, F3, F4, F5, F6, F7, F8 |
| diff-nohandler | whether worker.py's handling of a job with no handler agrees with the doc (logs an error, moves it to queue.dead; the doc says dead) | F1 yes, it agrees; F2 worker.py logs an error; F3 worker.py appends the job to queue.dead; F4 the doc says no handler means dead | F1 worker.py:36-38, lifecycle.md:9; F2 worker.py:37; F3 worker.py:38; F4 lifecycle.md:9 | F2, F3 |
| sum-worker | 3 to 5 bullets on worker.py (handler registry, run_one pops a job, failure retried, no handler -> dead, run_forever loop) | F1 a registry of handlers; F2 run_one pops a ready job; F3 a failing job is retried; F4 a job with no handler is dead; F5 run_forever loops and sleeps | F1 worker.py:9,13,15; F2 worker.py:31-32; F3 worker.py:42-44; F4 worker.py:37-38; F5 worker.py:49,51-53 | F1, F2, F3, F4, F5 |
| sum-cache | 3 to 5 bullets on cache.py (an LRU cache, capacity, optional ttl, injectable clock) | F1 it is an LRU cache; F2 a capacity limit; F3 an optional ttl expires entries; F4 an injectable clock | F1 cache.py:1,11,22,28,30; F2 cache.py:8,26,29-30; F3 cache.py:1,9,19-21; F4 cache.py:7,10,19,27 | F1, F2, F3, F4 |
| sum-lifecycle | 3 to 5 bullets on lifecycle.md (five states, retrying with a backoff, dead after the third retry or no handler, dead is final, memory only) | F1 five states; F2 retrying with a backoff; F3 dead after the third retry or no handler; F4 dead is final; F5 dead jobs are in memory only | F1 lifecycle.md:3; F2 lifecycle.md:8; F3 lifecycle.md:9; F4 lifecycle.md:9; F5 lifecycle.md:13 | none |
| sum-cli | 3 to 5 bullets on cli.py (run, enqueue, handlers, --config, enqueue is in this process only) | F1 the run command; F2 the enqueue command; F3 the handlers command; F4 the --config option; F5 enqueue is in this process only | F1 cli.py:1,15,34; F2 cli.py:1,16,31; F3 cli.py:19,27; F4 cli.py:13,25; F5 cli.py:1,29 | F1, F2, F3, F4, F5 |
| sum-limits | 3 to 5 bullets on limits.md (3 retries, in memory, 2 workers, log level, no authentication) | F1 at most 3 retries; F2 the queue is in memory; F3 default 2 workers; F4 log level info; F5 no authentication | F1 limits.md:3; F2 limits.md:4; F3 limits.md:5; F4 limits.md:6; F5 limits.md:7 | none |
