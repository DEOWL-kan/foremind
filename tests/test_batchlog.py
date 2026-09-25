import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import batchlog
from foremind.events import EventLog
from foremind.fsutil import sha256_file


class BatchLogTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.log = self.root / ".foremind" / "batches" / "M1.1.log.md"

    def test_append_then_verify(self):
        self.assertTrue(batchlog.verify(self.root, "M1.1"))
        h1 = batchlog.append(self.root, "M1.1", "D1: 用 flock", author="fm-p-M1.1-1")
        h2 = batchlog.append(self.root, "M1.1", "F1: done\n", author="user")
        self.assertEqual(h2, sha256_file(self.log))
        self.assertNotEqual(h1, h2)
        text = self.log.read_text()
        self.assertIn("fm-p-M1.1-1\n\nD1: 用 flock\n", text)
        self.assertTrue(batchlog.verify(self.root, "M1.1"))
        events = [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter()]
        self.assertEqual([(e["type"], e["batch"]) for e in events], [("batch_log_appended", "M1.1")] * 2)

    def test_rewrite_is_detected(self):
        batchlog.append(self.root, "M1.1", "D1: original", author="a")
        self.log.write_text(self.log.read_text().replace("original", "rewrote!"))
        self.assertFalse(batchlog.verify(self.root, "M1.1"))
        with self.assertRaises(batchlog.LogRewritten):
            batchlog.append(self.root, "M1.1", "more", author="a")

    def test_hand_appended_tail_still_verifies_prefix(self):
        batchlog.append(self.root, "M1.1", "D1", author="a")
        with open(self.log, "a") as f:
            f.write("tail\n")
        self.assertTrue(batchlog.verify(self.root, "M1.1"))
        self.log.unlink()
        self.assertFalse(batchlog.verify(self.root, "M1.1"))

    def test_verify_survives_rotation(self):
        events = EventLog(self.root / ".foremind" / "events.jsonl")
        with mock.patch("foremind.events._now", return_value="2026-08-15T00:00:00+00:00"):
            batchlog.append(self.root, "M1.1", "August", author="a")
            batchlog.append(self.root, "M1.2", "other batch", author="a")
        with mock.patch("foremind.events._now", return_value="2026-09-02T00:00:00+00:00"):
            self.assertEqual(events.rotate(self.root / ".foremind" / "archive", "2026-09"), 2)
            self.assertEqual(list(events.iter())[0]["type"], "rotated")  # nothing but the marker is left in the log
            self.assertTrue(batchlog.verify(self.root, "M1.1"))
            batchlog.append(self.root, "M1.1", "September", author="a")  # appending after rotation still works
            self.assertTrue(batchlog.verify(self.root, "M1.1"))
            self.log.write_text(self.log.read_text().replace("August", "Augusx"))
            self.assertFalse(batchlog.verify(self.root, "M1.1"))
        self.assertEqual(events.verify_chain(), [])

    def test_bad_ids(self):
        for bad in ("", "../x", ".hidden", "a/b"):
            with self.assertRaises(ValueError):
                batchlog.append(self.root, bad, "t", author="a")


if __name__ == "__main__":
    unittest.main()
