---
context_id: example.case_work_prompt
status: example
last_reviewed: 2026-11-04
repos: [<repo>, <context-repo>, tmux-pi]
---

# Case work: standard instructions (example)

This folder is an **example of lane-setup and agent-guidance prompts for campaigns**: how one person sets up lanes,
briefs workers and keeps an orchestrator ("Main") on the rails across a batch of tracker cases. Every private name,
path and id has been replaced with a placeholder (table at the bottom); fill them in for your own project. The rules
and the reasons behind them are kept as they were.

How Claude (as tmux-pi Main) takes a set of `<tracker>` cases in `<repo>` from assigned to MR-ready. Built from
several campaigns (`.tmux-pi/campaigns/<campaign>` and later rounds), an earlier task checklist and a weekend work
prompt. When this folder and an older document disagree, this folder wins; a decision `<owner>` made later wins over
both.

## Starting a batch

Open a tmux-pi session in `<repo>` and give Main a message like this:

```text
Read <ctx>/prompt/case-work-prompt.md
and follow it.
Cases: <CASE-ID>, <CASE-ID>, <CASE-ID>
Campaign folder: .tmux-pi/campaigns/<campaign>   (new or existing)
Base: origin/develop                               (or the parent case branch if stacked)
Sprint: <N>                                        (decision labels are S<N>D<n>; the first is S<N>D1)
Deck: research/decision-deck-<round>
```

Leave out a line to accept its default: a new campaign named after the sprint, base `origin/develop`, the next free
label in the campaign's decision log (numbering restarts at D1 in each campaign), and a new deck folder.

## Files, in reading order

| File | What it is for |
|---|---|
| `case-work-prompt.md` | The prompt itself: Main's role, standing orders, approvals, and the per-case flow at a glance. |
| `01-sources-of-truth.md` | Which source wins; where the roadmap, prototype, regulations and decisions live, and how to use each. |
| `02-case-lifecycle.md` | Steps 0–10 for a case, each with its checks, outputs and stop conditions. |
| `03-security-and-quality.md` | Domain rules, secops, lint and code scanning, test standards, comments, text truth, other-product impact. |
| `04-tmux-pi-operations.md` | Lanes, dispatch, review rounds, close guard and owner override, worker prompts, the DB test runner. |
| `05-decisions-and-deck.md` | Decision-log format, numbering, sign-off, and building the HTML decision deck. |
| `06-handoff-mr-tracker.md` | MR body, `<tracker>` comments, posting, assignment, time logging, one MR at a time. |
| `07-known-failures.md` | What went wrong in early campaigns and the rule that now prevents each. |
| `templates/` | Worker brief templates: common rules, lane step, design, fix round, secops, send gate (pre-MR), case handoff, sync merge. |
| `deck/` | Reusable decision-deck kit (`build.mjs`, `template.html`, starter `data.mjs` with one made-up decision). |

## Placeholders used throughout

Use the same placeholder for the same thing in every file.

| Placeholder | Meaning |
|---|---|
| `<company>` | the employer / organization that owns the product |
| `<product>` | the main web app and API; `<api>` and `<web>` are its app folders under `apps/` |
| `<admin-app>` | the internal admin app that ships alongside it |
| `<program>` | the feature area the campaign works on (the "program" in file and permission names) |
| `<contact-role>` | the customer-side role that receives notices about the program |
| `<tracker>` | the issue tracker; `<git-host>` is the git host and `<mr-cli>` its command-line client |
| `<owner>` | the person who owns the campaign, approves decisions and gives every outward OK |
| `<reviewer>` | the human reviewer the MR is assigned to |
| `<CASE-ID>` | a tracker case key (lowercase `<case-id>` in file, lane and folder names) |
| `<REG>` | the regulation that governs the program, cited as `<REG> §<section>` |
| `<repo>` | `~/projects/<repo>`, the product monorepo |
| `<context-repo>` | `~/projects/<context-repo>`, the repo of context documents |
| `<ctx>` | `~/projects/<context-repo>/domains/<domain>` |
| `<C>` | the campaign folder, `<repo>/.tmux-pi/campaigns/<campaign>` (git-ignored, never committed) |
| `<wt>` | a case worktree, `~/projects/<repo>-wt-<lane>` |
| `<lane>` | lowercase lane name, `<case-id>-<short>` |
| `<branch>` | `feature/<CASE-ID>-<kebab-slug>` |
| `<base>` | the commit the lane was cut from (develop or the stacked parent) |

An older checklist file now points here. Update this folder, not that file.
