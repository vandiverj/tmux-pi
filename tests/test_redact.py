"""Deterministic redaction: red-team cases must be caught, ordinary code must pass through.

Fake secrets are generated at runtime so no key-shaped literal is committed.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import string
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from redact import Redactor, exact_values
from redaction_store import Store

ALNUM = string.ascii_letters + string.digits


def fake(prefix="", n=32, alphabet=ALNUM, seed=None):
    rng = random.Random(seed if seed is not None else prefix + str(n))
    return prefix + "".join(rng.choice(alphabet) for _ in range(n))


def pem(kind="RSA "):
    body = "\n".join(fake("", 64, ALNUM + "+/", seed=i) for i in range(4))
    return "-----BEGIN {0}PRIVATE KEY-----\n{1}\n-----END {0}PRIVATE KEY-----".format(kind, body)


class RedactTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.redactor = Redactor(Store(persist=False))

    def redact(self, text, source=None, redactor=None):
        return (redactor or self.redactor).redact(text, source)

    def assertGone(self, secret, text, source=None, redactor=None):
        out, findings = self.redact(text, source, redactor)
        self.assertNotIn(secret, out)
        self.assertIn("<REDACTED:", out)
        self.assertNotIn(secret, json.dumps(findings))
        return out, findings

    def assertKept(self, text, source=None):
        out, findings = self.redact(text, source)
        self.assertEqual(out, text, findings)

    def test_dotenv_secret_becomes_named_placeholder_and_public_key_stays(self):
        secret, public = fake("sk_test_"), fake("pk_test_", 40)
        text = "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY={}\nCLERK_SECRET_KEY={}\nDEBUG=true\n".format(public, secret)
        for source in ("apps/web/.env.local", None):  # Read of the file, or `cat .env` output
            with self.subTest(source=source):
                out, findings = self.assertGone(secret, text, source, Redactor(Store(persist=False)))
                self.assertIn("CLERK_SECRET_KEY=<REDACTED:clerk_secret_key_1>", out)
                self.assertIn(public, out)
                self.assertEqual([(f["kind"], f["line"]) for f in findings], [("clerk_secret_key", 2)])

    def test_known_formats(self):
        cases = {
            "aws_access_key": fake("AKIA", 16, string.ascii_uppercase + string.digits),
            "github_token": fake("ghp_", 36),
            "github_pat": fake("github_pat_", 40),
            "gitlab_token": fake("glpat-", 20),
            "slack_token": "xoxb-" + fake("", 24, string.digits + "-"),
            "slack_webhook": "https://hooks.slack.com/services/" + fake("", 30, ALNUM + "/"),
            "stripe_live": fake("sk_live_", 24),
            "restricted": fake("rk_live_", 24),
            "webhook_secret": fake("whsec_", 32),
            "anthropic_key": fake("sk-ant-api03-", 40),
            "openai_key": fake("sk-proj-", 40),
            "google_api_key": fake("AIza", 35),
            "npm_token": fake("npm_", 36),
            "jwt": "eyJ" + fake("", 20) + ".eyJ" + fake("", 30) + "." + fake("", 43),
        }
        for kind, secret in cases.items():
            with self.subTest(kind=kind):
                self.assertGone(secret, "log line: value {} trailing".format(secret))

    def test_private_key_blocks(self):
        key = pem()
        self.assertGone(key.splitlines()[1], "const fixture = `\n{}\n`;\n".format(key), "src/test/keys.ts")
        escaped = json.dumps({"private_key": pem("")})
        out, _ = self.assertGone(pem("").splitlines()[1], escaped, "sa.json")
        self.assertNotIn("PRIVATE KEY", out)
        unterminated = pem().rsplit("-----END", 1)[0]
        self.assertGone(unterminated.splitlines()[2], "head -5 output\n" + unterminated)

    def test_connection_strings_and_headers(self):
        password, account = fake("", 16), fake("", 64, ALNUM + "+/") + "=="
        token = fake("", 40)
        cases = [
            (password, "DATABASE_URL=postgres://app:{}@db.internal:5432/app".format(password), None),
            (password, "Server=db;User Id=sa;Password={};Encrypt=true".format(password), "appsettings.json"),
            (account, "DefaultEndpointsProtocol=https;AccountName=x;AccountKey={};EndpointSuffix=core.windows.net".format(account), None),
            (token, "curl -H 'Authorization: Bearer {}' https://api".format(token), None),
            (token, '{{"headers": {{"authorization": "Token {}"}}}}'.format(token), None),
        ]
        for secret, text, source in cases:
            with self.subTest(text=text[:30]):
                self.assertGone(secret, text, source)

    def test_config_file_assignments(self):
        secret = fake("", 24, seed=7)
        cases = [
            ("config/settings.json", '{{"clientSecret": "{}", "clientId": "abc"}}'.format(secret)),
            ("deploy/values.yaml", "auth:\n  password: {}\n  user: admin\n".format(secret)),
            ("web.config", '<add key="ApiSecret" value="{}" />'.format(secret)),
            ("web.config", '<add name="Main" connectionString="Server=x;Password={};" />'.format(secret)),
            ("src/config.ts", 'export const apiKey = "{}";'.format(secret)),
            (None, "export STRIPE_SECRET_KEY='{}'".format(secret)),
        ]
        for source, text in cases:
            with self.subTest(source=source, text=text[:25]):
                self.assertGone(secret, text, source)

    def test_exact_machine_values_and_encodings(self):
        secret = "Bearer perm:" + fake("", 30, seed=3)
        env = {"YOUTRACK_AUTH": secret, "PWD": "/Users/someone/project", "SSH_AUTH_SOCK": "/tmp/agent.sock",
               "AUTH_URL": "https://example.com/oauth", "HOME": "/Users/someone"}
        local_env = self.dir / "mcp.local.env"
        file_secret = fake("", 20, seed=4)
        local_env.write_text('export NATACS_DEV_PASSWORD="{}"\nexport NATACS_DEV_HOST="db.example"\n'.format(file_secret))
        values = exact_values(env, [local_env])
        self.assertEqual(values, {secret, file_secret})
        redactor = Redactor(Store(persist=False), values)
        printenv = "\n".join("{}={}".format(k, v) for k, v in env.items())
        out, _ = self.assertGone(secret, printenv, redactor=redactor)
        self.assertIn("PWD=/Users/someone/project", out)
        for encoded in (base64.b64encode(file_secret.encode()).decode().rstrip("="), quote(secret, safe="")):
            with self.subTest(encoded=encoded[:6]):
                self.assertGone(encoded, "blob: " + encoded + " end", redactor=redactor)

    def test_high_entropy_candidates_fail_closed_and_classifier_only_clears_candidates(self):
        unknown, known = fake("", 40, ALNUM + "+/", seed=9), fake("sk_live_", 24)
        text = "opaque={} known={}".format(unknown, known)
        out, findings = self.assertGone(unknown, text)
        self.assertIn("unverified", {f["kind"] for f in findings})
        cleared = Redactor(Store(persist=False), classify=lambda items: set())
        out, findings = cleared.redact(text)
        self.assertIn(unknown, out)
        self.assertNotIn(known, out)

    def test_same_value_same_placeholder_across_calls_sources_and_processes(self):
        secret, other = fake("sk_test_", seed=11), fake("sk_test_", seed=12)
        store_root = self.dir / "store"
        first = Redactor(Store("s1", store_root)).redact("A_SECRET_KEY=" + secret, "a/.env")[0]
        second = Redactor(Store("s1", store_root)).redact("cat output " + secret + " and " + other)[0]
        self.assertEqual(first.split("=", 1)[1], second.split()[2])
        self.assertNotEqual(second.split()[2], second.split()[4])
        other_session = Redactor(Store("s2", store_root)).redact(secret)[0]
        self.assertEqual(other_session, "<REDACTED:secret_key_1>")
        env = {**os.environ, "TP_REDACTION_DIR": str(store_root)}
        def run(_):
            return subprocess.run([str(ROOT / "bin/tp-redact"), "--stdin", "--session", "s3"], input="x " + secret,
                                  capture_output=True, text=True, env=env, check=True).stdout
        with ThreadPoolExecutor(8) as pool:
            self.assertEqual(set(pool.map(run, range(8))), {"x <REDACTED:secret_key_1>"})

    def test_store_keeps_pointers_for_files_and_is_private(self):
        file_secret, output_secret = fake("sk_test_", seed=21), fake("sk_test_", seed=22)
        root = self.dir / "store"
        store = Store("s", root)
        redactor = Redactor(store)
        redactor.redact("X=1\nAPI_SECRET_KEY=" + file_secret, "apps/.env")
        redactor.redact("printed " + output_secret)
        raw = (root / "s.json").read_text()
        self.assertNotIn(file_secret, raw)
        self.assertIn(output_secret, raw)
        entry = store.entry("<REDACTED:api_secret_key_1>")
        self.assertEqual(entry["sources"], [{"path": "apps/.env", "line": 2}])
        self.assertEqual((root / "s.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_idempotent_on_already_redacted_text(self):
        once, _ = self.redact("CLERK_SECRET_KEY=" + fake("sk_test_", seed=31), ".env")
        self.assertEqual(self.redact(once, ".env")[0], once)

    def test_large_minified_output_is_fast(self):
        secret = fake("ghp_", 36, seed=41)
        blob = "".join("var a{0}=function(b){{return b+{0}}};".format(i) for i in range(60000))
        text = blob[:1_000_000] + secret + blob[1_000_000:]
        self.assertGreater(len(text), 2_000_000)
        start = time.monotonic()
        self.assertGone(secret, text, "dist/app.min.js")
        self.assertLess(time.monotonic() - start, 5)

    def test_ordinary_code_and_config_pass_through(self):
        uuid, sha = "3f2b8c1e-9a4d-4e6f-8b2a-1c3d5e7f9a0b", "ed642cd1b4f0e7a9c3d2b1a0f9e8d7c6b5a49382"
        cases = [
            ("src/auth.ts", "const clientSecret = process.env.CLERK_SECRET_KEY;\nconst token = useToken();\n"),
            ("src/schema.ts", "password: z.string().min(8),\nconfirmPassword: z.string(),\n"),
            ("src/app/(protected)/credentials/page.tsx", "export default function CredentialsPage() { return null }\n"),
            ("src/settings.py", 'SECRET_KEY = os.environ["SECRET_KEY"]\nAPI_KEY = config["key"]\n'),
            ("agent/models.json", '{"apiKey": "local", "baseUrl": "http://localhost:1234/v1"}'),
            ("locales/en.json", '{"password": "Password", "passwordLabel": "Enter your password"}'),
            ("tests/router.py", 'top = [{"token": "worker", "logprob": -0.1}]'),
            ("package.json", '{"@aws-sdk/credential-provider-node": "^3.540.0", "js-cookie": "^3.0.5"}'),
            ("package-lock.json", '"integrity": "sha512-' + fake("", 86, ALNUM + "+/", seed=51) + '=="'),
            ("fixtures/users.json", '{{"organizationId": "{}", "userId": "{}"}}'.format(fake("org_", 27, seed=52), fake("user_", 27, seed=53))),
            ("fixtures/legacy.json", '{"auth_signer_phone": "555-0100", "authorization_number": "A1234567"}'),
            ("bin/tp-task", 'allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")'),
            (None, "commit {}\nAuthor: Dev <dev@example.com>\nid {}\n".format(sha, uuid)),
            (None, "src/app/api/launchpad/organizations/route.ts\nsrc/app/(protected)/credentials/organizations/page.tsx\n"),
            ("apps/web/.env.example", "CLERK_SECRET_KEY=\nAPI_TOKEN=your-token-here\nDB_PASSWORD=changeme\nX_SECRET=${X_SECRET}\n"),
            ("infra/stack.yml", "SecretArn: arn:aws:secretsmanager:us-east-1:123:secret:x\nGenerateSecretString:\n  SecretStringTemplate: '{}'\n"),
        ]
        for source, text in cases:
            with self.subTest(source=source):
                self.assertKept(text, source)

    def test_cli_outputs_text_or_json_without_values(self):
        secret = fake("sk_test_", seed=61)
        env = {**os.environ, "TP_REDACTION_DIR": str(self.dir / "cli")}
        def cli(*args):
            return subprocess.run([str(ROOT / "bin/tp-redact"), "--stdin", "--no-store", *args],
                                  input="CLERK_SECRET_KEY=" + secret + "\n", capture_output=True, text=True, env=env)
        plain = cli("--source", ".env")
        self.assertEqual(plain.stdout, "CLERK_SECRET_KEY=<REDACTED:clerk_secret_key_1>\n")
        result = json.loads(cli("--json").stdout)
        self.assertEqual(result["findings"], [{"placeholder": "<REDACTED:clerk_secret_key_1>", "kind": "clerk_secret_key", "line": 1}])
        self.assertNotIn(secret, json.dumps(result))
        self.assertFalse((self.dir / "cli").exists())
        bad = subprocess.run([str(ROOT / "bin/tp-redact")], input="", capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")


if __name__ == "__main__":
    unittest.main()
