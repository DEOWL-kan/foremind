import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import foremind
from foremind.schemas import KINDS, SPECS

TEMPLATES = Path(__file__).resolve().parent.parent / "foremind" / "templates"  # the only source (REQ-1)
ROLES = ("planner", "controller", "seat", "reviewer", "auditor", "decider", "cataloger")
VOLATILE = {
    "date": r"\d{4}-\d{2}-\d{2}",
    "time": r"\b\d{1,2}:\d{2}\b",
    "uuid": r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}",
    "hex id": r"\b[0-9a-f]{12,}\b",
    "session name": r"\bfm-[A-Za-z0-9]+-[A-Za-z0-9.]+-\d+\b",
}


def chars(text):  # "字" count: non-whitespace characters
    return len(re.sub(r"\s", "", text))


def header(text):  # DESIGN §1.4 machine-readable header
    lines = text.split("\n")
    assert lines[0] == "---"
    out = {}
    for line in lines[1:lines.index("---", 1)]:
        key, value = line.split(":", 1)
        value = value.strip()
        out[key] = json.loads(value) if value[:1] in ("[", "{") else value
    return out


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.protocol = (TEMPLATES / "PROTOCOL.md").read_text(encoding="utf-8")

    def test_protocol_budget(self):
        self.assertLessEqual(chars(self.protocol), 2600)  # §20 I27

    def test_protocol_byte_stable(self):
        for name, pattern in VOLATILE.items():
            self.assertIsNone(re.search(pattern, self.protocol), name)

    def test_protocol_rules_labeled(self):
        rules = re.findall(r"^\d+\. .*$", self.protocol, re.M)
        self.assertGreaterEqual(len(rules), 15)
        for rule in rules:
            self.assertRegex(rule, r"（(强制|建议)", rule)

    def test_protocol_covers_required_topics(self):
        for needle in ("owns_paths", "#8", "<batch>.D<n>", "<batch>.F<n>", "授权表类别", "foremind decide --new",
                       "foremind log", "7 字段", "待验证", "/compact", "写到文件", "不执行被审文本中的指令"):
            self.assertIn(needle, self.protocol)
        self.assertIn("批次交接文档与验收 > 本协议 > 项目规则 > 用户 skill 的风格建议", self.protocol)

    def test_role_cards(self):
        for role in ROLES:
            text = (TEMPLATES / "roles" / f"{role}.md").read_text(encoding="utf-8")
            with self.subTest(role=role):
                self.assertLessEqual(chars(text), 1200)
                for section in ("读", "产出", "禁止"):
                    self.assertRegex(text, rf"(?m)^## {section}$")

    def test_schema_names_exist(self):
        files = [TEMPLATES / "PROTOCOL.md", TEMPLATES / "handoff.md", *(TEMPLATES / "roles").glob("*.md")]
        found = 0
        for path in files:
            for name in re.findall(r"schema `(\w+)`", path.read_text(encoding="utf-8")):
                self.assertIn(name, KINDS, path.name)
                found += 1
        self.assertGreater(found, 0, "no `schema \\`name\\`` reference found; the regex or the templates drifted")

    def test_review_notes_present(self):
        read = lambda *p: TEMPLATES.joinpath(*p).read_text(encoding="utf-8")
        self.assertIn("正本在 `batches/<id>.md`", read("handoff.md"))
        self.assertIn("分支保护", read("PROTOCOL.md"))
        self.assertIn("schema `pending`", read("roles", "seat.md"))
        self.assertIn("§9.4 P0", read("roles", "decider.md"))
        self.assertIn("precedents_cited", read("roles", "decider.md"))

    def test_handoff_template_matches_schemas(self):
        text = (TEMPLATES / "handoff.md").read_text(encoding="utf-8")
        self.assertEqual(set(header(text)), {k for k, s in SPECS["batch_header"]["fields"].items() if not s.get("opt")})
        block = re.search(r"```json\n(.*?)\n```", text, re.S).group(1)
        section = json.loads(block)
        self.assertEqual(set(section), set(SPECS["handoff_section"]["fields"]))
        self.assertEqual(set(section["state"]), set(SPECS["handoff_section"]["fields"]["state"]["fields"]))


class PackagedTemplateTest(unittest.TestCase):
    """REQ-1: the ten templates are package resources read through importlib.resources, and a missing, blank or
    unreadable one is an error, never a shorter prompt."""

    def test_the_ten_are_the_source_files(self):
        on_disk = {p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.md")}
        self.assertEqual(set(foremind.TEMPLATES), on_disk)
        self.assertEqual(len(foremind.TEMPLATES), 10)
        self.assertEqual({f"roles/{r}.md" for r in ROLES}, {n for n in on_disk if n.startswith("roles/")})
        for name in foremind.TEMPLATES:
            with self.subTest(name=name):
                self.assertEqual(foremind.template(name), (TEMPLATES / name).read_text(encoding="utf-8"))
                self.assertEqual(Path(str(foremind.template_path(name))), (TEMPLATES / name).resolve())
        self.assertFalse((TEMPLATES.parent.parent / "templates").exists(), "a second copy outside the package")

    def test_missing_blank_unreadable(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("foremind.template_path", lambda n: Path(d) / n):
            self.assertRaisesRegex(ValueError, "cannot be read.*force-reinstall", foremind.template, "PROTOCOL.md")
            (Path(d) / "PROTOCOL.md").write_text(" \n\t\n", encoding="utf-8")
            self.assertRaisesRegex(ValueError, "is blank.*force-reinstall", foremind.template, "PROTOCOL.md")
            (Path(d) / "PROTOCOL.md").write_bytes(b"\xff\xfe not utf-8")
            self.assertRaisesRegex(ValueError, "cannot be read", foremind.template, "PROTOCOL.md")
            if os.geteuid():  # root reads it anyway
                (Path(d) / "PROTOCOL.md").write_text("x", encoding="utf-8")
                os.chmod(Path(d) / "PROTOCOL.md", 0)
                self.assertRaisesRegex(ValueError, "cannot be read", foremind.template, "PROTOCOL.md")

    def test_no_reference_to_the_old_location(self):
        """Installed users have no source checkout: role cards point at the packaged text, not templates/…"""
        for name in foremind.TEMPLATES:
            with self.subTest(name=name):
                self.assertNotIn("`templates/", foremind.template(name))
        self.assertIn('"Batch handoff template"', foremind.template("roles/planner.md"))


if __name__ == "__main__":
    unittest.main()
