"""m2f REQ-9: the gate keeps the end of a failed command's output; an in_review gate failing only acceptance or
checks is held one pass, then rerun once (heads unchanged), sent late (heads moved) or dropped (no longer in_review)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import acceptance, gate, lock, review
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


if __name__ == "__main__":
    unittest.main()
