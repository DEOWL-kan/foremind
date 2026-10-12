"""m2f.3 REQ-5: `foremind say --direct` (refusals, WAIT, the lock-held recheck and intent, holder_changed) and the
seat hook's three kinds of prompt (program, controller, external)."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from test_hooks import SESSION, HookBase

from foremind import carriers, cli, header, heartbeat, hooks, inbox, lock
from foremind.carriers import Carrier
from foremind.commands import say
from foremind.events import EventLog
from foremind.schemas import EXAMPLES

B, S2 = "auth.2", "fm-shop-auth_2-2"
GET = carriers.get


class FakeCarrier(Carrier):
    name = "fake"

    def __init__(self, root, cfg=None, confirm=True, on_send=None):
        super().__init__(root, cfg)
        self.confirm, self.on_send, self.sent = confirm, on_send, []

    def send(self, session, text):
        log = EventLog(self.root / ".foremind" / "events.jsonl")
        intents = [e for e in log.iter() if e["type"] == "controller_delivery" and e["phase"] == "intent"]
        self.sent.append((session, text, len(intents)))
        if self.on_send:
            self.on_send()
        return self.confirm


class DirectTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = tmp / "proj"
        (self.root / ".foremind" / "batches").mkdir(parents=True)
        (self.root / ".foremind" / "batches" / f"{B}.md").write_text(
            header.render({**EXAMPLES["batch_header"], "state": "running"}, ""))
        (tmp / "cfg").mkdir()
        lock.acquire(self.root, B, SESSION)
        self.log = EventLog(self.root / ".foremind" / "events.jsonl")
        self.carrier = FakeCarrier(self.root)
        self.get = self.enterContext(mock.patch.object(carriers, "get", return_value=self.carrier))
        keep = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_")}
        self.enterContext(mock.patch.dict(os.environ, {**keep, "FOREMIND_PROJECT": str(self.root),
                                                       "FOREMIND_CONFIG_HOME": str(tmp / "cfg")}, clear=True))

    def say(self, text="  看一下 PR\r\n", direct=True):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["say", B, text] + (["--direct"] if direct else []))
        return code, out.getvalue() + err.getvalue()

    def deliveries(self, phase=None):
        return [e for e in self.log.iter() if e["type"] == "controller_delivery" and phase in (None, e["phase"])]

    def assert_refused(self, code, needle):
        got, out = self.say()
        self.assertEqual((got, self.carrier.sent, self.deliveries()), (code, [], []), out)
        self.assertIn(needle, out)

    def test_sends_to_the_holder_after_its_intent(self):
        code, out = self.say()
        self.assertEqual((code, out.strip()), (0, f"-> {SESSION}（已确认送达）"))
        self.assertEqual(self.carrier.sent, [(SESSION, "看一下 PR", 1)])  # normalized; the intent came first
        [i] = self.deliveries("intent")
        self.assertEqual({k: i[k] for k in ("session", "batch", "carrier", "text_sha256", "by")},
                         {"session": SESSION, "batch": B, "carrier": "fake", "by": "user",
                          "text_sha256": hooks.delivery_sha("看一下 PR")})
        [r] = self.deliveries("result")
        self.assertEqual((r["dedupe_id"], r["ok"], r["confirmed"], "holder_changed" in r), (i["dedupe_id"], True, True,
                                                                                             False))
        self.assertEqual(inbox.pending_messages(SESSION, root=self.root), [])  # no inbox
        self.carrier.confirm = None
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "controller"}):
            self.assertEqual(self.say(), (0, f"-> {SESSION}（已发送，未确认）\n"))
        self.assertEqual(self.deliveries("intent")[-1]["by"], "controller")

    def test_carrier_from_the_seat_launch_else_config(self):
        self.say()
        self.assertEqual(self.get.call_args.args[0], "tmux")  # carrier.kind's default
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{SESSION}", session=SESSION,
                        carrier="orca")
        self.say()
        self.assertEqual(self.get.call_args.args[0], "orca")

    def test_no_session_or_the_user(self):
        lock.release(self.root, B, SESSION)
        self.assert_refused(1, "no session holds it")
        lock.acquire(self.root, B, lock.USER)
        self.assert_refused(1, "held by the user")

    def test_holder_handing_off(self):
        heartbeat.update(self.root, SESSION, handoff_requested=True)
        self.assert_refused(1, f"REFUSED：{SESSION} 正在交接")

    def test_a_successor_opening(self):
        self.log.append("seat_open", phase="intent", dedupe_id=f"seat_open:{S2}", batch=B, session=S2,
                        successor=True, predecessor=SESSION)
        self.assert_refused(1, f"REFUSED：继任 {S2} 已在开")
        self.log.append("seat_open", dedupe_id=f"seat_open:{S2}", batch=B, session=S2, ok=False, error="x")
        self.assertEqual(self.say()[0], 0)  # a failed open is no successor

    def test_a_successor_opened_until_it_is_gone_or_accepted(self):
        self.log.append("seat_opened", batch=B, session=S2, successor=True, predecessor=SESSION)
        self.assert_refused(1, f"REFUSED：继任 {S2} 已在开")
        self.log.append("sv_close", session=S2, carrier="fake", confirmed=True)
        self.assertEqual(self.say()[0], 0)
        s3 = "fm-shop-auth_2-3"
        self.log.append("seat_opened", batch=B, session=s3, successor=True, predecessor=SESSION)
        self.assertEqual(self.say()[0], 1)
        self.log.append("handoff_accept", batch=B, session=s3)
        self.assertEqual(self.say()[0], 0)
        self.log.append("seat_opened", batch=B, session="fm-shop-auth_2-4", successor=True, predecessor="fm-other")
        self.assertEqual(self.say()[0], 0)  # another holder's successor

    def test_wait_while_a_handoff_receipt_is_fresh(self):
        self.log.append("review_receipt", batch=B, round=1)
        self.assertEqual(self.say()[0], 0)  # no handoff written
        self.log.append("handoff_written", batch=B, session=SESSION)
        self.carrier.sent.clear()
        self.assertEqual(self.say()[0], 3)
        self.assertIn(f"WAIT：{SESSION} 写过交接段", self.say()[1])
        self.assertEqual(self.carrier.sent, [])
        with mock.patch.object(say, "RECEIPT_WAIT", timedelta(0)):
            self.assertEqual(self.say()[0], 0)

    def test_lock_moved_before_the_recheck(self):
        def moved(*a):
            lock.transfer(self.root, B, SESSION, S2)
            return self.carrier
        self.get.side_effect = moved
        self.assert_refused(1, f"REFUSED：锁已转给 {S2}")

    def test_recheck_under_the_lock_sees_a_handoff(self):
        def requested(*a):
            heartbeat.update(self.root, SESSION, handoff_requested=True)
            return self.carrier
        self.get.side_effect = requested
        self.assert_refused(1, "正在交接")

    def test_lock_moved_while_sending(self):
        self.carrier.on_send = lambda: lock.transfer(self.root, B, SESSION, S2)
        code, out = self.say()
        self.assertEqual(code, 4)
        self.assertIn(f"WARN：发送期间锁已转给 {S2}", out)
        [r] = self.deliveries("result")
        self.assertEqual((r["ok"], r["holder_changed"]), (True, True))

    def test_carrier_errors_and_manual(self):
        def boom():
            raise carriers.CarrierError("terminal_handle_stale")
        self.carrier.on_send = boom
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertIn("terminal_handle_stale", out)
        self.assertEqual(self.deliveries("result")[-1]["ok"], False)
        self.get.side_effect = GET  # the real one: the seat was launched on manual
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{SESSION}", session=SESSION,
                        carrier="manual")
        code, out = self.say()
        self.assertEqual(code, 1)
        self.assertNotIn("请把下面的消息粘贴", out)
        r = self.deliveries("result")[-1]
        self.assertEqual((r["ok"], "manual" in r["error"]), (False, True))

    def test_without_direct_nothing_changes(self):
        self.assertEqual(self.say("hi", direct=False), (0, f"-> {SESSION}\n"))
        self.assertEqual([m.text for m in inbox.pending_messages(SESSION, root=self.root)], ["hi"])
        self.assertEqual((self.deliveries(), self.get.called), ([], False))


class SeatPromptKindsTest(HookBase):
    def prompt(self, text):
        self.assertIsNone(self.run_hook("UserPromptSubmit", {**self.data("UserPromptSubmit", self.root),
                                                             "prompt": text}))

    def kinds(self):
        return [(e["type"], e["sha256"]) for t in ("seat_prompt_controller", "seat_prompt_external") for e in self.events(t)]

    def test_program_controller_external(self):
        log = EventLog(self.state / "events.jsonl")
        log.append("program_delivery", phase="intent", dedupe_id="p", session=SESSION,
                   text_sha256=hooks.delivery_sha("a"))
        log.append("controller_delivery", phase="intent", dedupe_id="c", session=SESSION,
                   text_sha256=hooks.delivery_sha("b"))
        log.append("controller_delivery", phase="intent", dedupe_id="pc", session=SESSION,
                   text_sha256=hooks.delivery_sha("a"))
        log.append("controller_delivery", dedupe_id="x", session=SESSION, text_sha256=hooks.delivery_sha("c"))
        for t in ("a", " b\r\n", "c", "d"):
            self.prompt(t)
        self.assertEqual(self.kinds(), [("seat_prompt_controller", hooks.delivery_sha("b")),
                                        ("seat_prompt_external", hooks.delivery_sha("c")),
                                        ("seat_prompt_external", hooks.delivery_sha("d"))])
        with mock.patch.object(hooks, "DELIVERY_WINDOW", timedelta(0)):  # the same window as program_delivery
            self.prompt("b")
        self.assertEqual(self.kinds()[-1], ("seat_prompt_external", hooks.delivery_sha("b")))

    def test_presence_ignores_controller_deliveries(self):
        EventLog(self.state / "events.jsonl").append("controller_delivery", phase="intent", dedupe_id="c",
                                                     session=SESSION, text_sha256=hooks.delivery_sha("b"))
        self.use_env(self.base_env)  # not a Foremind session
        self.prompt("b")
        self.assertEqual(len(self.events("user_present")), 1)
        self.assertEqual(self.kinds(), [])


if __name__ == "__main__":
    unittest.main()
