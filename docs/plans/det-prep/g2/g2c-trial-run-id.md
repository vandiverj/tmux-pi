# G2-C (g2-trial-runid): default trial run ids collide within one second

Lane `g2-trial-runid`, step G2C. Level: `mid` coder. Base: `main` (cd63870 or later). Touches: `lib/trial_shadow.py`,
`tests/trial_shadow_case.py`, `tests/test_trial_shadow_branches.py`, `tests/test_trial_shadow_end_path.py`, `tests/test_trial_shadow_run_id.py` (new, if needed).

## Why
Release gate G2: every baseline failure gets a found cause and a real fix. Baseline names:
- `test_trial_shadow_branches.AccidentTests.test_an_index_or_a_head_file_that_is_garbage_stops_the_collect_with_an_error_and_the_cleanup_still_works (name='HEAD')`
- `test_trial_shadow_end_path.ReviewVerdictTests.test_an_inconclusive_first_review_is_no_review_and_its_row_keeps_inconclusive (review='I looked at it and i')`

Main's runs on a clean main (1b1e9fb), session keys unset: each passes alone. The whole test_trial_shadow_branches
module fails this subtest 2/2 on /opt/homebrew/bin/python3 3.14 and passes 2/2 on 3.9 (logs:
/Users/jerrickvandiver/.claude/jobs/6061f696/tmp/g2order/test_trial_shadow_branches.314.{1,2}.log):
`trial_shadow.ShadowError: trial run 'sh-src-1-20261109010109' already exists (...); choose another --run`
from `prepare` (`lib/trial_shadow.py:525-528`). `default_run_id` (`lib/trial_shadow.py:482`) stamps to the second,
so two `prepare` calls for one task inside the same second pick the same id; subtests that prepare twice hit it
when the interpreter is fast enough. test_trial_shadow_end_path passed 4/4 whole-module runs for Main; it failed in
the full-suite gate runs (4 modules in parallel); find whether it shares this cause and show it.

## Change
1. Reproduce deterministically first (e.g. freeze or patch the clock so two prepares land in one second) on main;
   save the before log.
2. Fix the cause in `lib/trial_shadow.py`: a default run id must not collide for a second default prepare of the
   same task (unique suffix, finer stamp, or pick the next free id under the trial dir without a race; say which and
   why, and keep explicit `--run` collisions refused as now). List every reader of run ids (grep lib/, bin/, tests/)
   and confirm each still works with the new form.
3. A test that fails on main without the fix regardless of interpreter speed.
4. For the end_path name: show its cause with a log; fix it if it is this cause or is in these files, else report.
5. Run test_trial_shadow_* modules and anything that imports trial_shadow, on /usr/bin/python3 and
   /opt/homebrew/bin/python3, session keys unset and with dummy TMUX/TP_*. Run test_trial_shadow_branches whole
   three times on 3.14.
6. Do not edit `docs/plans/det-prep/baseline-failing.txt` (Main removes lines at merge); list verbatim the
   baseline lines your fix makes pass.

Out of scope: lib/trial_record.py and lib/routing_evals.py (lane g2-py39 is open there); other baseline lines.

## Done means
The collision cannot happen for default ids (test shows it), branches whole-module passes 3/3 on 3.14, the
end_path cause is shown, and the lines fixed are listed.
