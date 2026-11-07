# C2: heading-anchored context specs

Lane `lane-facts`, step 2. Level: `mid` coder. Touches: `bin/tp-spawn` (`read_context`), the test module that covers
`--context PATH:START-END` today (find it with `grep -rln "START-END\|:1-" tests/`; name it), `docs/` page that documents
`--context` (README section or docs/agent-tools.md: this step may edit the one paragraph that documents context specs).

## Why
39 coder tasks re-read the whole engineering-context prototype (1.0M tokens) because Main can only point at line ranges, which
drift. A heading is stable; the resolved range is printed so the worker knows exactly what it got.

## Change
1. Accept `PATH#Heading text` (the text after `#`, case-insensitive, leading `#`s and whitespace of the markdown heading
   ignored). Resolve to the line range from that heading to the line before the next heading of the same or higher level.
   Label the context as `path (section "Heading text", lines a-b)`. Refuse with a clear message when the heading is missing or
   matches more than once (list the matches); the dispatch fails like a bad line range does today. The existing cap applies
   (an over-cap section is refused with the largest range that fits, the same as now).
2. `tp-agent context --task` output is unchanged (suggestions stay line ranges).

## Done means
1. `--context docs/x.md#Rules` yields exactly the section's lines, labelled with the resolved range.
2. A missing heading and an ambiguous heading each refuse with the message naming the file and, for ambiguity, the matching headings.
3. Headings nested under the target are included; the next same-or-higher heading ends the range.
4. A file name containing `#` still works when given as a line-range spec (no regression; test it).
5. Tests for 1 to 4 fail without the change.
6. The context-spec paragraph in the docs shows the new form.
