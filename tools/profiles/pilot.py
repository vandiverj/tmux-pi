#!/usr/bin/env python3
"""HB7c (M1) profile pilot: the derived catalogs that pin an arm's model, and a dry run that prints the packet each arm would send.

  pilot.py catalog --model MODEL --out PATH
      Write config/model-catalog.json with level small listing MODEL alone (the models small gave up move to MODEL's old level, or
      to mid, because the router's chain and every model must stay listed). tp-bench dispatches code tasks with --level small, so `--env TP_MODEL_CATALOG=PATH` pins the arm's model.
  pilot.py dry-run --out DIR [--base DIR] [--arm DIR] [--tasks ID,ID]
      For every arm (model x profile, plus a no-profile arm per model), dispatch each pilot task exactly as lib/bench/runner.py
      does (`tp-agent dispatch --role coder --task BRIEF --workdir W --tests affected --level small`), against a fake tmux,
      claude and pi, so no worker starts and nothing reaches LM Studio. Each packet is written to DIR/<arm>/<task>.packet.md.
      With --base (a checkout of main), the no-profile packets are compared byte for byte with main's, the task id being the
      only text replaced (dispatch makes a random one). Prints the tp-bench commands of the pilot.

The derived catalogs and every dry-run file are written where --out says; nothing here is committed or reads local config."""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}
PROFILES = ("expert", "guided")
TASKS = ("code-accuracy-bug", "code-shot-clock")
REPEAT = 3
ID_TOKEN = "<TASK-ID>"
FAKE = {  # the fake tools put first on PATH; every call is logged, and a call to a worker harness fails
    "tmux": 'case "$1" in has-session) exit 0 ;; show-options) printf "%s\\n" "$TP_STATE_DIR" ;; new-window) printf "%%dry\\n" ;; *) exit 0 ;; esac',
    "claude": "exit 97", "pi": "exit 97", "curl": "exit 97",
}


def pinned_catalog(source, model):
    """The catalog at `source` with level small listing `model` alone, so no other model of level small (an OpenAI or other
    alternative) can be chosen for an arm, and a health fallback has nothing to swap in. The router's own chain and every
    other model must stay listed, so the models small gave up move to the level that held `model` (or, when small held it,
    to level mid). Raises ValueError when no level lists the model or the catalog has no small (or, for that move, mid) level."""
    catalog = json.loads(Path(source).read_text())
    levels = {level["key"]: level for level in catalog["levels"]}
    if "small" not in levels:
        raise ValueError(f"{source} has no level small")
    home = next((level for level in catalog["levels"] for m in level.get("models", []) if m.get("model") == model), None)
    if home is None:
        raise ValueError(f"no level of {source} lists {model}")
    small = levels["small"]
    entry = next(m for m in home["models"] if m.get("model") == model)
    if entry.get("provider") == "local":
        raise ValueError(f"{model} is a local model: the pilot arms are frontier only (the local arm is slice 2)")
    displaced = [m for m in small["models"] if m.get("model") != model]
    dest = home if home is not small else levels.get("mid")
    if dest is None:
        raise ValueError(f"{source} has no level to hold the models level small gives up")
    dest["models"] = [m for m in dest["models"] if m.get("model") != model] + displaced
    small["models"] = [entry]
    catalog["description"] = f"{catalog.get('description', '')} [M1 pilot: level small pinned to {model}]".strip()
    return catalog


def write_catalog(model, out, root=ROOT):
    """Write the pinned catalog and return its ABSOLUTE path (the dispatch that reads it runs in another directory)."""
    out = Path(out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pinned_catalog(Path(root) / "config/model-catalog.json", model), indent=2) + "\n")
    return out


def outside_env():
    """This process's environment without any session or task binding (a dry run must not touch the caller's session)."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("TMUX", "TP_", "CLAUDE_CODE_SESSION", "LANE_"))}


class Sandbox:
    """One fixed set of paths (fake bin, state dir, workdir, work store) reused by every dispatch, so packets from two checkouts
    differ only where the checkouts differ."""

    def __init__(self, top):
        self.top = Path(top)
        self.bin, self.log = self.top / "bin", self.top / "fake-calls.log"
        self.bin.mkdir(parents=True, exist_ok=True)
        for name, body in FAKE.items():
            path = self.bin / name
            path.write_text(f'#!/bin/sh\nprintf "%s %s\\n" "{name}" "$*" >> "{self.log}"\n{body}\n')
            path.chmod(0o755)
        self.project = self.top / "project"
        self.project.mkdir(exist_ok=True)
        self.state = self.project / ".tmux-pi"

    def dispatch(self, checkout, brief, catalog, profile):
        shutil.rmtree(self.state, ignore_errors=True)
        shutil.rmtree(self.top / "work-store", ignore_errors=True)
        (self.state / "tasks").mkdir(parents=True)
        env = {**outside_env(), "TP_STATE_DIR": str(self.state), "TP_SESSION": "bench-dryrun", "TP_WORK_DIR": str(self.top / "work-store"),
               "TP_MODEL_CATALOG": str(catalog), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"}
        if profile:
            env["TP_PROFILE"] = profile
        run = subprocess.run([str(Path(checkout) / "bin/tp-agent"), "dispatch", "--role", "coder", "--task", brief, "--workdir",
                              str(self.project), "--tests", "affected", "--level", "small"],
                             env=env, cwd=self.project, text=True, capture_output=True, timeout=120)
        if run.returncode:
            raise RuntimeError(f"dispatch in {checkout} failed: {run.stderr.strip()[:400]}")
        item = json.loads(run.stdout)
        task = self.state / "tasks" / item["id"]
        meta = json.loads((task / "meta.json").read_text())
        return (task / "packet.md").read_bytes().replace(item["id"].encode(), ID_TOKEN.encode()), meta.get("model"), meta.get("worker")

    def harness_calls(self):
        """Calls to claude, pi or curl (a worker start or an LM Studio request): the dry run must have none."""
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if not line.startswith("tmux ")]


def bench_commands(arm, catalogs, tasks):
    """One shell-safe line per pilot cell (paths may hold spaces): shlex.split of a line gives back its argv."""
    return [shlex.join(["bin/tp-bench", "run", "--arm", str(arm), "--label", f"m1-{short}-{profile}", "--tasks", ",".join(tasks),
                        "--repeat", str(REPEAT), "--max-worker-runs", str(len(tasks) * REPEAT), "--order", "fixed",
                        "--env", f"TP_PROFILE={profile}", "--env", f"TP_MODEL_CATALOG={catalogs[short]}"])
            for short in MODELS for profile in PROFILES]


def dry_run(out, arm, base, tasks):
    out, arm = Path(out).resolve(), str(Path(arm).resolve())   # absolute before any cwd change (dispatch runs in the sandbox)
    base = str(Path(base).resolve()) if base else None
    out.mkdir(parents=True, exist_ok=True)
    catalogs = {short: write_catalog(model, out / "catalogs" / f"small-{short}.json", Path(arm)) for short, model in MODELS.items()}
    specs = {t: json.loads((Path(arm) / "bench/tasks" / f"{t}.json").read_text()) for t in tasks}
    box = Sandbox(Path(tempfile.mkdtemp(prefix="tp-m1-dry-")))
    rows, same = [], True
    try:
        for short, model in MODELS.items():
            for profile in (None, *PROFILES):
                cell = f"{short}-{profile or 'none'}"
                (out / cell).mkdir(exist_ok=True)
                for task in tasks:
                    brief = specs[task].get("brief") or specs[task]["question"]
                    packet, got, worker = box.dispatch(arm, brief, catalogs[short], profile)
                    (out / cell / f"{task}.packet.md").write_bytes(packet)
                    row = {"arm": cell, "task": task, "model": got, "worker": worker, "bytes": len(packet), "pinned": got == model}
                    if profile is None and base:
                        main_packet, main_model, _ = box.dispatch(base, brief, catalogs[short], None)
                        (out / cell / f"{task}.main.packet.md").write_bytes(main_packet)
                        row["same_as_main"] = main_packet == packet and main_model == got
                        same = same and row["same_as_main"]
                    rows.append(row)
        calls = box.harness_calls()
    finally:
        shutil.rmtree(box.top, ignore_errors=True)
    summary = {"rows": rows, "harness_calls": calls, "no_profile_same_as_main": same if base else None,
               "commands": bench_commands(arm, catalogs, tasks),
               "worker_runs": len(MODELS) * len(PROFILES) * len(tasks) * REPEAT}
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    for row in rows:
        print(f"{row['arm']:14} {row['task']:20} {row['worker']}/{row['model']} pinned={row['pinned']} {row['bytes']} bytes"
              + (f" same_as_main={row['same_as_main']}" if "same_as_main" in row else ""))
    print(f"worker or LM Studio calls: {len(calls)}")
    print(f"pilot worker runs: {summary['worker_runs']}")
    print("\n".join(summary["commands"]))
    ok = not calls and all(r["pinned"] for r in rows) and (same or not base)
    return 0 if ok else 1


def main(argv=None):
    top = argparse.ArgumentParser(prog="pilot.py", description=__doc__.splitlines()[0])
    sub = top.add_subparsers(dest="command", required=True)
    cat = sub.add_parser("catalog", help="write a catalog whose level small lists one model")
    cat.add_argument("--model", required=True)
    cat.add_argument("--out", required=True)
    dry = sub.add_parser("dry-run", help="print the packet each pilot arm would send; start no worker")
    dry.add_argument("--out", required=True)
    dry.add_argument("--arm", default=str(ROOT))
    dry.add_argument("--base", default=None, help="a checkout of main to compare the no-profile packets with")
    dry.add_argument("--tasks", default=",".join(TASKS))
    args = top.parse_args(argv)
    if args.command == "catalog":
        print(write_catalog(args.model, args.out))
        return 0
    return dry_run(args.out, args.arm, args.base, [t for t in args.tasks.split(",") if t])


if __name__ == "__main__":
    sys.exit(main())
