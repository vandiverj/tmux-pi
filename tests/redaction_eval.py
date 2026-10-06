#!/usr/bin/env python3
"""Measure the local model's review of ambiguous redaction candidates against labeled contexts.

Run: python3 tests/redaction_eval.py [--rounds N] [--batch N]. Needs LM Studio; not part of
the unit suite. Every case is checked to reach the model as an unconfirmed candidate.
"""
import argparse
import random
import string
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from redact import Redactor
from redact_llm import LocalClassifier
from redaction_store import Store

B62 = string.ascii_letters + string.digits

SECRET = [
    "curl -u admin:{t} https://api.example.com/v1/users",
    "docker login -u ci -p {t} registry.example.com",
    "mysql -h db.internal -u root --password {t} app",
    "aws configure set aws_secret_access_key {t}",
    'curl -H "X-Api-Key: {t}" https://api.example.com/v2/items',
    "git clone https://{t}@github.com/acme/repo.git",
    'psql "host=db user=app password={t} dbname=app"',
    "redis-cli -h cache -a {t} ping",
    "INFO using session cookie {t} for user 42",
    "//registry.npmjs.org/:_authToken={t}",
    "openssl enc -aes-256-cbc -k {t} -in backup.tar",
    "apiVersion: v1\nkind: Secret\ndata:\n  password: {b}",
    "Set-Cookie: session={t}; Path=/; HttpOnly; Secure",
    "connecting to wss://stream.example.com/socket?access_token={t}",
    "heroku config:set STRIPE_KEY {t} --app web",
]
NOT_SECRET = [
    '{{"requestId": "{t}", "status": 200}}',
    '{{"etag": "{b}", "size": 10432}}',
    '<script src="/assets/app.{t}.js"></script>',
    '{{"traceId": "{t}", "spanId": "a1b2"}}',
    '{{"thumbnail": "{long}"}}',
    '{{"objectKey": "uploads/{t}/photo.jpg"}}',
    '{{"id": "{t}", "name": "Crew roster"}}',
    '{{"cursor": "{b}", "hasMore": true}}',
    '{{"checksum": "{b}", "algorithm": "sha256"}}',
    "GET /api/files/{t}/download 200 12ms",
    '{{"messageId": "<{t}@mail.example.com>"}}',
    '{{"correlationId": "{t}", "level": "info"}}',
    "webpack: emitted main.{t}.chunk.js (412 KiB)",
]


def token(rng, n, alphabet=B62):
    while True:
        t = "".join(rng.choice(alphabet) for _ in range(n))
        if sum(c.isdigit() for c in t) >= 3 and any(c.islower() for c in t) and any(c.isupper() for c in t):
            return t


def cases(rounds, seed=7):
    rng = random.Random(seed)
    out = []
    for r in range(rounds):
        for label, templates in ((True, SECRET), (False, NOT_SECRET)):
            for template in templates:
                text = template.format(t=token(rng, rng.choice([24, 32, 40])), b=token(rng, 32, B62 + "+/") + "==",
                                       long=token(rng, 600, B62 + "+/"))
                out.append((label, template, text))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--batch", type=int, default=8)
    args = parser.parse_args()
    planner = Redactor(Store(persist=False))
    items, labels, names = [], [], []
    for label, template, text in cases(args.rounds):
        candidates = [s for s in planner.plan(text) if not s[4]]
        if not candidates:
            print("NOT A CANDIDATE (fix the case):", template[:60])
            continue
        for s in candidates:
            items.append((text, (s[0], s[1]), None))
            labels.append(label)
            names.append(template)
    classifier = LocalClassifier(budget=60)
    order = list(range(len(items)))
    random.Random(1).shuffle(order)
    kept, times = set(), []
    for i in range(0, len(order), args.batch):
        chunk = order[i:i + args.batch]
        started = time.monotonic()
        keep = classifier([items[j] for j in chunk])
        times.append(time.monotonic() - started)
        if classifier.stats.get("status") != "ok":
            print("batch status:", classifier.stats)
        kept |= {chunk[k] for k in keep}
    tp = sum(1 for i, l in enumerate(labels) if l and i in kept)
    fn = [names[i] for i, l in enumerate(labels) if l and i not in kept]
    tn = sum(1 for i, l in enumerate(labels) if not l and i not in kept)
    fp = [names[i] for i, l in enumerate(labels) if not l and i in kept]
    pos, neg = sum(labels), len(labels) - sum(labels)
    times.sort()
    print("secrets kept redacted: {}/{} ({:.0%})   missed: {}".format(tp, pos, tp / pos, len(fn)))
    print("non-secrets cleared:   {}/{} ({:.0%})   over-redacted: {}".format(tn, neg, tn / neg, len(fp)))
    print("batch of {}: p50 {:.2f}s p95 {:.2f}s over {} calls".format(
        args.batch, times[len(times) // 2], times[max(0, int(len(times) * 0.95) - 1)], len(times)))
    for title, rows in (("MISSED (secret cleared)", fn), ("OVER-REDACTED (non-secret kept)", fp)):
        for template in sorted(set(rows)):
            print("  {}: {}x {}".format(title, rows.count(template), template.replace("\n", "\\n")[:80]))


if __name__ == "__main__":
    main()
