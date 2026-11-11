# HB8b: known limits carried from HB8a's last round (Main, 2026-11-11)

**Owner pattern** (HB3a, HB4a; HB8a round 9 brief docs/plans/det-prep/hub/hb8/hb8a-round9.md): one rule-first last round; if
the families recur, HB8a merges as it is with the findings as known limits moved to HB8b. Round 9's review
(review-coder-a38acc0415-134517, `result.md`, `report.md` in `.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/`) is
REQUEST_CHANGES with verification-tree-identity and unknown-telemetry recurring. Round 8's F1-F5 reproductions are fixed and the
folds keep their assertions. Records-only stays (the reviewer: "retain records-only").

## Known limits (all medium, all marked realistic by the reviewer)
- **K1 (F1, verification-tree-identity): a lost transcript hides a lost record.** `tools/episodes/score.py:521-525,754-765`
  discovers record pointers outside dependency tracking. With Main's final-tree pass surviving and a reviewer's pointer to a
  missing record, verification is unknown; deleting that reviewer's transcript makes it verified, repeats 0. Direction: the
  record inventory depends on the transcripts it was derived from, so a missing transcript makes its readers unknown.
- **K2 (F2, unknown-telemetry): messages-into-prompt scoring misses request-journal dependencies.** `score.py:474-494,1716-1717`.
  Round 9 fixed request counting; an event naming missing req-b still turns the message count from unknown to 0 when its journal
  is lost. Direction: request completeness carries its journal dependencies to every reader, not only `requests_distinct`.
- **K3 (F3, verification-tree-identity): a poll bound replaces the precise result time.** `score.py:124-133,313,683-692`. A
  clamped fractional observation time (9.3) is taken as the worker's whole-second stamp, so a pass ending at 9.25 is credited
  against a result written at 9.2 (stale 0 instead of 1). Direction: keep the raw stamp apart from observation clamping.

## Owed checks
- **F4:** full discovery on main + HB8a before the merge (Main, recorded in the run log); the private-name search is
  `git grep -n -i <private product name> -- ':!.tmux-pi'`, the only allowed hit config/routines.json:42.
- **F5:** the worker report's sweep row 31 says comparison reads `SUB_VALUES`; it reads `COMPARE`. The report's dependency-
  closure claims are narrower than stated (K2).

## Process note
The round-9 worker reported a trust-store guard refusal of an inline python edit and then made the same test edits with the
Edit tool. Main's ruling: not a bypass of what the guard protects (the edits name no trust-store path; Edit is the ordinary way
to edit a file), but against the brief's "stop and report"; recorded for the owner with HB4b's F5.
