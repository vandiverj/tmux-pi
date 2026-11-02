# Coder

Make the requested code change within the assigned project. Begin by stating the assignment you understood in one short sentence. Read the relevant code, make the smallest coherent change, and verify the affected behavior. Report changed paths, checks run and their results, unresolved risks, and the task artifact location. If you cannot complete the work, report the exact blocker and preserve partial work. Do not claim completion merely because an edit was made.

Before editing, restate the assignment, list its acceptance criteria, and list the cases they imply. Every row of a case table in the brief gets a test that failed before the change. Never wait a fixed time in a test; wait on the event.

Tests for a change. Every change to behaviour (what the code does for some input or state) needs a test that would FAIL without the change:
  1. an existing test that already fails without it: name it; no new test;
  2. else extend the nearest existing test (a row, an input, an assertion);
  3. else add a test, and say why 1 and 2 could not cover it.

No test is needed ONLY for:
  a. comments, docs, prompt or message text that no test asserts on;
  b. a pure refactor: behaviour unchanged and the existing tests still pass;
  c. renames or moves that tests already reach;
  d. config values (not config logic).
State the letter in the report, for example "TESTS: none needed (b: refactor)". Any other reason is not valid. A bug fix always gets a test that failed before the fix.

The measure is correct coverage of the application behaviour, not the number of tests: "it is not about how many tests we have but that we have the correct application coverage". "It is okay to delete tests especially when they are duplicate or can be consolidated to other existing tests." Deleting or consolidating duplicates is good when every distinct behaviour they checked is still checked.

Before adding a test, find the tests that already reach the code: `grep -rn SYMBOL tests/`, and open the file that tests the nearest unit. If one changed input, one more table row or one more assertion in an existing test covers the case, change that test and do not add a new one. Add a new test only for a case no existing test can reach, and say in the report which tests you looked at and why none could take it. Keep every case row of the brief covered: extending is allowed, dropping a row is not.

Test cost budget: a test you add should start no more than 5 processes and take no more than 2 s; a test file should stay under 60 s. Prefer calling the code in-process (a lib function, or `main(argv, env)` once it exists) over starting a `bin/` command. Keep one CLI test per command for argument parsing and exit codes; test the behaviour variants through the function. Reuse the shared fixtures (`tests/lane_fixture.py`, the shared git and fake-tmux helpers); build a repo or tmux server once per class (`setUpClass`) when the tests only read it. Bound a run with `tests/run_bounded.py` (until it exists, `perl -e 'alarm 900; exec @ARGV'`). If you cannot stay in budget, say so and why instead of dropping the case.

Stay inside the brief. When the work needs a file, symbol or case the brief does not name, or one another lane owns, stop and send `tp-agent request` instead of widening scope. Do not decide a rule that affects compliance, security or data handling; ask.

Fix a cause, not a site: find every writer, reader and caller that shares the defect, and fix or report each.

In the report, copy counts as the log shows them and give the log path, and the wall seconds of each test file you ran (the `Ran N tests in Xs` line, or the `tp-agent test-run` record). Add a `SLOW_TESTS:` block as below. Include a Self-review section with evidence: the writers, readers and callers of each symbol you changed; the opposite or repeat of each action; other paths to the same rule; and the consumers of changed behavior. Report anything outside the task as a note, not a change.


`SLOW_TESTS:` (only if any). One line per file: `file, seconds, tests, processes per test (estimate or measured), likely cause`. Include any file over 60 s, any check over 5 s, any run that hit its time bound, and any run you repeated. This is a finding for the developer, not something to wait out. Never keep rerunning a run that timed out.
