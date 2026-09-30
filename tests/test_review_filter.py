import json
import os
import unittest
from unittest import mock

from foremind import gate, review, schemas
from foremind.paths import state_dir
from test_gate_fixture import Project, sh
from test_review import CHANGES, REG, cli, issue

GOAL = ("# 目标\n\nREQ-1: 登录返回刷新令牌。\n  WHEN 令牌过期 THEN 用刷新令牌换新（m2c REQ-6 不变）\n"
        "REQ-2: [防对抗] 守卫拦下 rm -rf 与 git push --force。\n约束：\n- 只用标准库\n")
TEXTS = review.req_texts(GOAL)
META = {"batch": "shop.1", "scope": "full", "heads": {"api": "a" * 40}, "reviewer_session": "s", "model": "m",
        "effort": "high", "round": 3}


def rules(**kw):
    return {"reqs": ["REQ-1", "REQ-2"], "texts": TEXTS, "adversarial": {"REQ-2"}, "withdrawn": set(), "delta": None,
            "cap": None, **kw}


def req(r, quote, **kw):
    return {"basis": "req", "req": r, "quote": quote, **kw}


def mf(loc, summary, **kw):
    return issue("must_fix", loc, summary, **kw)


def screened(r):
    return [(i["severity"], i.get("filtered")) for i in r["issues"]]


class ScreenTest(unittest.TestCase):  # REQ-15
    def test_req_texts_end_at_the_next_req_or_the_constraints(self):
        self.assertEqual(set(TEXTS), {"REQ-1", "REQ-2"})
        self.assertIn("m2c REQ-6 不变", TEXTS["REQ-1"])  # a REQ named mid-line does not end the text
        self.assertTrue(TEXTS["REQ-2"].startswith("REQ-2: [防对抗]"))
        self.assertNotIn("标准库", TEXTS["REQ-2"])

    def test_each_must_fix_needs_a_basis_the_program_can_check(self):
        gone = review.fingerprint("api:src/w.py", "withdrawn one")
        out = {"verdict": "changes_requested", "issues": [
            mf("api:src/a.py:1", "no basis"),
            mf("api:src/a.py:2", "unknown basis", basis="taste"),
            mf("api:src/a.py:3", "req of another batch", **req("REQ-3", "登录返回刷新令牌")),
            mf("api:src/a.py:4", "quote not in the text", **req("REQ-1", "只用标准库")),
            mf("api:src/a.py:5", "blank quote", **req("REQ-1", " \n ")),
            mf("api:src/a.py:6", "quote with other spacing", **req("REQ-1", "WHEN令牌过期 THEN\n用刷新令牌换新")),
            mf("api:src/a.py:7", "category out of range", basis="authz", category=24),
            mf("api:src/a.py:8", "category not a number", basis="authz", category=True),
            mf("api:src/a.py:9", "authz", basis="authz", category=4),
            mf("api:src/a.py:10", "regression without where", basis="regression", broken=" "),
            mf("api:src/a.py:11", "regression", **REG),
            mf("api:src/a.py:12", "new form", **req("REQ-2", "守卫拦下 rm -rf", form="new")),
            mf("api:src/a.py:13", "listed form", **req("REQ-2", "守卫拦下 rm -rf", form="listed")),
            mf("api:src/w.py:14", "withdrawn one", **REG),
            issue("should_fix", "api:src/a.py:15", "no basis needed")]}
        r = review.assemble(out, META, [], rules(withdrawn={gone}))
        n = ("note", "no_basis")
        self.assertEqual(screened(r), [n, n, n, n, n, ("must_fix", None), n, n, ("must_fix", None), n,
                                       ("must_fix", None), ("note", "outside_coverage"), ("must_fix", None),
                                       ("note", "withdrawn"), ("should_fix", None)])
        self.assertEqual({i.get("was") for i in r["issues"] if i.get("filtered")}, {"must_fix"})
        self.assertEqual((r["issues"][5]["basis"], r["issues"][5]["req"], r["issues"][8]["basis"]),
                         ("req", "REQ-1", "authz"))
        self.assertEqual(r["issues"][2]["req"], "REQ-3")  # recorded as given
        self.assertEqual(r["verdict"], "changes_requested")
        self.assertEqual(schemas.validate("review_receipt", r), [])
        # all lowered: the program approves; the receipt is an ordinary one
        r = review.assemble({"verdict": "changes_requested", "issues": out["issues"][:2]}, META, [], rules())
        self.assertEqual((r["verdict"], screened(r)), ("approved", [n, n]))
        self.assertEqual(schemas.validate("review_receipt", r), [])
        # a verdict contradicting the reviewer's own severities is still invalid output
        with self.assertRaisesRegex(ValueError, "contradicts"):
            review.assemble({"verdict": "approved", "issues": out["issues"][:1]}, META, [], rules())

    def test_without_goal_md_nothing_is_lowered_for_what_only_its_text_tells(self):
        out = {"verdict": "changes_requested", "issues": [
            mf("api:a:1", "any quote", **req("REQ-1", "不在原文里")), mf("api:a:2", "new form", **req("REQ-2", "x", form="new")),
            mf("api:a:3", "not this batch's", **req("REQ-9", "x")), mf("api:a:4", "none")]}
        r = review.assemble(out, META, [], rules(texts=None, adversarial=set()))
        self.assertEqual(screened(r), [("must_fix", None), ("must_fix", None), ("note", "no_basis"), ("note", "no_basis")])


class RoundsTest(unittest.TestCase):  # REQ-16
    def test_reconcile_delta_and_cap(self):
        prev = review.assemble({"verdict": "changes_requested", "issues": [
            mf("api:src/a.py:1", "A", **REG), issue("should_fix", "api:src/a.py:2", "B"),
            issue("note", "api:src/a.py:3", "C"), mf("api:src/a.py:4", "D")]}, {**META, "round": 2}, [], rules())
        out = {"verdict": "changes_requested", "issues": [
            issue("should_fix", "api:src/a.py:9", "A"),  # reported again, whatever its severity now
            mf("api:src/old.py:1", "outside", **REG),
            mf("api:src/b.py:1", "first new", **REG), mf("api:src/b.py:2", "second new", **REG),
            mf("api:src/a.py:8", "D", **REG)]}  # D is a repeat (a note before): not limited
        fp = {s: review.fingerprint("api:src/a.py", s) for s in "ABCD"}
        r = review.assemble(out, META, [prev], rules(delta={"api": {"src/b.py"}}, cap=1))
        self.assertEqual((r["resolved"], r["unresolved"]), ([fp["B"]], [fp["A"]]))  # C a note, D lowered in prev
        self.assertEqual(screened(r)[1:], [("should_fix", "outside_delta"), ("must_fix", None),
                                           ("should_fix", "over_cap"), ("must_fix", None)])
        self.assertEqual(schemas.validate("review_receipt", r), [])
        r = review.assemble(out, META, [prev], rules())  # before the third round, no cap: nothing lowered
        self.assertEqual([s for s, _ in screened(r)[1:]], ["must_fix"] * 4)
        self.assertNotIn("resolved", review.assemble(out, META, [], rules()))  # no previous receipt

    def test_from_the_third_counted_round_new_must_fix_stay_in_the_increment(self):
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit(path="src/a.py", text="a = 1\n")
        r1 = p.run_review(CHANGES)
        p.commit(path="src/b.py", text="b = 1\n")
        z = mf("api:src/z.py:1", "other", **REG)
        r2 = p.run_review({"verdict": "changes_requested", "issues": [CHANGES["issues"][0], z]})
        self.assertEqual(screened(r2), [("must_fix", None)] * 2)  # round 2: not limited
        self.assertEqual((r2["resolved"], r2["unresolved"]), ([], [r1["issues"][0]["fingerprint"]]))
        p.commit(path="src/数据 c.py", text="c = 1\n")  # r1: git quotes such a path unless -z
        review.request(p.root, "shop.1", p.cfg)
        p.review_output({"verdict": "changes_requested", "issues": [
            mf("api:src/z.py:2", "third", **REG), mf("api:src/数据 c.py:1", "c bug", **REG)]})
        session, real = review.start(p.root, "shop.1", p.cfg), review.git

        def flaky(cwd, *args, **kw):
            if "--name-only" in args:
                raise review.FlowError("git hiccup")
            return real(cwd, *args, **kw)

        with mock.patch.object(review, "git", flaky), self.assertRaisesRegex(review.FlowError, "hiccup"):
            p.harvest(session)  # r1: no receipt that lifts the limit; the supervisor harvests again
        self.assertEqual((len(review.receipts(p.root, "shop.1")), p.events("review_receipt")[-1]["round"]), (2, 2))
        r3 = p.harvest(session)
        mat = state_dir(p.root) / "reviews" / session  # r2: the reviewer's materials name the path as it is, too
        self.assertIn("src/数据 c.py", (mat / "api.files.txt").read_text().splitlines())
        self.assertIn("b/src/数据 c.py", (mat / "api.delta.diff").read_text())
        self.assertEqual((r3["round"], r3["scope"]), (3, "incremental"))
        self.assertEqual(screened(r3), [("should_fix", "outside_delta"), ("must_fix", None)])
        self.assertEqual(sorted(r3["resolved"]), sorted(i["fingerprint"] for i in r2["issues"]))
        self.assertEqual(review.diagnose(review.load_receipts(p.root, "shop.1")), ["freeze_scope_or_split"])
        # r3's heads rewritten, no ancestor: not limited; review.new_must_fix_max caps each round
        sh(p.wt(), "git", "commit", "-q", "--amend", "-m", "rewritten")
        p.commit(path="src/c.py", text="c = 2\n")
        p.cfg["review.new_must_fix_max"] = "x"
        review.request(p.root, "shop.1", p.cfg)
        with self.assertRaisesRegex(review.FlowError, "new_must_fix_max"):
            review.start(p.root, "shop.1", p.cfg)
        self.assertEqual(p.state(), "review_ready")
        p.cfg["review.new_must_fix_max"] = 1
        p.review_output({"verdict": "changes_requested", "issues": [
            mf("api:src/z.py:3", "fourth", **REG), mf("api:src/z.py:4", "fifth", **REG)]})
        r4 = p.harvest(review.start(p.root, "shop.1", p.cfg))
        self.assertEqual((r4["scope"], screened(r4)), ("full", [("must_fix", None), ("should_fix", "over_cap")]))
        self.assertIn("b/src/数据 c.py", (state_dir(p.root) / "reviews" / r4["reviewer_session"] / "api.diff").read_text())
        # a round whose must_fix are all lowered approves, and the gate takes the receipt as any other
        p.commit(path="src/c.py", text="c = 3\n")
        p.cfg.pop("review.new_must_fix_max")
        r5 = p.run_review({"verdict": "changes_requested", "issues": [mf("api:src/c.py:1", "no basis given")]})
        self.assertEqual((r5["verdict"], r5["scope"], screened(r5)), ("approved", "incremental", [("note", "no_basis")]))
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["verdict"], "pass")


class DeltaTest(unittest.TestCase):  # REQ-16, r1
    def test_a_location_names_a_changed_file_in_any_common_form(self):
        d = {"api": {"src/b.py", "src/数据 c.py"}}
        for loc in ("api:src/b.py", "api:src/b.py:3", "api:src/b.py:12,30", "api:src/b.py:12:5", "api:./src/b.py:3",
                    "api:src/b.py:do_it", "src/b.py:3", " api:src/数据 c.py:1 "):
            self.assertTrue(review._in_delta(loc, d), loc)
        for loc in ("api:src/b.pyc", "api:src/b", "app:src/b.py", "api:src/c.py:1", "src/c.py"):
            self.assertFalse(review._in_delta(loc, d), loc)

    def test_quoted_paths_as_they_are(self):  # m2e REQ-7: git's C-style quoting, as `diff -z` does not quote
        d = {"api": {'src/a"b.py', "src/t\tx\\y.py", "src/数据.py", "src/n\nl.py"}}
        for loc in ('api:"src/a\\"b.py":12', '"src/a\\"b.py"', 'api:"src/t\\tx\\\\y.py":3-9',
                    'api:"src/\\346\\225\\260\\346\\215\\256.py":1', 'api:"src/n\\nl.py"'):
            self.assertTrue(review._in_delta(loc, d), loc)
        for loc in ('api:"src/a\\qb.py":1', 'api:"src/\\777.py"', 'api:"src/\\377.py"'):
            self.assertFalse(review._in_delta(loc, d), loc)  # does not unquote: compared as written
        self.assertEqual(review.locate('api:"a\\qb.py":7', {"api"}), ("api", '"a\\qb.py"', 7))
        self.assertEqual(review.locate('api:"a:b.py":7', {"api"}), ("api", "a:b.py", 7))
        self.assertEqual(review.locate('"a\\bb\\"":x', {"api"}), (None, 'a\bb"', None))
        for loc, want in (("api:./src/b.py:12-20", ("api", "src/b.py", 12)),
                          ("app:src/b.py:3", (None, "app:src/b.py", 3)),
                          ("src/b.py:fn:3", (None, "src/b.py:fn", 3)), ("api:src/b.py", ("api", "src/b.py", None)),
                          ("api:a:b.py:12:5", ("api", "a:b.py", 12)), ("x:a.py:1", (None, "x:a.py", 1)),
                          ("a.py", (None, "a.py", None)), ("api:a.py:do_it", ("api", "a.py", None))):
            self.assertEqual(review.locate(loc, {"api"}), want, loc)
        for loc in ("main:a.py:12 (fn)", "main:a.py:12-20", "main:a.py:12\u201320", "main:a.py:12, 40"):
            self.assertEqual(review.locate(loc, {"main"}), ("main", "a.py", 12), loc)


class WithdrawTest(unittest.TestCase):  # REQ-15
    def test_the_controller_withdraws_and_the_next_round_lowers_it(self):
        p = Project(self)
        (p.root / "foremind.toml").write_text(
            '[[repos]]\nid = "api"\npath = "api"\n\n[delivery.repo.api]\ntarget_branch = "main"\n')
        goal = state_dir(p.root) / "plans" / "shop" / "goal.md"
        goal.parent.mkdir(parents=True)
        goal.write_text(GOAL)
        p.batch()
        p.commit()
        fp = p.run_review(CHANGES)["issues"][0]["fingerprint"]
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1", "FOREMIND_ROLE": "seat"}):
            code, _, err = cli(["review", "shop.1", "--withdraw", fp, "--reason", "约定值"])
        self.assertEqual(code, 1)
        self.assertIn("Foremind session", err)
        for argv, why in ((["--withdraw", "0" * 16, "--reason", "r"], "no review receipt"),
                          (["--withdraw", fp], "needs --reason"), (["--reason", "r"], "goes with --withdraw"),
                          (["--withdraw", fp, "--reason", "r", "--ab", "low"], "goes alone")):
            code, _, err = cli(["review", "shop.1", *argv])
            self.assertEqual(code, 1, argv)
            self.assertIn(why, err)
        self.assertEqual(p.events("review_withdrawn"), [])
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "controller"}):  # controller.py: no FOREMIND_SESSION
            code, out, _ = cli(["review", "shop.1", "--withdraw", fp, "--reason", " x = 1 是约定值（D3） "])
        want = {"batch": "shop.1", "fingerprint": fp, "reason": "x = 1 是约定值（D3）", "by": "controller"}
        self.assertEqual((code, json.loads(out)), (0, want))
        self.assertEqual({k: p.events("review_withdrawn")[-1][k] for k in want}, want)
        p.commit(text="x = 2\n")
        r = p.run_review({"verdict": "changes_requested", "issues": [
            CHANGES["issues"][0], mf("api:src/app.py:1", "quoted", **req("REQ-1", "用刷新令牌换新"))]})
        self.assertEqual(screened(r), [("note", "withdrawn"), ("must_fix", None)])
        mat = state_dir(p.root) / "reviews" / r["reviewer_session"]
        self.assertIn(f"- {fp}（api:src/a.py:1：bug）撤回理由：x = 1 是约定值（D3）", (mat / "withdrawn.md").read_text())
        self.assertEqual((mat / "reqs.md").read_text(), TEXTS["REQ-1"].rstrip() + "\n")  # the batch's REQ-1 only
        prompt = (mat / "prompt.md").read_text()
        self.assertIn("./reqs.md", prompt)
        self.assertIn("./withdrawn.md", prompt)
        self.assertIn("## 程序对 must_fix 的核对", prompt)  # the rules, outside the capped role card
        # r1: a withdrawal while a review runs counts from the next round on, as its materials had it
        p.commit(text="x = 3\n")
        review.request(p.root, "shop.1", p.cfg)
        quoted = r["issues"][1]
        p.review_output({"verdict": "changes_requested", "issues": [
            mf(quoted["location"], quoted["summary"], **req("REQ-1", "用刷新令牌换新"))]})
        session = review.start(p.root, "shop.1", p.cfg)
        review.withdraw(p.root, "shop.1", quoted["fingerprint"], "later", by="user")
        self.assertEqual(screened(p.harvest(session)), [("must_fix", None)])


if __name__ == "__main__":
    unittest.main()
