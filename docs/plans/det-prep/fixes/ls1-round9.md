# LS1 round 9: every line reference, in any shape and across lines, is found and checked (Main, 2026-11-08)

Review 8 (review-coder-1e9a5b5bb0-213907) is REQUEST_CHANGES: two defects (F1, F2), a note F3 that Main rules on below,
a note F4, and owed check C1. Main reproduced F1 and F2 on the lane with `$J/ls1probe9.py` (output `ls1probe9-lane.txt`):
- `Also [\nsrc/nonexistent.rs:999\n]`, `Also [src/nonexistent.rs:\n999]` and `Also [src/nonexistent.rs:999\n]` appended to
  use-jobqueue's reference all pass, with 0 bad citations.
- Zero-padding every reference citation to 18 digits gives six `range` failures. Padding to 4 digits passes.

Round 8 is committed as b7a7489. That is a checkpoint, not an approval.

## Main's ruling on F3 (owed since round 8's probe)
S2's bar is "zero fabricated citations". A reference the checker cannot see is a reference it cannot count. So every line
reference in the reply, in any shape, is found and checked. Only the canonical form on a fact line can earn credit.
Main confirmed these shapes on round 8: each one with a fabricated target passes with 0 bad citations:
`See src/nonexistent.rs:999 for more.`, `(src/nonexistent.rs:999)`, `[src/nonexistent.rs#L999]`, `[src/nonexistent.rs line 999]`,
`` `src/nonexistent.rs:999` ``, `[src/a b.py:3]`, `[src/x.py: 3]`.

## Rules (numbering continues round 8)
13. **A line reference is any of these shapes, anywhere in the reply, including across line breaks:**
    - `PATH:N` or `PATH:N-M`, with optional spaces around `:` and `-`;
    - `PATH#LN` or `PATH#LN-LM` (also `-M` without the `L`);
    - `PATH line N`, `PATH, line N`, `PATH (line N)`, `PATH lines N-M`, where `line` matches any case;
    - any of these inside `[]`, `()`, backticks, a code block, an HTML comment or nested brackets.

    PATH is either a chore file (its full path or its basename) or a path-shaped token: it contains `/` or ends in `.<letters>`.
    Inside brackets or backticks, PATH may contain spaces: everything from the opening delimiter up to the line-number part.
    Line breaks and any Unicode whitespace between the parts of a reference do not stop it from being found. Test with `\n`, `\r\n`, U+2028 and U+00A0.
14. **Every line reference is checked by rule 3.** The path must be a chore file, and the line or range must exist and stay within the
    size limit. A reference that fails is counted in `bad_citations`, and so in the run's fabricated count. It also fails the
    reply, with the kind it already has (`range`, `missing_citation`, and so on).
15. **Only the canonical form earns credit.** A line reference that is not the exact canonical bracketed citation at the end of a
    fact line fails the reply, with kind `noncanonical_citation`. This applies even when its target is valid. Rule 10's `stray_citation` stays
    for a canonical bracket on a non-fact line. A valid noncanonical reference is NOT counted as fabricated.
16. **Numbers by value (F2).** A line number's value is what counts, not how many digits it has. Strip leading zeros (any Unicode decimal
    zero the parser accepts) before the significant-digit cap. `000000000000000029` is line 29. Keep a cap that prevents
    huge-int conversion. Test it at the cap, one digit over, and with 18-, 40- and 5000-digit zero padding.

## False positives
Before you finish, run the new checker over `tests/calibration/summarize/samples/` and every reference answer. List every reference
the new rules find that the old ones did not, each with its line and your judgement: a true reference, or prose (for example a version
string, a time like `10:30`, or a URL port). Do not tune a prose match away silently. List it for Main's ruling. References must
still pass 24/24.

## Done means
- Tests for each review-8 F1 shape, each F3 shape above, and the F2 padding cases, each failing for the stated rule and kind or
  passing as stated.
- The GENERATED attack test gains:
  - j: every rule-13 shape × every wrapper (none, `[]`, `()`, backticks, a code block, an HTML comment) × a line break placed at
    each separator × target (valid, line past end of file, range past end of file, nonexistent file), over all 24 chores;
  - h: ranges past end of file, not only single lines (review-8 F4).

  Report counts per kind, and fail-before counts against b7a7489 (`git show b7a7489:tools/qual/summarize_check.py`, loaded in
  memory; no scratch copies).
- `fabricated_citations` in summarize_run counts rule-14 bad references. Add a stub-runner test: a multi-line fabricated reference and
  a bare `path:N` fabricated reference are each counted.
- Update the qwen prompt in config/local.example.json to match rules 13-15: write a file and line only as the trailing
  `[path:line]` of a fact line, never in any other shape or place. Add a test that the prompt's own example fact line passes the checker.
- C1: correct sweep rows 1, 11, 12 and 13 of your report (review 8, report.md:135-147). Each row's explanation must describe
  the finding that site was for.
- State which commands could reach a model (none is expected). List every script you wrote and ran, with its path under the
  task directory (not a scratchpad the reviewer cannot read).
- Same modules and matrix as round 8. No live runs. Touches as round 8.
