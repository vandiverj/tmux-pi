# HL1: eval pi-hashline-readmap for the Pi coder, and what carries over to Claude Code

Owner, 2026-11-08: "add another eval run for https://github.com/coctostan/pi-hashline-readmap, this is specifically for pi coder but also evaluate if some of or all of the ideas could be applied to claude code and what the benefits of both are if any". Loop idea H18. Status: **queued**. The offline phases (0–1) can run before J1; the in-task A/B (phase 2) needs the J1 bench.

## What it is (from the README, 2026-11-08; verify against pinned source in phase 0)
- MIT license, about 443 commits. It needs Node 22.19 or newer and is validated against **Pi 1.5.0**. **We run Pi 1.1.0** (`pi` on PATH), and the pinned `TP_PI_NODE` path in `bin/tp-pi` does not currently exist, which is why we run with `TP_PI_NODE=`.
- It **replaces** Pi's `read`, `edit`, `grep`, `write`, `ls`, `find` and `bash`, and **adds** `ast_search` (an ast-grep wrapper) plus an optional, deprecated `nu` shell. `codemode` is opt-in.
  - `read` returns `LINE:HASH|text` anchors. It can also return a structural `map`, jump to a `symbol`, take a `bundle:"local"` symbol bundle, and page by characters.
  - `edit` takes anchor-addressed operations: `set_line`, `replace_lines`, `insert_after`, `copy_lines`, `move_lines`, and `replace` as an exact-text fallback. A stale anchor is refused (mismatch) rather than editing the wrong line. Edits are atomic, with overlap rejection, syntax checks and a structured diff.
  - `grep` and `write` return anchors, so an edit can follow without a re-read. `bash` strips ANSI codes and adds tool hints.
- The README cites no benchmarks and no token figures. It links a "Benchmarking edits" doc that phase 0 reads.

## Why it might help us
- **Qwen edits.** Today Pi's edit needs the model to reproduce the old text exactly. Weak models misquote it, and that causes retries or edits to the wrong place. Anchors shrink the edit to short line references, which should mean fewer failed edits and fewer output tokens.
- **Navigation.** The `map` and `symbol` reads can replace whole-file reads, which should cut input tokens. This overlaps with our ctx tools (`TP_CTX_TOOLS`, `ctx_symbol`).
- **`ast_search`** is ast-grep inside the worker, which overlaps with H13 (family sweeps).
- It fits X1's rung 2 (simpler tools, schemas and outputs). An HL1 result can become an X1 arm-B candidate.

## Risks to check before any trial (phase 0, blocking)
1. **Guard parity.** Our `sensitive.ts`, `safe.ts`, `kill-guard.ts` and the reporter key on tool names and inputs, for example `toolName === "grep"`, and `bash` for kill-guard. Overriding `read`/`grep`/`bash` with new arguments (`symbol`, `map`, `bundle`) could slip past the sensitive-read and redact checks. A `bundle` can read other files. `ast_search` takes a path, and `nu` is a second shell that bypasses bash guards. **`nu` and codemode stay disabled.** Any tool our guards don't cover is either added to the guards in the experiment branch or left disabled.
2. **Pi version.** Its compat suite runs against our Pi 1.1.0. If it fails, upgrading Pi is an owner decision and is not part of this eval.
3. **Supply chain.** Pin one commit, read the source for network calls, telemetry and install scripts, and run `npm ci --ignore-scripts` in an isolated dir (`~/.local/hl1-eval/`). Load it only through `--extension <path>` in an experiment launcher gated by `TP_HL1=1`. Never use `pi install`, the global `~/.pi` settings, or anything on the work machine.
4. **Ordering.** Its `bash` hints and RTK (H14) both rewrite shell use, so they are never combined in one arm.

## Eval phases
- **Phase 0 (no model runs).** Pin, review the source, install isolated, run the compat suite on Pi 1.1.0, and add guard-parity tests to the experiment branch. The guard-parity tests use decoy-secret fixtures, which are deliberately sensitive files and secret-looking strings, and must confirm all of these:
  - a sensitive read through `read`/`map`/`symbol`/`bundle`/`grep`/`ast_search` is blocked or redacted exactly as stock;
  - a kill through `bash` is guarded;
  - the reporter still records every tool call.
  - Decision: go, go with tools disabled, or stop.
- **Phase 1 (edit micro-benchmark, cheap).**
  - **Baseline from history:** mine the Qwen/Pi `transcript.jsonl` files we already have for edit outcomes: failure rate, error kinds and retries per edit.
  - **Bench:** 24 sealed edit tasks on battle-soccer files at `bench-base-v1`: single-line, multi-hunk, a repeated-text ambiguity, a whitespace-sensitive edit, a file changed between read and edit (stale), a large file, and a move/copy. The expected final file is sealed.
  - **Arms on S1 (Qwen via Pi), 3 repeats each:** P0 stock Pi tools; P1 hashline `read`+`edit` only; P2 full hashline (`map`/`symbol`/`grep`/`ast_search`). Then the better of P1/P2 against P0 on Haiku via Pi, 1 repeat.
  - **Metrics:**
    - first-try edit success;
    - **silent wrong edits**: the edit applied but the final file differs from the sealed one. This is the most important metric, and any rise is disqualifying.
    - stale-edit refusals and how the model recovers from them;
    - output and input tokens per completed edit;
    - tool calls and wall time.
- **Phase 2 (in-task A/B, needs J1).** J1 `code` tasks on S1, P0 vs the phase-1 winner, at least 3 repeats. J1 metrics plus edit-failure, re-read and whole-file-read rates. Review findings must not rise.
- **Phase 3 (Claude Code portability: analysis first, prototype only where there is a gap).**

  | Idea | Claude Code today | Gap and portable form |
  |---|---|---|
  | Stale-edit refusal | Edit requires a prior Read and refuses if the file changed since the read; `old_string` must match uniquely | Likely little gap; confirm from Claude transcripts how often edits fail and why |
  | Anchor-addressed edits (short output) | Edit reproduces `old_string` (output tokens; uniqueness failures) | Possible gain for Haiku. Portable only as an MCP tool, `hl_edit`, plus `--disallowedTools Edit`; our PreToolUse hooks match on tool name, so they need parity |
  | `map`/`symbol` reads | Read by line offset/limit; no structural read | Portable as an MCP tool, or via existing ctx tools; measure whole-file reads in Claude traces first |
  | `ast_search` | No built-in | Same as H13 (ast-grep), shared |
  | ANSI strip / bash hints | Output is already plain; there are no hints | Small; overlaps RTK |

  - Measure the gap from existing Claude transcripts first: edit failures per edit, whole-file reads and re-reads.
  - Prototype only an idea whose gap is at least 5% of input or output tokens or causes at least 1 edit failure in 20 edits. Prototypes are a shared lib plus a thin MCP adapter, which follows the X1 rule of keeping shared logic in a lib with thin per-runtime adapters.
  - A/B on Haiku and Sonnet with the phase-1 edit set: 2 repeats on Haiku and 1 on Sonnet.

## Decision rules
- **Pi, adopt as an experiment-gated option, then owner review:**
  - phase 0 is go;
  - phase 1 shows no rise in silent wrong edits, and first-try edit success rises by at least 10 pp or edit output tokens drop by at least 25%;
  - phase 2 shows correctness not worse than control spread (PROTOCOL keep rule) and tokens per accepted task not higher.
- **Claude:** per idea, the same two gates on Haiku. Sonnet is reported but not required to improve, since the payoff is expected for weaker models.
- **Report:** each idea × runtime × model, with benefit, cost, risk and verdict: reject / revise / promising-inconclusive / ready for review. Report the one-time cost (review, integration, guard parity) separately.
- **Stop before adoption** and present the evidence, the isolated diff and rollback to the owner.

## Frontier budget
At most 30 frontier runs across phases 1–3, for Haiku and Sonnet. Qwen runs are uncapped but logged.
