# HB2b round 4: Main's record keeps every caller argument, parsed once (Main, 2026-11-11)

Earlier briefs (`hb2b-brief.md`, `hb2b-round2.md`, `hb2b-round3.md`, this dir) still apply. Kind of round: rule-first (Opus);
family main-argv-evidence has now been named in two reviews in a row. Lane base for fail-before: 13e56b4.

## Review (review-coder-47b147fee2-173305)
- F1: pi Main's `--extension /tmp/extra.ts` reaches pi but the record has no path or hash (`lib/launch_record.py:296–299`
  lists only Claude options).
- F2 (contrived, low): `tp-main --append-system-prompt '--tools=Read'` is recorded as `tools.exposed=["Read"]` because
  instruction and tool options are scanned separately, so a value is read as an option.
**Diagnosis:** Main's round-3 brief asked for an inventory of options; any inventory misses one (F1), and two scans disagree
about which tokens are values (F2). The brief's design was the fault, so this round changes the design, not the list.

## The rule
1. **Nothing the caller passed is left out.** Main's record gains `caller_args`: the caller's arguments (what tp-main forwards,
   before tp-main's own additions) as an ordered list, one entry per option or positional, from **one left-to-right pass**
   that consumes each known option's value before looking at the next token. `--` ends options; what follows is positional.
2. **Known options are classified** from that one pass (the round-3 Claude set plus pi's: `--extension`/`-e`, `--skill`,
   `--prompt-template`, `--system-prompt`, `--append-system-prompt`, `--tools`, and any option bin/tp-pi itself handles;
   find pi's from bin/tp-pi and the repo, do not start pi): files and extensions by path + sha256, directories by path, text
   and inline JSON by sha256 only; tools into `tools` as in round 3. The instruction entries and tools fields are derived from
   `caller_args`, never from a second scan.
3. **Unknown options are kept, not guessed:** an unknown `--x=v` is one entry; an unknown `--x` followed by a token not starting
   with `-` is recorded as the flag plus that next token marked `"value_guess": true` (the record cannot know its arity; it
   must not use it for classification). Any value over 200 characters or starting with `{` or `[` is stored as sha256 only.
   Never store or print the contents of a settings or mcp file.
4. Launch argv stays byte-identical (existing test).

## Attack (extend `caller_argument_table`, no new file)
Both harnesses; each known option in both forms; repeats; F1 and F2 cases; a value that looks like an option for every
text-taking option; an unknown flag with and without `=`; `--` then option-looking positionals; resume plus extras; an
unreadable file; JSON and long values hashed. Expected answers written from the rule first. Fail-before on 13e56b4 for F1
and F2 rows. Review F2 of round 1 (error summary "none" vs "unknown") stays a note.

## Verification (the worker tests once)
Run once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`) through `tools/agent/test-run.sh`, fresh short `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 600; exec @ARGV'`:
tests.test_main_notify tests.test_work_brief tests.test_worker_env tests.test_session_env. **Write report.md in the task dir**
(attack table expected vs got on base and tree, files changed, the run record path) and keep the fail-before log under
evidence/. Main runs the cells and discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never the
default tmux server. Edit only lib/launch_record.py, tests/test_main_notify.py and evidence files. Search with safe_grep, read
with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Guard refusals: if a guard hook
refuses a shell or inline-script edit, make the same edit with Edit/Write and list it in the report; any other refusal means
stop and report. Do not approve any permission or placeholder prompt. Commit nothing. `RESULT_STATUS: complete` last.
