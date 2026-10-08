"""Trust-store guard (T7 slice 3): a red-team table of ways to reach the trust store, and ordinary commands that must not be touched.

Everything runs in a throwaway home; nothing here reads or writes the owner's real configuration. The
guard takes its home as an argument, so a row in the table names the fake home through `~`, `$HOME` or
a path built from the fixture. `{TRUST}`, `{STATE}`, `{LINK}`, `{LINKCFG}`, `{HOME}`, `{CONFIG}` and `{PROJ}` are
filled in from the fixture, and so are `{TREE}` (holds a link to the store), `{TREE2}` (a link to a directory that
links to the one above the store), `{TREE3}` (a link to the home directory) and `{PLAIN}` (links, none of which
lead near the store).
"""
import ast
import contextlib
import glob
import io
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import trust_guard  # noqa: E402

CLI = str(ROOT / "bin/tp-trust-guard")
HOOK = str(ROOT / "claude/plugins/tmux-pi/hooks/trust-guard.sh")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="guardtest-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.home, ".config", "tmux-pi")
        self.trust = os.path.join(self.config, "trust")
        self.state = os.path.join(self.config, "state", "trust")
        self.proj = os.path.join(self.home, "proj")
        for d in (self.trust, self.state, self.proj, os.path.join(self.tmp, "scratch")):
            os.makedirs(d)
        with open(os.path.join(self.trust, "rec.json"), "w") as f:
            f.write("{}")
        self.link = os.path.join(self.tmp, "link")           # symlink to the trust directory
        self.linkcfg = os.path.join(self.tmp, "linkcfg")     # symlink to the directory above it
        os.symlink(self.trust, self.link)
        os.symlink(self.config, self.linkcfg)
        # Links that lead to the store, to the directory above it, and to it through another link.
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.tree, "sub", "inner"))
        os.symlink(self.trust, os.path.join(self.tree, "sub", "inner", "linked"))
        self.tree2 = os.path.join(self.tmp, "tree2")
        os.makedirs(os.path.join(self.tree2, "sub"))
        os.makedirs(os.path.join(self.tmp, "mid"))
        os.symlink(os.path.join(self.tmp, "mid"), os.path.join(self.tree2, "sub", "chain"))
        os.symlink(self.config, os.path.join(self.tmp, "mid", "deep"))
        self.tree3 = os.path.join(self.tmp, "tree3")       # a link to the home directory, which holds the store
        os.makedirs(self.tree3)
        os.symlink(self.home, os.path.join(self.tree3, "home"))
        self.plain = os.path.join(self.tmp, "plain")        # nothing in it leads anywhere near the store
        os.makedirs(os.path.join(self.plain, "a", "b"))
        for name in ("a/one.txt", "a/b/two.txt"):
            Path(self.plain, name).write_text("x")
        os.symlink(os.path.join(self.plain, "a"), os.path.join(self.plain, "a", "b", "back"))   # a loop
        os.symlink(os.path.join(self.tmp, "scratch"), os.path.join(self.config, "lnk2"))      # for a logical `cd`
        # A second home where the protected directories do not exist yet.
        self.bare = os.path.join(self.tmp, "bare")
        os.makedirs(os.path.join(self.bare, ".config"))

    def sub(self, text):
        return (text.replace("{TRUST}", self.trust).replace("{STATE}", self.state).replace("{LINK}", self.link)
                .replace("{LINKCFG}", self.linkcfg).replace("{HOME}", self.home).replace("{PROJ}", self.proj)
                .replace("{SCRATCH}", os.path.join(self.tmp, "scratch")).replace("{CONFIG}", self.config)
                .replace("{TREE2}", self.tree2).replace("{TREE3}", self.tree3).replace("{TREE}", self.tree)
                .replace("{PLAIN}", self.plain))

    def judge(self, command, cwd=None, worker=True, home=None, tool="Bash"):
        return trust_guard.judge(tool, {"command": self.sub(command)}, cwd or self.proj, home=home or self.home,
                                 worker=worker)

    def bash(self, command, cwd=None, worker=True, home=None, tool="Bash"):
        """None when allowed, else the reason to deny. An ask fails the test: asks are checked with `judge`/`assertAsk`."""
        answer = self.judge(command, cwd, worker, home, tool)
        self.assertNotEqual(answer.kind, trust_guard.ASK, "asked, not denied: %s" % command)
        return answer.reason

    def assertAsk(self, command, reason=None, **kw):
        answer = self.judge(command, **kw)
        self.assertEqual(answer.kind, trust_guard.ASK, "%s: %s" % (answer, command))
        self.assertEqual(answer.reason, reason or trust_guard.WALK_REASON)
        return answer

    def assertHardDeny(self, command, reason=None, **kw):
        """A deny, and not an ask, with this reason when one is given."""
        answer = self.judge(command, **kw)
        self.assertEqual(answer.kind, trust_guard.DENY, "%s: %s" % (answer, command))
        if reason:
            self.assertEqual(answer.reason, reason, command)
        return answer

    def deep(self, value):
        if isinstance(value, str):
            return self.sub(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        return value

    def tool(self, name, params, worker=True):
        answer = trust_guard.judge(name, self.deep(params), self.proj, home=self.home, worker=worker)
        self.assertNotEqual(answer.kind, trust_guard.ASK, "asked, not denied: %s" % name)
        return answer.reason

    def make_repo(self):
        """A repository about trust, in the fake home: the words `trust` and `tmux-pi` appear, the store is never touched."""
        repo = os.path.join(self.home, "Documents", "Projects", "tmux-pi")
        for name in ("lib/trust.py", "lib/trust_guard.py", "tests/test_trust_record.py", "tests/test_trust_guard.py",
                     "bin/tp-trust", "README.md", "claude/plugins/tmux-pi/x"):
            path = Path(repo, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x")
        Path(repo, ".git").mkdir()
        # Make the home look lived in: a node_modules-like tree full of links, none of them near the store.
        os.symlink(os.path.join(repo, "lib"), os.path.join(repo, "tests", "libs"))
        return repo

    def assertDenied(self, command, **kw):
        self.assertHardDeny(command, **kw)

    def assertAllowed(self, command, **kw):
        reason = self.bash(command, **kw)
        self.assertIsNone(reason, "denied (%s): %s" % (reason, command))


# Every way in the design's red-team list. Each one must be denied for a worker and for the main agent.
REDTEAM = [
    # redirects
    "echo x > ~/.config/tmux-pi/trust/a.json",
    "echo x >> ~/.config/tmux-pi/trust/a.json",
    "echo x &> ~/.config/tmux-pi/trust/a.json",
    "echo x 2>~/.config/tmux-pi/trust/a.json",
    "echo x > ~/.config/tmux-pi/state/trust/skipped.jsonl",
    "echo x | tee ~/.config/tmux-pi/trust/a.json",
    "echo x | tee -a {TRUST}/a.json",
    # copy, move, link, install, dd
    "cp /tmp/f {TRUST}/a.json",
    "cp -p /tmp/f ~/.config/tmux-pi/trust/",
    "mv /tmp/f {TRUST}/a.json",
    "mv {TRUST}/rec.json /tmp/rec.json",
    "ln -s /tmp/f {TRUST}/a.json",
    "ln {TRUST}/rec.json {SCRATCH}/alias.json",      # a hard link made from a record
    "ln {SCRATCH}/f {TRUST}/a.json",                 # a hard link made into the store
    "install -m 400 /tmp/f {TRUST}/a.json",
    "dd if=/dev/zero of={TRUST}/a.json",
    "dd of={TRUST}/a.json bs=1 count=1",
    "rsync -a /tmp/f {TRUST}/",
    "touch {TRUST}/a.json",
    "mkdir -p ~/.config/tmux-pi/trust/x",
    "rm {TRUST}/rec.json",
    "chmod 600 {TRUST}/rec.json",
    # in-place editors and inline scripts
    "sed -i s/a/b/ {TRUST}/rec.json",
    "sed -i '' 's/a/b/' ~/.config/tmux-pi/trust/rec.json",
    "perl -i -pe 's/a/b/' {TRUST}/rec.json",
    "python3 -c \"open('{TRUST}/a.json','w').write('x')\"",
    "python3 -c \"open('/' + 'x' + '/.config/tmux-pi/trust/a.json', 'w')\"",
    "node -e \"require('fs').writeFileSync('{TRUST}/a.json','x')\"",
    "ruby -e \"File.write('{TRUST}/a.json','x')\"",
    "perl -e 'open(F, \">{TRUST}/a.json\"); print F 1'",
    # here-documents
    "cat > {TRUST}/a.json <<EOF\n{}\nEOF",
    "cat <<'EOF' > ~/.config/tmux-pi/trust/a.json\n{}\nEOF",
    "sh <<EOF\necho x > {TRUST}/a.json\nEOF",
    "python3 - <<EOF\nopen('{TRUST}/a.json', 'w')\nEOF",
    # a directory change, then a relative name
    "cd ~/.config/tmux-pi/trust && echo x > a.json",
    "cd ~/.config/tmux-pi && echo x > trust/a.json",
    "cd ~/.config && cd tmux-pi && cd trust && touch a.json",
    "cd ~/.config; echo x > tmux-pi/trust/a.json",
    "cd ~/.config/tmux-pi; mv /tmp/f trust/a.json",
    "pushd ~/.config/tmux-pi >/dev/null; echo x > trust/a.json",
    "(cd ~/.config/tmux-pi && echo x > trust/a.json)",
    "cd {HOME}/.config/tmux-pi && rm -rf trust",
    "cd {TRUST} && cat rec.json",
    "cd {LINKCFG} && echo x > trust/a.json",
    "cd {LINK}; ls",
    # traversal
    "echo x > ~/.config/tmux-pi/state/../trust/a.json",
    "echo x > {SCRATCH}/../../home/.config/tmux-pi/trust/a.json",
    "echo x > {LINK}/../../.config/tmux-pi/trust/a.json",
    # the home, spelled each way
    "echo x > ~/.config/tmux-pi/trust/a.json",
    "echo x > $HOME/.config/tmux-pi/trust/a.json",
    "echo x > ${HOME}/.config/tmux-pi/trust/a.json",
    "echo x > \"$HOME/.config/tmux-pi/trust/a.json\"",
    "echo x > $PWD/../.config/tmux-pi/trust/a.json",
    "echo x > ~/.config/tmux-pi/tr\"us\"t/a.json",
    "echo x > ~/.config/tmux-pi/tr\\ust/a.json",
    "echo x > ~/.config/tmux-pi/$'trust'/a.json",
    "echo x > ~/.config/tmux-pi/{trust,other}/a.json",
    "echo x > {HOME}/.config/tmux-pi/./trust/a.json",
    "echo x > {HOME}/.config//tmux-pi//trust//a.json",
    # globs
    "cat ~/.config/tmux-pi/tr*/rec.json",
    "cat ~/.config/*/trust/*",
    "cd ~/.config && cat t*/tr*/rec.json",
    # substitutions, eval, sh -c, xargs, find -exec
    "cat $(echo ~/.config/tmux-pi/trust)/rec.json",
    "cat `echo {TRUST}`/rec.json",
    "cat $(printf '%s/.config/tmux-%s/trust' \"$HOME\" pi)/rec.json",
    "eval \"echo x > {TRUST}/a.json\"",
    "eval echo x '>' ~/.config/tmux-pi/trust/a.json",
    "sh -c 'echo x > {TRUST}/a.json'",
    "bash -c \"echo x > ~/.config/tmux-pi/trust/a.json\"",
    "bash -lc 'cd ~/.config/tmux-pi && echo x > trust/a.json'",
    "zsh -c 'sh -c \"echo x > {TRUST}/a.json\"'",
    "echo {TRUST}/a.json | xargs touch",
    "echo a | xargs -I{} cp {} {TRUST}/",
    "find {SCRATCH} -name f -exec cp {} {TRUST}/a.json \\;",
    "find {HOME}/.config/tmux-pi -type f -delete",
    "ssh host 'echo x > ~/.config/tmux-pi/trust/a.json'",
    "echo 'cat ~/.config/tmux-pi/trust/rec.json' | sh",
    # symlinked parent, case variant, Unicode variant
    "echo x > {LINK}/a.json",
    "echo x > {LINKCFG}/trust/a.json",
    "cat {LINK}/rec.json",
    "echo x > ~/.CONFIG/Tmux-Pi/TRUST/a.json",
    "echo x > ~/.config/TMUX-PI/State/Trust/skipped.jsonl",
    "cd ~/.CONFIG/Tmux-Pi && echo x > TRUST/a.json",
    # a run-time path next to a mention of the store
    "cat $DIR/trust/rec.json",
    "D=~/.config; cat $D/tmux-pi/tr*/rec.json",
    "D=$(cd ~/.config && pwd); echo x > $D/tmux-pi/trust/a.json",
    "cat $(dirname $X)/trust/rec.json",
    "cd $SOMEWHERE && cat trust/rec.json",
    "cat ${A}${B}/tmux-pi/trust/rec.json",
    # the environment moved so a relative or temporary name lands in the store
    "export XDG_CONFIG_HOME=~/.config; touch x",
    "TMPDIR={HOME}/.config/tmux-pi python3 x.py",
    "export HOME=~/.config/tmux-pi\ncp /tmp/f x",
    "export TMPDIR=~/.config/tmux-pi/trust; mktemp",
    # code that builds the path while it runs, next to the words that name the store
    "python3 -c \"import os; p=os.path.join(os.path.expanduser('~'),'.config','tmux-pi','trust','a.json'); open(p,'w')\"",
    "python3 -c \"import os; open(os.environ['HOME']+'/.config/'+'tmux-pi/tru'+'st/a.json','w')\"",
    "node -e \"require('fs').writeFileSync(require('os').homedir()+'/.config/tmux-pi/'+'trust/a','x')\"",
    "python3 - <<EOF\nimport os\nopen(os.path.expanduser('~') + '/.config/tmux-pi/' + 'trust/a.json', 'w')\nEOF",
    # the commands that make a record
    "tp-trust grant {PROJ}",
    "tp-trust revoke {PROJ}",
    "tp-trust",
    "tp-trust $SUB",
    "bin/tp-trust grant .",
    "./bin/tp-trust grant --days 3 .",
    "/Users/someone/tmux-pi/bin/tp-trust grant .",
    "python3 bin/tp-trust grant .",
    "bash bin/tp-trust revoke abc",
    "sudo tp-trust grant .",
    "env X=1 tp-trust grant .",
    "echo . | xargs tp-trust grant",
    "sh -c 'tp-trust grant .'",
    "eval tp-trust grant .",
    "tp-trust list; tp-trust grant .",
    "tp-trust --trust-dir /tmp/other list",
    "tp-trust list --trust-dir=/tmp/other",
    "python3 -c \"import subprocess; subprocess.run(['tp-trust', 'grant', '.'])\"",
]

# A worker may not type into, read from, or run things in a pane; the main agent may.
TMUX = [
    "tmux send-keys -t 0 'tp-trust grant .' Enter",
    "tmux send -t 0 x",
    "tmux paste-buffer -t 0",
    "tmux pasteb",
    "tmux load-buffer /tmp/f \\; paste-buffer",
    "tmux set-buffer x",
    "tmux capture-pane -p -t 0",
    "tmux capturep -p",
    "tmux run-shell 'tp-trust grant .'",
    "tmux run 'x'",
    "tmux pipe-pane -t 0 'cat > /tmp/x'",
    "tmux respawn-pane -k -t 0 'tp-trust grant .'",
    "tmux source-file /tmp/f",
    "tmux save-buffer /tmp/x",
    "tmux new-window 'tp-trust grant .'",
    "tmux split-window 'tp-trust grant .'",
    "tmux -L other send-keys -t 0 x",
    "tmux -S /tmp/sock send-keys x",
    "tmux if-shell true 'send-keys x'",
    "tmux $VERB -t 0",
    "command tmux send-keys x",
    "env tmux send-keys x",
    "xargs tmux send-keys",
    "sh -c 'tmux send-keys -t 0 x'",
    "echo x | tmux load-buffer -",
    "python3 -c \"import subprocess; subprocess.run(['tmux', 'send-keys', 'x'])\"",
]

ORDINARY = [
    "ls ~/.config",
    "ls -la ~/.config/tmux-pi",
    "git diff",
    "git status --short",
    "git log --oneline -5",
    "cat README.md",
    "cat lib/trust.py",
    "grep -rn trust lib/",
    "grep -n grant bin/tp-trust",
    "cat bin/tp-trust",
    "git add bin/tp-trust lib/trust_guard.py",
    "python3 -m unittest tests/test_trust_record.py",
    "python3 tests/test_trust_guard.py",
    "python3 -c \"print('trust me')\"",
    "python3 -c \"import trust; print(trust.STATEMENT)\"",
    "echo $HOME",
    "export FOO=$HOME/x",
    "TMPDIR=/tmp python3 -m unittest",
    "TMPDIR=$(mktemp -d) python3 -m unittest",
    "XDG_CONFIG_HOME=/tmp/x python3 y.py",
    "mkdir -p ~/.config/other",
    "touch ~/.config/foo",
    "cp notes.txt ~/.config/",
    "cp -r somedir ~/.config/",
    "mv a.txt ~/.config/b.txt",
    "rm -rf /tmp/scratch-dir",
    "rm ~/.config/foo",
    "cd ~/.config && ls",
    "cd ~/.config/tmux-pi && ls",
    "cd ~/.config/tmux-pi && cat other.json",
    "find . -name '*.py'",
    "find ~/.config -maxdepth 1",
    "grep -rn pattern ~/.config/nvim",
    "tp-trust list",
    "tp-trust show abc",
    "tp-trust check .",
    "tp-trust log -n 50",
    "bin/tp-trust list",
    "git commit -m \"Protect ~/.config/tmux-pi/trust from tp-trust grant\"",
    "tp-agent request --kind context --what 'the trust store layout' --reason 'tp-trust grant'",
    "tmux list-panes -a",
    "tmux display-message -p '#S'",
    "tmux has-session -t x",
    "ls /tmp | head",
    "echo 'the trust model' | wc -w",
    "for f in *.py; do echo $f; done",
    "cat <<EOF > notes.txt\nhello\nEOF",
    "curl -s https://example.com/trust",
    "ln -s /tmp/a ./b",
]


class RedTeamTests(Fixture):
    def test_every_route_is_denied_for_a_worker_and_for_the_main_agent(self):
        for command in REDTEAM:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertDenied(command, worker=worker)

    def test_tmux_verbs_are_denied_for_a_worker_only(self):
        for command in TMUX:
            with self.subTest(command=command):
                self.assertDenied(command, worker=True)
        for command in ("tmux send-keys -t 0 x Enter", "tmux capture-pane -p -t 0", "tmux run-shell true"):
            with self.subTest(command=command, worker=False):
                self.assertAllowed(command, worker=False)

    def test_ordinary_commands_are_not_affected(self):
        for command in ORDINARY:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertAllowed(command, worker=worker)

    def test_a_variable_next_to_the_store_as_a_path_is_denied_but_next_to_the_word_alone_is_not(self):
        # The design denies a run-time path next to a mention of the store. A mention is the store as a path
        # (`/trust/`, `tmux-pi/` then a variable or glob); `trust` as a search pattern or a module name is not one.
        self.assertTrue(self.bash("grep -rn $DIR/trust lib"))
        self.assertTrue(self.bash("cat $DIR/trust"))
        self.assertTrue(self.bash("cat ~/.config/tmux-pi/$X"))
        self.assertTrue(self.bash("cat /anywhere/tmux-pi/$X"))
        self.assertIsNone(self.bash("grep -rn trust $DIR"))
        self.assertIsNone(self.bash("cat /anywhere/tmux-pi/lib/trust.py $DIR"))

    def test_powershell_backslash_paths(self):
        self.assertTrue(trust_guard.check("PowerShell", {"command": "Set-Content " + self.trust.replace("/", "\\") + "\\a.json x"},
                                          self.proj, home=self.home, worker=True))

    def test_a_working_directory_inside_the_store_does_not_hide_a_relative_name(self):
        self.assertTrue(self.bash("echo x > a.json", cwd=self.trust))
        self.assertTrue(self.bash("cat rec.json", cwd=self.trust))
        self.assertTrue(self.bash("rm -rf trust", cwd=self.config))

    def test_a_working_directory_that_cannot_be_placed_denies_every_call(self):
        # Nothing relative can be resolved from a directory nobody can place, so the call is refused, not guessed at.
        for cwd in ("relative/dir", "", "~"):
            for tool, params in (("Bash", {"command": "git diff"}), ("Read", {"file_path": "README.md"}),
                                 ("Grep", {"pattern": "x"})):
                with self.subTest(cwd=cwd, tool=tool):
                    self.assertEqual(trust_guard.check(tool, params, cwd, home=self.home, worker=True),
                                     "the working directory could not be placed")

    def test_the_main_agent_is_a_worker_only_when_the_launch_environment_says_so(self):
        for env, expected in (({}, False), ({"TP_TASK_DIR": "/x"}, True), ({"TP_TASK_ID": "t1"}, True),
                              ({"TP_MODE": "strict"}, True), ({"TP_MODE": "orchestrator"}, False)):
            with self.subTest(env=env):
                self.assertEqual(trust_guard.is_worker(env), expected)


class Layers(Fixture):
    """One case for each decision, built so that only that decision can catch it."""

    def test_text_inside_catches_a_path_that_does_not_exist_yet(self):
        # Nothing below the bare home's .config exists, so only the text of the path can say where it leads.
        self.assertTrue(self.bash("cd ~/.config/tmux-pi && echo x > trust/a.json", home=self.bare, cwd=self.bare))

    def test_text_above_catches_removal_of_a_directory_that_will_hold_the_store(self):
        self.assertTrue(self.bash("rm -rf ~/.config/tmux-pi", home=self.bare, cwd=self.bare))
        self.assertTrue(self.bash("rm -rf ~/.config/tmux-pi/state", home=self.bare, cwd=self.bare))
        self.assertIsNone(self.bash("rm -rf ~/.config/tmux-pi-other", home=self.bare, cwd=self.bare))

    def test_inode_inside_catches_a_spelling_the_text_comparison_cannot_fold(self):
        if not os.path.exists(self.trust.upper()):
            self.skipTest("case-sensitive file system")
        with mock.patch.object(trust_guard, "fold", lambda text: text):
            guard = trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)
            self.assertEqual(guard.where(self.trust.upper() + "/new.json"), "inside")

    def test_inode_above_catches_a_spelling_the_text_comparison_cannot_fold(self):
        if not os.path.exists(self.config.upper()):
            self.skipTest("case-sensitive file system")
        with mock.patch.object(trust_guard, "fold", lambda text: text):
            guard = trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)
            self.assertEqual(guard.where(self.config.upper()), "above")

    def test_firmlink_spelling_of_the_store(self):
        firm = "/System/Volumes/Data" + self.trust
        if not os.path.isdir(firm):
            self.skipTest("no firmlinks here")
        self.assertTrue(self.bash("echo x > " + firm + "/a.json"))

    def test_unicode_variants_of_the_path_are_folded(self):
        import unicodedata
        home = os.path.join(self.tmp, unicodedata.normalize("NFC", "héme"))
        variant = unicodedata.normalize("NFD", home)
        self.assertNotEqual(home, variant)
        self.assertTrue(trust_guard.check("Bash", {"command": "cd %s/.config/tmux-pi && echo x > trust/a" % variant},
                                          self.tmp, home=home, worker=True))

    def test_the_tail_names_the_store_wherever_the_home_is(self):
        self.assertTrue(self.bash("cat /Users/nobody/.config/tmux-pi/trust/x.json"))
        self.assertTrue(self.bash("cat /Users/nobody/.config/tmux-pi/state/trust/x.jsonl"))

    def test_unresolved_path_next_to_a_mention_is_denied_but_not_without_one(self):
        self.assertTrue(self.bash("cat $DIR/trust/rec.json"))
        self.assertIsNone(self.bash("cat $DIR/readme.md"))
        self.assertIsNone(self.bash("cat $(dirname $X)/lib/goal.py"))

    def test_environment_moves_only_matter_when_they_reach_the_store_and_something_writes(self):
        self.assertTrue(self.bash("export XDG_CONFIG_HOME={HOME}/.config; touch x"))
        self.assertIsNone(self.bash("export XDG_CONFIG_HOME={HOME}/.config; ls"))
        self.assertIsNone(self.bash("export XDG_CONFIG_HOME=/tmp/elsewhere; touch x"))

    def test_inline_code_that_builds_a_path_is_denied_but_plain_code_is_not(self):
        self.assertTrue(self.bash("python3 -c \"import os; os.path.join('a', 'trust')\""))
        self.assertIsNone(self.bash("python3 -c \"print('trust')\""))

    def test_nesting_too_deep_to_verify_is_denied(self):
        command = "echo x"
        for _ in range(9):
            command = "sh -c '%s'" % command.replace("'", "'\\''")
        self.assertTrue(self.bash(command))

    def test_glob_that_can_only_reach_the_store_is_denied(self):
        self.assertTrue(self.bash("cat {HOME}/.config/tmux-pi/tr*"))
        self.assertIsNone(self.bash("cat {HOME}/.config/tmux-pi/zz*"))

    def test_cp_into_a_directory_is_judged_by_the_name_it_will_have(self):
        outer = os.path.join(self.tmp, "outer")
        os.makedirs(os.path.join(outer, "tmux-pi"))
        os.makedirs(os.path.join(self.tmp, "stage", "tmux-pi", "trust"))
        # `cp -r stage/tmux-pi {HOME}/.config/` lands as {HOME}/.config/tmux-pi, the directory that holds the store.
        self.assertTrue(self.bash("cp -r {SCRATCH}/../stage/tmux-pi {HOME}/.config/"))
        self.assertTrue(self.bash("mv {SCRATCH}/../stage/tmux-pi {HOME}/.config/"))
        self.assertIsNone(self.bash("cp -r {SCRATCH}/../outer/tmux-pi {SCRATCH}/"))


class ToolTests(Fixture):
    def test_file_tools_are_denied_by_every_route_to_the_store(self):
        targets = ["{TRUST}/a.json", "{LINK}/a.json", "{LINKCFG}/trust/a.json", "~/.config/tmux-pi/trust/a.json",
                   "$HOME/.config/tmux-pi/trust/a.json", "../.config/tmux-pi/trust/a.json",
                   "{HOME}/.CONFIG/Tmux-Pi/TRUST/a.json", "{STATE}/skipped.jsonl", "{HOME}/.config/tmux-pi/state/../trust/a"]
        calls = [("Write", "file_path"), ("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path"),
                 ("Read", "file_path"), ("Grep", "path"), ("Glob", "path"), ("write", "path"), ("edit", "path"),
                 ("read", "path"), ("grep", "path"), ("find", "path"), ("ls", "path")]
        for tool, key in calls:
            for target in targets:
                for worker in (True, False):
                    with self.subTest(tool=tool, target=target, worker=worker):
                        self.assertTrue(self.tool(tool, {key: target}, worker=worker))

    def test_a_glob_pattern_with_an_absolute_directory_is_denied(self):
        for tool in ("Glob", "glob", "find"):
            with self.subTest(tool=tool):
                self.assertTrue(self.tool(tool, {"pattern": "{TRUST}/*.json"}))
                self.assertIsNone(self.tool(tool, {"pattern": "**/*.py"}))

    def test_a_grep_pattern_is_a_regular_expression_not_a_path(self):
        self.assertIsNone(self.tool("Grep", {"pattern": "tmux-pi/trust", "path": "lib"}))

    def test_file_contents_are_never_read_as_paths(self):
        self.assertIsNone(self.tool("Write", {"file_path": "notes.md", "content": "records live in {TRUST}/a.json"}))
        self.assertIsNone(self.tool("Edit", {"file_path": "notes.md", "old_string": "{TRUST}", "new_string": "~/.config/tmux-pi/trust"}))

    def test_ordinary_tool_calls_pass(self):
        for tool, params in (("Write", {"file_path": "x.md"}), ("Read", {"file_path": "README.md"}), ("Grep", {"pattern": "a", "path": "lib"}),
                             ("Glob", {"pattern": "*.py"}), ("read", {"path": "~/.config/nvim/init.lua"}),
                             ("ls", {"path": "~/.config"}), ("ls", {"path": "~/.config/tmux-pi"}), ("edit", {"path": "{PROJ}/a.py"})):
            with self.subTest(tool=tool, params=params):
                self.assertIsNone(self.tool(tool, params))

    def test_an_unknown_tool_with_a_path_parameter_is_judged_too(self):
        self.assertTrue(self.tool("mcp__other__save", {"path": "{TRUST}/a.json"}))
        self.assertIsNone(self.tool("mcp__other__save", {"path": "{PROJ}/a.json"}))

    def test_an_unknown_tool_is_judged_by_any_path_shaped_value_under_any_key(self):
        for params in ({"uri": "file://{TRUST}/a.json"}, {"target_file": "{TRUST}/a.json"},
                       {"options": {"deep": ["{LINK}/a.json"]}}, {"items": [{"name": "~/.config/tmux-pi/trust/a"}]}):
            for worker in (True, False):
                with self.subTest(params=params, worker=worker):
                    self.assertTrue(self.tool("mcp__other__fetch", params, worker=worker))

    def test_an_unknown_tool_with_ordinary_values_passes(self):
        for params in ({"uri": "file://{PROJ}/a.json"}, {"text": "see {TRUST}/a.json for the records"},
                       {"url": "https://example.com/a"}, {"count": 3, "flag": True},
                       {"options": {"deep": ["{PROJ}/a.json", "name"]}}):
            with self.subTest(params=params):
                self.assertIsNone(self.tool("mcp__other__fetch", params))

    def test_a_path_at_any_nesting_is_judged(self):
        # The review's reproduction (five levels), and far past it.
        for levels in (1, 4, 5, 6, 40, 3000):
            value = self.trust + "/record"
            for _ in range(levels):
                value = {"child": value}
            for shape in ({"argument": value}, {"argument": [[[[[[value]]]]]]}, {"argument": [{"a": [value]}]}):
                for worker in (True, False):
                    with self.subTest(levels=levels, worker=worker):
                        self.assertEqual(trust_guard.check("mcp__write_any", shape, self.proj, home=self.home, worker=worker),
                                         "names the trust store")
        value = self.proj + "/record"
        for _ in range(3000):
            value = {"child": value}
        self.assertIsNone(trust_guard.check("mcp__write_any", {"argument": value}, self.proj, home=self.home, worker=True))

    def test_a_tool_call_with_more_values_than_the_guard_looks_at_is_denied(self):
        many = {"items": ["plain"] * 50}
        self.assertIsNone(self.tool("mcp__other__batch", many))
        with mock.patch.object(trust_guard, "_PARAM_VALUES", 20):
            self.assertEqual(self.tool("mcp__other__batch", many),
                             "the tool call holds more values than can be checked against the trust store")
            self.assertIsNone(self.tool("mcp__other__batch", {"items": ["plain"] * 5}))
        self.assertTrue(self.tool("mcp__other__batch", {"items": ["plain"] * 30000 + ["{TRUST}/a.json"]}))

    def test_a_value_the_guard_cannot_read_is_denied(self):
        for value in (b"/x", {"/x"}, object()):
            with self.subTest(value=value):
                self.assertEqual(trust_guard.check("mcp__other__save", {"data": {"inner": value}}, self.proj, home=self.home,
                                                   worker=True), "the tool call holds a value that could not be read")
        self.assertEqual(trust_guard.check("mcp__other__save", {"data": {3: "x"}}, self.proj, home=self.home, worker=True),
                         "the tool call holds a parameter name that could not be read")
        self.assertIsNone(self.tool("mcp__other__save", {"n": 1, "f": 1.5, "b": False, "none": None, "list": [1, None]}))

    def test_a_path_used_as_a_parameter_name_is_judged(self):
        self.assertTrue(self.tool("mcp__other__write_files", {"files_by_path": {self.trust + "/a.json": "body"}}))
        self.assertTrue(self.tool("mcp__other__write_files", {"files_by_path": {self.link + "/a.json": "body"}}))
        self.assertTrue(self.tool("mcp__other__write_files", {"batch": [{"~/.config/tmux-pi/trust/a": {"mode": 1}}]}))
        self.assertIsNone(self.tool("mcp__other__write_files", {"files_by_path": {self.proj + "/a.json": "body"}}))

    def test_a_known_tool_is_judged_by_a_parameter_the_guard_does_not_expect(self):
        for tool, params in (("Write", {"target_file": "{TRUST}/a.json", "content": "x"}),
                             ("Edit", {"file_path": "a.md", "extra": {"deep": ["{LINK}/a.json"]}}),
                             ("Bash", {"command": "ls", "stdout_file": "{TRUST}/a.json"}),
                             ("read", {"path": "a.md", "also": "~/.config/tmux-pi/trust/a"})):
            for worker in (True, False):
                with self.subTest(tool=tool, worker=worker):
                    self.assertTrue(self.tool(tool, params, worker=worker))
        # What a known tool writes into a file, searches for or runs is still never a path.
        for tool, params in (("MultiEdit", {"file_path": "a.md", "edits": [{"old_string": "{TRUST}/a", "new_string": "{TRUST}/b"}]}),
                             ("edit", {"path": "a.md", "edits": [{"oldText": "{TRUST}/a", "newText": "{TRUST}/b"}]}),
                             ("NotebookEdit", {"notebook_path": "a.ipynb", "new_source": "{TRUST}/a"}),
                             ("Grep", {"pattern": "{TRUST}/a", "path": "lib"}),
                             ("Bash", {"command": "ls", "description": "List files", "timeout": 5000})):
            with self.subTest(tool=tool):
                self.assertIsNone(self.tool(tool, params))

    def test_a_judgement_that_takes_too_long_is_denied(self):
        def slow(*_args, **_kw):
            time.sleep(5)
        with mock.patch.object(trust_guard.Guard, "command", slow):
            started = time.monotonic()
            answer = trust_guard.check_within(0.2, "Bash", {"command": "ls"}, self.proj, home=self.home, worker=True)
        # Not in a walk or a glob, so it is a deny and not an ask.
        self.assertEqual(answer, (trust_guard.DENY, "the tool call could not be analysed in time"))
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(trust_guard.check_within(5, "Bash", {"command": "ls"}, self.proj, home=self.home, worker=True),
                         (trust_guard.ALLOW, None))
        self.assertEqual(trust_guard.check_within(5, "Bash", {"command": "ls {TRUST}".replace("{TRUST}", self.trust)},
                                                  self.proj, home=self.home, worker=True).kind, trust_guard.DENY)

    def test_the_limit_is_not_swallowed_by_a_walk_that_ignores_file_errors(self):
        # `TimeoutError` is an `OSError`, which the walk catches and skips; the limit must not be one.
        self.assertFalse(issubclass(trust_guard._OutOfTime, (Exception, OSError)))

    def test_a_shell_tool_is_judged_by_its_directory_parameter_too(self):
        for key in ("cwd", "workdir", "directory"):
            for worker in (True, False):
                with self.subTest(key=key, worker=worker):
                    self.assertTrue(self.tool("Bash", {"command": "ls", key: "{TRUST}"}, worker=worker))
        self.assertIsNone(self.tool("Bash", {"command": "ls", "cwd": "{PROJ}"}))

    def test_params_that_are_not_an_object_are_denied(self):
        for params in (None, [], "x", 3):
            with self.subTest(params=params):
                # The catch-all would deny these too, with a vaguer reason; the named reason shows the explicit check ran.
                for tool in ("Bash", "Read"):
                    self.assertEqual(trust_guard.check(tool, params, self.proj, home=self.home, worker=True),
                                     "the tool call could not be read")

    def test_a_command_that_is_not_text_is_denied(self):
        for command in (None, ["ls"], 3):
            with self.subTest(command=command):
                self.assertEqual(trust_guard.check("Bash", {"command": command}, self.proj, home=self.home, worker=True),
                                 "the shell command could not be read")


# Programs that walk a tree and follow the links in it. {TREE} holds a link to the store, {TREE2} a link to a
# directory that links to the directory above the store, {TREE3} a link to the home directory.
FOLLOWING = [
    "find -L {TREE} -name rec.json",
    "find -L {TREE}",
    "find {TREE} -follow -name rec.json",
    "find -L {TREE2} -name rec.json",
    "find -L {TREE3} -name rec.json",
    "find -H -L {TREE} -type f",
    "find -L {TREE} -type f | xargs cat",
    "grep -R pattern {TREE}",
    "grep -rR pattern {TREE}",
    "grep --dereference-recursive pattern {TREE}",
    "grep -rnS pattern {TREE}",
    "rg -L pattern {TREE}",
    "rg --follow pattern {TREE}",
    "ag -f pattern {TREE}",
    "ack --follow pattern {TREE}",
    "fd -L pattern {TREE}",
    "rsync -avL {TREE}/ {SCRATCH}/out/",
    "rsync -r --copy-links {TREE} {SCRATCH}/out",
    "rsync -a -k {TREE}/ {SCRATCH}/out/",
    "cp -RL {TREE} {SCRATCH}/out",
    "cp -R -L {TREE2} {SCRATCH}/out",
    "cp -rL {TREE3} {SCRATCH}/out",
    "tar -chf {SCRATCH}/out.tar {TREE}",
    "tar chf {SCRATCH}/out.tar {TREE}",
    "tar --dereference -cf {SCRATCH}/out.tar {TREE}",
    "du -L {TREE}",
    "du -aL {TREE2}",
    "tree -l {TREE}",
    "ls -RL {TREE}",
    "chmod -R -L 700 {TREE}",
    "chmod -RL 700 {TREE2}",
    "chown -R -L me {TREE}",
    "chgrp -RL staff {TREE3}",
    "zip -r {SCRATCH}/out.zip {TREE}",              # follows links unless told not to
    "scp -r {TREE} host:/tmp/",
    "diff -r {TREE} {PLAIN}",
    "cd {TREE} && find -L . -name rec.json",
    "cd {TREE} && grep -R pattern .",
    "cd {TREE} && grep -R pattern",
    "sh -c 'find -L {TREE}'",
    "echo x; (cd {TREE2}; du -L)",
]

# The same programs with nothing in the way: no link into the store, or links not followed.
NOT_FOLLOWING = [
    "find -L {PLAIN} -name one.txt",             # a loop of links, none leading near the store
    "find {TREE} -name rec.json",                 # `find` does not follow links unless told to
    "find -H {TREE} -type f",
    "find -P {TREE3}",
    "grep -r pattern {TREE}",
    "grep -rn pattern {TREE2}",
    "rg pattern {TREE}",
    "rsync -av {TREE}/ {SCRATCH}/out/",
    "cp -r {TREE} {SCRATCH}/out",
    "cp -rH {TREE} {SCRATCH}/out",
    "tar -cf {SCRATCH}/out.tar {TREE}",
    "du {TREE}",
    "du -sh {TREE3}",
    "ls -R {TREE}",
    "chmod -R 700 {TREE}",
    "zip -ry {SCRATCH}/out.zip {TREE}",
    "rm -rf {TREE}",                               # `rm -r` unlinks a link, it does not go through it
    "grep -L pattern {TREE}/sub/inner/nothing",   # -L is "files without match" for grep
    "cd {TREE} && find . -name rec.json",
]

# Recursive reads, and walks of the directory that holds the store, without any link at all.
ABOVE = [
    "find {HOME}/.config -name rec.json",
    "find ~ -name rec.json",
    "find / -name rec.json",
    "find ~/.config/tmux-pi -type f",
    "find ~/.config -maxdepth 3 -name rec.json",
    "grep -r pattern ~/.config",
    "grep -rn pattern {HOME}",
    "rg pattern ~/.config",
    "ls -R ~/.config",
    "ls -R ~",
    "du -a ~/.config",
    "du -sh ~",
    "tree ~/.config",
    "tar -cf {SCRATCH}/out.tar ~/.config",
    "cp -r ~/.config {SCRATCH}/out",
    "rsync -a ~/.config/ {SCRATCH}/out/",
    "cd ~/.config && grep -r pattern",
    "cd ~/.config && find . -type f",
    "cd ~ && rg pattern",
    "cd {CONFIG} && du",
    "diff -r ~/.config {SCRATCH}",
    # An option that limits what is printed, not what is read: du and tree still go through the whole tree.
    "du -d 1 ~/.config",
    "du -d 0 ~/.config",
    "du -k -d 2 ~/.config",
    "du --max-depth=1 ~/.config",
    "du -s ~/.config",
    "tree -L 1 ~/.config",
    # Programs that do stop, but whose effective depth the guard does not read (last one wins, clusters, other counting).
    "rg --max-depth 1 pattern ~/.config",
    "rg --max-depth 1 --max-depth 9 pattern ~/.config",
    "rg -e -d 1 ~/.config",
    "rg -uud9 --max-depth 1 pattern ~/.config",
    "fd --max-depth 1 . ~/.config",
    "ag --depth 2 pattern ~/.config",
    "ack -maxdepth 1 pattern ~/.config",            # find's option on a program that is not find
    # find's own limit, when it does not hold: the store directory itself is reached, the last of two wins, the
    # number is another option's value or is not a number, or a program find runs goes on from there.
    "find ~/.config -maxdepth 2",
    "find ~/.config -maxdepth 1 -maxdepth 9",
    "find ~/.config -maxdepth 9 -maxdepth 1",
    "find ~/.config -name -maxdepth 1",
    "find ~/.config -maxdepth $N",
    "find ~/.config -maxdepth",
    "find ~/.config -maxdepth 1 -exec grep -r pattern {} +",
    "find ~/.config -maxdepth 1 | xargs grep -r pattern",
    "find ~/.config -maxdepth 1 -print0 | xargs -0 ls -R",
    "grep -r pattern $(find ~/.config -maxdepth 1)",
    "for d in $(find ~/.config -maxdepth 1); do ls $d; done",
    "find ~/.config -maxdepth 1 | while read d; do grep -r pattern \"$d\"; done",
    # Recursion asked for in a way that is easy to miss.
    "cp -a ~/.config {SCRATCH}/out",
    "cp --archive ~/.config {SCRATCH}/out",
    "grep -d recurse pattern ~/.config",
    "grep --directories recurse pattern ~/.config",
    "grep --directories=recurse pattern ~/.config",
    "rgrep pattern ~/.config",
    # A recursive glob is a walk.
    "cat ~/.config/**/*.json",
    "ls {HOME}/**/rec.json",
    "cd ~/.config && ls **/*.json",
    # A program the guard has no rule for, handed a directory that holds the store.
    "gzip -r ~/.config",
    "git -C ~ clean -fdx",
    "prettier --write ~/.config",
    "python3 walk.py ~/.config",
    "black {HOME}",
    "open ~/.config/tmux-pi",
    "code --folder=~/.config",
    "cd ~/.config && gzip -r .",
    "cd ~/.config/tmux-pi && prettier --write .",
]

# Walks that are told how far to go, or that are not walks.
ABOVE_ALLOWED = [
    "find ~/.config -maxdepth 1",
    "find ~/.config -maxdepth 1 -name nvim",
    "find ~/.config -type d -maxdepth 1",
    "find ~/.config -maxdepth 0",
    "find ~ -maxdepth 2 -name '*.txt'",
    "ls ~/.config",
    "echo ~",
    "stat ~/.config",
    "test -d ~/.config && echo yes",
    "[ -d ~/.config/tmux-pi ] || mkdir -p ~/.config/tmux-pi",
    "cd ~ && pwd",
    "mv notes.txt ~/.config/",
    "git -C {PROJ} status",
    "ls **/*.py",
    "ls {PLAIN}/**/*.txt",
    "ls -la ~",
    "ls -R ~/.config/nvim",
    "grep -r pattern ~/.config/nvim",
    "find / -maxdepth 1",
    "grep pattern ~/.config/notes.txt",
    "du -sh ~/Documents",
]


class TreeWalks(Fixture):
    """A command that walks a tree is judged by where the walk can end up, not only by the words in it."""

    def test_walks_that_follow_links_into_the_store_are_denied(self):
        for command in FOLLOWING:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertDenied(command, worker=worker)

    def test_the_same_programs_are_left_alone_when_nothing_is_followed_to_the_store(self):
        for command in NOT_FOLLOWING:
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_a_walk_of_a_directory_that_holds_the_store_is_denied_even_without_a_link(self):
        for command in ABOVE:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertDenied(command, worker=worker)

    def test_a_walk_told_to_stop_before_the_store_is_allowed(self):
        for command in ABOVE_ALLOWED:
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_a_walk_too_large_to_check_far_from_the_store_asks(self):
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 3):
            self.assertAsk("find -L {PLAIN} -name one.txt")
            self.assertAllowed("find {PLAIN} -name one.txt")        # nothing is followed, so nothing is counted
            self.assertAllowed("grep -R pattern {PLAIN}/a/b/nothing")
        self.assertAllowed("find -L {PLAIN} -name one.txt")

    def test_a_walk_that_runs_out_of_time_far_from_the_store_asks(self):
        with mock.patch.object(trust_guard, "_WALK_SECONDS", -1):
            self.assertAsk("find -L {PLAIN} -name one.txt")

    def test_a_recursive_copy_writes_through_links_already_in_the_place_it_copies_to(self):
        os.makedirs(os.path.join(self.tmp, "src", "keep"))
        dest = os.path.join(self.tmp, "dest")
        os.makedirs(dest)
        os.symlink(self.trust, os.path.join(dest, "keep"))
        self.assertDenied("cp -r {SCRATCH}/../src/keep {SCRATCH}/../dest")        # lands as dest/keep, a link to the store
        self.assertDenied("cp -r {SCRATCH}/../src/ {SCRATCH}/../dest")            # contents of src go into dest, and dest/keep is a link
        self.assertDenied("rsync -a {SCRATCH}/../src/ {SCRATCH}/../dest/")
        os.unlink(os.path.join(dest, "keep"))
        self.assertAllowed("cp -r {SCRATCH}/../src/keep {SCRATCH}/../dest")
        self.assertAllowed("cp -r {SCRATCH}/../src/ {SCRATCH}/../dest")

    def test_a_recursive_copy_into_a_big_directory_only_looks_where_it_will_write(self):
        # The destination is ~/.config: copying a directory in must not mean looking through all of ~/.config.
        os.makedirs(os.path.join(self.tmp, "src", "notes"))
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 1):
            self.assertAllowed("cp -r {SCRATCH}/../src/notes {HOME}/.config/")

    def test_search_tools_start_where_they_are_told_and_a_directory_that_holds_the_store_is_denied(self):
        denied = [("Grep", {"pattern": "x", "path": "{HOME}"}), ("Grep", {"pattern": "x", "path": "~/.config"}),
                  ("Glob", {"pattern": "**/*.json", "path": "{CONFIG}"}), ("Glob", {"pattern": "*/*", "path": "{CONFIG}"}),
                  ("Glob", {"pattern": "*/*/*", "path": "{HOME}/.config"}), ("Glob", {"pattern": "{HOME}/.config/**/*.json"}),
                  ("Glob", {"pattern": "~/.config/*/*/*.json"}), ("grep", {"pattern": "x", "path": "/"}),
                  ("find", {"pattern": "*.json", "path": "{HOME}"}), ("find", {"pattern": "rec.json", "path": "{HOME}/.config"}),
                  # A pattern with no `/` or `**` is no limit: Claude's Glob returns `a/b/x.json` for `*.json`.
                  ("Glob", {"pattern": "*.json", "path": "{CONFIG}"}), ("Glob", {"pattern": "*", "path": "{HOME}/.config"}),
                  ("Glob", {"pattern": "{HOME}/.config/*"}), ("glob", {"pattern": "*.json", "path": "{HOME}"})]
        for tool, params in denied:
            with self.subTest(tool=tool, params=params):
                self.assertTrue(self.tool(tool, params))
        allowed = [("Grep", {"pattern": "x", "path": "{HOME}/.config/nvim"}), ("Grep", {"pattern": "x"}),
                   ("Glob", {"pattern": "*.json", "path": "{HOME}/.config/nvim"}), ("Glob", {"pattern": "**/*.py"}),
                   ("grep", {"pattern": "x", "path": "{PROJ}"}), ("find", {"pattern": "*.py", "path": "{PROJ}"}),
                   ("ls", {"path": "{HOME}"}), ("ls", {"path": "~/.config"})]
        for tool, params in allowed:
            with self.subTest(tool=tool, params=params):
                self.assertIsNone(self.tool(tool, params))

    def test_a_search_with_no_directory_starts_in_the_working_directory(self):
        for cwd in (self.home, self.config):
            for tool, params in (("Grep", {"pattern": "x"}), ("Glob", {"pattern": "**/*.json"}), ("grep", {"pattern": "x"}),
                                 ("find", {"pattern": "*.json"}), ("Glob", {"pattern": "*.json"}), ("Glob", {"pattern": "*"})):
                with self.subTest(cwd=cwd, tool=tool, params=params):
                    self.assertTrue(trust_guard.check(tool, params, cwd, home=self.home, worker=True))
        self.assertIsNone(trust_guard.check("Glob", {"pattern": "*.json"}, self.proj, home=self.home, worker=True))

    def test_a_working_directory_that_holds_the_store_denies_a_search_that_starts_there(self):
        self.assertTrue(self.bash("grep -r pattern", cwd=self.home))
        self.assertTrue(self.bash("find . -name rec.json", cwd=self.config))
        self.assertTrue(self.bash("rg pattern", cwd=self.home))
        self.assertTrue(self.bash("du", cwd=self.home))
        self.assertTrue(self.bash("ls -R", cwd=self.config))
        self.assertIsNone(self.bash("grep -r pattern lib", cwd=self.proj))
        self.assertIsNone(self.bash("grep -r pattern", cwd=self.proj))
        self.assertIsNone(self.bash("ls", cwd=self.home))

    def test_a_glob_with_more_matches_than_the_guard_looks_at_is_not_allowed(self):
        # Matches past the budget are not looked at, so they are not allowed either, whatever the pattern looks like.
        # Far from the store that is an ask; a pattern that could lead anywhere (`..` after the wildcard front) is a deny.
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 1):
            self.assertAsk("cat {PLAIN}/a/*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("cat {PLAIN}/a/*/../*", reason=trust_guard.GLOB_REASON)
            self.assertAllowed("cat {PLAIN}/a/*.txt")               # one match: all of them were looked at
        with mock.patch.object(trust_guard, "_WALK_SECONDS", -1):
            self.assertAsk("cat {PLAIN}/a/*.txt", reason=trust_guard.GLOB_REASON)
        self.assertAllowed("cat {PLAIN}/a/*")

    def test_a_recursive_glob_is_a_walk_of_the_tree_it_starts_in(self):
        self.assertEqual(self.bash("cat ~/.config/**/*.json"), "walks a directory tree that holds the trust store")
        # zsh follows links for `***`, and not for `**`.
        self.assertEqual(self.bash("ls {TREE}/***/rec.json"), "walks through a link into the trust store")
        self.assertAllowed("ls {TREE}/**/rec.json")
        self.assertAllowed("ls {PLAIN}/***/*.txt")
        # Climbing back out of what `**` matched leaves the tree the guard looked at.
        self.assertEqual(self.bash("ls {SCRATCH}/**/../x"), "a recursive glob that climbs back up with `..` cannot be placed")
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 2):
            self.assertAsk("ls {PLAIN}/***/*.txt")
        # A glob tool's `**` does not follow links (rg and fd are run without their follow option).
        self.assertIsNone(self.tool("Glob", {"pattern": "**/rec.json", "path": "{TREE}"}))
        self.assertTrue(self.tool("Glob", {"pattern": "{HOME}/.config/**/rec.json"}))

    def test_a_glob_matches_hidden_names_too(self):
        # `setopt globdots` or `shopt -s dotglob` makes `*` match `.config`; the guard does not read those settings.
        self.assertDenied("cat {HOME}/*/*/*/*")
        self.assertDenied("cat ~/*/tmux-p?/tr*/rec.json")

    def test_du_reads_the_whole_tree_whatever_depth_it_prints(self):
        # The review's reproduction: a 9000-byte record shows up in `du -d 1` of a directory two levels up.
        with open(os.path.join(self.trust, "big.json"), "w") as f:
            f.write("x" * 9000)
        for command in ("du -k -d 1 ~/.config", "du -k -d 2 ~/.config", "du -k -d 0 {HOME}"):
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertDenied(command, worker=worker)
        # And a link below the depth it prints is still followed with -L.
        deep = os.path.join(self.tmp, "repo", "sub")
        os.makedirs(deep)
        os.symlink(self.trust, os.path.join(deep, "link"))
        for command in ("du -L -k -d 1 {SCRATCH}/../repo", "du -L -d 0 {SCRATCH}/../repo", "tree -l -L 1 {SCRATCH}/../repo"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), "walks through a link into the trust store")
        self.assertAllowed("du -d 1 {SCRATCH}/../repo")            # nothing followed, nothing above the store

    def test_find_is_held_to_maxdepth_through_links_too(self):
        deep = os.path.join(self.tmp, "repo", "sub")
        os.makedirs(deep)
        os.symlink(self.trust, os.path.join(deep, "link"))
        self.assertAllowed("find -L {SCRATCH}/../repo -maxdepth 1")     # find stops at `sub` and never sees the link
        self.assertDenied("find -L {SCRATCH}/../repo -maxdepth 2")
        self.assertDenied("find -L {SCRATCH}/../repo -maxdepth 1 -maxdepth 2")
        self.assertDenied("find -L {SCRATCH}/../repo")

    def test_brace_ranges_and_long_brace_lists_are_written_out(self):
        long_list = ",".join("a%d" % i for i in range(70))
        for command in ("touch {CONFIG}/trus{t..t}/new.json", "touch {CONFIG}/tr{u..u}s{t..t}/new.json",
                        "touch {CONFIG}/{%s,trust}/new.json" % long_list,
                        "cd {CONFIG} && touch {%s,trust}/new.json" % long_list,
                        "cd {CONFIG} && touch tr{u,u}st/new.json", "cd {CONFIG} && touch trus{s..t}/new.json",
                        "cd {HOME}/.config && touch tmux-p{i..i}/trust/new.json",
                        "cd {CONFIG} && echo x > t{r,r}u{s,s}t/new.json"):
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertDenied(command, worker=worker)
        # The store's name spelled through braces is read as the name, wherever the path would land.
        for command in ("touch {SCRATCH}/tmux-p{i,i}/trust/new.json", "touch $DIR/tmux-pi/tr{u..u}st/new.json",
                        "echo x > {SCRATCH}/tmux-pi/{state,cache}/trust/new.json"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command), "names the trust store")
        for command in ("for i in {1..5}; do echo $i; done", "touch {SCRATCH}/f{1..20}.txt", "echo {a..e}{01..10}",
                        "touch {SCRATCH}/{%s}.txt" % long_list, "awk '{print $1, $2}' notes.txt",
                        "curl -s -d '{\"a\":1,\"b\":2}' https://example.com"):
            with self.subTest(command=command):
                self.assertAllowed(command)

    def test_braces_with_more_alternatives_than_the_guard_writes_out_are_asked_about_far_from_the_store(self):
        # Nothing names the store or reaches it, and no root in the call holds it or lies inside it: the owner is asked.
        for command in ("echo {1..100000}", "echo {a,b}{1..3000}"):
            self.assertAsk(command, reason=trust_guard.BRACE_REASON)
        with mock.patch.object(trust_guard, "_BRACE_CHOICES", 3):
            self.assertAsk("touch {SCRATCH}/{a,b,c,d}.txt", reason=trust_guard.BRACE_REASON)
            self.assertAsk("touch {SCRATCH}/f{1..4}.txt", reason=trust_guard.BRACE_REASON)
            self.assertAllowed("touch {SCRATCH}/{a,b,c}.txt")
            self.assertAllowed("touch {SCRATCH}/f{1..3}.txt")

    def test_a_cd_through_a_pattern_goes_where_the_shell_would(self):
        self.assertDenied("cd ~/.config && cd tmux-* && touch trust/new.json")
        self.assertEqual(self.bash("cd ~/.config && cd tmux-* && cd tr* && touch new.json"), "reaches the trust store")
        self.assertEqual(self.bash("cd ~/.config && cd tmux-* && cd t[r]ust; touch new.json"), "reaches the trust store")
        self.assertDenied("cd ~/.config && cd tmux-p{i,i} && touch trust/new.json")
        # Two matches next to the store: the shell's choice is not known, so nothing after it can be placed.
        os.makedirs(os.path.join(self.home, ".config", "tmux-pi.bak"))
        self.assertEqual(self.bash("cd ~/.config && cd tmux-* && touch x"),
                         "cd through a pattern next to the trust store cannot be placed")
        self.assertAllowed("cd {PLAIN}/* && ls")
        self.assertAllowed("cd {PLAIN}/a* && cat one.txt")

    def test_the_text_of_a_message_is_still_one_path(self):
        spaced = os.path.join(self.tmp, "a b")
        os.makedirs(spaced)
        os.symlink(self.trust, os.path.join(spaced, "l"))
        self.assertEqual(self.bash("git commit -F '{SCRATCH}/../a b/l/rec.json'"), "reaches the trust store")
        self.assertAllowed("git commit -F '{SCRATCH}/../a b/notes.txt'")
        self.assertAllowed("git commit -m 'Protect ~/.config/tmux-pi/trust from tp-trust grant'")

    def test_a_path_far_below_the_store_is_found_by_every_ancestor(self):
        # On macOS the same directory answers under /System/Volumes/Data, where no text matches and only the
        # ancestors' inodes do. The walk up must not stop before it reaches one that exists.
        alias = "/System/Volumes/Data" + self.trust
        if not os.path.isdir(alias):
            self.skipTest("no second name for the same directory on this system")
        guard = trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)
        for levels in (1, 63, 64, 80, 200):
            with self.subTest(levels=levels):
                self.assertEqual(guard.where(alias + "/d" * levels), "inside")
        self.assertIsNone(guard.where("/System/Volumes/Data" + self.proj + "/d" * 80))
        # The store's own directory is counted with one stat, and the directories above it with as many as there are.
        deep_home = os.path.join(self.tmp, *["h"] * 70)
        os.makedirs(os.path.join(deep_home, ".config", "tmux-pi", "trust"))
        guard = trust_guard.Guard(home=deep_home, worker=True, cwd=self.proj)
        self.assertEqual(guard.where(self.tmp), "above")
        self.assertEqual(guard.where("/System/Volumes/Data" + self.tmp), "above")

    def test_the_names_find_and_xargs_fill_in_are_unresolved(self):
        # `{}` is decided while the command runs, so it counts like a variable next to the store as a path.
        self.assertDenied("ls ~/.config/tmux-pi/state | xargs -I{} cat ~/.config/tmux-pi/state/{}")
        self.assertDenied("find {SCRATCH} -name f -exec cat {HOME}/.config/tmux-pi/{} \\;")
        self.assertAllowed("find . -name '*.py' -exec grep -l trust {} +")
        self.assertAllowed("ls lib | xargs -I{} wc -l lib/{}")


# Brace lists and ranges over the budget, with nothing near the store: the guard cannot write them out, and asks.
BRACE_ASKS = ["echo {1..100000}", "echo {a,b}{1..3000}", "touch {SCRATCH}/f{1..100000}.txt",
              "for i in {1..100000}; do echo $i; done", "cat {PLAIN}/{1..100000}.txt", "echo {1..100000} {1..100000}",
              "echo {a..z}{a..z}{a..z}{a..z}", "echo {1..100000..2}"]


class AskTests(Fixture):
    """The third answer: a budget ran out on a tree far from the store, and nothing else in the call is a reason to deny."""

    def small(self, entries=3):
        return mock.patch.object(trust_guard, "_WALK_ENTRIES", entries)

    def test_the_three_answers_and_the_shape_callers_use(self):
        self.assertEqual(self.judge("git diff"), trust_guard.Answer(trust_guard.ALLOW, None))
        self.assertEqual(self.judge("echo x > {TRUST}/a").kind, trust_guard.DENY)
        with self.small():
            answer = self.judge("find -L {PLAIN} -name x")
            self.assertEqual(answer, (trust_guard.ASK, trust_guard.WALK_REASON))
            # `check` keeps its old shape: a reason for anything that may not simply go on, so a caller that only knows
            # allow and deny treats an ask as a deny.
            self.assertEqual(trust_guard.check("Bash", {"command": self.sub("find -L {PLAIN} -name x")}, self.proj,
                                               home=self.home, worker=True), trust_guard.WALK_REASON)
            # `check_within` gives the whole answer.
            self.assertEqual(trust_guard.check_within(5, "Bash", {"command": self.sub("find -L {PLAIN} -name x")},
                                                      self.proj, home=self.home, worker=True), answer)

    def test_the_three_commands_from_a_large_repository_ask_for_a_worker_and_for_main(self):
        with self.small():
            for command in ("grep -R foo .", "find -L . -name x", "tar -czhf {SCRATCH}/a.tgz ."):
                for worker in (True, False):
                    with self.subTest(command=command, worker=worker):
                        self.assertAsk(command, cwd=self.plain, worker=worker)

    def test_a_link_into_the_store_found_within_the_budget_still_denies(self):
        # The same walks, in a tree whose link to the store is within reach of the budget.
        for command in ("grep -R foo .", "find -L . -name x", "tar -czhf {SCRATCH}/a.tgz ."):
            with self.subTest(command=command):
                self.assertHardDeny(command, cwd=self.tree, reason="walks through a link into the trust store")
        # Cut short, the walk may not have got to the link: an ask or a deny, never an allow.
        with self.small(2):
            self.assertNotEqual(self.judge("grep -R foo .", cwd=self.tree).kind, trust_guard.ALLOW)

    def test_a_root_that_holds_the_store_over_budget_still_denies(self):
        with self.small(0):
            # A bounded walk through the home directory: it holds the store, so the budget running out is no ask.
            self.assertHardDeny("find -L {HOME} -maxdepth 1 -name x", reason=trust_guard.WALK_REASON)
            self.assertHardDeny("find -L . -maxdepth 1 -name x", cwd=self.home, reason=trust_guard.WALK_REASON)
            # Globs: a front that holds the store, or lies inside it, or climbs with `..`.
            self.assertHardDeny("ls {HOME}/.config/*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("ls {CONFIG}/*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("ls /*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("ls {PLAIN}/*/../*", reason=trust_guard.GLOB_REASON)
            Path(self.trust, "other.json").write_text("{}")
            self.assertHardDeny("cat {TRUST}/*.json")
        with mock.patch.object(trust_guard, "_WALK_SECONDS", -1):
            self.assertHardDeny("ls {HOME}/.config/*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("find -L {HOME} -maxdepth 1 -name x", reason=trust_guard.WALK_REASON)

    def test_the_rest_of_the_call_is_still_judged_after_the_budget_runs_out(self):
        later = ["cat {TRUST}/rec.json", "echo x > ~/.config/tmux-pi/trust/a.json", "tp-trust grant .",
                 "cat {LINK}/rec.json", "cat $HOME/.config/tmux-pi/trust/rec.json", "cd {TRUST} && ls",
                 "export TMPDIR={CONFIG}; echo x > out", "cat $X/trust/rec.json", "ls {TREE}/***/rec.json",
                 "find -L {TREE} -name x"]
        with self.small():
            for walk in ("grep -R foo .", "find -L . -name x"):
                for rest in later:
                    for join in (";", "&&", "||"):
                        with self.subTest(walk=walk, rest=rest, join=join):
                            self.assertHardDeny("%s %s %s" % (walk, join, rest), cwd=self.plain)
                    with self.subTest(rest=rest, first=True):
                        self.assertHardDeny("%s ; grep -R foo ." % rest, cwd=self.plain)
            self.assertHardDeny("grep -R foo . ; tmux send-keys -t 0 x", cwd=self.plain)
            self.assertEqual(self.judge("grep -R foo . ; tmux send-keys -t 0 x", cwd=self.plain, worker=False).kind,
                             trust_guard.ASK)

    def test_every_walk_gets_its_own_budget_so_a_later_link_is_still_found(self):
        # {PLAIN}/a has four entries and {TREE} three, the last one a link to the store.
        with self.small(3):
            self.assertAsk("grep -R foo {PLAIN}/a")
            self.assertHardDeny("grep -R foo {PLAIN}/a ; find -L {TREE} -name x",
                                reason="walks through a link into the trust store")

    def test_a_glob_that_runs_out_far_from_the_store_asks_and_the_rest_is_judged(self):
        with self.small(0):
            self.assertAsk("cat {PLAIN}/a/*", reason=trust_guard.GLOB_REASON)
            self.assertHardDeny("cat {PLAIN}/a/* {TRUST}/rec.json")
            self.assertHardDeny("cat {PLAIN}/a/* ; cat {HOME}/.config/*", reason=trust_guard.GLOB_REASON)
            # The tools take a glob as a pattern.
            answer = trust_guard.judge("Glob", {"pattern": self.plain + "/**/*"}, self.proj, home=self.home, worker=True)
            self.assertEqual(answer, (trust_guard.ASK, trust_guard.GLOB_REASON))
            answer = trust_guard.judge("Glob", {"pattern": self.config + "/**/*"}, self.proj, home=self.home, worker=True)
            self.assertEqual(answer.kind, trust_guard.DENY)

    def test_a_cd_through_a_pattern_that_ran_out_is_not_placed(self):
        for name in ("d1", "d2"):
            os.makedirs(os.path.join(self.tmp, "multi", name))
        with self.small(1):
            guard = trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)
            # The first match alone would be taken for the one the shell goes to.
            self.assertIsNone(guard._chdir("", os.path.join(self.tmp, "multi", "*")))
        self.assertEqual(guard.asks, [trust_guard.GLOB_REASON])
        self.assertEqual(trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)._chdir(
            "", os.path.join(self.tmp, "multi", "d?")), None)    # two matches: not one place either

    # ---- a brace list or range over the budget --------------------------------------------------

    def test_a_long_brace_list_far_from_the_store_asks_for_a_worker_and_for_main(self):
        for command in BRACE_ASKS:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    answer = self.assertAsk(command, reason=trust_guard.BRACE_REASON, worker=worker)
                    self.assertIn("too many values to check", answer.reason)
        with mock.patch.object(trust_guard, "_BRACE_CHOICES", 3):
            for command in ("touch {SCRATCH}/{a,b,c,d}.txt", "touch {SCRATCH}/f{1..4}.txt", "cat {PLAIN}/a/{one,two,x,y}.txt"):
                with self.subTest(command=command):
                    self.assertAsk(command, reason=trust_guard.BRACE_REASON)
            self.assertAllowed("touch {SCRATCH}/{a,b,c}.txt")

    def test_a_range_is_never_written_out_past_the_budget(self):
        # The count is checked before the values are made, so a long range costs nothing to refuse.
        for text in ("{1..100000}", "{1..200000}", "{1..100000..2}", "{-99999..99999}"):
            with self.subTest(text=text):
                with self.assertRaises(trust_guard._TooLong):
                    trust_guard._range(trust_guard._RANGE.search(text))
        self.assertEqual(len(trust_guard._range(trust_guard._RANGE.search("{1..4096}"))), 4096)
        with self.assertRaises(trust_guard._TooLong):
            trust_guard._range(trust_guard._RANGE.search("{1..4097}"))

    def test_a_long_brace_list_that_names_the_store_still_denies(self):
        with mock.patch.object(trust_guard, "_BRACE_CHOICES", 3):
            for command in ("touch {SCRATCH}/{a,b,c,d,trust}/x", "touch {SCRATCH}/{trust,b,c,d,e}/x",
                            "touch {SCRATCH}/{a,b,c,d,TRUST}/x", "touch {SCRATCH}/{a,b,c,d}/tmux-pi/x",
                            "cd {CONFIG} && touch {a,b,c,d,trust}/new.json", "touch {SCRATCH}/{a,b,c,d,tmux-pi}",
                            "touch {SCRATCH}/{a,b,c,d}/trust", "touch ~/.config/tmux-pi/{a,b,c,d}"):
                with self.subTest(command=command):
                    self.assertHardDeny(command)
            self.assertHardDeny("touch {SCRATCH}/{a,b,c,d,trust}/x", reason=trust_guard.NAMES)
        for command in ("echo {1..100000}trust", "echo {a,b}{1..3000}/trust/x", "echo {1..100000}/tmux-pi",
                        "echo {CONFIG}/{1..100000}",
                        "cat ~/.config/tmux-pi/{1..100000}", "cat ~/.config/{1..100000}/trust"):
            with self.subTest(command=command):
                self.assertHardDeny(command)
        long_list = ",".join("a%d" % i for i in range(5000))
        self.assertHardDeny("touch {SCRATCH}/{%s,trust}/x" % long_list, reason=trust_guard.NAMES)

    def test_a_long_brace_list_next_to_the_store_denies(self):
        # The directory the command runs in, or the directory in front of the braces, holds the store or lies inside it.
        for command, cwd in (("echo {1..100000}", self.home), ("echo {1..100000}", os.path.join(self.home, ".config")),
                             ("cd ~ && echo {1..100000}", None), ("cd {HOME}/.config && echo {1..100000}", None),
                             ("echo {HOME}/{1..100000}", None), ("echo {HOME}/.config/{1..100000}", None),
                             ("echo ~/{1..100000}", None), ("echo {HOME}/.config/f{1..100000}", None),
                             ("cd .. && echo {1..100000}", None), ("echo ../{1..100000}", None)):
            for worker in (True, False):
                with self.subTest(command=command, cwd=cwd, worker=worker):
                    self.assertHardDeny(command, cwd=cwd or self.home, worker=worker, reason=trust_guard.BRACE_NEAR)
        self.assertAsk("echo {PLAIN}/{1..100000}", reason=trust_guard.BRACE_REASON)
        self.assertAsk("cd {PLAIN} && echo {1..100000}", reason=trust_guard.BRACE_REASON)

    def test_a_long_brace_list_in_a_word_that_depends_on_run_time_state_denies(self):
        for command in ("echo $X{1..100000}", "echo {1..100000}/../x", "echo {1..100000}*", "echo `pwd`{1..100000}",
                        "echo {PLAIN}/*/{1..100000}", "echo \\~/{1..100000}", "cd $X && echo {1..100000}",
                        "echo {a,$HOME,b}{1..3000}", "echo {PLAIN}/[ab]{1..100000}", "echo {a,b..c}{1..3000}"):
            with self.subTest(command=command):
                self.assertHardDeny(command, reason=trust_guard.BRACE_NEAR)

    def test_the_rest_of_the_call_is_judged_around_a_brace_list_that_was_asked_about(self):
        for rest in ("cat {TRUST}/rec.json", "echo x > {STATE}/a", "tp-trust grant .", "cat {LINK}/rec.json",
                     "cd {TRUST}", "grep -R foo {TREE}", "ls tmux-pi/state/", "ls {PLAIN}/$X/trust/x"):
            for command in ("echo {1..100000}; %s" % rest, "%s; echo {1..100000}" % rest, "echo {1..100000} && %s" % rest):
                with self.subTest(command=command):
                    self.assertHardDeny(command)
        # The word that could not be written out counts as one whose value is not known, next to a command that names the store.
        self.assertAllowed("ls tmux-pi/state/")
        self.assertHardDeny("echo {1..100000}; ls tmux-pi/state/", reason=trust_guard.RUNTIME_PATH)
        self.assertEqual(self.judge("echo {1..100000}; cat {PLAIN}/a/one.txt").kind, trust_guard.ASK)
        self.assertEqual(self.judge("echo {1..100000}; echo {1..100000}").kind, trust_guard.ASK)

    def test_a_cd_into_a_long_brace_list_is_not_placed(self):
        self.assertAsk("cd {SCRATCH}/{1..100000}", reason=trust_guard.BRACE_REASON)
        self.assertHardDeny("cd {SCRATCH}/{1..100000} && cat trust/rec.json")
        self.assertHardDeny("cd {SCRATCH}/{1..100000}; cd ../home/.config; ls tmux-pi/trust")

    def test_the_brace_ask_is_what_the_callers_say(self):
        out = json.loads(self.main(self.event("echo {1..100000}"), "--claude-hook", "--home", self.home))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "ask")
        self.assertTrue(out["permissionDecisionReason"].startswith(trust_guard.ASK_ROOT))
        self.assertIn("brace list or range with too many values to check", out["permissionDecisionReason"])
        self.assertNotIn("tp-trust", out["permissionDecisionReason"])
        gate = json.loads(self.main(self.event("echo {1..100000}"), "--home", self.home))
        self.assertEqual((gate["deny"], gate["ask"]), (True, True))
        self.assertIn("too many values to check", gate["reason"])
        self.assertEqual([(r["event"], r["reason"]) for r in self.log()],
                         [("trust_guard_ask", trust_guard.BRACE_REASON)] * 2)
        out = json.loads(self.main(self.event("echo {1..100000}/trust"), "--claude-hook", "--home", self.home))["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")

    # ---- the whole-judgement time limit -------------------------------------------------------

    def timed(self, command, seconds=0.2):
        return trust_guard.check_within(seconds, "Bash", {"command": self.sub(command)}, self.proj, home=self.home,
                                        worker=True)

    @staticmethod
    def slow_scandir(pause=0.5):
        real = os.scandir

        def scandir(path):
            time.sleep(pause)
            return real(path)
        return mock.patch.object(os, "scandir", scandir)

    @staticmethod
    def slow_glob(pause=0.5):
        real = glob.iglob

        def iglob(*args, **kw):
            for hit in real(*args, **kw):
                time.sleep(pause)
                yield hit
        return mock.patch.object(glob, "iglob", iglob)

    def test_the_time_limit_in_the_middle_of_a_walk_far_from_the_store_asks(self):
        started = time.monotonic()
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            answer = self.timed("find -L {PLAIN} -name x")
        self.assertEqual(answer, (trust_guard.ASK, trust_guard.WALK_REASON))
        self.assertLess(time.monotonic() - started, 3)

    def test_the_time_limit_in_the_middle_of_a_glob_far_from_the_store_asks(self):
        with self.slow_glob(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            self.assertEqual(self.timed("cat {PLAIN}/a/*"), (trust_guard.ASK, trust_guard.GLOB_REASON))

    def test_the_time_limit_in_a_walk_that_holds_the_store_denies(self):
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            answer = self.timed("find -L {HOME} -maxdepth 1 -name x")
        self.assertEqual(answer, (trust_guard.DENY, trust_guard.WALK_REASON))
        with self.slow_glob(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            self.assertEqual(self.timed("cat {HOME}/.config/*").kind, trust_guard.DENY)

    def test_what_is_left_of_the_call_is_judged_after_the_time_limit_ended_a_walk(self):
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            answer = self.timed("find -L {PLAIN} -name x ; cat {TRUST}/rec.json")
        self.assertEqual(answer, (trust_guard.DENY, "names the trust store"))
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100):
            # A walk after the limit ended one is not looked through at all, and so is no reason to deny by itself,
            # but one that holds the store still is.
            self.assertEqual(self.timed("find -L {PLAIN} -name x ; find -L {TREE} -name y").kind, trust_guard.DENY)

    def test_a_second_expiry_outside_a_walk_denies(self):
        real = trust_guard.Guard._closing

        def slow(guard, run):
            if guard.timed_out:
                time.sleep(1)
            return real(guard, run)
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100), \
                mock.patch.object(trust_guard, "_GRACE_SECONDS", 0.1), mock.patch.object(trust_guard.Guard, "_closing", slow):
            started = time.monotonic()
            answer = self.timed("find -L {PLAIN} -name x")
        self.assertEqual(answer, (trust_guard.DENY, "the tool call could not be analysed in time"))
        self.assertLess(time.monotonic() - started, 0.9)

    def test_a_second_expiry_in_a_later_walk_or_glob_denies(self):
        with self.slow_scandir(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100), \
                mock.patch.object(trust_guard, "_GRACE_SECONDS", 0.1):
            self.assertEqual(self.timed("find -L {PLAIN} -name x ; find -L {PLAIN} -name y"),
                             (trust_guard.DENY, "the tool call could not be analysed in time"))
        with self.slow_glob(), mock.patch.object(trust_guard, "_WALK_SECONDS", 100), \
                mock.patch.object(trust_guard, "_GRACE_SECONDS", 0.1):
            self.assertEqual(self.timed("cat {PLAIN}/a/* ; cat {PLAIN}/a/*"),
                             (trust_guard.DENY, "the tool call could not be analysed in time"))

    def test_the_time_limit_outside_a_walk_denies(self):
        def slow(*_args, **_kw):
            time.sleep(5)
        with mock.patch.object(trust_guard.Guard, "_programs", slow):
            self.assertEqual(self.timed("ls"), (trust_guard.DENY, "the tool call could not be analysed in time"))

    # ---- what the callers see ------------------------------------------------------------------

    def main(self, event, *args):
        """Run bin/tp-trust-guard in this process, so the budgets patched above apply; returns what it printed."""
        out = io.StringIO()
        env = {"TP_TRUST_GUARD_LOG_DIR": os.path.join(self.tmp, "log")}
        with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", [CLI, *args]), \
                mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), mock.patch.object(sys, "stdout", out):
            runpy.run_path(CLI, run_name="__main__")
        return out.getvalue()

    def event(self, command, cwd=None):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": self.sub(command)},
                "cwd": cwd or self.plain}

    def log(self):
        with open(os.path.join(self.tmp, "log", "guard.jsonl")) as handle:
            return [json.loads(line) for line in handle]

    def test_the_claude_hook_asks_with_the_reason_and_logs_it(self):
        with self.small():
            out = json.loads(self.main(self.event("grep -R foo ."), "--claude-hook", "--home", self.home))["hookSpecificOutput"]
        self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "ask"))
        self.assertIn("too large to check for links into the trust store", out["permissionDecisionReason"])
        self.assertTrue(out["permissionDecisionReason"].startswith(trust_guard.ASK_ROOT))
        self.assertNotIn(trust_guard.DENY_ROOT, out["permissionDecisionReason"])
        self.assertEqual([(r["event"], r["reason"]) for r in self.log()], [("trust_guard_ask", trust_guard.WALK_REASON)])

    def test_the_claude_hook_still_denies_what_is_named_and_what_a_root_holds(self):
        with self.small(1):
            for command in ("grep -R foo . ; cat {TRUST}/rec.json", "find -L {HOME} -maxdepth 1 -name x"):
                out = json.loads(self.main(self.event(command), "--claude-hook", "--home", self.home))["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "deny", command)
                self.assertTrue(out["permissionDecisionReason"].startswith(trust_guard.DENY_ROOT))
        self.assertEqual({r["event"] for r in self.log()}, {"trust_guard_deny"})

    def test_the_pi_gate_marks_an_ask_and_still_blocks_a_reader_that_does_not_know_it(self):
        with self.small():
            gate = json.loads(self.main(self.event("grep -R foo ."), "--home", self.home))
        self.assertEqual(set(gate), {"deny", "ask", "reason"})
        self.assertIs(gate["ask"], True)
        self.assertIs(gate["deny"], True)         # an old reader that only looks at `deny` blocks
        self.assertTrue(gate["reason"].startswith(trust_guard.ASK_ROOT))
        self.assertIn("too large to check for links into the trust store", gate["reason"])
        deny = json.loads(self.main(self.event("echo x > {TRUST}/a"), "--home", self.home))
        self.assertEqual(set(deny), {"deny", "reason"})
        self.assertIs(deny["deny"], True)
        self.assertEqual(json.loads(self.main(self.event("git diff"), "--home", self.home)), {"deny": False, "reason": None})

    def test_the_cli_goes_through_check_within_with_the_judgement_limit(self):
        seen = []

        def spy(seconds, tool, params, cwd=None, home=None, worker=None):
            seen.append((seconds, tool, params, cwd, home))
            return trust_guard.Answer(trust_guard.ASK, "spied")

        def never(*_args, **_kw):
            raise AssertionError("the CLI judged the call without the time limit")
        event = self.event("git diff")
        with mock.patch.object(trust_guard, "check_within", spy), mock.patch.object(trust_guard, "judge", never), \
                mock.patch.object(trust_guard, "check", never):
            out = json.loads(self.main(event, "--claude-hook", "--home", self.home))["hookSpecificOutput"]
            gate = json.loads(self.main(event, "--home", self.home))
        self.assertEqual(len(seen), 2)
        self.assertEqual({row[0] for row in seen}, {trust_guard._JUDGE_SECONDS})
        self.assertEqual(seen[0][1:], ("Bash", event["tool_input"], self.plain, self.home))
        self.assertEqual(out["permissionDecision"], "ask")      # what check_within answered is what the hook says
        self.assertIs(gate["ask"], True)


# Option values that look like flags, for every program the guard walks with. Each row: the command where the word after the
# option is that option's value, and what a tree over the budget makes of it; then the command where the same word is
# really a flag. A walk that follows links asks over the budget; one that follows nothing is allowed.
ALLOW, ASK = trust_guard.ALLOW, trust_guard.ASK
OPTION_VALUES = [
    ("grep -e -R foo .", ALLOW, "grep -R foo .", ASK),
    ("grep -eR foo .", ALLOW, "grep -nR foo .", ASK),
    ("grep -ne -R foo .", ALLOW, "grep -nR foo .", ASK),
    ("grep -r -f -R foo .", ALLOW, "grep -r -R foo .", ASK),
    ("grep --regexp -R foo .", ALLOW, "grep --regexp x -R .", ASK),
    ("grep --file -R x .", ALLOW, "grep --file x -R .", ASK),
    ("grep -m -R foo .", ALLOW, "grep -m 1 -R foo .", ASK),
    ("egrep -e -R foo .", ALLOW, "egrep -R foo .", ASK),
    ("fgrep -e -R foo .", ALLOW, "fgrep -R foo .", ASK),
    ("ggrep -e -R foo .", ALLOW, "ggrep -R foo .", ASK),
    ("zgrep -e -R foo .", ALLOW, "zgrep -R foo .", ASK),
    ("rgrep -e -R foo .", ALLOW, "rgrep -R foo .", ASK),
    ("rg -e --follow foo .", ALLOW, "rg --follow foo .", ASK),
    ("rg -g -L foo .", ALLOW, "rg -L foo .", ASK),
    ("rg --glob --follow foo .", ALLOW, "rg --glob x --follow foo .", ASK),
    ("rg -ng -L foo .", ALLOW, "rg -nL foo .", ASK),
    ("rg -ge -L foo .", ASK, "rg -eL foo .", ALLOW),      # `-ge`: `e` is the value of `-g`; `-eL`: `L` is the pattern
    ("ag -G -f foo .", ALLOW, "ag -f foo .", ASK),
    ("ag --ignore-dir --follow foo .", ALLOW, "ag --follow foo .", ASK),
    ("ack --ignore-dir --follow foo .", ALLOW, "ack --follow foo .", ASK),
    ("ack --type --follow foo .", ALLOW, "ack --type=py --follow foo .", ASK),
    ("fd -e -L foo .", ALLOW, "fd -L foo .", ASK),
    ("fd --extension --follow foo .", ALLOW, "fd --extension py --follow foo .", ASK),
    ("tree -P -l .", ALLOW, "tree -l .", ASK),
    ("tree -I -l .", ALLOW, "tree -I x -l .", ASK),
    ("du -B -L .", ALLOW, "du -L .", ASK),
    ("du --exclude -L .", ALLOW, "du --exclude x -L .", ASK),
    ("ls -R --ignore -L .", ALLOW, "ls -RL .", ASK),
    ("ls -R --hide -L .", ALLOW, "ls -R --hide x -L .", ASK),
    ("cp -r -S -L a {SCRATCH}/dst", ALLOW, "cp -rL a {SCRATCH}/dst", ASK),
    ("cp -r --suffix -L a {SCRATCH}/dst", ALLOW, "cp -r --suffix x -L a {SCRATCH}/dst", ASK),
    ("rsync -r -e -L a {SCRATCH}/dst/", ALLOW, "rsync -rL a {SCRATCH}/dst/", ASK),
    ("rsync -r --exclude -L a {SCRATCH}/dst/", ALLOW, "rsync -r --exclude x -L a {SCRATCH}/dst/", ASK),
    ("tar -cf -h .", ALLOW, "tar -chf {SCRATCH}/a.tgz .", ASK),
    ("tar cf -h .", ALLOW, "tar chf {SCRATCH}/a.tgz .", ASK),
    ("tar -c --file -h .", ALLOW, "tar -c --file {SCRATCH}/a.tgz -h .", ASK),
    ("tar -cf -L .", ALLOW, "tar -cL -f {SCRATCH}/a.tgz .", ASK),
    ("tar cf --dereference .", ALLOW, "tar c --dereference -f {SCRATCH}/a.tgz .", ASK),
    # These follow links unless told not to: the value form asks, and the genuine flag that stops it is allowed.
    ("zip -r -b -y {SCRATCH}/o.zip .", ASK, "zip -ry {SCRATCH}/o.zip .", ALLOW),
    ("zip -r -t -y {SCRATCH}/o.zip .", ASK, "zip -r -y {SCRATCH}/o.zip .", ALLOW),
    ("scp -c -r a host:dst", ALLOW, "scp -r a host:dst", ASK),
    ("scp -r -c -r a host:dst", ASK, "scp -rc x a host:dst", ASK),
    ("diff -x -r a a", ALLOW, "diff -r a a", ASK),
    ("diff -r -x -L a a", ASK, "diff --recursive a a", ASK),
    ("chmod -R --reference -L ref .", ALLOW, "chmod -RL 755 .", ASK),
    ("chown -R --reference -L ref .", ALLOW, "chown -RL root .", ASK),
    ("chgrp -R --reference -L ref .", ALLOW, "chgrp -RL staff .", ASK),
    (None, None, "chflags -RL nohidden .", ASK),
    ("find . -name -follow", ALLOW, "find . -follow", ASK),
    ("find . -name -L", ALLOW, "find -L . -name x", ASK),
    ("find . -type -follow", ALLOW, "find . -name x -follow", ASK),
    ("find . -newermt -follow", ALLOW, "find . -newer x -follow", ASK),
    ("find . -newer -follow", ALLOW, "find . -maxdepth 3 -follow", ASK),
    ("find . -fprintf -follow -follow", ALLOW, "find . -fprintf x y -follow", ASK),
    ("find . -iname -follow -print", ALLOW, "find . -print -follow", ASK),
    ("find -D tree . -name -follow", ALLOW, "find -D tree . -follow", ASK),
]


class OptionValues(Fixture):
    """A word after an option that takes a value is that value and not a flag, for every program the guard walks with."""

    def kind(self, command, worker=True):
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 0):
            answer = self.judge(command, cwd=self.plain, worker=worker)
        self.assertNotEqual(answer.kind, trust_guard.DENY, "%s: %s" % (answer, command))
        if answer.kind == ASK:
            self.assertEqual(answer.reason, trust_guard.WALK_REASON, command)
        return answer.kind

    def test_every_row_has_the_value_form_and_the_flag_form(self):
        for value, value_kind, flag, flag_kind in OPTION_VALUES:
            for worker in (True, False):
                with self.subTest(command=flag, worker=worker):
                    self.assertEqual(self.kind(flag, worker), flag_kind)
                if value:
                    with self.subTest(command=value, worker=worker):
                        self.assertEqual(self.kind(value, worker), value_kind)

    def test_the_rows_cover_every_program_the_guard_walks_with(self):
        covered = {(row[0] or row[2]).split()[0] for row in OPTION_VALUES}
        self.assertEqual(set(trust_guard._WALKERS) - covered, {"ditto"})
        # ditto has no option that follows links, so no value of one can make it ask.
        self.assertEqual(trust_guard._WALKERS["ditto"][1], set())
        self.assertEqual(set(trust_guard._VALUE_OPTIONS), set(trust_guard._WALKERS) - {"find"})

    def test_a_value_form_row_is_allowed_with_the_normal_budget_too(self):
        for value, _, flag, _ in OPTION_VALUES:
            for command in (value, flag):
                if command:
                    with self.subTest(command=command):
                        self.assertNotEqual(self.judge(command, cwd=self.plain).kind, trust_guard.DENY)
                        self.assertIsNone(self.bash(command, cwd=self.plain))

    def test_the_options_each_program_reads_a_value_for(self):
        flags = trust_guard._flags
        self.assertEqual(flags("grep", ["-e", "-R", "foo", "."]), {"-e"})
        self.assertEqual(flags("grep", ["-eR", "foo", "."]), {"-eR", "-e"})
        self.assertEqual(flags("grep", ["-nR", "foo", "."]), {"-nR", "-n", "-R"})
        self.assertEqual(flags("grep", ["-R", "-e", "-r"]), {"-R", "-e"})
        self.assertEqual(flags("grep", ["--regexp", "--follow"]), {"--regexp"})
        self.assertEqual(flags("grep", ["--regexp=-R"]), {"--regexp=-R", "--regexp"})
        self.assertEqual(flags("rg", ["-e", "--follow", "x"]), {"-e"})
        self.assertEqual(flags("rg", ["--follow", "-e", "x"]), {"--follow", "-e"})
        self.assertEqual(flags("tar", ["cf", "-h", "."]), {"-c", "-f"})
        self.assertEqual(flags("tar", ["chf", "x", "."]), {"-c", "-h", "-f"})
        self.assertEqual(flags("tar", ["cfC", "-h", "-L", "."]), {"-c", "-f", "-C"})
        self.assertEqual(flags("ditto", ["--arch", "-v", "a"]), {"--arch"})
        self.assertEqual(flags("chmod", ["-R", "--reference", "-L", "x"]), {"-R", "--reference"})
        # Options that are flags in some versions and take a value in others, and unknown programs, are read as flags.
        self.assertEqual(flags("grep", ["-E", "-R"]), {"-E", "-R"})
        self.assertEqual(flags("nothing", ["-e", "-R"]), {"-e", "-R"})
        # What follows a `--` is not an option.
        self.assertEqual(flags("grep", ["--", "-R"]), set())
        find = trust_guard._find_parts
        self.assertEqual(find([".", "-name", "-follow"]), (set(), ["."]))
        self.assertEqual(find([".", "-follow"]), ({"-follow"}, ["."]))
        self.assertEqual(find([".", "-fprintf", "-follow", "-follow"]), (set(), ["."]))
        self.assertEqual(find(["-L", ".", "-name", "x"]), ({"-L"}, ["."]))
        self.assertEqual(find([".", "-name", "x", "-o", "-follow"]), ({"-follow"}, ["."]))


def _call(command, cwd=None, tool="Bash", **options):
    params = command if isinstance(command, dict) or command is None else {"command": command}
    return dict(tool=tool, params=params, cwd=cwd, **options)


def _deep_command():
    return "echo " + "$(echo " * 8 + "x" + ")" * 8


# A call that makes the guard answer with each reason, as `Fixture.sub` placeholders and patches: name -> the call.
# `patch` names a module value to set while it is judged; `slow` makes the judgement take too long; `broken` makes it fail.
REASON_CALLS = {
    "NAMES": _call("cat {TRUST}/rec.json"),
    "REACHES": _call("cat {LINK}/rec.json"),
    "CWD_INSIDE": _call("cd {CONFIG}/lnk2/../trust && ls"),
    "CWD_ROOT_INSIDE": _call("ls", cwd="{TRUST}"),
    "CWD_UNPLACED": _call("git diff", cwd="relative/dir"),
    "TP_TRUST": _call("tp-trust grant ."),
    "TRUST_DIR": _call("tp-trust list --trust-dir=/tmp/other"),
    "TMUX_PANE": _call("tmux send-keys -t 0 ls Enter"),
    "CDPATH": _call("CDPATH=. cd lib"),
    "ENV_SET": _call("export XDG_CONFIG_HOME={HOME}/.config; touch x"),
    "RUNTIME_PATH": _call("cat $DIR/trust/rec.json"),
    "LISTED_THEN_PASSED": _call("find {HOME}/.config -maxdepth 1 | xargs cat"),
    "INLINE_RUNTIME": _call("python3 -c \"import os; print(os.path.expanduser('~') + '/.config/tmux-pi/')\""),
    "INLINE_GRANT": _call("python3 -c \"import subprocess; subprocess.run(['tp-trust','grant','.'])\""),
    "INLINE_TMUX": _call("python3 -c \"import os; os.system('tmux send-keys -t 0 ls Enter')\""),
    "TOO_DEEP": _call(_deep_command()),
    "EDITS_ABOVE": _call("rm -rf {HOME}/.config"),
    "CD_PATTERN": _call("cd ~/.config && cd tmux-* && touch x", extra_dir="{HOME}/.config/tmux-pi.bak"),
    "UNKNOWN_PROGRAM": _call("mystery {HOME}/.config"),
    "WALK_ABOVE": _call("grep -r foo {HOME}/.config"),
    "WALK_LINK": _call("ls {TREE}/***/rec.json"),
    "WALK_REASON": _call("grep -R foo .", cwd="{PLAIN}", patch=("_WALK_ENTRIES", 0)),
    "GLOB_REASON": _call("cat {PLAIN}/a/*", patch=("_WALK_ENTRIES", 1)),
    "GLOBSTAR_CLIMB": _call("ls {SCRATCH}/**/../x"),
    "BRACE_REASON": _call("echo {1..100000}"),
    "BRACE_NEAR": _call("echo ~/{1..100000}"),
    "SEARCH_ABOVE": _call({"pattern": "x", "path": "{HOME}"}, tool="Grep"),
    "PARAMS_TYPE": _call(None),
    "COMMAND_TYPE": _call({"command": None}),
    "PARAM_BUDGET": _call({"items": ["plain"] * 50}, tool="mcp__other__batch", patch=("_PARAM_VALUES", 20)),
    "PARAM_KEY": _call({"data": {3: "x"}}, tool="mcp__other__save"),
    "PARAM_VALUE": _call({"data": {"inner": b"/x"}}, tool="mcp__other__save"),
    "ANALYSIS_FAILED": _call("ls", broken=True),
    "ANALYSIS_TIMED": _call("ls", slow=True),
}
# `WALK_INSIDE` is a second line of defence: a call that reaches it is already refused by an earlier check, so the walk is
# tested on the guard itself.
UNREACHABLE = {"WALK_INSIDE"}
# The reasons whose remedy is about the trust store; every other remedy is about the call and must not send the reader to tp-trust.
STORE_REMEDIES = {"NAMES", "REACHES", "CWD_INSIDE", "TP_TRUST", "TRUST_DIR", "RUNTIME_PATH", "INLINE_RUNTIME", "INLINE_GRANT",
                  "EDITS_ABOVE", "WALK_INSIDE"}


class ReasonRemedies(Fixture):
    """Every reason the guard can give carries its own next step, and the callers print that one."""

    def reasons(self):
        names = {id(v): k for k, v in vars(trust_guard).items() if isinstance(v, trust_guard.Why)}
        return {names[id(v)]: v for v in trust_guard.REASONS}

    def answer(self, call):
        params = self.deep(call["params"]) if call["params"] is not None else None
        cwd = self.sub(call["cwd"]) if call.get("cwd") else self.proj
        if call.get("extra_dir"):
            os.makedirs(self.sub(call["extra_dir"]), exist_ok=True)
        with contextlib.ExitStack() as stack:
            if call.get("patch"):
                stack.enter_context(mock.patch.object(trust_guard, *call["patch"]))
            seconds = 5
            if call.get("slow"):
                seconds = 0.2
                stack.enter_context(mock.patch.object(trust_guard.Guard, "command", lambda *_a, **_k: time.sleep(5)))
            if call.get("broken"):
                stack.enter_context(mock.patch.object(trust_guard.Guard, "command", side_effect=ValueError("broken")))
            return trust_guard.check_within(seconds, call["tool"], params, cwd, home=self.home, worker=call.get("worker", True))

    def test_every_reason_has_a_remedy_and_every_remedy_is_about_its_own_cause(self):
        reasons = self.reasons()
        self.assertEqual(len(reasons), len(trust_guard.REASONS))
        self.assertGreaterEqual(len(reasons), 35)
        self.assertEqual(set(reasons), set(REASON_CALLS) | UNREACHABLE)
        for name, reason in reasons.items():
            with self.subTest(reason=name):
                self.assertTrue(reason.remedy.strip().endswith("."), reason.remedy)
                self.assertGreater(len(reason.remedy), 20)
                if name in STORE_REMEDIES:
                    self.assertIn("owner", reason.remedy)
                else:
                    self.assertNotIn("Trust records are made", reason.remedy)

    def test_a_limit_or_parse_reason_does_not_tell_the_reader_to_make_a_trust_record(self):
        for name in ("TOO_DEEP", "CDPATH", "ENV_SET", "BRACE_REASON", "BRACE_NEAR", "WALK_REASON", "GLOB_REASON", "GLOBSTAR_CLIMB",
                     "WALK_ABOVE", "SEARCH_ABOVE", "PARAMS_TYPE", "COMMAND_TYPE", "PARAM_BUDGET", "PARAM_KEY", "PARAM_VALUE",
                     "ANALYSIS_FAILED", "ANALYSIS_TIMED", "CWD_UNPLACED", "CD_PATTERN", "LISTED_THEN_PASSED"):
            remedy = self.reasons()[name].remedy
            with self.subTest(reason=name):
                self.assertNotIn("tp-trust", remedy)
                self.assertNotIn("Trust records are made", remedy)
        self.assertIn("Split", trust_guard.TOO_DEEP.remedy)
        self.assertIn("CDPATH", trust_guard.CDPATH.remedy)
        self.assertIn("Split", trust_guard.BRACE_REASON.remedy)
        self.assertIn("narrower", trust_guard.WALK_REASON.remedy)

    def test_no_reason_is_a_bare_string(self):
        # Every `return`/`raise` of a reason in the guard names one of the constants, so each carries its remedy.
        tree = ast.parse(Path(trust_guard.__file__).read_text())
        bare = []
        for node in ast.walk(tree):
            call = isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("_Deny", "Answer")
            if call and any(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args):
                bare.append(node.lineno)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in ("append",) and \
                    getattr(getattr(node.func, "value", None), "attr", "") == "asks":
                if any(isinstance(a, ast.Constant) for a in node.args):
                    bare.append(node.lineno)
        for function in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name != "where"):
            for node in ast.walk(function):
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    bare.append(node.lineno)
        self.assertEqual(bare, [])

    def test_each_reason_can_be_reached_and_is_the_one_given(self):
        reasons = self.reasons()
        for name, call in REASON_CALLS.items():
            with self.subTest(reason=name):
                answer = self.answer(call)
                self.assertIs(answer.reason, reasons[name], "%s: %s" % (name, answer))
                self.assertEqual(answer.kind, trust_guard.ASK if name in ("WALK_REASON", "GLOB_REASON", "BRACE_REASON") else trust_guard.DENY)

    def test_the_walk_of_a_directory_in_the_store_names_its_own_remedy(self):
        guard = trust_guard.Guard(home=self.home, worker=True, cwd=self.proj)
        self.assertIs(guard._tree(self.trust, False, None, None), trust_guard.WALK_INSIDE)

    def test_the_claude_hook_and_the_pi_gate_print_the_remedy_of_the_reason_given(self):
        for name, call in REASON_CALLS.items():
            with self.subTest(reason=name):
                answer = self.answer(call)
                reason = answer.reason
                root = trust_guard.ASK_ROOT if answer.kind == trust_guard.ASK else trust_guard.DENY_ROOT
                expected = "%s: %s. What to do instead: %s" % (root, reason, reason.remedy)
                hook = trust_guard.hook_response(answer)["hookSpecificOutput"]
                self.assertEqual(hook["permissionDecision"], answer.kind)
                self.assertEqual(hook["permissionDecisionReason"], expected)
                gate = trust_guard.gate_response(answer)
                self.assertEqual(gate["reason"], expected)
                self.assertIs(gate["deny"], True)
                self.assertEqual(gate.get("ask"), True if answer.kind == trust_guard.ASK else None)
        # A reason without a remedy (one a later change forgot) still gets a next step.
        bare = trust_guard.message(trust_guard.Answer(trust_guard.DENY, "something"))
        self.assertTrue(bare.endswith("What to do instead: " + trust_guard._FALLBACK_REMEDY))

    def test_the_cli_prints_the_remedy_of_the_reason_given(self):
        for name in ("TOO_DEEP", "BRACE_REASON", "BRACE_NEAR", "NAMES", "ANALYSIS_TIMED", "WALK_REASON"):
            call = REASON_CALLS[name]
            if call.get("slow") or call.get("patch"):
                continue
            event = {"hook_event_name": "PreToolUse", "tool_name": call["tool"],
                     "tool_input": self.deep(call["params"]), "cwd": self.proj}
            out = io.StringIO()
            env = {"TP_TRUST_GUARD_LOG_DIR": os.path.join(self.tmp, "log")}
            with mock.patch.dict(os.environ, env), mock.patch.object(sys, "argv", [CLI, "--claude-hook", "--home", self.home]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(event))), mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(trust_guard, "is_worker", lambda *_a, **_k: True):
                runpy.run_path(CLI, run_name="__main__")
            text = json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecisionReason"]
            with self.subTest(reason=name):
                self.assertTrue(text.endswith(getattr(trust_guard, name).remedy), text)


class LargeRepository(Fixture):
    """The false-denial table, over the budget: ordinary work in a large repository is asked about, never denied."""

    def test_the_rows_ask_when_the_repository_is_too_large_to_look_through(self):
        repo = self.make_repo()
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 2):
            for command in OVER_BUDGET_ASKS:
                for worker in (True, False):
                    with self.subTest(command=command, worker=worker):
                        self.assertAsk(command, cwd=repo, worker=worker)
            # Walks that follow nothing never ask, however large the repository is.
            for command in ("grep -r foo .", "find . -name x", "tar -czf {SCRATCH}/a.tgz .", "rg foo", "du -sh .",
                            "grep -e -R foo .", "rg -e --follow foo .", "find . -name -follow", "du -B -L ."):
                with self.subTest(command=command):
                    self.assertAllowed(command, cwd=repo)
        for command in OVER_BUDGET_ASKS:
            with self.subTest(command=command, small=True):
                self.assertAllowed(command, cwd=repo)

    def test_a_brace_list_over_the_budget_asks_in_a_large_repository_and_never_denies(self):
        repo = self.make_repo()
        for command in BRACE_ASKS:
            for worker in (True, False):
                with self.subTest(command=command, worker=worker):
                    self.assertAsk(command, reason=trust_guard.BRACE_REASON, cwd=repo, worker=worker)
        for command in ("echo {1..5}", "touch {SCRATCH}/f{1..20}.txt", "echo {a,b,c}{1..30}"):
            with self.subTest(command=command):
                self.assertAllowed(command, cwd=repo)

    def test_a_working_directory_that_holds_the_store_denies_the_same_walks(self):
        with mock.patch.object(trust_guard, "_WALK_ENTRIES", 2):
            for command in ("grep -R foo .", "find -L . -name x", "tar -czhf {SCRATCH}/a.tgz ."):
                with self.subTest(command=command):
                    self.assertHardDeny(command, cwd=self.home)
                    self.assertHardDeny(command, cwd=self.config)


class LargeTree(Fixture):
    """A real tree over the real budget (about a hundred thousand entries), through the hook script and the CLI."""

    @classmethod
    def setUpClass(cls):
        cls.big = os.path.realpath(tempfile.mkdtemp(prefix="trustguard-big-"))
        for i in range(101):
            os.mkdir(os.path.join(cls.big, "d%d" % i))
            for j in range(1000):
                open(os.path.join(cls.big, "d%d" % i, "f%d" % j), "w").close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.big, True)

    def run_script(self, script, command, *args):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("TP_")}, "TP_REDACTION": "on",
               "TP_TRUST_GUARD_LOG_DIR": os.path.join(self.tmp, "log")}
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": self.sub(command)},
                 "cwd": self.big}
        return subprocess.run([script, *args], input=json.dumps(event), text=True, capture_output=True, env=env, timeout=30)

    def test_the_hook_script_asks_for_the_three_commands(self):
        for command in ("grep -R foo .", "find -L . -name x", "tar -czhf {SCRATCH}/a.tgz ."):
            with self.subTest(command=command):
                ran = self.run_script(HOOK, command)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                out = json.loads(ran.stdout)["hookSpecificOutput"]
                self.assertEqual(out["permissionDecision"], "ask")
                self.assertIn("too large to check for links into the trust store", out["permissionDecisionReason"])

    def test_the_pi_gate_asks_for_them_too(self):
        gate = json.loads(self.run_script(CLI, "grep -R foo .").stdout)
        self.assertEqual((gate["deny"], gate.get("ask")), (True, True))

    def test_naming_the_store_in_the_same_call_is_still_a_deny(self):
        ran = self.run_script(HOOK, "grep -R foo . ; cat ~/.config/tmux-pi/trust/rec.json")
        self.assertEqual(json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class WorkingDirectory(Fixture):
    """Tools act on the working directory when they are given nothing else; it must not be the store."""

    def at_store(self, tool, params, cwd=None):
        return trust_guard.check(tool, params, cwd or self.trust, home=self.home, worker=True)

    def test_every_tool_call_is_denied_while_the_working_directory_is_inside_the_store(self):
        calls = [("Bash", {"command": "ls"}), ("Bash", {"command": "ls -la"}), ("Bash", {"command": "pwd"}),
                 ("Bash", {"command": "cat rec.json"}), ("Bash", {"command": "echo x > new.json"}),
                 ("Bash", {"command": "find"}), ("Bash", {"command": "grep -r rec"}), ("Bash", {"command": "git diff"}),
                 ("Bash", {"command": "cd /tmp && ls"}), ("Bash", {"command": "cd .. && ls"}),
                 ("Grep", {"pattern": "rec"}), ("Glob", {"pattern": "*.json"}), ("Glob", {"pattern": "**/*"}),
                 ("Read", {"file_path": "rec.json"}), ("Read", {"file_path": "/etc/hosts"}), ("Write", {"file_path": "a.json"}),
                 ("Edit", {"file_path": "rec.json"}), ("MultiEdit", {"file_path": "rec.json"}),
                 ("ls", {}), ("ls", {"path": "."}), ("find", {"pattern": "*.json"}), ("grep", {"pattern": "rec"}),
                 ("read", {"path": "rec.json"}), ("write", {"path": "a.json"}), ("edit", {"path": "rec.json"}),
                 ("mcp__other__anything", {})]
        for cwd in (self.trust, self.state, os.path.join(self.trust, "x"), self.link, self.link + "/../trust",
                    self.trust.upper()):
            for tool, params in calls:
                with self.subTest(cwd=cwd, tool=tool, params=params):
                    reason = self.at_store(tool, params, cwd)
                    if os.path.exists(cwd) or cwd == os.path.join(self.trust, "x"):
                        self.assertEqual(reason, "the working directory is inside the trust store")

    def test_the_same_calls_at_an_ordinary_working_directory_are_allowed(self):
        for tool, params in (("Bash", {"command": "ls"}), ("Bash", {"command": "cat README.md"}), ("Grep", {"pattern": "rec"}),
                             ("Glob", {"pattern": "*.json"}), ("Read", {"file_path": "README.md"}), ("ls", {}),
                             ("find", {"pattern": "*.json"}), ("Write", {"file_path": "a.json"})):
            with self.subTest(tool=tool):
                self.assertIsNone(self.at_store(tool, params, self.proj))
        # Above the store is not inside it: the directory itself may be listed and used.
        for cwd in (self.config, self.home):
            with self.subTest(cwd=cwd):
                self.assertIsNone(self.at_store("Bash", {"command": "ls"}, cwd))
                self.assertIsNone(self.at_store("Read", {"file_path": "x.txt"}, cwd))

    def test_the_working_directory_through_a_link_or_a_case_variant_is_inside_the_store(self):
        self.assertTrue(self.at_store("Bash", {"command": "ls"}, self.link))
        if os.path.exists(self.trust.upper()):
            self.assertTrue(self.at_store("Bash", {"command": "ls"}, self.trust.upper()))

    def test_a_cd_that_lands_in_the_store_without_naming_it_makes_the_next_command_denied(self):
        # `lnk2` is a link to a scratch directory, so `lnk2/..` is the store's neighbour to the kernel but `trust` to the shell's `cd`.
        self.assertIsNone(self.bash("cat {CONFIG}/lnk2/../trust/other.json"))   # the kernel reads scratch/../trust: not the store
        self.assertEqual(self.bash("cd {CONFIG}/lnk2/../trust && ls"), "runs in the trust store")
        self.assertEqual(self.bash("cd {CONFIG}/lnk2/../trust; echo x > new.json"), "runs in the trust store")
        self.assertEqual(self.bash("(cd {CONFIG}/lnk2/../trust; ls)"), "runs in the trust store")

    def test_cdpath_moves_where_a_relative_cd_goes(self):
        self.assertDenied("CDPATH={CONFIG}/tmux-pi cd trust && ls")
        self.assertDenied("export CDPATH={CONFIG}; cd tmux-pi/trust; ls")
        self.assertDenied("CDPATH=. cd lib")
        self.assertAllowed("cd lib && ls")


# Ordinary work in a repository that is about trust: the words `trust` and `tmux-pi` appear, the store is never touched.
# Each row is its own test (see the loop below the class), run in the repository's directory, for a worker and for main.
FALSE_DENIALS = [
    "python3.12 -m unittest tests.test_trust_record",
    "python3.12 -m unittest tests.test_trust_record tests.test_trust_guard -v",
    "python3.12 tests/test_trust_guard.py",
    "python3.12 -m unittest discover -s tests -p 'test_trust*.py' 2>&1 | tail -20",
    "python3.12 bin/tp-trust list",
    "git diff -- lib/trust.py",
    "git diff --stat -- lib/trust.py lib/trust_guard.py tests/test_trust_guard.py",
    "git log --oneline -5 -- lib/trust.py",
    "git show HEAD:lib/trust.py | head -40",
    "git add lib/trust.py tests/test_trust_record.py",
    "git commit -m 'Read the trust record and validate it'",
    "git grep -n trust",
    "git grep -n 'tmux-pi' -- README.md",
    "git status --short",
    "grep -rn trust lib",
    "grep -rn trust .",
    "grep -rln trust lib tests | sort",
    "grep -rn 'tmux-pi' README.md",
    "grep -rn trust $(git ls-files)",
    "grep -n trust $(git rev-parse --show-toplevel)/lib/trust.py",
    "grep -rn --include='*.py' trust lib",
    "rg -n trust lib",
    "rg -n tmux-pi",
    "rg -n trust",
    "rg --files",
    "cat lib/trust.py",
    "cat lib/trust.py tests/test_trust_record.py",
    "cat $(git rev-parse --show-toplevel)/lib/trust.py",
    "sed -n 1,40p lib/trust.py",
    "wc -l lib/trust.py tests/test_trust_*.py",
    "head -20 bin/tp-trust",
    "tail -n 5 tests/test_trust_record.py",
    "ls",
    "ls -la",
    "ls lib",
    "ls -R lib",
    "ls lib/*trust*",
    "ls tests | grep trust",
    "find . -name '*trust*'",
    "find lib -name '*.py' -exec grep -l trust {} +",
    "find . -name '*.py' | xargs grep -n trust",
    "find . -type f | xargs wc -l",
    "ls lib | xargs -I{} wc -l lib/{}",
    "for f in lib/*trust*; do wc -l $f; done",
    "for f in $(ls lib); do grep -c trust lib/$f; done",
    "cd lib && grep -n trust *.py",
    "cd $(git rev-parse --show-toplevel) && ls",
    "echo trust | wc -c",
    "echo \"the trust store is read only\" | wc -w",
    "python3.12 -c \"import sys; sys.path.insert(0, 'lib'); import trust; print(trust.__file__)\"",
    "python3.12 -c \"from trust import STATEMENT; print('{} {}'.format(STATEMENT, 1))\"",
    "node tests/sensitive_runtime.mjs",
    "claude plugin test claude/plugins/tmux-pi",
    "tar czf {SCRATCH}/out.tgz lib tests",
    "cp -r lib {SCRATCH}/lib-copy",
    "diff -r lib {PLAIN}",
    "du -sh lib",
    "mkdir -p tests/trust_fixtures && touch tests/trust_fixtures/a.json",
    "mv tests/trust_fixtures {SCRATCH}/trust_fixtures",
    "rm -rf {SCRATCH}/trust_fixtures",
    "chmod +x bin/tp-trust",
    "tp-trust list",
    "tp-trust show abc123",
    "export TRUST_NOTES=1; ls",
    "TRUST_LEVEL=high python3.12 -m unittest tests.test_trust_record",
    "curl -s https://example.com/trust > {SCRATCH}/page.html",
    # Walks that follow links, in a repository small enough to look through. Over the budget they ask (LargeRepository).
    "grep -R foo .",
    "find -L . -name x",
    "tar -czhf {SCRATCH}/a.tgz .",
    # A word that looks like a flag but is the value of an option before it follows nothing.
    "grep -e -R foo .",
    "rg -e --follow foo .",
    "find . -name -follow",
]
# The same walks in a repository too large to look through: the guard cannot say, so it asks and does not deny.
OVER_BUDGET_ASKS = ["grep -R foo .", "find -L . -name x", "tar -czhf {SCRATCH}/a.tgz .", "grep -R foo", "ls -LR",
                    "rg --follow foo", "du -L -a ."]
FALSE_DENIAL_TOOLS = [
    ("Edit", {"file_path": "lib/trust.py", "old_string": "a", "new_string": "b"}),
    ("MultiEdit", {"file_path": "lib/trust.py", "edits": []}),
    ("Write", {"file_path": "tests/test_trust_record.py", "content": "~/.config/tmux-pi/trust/x.json"}),
    ("Write", {"file_path": "{REPO}/lib/trust_guard.py", "content": "x"}),
    ("Read", {"file_path": "lib/trust.py"}),
    ("Read", {"file_path": "{REPO}/lib/trust.py"}),
    ("Grep", {"pattern": "trust", "path": "lib"}),
    ("Grep", {"pattern": "tmux-pi/trust"}),
    ("Grep", {"pattern": "trust", "path": "{REPO}"}),
    ("Glob", {"pattern": "lib/*trust*"}),
    ("Glob", {"pattern": "**/*trust*"}),
    ("Glob", {"pattern": "**/*.py", "path": "{REPO}"}),
    ("edit", {"path": "lib/trust.py", "oldText": "a", "newText": "b"}),
    ("write", {"path": "lib/trust_guard.py", "content": "x"}),
    ("read", {"path": "lib/trust.py"}),
    ("grep", {"pattern": "trust", "path": "lib"}),
    ("find", {"pattern": "*trust*", "path": "{REPO}"}),
    ("ls", {"path": "lib"}),
    ("ls", {"path": "{REPO}"}),
]


class FalseDenials(Fixture):
    """The guard must not get in the way of work on the trust code itself."""

    def setUp(self):
        super().setUp()
        self.repo = self.make_repo()

    def tool_in_repo(self, name, params, worker):
        params = {k: v.replace("{REPO}", self.repo) if isinstance(v, str) else v for k, v in params.items()}
        return trust_guard.check(name, {k: self.sub(v) if isinstance(v, str) else v for k, v in params.items()},
                                 self.repo, home=self.home, worker=worker)

    def test_nothing_in_the_table_is_denied_for_the_wrong_reason(self):
        # A row that the guard denies because it really reaches the store would belong in REDTEAM, not here.
        for command in FALSE_DENIALS:
            self.assertNotIn(self.trust, self.sub(command))


def _row(command, worker):
    def test(self):
        self.assertIsNone(self.bash(command, cwd=self.repo, worker=worker), command)
    return test


def _tool_row(name, params, worker):
    def test(self):
        self.assertIsNone(self.tool_in_repo(name, params, worker), name)
    return test


for _i, _command in enumerate(FALSE_DENIALS):
    for _worker in (True, False):
        setattr(FalseDenials, "test_row_%02d_%s" % (_i, "worker" if _worker else "main"), _row(_command, _worker))
for _i, (_name, _params) in enumerate(FALSE_DENIAL_TOOLS):
    for _worker in (True, False):
        setattr(FalseDenials, "test_tool_row_%02d_%s_%s" % (_i, _name, "worker" if _worker else "main"),
                _tool_row(_name, _params, _worker))


# Harmless-looking commands the guard still denies, because the design requires it or because it cannot tell them from
# a way in. Each is listed in the report; none can be allowed without opening a route the red-team table closes.
DENIED_BY_DESIGN = [
    "echo 'records live in ~/.config/tmux-pi/trust' | wc -w",        # the store's path as a word, even in prose
    "cat $DIR/trust/rec.json",                                         # a run-time path next to the store as a path
    "grep -r pattern ~/.config",                                       # a walk of the directory that holds the store
    "find ~ -name '*.json'",
    "find / -name notes.txt",
    "du -sh ~",
    "ls -R ~",
    "CDPATH=. cd lib",                                                 # where a relative cd goes is no longer known
    "tp-trust grant .",
    "tp-trust",
    "tmux send-keys -t 0 ls Enter",                                    # worker only
    "tmux capture-pane -p",
    "python3 -c \"import os; print(os.path.expanduser('~') + '/.config/tmux-pi/')\"",
]


class DeniedByDesign(Fixture):
    def test_each_row_is_denied_for_a_worker(self):
        for command in DENIED_BY_DESIGN:
            with self.subTest(command=command):
                self.assertDenied(command, worker=True)

    def test_the_rows_that_are_not_about_a_worker_are_denied_for_main_too(self):
        for command in DENIED_BY_DESIGN:
            if "tmux " in command:
                continue
            with self.subTest(command=command):
                self.assertDenied(command, worker=False)


class CliTests(Fixture):
    def run_cli(self, event, *args, env=None, script=CLI, raw=None):
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("TP_")},
               "TP_TRUST_GUARD_LOG_DIR": os.path.join(self.tmp, "log"), **(env or {})}
        return subprocess.run([script, *args], input=raw if raw is not None else json.dumps(event), text=True,
                              capture_output=True, env=env, timeout=30)

    def event(self, command, name="PreToolUse", tool="Bash"):
        return {"hook_event_name": name, "tool_name": tool, "tool_input": {"command": self.sub(command)}, "cwd": self.proj}

    def log_lines(self):
        path = os.path.join(self.tmp, "log", "guard.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as handle:
            return [json.loads(line) for line in handle]

    def test_claude_hook_denies_and_never_asks(self):
        for command in ("echo x > ~/.config/tmux-pi/trust/a.json", "tp-trust grant .", "echo x > {LINK}/a.json"):
            with self.subTest(command=command):
                ran = self.run_cli(self.event(command), "--claude-hook", "--home", self.home)
                self.assertEqual(ran.returncode, 0, ran.stderr)
                out = json.loads(ran.stdout)["hookSpecificOutput"]
                self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "deny"))
                self.assertIn("trust", out["permissionDecisionReason"])

    def test_claude_hook_is_silent_for_an_ordinary_call_and_for_other_events(self):
        self.assertEqual(self.run_cli(self.event("git diff"), "--claude-hook", "--home", self.home).stdout, "")
        for name in ("PostToolUse", "PermissionDenied", "UserPromptSubmit"):
            ran = self.run_cli(self.event("tp-trust grant .", name=name), "--claude-hook", "--home", self.home)
            self.assertEqual((ran.returncode, ran.stdout), (0, ""), name)

    def test_unreadable_input_exits_non_zero(self):
        for raw in ("", "not json", "[1]", "3"):
            with self.subTest(raw=raw):
                self.assertNotEqual(self.run_cli(None, "--claude-hook", "--home", self.home, raw=raw).returncode, 0)

    def test_a_tool_call_with_no_input_object_is_denied(self):
        ran = self.run_cli({"hook_event_name": "PreToolUse", "tool_name": "Bash"}, "--claude-hook", "--home", self.home)
        self.assertEqual(json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_pi_gate_answer(self):
        denied = json.loads(self.run_cli({"tool_name": "write", "tool_input": {"path": "{TRUST}/a.json".replace("{TRUST}", self.trust)},
                                          "cwd": self.proj}, "--home", self.home).stdout)
        self.assertTrue(denied["deny"])
        self.assertIn("trust", denied["reason"])
        allowed = json.loads(self.run_cli({"tool_name": "bash", "tool_input": {"command": "git diff"}, "cwd": self.proj},
                                          "--home", self.home).stdout)
        self.assertEqual(allowed, {"deny": False, "reason": None})

    def test_a_worker_is_known_from_the_launch_environment(self):
        event = self.event("tmux send-keys -t 0 x")
        for env, denied in (({"TP_TASK_DIR": self.tmp}, True), ({"TP_TASK_ID": "t1"}, True), ({}, False)):
            with self.subTest(env=env):
                out = self.run_cli(event, "--claude-hook", "--home", self.home, env=env).stdout
                self.assertEqual(bool(out), denied)

    def test_every_denial_is_logged_without_the_command(self):
        self.run_cli(self.event("echo SECRETWORD > {TRUST}/a.json"), "--claude-hook", "--home", self.home,
                     env={"TP_TASK_ID": "t-77"})
        self.run_cli({"tool_name": "write", "tool_input": {"path": self.trust + "/b.json"}, "cwd": self.proj}, "--home", self.home)
        self.run_cli(self.event("git diff"), "--claude-hook", "--home", self.home)
        lines = self.log_lines()
        self.assertEqual([(r["event"], r["harness"], r["tool"]) for r in lines],
                         [("trust_guard_deny", "claude", "Bash"), ("trust_guard_deny", "pi", "write")])
        self.assertEqual(lines[0]["task"], "t-77")
        with open(os.path.join(self.tmp, "log", "guard.jsonl")) as handle:
            self.assertNotIn("SECRETWORD", handle.read())
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "log", "guard.jsonl")).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "log")).st_mode), 0o700)

    def test_the_log_is_written_under_the_state_directory_by_default(self):
        trust_guard.log_denial("why", "Bash", "claude", home=self.home, env={})
        with open(os.path.join(self.state, "guard.jsonl")) as f:
            self.assertEqual(json.loads(f.read())["reason"], "why")

    def test_a_log_that_cannot_be_written_does_not_change_the_answer(self):
        blocked = os.path.join(self.tmp, "blocked")
        open(blocked, "w").close()
        ran = self.run_cli(self.event("echo x > {TRUST}/a.json"), "--claude-hook", "--home", self.home,
                           env={"TP_TRUST_GUARD_LOG_DIR": blocked + "/dir"})
        self.assertEqual(json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


class HookScriptTests(Fixture):
    """The shipped hook script, with redaction on, as Claude Code runs it."""

    def run_hook(self, event, script=HOOK, env=None):
        base = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
        return subprocess.run([script], input=json.dumps(event), text=True, capture_output=True, timeout=30,
                              env={**base, "TP_REDACTION": "on", "TP_TRUST_GUARD_LOG_DIR": os.path.join(self.tmp, "log"), **(env or {})})

    def test_it_denies_in_every_redaction_mode(self):
        # The home is the real one from the password database; nothing is written there, only the log directory in the temp tree.
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": self.proj,
                 "tool_input": {"command": "echo x > ~/.config/tmux-pi/trust/a.json"}}
        for mode in ("on", "shadow", "off"):
            with self.subTest(mode=mode):
                ran = self.run_hook(event, env={"TP_REDACTION": mode})
                self.assertEqual(ran.returncode, 0, ran.stderr)
                self.assertEqual(json.loads(ran.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_it_exits_two_when_the_guard_cannot_run(self):
        root = Path(self.tmp) / "fake"
        hooks = root / "claude/plugins/tmux-pi/hooks"
        hooks.mkdir(parents=True)
        (root / "bin").mkdir()
        shutil.copy(HOOK, hooks / "trust-guard.sh")
        broken = root / "bin/tp-trust-guard"
        broken.write_text("#!/bin/sh\nexit 1\n")
        broken.chmod(0o755)
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "git diff"}}
        ran = self.run_hook(event, script=str(hooks / "trust-guard.sh"))
        self.assertEqual(ran.returncode, 2)
        self.assertIn("cannot be verified", ran.stderr)
        broken.unlink()
        self.assertEqual(self.run_hook(event, script=str(hooks / "trust-guard.sh")).returncode, 2)


if __name__ == "__main__":
    unittest.main()
