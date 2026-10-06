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
"""
import fnmatch
import json
import os
from pathlib import Path
import posixpath
import re
import select
import signal
import subprocess
import tempfile
import time

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
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
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
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}
_SYNTAX = {"done", "fi", "esac", "}", "in"}
_WRAPPERS = {"sudo", "doas", "env", "command", "exec", "builtin", "nohup", "nice", "ionice", "timeout", "gtimeout",
             "stdbuf", "caffeinate", "noglob", "nocorrect"}
_WRAPPER_VALUES = {"-u", "-g", "-n", "-k", "-s"}
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
_BULK = _READERS | _GREPS | _SCRIPT_FIRST | _SHELLS | _INTERPRETERS
_INNER = _BULK - {".", "source"}
# Programs whose arguments are another command line; elsewhere a reader name only counts after run/exec/--.
_FRONT_ENDS = {"docker", "podman", "nerdctl", "kubectl", "oc", "watch", "parallel", "npx", "bunx", "pnpx", "uvx",
               "chroot", "flock", "script", "su", "runuser", "arch", "setsid", "entr", "strace", "dtruss", "xcrun",
               "sandbox-exec", "firejail", "lima", "vagrant"}
_RUN_WORDS = {"run", "exec", "--"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_FD_EXEC = {"-x", "-X", "--exec", "--exec-batch"}
_XARGS_VALUES = {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "-J", "-R", "-S", "--max-args", "--max-procs",
                 "--delimiter", "--arg-file", "--max-lines", "--max-chars"}
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
_GIT_ENV = {"LC_ALL": "C", "LANG": "C", "GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
# Variables that would point the query at a repository other than the working directory's, or give
# the real command settings and a diff program that the query, run without them, would not see.
_GIT_ELSEWHERE = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                  "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_CONFIG", "GIT_CONFIG_COUNT",
                  "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
                  "GIT_EXTERNAL_DIFF")
_RAW_ENTRY = re.compile(r"^:+((?:[0-7]{6} )+)(?:[0-9a-f]+ )+[A-Z]+[0-9]*$")
_GITLINK = "160000"
_SUBMODULE_REASON = "patch output touches a submodule and may include the files inside it"
_SUBMODULE_CONFIG_REASON = "diff.submodule=diff prints the files inside submodules"
_DRIVER_REASON = "a configured diff program or text conversion decides what the patch prints"
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
_GIT_GREP_ARGS = ({"-A", "-B", "-C", "-O", "-m", "--max-depth", "--max-count", "--threads", "--open-files-in-pager"},
                  "ABCOm", {"-e", "-f"}, "ef")
_SCRIPT_ARGS = ({"-v", "-F", "--arg", "--argjson", "--slurpfile", "--rawfile", "--indent", "-L", "-i"}, "vFL",
                {"-e", "--expression", "-f", "--file", "--from-file"}, "ef")
_NAME_REASON = "sensitive path in shell command"
_BULK_REASON = "dynamic file selection may include credential files"
_GLOB_REASON = "a glob that can match credential files"
_GLOB_PROMPTS = {_CREDENTIAL_DIR, "a credential-bearing path", _GLOB_REASON, "a home directory"}

# The working directory of the shell or tool being judged, when the caller knows it.
# `unknown` is set when the caller named a directory that cannot be placed.
_SHELL = {"cwd": None, "unknown": False}


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


def _named(parts):
    return _parts_hit(parts) or any(part in (".git", ".config") or SENSITIVE_NAME.match(_plain_name(part))
                                    for part in parts)


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


def _absolute(path):
    """The path as the shell would resolve it; None when that depends on run-time state."""
    path = _expand(path)
    if "$" in path or "`" in path or path.startswith("~"):
        return None
    if not path.startswith("/"):
        cwd = _shell_cwd()
        if cwd is None:
            return None
        path = posixpath.join(cwd, path)
    return "/" + posixpath.normpath(path).lstrip("/")


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
    if (SENSITIVE_NAME if weak else STRONG_NAME).match(_plain_name(path.name)) or _parts_hit(path.parts):
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
    return str(resolved) != absolute and (
        (SENSITIVE_NAME if weak else _STRONG_OR_KEY).match(_plain_name(resolved.name)) is not None or
        _parts_hit(resolved.parts))


def sensitive_path(value):
    return _sensitive(value)


def _join(base, path):
    for prefix in ("${PWD}", "$PWD"):
        if path == prefix or path.startswith(prefix + "/"):
            path = "." + path[len(prefix):]
    if not base or path.startswith(("/", "~", "$")):
        return path
    return posixpath.join(base, path)


def _cd(base, target):
    joined = _join(base, target)
    if "$" in joined or "`" in joined or joined.startswith("~"):
        return joined
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


def _layout(parts, tree=True, anchor=None, pathspec=False):
    """Judge a path relative to a project root by this repository's layout."""
    if _named(parts):
        return "a credential-bearing path"
    globs = [index for index, part in enumerate(parts) if _GLOB.search(part)]
    if not globs:
        return _CREDENTIAL_DIR if parts[-1].lower() in CREDENTIAL_DIRS else None
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
    if not parts:
        return "the repository root"
    return _layout(parts, tree, anchor, pathspec)


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


def _program(words):
    i, wrapped = 0, False
    while i < len(words):
        word = words[i]
        name = posixpath.basename(word).lower()
        if _ASSIGN.match(word) or word in _KEYWORDS:
            i += 1
        elif name in _WRAPPERS:
            if name == "command" and words[i + 1:i + 2] in (["-v"], ["-V"]):
                return None, []  # looks a command up without running it
            wrapped, i = True, i + 1
        elif wrapped and word in _WRAPPER_VALUES:
            i += 2
        elif wrapped and (word.startswith("-") or _DURATION.match(word)):
            i += 1
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
                return piece, f"glob over {why} may include credential files"
    return None


def _weak_file(piece, interpreter):
    """A path token, not a prose word, whose name says it stores credentials."""
    name = _plain_name(posixpath.basename(piece)).lower()
    if not _WEAK_NAME.match(name):
        return False
    stem, dot, extension = name.rpartition(".")
    if dot and stem:
        return bool(_DATA_EXT.match(extension)) and extension not in _CODE_EXT
    if name.startswith("."):
        return True
    return name.endswith("credentials") and (interpreter or "/" in piece or name != "credentials")


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
                    _weak_file(piece, interpreter)):
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


def _roots_hit(label, roots, base, reason, pathspec=False):
    for root in roots:
        why = _root_reason(root, base, True, pathspec)
        if why:
            return f"{label} of {root}", reason.format(why=why)
    return None


def _not_a_path(word, base):
    """A git operand that names nothing on disk is read as a revision."""
    if _GLOB.search(word) or word.startswith(":"):
        return False
    full = _absolute(_join(base, word))
    return full is not None and not os.path.lexists(full)


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


def _search_hit(prog, args, cmd, base, bulk, git=False, listing=False):
    """A search command. `bulk` means file names are appended at run time; `listing` means its output
    feeds a reader, so even a names-only search is judged by its roots."""
    spec = _GIT_GREP_ARGS if git else _RG_ARGS if prog in _ALWAYS_RECURSIVE else _GREP_ARGS
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
        revision = next((root for root in roots if _REVISION.match(root) or _not_a_path(root, base)), None)
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
        if not narrowed or _first(_root_reason(root, base) for root in roots or ["."]):
            return "broad hidden/unignored search", "search may include credential files"
    if not recursive or (names_only and not listing) or narrowed:
        return None  # a names-only search lists files as find does
    if not roots:
        if bulk:
            return None  # the appended file names are judged by their producer
        if prog in _ALWAYS_RECURSIVE and cmd["piped"] and not names_only:
            return None  # searches the piped input, not the tree
        roots = ["."]
    return _roots_hit("recursive search", roots, base, "search root is {why} and may include credential files", git)


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


def _listed_hit(label, query, base, unproven):
    """Judge an unscoped patch by the paths it covers; `unproven` stands when they cannot be listed.

    A patch that covers no credential path still prompts when it may print more than the paths listed:
    when it touches a submodule, and when the settings in force print submodule files or hand the
    printing to a configured program. The two queries share one time allowance.
    """
    deadline = time.monotonic() + _GIT_TIMEOUT
    listed = _git_names(query, base, deadline) if query else None
    if listed is None:
        return unproven
    names, links, folder = listed
    folder = os.path.realpath(folder)
    above = [part for part in (_top(folder) or folder).split("/") if part]

    def shown(name):
        return name if len(name) <= 200 and _PATHISH.match(name) else "a credential-bearing path"

    for name in names:
        parts = [part for part in name.split("/") if part]
        if parts and (SENSITIVE_NAME.match(_plain_name(parts[-1])) or _parts_hit(above + parts)):
            return f"{label} of {shown(name)}", "patch output covers a credential-bearing path"
    if links:
        link = links[0] if len(links[0]) <= 200 and _PATHISH.match(links[0]) else "a submodule"
        return f"{label} of {link}", _SUBMODULE_REASON
    if not names:
        return None  # nothing is printed, whatever would have printed it
    why = _config_reason(base, deadline)
    if why is None:
        return unproven
    return (f"{label} patch output", why) if why else None


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


def _command_hit(prog, args, cmd, base, depth, bulk=False):
    if prog == "xargs":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 2 if args[i] in _XARGS_VALUES else 1
        sub, rest = _program(args[i:])
        if sub is None:
            return None
        return (_producer_hit(cmd, base) if sub in _BULK else None) or _command_hit(sub, rest, cmd, base, depth, True)
    if prog == "find":
        roots = _find_roots(args)
        for i, arg in enumerate(args):
            if arg in _FIND_EXEC:
                end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args))
                sub, rest = _program(args[i + 1:end])
                hit = (_roots_hit("bulk read", roots, base, _BULK_REASON) if sub in _BULK else None) or (
                    _command_hit(sub, rest, cmd, base, depth, True) if sub else None)
                if hit:
                    return hit
        return None
    if prog in ("fd", "fdfind"):
        for i, arg in enumerate(args):
            if arg in _FD_EXEC and _program(args[i + 1:])[0] in _BULK:
                return "bulk shell read", _BULK_REASON
        return None
    if prog == "git":
        sub, rest, base = _git_sub(args, base)
        if sub == "grep":
            return _search_hit(prog, rest, cmd, base, bulk, git=True)
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
        if (hit and hit[1] == _UNSCOPED_REASON and sub in ("diff", "log") and not after and
                _queried(args, rest, cmd, depth, bulk)):
            return _listed_hit(f"git {sub}", _patch_query(sub, [arg for arg in rest if arg != "--"], base), base, hit)
        return hit
    if prog in _GREPS:
        return _search_hit(prog, args, cmd, base, bulk)
    if prog in _SHELLS:
        for i, arg in enumerate(args[:-1]):
            if re.fullmatch(r"-[A-Za-z]*c", arg):
                return _shell_hit(args[i + 1], base, depth + 1)
        return _other_hit(args, base, True)
    if prog == "eval":
        return _shell_hit(" ".join(args), base, depth + 1)
    if prog in _INTERPRETERS:
        rest, skip = [], False
        for i, arg in enumerate(args):
            if skip:
                skip = False
            elif arg in ("--eval", "--print") or re.fullmatch(r"-[A-Za-z]*[cep]", arg):
                hit = _literal_hit(args[i + 1], base) if i + 1 < len(args) else None
                if hit:
                    return hit
                skip = True
            elif arg == "-m":
                skip = True
            else:
                rest.append(arg)
        return _other_hit(rest, base, True)
    if prog in ("curl", "wget"):
        files = [name for arg in args for name in re.findall(r"@([^;,\s]+)", arg)]
        for i, arg in enumerate(args):
            name, equals, value = arg.partition("=")
            if name in _CURL_FILES:
                files.append(value if equals else "".join(args[i + 1:i + 2]))
        return _first(_read_hit(name, base) for name in files if name)
    if prog == "ssh":
        _, before, after, values, _ = _split_args(args, *_SSH_ARGS)
        remote = (before + (after or []))[1:]
        return _first(_read_hit(word, base) for word in values) or (
            _shell_hit(" ".join(remote), "", depth + 1) if remote else None)
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
        return _first(_read_hit(word, base) for word in operands + values)
    if prog in _LISTERS or prog in _SYNTAX:
        return None
    hit = _other_hit(args, base)
    if hit:
        return hit
    # A container, remote or task runner may still run a reader.
    for i, arg in enumerate(args):
        name = posixpath.basename(arg).lower()
        name = _ALIASES.get(name, name)
        if name in _INNER and (prog in _FRONT_ENDS or any(word in _RUN_WORDS for word in args[:i])):
            return _command_hit(name, args[i + 1:], cmd, base, depth, bulk)
    return None


def _document_hit(prog, cmd, base, depth):
    """Here-documents and here-strings: path literals always, commands when a shell receives them."""
    for holder, expands in cmd["heredocs"]:
        body = "".join(holder)
        hit = _literal_hit(body, base) or (
            _first(_shell_hit(inner, base, depth + 1) for inner in _substitutions(body)) if expands else None) or (
            _shell_hit(body, base, depth + 1) if prog in _SHELLS else None)
        if hit:
            return hit
    if prog in _SHELLS:
        return _first(_shell_hit(text, base, depth + 1) for text in cmd["strings"])
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


def _shell_hit(command, base="", depth=0):
    inline = INLINE_NAME.search(command)
    if inline and not _only_printed(command):
        return inline.group(0), _NAME_REASON
    if depth > 4:
        return "nested shell command", "command nesting is too deep to verify"
    # Directory context: `cd -` target, pushd stack, enclosing subshells, and open loops with the
    # hit their word list or their file-name producer would cause once a command reads the loop
    # variable (any command but a lister for a word list, a reader for a `while read` loop).
    # `settled` holds while every command so far leaves the repository as a git query finds it now.
    previous, pushed, scopes, loops, settled = "$OLDPWD", [], [], [], True
    for cmd in _commands(command):
        if cmd == "(":
            scopes.append((base, previous, list(pushed)))
            continue
        if cmd == ")":
            if scopes:
                base, previous, pushed = scopes.pop()
            continue
        prog, args = _program(cmd["words"])
        listed = None
        for inner in cmd["subs"]:
            hit = _shell_hit(inner, base, depth + 1)
            if hit:
                return hit
            for item in _commands(inner) if any(pending for _, pending, _ in loops) else []:
                name, rest = _program(item["words"]) if isinstance(item, dict) else (None, [])
                hit = _loop_hit(loops, name, rest + item["inputs"]) if name else None
                if hit:
                    return hit
            if prog in _BULK or prog == "for":
                listed = listed or _listing_hit(inner, base)
        if listed and prog != "for":
            return listed
        hit = _first(_read_hit(word, base) for word in cmd["inputs"]) or _document_hit(prog, cmd, base, depth)
        if hit:
            return hit
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
            base, previous = previous if target == "-" else _cd(base, target), base
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
            hit = _loop_hit(loops, prog, args + cmd["inputs"]) or _command_hit(prog, args, cmd, base, depth)
            if hit:
                return hit
        settled = settled and bool(prog) and _keeps_state(prog, args, cmd)
    return None


def _glob_can_match(glob):
    """Whether a Grep file filter can select a credential file."""
    for choice in _brace_expand(glob):
        parts = [part for part in choice.split("/") if part]
        if choice.startswith("!") or not parts or not parts[-1].strip("*") or _named(parts):
            return True
        if _glob_hits(parts[-1], _FILE_SAMPLES):
            return True
    return False


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
    why = _root_reason(root)
    if not why:
        return None
    if why == "the repository root":
        # A project-wide search prompts only when its filter names a credential file outright.
        named = glob and any(_named([part for part in choice.lstrip("!").split("/") if part])
                             for choice in _brace_expand(glob) if not choice.startswith("!"))
        return (f"{glob} under {root}", "search filter names a credential-bearing file") if named else None
    reason = f"search root is {why} and may include credential files"
    if glob:
        return (f"{glob} under {root}", reason) if _glob_can_match(glob) else None
    if isinstance(kind, str) and kind.lower() in _CODE_TYPES:
        return None
    return f"search of {root}", reason


def classify(tool, params, cwd=None):
    """Judge one tool call. `cwd` is the working directory the call runs in, when the caller knows it.

    With no `cwd` the call is judged from this process's directory. A `cwd` that is not an absolute
    path is not replaced by it: the directory is unknown, and relative paths are judged on their own.
    """
    params = params or {}
    saved = dict(_SHELL)
    placed = isinstance(cwd, str) and cwd.startswith("/")
    _SHELL.update(cwd=cwd if placed else None, unknown=cwd is not None and not placed)
    try:
        hit = None
        if tool in _READ_TOOLS:
            hit = _tool_hit(tool, params)
        elif tool in _SHELL_TOOLS:
            command = str(params.get("command") or "")
            hit = _shell_hit(command.replace("\\", "/") if tool.lower() == "powershell" else command)
    except Exception:  # a guard that cannot parse the call must not wave it through
        hit = "unparsed tool call", "the call could not be analysed"
    finally:
        _SHELL.update(saved)
    if hit:
        return {"sensitive": True, "target": hit[0], "reason": hit[1]}
    return {"sensitive": False}


def hook_response(event, cwd=None):
    result = classify(event.get("tool_name", ""), event.get("tool_input", {}), cwd or event.get("cwd"))
    if not result["sensitive"]:
        return None
    reason = f"Read may expose {result['target']}. Authorize this specific tool call only if its scope is needed."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}
