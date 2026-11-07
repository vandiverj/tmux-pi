# <CASE-ID> design research (report only)

**Rules:** read only; write only your report with the Write tool. No edits, commits, posts or dispatches. No
`.env*`, databases or MCP database tools. No heredocs or inline scripts. Outside the repo, `safe_grep` and
`safe_read`.

**Case:** <C>/cases/<CASE-ID>.md (snapshot of the `<tracker>` case and comments).
**Read:** the documents in `<ctx>/prompt/01-sources-of-truth.md` that apply (at least the roadmap, the rules model,
the schema docs and `regulatory-requirements.context.md`), the decision logs, and the prototype screens
<screens> in `<ctx>/<program>-prototype/src/screens/`.
**Code:** develop at `<sha>` (or the parent branch `<branch>` at `<sha>`), in <repo> read with `git -C <repo> show`
and `git -C <repo> grep -n ... -- apps lib docs`.

## Answer
1. **AC status:** for each criterion, built / partly built / missing, with file:line.
2. **Hook points:** where the change goes (routes, domain functions, tables, components), with file:line.
3. **Mechanism and record shape:** tables, columns, types, transactions, audit events. Note `S1D1`, `S1D2` and
   `S1D7` (see `case-work-prompt.md`).
4. **Regulation and prototype per AC:** the regulation section and what it requires; the prototype screen and state;
   any disagreement between roadmap, prototype and case.
5. **Existing tests:** for each area, the test files and tables a new test should extend.
6. **Shared surfaces and other-product impact:** shared code the change would touch, and its callers.
7. **Dependencies:** other cases or branches this needs, and the order.
8. **Lane plan:** 3–5 steps, each owning one rule, with done-means, files, the existing tests to extend and what is
   out of scope. For each step that changes a state: the rule in one sentence and its case table (starting state ×
   trigger → result, including the opposite action, a repeat, a later contradicting event and an unusual person).
   For each email, outside call or background job: the guarantee level (best effort and bounded, or guaranteed).
9. **Open questions:** each with options, trade-offs and your recommendation.
10. **Risk:** Low, Medium, High or Critical, and why.

Write <C>/research/<case-id>-design.md. End your reply with `RESULT_STATUS: complete`.
