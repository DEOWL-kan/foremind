import json
import os
import unittest
from unittest import mock

from foremind import batchlog, review
from foremind.paths import state_dir
from test_gate_fixture import Project, sh
from test_review import CHANGES, cli, issue, raw_claude, result


class AbTest(unittest.TestCase):  # REQ-14
    def test_reruns_the_latest_full_review_outside_the_rounds(self):
        p = Project(self)
        raw_claude(p)
        (p.root / "foremind.toml").write_text(
            '[[repos]]\nid = "api"\npath = "api"\n\n[delivery.repo.api]\ntarget_branch = "main"\n')
        p.batch()
        p.commit(path="src/a.py", text="a = 1\n")
        batchlog.append(p.root, "shop.1", "- 开工记录", author="fm-shop-shop.1-1")
        r1 = p.run_review(result(CHANGES, usd=2.0))
        batchlog.append(p.root, "shop.1", "- r1 答复：已修", author="fm-shop-shop.1-1")
        p.commit(path="src/a.py", text="a = 2\n")
        r2 = p.run_review(result(usd=1.0))
        self.assertEqual((r1["scope"], r2["scope"]), ("full", "incremental"))
        # delivered and cleaned up: merged into the target, the worktree gone
        sh(p.root / "api", "git", "merge", "-q", "--ff-only", "fm/shop.1")
        sh(p.root / "api", "git", "worktree", "remove", "--force", str(p.wt()))
        started, n, rs = p.events("review_started"), review.next_round(review.all_events(p.root), "shop.1"), \
            review.receipts(p.root, "shop.1")

        for role in ("seat", "controller"):  # refused inside any Foremind session
            with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1", "FOREMIND_ROLE": role}):
                code, _, err = cli(["review", "shop.1", "--ab", "low"])
            self.assertEqual((code, len(p.claude_calls())), (1, 2))
            self.assertIn("Foremind session", err)

        p.review_output(result({"verdict": "changes_requested", "issues": [
            issue("must_fix", "api:src/a.py:3", "BUG"), issue("must_fix", "api:src/a.py:9", "other"),
            issue("note", "api:src/a.py:1", "nit")]}, usd=0.4))
        env = {k: v for k, v in os.environ.items() if k != "FOREMIND_SESSION"}
        with mock.patch.dict(os.environ, {**env, "FOREMIND_ROLE": "controller"}, clear=True):  # controller.py
            code, out, err = cli(["review", "shop.1", "--ab", "low"])
        # err may carry job.start's ResourceWarning (its detached runner's Popen is dropped while running, shown
        # under unittest only); the command's own errors are what must be absent
        self.assertEqual(code, 0)
        self.assertNotIn("foremind review:", err)
        out = json.loads(out)
        self.assertEqual({k: out[k] for k in ("batch", "heads", "effort", "base_round", "base_effort", "verdict",
                                              "must_fix", "overlap", "matched", "cost_usd", "base_verdict",
                                              "base_must_fix", "model", "started_by")},
                         {"batch": "shop.1", "heads": r1["heads"], "effort": "low", "base_round": 1,
                          "base_effort": "xhigh", "verdict": "changes_requested", "must_fix": 2, "overlap": 1, "matched": 1,
                          "cost_usd": 0.4, "base_verdict": "changes_requested", "base_must_fix": 1,
                          "model": r1["model"], "started_by": "user"})
        self.assertEqual(p.events("ab_review"), [{**p.events("ab_review")[0], **out}])
        argv = p.claude_calls()[-1]["argv"]
        self.assertEqual((argv[argv.index("--effort") + 1], argv[argv.index("--model") + 1]), ("low", r1["model"]))
        # r1's materials: its full diff against the recorded base, the log up to its start, no later receipt
        mat = state_dir(p.root) / "reviews" / out["reviewer_session"]
        self.assertIn("+a = 1", (mat / "api.diff").read_text())
        self.assertIn("开工记录", (mat / "log.md").read_text())
        self.assertNotIn("r1 答复", (mat / "log.md").read_text())
        self.assertFalse((mat / "previous-receipt.json").exists())
        self.assertFalse((mat / "api").exists())  # checkout dropped
        ab = json.loads((mat / "ab.json").read_text())
        self.assertEqual(({k: ab[k] for k in out}, len(ab["issues"])), (out, 3))
        meta = json.loads((mat / "meta.json").read_text())
        self.assertEqual((meta["ab"], meta["effort"], meta["cost_usd"], meta["cache_read_tokens"]), (True, "low", 0.4, None))
        # outside the rounds and the gate
        self.assertEqual((p.events("review_started"), review.next_round(review.all_events(p.root), "shop.1"),
                          review.receipts(p.root, "shop.1")), (started, n, rs))
        self.assertEqual(review.spent(review.all_events(p.root), "shop.1"), 3.0)  # not in the batch's cap

    def test_failed_comparison_is_recorded(self):
        p = Project(self)
        raw_claude(p)
        p.batch()
        with self.assertRaisesRegex(review.FlowError, "no full review receipt"):
            review.ab(p.root, "shop.1", p.cfg, "low")
        p.commit()
        r1 = p.run_review(result())
        p.review_output({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 1.1})
        p.cfg["routes.reviewer.model"] = "claude-sonnet-5"  # changed since r1: the comparison keeps r1's model
        with self.assertRaisesRegex(review.FlowError, "max_budget_usd"):
            review.ab(p.root, "shop.1", p.cfg, "medium")
        argv = p.claude_calls()[-1]["argv"]
        self.assertEqual((argv[argv.index("--model") + 1], review.route(p.cfg)[0]), (r1["model"], "claude-sonnet-5"))
        e = p.events("ab_review")[-1]
        self.assertEqual((e["reason"], e["cost_usd"], e["effort"]), ("budget", 1.1, "medium"))
        self.assertEqual(p.state(), "in_review")  # the batch is untouched


class MatchedTest(unittest.TestCase):  # m2e REQ-1
    def test_pairs_by_location_one_to_one(self):
        def mf(loc, **kw):
            return {"severity": "must_fix", "location": loc, "summary": "worded differently each time", **kw}
        repos = {"main", "web"}
        # the sample of the handoff: one defect, lines 9 apart, other words
        self.assertEqual(review.matched([mf("main:foremind/review.py:664")], [mf("main:foremind/review.py:673")],
                                        repos), 1)
        for a, b in (("main:a.py:10", "main:a.py:41"), ("main:a.py:1", "web:a.py:1"), ("main:a.py:1", "main:b.py:1"),
                     ("main:a.py:1", "main:./a.pyc:1"), ("x:a.py:1", "x:b.py:80")):  # x is no repo of heads
            self.assertEqual(review.matched([mf(a)], [mf(b)], repos), 0, (a, b))
        for a, b in (("main:a.py:10", "main:a.py:40"), ("a.py:3", "web:a.py:30"), ("a.py", "web:a.py:90"), ("main:a.py", "main:./a.py:500"),
                     ("main:a.py:12-20", "main:a.py:42"), ('main:"a\\"b.py":1', 'main:a"b.py:2'),
                     ("x:a.py:1", "x:a.py:20"),  # x is no repo of heads: path "x:a.py" on both sides
                     ("main:a.py:12 (fn)", "main:a.py:20")):
            self.assertEqual(review.matched([mf(a)], [mf(b)], repos), 1, (a, b))
        # only the reviewer's own must_fix: `was` counts, a lowered should_fix or a note does not
        low = {"severity": "note", "was": "must_fix", "location": "main:a.py:1"}
        self.assertEqual(review.matched([low], [mf("main:a.py:1")], repos), 1)
        self.assertEqual(review.matched([{"severity": "should_fix", "location": "main:a.py:1"}], [mf("main:a.py:1")],
                                        repos), 0)
        # one to one, nearest first: base 20 takes 21 though base 10 (first in order) is within reach of it too;
        # the line-less comparison goes last and takes what is left
        base = [mf("main:a.py:10"), mf("main:a.py:20"), mf("main:a.py:90")]
        self.assertEqual(review.matched([mf("main:a.py:21"), mf("main:a.py:25")], base, repos), 2)
        self.assertEqual(review.matched([mf("main:a.py:21"), mf("main:a.py")], base, repos), 2)
        self.assertEqual(review.matched([mf("main:a.py:21"), mf("main:a.py:39"), mf("main:a.py")], base, repos), 3)
        self.assertEqual(review.matched([mf("main:a.py:15")] * 3, base[:2], repos), 2)


if __name__ == "__main__":
    unittest.main()
