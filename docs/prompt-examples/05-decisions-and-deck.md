# Decisions and the decision deck

## The decision log

One file per batch: `<C>/briefs/<batch>-decisions-<date>.md`. It wins over older decision files for anything it
covers. Labels are `S<sprint>D<n>` (second campaign: S2D1, S2D2…). Numbering restarts at D1 in each campaign, so take
the next one from that campaign's log (the last used, plus one). A first campaign's log may keep bare `D<n>` ids; it
is history.

**Decision ids are local to `<owner>`.** They live in the decision log, the deck and Main's own notes, so `<owner>`
can find a call again. Outward text (`<tracker>` comments, case descriptions, MR descriptions and replies, handoff
drafts) says that we decided and what, never the id: a reader outside this campaign cannot resolve it, and another
campaign reuses the same number.

Each decision is one top-level bullet, so the deck can pull it verbatim:

```markdown
- **S1D1. <one-line title stating the call>**
  - Context: <what raised it; quote <owner> or the case where relevant>.
  - Facts: <what the code, prototype and case show, with file:line>.
  - Decision: a. <first call>; b. <second call> …
  - Alternatives rejected: <each with its trade-off>.
  - Regulation: <REG> §<section> and what it says, or "none: <why it is not a regulatory question>".
  - Prototype: <screen and state>, or "not shown".
  - Status: provisional (Main's call), awaiting <owner>'s sign-off.
```

When `<owner>` answers, add a section `## <owner>'s sign-off on S1D1–S1Dn (<date>)` quoting them per item: approved,
CHANGED → their text, or OTHER → their text. A changed decision reopens the work it affected; queue it.

Also record each decision in tmux-pi with `tp-agent work note add`, so a fresh session finds it.

## What needs a decision

Listed in `02-case-lifecycle.md` step 2. In short: sources disagree; scope beyond the prototype and case; a
regulatory interpretation; a schema or shared-model change; a shared-code change; anything hard to reverse. Also a
reviewer finding that Main overrules: record why.

## The deck

A self-contained HTML page `<owner>` reads to approve or change each call. Each slide shows:
- the call to approve, quoted verbatim from the decision log;
- what raised it and why this call;
- the alternatives, each with its trade-off;
- the regulation that applies, or why none does;
- the prototype screens it relates to;
- other factors (dependencies, effort, what it reopens);
- screenshots: the app as built, the prototype, or both.

They pick Approve, an alternative, or Other with text for each item. The page saves their answers in their browser
and gives a summary to paste back.

### Building it

1. Copy `deck/` from this folder to `<C>/research/decision-deck-<round>/src/`.
2. At the top of `build.mjs`, set `DECISIONS_MD` (the batch decision log, relative to the campaign folder),
   `DECK_TITLE`, `FIRST_ID` (the n of the batch's first decision, e.g. 1 for S1D1; `build.mjs` reads both `S<N>D<n>` and bare `D<n>` ids) and `DECK_KEY` (a new key per deck, so saved answers don't mix). Leave `template.html` alone.
3. Add one entry per decision to `data.mjs` (shape below) and one entry per screenshot to `SHOTS`.
4. `node build.mjs` in `src/` writes `../index.html`. It fails if a decision has no block in the log, if a logged
   decision from `FIRST_ID` up has no entry in `data.mjs`, or if a `reg` entry lacks `cite`, `quote` or `src`.
5. Publish it as a private page and give `<owner>` the link. Republish to the same link as decisions are added.

```js
{ id: 'S1D1', title: '…', cases: ['<CASE-ID>', '…'],
  what: 'What raised the decision.', reason: 'Why this call.',
  items: [{ key: 'S1D1a', label: 'The call', alts: [A('Alternative', 'Its trade-off')] }],
  reg: [{ cite: '<REG> §<section>', quote: '…', src: 'official regulation text, read <date>' }],  // or reg: [], regNone: 'none: …'
  proto: ['Screen: …'], factors: ['…'],
  shots: ['shotId'] }                       // or shots: [], noShot: 'why no screenshot'
```

### Screenshots

- App screenshots come from the browser-check harness on throwaway databases, or a component render with fake
  people. Prototype screenshots come from `npm run screenshots` in the prototype, or a crop of a walkthrough.
- Synthetic data only. Crop to at most 1200px wide. Store them under `<C>/research/decision-shots/{app,prototype}/`
  with an `INDEX.md` line each: file, branch and sha, what it shows.
- One branch at a time when capturing from the app.
