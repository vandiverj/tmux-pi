# TQ1 S2: sensitive reads and refusals, measured (Main, 2026-11-11)

Plan: docs/plans/det-prep/tq1-test-consolidation.md. **Nothing is changed yet: each "Decide" line is the owner's.**

## What was measured
11 modules (test_refusal_remedies, test_sensitive_trust, test_sensitive_read, test_trust_guard, test_pi_redaction,
test_claude_redact_hook, test_safe_source_redaction, test_claude_safe_mcp, test_redact_llm, test_redact, test_pi_safe_tools) under
coverage 7.6.1 on a clean worktree of main 468b9b0, one context per test. 3 shards, 6 minutes wall, 671 tests, all passed. No test
calls a model (no test in the section names the LM Studio address).

## Where the time goes (four-cell wall per module)
refusal_remedies 284 s (59 tests), sensitive_trust 122 s (202), sensitive_read 53 s (38), trust_guard 25 s (310), pi_redaction
21 s (1), claude_redact_hook 12 s (16), the rest under 3 s.

### Finding 1 (largest): the sensitive-read table runs three times
`tests/test_sensitive_trust.py:194-198` re-runs every test of `test_sensitive_read.SensitiveReadTableTests` (and
`UnscopedPatchTests`) twice more, with an unrelated trust record and with a refused one, to prove a trust record that does not
apply changes no answer. That proof is worth keeping. But it re-runs the expensive rows too:
- `test_oversized_input_is_judged_without_failing`: 63 s plain, 26 s and 22 s in the reruns (inputs of 150k-250k characters);
- `test_claude_hook_refuses_a_worker_shell_search_and_names_the_safe_tool`: 15 s plain, 47 s and 12 s in the reruns (starts the
  real hook as a subprocess per row).

All 38 tests of test_sensitive_read reach no source line that another test in the section does not also reach: the reruns are
complete copies.
Proposal: keep the reruns for the decision rows (fast, in-process); run the oversized and hook-subprocess rows once, plain, and in
the reruns check one representative row each (the record lookup is the same code for every input). Expected saving ~100 s per cell
under load. **Decide:** apply (recommended) / leave.

### Finding 2: the oversized inputs are larger than the rule needs
The oversized test checks "judged without failing" with 150k-250k character commands. Main has not yet found a size limit in
`lib/sensitive_read.py` that these sizes sit just above (`_brace_expand` caps at 64 alternatives; walks are capped at 2 s). If no
limit needs crossing, the same cases at ~10-20k characters check the same paths. The consolidation worker would show, by coverage,
that the smaller inputs reach the same lines and still fail on the old bug they were added for (git log of the test). **Decide:**
include in the lane (recommended) / leave.

### Finding 3: `test_pi_redaction` is one 44 s test
`PiRedactionTests.test_tool_result_handler` runs the pi extension under node, so coverage cannot see what it reaches. It is one
test running a list of cases; its time is node start-up plus cases. Not judged here; listed for S8 (node tests) if the owner wants
a closer look.

### Not a problem
- `test_refusal_remedies` is the slowest module, but it is mostly about lanes and process remedies, not sensitive reads, and it
  holds 5744 source lines no other module in this section reaches (lanes, review rounds, worker process, routing). Keep. Its
  per-test setup (real dispatch, 6-33 s per test) is the same cost as the lanes section and benefits from the same shared-setup
  change proposed in S1.
- `test_trust_guard` has 310 tests in 25 s: generated `_main`/`_worker` row pairs (112 identical-coverage groups across the
  section, mostly these). They are cheap; folding them would only shorten the listing. Leave.

## Proposed consolidation lane (after the owner's decisions)
One lane, Sonnet worker, cross-provider review: finding 1 (reruns keep decision rows, one representative of the expensive rows),
finding 2 (smaller oversized inputs if coverage and the old bug agree). Proof: covered lines of lib/sensitive_read.py,
lib/trust_guard.py, lib/trust.py, lib/redact_hook.py the same or larger; time and count before and after; the section's modules plus
`tests.test_session_env` pass in four cells.

**Decide:** (a) finding 1; (b) finding 2 in the same lane. Main's recommendation: both.
