# G2-B (g2-lane-cap): two tests still expect a lane cap of 16

Lane `g2-lane-cap`, step G2B. Level: `mid` coder. Base: `main` (cd63870 or later). Touches: `tests/test_lane_records.py`,
`tests/test_trial_session.py`.

## Why
Release gate G2: every baseline failure fixed. Both fail on both interpreters on a clean main (1b1e9fb):
- `test_lane_records.ConfigTests.test_the_shipped_config_sets_max_active_lanes` (line 613): `{'max_active_lanes': 20,
  'defaulted': []} != {... 16 ...}`.
- `test_trial_session.ConfigTests.test_the_shipped_config_keeps_the_keys_the_other_readers_use` (line 81): `20 != 16`.
Cause: the owner raised `config/orchestration.json` `lanes.max_active_lanes` from 16 to 20 in commit 899551c
(2026-11-07, "example guidance prompts for campaigns"); the tests (from 3f0c226) still assert 16. The config is the
owner's decision and is not changed here.

## Change
1. Reproduce both on main; save before logs.
2. Make the two assertions match the shipped value 20. Check (grep tests/, lib/, bin/, docs/) for any other place that
   states 16 as the shipped lane cap and list each with what you did; change only stale statements of the shipped
   value, not defaults or fixtures that set their own cap on purpose.
3. Run test_lane_records and test_trial_session plus any module you changed, on `/usr/bin/python3` and
   `/opt/homebrew/bin/python3`, session keys unset and with dummy `TMUX`/`TP_*`.
4. Do not edit `docs/plans/det-prep/baseline-failing.txt` (Main removes the lines at merge). List, verbatim, the 2
   baseline lines your fix makes pass.

Out of scope: config/ (no change), any other baseline line.

## Done means
Both tests pass on both interpreters in both modes, they failed before, the 2 lines are listed verbatim, and the grep list is
in the result.
