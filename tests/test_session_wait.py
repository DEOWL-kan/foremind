"""m2b.8: a seat waiting in its terminal (Notification hook -> heartbeat waiting_input -> one notice, no stuck stages);
finding 26: a PreToolUse after the turn ended never marks the seat busy, and a stale tool_open is cleared."""
import json
import os
from datetime import datetime, timezone
from unittest import mock

from test_hooks import BATCH, SESSION, HookBase
from test_supervisor import Base, iso

from foremind import hooks, inbox

M = 60
GHOST = "toolu_01QidaLdfUCRhnBseC6ReM6e"  # finding 26's call, never in the transcript


class NotificationHookTest(HookBase):
    def notify(self, **over):
        return self.run_hook("Notification", {**self.data("Notification"), **over})

    def test_waiting_is_recorded_and_cleared_by_the_next_hook(self):
        self.run_hook("PreToolUse", self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")))
        for clear in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"):
            self.assertIsNone(self.notify(), "no output, no push from the hook (I38)")
            w = self.hb()["waiting_input"]
            self.assertEqual((w["kind"], w["message_sha256"]),
                             ("permission_prompt", hooks._sha("Claude needs your permission to use Bash")))
            self.assertNotEqual(self.hb()["event"], "Notification", "event stays the last turn hook")
            if clear == "PreToolUse":
                self.run_hook(clear, self.data("PreToolUse-Write", file_path=str(self.wt / "src/b.py")))
            elif clear == "PostToolUse":
                self.run_hook(clear, self.data("PostToolUse-Write", file_path=str(self.wt / "src/b.py")))
            else:
                self.run_hook(clear, self.data(clear))
            self.assertIsNone(self.hb()["waiting_input"], clear)
        got = self.events("session_waiting")
        self.assertEqual([(e["session"], e["batch"], e["kind"]) for e in got],
                         [(SESSION, BATCH, "permission_prompt")] * 4)
        self.assertEqual(self.events("notify"), [])

    def test_only_types_that_wait_on_someone(self):
        self.notify(notification_type="auth_success")
        self.assertIsNone((self.hb() or {}).get("waiting_input"))
        self.notify(notification_type="idle_prompt")  # asked in its terminal and stopped (finding 4)
        self.assertEqual(self.hb()["waiting_input"]["kind"], "idle_prompt")
        self.assertEqual(self.hb()["transcript_path"], str(self.transcript))  # m2c.2: stuck.py reads its tail
        d = self.data("Notification")
        del d["notification_type"]  # older Claude Code: no type, counted
        self.run_hook("Notification", d)
        self.assertIsNone(self.hb()["waiting_input"]["kind"])
        self.assertEqual([e["kind"] for e in self.events("session_waiting")], ["idle_prompt", None])

    def test_one_wait_keeps_its_first_at(self):
        with mock.patch.object(hooks, "datetime") as dt:
            dt.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            self.notify()
            first = self.hb()["waiting_input"]["at"]
            dt.now.return_value = datetime(2030, 1, 1, 0, 5, tzinfo=timezone.utc)
            self.notify(notification_type="idle_prompt")
            self.assertEqual(self.hb()["waiting_input"]["at"], first)
            self.run_hook("UserPromptSubmit", self.data("UserPromptSubmit"))  # cleared: the next wait is a new one
            self.notify()
            self.assertNotEqual(self.hb()["waiting_input"]["at"], first)

    def test_not_a_foremind_session(self):
        self.use_env(self.base_env)
        self.notify()
        self.assertEqual(self.events("session_waiting"), [])


class GhostToolHookTest(HookBase):
    def pre(self, tool_use_id):
        return self.run_hook("PreToolUse", {**self.data("PreToolUse-Write", file_path=str(self.wt / "src/a.py")),
                                            "tool_use_id": tool_use_id})

    def stop(self):
        return self.run_hook("Stop", {**self.data("Stop"), "stop_hook_active": False})

    def test_a_call_after_the_turn_ended_is_no_work(self):
        self.stop()
        self.pre(GHOST)
        hb = self.hb()
        self.assertEqual((hb["tool_open"], hb["event"]), (False, "Stop"), "still idle for the supervisor")
        self.assertEqual([e["tool_use_id"] for e in self.events("tool_after_stop")], [GHOST])
        self.run_hook("UserPromptSubmit", self.data("UserPromptSubmit"))  # a new turn: its calls count
        self.pre("toolu_real")
        self.assertEqual(self.hb()["open_tools"], ["toolu_real"])

    def test_a_stop_that_blocks_lets_the_turn_go_on(self):
        inbox.append(SESSION, "review list", sender="supervisor#x", root=self.root)
        self.assertEqual(self.stop()["decision"], "block")
        self.pre("toolu_next")
        self.assertEqual(self.hb()["open_tools"], ["toolu_next"])
        self.assertEqual(self.events("tool_after_stop"), [])

    def test_stop_blocked_is_written_before_the_cursor_moves(self):
        inbox.append(SESSION, "review list", sender="supervisor#x", root=self.root)
        real, seen = inbox.mark_delivered, []

        def mark(*a, **kw):
            seen.append(self.hb().get("stop_blocked"))
            return real(*a, **kw)

        with mock.patch.object(inbox, "mark_delivered", mark):
            self.stop()
        self.assertEqual(seen, [True], "a Stop heartbeat after the delivery would read as idle")

    def test_a_block_that_never_got_out_ended_the_turn(self):
        inbox.append(SESSION, "review list", sender="supervisor#x", root=self.root)

        def broken_pipe(text):
            raise BrokenPipeError

        hooks.main("Stop", json.dumps({**self.data("Stop"), "stop_hook_active": False}), emit=broken_pipe)
        self.pre(GHOST)
        self.assertEqual(self.hb()["tool_open"], False)


class SupervisorWaitTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.carrier.idle = False  # keep the supervisor's messages in the inbox

    def hook(self, event, **data):
        env = {"FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-s", "FOREMIND_BATCH": "p.1",
               "FOREMIND_ROLE": "seat"}
        with mock.patch.dict(os.environ, env):
            out = hooks.main(event, json.dumps({"cwd": str(self.root), "session_id": "sid", **data}))
        return json.loads(out) if out else None

    def test_waiting_in_the_terminal_is_notified_once_not_stuck(self):
        w = {"at": iso(self.now), "kind": "permission_prompt", "message_sha256": "0" * 64}
        self.hold("p.1", "fm-s", waiting_input=w)
        for at in (21, 41, 62, 90):
            self.tick(at * M)
        self.assertEqual((self.header("p.1")["state"], self.pending("fm-s")), ("running", []))
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 在终端里等你回答"])
        self.beat("fm-s", "p.1", self.now + 90 * M)  # answered: the stages start over from there
        self.tick(111 * M)
        self.assertEqual(len(self.pending("fm-s")), 1)
        self.carrier.alive["fm-s"] = False  # gone while waiting: taken over as ever
        self.beat("fm-s", "p.1", self.now + 111 * M, waiting_input={**w, "at": iso(self.now + 111 * M)})
        self.tick(112 * M)
        self.assertEqual(self.header("p.1")["state"], "stuck")

    def test_ghost_pre_tool_use_after_stop_still_gets_direct_delivery(self):  # finding 26
        self.hold("p.1", "fm-s")
        self.hook("Stop", stop_hook_active=False)
        self.hook("PreToolUse", tool_name="Grep", tool_input={"pattern": "x"}, tool_use_id=GHOST)
        self.assertEqual([e["tool_use_id"] for e in self.events("tool_after_stop")], [GHOST])
        inbox.append("fm-s", "审查修改清单", sender="supervisor#r1", root=self.root)
        self.tick()
        self.assertEqual(len(self.carrier.sent), 1, "idle by its last Stop, never busy")
        self.assertEqual(self.pending("fm-s"), [])

    def test_stale_tool_open_on_an_idle_session_is_cleared(self):  # finding 26, a call that never finished
        self.carrier.idle = True
        self.hold("p.1", "fm-s", event="PreToolUse", tool_open=True, open_tools=[GHOST])
        inbox.append("fm-s", "审查修改清单", sender="supervisor#r1", root=self.root)
        self.tick(19 * M)
        self.assertEqual((self.carrier.sent, self.events("tool_open_cleared")), ([], []))
        self.tick(21 * M)
        [e] = self.events("tool_open_cleared")
        self.assertEqual((e["session"], e["batch"], e["tools"]), ("fm-s", "p.1", [GHOST]))
        self.assertEqual(len(self.carrier.sent), 1)
        self.assertEqual(json.loads((self.root / ".foremind" / "heartbeats" / "fm-s.json").read_text())["open_tools"],
                         [])
