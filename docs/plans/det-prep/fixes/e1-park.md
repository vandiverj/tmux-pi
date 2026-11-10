# E1 (measure) parked after round 3

Round 3 reviewer (review-coder-5a5f76bab8-160611): REQUEST_CHANGES, one code finding, family probe-specificity (the same family three rounds running).
- `git -c protocol.version=2 fetch origin/develop` still counts as lane-base, because `GIT_FETCH` (`lib/routines.py:36`) does not consume the value after `-c`.

Suggested next brief (rule-first): parse git global options as a token list (`-C`/`-c` take a value; `--opt=val`/`--opt` don't) and pick the subcommand from the first non-option token, instead of extending the regex. Test every global option git documents.

State: uncommitted in ../tmux-pi-wt-measure. 39 tests OK. Every other finding from rounds 1–3 is fixed. The owner decides.
