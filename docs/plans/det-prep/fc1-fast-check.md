# FC1: fast-check for proposed consolidations (loop idea H30)

Owner, 2026-11-10: when the reviewer recommends combining duplicated TypeScript logic, run a focused property-based test that
checks the existing implementations and the proposed shared one behave the same across the inputs where they should agree;
return any minimized counterexample and its replay seed to the reviewer. Advisory only: the owner decides whether to combine
code or keep separate behavior. Status: **queued**.

## Expected gain
Subtle behavior differences are caught before a consolidation is approved, and the owner gets a concrete counterexample when
deciding.

## Cases
1. **Real candidates.** Duplicates found by the code-quality eval's scans on main (jscpd 35, fallow semantic 68 groups), first
   the `reviewNotice` logic in `agent/extensions/subagents.ts` and `claude/mcp/subagents.mjs`. Pick 2-3 where a shared
   implementation is plausible.
2. **Intentional difference.** A pair that looks duplicated but is meant to differ (for example a Claude path and a Pi path of
   the same notice). Each side is checked against its own contract, and the eval passes only if that difference is preserved.
3. **Regression.** A proposed shared implementation with a known or deliberately introduced change (an off-by-one, a dropped
   branch, a changed default), labeled in `expected.md`.

For every case, write before any generator: the shared contract, the valid input domain (where the implementations should
agree), and the known intended differences.

## Arms (same cases)
- **A, existing targeted tests** that already cover the functions (`tests/*.mjs`, hook tests).
- **B, hand-written edge cases:** at most 10 inputs written from the contract.
- **C, bounded fast-check:** one property per contract (old vs shared on the valid domain), fixed seeds, `numRuns` 1,000,
  time limit 10 s per property. Record the seed and replay path (`fc.assert(…, { seed, path })`) for every failure, plus the
  minimized counterexample.

## Measures
Meaningful differences found per arm; misleading failures (inputs outside the domain, a generator bug, a difference the
contract allows); setup effort (minutes and lines for the generators); runtime.

## Decision
Recommend whether fast-check earns an on-demand place in the refactor workflow, and which changes justify it (for example:
pure functions with string or number inputs and more than 2 branches). Fit it in only if C finds a meaningful difference that
A and B miss in at least one real or regression case, with no more than one misleading failure across the set.

## Missing before it can run
- fast-check is not installed. It needs the owner's approval for a pinned, isolated install with `--ignore-scripts` (the
  code-quality eval's approval covered only its four tools).
- The repo has no `package.json` or `tsconfig`. Properties run under node 24 type stripping, or modules that need more are
  compiled with the local TypeScript 5.9.3 already in the npx cache. Check this per case.
- Fixtures for cases 2 and 3 if no real intentional-difference pair or historical regression is found.
