"""Replace secrets in tool output with stable placeholders, deterministically and fail-closed.

Order: exact known values, known formats, secret-named assignments, then high-entropy
candidates. Candidates go to `classify` (lib/redact_llm.py); without one they are redacted
as `unverified`. A classifier can only clear candidates, never earlier hits.
"""
import base64
import bisect
import math
import os
from pathlib import Path
import re
from urllib.parse import quote

from redact_rules import (CANDIDATE, DOTENV, ENV_LIKE, ENV_LINE, FORMATS, INTEGRITY, INTEGRITY_TOKEN, LOCKFILE,
                          ENTROPY_SCALE, MAX_WORD_SHARE, MIN_DIGITS, MIN_ENTROPY, PREFIXED_ID, WORD_RUN,
                          NOT_CANDIDATE, PLACEHOLDER, PLACEHOLDER_VALUE, PUBLIC_TOKENS, PUBLIC_VALUE, QUOTED_PAIR,
                          SEQUENCE, WORD_VALUE, XML_PAIR, YAML_PAIR, is_secret_name,
                          name_tokens)
from redaction_store import Store

MIN_EXACT = 8


def entropy(text):
    counts = {c: text.count(c) for c in set(text)}
    return -sum(n / len(text) * math.log2(n / len(text)) for n in counts.values())


def secret_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if PLACEHOLDER_VALUE.match(value) or PLACEHOLDER.search(value) or len(value) < 4:
        return None
    if value.isdigit() and len(value) < 8:
        return None
    return value


def parse_env_text(text):
    for match in ENV_LINE.finditer(text):
        value = match.group(2)
        if value[:1] not in "\"'":
            value = re.sub(r"\s+#.*$", "", value)
        yield match.group(1), value.strip()


def exact_values(env=None, files=()):
    """Secret values this machine already holds: secret-named env vars and listed env files."""
    values = set()
    pairs = list((env if env is not None else os.environ).items())
    for path in files:
        try:
            pairs += list(parse_env_text(Path(path).expanduser().read_text()))
        except (OSError, UnicodeDecodeError):
            continue
    for name, raw in pairs:
        value = secret_value(raw) if is_secret_name(name) else None
        if value and len(value) >= MIN_EXACT and not value.startswith(("/", "~/")):
            values.add(value)
    return values


def exact_variants(value):
    yield value
    yield base64.b64encode(value.encode()).decode().rstrip("=")
    encoded = quote(value, safe="")
    if encoded != value:
        yield encoded


def kind_for(name):
    tokens = [t for t in name_tokens(name) if t not in {"next", "vite", "react", "app", "export"}]
    return re.sub(r"[^a-z0-9_]", "", "_".join(tokens))[:40] or "secret"


class Redactor:
    def __init__(self, store=None, exact=(), classify=None):
        self.store = store or Store(persist=False)
        self.exact = sorted({v for value in exact for v in exact_variants(value) if len(v) >= MIN_EXACT}, key=len, reverse=True)
        self.classify = classify

    def add_exact(self, values):
        """Also redact these values wherever they appear (a command was given them), even short ones."""
        added = {v for value in values for v in exact_variants(value) if len(v) >= 4}
        self.exact = sorted(set(self.exact) | added, key=len, reverse=True)

    def spans(self, text, source=None):
        """Yield (start, end, rank, kind, confirmed); a lower rank names overlapping spans."""
        for value in self.exact:
            start = text.find(value)
            while start != -1:
                yield start, start + len(value), 2, "env_secret", True
                start = text.find(value, start + len(value))
        for kind, pattern in FORMATS:
            for match in pattern.finditer(text):
                group = 1 if pattern.groups and match.group(1) is not None and kind != "private_key" else 0
                if kind in {"url_password", "password"} and not secret_value(match.group(group)):
                    continue
                yield match.start(group), match.end(group), 1, kind, True
        if source and LOCKFILE.search(str(source)):
            return  # package metadata and hashes; formats and exact values above still apply
        env_source = bool(source and ENV_LIKE.search(str(source)))
        dotenv = bool(source and DOTENV.search(str(source)))
        pairs = [ENV_LINE, QUOTED_PAIR, XML_PAIR] + ([YAML_PAIR] if env_source else [])
        public = []
        for pattern in pairs:
            for match in pattern.finditer(text):
                name, raw = match.group(1), match.group(2)
                if PUBLIC_TOKENS & set(name_tokens(name)):
                    public.append((match.start(2), match.end(2)))
                if not is_secret_name(name):
                    continue
                quoted = raw[:1] in "\"'"
                if pattern is ENV_LINE and not quoted:
                    raw = re.sub(r"\s+#.*$", "", raw)
                value = secret_value(raw)
                # Outside config, only quoted literals or SHOUTY_ENV=value lines (printenv, cat .env) count;
                # code such as `token = get()` or `API_KEY = config["key"]` doesn't.
                env_line = pattern is ENV_LINE and name.isupper() and re.match(r"[ \t]*(?:export[ \t]+)?[^\s=]+=\S", match.group(0))
                if not value or (not env_source and not quoted and not env_line):
                    continue
                if not env_source and quoted and re.search(r"\s", value):
                    continue
                if not dotenv and not env_line and WORD_VALUE.fullmatch(value):
                    continue
                start = match.start(2) + raw.find(value)
                yield start, start + len(value), 0, kind_for(name), True
        for match in CANDIDATE.finditer(text):
            token = match.group(0).rstrip("=")
            if (NOT_CANDIDATE.match(token) or PUBLIC_VALUE.match(token) or SEQUENCE.search(token) or
                    PREFIXED_ID.match(token) or INTEGRITY_TOKEN.match(token) or
                    sum(map(len, WORD_RUN.findall(token))) / len(token) > MAX_WORD_SHARE or
                    any(a <= match.start() < b for a, b in public) or sum(c.isdigit() for c in token) < MIN_DIGITS or
                    not re.search(r"[a-z]", token) or not re.search(r"[A-Z]", token) or
                    entropy(token) < min(MIN_ENTROPY, ENTROPY_SCALE * math.log2(len(token))) or
                    INTEGRITY.search(text[max(0, match.start() - 40):match.start()])):
                continue
            yield match.start(), match.start() + len(token), 3, "unverified", False

    def plan(self, text, source=None):
        """Merged (start, end, rank, kind, confirmed) spans; confirmed=False spans await review."""
        protected = [(m.start(), m.end()) for m in PLACEHOLDER.finditer(text)]
        found = sorted(s for s in self.spans(text, source)
                       if s[1] > s[0] and not any(s[0] < b and a < s[1] for a, b in protected))
        merged = []
        for start, end, rank, kind, confirmed in found:
            if merged and start < merged[-1][1]:
                last = merged[-1]
                named = (rank, kind) if rank < last[2] else (last[2], last[3])
                merged[-1] = (last[0], max(last[1], end), *named, last[4] or confirmed)
            else:
                merged.append((start, end, rank, kind, confirmed))
        return merged

    def review(self, items):
        """Indices of [(text, (start, end), source)] candidates to keep redacted; all of them without a classifier."""
        if not items or not self.classify:
            return set(range(len(items)))
        return set(self.classify(items))

    def apply(self, text, spans, source=None):
        """Replace spans with placeholders. Findings carry placeholder, kind and line, never values."""
        if not spans:
            return text, []
        lines = [m.start() for m in re.finditer("\n", text)]
        out, findings, pos = [], [], 0
        with self.store.locked():
            for start, end, _, kind, _ in spans:
                line = bisect.bisect_left(lines, start) + 1
                name = self.store.placeholder(text[start:end], kind, source, line if source else None)
                out += [text[pos:start], name]
                findings.append({"placeholder": name, "kind": kind, "line": line})
                pos = end
        out.append(text[pos:])
        return "".join(out), findings

    def redact(self, text, source=None):
        """Return (redacted_text, findings)."""
        spans = self.plan(text, source)
        review = [s for s in spans if not s[4]]
        keep = self.review([(text, (s[0], s[1]), source) for s in review])
        cleared = {review[i] for i in range(len(review)) if i not in keep}
        return self.apply(text, [s for s in spans if s not in cleared], source)
