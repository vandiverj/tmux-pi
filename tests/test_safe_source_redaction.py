"""Which redaction rules apply to a line is decided by the real file it came from, never by its display text (F-GREP).

Every read/search tool, in both runtimes (Claude MCP: claude/mcp/safe.mjs; Pi: agent/extensions/safe.ts through
tests/safe_runtime.mjs), over .env, .env.local, config/local.json and one ordinary file, by absolute, relative,
`./`-prefixed and symlinked-directory paths, and through a link with an ordinary name that points at .env. The decoys
are made up here and only match the config-file rules, so a line whose source is missed comes back unredacted.
The scratch folders carry a hyphen, as the grep-line parser once split paths there.

The path of a line reaches the model too, so it is redacted as text: a token-shaped directory name never shows (F1).
A link re-pointed from a protected file to an ordinary one between the tool's read and its redaction still gets the
protected file's rules, and a file that changed during the call is withheld (F2).
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from redact import Redactor  # noqa: E402
from redact_hook import PostRedactor, bash_searches, post_result  # noqa: E402
from redaction_store import Store  # noqa: E402
from test_pi_safe_tools import pi_package  # noqa: E402

NODE = shutil.which("node")
DECOY = "decoy0000"
PLAIN = "api_token=plain0000note"
OPEN = "<" + "REDACTED:"  # a placeholder's start; split so this file holds no placeholder-shaped text
# Lowercase and YAML-style pairs: only a config-file source redacts them. FAKE_TOKEN lines are what HL1 saw leak.
FILES = {
    ".env": "FAKE_TOKEN=decoy0000env\napi_token=decoy0000envlow\n",
    ".env.local": "FAKE_TOKEN=decoy0000local\napi_token=decoy0000locallow\n",
    "config/local.json": '{\n  "apiToken": "decoy0000json"\n}\n  api_key: decoy0000jsonyaml\n',
    "notes.txt": PLAIN + "\n",
}
PROTECTED = {".env", ".env.local", "config/local.json"}
MODES = ("on", "off")
HELD = re.compile(re.escape(OPEN) + r"[a-z0-9_]+>")
HEAD = re.compile(r"(.+?):\d+:")  # the path part of a grep line (a placeholder holds a colon, never `:N:`)


def shows(line, path, start="", rest=None):
    """Whether `line` is `path` + ":" + text starting with `start` (or equal to `rest`). The path is redacted as text (a
    random-looking scratch folder comes back as a placeholder, as any secret-shaped directory name must), so a placeholder
    may stand for parts of it."""
    for end in (i for i, c in enumerate(line) if c == ":"):
        after = line[end + 1:]
        if (after == rest if rest is not None else after.startswith(start)) and \
                re.fullmatch(".+".join(map(re.escape, HELD.split(line[:end]))), path):
            return True
    return False


def build(base):
    proj = base / "my-proj"
    for name, text in FILES.items():
        (proj / name).parent.mkdir(parents=True, exist_ok=True)
        (proj / name).write_text(text)
    (base / "linked-dir").symlink_to(proj, target_is_directory=True)
    (base / "alias").mkdir()
    (base / "alias/settings.txt").symlink_to(proj / ".env")
    (base / "alias/.env").symlink_to(proj / "notes.txt")  # the other way round: the name is the stricter one
    (base / "home").mkdir()


def table(base):
    """[(label, tool, input, kind, display)]: kind is protected, plain or tree; display is the path the tool was given,
    made absolute from the working directory as the tools do (links in it kept)."""
    rows = []

    def row(label, tool, path, kind):
        args = {"path": path} if tool == "safe_read" else {"pattern": "0000", "path": path}
        rows.append((label, tool, args, kind, os.path.normpath(os.path.join(str(base), path))))

    for name in FILES:
        kind = "protected" if name in PROTECTED else "plain"
        for form, path in (("absolute", str(base / "my-proj" / name)), ("relative", "my-proj/" + name),
                           ("dot", "./my-proj/" + name), ("linked dir", "linked-dir/" + name)):
            for tool in ("safe_read", "safe_grep"):
                row("%s %s %s" % (tool, form, name), tool, path, kind)
    for form, path in (("relative", "alias/settings.txt"), ("absolute", str(base / "alias/settings.txt"))):
        for tool in ("safe_read", "safe_grep"):
            row("%s ordinary-named link %s" % (tool, form), tool, path, "protected")
    for form, path in (("absolute", str(base / "my-proj")), ("relative", "my-proj"), ("dot", "./my-proj"),
                       ("linked dir", "linked-dir")):
        row("safe_grep tree " + form, "safe_grep", path, "tree")
    return rows


class SourceTable:
    """Mixin: run the table in one runtime (`outputs`), then check every row."""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="tp-src-")
        cls.addClassCleanup(temp.cleanup)
        cls.base = Path(os.path.realpath(temp.name))
        build(cls.base)
        cls.rows = table(cls.base)
        cls.results = cls.outputs(cls.base, cls.rows)

    def test_no_decoy_from_a_protected_source_and_the_ordinary_file_unchanged(self):
        for mode in MODES:
            for (label, tool, _, kind, display), (ok, text) in zip(self.rows, self.results[mode]):
                with self.subTest(mode=mode, row=label):
                    self.assertTrue(ok, text)
                    self.assertNotIn(DECOY, text)
                    if kind == "protected":
                        self.assertIn(OPEN, text)
                        if tool == "safe_grep":  # the lines came back, redacted, under the path the tool was given
                            self.assertTrue(all(shows(line, display) for line in text.splitlines()), text)
                    elif kind == "plain":
                        if tool == "safe_read":
                            self.assertIn("     1\t" + PLAIN, text)
                        else:
                            self.assertTrue(shows(text, display, rest="1:" + PLAIN), text)
                    else:
                        lines = text.splitlines()
                        self.assertTrue(any(shows(line, display + "/notes.txt", rest="1:" + PLAIN) for line in lines), text)
                        self.assertTrue(any(shows(line, display + "/.env", "1:FAKE_TOKEN=" + OPEN) for line in lines), text)
                        self.assertTrue(any(shows(line, display + "/config/local.json", "4:  api_key: " + OPEN)
                                            for line in lines), text)


def mcp_calls(base, calls, mode, server=ROOT / "claude/mcp/safe.mjs", extra=None):
    """[(ok, text)] for each (tool, arguments) in `calls`, from one Claude MCP server run in `base`."""
    requests = [{"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}]
    requests += [{"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": tool, "arguments": args}}
                 for n, (tool, args) in enumerate(calls, 1)]
    env = {"PATH": os.environ["PATH"], "HOME": str(base / "home"), "TP_REDACTION": mode,
           "TP_REDACTION_DIR": str(base / "store"), "TP_REDACTION_LAUNCH": "source-table",
           "TP_REDACTION_LLM_URL": "http://127.0.0.1:9", **(extra or {})}
    run = subprocess.run([NODE, str(server)], cwd=base, env=env, text=True, timeout=120,
                         input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True)
    replies = {m["id"]: m["result"] for m in map(json.loads, run.stdout.splitlines())}
    return [(not replies[n].get("isError"), replies[n]["content"][0]["text"]) for n in range(1, len(calls) + 1)]


def pi_calls(base, calls, modes, name="calls", agent_dir=None, extra=None):
    """{mode: [(ok, text)]} for each (tool, input) in `calls`, from the Pi extension (tests/safe_runtime.mjs)."""
    package = pi_package()
    if not package or not (package / "dist/index.js").exists():
        raise unittest.SkipTest("installed Pi package not found")
    spec, out = base / (name + ".json"), base / (name + "-results.json")
    # A call (tool, input, text) hands `text` to the tool_result pass as that tool's output instead of running it.
    spec.write_text(json.dumps({"cwd": str(base), "modes": modes, "agent_dir": agent_dir and str(agent_dir),
                                "calls": [{"tool": call[0], "input": call[1], **({"result": call[2]} if call[2:] else {})}
                                          for call in calls]}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
    env.update(PI_CODING_AGENT_PACKAGE=str(package), SAFE_SOURCE_TABLE=str(spec), SAFE_SOURCE_OUT=str(out), **(extra or {}))
    run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/safe_runtime.mjs")], cwd=ROOT,
                         env=env, capture_output=True, text=True, timeout=300)
    if run.returncode != 0:
        raise AssertionError(run.stdout + run.stderr)
    got = json.loads(out.read_text())
    return {mode: [(r["ok"], r["text"]) for r in got if r["mode"] == mode] for mode in modes}


@unittest.skipUnless(NODE, "node is not installed")
class ClaudeMcpTable(SourceTable, unittest.TestCase):
    @classmethod
    def outputs(cls, base, rows):
        return {mode: mcp_calls(base, [(tool, args) for _, tool, args, _, _ in rows], mode) for mode in MODES}


@unittest.skipUnless(NODE, "node is not installed")
class PiExtensionTable(SourceTable, unittest.TestCase):
    @classmethod
    def outputs(cls, base, rows):
        return pi_calls(base, [(tool, args) for _, tool, args, _, _ in rows], MODES)


# Stands in for bin/tp-redact: before redacting, it re-points the first link in SWAP_LINKS that still leads to SWAP_FROM
# at SWAP_TO, so the file changes between the tool's read (or search) and its redaction. With SWAP_BACK it points the link
# back afterwards, so only the redactor could see the change.
SWAPPER = '''#!/usr/bin/env python3
import os, subprocess, sys
def point(link, to):
    os.symlink(to, link + ".new")
    os.replace(link + ".new", link)
link = next((l for l in os.environ["SWAP_LINKS"].split(os.pathsep) if os.readlink(l) == os.environ["SWAP_FROM"]), None)
if link:
    point(link, os.environ["SWAP_TO"])
code = subprocess.run([%r] + sys.argv[1:]).returncode
if link and os.environ.get("SWAP_BACK"):
    point(link, os.environ["SWAP_FROM"])
sys.exit(code)
''' % str(ROOT / "bin/tp-redact")


@unittest.skipUnless(NODE, "node is not installed")
class SwapDuringCall(unittest.TestCase):
    """A link from a protected file to an ordinary one, re-pointed after the tool resolved and read it (F2).

    Swapped and back again before the tool looks: the redaction still follows the file read. Swapped for good: the call
    withholds its output. In both runtimes, for safe_read and safe_grep."""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="tp-swap-")
        cls.addClassCleanup(temp.cleanup)
        base = cls.base = Path(os.path.realpath(temp.name))
        build(base)
        for root in ("mcp/claude/mcp", "mcp/bin", "pi/agent", "pi/bin"):
            (base / root).mkdir(parents=True)
        shutil.copy(ROOT / "claude/mcp/safe.mjs", base / "mcp/claude/mcp/safe.mjs")  # its tp-redact is ../../bin/
        for redactor in ("mcp/bin/tp-redact", "pi/bin/tp-redact"):
            (base / redactor).write_text(SWAPPER)
            (base / redactor).chmod(0o755)
        env, plain = str(base / "my-proj/.env"), str(base / "my-proj/notes.txt")
        cls.results = {}
        for runtime in ("mcp", "pi"):
            for variant in ("back", "stay"):
                where = base / (runtime + "-" + variant)
                where.mkdir()
                links = [where / "read.txt", where / "grep.txt"] if variant == "stay" else [where / "both.txt"] * 2
                for link in set(links):
                    link.symlink_to(env)
                calls = [("safe_read", {"path": str(links[0].relative_to(base))}),
                         ("safe_grep", {"pattern": "0000", "path": str(links[1].relative_to(base))})]
                extra = {"SWAP_LINKS": os.pathsep.join(map(str, links)), "SWAP_FROM": env, "SWAP_TO": plain,
                         **({"SWAP_BACK": "1"} if variant == "back" else {})}
                try:
                    # One MCP server per call: the server handles requests concurrently, and two calls swapping one link
                    # at once would race each other rather than test the tool.
                    got = ([mcp_calls(base, [call], "on", base / "mcp/claude/mcp/safe.mjs", extra)[0] for call in calls]
                           if runtime == "mcp" else
                           pi_calls(base, calls, ["on"], "swap-" + variant, base / "pi/agent", extra)["on"])
                except unittest.SkipTest as skip:
                    got = skip
                cls.results[runtime, variant] = (links, got)

    def outputs(self, runtime, variant):
        links, got = self.results[runtime, variant]
        if isinstance(got, unittest.SkipTest):
            self.skipTest(str(got))
        return links, got

    def test_the_redaction_follows_the_file_read_not_where_the_name_leads_later(self):
        for runtime in ("mcp", "pi"):
            links, ((read_ok, read), (grep_ok, grep)) = self.outputs(runtime, "back")
            with self.subTest(runtime=runtime):
                self.assertTrue(read_ok and grep_ok, (read, grep))
                for text in (read, grep):
                    self.assertNotIn(DECOY, text)
                    self.assertIn(OPEN, text)
                self.assertTrue(all(shows(line, str(links[1])) for line in grep.splitlines()), grep)

    def test_a_file_changed_during_the_call_is_withheld(self):
        for runtime in ("mcp", "pi"):
            _, got = self.outputs(runtime, "stay")
            for tool, (ok, text) in zip(("safe_read", "safe_grep"), got):
                with self.subTest(runtime=runtime, tool=tool):
                    self.assertFalse(ok, text)
                    self.assertIn("changed during the call", text)
                    self.assertNotIn(DECOY, text)


class SourceOfALine(unittest.TestCase):
    """tp-redact's side, in process: the source is the real file behind the path the search tool printed."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="tp-src-")
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        build(self.base)
        self.post = PostRedactor(Redactor(Store(persist=False)), str(self.base))

    def redact(self, text, null):
        (self.post.collect_sourced if null else self.post.collect)(text)
        self.post.resolve()
        return self.post.render(self.post.entries[-1])

    def test_the_path_before_the_nul_names_the_source(self):
        env, alias = str(self.base / "my-proj/.env"), "alias/settings.txt"
        text = "{0}\x001:FAKE_TOKEN=decoy0000env\n{0}\x002:api_token=decoy0000envlow\n{1}\x002:api_token=decoy0000envlow\n" \
               "my-proj/notes.txt\x001:{2}\nalias/.env\x001:{2}\n".format(env, alias, PLAIN)
        out = self.redact(text, null=True)
        self.assertNotIn(DECOY, out)
        lines = out.splitlines()
        self.assertTrue(shows(lines[0], env, "1:FAKE_TOKEN=" + OPEN + "fake_token_"), lines[0])
        self.assertTrue(lines[2].startswith(alias + ":2:api_token=" + OPEN), lines[2])
        self.assertEqual(lines[3], "my-proj/notes.txt:1:" + PLAIN)
        self.assertTrue(lines[4].startswith("alias/.env:1:api_token=" + OPEN), lines[4])  # never weaker than the name
        sourced = [f for f in self.post.findings if f["source"]]  # the others are parts of a path, redacted as text
        self.assertEqual(sourced[0]["source"], env)
        self.assertEqual(sourced[2]["source"], env)  # the link's pointer is the file it leads to

    def test_a_line_whose_file_cannot_be_established_is_withheld(self):
        missing = str(self.base / "my-proj/gone.txt")
        for text in ("api_token=decoy0000x\n", missing + "\x001:api_token=decoy0000x\n", str(self.base / "my-proj") +
                     "\x001:api_token=decoy0000x\n", str(self.base / "my-proj/.env") + "\x00api_token=decoy0000x\n"):
            with self.subTest(text=text.split("\x00")[0]):
                out = self.redact(text, null=True)
                self.assertNotIn(DECOY, out)
                self.assertIn("withheld", out)

    def test_display_text_grep_lines_use_the_real_file(self):
        # Claude's own Grep results and Pi's grep tool_result still arrive as text: a hyphen in the path, or a link with
        # an ordinary name, must not hide which file the line came from.
        text = "{0}:2:api_token=decoy0000envlow\nalias/settings.txt:1:FAKE_TOKEN=decoy0000env\n" \
               "my-proj/config/local.json-4-  api_key: decoy0000jsonyaml\nmy-proj/notes.txt:1:{1}".format(
                   self.base / "my-proj/.env", PLAIN)
        out = self.redact(text, null=False)
        self.assertNotIn(DECOY, out)
        self.assertTrue(shows(out.splitlines()[0], str(self.base / "my-proj/.env"), "2:api_token=" + OPEN), out)
        self.assertEqual(out.splitlines()[-1], "my-proj/notes.txt:1:" + PLAIN)

    def test_a_secret_shaped_directory_name_is_hidden_at_every_display_site(self):
        # A path from tool output reaches the model at several sites; one function (PostRedactor.shown) makes its
        # displayed text, so a segment hidden at one is hidden at all (F1, rounds 2 and 3). Segments: a known token format,
        # and an unverified random-looking name (round 2 left the latter in Claude's pointers). Each sits above a protected
        # file and above an ordinary one.
        segments = {"known format": "ghp" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",  # decoys built here
                    "unverified": "Qx7Lm2Vt9Rk4Wp8Zn3Hs6Jd"}
        rg = shutil.which("rg")
        for kind, segment in segments.items():
            tree = self.base / segment
            for name in ("config/local.json", "notes.txt"):
                (tree / name).parent.mkdir(parents=True, exist_ok=True)
                (tree / name).write_text(FILES[name])
            files = [str(tree / "config/local.json"), str(tree / "notes.txt")]
            search = ([rg, "--no-config", "--null", "-n", "--no-heading", "--with-filename", "-e", "0000", "--", str(tree)]
                      if rg else ["grep", "--null", "-rn", "-e", "0000", "--", str(tree)])
            found = subprocess.run(search, capture_output=True, text=True, check=True).stdout
            for site, (text, post, heads, pointers) in self.display_sites(tree, found).items():
                with self.subTest(segment=kind, site=site):
                    self.assertNotIn(segment, text)
                    self.assertNotIn(DECOY, text)
                    self.assertNotIn("withheld", text)
                    shown = {post.shown(path): path for path in files}  # what the shared function makes of each path
                    for head in heads:  # each head is that function's output for one of the files
                        self.assertIn(head, shown, text)
                    for pointer in pointers:
                        self.assertIn(pointer, shown, text)
                    if heads:
                        self.assertEqual(len(heads), 3, text)  # local.json lines 2 and 4, notes.txt line 1
                        self.assertIn(":1:" + PLAIN, text)  # the ordinary body unchanged
                    if site.endswith("additionalContext"):
                        self.assertTrue(pointers, text)

    def display_sites(self, tree, found):
        """{site: (text shown to the model, its PostRedactor, path heads of its grep lines, paths in its pointers)}."""
        sites = {}
        for route, text in (("rg --null route", found), ("plain-text route", found.replace("\0", ":"))):
            post = PostRedactor(Redactor(Store(persist=False)), str(self.base))
            (post.collect_sourced if route.startswith("rg") else post.collect)(text)
            post.resolve()
            out = post.render(post.entries[-1])
            sites[route] = (out, post, [HEAD.match(line).group(1) for line in out.splitlines()], [])
        posts = []

        class Spy(PostRedactor):  # the hook's own PostRedactor, kept so its shown() output can be compared
            def __init__(self, *args):
                super().__init__(*args)
                posts.append(self)

        events = {"Claude Grep": ("Grep", {"pattern": "0000", "path": str(tree)},
                                  {"mode": "content", "content": found.replace("\0", ":"), "numLines": 3}),
                  "Claude Read": ("Read", {"file_path": str(tree / "config/local.json")},
                                  {"type": "text", "file": {"filePath": str(tree / "config/local.json"),
                                                            "content": FILES["config/local.json"], "numLines": 4,
                                                            "startLine": 1, "totalLines": 4}})}
        for name, (tool, params, response) in events.items():
            event = {"tool_name": tool, "cwd": str(self.base), "tool_input": params, "tool_response": response}
            with unittest.mock.patch("redact_hook.PostRedactor", Spy):
                output = post_result(event, Redactor(Store(persist=False)), "on")[0]["hookSpecificOutput"]
            updated = json.dumps(output["updatedToolOutput"])
            heads = [HEAD.match(line).group(1) for line in output["updatedToolOutput"]["content"].splitlines()] \
                if tool == "Grep" else []
            pointers = [where.rsplit(":", 1)[0] for where in re.findall(r"\(([^()]*:\d+)\)", output["additionalContext"])]
            if heads:  # Read output shows no path of its own (its filePath echoes the call; a KEEP_KEYS field)
                sites[name + " output"] = (updated, posts[-1], heads, [])
            sites[name + " additionalContext"] = (output["additionalContext"], posts[-1], [], pointers)
        return sites

    def test_a_link_swapped_after_the_search_keeps_the_rules_of_the_file_searched(self):
        # The step the reviewer hooked: the link was resolved and searched while it led to .env, and leads to an ordinary
        # file by the time its lines are redacted. The caller hands over the real path it searched; it is not resolved again.
        link, env = self.base / "alias/swap.txt", str(self.base / "my-proj/.env")
        link.symlink_to(env)
        real = os.path.realpath(link)
        link.unlink()
        link.symlink_to(self.base / "my-proj/notes.txt")
        self.post.collect_sourced(real + "\x002:api_token=decoy0000envlow\n", str(link), real)
        self.post.resolve()
        out = self.post.render(self.post.entries[-1])
        self.assertNotIn(DECOY, out)
        self.assertTrue(shows(out, str(link), "2:api_token=" + OPEN), out)  # shown under the name the call gave
        self.assertEqual(self.post.findings[-1]["source"], env)

    def test_a_claude_read_through_an_ordinary_named_link_uses_the_env_rules(self):
        alias = str(self.base / "alias/settings.txt")
        content = FILES[".env"]
        event = {"tool_name": "Read", "cwd": str(self.base), "tool_input": {"file_path": alias},
                 "tool_response": {"type": "text", "file": {"filePath": alias, "content": content, "numLines": 2,
                                                            "startLine": 1, "totalLines": 2}}}
        output, fields = post_result(event, Redactor(Store(persist=False)), "on")
        self.assertNotIn(DECOY, json.dumps(output))
        # Path segments in the pointer are findings too (source None); every value from the file is judged as .env.
        self.assertEqual({f["source"] for f in fields["findings"] if f["source"]}, {str(self.base / "my-proj/.env")})


class PlainTextHeads(unittest.TestCase):
    """Plain-text grep lines (Claude Grep, Claude and Pi Bash running grep, Pi grep, Pi's second pass over safe_grep) fail
    closed (round 4).

    A head is found at any `:` or `-`, with no cap but the OS path limit; when two splits name files, the stricter rules
    win; a line whose file cannot be established is judged by the strictest rules, never ordinary ones. An ordinary file's
    line whose head parses keeps ordinary rules. All rows go through each route as one output.

    Round 5: a prefix is a head only when a decimal line number follows it (`:N:`, `-N-`); an ordinary file's name
    followed by anything else claims nothing (F1). Whether Bash output is grep output is decided by parsing the command,
    failing closed when that is not certain (F2)."""

    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="tp-heads-")
        cls.addClassCleanup(temp.cleanup)
        base = cls.base = Path(os.path.realpath(temp.name))
        limit = os.pathconf("/", "PC_PATH_MAX")  # the OS path limit
        over_300 = "/".join(["d" * 100] * 3)
        room = min(limit, 1024) - len(str(base)) - len("/local.json") - 4  # a path over 1000 characters, under the limit
        over_1000 = "/".join(["e" * 200] * (room // 201) + ["e" * (room % 201 or 1)])
        held, plain = ":4:  api_key: decoy0000", ":1:" + PLAIN
        notes = "my-proj/notes.txt"  # an ordinary file (the "ordinary" row makes it)
        # (label, path under base, the rest of its line from the separator on, expected: held or plain, file made)
        cls.rows = [
            ("one colon, protected", "one:colon/local.json", held + "one", "held", True),
            ("one colon, ordinary", "one:colon/notes.txt", plain, "plain", True),
            ("several colons, protected", "a:1:b:2:c/local.json", held + "several", "held", True),
            ("several colons, ordinary", "a:1:b:2:c/notes.txt", plain, "plain", True),
            ("over 300, protected", over_300 + "/local.json", held + "long", "held", True),
            ("over 300, ordinary", over_300 + "/notes.txt", plain, "plain", True),
            ("over 1000, protected", over_1000 + "/local.json", held + "longer", "held", True),
            ("over 1000, ordinary", over_1000 + "/notes.txt", plain, "plain", True),
            # `pair` is an ordinary file and `pair:1:x/local.json` a protected one: both splits name a file.
            ("two splits, protected wins", "pair:1:x/local.json", held + "two", "held", True),
            ("past the path limit", "f" * limit + "/local.json", held + "far", "held", False),
            ("unsourced", "gone/local.json", held + "gone", "held", False),
            ("ordinary", notes, plain, "plain", True),
            # Round 5 (F1): an ordinary file's name is a head only if a decimal line number follows it.
            ("reviewer: ordinary name, then a path", notes + ":missing/local.json", held + "rev", "held", False),
            ("ordinary name, then a word", notes, ":x4:  api_key: decoy0000word", "held", False),
            ("ordinary name, then 0x4", notes, ":0x4:  api_key: decoy0000hex", "held", False),
            ("ordinary name, then a non-ASCII digit", notes, ":\u0664:  api_key: decoy0000digit", "held", False),
            ("ordinary name, mixed separators", notes, ":4-  api_key: decoy0000mixed", "held", False),
            ("ordinary name, then a path with a column", notes + ":gone/local.json", ":4:7:  api_key: decoy0000col",
             "held", False),
            ("ordinary name, then a context line's path", notes + "-gone/local.json", "-4-  api_key: decoy0000ctx",
             "held", False),
            ("a directory as head", "my-proj", held + "dir", "held", False),
            ("column shape, ordinary", notes, ":1:5:" + PLAIN, "plain", False),
            ("context shape, ordinary", notes, "-1-" + PLAIN, "plain", False),
        ]
        (base / "pair").write_text(PLAIN + "\n")
        for _, path, line, _, made in cls.rows:
            if made:
                (base / path).parent.mkdir(parents=True, exist_ok=True)
                (base / path).write_text(line[1:] + "\n")
        assert len(str(base / over_1000 / "notes.txt")) > 1000
        (base / "cfg").mkdir()
        (base / "cfg/local.json").write_text("{}\n")  # a protected file for the generated and command checks
        cls.text = "\n".join("{}/{}{}".format(base, path, line) for _, path, line, _, _ in cls.rows)
        cls.outputs = {"Claude Grep": cls.claude("Grep", {"pattern": "0000", "path": str(base), "output_mode": "content"},
                                                 {"mode": "content", "content": cls.text, "numLines": len(cls.rows)}),
                       "Claude Bash grep": cls.claude("Bash", {"command": "grep -rn 0000 " + str(base)},
                                                      {"stdout": cls.text, "stderr": "", "interrupted": False})}
        try:
            pi = pi_calls(base, [("grep", {"pattern": "0000", "path": str(base)}, cls.text),
                                 ("safe_grep", {"pattern": "0000", "path": str(base)}, cls.text),
                                 ("bash", {"command": "grep -rn 0000 " + str(base)}, cls.text)], ["on"], "heads")["on"]
            cls.outputs.update({"Pi grep": pi[0][1], "Pi safe_grep second pass": pi[1][1], "Pi bash grep": pi[2][1]})
        except unittest.SkipTest as skip:
            cls.pi_skipped = str(skip)

    @classmethod
    def claude(cls, tool, params, response):
        event = {"tool_name": tool, "cwd": str(cls.base), "tool_input": params, "tool_response": response}
        output = post_result(event, Redactor(Store(persist=False)), "on")[0]
        out = output["hookSpecificOutput"]["updatedToolOutput"] if output else response
        return out["content"] if tool == "Grep" else out["stdout"]

    def test_each_plain_text_route_judges_every_head_shape_and_fails_closed(self):
        if hasattr(self, "pi_skipped"):
            self.assertEqual(set(self.outputs), {"Claude Grep", "Claude Bash grep"}, self.pi_skipped)
        for route, out in self.outputs.items():
            lines = out.split("\n")
            self.assertEqual(len(lines), len(self.rows), (route, out))
            for (label, _, line, expected, _), got in zip(self.rows, lines):
                with self.subTest(route=route, row=label):
                    if expected == "held":
                        self.assertNotIn(DECOY, got)
                        self.assertIn(OPEN, got)
                    else:  # the head parsed, so the ordinary file's own (looser) rules kept its line
                        self.assertTrue(got.endswith(line), got)

    def test_every_head_shape_with_a_protected_decoy_is_held(self):
        # Generated: every separator shape after every kind of prefix, the body a protected decoy. Whatever the prefix
        # (a protected file, a missing one, an ordinary name before a missing one, a directory, an ordinary file with no
        # line number), the decoy never shows in clear. An ordinary file followed by a valid head is the one prefix left
        # out: its line is its own and keeps ordinary rules (the "plain" rows above).
        valid = [":4:", ":4:7:", ":4:120:", "-4-", "-4-7-"]
        invalid = [":", "-", ":x4:", ":0x4:", ":\u0664:", ":4-", "-4:", ": 4:", ":4 :"]
        prefixes = {"protected file": ("cfg/local.json", valid + invalid),
                    "missing file": ("gone/local.json", valid + invalid),
                    "ordinary name before a missing file": ("my-proj/notes.txt:gone/local.json", valid + invalid),
                    "ordinary name before a missing file (context)": ("my-proj/notes.txt-gone/local.json", valid + invalid),
                    "directory": ("my-proj", valid + invalid),
                    "ordinary file, no line number": ("my-proj/notes.txt", invalid)}
        lines = [(label, shape, "{}/{}{}  api_key: decoy0000gen{}".format(self.base, prefix, shape, n))
                 for label, (prefix, shapes) in prefixes.items() for n, shape in enumerate(shapes)]
        post = PostRedactor(Redactor(Store(persist=False)), str(self.base))
        post.collect("\n".join(line for _, _, line in lines), grep=True)
        post.resolve()
        out = post.render(post.entries[0]).split("\n")
        self.assertEqual(len(out), len(lines))
        for (label, shape, _), got in zip(lines, out):
            with self.subTest(prefix=label, shape=shape):
                self.assertNotIn(DECOY, got)

    def test_search_commands_are_read_by_one_fail_closed_detector(self):
        # F2: Claude Bash (post_result) and Pi bash (tp-redact --command, called in process) decide by one parser
        # whether the output is grep lines. A search, however it is invoked, and a command that cannot be read for
        # certain, fail closed: the unsourced line is held. A command that is neither keeps ordinary rules.
        line = "{}/gone/local.json:4:  api_key: decoy0000cmd".format(self.base)
        rows = [("git -C . grep -n api_key", "held"), ("git -c core.quotepath=off grep -n api_key", "held"),
                ("git --no-pager grep -n api_key", "held"), ("/usr/bin/grep -rn api_key .", "held"),
                ("env X=1 rg -n api_key", "held"), ("find . -type f | xargs grep -n api_key", "held"),
                ("timeout 5 rg -n api_key", "held"), ("sh -c 'grep -rn api_key .'", "held"),
                ("cat cfg/local.json | grep -n api_key", "held"), ("grep -rn api_key . && echo done", "held"),
                ("$(echo grep) -rn api_key .", "held"), ("eval 'grep -rn api_key .'", "held"),
                ("timeout -s KILL 5 rg -n api_key", "held"), ("bash -o pipefail -c 'rg -n api_key'", "held"),
                ("find . -type f -exec grep -Hn api_key {} +", "held"), ("git -c alias.s=grep s -n api_key", "held"),
                ("echo grep -rn api_key . | sh", "held"), ("2>/dev/null grep -rn api_key .", "held"),
                ("grep -rn 'api_key", "held"),
                *(("env " + " ".join("X{}=1".format(i) for i in range(count)) + " rg -n api_key .", "held")
                  for count in (65, 10_000)),
                ("ls", "plain"), ("git status", "plain")]
        tp_redact = load_tp_redact()
        for command, expected in rows:
            event = {"tool_name": "Bash", "cwd": str(self.base), "tool_input": {"command": command},
                     "tool_response": {"stdout": line, "stderr": "", "interrupted": False}}
            output = post_result(event, Redactor(Store(persist=False)), "on")[0]
            outs = {"Claude Bash": output["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if output else line,
                    "tp-redact --command": tp_redact(["--stdin", "--no-store", "--command", command, "--cwd",
                                                      str(self.base)], line)}
            for route, got in outs.items():
                with self.subTest(command=command, route=route):
                    if expected == "held":
                        self.assertNotIn(DECOY, got)
                    else:  # ordinary rules: the body is kept (the random temp folder in the path is redacted as text)
                        self.assertTrue(got.endswith(":4:  api_key: decoy0000cmd"), got)
        for tool in ("BashOutput", "TaskOutput"):  # a background shell's output: no command to read, so fail closed
            with self.subTest(tool=tool):
                event = {"tool_name": tool, "cwd": str(self.base), "tool_input": {"bash_id": "bg1"},
                         "tool_response": {"stdout": line, "stderr": "", "status": "completed"}}
                output = post_result(event, Redactor(Store(persist=False)), "on")[0]
                self.assertNotIn(DECOY, output["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if output else line)

    def test_parser_limits_choose_grep_mode_even_without_a_visible_search(self):
        # The last command may be past the wrapper's scan limit (64 operands: assignments plus the command's words).
        # A complete parse at the limit stays ordinary: 62 assignments + `echo ok` is 64 operands.
        self.assertFalse(bash_searches("env " + "X=1 " * 62 + "echo ok"))
        self.assertTrue(bash_searches("env " + "X=1 " * 63 + "echo ok"))  # 65 operands
        for count in (64, 65, 10_000):
            with self.subTest(assignments=count):
                self.assertTrue(bash_searches("env " + "X=1 " * count + "echo ok"))
        self.assertTrue(bash_searches("echo ok", depth=9))  # the recursion limit also fails closed

    def test_a_command_reading_one_file_uses_its_rules(self):
        # The file a non-search command reads decides the rules on both routes (Pi bash gained it in round 5). A search's
        # line with no path head is unsourced, whatever file the command names: judged by the strictest rules and from
        # after its number too (`grep -n` on stdin prints `4:  key: value`; `xargs grep -h` prints other files' lines).
        tp_redact = load_tp_redact()
        both = ("Claude Bash", "tp-redact --command")
        for command, text, routes in (("cat cfg/local.json", "  api_key: decoy0000cat", ("tp-redact --command",)),
                                      ("cat cfg/local.json | grep -n api_key", "4:  api_key: decoy0000src", both),
                                      ("cat my-proj/notes.txt | xargs grep -h api_key", "  api_key: decoy0000xargs",
                                       both)):
            for route in routes:
                with self.subTest(command=command, route=route):
                    if route == "Claude Bash":
                        event = {"tool_name": "Bash", "cwd": str(self.base), "tool_input": {"command": command},
                                 "tool_response": {"stdout": text, "stderr": "", "interrupted": False}}
                        output = post_result(event, Redactor(Store(persist=False)), "on")[0]
                        got = output["hookSpecificOutput"]["updatedToolOutput"]["stdout"] if output else text
                    else:
                        got = tp_redact(["--stdin", "--no-store", "--command", command, "--cwd", str(self.base)], text)
                    self.assertNotIn(DECOY, got)
                    self.assertIn(OPEN, got)

def load_tp_redact():
    """bin/tp-redact's main, called in process: f(argv, stdin text) -> stdout text."""
    loader = importlib.machinery.SourceFileLoader("tp_redact", str(ROOT / "bin/tp-redact"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("tp_redact", loader))
    loader.exec_module(module)

    def run(argv, text):
        stdin, stdout = io.TextIOWrapper(io.BytesIO(text.encode())), io.TextIOWrapper(io.BytesIO(), write_through=True)
        with unittest.mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
            module.main(argv)
        return stdout.buffer.getvalue().decode()
    return run


if __name__ == "__main__":
    unittest.main()
