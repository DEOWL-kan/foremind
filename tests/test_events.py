import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import events
from foremind.events import EventLog
from foremind.install import settings
from foremind.supervisor import tick as sv


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


class SeatAncestorTest(unittest.TestCase):
    """m2c.6 (REQ-11 ④): an event claiming the user records the session whose settings file an ancestor names."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.sd = self.tmp / "proj" / ".foremind"
        self.sd.mkdir(parents=True)
        self.log = EventLog(self.sd / "events.jsonl")

    def chain(self, *cmds, fail_at=None):
        """ps as a process table: this process, then its ancestors running `cmds`, then launchd."""
        pids = [os.getpid(), *range(1000, 1000 + len(cmds))]
        table = {p: (pids[i + 1] if i + 1 < len(pids) else 1, c)
                 for i, (p, c) in enumerate(zip(pids, ["python", *cmds]))}

        def ps(argv, **kw):
            pid = int(argv[-1])
            if pid == fail_at or pid not in table:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, f"{table[pid][0]:6d} {table[pid][1]}\n", "")
        return mock.patch.object(events.subprocess, "run", side_effect=ps)

    def seat(self, sd):
        return f"claude --settings {sd}/sessions/fm-p-1.settings.json --permission-mode auto"

    def test_a_seat_of_this_project(self):
        with self.chain("zsh -c foremind", self.seat(self.sd), "zsh -l"):
            e = self.log.append("pending_answered", question="Q-1", by="user")
        self.assertEqual(e["seat_ancestor"], "fm-p-1")
        self.assertEqual(self.log.verify_chain(), [])

    def test_another_project_or_no_seat_adds_nothing(self):
        other = self.tmp / "real" / ".foremind"
        for cmds in ((self.seat(other),), ("zsh -l", "login")):
            with self.chain(*cmds):
                e = self.log.append("plan_approved", plan="p", approved_by="user")
            self.assertNotIn("seat_ancestor", e)

    def test_ps_failing_or_a_chain_too_long_is_unknown(self):
        with self.chain("zsh", fail_at=1000):
            self.assertEqual(self.log.append("x", author="user")["seat_ancestor"], "unknown")
        with self.chain(*["zsh"] * events.ANCESTORS):
            self.assertEqual(self.log.append("x", requested_by="user")["seat_ancestor"], "unknown")
        with self.chain(*["zsh"] * (events.ANCESTORS - 2)):
            self.assertNotIn("seat_ancestor", self.log.append("x", requested_by="user"))

    def test_other_events_are_written_as_before(self):
        with self.chain(self.seat(self.sd)) as ps:
            e = self.log.append("review_requested", batch="p.1", requested_by="fm-p-1", by="controller")
        ps.assert_not_called()
        self.assertNotIn("seat_ancestor", e)

    def test_the_user_only_commands_by_their_type(self):
        """resume, pause, confirm-exit, audit --accept-config, decide --confirm, quota --reset, seat --user, init /
        uninstall, plan new, `hook` outside Foremind's sessions: no claim field; audit --release: batch_state's via."""
        self.assertEqual(events.USER_TYPES, {"resumed", "paused", "exit_confirmed", "l0_config_accepted",
                                             "pending_confirmed", "quota_reset", "seat_user", "program_config_write",
                                             "planner_opened", "user_config_edit"})
        for ty in sorted(events.USER_TYPES):
            with self.chain(self.seat(self.sd)) as ps:
                self.assertEqual(self.log.append(ty)["seat_ancestor"], "fm-p-1")
            self.assertEqual(ps.call_args.args[0][0], next(filter(os.path.exists, events.PS)))  # not PATH's first
        with self.chain(self.seat(self.sd)) as ps:
            self.assertTrue(sv.pause(self.sd.parent, True))
            self.assertTrue(sv.pause(self.sd.parent, False))
            settings.record(self.sd.parent, self.tmp / "proj" / ".claude" / "settings.json", "{}")
            self.log.append("batch_state", batch="p.1", prior="awaiting_audit", state="delivered",
                            via="audit --release")
            walks = ps.call_count
            self.log.append("batch_state", batch="p.1", prior="ready", state="running")  # the program's
            self.log.append("merge", batch="p.1", repo="main", head="abc", via="gh")
            self.assertEqual(ps.call_count, walks)
        self.assertEqual([(e["type"], e.get("seat_ancestor")) for e in self.log.iter()][-6:],
                         [("paused", "fm-p-1"), ("resumed", "fm-p-1"), ("program_config_write", "fm-p-1"),
                          ("batch_state", "fm-p-1"), ("batch_state", None), ("merge", None)])

    def test_ps_at_the_next_fixed_path_or_none(self):
        usr = self.tmp / "usr-bin-ps"
        usr.touch()
        with mock.patch.object(events, "PS", (str(self.tmp / "bin-ps"), str(usr))):
            with self.chain(self.seat(self.sd)) as ps:
                self.assertEqual(self.log.append("paused")["seat_ancestor"], "fm-p-1")
            self.assertEqual(ps.call_args.args[0][0], str(usr))
        with mock.patch.object(events, "PS", (str(self.tmp / "bin-ps"), str(self.tmp / "usr-ps"))):
            self.assertEqual(self.log.append("paused")["seat_ancestor"], "unknown")

    def test_a_test_project_is_not_the_real_one(self):
        """The real ps: tests write their events in temporary projects, so a seat running them adds nothing."""
        self.assertNotIn("seat_ancestor", self.log.append("batch_retried", batch="p.1", by="user"))


if __name__ == "__main__":
    unittest.main()
