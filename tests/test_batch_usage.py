"""m2f REQ-12: supervisor/phases/usage.py records batch_usage once per merged or cataloged batch."""
import json
import os
from unittest import mock

from foremind.plan import model
from test_supervisor import Base

USE = {"input_tokens": 100, "cache_write_tokens": 0, "cache_read_tokens": 1000, "output_tokens": 10}
LINE = {"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
    "input_tokens": 100, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1000, "output_tokens": 10}}}


class BatchUsageTest(Base):
    def test_five_a_pass_in_number_order_once_each_also_under_a_full_block(self):
        home = self.tmp / "claude"
        (home / "projects").mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}))
        done = [{"state": "merged" if i % 2 else "cataloged"} for i in range(11)]
        self.plan("p", *done, {"state": "failed"})  # p.12 failed: every unfinished batch waits on the user
        seat = self.tmp / "seat.jsonl"
        seat.write_text(json.dumps(LINE) + "\n")
        log = self.log
        log.append("seat_opened", batch="p.1", session="s1", successor=False)
        log.append("seat_opened", batch="p.1", session="s1b", continued_from="s1")  # the same seat: not its own
        log.append("seat_opened", batch="p.2", session="s2", successor=False)
        self.beat("s1", "p.1", self.now, transcript_path=str(seat))
        self.beat("s2", "p.2", self.now, transcript_path=str(self.tmp / "gone.jsonl"))
        log.append("review_receipt", batch="p.1", round=1, verdict="approved", reviewer_session="r1", cost_usd=1, **USE)
        log.append("review_receipt", batch="p.1", round=1, verdict="approved", reviewer_session="rb",
                   rebound_from="p.1.review.r1.json")
        log.append("review_failed", batch="p.2", round=1, reviewer_session="r2")  # no tokens, no transcript
        log.append("seat_opened", batch="p.12", session="s12", successor=False)  # not merged: never recorded
        before = {b: d.header["state"] for b, d in model.load(self.root, "p").batches.items()}

        def got():
            return [e["batch"] for e in self.events("batch_usage")]
        self.tick()
        self.assertEqual(got(), ["p.1", "p.2", "p.3", "p.4", "p.5"])
        self.assertEqual([t for t, _, _ in self.rec.sent], ["全部批次都在等你"], "a full block, recorded all the same")
        e1, e2 = self.events("batch_usage")[:2]
        self.assertEqual((e1["dedupe_id"], e1["writer"]["s1"]["calls"], e1["writer"]["s1"]["cache_read_tokens"]),
                         ("batch_usage:p.1", 1, 1000))
        self.assertEqual((set(e1["writer"]), e1["reviewer"]), ({"s1"}, {"r1": USE}))
        self.assertEqual((e2["writer"], e2["reviewer"]), ({"s2": None}, {"r2": None}), "what cannot be read is null")
        self.tick(60)
        self.assertEqual(got()[5:], ["p.6", "p.7", "p.8", "p.9", "p.10"])
        self.tick(120)
        self.tick(180)
        self.assertEqual(got()[10:], ["p.11"], "once each")
        self.assertEqual({b: d.header["state"] for b, d in model.load(self.root, "p").batches.items()}, before)
        self.assertEqual(self.jobs.started, [], "no model, no job")
