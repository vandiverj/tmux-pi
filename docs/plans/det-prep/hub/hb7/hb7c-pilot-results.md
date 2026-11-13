# HB7c pilot results (Main, 2026-11-12)

Owner approval: "HB7c pilot approved" (24 runs as planned; the no-profile G0 arm was not added). Run from main cb74117 with
`tools/profiles/pilot.py` catalogs (copies in the ignored `bench-results/m1-pilot-catalogs/`); records in
`bench-results/m1-{haiku,sonnet}-{expert,guided}/{1,2,3}.json`. Tasks code-accuracy-bug and code-shot-clock, 3 reps per arm,
order fixed, one arm at a time. 24 worker runs, 0 LM Studio calls.

**Pin check:** every record's `models` is exactly the arm's pinned model (Haiku arms: claude-haiku-5-5; Sonnet arms:
claude-sonnet-5-5) and its `env` carries the arm's TP_PROFILE and catalog. No run is invalid.

| Arm | code_pass (3 reps) | output tokens | cache-read tokens | wall s |
|---|---|---|---|---|
| Haiku expert | 1.0, 1.0, 1.0 | 16303, 13546, 18210 | 901k, 879k, 1093k | 106, 101, 122 |
| Haiku guided | 1.0, 1.0, 1.0 | 14922, 20606, 16235 | 895k, 1519k, 1208k | 101, 148, 116 |
| Sonnet expert | 1.0, 1.0, 1.0 | 3496, 2543, 4036 | 239k, 278k, 420k | 54, 54, 60 |
| Sonnet guided | 1.0, 1.0, 1.0 | 3981, 4254, 3946 | 399k, 439k, 346k | 65, 60, 55 |

Tokens are per rep (both tasks). Main's tokens are not in the records (E2).

## Decision (M1 rule)
Adopt a profile for a model only if it beats the other on code_pass beyond the spread of the reps without more tokens per
pass. Every run passed, so correctness cannot separate the profiles, and token ranges overlap within each model. **No profile
is adopted; workers keep the default packet (G0).** The profiles stay available by `TP_PROFILE` for later tests.

## What the pilot does show
- Both tasks are at ceiling for both models. A deciding pilot needs harder tasks (planted traps that a careless edit fails),
  or a review-verdict / retries-to-approval metric, which the bench does not record yet.
- On these tasks Sonnet used about a quarter of Haiku's output tokens and a third of its cache reads, in about half the wall
  time. Haiku's lower price per token may not make it cheaper per accepted result here; that needs a price check before any
  routing change (owner decision).
- Next slice options (not started): the Qwen local arm under the LM Studio cap; harder tasks; G0 arm (owner decision).
