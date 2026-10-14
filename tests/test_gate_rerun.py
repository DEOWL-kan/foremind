"""m2f REQ-9: the gate keeps the end of a failed command's output; an in_review gate failing only acceptance or
checks is held one pass, then rerun once (heads unchanged), sent late (heads moved) or dropped (no longer in_review)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import acceptance, gate, lock, review
from foremind.commands import status
from foremind.events import EventLog
from foremind.fsutil import sha256_bytes

from test_supervisor import M, Base

HD = {"main": "a" * 40}
OUT = "ok   receipt\nFAIL checks: failed: ['make test']\n  make test（exit 1）输出末尾：\n    | boom\n" \
      "fail · p.1 is in_review · x\n"


class DetailTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".foremind" / "batches").mkdir(parents=True)

    def job(self, name, stderr=None, stdout=None):
        d = self.root / "jobs" / name
        if stderr is not None:
            d.mkdir(parents=True)
            (d / "stderr.log").write_text(stderr)
            (d / "stdout.log").write_text(stdout)
        return str(d / "stdout.log")

    def detail(self, cmds):
        res = {"batch": "p.1", "heads": HD, "commands": [{**c, "repo": "main"} for c in cmds]}
        data = json.dumps(res).encode()
        path = acceptance.result_path(self.root, "p.1", "accept", HD)
        path.write_bytes(data)
        EventLog(self.root / ".foremind" / "events.jsonl").append("accept_run", path=path.name, sha256=sha256_bytes(data))
        got = []
        gate._local_check(self.root, "p.1", {}, {"accept_commands": [c["command"] for c in cmds], "repos": ["main"]},
                          HD, "accept", lambda name, ok, detail: got.append((ok, detail)), may_run=False, rerun=False)
        return got

    def test_failed_commands_get_their_output_tail(self):
        long = "x" * 400
        cmds = [{"command": "a", "exit_code": 1,
                 "output_path": self.job("j1", "".join(f"e{i}\n" for i in range(39)) + long + "\n",
                                         "".join(f"o{i}\n" for i in range(12)))},
                {"command": "b", "exit_code": 0, "output_path": self.job("j2", "never shown\n", "nor this\n")},
                {"command": "c", "exit_code": 2, "output_path": self.job("j3", "", "only\n")},
                {"command": "d", "exit_code": 1, "output_path": self.job("j4")}]
        [(ok, detail)] = self.detail(cmds)
        self.assertFalse(ok)
        lines = detail.split("\n")
        self.assertEqual(lines[0], "failed: ['a', 'c', 'd']")
        self.assertEqual(lines[1], "  a（exit 1）输出末尾：")
        self.assertEqual(lines[2:32], [f"    | e{i}" for i in range(10, 39)] + ["    | " + "x" * 300])
        self.assertEqual(lines[32:42], [f"    | o{i}" for i in range(2, 12)])
        self.assertEqual(lines[42:44], ["  c（exit 2）输出末尾：", "    | only"], "an empty stderr.log is skipped")
        self.assertEqual(lines[44:], ["  d（exit 1）输出末尾：", f"    （输出读不到：{self.root / 'jobs' / 'j4' / 'stderr.log'}）"])
        self.assertNotIn("never shown", detail)

    def test_passing_detail_unchanged(self):
        self.assertEqual(self.detail([{"command": "a", "exit_code": 0, "output_path": self.job("j1", "e\n", "o\n")}]),
                         [(True, "1 passed")])

    def test_long_lines_keep_their_count_and_head(self):
        p = self.root / "big.log"
        p.write_text("".join(f"L{i}" + "y" * 5000 + "\n" for i in range(40)))  # 30 lines are past one block
        self.assertEqual(gate._tail(p, 30), [f"L{i}" + "y" * (300 - len(f"L{i}")) for i in range(10, 40)])
        p.write_text("head" + "z" * 200_000)  # one line, no final break
        self.assertEqual(gate._tail(p, 30), ["head" + "z" * 296])

    def test_tail_matches_splitlines_at_any_block_size(self):
        p = self.root / "t.log"
        for text in ["", "\n", "\n\n", "a", "a\n", "a\nb", "a\r\nbb\n\nccc\n", "é\n" * 7 + "ü"]:
            p.write_bytes(text.encode())
            for block in (1, 2, 3, 64 * 1024):
                with mock.patch.object(gate, "TAIL_BLOCK", block):
                    for n in (1, 3, 30):
                        self.assertEqual(gate._tail(p, n), text.splitlines()[-n:], (text, block, n))


class HeldTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p", {"state": "in_review"})
        lock.acquire(self.root, "p.1", "fm-s")
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.carrier.idle = False
        self.cur = dict(HD)
        self.enterContext(mock.patch.object(review, "batch_repos", lambda root, h, cfg: []))
        self.enterContext(mock.patch.object(review, "heads", lambda pairs: dict(self.cur)))
        self.enterContext(mock.patch.object(gate, "batch_merged", lambda root, bid, cfg: False))  # reconcile's
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]])

    def gate_fails(self, n, failing=("checks",), out=OUT):
        """What the gate job leaves: its gate_result event, its output, exit 1."""
        self.log.append("gate_result", batch="p.1", heads=HD, verdict="fail", failing=list(failing), pending=[])
        self.jobs.finish(n, code=1, stdout=out)

    def msgs(self):
        return [m.text for m in self.pending("fm-s")]

    def test_held_then_rerun_passes(self):
        self.gate_fails(0)
        self.tick(60)
        self.assertEqual(self.msgs(), [], "held")
        self.assertEqual(len(self.jobs.started), 1, "the next pass decides")
        self.tick(90)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"], ["gate", "--rerun", "p.1"]], "no gate_retry_min wait")
        self.assertTrue(self.events("sv_gate", "intent")[-1]["rerun"])
        self.log.append("gate_result", batch="p.1", heads=HD, verdict="pass", failing=[], pending=[])
        self.set_state("p.1", "approved")  # what the passing gate does
        self.jobs.finish(1, code=0)
        self.tick(120)
        self.tick(150)
        self.assertEqual(self.msgs(), [])
        self.assertEqual(len(self.jobs.started), 2)

    def test_rerun_failing_again_is_sent_with_its_output(self):
        self.gate_fails(0)
        self.tick(60)
        self.tick(90)
        self.gate_fails(1)
        self.tick(120)
        self.tick(150)
        [m] = self.msgs()
        self.assertTrue(m.startswith("Foremind：已自动重跑一次，仍未通过；门禁未通过：\nFAIL checks"))
        self.assertIn("  make test（exit 1）输出末尾：\n    | boom\n", m)
        self.assertNotIn("fail · p.1", m)
        self.assertEqual(len(self.jobs.started), 2, "one rerun per receipt round")

    def test_rerun_error_sends_the_held_failure(self):
        self.gate_fails(0)
        self.tick(60)
        self.tick(90)
        self.jobs.finish(1, code=2)  # e.g. another gate run held the batch
        self.tick(120)
        self.tick(150 + 10 * M)
        [m] = self.msgs()
        self.assertTrue(m.startswith("Foremind：自动重跑没有结果（exit 2）；门禁未通过：\nFAIL checks"))
        self.assertIn("    | boom", m)
        self.assertEqual(len(self.jobs.started), 2, "at most one rerun per receipt round")

    def test_the_hold_is_decided_once(self):
        self.gate_fails(0)
        self.tick(60)
        # a later gate_result (say a manual `foremind gate`) does not undo the hold: the next pass still reruns
        self.log.append("gate_result", batch="p.1", heads=HD, verdict="fail", failing=["receipt"], pending=[])
        self.tick(90)
        self.assertEqual(self.jobs.cmds()[1], ["gate", "--rerun", "p.1"])

    def test_moved_heads_send_the_held_failure(self):
        self.gate_fails(0)
        self.tick(60)
        self.cur = {"main": "b" * 40}
        self.tick(90)
        self.tick(120)
        [m] = self.msgs()
        self.assertTrue(m.startswith("Foremind：未自动重跑：heads 已变；门禁未通过：\nFAIL checks"))
        self.assertIn("    | boom", m)
        self.assertEqual(len(self.jobs.started), 1)

    def test_no_longer_in_review_neither_reruns_nor_sends(self):
        self.gate_fails(0)
        self.tick(60)
        self.set_state("p.1", "review_ready")
        with mock.patch.object(review, "start", side_effect=review.FlowError("no reviewer here")):
            self.tick(90)
            self.tick(120)
        self.assertFalse([m for m in self.msgs() if "门禁未通过" in m])
        self.assertEqual(len(self.jobs.started), 1)

    def test_other_failing_checks_are_sent_at_once(self):
        self.gate_fails(0, ("checks", "receipt"))
        self.tick(60)
        [m] = self.msgs()
        self.assertTrue(m.startswith("Foremind：门禁未通过：\nFAIL checks"))
        self.tick(90)
        self.assertEqual(len(self.jobs.started), 1)


class ExplicitTest(HeldTest):
    """m3a.9 (REQ-11): after m2f's one automatic rerun, `foremind review` on the same heads asks for a gate rerun
    (review._gate_rerun), started by tick._explicit at most supervisor.seat_retries times per heads; a gate waiting
    only on CI is looked at again every gate_retry_min."""

    def setUp(self):
        super().setUp()
        self.receipt(1, HD)

    def receipt(self, n, hd):
        (self.root / ".foremind" / "batches" / f"p.1.review.r{n}.json").write_text(
            json.dumps({"verdict": "approved", "heads": hd}))

    def gate_fails(self, n, failing=("checks",), out=OUT, pending=(), heads=None):
        self.log.append("gate_result", batch="p.1", heads=heads or self.cur, verdict="fail", failing=list(failing),
                        pending=list(pending))
        if n is not None:
            self.jobs.finish(n, code=1, stdout=out)

    def ask(self):
        return review._gate_rerun(self.root, "p.1", {}, dict(self.cur), review.all_events(self.root))

    def phase(self):
        return status._review_gate(status._reader(self.root), "p.1")

    def auto_rerun_fails(self):
        self.gate_fails(0)
        self.tick(60)
        self.assertEqual(self.phase(), "held")
        self.tick(90)
        self.assertEqual(self.phase(), "rerunning")
        self.gate_fails(1)
        self.tick(120)
        self.assertEqual(self.phase(), "failed")

    def test_requested_reruns_up_to_the_cap(self):
        self.auto_rerun_fails()
        self.assertEqual(self.ask(), {"gate_rerun": {"used": 0, "limit": 2, "failing": ["checks"]}})
        self.ask()  # the same failure again: the same request
        self.assertEqual(len(self.events("gate_rerun_requested")), 1)
        self.assertEqual(self.phase(), "queued")
        self.tick(90 + 10 * M - 1)  # gate_retry_min after the last gate start, on the dot
        self.assertEqual(len(self.jobs.started), 2)
        self.tick(90 + 10 * M)
        self.assertEqual(self.jobs.cmds()[2], ["gate", "--rerun", "--no-merge", "p.1"])
        self.assertEqual(self.phase(), "rerunning")
        self.tick(90 + 10 * M + 1)  # a repeated pass starts nothing
        self.assertEqual(len(self.jobs.started), 3)
        self.gate_fails(2)
        self.tick(700)
        self.assertTrue(self.msgs()[-1].startswith("Foremind：requested gate rerun 1 of 2 at these heads still fails"))
        self.assertIn("    | boom", self.msgs()[-1])
        # `foremind run` and a new pass (a supervisor restart reads the same log) give nothing back
        self.log.append("run_requested", batches=["p.1"])
        self.assertEqual(self.ask()["gate_rerun"]["used"], 1)
        self.tick(90 + 20 * M)
        self.assertEqual(len(self.jobs.started), 4)
        self.jobs.state[self.jobs.started[3]["id"]] = {"state": "lost"}  # no answer: it counts all the same
        self.tick(90 + 21 * M)
        with self.assertRaisesRegex(review.FlowError, r"2 of 2 times .* still fails checks"):
            self.ask()
        self.receipt(2, HD)  # another receipt on these heads: still spent
        with self.assertRaisesRegex(review.FlowError, "2 of 2"):
            self.ask()
        self.tick(90 + 40 * M)  # receipt r2's own gate run, as for any new receipt: no explicit rerun
        self.assertEqual((self.jobs.cmds()[4:], review.explicit_reruns(self.events(), "p.1", HD)), ([["gate", "p.1"]], 2))
        self.cur = {"main": "b" * 40}  # other heads count on their own
        self.receipt(3, self.cur)
        self.gate_fails(None)
        self.assertEqual(self.ask()["gate_rerun"]["used"], 0)
        self.cur = dict(HD)  # back to the first heads: their count stands
        self.receipt(4, HD)
        self.gate_fails(None)
        with self.assertRaisesRegex(review.FlowError, "2 of 2"):
            self.ask()

    def test_a_rerun_with_no_answer_is_told_and_asked_again(self):  # r1 must_fix
        self.auto_rerun_fails()
        self.ask()
        self.tick(90 + 10 * M)
        self.jobs.finish(2, code=2)  # e.g. a manual gate held the batch's gate lock: no gate_result
        self.tick(90 + 10 * M + 1)
        self.assertEqual(self.msgs()[-1], "Foremind: requested gate rerun 1 of 2 ended with no gate answer (exit 2); "
                                          "run `foremind review p.1` to ask again (1 left).")
        self.assertEqual(self.phase(), "failed")
        self.assertEqual(self.ask()["gate_rerun"]["used"], 1)
        self.assertEqual((len(self.events("gate_rerun_requested")), self.phase()), (2, "queued"))
        self.tick(90 + 20 * M + 1)
        self.assertEqual(self.jobs.cmds()[3], ["gate", "--rerun", "--no-merge", "p.1"])

    def test_a_local_failure_after_a_ci_wait_goes_out_at_once(self):  # r1 should_fix 1
        self.gate_fails(0, ("ci:main",), "", pending=("ci:main",))
        self.tick(60)
        self.tick(10 * M)
        self.gate_fails(1)
        self.tick(10 * M + 1)
        self.assertTrue(self.msgs()[0].startswith("Foremind：门禁未通过：\nFAIL checks"))
        self.assertEqual((self.events("gate_held"), self.phase()), ([], "failed"))

    def test_a_stale_request_is_not_run(self):
        self.auto_rerun_fails()
        self.ask()
        self.gate_fails(None, ("receipt",))  # another failure since (say a manual gate run)
        self.tick(90 + 10 * M)
        self.assertEqual(len(self.jobs.started), 2)
        self.assertEqual(self.ask(), {}, "not a local failure: as before, unchanged")

    def test_cap_lowered_after_the_request(self):
        self.auto_rerun_fails()
        self.ask()
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nseat_retries = 1\n')
        self.log.append("sv_gate", phase="intent", dedupe_id="sv_gate:p.1:old", batch="p.1", round=1, explicit=True,
                        heads=HD, at=self.now)  # one spent before
        self.tick(30 * M)
        self.assertEqual(len(self.jobs.started), 2)
        self.assertEqual([e["used"] for e in self.events("gate_rerun_refused")], [1])
        self.assertEqual(self.phase(), "failed")
        with self.assertRaisesRegex(review.FlowError, r"1 of 1 times .* still fails checks"):  # r2: not queued again
            self.ask()

    def test_mixed_failure_is_no_ci_wait(self):
        self.gate_fails(0, ("checks", "ci:main"), pending=("ci:main",))
        self.tick(60)
        self.assertTrue(self.msgs()[0].startswith("Foremind：门禁未通过：\nFAIL checks"))
        self.assertEqual(self.ask(), {})
        self.tick(30 * M)
        self.assertEqual(len(self.jobs.started), 1)

    def test_ci_late_green(self):
        self.gate_fails(0, ("ci:main",), "FAIL ci:main: required: waiting: ['check build']\n", pending=("ci:main",))
        self.tick(60)
        self.assertEqual((self.msgs(), self.phase()), ([], "ci_wait"))
        self.assertEqual(self.ask(), {})
        self.tick(10 * M - 1)
        self.assertEqual(len(self.jobs.started), 1)
        self.tick(10 * M)
        self.assertEqual(self.jobs.cmds()[1], ["gate", "p.1"], "local results reused: no --rerun, nothing spent")
        self.gate_fails(1, ("ci:main",), "", pending=("ci:main",))
        self.tick(20 * M)
        self.assertEqual(len(self.jobs.started), 3)
        self.log.append("gate_result", batch="p.1", heads=HD, verdict="pass", failing=[], pending=[])
        self.set_state("p.1", "approved")
        self.jobs.finish(2, code=0)
        self.tick(20 * M + 1)
        self.assertEqual(self.msgs(), [])
        self.assertEqual(review.explicit_reruns(self.events(), "p.1", HD), 0)


if __name__ == "__main__":
    unittest.main()
