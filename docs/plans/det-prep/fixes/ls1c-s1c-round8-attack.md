# LS1c S1c round 8 addendum: attack your own rules before you report (Main, 2026-11-09)

Seven reviews have named the same family (binding-ambiguity). Each time the fix covered the shape the reviewer showed, and
the next reviewer found a new input shape that defeats the same rule. The sweep in your packet lists code sites. Here the
gap is in the **inputs**, and the inputs are a closed set: Python's own grammar.

## Required before you report
1. **The grammar is the case list.** Build `evidence/attack.py`, a table-driven script. For each rule you own, it must
   cover every way Python can bind, rebind, alias, mutate or delete a name or an attribute.
   - The rules: member proof (`_single_names`), module-attribute attribution (round 8 F1), direct-name attribution, and the
     absence path.
   - Derive the forms from the `ast` node types, not from memory. List them by walking `ast` for every node with a `Store`
     or `Del` context, plus `Import`, `ImportFrom`, `Global`, `Nonlocal`, `FunctionDef`, `AsyncFunctionDef`, `ClassDef`,
     `NamedExpr`, `ExceptHandler`, `MatchAs`, `MatchStar` and `MatchMapping`. Print that list from the running interpreter
     into the log, so the list is proven complete for this Python.
   - Cover each form at module scope, in a function, in a class body, under if/try/with/for, and through an alias
     (`a = t`, `a = t.Q`, `from x import t as a`).
   - Cover the dynamic forms (`setattr`, `globals()`, `sys.modules`, `importlib`) as already-blocked rows.
2. **Each row states its expected outcome and the reason** (line kept, or no line), then runs the real code and compares.
   Where it is cheap, confirm the expected outcome by executing the snippet.
3. **Any row where the code emits a line it cannot prove is a defect of this round.** Fix it by the rule, not by adding
   the row's shape as a special case. Then rerun the whole table.
4. Report the table's counts (forms × positions, kept, blocked, mismatches before and after your fix), and give
   `evidence/attack.py` and `evidence/attack.log` by absolute path. Rows that should become regression tests go into the
   existing import-graph table, so the coverage stays in the suite. The attack script itself stays under `evidence/`.

Recall still matters: a row where the code drops a line that is provably true is reported as a recall cost. It is not
fixed by loosening a rule. The round 8 brief's stop condition on use-lrucache's lines still holds.
