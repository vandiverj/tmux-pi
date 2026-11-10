# LS1c S1c round 7b: a write into a name's object does not unbind the name (Main, 2026-11-09)

Round 7 (coder-747cbc5626) stopped as the brief asked: the member rule lost use-lrucache's `cli.py` line and three
use-utils lines. The cause is `from worker import HANDLERS`, where `worker.py` writes `HANDLERS[name] = fn`. The
round-7 report is in `.../tasks/coder-747cbc5626/report.md`.

## Main's decision
The loss is not a necessary cost of the rule. It comes from asking the wrong question. A member import needs to know
that the name **exists** at `h`'s module scope once `h` has run. `HANDLERS[k] = v` and `HANDLERS.attr = v` change the
object the name holds. They cannot unbind or rebind the name. `version_diff` and the attribution scan ask a different
question, about the **value**, and for them these writes must keep counting.

## Build
1. Give the existing `_single_binding` (and `_binds` under it) one keyword, for example `existence=False`. Do not write a
   second predicate.
   - With `existence=True`, a `write` event is not counted when the name is the **root** of the target (`name[k] = v`,
     `name.x = v`, `name[k] += v`) and the context is Store.
   - Still counted under `existence=True`: a Del of any form rooted at the name (`del name[k]` is harmless, but keep the
     rule simple: Del counts), a write where the name is the **attribute** (`x.name = v`, since `x` may be the module),
     and every other event kind.
   - `_binding_index` must record which role produced a `write` (root or attribute) and its context. Name the change in
     its docstring.
2. Use `existence=True` only in the member-proof path (`_single_names`, and through it round 7's member rule). Every
   other caller keeps the default.
3. Keep everything else from round 7, including the cross-file attribute write/delete condition.

## Tests
- In the import-graph table: `h.py` with `X = {}` then `X["a"] = 1` and `u.py` with `from h import X` keeps the absence
  line. `X = {}` then `del X["a"]` blocks. `X = {}` with another file doing `h.X = 2` blocks (round 7's condition).
- A `version_diff` row: `X = 1` then `X.y = 2` (or a subscript write) is still not a proven single binding there.
- Fail-before against 0f530c6 and against your round-7 working tree (save a `git diff` of round 7 under `evidence/`
  first). Name the script and log by absolute path.

## Report
Guard per-split counts and the probe count against 0f530c6 and against round 7. State whether use-lrucache's `cli.py`
line and the three use-utils lines are back. If any is still lost, list why; do not loosen anything beyond item 1.

## Done means
Same as round 7: the seven modules, two interpreters, outside and inside tmux, after your last edit, with each file's
time; self-review, sweep table, SLOW_TESTS; commit nothing; `RESULT_STATUS` as the last line; no lingering background
shells. No live model, nothing sent to 127.0.0.1:1234. The round-7 safety rules apply unchanged.
