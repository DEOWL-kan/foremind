"""m2d.3: a 1M seat's lines (REQ-8), mid-turn reminders (REQ-9), soft prompts only at a breakpoint (REQ-10)."""
import json
import subprocess

from foremind import controller, hooks
from foremind.events import EventLog
from test_hooks import SESSION, BATCH, HookBase, usage_line


class Base(HookBase):
    def usage(self, tokens):
        with open(self.transcript, "a") as f:
            f.write(usage_line(f"msg_{tokens}", tokens) + "\n")

    def window(self, size):
        (self.state / "telemetry").mkdir(exist_ok=True)
        (self.state / "telemetry" / f"{SESSION}.statusline.json").write_text(
            json.dumps({"context_window": {"context_window_size": size}}))

    def post(self, cwd=None):
        out = self.run_hook("PostToolUse", self.data("PostToolUse-Bash", cwd=cwd))
        return out and out["hookSpecificOutput"]["additionalContext"]

    def stop(self):
        return self.run_hook("Stop", self.data("Stop"))


class BudgetTest(Base):
    def test_a_1m_window_moves_the_defaults_only(self):
        self.assertEqual(hooks.budget({}, "seat", None, 1_000_000), (200_000, 300_000))
        self.assertEqual(hooks.budget({}, "seat", None, 200_000), (130_000, 160_000))
        self.assertEqual(hooks.budget({}, "seat", None, None), (130_000, 160_000))
        cfg = {"context.by_role.seat.claude-opus-5-5.hard_pct": 40, "context.abs_cap_tokens": 350_000}
        self.assertEqual(hooks.budget(cfg, "seat", "claude-opus-5-5", 1_000_000), (175_000, 350_000))  # set keys win
        self.assertEqual(hooks.budget({"context.window_tokens": 1_000_000}, "seat", None), (146_250, 180_000))

    def test_the_seat_stop_reads_the_status_line_window(self):
        self.usage(170_000)
        self.window(1_000_000)
        self.assertIsNone(self.stop(), "170k is under a 1M seat's soft line")
        self.usage(250_000)
        self.assertIn("软阈值 200000（有效预算 300000）", self.stop()["reason"])
        self.usage(310_000)
        self.assertIn("有效预算 300000", self.stop()["reason"])

    def test_only_a_seat_gets_the_window(self):
        self.use_env({**self.seat_env, "FOREMIND_ROLE": "planner"})
        self.window(1_000_000)
        self.usage(170_000)
        self.assertIn("有效预算 160000", self.stop()["reason"])


class MidTurnTest(Base):
    def test_cadence(self):
        self.usage(100_000)
        self.assertIsNone(self.post(), "under the soft line")
        self.usage(140_000)  # soft 130k, hard 160k
        got = [self.post() for _ in range(7)]
        self.assertIn("过了软阈值 130000", got[0])
        self.assertIn("提交之后、没有未提交改动", got[0])
        self.assertEqual([bool(g) for g in got], [True, False, False, False, False, True, False])
        self.usage(165_000)
        self.assertIn("现在按协议写交接段", self.post(), "a rise reminds at once")
        self.assertIsNone(self.post())
        [e] = self.events("handoff_requested")  # REQ-10: the first request is the mid-turn one
        self.assertEqual(e["context_tokens"], 165_000)
        self.assertTrue(self.hb()["handoff_requested_at"])
        self.handoff_section("written after the mid-turn request")
        reason = self.stop()["reason"]
        self.assertIn("硬阈值", reason)  # the Stop still blocks at the hard line
        self.assertNotIn("早于本次请求", reason, "the section after the mid-turn request counts")
        self.assertEqual(len(self.events("handoff_requested")), 1)

    def test_a_subagent_call_is_no_step(self):
        self.usage(140_000)
        sub = {**self.data("PostToolUse-Bash"), "agent_id": "agent-1", "agent_type": "Explore"}
        self.assertIsNone(self.run_hook("PostToolUse", sub))
        self.assertIn("过了软阈值", self.post(), "the main thread's first call at the line still reminds")
        self.assertIsNone(self.run_hook("PostToolUse", sub))
        self.assertEqual(self.hb()["context_line"], {"level": 1, "calls": 0})

    def test_controller(self):
        sid = self.data("PostToolUse-Bash")["session_id"]
        self.use_env({**self.base_env, "FOREMIND_PROJECT": str(self.root), controller.MARKER: "ctl-1"})
        self.usage(250_000)  # controller defaults: soft 200k, hard 300k
        self.assertIsNone(self.post(cwd=self.root), "not bound")
        controller.update(self.root, sid, controller="ctl-1")
        text = self.post(cwd=self.root)
        self.assertIn("过了软线（软线 200000，硬线 300000）", text)
        self.assertIn("HANDOFF-controller.md", text)
        self.assertIsNone(self.post(cwd=self.root))
        self.usage(310_000)
        self.assertIn("达到硬线", self.post(cwd=self.root))
        EventLog(self.state / "events.jsonl").append("controller_handoff", from_session="ctl-1")
        self.usage(320_000)
        self.assertIsNone(self.post(cwd=self.root), "handed off")


class BreakpointTest(Base):
    def setUp(self):
        super().setUp()
        git = lambda *a: subprocess.run(["git", "-C", str(self.wt), *a], check=True, capture_output=True)  # noqa: E731
        git("init", "-q")
        (self.wt / "src" / "a.py").write_text("x = 1\n")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
        EventLog(self.state / "events.jsonl").append("seat_opened", batch=BATCH, session=SESSION,
                                                     worktrees={"api": str(self.wt)})

    def test_soft_waits_for_a_clean_tree(self):
        self.usage(140_000)
        (self.wt / "src" / "b.py").write_text("untracked\n")
        self.assertIsNone(self.stop())
        self.assertFalse(self.hb().get("soft_prompted"))
        (self.wt / "src" / "b.py").unlink()
        self.assertIn("软阈值", self.stop()["reason"])
        self.assertIsNone(self.stop(), "once")

    def test_unreadable_git_is_a_breakpoint(self):
        EventLog(self.state / "events.jsonl").append("seat_opened", batch=BATCH, session=SESSION,
                                                     worktrees={"api": str(self.tmp / "gone")})
        self.usage(140_000)
        self.assertIn("软阈值", self.stop()["reason"])

    def test_hard_asks_regardless_and_records_the_request_once(self):
        self.window(200_000)
        (self.wt / "src" / "a.py").write_text("x = 2\n")
        self.usage(170_000)
        self.assertIn("硬阈值", self.stop()["reason"])
        self.stop()
        [e] = self.events("handoff_requested")
        self.assertEqual({k: e[k] for k in ("session", "batch", "role", "context_tokens", "soft", "hard", "window",
                                            "dirty")},
                         {"session": SESSION, "batch": BATCH, "role": "seat", "context_tokens": 170_000,
                          "soft": 130_000, "hard": 160_000, "window": 200_000, "dirty": 1})
