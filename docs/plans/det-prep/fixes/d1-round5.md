# D1 round 5: four-state rule for the review packet (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-eade871e2c-165848/result.md and report.md beside it.

Round 4 compared the index with the review base. When the base is not HEAD, that either hides the change (index = HEAD, worktree restored to base) or mislabels it (it prints base→index as "staged").

## Rule
For every path, take four states: base, HEAD, index and worktree. The main diff stays base→worktree.

When HEAD or the index differs from the worktree for a path, the packet adds a section for that path with each diff that is non-empty:
- **"Committed since the base, not in the working files":** base→HEAD.
- **"Staged, not in the working files":** HEAD→index.

These go through the same withholding and redaction as the main diff. `render_scope` names each comparison exactly. The same function serves round 1 and later rounds; a later round uses the previous round's tree as the base.

Update the header comment at `bin/tp-review:22`.

## Tests
One table generated over every way the four states can be equal or different, for one path: all 15 partitions of {base, HEAD, index, worktree}, plus binary and deleted rows. Each row asserts which sections appear, what they contain, and what the scope line says. Run it for round 1 and for a later round.

## Report
Include the table's failure count against a copy of the round-4 code (not by disabling a function), and a Site / Disposition / Evidence sweep table.
