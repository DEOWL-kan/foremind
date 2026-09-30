import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foremind import controller, header, inbox, install, sessions  # noqa: E402
from foremind.commands import status  # noqa: E402
from foremind.events import EventLog  # noqa: E402
from foremind.supervisor import tick as sv  # noqa: E402
from test_install_fixture import Env  # noqa: E402
from test_sessions import NOBODY, T0, row  # noqa: E402
from test_supervisor import Base  # noqa: E402


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
        self.env.config.mkdir()
        (self.env.config / "quota.json").write_text(json.dumps({"groups": {"claude/long": {"state": "low"},
                                                                         "claude/oneshot": {"state": "available"}}}))
        # the project's of before m2b.10, not migrated yet: shown folded in (the stricter), left for the tick (r1 #3)
        (sd / "quota.json").write_text(json.dumps({"groups": {"claude/long": {"state": "exhausted"},
                                                              "claude/oneshot": {"state": "low"}}}))
        rc, out = self.env.run("status", project=root)
        self.assertEqual(rc, 0, out)
        self.assertIn("批次：merged 1 · running 2", out)
        self.assertIn("p.3              merged  t p.3", out)
        self.assertIn("fm-shop-p_1-1  批次 p.1  心跳 2026-09-25T10:00:00+00:00 busy_tool", out)
        self.assertIn("未决：2", out)
        self.assertIn("额度：claude/long exhausted · claude/oneshot low", out)
        self.assertTrue((sd / "quota.json").exists())

    def test_closed_sessions_are_counted_not_listed(self):  # finding 11
        root = project(self.env, "shop", [("p.1", "running")])
        sd = root / ".foremind"
        for n in (1, 2, 3):
            s = f"fm-shop-p_1-{n}"
            (sd / "heartbeats" / f"{s}.json").write_text(json.dumps({"session": s, "batch": "p.1", "ts": "t"}))
        log = EventLog(sd / "events.jsonl")
        log.append("sv_close", session="fm-shop-p_1-1", carrier="tmux", how="absent", confirmed=True)
        log.append("exit_confirmed", session="fm-shop-p_1-2")
        log.append("sv_close", session="fm-shop-p_1-3", carrier="tmux", confirmed=False)  # not confirmed: still open
        rc, out = self.env.run("status", project=root)
        self.assertEqual(rc, 0, out)
        self.assertIn("会话：1（另有 2 个已关闭，未列出）", out)
        self.assertIn("fm-shop-p_1-3", out)
        self.assertNotIn("fm-shop-p_1-1", out)
        self.assertNotIn("fm-shop-p_1-2", out)

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


class ExplainTest(Base):
    """REQ-10: per unfinished batch who it waits on, why, and what the user runs (m2c.5)."""

    def setUp(self):
        super().setUp()
        sv.supervisor_path(self.root).write_text(json.dumps({"pid": os.getpid(), "code": sv.code_fingerprint()}))

    def line(self, bid, out=None):
        out = status.report(self.root) if out is None else out
        return next(x for x in out.splitlines() if x.startswith(f"  {bid} ") and " — 在等" in x)

    def files(self):
        return {p: p.read_bytes() for d in (self.root, self.cfg_home) for p in d.rglob("*")
                if p.is_file() and p.suffix != ".lock"}

    def test_read_only(self):
        """No write, no notice: the old project quota file stays (no migrate), retries used up notify nothing."""
        self.plan("p", {"state": "ready"})
        (self.root / ".foremind" / "quota.json").write_text(json.dumps({"groups": {}}))
        for _ in range(2):
            self.log.append("sv_seat", phase="result", batch="p.1", ok=False)
        before = self.files()
        self.assertIn("你要做：foremind run p.1", self.line("p.1"))
        self.assertEqual((self.files(), self.rec.sent), (before, []))

    def test_waiting_on_the_user_maps_to_commands(self):
        self.plan("p", {}, {"state": "delivered"}, {"state": "failed"}, {"mode": "watch"})
        self.decision("Q-1", ["p.1"])
        out = status.report(self.root)
        self.assertIn("  p.1 planned — 在等：你；原因：待决 Q-1；你要做：foremind decide show Q-1", out)
        self.assertIn("你要做：foremind land p.2", self.line("p.2", out))
        self.assertIn("你要做：foremind run p.3", self.line("p.3", out))
        self.assertIn("你要做：foremind run p.4", self.line("p.4", out))
        self.assertNotIn("不会自动推进", out)

    def test_a_plan_unbound_or_never_approved(self):  # m2e REQ-4
        self.plan("p", {"state": "ready"})
        self.plan("q", {"state": "ready"})
        pm = self.root / ".foremind" / "plans" / "q" / "plan.md"
        h, body = header.parse(pm.read_text())
        del h["approved_at"]
        pm.write_text(header.render(h, body))
        self.log.append("plan_approved", plan="p", plan_hash="0" * 64)  # what is on disk is no longer approved
        out = status.report(self.root)
        self.assertIn("原因：计划批准后被改动，调度已停；你要做：改回批准时的样子，或 foremind plan amend p",
                      self.line("p.1", out))
        self.assertIn("原因：计划待批准；你要做：foremind plan approve q", self.line("q.1", out))
        direct, _ = status._reader(self.root).waits()
        self.assertEqual((direct["p.1"].code, direct["q.1"].code), ("plan_unbound", "approve_plan"))

    def test_not_ready_reasons(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 1\n')
        self.plan("p", {}, {"depends_on": ["p.1"]}, {"state": "ready"})
        self.hold("p.1", "fm-a")
        self.telemetry(10, self.now)
        self.tick(five=10)  # quota known
        out = status.report(self.root)
        self.assertIn("在等：上游 p.1；原因：上游 p.1 未合入；你要做：无需操作", self.line("p.2", out))
        self.assertIn("在等：名额；原因：名额已满（1/1，其中待开继任 0 个）", self.line("p.3", out))

    def test_machine_deferred(self):
        """REQ-6: a seat_deferred since the last seat_opened, with its reading; a seat opened since: gone."""
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n')
        self.telemetry(10, self.now)
        self.tick(five=10)  # quota known, before the batch: no seat opened on it
        self.plan("p", {"state": "ready"})
        self.log.append("seat_deferred", dedupe_id="seat_deferred:none", reason="load", load1=9.5, cpus=8,
                        avail_mb=4000, max_load_per_cpu=0.8, min_free_mem_mb=2048)
        self.assertIn("在等：监督进程；原因：整机负载高，推迟开席（1 分钟负载 9.5（8 核 × 0.8），读于 ", self.line("p.1"))
        self.log.append("seat_opened", batch="p.9", session="fm-z", successor=False, carrier="tmux")
        self.assertIn("原因：下一轮开席", self.line("p.1"))

    def test_successor_held_back(self):
        """r1: a batch nobody holds waits for its successor as Tick.successor does: seats (used alone against the
        cap; the text counts the waiting successors, itself included), then the machine; a new batch counts them too."""
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 1\n')
        self.telemetry(10, self.now)
        self.tick(five=10)  # quota known, before the batches: nothing opened on them
        self.plan("p", {"state": "running"}, {"state": "running"}, {"state": "ready"})
        self.hold("p.1", "fm-a")
        self.assertIn("在等：名额；原因：名额已满（2/1，其中待开继任 1 个）", self.line("p.2"))
        self.assertIn("在等：名额；原因：名额已满（2/1，其中待开继任 1 个）", self.line("p.3"))
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 2\n')
        self.assertIn("在等：监督进程；原因：无人在做，下一轮开继任", self.line("p.2"))
        self.log.append("seat_deferred", dedupe_id="seat_deferred:none", reason="memory", load1=1.0, cpus=8,
                        avail_mb=900, max_load_per_cpu=0.8, min_free_mem_mb=2048)
        self.assertIn("在等：监督进程；原因：整机负载高，推迟开席（可用内存 900 MB（下限 2048 MB），读于 ",
                      self.line("p.2"))
        self.assertIn("在等：名额；原因：名额已满（2/2，其中待开继任 1 个）", self.line("p.3"))  # the successor goes first

    def test_successor_of_a_holder_is_not_held_back(self):
        """r2: a stuck batch whose predecessor still holds the lock (its session gone, m2b.8 r3): Tick.successor
        opens one on that seat, seats full or machine busy (REQ-5, REQ-6)."""
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 1\n')
        self.telemetry(10, self.now)
        self.tick(five=10)
        self.plan("p", {"state": "stuck"}, {"state": "running"})
        self.hold("p.1", "fm-a")
        self.hold("p.2", "fm-b")
        self.set_state("p.1", "stuck")
        self.log.append("exit_confirmed", session="fm-a")
        self.log.append("seat_deferred", dedupe_id="seat_deferred:none", reason="load", load1=9.5, cpus=8,
                        avail_mb=4000, max_load_per_cpu=0.8, min_free_mem_mb=2048)
        self.assertIn("在等：监督进程；原因：无人在做，下一轮开继任", self.line("p.1"))

    def test_started_batches(self):
        self.plan("p", {}, {}, {}, {"state": "review_ready"}, {"state": "approved", "depends_on": ["p.4"]})
        self.hold("p.1", "fm-a", waiting_input={"message": "?"})
        self.hold("p.2", "fm-b", handoff_requested=True)
        self.hold("p.3", "fm-c")
        self.log.append("sv_say", phase="intent", dedupe_id="sv_say:apiretry:fm-c:e1:1", session="fm-c", token="t1")
        inbox.append("fm-c", "续跑", sender="supervisor#t1", root=self.root)
        out = status.report(self.root)
        self.assertIn("在等：席位 fm-a（心跳 ", self.line("p.1", out))
        self.assertIn("原因：在终端里等你回答；你要做：到这个席位的终端里回答", self.line("p.1", out))
        self.assertIn("原因：交接中，等继任", self.line("p.2", out))
        self.assertIn("原因：API 错误自动续跑中", self.line("p.3", out))
        self.assertIn("在等：审查者", self.line("p.4", out))
        self.assertIn("在等：门禁；原因：等上游 p.4 合入", self.line("p.5", out))

    def test_nothing_advances_by_itself(self):
        self.plan("p", {"state": "review_ready"}, {"state": "merged"})
        sv.supervisor_path(self.root).unlink()
        sv.pause(self.root, True)
        out = status.report(self.root)
        self.assertIn("（不会自动推进：已暂停，foremind resume；监督进程未运行，foremind supervise）",
                      self.line("p.1", out))
        self.assertNotIn("  p.2 merged — ", out)  # finished: no line

    def test_review_round_1(self):
        self.plan("p", {}, {}, {"state": "in_review"}, {"state": "blocked", "blocked_reason": "pending"})
        self.set_state("p.1", "running")  # no holder, its successor job on its way: not "关闭 None 中"
        self.log.append("sv_successor", phase="intent", dedupe_id="sv_successor:p.1:1", batch="p.1")
        self.set_state("p.2", "stuck")
        self.log.append("sv_successor", phase="intent", dedupe_id="sv_successor:p.2:1", batch="p.2")
        (self.root / ".foremind" / "batches" / "p.3.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.decision("Q-2", ["p.4"], state="deciding")
        out = status.report(self.root)
        self.assertIn("在等：监督进程；原因：开继任中", self.line("p.1", out))
        self.assertIn("在等：监督进程；原因：开继任中", self.line("p.2", out))
        self.assertIn("在等：门禁", self.line("p.3", out))
        self.log.append("sv_gate", phase="intent", dedupe_id="sv_gate:p.3:r1:t1", batch="p.3", round=1)
        self.log.append("sv_gate", phase="result", dedupe_id="sv_gate:p.3:r1:t1", batch="p.3", ok=False, exit_code=1)
        self.assertIn("原因：门禁未通过，改完再 foremind review", self.line("p.3"))  # r2: no gate runs now
        self.log.append("review_started", phase="intent", dedupe_id="review:p.3:2", batch="p.3")
        self.assertIn("在等：审查者；原因：在审", self.line("p.3"))  # the next round, r2 not written yet
        self.assertIn("在等：决策者；原因：Q-2 决策中", self.line("p.4", out))
        with mock.patch.object(sv.Tick, "waits", side_effect=KeyError("phase")):
            out = status.report(self.root)
        self.assertIn("等待：读不了（KeyError", out)
        self.assertIn("会话：", out)  # the other lines still print
        two = SimpleNamespace(headers={"x": {"repos": ["a", "b"]}}, plan_of={"x": "p"})
        self.assertEqual(status._todo(two, "x", "等你合入"), "foremind land x")  # m2d.8: land takes several repos
        self.assertEqual(status._todo(two, "x", sv.Reason("文案改了", "land")), "foremind land x")  # by code
        self.assertEqual(status._todo(two, "x", sv.Reason("文案改了", "decide", ["Q-1", "Q-2"])),
                         "foremind decide show Q-1；foremind decide show Q-2")  # m2e REQ-4
        self.assertEqual(status._todo(two, "x", "待决 Q-3"), "foremind decide show Q-3")  # by text
        self.assertEqual(status._todo(two, "x", sv.Reason("计划待批准", "new")), "foremind plan approve p")  # text


class ProcessesTest(Base):
    """REQ-4: the Foremind sessions running now, known by their record, with what they use; the controller's reading."""

    def test_processes_and_the_controller(self):
        self.enterContext(mock.patch.dict(os.environ, {controller.MARKER: "fm-x-controller-1"}))  # an older one's shell
        sd = self.root / ".foremind"
        mb = 1024
        ps = (NOBODY + row(101, 1, f"claude --settings {sd}/sessions/fm-x-p_1-1.settings.json", 2.0, 400 * mb)
              + row(102, 101, "node mcp", 1.0, 100 * mb)
              + row(103, 1, f"claude --settings {sd}/sessions/fm-x-p_2-1.settings.json", 0.5, 200 * mb)
              + row(104, 1, "claude --model m", 50.0, 900 * mb)  # a claude no record names: not ours
              + row(105, 1, "claude --model m[1m]", 3.0, 300 * mb))  # the bound controller
        self.log.append("seat_launch", phase="intent", dedupe_id="seat_launch:fm-x-p_1-1", batch="p.1",
                        session="fm-x-p_1-1")
        self.log.append("seat_launch", phase="intent", dedupe_id="seat_launch:fm-x-p_2-1", batch="p.2",
                        session="fm-x-p_2-1")
        self.log.append("sv_close", session="fm-x-p_2-1", carrier="orca", how="absent", confirmed=True)
        with mock.patch.object(controller, "_now", return_value="2026-09-01T00:00:00+00:00"):
            controller.update(self.root, "a0", controller="fm-x-controller-1", context_tokens=5)
        controller.update(self.root, "a1", controller="fm-x-controller-2", pid=105, lstart=T0, context_tokens=123456,
                          model="claude-opus-5-5[1m]")
        with mock.patch.object(sessions, "_ps", lambda: ps):
            out = status.report(self.root)
        self.assertIn("存活的 Foremind 会话：3 个，CPU 合计 6.5%，内存合计 1000 MB\n"
                      "  fm-x-controller-2  总控  批次 -  pid 105  CPU 3.0%  内存 300 MB\n"
                      "  fm-x-p_1-1  席位  批次 p.1  pid 101  CPU 3.0%  内存 500 MB\n"
                      "  fm-x-p_2-1  席位  批次 p.2  pid 103  CPU 0.5%  内存 200 MB  已记关闭仍在跑\n"
                      "总控：fm-x-controller-2  context_tokens 123456  软线 200000  硬线 300000  更新于 ", out)
        with mock.patch.object(sessions, "_ps", lambda: NOBODY):
            self.assertIn("存活的 Foremind 会话：0 个，CPU 合计 0.0%，内存合计 0 MB\n", status.report(self.root))

        def denied():
            raise sessions.SessionsError("/bin/ps: exit 1: denied")
        with mock.patch.object(sessions, "_ps", denied):
            self.assertIn("存活的 Foremind 会话：未知（进程读不出：/bin/ps: exit 1: denied），CPU 未知，内存 未知",
                          status.report(self.root))


if __name__ == "__main__":
    unittest.main()
