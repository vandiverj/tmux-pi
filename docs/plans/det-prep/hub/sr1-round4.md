# SR1 round 4: one structural boundary rule, oracle from the rule (Main, 2026-11-11)

Round 3's review (review-coder-7fe43981bd-160233: `result.md`, `report.md`, `probe.py`, `boundary_probe.py`, `probe.log` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-7fe43981bd-160233/`) is REQUEST_CHANGES: F1 (incomplete-stop-findings, third time), F4/F5 checks.
Rounds 1-3 briefs (sr1-stop-rule.md, sr1-round2.md, sr1-round3.md) still apply except where a rule below replaces them.

**Main's diagnosis.** Partly the brief: round 3 and Main's follow-up named boundaries by example (headings, bold paragraphs,
`---`), so the fix covered the examples. Partly the worker: the `**Closes by:**` label case was handled by exempting any
paragraph that *ends* in a label, which reopened borrowing, and list containers were not considered. **Route rule-first:** the
rule below is structural and the attack oracle is computed from the rule, not from the code.

## The rule (one function, used by authorize and close)
Every line after a counted finding lead, up to the next counted finding lead or end of text, is exactly one of:
1. **Label line:** the whole stripped line is a label: optional `**`/`__` wrap, `Family:` or `Closes by:`, a value, nothing
   before it. It belongs to the open section.
2. **Boundary line:** an ATX heading; a line that starts with `**` or `__` and is not a label line; a list item with any marker
   (`-`, `+`, `*`, `1.`, `1)`) at any indent, with or without a bold lead; a `>` quote; a code fence; a line indented 4+
   spaces; a thematic break (`---`, `***`, `___`). It **closes** the section; nothing on it or after it counts for that finding.
   A label written on a boundary line (`**Owed before close:** ... Closes by: check`) belongs to no finding.
3. **Prose line:** anything else. It stays in the section. A label written as the suffix of a prose line in an open section
   counts for that finding (today's reviews do this); plain prose borrowing across paragraphs stays a known limit (review F2 note).
A finding with no label of a kind in its section, or two of one kind, refuses (unchanged). Remove the "paragraph ends in a
label" exemption. Measure the 243 real reviews again and report accounted/refused against round 3 (122/42), naming every
review whose outcome changed and why.

## Attack (input space, not examples)
Build the attack script's cases as a product: {each boundary kind in 2 above, each list marker with and without bold lead,
indented list, label line wrapped `**`/`__`/plain, prose line} x {label on its own next line, label as suffix on the same line,
no label} x {authorize, close}, after a code finding with a family and no closure. The expected outcome of each case comes from
the rule text above (write the oracle function first, from this brief, before running it against the code); report any case
where you think the rule is wrong instead of changing the oracle to match the code. Include the review's two F1 cases verbatim.

## Tests (tests earn their place)
Extend the existing StopRuleTests table, same fixture: the review's two F1 cases (bold list lead `- **rerun log:** ... Closes
by: check`; `**Owed before close:** ... Closes by: check` paragraph) and one row per boundary family not yet covered (list,
quote/fence/indent as one row each only if cheap). New rows run through both authorize and close. Fail-before on 8bb2ad8.
**Report correction (review F5):** `report.md` must say which table rows run through authorize (only those with the fifth field)
instead of "every row".

## Main owes (review F4)
Four-cell logs of round 3 on preview 5b65640 (358 tests each, 0 failures), absolute paths:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-7fe43981bd/main-evidence/py39-out.log`, `py39-in.log`, `py314-in.log`, `py314-out.log`, `sr1c-verify.log`,
`sr1c-verify.sh` (exact command), same dir. Main reruns them for this round and runs combined discovery before close.

## Verification (the worker tests once)
As round 3 (tests.test_lane_steps, tests.test_lane_records, tests.test_family_sweep, tests.test_session_env once, same
environment and log rules). Same edit list, same safety rules: no Claude/pi start, nothing to 127.0.0.1:1234 or any network,
private tmux `-L` sockets only, never the real lanes, safe_grep/Read only, never read claude/mcp.json, .env*, auth files or
config/local.json; a guard refusal means stop and report; approve no prompt; commit nothing; no background shells.
`RESULT_STATUS: complete` as the last line.

## Main's decision on the worker's corpus result (addendum, same round)
Refusing 41 more real reviews because evidence bullets or code sit inside a finding is the brief's mistake: lists and code are
normal finding bodies. Replace rule 2's split with:
- **Boundary lines** (close the section): ATX heading, a non-label line starting `**`/`__` that is not inside a list item,
  thematic break, counted finding lead.
- **Contained lines** (stay in the section, never carry a label for any finding): list items with any marker at any indent and
  their continuation lines, `>` quotes, fenced code (whole fence), lines indented 4+ spaces. A label on one of these counts for
  nothing, so the review's F1 list case still refuses (F7 has no closure of its own).
- Label lines and prose lines as before. A 4+ space indented label no longer counts (contained).
Known limit to state in docs and report: a standalone label line right after a contained block counts for the open finding.
Recompute the oracle from this text first, rerun attack, corpus (report against round 3's 122/42 and your 81/84) and only the
test module(s) you change; keep the 6 rows (adjust expectations only where this text says so).
