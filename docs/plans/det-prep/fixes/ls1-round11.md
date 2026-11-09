# LS1 round 11: every numeric character, and paths read exactly as written (Main, 2026-11-08)

Review 10 (review-coder-71799ae16a-230447) is REQUEST_CHANGES with defects F1 and F2. Main reproduced both on the round-10 tree
(`ls1probe13.py` / `ls1probe13-r10.txt` in this task's main-evidence). Main checkpointed rounds 9 and 10 as lane commit `743f3c7`;
that commit is round 10's tree.

## F1: a numeric character that is not `\d` escapes the floor
`line ፩`, `src/nonexistent.rs:፩` (Ethiopic) and `line Ⅰ` (Roman numeral) pass 24/24 references with 0 unparsed references.
`tools/qual/summarize_check.py:145,530` uses `\d`.

## F2: an unlisted leading character is dropped from the path
`{src/cli.py:6` counts 0 bad citations, while `|src/cli.py:6` and `x{src/cli.py:6` count `unknown_file`. `{` is treated as an opening
delimiter, which rule 18 does not list (`summarize_check.py:141,444-479`).

## Rules (numbering continues round 10)
22. **A digit is any character with a Unicode numeric value.** For the floor and the grammar, a digit is any character for which
    `unicodedata.numeric(c, None)` is not None (categories Nd, Nl and No: `፩`, `Ⅰ`, `①`, `½`, `²` included). A number made only of
    Nd digits is read by value, as in rule 16. Any other numeric character in a line-number position fails the reply as
    `noncanonical_citation`, or as `unparsed_reference` where rule 21 applies. Do not write a hand list of digit ranges.
23. **Delimiters are exactly the rule-18 pairs.** The delimiters are `[]`, `()`, `<>`, backticks, and `'` / `"` / their typographic forms,
    each only as a matched pair. Any other leading character, and an opener with no closer, is part of the path as written, so
    `{src/cli.py:6` is a reference to the file `{src/cli.py`: `unknown_file`, counted in `fabricated_citations`. Only the trailing
    punctuation listed in rule 18 is removed.

## Done means
- Tests for the review's and Main's reproducers above, each failing for the stated kind.
- **Extend the generated grammar test:**
  - put every printable ASCII punctuation character and the same 20 non-ASCII characters in front of the path too, not only inside it;
  - for the floor and the line-number part, generate every character `unicodedata.numeric` accepts, from the running
    interpreter's database. Report that count on py3.9 and on py3.14.
  - Report counts per kind, and fail-before against `743f3c7`. Save that copy before you edit, and load it in memory.
- Keep every earlier generated kind passing, the references 24/24, and the benign corpus. Report any new flag for Main's ruling;
  do not tune any away.
- Rule 20 still holds: a 1 MB reply in under 2 s on py3.9.
- State which commands could reach a model (none is expected). List every script you wrote and ran, with its path in the task directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).
- Same modules and matrix as round 10. No live runs. Same touches as round 10.
