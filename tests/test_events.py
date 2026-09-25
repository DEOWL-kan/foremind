import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind.events import EventLog


def _append_many(path, tag, n, barrier):
    log = EventLog(path)
    barrier.wait(30)  # both workers start together
    for i in range(n):
        log.append("tick", worker=tag, i=i)


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = EventLog(self.tmp / "events.jsonl")

    def at(self, ts):
        return mock.patch("foremind.events._now", return_value=ts)

    def test_fields_and_chain(self):
        a = self.log.append("seat_opened", batch="M1.1", session="fm-x-1")
        b = self.log.append("seat_closed", batch="M1.1")
        self.assertEqual(a["prev"], "")
        self.assertEqual(b["prev"], a["hash"])
        self.assertEqual((a["type"], a["phase"], a["dedupe_id"], a["batch"]), ("seat_opened", "result", None, "M1.1"))
        self.assertEqual(len(a["id"]), 32)
        self.assertEqual(list(self.log.iter()), [a, b])
        self.assertEqual(self.log.verify_chain(), [])
        with self.assertRaises(ValueError):
            self.log.append("x", hash="forged")
        with self.assertRaises(ValueError):
            self.log.append("x", phase="maybe")
        with self.assertRaises(ValueError):
            self.log.append("x", phase="intent")

    def test_dedupe_is_idempotent(self):
        first = self.log.append("spawn", phase="intent", dedupe_id="t1:spawn")
        again = self.log.append("spawn", phase="intent", dedupe_id="t1:spawn", extra=1)
        self.assertEqual(again, first)
        self.log.append("spawn", phase="result", dedupe_id="t1:spawn")
        self.log.append("spawn", phase="result", dedupe_id="t1:spawn")
        self.assertEqual(len(list(self.log.iter())), 2)

    def test_open_intents(self):
        self.log.append("a", phase="intent", dedupe_id="a")
        self.log.append("b", phase="intent", dedupe_id="b")
        self.log.append("b", phase="result", dedupe_id="b")
        self.assertEqual([e["dedupe_id"] for e in self.log.open_intents()], ["a"])

    def test_tampering_is_detected(self):
        for i in range(3):
            self.log.append("e", i=i)
        lines = self.log.path.read_text().splitlines()
        edited = json.loads(lines[1])
        edited["i"] = 99
        self.log.path.write_text("\n".join([lines[0], json.dumps(edited), lines[2]]) + "\n")
        self.assertTrue(any("line 2" in p for p in self.log.verify_chain()))
        self.log.path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # dropped entry
        self.assertTrue(any("line 2" in p for p in self.log.verify_chain()))

    def test_rotate_moves_only_closed_entries(self):
        with self.at("2026-07-10T00:00:00+00:00"):
            self.log.append("x", phase="intent", dedupe_id="open")
            self.log.append("y", phase="intent", dedupe_id="closed")
            self.log.append("y", phase="result", dedupe_id="closed")
        with self.at("2026-08-02T00:00:00+00:00"):
            last_moved = self.log.append("z")
        self.enterContext(self.at("2026-09-01T00:00:00+00:00"))  # rest of the test happens in September
        self.log.append("w", phase="intent", dedupe_id="late")
        self.log.append("x", phase="result", dedupe_id="late")
        archive = self.tmp / "archive"
        self.assertEqual(self.log.rotate(archive, "2026-09"), 3)
        self.assertEqual([e["dedupe_id"] for e in EventLog(archive / "events-2026-07.jsonl").iter()],
                         ["closed", "closed"])
        self.assertEqual([e["type"] for e in EventLog(archive / "events-2026-08.jsonl").iter()], ["z"])
        kept = list(self.log.iter())
        self.assertEqual([(e["type"], e["dedupe_id"]) for e in kept],
                         [("rotated", None), ("x", "open"), ("w", "late"), ("x", "late")])
        self.assertEqual(kept[0]["prev"], last_moved["hash"])
        self.assertEqual(self.log.verify_chain(), [])
        self.assertEqual([e["dedupe_id"] for e in self.log.open_intents()], ["open"])
        self.log.append("after")
        self.assertEqual(self.log.verify_chain(), [])
        self.assertEqual(self.log.rotate(archive, "2026-09"), 0)  # the open intent stays put

    def test_rotate_refuses_broken_chain(self):
        with self.at("2026-07-10T00:00:00+00:00"):
            self.log.append("a")
            self.log.append("b")
        self.log.path.write_text(self.log.path.read_text().replace('"b"', '"c"'))
        with self.assertRaises(ValueError):
            self.log.rotate(self.tmp / "archive", "2026-09")

    def test_concurrent_append_loses_nothing(self):
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(2)
        procs = [ctx.Process(target=_append_many, args=(self.log.path, t, 200, barrier)) for t in ("a", "b")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            self.assertEqual(p.exitcode, 0)
        events = list(self.log.iter())
        self.assertEqual(len(events), 400)
        self.assertEqual(sorted((e["worker"], e["i"]) for e in events),
                         sorted((t, i) for t in ("a", "b") for i in range(200)))
        self.assertEqual(self.log.verify_chain(), [])

    def test_rotate_validates_before(self):
        for bad in ("2026-9", "2026-13", "2026-00", "2026-09-01", "", "sept"):
            with self.assertRaises(ValueError, msg=bad):
                self.log.rotate(self.tmp / "archive", bad)


if __name__ == "__main__":
    unittest.main()
