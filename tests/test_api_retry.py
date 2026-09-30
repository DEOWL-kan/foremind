"""m2c.2 REQ-3 (finding 29): a seat stopped on a retryable API error is told to go on, with backoff and a cap.

The records copy the shape Claude Code 2.1.280 wrote on 2026-09-27 07:13:59Z (m2b.10's transcript, ECONNRESET) and
the rate_limit / authentication_failed ones seen on this machine (m2c.2.D1)."""
import json
import os
from datetime import datetime, timezone
from unittest import mock

from test_supervisor import Base, iso

from foremind import hooks, inbox, lock, telemetry
from foremind.supervisor.tick import LOW_TEXT

M = 60
ECONNRESET = "API Error: Connection dropped (ECONNRESET)"


def z(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def reply(uid, t, tokens=1000):
    return {"type": "assistant", "isSidechain": False, "uuid": uid, "timestamp": z(t),
            "message": {"id": f"msg_{uid}", "model": "claude-opus-5-5", "role": "assistant",
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": tokens, "output_tokens": 5, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0}}}


def api_error(uid, t, text=ECONNRESET, error="server_error", **extra):
    return {"parentUuid": "p", "isSidechain": False, "type": "assistant", "uuid": uid, "timestamp": z(t),
            "error": error, "isApiErrorMessage": True, "perTurnEffort": "xhigh", "version": "2.1.280", **extra,
            "message": {"id": f"synthetic-{uid}", "model": "<synthetic>", "role": "assistant",
                        "stop_reason": "stop_sequence", "type": "message",
                        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0},
                        "content": [{"type": "text", "text": text}]}}


def user(uid, t, text="收件箱新消息"):
    return {"type": "user", "isSidechain": False, "uuid": uid, "timestamp": z(t),
            "message": {"role": "user", "content": text}}


def after_error(t):  # what 2.1.280 writes right after the error: not the main chain
    return {"type": "system", "subtype": "turn_duration", "timestamp": z(t)}


class LastApiErrorTest(Base):
    def transcript(self, *records):
        p = self.tmp / "t.jsonl"
        p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        return str(p)

    def test_reads_the_error_the_main_chain_ends_on(self):
        t = self.now
        p = self.transcript(reply("a1", t), api_error("e1", t + 1), after_error(t + 1),
                            {**reply("side", t + 2), "isSidechain": True})
        for chunk in (7, 1 << 16):  # backwards across chunk borders
            e = telemetry.last_api_error(p, chunk=chunk)
            self.assertEqual((e["id"], e["text"], e["retryable"], e["run"]), ("e1", ECONNRESET, True, ["e1"]))
            self.assertAlmostEqual(e["at"], t + 1, delta=0.01)
        self.assertEqual(telemetry.last_context(p), 1000, "the synthetic record is still no context reading")
        # a retry that failed again: one run of two errors, until a good reply
        p = self.transcript(reply("a1", t), api_error("e1", t + 1), user("u1", t + 2), api_error("e2", t + 3))
        self.assertEqual(telemetry.last_api_error(p)["run"], ["e2", "e1"])
        p = self.transcript(api_error("e1", t), reply("a2", t + 1), api_error("e2", t + 2))
        self.assertEqual(telemetry.last_api_error(p)["run"], ["e2"])
        for gone_on in (user("u1", t + 2), reply("a2", t + 2)):
            self.assertIsNone(telemetry.last_api_error(self.transcript(api_error("e1", t + 1), gone_on)))
        self.assertIsNone(telemetry.last_api_error(None))
        self.assertIsNone(telemetry.last_api_error(str(self.tmp / "missing.jsonl")))

    def test_retryable_by_field_then_status_then_text(self):
        cases = [
            (api_error("e", 0, "API Error: Connection refused — a firewall or proxy may be blocking it"), True),
            (api_error("e", 0, "API Error: Your computer went to sleep mid-response."), True),
            (api_error("e", 0, "overloaded", error="overloaded"), True),
            (api_error("e", 0, "You've hit your session limit · resets 2:40pm (Asia/Shanghai)", error="rate_limit",
                       apiErrorStatus=429), False),
            (api_error("e", 0, "Failed to authenticate. API Error: 403 Request not allowed",
                       error="authentication_failed", apiErrorStatus=403), False),
            (api_error("e", 0, "Not logged in · Please run /login", error="authentication_failed"), False),
            (api_error("e", 0, "Prompt is too long", error="invalid_request"), False),
            (api_error("e", 0, "API Error: 529", error=None, apiErrorStatus=529), True),
            (api_error("e", 0, "API Error: 400 bad", error=None, apiErrorStatus=400), False),
            (api_error("e", 0, "API Error: Request timed out.", error=None), True),
            (api_error("e", 0, "API Error: 500 Internal server error", error="brand_new"), True),
            (api_error("e", 0, "Claude AI usage limit reached", error=None), False),
            (api_error("e", 0, "something else", error=None), False),
            (api_error("e", 0, "API Error: connect ECONNREFUSED 127.0.0.1:443", error=None), True),  # a port
            (api_error("e", 0, "API Error: 503 upstream", error={"type": "new_shape"}), True),  # r1 #3: no TypeError
            (api_error("e", 0, "API Error: 401", error=["x"]), False),
        ]
        for rec, want in cases:
            with self.subTest(text=rec["message"]["content"][0]["text"], error=rec["error"]):
                self.assertIs(telemetry.last_api_error(self.transcript(rec))["retryable"], want)


class ApiRetryTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.tp = self.tmp / "t.jsonl"
        self.write(reply("a1", self.now - 60), user("u0", self.now - 30), api_error("e1", self.now))

    def write(self, *records):
        self.tp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

    def hook(self, event, **data):
        env = {"FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-s", "FOREMIND_BATCH": "p.1",
               "FOREMIND_ROLE": "seat"}
        with mock.patch.dict(os.environ, env):
            hooks.main(event, json.dumps({"cwd": str(self.root), "session_id": "sid", **data}))

    def retries(self):
        return [e["text"] for e in self.events("sv_say", "intent") if ":apiretry:" in e["dedupe_id"]]

    def test_retried_with_backoff_then_the_waiting_notice_names_it(self):
        self.hold("p.1", "fm-s")
        self.hook("Stop", transcript_path=str(self.tp), stop_hook_active=False)  # records transcript_path
        self.tick(1 * M)  # retry 1 is due 2 min after the error
        self.assertEqual(self.retries(), [])
        self.tick(2 * M + 1)
        [first] = self.retries()
        self.assertIn(ECONNRESET, first)
        self.assertIn("第 1 次", first)
        self.assertIn(first, self.carrier.sent[-1][1], "delivered by tick.deliver through the carrier")
        self.tick(21 * M)  # retry 2: 4 min after retry 1; no stuck reminder while retrying
        self.tick(22 * M)  # retry 3 is due 8 min after retry 2
        self.assertEqual(len(self.retries()), 2)
        self.assertFalse([t for s, t in self.carrier.sent if "没有看到你的心跳" in t])
        self.tick(29 * M + 1)
        self.assertEqual(len(self.retries()), 3)
        w = {"at": iso(self.now + 30 * M), "kind": "idle_prompt", "message_sha256": "0" * 64}
        self.beat("fm-s", "p.1", self.now, event="Stop", transcript_path=str(self.tp), waiting_input=w)
        self.tick(40 * M)  # used up: the waiting notice, with the error
        self.assertEqual(len(self.retries()), 3)
        [(title, body, _)] = self.rec.sent
        self.assertEqual(title, "p.1 在终端里等你回答")
        self.assertIn(f"最后一条是 API 错误：{ECONNRESET}", body)
        self.assertEqual(self.header("p.1")["state"], "running")

    def test_a_retry_that_fails_again_counts_toward_the_same_cap(self):
        self.hold("p.1", "fm-s", event="Stop", transcript_path=str(self.tp))
        self.tick(2 * M)
        self.write(api_error("e1", self.now), user("r1", self.now + 2 * M + 5), api_error("e2", self.now + 3 * M))
        self.tick(5 * M)  # retry 2 is due 4 min after retry 1, whatever the new error's time
        self.assertEqual(len(self.retries()), 1)
        self.tick(6 * M)
        self.assertIn("第 2 次", self.retries()[-1])
        self.write(api_error("e2", self.now), reply("a2", self.now + 7 * M), api_error("e3", self.now + 8 * M))
        self.tick(10 * M)  # a good reply in between: a new run, from retry 1
        self.assertIn("第 1 次", self.retries()[-1])

    def test_a_batch_waiting_on_a_decision_still_gets_them(self):
        """r2 #1: a Q-n blocking the batch held back the retries and the notice until it was answered. (Another
        batch not waiting: no full block, see below.)"""
        self.plan("o")
        self.set_state("o.1", "in_review")
        self.decision("Q-1", ["p.1"])
        w = {"at": iso(self.now), "kind": "idle_prompt", "message_sha256": "0" * 64}
        self.hold("p.1", "fm-s", event="Stop", transcript_path=str(self.tp), waiting_input=w)
        for at in (2 * M, 6 * M, 14 * M):
            self.tick(at)
        self.assertEqual(len(self.retries()), 3)
        self.assertIn(self.retries()[-1], self.carrier.sent[-1][1])
        self.assertEqual(self.rec.sent, [])
        self.tick(40 * M)  # used up: the notice; still no stuck stage
        self.assertIn(f"最后一条是 API 错误：{ECONNRESET}", self.rec.sent[-1][1])
        self.write(reply("a1", self.now))  # no error: as before, nothing while the decision is open
        self.beat("fm-s", "p.1", self.now, event="Stop", transcript_path=str(self.tp),
                  waiting_input={**w, "at": iso(self.now + 1)})
        self.tick(90 * M)
        self.assertEqual(len(self.rec.sent), 1)
        self.assertFalse([t for s, t in self.carrier.sent if "没有看到你的心跳" in t])
        self.assertEqual(self.header("p.1")["state"], "running")

    def test_none_that_deliver_would_not_send(self):
        """r1 #1: a retry left in the inbox would hold back the waiting notice and the stuck stages for good."""
        self.plan("w", {"mode": "watch"})
        cases = [("p.1", {"tool_open": True, "open_tools": ["t1"]}, True), ("w.1", {}, True),
                 ("p.1", {}, False)]  # idle by waiting_input only: an API error ends the turn with no Stop
        for i, (bid, beat, idle) in enumerate(cases):
            with self.subTest(bid=bid, beat=beat, idle=idle):
                s = f"fm-{bid}"
                self.carrier.idle = idle
                w = {"at": iso(self.now + i), "kind": "idle_prompt", "message_sha256": "0" * 64}
                self.hold(bid, s, event="PostToolUse", transcript_path=str(self.tp), waiting_input=w, **beat)
                self.tick(3 * M + i)
                self.assertEqual(self.retries(), [])
                self.assertEqual(len(self.rec.sent), i + 1, self.rec.sent)
                self.assertIn(f"会话 {s}（", self.rec.sent[-1][1])
                self.assertIn(f"最后一条是 API 错误：{ECONNRESET}", self.rec.sent[-1][1])
                lock.release(self.root, bid, s)

    def test_a_session_handing_off_gets_its_retries_and_nothing_else(self):
        """Q-25: REQ-3 covers a holder in its handoff window; the rest of its inbox stays for the successor (§6.4)."""
        handing_off = {"event": "PostToolUse", "transcript_path": str(self.tp), "handoff_requested": True,
                       "handoff_requested_at": iso(self.now - 60)}
        self.hold("p.1", "fm-s", **handing_off)
        self.tick(2 * M + 1)
        [first] = self.retries()
        self.assertEqual(self.carrier.sent, [("fm-s", f"收件箱新消息（按顺序处理）：\n\n［{self.sender(0)}］{first}")])
        inbox.append("fm-s", "审查回执：改 x", sender="supervisor#abc", root=self.root)  # held for the successor
        w = {"at": iso(self.now + 5 * M), "kind": "idle_prompt", "message_sha256": "0" * 64}
        self.beat("fm-s", "p.1", self.now, waiting_input=w, **handing_off)
        self.tick(7 * M)  # retry 2 is due, but would sit behind the held message: the waiting notice instead
        self.assertEqual((len(self.retries()), len(self.carrier.sent)), (1, 1))
        self.assertEqual([m.text for m in inbox.pending_messages("fm-s", root=self.root)], ["审查回执：改 x"])
        self.assertIn(f"最后一条是 API 错误：{ECONNRESET}", self.rec.sent[-1][1])

    def test_a_session_handing_off_that_went_on_keeps_an_unsent_retry_for_the_successor(self):
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp), handoff_requested=True)
        with mock.patch.object(self.carrier, "send", side_effect=OSError("down")):
            self.tick(2 * M + 1)
        self.assertEqual(len(self.retries()), 1)
        self.write(api_error("e1", self.now), reply("a2", self.now + 3 * M))  # went on by hand
        self.tick(4 * M)
        self.assertEqual(self.carrier.sent, [])
        self.assertEqual(len(inbox.pending_messages("fm-s", root=self.root)), 1)

    def test_a_retry_left_in_the_inbox_is_not_sent_after_the_section(self):
        """r3: the successor verifies against the section, so nothing tells the predecessor to go on after it."""
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp), handoff_requested=True,
                  handoff_requested_at=iso(self.now - 60))
        with mock.patch.object(self.carrier, "send", side_effect=OSError("down")):
            self.tick(2 * M + 1)
        self.log.append("handoff_written", batch="p.1", session="fm-s")
        self.tick(3 * M)
        self.assertEqual((len(self.retries()), self.carrier.sent), (1, []))

    def test_no_retry_for_a_blocked_batch_after_the_section(self):
        self.plan("o")
        self.set_state("o.1", "in_review")
        self.decision("Q-1", ["p.1"])
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp), handoff_requested=True,
                  handoff_requested_at=iso(self.now - 60))
        self.log.append("handoff_written", batch="p.1", session="fm-s")
        self.tick(3 * M)
        self.assertEqual((self.retries(), self.carrier.sent), ([], []))

    def test_a_full_block_still_gets_the_retries_and_the_notice(self):
        """r3: REQ-3 leaves out no full block; resuming a holder's turn is no new model start (§10.2). r4: used up,
        the waiting notice once, as for a batch waiting on a decision (r2)."""
        self.decision("Q-1", ["p.1"])  # the only batch: every unfinished batch waits on the user
        w = {"at": iso(self.now), "kind": "idle_prompt", "message_sha256": "0" * 64}
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp), waiting_input=w)
        for at in (2 * M, 6 * M, 14 * M):
            self.tick(at)
        self.assertEqual([r in t for r, (_, t) in zip(self.retries(), self.carrier.sent)], [True] * 3)
        inbox.append("fm-s", "总控：改 x", sender="controller", root=self.root)
        self.tick(40 * M)  # used up: the notice; no stuck stage, and with no retry to go with it no delivery
        self.tick(41 * M)
        self.assertEqual(len(self.carrier.sent), 3)
        [body] = [b for t, b, _ in self.rec.sent if t == "p.1 在终端里等你回答"]
        self.assertIn(f"最后一条是 API 错误：{ECONNRESET}", body)
        self.assertEqual(self.header("p.1")["state"], "running")

    def test_a_full_block_retry_takes_the_pending_inbox_along(self):
        """r4: a message waiting before it (LOW_TEXT, a decide answer, foremind say) held the retry back until the
        user answered."""
        self.decision("Q-1", ["p.1"])
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp))
        inbox.append("fm-s", LOW_TEXT, sender="supervisor#low", root=self.root)
        self.tick(1 * M)  # no retry due yet: the inbox waits for the block to lift
        self.assertEqual(self.carrier.sent, [])
        self.tick(2 * M + 1)
        [first] = self.retries()
        [(_, text)] = self.carrier.sent
        self.assertLess(text.index(LOW_TEXT), text.index(first))
        self.assertEqual(inbox.pending_messages("fm-s", root=self.root), [])

    def sender(self, i):
        return f"supervisor#{self.events('sv_say', 'intent')[i]['token']}"

    def test_an_open_tool_is_closed_first(self):
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp), tool_open=True, open_tools=["t1"])
        self.tick(21 * M)  # closed stuck.remind_min after its heartbeat, with the reminder
        self.assertEqual(self.retries(), [])
        self.tick(22 * M)
        self.assertIn("第 1 次", self.retries()[-1])
        self.assertIn(self.retries()[-1], self.carrier.sent[-1][1])

    def test_not_retryable_busy_or_no_error(self):
        w = {"at": iso(self.now), "kind": "idle_prompt", "message_sha256": "0" * 64}
        self.write(api_error("e1", self.now, "You've hit your session limit", error="rate_limit"))
        self.hold("p.1", "fm-s", event="Stop", transcript_path=str(self.tp), waiting_input=w)
        self.tick(3 * M)
        self.assertEqual(self.retries(), [])
        self.assertIn("最后一条是 API 错误：You've hit your session limit", self.rec.sent[-1][1])
        self.write(api_error("e2", self.now))  # retryable, but mid-turn with a busy screen
        self.carrier.idle = False
        self.beat("fm-s", "p.1", self.now, event="PostToolUse", transcript_path=str(self.tp))
        self.tick(4 * M)
        self.assertEqual(self.retries(), [])
        self.write(reply("a1", self.now))  # no error: the waiting notice as before
        self.beat("fm-s", "p.1", self.now, event="Stop", transcript_path=str(self.tp),
                  waiting_input={**w, "at": iso(self.now + 1)})
        self.tick(5 * M)
        self.assertEqual(len(self.rec.sent), 2)
        self.assertEqual(self.rec.sent[-1][1], "会话 fm-s（idle_prompt）停在终端里等回答，回到它的终端处理。")

    # m2d.6 REQ-11: the heartbeat's api_error (StopFailure) later than the last good reply is classified by its type;
    # REQ-17: each run of errors ends in one api_retry_result

    def stop_failure(self, typ, at, **extra):
        self.beat("fm-s", "p.1", self.now, event="PostToolUse", transcript_path=str(self.tp),
                  api_error={"type": typ, "at": iso(self.now + at)}, **extra)

    def results(self):
        return [(e["error_id"], e["tries"], e["ok"]) for e in self.events("api_retry_result")]

    def test_the_type_wins_over_the_text(self):
        self.hold("p.1", "fm-s")
        self.stop_failure("rate_limit", 0)  # the transcript's ECONNRESET reads retryable
        self.tick(3 * M)
        self.assertEqual(self.retries(), [])
        self.write(reply("a1", self.now - 60), api_error("e1", self.now, "something else", error=None))
        self.stop_failure("overloaded", 0)
        self.tick(4 * M)
        self.assertIn("something else", self.retries()[-1], "the transcript's record still names it")
        for typ, want in (("brand_new", False), ("unknown", True)):  # another type: as the transcript says
            with self.subTest(typ=typ):
                self.write(reply("a1", self.now - 60), api_error(typ, self.now + 5 * M, "x", error=None))
                self.stop_failure(typ, 5 * M)
                self.tick(8 * M)
                self.assertEqual(any(f":{typ}:" in e["dedupe_id"] for e in self.events("sv_say", "intent")), want)

    def test_older_than_the_last_good_reply_it_is_ignored(self):
        self.write(reply("a1", self.now - 60), api_error("e1", self.now))
        self.hold("p.1", "fm-s")
        self.stop_failure("rate_limit", -120)  # from an earlier run of errors
        self.tick(3 * M)
        self.assertEqual(len(self.retries()), 1)

    def test_no_record_in_the_transcript(self):
        """The StopFailure alone: id hb:<at>, and a later one is the same run (its at moves on every failure)."""
        self.write(reply("a1", self.now - 60), user("u0", self.now - 30))
        self.hold("p.1", "fm-s")
        self.stop_failure("overloaded", 0)
        self.tick(2 * M + 1)
        [first] = self.retries()
        self.assertIn("StopFailure overloaded", first)
        [said] = self.events("sv_say", "intent")
        self.assertEqual(said["dedupe_id"], f"sv_say:apiretry:fm-s:hb:{iso(self.now)}:1")
        for n, (failed, due) in enumerate(((3, 6), (7, 14)), 2):
            self.stop_failure("server_error", failed * M)
            self.tick(due * M)
            self.assertEqual(len(self.retries()), n - 1, "not before 2^(n-1) × 2 min after the last retry")
            self.tick(due * M + 2)
            self.assertIn(f"第 {n} 次", self.retries()[-1])
        self.stop_failure("server_error", 15 * M)
        self.tick(40 * M)
        self.tick(41 * M)
        self.assertEqual(len(self.retries()), 3)
        self.assertEqual(self.results(), [(f"hb:{iso(self.now)}", 3, False)])
        self.write(reply("a1", self.now - 60), user("u0", self.now - 30), reply("a2", self.now + 42 * M))
        self.tick(43 * M)
        self.assertEqual(len(self.results()), 1, "used up stays the run's result")

    def test_a_good_reply_after_the_retries_ends_the_run(self):
        self.hold("p.1", "fm-s", event="Stop", transcript_path=str(self.tp))
        self.tick(2 * M + 1)
        self.write(reply("a1", self.now - 60), api_error("e1", self.now), user("r1", self.now + 2 * M + 5),
                   api_error("e2", self.now + 3 * M))
        self.tick(4 * M)
        self.assertEqual(self.results(), [])
        self.tick(6 * M + 2)  # retry 2
        self.write(reply("a1", self.now - 60), api_error("e1", self.now), user("r1", self.now + 2 * M + 5),
                   api_error("e2", self.now + 3 * M), user("r2", self.now + 6 * M + 5), reply("a2", self.now + 7 * M))
        self.tick(8 * M)
        self.tick(9 * M)
        self.assertEqual(self.results(), [("e1", 2, True)])
        self.write(reply("a2", self.now + 7 * M), api_error("e3", self.now + 10 * M))  # a new run
        self.tick(12 * M + 1)
        self.write(reply("a2", self.now + 7 * M), api_error("e3", self.now + 10 * M), user("r3", self.now + 12 * M),
                   reply("a3", self.now + 13 * M))
        self.tick(14 * M)
        self.assertEqual(self.results(), [("e1", 2, True), ("e3", 1, True)])

    def test_a_full_block_delivers_a_retry_for_the_stop_failure_alone(self):
        """tick._deliver checks the same classification: the transcript alone would say there is no error."""
        self.decision("Q-1", ["p.1"])
        self.write(reply("a1", self.now - 60), user("u0", self.now - 30))
        self.hold("p.1", "fm-s")
        self.stop_failure("overloaded", 0)
        self.tick(2 * M + 1)
        [first] = self.retries()
        self.assertIn(first, self.carrier.sent[-1][1])

    def test_the_run_ends_when_the_batch_has_moved_on_busy(self):  # r1 must_fix 1
        self.hold("p.1", "fm-s", event="Stop", transcript_path=str(self.tp))
        self.tick(2 * M + 1)
        self.write(reply("a1", self.now - 60), api_error("e1", self.now), user("r1", self.now + 2 * M + 5),
                   reply("a2", self.now + 3 * M))
        self.set_state("p.1", "in_review")  # went on to `foremind review` in the same turn, still busy
        self.carrier.idle = False
        self.beat("fm-s", "p.1", self.now + 3 * M, event="PostToolUse", transcript_path=str(self.tp))
        self.tick(4 * M)
        self.assertEqual(self.results(), [("e1", 1, True)])
