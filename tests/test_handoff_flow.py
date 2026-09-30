"""m2a.7: a session handing off gets no deliveries and its inbox goes to the successor; handoff_requested_at; D/F
index only from a verified log; bounded inbox lock; D/F record format; Q-n re-push; idle by heartbeat."""
import builtins
import contextlib
import io
import json
import os
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

from test_hooks import BATCH, SESSION, HookBase, usage_line
from test_supervisor import Base, iso

from foremind import batchlog, cli, fsutil, handoff, heartbeat, hooks, inbox, lock, schemas, telemetry
from foremind.commands import log as log_cmd
from foremind.fsutil import LockBusy

S2 = "fm-shop-auth_2-2"


class StopHookTest(HookBase):
    def stop(self):
        d = self.data("Stop")
        d["stop_hook_active"] = False
        out = hooks.main("Stop", json.dumps(d))
        return json.loads(out) if out else None

    def test_handing_off_session_takes_nothing_from_its_inbox(self):
        inbox.append(SESSION, "for the successor", sender="controller", root=self.root)
        self.transcript.write_text(usage_line("msg_1", 170_000) + "\n")
        out = self.stop()
        self.assertIn("硬阈值", out["reason"])
        self.assertNotIn("for the successor", out["reason"])
        at = self.hb()["handoff_requested_at"]
        self.assertLessEqual(abs(datetime.fromisoformat(at).timestamp() - time.time()), 5)
        self.transcript.write_text(usage_line("msg_2", 10) + "\n")  # below every threshold: still handing off
        self.assertIsNone(self.stop())
        self.assertEqual(len(inbox.pending_messages(SESSION, root=self.root)), 1)
        self.transcript.write_text(usage_line("msg_3", 170_000) + "\n")
        with mock.patch.object(hooks, "datetime") as dt:  # a later hard stop keeps the first request time
            dt.now.return_value = datetime(2030, 1, 1, tzinfo=timezone.utc)
            self.stop()
        self.assertEqual(self.hb()["handoff_requested_at"], at)

    def test_busy_inbox_lock_skips_delivery_this_turn(self):
        inbox.append(SESSION, "later", sender="user", root=self.root)
        with mock.patch.object(hooks, "INBOX_WAIT_S", 0.2), inbox.locked(SESSION, root=self.root):
            t = time.monotonic()
            self.assertIsNone(self.stop())
            self.assertLess(time.monotonic() - t, 2)
        self.assertIn("later", self.stop()["reason"])
        self.assertEqual(self.events("hook_error"), [])

    def test_transcript_that_breaks_context_usage_does_not_stop_delivery(self):
        self.assertRaises(TypeError, lambda: telemetry.context_usage(self._bad_transcript()))  # item 6: it can raise
        inbox.append(SESSION, "still here", sender="user", root=self.root)
        self.assertIn("still here", self.stop()["reason"])
        self.assertTrue(any("TypeError" in e["error"] for e in self.events("hook_error")))

    def _bad_transcript(self):
        rec = json.loads(usage_line("x", 1000))
        rec["message"]["id"] = ["unhashable"]
        self.transcript.write_text(json.dumps(rec) + "\n")
        return str(self.transcript)


class L1AndDenyTest(HookBase):
    def test_rewritten_log_gives_no_df_index(self):
        batchlog.append(self.root, BATCH, f"- {BATCH}.D1 · a · b · c · #1 · p", author=SESSION)
        ctx = self.run_hook("SessionStart", self.data("SessionStart"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"{BATCH}.D1", ctx)
        path = self.state / "batches" / f"{BATCH}.log.md"
        path.write_text(path.read_text().replace("· a ·", "· FORGED ·"))
        ctx = self.run_hook("SessionStart", self.data("SessionStart"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("D/F 索引未注入", ctx)
        self.assertNotIn("FORGED", ctx)

    def test_a_rewrite_after_verify_gives_no_df_index(self):  # m2b.8 r3: between verify() and the read
        batchlog.append(self.root, BATCH, f"- {BATCH}.D1 · a · b · c · #1 · p", author=SESSION)
        path = self.state / "batches" / f"{BATCH}.log.md"
        path.write_text(path.read_text().replace("· a ·", "· FORGED ·"))
        with mock.patch.object(batchlog, "verify", return_value=True):  # it read the log before the rewrite
            ctx = self.run_hook("SessionStart", self.data("SessionStart"))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("D/F 索引未注入", ctx)
        self.assertNotIn("FORGED", ctx)

    def test_deny_stands_when_the_decision_layer_cannot_be_imported(self):  # I38
        real = builtins.__import__

        def imp(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "foremind.decide" and "pending" in (fromlist or ()):
                raise ImportError("broken decision layer")
            return real(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", imp):
            out = self.write(self.wt / "package.json")
        self.assert_denied(out, "硬拦截", "登记待决失败")
        self.assertTrue(any("broken decision layer" in e["error"] for e in self.events("hook_error")))


class InboxTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".foremind").mkdir()

    def test_bounded_lock(self):
        held, go = threading.Event(), threading.Event()

        def holder():
            with inbox.locked("a", root=self.root):
                held.set()
                go.wait(5)

        th = threading.Thread(target=holder)
        th.start()
        held.wait(5)
        t = time.monotonic()
        with self.assertRaises(LockBusy):
            with inbox.locked("a", root=self.root, timeout=0.2):
                pass
        self.assertGreaterEqual(time.monotonic() - t, 0.2)
        threading.Timer(0.1, go.set).start()  # released while we retry: taken
        with inbox.locked("a", root=self.root, timeout=3):
            pass
        th.join()
        with self.assertRaises(ValueError):  # the body's errors are not mistaken for a busy lock
            with inbox.locked("a", root=self.root, timeout=1):
                raise ValueError

    def test_forward_keeps_id_and_sender_and_never_twice(self):
        inbox.append("a", "seen", sender="user", root=self.root)
        inbox.mark_delivered("a", inbox.pending_messages("a", root=self.root)[-1].end, root=self.root)
        ids = [inbox.append("a", t, sender=s, root=self.root) for t, s in (("one", "controller"), ("two", "sv#1"))]
        inbox.append("b", "own", sender="user", root=self.root)
        with mock.patch.object(inbox, "mark_delivered", side_effect=OSError("crash")):
            self.assertRaises(OSError, inbox.forward, "a", "b", root=self.root)
        self.assertEqual(inbox.forward("a", "b", root=self.root), [], "already there after the crash")
        got = inbox.pending_messages("b", root=self.root)
        self.assertEqual([(m.id, m.sender, m.text) for m in got[1:]],
                         [(ids[0], "controller", "one"), (ids[1], "sv#1", "two")])
        self.assertEqual(inbox.pending_messages("a", root=self.root), [])


class AcceptForwardTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path

        from foremind import header
        from foremind.events import EventLog
        from foremind.schemas import EXAMPLES
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".foremind" / "batches").mkdir(parents=True)
        (self.root / ".foremind" / "batches" / "auth.2.md").write_text(
            header.render({**EXAMPLES["batch_header"], "state": "running"}, ""))
        lock.acquire(self.root, "auth.2", SESSION)
        self.log = EventLog(self.root / ".foremind" / "events.jsonl")
        self.log.append("seat_opened", batch="auth.2", session=S2, successor=True, predecessor=SESSION)

    def fwd(self):
        return [e for e in self.log.iter() if e["type"] == "inbox_forwarded" and e["phase"] == "result"]

    def test_accept_forwards_the_predecessors_inbox_once(self):
        mid = inbox.append(SESSION, "must-fix list", sender="supervisor#abc", root=self.root)
        with mock.patch("foremind.batchlog.append", side_effect=OSError("disk full")):
            self.assertRaises(OSError, handoff.accept, self.root, "auth.2", S2)  # forwarded, not logged
        self.assertEqual(handoff.accept(self.root, "auth.2", S2), SESSION)
        self.assertEqual(handoff.accept(self.root, "auth.2", S2), SESSION)
        got = inbox.pending_messages(S2, root=self.root)
        self.assertEqual([(m.id, m.sender, m.text) for m in got], [(mid, "supervisor#abc", "must-fix list")])
        self.assertEqual(inbox.pending_messages(SESSION, root=self.root), [])
        self.assertEqual([(e["frm"], e["to"], e["ids"]) for e in self.fwd()], [(SESSION, S2, [mid])])

    def test_nothing_to_forward_writes_no_event(self):
        handoff.accept(self.root, "auth.2", S2)
        self.assertEqual(self.fwd(), [])

    def test_a_broken_inbox_is_recorded_and_the_take_over_goes_on(self):  # m2a.7 r1 should_fix
        inbox.append(SESSION, "lost?", sender="user", root=self.root)
        (self.root / ".foremind" / "inbox" / f"{SESSION}.md").write_bytes(b"garbage\n")
        self.assertEqual(handoff.accept(self.root, "auth.2", S2), SESSION)
        self.assertEqual(lock.holder(self.root, "auth.2"), S2)
        [e] = [e for e in self.log.iter() if e["type"] == "inbox_forward_failed"]
        self.assertEqual((e["batch"], e["frm"], e["to"]), ("auth.2", SESSION, S2))
        self.assertIn("InboxCorrupt", e["error"])

    def test_a_crash_between_intent_and_result_is_finished_by_a_re_run(self):
        mid = inbox.append(SESSION, "must-fix list", sender="user", root=self.root)
        real = inbox.forward

        def crash(*a, **kw):
            real(*a, **kw)
            raise KeyboardInterrupt  # the process dies right after forwarding

        with mock.patch.object(inbox, "forward", crash):
            self.assertRaises(KeyboardInterrupt, handoff.accept, self.root, "auth.2", S2)
        [intent] = [e for e in self.log.iter() if e["type"] == "inbox_forwarded"]
        self.assertEqual((intent["phase"], intent["ids"]), ("intent", [mid]))
        handoff.accept(self.root, "auth.2", S2)
        self.assertEqual([(e["frm"], e["to"], e["ids"]) for e in self.fwd()], [(SESSION, S2, [mid])])
        self.assertEqual([m.id for m in inbox.pending_messages(S2, root=self.root)], [mid])

    def test_a_lock_broken_without_stuck_forwards_nothing(self):
        self.log.append("batch_state", batch="auth.2", to="stuck", session="fm-shop-auth_2-0")  # an old stuck
        self.log.append("handoff_accept", phase="result", batch="auth.2", session=SESSION)
        inbox.append(SESSION, "left behind", sender="user", root=self.root)
        lock.release(self.root, "auth.2", SESSION)  # e.g. release_idle
        self.log.append("seat_opened", batch="auth.2", session="fm-shop-auth_2-3", successor=True, predecessor=None)
        handoff.accept(self.root, "auth.2", "fm-shop-auth_2-3")
        self.assertEqual(self.fwd(), [])

    def test_after_a_stuck_take_over_the_stuck_sessions_inbox_is_forwarded(self):
        mid = inbox.append(SESSION, "reminder", sender="supervisor#s", root=self.root)
        self.log.append("batch_state", batch="auth.2", to="stuck", session=SESSION)
        lock.release(self.root, "auth.2", SESSION)
        self.log.append("seat_opened", batch="auth.2", session="fm-shop-auth_2-3", successor=True, predecessor=None)
        self.assertIsNone(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-3"))
        self.assertEqual([m.id for m in inbox.pending_messages("fm-shop-auth_2-3", root=self.root)], [mid])
        self.assertEqual([e["frm"] for e in self.fwd()], [SESSION])

    def test_unaccepted_successors_inboxes_are_forwarded_too(self):  # m2b.8 r3
        m1 = inbox.append(SESSION, "before the handoff", sender="user", root=self.root)
        m2 = inbox.append(S2, "said to the successor", sender="say:user", root=self.root)
        self.log.append("batch_state", batch="auth.2", to="stuck", session=S2)  # never accepted; SESSION holds on
        s3 = "fm-shop-auth_2-3"
        self.log.append("seat_opened", batch="auth.2", session=s3, successor=True, predecessor=SESSION)
        self.assertEqual(handoff.accept(self.root, "auth.2", s3), SESSION)
        self.assertEqual([m.id for m in inbox.pending_messages(s3, root=self.root)], [m1, m2])
        self.assertEqual([(e["frm"], e["ids"]) for e in self.fwd()], [(SESSION, [m1]), (S2, [m2])])

    def test_after_a_lock_break_the_stuck_source_is_the_session_it_took(self):  # m2b.8 r1
        mid = inbox.append(SESSION, "must-fix list", sender="supervisor#r1", root=self.root)
        self.log.append("batch_state", batch="auth.2", to="stuck", session=SESSION)
        lock.break_lock(self.root, "auth.2", lock.ExitEvidence(SESSION, "fake", "absent"))
        s3, s4 = "fm-shop-auth_2-3", "fm-shop-auth_2-4"
        self.log.append("seat_opened", batch="auth.2", session=s3, successor=True, predecessor=None)
        self.log.append("batch_state", batch="auth.2", to="stuck", session=s3)  # a later stuck, not the break's
        left = inbox.append(s3, "said to the later stuck one", sender="say:user", root=self.root)
        self.log.append("seat_opened", batch="auth.2", session=s4, successor=True, predecessor=None)
        self.assertIsNone(handoff.accept(self.root, "auth.2", s4))
        self.assertEqual([m.id for m in inbox.pending_messages(s4, root=self.root)], [mid])
        self.assertEqual([e["frm"] for e in self.fwd()], [SESSION])
        self.assertEqual([m.id for m in inbox.pending_messages(s3, root=self.root)], [left])  # REQ-14: not a source


class SayTest(unittest.TestCase):
    setUp = AcceptForwardTest.setUp

    def say(self, text, **env):
        keep = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_")}
        with mock.patch.dict(os.environ, {**keep, "FOREMIND_PROJECT": str(self.root), **env}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["say", "auth.2", text])
        return code, err.getvalue()

    def got(self, session):
        return [(m.sender, m.text) for m in inbox.pending_messages(session, root=self.root)]

    def test_recipient(self):
        self.assertEqual(self.say("to the holder")[0], 0)  # S2 was opened for SESSION, but SESSION is not handing off
        self.assertEqual(self.got(SESSION), [("say:user", "to the holder")])
        hb = heartbeat.update(self.root, SESSION, handoff_requested=True)
        self.assertTrue(hb["handoff_requested"])
        self.say("to the successor", FOREMIND_SESSION="fm-shop-controller-1", FOREMIND_ROLE="controller")
        self.assertEqual(self.got(S2), [("say:controller", "to the successor")])
        lock.release(self.root, "auth.2", SESSION)
        lock.acquire(self.root, "auth.2", lock.USER)
        code, err = self.say("nobody")
        self.assertEqual(code, 1)
        self.assertIn("held by the user", err)

    def test_no_session_at_all(self):
        lock.release(self.root, "auth.2", SESSION)  # S2 was opened for SESSION, not for a broken lock
        code, err = self.say("nobody")
        self.assertEqual(code, 1)
        self.assertIn("no session holds it", err)

    def test_a_successor_that_accepted_and_left_is_no_recipient(self):
        lock.release(self.root, "auth.2", SESSION)
        c = "fm-shop-auth_2-3"
        self.log.append("seat_opened", batch="auth.2", session=c, successor=True, predecessor=None)
        self.assertEqual(self.say("to the new successor")[0], 0)
        self.assertEqual(self.got(c), [("say:user", "to the new successor")])
        self.log.append("handoff_accept", phase="result", batch="auth.2", session=c)  # took the lock, released since
        self.assertEqual(self.say("lost")[0], 1)
        self.log.append("seat_opened", batch="auth.2", session="fm-shop-auth_2-4", successor=True, predecessor=None)
        self.log.append("sv_close", session="fm-shop-auth_2-4", carrier="orca", confirmed=True)  # gone unaccepted
        self.assertEqual(self.say("lost")[0], 1)


class LogFormatTest(unittest.TestCase):
    def test_records_are_checked(self):
        b = "m2a.7"
        good = [f"{b}.D1 · 选 A · 不选 B · 因为 C · #1 · main:x.py:3", f"- {b}.D2 · a · b · c · 授权表类别 #8 · p",
                f"{b}.F1 · 做了 → 看到 → 判断 → main:y.py", "正文提到 m2a.7.D1 不算记录", f"{b}.Dx 不是编号",
                f"m2a.70.D1 · 别的批次"]
        self.assertEqual(log_cmd.bad_records("\n".join(good), b), [])
        bad = [f"{b}.D3 · a · b · c · p", f"{b}.D4 · a · b · c · 1 · p", f"{b}.D5 · a ·  · c · #1 · p",
               f"{b}.D6 · a · b · c · #1 · p · q", f"{b}.F2 · 做了 → 看到 → 判断", f"* {b}.F3 做了 → a → b → c"]
        self.assertEqual(log_cmd.bad_records("\n".join(good + bad), b), bad)

    def test_cli_rejects_and_shows_the_format(self):
        with mock.patch("sys.stdin", io.StringIO("m2a.7.D1 · 缺段\n")), \
                contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.object(batchlog, "append") as app:
            self.assertEqual(cli.main(["log", "m2a.7"]), 1)
        app.assert_not_called()
        self.assertIn("m2a.7.D<n> · 选了什么 · 没选什么 · 为什么 · #k · 证据指针", err.getvalue())
        self.assertIn("m2a.7.D1 · 缺段", err.getvalue())


class SupervisorTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")

    def test_no_delivery_to_a_session_handing_off(self):
        self.hold("p.1", "fm-s", handoff_requested=True, handoff_requested_at=iso(self.now + 3600))
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        self.tick()
        self.assertEqual(self.carrier.sent, [])
        self.assertEqual(len(self.pending("fm-s")), 1)

    def test_heartbeat_stop_counts_as_idle(self):  # finding 18
        self.hold("p.1", "fm-s", event="Stop")
        self.carrier.idle = False  # the screen check missed it
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        self.tick()
        self.assertEqual(len(self.carrier.sent), 1)
        self.beat("fm-s", "p.1", self.now, event="PreToolUse")
        inbox.append("fm-s", "again", sender="user", root=self.root)
        self.tick(30)
        self.assertEqual(len(self.carrier.sent), 1, "mid-turn: left to the Stop hook")

    def test_only_a_handoff_written_after_the_request_opens_a_successor(self):
        self.log.append("handoff_written", batch="p.1", session="fm-s")  # an earlier, voluntary one
        self.hold("p.1", "fm-s", handoff_requested=True, handoff_requested_at=iso(time.time() + 3600))
        self.tick(60)
        self.assertEqual(self.jobs.cmds(), [])
        self.beat("fm-s", "p.1", self.now, handoff_requested=True, handoff_requested_at=iso(time.time() - 60))
        self.tick(90)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_unpushed_open_decision_is_pushed_once_after_the_job_limit(self):
        self.decision("Q-1", ["p.2"])
        self.log.append("pending_created", question="Q-1", request={"kind": "hit", "approve_option": 1})
        p = self.root / ".foremind" / "decisions" / "Q-1.json"
        p.write_text(json.dumps({**schemas.EXAMPLES["pending"], **json.loads(p.read_text()),
                                 "options": ["批准", "不批"], "code": "ABC123"}))
        self.tick(0)
        self.assertEqual(self.rec.sent, [], "its own push job may still be running")
        self.tick(120)
        self.tick(150)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["待决 Q-1"])
        self.assertIn("ABC123", self.rec.sent[0][1])


if __name__ == "__main__":
    unittest.main()
