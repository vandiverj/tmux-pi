import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOD = "---\nname: {n}\ndescription: Does a useful thing; use when the task needs that thing.\n---\n# T\n{b}\n"


def check(path, *flags):
    p = subprocess.run([str(ROOT / "bin/tp-skill-check"), str(path), *flags], text=True, capture_output=True)
    return p.returncode, p.stderr


class SkillCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def skill(self, name="my-skill", text=None, body=""):
        d = self.tmp / name
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(text if text is not None else GOOD.format(n=name, b=body))
        return d

    def test_template_valid_and_not_discovered(self):
        t = ROOT / "templates/skill-template"
        self.assertEqual(check(t, "--single-file")[0], 0)
        self.assertFalse((ROOT / "skills/skill-template").exists())

    def test_invalid_frontmatter(self):
        bad = [
            "# no frontmatter\n",
            GOOD.format(n="Bad_Name", b=""),
            GOOD.format(n="a--b", b=""),
            GOOD.format(n="other", b=""),
            "---\nname: my-skill\n---\nx\n",
            "---\nname: my-skill\ndescription: TODO\n---\n",
            "---\nname: my-skill\ndescription: " + "x" * 1025 + "\n---\n",
            "---\nname: my-skill\ndescription: Does a useful thing; use when needed.\nallowed-tools: Bash\n---\n",
            "---\nname: my-skill\ndescription: Does a useful thing; use when needed.\nhooks: x\n---\n",
            "---\nname: my-skill\ndescription: >\n  folded text here\n---\n",
        ]
        for text in bad:
            with self.subTest(text=text):
                d = self.skill(text=text)
                self.assertEqual(check(d)[0], 1, text)

    def test_one_hop_reference_ok_and_single_file_rejects(self):
        d = self.skill(body="[d](references/d.md)")
        (d / "references").mkdir()
        (d / "references/d.md").write_text("detail, no links\n")
        self.assertEqual(check(d)[0], 0)
        rc, err = check(d, "--single-file")
        self.assertEqual(rc, 1)
        self.assertIn("single-file", err)

    def test_path_escape_missing_and_symlink(self):
        for link in ("../x.md", "/etc/hosts", "references/none.md"):
            with self.subTest(link=link):
                d = self.skill(body=f"[x]({link})")
                self.assertEqual(check(d)[0], 1, link)
        d = self.skill(body="[x](references/s.md)")
        (d / "references").mkdir()
        outside = self.tmp / "outside.md"
        outside.write_text("secret\n")
        (d / "references/s.md").symlink_to(outside)
        rc, err = check(d)
        self.assertEqual(rc, 1)
        self.assertIn("symlink", err)

    def test_nested_reference_rejected(self):
        d = self.skill(body="[a](a.md)")
        (d / "a.md").write_text("see [b](b.md)\n")
        (d / "b.md").write_text("b\n")
        rc, err = check(d)
        self.assertEqual(rc, 1)
        self.assertIn("one hop", err)

    def test_empty_frontmatter_rejected(self):
        d = self.skill(text="---\n---\nx\n")
        rc, err = check(d)
        self.assertEqual(rc, 1)
        self.assertIn("'name' is required", err)
        self.assertIn("'description' is required", err)

    def test_invalid_yaml_values_rejected(self):
        for desc in ("Does a thing: use when needed here", "Does a useful thing # use when needed",
                     "'unterminated quote value here", "\"quoted\" trailing junk here", "[a, b] list value here"):
            with self.subTest(desc=desc):
                d = self.skill(text=f"---\nname: my-skill\ndescription: {desc}\n---\n")
                self.assertEqual(check(d)[0], 1, desc)

    def test_quoted_values_valid(self):
        for desc in ("'Does a thing: use when needed here'", "\"Does a thing # use when needed here\"",
                     "Does a useful thing; use when it's needed # not a comment".replace(" #", "#")):
            with self.subTest(desc=desc):
                d = self.skill(text=f"---\nname: my-skill\ndescription: {desc}\n---\n")
                self.assertEqual(check(d)[0], 0, desc)

    def test_link_syntaxes_cannot_bypass_confinement(self):
        for body in ("[s]: ../../etc/passwd", "[s]: <../x.md>", "[x](<../x.md>)", "![i](../x.png)",
                     '<a href="../x.md">x</a>', "<img src='/etc/hosts'>", "[x](%2e%2e/x.md)",
                     "[x](..\\x.md)"):
            with self.subTest(body=body):
                d = self.skill(body=body)
                rc, err = check(d)
                self.assertEqual(rc, 1, body)
                self.assertNotIn("Traceback", err)

    def test_reference_file_one_hop_bypass_rejected(self):
        for rbody in ("[x]: other.md", '<a href="other.md">x</a>', "[x](<other.md>)", "![i](pic.png)"):
            with self.subTest(rbody=rbody):
                d = self.skill(body="[a](a.md)")
                (d / "a.md").write_text(rbody + "\n")
                (d / "other.md").write_text("o\n")
                (d / "pic.png").write_bytes(b"x")
                rc, err = check(d)
                self.assertEqual(rc, 1, rbody)
                self.assertIn("one hop", err)

    def test_fences_external_urls_and_valid_ref_links(self):
        body = ("```\n[x](../nope.md)\n```\n`[y](../nope.md)`\n[e](https://example.com/a.md)\n"
                '<a href="https://example.com/x">e</a>\n[a]: https://example.com\n[l](<a b.md>)\n[r]: refs/r.md\n')
        d = self.skill(body=body)
        (d / "a b.md").write_text("x\n")
        (d / "refs").mkdir()
        (d / "refs/r.md").write_text("detail [top](#top)\n")
        self.assertEqual(check(d)[0], 0)

    def test_non_utf8_reported_cleanly(self):
        d = self.skill(body="[a](a.md)")
        (d / "a.md").write_bytes(b"\xff\xfe bad\n")
        rc, err = check(d)
        self.assertEqual((rc, "Traceback" in err, "UTF-8" in err), (1, False, True))
        (d / "SKILL.md").write_bytes(b"---\n\xff\n---\n")
        rc, err = check(d)
        self.assertEqual((rc, "Traceback" in err, "UTF-8" in err), (1, False, True))

    def test_toc_placement(self):
        d = self.skill(body="[a](a.md)")
        long = "\n".join(f"line {i}" for i in range(120))
        (d / "a.md").write_text("# A\n\n" + long + "\n")
        self.assertEqual(check(d)[0], 1)
        (d / "a.md").write_text("# A\n\n## Contents\n\n- one\n- two\n\n" + long + "\n")
        self.assertEqual(check(d)[0], 0)
        (d / "a.md").write_text("# A\n\n" + long + "\n\n## Contents\n")
        self.assertEqual(check(d)[0], 1)

    def test_existing_catalog_accepted_unchanged(self):
        before = subprocess.run(["git", "status", "--porcelain", "catalog", "skills"], cwd=ROOT,
                                text=True, capture_output=True).stdout
        entries = json.loads((ROOT / "config/skill-catalog.json").read_text())["skills"]
        self.assertTrue(entries)
        for e in entries:
            with self.subTest(skill=e["id"]):
                d = (ROOT / e["path"]).parent
                self.assertEqual(check(d, "--legacy")[0], 0, e["id"])
        after = subprocess.run(["git", "status", "--porcelain", "catalog", "skills"], cwd=ROOT,
                               text=True, capture_output=True).stdout
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
