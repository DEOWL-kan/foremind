import json
import unittest

from foremind import install
from test_install_fixture import Env


def project(env, name, batches):
    sd = env.tmp / name / ".foremind"
    for d in ("batches", "heartbeats", "decisions"):
        (sd / d).mkdir(parents=True)
    for bid, state in batches:
        (sd / "batches" / f"{bid}.md").write_text(f"---\nid: {bid}\nstate: {state}\ntitle: t {bid}\n---\n")
    return sd.parent


class StatusTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def test_one_project(self):
        root = project(self.env, "shop", [("p.1", "running"), ("p.2", "running"), ("p.3", "merged")])
        sd = root / ".foremind"
        (sd / "batches" / "p.1.log.md").write_text("not a header\n")  # logs and handoffs are not batches
        (sd / "heartbeats" / "fm-shop-p_1-1.json").write_text(json.dumps(
            {"session": "fm-shop-p_1-1", "batch": "p.1", "ts": "2026-09-25T10:00:00+00:00", "tool_open": True}))
        for n, st in ((1, "open"), (2, "answered"), (3, "escalated")):
            (sd / "decisions" / f"Q-{n}.json").write_text(json.dumps({"id": f"Q-{n}", "state": st}))
        (sd / "quota.json").write_text(json.dumps({"groups": {"claude/long": {"state": "low"},
                                                              "claude/oneshot": {"state": "available"}}}))
        rc, out = self.env.run("status", project=root)
        self.assertEqual(rc, 0, out)
        self.assertIn("批次：merged 1 · running 2", out)
        self.assertIn("p.3              merged  t p.3", out)
        self.assertIn("fm-shop-p_1-1  批次 p.1  心跳 2026-09-25T10:00:00+00:00 busy_tool", out)
        self.assertIn("未决：2", out)
        self.assertIn("额度：claude/long low · claude/oneshot available", out)

    def test_all_reads_the_registry(self):
        a, b = project(self.env, "a", [("x.1", "ready")]), project(self.env, "b", [])
        for r in (a, b):
            install.set_registered(r, True)
        rc, out = self.env.run("status", "--all")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"项目 {a}", out)
        self.assertIn(f"项目 {b}", out)
        self.assertIn("批次：ready 1", out)
        self.assertIn("额度：unknown", out)
        install.set_registered(a, False)
        self.assertEqual(install.registered(), [str(b)])


if __name__ == "__main__":
    unittest.main()
