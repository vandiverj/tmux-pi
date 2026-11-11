"""Identify ordinary tool reads that may expose credentials or sensitive config.

Shell commands are judged by path-shaped tokens and by the root of each bulk read, not by
substrings of the command text. Relative paths are resolved against the shell's working
directory when the caller supplies it and against this process's when the caller supplies
none. A directory that is supplied but is not absolute cannot be placed, so nothing is assumed
about where relative paths lead. A script that builds a path at run time cannot be seen here.

One kind of command is judged by asking git: an unscoped `git diff`, `git log -p` or
`git stash show -p` is decided by the paths its patch would cover, listed by a names-only query
in the caller's working directory. No file content is read, and the patch prompts whenever that
list cannot be established or the patch may print more than the list names: files inside a
submodule, or whatever a configured diff program or text conversion writes.

A caller that prompts the owner may pass a `trust` resolver (lib/trust_lift.py). Where the owner has
stated that a working tree holds no credentials, these guesses are lifted for paths inside that tree
(or inside a linked worktree of it that the record covers):

  L1  a weak name (`*secret*`, `*credentials*`) that is not a known credential file name;
  L2  the name of a directory (`agent`, `claude`, `config`) whose entries, read by name only, hold no
      credential location;
  L3  the repository root, or any root of a hidden or unignored search, when the same walk of what the
      command would visit finds no credential location (`rg foo .` passes, `grep -r foo .` reaches `.git`);
  L4  an unscoped, `--check` or pathspec patch whose names-only git listing holds no credential path.

Nothing else changes, and with no resolver the result is what it always was.
"""
import collections
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import posixpath
import re
import select
import shlex
import signal
import stat
import subprocess
import tempfile
import time

import command_wrappers

# Names that are credential files wherever they appear, names that are only key material as a
# file suffix, and names that are only sensitive when a command actually reads that file.
_STRONG = (r"\.env(?:[._-].*)?|\.envrc|\.netrc|\.npmrc|\.pypirc|auth\.json|mcp-auth\.json|\.?mcp\.json|"
           r"mcp\.local\.env|models-store\.json|web\.config|appsettings(?:\.[^.]+)?\.json|"
           r"id_(?:rsa|ed25519|ecdsa).*")
_KEYS = r".*\.(?:pem|p12|pfx|key|token)|(?!.*\.example\.).*\.local\..+"
_WEAK = r".*credentials.*|.*secret.*|.*\.env"
SENSITIVE_NAME = re.compile(rf"^(?:{_STRONG}|{_KEYS}|{_WEAK})$", re.I)
STRONG_NAME = re.compile(rf"^(?:{_STRONG})$", re.I)
_STRONG_OR_KEY = re.compile(rf"^(?:{_STRONG}|{_KEYS})$", re.I)
_WEAK_NAME = re.compile(rf"^(?:{_WEAK})$", re.I)
_SUFFIX_TOKEN = re.compile(rf"^(?!\.)(?:{_KEYS})$", re.I)
# A backup copy of a credential file is the same credential file.
_BACKUP = re.compile(r"(?:\.(?:bak|backup|orig|old|save|swp|tmp|copy|\d+)|~)+$", re.I)
SENSITIVE_DIR = {".aws", ".ssh", ".gnupg", ".kube", ".docker"}
SENSITIVE_CONFIG_DIR = {"gcloud", "gh"}
SENSITIVE_PAIRS = ({(".config", name) for name in SENSITIVE_CONFIG_DIR} |
                   {("agent", "sessions"), ("config", "local.json"), (".git", "config"), (".git", "credentials")})
# Directories that hold credential files next to ordinary ones in this repository layout.
CREDENTIAL_DIRS = {"agent", "claude", "config"}
TOOL_PATH_KEYS = ("file_path", "path", "notebook_path", "glob")
_READ_TOOLS = {"Read", "read", "NotebookRead", "notebook_read", "Grep", "grep", "Glob", "glob", "find"}
_SHELL_TOOLS = {"Bash", "bash", "PowerShell", "powershell"}
# Grep type filters that cannot select a credential file.
_CODE_TYPES = {"py", "python", "js", "ts", "tsx", "jsx", "rust", "go", "java", "c", "cpp", "h", "sh", "md",
               "markdown", "html", "css", "ruby", "php", "swift", "kotlin", "lua"}

# A credential file name that every release has prompted on counts anywhere in raw command text,
# as a whole path segment: not after an identifier, `$`, `)`, `]` or `.` (attribute access such
# as an env object member), and not when the dot is a regex escape inside an identifier chain.
# Names specific to this repository layout are judged per command instead, so listing, staging
# or mentioning them does not prompt.
_SEGMENT = r"(?<![\w.$\])}?-])(?<![\w.$\])}?-]\\)"
INLINE_NAME = re.compile(
    _SEGMENT + r"(?:\.env(?:rc|[._-][\w.-]*)?(?![\w/])|\.(?:netrc|npmrc|pypirc)(?![\w-])|"
    r"auth\.json(?![\w-])|web\.config(?![\w-])|appsettings(?:\.[\w-]+)?\.json(?![\w-])|id_(?:rsa|ed25519|ecdsa)|"
    r"\.(?:aws|ssh|gnupg|kube|docker)(?![\w.-])|\.config/(?:gcloud|gh)(?![\w.-]))", re.I
)
_PLAIN = re.compile(r"^[\w.~/@%+$-]+$")
_PATHISH = re.compile(r"^[\w.~/@%+$*?\[\]{}!-]+$")
_LITERAL_PATH = re.compile(r"^[\w.~/@%+-]+$")
_LITERAL = re.compile(r"""(['"])([^'"\n]{1,200})\1""")
# Calls whose quoted argument is a file to read, and calls that read a whole directory.
_OPENER = re.compile(r"(?:open|Path|require|readFile(?:Sync)?|createReadStream|read_text|read_bytes|load|"
                     r"file_get_contents|fopen)\s*\(\s*$")
_WALKER = re.compile(r"(?:walk|copytree|make_archive)\s*\(\s*$")
_GLOB = re.compile(r"[*?\[]")
_BRACES = re.compile(r"\{[^{}]*\}")
_BRACE_LIST = re.compile(r"\{([^{}]*,[^{}]*)\}")
_VARIABLE = re.compile(r"\$\{?\w+\}?")
_PIECES = re.compile(r"[=:,]")
_ASSIGN = re.compile(r"^[A-Za-z_]\w*=")
_HEREDOC = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]*)'|"([^"\n]*)"|\\?([\w.+-]+))""")
_ANSI = re.compile(r"\\(x[0-9a-fA-F]{1,2}|u[0-9a-fA-F]{1,4}|[0-7]{1,3}|.)", re.S)
_REVISION = re.compile(r"^(?:HEAD|FETCH_HEAD|ORIG_HEAD|MERGE_HEAD|main|master|stash|origin/.*|[0-9a-f]{7,40}|.*[~^@].*|.*\.\..*)$")
_RECURSIVE_SHORT = re.compile(r"^-[A-Za-z]*[rR][A-Za-z0-9]*$")
_COPY_RECURSIVE = re.compile(r"^-[A-Za-z]*[rRa][A-Za-z]*$")
_RECURSIVE_LONG = {"--recursive", "--dereference-recursive", "--directories=recurse", "--archive"}
_HIDDEN_LONG = {"--hidden", "--no-ignore", "--no-ignore-vcs", "--unrestricted", "-uu", "-uuu"}
_HIDDEN_SHORT = re.compile(r"^-[A-Za-z.]*[u.][A-Za-z.]*$")
# Invocations that print tool information and search nothing.
_INFO_OPTS = {"--version", "--help", "--type-list", "--pcre2-version", "-V"}
# Names a glob is tested against to decide whether it can select a credential file or directory.
_FILE_SAMPLES = (".env", ".env.local", ".envrc", ".netrc", ".npmrc", ".pypirc", "auth.json", "mcp-auth.json",
                 "mcp.json", ".mcp.json", "mcp.local.env", "models-store.json", "local.json", "settings.local.json",
                 "web.config", "appsettings.json", "appsettings.production.json", "credentials", "credentials.json",
                 ".credentials.json", ".git-credentials", "secrets.json", "secrets.yml", "secrets.yaml",
                 "client_secret.json", "prod.env", "api.token", "id_rsa", "id_ed25519", "id_ecdsa", "id_rsa.pub",
                 "server.pem", "server.key", "cert.p12", "cert.pfx", "hosts.yml")
_SAMPLES = _FILE_SAMPLES + ("sessions", "agent", "claude", "config", ".aws", ".ssh", ".gnupg", ".kube", ".docker",
                            ".config", ".git")
# Extensions of source and prose files: a weak name with one of these is not a credential store.
_CODE_EXT = {"py", "pyc", "pyi", "js", "mjs", "cjs", "ts", "mts", "tsx", "jsx", "sh", "bash", "zsh", "fish", "rb", "go",
             "rs", "c", "h", "cc", "cpp", "hpp", "java", "kt", "swift", "md", "rst", "html", "css", "scss", "lua",
             "php", "pl", "pm", "ex", "exs", "cs", "vue", "svelte"}
_DATA_EXT = re.compile(r"^[a-z0-9]{1,8}$")
# Variables that name scratch space rather than a project tree.
_SCRATCH_VARS = ("TMPDIR", "TP_TASK_DIR")

_OPERATORS = ("<<<", "<<-", "&&", "||", "|&", ";;", ">>", "<<", ">&", "<&", "&>", "|", "&", ";", "(", ")", "<", ">")
_REDIRECTS = {"<<<", "<<-", ">>", "<<", ">&", "<&", "&>", "<", ">"}
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{"}
_SYNTAX = {"done", "fi", "esac", "}", "in"}
# Programs that run the command after them: one table (lib/command_wrappers.py), read by redact_hook too. `_program` replaces
# each of `_UNWRAPPED` by the command it runs, found with that wrapper's own option table (an option the table does not list
# makes the command unknown: a hit, _OPAQUE). `command_wrappers.OWN` are not replaced: ssh and xargs have branches of their own.
_UNWRAPPED = set(command_wrappers.NAMES - command_wrappers.OWN)
_OPAQUE = "\0wrapper with an option that cannot be read"
_ALIASES = {"gfind": "find", "ggrep": "grep", "gegrep": "egrep", "gfgrep": "fgrep", "gcat": "cat", "gzcat": "zcat",
            "ghead": "head", "gtail": "tail", "gtac": "tac", "gsort": "sort", "gcut": "cut", "gtar": "tar",
            "bsdtar": "tar", "gcp": "cp", "gdiff": "diff", "ripgrep": "rg"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish"}
_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php", "osascript"}
_GREPS = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "rga", "ugrep", "ug"}
_ALWAYS_RECURSIVE = {"rg", "ag", "ack", "rga", "ug"}
_SCRIPT_FIRST = {"sed", "gsed", "awk", "gawk", "mawk", "jq", "gojq", "yq"}
_READERS = {"cat", "tac", "nl", "head", "tail", "less", "more", "most", "bat", "batcat", "view", "vi", "vim", "nvim",
            "nano", "emacs", "code", "open", "cut", "sort", "uniq", "strings", "xxd", "hexdump", "od", "base64",
            "base32", "diff", "cmp", "comm", "paste", "cp", "mv", "ln", "scp", "sftp", "rsync", "ditto", "tar", "zip",
            "unzip", "gzip", "gunzip", "zcat", "bzcat", "xzcat", "zstdcat", "zless", "zmore", "bzip2", "xz",
            "openssl", "ssh", "ssh-add", "ssh-keygen", "gpg", "source", ".", "dd", "tr", "column", "fold", "rev",
            "glow", "mdcat", "jless", "fx", "delta", "hx", "micro", "subl", "pygmentize", "plutil", "get-content",
            "gc", "type", "select-string", "sls", "copy-item"}
# Commands that name, create or change files without printing what is in them.
_LISTERS = {"ls", "ll", "la", "stat", "test", "[", "[[", "file", "tree", "du", "df", "wc", "touch", "mkdir", "rm",
            "rmdir", "chmod", "chown", "echo", "printf", "basename", "dirname", "realpath", "readlink", "which",
            "pwd", "true", "false", "export", "set", "unset", "shasum", "md5", "sha256sum", "tee", "hash", "alias"}
_COPIERS = {"cp", "mv", "ln", "scp", "rsync", "ditto", "zip", "copy-item"}
_DESTINATION = {"cp", "mv", "ln", "scp", "rsync", "ditto", "copy-item"}
_CREDENTIAL_DIR = "a directory known to hold credentials"
_ROOT_REASON = "the repository root"
_BULK = _READERS | _GREPS | _SCRIPT_FIRST | _SHELLS | _INTERPRETERS
_INNER = _BULK - {".", "source"}
# Programs whose arguments are another command line; elsewhere a reader name only counts after run/exec/--. (The wrappers of
# lib/command_wrappers.py are not here: `_program` already returns the command they run.)
_FRONT_ENDS = {"docker", "podman", "nerdctl", "kubectl", "oc", "npx", "bunx", "pnpx", "uvx",
               "entr", "strace", "dtruss", "xcrun", "lima", "vagrant"}
_RUN_WORDS = {"run", "exec", "--"}
# The front ends whose command runs in a container or VM, not on this host's files (sg1 round 4: ELSEWHERE).
_ELSEWHERE = {"docker", "podman", "nerdctl", "kubectl", "oc", "lima", "vagrant"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_FD_EXEC = {"-x", "-X", "--exec", "--exec-batch"}
_CURL_FILES = {"-T", "--upload-file", "-K", "--config", "-E", "--cert", "--key", "--cacert", "--netrc-file",
               "--post-file", "--body-file", "--input-file", "--load-cookies", "--certificate", "--private-key",
               "--ca-certificate"}
_SSH_ARGS = ({"-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R",
              "-S", "-W", "-w"}, "bcDEeFIiJLlmOopQRSWw")
_TEXT_OPTS = {"-m", "--message", "-e", "--eval", "--grep", "--body", "--title", "--what", "--reason", "--note"}
_GIT_READ = {"show", "diff", "blame", "annotate", "cat-file", "difftool"}
_GIT_VALUES = {"-S", "-G", "--grep", "--author", "--since", "--until", "--format", "--pretty", "-L", "-n", "-U"}
# Subcommands that print file contents as patches: history only when asked, a diff unless told not to.
_GIT_HISTORY = {"log", "whatchanged", "reflog", "format-patch"}
_GIT_DIFFS = {"diff", "show", "difftool"}
_GIT_PATCH = {"-p", "-u", "--patch", "--patch-with-stat", "--patch-with-raw", "-c", "--cc", "-W",
              "--function-context", "--unified"}
_GIT_SUMMARY = {"--stat", "--shortstat", "--numstat", "--name-only", "--name-status", "--summary", "--dirstat",
                "--quiet", "-s", "--no-patch", "--raw", "--compact-summary", "--cumulative"}
_GIT_ARCHIVE_VALUES = {"--format", "--prefix", "-o", "--output", "--remote", "--exec", "--add-file",
                       "--add-virtual-file", "--mtime"}
_GIT_EXCLUDE = (":!", ":^", ":(exclude")
_UNSCOPED_REASON = "unscoped patch output may include credential files"
_STASH_REASON = "stash contents may include credential files"
# The names-only query that stands in for an unscoped patch. It runs git by argument list, never
# through a shell, with nothing the repository configures allowed to start a program: no pager,
# file-system monitor, hook, external diff, text conversion, signature check or object fetch.
_GIT_SAFE = ("--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=", "-c", "diff.external=",
             "-c", "core.hooksPath=/dev/null", "-c", "core.pager=cat", "-c", "log.showSignature=false")
# Each path is listed with its file modes, from the top of the work tree whatever `diff.relative`
# says, so a submodule entry (a gitlink) is told from a file without reading either.
_GIT_NAMES = ("--raw", "-z", "--no-renames", "--no-relative", "--no-ext-diff", "--no-textconv")
_GIT_QUERY = {"diff": ("diff",) + _GIT_NAMES,
              "log": ("log", "--format=", "--no-show-signature") + _GIT_NAMES,
              "stash": ("stash", "show") + _GIT_NAMES}
# The settings under which the real patch prints what no listing shows: files inside a submodule,
# or the output of a program the configuration names for all files or for a diff attribute.
_GIT_CONFIG = ("config", "-z", "--get-regexp", r"^diff\.(submodule|external|.+\.(textconv|command))$")
# Content filters run a program on every work-tree file git compares, so the listing must not run
# while one is configured, in any scope of configuration or through an include.
_GIT_FILTERS = ("config", "-z", "--get-regexp", r"^filter\..+\.(clean|smudge|process)$")
# Where git itself says the work tree is, with `core.worktree` and includes applied.
_GIT_TOPLEVEL = ("rev-parse", "--show-toplevel")
_GIT_ENV = {"LC_ALL": "C", "LANG": "C", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
# Variables that would point the query at a repository other than the working directory's, or give
# the real command settings and a diff program that the query, run without them, would not see.
_GIT_ELSEWHERE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                  "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                  "GIT_EXTERNAL_DIFF")
# The options a git grep may carry and still be judged by its tracked files (K1): they only choose how matches are
# found and printed. Everything else (reading a file, a content driver or a pager, widening the source) is outside.
_GIT_GREP_LONG_PLAIN = frozenset((
    "invert-match", "ignore-case", "word-regexp", "text", "recursive", "extended-regexp", "basic-regexp",
    "fixed-strings", "perl-regexp", "line-number", "column", "full-name", "files-with-matches", "name-only",
    "files-without-match", "null", "only-matching", "count", "color", "break", "heading", "context",
    "before-context", "after-context", "show-function", "function-context", "and", "or", "not", "quiet",
    "all-match", "max-depth", "max-count"))
# Every long option git grep knows (`git grep -h`); `no-` forms are added below. A prefix of one is an option too.
_GIT_GREP_LONG_ALL = frozenset(_GIT_GREP_LONG_PLAIN | {
    "cached", "no-index", "index", "untracked", "exclude-standard", "recurse-submodules", "textconv", "threads",
    "open-files-in-pager", "ext-grep"})
_GIT_GREP_NEGATED = frozenset("no-" + name for name in _GIT_GREP_LONG_ALL if name not in ("no-index", "and", "or", "not"))
_GIT_GREP_LONG_KNOWN = _GIT_GREP_LONG_ALL | _GIT_GREP_NEGATED
_GIT_GREP_FLAGS = "vi" "wa" "rEGFP" "InhHlLzocqpW"  # short options without a value
_GIT_GREP_VALUED = "ABCm"  # short options whose value is the rest of the word or the next word
# One `git ls-files --stage` entry: mode, object name and stage.
_STAGE_ENTRY = re.compile(r"^([0-7]{6}) [0-9a-f]+ ([0-3])$")
_REGULAR_MODES = ("100644", "100755")
_COMMITTED_SIZE = 1 << 20  # a credential-named tracked file larger than this is not hashed: the prompt stays
_COMMITTED_FILES = 64  # credential-named tracked files one root may have and still be checked against HEAD
_RAW_ENTRY = re.compile(r"^:+((?:[0-7]{6} )+)(?:[0-9a-f]+ )+[A-Z]+[0-9]*$")
_GITLINK = "160000"
_SUBMODULE_REASON = "patch output touches a submodule and may include the files inside it"
_SUBMODULE_CONFIG_REASON = "diff.submodule=diff prints the files inside submodules"
_FILTER_REASON = "a configured content filter would run a program to compare files"
_DRIVER_REASON = "a configured diff program or text conversion decides what the patch prints"
_GIT_ENV_REASON = ("{names} set in the environment can point git at another configuration file, so the patch cannot be "
                   "checked; run the same command without {them} (`env -u {first} git ...`) or give the setting "
                   "with `git -c key=value`")
_GIT_TIMEOUT = 2.0
_GIT_OUTPUT_LIMIT = 1 << 20
_GIT_PLAIN_GLOBALS = {"--no-pager", "-P"}
# Options that change how a patch is drawn, not which files it covers; the query leaves them out.
_QUERY_DROP = {"-p", "-u", "--patch", "--patch-with-stat", "--patch-with-raw", "-W", "--function-context",
               "--color", "--no-color", "--color-words", "--color-moved", "--word-diff", "-w", "-b",
               "--ignore-all-space", "--ignore-space-change", "--ignore-space-at-eol", "--ignore-blank-lines",
               "--ignore-cr-at-eol", "--minimal", "--patience", "--histogram", "--stat", "--shortstat",
               "--numstat", "--summary", "--compact-summary", "--oneline", "--decorate", "--no-decorate",
               "--graph", "--abbrev-commit", "--no-abbrev-commit", "--full-index", "--binary", "-a", "--text",
               "--no-prefix", "--no-ext-diff", "--no-textconv", "--exit-code", "--no-renames", "-M",
               "--find-renames", "--pretty"}
_QUERY_DROP_VALUED = {"--color", "--word-diff", "--word-diff-regex", "--color-moved", "--unified", "--format",
                      "--pretty", "--date", "--abbrev", "--diff-algorithm", "--inter-hunk-context", "--stat",
                      "--decorate", "--src-prefix", "--dst-prefix", "--diff-filter", "--find-renames"}
# Options that choose what the patch covers; the query repeats them.
_QUERY_KEEP = {"diff": {"--cached", "--staged", "--merge-base"},
               "log": {"--no-merges", "--reverse", "--all"},
               "stash": {"-u", "--include-untracked", "--only-untracked", "--no-include-untracked"}}
_QUERY_LOG_VALUED = {"--max-count", "--skip", "--since", "--until", "--after", "--before", "--author", "--grep"}
_QUERY_COUNT = re.compile(r"^-n?\d+$")
# A revision the shell passes through as written: no option, expansion, glob or `rev:path`.
_QUERY_REVISION = re.compile(r"^[\w@](?:[\w./@^~-]|\{[\w.-]*\})*$")
# Commands that leave the index, the work tree and the refs as a later query will find them.
_QUIET = {"cd", "pushd", "popd", "pwd", "true", "echo", "printf"}
_GIT_QUIET = {"status", "diff", "log", "show", "rev-parse", "ls-files"}
_NULL_SINK = re.compile(r"^(?:/dev/null|\d+|-)$")
# Search programs whose positive file filters are understood: (type and glob options) or include options.
_TYPE_FILTERS = {"rg", "rga"}
_INCLUDE_FILTERS = {"grep", "egrep", "fgrep", "ugrep", "ug"}
_PRINTERS = {"echo", "printf"}
_READ_VALUES = "dnNptuik"
_PROCESS_SUB = re.compile(r"<\s*<\(")
_INPUT_SUB = re.compile(r"<\(")  # any process substitution read as a file
_SOURCE_SPLIT = re.compile(r"[\s;|&()<>`\"']+")  # blanks and shell operators inside one word of shell source
# (options taking a value, short letters taking a value, options naming the pattern, their short letters)
_GREP_ARGS = ({"-A", "-B", "-C", "-m", "-d", "-D", "--include", "--exclude", "--exclude-dir", "--exclude-from",
               "--max-count", "--after-context", "--before-context", "--context", "--label", "--binary-files",
               "--devices", "--directories"}, "ABCmdD", {"-e", "--regexp", "-f", "--file"}, "ef")
_RG_ARGS = ({"-A", "-B", "-C", "-g", "-j", "-m", "-M", "-r", "-t", "-T", "-E", "--after-context", "--before-context",
             "--context", "--color", "--colors", "--context-separator", "--dfa-size-limit", "--encoding", "--engine",
             "--field-context-separator", "--field-match-separator", "--glob", "--iglob", "--ignore-file",
             "--max-columns", "--max-count", "--max-depth", "--max-filesize", "--path-separator", "--pre",
             "--pre-glob", "--regex-size-limit", "--replace", "--sort", "--sortr", "--threads", "--type",
             "--type-add", "--type-clear", "--type-not", "--hyperlink-format", "--depth", "--ignore", "--ignore-dir"},
            "ABCgjmMrtTE", {"-e", "--regexp", "-f", "--file"}, "ef")
_GIT_GREP_ARGS = ({"-A", "-B", "-C", "-O", "-m", "--max-depth", "--max-count", "--threads", "--open-files-in-pager",
                   "--context", "--before-context", "--after-context"},
                  "ABCOm", {"-e", "-f"}, "ef")
_SCRIPT_ARGS = ({"-v", "-F", "--arg", "--argjson", "--slurpfile", "--rawfile", "--indent", "-L", "-i"}, "vFL",
                {"-e", "--expression", "-f", "--file", "--from-file"}, "ef")
_NAME_REASON = "sensitive path in shell command"
_BULK_REASON = "dynamic file selection may include credential files"
_GLOB_REASON = "a glob that can match credential files"
_GLOB_PROMPTS = {_CREDENTIAL_DIR, "a credential-bearing path", _GLOB_REASON, "a home directory"}

# Names that are weak by pattern but are credential files by name. An owner's trust statement never lifts
# them (T7 3.3): the file names the redaction hook also treats as credential files, the exact names in
# _FILE_SAMPLES, `*.env`, and (as directories only, see _lift_name) `secrets` and `.secrets`.
_CREDENTIAL_FILE = re.compile(r"^(?:credentials(?:\.json)?|\.credentials\.json|\.git-credentials|"
                              r"secrets?\.(?:json|ya?ml|toml)|client_secret.*\.json|.*\.tfvars|.*\.env)$", re.I)
_SECRET_DIRS = {"secrets", ".secrets"}
# A walk of a directory the owner has vouched for: what it may spend, and which segments it never lifts.
WALK_ENTRIES = 20000
WALK_SECONDS = 1.0
_NEVER_LIFTED_SEGMENTS = (".git", ".config")


class Walk(collections.namedtuple("Walk", "follow exclude skip_git root ignored_env", defaults=(False, True, False))):
    """What a read of a directory visits, as far as the command line says.

    `follow`: it follows links it meets. `exclude`: directory names it skips (a literal `--exclude-dir`).
    `skip_git`: it never reads the `.git` of the tree it starts in (rg without a hidden flag or config).
    `root`: the owner's trust may lift the repository root for it (false for the Grep tool, which judges the
    root by its filter). `ignored_env`: a plain rg, which honours ignore files, so an `.env` that git
    confirms is ignored is not read by it. The default visits everything.
    """
    __slots__ = ()


# The working directory of the shell or tool being judged, when the caller knows it.
# `unknown` is set when the caller named a directory that cannot be placed.
# `trust` is the caller's resolver, or None. `walk` says what a read of a directory visits (see `Walk`).
# `memo` holds walks already done in this call.
WALK_CALL_SECONDS = 2.0     # all the walks one call may make, so the caller's own time limit holds
_SHELL = {"cwd": None, "unknown": False, "trust": None, "walk": Walk(True, ()), "memo": {}, "until": None, "text": ""}


def _shell_cwd():
    if _SHELL["unknown"]:
        return None
    if _SHELL["cwd"]:
        return _SHELL["cwd"]
    try:
        return os.getcwd()
    except OSError:
        return None


def _home():
    try:
        return str(Path.home())
    except (RuntimeError, OSError):
        return "/nonexistent-home"


def _plain_name(name):
    return _BACKUP.sub("", name) or name


def _parts_hit(parts):
    parts = [_plain_name(part.lower()) for part in parts]
    return (any(part in SENSITIVE_DIR for part in parts) or
            any(pair in SENSITIVE_PAIRS for pair in zip(parts, parts[1:])))


def _named(parts, lift=None):
    """Whether a path's parts name a credential location or file. `lift(index)` is asked about a part that
    is only a weak name; it answers True when the owner's trust covers that part, which then counts as clear."""
    if _parts_hit(parts):
        return True
    for index, part in enumerate(parts):
        if part in _NEVER_LIFTED_SEGMENTS:
            return True
        if SENSITIVE_NAME.match(_plain_name(part)) and not (lift and lift(index)):
            return True
    return False


class _Refused(str):
    """A root reason that stays equal to the plain reason, with what the owner's trust walk found."""

    def __new__(cls, text, detail):
        self = super().__new__(cls, text)
        self.detail = detail
        return self


# Why a shell call's ask is not a search or read (sg1 round 4). A worker's Bash ask is refused unless the classifier gives one of
# these: the list is closed, and each is set only at the site that decides it, a `_because(CAUSE, hit)` call (the sites are
# enumerated from this source by tests/test_sensitive_read.py test_non_search_sites). A hit any other path makes is a plain
# tuple, and a plain tuple refuses: a new path with no cause here refuses by default.
RUNS_CODE = "runs code"                      # an interpreter or shell given a script file or code, `source`/`.`
# (RUNS_CODE: only from _runs_code, for a script file _code_run's positive parse shows, not itself sensitive, given the
# file. Inline code never has it, sg1 round 7. Each site that gives it says SCRIPT FILE, _command_hit.)
ELSEWHERE = "runs on a remote host or in a container"           # ssh, docker/kubectl/... and what they run
TRANSFORM = "encodes or transforms a file"   # base64, xxd, gzip, openssl, ... on a file operand or `<` input
GIT_OUTPUT = "git output, not a search"     # git diff/show/log/blame/cat-file/archive/stash: history and patches
NAMES_ONLY = "names or changes files without reading them"      # ls, stat, wc, rm, echo, ... given a sensitive name
NON_SEARCH_CAUSES = (RUNS_CODE, ELSEWHERE, TRANSFORM, GIT_OUTPUT, NAMES_ONLY)
# xargs, find -exec, fd -x, a loop or a substituted listing handing a program file names: the program's own cause, judged by
# _program_cause as if it were given a sensitive file; none when it searches or reads them, or does nothing the list names.
# Encoders, compressors and crypto tools: what they print is not the file's text (round 1 kept base64's ask).
_TRANSFORMS = {"base64", "base32", "xxd", "hexdump", "od", "gzip", "bzip2", "xz", "openssl", "gpg"}


class _NonSearch(tuple):
    """A hit (target, reason) with the non-search cause (one of NON_SEARCH_CAUSES) that keeps a worker's ask an ask."""

    def __new__(cls, hit, cause):
        self = super().__new__(cls, hit)
        self.cause = cause
        return self


def _because(cause, hit):
    """`hit` with non-search cause `cause`; a falsy hit as it is, and with no cause (None) a plain hit, which refuses."""
    if not hit:
        return hit
    return _NonSearch(hit, cause) if cause else tuple(hit)


def _cause(hit):
    return getattr(hit, "cause", None)


# The shell reads the safe tools replace (safe_read); with the grep family and `git grep` (safe_grep) they are the shell
# searches and reads of _search_read. Every other reader (an editor, a copier, base64, ...) does something the safe tools do not.
_ROUTE_READERS = {"cat", "tac", "nl", "head", "tail", "less", "more", "bat", "batcat"}


def _sed_prints(args):
    """Whether `sed ARGS` only prints (`-n`, `--quiet`, `--silent`) and edits no file in place (`-i`, `--in-place`)."""
    quiet = False
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            if arg.startswith("--in-place"):
                return False
            quiet = quiet or arg in ("--quiet", "--silent")
        elif arg.startswith("-") and len(arg) > 1:
            letters = re.split(r"[efl]", arg[1:], maxsplit=1)[0]  # -e, -f and -l take the rest of the word as their value
            if "i" in letters:
                return False  # -i takes the rest of the word as its suffix: it writes the file
            quiet = quiet or "n" in letters
    return quiet


def _search_read(prog, args):
    """Whether program `prog` with `args` is a shell search or read: the safe calls _route_parts names, and a program
    `_program_cause` never gives a non-search cause."""
    return (prog in _GREPS or prog in _ROUTE_READERS or (prog in ("sed", "gsed") and _sed_prints(args)) or
            (prog == "git" and _git_sub(args, "")[0] == "grep"))


def _said(why):
    """The reason as shown in a prompt: with what a trust walk found, when it found something."""
    detail = getattr(why, "detail", None)
    return f"{why} ({detail})" if detail else str(why)


def _weak_only(name):
    """A name that is only weak: not a strong name or key suffix, and not a known credential file name."""
    return bool(_WEAK_NAME.match(name)) and not _STRONG_OR_KEY.match(name) and not _CREDENTIAL_FILE.match(name)


def _tree_for(path):
    """The trusted tree that holds this absolute path, or None: no resolver, no record, or not inside it."""
    trust = _SHELL["trust"]
    if trust is None or not path:
        return None
    try:
        return trust.tree(path)
    except Exception:       # a resolver that fails lifts nothing
        return None


def _lift_name(absolute, name, rule="L1"):
    """Whether a weak-name guess at this absolute path is lifted by the owner's trust (T7 L1; L4 for a name in a git listing).

    Only a weak name that is not a known credential file name, at a path that really lies in the trusted
    tree, with no `.git`, `.config` or credential directory on the way. The name is a literal one: a pattern
    could stand for any name. `absolute` is the path as written (`_absolute(..., raw=True)`), and where it
    leads is what `_opens` says: a path that leaves the tree through a link and ends outside is not covered,
    and the name that counts is the one it ends at as well as the one written.
    """
    if absolute is None or _SHELL["trust"] is None or _GLOB.search(name):
        return False
    plain = _plain_name(name)
    if not _weak_only(plain):
        return False
    real = _opens(absolute)
    tree = _tree_for(real)
    if tree is None:
        return False
    ends = _plain_name(posixpath.basename(real))
    if SENSITIVE_NAME.match(ends) and not _weak_only(ends):
        return False        # the path ends at a strong or exact credential name, whatever it was called on the way
    below = [part for part in real[len(tree.top):].split("/") if part]
    if any(part.lower() in _NEVER_LIFTED_SEGMENTS for part in below) or _parts_hit(below):
        return False
    if {plain.lower(), ends.lower()} & _SECRET_DIRS and (not os.path.exists(real) or os.path.isdir(real)):
        return False        # a directory named for its secrets, or one that is not there to be told apart
    _SHELL["trust"].note(rule, real, "credential-bearing path")
    return True


def _expand(path):
    """Leading `~`, `$HOME` and scratch variables as directories; any other variable stays unresolved."""
    for prefix in ("${HOME}", "$HOME", "~"):
        if path == prefix or path.startswith(prefix + "/"):
            return _home() + path[len(prefix):]
    for name in _SCRATCH_VARS:
        for prefix in ("${" + name + "}", "$" + name):
            if path == prefix or path.startswith(prefix + "/"):
                return "/tmp" + path[len(prefix):]
    return path


def _absolute(path, raw=False):
    """The path as the shell would resolve it; None when that depends on run-time state.

    `raw` leaves the text as written (`link/..` is not folded away), for `_opens`, which asks the file system."""
    path = _expand(path)
    if "$" in path or "`" in path or path.startswith("~"):
        return None
    if not path.startswith("/"):
        cwd = _shell_cwd()
        if cwd is None:
            return None
        path = posixpath.join(cwd, path)
    return path if raw else "/" + posixpath.normpath(path).lstrip("/")


def _opens(written):
    """The real path the kernel opens for an absolute path as written: each link followed where it is met and
    each `..` applied to what is left after the links before it, so `link/..` is the parent of the link's target.
    Every trusted lift (T7 L1, L2) asks where a path really leads through this one function."""
    return os.path.realpath(written)


def _pair_tail(path, weak):
    """A relative path that is credential-bearing if the unknown working directory is its usual parent."""
    if path.startswith(("/", "~", "$", "`")):
        return False
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts:
        return False
    name = _plain_name(parts[0].lower())
    if len(parts) > 1:
        return name == "sessions" or name in SENSITIVE_CONFIG_DIR
    return name in ("local.json", "sessions") or (weak and name == "config")


def _sensitive(value, weak=True):
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip().strip("'\"")
    path = Path(value)
    if _parts_hit(path.parts):
        return True
    if (SENSITIVE_NAME if weak else STRONG_NAME).match(_plain_name(path.name)) and not (
            weak and _lift_name(_absolute(value, True), path.name)):
        return True
    absolute = _absolute(value)
    if absolute is None:
        return _SHELL["unknown"] and _pair_tail(_expand(value), weak)
    if _parts_hit(absolute.split("/")):
        return True
    try:
        resolved = Path(absolute).resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        # A file that is not there is still judged by where its directory really is.
        try:
            parent = os.path.realpath(posixpath.dirname(absolute))
        except (OSError, ValueError):
            return False
        return _parts_hit(parent.split("/") + [posixpath.basename(absolute)])
    if str(resolved) == absolute:
        return False
    if _parts_hit(resolved.parts):
        return True
    return (SENSITIVE_NAME if weak else _STRONG_OR_KEY).match(_plain_name(resolved.name)) is not None and not (
        weak and _lift_name(_absolute(value, True), resolved.name))


def sensitive_path(value):
    return _sensitive(value)


def _join(base, path):
    for prefix in ("${PWD}", "$PWD"):
        if path == prefix or path.startswith(prefix + "/"):
            path = "." + path[len(prefix):]
    if not base or path.startswith(("/", "~", "$")):
        return path
    return posixpath.join(base, path)


def _cd(base, target, physical=False):
    joined = _join(base, target)
    if "$" in joined or "`" in joined or joined.startswith("~"):
        return joined
    if physical:
        # `cd -P link/..` lands in the parent of the link's target, not in the folded text.
        cwd = joined if joined.startswith("/") else _shell_cwd()
        if cwd is not None:
            return _opens(posixpath.join(cwd, joined))
    joined = posixpath.normpath(joined)
    return "" if joined == "." else joined


def _within(inner, outer):
    return inner == outer or inner.startswith(outer.rstrip("/") + "/")


def _temp_roots():
    temp = tempfile.gettempdir().rstrip("/")
    return {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", temp, os.path.realpath(temp)}


def _top(path):
    """The nearest directory at or above the path that is a git work tree root."""
    for _ in range(64):
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def _brace_expand(word, limit=64):
    """The alternatives a brace list stands for; a list too long to enumerate is judged as a glob."""
    out = [word]
    while True:
        grown, changed = [], False
        for item in out:
            match = _BRACE_LIST.search(item)
            if not match:
                grown.append(item)
                continue
            changed = True
            grown.extend(item[:match.start()] + choice + item[match.end():] for choice in match.group(1).split(","))
        if len(grown) > limit:
            return [_BRACES.sub("*", word)]
        out = grown
        if not changed:
            return out


def _glob_hits(pattern, samples=_SAMPLES):
    return any(fnmatch.fnmatchcase(sample, pattern.lower()) for sample in samples)


def _part_lift(anchor, parts):
    """The lift `_named` asks about a weak part of a path below `anchor`; None when nothing can be lifted."""
    if anchor is None or _SHELL["trust"] is None:
        return None

    def lift(index):
        if any(_GLOB.search(part) for part in parts[:index + 1]):
            return False
        return _lift_name(posixpath.join(anchor, *parts[:index + 1]), parts[index])
    return lift


def _walk_hit(parent, name, is_dir):
    """Whether an entry of a directory is a credential location or file by its name and its folder's."""
    if name.lower() in _NEVER_LIFTED_SEGMENTS or _parts_hit([parent, name]):
        return True
    plain = _plain_name(name)
    if not SENSITIVE_NAME.match(plain):
        return False
    return not _weak_only(plain) or (plain.lower() in _SECRET_DIRS and is_dir)


def _shown(path):
    return path if len(path) <= 200 and _PATHISH.match(path) else "a path"


def _walk(root, tree, walk):
    """None when nothing a read of this directory would visit is a credential location, else why not.

    Reads names only, never content, and does not follow links found on the way unless the command
    does. A link that leads out of the tree (or into another repository) when followed is a hit, and so
    is a nested `.git`; the `.git` of the tree itself is passed over only for a walk that never reads it
    (`Walk.skip_git`). A tree over the entry or time allowance is not verified.
    """
    follow, exclude = walk.follow, walk.exclude
    deadline = time.monotonic() + WALK_SECONDS
    if _SHELL["until"] is not None:
        deadline = min(deadline, _SHELL["until"])
    top = os.path.realpath(root)
    label = posixpath.basename(top)
    try:
        info = os.lstat(top)
    except OSError:
        return "it could not be read"
    if not stat.S_ISDIR(info.st_mode):
        return None if stat.S_ISREG(info.st_mode) else "it is not a directory"
    todo, seen, count = [(top, label, label)], {(info.st_dev, info.st_ino)}, 0
    while todo:
        folder, parent, shown = todo.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    count += 1
                    if count > WALK_ENTRIES or time.monotonic() > deadline:
                        return "tree too large to verify"
                    name, here = entry.name, shown + "/" + entry.name
                    link = entry.is_symlink()
                    target = os.path.realpath(entry.path) if link else entry.path
                    is_dir = os.path.isdir(target) if link else entry.is_dir(follow_symlinks=False)
                    nested = name == ".git" and folder != tree.top  # another repository: never passed over
                    if is_dir and not nested and any(fnmatch.fnmatchcase(name, skip) for skip in exclude):
                        continue
                    if walk.skip_git and name == ".git" and folder == tree.top:
                        continue
                    if _walk_hit(parent, name, is_dir) and not (
                            walk.ignored_env and name == ".env" and not is_dir and not link and
                            not _parts_hit([parent, name]) and _env_ignored(folder, deadline)):
                        return "reaches " + _shown(here)
                    if link:
                        if not follow:
                            continue
                        there = _tree_for(target)
                        if there is None or there.top != tree.top:
                            return "follows a link out of the tree at " + _shown(here)
                        if _walk_hit(posixpath.basename(posixpath.dirname(target)), posixpath.basename(target), is_dir):
                            return "reaches " + _shown(here) + " through a link"
                    if not is_dir:
                        continue
                    seen_key = os.stat(target)
                    if (seen_key.st_dev, seen_key.st_ino) not in seen:
                        seen.add((seen_key.st_dev, seen_key.st_ino))
                        todo.append((target, name, here))
        except OSError:
            return "a folder in it could not be read"
    return None


def _env_ignored(folder, deadline):
    """Whether git confirms the `.env` of this folder is ignored, so a plain rg passes over it (F2).

    `git check-ignore -q` ends with 0 only for an ignored path; a tracked file, an unknown path and a folder
    that is no repository end otherwise and are not confirmed. A `.ignore` or `.rgignore` in the folder or
    above it can bring the file back for rg, and then nothing is confirmed.
    """
    parents, up = [folder], folder
    while up != posixpath.dirname(up):
        up = posixpath.dirname(up)
        parents.append(up)
    if any(os.path.lexists(posixpath.join(parent, name)) for parent in parents for name in (".ignore", ".rgignore")):
        return False
    return _git_ask(("check-ignore", "-q", "--", ".env"), folder, min(deadline, time.monotonic() + _GIT_TIMEOUT)) is not None


def _walked(path, tree, walk):
    """`_walk` of this directory, done once per call for the same place and the same way of reading it."""
    key = (os.path.realpath(path), walk.follow, walk.exclude, walk.skip_git, walk.ignored_env)
    if key not in _SHELL["memo"]:
        _SHELL["memo"][key] = _walk(path, tree, walk)
    return _SHELL["memo"][key]


def _walk_lift(rule, path, reason, pathspec):
    """The trusted lift of a bulk-read guess at this directory: None when lifted, else the reason, with what the walk found.

    Lifted when the directory lies in the trusted tree and a walk of what a read of it would visit finds no
    credential location. A git pathspec is never walked: a patch also covers history (and is judged by its own
    listing instead, see `_patch_listing`), and the files a git search reads are not the files on disk.
    """
    walk = _SHELL["walk"]
    if _SHELL["trust"] is None or pathspec:
        return reason
    tree = _tree_for(path)
    if tree is None:
        return reason
    refusal = _walked(path, tree, walk)
    if refusal:
        return _Refused(reason, refusal)
    _SHELL["trust"].note(rule, os.path.realpath(path), reason)
    return None


def _credential_dir(parts, anchor, pathspec):
    """The reason a directory named `agent`, `claude` or `config` is a bulk root, unless the owner's trust lifts it (T7 L2)."""
    if anchor is None:
        return _CREDENTIAL_DIR
    return _walk_lift("L2", posixpath.join(anchor, *parts), _CREDENTIAL_DIR, pathspec)


def _repository_root(anchor, pathspec, lift):
    """The reason the root of the repository is a bulk root, unless the owner's trust lifts it (T7 L3).

    Lifted when the root is the top of the trusted tree (or of a linked worktree of it), the written path
    opens there (`lift`), and the same walk a read of it would make finds no credential location. A search of
    the whole tree is not cleared by the name alone: `rg foo .` has to pass the walk, `grep -r foo .` reaches
    `.git/config` in it, and an `.env` anywhere under the root is named in the prompt.
    """
    if not lift or not _SHELL["walk"].root:
        return _ROOT_REASON
    return _walk_lift("L3", anchor, _ROOT_REASON, pathspec)


def _layout(parts, tree=True, anchor=None, pathspec=False, lift=True):
    """Judge a path relative to a project root by this repository's layout.

    `lift` is false when the owner's trust may not be applied to these parts (see `_root_reason`)."""
    if _named(parts, _part_lift(anchor if lift else None, parts)):
        return "a credential-bearing path"
    globs = [index for index, part in enumerate(parts) if _GLOB.search(part)]
    if not globs:
        return _credential_dir(parts, anchor if lift else None, pathspec) if parts[-1].lower() in CREDENTIAL_DIRS else None
    prefix = parts[:globs[0]]
    if (prefix and prefix[-1].lower() not in CREDENTIAL_DIRS) or not any(_glob_hits(parts[i]) for i in globs):
        return None
    # A glob at the project root is cleared when nothing it matches there is a credential path. Globs
    # inside a credential directory, dot-file globs and git pathspecs (which cross directories) are not.
    if prefix or pathspec or anchor is None or parts[0].startswith("."):
        return _GLOB_REASON
    try:
        names = os.listdir(anchor)
    except OSError:
        return _GLOB_REASON
    for name in names:
        if name.startswith(".") or not fnmatch.fnmatchcase(name, parts[0]):
            continue
        if parts[1:] or tree:
            if _layout([name] + parts[1:], tree, None, True):
                return _GLOB_REASON
        elif _named([name]):
            return _GLOB_REASON
    return None


def _root_reason(root, base="", tree=True, pathspec=False):
    """Why a bulk read rooted here may reach credentials; None when the target can be shown benign.

    `tree` is false for a glob given to a command that reads the matched files without descending.
    """
    choices = _brace_expand(root)
    if len(choices) > 1:
        return _first(_root_reason(choice, base, tree, pathspec) for choice in choices)
    joined = _expand(_join(base, choices[0]))
    full = _absolute(joined)
    if full is None:
        if "$" in joined or "`" in joined or joined.startswith("~"):
            return "a directory only known at run time"
        parts = [part for part in joined.split("/") if part not in ("", ".")]
        if ".." in parts:
            return "a parent directory"
        return _layout(parts, tree, None, pathspec) if parts else "the working directory"
    parts = [part for part in full.split("/") if part]
    cut = next((index for index, part in enumerate(parts) if _GLOB.search(part)), len(parts))
    literal = os.path.realpath("/" + "/".join(parts[:cut]))
    parts = [part for part in literal.split("/") if part] + parts[cut:]
    if _within(os.path.realpath(_home()), literal):
        return "a home directory"
    cwd = _shell_cwd()
    cwd = os.path.realpath(cwd) if cwd else None
    top, cwd_top = _top(literal), _top(cwd) if cwd else None
    if top and cwd and (top == cwd_top or _within(top, cwd)):
        anchor = top
    elif cwd and literal != cwd and _within(cwd, literal):
        return "a directory containing the working directory"
    elif (_SHELL["cwd"] or joined.startswith("/")) and any(_within(literal, temp) for temp in _temp_roots()):
        # Scratch space, when the path was written out or the caller vouched for the working directory.
        return "a credential-bearing path" if _named(parts) else None
    elif cwd and not top and not cwd_top and _within(literal, cwd):
        anchor = cwd
    else:
        return "a directory outside the working tree"
    parts = parts[len([part for part in anchor.split("/") if part]):]
    same = _same_place(joined, literal)
    if not parts:
        return _repository_root(anchor, pathspec, same)
    return _layout(parts, tree, anchor, pathspec, same)


def _same_place(joined, literal):
    """Whether the literal part of a root, as written, opens where the judged (folded) path says it does.

    `_root_reason` judges a path with `link/..` folded away, as the shell's text would read; the kernel opens
    it somewhere else when `link` leads out of that directory. The owner's trust is applied only where the
    two agree, so a root that really ends elsewhere is judged, and prompts, as it does without trust."""
    if _SHELL["trust"] is None:
        return False
    written = _absolute(joined, True)
    if written is None:
        return False
    pieces = [part for part in written.split("/") if part]
    stop = next((index for index, part in enumerate(pieces) if _GLOB.search(part)), len(pieces))
    return _opens("/" + "/".join(pieces[:stop])) == literal


def _heredoc_body(text, i, delimiter, dash):
    """The here-document body starting at i and the index after its closing line."""
    start = i
    while i < len(text):
        end = text.find("\n", i)
        line = text[i:] if end < 0 else text[i:end]
        if (line.lstrip("\t") if dash else line) == delimiter:
            return text[start:i], len(text) if end < 0 else end + 1
        if end < 0:
            break
        i = end + 1
    return text[start:], len(text)


def _close_paren(text, i):
    """Index after the `)` that closes a substitution whose body starts at i; the end when unbalanced."""
    depth, size, pending = 1, len(text), []
    while i < size:
        char = text[i]
        if char == "\\":
            i += 2
        elif char == "'":
            end = text.find("'", i + 1)
            i = size if end < 0 else end + 1
        elif char == '"':
            i += 1
            while i < size and text[i] != '"':
                if text[i] == "\\":
                    i += 2
                elif text.startswith("$(", i):
                    i = _close_paren(text, i + 2)
                else:
                    i += 1
            i += 1
        elif char == "`":
            end = text.find("`", i + 1)
            i = size if end < 0 else end + 1
        elif char == "(":
            depth, i = depth + 1, i + 1
        elif char == ")":
            depth, i = depth - 1, i + 1
            if not depth:
                return i
        elif char == "\n" and pending:
            i += 1
            for here in pending:
                i = _heredoc_body(text, i, _delimiter(here), bool(here.group(1)))[1]
            pending = []
        else:
            here = _HEREDOC.match(text, i) if char == "<" and not text.startswith("<<<", i) else None
            if here:
                pending.append(here)
            i = here.end() if here else i + 1
    return size


def _delimiter(here):
    return next(group for group in here.groups()[1:] if group is not None)


def _substitution(text, i):
    """The command inside the `$(…)` or backtick substitution that starts at i, and the index after it."""
    if text[i] == "`":
        end = text.find("`", i + 1)
        end = len(text) if end < 0 else end
        return text[i + 1:end], min(end + 1, len(text))
    end = _close_paren(text, i + 2)
    return (text[i + 2:end - 1] if text[end - 1:end] == ")" and end > i + 2 else text[i + 2:end]), end


def _substitutions(text):
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
        elif text.startswith("$(", i) or text[i] == "`":
            inner, i = _substitution(text, i)
            out.append(inner)
        else:
            i += 1
    return out


def _ansi(text):
    def decode(match):
        code = match.group(1)
        if code[0] in "xu" and len(code) > 1:
            return chr(int(code[1:], 16))
        if code[0] in "01234567":
            return chr(int(code, 8))
        return {"n": "\n", "t": "\t", "r": "\r", "e": "\x1b"}.get(code, code)
    return _ANSI.sub(decode, text)


def _tokens(command):
    """Words with quotes removed (each with the substitutions it holds), operators and here-documents.

    A file-descriptor number written against a redirect, as in `2>/dev/null`, is not a word.
    """
    out, word, subs, plain, pending = [], None, [], True, []
    i, size = 0, len(command)
    while i < size:
        char = command[i]
        if char == "\\":
            if command[i + 1:i + 2] != "\n":
                word, plain = (word or "") + command[i + 1:i + 2], False
            i += 2
        elif char == "'" or command.startswith("$'", i):
            ansi = char == "$"
            start = end = i + (2 if ansi else 1)
            while end < size and command[end] != "'":
                end += 2 if ansi and command[end] == "\\" else 1
            text = command[start:end]
            word, plain, i = (word or "") + (_ansi(text) if ansi else text), False, end + 1
        elif char == '"':
            word, plain, i = word or "", False, i + 1
            while i < size and command[i] != '"':
                if command.startswith("$(", i) or command[i] == "`":
                    inner, end = _substitution(command, i)
                    subs.append(inner)
                    word, i = word + command[i:end], end
                    continue
                if command[i] == "\\" and command[i + 1:i + 2] in ('"', "\\", "$", "`"):
                    i += 1
                word += command[i:i + 1]
                i += 1
            i += 1
        elif command.startswith("$(", i) or char == "`":
            inner, end = _substitution(command, i)
            subs.append(inner)
            word, plain, i = (word or "") + command[i:end], False, end
        elif char in " \t\n" or (char == "#" and word is None):
            if word is not None:
                out.append(("word", word, subs))
                word, subs, plain = None, [], True
            if char == "#":
                end = command.find("\n", i)
                i = size if end < 0 else end
            elif char == "\n":
                i += 1
                for delimiter, dash, holder in pending:
                    body, i = _heredoc_body(command, i, delimiter, dash)
                    holder.append(body)
                pending = []
                out.append(("op", ";"))
            else:
                i += 1
        else:
            here = _HEREDOC.match(command, i) if char == "<" and not command.startswith("<<<", i) else None
            op = None if here else next((op for op in _OPERATORS if command.startswith(op, i)), None)
            if not here and op is None:
                word = (word or "") + char
                i += 1
                continue
            if word is not None and not (plain and word.isdigit() and (here or op in _REDIRECTS)):
                out.append(("word", word, subs))
            word, subs, plain = None, [], True
            if here:
                holder = []
                quoted = here.group(2) is not None or here.group(3) is not None or "\\" in here.group(0)
                out.append(("heredoc", holder, not quoted))
                pending.append((_delimiter(here), bool(here.group(1)), holder))
                i = here.end()
            else:
                out.append(("op", op))
                i += len(op)
    if word is not None:
        out.append(("word", word, subs))
    return out


def _blank(piped, first):
    return {"words": [], "inputs": [], "strings": [], "heredocs": [], "subs": [], "outputs": [], "piped": piped,
            "first": first, "feeds": False}


def _commands(command):
    """Simple commands in order, with `(` and `)` between them where a subshell opens and closes.

    Each command carries its input and output redirects, here-strings, here-documents, the
    substitutions in its words, the first command of its pipeline, and whether its output feeds
    another command.
    """
    items, redirect, current = [], None, _blank(False, None)

    def close():
        if any(current[key] for key in ("words", "inputs", "strings", "heredocs", "subs", "outputs")):
            current["first"] = current["first"] or current
            items.append(current)
            return True
        return False

    for token in _tokens(command):
        kind, text = token[0], token[1]
        if kind == "heredoc":
            current["heredocs"].append(token[1:])
        elif kind == "op" and text in _REDIRECTS:
            redirect = text
        elif kind == "op":
            piped = close() and text in ("|", "|&")
            if piped:
                items[-1]["feeds"] = True
            current = _blank(piped, items[-1]["first"] if piped else None)
            if text in ("(", ")"):
                items.append(text)
            redirect = None
        else:
            current["subs"].extend(token[2])
            if redirect == "<":
                current["inputs"].append(text)
            elif redirect == "<<<":
                current["strings"].append(text)
            elif redirect in (">", ">>", "&>", ">&"):
                current["outputs"].append(text)
            elif not redirect:
                current["words"].append(text)
            redirect = None
    close()
    return items


class _Line(str):
    """A command line a wrapper hands to a shell (`su -c`, `flock -c`, an ssh remote command): its text is not the user's own
    `sh -c` string, so a command word it leaves to the shell's expansion is unknown, not ignored (_unresolved)."""


def _program(words):
    """The program a simple command runs, and its arguments, looking through the wrappers of lib/command_wrappers.py."""
    i = 0
    while i < len(words):
        word = words[i]
        name = posixpath.basename(word).lower()
        if _ASSIGN.match(word) or word in _KEYWORDS:
            i += 1
        elif name in _UNWRAPPED:
            found = command_wrappers.unwrap(name, words[i + 1:])
            if found is None:
                return None, []  # nothing follows the wrapper to run
            if found is command_wrappers.UNKNOWN:
                return _OPAQUE, words[i + 1:]  # where its command starts is not known
            if found[0] == "line":
                return "sh", ["-c", _Line(found[1])]  # `su -c 'cmd'`, `script -c 'cmd'`, `flock FILE -c 'cmd'`: a shell reads it
            words, i = found[1], 0
        else:
            return _ALIASES.get(name, name), words[i + 1:]
    return None, []


def _split_args(args, value_opts=(), value_letters="", pattern_opts=(), pattern_letters=""):
    """Options, operands before and after `--`, option values, and whether an option supplied the pattern."""
    opts, before, after, values, has_pattern, i = [], [], None, [], False, 0
    while i < len(args):
        arg = args[i]
        i += 1
        if after is not None:
            after.append(arg)
        elif arg == "--":
            after = []
        elif arg.startswith("-") and len(arg) > 1:
            opts.append(arg)
            name, equals, value = arg.partition("=")
            short = not arg.startswith("--")
            pattern = name in pattern_opts or (short and not equals and arg[-1] in pattern_letters)
            takes = name in value_opts or (short and not equals and arg[-1] in value_letters)
            has_pattern = has_pattern or pattern
            if equals:
                if not pattern:
                    values.append(value)
            elif pattern:
                i += 1
            elif takes and i < len(args):
                values.append(args[i])
                i += 1
        else:
            before.append(arg)
    return opts, before, after, values, has_pattern


def _read_hit(word, base, globs=True):
    """A file operand of a command that reads it: any sensitive name counts, and so does a risky glob."""
    if word == "{}":
        return None
    for choice in _brace_expand(word):
        for piece in _PIECES.split(choice):
            piece = piece.lstrip("@")
            if not piece or piece == "{}" or len(piece) > 4096 or "\n" in piece:
                continue
            if _sensitive(_join(base, piece)):
                return piece, _NAME_REASON
            if not globs or not _PATHISH.match(piece):
                continue
            why = None
            if _GLOB.search(piece):
                why = _root_reason(piece, base, False)
            elif "$" in piece and "/" in piece.split("$", 1)[0]:
                # A variable below a written-out directory selects among that directory's files.
                why = _root_reason(_VARIABLE.sub("*", piece), base, False)
            if why:
                return piece, f"glob over {_said(why)} may include credential files"
    return None


def _weak_file(piece, interpreter, base=None):
    """A path token, not a prose word, whose name says it stores credentials. With `base`, a name the
    owner's trust covers (T7 L1) is not one."""
    name = _plain_name(posixpath.basename(piece)).lower()
    if not _WEAK_NAME.match(name):
        return False
    stem, dot, extension = name.rpartition(".")
    if dot and stem:
        hit = bool(_DATA_EXT.match(extension)) and extension not in _CODE_EXT
    elif name.startswith("."):
        hit = True
    else:
        hit = name.endswith("credentials") and (interpreter or "/" in piece or name != "credentials")
    return hit and not (base is not None and _lift_name(_absolute(_join(base, piece), True), posixpath.basename(piece)))


def _other_hit(args, base, interpreter=False):
    """Arguments of a command not known to read its operands: credential names, key files, credential stores."""
    skip = False
    for arg in args:
        if skip or arg in _TEXT_OPTS:
            skip = not skip
            continue
        for piece in _PIECES.split(arg):
            piece = piece.lstrip("@")
            if not piece or not _PLAIN.match(piece):
                continue
            if (_SUFFIX_TOKEN.match(posixpath.basename(piece)) or _sensitive(_join(base, piece), False) or
                    _weak_file(piece, interpreter, base)):
                return piece, _NAME_REASON
    return None


def _literal_hit(code, base=""):
    """Quoted path-shaped literals inside inline code or a here-document."""
    for match in _LITERAL.finditer(code):
        text, before = match.group(2), code[max(0, match.start() - 32):match.start()]
        if _GLOB.search(text):
            # Only a glob that reaches a credential folder or name: a glob over other files, or one
            # whose root is unknown because the script builds it, is ordinary code.
            if "/" in text and _PATHISH.match(text) and _root_reason(text, base, False) in _GLOB_PROMPTS:
                return text, "glob in inline script may include credential files"
        elif _LITERAL_PATH.match(text):
            if ("/" in text or "." in text or _OPENER.search(before)) and _sensitive(_join(base, text)):
                return text, "sensitive path in inline script"
            if _WALKER.search(before) and _root_reason(text, base) in (_CREDENTIAL_DIR, "a credential-bearing path"):
                return text, "inline script reads " + _CREDENTIAL_DIR
    return None


def _first(hits):
    return next((hit for hit in hits if hit), None)


def _roots_hit(label, roots, base, reason, pathspec=False, tracked=False):
    """The first root a bulk read of these roots may reach credentials through. `tracked` is for a plain git grep
    that the shell itself runs: a credential directory or the repository root is cleared for it by the index (K1)."""
    for root in roots:
        why = _root_reason(root, base, True, pathspec)
        if why and tracked and why in (_CREDENTIAL_DIR, _ROOT_REASON) and _tracked_clear(root, base):
            continue
        if why:
            return f"{label} of {root}", reason.format(why=_said(why))
    return None


def _not_a_path(word, base):
    """A git operand that names nothing on disk is read as a revision."""
    if _GLOB.search(word) or word.startswith(":"):
        return False
    full = _absolute(_join(base, word))
    return full is not None and not os.path.lexists(full)


def _exists(word, base):
    """A git operand that names something on disk."""
    full = None if _GLOB.search(word) else _absolute(_join(base, word))
    return full is not None and os.path.lexists(full)


def _narrowed(prog, args, spec):
    """Whether positive type or glob filters limit a search to files that cannot be credential files."""
    typed = prog in _TYPE_FILTERS
    if not typed and prog not in _INCLUDE_FILTERS:
        return False
    kinds, globs, letters, i = [], [], spec[1] + spec[3], 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if not arg.startswith("-") or len(arg) < 2:
            continue
        if arg.startswith("--"):
            name, equals, value = arg.partition("=")
            if not equals and (name in spec[0] or name in spec[2]):
                value, i = "".join(args[i:i + 1]), i + 1
            if typed and name == "--type-add":
                return False  # a redefined type can select anything
            if typed and name == "--type":
                kinds.append(value)
            elif name in (("--glob", "--iglob") if typed else ("--include",)):
                globs.append(value)
            continue
        for at, letter in enumerate(arg[1:], 2):
            if letter in letters:
                value = arg[at:]
                if not value:
                    value, i = "".join(args[i:i + 1]), i + 1
                if typed and letter == "t":
                    kinds.append(value)
                elif typed and letter == "g":
                    globs.append(value)
                break
    globs = [glob for glob in globs if not glob.startswith("!")]  # an exclusion narrows, it selects nothing
    return (bool(kinds or globs) and all(kind.lower() in _CODE_TYPES for kind in kinds) and
            not any(_glob_can_match(glob) for glob in globs))


def _search_walk(prog, args, spec):
    """What a search visits below its roots, for a trust walk (see `Walk`).

    Only what the command line plainly says is believed: grep follows links found on the way only for -R,
    rg only for -L or --follow, and the other searches are taken to follow. A directory is skipped only for
    a literal `--exclude-dir`, which grep and ugrep apply to the names of the directories they meet.
    The `.git` of the tree is passed over only by rg, and only when nothing on the line (a hidden or unignored
    flag, a positive glob) or in its environment (a config file) could make it read there. Hidden entries
    below the root are walked all the same: the walk looks at what the tree holds, not at what rg would skip.
    """
    exclude, follow, letters, i = [], prog not in ("grep", "egrep", "fgrep", "zgrep", "rg", "rga"), spec[1] + spec[3], 0
    # A config file named anywhere on the line (inline, through env, or exported earlier) counts as one in force.
    unhidden = (prog in ("rg", "rga") and not os.environ.get("RIPGREP_CONFIG_PATH")
                and "RIPGREP_CONFIG_PATH" not in _SHELL["text"])
    # Whatever turns the ignore files off, adds one, or re-includes a path defeats the pass over an ignored `.env`.
    honours = unhidden and prog == "rg"
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        if not arg.startswith("-") or len(arg) < 2:
            continue
        name, equals, value = arg.partition("=")
        short = not arg.startswith("--")
        if not equals and (name in spec[0] or name in spec[2] or (short and arg[-1] in letters)):
            value, i = "".join(args[i:i + 1]), i + 1
        if name in ("--exclude-dir",) and value:
            exclude.append(value)
        elif short and re.match(r"^-[A-Za-z]*(?:L)", arg) and prog in ("rg", "rga"):
            follow = True
        elif short and re.match(r"^-[A-Za-z]*R", arg) and prog in ("grep", "egrep", "fgrep", "zgrep"):
            follow = True
        elif name in ("--follow", "--dereference-recursive"):
            follow = True
        if name in _HIDDEN_LONG or (short and _HIDDEN_SHORT.match(arg)):
            unhidden = False
        elif name in ("--glob", "--iglob") or (short and "g" in arg[1:]):
            # a positive glob can bring back what the search would pass over; an exclusion cannot
            if not (value.startswith("!") and (not short or arg[-1] == "g")):
                unhidden = honours = False
        if (name.startswith(("--no-ignore", "--ignore-file")) or name == "--unrestricted" or
                (short and re.match(r"^-[A-Za-z]*u", arg))):
            honours = False
    return Walk(follow, tuple(exclude), unhidden, ignored_env=honours)


def _hidden_roots_clear(roots, base):
    """Whether the owner's trust clears a hidden or unignored search of these roots (T7 L3).

    Such a search reads what the name-based rules cannot see, so it prompts unless every root is benign by
    its name, really lies inside the trusted tree, and the same walk as any other search of it (hidden
    entries and `.git` included) finds no credential location.
    """
    if _SHELL["trust"] is None:
        return False
    walk, places = _SHELL["walk"], []
    for root in roots:
        written = _absolute(_expand(_join(base, root)), True)
        if written is None or _GLOB.search(root) or _root_reason(root, base):
            return False
        real = _opens(written)
        tree = _tree_for(real)
        if tree is None or not _within(real, tree.top):
            return False
        below = [part for part in real[len(tree.top):].split("/") if part]
        if _named(below) or _walked(real, tree, walk):
            return False
        places.append(real)
    for real in places:
        _SHELL["trust"].note("L3", real, "broad hidden/unignored search")
    return bool(places)


def _search_hit(prog, args, cmd, base, bulk, git=False, listing=False, queried=False):
    """A search command, judged with the walk it implies (see `_search_walk`)."""
    spec = _GIT_GREP_ARGS if git else _RG_ARGS if prog in _ALWAYS_RECURSIVE else _GREP_ARGS
    saved = _SHELL["walk"]
    _SHELL["walk"] = _search_walk(prog, args, spec) if not git else Walk(True, ())
    try:
        return _search_scan(prog, args, cmd, base, bulk, git, listing, spec, queried)
    finally:
        _SHELL["walk"] = saved


def _plain_git_grep(opts, before, after):
    """Whether a git grep reads only the tracked files under its pathspecs in the work tree (K1).

    Only with options on an allowlist (`_GIT_GREP_LONG_PLAIN`, `_GIT_GREP_FLAGS`, `_GIT_GREP_VALUED` and `-e`),
    so an option that reads a file (`-f`), runs a content driver or a pager (`--textconv`, `-O`) or widens the
    source (`--untracked`, `--no-index`, ...), and any option git adds later, keeps the prompt. A long option
    git takes by prefix counts only when everything the prefix could mean is on the list. Not with a revision
    or tree operand: with `--`, every operand before it other than the pattern is one."""
    if after is not None and before:
        return False
    return all(_plain_git_grep_option(opt) for opt in opts)


def _plain_git_grep_option(opt):
    name = opt.partition("=")[0]
    if name.startswith("--"):
        if name == "--":
            return False
        word = name[2:]
        meant = [word] if word in _GIT_GREP_LONG_KNOWN else [known for known in _GIT_GREP_LONG_KNOWN
                                                             if known.startswith(word)]
        return bool(meant) and all(known in _GIT_GREP_LONG_PLAIN for known in meant)
    letters, at = opt[1:], 0
    if "=" in opt or not letters:
        return False
    if letters.isdigit():
        return True  # -NUM
    while at < len(letters):
        letter = letters[at]
        at += 1
        if letter in _GIT_GREP_FLAGS:
            continue
        if letter == "e":  # the pattern is the next word; one joined to the letter is not found by `_split_args`
            return at == len(letters)
        if letter in _GIT_GREP_VALUED:
            return letters[at:].isdigit() or at == len(letters)
        return False
    return True


def _tracked_clear(root, base):
    """Whether the files git tracks under this git grep root hold no credential-named path (K1).

    A plain git grep reads only tracked files, so a credential directory or the repository root whose tracked
    files all have plain names is not a bulk read of credentials. Asked once per root and call (the walk memo).
    Anything that cannot be shown keeps the prompt: a root git would read as pathspec magic, a glob or anything
    the shell expands; a query that fails, times out or prints more than allowed; git's top level not being the
    work tree the root was placed in; more entries than a walk may count; a submodule entry (`submodule.recurse`
    makes git grep read inside it); or a folder on the way to a tracked file that is a link on disk, which git
    grep follows (it does not follow a tracked link itself, nor a file replaced by one).
    """
    if not root or root.startswith((":", "-")) or re.search(r"[*?\[\]\\{}$`~'\"]", root):
        return False
    key = ("K1", base, root)
    if key not in _SHELL["memo"]:
        _SHELL["memo"][key] = _tracked_listing_clear(root, base)
    return _SHELL["memo"][key]


def _tracked_listing_clear(root, base):
    deadline = time.monotonic() + _GIT_TIMEOUT
    asked = _git_ask(_GIT_TOPLEVEL, base, deadline)
    if asked is None or not asked[0].endswith("\n"):
        return False
    top = _top(os.path.realpath(asked[1]))
    if not top or os.path.realpath(asked[0][:-1]) != top:
        return False
    listed = _git_ask(("ls-files", "--stage", "-z", "--full-name", "--", root), base, deadline)
    if listed is None:
        return False
    fields = listed[0].split("\0")
    if fields.pop() or len(fields) > WALK_ENTRIES:
        return False
    above, folders, named = [part for part in top.split("/") if part], set(), {}
    for field in fields:
        entry, tab, name = field.partition("\t")
        match = _STAGE_ENTRY.match(entry)
        parts = [part for part in name.split("/") if part]
        if not tab or not match or match.group(1) == _GITLINK or not parts:
            return False
        if _named(parts) or _parts_hit(above + parts):
            # A credential-named file is let through only as committed (see `_committed`).
            if (_parts_hit(above) or match.group(2) != "0" or match.group(1) not in _REGULAR_MODES
                    or len(named) >= _COMMITTED_FILES):
                return False
            named[name] = (match.group(1), entry.split(" ")[1])
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            if folder not in folders:
                if os.path.islink(posixpath.join(top, folder)):
                    return False
                folders.add(folder)
        if time.monotonic() > deadline:
            return False
    return not named or _committed(named, top, base, deadline)


def _committed(named, top, base, deadline):
    """Whether every one of these tracked, credential-named files is, on disk, the very file HEAD holds, so that
    git grep reads only what history already holds (K1, owner's decision).

    `named` maps a path from the top of the work tree to its index mode and object name. Nothing from git's change
    detection is trusted (`--assume-unchanged`, `--skip-worktree` and `core.filemode=false` all hide an edit from
    it). HEAD is read by one `ls-tree` (same mode and object name as the index); `ls-files -v` must show no
    assume-unchanged or skip-worktree flag; and each file is checked here: a regular file (no link, no other type)
    of at most `_COMMITTED_SIZE` bytes, whose exec bit matches HEAD's mode and whose git blob id, computed from its
    bytes, is the object name HEAD holds. The bytes are hashed, never printed or returned. Comparing nothing
    through the content filters means none may be configured. Any failure, error or timeout is "not committed".
    """
    if _filter_reason(base, deadline) != "":
        return False
    spec = [f":(top,literal){name}" for name in named]
    head = _git_ask(("ls-tree", "-z", "--full-tree", "HEAD", "--", *spec), base, deadline)
    if head is None:
        return False
    seen = {}
    for field in head[0].split("\0")[:-1]:
        entry, tab, name = field.partition("\t")
        bits = entry.split(" ")
        if not tab or len(bits) != 3 or bits[1] != "blob":
            return False
        seen[name] = (bits[0], bits[2])
    if seen != named:
        return False
    flags = _git_ask(("ls-files", "-v", "-z", "--full-name", "--", *spec), base, deadline)
    if flags is None:
        return False
    tagged = set()
    for field in flags[0].split("\0")[:-1]:
        tag, space, name = field.partition(" ")
        if not space or tag != "H" or name not in named:  # lower case: assume-unchanged; S: skip-worktree
            return False
        tagged.add(name)
    if tagged != set(named):
        return False
    kind = _git_ask(("rev-parse", "--show-object-format"), base, deadline)
    algorithm = {"sha1": "sha1", "sha256": "sha256"}.get(kind[0].strip()) if kind else None
    if not algorithm:
        return False
    for name, (mode, oid) in named.items():
        if time.monotonic() > deadline or _blob_id(posixpath.join(top, name), mode, algorithm) != oid:
            return False
    return True


def _blob_id(path, mode, algorithm):
    """The git object name of the file at `path` when it is a regular file of the given mode, else None."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _COMMITTED_SIZE:
            return None
        if bool(before.st_mode & 0o100) != (mode == "100755"):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            data = b""
            while len(data) <= _COMMITTED_SIZE:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None
    if len(data) > _COMMITTED_SIZE:
        return None
    return hashlib.new(algorithm, b"blob %d\0" % len(data) + data).hexdigest()


def _search_scan(prog, args, cmd, base, bulk, git, listing, spec, queried=False):
    """A search command. `bulk` means file names are appended at run time; `listing` means its output
    feeds a reader, so even a names-only search is judged by its roots. `queried` is for a git grep the
    shell itself runs in a repository state that can be asked about now (see `_queried`)."""
    opts, before, after, values, has_pattern = _split_args(args, *spec)
    if any(opt in _INFO_OPTS for opt in opts):
        return None
    names_only = "--files" in opts
    if not has_pattern and not names_only:
        if not before and not after:
            return None  # no pattern: prints usage or help
        if before:
            before = before[1:]
        else:
            after = after[1:]
    roots = before + (after or [])
    if git and after is not None:
        roots = after
    elif git:
        # A word led by `..` that names something on disk is a path to git: one that were also a revision would
        # stop git as ambiguous. Only the queried form relies on that; it is judged by its tracked files (K1).
        revision = next((root for root in roots if (_REVISION.match(root) and not (
            queried and (root == ".." or root.startswith("../")) and _exists(root, base))) or _not_a_path(root, base)),
            None)
        if revision:
            return f"git grep at {revision}", "revision search may include credential files"
    if git:  # an exclusion narrows the search, it does not name a root
        roots = [root for root in roots if not root.startswith((":!", ":^", ":(exclude"))]
    recursive = git or prog in _ALWAYS_RECURSIVE or "recurse" in values or any(
        opt in _RECURSIVE_LONG or _RECURSIVE_SHORT.match(opt) for opt in opts)
    hit = _first(_read_hit(word, base, globs=not recursive) for word in roots) or _first(
        _read_hit(word, base, globs=False) for word in values)
    if hit:
        return hit
    roots = [root for root in roots if root != "{}"]
    # A filter that admits no credential file clears the search, as it does for the Grep tool; a
    # hidden or unignored search is cleared by it only over roots that are themselves benign.
    narrowed = not git and _narrowed(prog, args, spec)
    if any(opt in _HIDDEN_LONG or (prog in _ALWAYS_RECURSIVE and _HIDDEN_SHORT.match(opt)) for opt in opts):
        if narrowed:
            unclear = _first(_root_reason(root, base) for root in roots or ["."])
        else:
            unclear = not _hidden_roots_clear(roots or ["."], base)
        if unclear:
            return "broad hidden/unignored search", "search may include credential files"
    if not recursive or (names_only and not listing) or narrowed:
        return None  # a names-only search lists files as find does
    if not roots:
        if bulk:
            return None  # the appended file names are judged by their producer
        if prog in _ALWAYS_RECURSIVE and cmd["piped"] and not names_only:
            return None  # searches the piped input, not the tree
        roots = ["."]
    return _roots_hit("recursive search", roots, base, "search root is {why} and may include credential files", git,
                      git and queried and _plain_git_grep(opts, before, after))


def _find_roots(args):
    roots = []
    for arg in args:
        if not roots and re.fullmatch(r"-[HLPEXdsx]+", arg):
            continue
        if arg.startswith("-") or arg in ("(", ")", "!"):
            break
        roots.append(arg)
    return roots or ["."]


def _git_sub(args, base):
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-C" and i + 1 < len(args):
            base, i = _cd(base, args[i + 1]), i + 2
        elif arg in ("-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif arg.startswith("-"):
            i += 1
        else:
            return arg, args[i + 1:], base
    return None, [], base


def _git_patch(sub, opts):
    """Whether the git command prints file contents as a patch."""
    names = {opt.partition("=")[0] for opt in opts}
    asked = bool(names & _GIT_PATCH) or any(re.fullmatch(r"-U\d*", opt) for opt in opts)
    if sub == "format-patch":
        return "--stdout" in names  # otherwise the patches are written to files
    return asked or (sub in _GIT_DIFFS and not names & _GIT_SUMMARY)


def _git_path(word, base):
    """An operand written without `--` is a path only when it is there; otherwise it is a revision."""
    if _REVISION.match(word) or word.startswith(":"):
        return False
    if _GLOB.search(word):
        return True
    full = _absolute(_join(base, word))
    return full is not None and os.path.lexists(full)


def _git_patch_hit(sub, before, after, base):
    """Patch output is judged by its pathspecs; with none it covers whatever the history holds."""
    paths = after if after is not None else [word for word in before if _git_path(word, base)]
    paths = [path for path in paths if not path.startswith(_GIT_EXCLUDE)]
    if paths:
        return _roots_hit(f"git {sub}", paths, base, "patch output over {why} may include credential files", True)
    if sub == "show":
        return None  # one named commit or object, not a sweep of history
    return f"git {sub} patch output", _UNSCOPED_REASON


_PATHSPEC_LISTED = {f"patch output over {why} may include credential files" for why in (_CREDENTIAL_DIR, _ROOT_REASON)}


def _patch_listing(sub, rest, before, after, base, hit):
    """A diff or log patch that `hit` stops, judged by the names git lists for it instead where that is allowed.

    An unscoped patch is judged by its listing always (the stand-in for a sweep of history); with `--check`, which
    prints the lines it flags, and with a pathspec over a credential directory or the repository root, the
    listing stands in for the name only in a tree the owner vouched for (T7 L4), and `hit` stands elsewhere.
    The pathspecs go to git as given, after `--`, so the listing covers what the patch does.
    """
    head = rest[:rest.index("--")] if "--" in rest else rest
    plain = [arg for arg in head if arg != "--check"]
    if hit[1] == _UNSCOPED_REASON:
        if after:
            return hit
        return _listed_hit(f"git {sub}", _patch_query(sub, plain, base), base, hit, lift="--check" in head)
    if hit[1] not in _PATHSPEC_LISTED:
        return hit
    paths = after if after is not None else [word for word in before if _git_path(word, base)]
    query = _patch_query(sub, [arg for arg in plain if after is not None or arg not in paths], base)
    return _listed_hit(f"git {sub}", query + ["--", *paths] if query else None, base, hit, lift=True)


def _patch_query(sub, rest, base=""):
    """The names-only query that lists what an unscoped patch command covers.

    None when any argument is not understood, so the patch is not waved through on a guess. An
    option is only left out with a value git takes as part of the same word, and a revision must
    name nothing on disk: git reads such a word as a path, which a listing is not judged as.
    """
    keep, revisions, i = [], [], 0
    while i < len(rest):
        arg = rest[i]
        i += 1
        name, equals, value = arg.partition("=")
        if arg in _QUERY_KEEP[sub] or (sub == "log" and (
                _QUERY_COUNT.match(arg) or (equals and name in _QUERY_LOG_VALUED and not value.startswith("-")))):
            keep.append(arg)
        elif arg in _QUERY_DROP or (equals and name in _QUERY_DROP_VALUED) or re.fullmatch(r"-U\d+", arg):
            continue
        elif sub == "log" and arg == "-n" and re.fullmatch(r"\d+", "".join(rest[i:i + 1])):
            keep += [arg, rest[i]]
            i += 1
        elif _QUERY_REVISION.match(arg):
            full = _absolute(_join(base, arg))
            if full is None or os.path.lexists(full):
                return None
            revisions.append(arg)
        else:
            return None
    return list(_GIT_QUERY[sub]) + keep + revisions


def _queried(args, rest, cmd, depth, bulk):
    """Whether this is the git command the shell itself runs, in a repository state that can be asked about now.

    Not a command inside a substitution, an inline script, a remote shell or xargs; not one behind a
    wrapper, an assignment or a git option that moves it elsewhere; and not one that follows a command
    which may change what it prints.
    """
    return (depth == 0 and not bulk and bool(cmd.get("settled")) and cmd["words"][:1] == ["git"] and
            not cmd["subs"] and all(arg in _GIT_PLAIN_GLOBALS for arg in args[:len(args) - len(rest) - 1]))


def _git_ask(query, base, deadline, accept=(0,)):
    """What a guarded git query prints in the command's directory, and that directory; None when it cannot be established.

    Only path names, file modes and the settings asked for are read, never file content, and nothing is
    printed or logged. The directory must come from the caller, the query must end normally by the
    deadline and within the size allowed, and git must not be pointed at another repository or given
    other settings by this process's environment.
    """
    if not _SHELL["cwd"] or any(name in os.environ for name in _GIT_ELSEWHERE):
        return None
    folder = _absolute(base) if base else _SHELL["cwd"]
    if not folder or not os.path.isdir(folder):
        return None
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(_GIT_ENV)
    proc, data, ended = None, b"", False
    try:
        proc = subprocess.Popen(["git", *_GIT_SAFE, *query], cwd=folder, env=env, shell=False,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        fd = proc.stdout.fileno()
        while len(data) <= _GIT_OUTPUT_LIMIT:
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                ended = proc.wait(max(deadline - time.monotonic(), 0.05)) in accept
                break
            data += chunk
    except (OSError, ValueError, subprocess.SubprocessError):
        ended = False
    finally:
        if proc is not None:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            try:
                proc.stdout.close()
                proc.wait(1)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    if not ended:
        return None
    return data.decode("utf-8", "surrogateescape"), folder


def _git_names(query, base, deadline):
    """The paths a names-only query lists, those that are submodule entries, and the directory asked in.

    None when they cannot be established, which includes a listing that is not entry and path in turn.
    """
    asked = _git_ask(query, base, deadline)
    if asked is None:
        return None
    fields = asked[0].split("\0")
    if fields.pop() or len(fields) % 2:
        return None
    names, links = [], []
    for entry, name in zip(fields[::2], fields[1::2]):
        match = _RAW_ENTRY.match(entry.lstrip("\n"))
        if not match or not name:
            return None
        names.append(name)
        if _GITLINK in match.group(1).split():
            links.append(name)
    return names, links, asked[1]


def _config_reason(base, deadline):
    """Why the settings in force let a patch print what its listing does not show: "" when they do not, None when unknown."""
    asked = _git_ask(_GIT_CONFIG, base, deadline, (0, 1))  # git ends with 1 when no setting matches
    if asked is None:
        return None
    for entry in asked[0].split("\0"):
        key, _, value = entry.partition("\n")
        key, value = key.lower(), value.strip()
        if key == "diff.submodule":
            if value.lower() == "diff":
                return _SUBMODULE_CONFIG_REASON
        elif key.startswith("diff.") and value:
            return _DRIVER_REASON
    return ""


def _filter_reason(base, deadline):
    """Why the listing must not run: "" when no content filter is configured, None when that is unknown."""
    asked = _git_ask(_GIT_FILTERS, base, deadline, (0, 1))  # git ends with 1 when no setting matches
    if asked is None:
        return None
    for entry in asked[0].split("\0"):
        if re.match(r"^filter\..+\.(?:clean|smudge|process)$", entry.partition("\n")[0], re.I):
            return _FILTER_REASON
    return ""


def _listed_hit(label, query, base, unproven, lift=False):
    """Judge a patch by the paths it covers; `unproven` stands when they cannot be listed.

    A patch that covers no credential path still prompts when it may print more than the paths listed:
    when it touches a submodule, and when the settings in force print submodule files or hand the
    printing to a configured program. The two queries share one time allowance.

    `lift` is for a patch that prompts today even when its listing is clean (T7 L4: `--check`, a pathspec
    over a credential directory). It stands only in a trusted tree that is the work tree git itself reports,
    and clears the call there; anywhere else `unproven` stands, as it does without a record.
    """
    set_here = sorted(name for name in _GIT_ELSEWHERE if name in os.environ)
    if set_here and _SHELL["cwd"]:  # the same prompt as `unproven`, with the reason in plain words
        return unproven[0], _GIT_ENV_REASON.format(
            names=", ".join(set_here),
            them="it" if len(set_here) == 1 else "them", first=set_here[0])
    if lift:    # nothing is asked of git before the tree is known to be one the owner vouched for
        folder = _absolute(base) if base else _SHELL["cwd"]
        here = _top(os.path.realpath(folder)) if folder else None
        tree = _tree_for(here) if here and _SHELL["trust"] is not None else None
        if tree is None or tree.top != here:
            return unproven
    deadline = time.monotonic() + _GIT_TIMEOUT
    if query:
        # Before any listing: comparing files with the work tree runs the filters configured for them.
        why = _filter_reason(base, deadline)
        if why is None:
            return unproven
        if why:
            return f"{label} patch output", why
    listed = _git_names(query, base, deadline) if query else None
    if listed is None:
        return unproven
    names, links, folder = listed
    folder = os.path.realpath(folder)
    top = _top(folder) or folder
    above = [part for part in top.split("/") if part]

    def shown(name):
        return name if len(name) <= 200 and _PATHISH.match(name) else "a credential-bearing path"

    placed = []

    def where_git_reads():
        """Whether git's own top level is the work tree the names were placed under (T7 3.3). `core.worktree`,
        an include that sets it, or a `.git` file pointing elsewhere make git list files of a directory this
        process never looked at; then no listed name is lifted. Asked once, only when a lift is wanted."""
        if not placed:
            asked = _git_ask(_GIT_TOPLEVEL, base, deadline)
            answer = asked[0][:-1] if asked and asked[0].endswith("\n") else None
            placed.append(bool(answer) and os.path.realpath(answer) == top)
        return placed[0]

    def lifted(name, parts):
        return (_SHELL["trust"] is not None and _weak_only(_plain_name(name)) and where_git_reads() and
                _lift_name("/" + "/".join(above + parts), name, "L4"))

    def cleared():
        """The call is clear: a patch that prompted for the listing alone is lifted here, and says so."""
        _SHELL["trust"].note("L4", top, unproven[1])
        return None

    if lift and not where_git_reads():
        return unproven
    for name in names:
        parts = [part for part in name.split("/") if part]
        if parts and (_parts_hit(above + parts) or (SENSITIVE_NAME.match(_plain_name(parts[-1])) and not
                                                    lifted(parts[-1], parts))):
            return f"{label} of {shown(name)}", "patch output covers a credential-bearing path"
    if links:
        link = links[0] if len(links[0]) <= 200 and _PATHISH.match(links[0]) else "a submodule"
        return f"{label} of {link}", _SUBMODULE_REASON
    if not names:
        return cleared() if lift else None  # nothing is printed, whatever would have printed it
    why = _config_reason(base, deadline)
    if why is None:
        return unproven
    if why:
        return f"{label} patch output", why
    return cleared() if lift else None


def _git_archive_hit(args, cmd, base):
    """An archive of a credential root is a bulk read; one of the whole tree is when a command receives it."""
    _, before, after, _, _ = _split_args(args, _GIT_ARCHIVE_VALUES)
    operands = before + (after or [])
    below = operands[0].partition(":")[2] if operands else ""
    roots = [posixpath.join(below, path) for path in operands[1:]] or [below or "."]
    hit = _first(_read_hit(root, base) for root in roots)
    if hit:
        return hit
    for root in roots:
        why = _root_reason(root, base, True, True)
        if why in (_CREDENTIAL_DIR, "a credential-bearing path", _GLOB_REASON) or (why and cmd["feeds"]):
            return f"git archive of {root}", _BULK_REASON
    return None


def _feed_hit(first, base):
    """Whether a command that supplies file names to a reader is scoped to benign roots."""
    unscoped = "bulk shell read", _BULK_REASON
    prog, args = _program(first["words"])
    if prog == "git":
        sub, rest, base = _git_sub(args, base)
        if sub == "grep":
            return None  # judged by the search rule
        if sub != "ls-files":
            return unscoped
        _, before, after, _, _ = _split_args(rest, {"-x", "-X", "--exclude", "--exclude-from"})
        return _roots_hit("bulk read", before + (after or []) or ["."], base, _BULK_REASON, True)
    if prog == "find":
        return _roots_hit("bulk read", _find_roots(args), base, _BULK_REASON)
    if prog in _GREPS:
        return _search_hit(prog, args, first, base, False, listing=True)
    if prog == "ls":
        return _roots_hit("bulk read", [arg for arg in args if not arg.startswith("-")] or ["."], base, _BULK_REASON)
    if prog in ("echo", "printf"):
        return _first(_read_hit(arg, base) for arg in args)
    return unscoped


def _producer_hit(cmd, base):
    """Whether the file list feeding xargs is scoped: judged from the first command of the pipeline."""
    first = cmd["first"]
    if first is None or first is cmd:
        return "bulk shell read", _BULK_REASON
    return _feed_hit(first, base)


def _listing_hit(inner, base):
    """A reader fed by a substituted file listing is a bulk read of that listing's roots."""
    listing = [item for item in _commands(inner) if isinstance(item, dict)]
    if not listing:
        return None
    prog, args = _program(listing[0]["words"])
    if (prog in ("find", "ls", "fd", "fdfind", "echo", "printf") or prog in _GREPS or
            (prog == "git" and _git_sub(args, base)[0] == "ls-files")):
        return _feed_hit(listing[0], base)
    return None


def _tar_hit(args, base):
    """Archive members are read; creating an archive over a directory reads all of it."""
    create, roots, i = False, [], 0
    while i < len(args):
        arg = args[i]
        bundle = arg[1:] if arg.startswith("-") and not arg.startswith("--") else arg if i == 0 else None
        i += 1
        if arg in ("-C", "--directory", "--cd") and i < len(args):
            base, i = _cd(base, args[i]), i + 1
        elif arg.startswith("--directory="):
            base = _cd(base, arg.partition("=")[2])
        elif arg == "--file":
            i += 1
        elif arg.startswith("--"):
            create = create or arg in ("--create", "--append", "--update")
        elif bundle is not None and re.fullmatch(r"[A-Za-z]+", bundle):
            create = create or bool(set(bundle) & set("cru"))
            i += "f" in bundle
            if "C" in bundle and i < len(args):
                base, i = _cd(base, args[i]), i + 1
        else:
            roots.append(arg)
    return _first(_read_hit(word, base) for word in roots) or (
        _roots_hit("archive", roots, base, _BULK_REASON) if create else None)


def _unresolved(line):
    """A hit when a command line a wrapper hands on cannot be read for certain (command_wrappers.uncertain): an expansion that is
    not resolved, a shell with no command string or with anything but `-c` before it, a program that runs other words
    (`find -exec`, `docker exec`, `xargs`, `eval`, ...), anywhere in it, in any shell or line nested inside it.

    What runs there is chosen by a shell that is not this one, so an unknown command is treated as a read, as
    redact_hook.bash_searches treats it as a search. The text stays the wrapper's whatever shells nest inside it, because the
    whole of it is read; the user's own top-level `sh -c '$X'` never comes here."""
    if command_wrappers.uncertain(line):
        return line, "the command it runs is chosen by the shell at run time and cannot be checked"
    return None


# Programs the floor looks for in a wrapper's tail: every one that _command_hit judges by name.
_FLOOR = _INNER | {"xargs", "find", "fd", "fdfind", "git", "eval", "curl", "wget", "ssh", "tar"}
# Programs `_command_hit` joins the words after them into one command line for (`ssh host LINE`, `eval LINE`): the floor hands them each
# word once (command_wrappers.bag), not a string and then the words made from it.
_JOINED = ("ssh", "eval")


def _floor_hit(words, cmd, base, depth):
    """The floor under every wrapper: when the command in `words` starts with a wrapper (at any depth), each word after it and
    each word of every string inside them is tried as the command, over the words that follow it (command_wrappers.bag). This
    does not depend on the wrapper's option table, so a table that is wrong about an option cannot hide a read. The table
    (`_program`) only ever adds to what this finds."""
    wrapped = command_wrappers.starts_with_wrapper(words, _ASSIGN, _KEYWORDS)
    if not wrapped:
        return None
    entries, too_deep = command_wrappers.bag(wrapped[1], once=_JOINED)
    if too_deep:
        return wrapped[0], "command nesting is too deep to verify"
    if command_wrappers.floor_uncertain(*wrapped):  # text a wrapper hands on that cannot be read for certain, found by name
        return wrapped[0], "the command it runs is chosen by the shell at run time and cannot be checked"
    unplaced = dict(cmd, settled=False)
    runs = _runs(words)
    for name, rest in entries:
        name = _ALIASES.get(name, name)
        if name in _FLOOR:
            # Any word of the tail is tried as the program, so a hit keeps its non-search cause only when the word tried
            # is the program the wrapper's table (`_program`, xargs' own table) says runs, with the same arguments
            # (`timeout 5 python3 x.py`). Any other word's hit is a guess at what runs: it has no cause, and refuses.
            hit = _command_hit(name, rest, unplaced, base, depth)
            if hit and (name, list(rest)) == runs:
                return hit
            if hit:
                # ssh's remote command line runs on the remote host: when ssh's own branch finds it runs there (a
                # ProxyCommand or LocalCommand runs here and is not that), the floor's guess at it does too.
                remote = wrapped[0] == "ssh" and _cause(_command_hit("ssh", wrapped[1], unplaced, base, depth)) == ELSEWHERE
                return _because(ELSEWHERE if remote else None, hit)
    return None


def _runs(words):
    """The program and arguments a command line runs by the wrappers' tables, through xargs too; (None, []) when unknown."""
    prog, args = _program(words)
    if prog == "xargs":
        found = command_wrappers.unwrap("xargs", args)
        prog, args = _program(found[1]) if isinstance(found, tuple) and found[0] == "words" else (None, [])
    return _ALIASES.get(prog, prog), list(args)


_PROBE = ".env"  # a file name the classifier finds sensitive in any directory
_FD_TOKENS = ("{}", "{/}", "{//}", "{.}", "{/.}")


_NAMES_ONLY_WORDS = {"cd", "pushd", "popd", "for", "while", "until", "select", "case", "read"}


def _program_cause(words, base, depth, tokens=("{}",), stdin=False):
    """The non-search cause of the program in `words` when a command hands it file names (xargs, find -exec, fd -x, a loop
    variable, a substituted listing) or, with `stdin`, a file on its standard input; None when it searches or reads them.

    The program is judged by this classifier as if it were given a sensitive file: each placeholder in `tokens` (xargs -I,
    find's {}, a loop variable) and one more operand at the end are that file. A shell search or read (_search_read), and any
    hit with no cause (`sh -c 'grep TOKEN {}'`), is None, and so is no hit at all: what the program does with the file is not
    known. On standard input a program that only names files (NAMES_ONLY) is None too: `done < .env` feeds the loop's
    commands, `while read l; do echo $l; done` prints it."""
    prog, args = _program(words)
    if prog is not None and _search_read(prog, args):
        return None
    probe = [word for word in words]
    for token in tokens:
        probe = [word.replace(token, _PROBE) for word in probe]
    saved = _SHELL.get("trust")
    _SHELL["trust"] = None  # what the program does, not what the owner vouched for
    try:
        hit = _shell_hit(" ".join(shlex.quote(word) for word in probe + [_PROBE]), base, depth + 1)
    except Exception:  # a program that cannot be judged is not known to be no search
        return None
    finally:
        _SHELL["trust"] = saved
    return None if stdin and _cause(hit) == NAMES_ONLY else _cause(hit)


def _xargs_tokens(args):
    """The strings xargs replaces with each name it reads (-I, -J, -i, --replace), with find's `{}`."""
    tokens = ["{}"]
    for i, arg in enumerate(args):
        if arg in ("-I", "-J") and i + 1 < len(args):
            tokens.append(args[i + 1])
        elif re.fullmatch(r"-[IJ].+", arg):
            tokens.append(arg[2:])
        elif arg.startswith("--replace="):
            tokens.append(arg.partition("=")[2])
    return tuple(token for token in tokens if token)


def _command_hit(prog, args, cmd, base, depth, bulk=False):
    if prog == _OPAQUE:
        return " ".join(args[:1]) or "a wrapper", "an option of the wrapper is not known, so the command it runs cannot be found"
    if prog == "xargs":
        found = command_wrappers.unwrap("xargs", args)
        if found is None:
            return None
        if found is command_wrappers.UNKNOWN:
            return _command_hit(_OPAQUE, args, cmd, base, depth)
        sub, rest = _program(found[1])
        if sub is None:
            return None
        # The names it is fed are judged for every bulk program, and for `git grep` too: they are the files it searches.
        # The hit is a non-search's only when the program it runs is one (_program_cause: `xargs python3`, `xargs git diff`);
        # `xargs grep`, `xargs sed -n`, `xargs git grep`, `xargs sh -c 'grep TOKEN {}'` are searches and refuse.
        hit = (_producer_hit(cmd, base) if sub in _BULK or _search_read(sub, rest) else None) or _command_hit(
            sub, rest, cmd, base, depth, True)
        # xargs' own options name its replace strings; the command's (`ruby -I lib`) do not (sg1 round 6 attack)
        return _because(_program_cause(found[1], base, depth, _xargs_tokens(args[:len(args) - len(found[1])])), hit)
    if prog == "find":
        roots = _find_roots(args)
        for i, arg in enumerate(args):
            if arg in _FIND_EXEC:
                end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args))
                sub, rest = _program(args[i + 1:end])
                hit = _floor_hit(args[i + 1:end], cmd, base, depth) or (_roots_hit("bulk read", roots, base, _BULK_REASON) if sub in _BULK else None) or (
                    _command_hit(sub, rest, cmd, base, depth, True) if sub else None)
                if hit:  # run once per file find selects: a non-search when the program is one (_program_cause)
                    return _because(_program_cause(args[i + 1:end], base, depth), hit)
        return None
    if prog in ("fd", "fdfind"):
        for i, arg in enumerate(args):
            if arg in _FD_EXEC:
                hit = ("bulk shell read", _BULK_REASON) if _program(args[i + 1:])[0] in _BULK else _floor_hit(
                    args[i + 1:], cmd, base, depth)
                if hit:  # run once per file fd finds, as find -exec
                    return _because(_program_cause(args[i + 1:], base, depth, _FD_TOKENS), hit)
        return None
    if prog == "git":
        sub, rest, base = _git_sub(args, base)
        if sub == "grep":
            return _search_hit(prog, rest, cmd, base, bulk, git=True, queried=_queried(args, rest, cmd, depth, bulk))
        return _because(GIT_OUTPUT, _git_output_hit(args, sub, rest, cmd, base, depth, bulk))
    if prog in _GREPS:
        return _search_hit(prog, args, cmd, base, bulk)
    # RUNS_CODE claims the command runs code and not a search or read of the file. Only _runs_code gives it, and only for
    # SCRIPT FILE: a script file _code_run's positive parse shows (a script file given the file as an argument is project
    # code; a known limit). INLINE CODE (code in any spelling, any count, on stdin, in a here-string or heredoc, or no parse)
    # never has it: inline code that touches a sensitive name anywhere is a read, and refuses in a worker (sg1 round 7).
    if prog in _SHELLS:
        form, _, _ = _code_run(prog, args, base)
        env = _before_program(cmd, prog, args)
        startup = _startup_hit(env, base)  # BASH_ENV=/ENV= naming a sensitive file: the shell sources it first
        at = _dash_c_at(args)
        if at is not None:  # INLINE CODE: the line it runs is judged as SHELL SOURCE (_commands_hit `source`)
            line = args[at]
            hit = (_unresolved(line) if isinstance(line, _Line) else None) or _shell_hit(line, base, depth + 1, source=True)
            given = (startup or _words_hit(args[:at], base) or _words_hit(args[at + 1:], base) or _env_hit(env, base) or
                     _input_text_hit(cmd, base))
            # A sensitive name reaching the line another way (an operand, an option value such as `--rcfile .env`, an env
            # value, stdin text), or options the parse does not show (`form` is not _INLINE): no cause.
            return _because(None, hit or given) if given or form != _INLINE else hit
        if startup:  # the shell runs the sensitive file before any script: not a script file the parse shows
            return _because(None, startup)
        # Every word alone: no shell option takes text, and the words after a script or `-s` are its data, so none is
        # skipped as an option's (`bash -s -- -e FILE <<EOF`, sg1 round 8).
        given = _words_hit(args, base) or (_env_hit(env, base) if form != _SCRIPT else None)
        if given or form != _STDIN:
            return _because(_runs_code(form), given)  # SCRIPT FILE; anything else: INLINE CODE, no cause
        return _piped_source_hit(cmd, base, depth)  # code on stdin from a pipe: the text printed into it is SHELL SOURCE
    if prog == "eval":
        return _shell_hit(" ".join(args), base, depth + 1, source=True)
    if prog in _INTERPRETERS:
        form, codes, rest = _code_run(prog, args, base)
        if form == _SCRIPT:  # options from the closed list take no text; the script's arguments are its data (sg1 round 8)
            return _because(_runs_code(form), _words_hit(args, base))  # SCRIPT FILE
        given = _other_hit(args, base, True)
        # INLINE CODE, or no parse: no cause, whatever hands it the name (a literal in any code or word after it, an
        # operand, an env value, here-string or heredoc text).
        literal = _first(_literal_hit(word, base) for word in codes + rest if isinstance(word, str) and word)
        return _because(None, given or _words_hit(rest, base) or literal or
                        _env_hit(_before_program(cmd, prog, args), base) or _input_text_hit(cmd, base))
    return _other_command_hit(prog, args, cmd, base, depth, bulk)


def _piped_source_hit(cmd, base, depth):
    """The hit in the SHELL SOURCE a pipeline prints into a shell reading code on stdin (`printf '%s\\n' LINE ... | bash -s`,
    `echo LINE | sh`, `cat <<'SH' | bash`): the arguments of echo or printf, and the here-document and here-string text of
    the pipeline's first command, judged as shell source (sg1 round 9). What any other program prints is not visible."""
    first = cmd.get("first") if isinstance(cmd, dict) and cmd.get("piped") else None
    if not isinstance(first, dict) or first is cmd:
        return None
    return _printed_source_hit(first, base, depth)


def _printed_source_hit(first, base, depth):
    """The hit in the text the command `first` prints for a shell to run: echo or printf arguments, here-document and
    here-string text, each judged as SHELL SOURCE (sg1 round 9)."""
    prog, args = _program(first["words"])
    texts = (list(args) if prog in _PRINTERS else []) + list(first["strings"]) + [
        "".join(holder) for holder, _ in first["heredocs"]]
    return _first(_shell_hit(text, base, depth + 1, source=True) for text in texts if isinstance(text, str) and text)


def _process_source_hit(body, base, depth):
    """The hit in a process substitution a shell or `.`/`source` runs as a script (`bash <(echo LINE)`): when its one
    command prints text (echo, printf, a here-document to cat), that text is SHELL SOURCE; any other body is judged as
    shell source itself, so a name in it has no cause (sg1 round 9)."""
    cmds = [c for c in _commands(body) if isinstance(c, dict)]
    if len(cmds) == 1 and (_program(cmds[0]["words"])[0] in _PRINTERS or cmds[0]["heredocs"] or cmds[0]["strings"]):
        printed = _printed_source_hit(cmds[0], base, depth)
        return printed or _because(None, _shell_hit(body, base, depth + 1))
    return _because(None, _shell_hit(body, base, depth + 1, source=True))


def _source_words_hit(cmd, base):
    """A sensitive name among the words of one command of SHELL SOURCE (sg1 round 9): its words (a `NAME=value` assignment's
    value, the operands of set, export, local, declare, echo), here-strings and redirect targets, each looked at alone. A word
    is also split at blanks and shell operators, so printed source in one word (`echo 'f=FILE; cat "$f"'`, `-c "$(echo
    ...)"`) has its words looked at too."""
    words = [w for w in cmd["words"] + cmd["strings"] + cmd["inputs"] + cmd["outputs"] if isinstance(w, str)]
    return _words_hit(words + [piece for w in words for piece in _SOURCE_SPLIT.split(w) if piece and piece != w], base)


def _reads_source(prog, args, base):
    """Whether `prog args` runs SHELL SOURCE from its standard input or a process substitution: a shell, or `.`/`source`
    reading /dev/stdin (sg1 round 9)."""
    return prog in _SHELLS or (prog in (".", "source") and _code_run(prog, args, base)[0] == _STDIN)


def _runs_code(form):
    """RUNS_CODE for a script file _code_run's positive parse shows (_SCRIPT); None for every other form: inline code in any
    spelling, code on stdin, or no parse (sg1 round 7). The one place the cause is given."""
    return RUNS_CODE if form == _SCRIPT else None


def _words_hit(words, base):
    """A sensitive name among `words`, each looked at alone: what follows inline code is the program's own arguments, so no
    word is skipped as an option's text (`node -e CODE -e FILE`: FILE is process.argv's, sg1 round 7 attack)."""
    return _first(_other_hit([word], base, True) for word in words if isinstance(word, str))


def _before_program(cmd, prog, args):
    """The words of `cmd` before the program that runs `args`: its assignments and wrappers (`F=x env G=y bash`), where
    startup env and an env value are set. A `NAME=value` after the program is its argument, not its environment (sg1
    round 8). When `args` is not found among the words (a line a wrapper hands a shell), every word, as before."""
    words = cmd.get("words", []) if isinstance(cmd, dict) else []
    args = list(args)
    for k in range(len(words) - len(args), 0, -1):
        name = posixpath.basename(words[k - 1]).lower() if isinstance(words[k - 1], str) else None
        if words[k:k + len(args)] == args and _ALIASES.get(name, name) == prog:
            return words[:k - 1]
    return words


def _startup_hit(words, base):
    """A sensitive file a shell sources before it runs anything, named by `BASH_ENV=` or `ENV=` before it (_before_program)."""
    for word in words:
        name, equals, value = word.partition("=")
        if equals and name in ("BASH_ENV", "ENV") and value:
            hit = _other_hit([value], base, True) or _read_hit(value, base, False)
            if hit:
                return hit
    return None


def _input_text_hit(cmd, base):
    """A sensitive name in the text a command gets on standard input (a here-string, a here-document body): a quoted
    literal or a bare word. Inline code given it may read the file it names (sg1 round 7)."""
    if not isinstance(cmd, dict):
        return None
    texts = list(cmd.get("strings", ())) + ["".join(holder) for holder, _ in cmd.get("heredocs", ())]
    # every word alone: stdin text is data, so no word is skipped after `-e`, `-f` or `--` (sg1 round 8)
    return _first(_literal_hit(text, base) or _words_hit(text.split(), base) for text in texts if isinstance(text, str))


def _env_hit(words, base):
    """A sensitive path that an assignment before the program (`F=config/local.json python3 -c ...`, `env F=... node -e
    ...`; `words` from _before_program) hands the code it runs in its environment, whatever the variable is called."""
    for word in words:
        if isinstance(word, str) and _ASSIGN.match(word):
            value = word.partition("=")[2]
            hit = _other_hit([value], base, True) or _read_hit(value, base, False)
            if hit:
                return hit
    return None


# _code_run: how an interpreter or shell is shown to run a script file (sg1 round 6). Each program has a closed list of the
# options that take no code: `flags` alone, `values` with one value after them, `forms` whole-word patterns (an attached or
# `=` value); `code` are its inline-code options (a value after them, or `--eval=CODE`); `attached` the code letters that take
# their code in the same word (`-c'CODE'`, `-ne'CODE'`) after any of the no-value letters of `cluster`; `sub` a subcommand
# that may (or with `need`, must) come first; `script` an option whose value is the script (php -f). Anything else ends the
# parse with no form: a list cannot hold every spelling of code, so what it does not hold is never shown to be a script.
_SCRIPT, _STDIN, _INLINE = "script file", "code on stdin", "inline code"
_SHELL_RUN = dict(flags={"-" + c for c in "euxvnfabhmpCETBHPilr"} | {"+" + c for c in "euxvnfabhmpCETBHP"} | {
    "--login", "--norc", "--noprofile", "--posix", "--verbose", "--noediting", "--restricted", "--no-config"},
    values={"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}, stdin={"-s"}, dash_c="euxvnfabhmpCETBHPilr")
_NODE_FORMS = r"--(?:max-old-space-size|max-semi-space-size|stack-size)=\d+|--unhandled-rejections=[\w-]+|--title=[\w.-]+"
_RUN_TABLE = {
    "python": dict(flags={"-b", "-bb", "-B", "-d", "-E", "-i", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-vv",
                          "-x", "-R"}, values={"-W", "-X", "--check-hash-based-pycs"}, code={"-c"}, attached="c",
                   cluster="bBdEiIOPqRsSuvx"),
    "node": dict(flags={"--no-warnings", "--no-deprecation", "--trace-warnings", "--trace-deprecation", "--trace-uncaught",
                        "--enable-source-maps", "--preserve-symlinks", "--preserve-symlinks-main", "--expose-gc",
                        "--experimental-vm-modules", "--experimental-strip-types", "--experimental-transform-types",
                        "--abort-on-uncaught-exception", "--pending-deprecation", "--throw-deprecation", "--inspect",
                        "--inspect-brk", "--watch"},
                 values={"-r", "--require", "--import"}, forms=_NODE_FORMS, code={"-e", "--eval", "-p", "--print"}),
    "bun": dict(flags={"--bun", "--smol", "--hot", "--watch", "--silent"}, code={"-e", "--eval", "-p", "--print"}, sub="run"),
    "deno": dict(flags={"-A", "--allow-all", "-q", "--quiet", "--no-check", "--no-prompt", "--reload"},
                 forms=r"--(?:allow|deny)-(?:read|write|net|env|run|sys|ffi|hrtime|import)(?:=\S*)?|--unstable(?:-[\w-]+)?",
                 sub="run", need=True),
    "ruby": dict(flags={"-w", "-W", "-W0", "-W1", "-W2", "-d", "-v", "--verbose", "-U", "--disable-gems", "--jit", "--yjit"},
                 values={"-I", "-r"}, forms=r"-I[\w./-]+|-r[\w./-]+", code={"-e"}, attached="e", cluster="nplawdv"),
    "perl": dict(flags={"-w", "-W", "-X", "-T", "-t", "-s"}, forms=r"-I[\w./-]+|-[Mm][\w:]+", code={"-e", "-E"},
                 attached="eE", cluster="nplaswWTtX"),
    "php": dict(flags={"-n", "-q", "-H", "-C", "-e"}, values={"-d", "-c"}, code={"-r", "-B", "-R", "-E"}, attached="rBRE",
                script="-f"),
    "osascript": dict(flags={"-i"}, values={"-l", "-s"}, code={"-e"}),
    ".": dict(), "source": dict(),
}
_RUN_TABLE["python3"] = _RUN_TABLE["python"]
for _shell in _SHELLS:
    _RUN_TABLE[_shell] = _SHELL_RUN


def _code_run(prog, args, base=""):
    """What an interpreter or shell given `args` runs, by a positive parse (sg1 round 6): (form, codes, rest).

    _SCRIPT: a script file (the operand, after options on the program's closed list and at most one `--`) that is not
    `-`, /dev/stdin, /dev/fd/N or a sensitive file, `rest` its arguments, never read as options. _STDIN: the code is read
    from standard input (`-`, /dev/stdin, no operand, a shell's -s), `rest` its arguments. _INLINE: the parse ended at an
    inline-code option in a spelling the table knows, `codes` the code, `rest` what follows it. None: anything else (an
    unknown option, a cluster, an attached value, a code option in another spelling, an option value or operand that is
    sensitive): nothing shows a script file runs."""
    table = _RUN_TABLE.get(prog)
    if table is None:
        return None, [], list(args)
    flags, values, code = table.get("flags", set()), table.get("values", set()), table.get("code", set())
    forms = re.compile(table["forms"]) if table.get("forms") else None
    i, dashed = 0, False
    if table.get("sub"):
        if args[:1] == [table["sub"]]:
            i = 1
        elif prog == "deno" and args[:1] == ["eval"]:
            return _deno_eval(args[1:])
        elif table.get("need"):
            return None, [], list(args)
    while i < len(args):
        arg = args[i]
        if dashed or arg in ("-", "/dev/stdin", "/dev/fd/0") or not arg.startswith(("-", "+")) or arg == "+":
            break
        if arg == "--":
            dashed, i = True, i + 1
            continue
        if arg in flags:
            i += 1
        elif forms and forms.fullmatch(arg):  # an attached or `=` value: a sensitive one is not a script's option
            if _other_hit([arg.partition("=")[2] if "=" in arg else arg[2:]], base, True):
                return None, [], list(args)
            i += 1
        elif arg in values or arg == table.get("script"):
            if i + 1 >= len(args) or _other_hit([args[i + 1]], base, True):
                return None, [], list(args)
            if arg == table.get("script"):
                return _script_operand(args[i + 1], args[i + 2:], base)
            i += 2
        elif arg in table.get("stdin", ()):
            return _STDIN, [], list(args[i + 1:])
        elif table.get("dash_c") and re.fullmatch(r"-[%s]*c[%s]*" % (table["dash_c"], table["dash_c"]), arg):
            # a shell's -c alone or in a cluster of its no-value letters (`-lc`, `-cx`): the next word is the line
            return (_INLINE, [args[i + 1]], list(args[i + 2:])) if i + 1 < len(args) else (None, [], list(args))
        else:
            return _inline(arg, args[i + 1:], table)
    if i >= len(args):
        return (_STDIN, [], []) if prog not in (".", "source") else (None, [], [])
    return _script_operand(args[i], args[i + 1:], base)


def _script_operand(operand, rest, base):
    if operand in ("-", "/dev/stdin", "/dev/fd/0"):
        return _STDIN, [], list(rest)
    if operand.startswith(("/dev/fd/", "<(", "=(")) or _other_hit([operand], base, True) or _read_hit(operand, base, False):
        return None, [], [operand, *rest]
    return _SCRIPT, [], list(rest)


def _inline(arg, rest, table):
    """The parse ended at `arg`: inline code when it is a code option in a spelling the table knows, else no form."""
    code, attached, cluster = table.get("code", set()), table.get("attached", ""), table.get("cluster", "")
    name, equals, value = arg.partition("=")
    if arg in code:
        codes, rest = [rest[0] if rest else ""], rest[1:]
    elif equals and name.startswith("--") and name in code:
        codes = [value]
    else:
        letters = arg[1:] if arg.startswith("-") and not arg.startswith("--") else ""
        at = next((j for j, letter in enumerate(letters) if letter not in cluster), len(letters))
        if at >= len(letters) or letters[at] not in attached:
            return None, [], [arg, *rest]
        tail = letters[at + 1:]
        codes, rest = ([tail], rest) if tail else ([rest[0] if rest else ""], rest[1:])
    # What follows is left as it is (perl's further -e, a program argument that looks like an option): inline code has no
    # cause whatever it holds, and every word of it is looked at for a sensitive name (sg1 round 7).
    return _INLINE, codes, list(rest)


def _deno_eval(args):
    for i, arg in enumerate(args):
        if arg in ("-p", "--print", "-q", "--quiet"):
            continue
        if arg.startswith("-"):
            return None, [], list(args)
        return _INLINE, [arg], list(args[i + 1:])
    return _INLINE, [""], []


def _git_output_hit(args, sub, rest, cmd, base, depth, bulk):
    """A git subcommand other than grep: what it prints is history, a patch or an object (GIT_OUTPUT)."""
    opts, before, after, values, _ = _split_args(rest, _GIT_VALUES)
    operands = before + (after or [])
    if sub == "diff" and "--no-index" in opts:
        return _first(_read_hit(word, base) for word in operands) or _roots_hit(
            "recursive diff", operands, base, _BULK_REASON)
    if sub == "archive":
        return _git_archive_hit(rest, cmd, base)
    if sub == "stash":
        if before[:1] in (["show"], ["list"]) and _git_patch("log", opts):
            unproven = "git stash patch output", _STASH_REASON
            if rest[:1] != ["show"] or not _queried(args, rest, cmd, depth, bulk):
                return unproven
            return _listed_hit("git stash show", _patch_query(sub, rest[1:], base), base, unproven)
        return None
    if sub in _GIT_READ:
        hit = _first(_read_hit(word, base) for word in operands)
    elif sub in _GIT_HISTORY:
        hit = _first(_read_hit(word, base) for word in after or [])
    else:
        return None
    if hit or sub not in _GIT_HISTORY | _GIT_DIFFS or not _git_patch(sub, opts):
        return hit
    hit = _git_patch_hit(sub, before, after, base)
    if hit and sub in ("diff", "log") and _queried(args, rest, cmd, depth, bulk):
        return _patch_listing(sub, rest, before, after, base, hit)
    return hit


def _other_command_hit(prog, args, cmd, base, depth, bulk):
    """Every program _command_hit does not judge itself."""
    if prog in ("curl", "wget"):
        files = [name for arg in args for name in re.findall(r"@([^;,\s]+)", arg)]
        for i, arg in enumerate(args):
            name, equals, value = arg.partition("=")
            if name in _CURL_FILES:
                files.append(value if equals else "".join(args[i + 1:i + 2]))
        return _first(_read_hit(name, base) for name in files if name)
    if prog == "ssh":
        _, _, _, values, _ = _split_args(args, *_SSH_ARGS)
        remote = command_wrappers.unwrap("ssh", args)  # what follows the host, options included, is the remote command line
        if remote is command_wrappers.UNKNOWN:
            return _command_hit(_OPAQUE, args, cmd, base, depth)
        # The key files ssh reads to log in, and the remote host's files: no local safe tool reads them. A command ssh runs
        # here (ProxyCommand, LocalCommand, KnownHostsCommand) is not elsewhere: with one, the hit has no cause.
        here = re.search(r"(?i)(?:proxy|local|knownhosts)command", " ".join(args))
        return _because(None if here else ELSEWHERE, _first(_read_hit(word, base) for word in values) or (
            _unresolved(remote[1]) or _shell_hit(remote[1], "", depth + 1) if remote else None))
    if prog in _SCRIPT_FIRST:
        _, before, after, values, has_script = _split_args(args, *_SCRIPT_ARGS)
        operands = before + (after or [])
        hit = _first(_read_hit(word, base) for word in (operands if has_script else operands[1:]) + values)
        if hit or has_script or not operands:
            return hit
        # The first operand is normally the script; a credential file there is still a file.
        script = operands[0]
        return (script, _NAME_REASON) if _PLAIN.match(script) and _sensitive(_join(base, script), False) else None
    if prog == "tar":
        return _tar_hit(args, base)
    if prog in _READERS:
        opts, before, after, values, _ = _split_args(args)
        operands = before + (after or [])
        if prog in _DESTINATION and len(operands) > 1:
            operands = operands[:-1]  # the last operand is written, not read
        if prog in _COPIERS:  # copying or archiving a credential directory reads all of it
            hit = _first((word, _BULK_REASON) for word in operands if _root_reason(word, base) == _CREDENTIAL_DIR)
            if hit:
                return hit
        if prog in ("rsync", "ditto"):
            deep = True
        elif prog == "diff":
            deep = any(opt == "--recursive" or _RECURSIVE_SHORT.match(opt) for opt in opts)
        else:
            deep = prog in ("cp", "scp", "zip", "copy-item") and any(
                opt in _RECURSIVE_LONG or _COPY_RECURSIVE.match(opt) for opt in opts)
        if deep:
            hit = _roots_hit("recursive read", operands[1:] if prog == "zip" else operands, base, _BULK_REASON)
            if hit:
                return hit
        hit = _first(_read_hit(word, base) for word in operands + values)
        script = _code_run(prog, args, base)[0] if prog in (".", "source") else None
        if script == _STDIN and not hit:  # `... | source /dev/stdin`: what the pipe prints is SHELL SOURCE, its hit the
            return _piped_source_hit(cmd, base, depth)  # cause of what that source runs, as for a shell (sg1 round 9)
        # `. FILE ARGS` / `source FILE ARGS`: SCRIPT FILE when _code_run shows FILE is a script; a sensitive FILE is not one.
        return _because(TRANSFORM if prog in _TRANSFORMS else _runs_code(script), hit)
    if prog in _LISTERS or prog in _SYNTAX:
        return None
    hit = _other_hit(args, base)
    if hit:  # a container's own operands (`docker run --env-file .env`) go into it; any other program's have no cause
        return _because(ELSEWHERE if prog in _ELSEWHERE else None, hit)
    # A container, remote or task runner may still run a reader.
    for i, arg in enumerate(args):
        name = posixpath.basename(arg).lower()
        name = _ALIASES.get(name, name)
        if name in _INNER and (prog in _FRONT_ENDS or any(word in _RUN_WORDS for word in args[:i])):
            inner = _command_hit(name, args[i + 1:], cmd, base, depth, bulk)
            # In a container or VM it runs elsewhere; through a local runner (npx, strace, `poetry run`) it is what it is.
            return _because(ELSEWHERE, inner) if prog in _ELSEWHERE else inner
    return None


def _document_hit(prog, cmd, base, depth):
    """Here-documents and here-strings: path literals always, commands when a shell (or `.`/`source` of /dev/stdin) receives
    them."""
    shell = _reads_source(prog, _program(cmd["words"])[1], base)
    for holder, expands in cmd["heredocs"]:
        body = "".join(holder)
        subs = [_shell_hit(inner, base, depth + 1) for inner in _substitutions(body)] if expands else []
        hit = _literal_hit(body, base) or _first(subs)
        if shell:  # a shell runs the body: SHELL SOURCE (_commands_hit `source`)
            if hit:
                _prompts_now()  # the cause is that of what it runs, none when it runs nothing found
                ran = _shell_hit(body, base, depth + 1, source=True)
                return _because(_cause(ran) if ran else None, hit)
            hit = _shell_hit(body, base, depth + 1, source=True)
            if hit:
                return hit
            continue
        if hit:
            # Text on a program's standard input. A script file's input is project code's (RUNS_CODE); the code itself on
            # stdin, or inline code's input, is inline code, which has no cause (sg1 round 7); a substitution the shell runs
            # in it is judged as any command, and one with no cause is that hit's. Any other program reads it as text
            # (`cat <<EOF`): no cause.
            unsearched = all(_cause(sub) for sub in subs if sub)
            form = _code_run(prog, _program(cmd["words"])[1], base)[0] if prog in _INTERPRETERS else None
            return _because(_runs_code(form) if unsearched else None, hit)  # SCRIPT FILE; anything else: no cause
    if shell:
        return _first(_shell_hit(text, base, depth + 1, source=True) for text in cmd["strings"])
    return None


def _only_printed(command):
    """Whether every credential file name in the text is a bare name that echo or printf prints.

    Nothing may consume the output or be substituted in, so the name is shown and no file is read.
    """
    if "<" in command or "`" in command or "$(" in command:
        return False
    printed = 0
    for cmd in _commands(command):
        if not isinstance(cmd, dict):
            continue
        prog, args = _program(cmd["words"])
        if prog not in _PRINTERS:
            if any(INLINE_NAME.search(word) for word in cmd["words"]):
                return False
            continue
        for arg in args:
            found = len(INLINE_NAME.findall(arg))
            if found and (cmd["feeds"] or any(char in arg for char in "/$~")):
                return False
            printed += found
    return printed == len(INLINE_NAME.findall(command))


def _read_names(args):
    """The variables a `read` command assigns."""
    names, i = [], 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg.startswith("-") and len(arg) > 1:
            i += arg[-1] in _READ_VALUES
        elif re.fullmatch(r"[A-Za-z_]\w*", arg):
            names.append(arg)
    return tuple(names) or ("REPLY",)


def _uses(names, words):
    pattern = re.compile(r"\$\{?(?:" + "|".join(re.escape(name) for name in names) + r")\b")
    return any(pattern.search(word) for word in words)


def _loop_hit(loops, prog, words):
    """The hit an open loop holds, once a command of the kind that reads files is given its variable."""
    for names, pending, readers in loops:
        if pending and names and (prog in _BULK if readers else prog not in _LISTERS) and _uses(names, words):
            return pending
    return None


def _keeps_state(prog, args, cmd):
    """Whether a command can be shown to leave the repository as a git query made beforehand found it."""
    if (cmd["subs"] or cmd["heredocs"] or cmd["strings"] or cmd["words"][:1] != [prog] or
            not all(_NULL_SINK.match(target) for target in cmd["outputs"])):
        return False
    if prog == "git":
        return _git_sub(args, "")[0] in _GIT_QUIET and not any(arg.startswith(("--output", "-c", "-C", "--git-dir",
                                                                                "--work-tree")) for arg in args)
    return prog in _QUIET


def _shell_hit(command, base="", depth=0, source=False):
    """The first hit in a shell command line, or None. It has a non-search cause (_NonSearch) only when every hit in the line
    has one: a hit with none anywhere (`python3 -c ...; grep TOKEN config/`) takes it away, though the target stays the first
    hit's, so the prompt reads as it did, and the line refuses in a worker (sg1). `source`: the line is SHELL SOURCE another
    shell runs (_commands_hit)."""
    inline = INLINE_NAME.search(command)
    if inline and not _only_printed(command):
        named = inline.group(0), _NAME_REASON
        if depth > 4:
            return named
        # The name may be in any command. Its cause is the one the commands, judged one by one, give: none when any hit has
        # none, or when a command names it and no look accounts for it (`grep -f .env x`, _commands_hit `named`).
        # The call prompts whatever that finds, so the owner's trust has nothing left to lift.
        _prompts_now()
        try:
            found = _commands_hit(command, base, depth, named=True, source=source)
        except Exception:  # what the name alone found stands, with no cause
            found = None
        return _because(_cause(found), named)
    if depth > 4:
        return "nested shell command", "command nesting is too deep to verify"
    return _commands_hit(command, base, depth, source=source)


def _prompts_now():
    """The call has a hit, so it prompts whatever else is found: the owner's trust has nothing left to lift, and the rest of
    the look (for a search or read, _shell_hit) notes no lift. classify puts the resolver back when the call is judged."""
    _SHELL.update(trust=None)


def _commands_hit(command, base, depth, named=False, source=False):
    """The first hit in the commands of a line, with a non-search cause only when every hit the look finds has one.

    `named`: the line names a sensitive file (_shell_hit). A command that names one where no look below finds a hit is a
    hit too, so the name is never left without a cause decided for it: one that only names or prints files (a lister, a
    printer, cd, a loop's word list) has NAMES_ONLY; any other (`grep -f .env x`, whose pattern file no look reads) none.

    `source`: the line is SHELL SOURCE another shell runs (a shell's -c line, eval, a here-document, here-string or pipe a
    shell reads as code, under any wrapper). Every command is judged as above, and a command whose look finds no hit is then
    checked word by word (_source_words_hit): a sensitive name in it (`set -- -e FILE`, `f=FILE`, `export F=FILE`,
    `echo FILE`) may reach a later command through a variable no look follows, so it has no cause and refuses in a worker,
    and so do names in command substitutions inside it (sg1 round 9). A look that finds a hit keeps its decision (round 6's
    script file: `bash -c 'python3 tools/x.py FILE'`)."""
    # Directory context: `cd -` target, pushd stack, enclosing subshells, and open loops with the
    # hit their word list or their file-name producer would cause once a command reads the loop
    # variable (any command but a lister for a word list, a reader for a `while read` loop).
    # `settled` holds while every command so far leaves the repository as a git query finds it now.
    previous, pushed, scopes, loops, settled = "$OLDPWD", [], [], [], True
    first, seen = None, False  # the first hit; whether the command being judged had one

    def stop(hit):
        """Whether `hit` ends the look: one with no non-search cause does, the line refuses in a worker whatever else it
        holds; one with a cause is kept (the first as `first`) and the look goes on for one with none."""
        nonlocal first, seen
        if not hit:
            return False
        seen = True
        if first is None:
            first = hit
        if _cause(hit) is None:
            return True
        _prompts_now()
        return False

    def tokens():
        return tuple(f"{form}" for names, _, _ in loops for name in names for form in (f"${{{name}}}", f"${name}"))

    for cmd in _commands(command):
        if cmd == "(":
            scopes.append((base, previous, list(pushed)))
            continue
        if cmd == ")":
            if scopes:
                base, previous, pushed = scopes.pop()
            continue
        seen = False
        prog, args = _program(cmd["words"])
        listed = None
        if (prog in _SHELLS or prog in (".", "source")) and _code_run(prog, args, base)[0] != _SCRIPT:
            # `bash <(echo LINE)`, `source <(printf ...)`: the process substitution's text is SHELL SOURCE (sg1 round 9)
            for match in _INPUT_SUB.finditer(command):
                body = command[match.end():_close_paren(command, match.end()) - 1]
                if stop(_process_source_hit(body, base, depth)):  # the cause is what that source runs
                    return _because(None, first)
        for inner in cmd["subs"]:
            if stop(_shell_hit(inner, base, depth + 1, source=source)):
                return _because(None, first)
            for item in _commands(inner) if any(pending for _, pending, _ in loops) else []:
                name, rest = _program(item["words"]) if isinstance(item, dict) else (None, [])
                hit = _loop_hit(loops, name, rest + item["inputs"]) if name else None
                if stop(_because(_program_cause(item["words"], base, depth, tokens()), hit)):
                    return _because(None, first)
            if prog in _BULK or prog == "for":
                listed = listed or _listing_hit(inner, base)
        # Names a listing produced reach the command as its words: its cause is what it does with such a file.
        if listed and prog != "for" and stop(_because(_program_cause(cmd["words"], base, depth), listed)):
            return _because(None, first)
        # What `< FILE` feeds a command is what it searches, reads or runs: its cause is the command's own with the file
        # given to it. A here-document is text, not a file (_document_hit judges it), nor is it the floor's (_floor_hit).
        fed = _first(_read_hit(word, base) for word in cmd["inputs"])
        fed = _because(_program_cause(cmd["words"], base, depth, stdin=True), fed)
        for hit in (fed, _document_hit(prog, cmd, base, depth), _floor_hit(cmd["words"], cmd, base, depth)):
            if stop(hit):
                return _because(None, first)
        if cmd["words"][:1] in (["while"], ["until"]):
            fed = None
            if prog == "read" and cmd["piped"]:
                fed = _producer_hit(cmd, base)  # the lines read are file names, as they are for xargs
            elif prog == "read":
                fed = _first(_listing_hit(command[match.end():_close_paren(command, match.end()) - 1], base)
                             for match in _PROCESS_SUB.finditer(command))
            loops.append((_read_names(args) if fed else (), fed, True))
        if prog == "cd":
            target = next((arg for arg in args if arg == "-" or not arg.startswith("-")), "~")
            flags = args[:args.index(target)] if target in args else args
            physical = any(flag.startswith("-") and "P" in flag for flag in flags)
            base, previous = previous if target == "-" else _cd(base, target, physical), base
        elif prog == "pushd":
            target = next((arg for arg in args if not arg.startswith(("-", "+"))), None)
            pushed.append(base)
            base = _cd(base, target) if target else "$DIRSTACK"
        elif prog == "popd":
            base = pushed.pop() if pushed else "$DIRSTACK"
        elif prog == "for":
            words = args[args.index("in") + 1:] if "in" in args else []
            loops.append((tuple(arg for arg in args[:1] if arg), listed or _first(_read_hit(word, base) for word in words), False))
        elif prog == "done":
            if loops:
                loops.pop()
        elif prog:
            cmd["settled"] = settled and not loops
            looped = _loop_hit(loops, prog, args + cmd["inputs"])
            if stop(_because(_program_cause(cmd["words"], base, depth, tokens()), looped)):
                return _because(None, first)
            if stop(_command_hit(prog, args, cmd, base, depth)):
                return _because(None, first)
        if source and not seen and stop(_because(None, _source_words_hit(cmd, base))):
            return _because(None, first)  # SHELL SOURCE: a name no look accounts for has no cause (sg1 round 9)
        if named and not seen and any(INLINE_NAME.search(word) for word in cmd["words"] + cmd["inputs"]):
            # A name no look above accounts for. Only a command that names or prints files without opening them gives a
            # cause; any other (`grep -f .env README.md`: the pattern file is read) refuses in a worker.
            names_only = prog in _LISTERS | _PRINTERS | _SYNTAX | _NAMES_ONLY_WORDS or prog is None
            hit = INLINE_NAME.search(" ".join(cmd["words"] + cmd["inputs"])).group(0), _NAME_REASON
            if stop(_because(NAMES_ONLY if names_only else None, hit)):
                return _because(None, first)
        settled = settled and bool(prog) and _keeps_state(prog, args, cmd)
    return first


def _glob_can_match(glob):
    """Whether a Grep file filter can select a credential file."""
    for choice in _brace_expand(glob):
        parts = [part for part in choice.split("/") if part]
        if choice.startswith("!") or not parts or not parts[-1].strip("*") or _named(parts):
            return True
        if _glob_hits(parts[-1], _FILE_SAMPLES):
            return True
    return False


def _filter_names(choice):
    """Whether a project-wide Grep filter names a credential file, outright or below wildcard folders only."""
    parts = [part for part in choice.split("/") if part]
    if _named(parts):
        return True
    last = _plain_name(parts[-1].lower()) if parts else ""
    return (not _GLOB.search(last) and (last == "local.json" or bool(SENSITIVE_NAME.match(last))) and
            all(_GLOB.search(part) for part in parts[:-1]))


def _tool_hit(tool, params):
    for key in TOOL_PATH_KEYS:
        value = params.get(key)
        if sensitive_path(value):
            return value, "credential-bearing or sensitive configuration path"
    if tool not in ("Grep", "grep"):
        return None  # Read names one file; Glob and find list names as `ls` does
    # Only a search that prints matching lines can show what a file holds. The default output is
    # the names of matching files (or counts), which is a listing, as Glob is.
    # Pi's lowercase `grep` has no such mode and always prints the lines.
    if tool == "Grep" and params.get("output_mode") != "content":
        return None
    root, glob, kind = params.get("path"), params.get("glob"), params.get("type")
    root = root.strip() if isinstance(root, str) and root.strip() else "."
    glob = glob.strip() if isinstance(glob, str) and glob.strip() else None
    # The tool's search does not follow links, and judges the project root by its filter alone (below).
    _SHELL["walk"] = Walk(False, (), root=False)
    why = _root_reason(root)
    if not why:
        return None
    if why == _ROOT_REASON:
        # A project-wide search prompts only when its filter names a credential file outright.
        named = glob and any(_filter_names(choice) for choice in _brace_expand(glob) if not choice.startswith("!"))
        return (f"{glob} under {root}", "search filter names a credential-bearing file") if named else None
    reason = f"search root is {_said(why)} and may include credential files"
    if glob:
        return (f"{glob} under {root}", reason) if _glob_can_match(glob) else None
    if isinstance(kind, str) and kind.lower() in _CODE_TYPES:
        return None
    return f"search of {root}", reason


def classify(tool, params, cwd=None, trust=None):
    """Judge one tool call. `cwd` is the working directory the call runs in, when the caller knows it.

    With no `cwd` the call is judged from this process's directory. A `cwd` that is not an absolute
    path is not replaced by it: the directory is unknown, and relative paths are judged on their own.

    `trust` is a resolver (lib/trust_lift.py) from a caller that prompts the owner. Without one the
    result is the same whatever records exist. With one, a prompt that only a name or a directory guess
    raised is lifted for paths in a tree the owner vouched for; see the module docstring.
    """
    params = params or {}
    saved = dict(_SHELL)
    placed = isinstance(cwd, str) and cwd.startswith("/")
    _SHELL.update(cwd=cwd if placed else None, unknown=cwd is not None and not placed, trust=trust,
                  walk=Walk(True, ()), memo={}, until=time.monotonic() + WALK_CALL_SECONDS,
                  text=str(params.get("command") or ""))
    hit = None
    try:
        if trust is not None:
            try:
                trust.begin(tool, params, cwd if placed else None)
            except Exception:   # a resolver that cannot start lifts nothing
                trust = _SHELL["trust"] = None
        if tool in _READ_TOOLS:
            hit = _tool_hit(tool, params)
        elif tool in _SHELL_TOOLS:
            command = str(params.get("command") or "")
            hit = _shell_hit(command.replace("\\", "/") if tool.lower() == "powershell" else command)
    except Exception:  # a guard that cannot parse the call must not wave it through
        hit = "unparsed tool call", "the call could not be analysed"
    finally:
        _SHELL.update(saved)
    if trust is not None:
        try:
            # What the call would have said without the owner's trust is asked of this same function, only if
            # the log needs it: the reason in the log is then the reason the prompt would have given.
            trust.finish(bool(hit), lambda: classify(tool, params, cwd).get("reason"))
        except Exception:       # a log that cannot be written changes nothing
            pass
    if hit:
        # "non_search": the closed-list cause (NON_SEARCH_CAUSES) the classifier gave a shell hit, or None. A worker's shell
        # call with none is refused (sg1 round 4): "worker_refuse". Read and Grep tool asks keep their ask.
        cause = _cause(hit) if tool in _SHELL_TOOLS else None
        return {"sensitive": True, "target": hit[0], "reason": hit[1], "non_search": cause,
                "worker_refuse": tool in _SHELL_TOOLS and cause is None}
    return {"sensitive": False}


def hook_response(event, cwd=None, trust=None):
    return response_for(classify(event.get("tool_name", ""), event.get("tool_input", {}), cwd or event.get("cwd"), trust))


# The safe tools by harness: (search, read). Claude's come from the tmux-pi-safe MCP server (claude/mcp/safe.mjs), pi's from
# agent/extensions/safe.ts. Both redact what they return and never ask.
SAFE_TOOL_NAMES = {"claude": ("mcp__tmux-pi-safe__safe_grep", "mcp__tmux-pi-safe__safe_read"),
                   "pi": ("safe_grep", "safe_read")}
# The shell searches and reads are _search_read's. Moving between directories, printing and the filters output is piped
# through are neither, and are no other command either. Anything else (an interpreter, `ls`, base64, find -exec, git show)
# is another command: the safe tools do not replace it, and the guidance says to run it separately (_route_parts).
_ROUTE_FILTERS = {"sort", "uniq", "cut", "wc", "tr", "column", "fold", "fmt"}
_ROUTE_QUIET = (_QUIET - {"cd", "pushd", "popd"}) | {":", "exit", "sleep"}
_ROUTE_READ_ARGS = ({"-n", "-c", "--lines", "--bytes"}, "nc")
# A word the shell still expands ($VAR, ${..}, $(..), `..`): what it stands for is not known here, so nothing is named from it.
_UNKNOWN_WORD = re.compile(r"\$[\w{(@*#?!$-]|`")
# git options before `grep` that change nothing about what it searches (-C and -c are read on their own).
_GIT_PLAIN = {"--no-pager", "-P", "--paginate"}


_REGEX_SPECIAL = set("\\.^$|?*+()[]{}")
_BRE_SWAPPED = set("|(){}+?")  # escaped in a basic regex to be special, bare in an extended one


def _as_safe_regex(pattern, syntax):
    """`pattern` as the extended (ripgrep) regex safe_grep runs: a basic regex (grep's and git grep's default) has its
    `\\|`, `\\(`, ... made bare and its bare `|`, `(`, ... escaped; fixed strings have every special character escaped."""
    if syntax == "extended":
        return pattern
    out, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        if syntax == "fixed":
            out.append("\\" + char if char in _REGEX_SPECIAL else char)
        elif char == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            out.append(nxt if nxt in _BRE_SWAPPED else char + nxt)
            i += 1
        else:
            out.append("\\" + char if char in _BRE_SWAPPED else char)
        i += 1
    return "".join(out)


def _route_grep(prog, args):
    """(pattern, paths, ignore_case, recursive) of one grep-family or `git grep` command, or None when there is no one
    pattern to name (patterns from a file, a pattern the shell still expands) or, for git grep, when it searches a revision.

    Short options are read as a cluster (`-rni`, `-A3`, `-efoo`): a letter that takes a value takes the rest of the word,
    or the next word. Several patterns (`-e a -e b`) are named as one alternation."""
    git = prog == "git"
    spec = _GIT_GREP_ARGS if git else _RG_ARGS if prog in _ALWAYS_RECURSIVE else _GREP_ARGS
    value_opts, value_letters, pattern_opts, pattern_letters = spec
    syntax = "extended" if prog in _ALWAYS_RECURSIVE or prog == "egrep" else "fixed" if prog == "fgrep" else "basic"
    modes = {"E": "extended", "P": "extended", "F": "fixed", "G": "basic"}
    if prog in _ALWAYS_RECURSIVE:
        modes = {"F": "fixed"}
    longs = {"--extended-regexp": "extended", "--perl-regexp": "extended", "--fixed-strings": "fixed",
             "--basic-regexp": "basic"}
    patterns, operands, ignore, i, after, recursive, dash = [], [], False, 0, False, False, None
    while i < len(args):
        arg = args[i]
        i += 1
        if after or arg == "-" or not arg.startswith("-"):
            operands.append(arg)
        elif arg == "--":
            after, dash = True, len(operands)
        elif arg.startswith("--"):
            name, equals, value = arg.partition("=")
            ignore = ignore or name == "--ignore-case"
            recursive = recursive or name in ("--recursive", "--dereference-recursive") or (
                name == "--directories" and (value if equals else args[i] if i < len(args) else "") == "recurse")
            syntax = longs.get(name, syntax) if not (prog in _ALWAYS_RECURSIVE and name != "--fixed-strings") else syntax
            if name in pattern_opts:
                if name == "--file":
                    return None  # patterns from a file: there is no one pattern to name
                patterns.append(value if equals else args[i] if i < len(args) else "")
                i += 0 if equals else 1
            elif name in value_opts and not equals:
                i += 1
        else:
            for at in range(1, len(arg)):
                letter, rest = arg[at], arg[at + 1:]
                if letter in pattern_letters:
                    if letter == "f":
                        return None
                    patterns.append(rest if rest else args[i] if i < len(args) else "")
                    i += 0 if rest else 1
                    break
                if letter in value_letters:
                    recursive = recursive or (letter == "d" and not git and prog not in _ALWAYS_RECURSIVE and
                                              (rest or (args[i] if i < len(args) else "")) == "recurse")
                    i += 0 if rest else 1
                    break
                ignore = ignore or letter == "i"
                recursive = recursive or (letter in "rR" and not git and prog not in _ALWAYS_RECURSIVE)
                syntax = modes.get(letter, syntax)
    if not patterns:
        if not operands:
            return None
        patterns, operands = [operands[0]], operands[1:]
        dash = None if dash is None else dash - 1
    if git and dash:
        return None  # `git grep x REV -- path`: the words before `--` are revisions, a tree the safe tools cannot search
    if any(_UNKNOWN_WORD.search(p) for p in patterns):
        return None
    if any("\n" in p for p in patterns):
        patterns = [part for p in patterns for part in p.split("\n")]  # grep reads each line as its own pattern
    pattern = "|".join(_as_safe_regex(p, syntax) for p in patterns)
    return pattern, operands, ignore, recursive or git or prog in _ALWAYS_RECURSIVE


def _moved(base, target):
    """Where `target` is from directory `base` (relative to the call's directory, "" for it), or None when unknown."""
    if base is None or target is None or target == "-" or _UNKNOWN_WORD.search(target):
        return None
    if target == "~" or target.startswith(("/", "~/")):
        return target
    if target.startswith("~"):
        return None  # ~user: whose home is not known here
    return posixpath.join(base, target) if base else target


def _place(path, base, cwd):
    """`path` as the shell opens it from `base`, named so both safe tools open the same file: absolute from the call's
    directory when it is known (the safe tools resolve a relative path from their own directory, which a worker's shell
    may have left), as given otherwise. None when it is not known.

    The kernel resolves `..` against the directory reached so far, a symlink's target and not the link (`link/../x` is
    x beside the link's target): such a path is named by resolving its part up to the last `..` (realpath), never by
    dropping `link/..` as text, which would name a different file. One that cannot be resolved here (relative with the
    call's directory unknown, or under `~`) is None: the guidance is then generic."""
    where = _moved(base, path)
    dotted = where is not None and ".." in where.split("/")
    if where is None or where.startswith("~"):
        return None if dotted else where  # the safe tools expand `~/` as the shell does
    if not where.startswith("/") and cwd:
        where = posixpath.join(cwd, where)
    if not dotted:
        return posixpath.normpath(where)
    if not where.startswith("/"):
        return None
    parts = where.split("/")
    last = len(parts) - 1 - parts[::-1].index("..")
    return posixpath.normpath(posixpath.join(os.path.realpath("/".join(parts[:last + 1]) or "/"), *parts[last + 1:]))


def _search_targets(words, base, cwd, globs=True):
    """[(path, glob)] for the file operands of a search, after the shell's brace and glob expansion: a glob in the last
    part becomes safe_grep's `glob` under its directory; one anywhere else, or (for a read, `globs` false) at all, cannot
    be named, nor can a word the shell still expands. None when any operand cannot be named."""
    out = []
    for word in words:
        for choice in _brace_expand(word):
            if choice == "-":
                return None  # standard input among the files
            head, tail = posixpath.split(choice)
            if _GLOB.search(head) or (_GLOB.search(tail) and (not globs or "{" in tail)):
                return None
            where = _place((head or ".") if _GLOB.search(tail) else choice, base, cwd)
            if where is None:
                return None
            out.append((where, tail if _GLOB.search(tail) else None))
    return out


def _dash_c(args):
    """The command line a shell's `-c` (alone or in a cluster: `-lc`, `-ec`) hands it, or None when it has none."""
    at = _dash_c_at(args)
    return None if at is None else args[at]


def _dash_c_at(args):
    """The index in `args` of the command line a shell's `-c` hands it (the words after it are $0, $1, ...), or None.

    Only the shell's own options are read: the first operand (a script file) or `--` ends them, and what follows belongs to
    the script (`bash tools/x.sh -c x`: the script's -c, sg1 round 6)."""
    skip = False
    for i, arg in enumerate(args[:-1]):
        if skip:
            skip = False
        elif arg == "--" or not arg.startswith(("-", "+")) or arg in ("-", "+"):
            return None
        elif re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", arg):  # `-c`, `-lc`, `-cx`: the first word after it is the line
            return i + 1
        elif arg in _SHELL_RUN["values"]:
            skip = True
    return None


def _git_worktree(args):
    """Whether `git grep ARGS` searches the work tree's files: not the index (`--cached`), nor files git does not track as
    a filesystem walk the safe tools match one to one (`--no-index`). A revision among its operands is _route_parts'."""
    for arg in args:
        if arg == "--":
            return True
        if arg in ("--cached", "--no-index"):
            return False
    return True


def _route_parts(command, cwd=None, base=""):
    """The shell searches and reads in a command (_search_read), and whether it holds any other command.

    This only decides which safe calls the guidance names; whether the call is refused is the classifier's (classify's
    "worker_refuse": no non-search cause, sg1 round 4). Each search or read is {"calls": [...]}: the safe calls that do what it does, ("grep", pattern, path,
    glob, ignore_case) or ("read", path); None when it cannot be named faithfully (patterns from a file, a revision or the
    index, a target the shell computes, xargs, a search inside a substitution). `base` is where the command starts,
    relative to the call's directory ("" for it, None when unknown): a `sh -c` script starts where its shell does."""
    parts, other, stack, scopes, before = [], False, [], [], None
    for item in _commands(command):
        if item in ("(", ")"):
            if item == "(":
                scopes.append((base, list(stack)))
            elif scopes:
                base, stack = scopes.pop()
            before = None
            continue
        words = item["words"]
        prog, args = _program(words)
        fed, before = (before if item["piped"] else None), None
        heredoc_subs = [inner for holder, expands in item["heredocs"] if expands for inner in _substitutions("".join(holder))]
        for inner in item["subs"] + heredoc_subs:
            if _route_parts(inner, cwd, None)[0]:
                parts.append({"calls": None})  # a search in `$(...)` runs too, in a word or an expanding here-document
        if prog is None:
            other = other or bool(words or item["inputs"] or item["outputs"])
            continue
        dynamic = bool(item["subs"] or item["heredocs"])
        script = " ".join(args) if prog == "eval" else _dash_c(args) if prog in _SHELLS else None
        if script is not None:
            # `sh -c LINE` / `eval WORDS` run LINE as this shell would: its searches are named as if typed here.
            inner, more = _route_parts(script, cwd, base)
            parts += inner
            other = other or more or not inner
        elif prog == "xargs":
            found = command_wrappers.unwrap("xargs", args)
            sub, rest = _program(found[1]) if isinstance(found, tuple) and found[0] == "words" else (None, [])
            if _search_read(sub, rest) or (sub and _route_parts(" ".join(map(shlex.quote, found[1])), cwd, None)[0]):
                parts.append({"calls": None})  # it searches or reads files its input names (`xargs sh -c 'grep ...'` too)
            else:
                other = True
        elif prog == "git":
            rest, where, plain = list(args), base, True
            while rest and rest[0].startswith("-"):
                option = rest.pop(0)
                if option == "-C" and rest:
                    where = _moved(where, rest.pop(0))
                elif option == "-c" and rest:
                    rest.pop(0)
                elif option not in _GIT_PLAIN:
                    plain = False  # --git-dir, --work-tree, ...: what it searches is not read here
            if not rest or rest[0] != "grep":
                other = True
                continue
            found = _route_grep("git", rest[1:]) if plain and not dynamic and _git_worktree(rest[1:]) else None
            targets = None
            if found and not any(_GLOB.search(p) or p.startswith(":") for p in found[1]):
                # pathspecs are relative to where git runs; a glob or magic pathspec is git's own matching, not the shell's
                targets = _search_targets(found[1], where, cwd, globs=False) if found[1] else (
                    None if where is None else [(_place(".", where, cwd), None)])
            if targets and not all(os.path.exists(os.path.expanduser(path)) for path, _ in targets):
                targets = None  # an operand that is no file here is a revision (`git grep x HEAD`): a tree, not a file
            parts.append({"calls": [("grep", found[0], path, None, found[2]) for path, _ in targets] if targets else None})
        elif prog in _GREPS:
            found = None if dynamic else _route_grep(prog, args)
            if found is None:
                parts.append({"calls": None})
                continue
            pattern, operands, ignore, recursive = found
            if operands:
                targets = _search_targets(operands, base, cwd)
            elif recursive and prog not in _ALWAYS_RECURSIVE:
                targets = [(_place(".", base, cwd), None)]  # grep -r with no file searches its directory, whatever stdin is
            elif item["inputs"]:
                targets = _search_targets(item["inputs"][-1:], base, cwd, globs=False)  # the last `<` is what it reads
            elif item["piped"]:
                if not fed:
                    continue  # it filters another command's output: no file is searched
                targets = None if fed["calls"] is None else [(call[1], None) for call in fed["calls"]]
                fed["calls"] = None if targets is None else []  # the file it reads is searched here, not read
            elif item["strings"] or prog not in _ALWAYS_RECURSIVE:
                continue  # it searches a here-string or the terminal
            else:
                targets = [(_place(".", base, cwd), None)]  # rg, ag, ... with no path search their directory
            if targets and any(path is None for path, _ in targets):
                targets = None
            parts.append({"calls": [("grep", pattern, path, glob, ignore) for path, glob in targets] if targets is not None
                          else None})
        elif _search_read(prog, args):  # one of _ROUTE_READERS, or `sed -n`
            if prog in ("sed", "gsed"):
                _, before_dash, after_dash, _, has_script = _split_args(args, *_SCRIPT_ARGS)
                files = (before_dash if has_script else before_dash[1:]) + (after_dash or [])
            else:
                # head and tail take a count after -n / -c; cat -n, nl and the rest take no value
                _, before_dash, after_dash, _, _ = _split_args(args, *(_ROUTE_READ_ARGS if prog in ("head", "tail") else ()))
                files = before_dash + (after_dash or [])
            files = [f for f in files if f != "-"] or list(item["inputs"][-1:])
            if not files:
                continue  # it reads its standard input: a filter
            targets = None if dynamic else _search_targets(files, base, cwd, globs=False)
            entry = {"calls": None if targets is None else [("read", path) for path, _ in targets]}
            parts.append(entry)
            before = entry
        elif prog in ("cd", "pushd", "popd"):
            if prog == "popd":
                base = stack.pop() if stack else None
                continue
            target = [a for a in args if a == "-" or not a.startswith("-")]
            if prog == "pushd":
                stack.append(base)
            # The shell's `cd` is logical: `cd link/..` is back where it started, as text. Its later relative paths are
            # opened from there by the kernel (_place resolves their own `..`). `-P` is physical: not named here.
            flags = args[:args.index(target[0])] if target else args
            physical = any(a.startswith("-") and a != "-" and "P" in a for a in flags)
            base = None if dynamic or physical else _moved(base, target[0] if target else ("~" if prog == "cd" else None))
            if base and ".." in base.split("/"):
                base = None if base.startswith("~") else posixpath.normpath(base)
        elif prog in ("find", "fd", "fdfind") and any(_route_parts(run, cwd, None)[0] for run in _execs(prog, args)):
            parts.append({"calls": None})  # it searches or reads each file it finds (find -exec grep, fd -x cat)
        elif prog not in _ROUTE_FILTERS and prog not in _ROUTE_QUIET:
            other = True
    return parts, other


def _execs(prog, args):
    """The commands `find -exec ...` / `fd -x ...` run, as shell lines, for _route_parts."""
    runs, i = [], 0
    while i < len(args):
        if args[i] in (_FIND_EXEC if prog == "find" else _FD_EXEC):
            end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args)) if prog == "find" else len(args)
            runs.append(" ".join(shlex.quote(word) for word in args[i + 1:end]))
            i = end
        i += 1
    return runs


def _generic(harness):
    grep, read = SAFE_TOOL_NAMES.get(harness, SAFE_TOOL_NAMES["pi"])
    return f"Search with {grep} and read with {read} instead of a shell search or read."


def safe_route(tool, params, harness, cwd=None, refused=False):
    """What to use instead of a shell call a worker is refused (classify's "worker_refuse"), as one instruction; None when
    `tool` is not a shell tool. It only names calls: it never decides whether the call is refused.

    Each search and read the shell would run is named with the safe call that searches or reads the same files (one call
    per path); when one cannot be named faithfully the safe tools are named generically, and when none can, that is all it
    says. Any other command in the call is to be run separately. `harness` is "claude" or "pi"; `cwd`, the call's
    directory, makes the named paths absolute. `refused`: the text is a worker's refusal; when the command holds no search
    or read (a copy, an edit, an upload, a code run) it also says to ask Main instead of retrying (NOT_A_SEARCH).

    Never raises: a hook that crashed would answer nothing, and Claude runs a call its hook did not answer. A command it
    cannot read gets the generic guidance."""
    if tool not in _SHELL_TOOLS:
        return None
    try:
        return _safe_route(params, harness, cwd if isinstance(cwd, str) and cwd.startswith("/") else None, refused)
    except Exception:
        return _generic(harness)


def _safe_route(params, harness, cwd, refused=False):
    grep, read = SAFE_TOOL_NAMES.get(harness, SAFE_TOOL_NAMES["pi"])
    parts, other = _route_parts(str((params or {}).get("command") or ""), cwd)
    calls = []
    for part in parts:
        for call in part["calls"] or []:
            if call[0] == "grep":
                _, pattern, path, glob, ignore = call
                named = (f"{grep} with pattern {json.dumps(pattern)}" + (f" and path {json.dumps(path)}" if path else "")
                         + (f" and glob {json.dumps(glob)}" if glob else "") + (" and ignore_case true" if ignore else ""))
            else:
                named = f"{read} with path {json.dumps(call[1])}"
            if named not in calls:
                calls.append(named)
    unnamed = any(part["calls"] is None for part in parts)
    if calls:
        text = "Use " + "; ".join(calls) + (" (one call each)" if len(calls) > 1 else "") + " instead of the shell."
        text += (f" For the searches and reads it does not name, search with {grep} and read with {read}." if unnamed
                 else f" Search with {grep}, read with {read}.")
    else:
        text = _generic(harness)
    if not parts:  # no shell search or read in it: a copy, move, edit, upload or code run the safe tools do not do (round 5)
        return text + (" " + NOT_A_SEARCH if refused else "")
    return text + (" Run the other commands in a call of their own, without the search or read." if other else "")


# What a worker refused on a command that is not a search or read is told to do instead (sg1 round 5, F3).
NOT_A_SEARCH = "If it is not a search or read, ask Main for it (tp-agent request) instead of retrying the command."


def worker_task(env=None):
    """Whether this process runs in a worker task: bin/tp-spawn exports TP_TASK_DIR to workers only, never to main."""
    return bool((os.environ if env is None else env).get("TP_TASK_DIR"))


def response_for(result, route=None, refuse=False):
    """The PreToolUse `ask` for a classification, or None when it is not sensitive.

    `route` (safe_route) is the safe call to use instead; it is named in the reason. With `refuse` (a worker task) a
    shell classification with no non-search cause ("worker_refuse", sg1 round 4) is a deny instead, naming `route` or the
    safe tools: nobody answers a worker's prompt. An ask with a cause, and a Read or Grep tool ask, stays an ask."""
    if not result["sensitive"]:
        return None
    if refuse and result.get("worker_refuse"):
        decision = "deny"
        reason = f"tmux-pi refuses this shell read in a worker task: it may expose {result['target']}. {route or _generic('claude')}"
    else:
        decision = "ask"
        reason = f"Read may expose {result['target']}. Authorize this specific tool call only if its scope is needed."
        reason += f" {route}" if route else ""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision,
                                   "permissionDecisionReason": reason}}
