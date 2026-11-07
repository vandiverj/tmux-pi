# I1 to I4: campaign rules that move from the prompt into tmux-pi

Lane `campaign`, steps i1 to i4, one brief section each; dispatch one step at a time. Level: `mid` coder for each.
Touches: `bin/tp-spawn` (i1), `bin/tp-review` (i1, i2), `lib/lane_commands.py`, `lib/lane_steps.py`, `lib/lane_sync.py` (new,
i3), `lib/report_check.py` (new, i4), `bin/tp-agent` (i2 to i4 subcommands), tests for each, `docs/lanes.md`,
`templates/worker-rules.example.md` (new). Starts after C1, D1 and D2 are merged (shared files).

## Why
`docs/prompt-examples/` holds about 12,000 words Main must read before the first dispatch and re-read after compaction.
Much of it is mechanics a program can do the same way every time. These four take the most prompt text and the most
recorded incidents.

## i1. Project worker rules travel in every packet
The prompts copy `brief-common.md` into each campaign and restate it in every brief "because review packets snapshot the
brief". Instead: `<project>/.tmux-pi/worker-rules.md` (template `templates/worker-rules.example.md`, built from the
sanitized `docs/prompt-examples/templates/brief-common.md` minus the product rules) is attached by `tp-spawn` to coder,
tester and researcher packets and by `tp-review` to reviewer packets as `Project rules (sha …)`, once, byte-capped (refuse
over 12 KB). Sections headed `## For reviewers` go only to reviewers; `## For workers` only to workers.
Done means: rules appear once in each packet kind with their sha; a brief that also links the file does not duplicate it;
over-cap is refused with the size; tests.

## i2. Note dispositions carry forward
The prompts ask Main to log each APPROVED_WITH_NOTES note as fold, decide, follow-up or drop, and to restate the folded and
dropped ones as "Already decided" in the next brief so reviewers stop raising them (the same three notes recurred for seven
rounds once). Instead: `tp-agent lane note LANE fold|decide|follow-up|drop --text "..." [--from REVIEW-ID]` appends to the
lane record; the next step's packet gets `## Already decided in this lane` and its reviewer packet gets the same list with
"do not raise these again unless the diff changes them". `tp-agent lane status` lists undisposed notes from the last
approving review.
Done means: notes recorded with schema; both packets render the list; undisposed notes listed; tests.

## i3. Sync merges without a model when nothing conflicts
Campaigns keep staged lanes current with `git merge --no-ff` of develop or a parent lane, dispatched as a step that "only
resolves conflicts and runs no tests". A clean merge needs no agent. `tp-agent lane sync LANE --from REF`: if the lane has
no open step and the merge applies cleanly in its worktree, make the merge commit with a fixed message, record it as a
closed step `sync-<n>` of kind `sync` (no review needed for a clean merge, the record says so), and print the new tip. On
conflict: abort, and print the dispatch command for a sync step with the conflicted file list. `lane list --tree` shows
stacked lanes (a lane whose base is another lane's branch) as a tree with each lane's ahead/behind against its parent.
Done means: clean merge recorded without a dispatch and the next dispatch accepted; conflicting merge aborted with the
file list; never rebases; refuses with an open step; tree view for a three-level stack; tests.

## i4. Report check before review
The prompts make Main check every coder report against a list before `tp-agent review`, because about 37 reviews once
asked for changes only for missing counts or log paths. `lib/report_check.py` checks the result against the project's
`.tmux-pi/report-rules.json` (default list: `RESULT_STATUS` line, a commit sha that exists in the worktree, each
`exit=N log=PATH` it quotes points at an existing log, at least one test-run record from B1 for a coder task with
`--tests affected`, a `Self-review` section when the rules ask for it). `tp-agent collect` prints the missing items;
`tp-agent review` refuses with them unless `--allow-incomplete-report`, and names
`tp-agent message ID --text "Add ... to your report; change no code"`.
Done means: each default rule detected on a fixture result; review refusal and override; tests.
