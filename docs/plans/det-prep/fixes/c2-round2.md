# C2 round 2: headings and fences by the CommonMark rules (route: rule-first)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-86b4ed5fe2-170237/result.md and report.md beside it.

The three findings come from hand-rolled heading and fence parsing. Replace it with one small parser that follows the CommonMark rules for the two block types that matter, and use it for both matching and section boundaries.

## Rules
- **Fenced code (CommonMark 4.5):**
  - A fence opens with a run of at least 3 backticks or at least 3 tildes, indented 0–3 spaces.
  - It closes only on a line with a run of the same character, at least as long as the opening run, indented 0–3 spaces and followed only by spaces.
  - An unclosed fence runs to the end of the file.
- **ATX headings (CommonMark 4.2):**
  - A heading is 0–3 spaces of indent, then 1–6 `#`, then a space or end of line.
  - An optional closing sequence of `#`s is dropped only when a space comes before it.
  - The text is trimmed. So `## C#` has the text `C#`, and `## C ##` has the text `C`.
  - Setext headings may be ignored; say so in the docs.
- **Spec matching:**
  - Leading `#`s and spaces in the spec are ignored, as now.
  - Trailing characters are kept.
  - The match is a case-insensitive comparison with the parsed heading text.
  - An empty heading (`file.md#` or `file.md# `) is refused with the file name, never treated as the whole file.

## Tests (one table)
- fences of 3, 4 and 5 backticks and of tildes, each holding a shorter inner fence and a heading;
- an unclosed fence;
- an indented (4 spaces) fence-like line;
- `## C` next to `## C#`, and `## C ##`;
- `#hashtag` with no space (not a heading);
- an empty spec.

Each row asserts the selected line range or the refusal text. If `markdown_it` is importable, add a parity test comparing heading lines and their levels on the table's fixtures, and skip it otherwise.

## Report
Include a Site / Disposition / Evidence sweep table, and the table's failure count against the round-1 code.
