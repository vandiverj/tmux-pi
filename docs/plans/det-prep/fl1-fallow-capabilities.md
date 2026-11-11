# FL1: what else fallow can do for review (loop idea H31)

Owner, 2026-11-10: "I feel there may be a lot of tool use out of fallow we should test", and tools may be more useful in
combination or as a check on another tool than alone. Advisory only. Status: **queued**. fallow 3.10.0 is already in the npx
cache (`~/.npm/_npx/e6d07818f0a04ee4`); nothing to install. Run with `FALLOW_TELEMETRY=off DO_NOT_TRACK=1`, on a scratch clone
(it can write `.fallow/` into the tree it scans).

## Main's first look (scratch clone of main 1c689ec, 2026-11-10)
- `fallow health --hotspots --format json`: 23 TS/JS files ranked by churn x complexity in 0.35 s. Top: `register.tsx` 65.2,
  `dispatcher.ts` 59.5, `runtime.js` 54.6. `subagents.ts` (43.4) and `subagents.mjs` (36.6) are both hotspots, and they are
  the duplicated pair that dc59477 had to change together. No co-change pairs (see H29).
- `fallow health` without a config: 86 files, 251 functions above threshold, but "dead files 89.5%" and "duplication 38.2%"
  are wrong for tmux-pi. Extensions and hooks are loaded at runtime (no `package.json` entry points), and the generated
  `claude/plugins/tmux-pi/hooks/index-text-cases.ts` dominates the duplicate list.
- `fallow review --brief --base dc59477^` at dc59477: 246 lines in 0.6 s. It called `subagents.ts`, `subagents.mjs` and
  `register.tsx` unused files, and 10 of the 28 clone groups were the generated file. In its default mode it did not link
  `subagents.ts` with `subagents.mjs`; the code-quality eval found that link only with semantic dupes.
- fallow sees TS/JS only. Most det-prep defect families (F1, SG1, D1) are Python in `lib/` and `bin/`.

## Step 0: one config, written once
A fallow config in the scratch clone only (never committed without the owner): entry points for `agent/extensions/*.ts`,
`claude/plugins/tmux-pi/hooks/*`, `claude/mcp/*.mjs`, `templates/product-design/runtime/runtime.js` and `tests/*.mjs`; ignore
the generated file and `tests/fixtures/**`. `fallow recommend` and `fallow list` (entry points, plugins) help write it. Record
the before/after noise counts; every later step uses this config.

## Capabilities to measure (each on the code-quality eval's cases plus main)
1. **`review --brief --base`** as the scanner report attached with `tp-review --evidence`: findings touching the change,
   noise, and length against the code-quality eval's `scan.sh` report.
2. **`health --hotspots`** (and `--ownership` off): does marking hotspots help a reviewer choose files (shared with H29 arm C)?
3. **`health --targets`** (refactor targets from complexity, coupling, churn, dead code): are the suggestions specific (names
   shared ownership or a reason to change, plus a concrete simplification), or generic?
4. **`dupes`** semantic vs default, and `inspect <file>:<symbol>`: does it find cross-language siblings (`.ts`/`.mjs`)?
5. **`dead-code --trace <file>:<export>`**: does the trace explain a Knip-style "unused" finding well enough to reject it?
6. **`audit --gate new-only`**: findings the change introduced vs inherited; does that attribution cut reviewer noise?
7. Not in scope unless the owner asks: `security` (opt-in, overlaps SG1), `fix` (edits code), `hooks`/`setup-hooks` (installs
   hooks), `coverage`/`license` (paid or runtime data), `viz`.

## Combinations (pre-registered; scored offline from saved outputs before any reviewer run)
- **Dupes + hotspots -> fast-check (H30):** a duplicate pair where both sides are hotspots is the first consolidation
  candidate; fast-check checks the merge.
- **fallow dupes + jscpd agreement:** both = exact copy; fallow only = renamed copy. Is "both" more precise than either alone?
- **fallow dead-code + Knip (configured) agreement:** is "both agree" precise enough to show a reviewer when either alone is
  too noisy?
- **Co-change (H29) list + fallow `inspect`/semantic dupes:** does fallow confirm which co-changed files share the rule?

A combination becomes a reviewer arm only if, offline, it beats the better single tool on the labeled cases. The cases are few,
so an offline result is a hint, not evidence.

## Decision
Per capability: fit in (default scanner report), keep on demand (named trigger), or defer. Same scoring sheet and conditions as
the code-quality eval (current / + same-cause instruction / + scanner), and nothing changes the default workflow without the
owner.
