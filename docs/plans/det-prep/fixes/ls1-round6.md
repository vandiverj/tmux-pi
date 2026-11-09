# LS1 round 6 — design change: a fixed answer format, parsed exactly (Main, 2026-11-08)

Review 5 (review-coder-10744b0248-193534) is REQUEST_CHANGES in the SAME three families as rounds 2-4:
required-fact-completeness (F1), citation-support-coverage (F2), citation-file-validation (F3). Each round closed the
sites named and the reviewer found new prose shapes. The cause is the rule, not the sites: the checker grades free prose
(headings, bullets, `Line N` under a heading, spans) with regexes, and prose has unbounded shapes. Change the rule.

## The rule
A summarize answer is a list of FACT LINES, and only fact lines count:

    - <claim text> [<path>:<line>[-<line>]][, <path>:<line>...]

1. One fact per line; each line starts with `- ` and ends with one bracketed citation list. The path is one of the chore's
   files, written in full as the chore names it. No heading, bold line, earlier bullet or `Line N` ever supplies a file.
2. Any other line (headings, prose, blank) is ignored for facts AND for citations. A line that starts with `- ` but has no
   valid trailing citation list is `uncited` and fails the reply (no partial credit, no borrowing from neighbours).
3. A citation is valid only when the file is a chore file, the range is within the file and at most N lines (keep the
   existing range_too_wide limit), and the range covers one of that fact's `support_lines`.
4. A required fact is met only by ONE fact line whose claim matches the fact AND whose citations support it (both on the
   same line). This removes heading-spanning matches (F2) and heading/file context inference (F3) by construction.
5. Behaviour facts (F1): match the behaviour regex against the claim with every backticked span, every `path:line`, and
   every identifier in the cited files' symbol table (names defined or parameters in the fixture, computed from the
   fixture, not hand-listed) removed. A claim that is only names and signature text has nothing left to match.
6. The prompt text in config/local.example.json `prompts` tells the model this exact format, with one short example. The
   runner's records keep the raw reply; the checker's result names, per failed line, which rule (1-5) it broke.

## Done means
- Old prose-shape code paths (heading context, `Line N` resolution, `_context_file`, span matching across lines) are deleted,
  not left beside the new parser. Answers.jsonl `reference` replies are rewritten in the fact-line format.
- Every reproducer from reviews 2-5 is a test (the round-5 report lists them: identifier-only `Job(name, payload, priority=0)`,
  heading-spanning creation bullet, `archive.7z` heading) and fails for the stated rule number.
- Main's probes still hold: references pass 24/24; every-line-shifted replies pass 0; a fact line citing a non-chore file fails.
- A table in the report maps each earlier family to the rule that closes it by construction, with the test name.
- Same modules and matrix as round 5 (both Pythons x both modes). No live model runs (S2 is Main's).
- Touches as before (tools/qual/, tests/test_qual_summarize.py, tests/calibration/summarize/, config/local.example.json
  prompts text and _prompts note, tests/calibration/experiments.md section).
