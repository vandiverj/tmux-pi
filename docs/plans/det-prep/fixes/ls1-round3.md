# LS1 round 3 (review-coder-ed3b02c22e-135709: REQUEST_CHANGES)

Same lane `local-summarizer`, step S1, same touches. Read tasks/review-coder-ed3b02c22e-135709/result.md and report.md.

Fix:
1. **F1 heading file resolution.** A heading or bullet gives `Line N` context only if it names exactly one file, and that
   name matches a `cite_files` entry as a whole path or basename token (not a prefix: `cache.py.bak` is not `cache.py`).
   The nearest preceding heading/bullet that names any file-like token (any extension) sets the context; if that token is
   not an allowed file, or the line names two or more files, the `Line N` under it is a bad citation
   (`ambiguous_context` / `context_not_allowed`). Add the reviewer's three reproducers as tests.
2. **F2 complete audit.** Re-audit all 24 answers against their chore text and fixtures, chore by chore: every item the
   chore asks for (each using file, each export, each step, each state, each difference) is its own required fact.
   use-jobqueue must require worker.py's use (receives the queue, pops jobs, handles failure). Write the audit as a table
   in tests/calibration/experiments.md (chore id, items the chore asks for, the required fact covering each).
   Add a test with a hand-written full reference reply per chore (in the answers or a test fixture) that passes, and for
   each required fact, the same reply with that fact's sentence removed fails (`missing`). This proves each fact is needed
   and the reference is complete.
3. **F3 order.** Where a chore asks for order (states, steps), keep one required fact per item and add an order check:
   the first match positions of the items must be increasing. The reversed reply from the review must fail (`order`).
   Test both.
4. **Owed checks:** none for you. Main drops the historical claims (live-smoke cleanup, sensitive-read timing) from the
   evidence; they are not needed for S1.
5. Same verification as before, both interpreters, both modes. Do not run the live model or tp-route.
