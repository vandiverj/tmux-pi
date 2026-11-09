"""lib/trial_shadow.py default trial run ids (G2-C): two default prepares of one task inside the same clock second must not collide,
however fast the machine is, and an explicit --run that is taken is still refused. The clock is frozen, so the test does not depend
on how quickly the interpreter runs. Fixtures: trial_shadow_case.py."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import datetime
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

FROZEN = datetime.datetime(2026, 11, 9, 1, 1, 9, tzinfo=datetime.timezone.utc)


class _FrozenDatetime(object):
    """Stands in for the `datetime` module inside trial_shadow: now() is always the same instant."""
    timezone = datetime.timezone

    class datetime(object):
        @staticmethod
        def now(tz=None):
            return FROZEN


class DefaultRunIdTests(ShadowCase):
    def test_pure_ids_after_the_first_in_one_stamp_are_distinct_and_valid(self):
        ids = [ts.default_run_id("src-1", "20261109010109", n) for n in range(12)]
        self.assertEqual(ids[0], "sh-src-1-20261109010109")
        self.assertEqual(ids[1], "sh-src-1-20261109010109-2")
        self.assertEqual(len(set(ids)), 12)
        long = [ts.default_run_id("x" * 40, "20261109010109", n) for n in (0, 1, 9, 10, 99)]
        self.assertEqual(len(set(long)), 5)
        for run in ids + long:
            self.assertTrue(trial_session.RUN.fullmatch(run), run)

    def test_two_default_prepares_in_one_second_get_two_runs_and_an_explicit_taken_run_is_still_refused(self):
        with mock.patch.object(ts, "datetime", _FrozenDatetime):
            first_dir, first = self.prepare()
            second_dir, second = self.prepare()
        self.assertEqual(first["run"], "sh-src-1-20261109010109")
        self.assertEqual(second["run"], "sh-src-1-20261109010109-2")
        self.assertTrue(first_dir.is_dir() and second_dir.is_dir() and first_dir != second_dir)
        with self.assertRaises(ts.ShadowError) as caught:
            ts.prepare(self.state, "src-1", model=MODEL, thinking="medium", env=self.env, run=first["run"])
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(ts.cleanup(second_dir, self.env), [])
        self.assertEqual(ts.cleanup(first_dir, self.env), [])
