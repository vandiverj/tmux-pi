# H48 IL1: what the owner's messages to Main are about (baseline, 2026-11-10)

Question (assessment Q1): what share of the owner's messages to Main are **process reminders** (telling Main how to operate),
as opposed to product decisions, authorizations or status questions? HUB's target is that this share falls.

## Method
- Corpus A: the det-prep campaign, run from a Claude Code Main on this Mac (sessions 66bb0341 → 6061f696, Nov 2026). 112 owner
  messages; 5 exact duplicates dropped (the continued session repeats its first message); 4 shell-test records labelled `other`.
- Corpus B: owner-typed messages in tmux-pi's own Claude Main sessions on this Mac (Oct 2026; mostly a personal project, some on
  tmux-pi itself). 232 messages, after dropping plugin notices, task packets and worker transcripts. About 13 are harness tests.
- One label per message: process (with a kind) / product / authorization / status / issue-report / manual-action / other, plus a
  recurrence flag when the same process point was already made. Labelled by a model pass (Sonnet) from the rubric; Main
  spot-checked 10 process labels in B and re-checked the long messages whose ends the first pass did not read (none changed).
- Per-message labels and quotes stay untracked under `.tmux-pi/il1/` (they quote private project content). Only counts are here.
- **Limit:** the work Mac's transcripts are not on this machine, so the production sessions the owner described (unsent
  worker messages, safe-tool reminders) are not in this baseline. Repeat the count there before judging HUB.

## Results
Correction (2026-11-12): five B rows labelled `process` were Main's instructions to workers, not owner messages
(found during HB7a). They are relabelled `not-owner` in the untracked labels file and left out of the counts below
(B was 232 messages, 30 process).

| corpus | messages | process | product | authorization | status | issue-report | manual-action | other |
|---|---|---|---|---|---|---|---|---|
| A campaign | 107 | 16 (15%) | 22 | 31 | 18 | 2 | 14 | 4 |
| B tmux-pi Main | 227 | 25 (11%) | 78 | 35 | 39 | 8 | 15 | 27 |

Process reminders by kind:
| kind | A | B |
|---|---|---|
| delegation-or-model (which model, use workers, run in parallel, use the tmux-pi process) | 7 | 5 |
| verification-or-evals (run targeted tests first, test it properly, re-score) | 2 | 6 |
| recovery-after-compaction-or-restart (save state in durable docs, resume from it) | 1 | 6 |
| purpose-or-scope-drift (what the work is for; who the tool serves) | 3 | 2 |
| safe-tools (workers/reviewers must use safe_grep/safe_read) | 2 | 0 |
| waiting-or-stall (work on your own, don't wait on me) | 0 | 2 |
| worker-messaging | 0 | 2 |
| git-or-workflow-procedure | 0 | 1 |
| other | 1 | 1 |

Repeats (the owner had to say the same process point again): A 3 of 16 (model choice; tools are for target projects;
safe search for workers and reviewers). B about 9 of 25, mostly state saving/restart (4) and "run only the affected tests" (3).

## What it means for the queue
1. Process reminders are about one message in seven. Authorizations plus status questions are about two in five in both
   corpora: the cheaper win for "leave and return" is HB6 (where work waits, a return brief) and fewer approval stops, not only
   more operating rules.
2. Recovery after compaction/restart is the most repeated reminder in B → HB3 (`tp-agent brief`, durable state).
3. Model/delegation choice is the largest kind in A → standing orders in HB2/HB3, router defaults (H51) are the owner's.
4. Safe-tool reminders are few here but repeated, and the owner reports them on the work Mac → SG1 + HB2 rule 1 stand.
5. Prompt-handling never appears as a reminder; prompts show up as manual actions ("clicked allow") or issue reports → HB4.
6. Re-run this count on the next campaign (and on the work Mac) as HB's before/after measure.
