# D1 round 7 (final before park): scope derived from the attached diffs (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-c905fc8013-173607/result.md and report.md beside it.

## Rules
1. **The scope's path list and commands come from the same computation that built the attached diffs.** The listed paths are exactly the union of paths in the main diff and in every held section. `scope_lines` and `render_scope` take that list as input and never recompute it against HEAD or anything else.
   - Remove the `review_base` patch in `tests/test_review_rounds.py:547-549` that masked this.
   - The later-round test must use the real base resolution.
2. **The commands work in every repository state.** When HEAD doesn't resolve (no commits yet), every command that names HEAD uses the empty tree instead (`git hash-object -t tree /dev/null`, sha1 or sha256 per the object format). The four-state rule treats HEAD as empty there.

## Tests
- **Extend the generated table:** every row also asserts that the scope's path list equals the set of paths in the attached diff sections.
- **New later-round rows:** `previous=two, HEAD=one, files=one`, and a run of the table in a repository with no commits.
- **Every scope command the packet prints is executed** in the fixture repository and must exit 0.

## Report
The exact commands and log paths per module, and individual sweep rows (no aggregate "other sites" row).

If this round is rejected, Main parks D1 for the owner.
