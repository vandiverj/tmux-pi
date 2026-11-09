# LS1 round 13: a delimiter's path takes precedence over the known-file shortcut (Main, 2026-11-09)

Review 12 (review-coder-e245072fca-015025) is REQUEST_CHANGES: F1 is a defect, F2 is a note that must be closed.
Round 12 is committed as lane checkpoint `c8f96f5`; work on top of it.

Main reproduced F1 on c8f96f5. `reference_spans("Also (junk src/cli.py:6)", ["src/cli.py"])` starts the path at `src`, giving
`src/cli.py`, which is a good file. `( src/cli.py.:6)` is also read as `src/cli.py`. The reverse form
`line 6 of (junk src/cli.py)` correctly gives `junk src/cli.py`, which is `unknown_file`.

The cause is in `tools/qual/summarize_check.py`, in the forward loop of `_scan` (about lines 532-548). The known-file shortcut
(`untrail(head, s)` plus the `whole` set) is tried before the delimiter. That rewrites a delimited path, which rules 24 and 26 forbid.
It also bypasses `span()`, the one cutter. The oracle model repeats the same shortcut (`tests/test_qual_summarize_oracle.py:110-112`),
and it never generates extra path text before a whitespace break, so it could not catch this.

## Rulings
27. **Inside a matched delimiter pair, the delimiter decides.** PATH is what rule 24 says: the text from just after the opener to the
    line part, with only whitespace trimmed. The known-file shortcut never applies inside a delimiter, in either order.
    - `(junk src/cli.py:6)` is `junk src/cli.py`, giving `unknown_file`.
    - `( src/cli.py.:6)` is `src/cli.py.`, giving `unknown_file`.
    - The same holds with a line break in place of the space, and for every delimiter (brackets, backticks, each quote pair under rule 25).
    - Outside delimiters, the shortcut may stay only if it is the rule-18/23 reading and goes through `span()`. If it is a second
      reader, remove it. State which of the two you did and why.
28. **The oracle model has no shortcut of its own.** Remove the shortcut from the model. Build the expected path only from the rules
    (18, 23, 24, 25, 27). Add a generated family for extra path text before the file, inside every delimiter and with none:
    - × the separators: space, two spaces, tab, line break, no-break space;
    - × the prefix: a word, a path fragment `a/`, and punctuation `.`;
    - × the suffix punctuation just before `:N`: none, `.`, `,`;
    - × both orders of rule 19 and a fact line;
    - over all 24 chores.

    Report the case count.

## Done means
- Unit tests for the reviewer's three forward reproducers and the reverse control, in both orders, on a line of prose and on a fact line.
  Each test asserts the path and the kind.
- **Fail-before:** run the new tests and the new oracle family against `c8f96f5`. Load that commit's checker with
  `git show c8f96f5:tools/qual/summarize_check.py` into a scratch copy in the task directory. Report the count of each that fails.
- All earlier tests, the 327,456 oracle cases, counts per kind, the 24/24 references, and the benign corpora A, B and C must pass
  as before. Report any new flag for Main's ruling; do not tune any away. The rule-20 CPU bound (2 s) still holds. Report the
  worst shape on py3.9, alone and with 4 copies.
- Update the documentation that still describes the known-file exception: the module docstring, the rule texts in
  `summarize_check.py`, and `tests/calibration/experiments.md` (add a Round 15 paragraph).
- **F2 (provenance):** every claim in the report must be backed by a script and log in the task directory, or be dropped.
  The rejected 1.10 s experiment (554 of 60,000 replies) had its scripts deleted. Either re-create a script and log that reproduce it,
  or state in the report that the claim is unaudited and is not relied on.
  List every script you wrote and ran, with its path in the task directory.
- Same five-module matrix (`test_qual_summarize test_qual_summarize_oracle test_router_catalog test_route_resolve test_router`).
  No live runs. Same touches. State which commands could reach a model (none is expected).
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).

## For you: how to search
- Read named files only. Use `git ls-files`, `git diff --stat` and `git show`.
- No `rg` or `git grep` over directories: `.env*` files trigger prompts.
- Never read `config/` or `agent/` recursively.
