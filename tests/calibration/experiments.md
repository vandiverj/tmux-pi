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
qualified shapes (holdout at least 57/60, 95%, with zero fabricated citations and zero unparsed references). The chores are in
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
2. A line that is not a list item (heading, sentence, blank) states no fact and is ignored, unless it holds a bracketed citation
   (round 8, rule 10: `stray_citation`). Every LIST ITEM is graded on the raw line (rules 9 and 11): a list item that is not a valid
   fact line fails the reply: `uncited` for a `- ` line with no valid trailing citation list, `not_a_fact_line` for any other list item
   (indented, any other bullet, numbered, a bare `-`, `-\t`). Nothing is borrowed from neighbours.
3. A citation is valid only if its file is a chore file, the range is inside the file and at most 5 lines (`range_too_wide`
   otherwise), and the range covers one of that fact's `support_lines`. Not valid: `unknown_file`, `range`, `range_too_wide`
   (a bad citation); on a line that states the fact but cites other lines: `unsupported`; no good citation at all:
   `missing_citation`.
4. A required fact is met only by ONE fact line whose claim matches AND whose citations support it (same line). Facts are met by
   different lines (bipartite matching), so a one-line list of everything satisfies one fact (`shared_line`, rule 1).
5. A behaviour fact is matched against the claim with every code span, every `path:line`, and every identifier of the cited files'
   symbol table removed. Code spans are removed as CommonMark reads them (round 7, rule 8): a run of N backticks up to the next run of
   exactly N backticks, so ``` ``returns`` ``` is removed too; a backtick with no closing run is removed as a character. The symbol
   table is computed from the fixture with `ast` (functions, classes, parameters, names, attributes, keyword arguments, imports and
   aliases, `except` names, identifier-like strings), so it is a superset of "names defined or parameters"; no list is kept by hand.
   A claim of only names and signature text has nothing left to match and, when no fact needs it, is `identifier_only` (rule 5).
   Python keywords are stripped for that test only, never for matching.
6. Subject (round 7). Each behaviour fact has a subject, `behaviour[k].name` in `answers.jsonl` (a symbol or a file name). The claim's
   subject is the FIRST token of the claim (code-span backticks removed for this step) that is a name in the cited files' symbol
   table or one of the chore's file names, in full or bare. A behaviour fact is met only when that token is the fact's subject. A
   claim with no such token has no subject and meets no behaviour fact. Failures: rule 6, `wrong_subject` or `no_subject`, reported
   for a line that meets no fact. The prompt tells the model to start each fact line with the name the fact is about.
7. Order (round 7, rule 12 in round 8). A fact line whose claim matches two or more of the ordered facts fails as
   `several_in_one_line` (rule 1): an intro line that lists every state cannot give each state the same position.
8. Code spans: see rule 5. 9. List items: see rule 2 and rule 11.
10. Every citation is checked, wherever it is (round 8). Any bracket of the reply with an item of the CITATION shape (`path:N`,
    `path:N-M`) is checked by rule 3, on any line: list or not, heading, quote, prose, inside or outside a code span or block. A bad
    one is in `bad_citations`, so in the run's `fabricated_citations`. A line that holds a citation and is no valid fact line fails
    the reply: `stray_citation` (rule 2) when it is no list item, `not_a_fact_line` / `uncited` / `two_citation_lists` when it is.
11. List items by any whitespace and any bullet (round 8). A line is unprefixed first: leading whitespace as `str.isspace` has it
    (U+00A0, U+2003, U+3000 ...), control and format characters (U+0000, U+200B, U+FEFF ...) and any number of `>` blockquote markers. It is a list item
    when it then starts with `-`, `*`, `+`, `•`, `‣`, `◦`, `⁃`, `–`, `—`, `·` or `<digits>.` / `<digits>)`, followed by whitespace of
    any kind or the end of the line. Only a line that is exactly `- ` + claim + citation list at column 0 is a fact line.
12. Order counts every line (round 8, Main's ruling). In a chore with `order`, every fact line that states an ordered fact is read in
    reply order and their ordered-fact indexes must be strictly increasing: a step stated twice is `repeated_step` (rule 7), a step
    out of order is `order`. Reference + reversed copy, reversed copy + reference and reference + reference all fail. A chore without
    `order` allows a repeated line. A restatement counts also when its own citation is wrong: it is still a line that states the step.
13. A line reference is found in any shape, anywhere, across line breaks (round 9). `PATH:N`, `PATH:N-M` (spaces allowed around `:`
    and `-`), `PATH#LN`, `PATH#LN-LM`, `PATH#LN-M`, `PATH line N`, `PATH, line N`, `PATH (line N)`, `PATH lines N-M` (any case), also inside
    `[]`, `()`, backticks, a code block, an HTML comment or nested brackets, with `\n`, `\r\n`, U+2028, U+00A0 or any Unicode space between
    its parts. PATH is a chore file (in full or by its bare name) or a token with `/` or ending in `.<letters>`; in a bracket or backticks it may hold spaces.
14. Every line reference is graded by rule 3 (round 9). A bad one is in `bad_citations`, so in the run's `fabricated_citations`, and fails
    the reply with its kind (`unknown_file`, `range`, `range_too_wide`). A basename is `unknown_file`, as before.
15. Only the canonical form earns credit (round 9, Main's ruling on F3). The exact `[path:line]` at the end of a fact line is the one
    form. Any other line reference fails the reply, kind `noncanonical_citation`, also when its target is good (then it is not counted as
    fabricated). The canonical bracket on a line that is no fact line stays `stray_citation` (rule 10).
16. Numbers by value (round 9, review 8 F2). Leading zeros (any Unicode decimal zero) are stripped before the cap of 18 significant digits:
    `000000000000000029` is line 29 at any padding, and 18 or more significant digits are capped (`range`), never converted, so a 5000-digit number cannot crash `int()`.
17. Normalize, then scan (round 10). The scan reads a copy of the reply in NFKC form in which every Unicode decimal digit is its value
    and every `isspace` character (line breaks included) is one space. Credit is judged on the text as written: a fullwidth colon or digit
    in a trailing citation is not the canonical form, so it is `noncanonical_citation` (and the line is `uncited`).
18. A path has no character list (round 10). Outside a delimiter PATH is the maximal run of non-whitespace characters, less trailing
    sentence punctuation (`.,;:!?)]}>'"` and the backtick or `*`); it is a path when it holds `/` or `\`, ends in `.<letters>`, or is a chore
    file's path or bare name. Inside a delimiter (`[]`, `()`, `<>`, backticks, quotes) PATH is everything from the opener to the line part
    (exactly, see rule 24); if that is not path-shaped the token outside the delimiter is used.
19. A line part comes in both orders (round 10). Forward: a run of separators (`:` `#` `,` `(` `·`, whitespace, line breaks), then optionally
    `L`, `l.`, `ln`, `line` or `lines` (any case), then a number or a range. Reverse: `line`, `lines` or `L` + number or range, then `of`, `in`
    or `at`, then PATH.
20. No window, linear time (round 10). Detection does not depend on the distance between the parts of a reference or on the reply's length:
    gaps of 0, 1, 4000, 4001 and 100000 spaces or line breaks are read the same, and a 1 MB reply is checked in under 2 CPU seconds on py3.9.
21. Table-free floor (round 10). After the canonical citations of fact lines and every reference of rules 17 to 19 are removed, a number
    (any Unicode digits) directly preceded by a path token or by a line word (`line`, `lines`, `L`, `l.`, `ln`, `#`) with only separators
    between them fails the reply, kind `unparsed_reference`. The runner counts these as `unparsed_references`, beside `fabricated_citations`;
    S2 needs both to be zero.
22. A digit is any character with a Unicode numeric value (round 11). For the floor and the grammar a digit is any character for which
    `unicodedata.numeric(c, None)` is not None (Nd, Nl, No: `፩`, `Ⅰ`, `①`, `½`, `²`). A number made only of Nd digits is read by value (rule 16).
    Any other numeric character in a line-number position fails the reply as `noncanonical_citation`, or as `unparsed_reference` where rule 21
    applies. There is no list of digit ranges; the interpreter's database decides (1862 such characters on py3.9, 2002 on py3.14).
23. Delimiters are exactly the rule-18 pairs (round 11). `[]`, `()`, `<>`, backticks and `'` / `"` with their typographic forms, each only as
    a matched pair. Any other leading character (`{`, `|`, a letter) and an opener with no closer is part of the path as written, so
    `{src/cli.py:6` is a reference to the file `{src/cli.py` (`unknown_file`, counted in `fabricated_citations`). Only the trailing punctuation of
    rule 18 is removed (U+201D and U+2019 join it, as the closers of the typographic quotes).
24. No rewriting inside a delimiter (round 12). Inside a matched pair PATH is exactly the text from just after the opener to the line part,
    with only the white space around it (line breaks included) trimmed. No punctuation is taken off either end, so `(;src/cli.py:6)` is the
    file `;src/cli.py` (`unknown_file`), like `[;src/cli.py:6]` and a bare `;src/cli.py:6`. Outside a pair only rule 18's trailing punctuation
    is removed. A reference that follows another in a list starts after the previous one and ONE separator (`,` or `;`).
25. Quotes and apostrophes (round 12, Main's ruling on review 11's F2). A quote (`'`, `"`, `‘ ’`, `“ ”`) opens a delimiter only at the start
    of the reply or after white space or an opening bracket, and closes one only before the end, white space or rule-18 trailing punctuation.
    Any other quote is an apostrophe and part of the word, as in `don't`. Brackets (`[]`, `()`, `<>`) and backticks are delimiters wherever
    they stand. So `x‘src/cli.py:6’` and `x'src/cli.py:6'` are the file `x‘src/cli.py` / `x'src/cli.py` (`unknown_file`), and `x(src/cli.py:6)`
    is the file `src/cli.py`. (The `<` of `a < b` or `<=` is no longer an exception: `<` opens a pair wherever it stands, and a pair needs its `>`.)
26. One path reader (round 12). Delimited and bare references, in either order, take their path from one function (`references()`), and the
    path is the text as written. The NFKC copy of rule 17 only finds the reference; the path that is looked up and reported is cut from the
    original text, so a fullwidth slash (`src／cli.py`) or an ellipsis (`src/cli.py…`) in a path is another file, as it is on disk. There is no
    other reader (round 13): rule 10 asks `references()` too. A `[bracket]` item is an exact `path:N` citation only when the reader reads it as
    one reference that starts and ends where the item does and has the item's own path. `[src/cli.py#:29]` and `(src/cli.py#:29)` are therefore
    both the file `src/cli.py` (rule 19 starts the line part at the `#`, rule 24 makes the path the text before it), neither is an exact item,
    and both are a noncanonical reference (rule 15); on a fact line the first is also `uncited`. An item with no reference overlapping it
    (`[nofile:9]`) is read as written.

The result also lists `failures`: one `{rule, kind, line, text}` per failed line, so a failed reply says which rule 1 to 26 it
broke. The runner keeps the raw reply. The prompt in `config/local.example.json` states the format with one example.

Completeness: every item a chore asks for (each using file, export, step, state, difference) is its own required fact, never one
alternation over the items. Where the task asks for an order (the states, the deploy steps) `order` lists the facts and the
line that states each must come after the lines of the one before, and none twice (rule 12). Each answer carries a hand-written `reference` reply, one
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


Round 7 (review 6 found four holes in the format, not new prose shapes: a subject swap, an intro line hiding a reversed order,
a double-backtick span supplying a behaviour word, and a bare `- ` bullet read as nothing). The rules are 6 to 9 above, 2 and 5
tightened, and `check()` reads the raw line before any stripping. The test that stands behind them is generated, not a list of
examples: `test_generated_attack_every_mutation_of_every_reference_fails` builds, for each of the 24 chores, every mutation of the
reference below and asserts each one fails and names a rule of the expected set (1197 mutations; `QUAL_ATTACK_COUNTS=1` prints the
counts per kind to stderr). a. subject swap: every pair of behaviour facts with different subjects, subject tokens swapped (99);
b. an intro line that names every fact, the rest in order and reversed (2 order chores); c. each behaviour word of each behaviour
line in a code span of 1, 2 and 3 backticks, with and without inner spaces, and an unmatched backtick (65 each); d. an appended
`-`, `- `, `-\t`, `- \t`, `  - x [cite]`, `* x [cite]`, `1. x [cite]` (168); e. round 6's kinds: shift -3..+3, citation on the
next line, heading file, basename, generic claim, one line, drop a line, wide range, extra non-chore cite, `*` bullets. The
references pass 24 of 24. Run against round 6's checker (c249ce3) the same mutations pass: subject swap 96 of 99, intro line 2 of 2
and 2 of 2, code span of 2 62 of 65 (plain and spaced), list items 168 of 168; the other kinds already failed there.

| Family | Rule | Why it cannot recur | Tests |
|---|---|---|---|
| a fact met by a line about another subject (review 6 F1) | 6 | The first name or file token of the claim must be the fact's `behaviour[k].name`; no token is `no_subject`. | `test_rule_6_a_behaviour_fact_is_met_only_by_a_line_about_its_subject`, `test_the_references_start_with_their_subject`, `test_review_6_reproducers_fail_for_their_rule`, generated kind a |
| order read on first matches (F2) | 7, 1 | Order uses the line that met each fact; a line stating two ordered facts is `several_in_one_line`. | `test_rule_7_order_is_read_on_the_lines_that_met_the_facts`, `test_order_is_checked_where_the_chore_asks_for_it`, `test_review_6_reproducers_fail_for_their_rule`, generated kind b |
| a code span other than one backtick supplies a behaviour word (F3) | 8, 5 | Every code span is removed as CommonMark reads it; an unmatched backtick is removed as a character. | `test_rule_8_every_code_span_is_removed_as_commonmark_reads_it`, `test_review_6_reproducers_fail_for_their_rule`, generated kind c |
| a list item the parser does not read as a fact line (F4) | 9, 2 | Every list item is classified on the raw line; one that is not a valid fact line is `uncited` or `not_a_fact_line`. | `test_rule_9_every_list_item_is_graded_as_it_is_written`, `test_rule_2_other_lines_are_ignored_and_an_uncited_bullet_fails`, `test_lines_that_are_not_fact_lines_count_for_nothing`, `test_review_6_reproducers_fail_for_their_rule`, generated kind d |

The cost of rule 6 is in the references: 27 reference lines were reworded to start with their subject (a file name or a symbol),
and four facts changed subject in `answers.jsonl` (diff-order facts 3, 4 and 7 to `JobQueue`, sum-cache fact 1 to `put`). A model that
opens a fact with a word that is not a name ("The handler echo returns ...", "Past the table ...", "It raises ValueError") has
no subject or the wrong one, and fails that fact by rule 6; "When a handler raises, run_one ..." is read as about `handler`.
The prompt in `config/local.example.json` says to start each fact line with the name it is about, with the one example it has.

Round 8 (review 7: whitespace-indented bullets escaped rule 9, and Main found that a fabricated citation outside a list item was
invisible to `fabricated_citations`, which counts only graded lines; both are one family, the prefix of a line decides whether it
is read at all). Rules 10 to 12 above; the checker reads every line for a bracketed citation first, then classifies the line.
`test_generated_attack_every_mutation_of_every_reference_fails` gains four kinds over the 24 chores (3171 mutations now, 1197
before): f. each of U+00A0, U+2003, U+3000, U+2009, `>`, `> `, `>>` in front of `- x [cite]`, of `- x`, and of every reference line
(504); g. each bullet of rule 11 (and `1.`, `2)`, `10.`) in place of `- `, appended with a citation, appended with no citation,
appended bare, and on every reference line (1152); h. a fabricated citation (`[src/nonexistent.rs:999]`, and a range past the
file's end) on a prose line, a heading, a quote, inside a code span, inside a code block, and in a sentence that repeats a claim
(288, and each has a bad citation); i. in the 2 order chores, reference + reversed, reversed + reference, reference + reference,
reversed alone, and each step repeated at the end and in place (30). Run against round 7's checker (the uncommitted tree reviewed,
copied before any edit) the test fails for 1492 of these 1974 mutations: f 336 pass and 168 fail for another rule, g 504 pass and
168 fail for another rule, h 288 pass, i 26 pass and 2 fail for another rule; kinds a to e (1197) are unchanged.

| Family | Rule | Why it cannot recur | Tests |
|---|---|---|---|
| a line with a prefix is read as no list item (review 7 F1) | 11, 2 | The prefix (any `isspace` character, format characters, `>` markers) is stripped before the list marker is looked for, and every bullet of the list is a marker. | `test_rule_11_list_items_by_any_whitespace_and_any_bullet`, `test_review_7_and_mains_round_8_reproducers_fail_for_their_rule`, generated kinds f, g |
| a citation outside a list item is never read (Main) | 10, 2 | Every bracket of every line is read for citations before the line is classified; a bad one is in `bad_citations`, a line that holds one and is no fact line fails. | `test_rule_10_every_citation_is_checked_wherever_it_is`, `test_failing_reply_is_scored_and_classed` (the runner's count), generated kind h |
| a step stated twice, or twice in two orders (review 7 F2, Main) | 12, 7 | The order is read on every line that states an ordered fact, not on the owner of each fact. | `test_rule_12_every_line_that_states_an_ordered_fact_counts`, `test_order_is_checked_where_the_chore_asks_for_it`, generated kind i |

Round 9 (review 8: a bracketed citation split over lines, other shapes of a line reference, and a zero-padded number all escaped; the
same family as round 8, "what the checker reads decides what it can count", so the design changed from a per-line bracket search to
one scan of the whole reply). Rules 13 to 16 above. `check()` blanks the canonical trailing citation of each fact line, then
`references()` finds every other line reference by the line part (`:N`, `#LN`, `line N`) and the path before it, on a copy of the reply in which
every Unicode space and control character is one space, so a line break between the parts does not matter. Each is graded by rule 3
and, being noncanonical, fails the reply. The generated test (29235 mutations now, 3171 before) gains: j. 11 shapes of a reference x 6
wrappers (none, `[]`, `()`, backticks, code block, HTML comment) x no break and a break at each separator (rotating through `\n`,
`\r\n`, U+2028, U+00A0) x 4 targets (valid, line past the end, range past the end for the 5 range shapes, a file that is not the chore's),
25632, in its own test for its time; h. a range past the end as well as a line (432, was 288); k. every reference line's numbers zero-padded by 4,
18, 40 and 5000 zeros of three kinds (ASCII, fullwidth, Arabic-Indic), which must pass (288). Run against b7a7489 (`git show`, loaded in
memory), 24838 of the 26352 mutations of h, j and k escape it (a reply that must fail passes, or, for k, a reply that must pass is
failed), and 842 more fail it for a rule the mutation is not for: j 24622 of 25632 pass outright, and of its 18432 invalid targets
18312 were not counted in `bad_citations`; k 216 of 288 are failed (18, 40 and 5000 zeros: 72 each); h 0 of 432 (the old checker
already read every bracketed citation, so F4 was a gap in the test, not in the checker). The lists: review 8's three split-bracket shapes x four breaks, 8 of 12 pass the old checker, 4 were
counted (the U+2028 and U+00A0 ones, where `strip()` made a one-line bracket); Main's seven F3 shapes, 7 of 7 pass; padding by 18, 40 and 5000
zeros is six `range` failures, by 4 passes.

| Family | Rule | Why it cannot recur | Tests |
|---|---|---|---|
| a reference the scan does not look at (review 8 F1, Main's F3) | 13, 14, 15 | One scan of the whole reply, on a copy where every space and control character is one space, finds a reference by its line part and its path, wherever it is and however it is wrapped. Each is graded by rule 3 and, unless it is the canonical citation of a fact line, fails the reply. | `test_rule_13_a_line_reference_is_found_in_any_shape_anywhere_across_line_breaks`, `test_rule_15_only_the_canonical_form_earns_credit_and_a_valid_other_form_is_not_fabricated`, `test_rule_13_prose_that_is_not_a_line_reference`, `test_failing_reply_is_scored_and_classed` (the runner's count), generated kinds h, j |
| a number is its digits, not its value (review 8 F2) | 16 | Leading Unicode zeros are stripped before the 18-digit cap. | `test_rule_16_a_line_number_is_its_value`, generated kind k |
| a path shaped by a character list, a reference found within a window, a line part by a table of shapes (review 9 F1, F2, Main's F3) | 17, 18, 19, 20, 21 | No list is left to be outside of: a path is any run of non-space characters (or, in a delimiter, everything up to the line part), a line part is a separator run followed by an optional line word and a number, in both orders, found in linear time at any distance; what the grammar still does not place, a number after a path or a line word, fails by itself (`unparsed_reference`). | `test_rule_17_...`, `test_rule_18_...`, `test_rule_19_...`, `test_rule_20_...` (two), `test_rule_21_...`, generated kinds l, m, n, o (`test_generated_grammar_...`, four tests) |
| a numeric character that is not `\d`, and an unlisted leading character dropped from the path (review 10 F1, F2) | 22, 23 | A digit is whatever `unicodedata.numeric` accepts (Nd read by value, the rest fails as `noncanonical_citation` or `unparsed_reference`); a delimiter is a matched rule-18 pair only, so any other leading character stays in the path. | `test_rule_22_...`, `test_rule_23_...`, generated kinds p, q, r (`test_generated_grammar_a_character_in_front_of_...`, `test_generated_numeric_characters_...`, two) |

Round 10 (review 9: `$` and `?` in a path, a path or bracket padded by more than the 4000-character window, and Main's four shapes -
a fullwidth colon, `line N of PATH`, `PATH L999`, `PATH:L9999` - all passed with 0 bad citations; the family is the same as rounds 8 and 9,
"what the checker reads decides what it can count", and each round had closed a list of shapes and been answered with one outside it, so the
lists were removed: no character list for a path, no window, no table of line-part shapes). Rules 17 to 21 above. `references()` now reads a
normalized copy (NFKC, Unicode digits by value, every space one space), finds every line part by a regex that starts only at the start of a
separator run (so a gap of any length costs the same as the text it covers: linear time, a 1 MB reply 0.1 to 1.3 s on py3.9), in both orders,
and takes the path as a maximal run of non-space characters or, inside a delimiter, everything from the opener. Whatever
the grammar does not place is read by the floor (rule 21): a number directly after a path token or a line word fails the reply as
`unparsed_reference`, so a shape the grammar misses fails for that reason and is counted in the runner as `unparsed_references`. S2 needs
that and `fabricated_citations` at zero. The generated test (kinds l, m, n, o; 9480 mutations, over the 24 chores) puts all 32 printable ASCII
punctuation characters and 20 non-ASCII ones inside a path (l), every separator x every line word, forward (m) and in reverse (n), and the
gaps 0, 1, 4000, 4001 and 100000 of spaces and of line breaks (o), each over the four targets (a valid line, a line past the end, a range
past the end, a file that is not the chore's). Run against the round-9 tree (`summarize_check.py` as committed in the worktree before this round, loaded in memory), the 9480 mutations: 1856 fail as stated, 7325 pass outright, 189 fail for another rule than the mutation is for, and 110 ran past 3 s and were stopped (the 100000-character gaps: 96 of 144 for spaces, 14 of 24 for line breaks; the round-9 scan is quadratic there). Per kind, passing outright: l 2282 of 2496, m 3090 of 4320, n 1329 of 1344, o 48 to 96 of 144 for each of the gaps 0 to 4001, and none of the gap 100000 (it times out or fails as stated). The new tree catches 9480 of 9480 in 8 s (`scripts/counts_round10.txt`, `scripts/fail_before_round9.txt`). Main and review 9's nine reproducers: 9 of 9 pass on the round-9 tree and 0 of 9 on this one (`scripts/sweep_check_round9_output.txt`).

The floor and the grammar flag these benign lines (Main's ruling is owed; none was tuned away). All 24 references and the saved sample's
facts pass the grammar (the references pass 24/24; the sample fails as before, now with 15 `unparsed_reference` for its "Line N" headings and
"(line N)" notes). A prose number after a verb, in a version or a time (`evicts after 128 entries`, `retries 3 times`, `version 2.1`, `at 10:30`,
`3:1`) is not flagged in any of the 24 per-chore rewrites. Flagged: a "line N" or "Line N" or "#N" with no file, however it is meant
(`it is line 6 of the story`, `the first line 6 characters long`, `see issue #12`) - `unparsed_reference`; "L2 cache" (the line word `L` + 2) -
`unparsed_reference`; a file name followed by a number in prose with a separator between (`src/cli.py, 3 retries`, `src/cli.py (3 items)`,
`the module src/cli.py 3 times`) - `noncanonical_citation`; `e.g.: 5 items` and `a/b 5-6` (a word that ends in `.letters`, or has a `/`, followed by a number) -
`noncanonical_citation` and `unknown_file`; a file name at the end of a line followed by a numbered list item (`src/cli.py` + newline + `1. first`) -
`noncanonical_citation`, since line breaks are separators; `<a href=src/cli.py#L6>` (the path is taken from the delimiter) -
`noncanonical_citation`. `nofile.rs$:999` is not a path (no `/`, ends in `$:999`), so it is not read; the floor does not flag it either.

Round 11 (review 10: `line ፩`, `src/nonexistent.rs:፩` and `line Ⅰ` passed 24/24 with 0 unparsed references because the floor and the grammar used `\d`;
`{src/cli.py:6` counted 0 bad citations because `{` was read as an opener that rule 18 does not list - the same family again: a
hand-written class, `\d` and the `LEADING` set, decided what could be read). Rules 22 and 23 above. Both classes are gone:
`_Normal` maps a decimal digit to its value and any other character with a `unicodedata.numeric` value to a digit that is not a decimal one
(its integer value, or 0), so a line number made of such characters is read and fails as `noncanonical_citation`, and the floor sees it as a
number; `LEADING` is deleted and `_delimiters` pairs only the rule-18 delimiters (with proper nesting for the same kind), so the path of
`{src/cli.py:6` is `{src/cli.py` (`unknown_file`). Counts (`scripts/counts_round11.py`; chores x targets; same module on py3.9 and py3.14):
l 2496, m 4320, n 1344, o 1176 (nine gap rows), p 1248 (each of the 32 printable ASCII punctuation characters and the same 20 non-ASCII ones in front of the
path, for each of the 24 chores, the wrapper, order and target rotating), and q and r from every character the running
interpreter's `unicodedata.numeric` accepts: 1862 on py3.9 (unicodedata 13.0.0), 2002 on py3.14 (16.0.0), in five forms (after a line word,
after a path, as a line number, in reverse, as the end of a range). Current tree: 20038 of 20038 caught as stated on py3.9, 20738 of 20738 on py3.14.
Against the round-10 tree (`743f3c7`, loaded in memory, py3.9): 14528 caught as stated, 4280 accepted outright (1070 characters x four forms:
the floor and the line number do not see them), 1230 fail for another kind than the mutation is for; per kind: l 105, m 737, n 144 and p 183 wrong
kind (a leading or typographic character), q/r 1070 accepted per form. Changed expectations (flagged for Main): an unclosed opener now belongs to the path
(`test_citations`, the unclosed-list row: `unknown_file`, not a citation), and a bolded line (`**x [p:1]`) is a bad line in
`test_lines_that_are_not_fact_lines_count_for_nothing`. No new benign flag: corpus A 24/24 pass, the references 24/24, corpus B
identical to round 10, corpus C (braces, fractions, superscripts, circled digits, f-strings) with the same two `no_subject` lines as round 10.

Round 12 (review 11: `(;src/cli.py:6)`, `(,src/cli.py:6)` and `` `;src/cli.py:6` `` counted 0 bad citations because a second strip took leading `" ,;"` off a
path inside a delimiter while `[;src/cli.py:6]` and a bare `;src/cli.py:6` did not; `x‘src/cli.py:6’` and `x'src/cli.py:6'` were `unknown_file` while
`x(src/cli.py:6)` was not - the same family once more: the path was rewritten between what the text says and what is looked up, and each round
found one more place). Rules 24 to 26 above. The second strip is deleted; `references()` cuts every path with one function (`span`) from the original
text (the NFKC copy only finds the references), and `_delimiters` lets a quote open and close only where rule 25 says. The test now compares the path
the checker read with the path a plain model of the rules reads from the text the generator wrote (`oracle_cases` / `oracle_mismatches` in
`tests/test_qual_summarize_oracle.py` since round 13, with its own pair matcher and its own list of trailing punctuation): 9 delimiters (each bracket pair, backticks, each of
the four quote pairs, none) x 52 characters (32 printable ASCII punctuation, 20 non-ASCII) x 4 positions (first, last, inside the directory, inside the name)
x 3 before the opener (a letter, a space, nothing) x both orders of rule 19 x all 24 chores = 269568 cases, each asserted for the path and for the failures
(`unknown_file` only when the path is not the chore's file, `noncanonical_citation`, or `unparsed_reference` when the model reads no path), and every 32nd
also inside the whole reference reply. Round 11's tree (`9e2a680`, loaded in memory): 67512 of the 269568 differ; the current tree: 0 (py3.9 and py3.14).
Changed readings (flagged for Main): a compatibility form in a path is now the path as written (`src／cli.py` is `unknown_file`, as is `src/cli.py…`),
a `<` is always an opener, and a list continues after ONE separator. No new benign flag: the references 24/24, corpus A 24/24, B and C byte-identical to round 11.

Round 13 (Main's rulings on the round-12 flags; accepted as they stand: a compatibility form in a path is the path as written, `<` opens a pair, a
doubled separator in a list is malformed and fails closed, `x“src/cli.py:6”` is `unknown_file`. Not accepted: sweep row 28. `[src/cli.py#:29]` was the file
`src/cli.py#` to rule 10 and `(src/cli.py#:29)` the file `src/cli.py` to rules 18 and 24, so there were two path readers, which rule 26 forbids.) Rule 19
says the line part is "a run of separators (`: # , (` and `·`, white space, line breaks), optionally L, l., ln, line or lines, then a number", so in
`[src/cli.py#:29]` it starts at the `#`, and rule 24 says PATH is "exactly the text from just after the opener to the line part": the file is `src/cli.py`
in every delimiter. Rule 10 now asks `references()` instead of cutting the path itself (`citation_items`): a `[bracket]` item is an exact `path:N` citation
only when the reader reads it as one reference that starts and ends where the item does and has the item's own path; an item with no reference
overlapping it (`[nofile:9]`) is read as written. `[src/cli.py#:29]` is therefore a noncanonical reference (rule 15, bad 0), and on a fact line it is also
`uncited`. The oracle moved to `tests/test_qual_summarize_oracle.py` (each test file under 60 s alone) and grew to 327456 cases: the 269568 of round 12
(non-fact lines) + 44928 of the same characters as a forward fact line `- ok <opener>path:N<closer>` + 12960 of every separator that can stand
before the colon (15: none, spaces, no-break space, tab, `#`, `(`, `·`, `,`, `:`, `#:`, ` #`, `# `, `: `, `,,`) on a line of prose (3 places before the opener) and on a
fact line, over 9 delimiters x 24 chores; the model decides which items are exact from the grammar's path, not from the item's own cut. The round-12
tree: 768 of the 327456 differ (all in brackets: 480 character cases, 288 separator cases); the current tree: 0 (py3.9 and py3.14). Cost of the
reader: `citation_items` is computed once per line and cached, and `_floor` builds its name set once; a 1 MB reply of 66k `[a:1,b:2,c:3]` on one line takes
1.8 s (round 12: 1.6 s).

Round 14 (Main ruling 8). The Rule 20 test measures CPU seconds (`time.process_time()`), not wall-clock, so a loaded machine does not fail it; the bound stays 2 s,
on py3.9. The comma-list shape is in the tests (`test_rule_20_a_megabyte_of_bracket_items_on_one_line_...`). To make rule 10's reading cheaper, the reading of a line for
its bracket items is `reference_spans()` (the references of `references()` without their numbers, their written text and the floor), `shaped()` tests the cheap
conditions first and a chore file's whole path is looked up in a set. The result is the same (300000 random texts: items and references equal to round 13's); the shape takes
about 1.5 s CPU on py3.9 (1.8 s before), 1.0 s on py3.14.

Round 15 (Main ruling 27, review 12 F1). Inside a matched pair the delimiter decides: PATH is the text from just after the opener to the line part, white space
trimmed, and there is no known-file exception. Rounds 12-14 tried the chore file written whole (`untrail(head, s)` and a set) before the delimiter, so
`(junk src/cli.py:6)` and `( src/cli.py.:6)` were read as the good file `src/cli.py` (zero bad citations) although rules 24 and 26 say the paths are `junk src/cli.py` and
`src/cli.py.` (`unknown_file`); the reverse form `line 6 of (junk src/cli.py)` was right, and the oracle's model repeated the shortcut, so nothing generated
the shape. Fix: the shortcut is removed, not moved. Outside a delimiter it was a second reader (for a chore file written whole `span(head, s, False)` gives the same
cut), so a delimiter's text is tried first and everything else goes through `span()`; the oracle's model has no shortcut of its own either. A new generated family
("prefixes") puts extra path text before the file, inside each of the 9 delimiters and with none: 3 prefixes (`junk`, `a/`, `.`) x 5 separators (space, two spaces, tab, line
break, no-break space) x 3 suffixes before `:N` (none, `.`, `,`) x forward / reverse / forward fact line x 24 chores = 29160 cases, so the oracle has 327456 + 29160 = 356616
cases. The round-12 checker (`c8f96f5`): 17280 of the 29160 prefix cases differ (8640 forward on prose and 8640 forward on a fact line in the 8 delimiters; the reverse
form and no delimiter agree); this tree: 0 of 356616 (py3.9 and py3.14), the old 327456 unchanged. Changed readings (flagged for Main): `<!-- src/x.py:6 -->` is an angle pair, its path
`!-- src/x.py` (`unknown_file`, counted as fabricated; it was a good target), and `[see, src/cache.py:6]` / `(see src/cache.py:6)` give the path with the leading words
(`unknown_file`; `[see, src/cli.py:6]` was `src/cli.py`). When the delimited text is not path-shaped at all (`( cli.py.:6)`) rule 18's fallback still reads the token outside the pair.
(Round 16 below supersedes the last two statements: rule 29 gives `[see, src/cli.py:6]` back as `src/cli.py`, and rule 30 removes the fallback.) No new benign flag:
references 24/24, corpora A, B and C byte-identical to round 14, counts per kind 20038 / 20738 as before, and the worst rule 20 shape (one line of `[a:1,b:2,c:3] `) is 1.43-1.44 s CPU alone on py3.9,
1.50-1.55 s with 4 copies, 0.92 s on py3.14.

Round 16 (Main rulings 1 to 5 of `ls1-round13-rulings.md`). Flag 1 accepted: `<!-- src/x.py:6 -->` is an angle pair, path `!-- src/x.py`, `unknown_file`.
**Rule 29 (list separator inside a pair):** inside a matched pair ONE list separator (`,` or `;`) splits off a lead-in. It splits only when non-empty text stands before
it and that text does not itself end in a separator; the path starts after the separator and any white space; a doubled separator means no split (fails closed, the
path is as written); text before a split that has no line part is prose. So `[see, src/cli.py:6]` and `[see,src/cli.py:6]` read `src/cli.py`, `[see, a:6, b:7-9]` keeps its two
exact items, `[,src/cli.py:6]` and `(;src/cli.py:6)` read `,src/cli.py` and `;src/cli.py` (`unknown_file`), `[see,, src/cli.py:6]` has no split (`unknown_file`), and `(see src/cli.py:6)`
with no separator is still `see src/cli.py`. **Rule 30:** a forward line part inside a matched pair is read only by that pair (rules 24, 27, 29), with no rule-18
fallback outside it; text that is not path-shaped is still the path as written, a bare name included (`( cli.py.:6)` is `cli.py.`, `unknown_file`; `(Note:3)` is prose: a bare word with no slash
and no extension is no reference). `shaped()` is unchanged; the in-pair test applies it to the text, or its last word, after the trailing sentence punctuation is taken off, which is what lets a bare
name with an extension count. The oracle model has both rules; it grew two families, "leadins" (3 lead-ins: a word, two words, a path fragment x 4 separators `,` `;` `,,` none x 3 white
spaces after it: none, space, line break x 9 delimiters x forward / reverse / forward fact line x 24 chores = 23328 cases) and "bare" (the name, and the name plus `.`, `,` or `;`, x 9
delimiters x both orders x 24 chores = 2592), so it has 356616 + 23328 + 2592 = **382536 cases**; current checker: 0 mismatches (py3.9 and py3.14). Against the round-12 checker
(`c8f96f5`) 33792 of the 382536 fail the model's expectation. What rules 29 and 30 change in the earlier families (flagged for Main, not tuned away): 4800 characters
cases (`,` `;` `[` `<` inside the path: brackets 768, parentheses 672, angle 768, backticks 672, each of the four quote pairs 480, no delimiter 0; for example `x[sr,c/cli.py:29]` was `sr,c/cli.py`, now
`c/cli.py`, and `x[src/cli.py[:29]` now has no reference at all), 0 separator cases, and the 17280 prefix cases of Round 15 (accepted then). Main's ruling 4 condition, a test assertion over
the whole product (`PathOracleTests.product_condition`, `oracle_mismatches(stats=...)`): no case that failed under the old model now passes, and no case with a bad citation under the old model has none
now unless the reply still fails on rule 21 or rule 15: 0 of 382536 break either, and the 4800 are asserted by delimiter (`CHANGED_BY_RULES_29_30`). Ruling 5: the 17 `l path character`
mutations that now leave no path (`src/no,file`, `src/no;file`, `src/no[file`, `src/no<file` inside a pair) each fail the reply on rule 21 with no bad citation; the count 17 and "every mutation fails" are asserted.
Measured with the real round-12 checker (not the model) over the same cases: no case that failed there passes now (0), and **72** line-level cases lose their bad citation without failing on rule 21 or 15: all are
`- ok [see,src/cli.py:N]` (a `,` with no white space after the lead-in, 3 lead-ins x 24 chores), which are now the real file by ruling 2 and fail on rule 2 (uncited) alone. They are reported to Main. Final figures:
references 24/24 and corpora A, B and C byte-identical to round 15; counts per kind 20038 / 20738 as before, but the 17 `l` mutations moved from caught to "wrong" (fail on rule 21, not as stated) and none is accepted; the sweep table
differs only in the `[see, src/cli.py:29]` row (the path is `src/cli.py` again); the worst rule 20 shape (one line of `[a:1,b:2,c:3] `, which now also scans for separators) is 1.52-1.56 s CPU alone on py3.9 (1.43-1.44 s in round 15) and
1.61-1.64 s with 4 copies (1.50-1.55 s), 0.93-0.94 s on py3.14 (1.03-1.05 s with 4 copies; worst there short fact lines 0.93 / 1.07). Bound 2 s unchanged.

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
