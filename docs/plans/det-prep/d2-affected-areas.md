# D2: a deterministic affected-areas list the reviewer must cover

Lane `review-order`, step 2. Level: `mid` coder. Touches: `lib/review_rounds.py`, `lib/review_verdict.py`, `bin/tp-review`,
`agent/roles/code-reviewer.md`, `tests/test_review_rounds.py`, `tests/test_review_verdict.py`, `tests/test_review_coverage.py`.
Requires D1.

## Why
The role prompt asks for "callers and readers of changed functions ... other writers of the same state", and the verdict
parser only checks that a `Coverage` section exists with three words. Enumeration is model recall. The context engine has a
TypeScript navigator (importers, references) but nothing in the review packet uses it, and nothing exists for Python or shell.

## Change
1. `lib/review_rounds.py affected WORKDIR OUT`: for each changed file in `scope`, list (a) files that import it (ctxengine
   navigator when the file is in a tsconfig or deno package, else none), (b) files whose text names the changed file's stem or
   any symbol whose definition line is in the diff (`git grep -n -w`, repo-wide, capped), (c) tests that name the file or
   those symbols. Render `Affected areas` with ids A1..An, each `id | path | why (imports X / names X at :line / test names X)`,
   capped at 60 rows and 12 KB with `and N more` plus the command to list the rest. Attach to the packet after the diff (D1 item c).
   Write the id list to the review task dir (`affected-ids.json`) for the verdict step.
2. `lib/review_verdict.py coverage_problem`: when `affected-ids.json` exists, the `Coverage` section must name every changed
   file and every id, each marked `reviewed` or `not reviewed: <reason>`; otherwise the reply is incomplete and the message
   names the missing ids. Without the file, today's behaviour.
3. `code-reviewer.md`: one sentence saying each `Affected areas` id must appear in Coverage with its mark.

## Done means
1. A fixture TS change produces an `Affected areas` block with an importer, a grep hit and a test, each with an id.
2. A fixture Python change (no navigator) produces the grep and test rows only, with no error.
3. The cap renders `and N more` and the listing command.
4. A reply missing one id or one changed file in Coverage is incomplete and the message names it; with every id marked it passes; without `affected-ids.json` the old rule applies.
5. Tests for 1 to 4 fail without the change.
6. `bin/tp-review`'s header and `docs/` (the review paragraph in README or docs/lanes.md: one paragraph only) document the block.
