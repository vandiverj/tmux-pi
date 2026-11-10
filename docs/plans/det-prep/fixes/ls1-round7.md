# LS1 round 7 — subjects, order on assigned lines, list items, and a generated attack test (Main, 2026-11-08)

Review 6 (review-coder-d50afe8142-204331) is REQUEST_CHANGES; reproducer: that review's `reproduce.py`. The fact-line format
held (Main's 17 mutation kinds: 0 passes beyond 3 benign). The four defects are holes in it, not new prose shapes:

| Finding | Cause |
|---|---|
| F1 subject swap passes (`- sleep_job returns ...` citing echo's line) | names are stripped before the behaviour regex, and nothing ties the claim to the fact's subject |
| F2 an intro line listing every state hides a reversed order | order uses the first matching line of each fact, so one line gives every fact the same position |
| F3 ``` ``returns`` ``` supplies a behaviour word | BACKTICKS removes single-backtick spans only |
| F4 a bare `- ` / `-\t` bullet is ignored | the line is rstripped before it is classified, so `- ` becomes `-` |

## Rules added or tightened (numbering continues the round-6 rules)
6. Subject. Each behaviour fact has a subject: `behaviour[k].name` in answers.jsonl (a symbol or a file name). The claim's
   subject is the FIRST token in the claim (backticks removed for this step) that is a name in the cited files' symbol table
   or one of the chore's file names (full or bare). A behaviour fact is met only when that token equals the fact's subject.
   A claim with no such token has no subject and meets no behaviour fact. The prompt tells the model to start each fact line
   with the name it is about, with the one example already there. Failures name rule 6, kind `wrong_subject` or `no_subject`.
7. Order. Order is checked on the lines the matching ASSIGNED (the bipartite owner), never on first matches. In a chore with
   `order`, a fact line whose claim matches two or more of the ordered facts fails (rule 1, kind `several_in_one_line`).
8. Code spans. Every code span is removed before rule 5, per CommonMark: a run of N backticks up to the next run of exactly N
   backticks. A backtick with no closing run is removed as a character. Test N = 1, 2, 3, unmatched, and spans with spaces.
9. List items. Classification reads the raw line before any stripping. Every list item is graded: a line whose first
   non-space characters are `-`, `*`, `+` or `<digits>.` / `<digits>)` followed by whitespace or the end of the line. A list
   item that is not a valid fact line (an indented bullet, `*` bullet, numbered item, bare `-`, `- `, `-\t`, a `- ` line with no
   citation list) fails the reply (rule 2, kind `uncited` or `not_a_fact_line`). Non-list lines stay ignored.

## Done means
- Every reproducer in review 6's `reproduce.py` is a test and fails for the stated rule and kind.
- A GENERATED attack test, not a list of examples: for each of the 24 chores it builds every mutation below from the
  reference and asserts each fails (and names the rule it fails). It reports counts per kind. Mutations:
  a. subject swap: for every pair of behaviour facts with different subjects, swap the subject tokens of their lines;
  b. intro line: prepend a line naming every fact (all claims joined, all citations), for order chores with the rest reversed;
  c. code spans: each behaviour word of each behaviour line wrapped in 1, 2 and 3 backticks, and an unmatched backtick;
  d. list items: append each of `-`, `- `, `-\t`, `- \t`, `  - x [<a valid citation>]`, `* x [...]`, `1. x [...]`;
  e. Main's round-6 kinds (shift -3..+3, citation on the next line, heading file, basename, generic claim, one-line, drop a
     line, wide range, extra non-chore cite, `*` bullets).
  References: 24/24 pass, with every reference line starting with its subject where it is a behaviour fact.
- A check that the generated test can fail: one mutation kind run against round 6's checker (git show of the lane's c249ce3)
  shows it passing there (fail-before), recorded in the report.
- The family table from round 6 gets rows for rules 6-9 with the test names.
- Same modules and matrix as round 6 (both Pythons x both modes). No live model runs. Touches as round 6.
- Report the subject-binding cost: list every reference line you had to reword for rule 6, and any legitimate phrasing
  rule 6 now refuses (for example `The handler echo returns ...`), so Main can judge the prompt.
