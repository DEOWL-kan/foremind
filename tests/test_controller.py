import contextlib
import io
import json
import os
import subprocess
import time
from pathlib import Path
from unittest import mock

from foremind import controller as ctl
from foremind import heartbeat, hooks, sessions
from foremind.carriers import Carrier, CarrierError
from foremind.commands import controller as cmd
from foremind.events import EventLog
from foremind.lock import ExitEvidence
from test_hooks import HookBase, usage_line

REAL_CLAUDE = ctl._claude  # ControllerBase stands a stub in for it

NAME = "fm-shop-controller-1"
SID = "7e068738-6aef-451c-88b8-d9fc42ff3c5e"  # the Stop sample's session_id

DOC = """# 交接：shop controller → 新会话

写于某时，由会话 x 写。

## 1. 目标与核对（先做）

```foremind-check
main_contains: {sha}
worktree_clean: true
batch auth.*: running
pending: 0
supervisor: stopped
```

## 2. 当前状态
## 3. 流程与工具
## 4. 决定与教训（勿重议）
## 5. 未完成义务（按优先级）
## 6. 指针
"""


def line(mid, tokens, model="claude-opus-5-5", out=10):
    e = json.loads(usage_line(mid, tokens, out=out))
    e["message"]["model"] = model
    return json.dumps(e)


class FakeCarrier(Carrier):
    """create() plays SessionStart through the real hook with the launch's environment."""
    name = "fake"

    def __init__(self, root, start=True):
        super().__init__(root)
        self.start, self.created, self.sent, self.closed = start, {}, [], []
        self.evidence = None  # close(): None, an ExitEvidence `how`, or an exception to raise

    def create(self, session, launch):
        self.created[session] = launch
        if self.start:
            with mock.patch.dict(os.environ, launch.env):
                hooks.main("SessionStart", json.dumps({"session_id": f"sid-{session}", "cwd": str(launch.cwd),
                                                       "transcript_path": f"/nowhere/{session}.jsonl"}))

    def send(self, session, text):
        self.sent.append((session, text))

    def close(self, session):
        self.closed.append(session)
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence and ExitEvidence(session, self.name, self.evidence)


class ControllerBase(HookBase):
    def setUp(self):
        super().setUp()
        self.ctl_env = {**self.base_env, "FOREMIND_PROJECT": str(self.root), "FOREMIND_ROLE": "controller",
                        ctl.MARKER: NAME}
        self.use_env(self.ctl_env)
        self.claude = (None, [])  # bind's ps walk (_claude): no real ps in these tests
        self.enterContext(mock.patch.object(ctl, "_claude", lambda: self.claude))

    def usage(self, tokens, model="claude-opus-5-5"):
        with open(self.transcript, "a") as f:
            f.write(line(f"msg_{tokens}", tokens, model) + "\n")

    def stop(self, active=False):
        d = self.data("Stop", cwd=self.root)
        d["stop_hook_active"] = active
        out = hooks.main("Stop", json.dumps(d))
        return json.loads(out) if out else None

    def st(self):
        return json.loads((self.state_dir() / f"{SID}.json").read_text())

    def state_dir(self):
        return self.state / "controller"

    def event(self, type, **fields):
        return EventLog(self.state / "events.jsonl").append(type, **fields)


class StopGateTest(ControllerBase):
    def setUp(self):
        super().setUp()
        hooks.main("SessionStart", json.dumps(self.data("SessionStart", cwd=self.root)))

    def other(self, event, sid, source="startup", **fields):
        """Hook `event` for another agent session under the same marker, with its own transcript."""
        d = {**self.data(event, cwd=self.root), "session_id": sid, "source": source,
             "transcript_path": str(self.root / f"{sid}.jsonl"), **fields}
        out = hooks.main(event, json.dumps(d))
        return json.loads(out) if out else None

    def test_soft_once_then_again_after_each_merge(self):
        self.usage(150_000)
        self.assertIsNone(self.stop())
        self.usage(210_000)
        reason = self.stop()["reason"]
        self.assertIn("总控上下文已用 210000 token（程序读数），过了软线 200000（硬线 300000）", reason)
        self.assertNotIn("又合入了", reason)
        self.assertIsNone(self.stop())
        self.event("batch_state", batch="auth.1", frm="in_review", to="merged")
        self.assertIn("又合入了 auth.1（自然断点）", self.stop()["reason"])
        self.assertIsNone(self.stop())
        self.event("batch_state", batch="auth.2", prior="running", state="blocked")  # not a merge
        self.assertIsNone(self.stop())
        self.event("reconciled", batch="auth.3", frm="in_review", to="merged")  # foremind land, the tick's catch-up
        self.assertIn("又合入了 auth.3（自然断点）", self.stop()["reason"])
        self.assertIsNone(self.stop())
        self.assertTrue(self.st()["soft_prompted"])
        self.assertEqual(self.st()["context_tokens"], 210_000)

    def test_hard_blocks_until_the_handoff_is_recorded(self):
        self.usage(310_000)
        reason = self.stop()["reason"]
        self.assertIn("总控上下文已用 310000 token（程序读数），达到硬线（有效预算 300000）", reason)
        self.assertIn(f"按 {ctl.TEMPLATE} 写 {self.root / ctl.DOC}，然后执行 `foremind controller handoff`", reason)
        at = self.st()["hard_requested_at"]
        self.assertIsNone(self.stop(active=True))
        self.assertEqual(self.stop()["decision"], "block")
        self.assertEqual(self.st()["hard_requested_at"], at, "the first request's time stays")
        self.event("controller_handoff", from_session="fm-shop-controller-9")
        self.assertEqual(self.stop()["decision"], "block", "someone else's handoff does not count")
        self.event("controller_handoff", from_session=NAME)
        self.assertIsNone(self.stop())

    def test_hard_request_holds_after_compact_or_clear(self):
        self.usage(310_000)
        at = self.stop() and self.st()["hard_requested_at"]
        self.transcript.write_text(line("m1", 40_000) + "\n")  # /compact: same session, reading below the line
        self.assertIn(f"硬线（有效预算 300000）已在 {at} 要求交接", self.stop()["reason"])
        new = "0c1ea2aa-0000-4000-8000-000000000001"
        self.other("SessionStart", new, source="clear")
        (self.root / f"{new}.jsonl").write_text(line("m2", 20_000) + "\n")
        self.assertIn("已在", self.other("Stop", new, stop_hook_active=False)["reason"])
        self.assertEqual(json.loads((self.state_dir() / f"{new}.json").read_text())["hard_requested_at"], at)
        EventLog(self.state / "archive" / "events-2026-08.jsonl").append("controller_handoff", from_session=NAME)
        self.assertTrue(ctl.handed_off(self.root, NAME), "archived events count (handoff.history)")
        self.assertIsNone(self.stop())

    def test_a_claude_started_from_the_controllers_shell_is_not_gated(self):
        nested = "0c1ea2aa-0000-4000-8000-000000000002"
        self.other("SessionStart", nested)  # inherits MARKER, but a fresh start under a bound name
        self.assertFalse((self.state_dir() / f"{nested}.json").exists())
        (self.root / f"{nested}.jsonl").write_text(line("m1", 400_000) + "\n")
        self.assertIsNone(self.other("Stop", nested, stop_hook_active=False))
        self.assertEqual(ctl.current(self.root)["agent_session_id"], SID)
        self.other("SessionStart", "0c1ea2aa-0000-4000-8000-000000000003", source="resume")  # --resume binds
        self.assertEqual(len(ctl._states(self.root, NAME)), 2)

    def test_explicit_controller_config_wins_over_the_defaults(self):
        self.usage(60_000)
        self.assertIsNone(self.stop())
        self.user_config("[context.by_role.controller]\nabs_cap_tokens = 50000\n")
        self.assertIn("达到硬线（有效预算 50000）", self.stop()["reason"])
        self.assertEqual(ctl.budget({}, None), (200_000, 300_000))
        seat_keys = {"context.abs_cap_tokens": 1000, "context.window_tokens": 200_000, "context.hard_pct": 80}
        self.assertEqual(ctl.budget(seat_keys, None), (200_000, 300_000),
                         "the seats' context.* never reaches the controller")
        m = "context.by_role.controller.claude-opus-5-5."
        self.assertEqual(ctl.budget({m + "abs_cap_tokens": 90_000}, "claude-opus-5-5")[1], 90_000)
        # a key left unset takes the controller's default, never below both it and the configured value
        self.assertEqual(ctl.budget({m + "abs_cap_tokens": 90_000}, "claude-other")[1], 300_000)
        self.assertEqual(ctl.budget({"context.by_role.controller.abs_cap_tokens": 400_000}, None)[1], 300_000)
        self.assertEqual(ctl.budget({"context.by_role.controller.hard_pct": 40}, None), (200_000, 400_000))

    def test_model_from_the_transcript_else_the_launch_record(self):
        self.user_config('[context.by_role.controller."claude-opus-5-5[1m]"]\nabs_cap_tokens = 50000\n')
        self.usage(60_000, model="claude-opus-5-5[1m]")
        self.assertIn("有效预算 50000", self.stop()["reason"])
        cfg = {"context.by_role.controller.claude-opus-5-5[1m].abs_cap_tokens": 50_000}
        self.assertIsNone(ctl._model(self.root, cfg, NAME, None), "no model in the record, no launch record")
        self.event("controller_launch", session=NAME, model="claude-opus-5-5[1m]")
        self.assertEqual(ctl._model(self.root, cfg, NAME, None), "claude-opus-5-5[1m]")
        self.assertEqual(ctl._model(self.root, cfg, NAME, "claude-opus-5-5"), "claude-opus-5-5[1m]",
                         "spelled without [1m]: the launch model has the entry")
        self.assertEqual(ctl._model(self.root, {}, NAME, "claude-opus-5-5"), "claude-opus-5-5")

    def test_unreadable_context_never_blocks_and_is_logged_once(self):
        self.assertIsNone(self.stop())
        self.assertIsNone(self.stop())
        errs = self.events("hook_error")
        self.assertEqual(len(errs), 1)
        self.assertIn("no context reading", errs[0]["error"])

    def test_other_sessions_are_untouched(self):
        self.usage(400_000)
        before = sorted(p.read_text() for p in self.state_dir().glob("*.json"))
        self.use_env(self.base_env)  # a plain session: no marker
        self.assertIsNone(self.stop())
        self.assertIsNone(hooks.main("SessionStart", json.dumps(self.data("SessionStart", cwd=self.root))))
        self.assertEqual(sorted(p.read_text() for p in self.state_dir().glob("*.json")), before)
        self.use_env(self.seat_env)  # a seat: the marker changes nothing
        seat_out = hooks.main("Stop", json.dumps(self.data("Stop")))
        heartbeat.update(self.root, "fm-shop-auth_2-1", soft_prompted=False, handoff_requested=False)
        self.use_env({**self.seat_env, ctl.MARKER: NAME})
        self.assertEqual(hooks.main("Stop", json.dumps(self.data("Stop"))), seat_out)
        self.assertIn("硬阈值", json.loads(seat_out)["reason"])
        self.assertEqual(sorted(p.read_text() for p in self.state_dir().glob("*.json")), before)

    def test_session_start_binds(self):
        self.assertIsNone(hooks.main("SessionStart", json.dumps(self.data("SessionStart", cwd=self.root))))
        st = self.st()
        self.assertEqual((st["controller"], st["transcript_path"], st["source"]),
                         (NAME, str(self.transcript), "startup"))
        self.assertEqual(ctl.current(self.root)["agent_session_id"], SID)
        self.assertFalse((self.state / "heartbeats").exists(), "no heartbeat: not a Foremind session")


class BindTest(ControllerBase):
    """bind keeps its claude's pid and lstart (sessions.live, check closing it); m2c.10 r2 note 2."""
    CTL = sessions.Proc(500, 400, 1.0, 1000, "Mon Sep 28 10:00:00 2026", "claude --model claude-opus-5-5[1m]")

    def start(self, sid, source="startup"):
        hooks.main("SessionStart", json.dumps({**self.data("SessionStart", cwd=self.root), "session_id": sid,
                                               "source": source}))
        p = self.state_dir() / f"{sid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def test_pid_lstart_and_claudes_under_a_bound_controller(self):
        self.claude = (self.CTL, [(400, "Mon Sep 28 09:59:59 2026"), (1, "Mon Sep  7 08:00:00 2026")])
        st = self.start(SID)
        self.assertEqual((st["pid"], st["lstart"]), (500, self.CTL.lstart))
        self.assertIsNotNone(self.start("sid-clear", source="clear"), "/clear in the same claude binds")
        nested = sessions.Proc(700, 650, 0.0, 0, "Mon Sep 28 11:00:00 2026", "claude -c")
        for source in ("resume", "startup"):  # continued / resumed from the controller's shell: not a controller
            self.claude = (nested, [(650, "Mon Sep 28 10:59:00 2026"), (500, self.CTL.lstart)])
            self.assertIsNone(self.start(f"sid-nested-{source}", source=source))
        self.claude = (nested, [(650, "Mon Sep 28 10:59:00 2026"), (500, "Tue Sep 29 08:00:00 2026")])
        self.assertIsNotNone(self.start("sid-reused", source="resume"), "pid 500 reused: no bound controller")
        self.claude = (None, [])  # ps cannot tell: bound as before, without a pid
        self.assertNotIn("pid", self.start("sid-nops", source="resume"))

    def test_the_nearest_claude_above_the_hook(self):
        me = os.getpid()
        ps = lambda: "".join(f"{pid} {ppid} 0.0 10 Mon Sep 28 10:00:00 2026 {cmd}\n" for pid, ppid, cmd in (  # noqa
            (me, 9001, "python3 -P -m foremind hook SessionStart"), (9001, 9002, "/bin/sh -c PYTHONPATH=x python3"),
            (9002, 9003, "/Users/u/.local/bin/claude --model m"), (9003, 1, "claude --resume"), (1, 0, "launchd")))
        p, above = REAL_CLAUDE(ps)
        self.assertEqual((p.pid, above), (9002, [(9003, p.lstart), (1, p.lstart)]))
        self.assertEqual(REAL_CLAUDE(lambda: f"{me} 1 0.0 10 Mon Sep 28 10:00:00 2026 python3\n"), (None, []))

        def broken():
            raise sessions.SessionsError("denied")
        self.assertEqual(REAL_CLAUDE(broken), (None, []))


class OpenHandoffTest(ControllerBase):
    def setUp(self):
        super().setUp()
        self.carrier = FakeCarrier(self.root)
        self.cfg = {"project.name": "shop"}

    def bound(self, tokens=250_000):
        """The running controller: bound, with context read by a Stop."""
        hooks.main("SessionStart", json.dumps(self.data("SessionStart", cwd=self.root)))
        self.usage(tokens)
        self.stop()

    def test_open_launches_without_a_foremind_session(self):
        out = ctl.open_controller(self.root, carrier=self.carrier, config=self.cfg)
        name = out["session"]
        self.assertRegex(name, r"^fm-shop-[0-9a-f]{6}-controller-1$")
        launch = self.carrier.created[name]
        self.assertEqual(launch.env, {"FOREMIND_ROLE": "controller", ctl.MARKER: name,
                                      "FOREMIND_PROJECT": str(self.root)})
        self.assertNotIn("--settings", launch.argv)
        self.assertEqual(launch.argv[launch.argv.index("--model") + 1], "claude-opus-5-5[1m]")
        self.assertEqual(launch.argv[launch.argv.index("--effort") + 1], "high")
        self.assertEqual(launch.cwd, self.root)
        (to, text), = self.carrier.sent
        self.assertEqual(to, name)
        self.assertIn("由 `foremind controller open` 开出", text)
        self.assertNotIn("controller check", text)
        self.assertEqual(self.events("controller_opened")[0]["session"], name)
        self.assertEqual(self.events("controller_launch")[0]["model"], "claude-opus-5-5[1m]")
        again = ctl.open_controller(self.root, carrier=self.carrier,
                                    config={**self.cfg, "routes.controller_live.effort": "xhigh"})
        self.assertTrue(again["session"].endswith("-controller-2"))
        self.assertEqual(again["effort"], "xhigh")

    def test_open_without_session_start_closes(self):
        carrier = FakeCarrier(self.root, start=False)
        self.assertRaisesRegex(ctl.ControllerError, "no SessionStart", ctl.open_controller, self.root,
                               carrier=carrier, config={**self.cfg, "seat.sessionstart_timeout_s": 0})
        self.assertEqual(carrier.closed, list(carrier.created))
        self.assertEqual(carrier.sent, [])

    def write_doc(self, text=None):
        (self.root / ctl.DOC).write_text(text or DOC.format(sha="abc123"), encoding="utf-8")

    def test_handoff(self):
        self.bound()
        self.assertRaisesRegex(ctl.ControllerError, "missing", ctl.handoff, self.root, carrier=self.carrier,
                               config=self.cfg)
        self.write_doc(DOC.format(sha="abc").replace("## 4. 决定与教训（勿重议）\n", ""))
        self.assertRaisesRegex(ctl.ControllerError, "sections missing: 4. 决定与教训", ctl.handoff, self.root,
                               carrier=self.carrier, config=self.cfg)
        self.write_doc(DOC.format(sha="abc").replace("pending: 0", "pending: none"))
        self.assertRaisesRegex(ctl.ControllerError, "cannot read 'pending: none'", ctl.handoff, self.root,
                               carrier=self.carrier, config=self.cfg)
        self.assertEqual(self.carrier.created, {})
        self.write_doc()
        out = ctl.handoff(self.root, carrier=self.carrier, config=self.cfg)
        text = (self.root / ctl.DOC).read_text(encoding="utf-8")
        self.assertIn("# 交接：shop controller → 新会话\n\n写交接时上下文 250000 token（程序读数，有效预算 300000）。\n写于某时",
                      text)
        ev, = self.events("controller_handoff")
        self.assertEqual((ev["from_session"], ev["agent_session_id"], ev["context_tokens"], ev["calls"]),
                         (NAME, SID, 250_000, 1))
        self.assertEqual((ev["input_tokens"], ev["cache_read_tokens"], ev["cache_write_tokens"], ev["output_tokens"]),
                         (2, 249_998, 0, 10))
        self.assertEqual(ev["sha256"], ctl.sha256_bytes(text.encode()))
        self.assertEqual(out["predecessor"], NAME)
        (to, kickoff), = self.carrier.sent
        self.assertEqual(to, out["session"])
        self.assertIn(f"接替 {NAME}", kickoff)
        self.assertIn("先执行 `foremind controller check`", kickoff)
        self.assertLess(kickoff.index("controller check"), kickoff.index(ctl.DOC))
        self.assertRaisesRegex(ctl.ControllerError, "already handed off", ctl.handoff, self.root,
                               carrier=self.carrier, config=self.cfg)
        self.assertIsNone(self.stop(), "the handed-off controller may stop")

    def test_handoff_after_a_carrier_failure_runs_again_and_replaces_the_reading(self):
        self.bound()
        self.write_doc()
        self.assertRaises(ctl.ControllerError, ctl.handoff, self.root, carrier=FakeCarrier(self.root, start=False),
                          config={**self.cfg, "seat.sessionstart_timeout_s": 0})
        self.assertEqual(len(self.events("controller_handoff")), 1, "document and event stay")
        self.usage(260_000)
        ctl.handoff(self.root, carrier=self.carrier, config=self.cfg)
        text = (self.root / ctl.DOC).read_text(encoding="utf-8")
        self.assertEqual(text.count("写交接时上下文"), 1)
        self.assertIn("写交接时上下文 260000 token", text)

    def test_the_doc_must_change_after_the_hard_request(self):
        self.write_doc()
        old = time.time() - 60
        os.utime(self.root / ctl.DOC, (old, old))
        self.bound(tokens=310_000)  # the hard line: hard_requested_at is now
        self.assertIn("hard_requested_at", self.st())
        self.assertRaisesRegex(ctl.ControllerError, "not changed since the hard line", ctl.handoff, self.root,
                               carrier=self.carrier, config=self.cfg)
        self.write_doc()
        os.utime(self.root / ctl.DOC, (time.time() + 5,) * 2)
        ctl.handoff(self.root, carrier=self.carrier, config=self.cfg)

    def test_a_voluntary_handoff_needs_a_doc_written_by_this_controller(self):
        self.write_doc()
        old = time.time() - 60
        os.utime(self.root / ctl.DOC, (old, old))  # the predecessor's, unchanged
        self.bound()  # past the soft line only
        self.assertRaisesRegex(ctl.ControllerError, "not changed since this controller started", ctl.handoff,
                               self.root, carrier=self.carrier, config=self.cfg)

    def test_command_line(self):
        self.bound()
        self.write_doc()
        with mock.patch.object(cmd.carriers, "get", return_value=self.carrier), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cmd._handoff(None, self.root), 0)
        self.assertIn("写交接时上下文 250000 token", out.getvalue())


class CheckSlipTest(ControllerBase):
    def setUp(self):
        super().setUp()
        git = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True, capture_output=True,  # noqa
                                        text=True).stdout.strip()
        git("init", "-q")
        (self.root / ".gitignore").write_text(".foremind/\n")
        git("add", ".gitignore")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "first")
        self.sha = git("rev-parse", "--short", "HEAD")
        (self.root / ctl.DOC).write_text(DOC.format(sha=self.sha), encoding="utf-8")  # untracked: not "dirty"
        self.event("controller_launch", session=NAME, predecessor="fm-shop-controller-0")
        hooks.main("SessionStart", json.dumps(self.data("SessionStart", cwd=self.root)))
        self.usage(120_000)
        self.usage(130_000)

    def test_all_ok(self):
        items, ev, _ = ctl.check(self.root)
        self.assertEqual([(i["key"], i["ok"]) for i in items],
                         [("main_contains", True), ("worktree_clean", True), ("batch auth.*", True),
                          ("pending", True), ("supervisor", True)])
        self.assertTrue(ev["ok"])
        self.assertEqual((ev["session"], ev["predecessor"], ev["context_tokens"], ev["calls"], ev["mismatches"]),
                         (NAME, "fm-shop-controller-0", 130_000, 2, []))
        self.assertEqual(ev["cache_read_tokens"], 120_000 + 130_000 - 4)
        line_ = ctl.takeover_line(items, ev)
        self.assertIn(f"总控 {NAME} 接手 fm-shop-controller-0：核对 5 项，全部 ok；接手时上下文 130000 token、2 次调用", line_)

    def handed_off_by(self, pred):
        """`pred` launched and opened by Foremind, handed off to NAME."""
        self.event("controller_launch", session=pred)
        self.event("controller_opened", session=pred, carrier="fake")
        self.event("controller_handoff", from_session=pred)
        self.event("controller_opened", session=NAME, predecessor=pred, carrier="fake")

    def check_out(self, car):
        """`foremind controller check`'s output, closing through `car`."""
        with mock.patch.object(ctl.carriers, "get", return_value=car), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cmd._check(None, self.root)
        return out.getvalue()

    def test_all_ok_closes_the_predecessor(self):
        pred = "fm-shop-controller-0"
        car = FakeCarrier(self.root)
        self.assertIn(f"前任 {pred}：没有 Foremind 启动记录（controller_launch），程序不关；请你关闭它的终端",
                      self.check_out(car))
        self.handed_off_by(pred)
        car.evidence = CarrierError("orca terminal close: busy")
        closed = ctl.check(self.root, carrier=car)[2]
        self.assertEqual({k: closed.get(k) for k in ("session", "predecessor", "confirmed", "how", "error")},
                         {"session": NAME, "predecessor": pred, "confirmed": False, "how": None,
                          "error": "orca terminal close: busy"})
        car.evidence = "absent"
        self.assertIn(f"前任 {pred}：已关闭（absent）", self.check_out(car))
        (ev,) = [e for e in self.events("controller_closed") if e["confirmed"]]
        self.assertEqual((ev["session"], ev["predecessor"], ev["carrier"], ev["how"]), (NAME, pred, "fake", "absent"))
        self.assertIsNone(ctl.check(self.root, carrier=car)[2], "closed once, for good")
        self.assertEqual(car.closed, [pred, pred])
        (self.root / "stray.txt").write_text("x")  # m2d.1 r2: a mismatch now does not keep what is closed
        self.assertEqual(ctl.check(self.root, carrier=car)[2], {"predecessor": pred, "kept": "前任已关闭（此前的 check）"})
        self.assertIn(f"前任 {pred}：前任已关闭（此前的 check）", self.check_out(car))

    def test_the_predecessor_stays_unless_every_item_is_ok_and_it_handed_off_to_us(self):
        pred = "fm-shop-controller-0"
        car = FakeCarrier(self.root)
        self.event("controller_launch", session=pred)
        self.event("controller_opened", session=pred, carrier="fake")
        self.event("controller_opened", session=NAME, predecessor=pred, carrier="fake")
        self.assertIn("前任保留：", ctl.check(self.root, carrier=car)[2]["kept"], "it never handed off")
        self.event("controller_handoff", from_session=pred)
        (self.root / "stray.txt").write_text("x")
        items, ev, closed = ctl.check(self.root, carrier=car)
        self.assertEqual((ev["ok"], closed, car.closed), (False, {"predecessor": pred, "kept": "核对有不符，前任保留"}, []))
        self.assertIn(f"前任 {pred}：核对有不符，前任保留", self.check_out(car))
        self.assertEqual((car.closed, self.events("controller_closed")), ([], []))

    def test_mismatches_and_the_optional_test(self):
        (self.root / "stray.txt").write_text("x")
        (self.state / "decisions").mkdir()
        (self.state / "decisions" / "Q-1.json").write_text(json.dumps({"state": "open"}))
        self.write_header(state="merged")
        doc = DOC.format(sha="0000000").replace("supervisor: stopped", "supervisor: running\ntest: exit 3")
        (self.root / ctl.DOC).write_text(doc, encoding="utf-8")
        items, ev, _ = ctl.check(self.root)
        got = {i["key"]: (i["ok"], i["got"]) for i in items}
        self.assertFalse(got["main_contains"][0])
        self.assertEqual(got["worktree_clean"][0], False)
        self.assertIn("stray.txt", got["worktree_clean"][1])
        self.assertEqual(got["batch auth.*"], (False, "auth.2 merged"))
        self.assertEqual(got["pending"], (False, "1"))
        self.assertEqual(got["supervisor"], (False, "stopped"))
        self.assertEqual(got["test"], (False, "exit 3"))
        self.assertFalse(ev["ok"])
        self.assertIn("pending: 应 0，实际 1", ev["mismatches"])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cmd._check(None, self.root), 1)
        self.assertIn("不符 pending: 0 → 实际 1", out.getvalue())
        self.assertIn("可贴进 PROGRESS.md", out.getvalue())

    def test_test_runs_only_when_written(self):
        ran = []
        ctl.check(self.root, run=lambda c: ran.append(c))
        self.assertEqual(ran, [])
        (self.root / ctl.DOC).write_text(DOC.format(sha=self.sha).replace("pending: 0", "pending: 0\ntest: true"),
                                         encoding="utf-8")
        items, ev, _ = ctl.check(self.root)
        self.assertIn({"key": "test", "want": "true", "ok": True, "got": "exit 0"}, items)

    def test_parse_checks(self):
        self.assertRaisesRegex(ctl.ControllerError, "no ```foremind-check", ctl.parse_checks, "# x\n")
        self.assertRaisesRegex(ctl.ControllerError, "empty", ctl.parse_checks, "```foremind-check\n# c\n\n```\n")
        for bad in ("color: red", "worktree_clean: yes", "supervisor: up", "batch : merged", "pending:"):
            self.assertRaisesRegex(ctl.ControllerError, "cannot read", ctl.parse_checks,
                                   f"```foremind-check\n{bad}\n```\n")
        self.assertEqual(ctl.parse_checks("```foremind-check\nmain_contains: a\nmain_contains: b\ntest: x: y\n```"),
                         [("main_contains", "a"), ("main_contains", "b"), ("test", "x: y")])
        template = ctl.TEMPLATE.read_text(encoding="utf-8")
        # every section is there, and a block left unfilled does not pass
        self.assertRaisesRegex(ctl.ControllerError, "cannot read 'pending: <未决数>'", ctl.check_doc, template)
        ctl.check_doc(template.replace("<未决数>", "0"))

    def test_no_marker_skips_controllers_handed_off(self):  # m2e REQ-6
        pred = "fm-shop-controller-0"
        (self.state_dir() / "sid-pred.json").write_text(json.dumps({  # its Stop after ours
            "controller": pred, "agent_session_id": "sid-pred", "updated_at": "9999-01-01T00:00:00+00:00"}))
        self.event("controller_handoff", from_session=pred)
        with mock.patch.dict(os.environ, {ctl.MARKER: ""}):
            self.assertEqual(ctl.current(self.root)["controller"], NAME)
            self.assertEqual(ctl.slip(self.root, "other", "x")["session"], NAME)
            self.assertEqual(ctl.check(self.root)[1]["session"], NAME)
            self.event("controller_handoff", from_session=NAME)  # every one handed off: none
            self.assertIsNone(ctl.current(self.root))
            ev = ctl.slip(self.root, "other", "x")
            self.assertEqual((ev["session"], ev["agent_session_id"], ev["context_tokens"]), (None, None, None))
            _, ev, closed = ctl.check(self.root)
            self.assertEqual((ev["session"], ev["predecessor"], closed), (None, None, None))
            self.assertRaisesRegex(ctl.ControllerError, "找不到总控会话", ctl.handoff, self.root,
                                   carrier=FakeCarrier(self.root), config={})
        self.assertEqual(ctl.current(self.root)["controller"], NAME, "the marker still picks it")
        with mock.patch.dict(os.environ, {ctl.MARKER: pred}):
            self.assertEqual(ctl.current(self.root)["controller"], pred)

    def test_slip(self):
        ev = ctl.slip(self.root, "wrong_fact", "  把 24 万写成 14 万 ")
        self.assertEqual((ev["kind"], ev["note"], ev["session"], ev["context_tokens"]),
                         ("wrong_fact", "把 24 万写成 14 万", NAME, 130_000))
        self.assertRaisesRegex(ctl.ControllerError, "kind must be one of", ctl.slip, self.root, "typo", "x")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(SystemExit, cmd_main, ["controller", "slip", "--kind", "typo", "--note", "x"])
            self.assertRaises(SystemExit, cmd_main, ["controller", "slip", "--kind", "other", "--note", "x",
                                                     "--context-tokens", "5"])  # never taken by hand
        for p in self.state_dir().glob("*.json"):
            p.unlink()
        self.assertIsNone(ctl.slip(self.root, "other", "no controller bound")["context_tokens"])


def cmd_main(argv):
    from foremind import cli
    return cli.main(argv)
