"""m2f REQ-3 / REQ-4: each should_fix and note's conflict with a REQ's text in the receipt, the gate sending the batch
back on a checked one (req_conflict), and the return categories on request_changes."""
import json
import unittest

from foremind import lock, review, schemas
from foremind.paths import state_dir
from test_gate_flow import Base

GOAL = ("# 目标\n\nREQ-1: 登录返回刷新令牌。\n  WHEN 令牌过期 THEN 用刷新令牌换新\n"
        "REQ-2: [防对抗] 守卫拦下 rm -rf 与 git push --force。\n约束：\n- 只用标准库\n")
META = {"batch": "shop.1", "scope": "full", "heads": {"api": "a" * 40}, "reviewer_session": "s", "model": "m",
        "effort": "high", "round": 1}
YES = {"conflict": True, "req": "REQ-1", "quote": "WHEN 令牌过期\nTHEN 用刷新令牌换新"}


def rules(**kw):
    return {"reqs": ["REQ-1", "REQ-2"], "texts": review.req_texts(GOAL), "adversarial": {"REQ-2"},
            "withdrawn": set(), "delta": None, "cap": None, **kw}


def it(sev, n, **kw):
    return {"severity": sev, "location": f"api:src/a.py:{n}", "summary": f"issue {n}", **kw}


def conflicts(r):
    return [(i["severity"], i.get("conflict"), i.get("conflict_why")) for i in r["issues"]]


class AssembleTest(unittest.TestCase):
    def test_four_values(self):
        out = {"verdict": "approved", "issues": [
            it("note", 1, **YES), it("should_fix", 2, conflict=False), it("note", 3),
            it("note", 4, conflict="true"), it("should_fix", 5, conflict=1),
            it("note", 6, conflict=True, req="REQ-1", quote="只用标准库")]}
        r = review.assemble(out, META, [], rules())
        self.assertEqual(conflicts(r), [("note", "yes", None), ("should_fix", "no", None), ("note", "missing", None),
                                        ("note", "missing", None), ("should_fix", "missing", None),
                                        ("note", "rejected", "quote_mismatch")])
        self.assertEqual((r["issues"][0]["req"], r["issues"][0]["quote"]), ("REQ-1", YES["quote"]))  # as given
        self.assertNotIn("quote", r["issues"][5])
        self.assertEqual(schemas.validate("review_receipt", r), [])

    def test_rejected_takes_the_first_failing_check(self):
        gone = review.fingerprint("api:src/a.py", "issue 7")
        q = "守卫拦下 rm -rf"
        out = {"verdict": "approved", "issues": [
            it("note", 1, conflict=True, req="REQ-9", quote=""),  # both fail: the first counts
            it("note", 2, conflict=True, quote=q),
            it("note", 3, conflict=True, req="REQ-1", quote=" \n "),
            it("note", 4, conflict=True, req="REQ-1"),
            it("note", 5, conflict=True, req="REQ-1", quote="守卫拦下"),
            it("note", 6, conflict=True, req="REQ-2", quote=q, form="new"),
            it("note", 7, conflict=True, req="REQ-2", quote=q, form="listed"),
            it("note", 8, conflict=True, req="REQ-2", quote=q)]}
        r = review.assemble(out, META, [], rules(withdrawn={gone}))
        self.assertEqual([i.get("conflict_why") for i in r["issues"]],
                         ["not_batch_req", "not_batch_req", "no_quote", "no_quote", "quote_mismatch",
                          "outside_coverage", "withdrawn", None])
        self.assertEqual(r["issues"][7]["conflict"], "yes")  # [防对抗] without form is not a new one
        r = review.assemble({"verdict": "approved", "issues": [it("note", 1, **YES)]}, META, [], rules(texts=None))
        self.assertEqual(conflicts(r), [("note", "rejected", "goal_unreadable")])
        self.assertEqual(schemas.validate("review_receipt", r), [])

    def test_must_fix_conflict_is_ignored_lowered_ones_count(self):
        reg = {"basis": "regression", "broken": "api:src/b.py:1"}
        out = {"verdict": "changes_requested", "issues": [it("must_fix", 1, **reg, **YES), it("must_fix", 2, **YES)]}
        r = review.assemble(out, META, [], rules())
        self.assertEqual(conflicts(r), [("must_fix", None, None), ("note", "yes", None)])  # 2: no basis, now a note
        self.assertNotIn("quote", r["issues"][0])
        self.assertEqual(schemas.validate("review_receipt", r), [])

    def test_without_rules_nothing_is_checked(self):  # --ab passes none
        out = {"verdict": "approved", "issues": [it("note", 1, **YES), it("should_fix", 2)]}
        r = review.assemble(out, META, [])
        self.assertEqual(conflicts(r), [("note", None, None), ("should_fix", None, None)])
        self.assertEqual(schemas.validate("review_receipt", r), [])

    def test_schema_ties(self):
        r = review.assemble({"verdict": "approved", "issues": [it("note", 1, **YES)]}, META, [], rules())
        bad = [({"severity": "must_fix"}, "only on should_fix and note"), ({"quote": None}, "needs req and quote"),
               ({"conflict": "rejected"}, "go together"), ({"conflict": "no", "conflict_why": "no_quote"}, "go together")]
        for change, want in bad:
            i = {k: v for k, v in {**r["issues"][0], **change}.items() if v is not None}
            x = {**r, "issues": [i], "verdict": "changes_requested" if i["severity"] == "must_fix" else "approved"}
            self.assertIn(want, "; ".join(schemas.validate("review_receipt", x)), change)

    def test_return_categories(self):
        self.assertEqual(schemas.RETURN_CATEGORIES, ("deviates_req", "regression", "missing_test", "overreach",
                                                     "protocol", "controller_slip", "other"))
        self.assertIn("conflict", review.REVIEW_SCHEMA["properties"]["issues"]["items"]["required"])


class Flow(Base):
    def project(self, **kw):
        p = super().project(**kw)
        goal = state_dir(p.root) / "plans" / "shop" / "goal.md"
        goal.parent.mkdir(parents=True)
        goal.write_text(GOAL)
        return p

    def events(self, p, type_):
        return [e for e in review.all_events(p.root) if e["type"] == type_]


class HarvestTest(Flow):
    def test_rejected_events_and_the_same_bytes_again(self):
        p = self.project()
        r = p.run_review({"verdict": "approved", "issues": [
            it("note", 1, **YES), it("should_fix", 2, conflict=True, req="REQ-2", quote="x"),
            it("note", 3, conflict=False)]})
        self.assertEqual(conflicts(r), [("note", "yes", None), ("should_fix", "rejected", "not_batch_req"),
                                        ("note", "no", None)])
        fp = r["issues"][1]["fingerprint"]
        want = {"batch": "shop.1", "round": 1, "fingerprint": fp, "req": "REQ-2", "why": "not_batch_req",
                "dedupe_id": f"conflict_rejected:shop.1:{r['reviewer_session']}:{fp}"}
        [e] = self.events(p, "review_conflict_rejected")
        self.assertEqual({k: e[k] for k in want}, want)
        path = review.receipts(p.root, "shop.1")[-1][1]
        data = path.read_bytes()
        path.unlink()  # a crash after the events, before the file: harvested again
        self.assertEqual(p.harvest(r["reviewer_session"]), r)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(len(self.events(p, "review_conflict_rejected")), 1)


class GateTest(Flow):
    OUT = {"verdict": "approved", "issues": [it("note", 1, **YES), it("should_fix", 2, conflict=False)]}

    def test_conflict_sends_the_batch_back(self):
        p = self.project()
        p.run_review(self.OUT)
        lock.acquire(p.root, "shop.1", "fm-shop-shop.1-2")
        res = self.gate(p)
        self.assertEqual((res["verdict"], self.failing(res), res["state"], p.state()),
                         ("fail", ["req_conflict"], "running", "running"))
        self.assertEqual(self.check(res, "req_conflict")["detail"], "api:src/a.py:1 REQ-1")
        self.assertNotIn("acceptance", [c["name"] for c in res["checks"]])
        self.assertEqual((self.events(p, "accept_run"), self.events(p, "checks_run")), ([], []))
        back = next(e for e in self.events(p, "batch_state") if e["state"] == "changes_requested")
        self.assertEqual({k: back[k] for k in ("by", "reason", "category")}, {"by": "gate", "reason": "req_conflict",
                                                                               "category": "deviates_req"})
        [cr] = self.events(p, "changes_requested_by")
        self.assertEqual((cr["by"], cr["category"]), ("gate", "deviates_req"))
        self.assertEqual(cr["items"], ["api:src/a.py:1：issue 1（REQ-1 原文：「WHEN 令牌过期 THEN 用刷新令牌换新」）"])
        self.assertIn("审查意见与 REQ 原文冲突，自动退回（gate，", review.batch_path(p.root, "shop.1").read_text())
        self.assertEqual(self.events(p, "gate_result")[-1]["failing"], ["req_conflict"])

    def test_receipt_checks_failing_take_the_old_path(self):
        p = self.project()
        r = p.run_review(self.OUT)
        p.commit(text="x = 2\n")  # receipt heads are not the current heads
        res = self.gate(p)
        self.assertEqual((self.failing(res), p.state()), (["acceptance", "checks", "receipt"], "in_review"))
        p = self.project()
        r = p.run_review(self.OUT)
        (state_dir(p.root) / "batches" / "shop.1.lock").write_text(r["reviewer_session"] + "\n")
        self.assertEqual((self.failing(self.gate(p)), p.state()), (["acceptance", "checks", "reviewer"], "in_review"))
        p = self.project()
        p.run_review(self.OUT)
        path = review.receipts(p.root, "shop.1")[-1][1]
        path.write_text(json.dumps(json.loads(path.read_text())))  # other bytes, no event for them
        self.assertEqual((self.failing(self.gate(p)), p.state()),
                         (["acceptance", "checks", "receipt_event"], "in_review"))
        self.assertEqual(self.events(p, "changes_requested_by"), [])

    def test_approved_and_delivered_unchanged(self):
        p = self.project()
        p.run_review(self.OUT)
        review.set_state(p.root, "shop.1", "approved", expect=("in_review",))
        res = self.gate(p)
        self.assertEqual((res["verdict"], res["state"]), ("pass", "delivered"))
        self.assertNotIn("req_conflict", [c["name"] for c in res["checks"]])
        self.assertEqual((self.gate(p)["state"], self.events(p, "changes_requested_by")), ("delivered", []))


class RequestChangesTest(Flow):
    def test_category(self):
        p = self.project()
        p.run_review()
        with self.assertRaisesRegex(review.FlowError, "not one of"):
            review.request_changes(p.root, "shop.1", ["x"], by="c1", category="taste")
        self.assertEqual(p.state(), "in_review")
        review.request_changes(p.root, "shop.1", ["x"], by="c1", category="missing_test")
        self.assertEqual(self.events(p, "changes_requested_by")[-1]["category"], "missing_test")
        self.assertEqual(self.events(p, "batch_state")[-1]["category"], "missing_test")
        review.set_state(p.root, "shop.1", "running")
        p.commit(text="x = 2\n")
        p.run_review()
        review.request_changes(p.root, "shop.1", ["y"], by="c1")  # the program's other moves carry none
        self.assertNotIn("category", self.events(p, "changes_requested_by")[-1])
        self.assertNotIn("category", next(e for e in reversed(self.events(p, "batch_state"))
                                          if e["state"] == "changes_requested"))


if __name__ == "__main__":
    unittest.main()
