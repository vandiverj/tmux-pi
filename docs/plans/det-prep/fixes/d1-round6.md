# D1 round 6: no filter but the four states (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3464a832e0-171939/result.md and report.md beside it.

Round 5's table passes, but `lib/review_rounds.py:423-432,496` adds a second condition: the committed section is shown only if HEAD moved since the previous review. That hides a non-empty base→HEAD in later rounds and for older reviewed-state records with no `head`.

## Rule
Whether each section appears depends only on the four states of the path: base (the previous round's tree in later rounds), HEAD, index and files. No other condition applies (no previous HEAD, no record field). Remove the prior-HEAD filter. A record with no `head` is handled identically.

## Tests
Run the same generated 15-partition table (plus binary and deleted rows) for round 1, for a later round where HEAD has not moved since the previous review, and for a later round from a record with no `head`. Each row asserts the sections and the scope line together, in the same assertion.

## Report
Include the exact test commands and log paths, and a Site / Disposition / Evidence sweep table.
