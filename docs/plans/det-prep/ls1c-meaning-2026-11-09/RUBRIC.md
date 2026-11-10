# Meaning grading rubric (LS1c, 2026-11-09)

You grade replies to a "summarize these files" chore by MEANING, not by wording. Each chore packet has the task, the
numbered files, the reference facts F1..Fn that a complete answer states, a list of claims that are NOT in the files,
and several anonymous replies R1..Rk. Each reply is a list of lines `- <claim> [path:line, ...]`.

For every reply, grade two things.

## 1. Recall: each reference fact Fi is `met` or `not_met`
`met` when some reply line (or two adjacent lines together) states the same information, in any wording, AND that line
cites at least one line of the file that actually shows it (any line that shows it counts, not only the reference's line).
- Any reasonable subject is fine ("fail requeues the job" meets "JobQueue requeues a failing job").
- Compound lines are fine: one line may meet several facts.
- Synonyms are fine ("moves it to the dead list" = "abandons it" = "gives up on it").
- Not met: the information is absent, vaguer in a way that loses the point (e.g. "manages a queue" does not meet "hands
  out the next ready one"), or the only line stating it cites lines that show nothing related.
- A fact about a difference or a use (diff/use chores) needs the reply to state the difference or the use, not just list
  both sides' values with the reader left to compare them. If both sides' values are each stated correctly and cited,
  side by side, count the difference as met only when the reply makes the comparison explicit or it is a value change
  (e.g. "workers 2" in v1 and "workers 4" in v2 both stated meets "v2 raises workers from 2 to 4").
- Line names in a deterministic name list (`X defines the function Y [path:N]`) count like any other line.

## 2. Precision: each reply line is `ok`, `wrong` or `unsupported`
- `wrong`: the claim is false according to the files (including "X is not in the files" when X is in a named file, or
  any claim matching the "not in the files" list), or it misstates a value or behaviour.
- `unsupported`: the claim may be true but none of its cited lines show it, or it cites a line that does not exist.
- `ok`: true and its citation shows it (or shows the thing it is about, e.g. the def line for "X is a function").
- Extra true facts beyond the reference are `ok`. Do not penalize verbosity.
- A line that is only a heading or prose with no citation: ignore it.

Be strict about truth and lenient about form. When unsure whether a fact is met, say `not_met` and explain in `note`.

## Output
Write one JSON file per chore at the path you are given, exactly this shape:
```json
{"chore": "<id>", "replies": {
  "R1": {"facts": {"F1": "met", "F2": "not_met", ...},
         "lines": [{"n": 1, "verdict": "ok"}, {"n": 4, "verdict": "wrong", "note": "says X; line 9 shows Y"}],
         "note": "<one sentence overall>"},
  ...}}
```
`lines` lists every cited reply line by its 1-based position in the reply (blank lines and uncited lines do not count),
with a `note` on every `wrong`/`unsupported` and on every `not_met` you judged borderline (put fact notes in the
reply `note`). Grade every reply in the packet. Read only the packet file; it contains everything you need.

## Amendment 1: absence claims
The model was told to cite line 1 of the file it checked when it says something is not in a file. No line can show an
absence, so grade an absence claim ("X is not in settings_v2.py", "no other file uses LRUCache") on its TRUTH alone:
`ok` when X really is absent from the file(s) it names, `wrong` when X is there. A true absence claim may meet a
reference fact (e.g. "state_file is not in settings_v2.py" meets "v2 drops state_file"), whatever line it cites.
