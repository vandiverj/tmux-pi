# G2-D (g2-handover): a lane successor without hand-over is not refused in a whole-module run

Lane `g2-handover`, step G2D. Level: `mid` coder, thinking high. Base: `main` (cd63870 or later). Touches:
`tests/test_lane_recovery.py`, `tests/test_lane_writer.py`, `tests/lane_fixture.py`, `bin/tp-spawn`,
`lib/lane_lease.py`, `lib/lanes.py` (only where the cause is; say which).

## Why
Release gate G2. Baseline name:
`test_lane_recovery.HandOverAndPreambleTests.test_a_lane_worker_hands_its_work_to_its_successor_through_the_same_reservation`.
Main's runs on a clean main (1b1e9fb), session keys unset: passes when run alone; the whole module fails it 4/4
(3.9 and 3.14, 2 each; logs /Users/jerrickvandiver/.claude/jobs/6061f696/tmp/g2order/test_lane_recovery.{39,314}.{1,2}.log):
`tests/test_lane_recovery.py:201 self.refused(run, item["id"])` -> `AssertionError: 0 != 1 : coder-next00001`:
a second `tp-spawn` for the lane with no `--lane-replaces` succeeded, so a second writer got in. That is either
state leaking from an earlier test in the module (test isolation) or a real hole in the one-writer rule that only
shows in some states. Which one matters: the second would be a production bug.

## Change
1. Reproduce: find the smallest set of earlier tests that makes it fail (bisect the module order); save logs.
2. Find the cause and show it (what state the earlier test leaves, and why the writer check passes with it).
3. If it is a real one-writer hole, fix it in the lib/bin code and add a test of that state; if it is test
   isolation, fix the fixture so each test starts clean, and still add a check that the one-writer rule refuses in
   the state that leaked. No skips, no reordering to hide it.
4. Run test_lane_recovery whole three times, plus test_lane_writer, test_lane_lease, test_lane_create and any
   module importing what you changed, on /usr/bin/python3 and /opt/homebrew/bin/python3, keys unset and dummy
   TMUX/TP_*.
5. Do not edit `docs/plans/det-prep/baseline-failing.txt`; list verbatim the baseline line your fix makes pass.

Out of scope: tests/test_lane_records.py (lane g2-lane-cap), other baseline lines.

## Done means
The cause is shown with logs, fixed at its source, whole-module passes 3/3 on both interpreters, and a test pins it.
