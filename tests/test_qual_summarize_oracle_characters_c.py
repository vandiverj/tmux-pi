"""The path oracle of the summarize_files checker, characters family (rules 24 to 26), part 3 of 3: the delimiters single quotes, curly double, curly single. Split from
tests/test_qual_summarize_oracle.py (which holds the helpers, the generator and the other families) so that each file runs in well under 60 s. No model
is run."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_qual_summarize_oracle as oracle  # noqa: E402  (names only: OracleCases is a mixin, so no test runs twice)


class CharacterOracleTests(oracle.OracleCases, unittest.TestCase):
    def test_generated_path_oracle_in_single_quotes(self):
        self.path_oracle("single quotes")

    def test_generated_path_oracle_in_curly_double_quotes(self):
        self.path_oracle("curly double")

    def test_generated_path_oracle_in_curly_single_quotes(self):
        self.path_oracle("curly single")


if __name__ == "__main__":
    unittest.main()
