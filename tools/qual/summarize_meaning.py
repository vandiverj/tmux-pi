#!/usr/bin/env python3
"""Grade summarize_files replies by MEANING, repeatably (LS1c step MG1).

    summarize_meaning.py packet --run NAME=FILE.jsonl [--run ...] (--seed N | --key FILE) --out DIR [--only ID,ID]
    summarize_meaning.py judge --packets DIR --model M --out DIR [--only ID,ID] [--timeout S] [--pi-command CMD]
    summarize_meaning.py score --grades DIR [--key FILE] [--run NAME=FILE.jsonl ...] [--json]
    summarize_meaning.py calibrate --judge DIR --reference DIR --key FILE [--json]

packet   One packet per chore from run files (summarize_run records; a chore's first record per run): the task, the numbered
         files, the reference facts F1..Fn, the claims that are not in the files, and the replies R1..Rk. The reply ids are
         shuffled by --seed (a stable hash, the same on every Python), or taken from an existing key with --key. The run-name key
         goes to DIR/key.json; the judge never sees it. The same inputs give byte-identical files.
judge    Grade each packet through `bin/tp-pi -p` the way summarize_run runs a worker (same child env, a fresh temp dir, a timeout,
         a process-group kill). --model has no default; a model whose provider is `local` in config/model-catalog.json, or whose id
         contains "qwen", is refused before any call: the judge is never the model under test. The reply must be the grade JSON of
         RUBRIC.md, complete and in shape; anything else is a judge_error for that chore (one retry), never a pass.
         Writes <chore>.grade.json per chore and judge.jsonl (one line per call).
score    The aggregate per run from grade files plus the key: strict pass (when run records are given), meaning pass, meaning >= 80%,
         recall, wrong, unsupported, and the bar verdict (zero wrong, zero unsupported, zero unparsed or judge_error, recall >= 90%
         overall, every chore >= ceil(0.8 n)).
calibrate Compare one judge's grade files with a reference set over the same packets and key; print every disagreement and the pass
         rule's verdict (constants CALIBRATION_RULE below). Exit 0 when the judge passes, 1 when not.

The child sees no tmux or task session (summarize_run.child_env). Standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
import summarize_run  # noqa: E402  (child_env, _run: one way to run a child, shared with the worker runner)

CHORES = ROOT / "tests" / "calibration" / "summarize"
MEANING = CHORES / "meaning"
CATALOG = ROOT / "config" / "model-catalog.json"

# The calibration pass rule. A judge counts only when ALL four hold; change a number here and nowhere else.
CALIBRATION_RULE = dict(
    min_fact_agreement=0.90,      # met/not_met agreement over every reply x fact
    min_precision_found=0.90,     # share of the reference's wrong + unsupported lines the judge also marks
    max_precision_added=5,        # wrong/unsupported lines the judge adds that the reference did not mark, across the set
    max_recall_gap=0.05,          # per run, |judge recall - reference recall|
)
BAR = dict(min_recall=0.90, chore_share=0.8)  # the LS1c meaning bar: recall >= 90% overall, every chore >= ceil(0.8 n)
FACT_VERDICTS = ("met", "not_met")
LINE_VERDICTS = ("ok", "wrong", "unsupported")
BAD = ("wrong", "unsupported")


class ToolError(Exception):
    pass


# ---------------------------------------------------------------- shared

def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def parse_named(specs, what):
    out = {}
    for spec in specs:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            raise ToolError("{} must be NAME=FILE, got {!r}".format(what, spec))
        if name in out:
            raise ToolError("{} {!r} given twice".format(what, name))
        out[name] = path
    return out


def first_records(path):
    """{chore: record}: the first record per chore (the summary line has no chore)."""
    out = {}
    for rec in read_jsonl(path):
        if "chore" in rec and rec["chore"] not in out:
            out[rec["chore"]] = rec
    return out


def need(n):
    """ceil(0.8 n) in integers."""
    return -(-n * 4 // 5)


def cited_lines(reply):
    """The reply lines that carry a citation (`name.ext:N` in any shape, also inside a table row or in backticks), in order. Blank
    lines and uncited lines do not count (RUBRIC.md). A free-form reply (a markdown table) has no fact-line shape, so a line
    counts when it holds any citation, or a "line N" reference; this gives the hand grading's count for 107 of the 108 replies (the
    hand grader numbered one extra line in exports-worker R7; the tests name it)."""
    return [ln for ln in reply.strip().splitlines() if re.search(r"\.\w+`?:\d+|\b[Ll]ines?\s+\d+", ln)]


# ---------------------------------------------------------------- packet

def order_for(chore, names, seed):
    """Reply ids by a stable hash of (seed, chore, run name): the same on every Python and every machine."""
    ranked = sorted(names, key=lambda n: hashlib.sha256("{}|{}|{}".format(seed, chore, n).encode()).hexdigest())
    return {"R{}".format(i): n for i, n in enumerate(ranked, 1)}


def numbered(text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return "".join("{:4d}\t{}\n".format(i, ln) for i, ln in enumerate(lines, 1))


def build_packet(chore, answer, replies, order, fixtures):
    """The text of one packet. replies: {run name: reply text}; order: {Rid: run name}."""
    parts = ["# Chore {}\n\n## Task\n{}\n\n## Files (numbered)\n".format(chore["id"], chore["task"])]
    for name in chore["files"]:
        parts.append("### {}\n```\n{}```\n".format(name, numbered((Path(fixtures) / name).read_text(encoding="utf-8"))))
    facts = "".join("F{}: {}\n".format(i, ref["text"][2:] if ref["text"].startswith("- ") else ref["text"])
                    for i, ref in enumerate(answer["reference"], 1))
    forbidden = "".join("- regex: `{}`\n".format(rx) for rx in answer.get("forbidden", ())) or "- (none)\n"
    parts.append("\n## Reference facts (what a complete answer states)\n{}\n".format(facts))
    parts.append("## Not in the files (a reply that says these is wrong)\n{}\n".format(forbidden))
    parts.append("## Replies\n")
    for rid, name in order.items():
        parts.append("### {}\n```\n{}\n```\n".format(rid, replies[name].strip() or "(empty reply)"))
    return "".join(parts)


def cmd_packet(args):
    runs = parse_named(args.run, "--run")
    if (args.seed is None) == (args.key is None):
        raise ToolError("exactly one of --seed or --key is required")
    records = {name: first_records(path) for name, path in runs.items()}
    answers = {a["id"]: a for a in read_jsonl(Path(args.chores_dir) / "answers.jsonl")}
    only = [x for x in args.only.split(",") if x]
    chores = [c for c in read_jsonl(Path(args.chores_dir) / "chores.jsonl") if not only or c["id"] in only]
    chores = [c for c in chores if any(c["id"] in r for r in records.values())]
    if only and {c["id"] for c in chores} != set(only):
        raise ToolError("no run has chore(s): " + ", ".join(sorted(set(only) - {c["id"] for c in chores})))
    key_in = json.loads(Path(args.key).read_text(encoding="utf-8")) if args.key else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    key = {}
    for chore in sorted(chores, key=lambda c: c["id"]):
        cid = chore["id"]
        missing = [n for n in runs if cid not in records[n]]
        if missing:
            raise ToolError("chore {} has no record in run(s): {}".format(cid, ", ".join(missing)))
        if key_in is not None:
            if cid not in key_in or sorted(key_in[cid].values()) != sorted(runs):
                raise ToolError("--key has no order of exactly these runs for chore {}".format(cid))
            order = {rid: key_in[cid][rid] for rid in sorted(key_in[cid], key=lambda r: int(r[1:]))}
        else:
            order = order_for(cid, list(runs), args.seed)
        key[cid] = order
        replies = {n: records[n][cid].get("reply") or "" for n in runs}
        text = build_packet(chore, answers[cid], replies, order, Path(args.chores_dir) / "fixtures")
        (out / "{}.md".format(cid)).write_text(text, encoding="utf-8")
    (out / "key.json").write_text(json.dumps(key, indent=1), encoding="utf-8")
    print(json.dumps(dict(packets=len(key), out=str(out), key=str(out / "key.json"))))
    return 0


# ---------------------------------------------------------------- grade shape

def packet_shape(text):
    """(fact ids, {reply id: number of cited lines}) of a packet text."""
    facts = re.findall(r"^(F\d+):", text.split("## Replies")[0], re.M)
    body = text.split("\n## Replies\n", 1)[1]
    replies = {m.group(1): len(cited_lines(m.group(2))) for m in re.finditer(r"^### (R\d+)\n```\n(.*?)\n```$", body, re.M | re.S)}
    return facts, replies


def validate_grade(grade, chore, facts, replies):
    """None when grade has exactly the RUBRIC.md shape for this packet, else the reason (strict: a missing reply, fact or
    line, an unknown verdict or a line index outside 1..N is an error). facts/replies as packet_shape; replies None = unknown
    line counts (shape only)."""
    if not isinstance(grade, dict) or grade.get("chore") != chore or not isinstance(grade.get("replies"), dict):
        return "not a {{chore: {}, replies: {{...}}}} object".format(chore)
    got = grade["replies"]
    if set(got) != set(replies):
        return "replies {} != packet replies {}".format(sorted(got), sorted(replies))
    for rid, rep in got.items():
        if not isinstance(rep, dict) or not isinstance(rep.get("facts"), dict) or not isinstance(rep.get("lines"), list):
            return "{}: needs facts and lines".format(rid)
        if set(rep["facts"]) != set(facts):
            return "{}: facts {} != packet facts {}".format(rid, sorted(rep["facts"]), sorted(facts))
        if any(v not in FACT_VERDICTS for v in rep["facts"].values()):
            return "{}: a fact verdict is not met/not_met".format(rid)
        seen = []
        for ln in rep["lines"]:
            if (not isinstance(ln, dict) or not isinstance(ln.get("n"), int) or isinstance(ln.get("n"), bool)
                    or ln.get("verdict") not in LINE_VERDICTS):
                return "{}: a line is not {{n: int, verdict: ok|wrong|unsupported}}".format(rid)
            seen.append(ln["n"])
        count = replies[rid]
        if count is not None and sorted(seen) != list(range(1, count + 1)):
            return "{}: line indexes {} != 1..{}".format(rid, sorted(seen), count)
        if count is None and len(set(seen)) != len(seen):
            return "{}: a line index repeats".format(rid)
    return None


def load_grades(directory, key):
    """({chore: grade or None}, {chore: reason}) for every chore of key: a missing, unreadable or malformed file is unparsed."""
    grades, errors = {}, {}
    for chore, order in key.items():
        path = Path(directory) / "{}.grade.json".format(chore)
        try:
            grade = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            grades[chore], errors[chore] = None, "unparsed: {}".format(exc.__class__.__name__)
            continue
        if isinstance(grade, dict) and grade.get("judge_error"):
            grades[chore], errors[chore] = None, "judge_error: {}".format(grade["judge_error"])
            continue
        reps = {rid: None for rid in order}
        facts = None
        if isinstance(grade, dict) and isinstance(grade.get("replies"), dict) and grade["replies"]:
            first = next(iter(grade["replies"].values()))
            facts = sorted(first.get("facts", {})) if isinstance(first, dict) and isinstance(first.get("facts"), dict) else None
        reason = validate_grade(grade, chore, facts or [], reps) if facts else "unparsed: no facts"
        if reason:
            grades[chore], errors[chore] = None, "unparsed: " + reason
        else:
            grades[chore] = grade
    return grades, errors


# ---------------------------------------------------------------- score

def reply_stats(rep):
    met = sum(v == "met" for v in rep["facts"].values())
    wrong = sum(ln["verdict"] == "wrong" for ln in rep["lines"])
    unsup = sum(ln["verdict"] == "unsupported" for ln in rep["lines"])
    return met, len(rep["facts"]), wrong, unsup, len(rep["lines"])


def run_stats(grades, errors, key, strict=None):
    """{run: stats} over the chores of key. grades/errors from load_grades; strict {run: {chore: bool}} or None."""
    names = sorted({n for order in key.values() for n in order.values()})
    out = {}
    for name in names:
        s = dict(chores=len(key), graded=0, errors=len(errors), met=0, facts=0, wrong=0, unsupported=0, lines=0,
                 meaning=0, meaning80=0, chore_shortfalls=[], per_chore={})
        if strict is not None:
            s["strict"] = sum(bool(strict.get(name, {}).get(c)) for c in key)
        for chore, order in key.items():
            grade = grades.get(chore)
            rid = next((r for r, n in order.items() if n == name), None)
            if grade is None or rid is None:
                continue
            m, n, w, u, ln = reply_stats(grade["replies"][rid])
            s["graded"] += 1
            s["met"] += m
            s["facts"] += n
            s["wrong"] += w
            s["unsupported"] += u
            s["lines"] += ln
            clean = w == 0 and u == 0
            s["meaning"] += clean and m == n
            s["meaning80"] += clean and m >= need(n)
            if m < need(n):
                s["chore_shortfalls"].append(chore)
            s["per_chore"][chore] = dict(met=m, facts=n, wrong=w, unsupported=u)
        s["recall"] = s["met"] / s["facts"] if s["facts"] else 0.0
        reasons = []
        if s["wrong"]:
            reasons.append("wrong lines")
        if s["unsupported"]:
            reasons.append("unsupported lines")
        if errors:
            reasons.append("unparsed or judge_error chores")
        if s["graded"] != len(key):
            reasons.append("not every chore graded")
        if s["recall"] < BAR["min_recall"]:
            reasons.append("recall below {:.0%}".format(BAR["min_recall"]))
        if s["chore_shortfalls"]:
            reasons.append("a chore below ceil(0.8 n)")
        s["bar"] = not reasons
        s["bar_failed"] = reasons
        out[name] = s
    return out


def load_strict(runs):
    return {name: {c: bool(r.get("pass")) for c, r in first_records(path).items()} for name, path in runs.items()}


def table(stats):
    has_strict = any("strict" in s for s in stats.values())
    head = "{:6}".format("run") + ("{:>8}".format("strict") if has_strict else "") + "{:>9}{:>10}{:>8}{:>7}{:>7}{:>7}{:>6}".format(
        "meaning", "mean>=80", "recall", "wrong", "unsup", "lines", "bar")
    rows = [head]
    for name, s in stats.items():
        rows.append("{:6}".format(name) + ("{:>8}".format("{}/{}".format(s["strict"], s["chores"])) if has_strict else "")
                    + "{:>9}{:>10}{:>8}{:>7}{:>7}{:>7}{:>6}".format(
                        "{}/{}".format(s["meaning"], s["chores"]), "{}/{}".format(s["meaning80"], s["chores"]),
                        "{:.0%}".format(s["recall"]), s["wrong"], s["unsupported"], s["lines"], "yes" if s["bar"] else "no"))
    return "\n".join(rows)


def cmd_score(args):
    key = json.loads(Path(args.key or Path(args.grades) / "key.json").read_text(encoding="utf-8"))
    grades, errors = load_grades(args.grades, key)
    strict = load_strict(parse_named(args.run, "--run")) if args.run else None
    stats = run_stats(grades, errors, key, strict)
    if args.json:
        print(json.dumps(dict(chores=len(key), errors=errors, runs=stats), indent=1))
    else:
        print(table(stats))
        for chore, why in sorted(errors.items()):
            print("ERROR {}: {}".format(chore, why))
        for name, s in stats.items():
            if not s["bar"]:
                print("bar {}: no ({})".format(name, "; ".join(s["bar_failed"])))
    return 0


# ---------------------------------------------------------------- calibrate

def compare(judge, reference, key, judge_errors, reference_errors):
    """The calibration numbers and every disagreement, plus the pass verdict (CALIBRATION_RULE)."""
    fact_total = fact_agree = 0
    line_total = line_agree = 0
    ref_bad = found = added = 0
    disagreements = []
    for chore, order in key.items():
        jg, rg = judge.get(chore), reference.get(chore)
        if rg is None:
            continue
        for rid, run in order.items():
            rrep = rg["replies"][rid]
            jrep = jg["replies"][rid] if jg else None
            for fid, rv in rrep["facts"].items():
                fact_total += 1
                jv = jrep["facts"][fid] if jrep else "missing"
                fact_agree += jv == rv
                if jv != rv:
                    disagreements.append(dict(kind="fact", chore=chore, reply=rid, run=run, item=fid, reference=rv, judge=jv,
                                              note=(jrep or {}).get("note", "")))
            jlines = {ln["n"]: ln for ln in (jrep["lines"] if jrep else ())}
            rlines = {ln["n"]: ln for ln in rrep["lines"]}
            for n, rl in sorted(rlines.items()):
                jl = jlines.get(n)
                jv = jl["verdict"] if jl else "missing"
                line_total += 1
                line_agree += jv == rl["verdict"]
                if rl["verdict"] in BAD:
                    ref_bad += 1
                    found += jv in BAD
                elif jv in BAD:
                    added += 1
                if jv != rl["verdict"]:
                    disagreements.append(dict(kind="line", chore=chore, reply=rid, run=run, item=n, reference=rl["verdict"],
                                              judge=jv, note=(jl or {}).get("note", "")))
            for n, jl in sorted(jlines.items()):
                if n not in rlines:  # a line the reference did not grade
                    line_total += 1
                    added += jl["verdict"] in BAD
                    disagreements.append(dict(kind="line", chore=chore, reply=rid, run=run, item=n, reference="missing",
                                              judge=jl["verdict"], note=jl.get("note", "")))
    r_stats = run_stats(reference, reference_errors, key)
    j_stats = run_stats(judge, judge_errors, key)
    gaps = {name: abs(j_stats[name]["recall"] - r_stats[name]["recall"]) for name in r_stats}
    rule = CALIBRATION_RULE
    fact_rate = fact_agree / fact_total if fact_total else 0.0
    found_rate = found / ref_bad if ref_bad else 1.0
    checks = dict(
        fact_agreement=fact_rate >= rule["min_fact_agreement"],
        precision_found=found_rate >= rule["min_precision_found"],
        precision_added=added <= rule["max_precision_added"],
        recall_gap=all(g <= rule["max_recall_gap"] + 1e-9 for g in gaps.values()),
        judged_every_chore=not judge_errors)
    return dict(
        fact_agreement=dict(agree=fact_agree, total=fact_total, rate=fact_rate),
        line_agreement=dict(agree=line_agree, total=line_total, rate=line_agree / line_total if line_total else 0.0),
        precision=dict(reference_bad=ref_bad, found=found, found_rate=found_rate, added=added),
        runs={name: dict(reference_recall=r_stats[name]["recall"], judge_recall=j_stats[name]["recall"], gap=gaps[name],
                         reference_bar=r_stats[name]["bar"], judge_bar=j_stats[name]["bar"]) for name in r_stats},
        judge_errors=judge_errors, checks=checks, rule=rule, passed=all(checks.values()), disagreements=disagreements)


def cmd_calibrate(args):
    key = json.loads(Path(args.key).read_text(encoding="utf-8"))
    judge, jerr = load_grades(args.judge, key)
    reference, rerr = load_grades(args.reference, key)
    if rerr:
        raise ToolError("the reference set is not valid: " + "; ".join("{}: {}".format(c, w) for c, w in sorted(rerr.items())))
    result = compare(judge, reference, key, jerr, rerr)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0 if result["passed"] else 1
    fa, la, pr = result["fact_agreement"], result["line_agreement"], result["precision"]
    print("fact agreement: {}/{} = {:.1%}".format(fa["agree"], fa["total"], fa["rate"]))
    print("line agreement: {}/{} = {:.1%}".format(la["agree"], la["total"], la["rate"]))
    print("precision failures (wrong + unsupported): reference {}, judge also marks {} ({:.1%}), judge adds {}".format(
        pr["reference_bad"], pr["found"], pr["found_rate"], pr["added"]))
    print("{:6}{:>12}{:>12}{:>8}{:>8}{:>8}".format("run", "ref recall", "judge recall", "gap", "ref bar", "jdg bar"))
    for name, r in result["runs"].items():
        print("{:6}{:>12.1%}{:>12.1%}{:>8.1%}{:>8}{:>8}".format(
            name, r["reference_recall"], r["judge_recall"], r["gap"], "yes" if r["reference_bar"] else "no", "yes" if r["judge_bar"] else "no"))
    for chore, why in sorted(jerr.items()):
        print("JUDGE ERROR {}: {}".format(chore, why))
    print("disagreements: {}".format(len(result["disagreements"])))
    for d in result["disagreements"]:
        print("  {kind} {chore} {reply} ({run}) {item}: reference={reference} judge={judge}{extra}".format(
            extra="  note: " + d["note"] if d["note"] else "", **d))
    print("rule: " + ", ".join("{} {}".format(k, "ok" if v else "FAIL") for k, v in result["checks"].items()))
    print("CALIBRATION {}".format("PASS" if result["passed"] else "FAIL"))
    return 0 if result["passed"] else 1


# ---------------------------------------------------------------- judge

def local_models(catalog=CATALOG):
    """Every model id the catalog lists under a provider of kind local (levels' models and the router chain)."""
    data = json.loads(Path(catalog).read_text(encoding="utf-8"))
    kinds = {k for k, v in data.get("providers", {}).items() if v.get("kind") == "local"} | {"local"}
    found = set()
    for level in data.get("levels", ()):
        for m in level.get("models", ()):
            if m.get("provider") in kinds:
                found.add(m.get("model"))
    for step in data.get("router", {}).get("chain", ()):
        if step.get("provider") in kinds:
            found.add(step.get("model"))
    return found


def refuse_model(model, catalog=CATALOG):
    """The reason a model may not judge, else None."""
    if "qwen" in model.lower():
        return "model {!r} contains qwen: the judge must not be the model under test".format(model)
    if model in local_models(catalog):
        return "model {!r} has provider local in the model catalog: the judge must not be the model under test".format(model)
    return None


def judge_prompt(rubric, packet):
    return ("{}\n\n---\n\nReturn the grade JSON for the packet below as your whole answer on stdout (a single JSON object in the shape "
            "above; no file is written and no tool is needed). Grade every reply and every fact; list every cited reply line.\n\n"
            "---\n\n{}".format(rubric.strip(), packet))


def extract_json(text):
    """The JSON value of a judge's stdout: the whole text, or the whole text inside one ``` fence. Anything else is an error."""
    body = (text or "").strip()
    m = re.fullmatch(r"```(?:json)?\s*\n(.*)\n```", body, re.S)
    return json.loads(m.group(1) if m else body)


def judge_once(prompt, chore, facts, replies, *, model, timeout, pi_command, env, thinking=None):
    """(grade or None, error or None, rc, seconds)."""
    import shlex
    work = tempfile.mkdtemp(prefix="tp-qual-meaning-")
    try:
        cmd = shlex.split(pi_command) + ["-p", "--no-session", "--no-context-files", "--model", model]
        if thinking:
            cmd += ["--thinking", thinking]
        rc, out, _, seconds = summarize_run._run(cmd + ["--", prompt], work, summarize_run.child_env(env), timeout)
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    if rc is None:
        return None, "timeout", rc, seconds
    if rc:
        return None, "exit {}".format(rc), rc, seconds
    try:
        grade = extract_json(out)
    except ValueError as exc:
        return None, "not JSON: {}".format(str(exc)[:80]), rc, seconds
    reason = validate_grade(grade, chore, facts, replies)
    return (None, "bad grade: " + reason, rc, seconds) if reason else (grade, None, rc, seconds)


def cmd_judge(args, env):
    why = refuse_model(args.model, args.catalog)
    if why:
        raise ToolError(why)
    rubric = Path(args.rubric).read_text(encoding="utf-8")
    only = [x for x in args.only.split(",") if x]
    packets = sorted(p for p in Path(args.packets).glob("*.md") if not only or p.stem in only)
    if not packets:
        raise ToolError("no packets in {}".format(args.packets))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    failed = 0
    with open(out / "judge.jsonl", "w", encoding="utf-8") as log:
        for path in packets:
            chore = path.stem
            text = path.read_text(encoding="utf-8")
            facts, replies = packet_shape(text)
            prompt = judge_prompt(rubric, text)
            grade, error = None, None
            for attempt in (1, 2):  # one retry on judge_error
                grade, error, rc, seconds = judge_once(prompt, chore, facts, replies, model=args.model, timeout=args.timeout,
                                                       pi_command=args.pi_command, env=env, thinking=args.thinking)
                log.write(json.dumps(dict(chore=chore, attempt=attempt, model=args.model, seconds=round(seconds, 2), rc=rc,
                                          error=error)) + "\n")
                log.flush()
                if grade is not None:
                    break
            result = grade if grade is not None else {"chore": chore, "judge_error": error}
            failed += grade is None
            (out / "{}.grade.json".format(chore)).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(dict(packets=len(packets), judge_errors=failed, out=str(out))))
    return 0 if not failed else 1


# ---------------------------------------------------------------- main

def main(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("packet")
    a.add_argument("--run", action="append", default=[], metavar="NAME=FILE.jsonl")
    a.add_argument("--seed", type=int)
    a.add_argument("--key", help="reuse the reply order of an existing key.json instead of shuffling")
    a.add_argument("--out", required=True)
    a.add_argument("--only", default="")
    a.add_argument("--chores-dir", default=str(CHORES), help=argparse.SUPPRESS)
    j = sub.add_parser("judge")
    j.add_argument("--packets", required=True)
    j.add_argument("--model", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--only", default="")
    j.add_argument("--timeout", type=float, default=600)
    j.add_argument("--thinking")
    j.add_argument("--rubric", default=str(MEANING / "RUBRIC.md"))
    j.add_argument("--pi-command", default=str(ROOT / "bin" / "tp-pi"))
    j.add_argument("--catalog", default=str(CATALOG), help=argparse.SUPPRESS)
    s = sub.add_parser("score")
    s.add_argument("--grades", required=True)
    s.add_argument("--key")
    s.add_argument("--run", action="append", default=[], metavar="NAME=FILE.jsonl")
    s.add_argument("--json", action="store_true")
    c = sub.add_parser("calibrate")
    c.add_argument("--judge", required=True)
    c.add_argument("--reference", required=True)
    c.add_argument("--key", required=True)
    c.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.cmd == "packet":
            if not args.run:
                p.error("packet needs at least one --run NAME=FILE")
            return cmd_packet(args)
        if args.cmd == "judge":
            return cmd_judge(args, env)
        return cmd_score(args) if args.cmd == "score" else cmd_calibrate(args)
    except ToolError as exc:
        print("summarize_meaning: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
