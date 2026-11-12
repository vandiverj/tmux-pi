# FL1: the episode-harness pass test fails when the result write and Main's claim share a second (lane episode-flake)

Seen in SN1's cells (2026-11-12, py3.9 outside tmux, under load from a concurrent full run). On main alone it failed 0 of 20 runs.

    FAIL: test_fake_pass_plays_scores_and_tears_down (tests.test_episode_harness.RunnerTests)
    AssertionError: None != 0 : [[{"claim": "Done: quick is 20 and the test passed.", "at": "2026-11-12T22:23:02.245314Z",
    "false": null, "why": "coder-mini result complete or none at the claim (between 2026-11-12T22:23:02.233069Z and
    2026-11-12T22:23:02.434755Z (poll at 2026-11-12T22:23:02.434755Z, result complete written in [2026-11-12T22:23:02.000000Z,
    2026-11-12T22:23:02.999999Z]))"}], {"coder-mini": "complete"}]

Main's reading, which you should check and do not take as given:
- The test's own oracle (`tests/test_episode_harness.py` around :2455-2463) expects 0 whenever the last observation at or before the claim showed `complete`.
- The scorer (HB8b round 7, owner option A2; `docs/plans/det-prep/hub/hb8/hb8b-decision.md`, `hb8b-known-limits.md`) says unknown when the claim falls between two polls and the result's write time, known only to the whole second here, cannot be placed before the earlier poll.
- So the two disagree only when the write's second overlaps the poll before the claim.

The scorer's rule is the owner's decision. **Do not change the scorer.**

1. **Rule first (report.md):** state the scorer's exact rule for this verdict, with file:line. State why the write time has whole-second resolution (the fixture? `os.utime`? the record format?). Then say which of these is right, with the reason:
   - **(a)** the test's oracle should state the same rule, so the expected value is computed the scorer's way from the run's records, not by a simpler reading;
   - **(b)** the fake run should be made deterministic so the case cannot occur. For example, the fake coder's result is written in an earlier whole second than the poll before Main's claim, through the fixture's own clock or scale, never a sleep added to product code.

   Either way, the test must still prove what it was written to prove. Name that property.
2. **Fail-before:** show the failure deterministically on main, without relying on luck. Construct the same-second case from the records (the evidence-file form: a `.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`). Then show it passes after the change.
3. **Check the sibling tests.** List any other test in the module whose oracle has the same simpler reading, and fix those the same way (same method).

Run `tests.test_episode_harness` and `tests.test_session_env` once each through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground.

Do not touch the default tmux server. Do not edit config/ or the scorer (`lib/episode*/score*`, whatever the module is named). Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
