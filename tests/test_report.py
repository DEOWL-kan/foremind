"""m2b.6: `foremind report`, the run report phase, failed batches (report.py, supervisor/phases/report.py,
commands/supervise.py run)."""
import contextlib
import io
import json
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

from foremind import catalog, cli, notify, report
from foremind.events import EventLog
from foremind.supervisor import tick as sv
from foremind.fsutil import sha256_bytes
from foremind.paths import state_dir
from foremind.plan import model
from test_supervisor import Base


class ReportTest(Base):
    def setUp(self):
        super().setUp()
        self.sd = state_dir(self.root)

    def test_facts_and_the_markdown(self):
        dev = {"delivery": {"level": "merge_dev"}}  # approved: the supervisor merges it, and it loads (r1 #5)
        self.plan("p", {"state": "delivered"}, {"state": "merged"}, {"state": "running"},
                  {"state": "delivered", "config": dev, "config_approved": model.config_hash({"config": dev})})
        log = self.log
        log.append("batch_state", batch="p.1", frm="approved", to="delivered")  # seat / tick shape
        log.append("batch_state", batch="p.3", prior="in_review", state="changes_requested")  # review's shape
        log.append("notify_unsent", key="k1", channel="ntfy", priority="P1", title="t1")
        log.append("notify_held", key="k2", title="t2")
        log.append("notify_deferred", key="k3", title="t3")
        for b, n, v in (("p.1", 1, "changes_requested"), ("p.1", 2, "approved"), ("p.2", 1, "approved"),
                        ("p.3", 2, "approved")):  # p.3's r1 was a failed review: its first receipt passed (r1 #2)
            log.append("review_receipt", batch=b, round=n, verdict=v)
        log.append("review_receipt", batch="p.2", round=2, verdict="approved", rebound_from="p.2.review.r1.json")
        self.decision("Q-1", [], state="provisional")
        self.decision("Q-2", [], state="overdue")
        self.decision("Q-3", ["p.3"])
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
        (self.sd / "decisions" / "PV-1.json").write_text(json.dumps({
            "id": "PV-1", "question": "Q-1", "batch": "p.1", "category": 3, "revert": "r", "deadline": soon}))
        (self.sd / "precedents.md").write_text(catalog._block({"id": "J-1", "needs_review": True}) +
                                               catalog._block({"id": "J-2", "needs_review": False}))
        path, text, f = report.generate(self.root, None)
        self.assertEqual(f["changes"], [{"batch": "p.1", "to": "delivered"}, {"batch": "p.3", "to": "changes_requested"}])
        self.assertEqual(f["awaiting_merge"], ["p.1"])
        self.assertEqual([x["key"] for x in f["unsent"] + f["held"] + f["deferred"]], ["k1", "k2", "k3"])
        self.assertEqual(f["provisional"], {"due": ["Q-1"], "overdue": ["Q-2"]})
        self.assertEqual((f["needs_review"], f["open_q"], f["quota"]), (["J-1"], 1, "unknown"))
        self.assertEqual(f["reviews"], {"first": 2, "batches": 3, "avg_rounds": 1.33})
        self.assertTrue(path.startswith("reports/") and path.endswith(".md"))
        self.assertEqual((self.sd / path).read_text(), text)
        for line in ("- p.1 → delivered", "首轮通过率：2/3（66%）", "平均审查轮数：1.33", "## 额度：unknown", "k2：t2"):
            self.assertIn(line, text)
        self.assertEqual(report.summary(f), "等你合入 1（p.1） · 未发出 1 · 被限流 1 · 被延后 1 · 暂定到期 Q-1、Q-2 · "
                                            "需复核 J-1 · 待决 1")
        [e] = self.events("report_generated")
        self.assertEqual((e["path"], e["since"]), (path, None))

        path2, _, f2 = report.generate(self.root, report.last(report.events(self.root)))  # only what came since
        self.assertNotEqual(path2, path)
        self.assertEqual((f2["since"], f2["changes"], f2["unsent"], f2["reviews"]["batches"]), (e["ts"], [], [], 0))

    def test_what_cannot_be_read_is_unknown(self):
        self.plan("p")
        (self.sd / "precedents.md").write_text("## J-1\n\n```json\n[1]\n```\n")
        (self.sd / "decisions").mkdir()
        (self.sd / "decisions" / "Q-1.json").write_text("{")
        _, text, f = report.generate(self.root, None)
        self.assertEqual((f["needs_review"], f["open_q"], f["provisional"]), ("unknown",) * 3)
        self.assertIn("## 未决待决：unknown", text)
        self.assertEqual(f["reviews"]["avg_rounds"], "unknown")

    def test_suggestions_over_all_events_archives_too_and_never_pushed(self):
        """m2d.10 REQ-22: the section reads every event, a month's archive included, whatever `since` is; the push
        summary and the config files are left as they were."""
        self.plan("p", {"state": "delivered"})
        archive = EventLog(self.sd / "archive" / "events-2026-08.jsonl")
        for _ in range(6):
            archive.append("api_retry_result", batch="p.1", session="s", error_id="x", tries=2, ok=True)
        for _ in range(4):
            self.log.append("api_retry_result", batch="p.1", session="s", error_id="y", tries=1, ok=True)
        self.user_config("[stuck]\napi_retry_max = 5\n")
        before = {p: p.read_bytes() for p in self.sd.rglob("*.toml")}
        report.generate(self.root, None)
        _, text, f = report.generate(self.root, report.last(report.events(self.root)))
        it = next(x for x in f["signals"] if x["name"] == "API 续跑次数")
        self.assertEqual((it["current"], it["n"], it["suggest"]), (5, 10, 2))
        self.assertIn("## 按实测复算的建议值", text)
        self.assertIn("- API 续跑次数：当前 5；样本 10；成功 10 串，最多 2 次；建议 2；规则：", text)
        self.assertIn("- 总控软线：当前 200000；样本 0；", text)
        self.assertEqual(report.summary(f), "等你合入 1（p.1）")
        self.assertEqual({p: p.read_bytes() for p in self.sd.rglob("*.toml")}, before)
        with mock.patch.object(report.signals, "compute", side_effect=ValueError("x")):
            _, text, f = report.generate(self.root, None)
        self.assertEqual(f["signals"], "unknown")
        self.assertIn("## 按实测复算的建议值（全部事件；只是建议，不改配置）：unknown", text)

    def test_recall_paired_again_from_ab_json_and_the_base_receipt(self):
        """m2e REQ-1: a comparison recorded before m2e (overlap only) is paired from its files; one whose ab.json is
        gone and whose event has no matched is counted as not recomputable."""
        self.plan("p", {"state": "in_review"})
        self.log.append("review_started", phase="intent", dedupe_id="r1", batch="p.1", round=1, reviewer_session="r1",
                        effort="high", difficulty="M", security=False)
        self.log.append("review_receipt", batch="p.1", round=1, verdict="changes_requested", reviewer_session="r1")
        (self.sd / "batches" / "p.1.review.r1.json").write_text(json.dumps({"round": 1, "heads": {"main": "x"}, "issues": [
            {"severity": "must_fix", "location": "main:foremind/review.py:673"},
            {"severity": "must_fix", "location": "main:foremind/gate.py:10"}]}))
        for s in ("a1", "a2"):
            self.log.append("ab_review", batch="p.1", base_round=1, reviewer_session=s, effort="medium",
                            base_effort="high", must_fix=1, base_must_fix=2, overlap=0)
        (self.sd / "reviews" / "a1").mkdir(parents=True)
        (self.sd / "reviews" / "a1" / "ab.json").write_text(json.dumps({"heads": {"main": "x"}, "issues": [
            {"severity": "must_fix", "location": "main:foremind/review.py:664", "summary": "other words"}]}))
        f = report.facts(self.root, None, cfg={"routes.reviewer.effort_m": "high"})
        it = next(x for x in f["signals"] if x["name"] == "审查强度（M·非安全）")
        self.assertIn("对照 medium（基准 high）：1 次，召回 0.5", it["stats"])
        self.assertIn("对照（基准 high）无法复算 1 次", it["stats"])

    def test_the_command(self):
        self.plan("p")
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["report"]), 0)
        self.assertIn("# 运行报告", out.getvalue())
        self.assertEqual(len(list((self.sd / "reports").glob("*.md"))), 1)


class RunReportPhaseTest(Base):
    def reports(self):
        return sorted((state_dir(self.root) / "reports").glob("*.md"))

    def test_full_block_one_report_and_one_push_per_fingerprint_and_failed_told_once(self):
        self.plan("p", {"state": "failed"})
        self.log.append("batch_state", batch="p.1", prior="in_review", state="failed", reason="review_failures")
        for at in (0, 30):
            self.tick(at)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 连续失败已停", "全部批次都在等你"])
        self.assertIn("foremind run p.1", self.rec.sent[0][1])
        self.assertIn("review_failures", self.rec.sent[0][1])
        self.assertEqual(self.rec.sent[1][1], "p.1：失败，等你决定")  # nothing else to act on: the reasons alone
        [r] = self.reports()
        self.assertIn("- p.1：失败，等你决定", r.read_text())

    def test_all_finished_reports_once_and_only_after_a_change(self):
        self.plan("p", {"state": "merged"}, {"state": "cancelled"})
        report.generate(self.root, None)  # a report since which nothing moved
        self.tick()
        self.assertEqual((len(self.reports()), self.rec.sent), (1, []))
        self.log.append("reconciled", batch="p.1", frm="delivered", to="merged", skipped=[])  # the user merged (r1 #1)
        for at in (30, 60):
            self.tick(at)
        self.assertEqual(len(self.reports()), 2)
        self.assertEqual(self.rec.sent, [("本段自动运行已结束", "状态变化 1", "P1")])
        self.assertIn("- p.1 → merged", self.reports()[-1].read_text())

    def test_cancelled_by_plan_amend_is_a_change(self):  # r2 #1: plan amend writes no batch_state
        self.plan("p", {"state": "merged"}, {"state": "cancelled"})
        report.generate(self.root, None)
        self.log.append("plan_amended", plan="p", dropped=["p.2"])
        self.tick()
        self.assertEqual(self.rec.sent, [("本段自动运行已结束", "状态变化 1", "P1")])
        self.assertIn("- p.2 → cancelled", (state_dir(self.root) / self.events("report_generated")[-1]["path"]).read_text())

    def test_an_unhandled_push_leaves_no_report(self):  # r2: an invalid channel, pushed again once fixed
        self.plan("p", {"state": "failed"})
        with mock.patch.object(notify, "get", side_effect=ValueError("unknown notify.channel")):
            self.tick()
        self.assertEqual(self.reports(), [])
        self.tick(30)
        self.assertEqual(([t for t, _, _ in self.rec.sent], len(self.reports())), (["全部批次都在等你"], 1))

    def test_held_p0s_come_out_under_the_pause_once(self):  # r2 controller ruling: REQ-13 pause, REQ-16 limit
        self.plan("p", {})
        for k in "abc":
            notify.notify(self.root, {}, f"l0:{k}", f"L0 {k}", "b", "P0")
        sv.pause(self.root, True)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["L0 a", "L0 b"], "the third is held")
        for at in (30, 60):
            self.tick(at)
        self.assertEqual([(t, p) for t, _, p in self.rec.sent[2:]], [("已暂停：有告警被限流", "P1")])
        self.assertIn("- L0 c", self.rec.sent[2][1])
        [r] = self.reports()
        self.assertIn("l0:c：L0 c", r.read_text())
        self.assertEqual([j for j in self.jobs.cmds()], [], "no action under the pause")

    def test_a_crash_before_the_report_leaves_the_push_sent_once(self):  # r1 #6
        self.plan("p", {"state": "failed"})
        with mock.patch.object(report, "generate", side_effect=OSError("disk")):
            self.tick()
        self.tick(30)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["全部批次都在等你"])
        self.assertEqual(len(self.reports()), 1)

    def test_a_block_announce_block_pushed_is_not_pushed_again(self):  # r1 #7: upgraded while blocked
        self.plan("p", {"state": "failed"})
        fp = sha256_bytes(json.dumps(["p.1：失败，等你决定"], ensure_ascii=False).encode())[:16]
        self.log.append("notify", dedupe_id=f"notify:full_block:{fp}", key=f"full_block:{fp}", channel="rec", sent=True)
        self.tick()
        self.assertEqual((self.rec.sent, self.reports()), ([], []))

    def test_nothing_while_work_goes_on(self):
        self.plan("p", {"state": "merged"}, {})
        self.log.append("batch_state", batch="p.1", frm="delivered", to="merged")
        with mock.patch.object(report, "events", wraps=report.events) as read:
            self.tick()
        self.assertEqual(self.reports(), [])
        read.assert_not_called()  # m2d.10 r1 note: the whole history is read only when a report may be due

    def test_the_summary_is_sent_when_p1_goes_to_the_report(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[notify]\np1 = "report"\n')
        self.plan("p", {"state": "failed"})
        self.log.append("batch_state", batch="p.1", prior="in_review", state="failed", reason="review_failures")
        self.tick()
        self.assertEqual([t for t, _, _ in self.rec.sent], ["全部批次都在等你"])
        self.assertIn("被延后 1", self.rec.sent[0][1])  # the failed notice, deferred to this very report
        self.assertEqual([e["key"] for e in self.events("notify_deferred")], [f"failed:p.1:{self.events('batch_state')[0]['id']}"])


class RetryTest(Base):
    def test_run_moves_a_failed_batch_to_ready(self):
        self.plan("p", {"state": "failed", "state_prior": "in_review"})
        self.telemetry(10, self.now)
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["run", "p.1"]), 0)
        self.assertEqual([(e["batch"], e["by"]) for e in self.events("batch_retried")], [("p.1", "user")])
        self.assertNotIn("state_prior", self.header("p.1"))
        self.assertEqual((self.header("p.1")["state"], self.jobs.cmds()), ("ready", [["seat", "p.1"]]))

    def test_an_unknown_id_moves_nothing(self):  # r1 #4
        self.plan("p", {"state": "failed"})
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertNotEqual(cli.main(["run", "p.1", "p.9"]), 0)
        self.assertEqual((self.header("p.1")["state"], self.events("batch_retried")), ("failed", []))
