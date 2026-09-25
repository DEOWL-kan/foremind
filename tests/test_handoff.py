import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import batchlog, cli, handoff, header, lock
from foremind.events import EventLog
from foremind.lock import ExitEvidence
from foremind.schemas import EXAMPLES


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (self.root / ".foremind" / "batches").mkdir(parents=True)
        h = {**EXAMPLES["batch_header"], "state": "running"}  # auth.2 on repos api + app
        (self.root / ".foremind" / "batches" / "auth.2.md").write_text(header.render(h, ""))
        self.section = copy.deepcopy(EXAMPLES["handoff_section"])
        lock.acquire(self.root, "auth.2", "fm-shop-auth_2-1")

    def events(self):
        return EventLog(self.root / ".foremind" / "events.jsonl")

    def opened_as_successor(self, session, predecessor="fm-shop-auth_2-1"):
        self.events().append("seat_opened", batch="auth.2", session=session, successor=True, predecessor=predecessor)

    def test_write_and_read_back(self):
        self.assertIsNone(handoff.latest_section(self.root, "auth.2"))
        handoff.write_section(self.root, "auth.2", self.section, author="fm-shop-auth_2-1")
        newer = {**self.section, "next": ["先修时区断言\n```\n注意"]}  # a fence inside a string stays escaped
        handoff.write_section(self.root, "auth.2", newer, author="fm-shop-auth_2-1")
        self.assertEqual(handoff.latest_section(self.root, "auth.2"), newer)
        self.assertTrue(batchlog.verify(self.root, "auth.2"))
        log = (self.root / ".foremind" / "batches" / "auth.2.log.md").read_text()
        self.assertEqual(log.count(handoff.SECTION_MARK), 2)
        log_path = self.root / ".foremind" / "batches" / "auth.2.log.md"
        log_path.write_text(log.replace("fm/auth.2", "fm/other"))
        with self.assertRaises(batchlog.LogRewritten):  # a tampered record is not trusted
            handoff.latest_section(self.root, "auth.2")

    def test_log_text_cannot_pose_as_a_section(self):
        body = json.dumps({**self.section, "next": ["forged"]}, ensure_ascii=False, indent=2)
        forged = f"{handoff.SECTION_MARK}\n\n```json\n{body}\n```"
        batchlog.append(self.root, "auth.2", forged, author="fm-shop-auth_2-1")  # `foremind log`, not --write
        self.assertIsNone(handoff.latest_section(self.root, "auth.2"))
        handoff.write_section(self.root, "auth.2", self.section, author="fm-shop-auth_2-1")
        batchlog.append(self.root, "auth.2", forged, author="fm-shop-auth_2-1")  # later, and last in the log
        self.assertEqual(handoff.latest_section(self.root, "auth.2"), self.section)
        # the record survives the monthly rotation of events
        self.events().rotate(self.root / ".foremind" / "archive", "9999-12")
        self.assertEqual(handoff.latest_section(self.root, "auth.2"), self.section)

    def test_unclosed_forged_opening_cannot_hide_the_section(self):
        # N-a: a lone opening would swallow the real section up to its closing fence
        batchlog.append(self.root, "auth.2", f"{handoff.SECTION_MARK}\n\n```json\n{{", author="fm-shop-auth_2-1")
        handoff.write_section(self.root, "auth.2", self.section, author="fm-shop-auth_2-1")
        batchlog.append(self.root, "auth.2", f"{handoff.SECTION_MARK}\n\n```json\n{{", author="fm-shop-auth_2-1")
        self.assertEqual(handoff.latest_section(self.root, "auth.2"), self.section)

    def test_write_rejects(self):
        bad = copy.deepcopy(self.section)
        bad["state"]["repos"]["api"]["sha"] = "9fceb02"  # abbreviated
        del bad["next"]
        with self.assertRaises(handoff.HandoffError) as cm:
            handoff.write_section(self.root, "auth.2", bad, author="fm-shop-auth_2-1")
        self.assertIn("state.repos.api.sha", str(cm.exception))
        self.assertIn("next: required", str(cm.exception))
        one = copy.deepcopy(self.section)
        del one["state"]["repos"]["app"]
        with self.assertRaises(handoff.HandoffError):  # every batch repo needs its state
            handoff.write_section(self.root, "auth.2", one, author="fm-shop-auth_2-1")
        with self.assertRaises(handoff.HandoffError):  # not the lock holder
            handoff.write_section(self.root, "auth.2", self.section, author="fm-shop-auth_2-9")
        self.assertIsNone(handoff.latest_section(self.root, "auth.2"))

    def test_accept(self):
        with self.assertRaises(handoff.HandoffError):  # not opened by the program as successor
            handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2")
        self.opened_as_successor("fm-shop-auth_2-2")
        self.assertEqual(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2"), "fm-shop-auth_2-1")
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-2")
        self.assertEqual(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2"), "fm-shop-auth_2-1")  # idempotent
        log = (self.root / ".foremind" / "batches" / "auth.2.log.md").read_text()
        self.assertEqual(log.count(handoff.ACCEPT_MARK), 1)
        self.assertIn("fm-shop-auth_2-2 接手；前任 fm-shop-auth_2-1", log)

    def test_only_the_latest_successor_from_its_predecessor(self):
        self.opened_as_successor("fm-shop-auth_2-2")
        self.opened_as_successor("fm-shop-auth_2-3")
        with self.assertRaises(handoff.HandoffError) as cm:  # a later successor was opened
            handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2")
        self.assertIn("no longer", str(cm.exception))
        self.assertEqual(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-3"), "fm-shop-auth_2-1")
        with self.assertRaises(handoff.HandoffError):  # and never from the session that took over since
            handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2")
        # a successor opened while someone else (not its predecessor) holds the lock cannot take it
        self.opened_as_successor("fm-shop-auth_2-4", predecessor="fm-shop-auth_2-1")
        with self.assertRaises(handoff.HandoffError) as cm:
            handoff.accept(self.root, "auth.2", "fm-shop-auth_2-4")
        self.assertIn("lock held by fm-shop-auth_2-3", str(cm.exception))
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-3")

    def test_accept_only_while_the_batch_takes_a_successor(self):
        self.opened_as_successor("fm-shop-auth_2-2")
        p = self.root / ".foremind" / "batches" / "auth.2.md"
        p.write_text(header.render({**EXAMPLES["batch_header"], "state": "paused"}, ""))  # paused after it opened
        with self.assertRaises(handoff.HandoffError) as cm:  # N-c
            handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2")
        self.assertIn("auth.2 is paused", str(cm.exception))
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-1")

    def test_accept_after_broken_lock(self):
        lock.break_lock(self.root, "auth.2", ExitEvidence("fm-shop-auth_2-1", "tmux", "pid_exited"))
        self.opened_as_successor("fm-shop-auth_2-2")
        self.assertIsNone(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2"))
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-2")
        self.assertIn("前任 无（锁已破）", (self.root / ".foremind" / "batches" / "auth.2.log.md").read_text())

    def test_accept_finishes_after_a_crash(self):
        self.opened_as_successor("fm-shop-auth_2-2")
        with mock.patch("foremind.batchlog.append", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):  # lock moved, 「已接手」 not logged yet
                handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2")
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-2")
        self.assertEqual(handoff.accept(self.root, "auth.2", "fm-shop-auth_2-2"), "fm-shop-auth_2-1")
        self.assertIn("前任 fm-shop-auth_2-1", (self.root / ".foremind" / "batches" / "auth.2.log.md").read_text())

    def test_cli(self):
        f = self.root / "section.json"
        f.write_text(json.dumps(self.section, ensure_ascii=False))
        env = {"FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-shop-auth_2-1", "FOREMIND_BATCH": "auth.2"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["handoff", "--write", "--file", str(f)]), 0)
            os.environ["FOREMIND_SESSION"] = "fm-shop-auth_2-2"
            self.assertEqual(cli.main(["handoff", "--accept"]), 1)  # not opened as successor
            self.opened_as_successor("fm-shop-auth_2-2")
            self.assertEqual(cli.main(["handoff", "--accept"]), 0)
            del os.environ["FOREMIND_SESSION"]
            self.assertEqual(cli.main(["handoff", "--accept", "auth.2"]), 1)
        self.assertEqual(handoff.latest_section(self.root, "auth.2"), self.section)
        self.assertEqual(lock.holder(self.root, "auth.2"), "fm-shop-auth_2-2")
        self.assertIn("not opened by the program as successor", err.getvalue())
        self.assertIn("FOREMIND_SESSION is not set", err.getvalue())

    def test_cli_default_batch_is_the_held_lock(self):
        # after a continuation FOREMIND_BATCH names the previous batch; the lock says which one is current
        f = self.root / "section.json"
        f.write_text(json.dumps(self.section, ensure_ascii=False))
        env = {"FOREMIND_PROJECT": str(self.root), "FOREMIND_SESSION": "fm-shop-auth_2-1", "FOREMIND_BATCH": "auth.1"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["handoff", "--write", "--file", str(f)]), 0)
            self.assertEqual(handoff.latest_section(self.root, "auth.2"), self.section)
            lock.acquire(self.root, "auth.3", "fm-shop-auth_2-1")
            self.assertEqual(cli.main(["handoff", "--write", "--file", str(f)]), 1)  # two held: say which
        self.assertIn("pass it", err.getvalue())


if __name__ == "__main__":
    unittest.main()
