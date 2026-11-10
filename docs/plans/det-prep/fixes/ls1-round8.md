# LS1 round 8: every citation anywhere is checked; list items by any whitespace and bullet; order counts repeats (Main, 2026-11-08)

Review 7 (review-coder-07bbf506d8-210919) is REQUEST_CHANGES with one defect, F1, a note F2 that Main now rules on, and
owed checks C1 and C3.

## What Main found while confirming F1
Appending any of these lines to a passing reference still passes (Main ran each on use-jobqueue):
`Also see [src/nonexistent.rs:999]`, `Note: cli.py creates a JobQueue [src/nonexistent.rs:999]`, `• x [src/nonexistent.rs:999]`,
`– x [...]`, `> - x [...]`, `　- x`, and the reviewer's ` - no citation` / ` - fabricated [...]`.
`summarize_run.py:121` counts fabricated citations only from graded lines. So a fabricated citation outside a list item
is invisible to S2's "zero fabricated citations" bar. This is the same family as F1 (fact-line-prefix) and is closed here.

## Rules (numbering continues round 7)
10. **Every citation is checked, wherever it is.** Any bracket in the reply whose items look like `path:N` or `path:N-M`
    (the CITATION shape) is checked by rule 3. This applies on any line: list or not, heading, quote, prose, inside or outside a
    code span or block. A bad one is counted in `bad_citations`, and so in the run's fabricated count. A line holding a citation
    that is not a valid fact line fails the reply (rule 2, kind `stray_citation`).
11. **List items by any whitespace and any bullet.** Classification strips leading whitespace as Python's `str.isspace` does
    (every Unicode space, including U+00A0, U+2003, U+3000). It also strips any number of leading `>` blockquote markers,
    each followed by optional whitespace. A list item is then a line starting with `-`, `*`, `+`, `•`, `‣`, `◦`, `⁃`, `–`, `—`,
    `·`, or `<digits>.` / `<digits>)`, followed by whitespace (any Unicode space) or the end of the line. Only a line that is
    exactly `- ` + claim + citation list, starting at column 0 with no other prefix, is a fact line. Every other list item fails
    (rule 2, kind `not_a_fact_line`), as in round 7.
12. **Order counts every line (Main's ruling on F2).** In a chore with `order`, take every fact line that meets an ordered
    fact (not only the assigned owner), in reply order. Their ordered-fact indexes must be strictly increasing. A step stated
    twice fails (rule 7, kind `repeated_step`) and a step out of order fails (kind `order`). Both of these fail:
    reference + reversed copy, and reversed copy + reference. Reference + reference fails too (`repeated_step`). Chores without
    `order` keep round 7's behaviour, so a repeated line there is allowed.

## Done means
- Tests for F1's two lines, Main's six lines above, and both duplicate-order shapes, each failing for the stated rule and kind.
- The GENERATED attack test gains these kinds, over all 24 chores:
  f. each of the leading prefixes U+00A0, U+2003, U+3000, U+2009, `>`, `> `, `>>` in front of `- x [valid cite]` and `- x`;
  g. each bullet in rule 11 in place of `- `;
  h. a fabricated citation (`[src/nonexistent.rs:999]`, and a range past the file's end) on a prose line, a heading, a quote and
     inside a code span;
  i. for order chores: reference + reversed, reversed + reference, reference + reference, and one step repeated.
  Report counts per kind, and fail-before counts against round 7's checker (the uncommitted tree as reviewed: save a copy first).
- `fabricated_citations` in summarize_run counts rule-10 bad citations. Add a stub-runner test that a prose-line fabricated
  citation is counted.
- References still pass 24/24.
- C1: correct the report's subject count (four: diff-order 3, 4, 7 and sum-cache 1), sweep row 3 (docs/deploy.md is allowed for
  deploy chores) and row 17 (`-\t` is `not_a_fact_line`), and fix experiments.md:348.
- C3: state plainly which commands you ran that could reach a model (none is expected), and list every script you wrote and
  ran, with its path.
- Same modules and matrix as round 7. No live runs. Touches as round 7.
