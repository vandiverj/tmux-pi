# LS1 round 5 (design change): evidence is a set of lines, behaviour is checked without the citations

Same lane `local-summarizer`, step S1, same touches. Review: tasks/review-coder-636cd4b784-185851 (F1-F4).
Rounds 2-4 patched regexes one case at a time and each review found the next case (families
required-fact-completeness, citation-file-validation, citation-support-coverage). This round changes the rule.

## Rule changes
1. **Support is a set of lines, not a regex.** Replace each fact's `support` regexes with `support_lines`:
   `{"src/x.py": ["14", "20-22"]}`, the fixture lines that are evidence of that fact. For a fact about what code
   *does*, the set is the lines that do it (body, return, raise, docstring that states it), and includes the
   signature only if the signature alone shows the behaviour. For a fact about a name existing, the definition line.
   A fact is supported when any citation on its sentence or bullet overlaps its set (ranges still at most 5 lines).
   Each entry also records an `anchor`: a short exact substring of each accepted line, and a test asserts every
   anchor is on its line, so a fixture edit cannot silently move the evidence. Closes F3 (body/docstring citations
   are accepted) and keeps round 4's wrong-line protection.
2. **Behaviour words are matched with the citations removed.** Before matching a fact's `required` regexes, remove
   every citation (the parenthesised `file:line ...` group and backtick quotes) from the reply text. A name plus a
   quoted signature then cannot satisfy "what it does". Split each exports fact into its name and its behaviour where
   the chore asks for behaviour, and audit every behaviour regex so it cannot be satisfied by the function's own
   identifier or parameter names alone (test below). Closes F1.
3. **Any filename-like heading sets context, whatever the extension.** A heading or bullet token that looks like a
   file (`name.ext` for any ext, or a path with `/`) and is not one of the chore's `cite_files` makes the citations
   under it `bad: unknown_file`; it never falls back to an earlier heading's file. Closes F2.
4. **F4:** restore the three-column separator at experiments.md:195.

## Tests (generated where the rule is general)
- The reviewer's three reproducers verbatim: chunked signature-only sentence fails (`missing`); the
  `### nonexistent.xyz` block fails (`bad_citations: unknown_file`); echo cited at worker.py:22 `return payload` and
  get cited at its docstring cache.py:14 pass.
- For every behaviour fact of every chore: replacing that fact's sentence with `NAME (file:SIGNATURE_LINE \`signature\`)`
  fails; citing each line in its `support_lines` passes; citing the nearest line outside the set fails.
- For every chore: unknown filename headings with 5 different extensions (`.xyz`, `.txt`, `.md`, `.json`, none-with-/)
  above a valid line fail.
- Keep: every reference passes quoted and unquoted; round 4's 144 shifted-line replies all fail; every-fact-needed.
- Re-score the saved smoke reply and report it.

## Verification and claims
Same 4 modules as Main ran (test_qual_summarize, test_router_catalog, test_route_resolve, test_router), both
interpreters, both modes. Do not run the live model or tp-route. Claims in the result cover only this round: drop
historical claims about earlier rounds (Main does not need them; the reviewer owed them as unaudited).
