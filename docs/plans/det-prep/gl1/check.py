#!/usr/bin/env python3
"""GL1 local fixture check (preparation only; proves nothing about GitLab itself).

Parses the fixture with PyYAML, resolves local includes and one-level `extends`, and evaluates `workflow:rules` and job
`rules` (first match wins; `if` with ==, != and =~ /re/; `changes` as path globs with ** ; `when: never`) for each scenario,
then compares the job list with the hand-written expectation. GitLab semantics it does NOT model: changes on new branches /
push pipelines without a compare base, compare_to, MR pipelines vs branch pipelines both existing, include rules, needs
validation, variables precedence. Rows marked gitlab_only are reported, never scored.

  check.py                       # validate fixture + score scenarios
  check.py --lint FILE SCENARIO  # compare a saved GitLab CI Lint response (dry_run, include_jobs) with a scenario
"""
import json, re, sys, fnmatch, pathlib
import yaml

HERE = pathlib.Path(__file__).resolve().parent
FIX = HERE / "fixture"
RESERVED = {"stages", "workflow", "include", "variables", "default", "image", "services", "before_script", "after_script", "cache"}


def load():
    root = yaml.safe_load((FIX / ".gitlab-ci.yml").read_text())
    merged = {}
    for inc in root.get("include", []):
        path = FIX / inc["local"]
        if not path.is_file():
            raise SystemExit(f"include does not resolve: {inc['local']}")
        merged.update(yaml.safe_load(path.read_text()) or {})
    merged.update({k: v for k, v in root.items() if k != "include"})
    jobs = {}
    for name, body in merged.items():
        if name in RESERVED or name.startswith(".") or not isinstance(body, dict):
            continue
        job = dict(body)
        ext = job.pop("extends", None)
        for e in ([ext] if isinstance(ext, str) else ext or []):
            if e not in merged:
                raise SystemExit(f"{name}: extends unknown {e}")
            job = {**merged[e], **job}
        for n in job.get("needs", []):
            if n not in merged:
                raise SystemExit(f"{name}: needs unknown job {n}")
        if job.get("stage") not in root.get("stages", []):
            raise SystemExit(f"{name}: stage {job.get('stage')} not in stages")
        jobs[name] = job
    return root, jobs


def glob(path, pat):
    rx = re.escape(pat).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, path) is not None


def cond(expr, env):
    m = re.fullmatch(r"\s*\$(\w+)\s*(==|!=|=~)\s*(.+?)\s*", expr)
    if not m:
        raise SystemExit(f"unsupported if: {expr}")
    left, op, right = env.get(m[1], ""), m[2], m[3]
    if op == "=~":
        return re.search(right[1:-1] if right.startswith("/") and right.endswith("/") else right, left) is not None
    right = env.get(right[1:], "") if right.startswith("$") else right.strip('"')
    return (left == right) if op == "==" else (left != right)


def rules_pass(rules, env, changes):
    for r in rules:
        ok = True
        if "if" in r:
            ok = cond(r["if"], env)
        if ok and "changes" in r:
            pats = r["changes"]["paths"] if isinstance(r["changes"], dict) else r["changes"]
            ok = any(glob(f, p) for f in changes for p in pats)
        if ok:
            return r.get("when", "on_success") != "never"
    return False


def run(sc, root, jobs):
    env = {"CI_PIPELINE_SOURCE": sc["source"], "CI_COMMIT_BRANCH": sc["branch"] if sc["source"] == "push" else "",
           "CI_DEFAULT_BRANCH": sc.get("default", "main")}
    if not rules_pass(root.get("workflow", {}).get("rules", [{}]), env, sc["changes"]):
        return []
    return sorted(n for n, j in jobs.items() if rules_pass(j.get("rules", [{}]), env, sc["changes"]))


def main():
    root, jobs = load()
    scen = {s["id"]: s for s in json.loads((HERE / "scenarios.json").read_text())["scenarios"]}
    if sys.argv[1:2] == ["--lint"]:
        resp = json.loads(pathlib.Path(sys.argv[2]).read_text())
        got = sorted(j["name"] for j in resp.get("jobs", []))
        exp = sorted(scen[sys.argv[3]]["expect"])
        print(json.dumps({"valid": resp.get("valid"), "errors": resp.get("errors"), "got": got, "expect": exp,
                          "missing": sorted(set(exp) - set(got)), "extra": sorted(set(got) - set(exp))}, indent=1))
        return
    print(f"fixture ok: {len(jobs)} jobs, {len(root['include'])} includes resolve, extends/needs/stages consistent")
    bad = 0
    for s in scen.values():
        got, exp = run(s, root, jobs), sorted(s["expect"])
        tag = "gitlab-only" if s.get("gitlab_only") else ("ok" if got == exp else "MISMATCH")
        bad += tag == "MISMATCH"
        print(f"{tag:11} {s['id']:20} local={got}" + ("" if got == exp else f" expect={exp}"))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
