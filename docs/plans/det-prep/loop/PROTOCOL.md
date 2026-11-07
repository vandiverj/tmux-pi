# Improvement loop protocol (autoresearch style)

Read this file first after any context compaction. State lives in this folder, not in the conversation.

Files:
- `PROTOCOL.md` (this file): the rules. Changed only by the owner or with a logged reason.
- `ideas.md`: candidate hypotheses, each with an id `H<n>`, source, expected effect, cost, status.
- `results.tsv`: one row per bench run: `date	cycle	hypothesis	arm_commit	label	repeat	code_pass	review_catch	review_fp	local_exact	tokens_per_accepted	orient_waste	wall_s	verdict	note`.
- `cycles.md`: one section per cycle: hypothesis, worktree, branch, change summary, runs, verdict, what was learned.
- `research/`: one file per research run with its sources and the panel's ranking.

## When the loop runs
After every brief in the main plan is closed and merged into `lane/integrate` and the J1 control runs are recorded.
The owner may be away; standing answers in `../run-log.md` apply (never approve prompts, never same-provider review,
never merge to `main`, never push).

## One cycle
1. **Pick** the highest-ranked `open` idea in `ideas.md` whose fingerprint (the files it changes plus its one-line
   mechanism) matches no row in `cycles.md`. A discarded idea is reopened only with a written reason why the new version
   differs.
2. **Branch**: `git worktree add ../tmux-pi-wt-loop-H<n> -b loop/H<n> lane/integrate`. One idea per worktree. Up to three
   cycles run at once when their files do not overlap.
3. **Change**: a dispatched coder step in that worktree (normal tmux-pi review rules apply; the change must have its own
   unit tests). Keep the change small: one mechanism per cycle.
4. **Evaluate**: `tp-bench run --arm ../tmux-pi-wt-loop-H<n> --label H<n> --repeat 2` on the task kinds the idea targets
   (all kinds if it touches shared paths: packets, preamble, routing), then `tp-bench compare control H<n>`.
5. **Decide** by the rule below; log the row(s) in `results.tsv` and the section in `cycles.md`.
6. **Keep**: merge `loop/H<n>` into `lane/integrate` with `--no-ff`, run the full unit suite, and re-run the control on
   the new `lane/integrate` (it is the new baseline). **Discard**: leave the branch (never delete; it is evidence),
   remove the worktree.

## Keep rule
- Correctness first: `code_pass`, `review_catch` and `local_exact` must not drop beyond the control's own spread, and no
  single task may flip from pass in both control repeats to fail in both candidate repeats.
- Keep when correctness rises beyond the spread, OR correctness is equal and tokens per accepted task drop by 10% or
  more, OR both equal and `orient_waste` drops by 25% or more.
- `review_fp` rising by more than one per clean diff is a discard.
- Otherwise `inconclusive`: one more repeat pair at most, then discard.
- A cycle that breaks the unit suite or the bench is `crash`: fix once if trivial, else discard.

## Budget (credits are limited)
- Per cycle: at most 1 coder step + 2 fix rounds + 1 bench pair (default task subset). Per day while the owner is away:
  at most 6 cycles and 2 research runs. Bench coders run at `--level small`; reviewers at the router default.
- Prefer ideas that move work to the local model; their bench runs cost little frontier spend.
- Stop the loop and log why when: three cycles in a row are discarded for the same reason, the reviewer provider is out
  for more than 3 hours, or the budget is reached.

## Research run (before the first cycle, then after every 4 cycles)
1. Inputs: `results.tsv`, `cycles.md`, `tp-improve routines` and `tp-evals families` over the bench sessions, the
   owner's friction report in `../README.md` and the prompt examples.
2. Four independent researchers, no one sees another's answer first: a Claude researcher (`mid`), a GPT researcher
   (`mid`, other provider), the local Qwen (`--local-only`, given the data files only), and a web-search researcher
   (published agent-harness and local-model techniques, with sources). Each returns up to 5 hypotheses with mechanism,
   the metric it should move, how to test it on the bench, and cost.
3. A panel step (a different researcher than the four, other provider from the one that proposed most ideas) merges
   duplicates, scores each on expected gain x confidence / cost, and flags ideas that the existing data already refutes.
4. Main adds the ranked ideas to `ideas.md` with their source and writes the run to `research/R<n>.md`.

## Adding an eval
When an idea targets something the bench cannot see, the cycle first adds the bench task (sealed answers, reviewed) and
records a control run with it, so the candidate is compared on the new task with a baseline.
