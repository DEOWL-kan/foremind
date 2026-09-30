"""m2d.10 REQ-22: the suggested values recomputed from events (signals.py), pure functions over hand-built events."""
import unittest

from foremind import signals

USAGE = {"input_tokens": 1000, "cache_write_tokens": 400_000, "cache_read_tokens": 0, "output_tokens": 2000}


class Events:
    def __init__(self):
        self.evs = []

    def __call__(self, type, phase="result", **kw):
        self.evs.append({"id": f"e{len(self.evs)}", "type": type, "phase": phase, **kw})
        return self.evs[-1]


def item(items, name):
    return next(x for x in items if x["name"] == name)


class ControllerLinesTest(unittest.TestCase):
    def test_knee_and_threshold(self):
        ev = Events()
        for _ in range(9):  # H = 1000 + 1.25·400000 + 5·2000 = 511000; K = 40 / 2
            ev("controller_takeover", context_tokens=80_000, calls=5, **USAGE)
            ev("controller_handoff", calls=40, context_tokens=300_000)
        ev("controller_takeover", context_tokens=80_000, calls=None, **USAGE)  # a null field: not counted
        self.assertEqual(item(signals.controller_lines(ev.evs, {}), "总控软线")["suggest"], "样本不足（9/10）")
        ev("controller_takeover", context_tokens=80_000, calls=5, **USAGE)
        ev("controller_handoff", calls=40, context_tokens=300_000)
        it = item(signals.controller_lines(ev.evs, {}), "总控软线")
        # c* = 80000 + 10·511000/20 = 335500 → 340000; the current soft line is controller.SOFT
        self.assertEqual((it["current"], it["n"], it["suggest"]),
                         (200_000, 10, "340000（+140000），不低于当前硬线 300000，需一并调硬线"))
        cfg = {"context.by_role.controller.hard_pct": 40}  # hard 400000, soft 20/40 of it
        self.assertEqual(item(signals.controller_lines(ev.evs, cfg), "总控软线")["suggest"], "340000（+140000）")
        ev.evs = [dict(e, input_tokens=10 ** 8) if e["type"] == "controller_takeover" else e for e in ev.evs]
        self.assertEqual(item(signals.controller_lines(ev.evs, cfg), "总控软线")["suggest"],
                         "400000（+200000），不低于当前硬线 400000，需一并调硬线")

    def test_hard_line_from_slips_only_tightens(self):
        ev = Events()
        for c in (160_000, 170_000, 180_000, 190_000):
            ev("controller_slip", kind="other", context_tokens=c)
        ev("controller_slip", kind="other", context_tokens=None)
        self.assertEqual(item(signals.controller_lines(ev.evs, {}), "总控硬线")["suggest"], "维持（样本不足（4/5））")
        ev("controller_slip", kind="other", context_tokens=200_000)
        # p20 of 160k..200k (inclusive) = 168000 → 160000
        self.assertEqual(item(signals.controller_lines(ev.evs, {}), "总控硬线")["suggest"], "160000（-140000）")
        high = Events()
        for c in (340_000, 350_000, 360_000, 380_000, 400_000):
            high("controller_slip", kind="other", context_tokens=c)
        self.assertEqual(item(signals.controller_lines(high.evs, {}), "总控硬线")["suggest"], "300000（+0）")
        low = {"context.by_role.controller.hard_pct": 12}  # hard 120000, below the range: kept, never raised (r1)
        it = item(signals.controller_lines(ev.evs, low), "总控硬线")
        self.assertEqual((it["current"], it["suggest"]), (120_000, "120000（+0）"))


class SeatLinesTest(unittest.TestCase):
    def test_knee_by_accept_and_written_and_dirty(self):
        ev = Events()
        req = {"type": "handoff_requested", "role": "seat"}
        ev(**req, window=200_000, dirty=2, context_tokens=290_000, soft=130_000, hard=160_000)
        ev(**req, window=1_000_000, dirty=0, context_tokens=310_000, soft=200_000, hard=300_000)
        ev(**req, window=1_000_000, dirty=0, context_tokens=None, soft=1, hard=2)  # no reading
        for i in range(10):
            ev("handoff_accept", phase="intent", dedupe_id=f"a{i}")  # no usage on the intent
            ev("handoff_accept", context_tokens=80_000, calls=5, **USAGE)
            ev("handoff_written", calls=40, dirty=1 if i < 3 else 0, **USAGE, context_tokens=300_000,
               requested=i < 2)
        soft, hard = signals.seat_lines(ev.evs, {})
        # the lines the latest request with a reading applied (r1: the seat's model and window included)
        self.assertEqual((soft["current"], hard["current"]), (200_000, 300_000))
        self.assertEqual(soft["suggest"], "340000（+140000），不低于当前硬线 300000，需一并调硬线")
        self.assertIn("交接请求 1/3，写交接 3/10", soft["stats"])
        self.assertEqual((hard["n"], hard["suggest"]), (2, "维持"))
        self.assertEqual(hard["stats"], "到交接点的请求 2 次，请求时上下文中位数 300000；写交接 10 次，其中因请求而写 2")
        # r2: a planner's request (its own lines) is no seat's, even when it is the latest
        ev("handoff_requested", role="planner", dirty=5, context_tokens=100_000, soft=60_000, hard=80_000)
        self.assertEqual(signals.seat_lines(ev.evs, {}), [soft, hard])
        soft, hard = signals.seat_lines([], {})
        self.assertEqual((soft["current"], hard["current"], hard["n"]), (130_000, 160_000, 0))


class ReviewTest(unittest.TestCase):
    def review(self, ev, batch, effort, verdicts, usd=1.0, sec=False, difficulty="M", **use):
        for n, v in enumerate(verdicts, 1):
            s = f"{batch}-{n}"
            ev("review_started", phase="intent", dedupe_id=s, batch=batch, round=n, reviewer_session=s,
               effort=effort, difficulty=difficulty, security=sec)
            ev("review_receipt", batch=batch, round=n, verdict=v, reviewer_session=s, cost_usd=usd, **use)

    def test_strength_groups_and_ab(self):
        ev = Events()
        cfg = {"routes.reviewer.effort_m": "high"}
        for i in range(10):
            self.review(ev, f"h{i}", "high", ["changes_requested", "approved"])
            self.review(ev, f"m{i}", "medium", ["approved"])
            ev("ab_review", batch=f"h{i}", effort="medium", base_effort="high", overlap=0, matched=1 if i else 0,
               base_must_fix=1, cost_usd=0.5)
            ev("ab_review", batch=f"h{i}", effort="low", base_effort="high", matched=0, base_must_fix=0)  # no recall
            ev("ab_review", batch=f"h{i}", effort="low", base_effort="xhigh", matched=1, base_must_fix=1)  # other base
        self.review(ev, "s1", "xhigh", ["approved"], sec=True)
        items = signals.strength(ev.evs, cfg, signals.batches(ev.evs), {}, {})
        m = item(items, "审查强度（M·非安全）")
        self.assertEqual((m["current"], m["n"]), ("high", 20))
        self.assertIn("high：10 批，到 approved 平均 2.0 轮，每批 $2.0", m["stats"])
        self.assertIn("对照 medium（基准 high）：10 次，召回 0.9", m["stats"])
        self.assertNotIn("对照 low", m["stats"])
        self.assertEqual(m["suggest"], "可考虑放宽到 medium（对 high 的召回 0.9，平均轮数 1.0 ≤ 2.0）")
        self.assertEqual(item(items, "审查强度（M·安全）")["suggest"], "维持")
        # with xhigh current, the arms measured against high do not count (r1)
        m = item(signals.strength(ev.evs, {"routes.reviewer.effort_m": "xhigh"}, signals.batches(ev.evs), {}, {}),
                 "审查强度（M·非安全）")
        self.assertIn("对照 low（基准 xhigh）：10 次，召回 1.0", m["stats"])
        self.assertNotIn("对照 medium", m["stats"])
        self.assertEqual(m["suggest"], "维持")  # no xhigh reviews: its rounds are unknown, so none "not higher"
        ev.evs = [e for e in ev.evs if not (e["type"] == "ab_review" and e["batch"] == "h9")]
        m = item(signals.strength(ev.evs, cfg, signals.batches(ev.evs), {}, {}), "审查强度（M·非安全）")
        self.assertEqual(m["suggest"], "维持（对照样本不足（9/10））")

    def test_recall_paired_again_from_the_files(self):  # m2e REQ-1: events before m2e carry only overlap
        ev = Events()
        self.review(ev, "h0", "high", ["changes_requested", "approved"])
        mf = [{"severity": "must_fix", "location": "main:a.py:664"}, {"severity": "note", "location": "main:c.py",
                                                                       "was": "must_fix", "filtered": "no_basis"}]
        base = {"round": 1, "heads": {"main": "x"}, "issues": [
            {"severity": "must_fix", "location": "main:a.py:673"}, {"severity": "note", "location": "c.py:9",
                                                                     "was": "must_fix"},
            {"severity": "must_fix", "location": "main:b.py:1"}, {"severity": "should_fix", "location": "main:d.py"}]}
        for s in ("s1", "s2", "s3", "s4"):
            ev("ab_review", batch="h0", round=None, base_round=1, reviewer_session=s, effort="medium",
               base_effort="high", overlap=0, base_must_fix=3, **({"matched": 3} if s == "s2" else {}))
        files = {"s1": {"heads": {"main": "x"}, "issues": mf}}  # s2: event's matched; s3, s4: nothing to go on
        m = item(signals.strength(ev.evs, {"routes.reviewer.effort_m": "high"}, signals.batches(ev.evs),
                                  {"h0": [base]}, files), "审查强度（M·非安全）")
        self.assertIn("对照 medium（基准 high）：2 次，召回 0.83", m["stats"])  # (2/3 + 3/3) / 2
        self.assertIn("对照（基准 high）无法复算 2 次", m["stats"])
        self.assertIn("召回（按位置配对的 must_fix ÷ 基准 must_fix", m["rule"])

    def test_dollar_caps(self):
        ev = Events()
        for i in range(10):
            self.review(ev, f"b{i}", "high", ["changes_requested", "approved"], usd=float(i + 1))
        ev("review_receipt", batch="b0", round=3, verdict="approved", rebound_from={"main": "x"}, cost_usd=99)
        items = signals.dollars(ev.evs, {"review.cost_cap_usd_m": 30}, signals.batches(ev.evs))
        run, m = item(items, "审查单次美元上限"), item(items, "审查每批美元上限（M）")
        self.assertEqual((run["current"], run["n"]), (None, 20))  # the rebound copy is no run
        self.assertAlmostEqual(run["suggest"], round(signals.pct([float(i) for i in range(1, 11)] * 2, 95) * 1.5, 2))
        self.assertEqual((m["current"], m["n"]), (30, 10))
        self.assertAlmostEqual(m["suggest"], round(signals.pct([2.0 * i for i in range(1, 11)], 90) * 1.2, 2))
        self.assertEqual(item(items, "审查每批美元上限（S）")["suggest"], "样本不足（0/10）")

    def test_token_caps(self):  # m2e REQ-2: same rule and threshold as the dollar ones, input-equivalent tokens
        ev = Events()
        use = {"input_tokens": 100, "cache_write_tokens": 40, "cache_read_tokens": 1000, "output_tokens": 20}
        for i in range(10):  # 100 + 50 + 100 + 100 = 350 per review
            self.review(ev, f"b{i}", "high", ["changes_requested", "approved"], **{**use, "input_tokens": 100 * i})
        ev("review_failed", batch="b0", cost_usd=1, **{**use, "output_tokens": None})  # after approved; not counted
        ev("review_receipt", batch="b1", round=3, verdict="approved", rebound_from={"main": "x"})
        bs = signals.batches(ev.evs)
        self.assertEqual((bs["b0"]["tokens"], bs["b0"]["counts"], bs["b3"]["tokens"]), (500, 2, 2 * (250 + 300)))
        items = signals.dollars(ev.evs, {"review.cost_cap_tokens_m": 5000}, bs)
        m = item(items, "审查每批 token 上限（M）")
        self.assertEqual((m["current"], m["n"]), (5000, 10))
        self.assertEqual(m["suggest"], round(signals.pct([2 * (250 + 100 * i) for i in range(10)], 90) * 1.2))
        self.assertEqual(item(items, "审查每批 token 上限（L）")["suggest"], "样本不足（0/10）")
        self.assertIs(signals.PRICE, signals.review.PRICE)  # one table


def receipt(n, verdict, must=(), resolved=(), rebound=False):
    r = {"round": n, "scope": "incremental" if n > 1 else "full", "verdict": verdict, "resolved": list(resolved),
         "issues": [{"fingerprint": fp, "severity": "must_fix", "status": "new"} for fp in must]}
    return {**r, "rebound_from": {"main": "x"}} if rebound else r


class RoundsCapTest(unittest.TestCase):
    def test_late_must_fix_acceptance(self):
        cr = "changes_requested"
        chain = [receipt(1, cr, ["a"]), receipt(2, cr, ["b"], ["a"]), receipt(3, cr, ["c", "d"], ["b"]),
                 receipt(4, "approved", resolved=["c", "d"])]
        ev = Events()
        ev("review_withdrawn", batch="p.1", fingerprint="d", reason="r")
        self.assertEqual(signals.late_must_fix(ev.evs, {"p.1": chain}), (1, 2))  # d was withdrawn
        del chain[3]["resolved"]  # a receipt from before REQ-16: nothing to judge by
        self.assertEqual(signals.late_must_fix(ev.evs, {"p.1": chain}), (0, 0))
        many = {f"p.{i}": [receipt(1, cr), receipt(2, cr), receipt(3, cr, ["x", "y"]), receipt(4, cr, [], [])]
                for i in range(10)}
        it = signals.rounds_cap([], {}, many)
        self.assertEqual((it["current"], it["n"], it["suggest"]), (3, 20, 2))
        self.assertEqual(signals.rounds_cap([], {"review.max_rounds": 2}, many)["suggest"], "维持")  # never below 2

    def test_rebound_receipts_are_skipped(self):
        """m2d.5 r1 note: a rebound copy keeps its original's resolved, which is against the original's predecessor;
        the must_fix of r3 is judged by r5, not by the copy in between."""
        cr = "changes_requested"
        chain = [receipt(1, cr), receipt(2, cr), receipt(3, cr, ["x"]), receipt(4, "approved", resolved=["x"],
                                                                               rebound=True),
                 receipt(5, cr, ["x"])]
        self.assertEqual(signals.late_must_fix([], {"p.1": chain}), (0, 1))
        chain[4]["resolved"] = ["x"]
        self.assertEqual(signals.late_must_fix([], {"p.1": chain}), (1, 1))


class StuckTest(unittest.TestCase):
    def reminds(self, n, recovered):
        ev = Events()
        for i in range(n):
            ev("sv_say", phase="intent", dedupe_id=f"sv_say:stuck:p.1:s:{i}:remind")
            ev("sv_say", dedupe_id=f"sv_say:stuck:p.1:s:{i}:remind", ok=True)
            ev("sv_say", dedupe_id=f"sv_say:stuck:p.1:s:{i}:ask", ok=True)  # an ask is no remind
        for i in range(recovered):
            ev("stuck_recovered", batch="p.1", session="s", stage="remind", after_s=60)
        ev("stuck_recovered", batch="p.1", session="s", stage="ask", after_s=60)  # the ask's doing: not counted
        return ev.evs

    def test_remind_interval(self):
        self.assertEqual(signals.remind(self.reminds(9, 9), {})["suggest"], "样本不足（9/10）")
        self.assertEqual(signals.remind(self.reminds(10, 9), {})["suggest"], "可考虑放宽到 30（恢复占比 90%）")
        self.assertEqual(signals.remind(self.reminds(10, 9), {"stuck.remind_min": 35})["suggest"],
                         "可考虑放宽到 40（恢复占比 90%）")  # within 0.5–2 × the code default 20
        self.assertEqual(signals.remind(self.reminds(10, 2), {})["suggest"], "15")
        self.assertEqual(signals.remind(self.reminds(10, 2), {"stuck.remind_min": 12})["suggest"], "10")
        self.assertEqual(signals.remind(self.reminds(10, 5), {})["suggest"], "维持")
        # r1: the range never turns a rule around; a value already past it is kept
        self.assertEqual(signals.remind(self.reminds(10, 2), {"stuck.remind_min": 8})["suggest"], "维持（已在 10–40 之外或限值上）")
        self.assertEqual(signals.remind(self.reminds(10, 9), {"stuck.remind_min": 45})["suggest"],
                         "维持（已在 10–40 之外或限值上）")
        self.assertIn("恢复 10，其中提醒后即恢复 9（90%）", signals.remind(self.reminds(10, 9), {})["stats"])

    def test_api_retries(self):
        ev = Events()
        for t in [1] * 9 + [3]:
            ev("api_retry_result", batch="p.1", session="s", error_id="x", tries=t, ok=True)
        ev("api_retry_result", batch="p.1", session="s", error_id="y", tries=3, ok=False)
        it = signals.retries(ev.evs, {})
        self.assertEqual((it["current"], it["n"], it["suggest"]), (3, 10, 3))
        for _ in range(10):
            ev("api_retry_result", batch="p.1", session="s", error_id="x", tries=1, ok=True)
        self.assertEqual(signals.retries(ev.evs, {})["suggest"], 1)  # 19 of 20 within 1


class ComputeTest(unittest.TestCase):
    def test_empty_log_still_has_every_item(self):  # r1: 审查强度 was missing with no reviews
        items = signals.compute([], {}, {})
        self.assertEqual([x["name"] for x in items], [
            "总控软线", "总控硬线", "席位软线", "席位交接点",
            *(f"审查强度（{d}·{s}）" for d in "SML" for s in ("非安全", "安全")),
            "审查单次美元上限", "审查每批美元上限（S）", "审查每批美元上限（M）", "审查每批美元上限（L）",
        "审查每批 token 上限（S）", "审查每批 token 上限（M）", "审查每批 token 上限（L）",
            "审查轮次上限", "卡住提醒间隔（分钟）", "API 续跑次数"])
        m = item(items, "审查强度（M·非安全）")
        self.assertEqual((m["current"], m["n"], m["stats"], m["suggest"]),
                         ("xhigh", 0, "无审查记录", "维持（对照样本不足（0/10））"))


if __name__ == "__main__":
    unittest.main()
