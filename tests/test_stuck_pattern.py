"""m2d.6 REQ-17: a seat going round in circles gets one reminder (stuck.pattern), and a quiet mark that moved after
a remind or ask is recorded (stuck_recovered). The records copy Claude Code's transcript shapes (one record per
content block, tool results in user records)."""
import json

from test_api_retry import z
from test_supervisor import M, Base, iso

from foremind.supervisor import stuck


def prompt(uid, text="收件箱新消息"):
    return {"type": "user", "isSidechain": False, "uuid": uid, "message": {"role": "user", "content": text}}


def text(uid, words="我再想想"):
    return {"type": "assistant", "isSidechain": False, "uuid": uid,
            "message": {"role": "assistant", "content": [{"type": "text", "text": words}]}}


def call(uid, name, **inp):
    return {"type": "assistant", "isSidechain": False, "uuid": uid,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"tu_{uid}", "name": name,
                                                          "input": inp}]}}


def result(uid, of, out, error=False):
    return {"type": "user", "isSidechain": False, "uuid": uid,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu_{of}",
                                                     "content": out, "is_error": error}]}}


def bash_fails(n, cmd="python3 -m unittest", out="Exit code 1\nFAILED (failures=1)", start=0):
    recs = []
    for i in range(start, start + n):
        recs += [call(f"c{i}", "Bash", command=cmd), result(f"r{i}", f"c{i}", out, error=True)]
    return recs


class PatternTest(Base):
    def test_the_same_failing_command(self):
        kind, n, fp = stuck.pattern([prompt("p"), *bash_fails(3)])
        self.assertEqual((kind, n), ("repeat_error", 3))
        self.assertEqual(stuck.pattern(bash_fails(4)), ("repeat_error", 4, fp), "the same fingerprint as it goes on")
        self.assertIsNone(stuck.pattern(bash_fails(3), repeat=4))  # stuck.pattern_repeat
        self.assertIsNone(stuck.pattern(bash_fails(2) + bash_fails(1, out="other error", start=2)))
        self.assertIsNone(stuck.pattern(bash_fails(2) + bash_fails(1, cmd="ls", start=2)))
        ok = [r for i in range(3) for r in (call(f"c{i}", "Bash", command="make"), result(f"r{i}", f"c{i}", "ok"))]
        self.assertIsNone(stuck.pattern(ok), "no error: working, not stuck")
        self.assertNotEqual(stuck.pattern(bash_fails(3, out="other"))[2], fp)

    def test_two_calls_taking_turns(self):
        def calls(n):
            return [r for i in range(n) for r in (call(f"c{i}", "Read", file_path=f"/x/{'ab'[i % 2]}.py"),
                                                   result(f"r{i}", f"c{i}", "…"))]
        kind, rounds, fp = stuck.pattern(calls(6))
        self.assertEqual((kind, rounds), ("alternate", 3))
        self.assertEqual(stuck.pattern(calls(7))[2], fp)
        self.assertIsNone(stuck.pattern(calls(5)))
        # a streak in the middle, after other calls, still counts
        head = [call("x", "Grep", pattern="y"), result("rx", "x", "none")]
        self.assertEqual(stuck.pattern(head + calls(6))[:2], ("alternate", 3))

    def test_turns_of_text_only(self):
        turns = [r for i in range(3) for r in (prompt(f"p{i}"), text(f"t{i}"))]
        kind, n, fp = stuck.pattern(turns)
        self.assertEqual((kind, n), ("text_only", 3))
        self.assertIsNone(stuck.pattern(turns, ended=False), "the last turn may still call a tool")
        self.assertEqual(stuck.pattern(turns + [prompt("p3")], ended=False)[:2], ("text_only", 3))
        with_tool = turns[:4] + [prompt("p2"), text("t2"), call("c", "Bash", command="ls")]
        self.assertIsNone(stuck.pattern(with_tool))
        # an API error is no reply: its prompt and the retry make one turn
        err = {**text("e1", "API Error: overloaded"), "isApiErrorMessage": True}
        self.assertEqual(stuck.pattern(turns[:4] + [prompt("p2"), err, prompt("p2b"), text("t2")])[:2],
                         ("text_only", 3))
        self.assertIsNone(stuck.pattern(turns[1:]), "a turn cut off by the window's start is not counted")

    def test_ordinary_work(self):
        recs = [prompt("p"), text("t"), call("c1", "Read", file_path="/a"), result("r1", "c1", "x"),
                call("c2", "Edit", file_path="/a", old_string="x", new_string="y"), result("r2", "c2", "ok"),
                *bash_fails(1), call("c3", "Edit", file_path="/a", old_string="y", new_string="z"),
                result("r3", "c3", "ok"), call("c4", "Bash", command="python3 -m unittest"), result("r4", "c4", "OK")]
        self.assertIsNone(stuck.pattern(recs))
        self.assertIsNone(stuck.pattern([]))


class PatternTickTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.carrier.idle = False  # keep the supervisor's messages in the inbox
        self.tp = self.tmp / "t.jsonl"
        self.write(prompt("p0"), text("t0"), *bash_fails(3))

    def write(self, *records):
        self.tp.write_text("".join(json.dumps({**r, "timestamp": z(self.now)}, ensure_ascii=False) + "\n"
                                   for r in records))

    def test_one_reminder_per_fingerprint_and_nothing_else(self):
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp))
        self.tick(M)
        [m] = self.pending("fm-s")
        self.assertIn("同一条 Bash 命令连续跑了 3 次", m.text)
        self.assertIn("foremind decide --new", m.text)
        [e] = self.events("stuck_pattern")
        self.assertEqual((e["batch"], e["session"], e["kind"], e["count"]), ("p.1", "fm-s", "repeat_error", 3))
        self.write(prompt("p0"), text("t0"), *bash_fails(4))  # the same loop goes on
        self.tick(2 * M)
        self.assertEqual((len(self.pending("fm-s")), len(self.events("stuck_pattern"))), (1, 1))
        self.assertEqual((self.header("p.1")["state"], self.rec.sent), ("running", []))
        self.write(*bash_fails(3, out="another error"))  # another fingerprint: told again
        self.tick(3 * M)
        self.assertEqual([e["count"] for e in self.events("stuck_pattern")], [3, 3])
        self.assertEqual(len(self.pending("fm-s")), 2)

    def test_a_kind_told_already_does_not_hold_back_the_next(self):  # m2e REQ-9
        self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp))
        self.tick(M)
        turns = [r for i in range(3) for r in (prompt(f"q{i}"), text(f"u{i}"))] + [prompt("q3")]
        self.write(prompt("p0"), text("t0"), *bash_fails(3), *turns)  # repeat_error still in the window
        self.tick(2 * M)
        self.tick(3 * M)
        self.assertEqual([e["kind"] for e in self.events("stuck_pattern")], ["repeat_error", "text_only"])
        self.assertEqual(len(self.pending("fm-s")), 2)

    def test_not_while_waiting_in_the_terminal_handing_off_or_gone(self):
        w = {"at": iso(self.now), "kind": "permission_prompt", "message_sha256": "0" * 64}
        self.hold("p.1", "fm-s", transcript_path=str(self.tp), waiting_input=w)
        self.tick(M)
        self.beat("fm-s", "p.1", self.now, transcript_path=str(self.tp), handoff_requested=True)
        self.tick(2 * M)
        self.assertEqual((self.pending("fm-s"), self.events("stuck_pattern")), ([], []))
        self.beat("fm-s", "p.1", self.now, transcript_path=str(self.tp))
        self.tick(3 * M)
        self.assertEqual(len(self.events("stuck_pattern")), 1)

    def test_pattern_repeat_is_configurable(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n\n[stuck]\npattern_repeat = 4\n')
        self.hold("p.1", "fm-s", transcript_path=str(self.tp))
        self.tick(M)
        self.assertEqual(self.events("stuck_pattern"), [])
        self.write(*bash_fails(4))
        self.tick(2 * M)
        self.assertEqual([e["count"] for e in self.events("stuck_pattern")], [4])

    def test_text_only_is_told_once_as_the_window_slides(self):  # r1 must_fix 2
        recs = [prompt("p0"), call("c0", "Bash", command="ls"), result("r0", "c0", "ok")]
        for i in range(14):  # up to 14 text-only turns: the window (20 records) slides past the run's start
            recs += [prompt(f"q{i}"), text(f"t{i}")]
            self.write(*recs)
            if i == 0:
                self.hold("p.1", "fm-s", event="PostToolUse", transcript_path=str(self.tp))
            self.tick((i + 1) * 10)
        self.assertEqual([e["kind"] for e in self.events("stuck_pattern")], ["text_only"])
        self.assertEqual(len(self.pending("fm-s")), 1)
        recs += [call("c1", "Read", file_path="/a"), result("r1", "c1", "x")]  # went on, then a new run
        recs += [r for i in range(3) for r in (prompt(f"n{i}"), text(f"u{i}"))] + [prompt("n3")]
        self.write(*recs)
        self.tick(300)
        self.assertEqual(len(self.events("stuck_pattern")), 2)

    def test_a_batch_waiting_on_a_decision_is_told_too(self):  # r1 should_fix 4, 5
        self.plan("o")
        self.hold("o.1", "fm-o", transcript_path=str(self.tp))  # the same loop in another session
        self.decision("Q-1", ["p.1"])  # p.1 alone: no full block
        self.hold("p.1", "fm-s", transcript_path=str(self.tp))
        self.tick(M)
        got = {e["session"]: e["fingerprint"] for e in self.events("stuck_pattern")}
        self.assertEqual(set(got), {"fm-s", "fm-o"})
        self.assertNotEqual(got["fm-s"], got["fm-o"], "the fingerprint takes the session in")
        self.decision("Q-2", ["o.1"])  # now a full block: only the API-error retries
        self.write(*bash_fails(3, out="another error"))
        self.tick(2 * M)
        self.assertEqual(len(self.events("stuck_pattern")), 2)


class RecoveredTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.carrier.idle = False

    def test_a_sign_of_life_after_remind_or_ask(self):
        self.hold("p.1", "fm-s")
        self.tick(21 * M)  # remind
        self.tick(22 * M)
        self.assertEqual(self.events("stuck_recovered"), [])
        self.beat("fm-s", "p.1", self.now + 25 * M)
        self.tick(26 * M)
        self.tick(27 * M)
        [e] = self.events("stuck_recovered")
        self.assertEqual((e["batch"], e["session"], e["stage"]), ("p.1", "fm-s", "remind"))
        self.assertAlmostEqual(e["after_s"], 4 * M, delta=2)
        self.tick(46 * M)  # remind, then ask for the new quiet mark
        self.tick(67 * M)
        self.beat("fm-s", "p.1", self.now + 70 * M)
        self.tick(71 * M)
        self.assertEqual([e["stage"] for e in self.events("stuck_recovered")], ["remind", "ask"])
        self.assertEqual(self.header("p.1")["state"], "running")

    def test_recorded_while_waiting_on_a_decision(self):  # r1 should_fix 3: decide --new after the reminder
        self.plan("o")
        self.hold("o.1", "fm-o")  # no full block
        self.hold("p.1", "fm-s")
        self.tick(21 * M)
        self.decision("Q-1", ["p.1"])
        self.beat("fm-s", "p.1", self.now + 23 * M)
        self.tick(24 * M)
        [e] = [e for e in self.events("stuck_recovered") if e["session"] == "fm-s"]
        self.assertAlmostEqual(e["after_s"], 2 * M, delta=2)

    def test_none_without_a_stage_or_once_stuck(self):
        self.hold("p.1", "fm-s")
        self.tick(10 * M)
        self.beat("fm-s", "p.1", self.now + 12 * M)
        self.tick(13 * M)
        for at in (33, 53, 74):
            self.tick(at * M)
        self.assertEqual(self.header("p.1")["state"], "stuck")
        self.assertEqual(self.events("stuck_recovered"), [])
