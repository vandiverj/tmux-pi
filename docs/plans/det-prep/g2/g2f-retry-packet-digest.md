# G2-F — retry_packet state digest names tasks in filesystem order on a tie

## Failure (found by Main 2026-11-08 while verifying G2-A; not in the original baseline)
`FAIL: test_a_family_named_in_two_rounds_refuses_a_plain_retry (test_retry_packet.RepeatedFamilyGateTests)` at
tests/test_retry_packet.py:256, `assertEqual(restored, self.state_digest())`: `...oder-1"... != ...oder-0"...`.
Clean main 64cc2f9, 3.14, dummy-session mode: 4 of 18 runs fail (repeat script: Main's rp-repeat.sh, six runs of the class).
Also seen on 3.9 by the G2-A reviewer.

## Suspected cause (prove or refute it first)
`state_digest` (tests/test_retry_packet.py:209-219) renames each task `<role>-<n>` in the order
`sorted(metas, key=(depth, review order))`. `metas` comes from `glob("*/meta.json")` (filesystem order of random ids), so two
tasks of one role with equal keys get their numbers by directory order, which differs between the two builds compared.

## Rule
A digest that compares two builds must name every task from facts both builds share, never from random ids or listing order.
If any two tasks of one role cannot be told apart by those facts, the digest must say so (fail loudly), not pick one.

## Done means
- Show the tie: which two tasks tie, with what keys (a log line or a small probe), and a forced-order reproduction that fails
  every time on the unfixed test (e.g. patch glob order reversed), not a 1-in-5 chance.
- The fix in the test helper only (no lib/ or bin/ change unless the cause is proven to be in the product; then stop and report).
- The forced-order reproduction passes after the fix; 30 repeats of RepeatedFamilyGateTests pass on both Pythons.
- Sweep: every other digest/naming helper in tests/ that orders by glob/listdir/iterdir with possible ties, listed with
  verdict (tie possible or not, fixed or not). Table in the report.
- Module matrix: test_retry_packet both Pythons x both modes.
- Do not edit docs/plans/det-prep/baseline-failing.txt (Main owns it).
