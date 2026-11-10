# LS1 round 4 (Main's finding on coder-ed25d9ee58, before review)

Same lane `local-summarizer`, step S1, same touches.

**Defect (Main reproduced):** a citation without a quote is only checked to be in range. use-jobqueue's own `reference`
with the backtick quotes removed and every line number changed to a wrong in-range one (cli.py:5, cli.py:7, jobq.py:3,
worker.py:2, worker.py:4, worker.py:6) passes `check()` with no bad citations. Qwen's known failure is exactly this
(12 of 15 smoke line numbers wrong), so S2's "zero fabricated citations" bar cannot be measured.

Fix:
1. **Support per fact.** Each required fact gets a `support` field: a regex (or small set) that the cited source line(s)
   must match, written from the fixture text (e.g. use-jobqueue fact 4: `queue\.pop\(`). The citation(s) on a fact's
   line or bullet must include at least one whose line or range (`a-b`, at most 5 lines) matches that fact's `support`.
   Otherwise the fact is `unsupported` and the reply fails. Range limit: a range wider than 5 lines is a bad citation
   (`range_too_wide`), so a model cannot cite a whole file.
2. **Every citation is checked, not just fact ones.** A quoted citation must contain the quote (as now). An unquoted
   citation that sits on a fact's line counts only if it matches that fact's `support` (item 1). An unquoted citation on
   a line with no required fact is `unchecked`: counted in the record, not a failure. S2 reports the count.
3. **Tests:** Main's wrong-line reproducer above fails with `unsupported` on all six facts; the same reply with the right
   numbers and no quotes passes; a 6-line range fails `range_too_wide`; every chore's `reference` passes and, for each
   fact, the reference with only that fact's citation moved to a wrong in-range line fails `unsupported`. Re-score the
   saved smoke reply and report it.
4. Audit: add a `support` column to the audit table in experiments.md.
5. Same verification (3 modules, both interpreters, both modes). Do not run the live model or tp-route.
