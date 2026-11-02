"""The record builder of lane L2 (lib/trial_record.py) and the records file, against the files a worker can leave as anything but a plain
file (review 8: the worker is not hostile but makes mistakes). The same matrix as test_trial_shadow_worker_files.py (`KindCase.matrix`):
every read of a task, reviewer or transcript file is a read_plain read, and a FIFO, a link, a directory or a huge file reads as no file.

Split from test_trial_shadow_worker_files.py so that each file is well under the 300 s bound. Fixtures: trial_shadow_case.py."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403
from test_trial_shadow_worker_files import KindCase, LIMIT, MALFORMED  # noqa: E402,F401
import trial_record  # noqa: E402
import trial_scan  # noqa: E402


class RecordFileTests(KindCase):
    # -- the real record builder (lib/trial_record.py, lane L2): every read of a file the worker can write is a read_plain read
    APPROVED = "APPROVED\nRESULT_STATUS: complete\n## Coverage\nReviewed lib/trial_record.py in full.\n"

    def real_fixture(self):
        """A finished task with every file the real builder reads, a reviewer that approved it, and a Claude transcript named by
        transcript-path. Returns (task dir, state dir, reviewer dir, transcript file, a function that builds the record)."""
        rdir, info, pane, task = self.setup_run()
        state = rdir / "state"
        reviewer = state / "tasks" / "trial-rev"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "trial-rev", "worker": "pi", "model": "rev-model", "thinking": "high"}))
        (reviewer / "result.md").write_text(self.APPROVED)
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "role": "coder", "worker": "pi", "model": MODEL, "thinking": "medium",
                                                    "review": "trial-rev", "reviews": ["trial-rev"], "created": "2026-10-31T10:00:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": "completed", "updated": "2026-10-31T10:05:00Z", "turns": 3}))
        (task / "dispatch.json").write_text(json.dumps({"base_commit": self.base, "context": ["a.py"]}))
        (task / "exit").write_text("0\n")
        (task / "request.md").write_text("Do a thing\n")
        (task / "result.md").write_text("Done <think>oops</think>\n")
        (task / "report.md").write_text("report\n")
        (task / "docs-calls.jsonl").write_text(json.dumps({"tool": "docs", "args": {"package": "p", "symbol": "s"}, "hit": True, "bytes": 5}) + "\n")
        transcript = self.root / "claude-transcript.jsonl"
        transcript.write_bytes((ROOT / "tests" / "fixtures" / "trial" / "claude_transcript.jsonl").read_bytes())
        (task / "transcript-path").write_text(str(transcript))

        def build():
            return trial_record.build(task, "qwen", "shadow", "p-1", state_dir=state)
        return task, state, reviewer, transcript, build

    def test_the_real_record_builder_opens_no_file_of_the_task_or_its_reviewer_itself(self):
        """Whatever it reads of the task, the reviewer and the transcript goes through trial_session.read_plain: no `open`, no
        Path.read_text, no Path.open on a path under the state dir or on the transcript."""
        task, state, reviewer, transcript, build = self.real_fixture()
        (reviewer / "acceptance-items.json").write_text('{"items": [1]}')
        roots = (str(state), str(transcript))
        opened = []
        real_open = io.open

        def spy(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and os.fsdecode(file).startswith(roots):
                opened.append(os.fsdecode(file))
            return real_open(file, *args, **kwargs)
        with mock.patch("builtins.open", spy), mock.patch("io.open", spy):
            record = build()
        self.assertEqual(opened, [])
        self.assertEqual((record["task_id"], record["state"], record["exit"], record["reviewer_model"]), ("trial-live", "completed", 0, "rev-model"))

    def test_the_real_record_builders_read_of_the_task_files(self):
        task, state, reviewer, transcript, build = self.real_fixture()

        def no_record(got):
            return got is None or isinstance(got, trial_record.RecordError)
        self.matrix("real meta.json", task / "meta.json", (task / "meta.json").read_bytes(), build,
                    lambda got: isinstance(got, dict) and got["model"] == MODEL, no_record, restore=False)
        (task / "meta.json").write_text(json.dumps({"id": "trial-live", "role": "coder", "worker": "pi", "model": MODEL, "thinking": "medium",
                                                    "review": "trial-rev", "reviews": ["trial-rev"], "created": "2026-10-31T10:00:00Z"}))
        for name, ok, refused, malformed in (
                ("dispatch.json", lambda got: got["base_commit"] == self.base, lambda got: got["base_commit"] is None, True),
                ("state.json", lambda got: got["state"] == "completed", lambda got: got["state"] is None, True),
                ("exit", lambda got: got["exit"] == 0, lambda got: got["exit"] is None, False),
                ("docs-calls.jsonl", lambda got: got["docs_calls"]["count"] == 1, lambda got: got["docs_calls"]["count"] == 0, True),
                ("result.md", lambda got: got["rule_slips"]["by_rule"].get("think_leak") == 1,
                 lambda got: "think_leak" not in got["rule_slips"]["by_rule"], False),
                ("transcript-path", lambda got: got["transcript"]["status"] == "ok", lambda got: got["transcript"]["status"] == "missing", False)):
            with self.subTest(file=name):
                self.matrix("real " + name, task / name, (task / name).read_bytes(), lambda: build(), ok, refused, malformed=malformed)
        for name, ok, refused in (
                ("meta.json", lambda got: got["reviewer_model"] == "rev-model", lambda got: got["reviewer_model"] is None),
                ("acceptance-items.json", lambda got: got["first_review_verdict"] == "incomplete", lambda got: got["first_review_verdict"] == "APPROVED")):
            if name == "acceptance-items.json":
                (reviewer / name).write_text('{"items": [1]}')
            with self.subTest(reviewer_file=name):
                self.matrix("real reviewer " + name, reviewer / name, (reviewer / name).read_bytes(), lambda: build(), ok, refused, malformed=False)
            if name == "acceptance-items.json":
                (reviewer / name).unlink()
        self.matrix("real reviewer result.md", reviewer / "result.md", self.APPROVED.encode(), lambda: build(),
                    lambda got: got["first_review_verdict"] == "APPROVED", lambda got: got["first_review_verdict"] is None, malformed=False)
        with mock.patch.object(trial_scan, "MAX_TRANSCRIPT_BYTES", LIMIT):
            self.matrix("real claude transcript", transcript, transcript.read_bytes(), lambda: build(),
                        lambda got: got["transcript"]["status"] == "ok", lambda got: got["transcript"]["status"] != "ok", malformed=False)

    def test_the_records_file_is_appended_to_without_waiting_for_a_reader(self):
        """L2's append opens records.jsonl and its lock for writing, which waits at a FIFO: `append_record` looks at both names first."""
        records = trial_record.trial_dir(self.repo) / "records.jsonl"
        records.parent.mkdir(parents=True, exist_ok=True)

        def append():
            ts.append_record(trial_record, self.repo, {"x": 1})
            return records.read_text()
        for name in ("records.jsonl", "records.jsonl.lock"):
            for kind in ("plain", "fifo", "symlink", "directory"):
                with self.subTest(name=name, kind=kind):
                    for leftover in (records, records.with_name("records.jsonl.lock")):
                        if os.path.lexists(str(leftover)):
                            self.clear(leftover)
                    stop = self.put(records.with_name(name), kind, b"")
                    try:
                        got = self.observe(append)
                    finally:
                        stop()
                    if kind == "plain":
                        self.assertEqual(got, '{"x":1}\n')
                    else:
                        self.assertIsInstance(got, ts.ShadowError)
                        self.assertIn("not a plain file", str(got))
        for leftover in (records, records.with_name("records.jsonl.lock")):
            if os.path.lexists(str(leftover)):
                self.clear(leftover)
