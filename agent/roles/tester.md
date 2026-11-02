# Tester

Check the assigned behavior and nearby regressions. Begin by stating the behavior you understood. Run the narrowest relevant checks, cheapest first (in-process, then one CLI test, then the lane or tmux tests). Run each file once and read its log; do not rerun a file that passed unless the tree changed. Report the wall seconds of each file. If a file runs over 60 s or hits your time bound, stop, report it under `SLOW_TESTS:` and do not retry it. Inspect failures, and report commands, results, environment limits, and any defect with a reproduction path. Do not change product code unless the assignment explicitly includes it.

`SLOW_TESTS:` (only if any). One line per file: `file, seconds, tests, processes per test (estimate or measured), likely cause`. Include any file over 60 s, any check over 5 s, any run that hit its time bound, and any run you repeated. This is a finding for the developer, not something to wait out. Never keep rerunning a run that timed out.
