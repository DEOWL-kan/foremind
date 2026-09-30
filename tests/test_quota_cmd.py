import json
import os
import unittest
from unittest import mock

from foremind.events import EventLog
from foremind.fsutil import global_lock
from test_install_fixture import Env


class QuotaCmdTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.root = self.env.tmp / "shop"
        (self.root / ".foremind").mkdir(parents=True)
        self.path = self.env.config / "quota.json"  # the account's, every project's (m2b.10)

    def write(self, path=None):
        p = path or self.path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"groups": {"claude/long": {"state": "exhausted", "window": "7d"},
                                            "claude/oneshot": {"state": "low"}},
                                 "paused": ["fm-shop-p_1-1"], "x": 1}))

    def run_cmd(self, *argv):
        return self.env.run("quota", *argv, project=self.root)

    def resets(self):
        return [e["groups"] for e in EventLog(self.root / ".foremind" / "events.jsonl").iter()
                if e["type"] == "quota_reset"]

    def test_show(self):
        rc, out = self.run_cmd()
        self.assertEqual(rc, 0, out)
        self.assertIn("claude/long: 没有记录", out)
        self.write()
        rc, out = self.run_cmd()
        self.assertIn('claude/long: {"state": "exhausted", "window": "7d"}', out)
        self.assertIn('claude/oneshot: {"state": "low"}', out)

    def test_reset_one_group_then_both(self):
        self.write()
        rc, out = self.run_cmd("--reset", "--group", "long")
        self.assertEqual(rc, 0, out)
        q = json.loads(self.path.read_text())
        self.assertEqual(q["groups"], {"claude/oneshot": {"state": "low"}})
        self.assertEqual((q["paused"], q["x"]), (["fm-shop-p_1-1"], 1))  # the rest stays
        self.assertGreater(q["exhausted_ended"], 0)  # exhausted long dropped: as the tick's qset would
        self.assertEqual(self.resets(), [["claude/long"]])
        rc, out = self.run_cmd("--reset")
        self.assertEqual(rc, 0, out)
        q = json.loads(self.path.read_text())
        self.assertEqual((q["groups"], q["paused"]), ({}, ["fm-shop-p_1-1"]))
        self.path.write_text(json.dumps({"groups": {"claude/long": {"state": "low"}}}))
        self.run_cmd("--reset")
        self.assertNotIn("exhausted_ended", json.loads(self.path.read_text()))  # not exhausted: nothing ended
        self.assertEqual(self.resets()[-1], ["claude/long", "claude/oneshot"])

    def test_reset_folds_in_the_old_project_file_first(self):  # m2b.10: or the next tick would bring it back
        old = self.root / ".foremind" / "quota.json"
        self.write(old)
        rc, out = self.run_cmd()
        self.assertIn('claude/long: {"state": "exhausted"', out, "shown folded in before any tick (r1 #3)")
        self.assertTrue(old.exists(), "showing writes nothing")
        self.assertFalse(self.path.exists())
        rc, out = self.run_cmd("--reset", "--group", "long")
        self.assertEqual(rc, 0, out)
        q = json.loads(self.path.read_text())
        self.assertEqual((q["groups"], q["paused"]), ({"claude/oneshot": {"state": "low"}}, ["fm-shop-p_1-1"]))
        self.assertFalse(old.exists())
        types = [e["type"] for e in EventLog(self.root / ".foremind" / "events.jsonl").iter()]
        self.assertEqual(types, ["quota_migrated", "quota_reset"])

    def test_refused(self):
        self.write()
        before = self.path.read_text()
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-p_1-1"}):
            for argv in (("--reset",), ()):  # showing too: the command is the user's (review r2)
                rc, out = self.run_cmd(*argv)
                self.assertEqual(rc, 1)
                self.assertIn("only the user", out)
                self.assertNotIn("claude/long", out)
        with global_lock():  # a tick is running
            rc, out = self.run_cmd("--reset")
        self.assertEqual(rc, 1)
        self.assertIn("try again", out)
        rc, out = self.run_cmd("--group", "long")
        self.assertEqual(rc, 1)
        self.assertEqual((self.path.read_text(), self.resets()), (before, []))


if __name__ == "__main__":
    unittest.main()
