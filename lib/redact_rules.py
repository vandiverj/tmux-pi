"""Deterministic secret rules for lib/redact.py. Formats follow gitleaks' patterns; each has a test."""
import re

# (kind, pattern). Group 1, when present, is the secret part; otherwise the whole match is.
FORMATS = [
    ("private_key", re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY(?: BLOCK)?-----.*?(?:-----END \1PRIVATE KEY(?: BLOCK)?-----|\Z)", re.S)),
    ("aws_access_key", re.compile(r"\b((?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16})\b")),
    ("github_token", re.compile(r"\b((?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b")),
    ("gitlab_token", re.compile(r"\b(glpat-[A-Za-z0-9_-]{20,})")),
    ("slack_token", re.compile(r"\b(xox[abprs]-[A-Za-z0-9-]{10,})")),
    ("slack_webhook", re.compile(r"(https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,})")),
    ("secret_key", re.compile(r"\b((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})\b")),
    ("webhook_secret", re.compile(r"\b(whsec_[A-Za-z0-9+/=]{20,})")),
    ("anthropic_key", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,})")),
    ("openai_key", re.compile(r"\b(sk-(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,})")),
    ("google_api_key", re.compile(r"\b(AIza[0-9A-Za-z_-]{35})")),
    ("npm_token", re.compile(r"\b(npm_[A-Za-z0-9]{36})\b")),
    ("jwt", re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")),
    ("azure_key", re.compile(r"(?:AccountKey|SharedAccessKey)=([A-Za-z0-9+/]{20,}={0,2})")),
    ("azure_sas", re.compile(r"[?&]sig=([A-Za-z0-9%+/=]{20,})")),
    ("url_password", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@'\"]+:([^\s@/'\"]+)@")),
    ("password", re.compile(r"(?i)(?<=[;\"'])[ \t]*(?:password|pwd)[ \t]*=[ \t]*([^;'\"\s<>]+)(?=;|[\"']|[ \t]*$)")),
    ("auth_header", re.compile(r"(?i)\bauthorization\s*[:=]\s*[\"']?(?:bearer|basic|token)\s+([A-Za-z0-9._~+/=:-]{8,})")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})")),
]

# Name tokens (split on _ . - and camelCase) that make an assignment secret.
SECRET_TOKENS = {"secret", "secrets", "password", "passwd", "passphrase", "pwd", "token", "apikey",
                 "credential", "credentials", "privatekey", "connectionstring"}
# Secret only as the final token: YOUTRACK_AUTH yes, auth_signer_phone and authorization_number no.
LAST_ONLY_TOKENS = {"auth", "authorization", "dsn", "cookie"}
KEY_QUALIFIERS = {"api", "access", "private", "signing", "encryption", "master", "secret", "service", "account", "client"}
PUBLIC_TOKENS = {"public", "publishable", "pub"}
NOT_SECRET_SUFFIX = {"id", "url", "uri", "path", "file", "name", "type", "label", "length", "expiry", "expires", "ttl", "header",
                     "sock", "socket", "dir", "host", "port", "user", "username",
                     "number", "manager", "provider", "node", "version", "count", "date", "at", "phone", "email",
                     "arn", "template"}
# Shell variables whose names look secret but hold paths.
SHELL_NAMES = {"PWD", "OLDPWD"}

# Values that are references or template text, never secrets.
PLACEHOLDER_VALUE = re.compile(
    r"(?i)^(?:|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|\$\(.*\)|<[^>]*>|\{\{.*\}\}|%\(?[A-Za-z_]+\)?s?%?|x+|\*+|\.+|"
    r"change[-_ ]?me|your[-_ ].*|example.*|placeholder|dummy|test|password|secret|token|string|null|none|nil|"
    r"true|false|undefined|todo|redacted|.*REDACTED.*|(?:process\.env|os\.environ|import\.meta\.env)\b.*|"
    r".*getenv\(.*|@Microsoft\.KeyVault\(.*|vault:.*|op://.*|[~^<>=v]*\d+(?:\.\d+){1,3}(?:-[\w.]+)?)$")
PLACEHOLDER = re.compile(r"<REDACTED:[a-z0-9_]+>")

# Sources whose assignments are configuration rather than code.
ENV_LIKE = re.compile(
    r"(?i)(?:^|/)(?:\.env(?:\.[^/]*)?|[^/]*\.env|\.netrc|\.npmrc|\.pypirc|credentials|config|"
    r"[^/]*\.(?:json|ya?ml|toml|ini|cfg|conf|config|properties|xml|tfvars)|docker-compose[^/]*)$")
# KEY=value lines and their quoted-literal counterparts in code or JSON/YAML.
ENV_LINE = re.compile(r"(?m)^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*=[ \t]*(\"[^\"\n]*\"|'[^'\n]*'|[^\n]*)")
QUOTED_PAIR = re.compile(r"[\"']?([A-Za-z_][A-Za-z0-9_.-]*)[\"']?[ \t]*[:=][ \t]*(\"[^\"\n]*\"|'[^'\n]*')")
YAML_PAIR = re.compile(r"(?m)^[ \t]*-?[ \t]*([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*:[ \t]+([^\s\"'#{\[|>&*!][^\n#]*?)[ \t]*(?:#.*)?$")
XML_PAIR = re.compile(r"(?:key|name)=\"([^\"]+)\"[^>]*?\b(?:value|connectionString)=\"([^\"]*)\"")

# High-entropy tokens worth a second look (Qwen in Phase 3), and shapes that are never secrets.
# Thresholds were loosened once the local model reviews candidates: they catch ~96% of random 20-char keys.
CANDIDATE = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{20,}={0,2}(?![A-Za-z0-9+/_=-])")
# Publishable keys are public by design (decision 3); they stay visible.
PUBLIC_VALUE = re.compile(r"^(?:pk_(?:live|test)_|phc_)")
# Prefixed record ids (Clerk org_/user_, Stripe cus_ ...) are identifiers; secret-looking prefixes stay candidates.
PREFIXED_ID = re.compile(r"^(?!(?:sk|rk|sig|key|tok|token|secret|whsec|pat)_)[a-z]{2,8}_[A-Za-z0-9]{10,}$")
LOCKFILE = re.compile(r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|deno\.lock|yarn\.lock|pnpm-lock\.yaml|"
                      r"bun\.lockb?|Cargo\.lock|poetry\.lock|uv\.lock|Gemfile\.lock|composer\.lock|go\.sum)$")
NOT_CANDIDATE = re.compile(
    r"^(?:[0-9a-fA-F-]+|[a-z0-9_-]+|[A-Z0-9_-]+|[A-Za-z_-]+)$")
INTEGRITY = re.compile(r"(?:sha(?:1|256|384|512)-|data:[a-z]+/[a-z0-9.+-]+;base64,)$")
INTEGRITY_TOKEN = re.compile(r"^sha(?:1|256|384|512)-")
# camelCase words, paths and names: mostly runs of 3+ lowercase letters.
WORD_RUN = re.compile(r"[A-Z]?[a-z]{3,}")
MAX_WORD_SHARE = 0.6
# Dotenv files keep every non-placeholder value; elsewhere letter-only words ("local", "worker") are config, not secrets.
DOTENV = re.compile(r"(?:^|/)(?:\.env(?:\.[^/]*)?|[^/]*\.env)$")
WORD_VALUE = re.compile(r"[A-Za-z]+(?:[-_ .][A-Za-z]+)*")
# Alphabet or digit runs are character sets in code, not keys.
SEQUENCE = re.compile(r"abcdefgh|ABCDEFGH|0123456")
# Shannon entropy floor, scaled because short tokens cannot reach 4 bits per char.
MIN_ENTROPY = 4.0
ENTROPY_SCALE = 0.8
MIN_DIGITS = 1


def name_tokens(name):
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[\s_.\-]+", spaced.lower()) if t]


def is_secret_name(name):
    tokens = name_tokens(name)
    if not tokens or name in SHELL_NAMES or PUBLIC_TOKENS & set(tokens) or tokens[-1] in NOT_SECRET_SUFFIX:
        return False
    joined = {a + b for a, b in zip(tokens, tokens[1:])}
    if (SECRET_TOKENS & set(tokens)) or (SECRET_TOKENS & joined) or tokens[-1] in LAST_ONLY_TOKENS:
        return True
    return tokens[-1] == "key" and len(tokens) > 1 and tokens[-2] in KEY_QUALIFIERS | {"stripe", "clerk"}
