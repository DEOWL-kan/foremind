import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foremind import exemptions

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class ExemptionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.dir = self.root / ".foremind" / "exemptions"
        self.dir.mkdir(parents=True)

    def put(self, qid, match, batch="auth.2", expires=NOW + timedelta(hours=1), category=4):
        (self.dir / f"{qid}.json").write_text(json.dumps(
            {"batch": batch, "category": category, "match": match, "expires_at": expires.isoformat()}))

    def find(self, kind, values, header=None, batch="auth.2", category=4):
        return exemptions.find(self.root, batch, kind, values, category=category, header=header, now=NOW)

    def test_only_its_own_category(self):  # §20 I37
        self.put("Q-1", {"paths": ["api:*"]}, category=3)
        self.assertIsNone(self.find("paths", ["api:package.json"]))  # a #4 hit
        self.assertIsNone(self.find("paths", ["api:CLAUDE.md"], category=22))
        self.assertEqual(self.find("paths", ["api:src/x.py"], category=3), "Q-1")
        (self.dir / "Q-2.json").write_text(json.dumps(  # no category: fails the schema, never trusted
            {"batch": "auth.2", "match": {"paths": ["api:*"]}, "expires_at": (NOW + timedelta(hours=1)).isoformat()}))
        self.assertIsNone(self.find("paths", ["api:package.json"]))

    def test_scope_kind_and_expiry(self):
        self.put("Q-1", {"paths": ["api:pyproject.toml"], "commands": ["uv add --dev *"]})
        self.put("Q-2", {"tools": ["mcp__gh__*"]}, expires=NOW)  # expired at NOW
        self.put("Q-3", {"paths": ["api:*.lock"]}, batch="auth.3")
        (self.dir / "Q-4.json").write_text(json.dumps({"batch": "auth.2", "match": {}, "expires_at": "soon"}))
        (self.dir / "Q-5.json").write_text("{not json")
        self.assertEqual(self.find("paths", ["/abs/pyproject.toml", "api:pyproject.toml"]), "Q-1")
        self.assertEqual(self.find("commands", ["uv add --dev pytest"]), "Q-1")
        self.assertIsNone(self.find("commands", ["uv add requests"]))
        self.assertIsNone(self.find("tools", ["uv add --dev x"]))  # a command glob never exempts a tool
        self.assertIsNone(self.find("tools", ["mcp__gh__create_pr"]))  # expired
        self.assertIsNone(self.find("paths", ["api:uv.lock"]))  # other batch
        self.assertEqual(self.find("paths", ["api:uv.lock"], batch="auth.3"), "Q-3")
        self.assertIsNone(self.find("paths", ["api:pyproject.toml"], batch=None))

    def test_batch_scope_ends_at_delivery(self):
        self.put("Q-1", {"paths": ["api:pyproject.toml"]})
        for header, want in (({"state": "approved"}, "Q-1"), ({"state": "delivered"}, None),
                             ({"state": "merged"}, None), ({"state": "blocked", "state_prior": "delivered"}, None),
                             ({"state": "blocked", "state_prior": "running"}, "Q-1"), ({}, "Q-1")):
            self.assertEqual(self.find("paths", ["api:pyproject.toml"], header=header), want, header)


if __name__ == "__main__":
    unittest.main()
