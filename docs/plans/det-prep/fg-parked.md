# F-GREP parked after round 5 (source-path-redaction family, 6th site)

Main's rule set before round 5: another site in this family after round 5 parks the lane for the owner.

Round-5 review review-coder-5365a5da2b-002800 F1: lib/redact_hook.py:483-487 checks only the first 64 operands of a wrapper (env etc.); 65 assignments then `rg -n api_key .` is not detected as a search and a protected decoy shows (Claude post_result and Pi tp-redact --command).

State: all earlier findings confirmed fixed by the reviewer (displayed-path redaction, pointer redaction, symlink swap, colon/long heads, unnumbered heads, git -C . grep). Main's 9-module two-mode run on round 5: all OK. Uncommitted in tmux-pi-wt-safe-source.

Main's recommendation: one more small round. The cap is a violation of the round-5 rule "uncertain parse counts as search" (an implementation slip, not a design gap): when any operand limit is reached, return true. Add a row with 65 and with 10,000 assignments, and a test that grep-mode is chosen whenever a limit stops the parser. Then review; merge to integrate on approval.

Owner decisions also open from this lane: F-PATHNAMES (Glob/Grep filenames lists), F-BASHOUT (non-search command output rules).
