"""m2e.10 REQ-14: precision and efficiency (metrics.py), the report's section, `report --plan`, `foremind defect`,
the controller's send-back `by`, the hook's seat_prompt_external."""
import contextlib
import io
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from foremind import cli, hooks, metrics, report
from foremind.events import EventLog
from foremind.paths import state_dir
from test_gate_fixture import Project
from test_hooks import SESSION, HookBase
from test_review import cli as review_cli
from test_supervisor import Base

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = (T0 + timedelta(minutes=400)).timestamp()
UNREAD = metrics.UNKNOWN
USE = {"input_tokens": 100, "cache_write_tokens": 0, "cache_read_tokens": 1000, "output_tokens": 10}  # 250 each


def ev(type, minute, **f):
    return {"id": f"{type}-{minute}-{len(f)}", "ts": (T0 + timedelta(minutes=minute)).isoformat(), "type": type,
            "phase": f.pop("phase", "result"), **f}


def moves(b, *pairs):
    return [ev("batch_state", m, batch=b, frm="x", to=s) for m, s in pairs]


FLOW = [ev("plan_approved", 0, plan="p"),
        *moves("p.1", (0, "ready"), (60, "running"), (120, "review_ready"), (130, "in_review")),
        ev("review_receipt", 150, batch="p.1", round=1, verdict="changes_requested", reviewer_session="r1",
           cost_usd=1.5, **USE),
        ev("batch_state", 150, batch="p.1", prior="in_review", state="changes_requested", reason="review"),
        *moves("p.1", (160, "running"), (200, "review_ready"), (210, "in_review")),
        ev("review_receipt", 240, batch="p.1", round=2, verdict="approved", reviewer_session="r2", cost_usd=0.5, **USE),
        *moves("p.1", (240, "approved"), (250, "delivered")),
        ev("reconciled", 370, batch="p.1", frm="delivered", to="merged"),
        ev("pending_created", 10, question="Q-1"), ev("pending_routed", 10, question="Q-1", to="decider"),
        ev("pending_answered", 40, question="Q-1", by="user"), ev("pending_created", 300, question="Q-2"),
        ev("seat_opened", 60, batch="p.1", session="s1", successor=False),
        ev("sv_say", 100, dedupe_id="sv_say:stuck:p.1:s1:5:remind", ok=True),
        ev("gate_result", 245, batch="p.1", verdict="fail"), ev("hook_denied", 70, batch="p.1"),
        ev("api_retry_result", 90, batch="p.1", tries=2, ok=False)]
FILES = [{"round": 1, "issues": [{"severity": "must_fix"}, {"severity": "note"},
                                 {"severity": "note", "was": "must_fix", "filtered": "over_cap"}]},
         {"round": 2, "issues": [{"severity": "note", "was": "must_fix", "filtered": "withdrawn"}]}]


def io_(**over):
    return {"receipts": {"p.1": FILES}, "qs": {"p.1": ["Q-1", "Q-2"]}, "writer": {"s1": USE}, "reviewer": {}, **over}


class PureTest(unittest.TestCase):
    def test_one_batch_through_a_send_back_to_merged(self):
        m = metrics.compute(FLOW, {"p": ["p.1"]}, {"p.1": "1000"}, io_(), NOW)
        r = m["batches"]["p.1"]
        self.assertEqual((r["verdicts"], r["musts"]), (["changes_requested", "approved"], [(2, 1, 0), (1, 0, 1)]))
        self.assertEqual([round(g / 60) for g in r["gaps"]], [60, 120, 10, 120])
        self.assertEqual(r["wait_user"], (130 * 60, 0))  # Q-1 30 min answered, Q-2 open 100 min to now
        self.assertEqual(sum(r["returns"].values()), 0)  # the reviewer's changes_requested is no send-back
        prec, eff = metrics.batch_lines("p.1", r)
        for s in ("首轮未过；轮数 2（回执 2）", "must_fix r1 2（降级 1、撤回 0） · r2 1（降级 0、撤回 1）", "门禁失败 1",
                  "协议违反 1"):
            self.assertIn(s, prec)
        for s in ("开席→提审 1.0h · 提审→approved 2.0h · approved→delivered 0.2h · delivered→merged 2.0h",
                  "写手 1.8h · 等审查 1.2h · 等开席 1.0h · 等合入 2.0h；等用户 2.2h", "写手 token 250；审查 $2.00、token 500",
                  "卡住提醒 remind 1、ask 0", "API 续跑 1 串 2 次、失败 1"):
            self.assertIn(s, eff)
        self.assertNotIn("stuck 0.0h", eff)  # the other states only when not 0

    def test_send_backs_by_kind_and_source(self):
        evs = [ev("batch_state", 1, batch="p.1", prior="delivered", state="changes_requested", reason="requested",
                  by="controller"),
               ev("batch_state", 2, batch="p.1", prior="in_review", state="changes_requested", reason="requested",
                  by="user"),
               ev("batch_state", 3, batch="p.1", prior="approved", state="changes_requested",
                  reason="head changed after approval", by="controller")]
        r = metrics.compute(evs, {}, {}, io_(receipts={"p.1": []}), NOW)["batches"]["p.1"]
        self.assertEqual(dict(r["returns"]), {("after", "controller"): 1, ("during", "user"): 1})
        self.assertIn("退回 审查中 1（总控 0、用户 1） · approved 后 1（总控 1、用户 0）", metrics.batch_lines("p.1", r)[0])

    def test_missing_fields_are_uncounted_or_unknown_never_zero(self):
        evs = [ev("review_receipt", 1, batch="p.1", verdict="approved", reviewer_session="r9"),  # before m2d.4
               ev("seat_opened", 0, batch="p.1", session="s1"), ev("seat_opened", 5, batch="p.1", session="s2")]
        got = io_(receipts={"p.1": UNREAD}, qs={"p.1": UNREAD}, writer={"s1": USE, "s2": None}, reviewer={"r9": None})
        r = metrics.compute(evs, {}, {}, got, NOW)["batches"]["p.1"]
        prec, eff = metrics.batch_lines("p.1", r)
        self.assertIn("must_fix unknown", prec)
        for s in ("等用户 unknown", "写手 token ≥ 250（1 个会话读不到）", "审查 unknown（未计 1 次）、token unknown（未计 1 次）"):
            self.assertIn(s, eff)
        r = metrics.compute(evs, {}, {}, io_(receipts={"p.1": UNREAD}, writer={}, reviewer={}), NOW)["batches"]["p.1"]
        self.assertIn("写手 token unknown；", metrics.batch_lines("p.1", r)[1])

    def test_plan_cumulative(self):
        evs = FLOW + [*moves("p.2", (100, "running"), (120, "stuck"), (150, "running")),
                      ev("reconciled", 220, batch="p.2", to="merged"),
                      ev("seat_opened", 100, batch="p.2", session="s2", successor=True),
                      ev("seat_opened", 150, batch="p.2", session="s3", successor=True),
                      ev("handoff_accept", 151, batch="p.2", session="s3"),
                      ev("notify", 50, phase="intent", dedupe_id="n1", channel="ntfy", priority="P0"),
                      ev("notify", 50, dedupe_id="n1", channel="ntfy", sent=True),
                      ev("notify", 60, phase="intent", dedupe_id="n2", channel="none", priority="P1"),
                      ev("notify", 60, dedupe_id="n2", channel="none", sent=True),
                      ev("controller_slip", 30), ev("l0_hard_failure", 500)]  # after the span
        m = metrics.compute(evs, {"p": ["p.1", "p.2"]}, {"p.1": "1000", "p.2": 1000},
                            io_(receipts={"p.1": FILES, "p.2": []}, qs={"p.1": [], "p.2": []}), NOW)
        s = m["plans"]["p"][1]
        self.assertEqual((s["span"][1] - s["span"][0], s["serial"]), (370 * 60, ((310 + 120) * 60, 0)))
        text = "\n".join(metrics.lines(m, ["p.1"]))
        for x in ("### 计划 p 累计（2 批）", "首轮通过率 0/1（0%）；到 approved 平均 2.00 轮；每轮平均 must_fix 1.50，降级占 33%、撤回占 33%",
                  "L0 硬失败 0", "跨度 6.2h；吞吐 2 批合入、7.78/天；归一吞吐 7783.78/天；并行对照 串行所需 7.2h、为跨度的 1.16 倍",
                  "交接 1、成功率 1/2（50%）", "待决 2（交给代理 1）；答复耗时 中位 0.5h、最长 0.5h；打扰 1（P0 1）",
                  "controller_slip 1", "、美元 unknown（无事件来源）；审查",
                  "等合入 1.0h · stuck 0.5h"):  # SF2: over the batches where it is not 0 (p.1's 0 left out)
            self.assertIn(x, text)
        self.assertEqual(text.count("效率："), 2)  # p.1's line and the plan's; p.2 was not asked for
        # a batch not done yet: the span runs to now
        s = metrics.compute(evs[:-1] + moves("p.2", (380, "running")), {"p": ["p.1", "p.2"]}, {},
                            io_(receipts={"p.1": FILES, "p.2": []}), NOW)["plans"]["p"][1]
        self.assertEqual(s["span"][1], NOW)
        self.assertNotIn("blocked", text)  # 0 in every batch: not listed

    def test_milestones_out_of_order_are_not_reached(self):  # SF1
        for pairs, want in (
                (((0, "running"), (10, "review_ready"), (20, "approved"), (30, "delivered"), (40, "changes_requested"),
                  (45, "running"), (50, "review_ready"), (60, "approved")), [10, 50, None, None]),
                (((0, "running"), (10, "review_ready"), (20, "approved")), [10, 10, None, None])):
            evs = moves("p.1", *pairs) + ([ev("reconciled", 30, batch="p.1", to="merged")] if len(pairs) == 3 else [])
            m = metrics.compute(evs, {"p": ["p.1"]}, {}, io_(receipts={"p.1": []}), NOW)
            gaps = m["batches"]["p.1"]["gaps"]
            self.assertEqual([g if g is None else round(g / 60) for g in gaps], want)
            self.assertIn("approved→delivered — · delivered→merged —；", metrics.batch_lines("p.1", m["batches"]["p.1"])[1])

    def test_serial_skips_a_merged_batch_never_running_as_uncounted(self):  # note 3
        evs = FLOW + [ev("reconciled", 200, batch="p.2", to="merged")]
        m = metrics.compute(evs, {"p": ["p.1", "p.2"]}, {}, io_(receipts={"p.1": FILES, "p.2": []}), NOW)
        self.assertIn("串行所需 5.2h（未计 1 次）", "\n".join(metrics.lines(m, [])))


class CollectTest(Base):
    def test_reviewer_transcript_fallback_and_seat_heartbeats(self):
        self.plan("p", {"state": "merged"})
        home = self.tmp / "claude"
        (home / "projects" / "x").mkdir(parents=True)
        (home / "projects" / "x" / "r1.jsonl").write_text("")
        self.log.append("review_receipt", batch="p.1", round=1, verdict="approved", reviewer_session="r1")
        self.log.append("seat_opened", batch="p.1", session="fm-p-1", successor=False)
        self.beat("fm-p-1", "p.1", self.now, transcript_path=str(self.tmp / "seat.jsonl"))
        read = mock.Mock(side_effect=lambda p: USE if str(p).endswith(("r1.jsonl", "seat.jsonl")) else None)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}):
            m = metrics.collect(self.root, report.events(self.root), ["p.1"], self.now, read=read)
        self.assertEqual({str(c.args[0]) for c in read.call_args_list},
                         {str(home / "projects" / "x" / "r1.jsonl"), str(self.tmp / "seat.jsonl")})
        self.assertIn("写手 token 250；审查 unknown（未计 1 次）、token 250", metrics.lines(m, ["p.1"])[1])
        self.assertEqual(m["batches"]["p.1"]["musts"], metrics.UNKNOWN)  # the receipt file is not there


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class BrokenPlanTest(Base):  # SF3
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}))

    def test_a_plan_that_does_not_load_is_unknown_never_left_out(self):
        self.plan("p", {"state": "merged"})
        self.plan("q", {"state": "running"})
        (state_dir(self.root) / "plans" / "q" / "plan.md").write_text("garbage\n")
        for b in ("p.1", "q.1", "r.1"):  # r: no plan, no header
            self.log.append("batch_state", batch=b, frm="ready", to="running")
        text = report.generate(self.root, None)[1]
        self.assertIn("### 计划 p 累计（1 批）", text)
        self.assertIn("### 计划 q 累计：unknown（", text)
        self.assertIn("### 计划 r 累计：unknown（没有计划 r）", text)
        code, out, _ = run(["report", "--plan", "p", "--plan", "q"])
        self.assertEqual(code, 1)
        self.assertIn("### 计划 p 累计（1 批）", out)
        self.assertIn("## 计划 q\n### 计划 q 累计：unknown（", out)
        self.assertEqual(run(["report", "--plan", "../plans"])[:2], (1, ""))


class CommandTest(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMIND_CONTROLLER", None)
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}))

    def test_report_section_and_plan_query(self):
        self.plan("p", {"state": "merged"}, {"state": "running"})
        self.log.append("batch_state", batch="p.1", frm="delivered", to="merged")
        _, text, f = report.generate(self.root, None)
        self.assertIn("## 开发精度与效率\n" + metrics.NOTE, text)
        self.assertIn("- p.1 精度：", text)
        self.assertNotIn("- p.2 精度：", text)  # no move this segment; its plan's section counts it
        self.assertIn("### 计划 p 累计（2 批）", text)
        self.assertLess(text.index("## 审查"), text.index("## 开发精度与效率"))
        self.assertLess(text.index("## 开发精度与效率"), text.index("## 按实测复算"))
        self.assertEqual(report.summary(f), "")
        with mock.patch.object(metrics, "collect", side_effect=ValueError("x")):
            self.assertIn("## 开发精度与效率：unknown", report.generate(self.root, None)[1])
        sd = state_dir(self.root)
        before = {p: p.read_bytes() for p in [sd / "events.jsonl", *(sd / "reports").iterdir()]}
        code, out, _ = run(["report", "--plan", "p", "--plan", "p"])
        self.assertEqual(code, 0)
        self.assertEqual(out.count("## 计划 p\n"), 2)
        self.assertIn("- p.2 精度：", out)
        self.assertEqual({p: p.read_bytes() for p in [sd / "events.jsonl", *(sd / "reports").iterdir()]}, before)
        code, _, err = run(["report", "--plan", "p", "--plan", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("nope", err)

    def test_defect(self):
        self.plan("p", {"state": "merged"}, {"state": "delivered"})
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-p-1"}):
            self.assertEqual(run(["defect", "p.1", "--source", "run", "--note", "x"])[0], 1)
        self.assertEqual(run(["defect", "p.9", "--source", "run", "--note", "x"])[0], 1)
        code, _, err = run(["defect", "p.2", "--source", "run", "--note", "x"])
        self.assertEqual(code, 1)
        self.assertIn("--request-changes", err)
        self.assertEqual(run(["defect", "p.1", "--source", "run", "--note", " "])[0], 1)
        self.assertEqual(self.events("defect_found"), [])
        code, out, _ = run(["defect", "p.1", "--source", "test", "--note", " 漏了空输入 "])
        self.assertEqual((code, json.loads(out)), (0, {"batch": "p.1", "source": "test", "note": "漏了空输入",
                                                      "by": "user"}))
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "controller"}):
            run(["defect", "p.1", "--source", "controller", "--note", "y"])
        self.assertEqual([e["by"] for e in self.events("defect_found")], ["user", "controller"])


class RequestChangesByTest(unittest.TestCase):
    def test_controller_without_a_session_is_controller(self):
        for env, by in (({"FOREMIND_ROLE": "controller"}, "controller"), ({}, "user"),
                        ({"FOREMIND_CONTROLLER": "c-1"}, "controller")):
            p = Project(self)
            p.batch(state="approved")
            with mock.patch.dict(os.environ, env):
                if "FOREMIND_CONTROLLER" not in env:
                    os.environ.pop("FOREMIND_CONTROLLER", None)
                code, _, _ = review_cli(["review", "shop.1", "--request-changes", "--item", "x"])
            self.assertEqual(code, 0)
            self.assertEqual([p.events(t)[-1]["by"] for t in ("batch_state", "changes_requested_by")], [by, by])


class ExternalPromptTest(HookBase):
    def test_a_seat_prompt_no_program_delivered(self):
        d = {**self.data("UserPromptSubmit", self.root), "prompt": "  你好\r\n"}
        self.assertIsNone(self.run_hook("UserPromptSubmit", d))
        [e] = self.events("seat_prompt_external")
        self.assertEqual((e["session"], e["sha256"]), (SESSION, hooks.delivery_sha("你好")))
        EventLog(self.state / "events.jsonl").append("program_delivery", session=SESSION,
                                                     text_sha256=hooks.delivery_sha("你好"))
        self.assertIsNone(self.run_hook("UserPromptSubmit", d))
        self.assertEqual(len(self.events("seat_prompt_external")), 1)
        self.use_env(self.base_env)  # not a seat
        self.assertIsNone(self.run_hook("UserPromptSubmit", {**d, "prompt": "别的"}))
        self.use_env({**self.seat_env, "FOREMIND_ROLE": "controller"})
        self.assertIsNone(self.run_hook("UserPromptSubmit", {**d, "prompt": "别的"}))
        self.assertEqual(len(self.events("seat_prompt_external")), 1)


if __name__ == "__main__":
    unittest.main()
