# LS1 round 10: one reference grammar with no character list and no window, plus a table-free floor (Main, 2026-11-08)

Review 9 (review-coder-8368348c70-220732) is REQUEST_CHANGES: defects F1 and F2, note F3 (Main's four shapes), and an owed
sweep check. Main reproduced F1, F2 and F3 on the round-9 tree (`ls1probe10.py`, `ls1probe11.py` in the round-9 task's main-evidence).
Each of these, appended to use-jobqueue's reference, passes with 0 bad citations:
- `src/no$file:999`, `src/no?file:999`, `src/no$file#L999` (F1: `PATH_TAIL` leaves out `$` and `?`);
- `[src/nonexistent` + 4001 spaces + `file line 999]`, and the same with 4001 newlines (F2: the 4000-character scan window);
- `src/nonexistent.rs：999`, `line 999 of src/nonexistent.rs`, `src/nonexistent.rs L999`, `src/cli.py:L9999` (F3).

Rounds 8 and 9 each closed a list of shapes, and each review found shapes outside the list. This round removes the lists.

## Rules (numbering continues round 9)
17. **Normalize, then scan.** Scan a copy of the reply in NFKC form, with every Unicode decimal digit mapped to its value. Credit
    is still judged on the original text, so a fullwidth colon or digit in a trailing citation is `noncanonical_citation`.
18. **A path has no character list.**
    - Outside delimiters, PATH is a maximal run of non-whitespace characters, minus trailing sentence punctuation (`.,;:!?)]}>'"`*`).
      It counts as a path when it contains `/` or `\`, ends in `.<letters>`, or equals a chore file's path or basename.
    - Inside a delimiter (`[]`, `()`, `<>`, backticks, quotes), PATH may hold any characters, whitespace and line breaks included,
      up to the line-number part.

    Write no hand-written allow-list of path characters.
19. **Line-number parts in both orders.** A reference is a PATH tied to a line number in any of these ways:
    - a separator run from `: # , ( ·` and whitespace, then optionally `L`, `l.`, `ln`, `line` or `lines` (any case), then a number or range;
    - or the reverse: `line` / `lines` / `L` + number or range, then `of`, `in` or `at`, then PATH.

    Separators may include line breaks.
20. **No window.** Detection does not depend on the distance between the parts of a reference, or on how long the reply is. The scan
    must run in linear time. Test with the gap 0, 1, 4000, 4001 and 100000 characters, of spaces and of newlines. Also test a 1 MB reply,
    which must finish within 2 s on py3.9.
21. **Table-free floor.** After removing every canonical fact-line citation and every reference rules 17-19 found, any remaining
    number (any Unicode digits) that is directly preceded by a path token or by a line word (`line`, `lines`, `L`, `l.`, `ln`, `#`),
    with only separators between them, fails the reply with kind `unparsed_reference`. summarize_run counts these as
    `unparsed_references`, beside `fabricated_citations`. **S2 requires both to be zero.** Update experiments.md's S2 bar
    and the runner summary line to say so.

## False positives
- Run the checker over the samples and all 24 references, and show the counts of `noncanonical_citation` and `unparsed_reference`.
  References must pass 24/24.
- Then build a benign corpus: each reference's claims rewritten with numbers in prose (`evicts after 128 entries`, `retries 3 times`,
  `version 2.1`, `at 10:30`), one per chore. Report every line the floor or the grammar flags.
- Do not tune any flagged line away. List each one for Main's ruling.

## Done means
- Tests for every Main and review reproducer above, each failing for the stated kind.
- **GENERATED test from the grammar, not from a shape list.** Use every printable ASCII punctuation character, and a sample of 20
  non-ASCII characters, inside a path. Cover both orders, every separator and line word, and the gaps from rule 20, over all 24 chores
  and every target (valid, line past end of file, range past end of file, nonexistent file). Report counts per kind, and fail-before
  against round 9's tree. Save that copy before you edit and load it in memory.
- Keep every earlier generated kind passing.
- C: correct every sweep row review 9 marked "not confirmed as explained" (see its report). Re-check every row's explanation
  against the finding that site was cited for.
- State which commands could reach a model (none is expected). List every script you wrote and ran, with its path in the task
  directory.
- End with a line that is exactly `RESULT_STATUS: complete` (or `blocked`). Concerns go in the body, not in that line.
- Same modules and matrix as round 9. No live runs. Touches as round 9.
