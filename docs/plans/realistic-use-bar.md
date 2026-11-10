# Realistic-use bar for the summarizer and product-design lanes (owner decision, 2026-11-10)

The owner: the summarizer is not planned for use by outside systems. Both the summarizer (LS1c, `lib/summarize_structured.py`) and
the product-design tools (`tp-product`, `lib/product_design/`, the review page) run only on the owner's own repos, work code and
packages, written and edited normally. Owner: "yes, switch both lanes to the realistic use bar". This applies from S1c round 21 and
PD-C round 7 onward.

## What blocks a merge
A defect blocks only when it can happen in normal use:
- code that people write and run normally (it runs without failing before the line in question, uses ordinary imports and names);
- packages, manifests and saved states that the tools themselves wrote, or that a person changed through the documented commands;
- the normal flows: build, status, review page, export, import, recovery after the browser refuses storage.

In those cases the earlier quality bar stands: zero wrong facts, no lost data, no false success or "nothing written" messages, every
earlier test and the guard, probe, full-file and real-repo checks unchanged.

## What does not block
A finding that needs a hand-made input that normal use does not produce is a **known limit**, not a defect, for example:
- programs that always fail before the credited line (reading an attribute an int or a member does not have, an unbound name);
- names bound in contrived combinations (`with` plus a three-level relative import of the same name, imports of two different modules
  under one name);
- manifests edited by hand into case-variant or ancestor/child file names, duplicated JSON members, numbers with thousands of digits;
- byte-exact size boundaries, when the result is a refusal and nothing is lost or misreported.

Known limits are listed in the lane's docs (one line each, with the input shape and what the tool does) and are not fixed round by
round. If a fix is small, local and loses nothing on real code, it may be made; it is never required.

## How workers and reviewers apply it
- Workers: attack tables cover realistic shapes (patterns that occur in real repos and normal flows) plus the reviewer's repro.
  No exhaustive cross-products of contrived binding kinds, Unicode normalization forms or byte offsets.
- Reviewers: mark each finding `realistic` or `contrived`, with the reason. `REQUEST_CHANGES` only for a realistic defect or a
  wrong statement in the report. A contrived finding goes to the known-limits list as a note.
- Main judges each finding against this bar before writing the next brief.
