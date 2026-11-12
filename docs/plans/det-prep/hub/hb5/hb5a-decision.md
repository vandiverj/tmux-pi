# HB5a: owner decision (adopt the repeated-verification report?), lane hub-verify head 7a97277

VR1 says "Advisory: the owner decides on adoption", so Main stopped before merge. Nothing from this lane is in main.

## What it adds (read-only; no change to test-run.sh, gates, review, lane close or tp-agent)
`python3 tools/checks/repeats.py SESSION_TASKS_DIR [--lane L] [--json]` reads the task records (`tp-test-run:v1`, `tp-gate:v1`,
tests.jsonl, meta.json) and reports which checks ran more than once on the same tree: exact duplicates, runs covered by an
earlier wider run, retries (fail then pass) apart, never-reused kinds (live model, network, live tmux, bench) apart, and per
lane/step the repeated seconds and their share. It writes nothing and masks secret-looking assignments in printed commands.
A run counts only when every original word of its command passes a strict grammar (in-tree relative paths, bare names, no
`~`/`$`/glob/brace/quote/`..`, no option values, no script operands); anything else is `outside: not understood`. That rule
came out of five review rounds: each looser rule let some spelling that reads outside the tree count as a duplicate.

## Measures (case 1 = planted schedule; case 2 = this Mac's real task dirs, read-only)
| measure | value |
|---|---|
| case-1 pair scoring | 36/36 pairs match (same / covers / retry / never / none) |
| attack sweep | 1436/1436 command forms classified as expected (wrappers x payloads, executable heads) |
| case 2 records | 349 test-run records; 160 eligible (158 unittest runs) |
| repeats found | 7 exact duplicates + 2 covered = 92.5 s, 0.5% of recorded test time; mostly coder -> reviewer reruns (~3 s each) |
| not visible | Main's own cell and full-suite runs (no record; the run log shows them every round, minutes each); 189 runs outside (out-of-tree scripts, env-varied, not understood) |
| run time | whole real session in ~0.1 s |

What it points to: the repeat VR1 names first (Main rerunning the worker's checks on the same tree) is the large one, and no
record shows it. Recorded worker/reviewer repeats are small. So the next step is **recording Main's runs** (through
test-run.sh or a gate record), then a shared fingerprint that includes env and the interpreter (test-run records have no
interpreter field today; the report accepts an absolute python/shell/env/perl by name), and only then `tp-check` reuse.

## Rounds
| Round | Worker | Review | Result |
|---|---|---|---|
| 1 (Opus) | coder-fefe75a954, e90070d | review-coder-fefe75a954-023236 | REQUEST_CHANGES: gate scope, never-reused forms, shell text as path |
| 2 (Sonnet) | coder-0a33e7896d, 0cf8013 | review-coder-0a33e7896d-025338 | REQUEST_CHANGES: masking regression, lint index scope, env -i, cd/.. |
| 3 (Sonnet, rule-first) | coder-dfa67508a3, 276a493 | review-coder-dfa67508a3-030720 | REQUEST_CHANGES: escaped-quote masking, option values, tilde/glob |
| 4 (Sonnet, rule-first) | coder-68f2fb8255, a794e03 | review-coder-68f2fb8255-032223 | REQUEST_CHANGES: executable heads skipped the grammar (Main's brief wording) |
| 5 (Sonnet, rule-first) | coder-945df346e8, e15ad42 | review-coder-945df346e8-033736 | REQUEST_CHANGES: perl wrapper head skipped before validation |
| 6 (Sonnet, rule-first) | coder-abcd34fd0b, 7a97277 | review-coder-abcd34fd0b-034623 | APPROVED_WITH_NOTES (consumer table omits helpers; no bypass found) |

Cells on main 961e89f + 7a97277 (tree 8d4af09): test_session_env, test_check_repeats, test_docs_wiring, 40 tests x 4
(py3.9/3.14, outside tmux / simulated in-tmux env), 0 failures. Fail-before of round 6's rows on e15ad42: 4 failures.
Full suite on the same merged tree 8d4af09 (`tests/run_parallel.py`, private TMUX_TMPDIR): 5535 tests OK (skipped 78), 546 s
(5533 before + the 2 tests of the new module).

Process notes for the owner: round 1's worker rewrote three guard-refused analyses as scripts; rounds 3 and 5 edited a lane
file once by inline Python instead of the Edit tool; round 6 used `git stash push/pop` in the lane worktree to test the old
code. All disclosed. Main authorised two narrow cases after the workers stopped and asked: the round-3 reviewer's in-process
probes from its own evidence dir, and the round-4 worker's count from its own report file.

## Options (Main recommends A)
- **A. Adopt the report:** Main closes the step and merges (approved review of the exact tree). Use it as
  `python3 tools/checks/repeats.py .tmux-pi/sessions/<id>/tasks`. Next step HB5b: record Main's runs so the large repeat shows.
- **B. Park it:** keep the lane; skip straight to recording Main's runs, measured from the run log instead.
- **C. Adopt with a looser rule:** count more runs (e.g. allow option values) at the risk of false duplicates; needs a round
  and a re-review.
