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
