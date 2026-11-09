#!/usr/bin/env python3
"""Run the summarize_files chores (tests/calibration/summarize/) against one local model and score the replies.

    summarize_run.py --model M [--split tune|holdout|all] [--runs N] --out FILE [--prompt-file P] [--only ID,ID]
                     [--thinking T] [--timeout SECONDS] [--pi-command CMD] [--route-command CMD]
    summarize_run.py --path structured --model M --out FILE [--endpoint URL] [--budget-chars N] [--split S] [--runs N] [--only ID,ID]
    summarize_run.py --route-only --out FILE [--split S] [--only ID,ID]

Per chore and run: copy the chore's fixture files into a fresh temp dir, run one Pi worker headless there (bin/tp-pi -p) with
the system prompt tp-spawn gives a worker on M (config/router.json "local" "prompts", read through lib/router.load_config as
tp-spawn does; --prompt-file replaces that text; the temp dir holds no .tmux-pi/worker-preamble.md, so no preamble is added),
score the reply with summarize_check.check, and append one JSON line to FILE. The record keeps the raw reply. The reply is graded
as a list of fact lines, `- <claim> [<path>:<line>[-<line>]]` (see summarize_check.py): the record's `failures` name, per failed
line, the rule (1-26) it broke. The last line is the summary: pass rate, median seconds, fabricated (bad) citations (every line reference of the reply, in any
shape, on a fact line or not, across line breaks: rules 10, 13, 14, 17 to 19; a good one in a noncanonical shape is not fabricated but fails the reply, rule 15),
unparsed references (a number that follows a file name or a line word and that no reference grammar placed: rule 21; the count that backs the grammar's
floor), unchecked citations (good citations no matching fact's support_lines vouch for) and the failure classes (incl. `uncited`, `identifier_only`,
`unparsed_references`). **S2 requires fabricated_citations and unparsed_references to both be zero.** The temp dir is always removed.

--path structured (the product path of LS1c) runs no worker: lib/summarize_structured numbers the chore's fixture files and makes one
JSON-schema call to --endpoint (system prompt and schema: the "summarize_structured" key of the local config, else of
config/local.example.json); facts whose citation does not check out are dropped and listed in the record's `drops`. Records, scoring
and the summary are the same; the summary adds `dropped_facts`. A refused file or a failed call is the record's `error`.

--route-only runs `tp-route --no-log --local-only` on each chore's task instead; no worker runs. tp-route's output has no shape
field (the router's shape tag decides the gate but is not printed or logged), so a record holds what the tool does give: exit
code, level, model, source and reason of a route, or the stderr text of a stop, and an outcome: "routed" (exit 0), "shape_not_qualified"
(a stop whose text says "outside the qualified local shapes": the router's shape tag was missing or not one the catalog has qualified), "stop"
(any other stop, exit 2 or 4), "error", "timeout" or "bad_output". Tagging accuracy is the share of chores that route local
once summarize_files is qualified, or stop as shape_not_qualified while it is a candidate.

The child sees no tmux or task session: TMUX, TMUX_PANE, CLAUDE_CODE_SESSION_ID and every TP_* variable are removed, except
TP_LOCAL_CONFIG (which machine config to read) and TP_PI_NODE (which node runs pi). The runner never creates a tmux session,
a worktree or a git call. It needs the model server (LM Studio) to be up; run it from a plain shell.
"""
import argparse
import json
import os
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CHORES = ROOT / "tests" / "calibration" / "summarize"
sys.path.insert(0, str(HERE))
from summarize_check import check  # noqa: E402

KEEP_TP = ("TP_LOCAL_CONFIG", "TP_PI_NODE")
DROP = ("TMUX", "TMUX_PANE", "CLAUDE_CODE_SESSION_ID")
ROUTE_KEYS = ("level", "model", "source", "reason")
SHAPE_STOP = "outside the qualified local shapes"


def child_env(env):
    return {k: v for k, v in env.items() if k not in DROP and (not k.startswith("TP_") or k in KEEP_TP)}


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def load_chores(split="all", only=(), directory=CHORES):
    answers = {a["id"]: a for a in read_jsonl(Path(directory) / "answers.jsonl")}
    chores = [c for c in read_jsonl(Path(directory) / "chores.jsonl") if split == "all" or c["split"] == split]
    if only:
        unknown = set(only) - {c["id"] for c in chores}
        if unknown:
            raise SystemExit("summarize_run: no such chore in this split: " + ", ".join(sorted(unknown)))
        chores = [c for c in chores if c["id"] in only]
    return [(c, answers[c["id"]]) for c in chores]


def system_prompt(model, prompt_file=None, root=ROOT):
    """The text tp-spawn appends to the system prompt of a worker on model (see bin/tp-spawn, `--append-system-prompt`)."""
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    sys.path.insert(0, str(Path(root) / "lib"))
    from router import load_config
    return load_config(str(Path(root) / "config" / "router.json"))["local"].get("prompts", {}).get(model, "")


def _run(cmd, cwd, env, timeout, stdin=None):
    """(rc, stdout, stderr, seconds); rc is None when the time ran out (the whole process group is killed)."""
    started = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        rc = None
    return rc, out, err, time.monotonic() - started


def run_pi(task, cwd, model, prompt, thinking, timeout, pi_command, env):
    cmd = shlex.split(pi_command) + ["-p", "--no-session", "--no-context-files", "--model", model]
    if thinking:
        cmd += ["--thinking", thinking]
    if prompt:
        cmd += ["--append-system-prompt", prompt]
    return _run(cmd + ["--", "# Task\n\n{}\n".format(task)], cwd, child_env(env), timeout)


def run_route(task, cwd, timeout, route_command, env):
    return _run(shlex.split(route_command) + ["--no-log", "--local-only", task], cwd, child_env(env), timeout)


def summarize(records, **extra):
    scored = [r for r in records if "pass" in r]
    classes = {}
    for r in scored:
        if r.get("error"):
            classes[r["error"]] = classes.get(r["error"], 0) + 1
        for key in ("missing", "missing_citation", "unsupported", "order", "forbidden_hit", "bad_citations", "unparsed_references", "uncited", "identifier_only"):
            if r.get(key):
                classes[key] = classes.get(key, 0) + 1
    passed = sum(r["pass"] for r in scored)
    return dict(summary=True, **extra, total=len(scored), passed=passed,
                pass_rate=round(passed / len(scored), 4) if scored else None,
                median_seconds=round(statistics.median(r["seconds"] for r in scored), 2) if scored else None,
                fabricated_citations=sum(len(r["bad_citations"]) for r in scored),
                unparsed_references=sum(len(r.get("unparsed_references", ())) for r in scored),
                unchecked_citations=sum(r["unchecked"] for r in scored), failure_classes=classes,
                s2_needs_zero=["fabricated_citations", "unparsed_references"])  # the summary says it: S2 needs both counts at 0


def run_chores(pairs, *, model, runs, out, prompt, thinking, timeout, pi_command, fixtures, env, label):
    records = []
    with open(out, "w", encoding="utf-8") as handle:
        for chore, answer in pairs:
            for n in range(1, runs + 1):
                work = tempfile.mkdtemp(prefix="tp-qual-sum-")
                try:
                    for name in chore["files"]:
                        target = Path(work) / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(Path(fixtures) / name, target)
                    rc, reply, _, seconds = run_pi(chore["task"], work, model, prompt, thinking, timeout, pi_command, env)
                    error = "timeout" if rc is None else "exit {}".format(rc) if rc else None
                    # A fixture the worker edited would change what a citation points at; score against the originals.
                    scored = check(reply or "", answer, fixtures)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                record = dict(chore=chore["id"], split=chore["split"], run=n, model=model, prompt=label, seconds=round(seconds, 2),
                              rc=rc, error=error, reply=reply, **scored)
                if error:
                    record["pass"] = False
                records.append(record)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        summary = summarize(records, model=model, prompt=label, runs=runs)
        handle.write(json.dumps(summary) + "\n")
    return records, summary


def run_structured(pairs, *, model, runs, out, timeout, fixtures, endpoint, settings, budget_chars, label):
    """--path structured: the same chores through lib/summarize_structured.summarize (the product path, not a copy). Nothing is
    copied or spawned: the module reads the fixture files itself, so there is no temp dir, no worker and no git call."""
    sys.path.insert(0, str(ROOT / "lib"))
    import summarize_structured as ss
    records = []
    with open(out, "w", encoding="utf-8") as handle:
        for chore, answer in pairs:
            for n in range(1, runs + 1):
                started = time.monotonic()
                reply, drops, error = "", [], None
                try:
                    reply, info = ss.summarize(fixtures, chore["files"], chore["task"], endpoint=endpoint, model=model,
                                               settings=settings, budget_chars=budget_chars, timeout=timeout)
                    drops = info["drops"]
                except (ss.PrepareError, ss.AskError) as exc:
                    error = "{}: {}".format(type(exc).__name__, exc)
                seconds = time.monotonic() - started
                scored = check(reply, answer, fixtures)
                record = dict(chore=chore["id"], split=chore["split"], run=n, model=model, prompt=label, seconds=round(seconds, 2),
                              rc=None, error=error, reply=reply, drops=drops, **scored)
                if error:
                    record["pass"] = False
                records.append(record)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
        summary = summarize(records, model=model, prompt=label, runs=runs, path="structured",
                            dropped_facts=sum(len(r["drops"]) for r in records))
        handle.write(json.dumps(summary) + "\n")
    return records, summary


def classify_route(rc, out, err):
    """The record fields for one tp-route run, from what the tool really gives: on exit 0 one JSON line (worker, model, thinking,
    level, provider, source, reason, steps, ... but no shape); on a stop, "tp-route: stop and ask the user: <reason>" on stderr
    and exit 2 (or 4); on a refused request, "tp-route: <reason>" and exit 1."""
    result = None
    if rc == 0:
        try:
            result = json.loads(out)
        except ValueError:
            result = None
        result = result if isinstance(result, dict) else None
    text = (err or "").strip()
    if rc is None:
        outcome = "timeout"
    elif rc == 0:
        outcome = "routed" if result is not None else "bad_output"
    elif SHAPE_STOP in text:
        outcome = "shape_not_qualified"
    else:
        outcome = "stop" if rc in (2, 4) else "error"
    return dict(rc=rc, outcome=outcome, **{k: (result or {}).get(k) for k in ROUTE_KEYS}, stderr=text[:400] or None)


def route_chores(pairs, *, out, timeout, route_command, env):
    outcomes, levels = {}, {}
    with open(out, "w", encoding="utf-8") as handle:
        for chore, _ in pairs:
            rc, text, err, seconds = run_route(chore["task"], str(ROOT), timeout, route_command, env)
            record = dict(chore=chore["id"], split=chore["split"], seconds=round(seconds, 2), **classify_route(rc, text, err))
            outcomes[record["outcome"]] = outcomes.get(record["outcome"], 0) + 1
            if record["outcome"] == "routed":
                levels[str(record["level"])] = levels.get(str(record["level"]), 0) + 1
            handle.write(json.dumps(record) + "\n")
        summary = dict(summary=True, route_only=True, total=len(pairs), outcomes=outcomes, levels=levels)
        handle.write(json.dumps(summary) + "\n")
    return summary


def main(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", help="roster model, e.g. lmstudio/qwen/qwen3.6-35b-a3b")
    p.add_argument("--split", choices=("tune", "holdout", "all"), default="all")
    p.add_argument("--runs", type=int, default=1)
    p.add_argument("--out", required=True)
    p.add_argument("--prompt-file", help="system-prompt text to use instead of the model's config/local.json prompt")
    p.add_argument("--only", default="", help="comma-separated chore ids")
    p.add_argument("--thinking", default="off")
    p.add_argument("--timeout", type=float, default=300)
    p.add_argument("--path", choices=("pi", "structured"), default="pi",
                   help="pi: one Pi worker per chore (default); structured: lib/summarize_structured (numbered files, one JSON-schema call)")
    p.add_argument("--endpoint", help="structured: base URL of the OpenAI-compatible server (default: the LM Studio base_url of the local config)")
    p.add_argument("--budget-chars", type=int, help="structured: refuse files whose numbered text exceeds this (default: the config's budget_chars, else 60000)")
    p.add_argument("--route-only", action="store_true")
    p.add_argument("--pi-command", default=str(ROOT / "bin" / "tp-pi"))
    p.add_argument("--route-command", default=str(ROOT / "bin" / "tp-route"))
    p.add_argument("--chores-dir", default=str(CHORES), help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.runs < 1:
        p.error("--runs must be at least 1")
    if not args.route_only and not args.model:
        p.error("--model is required unless --route-only")
    pairs = load_chores(args.split, [x for x in args.only.split(",") if x], args.chores_dir)
    if args.route_only:
        summary = route_chores(pairs, out=args.out, timeout=args.timeout, route_command=args.route_command, env=env)
    elif args.path == "structured":
        sys.path.insert(0, str(ROOT / "lib"))
        import summarize_structured as ss
        local = env.get("TP_LOCAL_CONFIG")
        settings = dict(ss.load_settings(ROOT, local))
        if args.prompt_file:
            settings["system"] = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        # the roster names a model with its provider (lmstudio/qwen/...); the server knows it without the provider
        served = args.model.split("/", 1)[1] if args.model.startswith("lmstudio/") else args.model
        _, summary = run_structured(pairs, model=served, runs=args.runs, out=args.out, timeout=args.timeout,
                                    fixtures=Path(args.chores_dir) / "fixtures", settings=settings,
                                    budget_chars=args.budget_chars or settings.get("budget_chars", ss.DEFAULT_BUDGET_CHARS),
                                    endpoint=args.endpoint or ss.default_endpoint(ROOT, local), label=args.prompt_file or "config")
    else:
        label = args.prompt_file or "config"
        _, summary = run_chores(pairs, model=args.model, runs=args.runs, out=args.out,
                                prompt=system_prompt(args.model, args.prompt_file), thinking=args.thinking,
                                timeout=args.timeout, pi_command=args.pi_command, fixtures=Path(args.chores_dir) / "fixtures",
                                env=env, label=label)
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
