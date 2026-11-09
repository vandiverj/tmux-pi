# LS1 round 13: Main's rulings on flags 1-3 (2026-11-09)

1. **Flag 1 is accepted.** `<!-- src/x.py:6 -->` is an angle pair, so its path is `!-- src/x.py`, which is `unknown_file`. This follows rule 24
   and fails closed. Main inspects any S2 hit by hand.

2. **Flag 2 is not accepted as is.** It undoes the round-12 behaviour Main accepted: `[see, a:6, b:7-9]` keeps its two exact items.
   A real file cited after a lead-in word must not count as fabricated, because that corrupts the S2 fabricated count.
   **Rule 29 (list separator inside a pair):** inside a matched pair, ONE list separator (`,` or `;`) splits the text into items.
   - The separator splits only when non-empty text stands before it in the same item, and that text does not itself end in a separator.
   - The path then starts after the separator and any whitespace.
   - Text before the separator with no line part is prose and is not a reference.

   Expected results:
   - `[see, src/cli.py:6]` is `src/cli.py`. It is exact iff it is CITATION-shaped and its path is the grammar's (rule 10, unchanged).
   - `[see, a:6, b:7-9]` has two exact items, as in round 12.
   - `[see,src/cli.py:6]` is `src/cli.py`.
   - `[,src/cli.py:6]` and `(;src/cli.py:6)` have nothing before the separator, so rule 24 still applies: `,src/cli.py` and `;src/cli.py`, `unknown_file`.
   - `[see,, src/cli.py:6]` has a doubled separator. Ruling 3 says it fails closed, so there is no split and the result is `unknown_file`.
   - `(see src/cli.py:6)` and `(junk src/cli.py:6)` have no separator, only whitespace. Ruling 27 applies: `see src/cli.py` and `junk src/cli.py`, `unknown_file`.
   - This holds in every delimiter and on a fact line.

   Add rule 29 to the docstring and to `experiments.md`. Add the cases above as unit tests. Add a generated oracle family:
   - the lead-in: a word, two words, a path fragment;
   - × the separator: `,`, `;`, `,,`, none;
   - × the whitespace after it: none, a space, a line break;
   - × every delimiter;
   - × both orders, and a fact line;
   - × all 24 chores.

   Report the case count.

3. **Flag 3 is not accepted.** `( cli.py.:6)` reading `cli.py` from the fallback strips text inside a pair. That is the F1 family again.
   **Rule 30:** a forward line part that stands inside a matched pair is read only by that pair, through rules 24, 27 and 29.
   There is no rule-18 fallback outside the pair. If the text in the pair is not path-shaped, it is still the path as written,
   which is `unknown_file`. Bare names count too: `( cli.py.:6)` is `cli.py.`, `unknown_file`.
   - Add a generated family with bare file names (no slash): the name, then the name with a trailing `.`, `,` or `;`, × every delimiter, × both orders.
   - If `shaped()` must accept such text inside a pair for this, say so and why.
   - If rule 30 changes any earlier oracle case, counts per kind, the 24/24 references or corpora A/B/C, report each change for Main's ruling. Do not tune it away.

For every change: run the fail-before against `c8f96f5` (the same scratch copy), run the 5-module matrix, and report the rule-20 worst shape
(py3.9 alone and with 4 copies). Update `report.md` in place (flags, rulings, counts and the sweep table). Keep the F2 statement.
End with `RESULT_STATUS: complete`.

## Main's rulings on the worker's two follow-up questions (2026-11-09)
4. **The 4,800 changed characters-family cases are accepted as rule 29 and 30, on one condition.** Write it as a test assertion over
   the whole product, not as a report claim:
   - No case that failed under the old model now passes. The set of failure kinds may change, but "passes" must not become true.
   - No case with an old bad citation now has 0 bad citations, unless the reply still fails on rule 21 (unparsed) or rule 15.

   Report the counts for each condition. If any case breaks either condition, stop and report it; do not adjust the rule.
   Update the oracle model to rules 29 and 30 so that these cases are expected, not exempted.
5. **The 17 `l` mutations with no path are accepted on the same condition.** Each reply must still fail (rule 21). Keep the count
   assertion at exactly 17, and also assert that each of the 17 fails. `(Note:3)` stays prose, as you argued.
