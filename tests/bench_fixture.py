"""Shared fixtures for the tp-bench tests: a bench directory with sealed answers, and a recording fake Shell that plays tmux, git,
bin/tp, bin/tp-exit and bin/tp-agent. The fake subclasses the real Shell, so `environment()` is the real env builder: what it records
is exactly what a child process would have received."""
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from bench import answers, runner, specs  # noqa: E402
from bench.shell import Result, Shell, ShellError  # noqa: E402

OK = Result(0, "", "")
MISSING = object()
EXPECTED = "forty two"
FINDINGS = "F1 src/a.ts:4 off-by-one in the loop bound\nCloses by: code\n"


def write_json(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n")


def make_bench(root):
    """(bench_dir, answers_dir, selected specs) for three tasks (local, code, review) with answers sealed outside bench_dir."""
    root = Path(root)
    bench, src, ans = root / "bench", root / "src", root / "answers"
    tasks = {
        "local-q": {"kind": "local", "question": "How many?", "answer": {"file": "local-q.json"}},
        "code-f": {"kind": "code", "brief": "Write f.", "test_path": "src/game/f.test.ts", "answer": {"file": "code-f.test.ts"}},
        "review-r": {"kind": "review", "request": "Add the loop.", "diff": "diffs/r.patch", "answer": {"file": "review-r.json"}},
    }
    for task_id, body in tasks.items():
        write_json(bench / "tasks" / f"{task_id}.json", {"schema": specs.SCHEMA, "id": task_id, "title": task_id, "budget_s": 100, **body})
    (bench / "diffs").mkdir(parents=True)
    (bench / "diffs" / "r.patch").write_text("--- a/src/a.ts\n+++ b/src/a.ts\n")
    write_json(bench / "fixtures" / "agent-tools.json", {"tools": []})
    write_json(src / "local-q.json", {"answer": EXPECTED})
    (src / "code-f.test.ts").write_text("test('f', () => {})\n")
    write_json(src / "review-r.json", {"defects": [{"id": "D1", "file": "src/a.ts", "line_start": 3, "line_end": 5, "keywords": ["off-by-one"]}]})
    loaded = specs.load_specs(bench / "tasks")
    answers.seal(loaded, bench / "tasks", src, ans)
    return bench, ans, specs.load_specs(bench / "tasks")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeShell(Shell):
    """Records every call as (argv, effective env, cwd). `sessions` maps a tmux session name to its @tp_state_dir."""

    def __init__(self, root, running=False, pane_text="", sessions=None, on_inspect=None, on_exit=None, settle_after=None, inject=None,
                 trust_inspect=False, result_bytes=None):
        """`running` keeps every task unfinished (until its `settle_after`-th inspect, when set). `trust_inspect` makes inspect answer as
        tp-agent does for a Claude worker stopped at the folder-trust dialog: status needs_input with trust_dialog {dir}, until it settles. `on_inspect(shell)` runs on each
        inspect; `on_exit(shell, env)` replaces tp-exit (return a Result, or None for the default). `inject(shell, argv)` runs before
        every call and may return a Result to stand in for it (a failure, a raise by raising). Task dirs and transcripts are
        made inside the project copy, as a real workspace's are, so they vanish with it unless the runner kept them.
        `raised` lists the argvs whose nonzero exit raised ShellError; `tolerated` the (argv, reason) of those the caller opted out of."""
        self.root, self.running, self.pane_text, self.on_inspect = Path(root), running, pane_text, on_inspect
        self.on_exit, self.settle_after, self.inspects, self.inject = on_exit, settle_after, 0, inject
        self.sessions, self.calls, self.dispatched, self.made, self.last_copy = dict(sessions or {}), [], [], 0, None
        self.raised, self.tolerated, self.failed = [], [], []
        self.trust_inspect = trust_inspect
        self.result_bytes = result_bytes  # None: the default text; bytes: written as result.md; MISSING: result_path named, file absent

    def names(self):
        return [(Path(argv[0]).name, argv[1] if len(argv) > 1 else "") for argv, _, _ in self.calls]

    def run(self, argv, env=None, cwd=None, timeout=None, tolerate=None):
        try:
            result = super().run(argv, env, cwd, timeout, tolerate)
        except ShellError:
            self.raised.append([str(a) for a in argv])
            self.failed.append([str(a) for a in argv])
            raise
        if result.returncode != 0:
            self.failed.append([str(a) for a in argv])
            self.tolerated.append(([str(a) for a in argv], tolerate))
        return result

    def execute(self, argv, env=None, cwd=None, timeout=None):
        argv = [str(a) for a in argv]
        self.calls.append((argv, self.environment(env), cwd))
        env = self.calls[-1][1]
        program = Path(argv[0]).name
        injected = self.inject(self, argv) if self.inject else None
        if injected is not None:
            return injected
        return getattr(self, "do_" + program.replace("-", "_").replace(".", "_"), self.other)(argv, env)

    def other(self, argv, env):
        return OK

    def do_tp(self, argv, env):
        name = env["TP_SESSION"]
        if name in self.sessions:
            return Result(1, "", "exists")
        state = self.root / "state" / name
        state.mkdir(parents=True, exist_ok=True)
        self.sessions[name] = str(state)
        return OK

    def do_tp_exit(self, argv, env):
        if self.on_exit:
            made = self.on_exit(self, env)
            if made is not None:
                return made
        self.sessions.pop(env.get("TP_SESSION"), None)
        return OK

    def do_tmux(self, argv, env):
        sub = argv[1]
        target = argv[argv.index("-t") + 1].lstrip("=").rstrip(":") if "-t" in argv else ""
        if sub == "list-sessions":
            return Result(0, "".join(f"{name}\n" for name in self.sessions), "")
        if sub == "show-options":
            return Result(0, self.sessions.get(target, "") + "\n", "")
        if sub == "kill-session":
            self.sessions.pop(target, None)
        if sub == "list-panes":
            return Result(0, "%1\n", "")
        if sub == "capture-pane":
            return Result(0, self.pane_text, "")
        return OK

    def do_git(self, argv, env):
        rest = argv[argv.index("-C") + 2:] if "-C" in argv else argv[1:]
        if rest[:2] == ["worktree", "add"]:
            Path(rest[-2]).mkdir(parents=True, exist_ok=True)
            self.last_copy = rest[-2]
        if rest and rest[0] == "rev-parse":
            return Result(0, "abc123\n", "")
        return OK

    def do_vitest(self, argv, env):
        return Result(0, json.dumps({"numTotalTests": 2, "numPassedTests": 2}), "")

    def do_tp_agent(self, argv, env):
        sub = argv[1]
        if sub == "dispatch":
            self.made += 1
            ident = f"t{self.made}"
            role = argv[argv.index("--role") + 1]
            self.dispatched.append((ident, role))
            return Result(0, json.dumps({"id": ident}), "")
        if sub == "review":
            return Result(0, json.dumps({"id": "review-1"}), "")
        if sub == "inspect":
            if self.on_inspect:
                self.on_inspect(self)
            return Result(0, json.dumps(self.inspect(argv[2])), "")
        return OK

    def task_dir(self, ident):
        return Path(self.last_copy or self.root) / ".tmux-pi" / "tasks" / ident

    def inspect(self, ident):
        self.inspects += 1
        if self.trust_inspect and not (self.settle_after and self.inspects >= self.settle_after):
            return {"status": "needs_input", "task_dir": str(self.task_dir(ident)), "trust_dialog": {"dir": "/work"},
                    "detail": "worker stopped at Claude Code's folder-trust dialog for /work; the owner trusts it once: bin/tp-claude-bootstrap /work"}
        if self.running and not (self.settle_after and self.inspects >= self.settle_after):
            return {"status": "running", "task_dir": str(self.task_dir(ident))}
        text = FINDINGS if ident == "review-1" else (f"ANSWER: {EXPECTED}\n" if dict(self.dispatched).get(ident) == "researcher" else "done\n")
        folder = self.task_dir(ident)
        folder.mkdir(parents=True, exist_ok=True)
        result = folder / "result.md"
        if self.result_bytes is None:
            result.write_text(text)
        elif self.result_bytes is not MISSING:
            result.write_bytes(self.result_bytes)
        (folder / ".env.local").write_text("SECRET=1\n")  # never copied out of the workspace
        (folder / "transcript.jsonl").write_text("{}\n")
        return {"status": "settled", "task_dir": str(folder), "result_path": str(result), "model": "m",
                "transcript": str(folder / "transcript.jsonl"), "tokens": {"input": 10, "output": 5, "cacheRead": None, "cacheWrite": 0}}


def make_runner(root, shell, bench, ans, clock=None, **kwargs):
    clock = clock or Clock()
    root = Path(root)
    (root / "arm" / "bin").mkdir(parents=True, exist_ok=True)
    return runner.Runner(shell, bench, root / "project", root / "arm", root / "results", "ctl", order="fixed", seed=1, clock=clock,
                         sleep=clock.sleep, poll_s=1.0, stamp=lambda: "2026-11-08T00:00:00Z", answers_directory=ans,
                         work_root=root / "work", token=lambda: "abc123", **kwargs), clock


def clean_up(*paths):
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


SESSION_RE = re.compile(r"^bench-[A-Za-z0-9_-]+-\d+-[0-9a-f]+$")
