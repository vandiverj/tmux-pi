"""FG-N1: the command wrappers both detectors must see through, from one table (lib/command_wrappers.py).

`redact_hook.bash_searches` decides whether a command's output is read as grep lines; `sensitive_read.classify` decides whether a
command reads credentials. A wrapper one of them did not know (`setsid rg KEY .`, `flock f -c 'rg --hidden x .'`) hid the command
from it. Every wrapper form is written once below and checked against both detectors, in-process.

The read side is probed with `rg --hidden TOKEN .` (judged by the program, so a wrapper that hides it is seen) and with `cat` of a
credential file name assembled from pieces (a name in the text is a hit whatever runs it, so that probe alone cannot tell). Nothing
here opens a real credential file: the names are created empty in a temp tree.
"""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
from collections import Counter
import re
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import redact_hook  # noqa: E402
import sensitive_read as sr  # noqa: E402

ENV_NAME = "." + "env"  # a credential file name, assembled so this source is not itself a read
# `{C}` is the command the wrapper runs. Real forms of each tool; a string option is quoted as a shell would.
FORMS = [
    "setsid {C}", "setsid -w {C}", "setsid --fork {C}",
    "unbuffer {C}", "unbuffer -p {C}",
    "chrt -f 10 {C}", "chrt --rr 5 {C}",
    "taskset 0x1 {C}", "taskset -c 0,1 {C}",
    "sg staff -c '{C}'", "sg staff '{C}'",
    "runuser -u nobody -- {C}", "runuser -u nobody {C}", "runuser -l nobody -c '{C}'", "runuser nobody -c '{C}'",
    "chroot /srv/jail {C}", "chroot --userspec=u:g /srv/jail {C}",
    "nsenter -t 1 -m {C}", "nsenter --target 1 --mount {C}",
    "unshare -m {C}", "unshare --fork --pid {C}", "unshare --propagation private {C}",
    "firejail {C}", "firejail --noprofile {C}",
    "sandbox-exec -p '(version 1)(allow default)' {C}", "sandbox-exec -f prof.sb {C}",
    "arch -arm64 {C}", "arch -x86_64 {C}", "arch -arch arm64 {C}",
    "gtimeout 5 {C}", "gtimeout -s KILL 5 {C}",
    "time {C}", "time -p {C}", "/usr/bin/time -f %e {C}",
    "xargs {C}", "xargs -n1 {C}", "xargs -I{} {C}",
    "busybox {C}", "busybox --list; busybox {C}",
    "flock /tmp/l {C}", "flock -n /tmp/l {C}", "flock -w 5 /tmp/l {C}", "flock -x /tmp/l -c '{C}'",
    "flock /tmp/l --command '{C}'",
    "script -q -c '{C}' /dev/null", "script -qc '{C}' /dev/null", "script --command '{C}' /dev/null",
    "script --command='{C}' /dev/null", "script -q /dev/null {C}", "script -q out.txt {C}", "script -q -t 0 out.txt {C}",
    "su -c '{C}' nobody", "su - nobody -c '{C}'", "su nobody -c '{C}'", "su -l nobody -c '{C}'",
    "su -s /bin/sh nobody -c '{C}'", "su --command='{C}' nobody",
    "ssh host {C}", "ssh host '{C}'", "ssh -p 22 -i k u@host {C}", "ssh -o StrictHostKeyChecking=no u@host '{C}'",
    "ssh -l u host {C}",
    # the plain wrappers, each with the options it really has (which take a value and which do not)
    "env {C}", "env -i {C}", "env -u NAME {C}", "env -uNAME {C}", "env -C /tmp {C}", "env A=1 B=2 {C}", "env -S '{C}'", "env - {C}",
    "command {C}", "command -p {C}", "builtin {C}", "exec {C}", "exec -a name {C}", "exec -cl {C}",
    "nice {C}", "nice -n 5 {C}", "nice -n5 {C}", "nice -5 {C}", "nice --adjustment=5 {C}",
    "ionice -c 3 {C}", "ionice -c2 -n 7 {C}", "ionice -t {C}",
    "timeout 5 {C}", "timeout -s KILL 5 {C}", "timeout -k 5 10 {C}", "timeout --signal=KILL 5s {C}", "timeout --foreground 5 {C}",
    "timeout -p -v 5 {C}",
    "sudo {C}", "sudo -n {C}", "sudo -u root {C}", "sudo -uroot {C}", "sudo -E -H {C}", "sudo -n -u root -- {C}", "sudo -g wheel -n {C}",
    "sudo -k {C}", "sudo -S -p prompt {C}", "sudo --user=root --preserve-env {C}", "sudo -i {C}",
    "doas {C}", "doas -n {C}", "doas -u root {C}", "doas -C /etc/doas.conf -u root -- {C}",
    "nohup {C}", "stdbuf -oL {C}", "stdbuf -o L -e0 {C}", "stdbuf --output=L {C}",
    "caffeinate {C}", "caffeinate -t 5 {C}", "caffeinate -dimsu {C}", "caffeinate -w 1 {C}", "noglob {C}", "nocorrect {C}",
    "watch {C}", "watch -n 5 '{C}'", "watch -n5 -d {C}", "watch --interval 2 -x {C}",
    "parallel {C}", "parallel -j4 '{C}' ::: a b", "parallel --jobs 4 {C} ::: a b", "parallel -k --tag {C}",
    "setsid nice flock /tmp/l {C}",  # wrappers stacked
    # `--` ends the options of the wrapper; its operands (lock file, root, host, mask, user) still come before the command
    "setsid -- {C}", "unbuffer -- {C}", "chrt -f -- 10 {C}", "taskset -- 0x1 {C}", "taskset -c 0 -- {C}",
    "flock -- /tmp/l {C}", "flock -n -- /tmp/l {C}", "chroot -- /srv/jail {C}", "chroot --userspec=u:g -- / {C}",
    "runuser -- nobody {C}", "runuser -u nobody -- {C}", "nsenter -t 1 -- {C}", "unshare -m -- {C}", "firejail -- {C}",
    "arch -arm64 -- {C}", "time -p -- {C}", "script -q -- out.txt {C}", "ssh -- host {C}", "ssh -- host '{C}'",
    "ssh -p 22 -- u@host {C}", "timeout 5 ssh -- host {C}",
]
# Forms that give the command as one string (`-c STR`, `--command=STR`, `-cSTR`, a remote line). A string is split like
# `sh -c` does, so tabs separate its words as spaces do.
STRING_FORMS = [form for form in FORMS if "'{C}'" in form]
STRING_FORMS += ["su -c'{C}' nobody", "su -l nobody --command '{C}'", "flock /tmp/l -c'{C}'", "script -qc'{C}' /dev/null"]
# Round 7: the floor reads the leading option words of `sudo` without asking its table where they end (a wrong row must not hide
# `sudo -u root -s "$X"`), and the word after an option may be its value. So after a flag of the stack, the `-s` of a program is taken
# for sudo's shell option and what follows it is looked at as a command line: `sudo -E su -s /bin/sh nobody -c ls` reads `/bin/sh` as
# a shell with no command string. Reported to Main (an extra look, never a hidden command); these stacked forms are pinned as such.
OVER_READ = {("sudo -E {C}", "su -s /bin/sh nobody -c '{C}'")}
# A wrapper inside another, so the inner one's string is not the outer's own argument.
STACKS = ["setsid {C}", "env {C}", "setsid env nice {C}", "timeout 5 {C}", "sudo -E {C}",
          "setsid flock -- /tmp/l {C}", "nice setsid unbuffer {C}"]
# Command lines no static reading can resolve: the shell that runs them picks the command.
UNKNOWN = ["$REMOTE_COMMAND", "$REMOTE_COMMAND --hidden TOKEN .", "$(echo rg) --hidden TOKEN .", "`echo rg` --hidden TOKEN .",
           "eval $REMOTE_COMMAND", "FOO=1 $REMOTE_COMMAND"]
# A wrapper with nothing after it runs nothing: not a search and not a read.
BARE = ["setsid", "setsid -w", "unbuffer", "chrt -f 10", "taskset 0x1", "sg staff", "runuser -u nobody", "chroot /srv/jail",
        "nsenter -t 1", "unshare -m", "firejail", "sandbox-exec -f prof.sb", "arch -arm64", "gtimeout 5", "time", "time -p",
        "xargs", "busybox", "busybox --list", "flock /tmp/l", "flock -n 9", "script -q /dev/null", "script -q out.txt",
        "su - nobody", "su nobody", "ssh host", "ssh -p 22 u@host",
        "env", "env -i", "env A=1", "command", "exec", "nice", "nice -n 5", "ionice -c 3", "timeout 5", "sudo", "sudo -n", "sudo -l",
        "doas -u root", "nohup", "stdbuf -oL", "caffeinate -t 5", "noglob", "watch", "watch -n 5", "parallel", "parallel -j4"]
SEARCH = "rg TOKEN ."
HIDDEN = "rg --hidden TOKEN ."


def run(form, command):
    return form.replace("{C}", command)


class WrapperTreeCase(unittest.TestCase):
    """A throwaway project tree: one credential-named file (empty) and one ordinary file."""

    @classmethod
    def setUpClass(cls):
        cls.tree = tempfile.mkdtemp(prefix="wrapper-lists-")
        (Path(cls.tree) / ENV_NAME).write_text("")
        (Path(cls.tree) / "notes.txt").write_text("hello\n")
        # The classifier fails closed when its wall-clock limits run out; machine load must not turn into a verdict.
        patch = mock.patch.multiple(sr, _GIT_TIMEOUT=120.0, WALK_SECONDS=120.0, WALK_CALL_SECONDS=120.0)
        patch.start()
        cls.addClassCleanup(patch.stop)
        cls.addClassCleanup(shutil.rmtree, cls.tree, True)

    def reads(self, command):
        return sr.classify("Bash", {"command": command}, cwd=self.tree)["sensitive"]


class WrapperFormTests(WrapperTreeCase):
    def test_search_behind_every_wrapper_form_is_a_search(self):
        for form in FORMS:
            with self.subTest(form=form):
                self.assertTrue(redact_hook.bash_searches(run(form, SEARCH)))

    def test_nothing_run_behind_a_wrapper_is_not_a_search(self):
        for form in FORMS:
            with self.subTest(form=form):
                self.assertFalse(redact_hook.bash_searches(run(form, "ls -la")))

    def test_credential_read_behind_every_wrapper_form_prompts(self):
        for form in FORMS:
            for probe in (HIDDEN, "cat " + ENV_NAME):
                with self.subTest(form=form, probe=probe):
                    self.assertTrue(self.reads(run(form, probe)))

    def test_ordinary_read_behind_every_wrapper_form_passes(self):
        for form in FORMS:
            for probe in ("cat notes.txt", "ls -la"):
                if form.startswith("xargs") and probe != "ls -la":
                    continue  # `xargs cat` with no producer reads files nobody named: a bulk read, by design
                with self.subTest(form=form, probe=probe):
                    self.assertFalse(self.reads(run(form, probe)))

    def test_a_wrapper_with_no_command_is_neither_a_search_nor_a_read(self):
        for bare in BARE:
            with self.subTest(bare=bare):
                self.assertFalse(redact_hook.bash_searches(bare))
                self.assertFalse(self.reads(bare))

    def test_a_command_string_is_split_like_sh_c_so_tabs_separate_its_words(self):
        tabbed_search, tabbed_read = "rg\tTOKEN\t.", "rg\t--hidden\tTOKEN\t."
        for form in STRING_FORMS:
            with self.subTest(form=form):
                self.assertTrue(redact_hook.bash_searches(run(form, tabbed_search)))
                self.assertTrue(self.reads(run(form, tabbed_read)))
                self.assertFalse(redact_hook.bash_searches(run(form, "ls\t-la")))
                self.assertFalse(self.reads(run(form, "ls\t-la")))
        # a newline starts the next command: the search on the second line is still found
        for form in STRING_FORMS:
            with self.subTest(form=form, lines=True):
                self.assertTrue(redact_hook.bash_searches(run(form, "ls\nrg TOKEN .")))
                self.assertTrue(self.reads(run(form, "ls\nrg --hidden TOKEN .")))

    def test_a_wrapper_inside_another_is_read_through_whatever_runs_it(self):
        # `setsid su --command='grep<TAB>-r<TAB>TOKEN<TAB>.' nobody`: the string belongs to the inner wrapper
        self.assertTrue(redact_hook.bash_searches("setsid su --command='grep\t-r\tTOKEN\t.' nobody"))
        self.assertTrue(self.reads("setsid su --command='rg\t--hidden\tTOKEN\t.' nobody"))
        for stack in STACKS:
            for form in STRING_FORMS:
                with self.subTest(stack=stack, form=form):
                    self.assertTrue(redact_hook.bash_searches(run(stack, run(form, SEARCH))))
                    self.assertTrue(self.reads(run(stack, run(form, HIDDEN))))
                    over = (stack, form) in OVER_READ
                    self.assertEqual(redact_hook.bash_searches(run(stack, run(form, "ls -la"))), over)
                    self.assertEqual(self.reads(run(stack, run(form, "ls -la"))), over)

    def test_a_command_the_remote_or_inner_shell_chooses_fails_closed(self):
        for form in (f for f in STRING_FORMS if f.startswith(("ssh", "su ", "sg ", "flock", "script", "runuser", "env", "watch", "parallel"))):
            for unknown in UNKNOWN:
                with self.subTest(form=form, unknown=unknown):
                    self.assertTrue(redact_hook.bash_searches(run(form, unknown)))
                    self.assertTrue(self.reads(run(form, unknown)))
        for unknown in UNKNOWN:  # also behind another wrapper, and as an unquoted remote command
            with self.subTest(stacked=unknown):
                self.assertTrue(self.reads(run("setsid su -c '{C}' nobody", unknown)))
                self.assertTrue(self.reads("timeout 5 ssh host '" + unknown + "'"))
        # (round 4 removed the exception that a variable which is only an argument leaves the command known: an unresolved expansion
        # anywhere in text a wrapper hands on is uncertain, UncertainTextTests)

    def test_the_command_is_the_one_the_wrapper_runs_not_the_wrapper_operand(self):
        # `flock rg -c ls`: rg is the lock file, ls is run. An operand named like a search program is no search to the reader.
        self.assertFalse(self.reads("flock rg -c 'ls'"))
        self.assertFalse(self.reads("chroot rg ls"))
        self.assertFalse(self.reads("su rg -c 'ls'"))


class LimitTests(WrapperTreeCase):
    def test_the_operand_scan_limit_fails_closed_for_every_wrapper(self):
        import command_wrappers
        for name in sorted(command_wrappers.NAMES):
            with self.subTest(wrapper=name):
                # 64 operands are read to the end (62 and `echo ok`); the 65th may hide the command: a search.
                self.assertFalse(redact_hook.bash_searches(name + " a" * 62 + " echo ok"))
                self.assertTrue(redact_hook.bash_searches(name + " a" * 63 + " echo ok"))
                self.assertTrue(redact_hook.bash_searches(name + " a" * 10_000 + " echo ok"))

    def test_nesting_deeper_than_the_limit_fails_closed_on_both_sides(self):
        def nested(levels):
            text = "ls"
            for _ in range(levels):
                text = "su -c " + shlex.quote(text) + " nobody"
            return text
        self.assertFalse(redact_hook.bash_searches(nested(2)))
        self.assertTrue(redact_hook.bash_searches(nested(redact_hook.SEARCH_DEPTH + 2)))
        self.assertFalse(self.reads(nested(2)))
        self.assertTrue(self.reads(nested(8)))


class TableTests(unittest.TestCase):
    def test_the_two_detectors_are_built_from_one_table(self):
        import command_wrappers as table
        self.assertEqual(redact_hook.WRAPPERS, set(table.NAMES))
        # sensitive_read replaces each wrapper by its command, except the two it judges in a branch of its own
        self.assertEqual(sr._UNWRAPPED | set(table.OWN), set(table.NAMES))
        self.assertEqual(sr._UNWRAPPED & set(table.OWN), set())
        for name in table.OWN:
            self.assertIn('prog == "%s"' % name, Path(sr.__file__).read_text())
        for name in sorted(table.NAMES):  # no wrapper is a bare guess: every row lists its own options
            self.assertIsInstance(table.SPECS[name], table.Spec)

    def test_every_described_wrapper_has_a_form_in_this_file(self):
        import command_wrappers as table
        words = {Path(word).name for form in FORMS for word in shlex.split(form.replace("{C}", "ls").replace(";", " "))}
        words |= {Path(word).name for stack in STACKS for word in shlex.split(stack.replace("{C}", "ls"))}
        self.assertEqual(set(table.SPECS) - words, set())

    def test_every_option_the_table_lists_is_known(self):
        # a listed option never gives UNKNOWN, alone or before the command; and it matters which take a value
        for name, spec in sorted(table.SPECS.items()):
            for flag in sorted(spec.flags | spec.valued | spec.string | spec.shell):
                args = [flag] + (["v"] if flag in spec.valued | spec.string and "=" not in flag else []) + ["rg", "x"]
                with self.subTest(wrapper=name, option=flag):
                    self.assertIsNot(table.unwrap(name, args), table.UNKNOWN)

    def test_unwrap_finds_where_the_command_starts(self):
        import command_wrappers as table
        for name, args, expected in (
                ("setsid", ["-w", "rg", "x"], ("words", ["rg", "x"])),
                ("setsid", [], None),
                ("flock", ["-n", "/tmp/l", "rg", "x"], ("words", ["rg", "x"])),
                ("flock", ["-x", "/tmp/l", "-c", "rg x"], ("line", "rg x")),
                ("flock", ["/tmp/l"], None),
                ("script", ["-qc", "rg x", "/dev/null"], ("line", "rg x")),
                ("script", ["-qcrg x"], ("line", "rg x")),  # the value attached to the bundle
                ("script", ["--command=rg x", "/dev/null"], ("line", "rg x")),
                ("script", ["-q", "/dev/null", "rg", "x"], ("words", ["rg", "x"])),  # macOS: file, then the command
                ("script", ["-t", "0", "f", "cat"], ("words", ["cat"])),  # macOS -t takes a number
                ("su", ["-", "nobody", "-c", "rg x"], ("line", "rg x")),
                ("su", ["-c", "rg x", "nobody"], ("line", "rg x")),
                ("su", ["-", "nobody"], None),
                ("runuser", ["-u", "nobody", "--", "rg", "x"], ("words", ["rg", "x"])),
                ("runuser", ["-l", "nobody", "-c", "rg x"], ("line", "rg x")),
                ("ssh", ["host", "rg", "--hidden", "x"], ("line", "rg --hidden x")),  # options after the host are the remote's
                ("ssh", ["-p", "22", "-i", "k", "u@h", "rg x"], ("line", "rg x")),
                ("ssh", ["host"], None),
                ("sg", ["staff", "-c", "rg x"], ("line", "rg x")),
                ("chrt", ["-f", "10", "rg"], ("words", ["rg"])),
                ("chrt", ["-p", "10", "1234"], None),  # changes a running process: runs nothing
                ("taskset", ["-cp", "0", "1"], None),
                ("taskset", ["-c", "0,1", "rg"], ("words", ["rg"])),
                ("arch", ["-arch", "arm64", "rg"], ("words", ["rg"])),
                ("time", ["-f", "%e", "rg"], ("words", ["rg"])),
                ("flock", ["--", "/tmp/l", "rg", "x"], ("words", ["rg", "x"])),  # `--` ends options, not operands
                ("flock", ["-n", "--", "/tmp/l", "-c", "x"], ("words", ["-c", "x"])),  # after `--`, -c is no option
                ("chroot", ["--", "/", "rg"], ("words", ["rg"])),
                ("chroot", ["--", "/"], None),
                ("ssh", ["--", "host", "rg", "--hidden", "x"], ("line", "rg --hidden x")),
                ("ssh", ["-p", "22", "--", "host"], None),
                ("runuser", ["--", "nobody", "rg"], ("words", ["rg"])),
                ("su", ["--", "nobody"], None),
                ("su", ["--command=rg x", "nobody"], ("line", "rg x")),
                ("su", ["-crg x", "nobody"], ("line", "rg x")),
                ("env", ["-S", "sudo -n", "rg", "--hidden", "x", "."], ("line", "sudo -n rg --hidden x .")),  # env appends the rest
                ("env", ["-S", "sudo -n"], ("line", "sudo -n")),
                ("env", ["-i", "--split-string=nice -n 5", "rg", "a b"], ("line", "nice -n 5 rg 'a b'")),
                ("script", ["-F", "out.txt", "rg", "x"], ("words", ["rg", "x"])),  # macOS -F is a flag
                ("busybox", ["--list"], None),
                ("busybox", ["grep", "x"], ("words", ["grep", "x"])),
                # round 5 rule 4: `-h` is the host option when a word follows it, help alone
                ("sudo", ["-h"], None),
                ("sudo", ["-h", "localhost", "rg", "x"], ("words", ["rg", "x"])),
                ("sudo", ["-h", "localhost"], None),
                ("sudo", ["--host=localhost", "rg", "x"], ("words", ["rg", "x"])),
                ("sudo", ["--host", "localhost", "rg", "x"], ("words", ["rg", "x"])),
                ("sudo", ["-hlocalhost", "rg", "x"], ("words", ["rg", "x"])),
                ("sudo", ["--help"], None),
                ("sudo", ["-V"], None),
                # round 7 rule 4: sudo's shell mode runs its arguments as one command line; bare, it runs a shell and nothing else
                ("sudo", ["-s"], None), ("sudo", ["-i"], None), ("sudo", ["--shell"], None), ("sudo", ["--login"], None),
                ("sudo", ["-Es"], None), ("sudo", ["-u", "root", "-s"], None),
                ("sudo", ["-s", "rg", "--hidden", "x"], ("line", "rg --hidden x")),
                ("sudo", ["-i", "rg", "a b"], ("line", "rg a b")),  # joined by spaces like ssh: more words, never fewer
                ("sudo", ["--shell", "rg x"], ("line", "rg x")),
                ("sudo", ["--login", "rg", "x"], ("line", "rg x")),
                ("sudo", ["--sh", "rg", "x"], ("line", "rg x")),  # an unambiguous abbreviation
                ("sudo", ["-Es", "rg", "x"], ("line", "rg x")), ("sudo", ["-Ei", "rg", "x"], ("line", "rg x")),
                ("sudo", ["-u", "root", "-s", "rg", "x"], ("line", "rg x")),
                ("sudo", ["-h", "localhost", "-i", "rg", "x"], ("line", "rg x")),
                ("sudo", ["-us", "rg", "x"], ("words", ["rg", "x"])),  # `-us` is -u with the user `s`: not shell mode
                ("sudo", ["--us", "root", "rg", "x"], ("words", ["rg", "x"])),  # --user is the only option that starts so
                ("sudo", ["--pre", "rg", "x"], table.UNKNOWN),  # --preserve-env or --preserve-groups: fail closed
                ):
            with self.subTest(wrapper=name, args=args):
                self.assertEqual(table.unwrap(name, args), expected)


import command_wrappers as table  # noqa: E402

# Ordinary commands that start with a wrapper and hold no search or read program and no dispatcher: judged exactly as on main
# (the first 25 are main's own probe; `git grep foo` is a search without any wrapper). (search, read) of each is (False, False).
ORDINARY = [
    "timeout 5 ls", "timeout --preserve-status 5 make test", "timeout -k 2 10 pytest", "env -i PATH=/usr/bin ls", "env FOO=1 make",
    "nice -n 5 make", "nice -10 make", "sudo -E make install", "sudo -H pip list", "time ls", "/usr/bin/time -l make", "nohup make &",
    "xargs -0 echo < list", "find . -name '*.py' | xargs wc -l", "ssh host uptime", "ssh -o BatchMode=yes host ls /tmp",
    "flock /tmp/l make", "caffeinate -i make", "stdbuf -oL make", "watch -n 2 ls", "env -u PATH ls",
    "timeout 30s python3 -m unittest tests.test_x", "sudo -u nobody ls", "nice --adjustment=5 make", "env --chdir=/tmp ls",
    # round 4: fifteen more
    "sudo -u nobody make", "timeout 30 npm test", "nice -n 5 cargo build", "ssh host systemctl status nginx",
    "env -i PATH=/usr/bin make", "sudo systemctl restart nginx", "time make -j4", "nohup python3 server.py &", "ionice -c 3 make all",
    "flock -n /tmp/l make all", "ssh -p 22 deploy@host df -h", "su -c 'make install' root", "setsid sleep 5",
    "env -u HOME ls -la /tmp", "timeout 5 ping -c 1 localhost",
]


# round 6: the spellings of a handed string, made from the wrapper's own row
PAYLOADS = [("$X", "uncertain"), ("$1", "uncertain"), ("$@", "uncertain"), ("$(cmd)", "uncertain"), ("`cmd`", "uncertain"),
            ("sh", "uncertain"), ("bash -s", "uncertain"), ("rg --hidden TOKEN .", "search"), ("git grep foo", "search")]
CONTEXTS = [[], ["sudo", "-h", "localhost"], ["nice", "-n", "5"]]
# what stands around the string option, per wrapper: words before it, words after it
FRAMES = {"env": [([], []), (["-i"], [])], "su": [([], ["nobody"]), (["-", "nobody"], [])],
          "runuser": [(["-u", "nobody"], []), (["nobody"], [])], "sg": [(["staff"], [])],
          "flock": [(["/tmp/l"], []), (["-n", "/tmp/l"], [])], "script": [([], ["/dev/null"]), (["-q"], ["/dev/null"])]}
# wrappers that run the rest of their words as one line: what stands before the line
REST_FRAMES = {"ssh": [["host"], ["-p", "22", "--", "host"], ["-o", "BatchMode=yes", "u@host"]], "watch": [[], ["-n", "2"]],
               "parallel": [[], ["-j4"]], "sg": [["staff"]]}


def handing_variants():
    """(wrapper, spelling, frame, make) for every text-handing row: make(payload) is the wrapper's tail. Short string options are
    spelled apart (`-S STR`), attached (`-SSTR`), as the last letter of a cluster with each flag letter the row lists (`-iS STR`,
    `-vSSTR`); long ones with `=`, apart, and abbreviated (getopt accepts it)."""
    for name in sorted(table.SPECS):
        spec = table.SPECS[name]
        shorts = sorted(flag for flag in spec.string if not flag.startswith("--"))
        longs = sorted(flag for flag in spec.string if flag.startswith("--"))
        letters = sorted(flag[1] for flag in spec.flags if len(flag) == 2)
        for frame in FRAMES.get(name, []) if spec.string else []:
            before, after = frame
            options = []
            for flag in shorts:
                options += [("separate", lambda t, f=flag: [f, t]), ("attached", lambda t, f=flag: [f + t])]
                for letter in letters:
                    options += [("cluster separate", lambda t, f=flag, l=letter: ["-" + l + f[1], t]),
                                ("cluster attached", lambda t, f=flag, l=letter: ["-" + l + f[1] + t])]
            for flag in longs:
                options += [("long=", lambda t, f=flag: [f + "=" + t]), ("long separate", lambda t, f=flag: [f, t]),
                            ("abbreviated=", lambda t, f=flag: [f[:5] + "=" + t]),
                            ("abbreviated separate", lambda t, f=flag: [f[:5], t])]
            for kind, spell in options:
                yield name, kind, tuple(frame), (lambda t, b=before, a=after, sp=spell: b + sp(t) + a)
        for before in REST_FRAMES.get(name, []) if spec.rest else []:
            yield name, "one word", tuple(before), (lambda t, b=before: b + [t])
            yield name, "words", tuple(before), (lambda t, b=before: b + shlex.split(t))


def has_search(entries):
    return any(not name.startswith("-") and (name in redact_hook.SEARCHERS or name.endswith("grep")) for name, _ in entries)


class _Wrong:
    """A wrong table: `unwrap` answers `answer`, or the wrapper's row is emptied of everything but what it hands on."""
    def __init__(self, label, answer=None, emptied=False):
        self.label, self.answer, self.emptied = label, answer, emptied

    def __repr__(self):
        return self.label


WRONG_TABLES = [_Wrong("unwrap=None", None), _Wrong("unwrap=echo words", ("words", ["echo"])),
                _Wrong("unwrap=echo line", ("line", "echo")), _Wrong("row emptied", emptied=True)]


def wrong_table(name, wrong):
    stack = contextlib.ExitStack()
    if wrong.emptied:
        row = table.SPECS[name]
        stack.enter_context(mock.patch.dict(table.SPECS, {name: table._spec(rest=row.rest)._replace(string=row.string)}))
    else:
        stack.enter_context(mock.patch.object(table, "unwrap", return_value=wrong.answer))
    return stack



class WrapperFloorTests(WrapperTreeCase):
    """Round 4: what a wrapper hands on is scanned whole; the option table may only narrow."""

    def test_ordinary_wrapper_commands_are_judged_as_on_main(self):
        for command in ORDINARY + ["git grep foo"]:
            with self.subTest(command=command):
                self.assertEqual(redact_hook.bash_searches(command), command == "git grep foo")
                self.assertFalse(self.reads(command))

    def test_a_wrapper_before_ssh_or_eval_reads_what_the_bare_command_reads(self):
        # Main's ruling on round 7: the floor handed ssh and eval the string and then the words made from it, so
        # `sudo ssh host 'git grep foo'` was read as `git grep foo git grep foo` (`git` a path) and was a read, while the bare command
        # and `sudo git grep foo` were not. A wrapper adds no read of its own; a credential read behind it is still a read.
        outers = ["sudo ", "nice ", "sudo -h localhost ", "env ", "nohup ", "sudo nice "]
        not_reads = ["ssh host 'git grep foo'", "ssh -p 22 host 'git grep -e foo'", "ssh host git grep foo", "eval 'git grep foo'",
                     "eval git grep foo", "ssh host 'ls -la'", "eval 'ls -la'"]
        reads = ["ssh host 'cat .env'", "ssh host cat .env", "ssh host 'cat .aws/credentials'", "ssh host 'rg --hidden TOKEN .'",
                 "ssh host 'git grep foo -- .env'", "ssh host 'git log -p'", "ssh host 'git grep foo bar'", "eval 'cat .env'",
                 "eval 'rg --hidden x .'", "eval 'git grep foo bar'"]
        for inner in not_reads + reads:
            bare = self.reads(inner)
            self.assertEqual(bare, inner in reads, inner)
            for outer in outers:
                with self.subTest(command=outer + inner):
                    self.assertEqual(self.reads(outer + inner), bare)

    def test_a_table_that_is_wrong_cannot_hide_a_search_or_a_read(self):
        # the table says nothing is run, or that only `echo` is: whatever it says, the words after the wrapper are still looked at
        for wrong in (None, ("words", ["echo"]), ("line", "echo")):
            with mock.patch.object(table, "unwrap", return_value=wrong):
                for form in FORMS:
                    if form.startswith(("busybox --list", "setsid nice flock")):
                        continue
                    with self.subTest(wrong=wrong, form=form):
                        self.assertTrue(redact_hook.bash_searches(run(form, HIDDEN)))
                        self.assertTrue(self.reads(run(form, HIDDEN)))

    def test_a_table_that_is_wrong_cannot_hide_uncertain_text(self):
        # round 5 rule 2: the uncertainty floor does not depend on the table either
        for wrong in (None, ("words", ["echo"]), ("line", "echo")):
            with mock.patch.object(table, "unwrap", return_value=wrong):
                for command in ("sudo -h localhost su -c '$X' nobody", "sudo -h localhost flock /tmp/l -c '$X'",
                                "sudo -h localhost ssh host '$X'", "ssh host '$1'", "ssh host 'echo $?'",
                                "nice -n 5 su -c 'docker exec c ls' nobody"):
                    with self.subTest(wrong=wrong, command=command):
                        self.assertTrue(redact_hook.bash_searches(command))
                        self.assertTrue(self.reads(command))

    def test_every_spelling_of_a_handed_string_reaches_the_floors_whatever_the_table_says(self):
        # round 6 rule 2: every text-handing wrapper x every spelling its row allows x every payload x outer context x wrong table.
        # The spellings are made from the row itself (`Spec.string`, `Spec.flags`), so a row cannot gain an option this skips.
        # Round 7 rule 3: no read assertion is left out. What the read must be is judged with the correct table, once per command,
        # and a wrong table may never turn a read into a non-read.
        mixes = list(handing_variants())
        wrapper_names = {name for name, _, _, _ in mixes}
        self.assertEqual(wrapper_names, {name for name, spec in table.SPECS.items() if spec.string or spec.rest})
        shapes = {kind for _, kind, _, _ in mixes}
        for kind in ("separate", "attached", "cluster separate", "cluster attached", "long=", "long separate", "abbreviated=",
                     "abbreviated separate", "one word", "words"):
            self.assertIn(kind, shapes)
        expected = len(mixes) * len(PAYLOADS) * len(CONTEXTS) * len(WRONG_TABLES)
        done = read_checked = read_true = read_false = 0
        more, lost = Counter(), Counter()  # cells where a wrong table reads and the correct table does not, and the reverse
        for name, kind, frame, make in mixes:
            for payload, how in PAYLOADS:
                tail = make(payload)
                for context in CONTEXTS:
                    words = context + [name] + tail
                    command = shlex.join(words)
                    outer = context[0] if context else name
                    outer_tail = words[1:]
                    want = self.reads(command)  # the correct table
                    read_true, read_false = read_true + want, read_false + (not want)
                    if payload != "git grep foo":  # `rg --hidden`, shells, expansions: reads whatever the wrapper
                        self.assertTrue(want, command)
                    # the floors alone, with the real table: the string reaches them
                    if how == "uncertain":
                        self.assertTrue(table.floor_uncertain(outer, outer_tail), command)
                    else:
                        self.assertTrue(has_search(table.bag(outer_tail)[0]), command)
                    for wrong in WRONG_TABLES:
                        with wrong_table(name, wrong):
                            done += 1
                            if how == "uncertain":
                                self.assertTrue(table.floor_uncertain(outer, outer_tail), (wrong, command))
                            else:
                                self.assertTrue(has_search(table.bag(outer_tail)[0]), (wrong, command))
                            self.assertTrue(redact_hook.bash_searches(command), (wrong, command))
                            got = self.reads(command)
                            read_checked += 1
                            if want and not got:  # a wrong table never turns a True into a False: none are allowed, see the end
                                lost[(name, outer, wrong.label, payload)] += 1
                            elif got and not want:
                                more[(name, outer, wrong.label)] += 1
        print("round 7 wrong-table product: %d wrappers, %d spellings x frames, %d payloads, %d contexts, %d tables = %d cases; "
              "read assertions made %d (none omitted); correct-table reads: %d True, %d False; wrong table reads where the correct "
              "table does not: %s; correct table reads and a wrong table does not: %s" % (
                  len(wrapper_names), len(mixes), len(PAYLOADS), len(CONTEXTS), len(WRONG_TABLES), done, read_checked, read_true,
                  read_false, dict(more), dict(lost)))
        self.assertEqual(done, expected)
        self.assertEqual(read_checked, expected)
        self.assertEqual(read_true + read_false, len(mixes) * len(PAYLOADS) * len(CONTEXTS))
        # The invariant, with no exception: a wrong table never turns a read into a non-read. (Round 7 had 14 cells,
        # `sudo|nice ssh host 'git grep foo'`: the floor handed the ssh branch `git grep foo git grep foo`, the string and then the words
        # made from it, so the correct table read `git` as a path. `bag(once=...)` hands it each word once; see
        # test_a_wrapper_before_ssh_or_eval_reads_what_the_bare_command_reads.)
        self.assertEqual(dict(lost), {})

    def test_a_string_option_that_two_rows_read_differently_is_read_every_way(self):
        # round 7 rule 2: `su -c'rg --hidden TOKEN . -Sls'` is su's `rg --hidden TOKEN . -Sls` and env's `ls`; the first row that
        # claims the word must not hide the other. Every pair of rows with a string option x each row's spelling (the same
        # spellings as above) x the competing option of the other row (its short option attached, its long form with `=`) placed
        # before, in the middle of, and after the payload x the payloads. With `unwrap` made to fail, each case is still flagged.
        rows = sorted(name for name, spec in table.SPECS.items() if spec.string)
        competing = {name: [flag + "ls" if not flag.startswith("--") else flag + "=ls" for flag in sorted(table.SPECS[name].string)]
                     for name in rows}
        for name in rows:  # the competing options are made from the rows: each has a short and a long form here
            self.assertTrue(competing[name], name)
        placements = (lambda p, c: c + " " + p, lambda p, c: p.replace(" ", " " + c + " ", 1) if " " in p else p + " " + c,
                      lambda p, c: p + " " + c)
        mixes = [(name, kind, frame, make) for name, kind, frame, make in handing_variants() if name in rows]
        expected = sum(len(competing[other]) for name, *_ in mixes for other in rows if other != name) * len(placements) * len(PAYLOADS)
        done = reads_true = reads_false = 0
        lost = Counter()
        real_unwrap = table.unwrap
        with mock.patch.object(table, "unwrap", return_value=None):
            for name, kind, frame, make in mixes:
                for other in rows:
                    if other == name:
                        continue
                    for option in competing[other]:
                        for place in placements:
                            for payload, how in PAYLOADS:
                                command = shlex.join([name] + make(place(payload, option)))
                                done += 1
                                with mock.patch.object(table, "unwrap", real_unwrap):
                                    want = self.reads(command)  # the correct table
                                if how == "uncertain":
                                    self.assertTrue(table.floor_uncertain(name, shlex.split(command)[1:]), command)
                                else:
                                    self.assertTrue(has_search(table.bag(shlex.split(command)[1:])[0]), command)
                                self.assertTrue(redact_hook.bash_searches(command), command)
                                got = self.reads(command)
                                reads_true, reads_false = reads_true + want, reads_false + (not want)
                                if payload != "git grep foo":  # `rg --hidden`, shells, expansions: read whatever the wrapper
                                    self.assertTrue(want, command)
                                    self.assertTrue(got, command)
                                elif want and not got:  # only `git grep foo`, which is no credential read: see below
                                    lost[name] += 1
        print("round 7 competing readings: %d rows with a string option, %d ordered pairs, %d spellings of the first row, %d cases; "
              "correct-table reads %d True, %d False; `git grep foo` read True by the correct table and False without it: %s" % (
                  len(rows), len(rows) * (len(rows) - 1), len(mixes), done, reads_true, reads_false, dict(lost)))
        self.assertEqual(done, expected)
        # The only True -> False cells (listed for Main's ruling): `sg GROUP --split-string=ls git grep foo`. `unwrap` cannot place
        # an option-looking word after sg's group and answers UNKNOWN, which fails closed to a read; without the table the floor
        # sees `git grep` at depth 0, which is not a read. The correct table over-reads; no wrong table misses a real read.
        self.assertEqual(dict(lost), {"sg": 12})

    def test_the_floor_reads_strings_inside_strings_whatever_the_wrapper(self):
        for command in ("timeout 5 sh -c \"bash -c 'rg --hidden TOKEN .'\"", "nice -n 5 env -S 'x rg --hidden TOKEN .'",
                        "caffeinate -w 1 su -qcrg\\ --hidden\\ TOKEN\\ . nobody", "time -p sh -c 'sh -c \"rg --hidden TOKEN .\"'"):
            with self.subTest(command=command):
                self.assertTrue(redact_hook.bash_searches(command))
                self.assertTrue(self.reads(command))
        self.assertTrue(self.reads("timeout 5 sh -c \"bash -c 'cat " + ENV_NAME + "'\""))

    def test_a_program_name_that_is_an_option_or_a_path_part_is_not_a_search(self):
        for command in ("timeout 5 make --grep", "sudo -u nobody make --hidden-rg", "nice make /usr/share/doc/rg-notes",
                        "ssh host 'ls /usr/bin/rg-extra'"):
            with self.subTest(command=command):
                self.assertFalse(redact_hook.bash_searches(command))
                self.assertFalse(self.reads(command))


class FlattenTests(unittest.TestCase):
    """Round 5 rule 1: flattening keeps the order, so the words after a string stay after its words."""

    def test_the_words_after_a_string_follow_the_words_of_the_string(self):
        flat, ends, deep = table.flatten(["env", "-S", "rg --hidden", "TOKEN", "."])
        self.assertEqual(flat, ["env", "-S", "rg --hidden", "rg", "--hidden", "TOKEN", "."])
        self.assertFalse(deep)
        self.assertEqual(ends[2], 5)  # the string's words end where TOKEN starts
        self.assertIn(("rg", ["--hidden", "TOKEN", "."]), table.bag(["-S", "rg --hidden", "TOKEN", "."])[0])

    def test_the_program_is_the_base_name_of_the_whole_word_and_its_arguments_follow(self):
        entries, _ = table.bag(["/tmp/a b/rg", "--hidden", "TOKEN", "."])
        self.assertIn(("rg", ["--hidden", "TOKEN", "."]), entries)

    def test_a_program_that_joins_its_words_is_handed_each_word_once(self):
        # `ssh host 'git grep foo'`: the entry of every other program has the words of the string after the string (they are
        # commands of their own); an entry named in `once` has the line once
        tail = ["ssh", "host", "git grep foo", "-q"]
        every, _ = table.bag(tail)
        once, _ = table.bag(tail, once=("ssh",))
        self.assertIn(("ssh", ["host", "git grep foo", "git", "grep", "foo", "-q"]), every)
        self.assertIn(("ssh", ["host", "git grep foo", "-q"]), once)
        self.assertEqual([entry for entry in every if entry[0] != "ssh"], [entry for entry in once if entry[0] != "ssh"])
        # a string inside a string is kept as one word too, and a program inside a string still has the words after its own
        entries, _ = table.bag(["ssh", "host", "sh -c 'git grep foo'", "x"], once=("ssh",))
        self.assertIn(("ssh", ["host", "sh -c 'git grep foo'", "x"]), entries)
        self.assertIn(("git", ["grep", "foo", "x"]), entries)

    def test_a_string_inside_a_string_is_placed_in_order(self):
        flat, _, _ = table.flatten(["sh -c 'rg --hidden' TOKEN", "."])
        self.assertEqual(flat[-4:], ["rg", "--hidden", "TOKEN", "."][-4:])

    def test_a_search_in_a_string_is_judged_over_the_words_after_it(self):
        for command in ("sudo -h localhost env -S 'rg --hidden' TOKEN .", "env -S 'rg' --hidden TOKEN .",
                        "nice -n 5 env -S 'timeout 5 rg' --hidden TOKEN ."):
            with self.subTest(command=command):
                self.assertTrue(redact_hook.bash_searches(command))


def _subsequence(small, big):
    words = iter(big)
    return all(word in words for word in small)


class StringOptionTests(unittest.TestCase):
    """Round 6 rule 1: every spelling of a string option is read from the row's `string` field, in one place."""

    def test_the_spellings_of_a_string_option(self):
        env, su, flock = table.SPECS["env"], table.SPECS["su"], table.SPECS["flock"]
        for spec, word, expected in (
                (env, "-S", (None, True)), (env, "-Ssh", ("sh", False)), (env, "-iS", (None, True)), (env, "-iSsh", ("sh", False)),
                (env, "-vSrg --hidden", ("rg --hidden", False)), (env, "--split-string=sh", ("sh", False)),
                (env, "--split-string", (None, True)), (env, "--split=sh", ("sh", False)), (env, "--spl", (None, True)),
                (su, "-c", (None, True)), (su, "-ccmd", ("cmd", False)), (su, "-lc", (None, True)), (su, "-lccmd", ("cmd", False)),
                (su, "--command=cmd", ("cmd", False)), (su, "--command", (None, True)), (su, "--comm=cmd", ("cmd", False)),
                (flock, "-nc", (None, True)), (flock, "-ncsh", ("sh", False)), (flock, "--command=sh", ("sh", False)),
                # an option that takes a value and is not a string option ends the word; a long form that is not one is not one
                (su, "-sc", None), (su, "-sccmd", None), (su, "-g", None), (su, "--shell=sh", None), (su, "--", None),
                (env, "-u", None), (env, "-uS", None), (env, "-i", None), (env, "--ignore-environment", None),
                (env, "-", None), (env, "x", None)):
            with self.subTest(word=word):
                self.assertEqual(table.string_option(spec, word), expected)

    def test_every_string_option_of_every_row_is_recognised_in_every_spelling(self):
        for name, spec in sorted(table.SPECS.items()):
            for flag in sorted(spec.string):
                with self.subTest(wrapper=name, option=flag):
                    if flag.startswith("--"):
                        self.assertEqual(table.string_option(spec, flag + "=X"), ("X", False))
                        self.assertEqual(table.string_option(spec, flag), (None, True))
                    else:
                        self.assertEqual(table.string_option(spec, flag + "X"), ("X", False))
                        self.assertEqual(table.string_option(spec, flag), (None, True))
                        for letter in sorted(f[1] for f in spec.flags if len(f) == 2):
                            self.assertEqual(table.string_option(spec, "-" + letter + flag[1]), (None, True))
                            self.assertEqual(table.string_option(spec, "-" + letter + flag[1] + "X"), ("X", False))

    def test_the_shell_options_of_sudo(self):
        sudo = table.SPECS["sudo"]
        for word, expected in (("-s", True), ("-i", True), ("--shell", True), ("--login", True), ("--sh", True), ("--log", True),
                               ("-Es", True), ("-Ei", True), ("-ns", True), ("-sE", True),
                               ("-us", False), ("-u", False), ("-hs", False), ("-Cs", False), ("-H", False), ("-A", False),
                               ("--user", False), ("--", False), ("-", False), ("x", False), ("", False)):
            with self.subTest(word=word):
                self.assertEqual(table.shell_option(sudo, word), expected)
        for name, spec in sorted(table.SPECS.items()):  # only a row that lists shell options has any
            for word in ("-s", "-i", "--shell", "--login"):
                if not spec.shell:
                    self.assertFalse(table.shell_option(spec, word), (name, word))
        self.assertEqual({name for name, spec in table.SPECS.items() if spec.shell}, {"sudo"})

    def test_a_word_is_read_every_way_whatever_the_order_of_the_rows(self):
        # round 7 rule 1: for any word, the bag and the uncertainty with every row's reading are a superset of those with any single
        # row's reading, and no order of the rows changes a result.
        rows = sorted(name for name, spec in table.SPECS.items() if spec.string)
        texts = ("rg --hidden TOKEN . -Sls", "$X", "sh -cls", "ls", "rg --hidden TOKEN . --command=ls", "bash -s")
        words = set()
        for name in rows:
            for flag in table.SPECS[name].string:
                for text in texts:
                    words.add(flag + text if not flag.startswith("--") else flag + "=" + text)
                    words.add(flag + text.split()[0])
        words |= {"-Sls", "-cls", "--command=ls", "-S", "-c", "x", "-Es", "--split-string=sh"}
        reversed_rows = dict(reversed(list(table.SPECS.items())))
        seen = 0
        for word in sorted(words):
            tail = [word, "rg", "--hidden", "TOKEN", "."]
            flat, ends, deep = table.flatten(tail)
            every = {(name, tuple(args)) for name, args in table.bag(tail)[0]}
            every_uncertain = table._uncertain_words(flat)
            with mock.patch.object(table, "SPECS", reversed_rows):
                self.assertEqual(table.flatten(tail), (flat, ends, deep), word)
                self.assertEqual(sorted(table.readings(word)), sorted(table.readings(word)))
            self.assertEqual(table.readings(word), sorted(table.readings(word)))
            for name in rows:
                if not (table.string_option(table.SPECS[name], word) or (None,))[0]:
                    continue  # this row does not read the word; the word split on its own is not a row's reading
                with mock.patch.object(table, "SPECS", {name: table.SPECS[name]}):
                    one_flat, _, _ = table.flatten(tail)
                    one = {(n, tuple(args)) for n, args in table.bag(tail)[0]}
                    one_uncertain = table._uncertain_words(one_flat)
                self.assertLessEqual(set(one_flat), set(flat), (word, name))
                for entry, args in one:  # every look of the single row is in the union (a reading may add words inside the list)
                    self.assertTrue(any(entry == other and _subsequence(args, rest) for other, rest in every), (word, name, entry))
                self.assertTrue(every_uncertain or not one_uncertain, (word, name))
                seen += 1
        self.assertGreater(seen, len(words))  # every word is read by at least one row, most by two or more
        # and the reported case: the word is read as su reads it and as env reads it
        self.assertEqual(table.readings("-c'rg x -Sls'"), sorted(table.readings("-c'rg x -Sls'")))
        self.assertIn("rg x -Sls", table.readings("-crg x -Sls"))
        self.assertIn("ls", table.readings("-crg x -Sls") + table.readings("-Sls"))

    def test_a_value_attached_to_a_string_option_is_exposed_to_the_floor(self):
        flat, ends, deep = table.flatten(["-Srg", "--hidden", "TOKEN", "."])
        self.assertEqual(flat, ["-Srg", "rg", "--hidden", "TOKEN", "."])
        self.assertFalse(deep)
        self.assertIn(("rg", ["--hidden", "TOKEN", "."]), table.bag(["-Srg", "--hidden", "TOKEN", "."])[0])
        self.assertTrue(table.floor_uncertain("env", ["-Ssh"]))
        self.assertEqual(table.floor_uncertain("env", ["-Ssh"]), table.floor_uncertain("env", ["-S", "sh"]))
        self.assertTrue(table.floor_uncertain("env", ["-iS$X"]))
        self.assertTrue(table.floor_uncertain("env", ["--split-string=sh"]))
        self.assertFalse(table.floor_uncertain("env", ["-Sls"]))


class UncertainTextTests(WrapperTreeCase):
    """Round 4 rule 2: text a wrapper hands on is sensitive when it cannot be read for certain. No allow-list of shapes."""
    HANDED = ["ssh host {Q}", "su -c {Q} nobody", "flock /tmp/l -c {Q}", "script -qc {Q} /dev/null", "sg staff -c {Q}",
              "runuser -u nobody -c {Q}", "env -S {Q}", "setsid timeout 5 su -c {Q} nobody", "ssh -p 22 -- host {Q}"]
    UNCERTAIN = ["$X", "echo ${X}", "echo $(date)", "echo `date`", "sh", "bash", "sh -cx 'ls'", "bash -xc 'ls'", "zsh -ec ls",
                 "sh script.sh", "sh -c", "find . -exec ls {} \\;", "find . -execdir ls {} +", "find . -ok rm {} \\;", "xargs ls",
                 "docker exec c ls", "docker run img ls", "podman exec c ls", "kubectl exec p -- ls", "oc exec p -- ls",
                 "nsenter -t 1 ls", "chroot / ls", "parallel ls ::: a", "watch ls", "eval ls", "exec ls", "command ls",
                 "builtin ls", "sh -c 'sh -cx ls'", "sh -c \"sh -c 'echo $X'\"", "ssh h 'docker exec c sh -c \"$X\"'"]
    CERTAIN = ["ls", "make all", "echo hi", "sh -c 'ls'", "uptime", "ls -la /tmp", "echo hi; echo there", "sh -c 'sh -c ls'",
               "docker ps", "docker images", "find . -name x", "kubectl get pods"]

    def test_every_uncertain_text_is_a_search_and_a_read_under_every_text_wrapper(self):
        for form in self.HANDED:
            for text in self.UNCERTAIN:
                command = form.replace("{Q}", shlex.quote(text))
                with self.subTest(command=command):
                    self.assertTrue(redact_hook.bash_searches(command))
                    self.assertTrue(self.reads(command))

    def test_a_text_that_can_be_read_for_certain_is_neither(self):
        for form in self.HANDED:
            for text in self.CERTAIN:
                command = form.replace("{Q}", shlex.quote(text))
                with self.subTest(command=command):
                    self.assertFalse(redact_hook.bash_searches(command))
                    self.assertFalse(self.reads(command))

    def test_the_users_own_words_are_not_made_uncertain(self):
        # only text a wrapper hands on is: the same shell shapes as the user's own words keep their behaviour
        for command in ("timeout 5 sh -cx 'ls'", "setsid docker exec c ls", "sudo find . -exec ls {} +", "nice sh -c 'echo $HOME'"):
            with self.subTest(command=command):
                self.assertFalse(self.reads(command))


class ReviewReproducers(WrapperTreeCase):
    """The commands the reviews of FG-N1 reported, verbatim: each is a search and a read."""
    COMMANDS = [
        # round 1
        "flock -- lock rg --hidden TOKEN .", "chroot -- / rg --hidden TOKEN .", "ssh -- host rg --hidden TOKEN .",
        "ssh host '$REMOTE_COMMAND'",
        # round 2
        "env -u su setsid su --command='rg\t--hidden\tTOKEN\t.' nobody",
        "env -u su setsid su -c'rg --hidden TOKEN .' nobody",
        "ssh host 'sh -c \"$REMOTE_COMMAND\"'",
        "su -c 'sh -c \"$REMOTE_COMMAND\"' nobody",
        # the worker's own note
        "sudo -n rg --hidden TOKEN .",
        # round 3, review-coder-b9c961eade-185926 (F1, F2, F3), verbatim
        "sudo -h localhost rg --hidden TOKEN .",
        "script -F out.txt rg TOKEN --hidden .",
        "env -S 'sudo -n' rg --hidden TOKEN .",
        "ssh host 'sh -cx \"$REMOTE_COMMAND\"'",
        "su -c 'bash -cx \"$REMOTE_COMMAND\"' nobody",
        "ssh host 'find . -exec sh -c \"$REMOTE_COMMAND\" \\;'",
        "ssh host 'docker exec c sh -c \"$REMOTE_COMMAND\"'",
        "ssh host sh",
        # round 4, review-coder-8b20c9cf68-205249 (probe.py), verbatim
        "sudo -h localhost su -c '$X' nobody",
        "sudo -h localhost flock /tmp/l -c '$X'",
        "sudo -h localhost ssh host '$X'",
        "sudo -h localhost env -S 'rg --hidden' TOKEN .",
        "sudo -h localhost '/tmp/a b/rg' --hidden TOKEN .",
        "ssh host '$1'",
        "ssh host '$@'",
        "ssh host 'echo $?'",
        # round 6, review-coder-d68351c236-215057 (probe.py), verbatim
        "env -Ssh",
        "env -Srg --hidden TOKEN .",
        # round 6 rule 3: one attached (or clustered) spelling for each other text-handing wrapper
        "env -iSrg --hidden TOKEN .",
        "env -vS'rg --hidden' TOKEN .",
        "env --split=sh",
        "env -S$X",
        "su -lc'rg --hidden TOKEN .' nobody",
        "su -c'$X' nobody",
        "runuser -u nobody -c'rg --hidden TOKEN .'",
        "runuser -lc'$@' nobody",
        "sg staff -c'rg --hidden TOKEN .'",
        "sg staff -c'$1'",
        "flock /tmp/l -c'rg --hidden TOKEN .'",
        "flock -nc'sh' /tmp/l",
        "script -qc'rg --hidden TOKEN .' /dev/null",
        "script -qc'bash -s' /dev/null",
        "sudo -h localhost env -Ssh",
        "nice -n 5 env -Srg --hidden TOKEN .",
        # round 7 F1, review-coder-acf3f2f10c-223756 (fgn1r7probe.py), verbatim: two rows read the same word
        "su -c'rg --hidden TOKEN . -Sls' nobody",
        "su -c'sh -Sls' nobody",
        "flock L -c'rg --hidden TOKEN . -Sls'",
        "script -qc'rg --hidden TOKEN . -Sls' /dev/null",
        # round 7 F3: sudo shell mode hands its arguments to a shell as one command line
        "sudo -s \"$X\"", "sudo --shell \"$X\"", "sudo -i \"$X\"", "sudo --login \"$X\"", "sudo -Es \"$X\"", "sudo -Ei \"$X\"",
        "sudo -s rg --hidden TOKEN .", "sudo -i rg --hidden TOKEN .",
        "sudo -h localhost sudo -s \"$X\"", "sudo -h localhost sudo -i rg --hidden TOKEN .", "nice -n 5 sudo -s \"$X\"",
        "sudo -u root -s \"$X\"", "sudo -s -- \"$X\"",
    ]
    # round 6: the probe's third command is a search (git grep) and not a read of a credential, on main too
    SEARCH_ONLY = ["sudo git grep TOKEN"]
    # round 5 rule 3: an expansion is `$` and a name start, `{`, `(`, a digit or one of @ * # ? $ ! -, or a backtick
    EXPANSIONS = ["$X", "$_", "${X}", "$(x)", "$1", "$0", "$9", "$@", "$*", "$#", "$?", "$$", "$!", "$-", "`x`"]
    NOT_EXPANSIONS = ["ls $", "ls $ x", "ls a$", "ls '$'", "ls 5$", "ls $."]

    def test_a_recursive_grep_behind_stacked_wrappers_is_a_search(self):
        # round 2 F2: the output of this is grep lines (it is not a read: grep -r skips hidden files)
        self.assertTrue(redact_hook.bash_searches("setsid su --command='grep\t-r\tTOKEN\t.' nobody"))

    def test_each_reported_command_is_a_search_and_a_read(self):
        for command in self.COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(redact_hook.bash_searches(command))
                self.assertTrue(self.reads(command))

    def test_a_search_that_is_not_a_read_stays_one(self):
        for command in self.SEARCH_ONLY:
            with self.subTest(command=command):
                self.assertTrue(redact_hook.bash_searches(command))
                self.assertFalse(self.reads(command))

    def test_each_reported_command_is_caught_whatever_the_table_says(self):
        # round 6: the review's probe made unwrap wrong; the floors alone must catch every command the reviews reported
        for wrong in WRONG_TABLES[:3]:
            with mock.patch.object(table, "unwrap", return_value=wrong.answer):
                for command in self.COMMANDS:
                    with self.subTest(wrong=wrong, command=command):
                        self.assertTrue(redact_hook.bash_searches(command))
                        self.assertTrue(self.reads(command))

    def test_sudo_shell_mode_with_no_arguments_runs_only_a_shell(self):
        # round 7 rule 4: `sudo -s` and `sudo -i` stay as they are today; so do ordinary commands behind them
        for command in ("sudo -s", "sudo -i", "sudo --shell", "sudo --login", "sudo -Es", "sudo -h localhost sudo -s", "sudo -u root -i",
                        "sudo -s ls -la", "sudo -i make install", "sudo -s make -j4", "sudo -s 'echo hi'"):
            with self.subTest(command=command):
                self.assertFalse(redact_hook.bash_searches(command))
                self.assertFalse(self.reads(command))

    def test_sudo_user_cluster_is_a_user_not_shell_mode(self):
        # `-us` is -u with the user `s`: the table (and only the table: it is not a shell option) reads it as it reads `-u s`
        for command in ("sudo -us '$X'", "sudo -us 'rg --hidden TOKEN .'"):
            with self.subTest(command=command):
                twin = command.replace("-us", "-u s")
                self.assertEqual(redact_hook.bash_searches(command), redact_hook.bash_searches(twin))
                self.assertEqual(self.reads(command), self.reads(twin))
        self.assertTrue(redact_hook.bash_searches("sudo -us 'rg --hidden TOKEN .'"))

    def test_sudo_shell_mode_reads_what_the_floor_cannot_place(self):
        # The table-free floor cannot tell `-n` (a flag) from `-u` (takes a value), so in `sudo -n du -sh $DIR` it takes `du` for
        # the value of `-n` and `-sh` for sudo's `-s`: an extra look at `$DIR`, a read. Path: `_floor_hit` -> `floor_uncertain` ->
        # `handed_text` -> `_shell_text` (stops at `-sh` / `-s`, `shell_option` true) -> the text after it holds `$DIR` (an unresolved
        # expansion) or `/bin/sh nobody -c ...` (a shell with no `-c` right after it). These four are the known over-reads of the
        # round 7 scan (accepted by Main as conservative, not to be tuned away), pinned here as what they are, not as wanted answers.
        # The same commands with no flag before the program (`sudo du -sh $DIR`) are neither a search nor a read.
        for command in ("sudo -n du -sh $DIR", "sudo -n ls -sh $HOME", "sudo -n rsync -a -s src $DEST",
                        "sudo -E su -s /bin/sh nobody -c 'ls -la'"):
            with self.subTest(command=command):
                self.assertTrue(redact_hook.bash_searches(command))
                self.assertTrue(self.reads(command))
        for command in ("sudo du -sh $DIR", "sudo -u root du -sh $DIR"):
            with self.subTest(command=command):
                self.assertFalse(redact_hook.bash_searches(command))
                self.assertFalse(self.reads(command))

    def test_every_expansion_form_is_uncertain_in_text_a_wrapper_hands_on(self):
        for form in ("ssh host {Q}", "su -c {Q} nobody", "sudo -h localhost flock /tmp/l -c {Q}", "env -S {Q}"):
            for text in self.EXPANSIONS:
                command = form.replace("{Q}", shlex.quote("echo " + text))
                with self.subTest(command=command):
                    self.assertTrue(redact_hook.bash_searches(command))
                    self.assertTrue(self.reads(command))
            for text in self.NOT_EXPANSIONS:
                command = form.replace("{Q}", shlex.quote(text))
                with self.subTest(command=command):
                    self.assertFalse(redact_hook.bash_searches(command))
                    self.assertFalse(self.reads(command))

    def test_the_users_own_top_level_shell_string_keeps_its_behaviour(self):
        self.assertTrue(redact_hook.bash_searches("sh -c '$X'"))   # the output is read as unknown text
        self.assertFalse(self.reads("sh -c '$X'"))                 # but the user's own expansion is not a read
        self.assertFalse(self.reads("setsid sh -c '$X'"))


if __name__ == "__main__":
    unittest.main()
