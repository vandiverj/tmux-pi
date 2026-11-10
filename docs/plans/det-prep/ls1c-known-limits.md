# LS1c code facts: known limits of the static use scan

This list follows the owner's [realistic-use bar](../realistic-use-bar.md). The scan (`use_scan` in `lib/summarize_structured.py`)
runs only on the owner's own repos and work code. A limit here is not fixed round by round. An entry that can give a wrong fact needs
an input that normal use does not produce. Every other entry only loses a line, which the bar allows.

"Lost line" means the scan says less than is true: the file is opaque, or the line is held back. "False credit" means a "uses /
calls / creates Q" line for a program that never reaches that line. "False absence" means "does not use Q" when the program does
use Q. "Round" names the review that found the limit and the Main brief that accepted it (`docs/plans/det-prep/fixes/ls1c-s1c-roundN.md`).

## Contrived inputs (a program that fails, or a contrived binding)

| Input shape | What the scan does | Round |
| --- | --- | --- |
| An attribute read past a member that lacks the attribute: `import pkg.mod` then `pkg.mod.v.w` (or `pkg.mod.v.w += 1`, `import pkg.mod as m; m.v.w`, `from pkg.mod import v; v.w`), where `v = 1`. The program raises `AttributeError` before the credited line. | False credit for a later line. Member chains are kept non-opaque on purpose, because real code reads `pkg.mod.CONST.attr`. | review-coder-df3aae6de1-072402 F1; accepted in brief round 21 |
| A subscript store or similar on a member's value that does not support it: `pkg.mod.v[0] = 1` with `v = 1` (TypeError before the line). Round 20 reads attack: 60 rows per interpreter. | False credit. | coder-df3aae6de1 round 20 attack; brief round 21 |
| A non-import binding read as a module: `hp = 3`, a bare `def hp`, or an empty `class hp`, then `hp.x.v` (AttributeError before the line). No import binds `hp`. Round 20 bindings attack: 1,536 rows per interpreter. | False credit. | coder-df3aae6de1 round 20 attack; Main decision 1 (round 20 evidence); brief round 21 |
| A module-level read of a name with no module-level binding: the name is only a parameter elsewhere, or its only module binding is under `if False:`. `hp.x.v` raises NameError before the line. Round 20 bindings attack, runs variant: 80 rows per interpreter. | False credit. | coder-df3aae6de1 round 20 attack; Main decision 1 (round 20 evidence); brief round 21 |
| A member whose single binding has a value that fails or is not what it looks like: `from h import x` where `h.py` has `x = nonexistent`, `x = unknown()` or `import os as x`. The member proof is existence, not value. | Keeps its lines. When `h` raises on import, the importer fails too, and that line is a false credit or a false absence on a program that fails. | review-coder-747cbc5626-183706 F2; Main decision in brief round 8 F2 |
| A same-named namespace directory beside a concrete module: `h.py` defines `x`, and `src/h/real.py` also exists. | Keeps the absence line for `from h import x`. This is right with the run root as `sys.path[0]`. A `sys.path` that puts `src/` first is the module-identity limit below. | review-coder-747cbc5626-183706 F3; brief round 8 F3 (no action) |

## Beyond static proof (stated once in the `_no_path_to` docstring)

| Input shape | What the scan does | Round |
| --- | --- | --- |
| A name that reaches the code from outside its source (a configuration file, the environment, the network), or that the source computes without writing any token of it (every character from `chr`). Since round 15, such a loader must also be on the allow-list, or the file is opaque, so this is mostly closed. | Possible false absence. The absence line is a statement about the source the scan was given. | review-coder-eba72607ca-193551 F1; briefs round 8b and round 9 |
| Module identity beyond the run root: another `sys.path` entry, a `.pth` file, or a run with no run root on disk (only supplied files are seen). | Possible false credit or false absence when the real interpreter resolves a name to another file. | review-coder-56e30e3898-205916 F1; brief round 11 |
| Import state set outside the run: site-packages, `.pth`, `sitecustomize` / `usercustomize`, `PYTHONPATH`, interpreter startup, or whatever ran in the process before the file was imported. | Possible false credit or false absence. The scan models a fresh interpreter under Python's default import state. | review-coder-3ed45a3a90-214404 F1-F2; brief round 12 |

## Conservative rules (lost lines only, no wrong fact)

| Input shape | What the scan does | Round |
| --- | --- | --- |
| Two imports that bind one name to different things: `import a.x as hp` and `from b import y as hp`; a member import and a module import (`from vals import hp` then `import hp.x`, which runs); two member imports. Two imports of the same module stay resolved. | Opaque file, lost lines. This is the intended cost of rule 1: about 3,500 sound credit lines per variant in the round 20 bindings attack (3,532 for two-module imports). The two reverse-order repro20 controls keep only the import line. | rule 1, brief round 20 (Main decision 4 of the round 20 evidence); brief round 21 |
| An imported module name that is also bound by anything else, in any scope, and then read: `import config` and `def load(config): ...`, or an assignment, `for`, `with`, `except ... as`, or `global` of it. | Opaque file, lost lines. This is the cross-scope `rebound` check. | review-coder-ee6d7b3348-054434 F3; accepted as is in brief round 20 |
| An import inside a class body that binds a module: `class C: import pkg.mod as m`. | Opaque file, lost lines, even when nothing reads `C.m`. | review-coder-ee6d7b3348-054434 F3; brief round 20 |
| An import inside a function, a class, a condition or a `try`, then a submodule read: `def f(): import pkg.sub; return pkg.sub.v`. `loaded_before` counts only unconditional top-level imports that come earlier. | Opaque file, lost lines. | review-coder-5633e87e87-045109 F2; recorded follow-up in brief round 19 |
| A submodule read as an attribute that neither the reading file nor `pkg/__init__.py` loads by an import: `import pkg; pkg.sub.f()`, where `pkg.sub` is loaded only by some other module that ran first. | Unknown, opaque file, lost lines. | rule 2, brief round 18 |
| An augmented assignment or `del` of a module attribute: `import pkg.cfg; pkg.cfg.n += 1`, or `del pkg.cfg.n`. The whole chain is resolved, and a written attribute is unknown (`_may_write`). A plain `pkg.cfg.n = 1` keeps its lines. | Opaque file, lost lines. | rule 2, brief round 20 (Main decision 2 of the round 20 evidence: `del a.b.c` reads `a.b`; resolving `.c` as well is the conservative reading); brief round 21 |
| A module value passed through anything but an import: `m = pkg.mod; m.Q()`, a container element, a return value, a default argument. | Unknown, opaque file, lost lines. | rule 3, brief round 19 |
| A file in an import cycle of supplied modules (any size, or a self-import). | Opaque file, lost lines. | briefs round 17 and round 18 rule 1 |
| A star import anywhere on the walk. `__all__` is not resolved. | Opaque file, lost lines. | brief round 14 |
| Any use of an outside module that is not on `_SAFE_CALLABLES` / `_SAFE_VALUES`. | Opaque file, lost lines. | brief round 15 |
| A run-root module that is imported but not supplied (an unseen, non-stdlib module). The guard went to holdout 9, tune 3, real 0; use-lrucache lost 2 lines, use-jobqueue 3 and use-utils 3. All of these come back when the files are supplied (`test_full_src_rows_keep_every_line`). | Opaque file, lost lines. | coder-2feae8a8fc stop; brief round 15b decision 1 |
| A file that changes import state (binds an `_STATE_ATTRS` name, mentions `sys.path`, `sys.modules`, `__path__` and the like, or imports `site`). A false word hit, in a comment or string, also counts. | Opaque file, lost lines. | brief round 12 |
| A name that the scan must prove bound once (`_single_binding`: a member import's name in its module, a target or a name read through) but that is bound twice (even by the same import repeated), or bound under an `if`, `try`, `with`, `for`, `while` or `match`. | No proof, so no line through it: lost lines. | rules of briefs round 1 and round 7b; recall note in review-coder-df3aae6de1-072402 (sweep, `_single_binding`) |
| The real-repo probe under the production policy. | 0 lines. The patched probe checks citations only and does not show coverage. | review-coder-61108a8b31-224635 F3; accepted in brief round 13 |
