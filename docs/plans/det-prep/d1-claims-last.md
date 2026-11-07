# D1: the reviewer sees the change before the claims

Lane `review-order`, step 1. Level: `mid` coder. Touches: `bin/tp-review`, `lib/review_rounds.py`,
`agent/roles/code-reviewer.md`, `tests/test_tp_review.py`, `tests/test_review_rounds.py`.

## Why
The reviewer's context today is: Main's Orient/Frame (`task.md`), the author's whole packet, the author's result with its
self-review and TESTS letters, and only then earlier findings and the diff; round 1 gets no diff at all. The reviewer reads
every claim before it has independent evidence. Published measurements show that a prior framing of a change as clean cuts
defect detection sharply across models, and same-family judges favour their own outputs; packet order is the cheapest lever
on both.

## Change
1. New order of the context list in `bin/tp-review`: (a) the brief or request text and the acceptance list, (b) `scope`
   (changed files, scoped git commands), (c) the diff of the current tree against the review base (round 1 included: add a
   `diff_now` in `lib/review_rounds.py` that produces the base...tree diff with the same redaction and segmentation the round-2
   diff gets; the base is the last reviewed state, else the lane's last closed step, else the lane base, else HEAD for a
   non-lane task with the working-tree diff), (d) earlier findings, (e) timer waits, (f) Main's evidence/contract/dispositions,
   (g) the author's packet, (h) the author's result, (i) `task.md`. Items g to i are headed `Author's account (read after
   forming your own findings)`.
2. Task text: add the sentence `Form your findings from the diff and the tree first. Then read the author's account and
   Main's frame and check each claim against what you found; a claim you could not verify is 'Closes by: check'.` Keep every
   existing rule.
3. The review packet budget is unchanged; when the round-1 diff does not fit, segment it like the round-2 diff; the packet
   is refused, never trimmed, exactly as today.
4. Record in the review task's `meta.json` the order version (`packet_order: "claims-last:v1"`) so evals can split before and
   after.

## Done means
1. For a round-1 review the context list order is a to i above; a test asserts the order of the generated list file.
2. Round 1 carries a diff of the current tree against the correct base for a lane task, a retried task and a plain task (three tests).
3. The author's packet, result and task.md come after every independent item and under the stated heading.
4. The task text contains the new sentence; existing rules (findings, coverage, verdict) are unchanged (extend the test that pins the task text).
5. The review `meta.json` has `packet_order`.
6. Tests fail without the change; `bin/tp-review`'s header comment documents the order.
