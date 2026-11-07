# C2 round 3: one parse order for context specs (route: rule-first)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-52ad5a71e4-172702/result.md and report.md beside it.

## Rule
`split_context` decides a spec's form in this one order, and the docs list it:
1. **Heading spec:** the spec has a `#` whose left side is an existing file. Use the first such `#`. Everything after it is the heading text, including any `:a-b`, so `file.md#Rules:2-3` is the heading `Rules:2-3`.
2. **Line-range spec:** otherwise, a spec ending in `:a-b` whose left side is an existing file (so `a#b.md:2-3` is a range).
3. **Plain path:** otherwise.

**Heading text:** drop the leading `#`s, then trim whitespace at both ends (a stray space from shell quoting is not part of a heading). All other characters are kept, including a trailing `#`. If the text is empty after trimming, the spec is refused. This replaces round 2's "trailing characters are kept" for whitespace only.

## Tests
- **One table of spec strings against fixtures:** `file.md#Rules:2-3` with a `## Rules:2-3` heading; `a#b.md:2-3`; `a#b.md#H`; `file.md#C `; `file.md#C#`; `file.md#`; `file.md# `; a file literally named `x:2-3`. Each row asserts the form, the selected range or file, and the refusal text.
- **The `markdown_it` parity test:** run it over the same fixtures as the fence and heading table, not a separate document. It still skips when the module is absent.

## Report
- Reconcile the orchestration result. It has 4 failures, all in `docs/plans/det-prep/baseline-failing.txt`; say so rather than reporting a pass.
- Give the exact commands and log paths.
