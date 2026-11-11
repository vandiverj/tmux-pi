#!/usr/bin/env python3
"""HB8 episode runner: one complete working episode through Main's normal entry point, in a private tmux server.

    tools/episodes/run.py EPISODE.json (--live | --fake [--fake-script NAME] | --dry) [--project DIR] [--out DIR]
                          [--test-command CMD] [--build-command CMD] [--claude-config DIR] [--timeout-scale F]

What it does, in order (every action and wait is a line with a UTC time in RUN_DIR/run/episode-log.jsonl):
  1. makes RUN_DIR = OUT/<episode id>-<UTC stamp>-<rand> and a scratch clone of --project in it (`git clone --no-hardlinks`, push
     disabled, `.tmux-pi/` excluded locally, node_modules copied by clonefile when the episode asks for it, the episode's setup
     files and setup commit applied). The original project is only read.
  2. starts Main with `bin/tp --main claude CLONE` (TP_NO_ATTACH=1) — the owner's entry point, which runs `tp-main` with
     TP_MAIN_HARNESS=claude in the main pane — in a private tmux server: a fresh TMUX_TMPDIR under /tmp and `-L tpep-...`, reached
     through a `tmux` wrapper first on PATH that pins both, so bin/tp, tp-main and everything Main starts use that server only. bin/tp
     gives the session its own state dir under the clone; TP_SESSION is the run's own name; TP_WORK_DIR is the run's own.
  3. plays the episode's steps: owner turns (one line, pasted whole with `paste-buffer -p`, then Enter; never typed keys, never a
     leading `!`), waits on record conditions with timeouts, forced events (/compact in Main's pane, Main restart = respawn the main
     pane and run tp-main again in it, text queued into a worker's input box, a worker left at its prompt). Every poll also records
     each task's state changes (`observe` lines), which the scorer uses as the state timeline.
  4. runs the project's test command once in the clone (the final check, with the tree fingerprint), lists Main's transcripts,
     writes RUN_DIR/run/run.json, and always kills its own private server (`kill-server` on its own -L socket) and removes its
     TMUX_TMPDIR. The clone and every record stay for scoring (tools/episodes/score.py RUN_DIR).

--fake: Main and workers are scripted stand-ins (tools/episodes/fake/claude, fake/worker.py) that write the same records; the
entry point is the same (bin/tp -> tp-main -> tp-claude); only `claude` on PATH is the fake. No model is started.
--dry : prints the plan and exits; nothing is created.
--live: real Claude Main (and whatever workers it dispatches). Run by Main only. Needs Claude Code folder trust for OUT
        (`bin/tp-claude-bootstrap OUT` once): the runner stops with that instruction when the trust screen appears.

Reused: tools/smoke/g3_e2e.py `bounded` (every subprocess has a hard bound and its own process group) and its private-socket
discipline; lib/pane_send.py `paste`/`pane_has_text` (whole bracketed paste, then Enter); lib/gate_record.py `fingerprint` (the
same tree fingerprint test-run.sh records). Python 3.9, standard library only.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools" / "smoke"))
import episode_lib as el  # noqa: E402
from g3_e2e import bounded  # noqa: E402
import gate_record  # noqa: E402  (lib/, put on sys.path by episode_lib)
import pane_send  # noqa: E402

DEFAULT_PROJECT = "~/Documents/Projects/battle-soccer"
DEFAULT_OUT = "~/Documents/Projects/tmux-pi-episodes"
SESSION_VARS = ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_TRIAL_ID", "TP_SESSION",
                "TP_STATE_DIR", "TP_WORK_DIR", "TP_MODE", "TP_WORKDIR", "CLAUDE_CODE_SESSION_ID", "TP_REDACTION_LAUNCH",
                "TP_REDACTION_SESSION", "TP_MAIN_HARNESS", "TP_MAIN", "TMUX_TMPDIR")
SHELLS = ("zsh", "-zsh", "bash", "-bash", "sh", "-sh", "fish", "login")
TRUST_MARKERS = ("Do you trust the files in this folder", "Quick safety check", "Yes, I trust this folder")
GIT_IDENTITY = {"GIT_AUTHOR_NAME": "episode-setup", "GIT_AUTHOR_EMAIL": "episode-setup@invalid",
                "GIT_COMMITTER_NAME": "episode-setup", "GIT_COMMITTER_EMAIL": "episode-setup@invalid"}


class RunError(Exception):
    pass


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class Runner:
    def __init__(self, args, episode, episode_path):
        self.args = args
        self.episode = episode
        self.episode_path = Path(episode_path).resolve()
        self.mode = "dry" if args.dry else "fake" if args.fake else "live"
        self.scale = args.timeout_scale
        self.project_src = Path(os.path.expanduser(args.project or episode["project"].get("path") or DEFAULT_PROJECT)).resolve()
        # Resolved: Claude Code names a project's transcript directory after the real path of its cwd (/private/var, not
        # /var), so Main's transcripts are found only under the resolved clone path.
        self.out = Path(os.path.expanduser(args.out or DEFAULT_OUT)).resolve()
        self.run_id = "%s-%s-%s" % (episode["id"], stamp(), secrets.token_hex(2))
        self.run_dir = (self.out / self.run_id)
        self.clone = self.run_dir / self.project_src.name
        self.log_path = self.run_dir / "run" / "episode-log.jsonl"
        self.session = "ep-" + secrets.token_hex(5)
        self.socket = "tpep-%d-%s" % (os.getpid(), secrets.token_hex(3))
        self.tmux_tmp = None
        self.bin_dir = self.run_dir / "run" / "bin"
        self.state_dir = None
        self.main_pane = None
        self.test_command = args.test_command or episode["project"].get("test_command") or "npm test"
        self.build_command = args.build_command or episode["project"].get("build_command") or ""
        self.claude_config = Path(os.path.expanduser(
            args.claude_config or (str(self.run_dir / "claude-config") if self.mode == "fake" else
                                   os.environ.get("TP_CLAUDE_CONFIG_DIR") or "~/.config/tmux-pi/claude")))
        self.seen = {}          # task id -> (state, tool, result) last logged
        self.started = time.monotonic()
        self.budget = float(episode.get("timeout_seconds", 3600)) * self.scale
        self.outcome = "incomplete"
        self.start_commit = None

    # ---- logging ------------------------------------------------------------------------------------------------------
    def log(self, kind, at=None, **fields):
        entry = {"at": at or el.utc_now(), "kind": kind}
        entry.update(fields)
        el.append_jsonl(self.log_path, entry)
        return entry

    # ---- processes ----------------------------------------------------------------------------------------------------
    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in SESSION_VARS}
        path = [str(self.bin_dir)]
        if self.mode == "fake":
            path.append(str(HERE / "fake"))
        env.update(PATH=os.pathsep.join(path + [env.get("PATH", "/usr/bin:/bin")]), TMUX_TMPDIR=str(self.tmux_tmp),
                   TP_SESSION=self.session, TP_NO_ATTACH="1", TP_MAIN_HARNESS="claude",
                   TP_WORK_DIR=str(self.run_dir / "work-store"), TP_EPISODE_RUN=str(self.run_dir))
        if self.mode == "fake" or self.args.claude_config:
            env["TP_CLAUDE_CONFIG_DIR"] = str(self.claude_config)
        if self.mode == "fake":
            env["TP_EPISODE_FAKE"] = str(self.run_dir / "run" / "fake.json")
        return env

    def sh(self, argv, timeout=60, cwd=None, env=None, check=True, stdin=None):
        run = bounded([str(a) for a in argv], timeout, cwd=str(cwd) if cwd else None, env=env or self.env(), stdin=stdin)
        if check and run.returncode != 0:
            raise RunError("`%s` exited %d: %s" % (" ".join(map(str, argv))[:200], run.returncode,
                                                   (run.stderr or run.stdout).strip()[-400:]))
        return run

    def tmux(self, *args, check=True, timeout=15):
        return self.sh([self.bin_dir / "tmux", *args], timeout=timeout, check=check)

    # ---- plan -----------------------------------------------------------------------------------------------------------
    def plan(self):
        lines = ["episode   %s (%s, %s): %s" % (self.episode["id"], self.episode["row"], self.episode["variant"], self.episode["title"]),
                 "mode      %s%s" % (self.mode, (" (script %s)" % self.args.fake_script) if self.args.fake else ""),
                 "project   %s  (read only; scratch clone: %s)" % (self.project_src, self.clone),
                 "tmux      private server: TMUX_TMPDIR=/tmp/tpep-XXXX, -L %s; session %s" % (self.socket, self.session),
                 "main      bin/tp --main claude %s  (TP_NO_ATTACH=1; pane runs TP_MAIN_HARNESS=claude bin/tp-main)" % self.clone,
                 "claude    CLAUDE_CONFIG_DIR=%s" % self.claude_config,
                 "budget    %.0f s" % self.budget]
        for step in self.episode["steps"]:
            lines.append("step      %-14s %-12s %s" % (step["id"], step["do"], describe(step)))
        lines.append("final     %s  (in the clone, with the tree fingerprint)" % self.test_command)
        for check in self.episode["expected"].get("checks", []):
            lines.append("expect    %-20s %s" % (check["id"], check.get("why", check["check"])))
        lines.append("teardown  kill-server -L %s; remove its TMUX_TMPDIR; keep %s" % (self.socket, self.run_dir))
        return "\n".join(lines)

    # ---- setup ------------------------------------------------------------------------------------------------------
    def make_clone(self):
        src = self.project_src
        if not (src / ".git").exists():
            raise RunError("not a git repository: %s" % src)
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "run").mkdir()
        self.log("run.start", run_id=self.run_id, mode=self.mode, episode=self.episode["id"], episode_file=str(self.episode_path))
        el.write_json(self.run_dir / "run" / "episode.json", self.episode)   # the scorer reads the run dir only
        self.sh(["git", "clone", "--quiet", "--no-hardlinks", src, self.clone], timeout=300, env=os.environ.copy())
        git = ["git", "-C", self.clone]
        self.sh(git + ["remote", "set-url", "--push", "origin", "no-push://episode-scratch-clone"], env=os.environ.copy())
        with open(self.clone / ".git" / "info" / "exclude", "a") as handle:
            handle.write("\n.tmux-pi/\nnode_modules\n")   # lane worktrees share this file and may link node_modules
        deps = self.episode["project"].get("deps")
        if deps and (src / deps).is_dir() and not (self.clone / deps).exists():
            copy = self.sh(["cp", "-cR", src / deps, self.clone / deps], timeout=600, check=False, env=os.environ.copy())
            if copy.returncode != 0:
                shutil.copytree(src / deps, self.clone / deps, symlinks=True)
            self.log("setup.deps", path=deps)
        setup = self.episode.get("setup", {})
        for rel, text in setup.get("files", {}).items():
            target = self.clone / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            self.log("setup.file", path=rel)
        edits = setup.get("commit", [])
        for edit in edits:
            path = self.clone / edit["path"]
            text = path.read_text(encoding="utf-8")
            if edit["old"] not in text:
                raise RunError("setup edit: %r not found in %s" % (edit["old"][:60], edit["path"]))
            path.write_text(text.replace(edit["old"], edit["new"], 1), encoding="utf-8")
        if edits:
            self.sh(git + ["commit", "-qam", setup.get("message", "episode setup")], env={**os.environ, **GIT_IDENTITY})
            self.log("setup.commit", paths=[e["path"] for e in edits])
        self.start_commit = self.sh(git + ["rev-parse", "HEAD"], env=os.environ.copy()).stdout.strip()
        self.log("setup.clone", src=str(src), src_commit=self.sh(["git", "-C", src, "rev-parse", "HEAD"], env=os.environ.copy()).stdout.strip(),
                 clone=str(self.clone), start_commit=self.start_commit)

    def make_server(self):
        self.tmux_tmp = Path(tempfile.mkdtemp(prefix="tpep-", dir="/tmp")).resolve()
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        wrapper = self.bin_dir / "tmux"
        real = shutil.which("tmux", path=os.environ.get("PATH"))
        if not real:
            raise RunError("tmux not found")
        wrapper.write_text('#!/bin/sh\nunset TMUX TMUX_PANE\nexport TMUX_TMPDIR="%s"\nexec "%s" -L %s "$@"\n'
                           % (self.tmux_tmp, real, self.socket))
        wrapper.chmod(0o755)
        # This process's own tmux calls (pane_send) go through the wrapper too; it never reaches the default server.
        for key in ("TMUX", "TMUX_PANE"):
            os.environ.pop(key, None)
        os.environ["TMUX_TMPDIR"] = str(self.tmux_tmp)
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + os.environ.get("PATH", "")
        self.log("server.private", socket=self.socket, tmux_tmpdir=str(self.tmux_tmp))

    def write_fake_config(self):
        mapping = {"test": self.test_command, "build": self.build_command or self.test_command, "clone": str(self.clone)}
        script = self.episode.get("fake", {}).get(self.args.fake_script)
        if script is None:
            raise RunError("episode %s has no fake script %r" % (self.episode["id"], self.args.fake_script))
        el.write_json(self.run_dir / "run" / "fake.json", {
            "episode": self.episode["id"], "script": el.substitute(script, mapping),
            "turns": {s["id"]: s["text"] for s in self.episode["steps"] if s["do"] == "owner"},
            "clone": str(self.clone), "run_dir": str(self.run_dir), "fake_dir": str(HERE / "fake"), "root": str(ROOT)})

    def start_main(self):
        if self.mode == "fake":
            self.write_fake_config()
        self.sh([ROOT / "bin" / "tp", "--main", "claude", self.clone], cwd=self.clone, timeout=60)
        opts = {}
        for name in ("@tp_state_dir", "@tp_main_pane", "@tp_workdir"):
            opts[name] = self.tmux("show-options", "-qv", "-t", "=%s:" % self.session, name).stdout.strip()
        self.state_dir, self.main_pane = opts["@tp_state_dir"], opts["@tp_main_pane"]
        if not self.state_dir or not self.main_pane:
            raise RunError("bin/tp did not record the session's state dir and main pane")
        self.log("main.start", session=self.session, state_dir=self.state_dir, main_pane=self.main_pane, workdir=opts["@tp_workdir"],
                 command="bin/tp --main claude %s" % self.clone)
        self.wait_main_ready("main.ready")

    def pane_text(self, pane):
        run = self.tmux("capture-pane", "-p", "-t", pane, check=False)
        return run.stdout if run.returncode == 0 else ""

    def pane_command(self, pane):
        return self.tmux("display-message", "-p", "-t", pane, "#{pane_current_command}", check=False).stdout.strip()

    def wait_main_ready(self, label, timeout=None):
        """Main's process runs in its pane (not the shell) and the pane has stopped changing for a moment. The trust screen stops
        the run with the instruction to bootstrap trust once."""
        timeout = timeout or (60 if self.mode == "fake" else 180) * max(self.scale, 0.1)
        quiet = 1.0 if self.mode == "fake" else 4.0
        deadline = time.monotonic() + timeout
        last, since = None, time.monotonic()
        while time.monotonic() < deadline:
            text = self.pane_text(self.main_pane)
            if any(marker in text for marker in TRUST_MARKERS):
                self.log(label, ok=False, reason="trust-screen")
                raise RunError("Claude Code asks for folder trust in %s: run `bin/tp-claude-bootstrap %s` once, approve, /exit, "
                               "then run the episode again" % (self.clone, self.out))
            command = self.pane_command(self.main_pane)
            if text != last:
                last, since = text, time.monotonic()
            elif command and command not in SHELLS and time.monotonic() - since >= quiet:
                self.log(label, ok=True, command=command)
                return True
            time.sleep(0.2)
        self.log(label, ok=False, reason="timeout", seconds=timeout)
        raise RunError("Main did not become ready within %.0f s" % timeout)

    # ---- records ----------------------------------------------------------------------------------------------------
    def observe(self):
        """Log every change of a task's (state, tool, result) since the last poll: the scorer's state timeline."""
        snaps = el.tasks(self.state_dir)
        for snap in snaps:
            key = (snap["state"], snap["tool"], snap["result"])
            if self.seen.get(snap["id"]) != key:
                self.seen[snap["id"]] = key
                self.log("observe", task=snap["id"], role=snap["role"], lane=snap["lane"], state=snap["state"], tool=snap["tool"],
                         result=snap["result"], updated=snap["updated"], pane=snap["pane"])
        return snaps

    def holds(self, cond, snaps):
        kind = cond["kind"]
        if kind == "any":
            return any(self.holds(c, snaps) for c in cond["of"])
        if kind == "all":
            return all(self.holds(c, snaps) for c in cond["of"])
        if kind == "not":
            return not self.holds(cond["of"], snaps)
        count = int(cond.get("count", 1))
        if kind == "task":
            return len(self.matching_tasks(cond, snaps)) >= count
        if kind == "lane":
            records = [r for r in el.lanes(self.clone) if (not cond.get("name") or r.get("name") == cond["name"])
                       and (not cond.get("state") or r.get("state") in _list(cond["state"]))]
            return len(records) >= count
        if kind == "request":
            found = 0
            for snap in snaps:
                for req in el.requests(snap["dir"]) or []:
                    if not cond.get("status") or req.get("status") in _list(cond["status"]):
                        found += 1
            return found >= count
        if kind == "file":
            try:
                text = (self.clone / cond["path"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            return re.search(cond["pattern"], text) is not None
        if kind == "main_quiet":
            return self.main_quiet(float(cond.get("seconds", 20)))
        raise RunError("unknown condition %r" % kind)

    def matching_tasks(self, cond, snaps):
        out = []
        for snap in snaps:
            if cond.get("role") and snap["role"] != cond["role"]:
                continue
            if cond.get("id") and snap["id"] != cond["id"]:
                continue
            if cond.get("lane") and snap["lane"] != cond["lane"]:
                continue
            if cond.get("in_lane") and not snap["lane"]:
                continue
            if cond.get("packet"):         # live Main names its own lanes and task ids: select by what the brief says
                packet = el.read_text(Path(snap["dir"]) / "packet.md") or el.read_text(Path(snap["dir"]) / "task.md") or ""
                if not re.search(cond["packet"], packet):
                    continue
            if cond.get("state") and snap["state"] not in _list(cond["state"]):
                continue
            result = cond.get("result")
            if result == "any" and not snap["result"]:
                continue
            if result and result != "any" and snap["result"] != result:
                continue
            out.append(snap)
        return out

    def main_quiet(self, seconds):
        """Main's pane has not changed for `seconds` (checked by this call, which polls until then or until it changes)."""
        seconds = seconds * (1.0 if self.mode == "live" else min(1.0, self.scale))
        first = self.pane_text(self.main_pane)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            time.sleep(min(0.5, seconds))
            self.observe()           # keep the state timeline while waiting
            if self.pane_text(self.main_pane) != first:
                return False
        return True

    def remaining(self):
        return self.budget - (time.monotonic() - self.started)

    def wait_until(self, cond, timeout, step_id):
        timeout = min(timeout * self.scale, max(self.remaining(), 0))
        deadline = time.monotonic() + timeout
        poll = 0.2 if self.mode == "fake" else 1.0
        while True:
            snaps = self.observe()
            if self.holds(cond, snaps):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll)

    # ---- actions ----------------------------------------------------------------------------------------------------
    def send_text(self, pane, text, enter=True):
        """One bracketed paste of the whole text (lib/pane_send.paste), then Enter once the text is in the input region (or after
        2 s, for a TUI that shows a long paste as a marker)."""
        pane_send.paste(pane, text)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pane_send.pane_has_text(pane, text):
            time.sleep(0.05)
        if enter:
            time.sleep(pane_send.SETTLE_SECONDS)
            self.tmux("send-keys", "-t", pane, "Enter")

    def task_for(self, step):
        snaps = self.observe()
        cand = [s for s in snaps if (not step.get("role") or s["role"] == step["role"]) and s["pane"]
                and (not step.get("state") or s["state"] in _list(step["state"]))]
        cand.sort(key=lambda s: s["created"] or "")
        return cand[-1] if cand else None

    def play(self):
        for step in self.episode["steps"]:
            if self.remaining() <= 0:
                self.log("budget", ok=False, step=step["id"], seconds=self.budget)
                self.outcome = "budget"
                return
            self.observe()
            do = step["do"]
            if do == "owner":
                if "unless" in step and self.holds(step["unless"], self.observe()):
                    self.log("owner.skipped", step=step["id"], tag=step["tag"], reason="unless held")
                    continue
                # The turn is dated when the paste starts (a reply can be written before send_text returns); `sent` is when
                # the paste and Enter were done.
                started = el.utc_now()
                self.send_text(self.main_pane, step["text"])
                self.log("owner", at=started, sent=el.utc_now(), step=step["id"], tag=step["tag"], text=step["text"], rule=step.get("rule"))
            elif do == "wait":
                t0 = time.monotonic()
                ok = self.wait_until(step["until"], float(step["timeout"]), step["id"])
                self.log("wait", step=step["id"], ok=ok, seconds=round(time.monotonic() - t0, 2), until=step["until"])
                if not ok and step.get("on_timeout") == "fail":
                    self.outcome = "wait-failed"
                    return
            elif do == "hold_prompt":
                cond = {"kind": "task", "role": step.get("role"), "state": step.get("state", list(el.PROMPT_STATES))}
                t0 = time.monotonic()
                ok = self.wait_until(cond, float(step["timeout"]), step["id"])
                task = self.task_for({"role": step.get("role"), "state": cond["state"]}) if ok else None
                self.log("hold_prompt", step=step["id"], ok=ok, task=task and task["id"], state=task and task["state"],
                         seconds=round(time.monotonic() - t0, 2))
            elif do == "queue_text":
                task = self.task_for(step)
                if not task:
                    self.log("queue_text", step=step["id"], ok=False, reason="no task with a pane")
                    continue
                started = el.utc_now()
                self.send_text(task["pane"], step["text"], enter=False)
                self.log("queue_text", at=started, step=step["id"], ok=True, task=task["id"], state=task["state"], text=step["text"])
            elif do == "compact":
                started = el.utc_now()
                self.send_text(self.main_pane, "/compact")
                self.log("compact", at=started, step=step["id"])
            elif do == "restart_main":
                self.log("restart.kill", step=step["id"], pane=self.main_pane)
                self.tmux("respawn-pane", "-k", "-t", self.main_pane, "-c", str(self.clone))
                command = "TP_MAIN_HARNESS=claude %s" % shell_quote(str(ROOT / "bin" / "tp-main"))
                if step.get("args"):
                    command += " " + " ".join(shell_quote(a) for a in step["args"])
                self.wait_shell(self.main_pane)
                self.send_text(self.main_pane, command)
                self.log("restart.launch", step=step["id"], command=command)
                self.wait_main_ready("restart.ready")
            elif do == "note":
                self.log("note", step=step["id"], text=step.get("text", ""))
        self.observe()
        self.outcome = "played"

    def wait_shell(self, pane, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.pane_command(pane) in SHELLS and self.pane_text(pane).strip():
                return
            time.sleep(0.1)

    # ---- end ------------------------------------------------------------------------------------------------------------
    def final_check(self):
        if self.args.no_final_check:
            self.log("final_check", skipped=True)
            return
        try:
            head, tree = gate_record.fingerprint(self.clone)
        except (RuntimeError, OSError) as error:
            head, tree = None, None
            self.log("final_check.fingerprint", ok=False, error=str(error))
        # When the clone's last commit was made and whether its worktree is clean: Main's own test command (Main is not a
        # task, so it leaves no test-run record with a tree) counts as a final-tree check when it ran after that commit on a
        # clean tree (score.py).
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.clone), capture_output=True, text=True, timeout=60)
        last = subprocess.run(["git", "log", "-1", "--format=%cI"], cwd=str(self.clone), capture_output=True, text=True, timeout=60)
        clean = status.returncode == 0 and not status.stdout.strip()
        committed = last.stdout.strip() if last.returncode == 0 else None
        log_path = self.run_dir / "run" / "final-check.log"
        t0 = time.monotonic()
        try:
            run = bounded(["/bin/sh", "-c", self.test_command], self.args.final_timeout, cwd=str(self.clone),
                          env={k: v for k, v in os.environ.items() if k not in SESSION_VARS})
            code, timed_out = run.returncode, False
            log_path.write_text(run.stdout + run.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired:
            code, timed_out = None, True
        record = {"cmd": self.test_command, "head": head, "tree": tree, "clean": clean, "head_committed": committed,
                  "exit": code, "timed_out": timed_out,
                  "seconds": round(time.monotonic() - t0, 2), "log": str(log_path)}
        el.write_json(self.run_dir / "run" / "final-check.json", record)
        self.log("final_check", **record)

    def main_transcripts(self):
        base = self.claude_config / "projects" / el.claude_project_slug(self.clone)
        workers = set()
        for snap in el.tasks(self.state_dir):
            path = el.transcript_path(snap["dir"])
            if path:
                workers.add(str(path))
        if not base.is_dir():
            return []
        found = [p for p in base.glob("*.jsonl") if str(p) not in workers]
        found.sort(key=lambda p: p.stat().st_mtime)
        return [str(p) for p in found]

    def teardown(self):
        if self.tmux_tmp is None:
            return
        killed = bounded([shutil.which("tmux") or "tmux", "-L", self.socket, "kill-server"], 15,
                         env={**{k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}, "TMUX_TMPDIR": str(self.tmux_tmp)})
        alive = bounded([shutil.which("tmux") or "tmux", "-L", self.socket, "list-sessions"], 15,
                        env={**os.environ, "TMUX_TMPDIR": str(self.tmux_tmp)}).returncode == 0
        shutil.rmtree(self.tmux_tmp, ignore_errors=True)
        self.log("teardown", socket=self.socket, killed=killed.returncode == 0, server_left=alive)
        self.tmux_tmp = None

    def write_run_record(self, error=None):
        transcripts = self.main_transcripts() if self.state_dir else []
        record = {"schema": el.RUN_SCHEMA, "run_id": self.run_id, "mode": self.mode,
                  "fake_script": self.args.fake_script if self.mode == "fake" else None,
                  "episode": self.episode["id"], "row": self.episode["row"], "variant": self.episode["variant"],
                  "episode_file": str(self.episode_path),
                  "episode_sha256": hashlib.sha256(self.episode_path.read_bytes()).hexdigest(),
                  "tmux_pi_root": str(ROOT), "tmux_pi_commit": _git_out(ROOT, "rev-parse", "HEAD"),
                  "tmux_pi_dirty": bool(_git_out(ROOT, "status", "--porcelain")),
                  "project_src": str(self.project_src), "clone": str(self.clone), "start_commit": self.start_commit,
                  "state_dir": self.state_dir, "session": self.session, "socket": self.socket, "main_pane": self.main_pane,
                  "claude_config": str(self.claude_config), "main_transcripts": transcripts,
                  "test_command": self.test_command, "build_command": self.build_command, "outcome": self.outcome,
                  "error": error, "seconds": round(time.monotonic() - self.started, 1)}
        el.write_json(self.run_dir / "run" / "run.json", record)
        return record

    def run(self):
        error = None
        saved_env = dict(os.environ)
        try:
            self.make_clone()
            self.make_server()
            self.start_main()
            self.play()
        except (RunError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
            error = "%s: %s" % (type(exc).__name__, exc)
            self.outcome = "error"
            if self.log_path.parent.is_dir():
                self.log("error", error=error)
        finally:
            if self.state_dir:
                try:
                    self.observe()
                except Exception as exc:  # noqa: BLE001 - teardown must still run
                    self.log("error", error="observe: %s" % exc)
            try:
                self.teardown()
            finally:
                if self.log_path.parent.is_dir():
                    if (self.clone / ".git").exists():
                        self.final_check()
                    record = self.write_run_record(error)
                    self.log("run.end", outcome=self.outcome, error=error)
                    print(json.dumps({"run_dir": str(self.run_dir), "outcome": self.outcome, "error": error,
                                      "main_transcripts": record["main_transcripts"]}))
                os.environ.clear()
                os.environ.update(saved_env)
        return 0 if error is None else 1


def _git_out(repo, *args):
    run = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    return run.stdout.strip() if run.returncode == 0 else None


def _list(value):
    return [value] if isinstance(value, str) else list(value or [])


def shell_quote(text):
    return "'" + text.replace("'", "'\\''") + "'"


def describe(step):
    do = step["do"]
    if do == "owner":
        extra = " unless %s" % json.dumps(step["unless"]) if "unless" in step else ""
        return "[%s] %s%s" % (step["tag"], step["text"][:90] + ("..." if len(step["text"]) > 90 else ""), extra)
    if do == "wait":
        return "%s (timeout %ss)" % (json.dumps(step["until"]), step["timeout"])
    if do == "hold_prompt":
        return "leave a %s worker at its prompt (wait up to %ss)" % (step.get("role", "any"), step["timeout"])
    if do == "queue_text":
        return "queue into the %s worker's input box: %s" % (step.get("role", "newest"), step["text"])
    if do == "restart_main":
        return "kill Main (respawn pane) and run tp-main again%s" % (" " + " ".join(step["args"]) if step.get("args") else "")
    if do == "compact":
        return "/compact in Main's pane"
    return step.get("text", "")


def parse(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("episode")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="real Claude Main (Main runs this, never a test)")
    mode.add_argument("--fake", action="store_true", help="scripted stand-ins for Main and workers")
    mode.add_argument("--dry", action="store_true", help="print the plan only")
    parser.add_argument("--fake-script", default="pass")
    parser.add_argument("--project")
    parser.add_argument("--out")
    parser.add_argument("--test-command")
    parser.add_argument("--build-command")
    parser.add_argument("--claude-config")
    parser.add_argument("--timeout-scale", type=float, default=1.0, help="multiply every wait timeout and the budget")
    parser.add_argument("--final-timeout", type=float, default=600)
    parser.add_argument("--no-final-check", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(argv if argv is not None else sys.argv[1:])
    try:
        episode = el.load_episode(args.episode)
    except el.EpisodeError as error:
        print("run.py: %s" % error, file=sys.stderr)
        return 2
    runner = Runner(args, episode, args.episode)
    if args.dry:
        print(runner.plan())
        return 0
    previous = signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        return runner.run()
    finally:
        signal.signal(signal.SIGTERM, previous)


def _raise_interrupt(_signum, _frame):
    raise KeyboardInterrupt("SIGTERM")


if __name__ == "__main__":
    sys.exit(main())
