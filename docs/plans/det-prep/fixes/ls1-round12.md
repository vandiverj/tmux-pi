# LS1 round 12: the path is the text as written, checked by an oracle (Main, 2026-11-08)

Review 11 (review-coder-9ea22c07bd-233606) is REQUEST_CHANGES with F1 and F2 and an owed report check. Main reproduced both on the
round-11 tree (`ls1probe14.py` / `ls1probe14-r11.txt` in this task's main-evidence):
- `(;src/cli.py:6)`, `(,src/cli.py:6)` and `` `;src/cli.py:6` `` count 0 bad citations. `[;src/cli.py:6]` and a bare `;src/cli.py:6` count
  `unknown_file`. Leading punctuation is still stripped inside some delimiters (`summarize_check.py:537-540`).
- `x‘src/cli.py:6’` and `x'src/cli.py:6'` count `unknown_file`, while `x(src/cli.py:6)` does not.

Each round since round 8 has found one more place where the checker rewrites the path before looking it up. This round makes the
test compare the path the checker read with the path the test wrote.

## Rules (numbering continues round 11)
24. **No rewriting inside a delimiter.** Inside a matched delimiter pair, PATH is exactly the text from just after the opener to the
    line-number part, with only surrounding whitespace (line breaks included) trimmed. No punctuation is stripped from either end.
    `(;src/cli.py:6)` is the file `;src/cli.py`: `unknown_file`. Outside delimiters, rule 18 and rule 23 are unchanged: only the
    listed trailing punctuation is removed.
25. **Quotes and apostrophes (Main's ruling on F2).** A quote character (`'`, `"`, `‘ ’`, `“ ”`) opens a delimiter only at the start of
    the reply or after whitespace or an opening bracket. It closes one only before the end, whitespace, or rule-18 trailing punctuation.
    Otherwise it is an apostrophe and part of the word, as in `don't`. Brackets (`[]`, `()`, `<>`) and backticks are delimiters
    wherever they stand. So `x‘src/cli.py:6’` and `x'src/cli.py:6'` stay `unknown_file` (path `x‘src/cli.py`), as they are now, and
    `x(src/cli.py:6)` is the path `src/cli.py`. Write this rule in experiments.md beside rules 18 and 23.
26. **One path reader.** Delimited and bare references go through one function that returns the path as written. Delete any second
    strip or normalisation step. The rule-17 NFKC copy is only for finding references; the path that is looked up and reported
    comes from the original text.

## Done means
- Tests for every reproducer above, each for the stated kind.
- **Oracle test, generated.** For every delimiter (each bracket pair, backticks, each quote pair) and for no delimiter:
  - × every printable ASCII punctuation character and the 20 non-ASCII characters, placed first, last, and both inside the path;
  - × a letter, a space, or nothing just before the opener;
  - × both orders of rule 19;
  - over all 24 chores.

  Assert that the path the checker reports equals the path the generator built, applying rules 18, 23, 24 and 25. Assert the kind
  too. Report the case count and any mismatch. Fail-before against round 11's tree, lane commit `9e2a680` (Main's checkpoint of round 11 as reviewed):
  save that copy before you edit, and load it in memory.
- Keep every earlier generated kind passing, the references 24/24, and the benign corpora A, B and C. Report any new flag for
  Main's ruling; do not tune any away. Rule 20 still holds.
- **Owed from review 11:** correct the report's sweep rows and numeric-character explanations that the review marked, and the
  corpus-C wording: say "flags only the 2 `no_subject` lines that round 10 also flags", not "PASS".
- State which commands could reach a model (none is expected). List every script you wrote and ran, with its path in the task directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`).
- Same modules and matrix as round 11. No live runs. Same touches.
