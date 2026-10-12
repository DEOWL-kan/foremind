"""m2d.7 ⑦ and item 8: what the user's own commands write claims the user, so events.append checks seat_ancestor
(REQ-11 ④); the session regex behind it is anchored and folds with the volume. m2f REQ-8: a claim of the controller
too."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_seat
from foremind import batchlog, events, review
from foremind.events import EventLog
from foremind.plan import freeze
from test_gate_fixture import Project
from test_plan_helpers import ProjectCase
from test_review import cli

NO_SESSION = {"FOREMIND_SESSION": "", "FOREMIND_ROLE": "", "FOREMIND_JOB": ""}


class ClaimsTest(ProjectCase):
    def events(self, type_):
        return [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == type_]

    def test_goal_frozen_names_who(self):
        cases = ((NO_SESSION, "user"), ({**NO_SESSION, "FOREMIND_ROLE": "planner", "FOREMIND_SESSION": "fm-p-2"},
                                        "planner"), ({**NO_SESSION, "FOREMIND_SESSION": "fm-p-3"}, "fm-p-3"))
        for n, (env, by) in enumerate(cases, 1):
            with mock.patch.dict(os.environ, env), mock.patch.object(events, "seat_ancestor", return_value="fm-x-1"):
                freeze.freeze_goal(self.root, f"p{n}", "REQ-1: 登录\n")
            e = self.events("goal_frozen")[-1]
            self.assertEqual(e["by"], by)
            self.assertEqual(e.get("seat_ancestor"), "fm-x-1" if by == "user" else None)

    def test_batch_log_appended_names_its_author(self):
        (self.root / ".foremind" / "batches").mkdir()
        with mock.patch.object(events, "seat_ancestor", return_value="fm-x-1"):
            batchlog.append(self.root, "p.1", "x", author="user")
            batchlog.append(self.root, "p.1", "y", author="fm-p-1")
        self.assertEqual([(e["author"], e.get("seat_ancestor")) for e in self.events("batch_log_appended")],
                         [("user", "fm-x-1"), ("fm-p-1", None)])
        self.assertTrue(batchlog.verify(self.root, "p.1"))


class ControllerClaimsTest(unittest.TestCase):  # m2f REQ-8
    def test_any_claim_of_the_controller(self):  # the general fallback; a session, gate or supervisor is none
        log = EventLog(Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.jsonl")
        with mock.patch.object(events, "seat_ancestor", return_value="fm-x-1"):
            for k in events.CLAIMS:
                self.assertEqual(log.append("x", **{k: "controller"}).get("seat_ancestor"), "fm-x-1", k)
                for v in ("fm-p-1", "gate", "supervisor"):
                    self.assertNotIn("seat_ancestor", log.append("x", **{k: v}), (k, v))

    def test_listed_forms(self):  # a seat that cleared FOREMIND_SESSION and set FOREMIND_ROLE or FOREMIND_CONTROLLER
        p = Project(self)
        p.batch(state="approved")
        p.batch("shop.2", state="merged")
        fp = "0" * 16
        for env, argv in (({"FOREMIND_ROLE": "controller"},
                           ["review", "shop.1", "--request-changes", "--category", "overreach", "--item", "x"]),
                          ({"FOREMIND_ROLE": "controller"}, ["review", "shop.1", "--withdraw", fp, "--reason", "r"]),
                          ({"FOREMIND_CONTROLLER": "c-1"}, ["defect", "shop.2", "--source", "run", "--note", "n"])):
            with mock.patch.dict(os.environ, {**NO_SESSION, **env}), \
                    mock.patch.object(events, "seat_ancestor", return_value="fm-shop-shop.1-1"), \
                    mock.patch.object(review, "load_receipts", return_value=[{"issues": [{"fingerprint": fp}]}]):
                self.assertEqual(cli(argv)[0], 0, argv)
        self.assertEqual([(e["by"], e.get("seat_ancestor")) for t in ("batch_state", "changes_requested_by",
                                                                       "review_withdrawn", "defect_found")
                          for e in p.events(t)], [("controller", "fm-shop-shop.1-1")] * 4)


class SeatOpenTest(unittest.TestCase):
    def test_seat_open_by_the_user(self):
        for env, by in ((NO_SESSION, "user"), ({**NO_SESSION, "FOREMIND_JOB": "j-1"}, None),
                        ({**NO_SESSION, "FOREMIND_SESSION": "fm-c-1"}, "user")):  # r1: a session's is no job
            t = test_seat.SeatTest("run")
            t.setUp()  # before env: it drops FOREMIND_JOB
            try:
                t.write_header("p.1")
                with mock.patch.dict(os.environ, env), mock.patch.object(events, "seat_ancestor",
                                                                         return_value="fm-x-1"):
                    t.open("p.1")
                [intent] = [e for e in t.events("seat_open") if e["phase"] == "intent"]
                [opened] = t.events("seat_opened")
                self.assertEqual(opened["starts"], {"api": t.main["api"]})  # bounds.pushed's base (⑥)
            finally:
                t.doCleanups()
            self.assertEqual((intent.get("by"), intent.get("seat_ancestor")), (by, "fm-x-1" if by else None), env)


class SessionRegexTest(unittest.TestCase):
    def setUp(self):
        self.sd = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve() / "proj" / ".foremind"
        self.sd.mkdir(parents=True)
        self.log = EventLog(self.sd / "events.jsonl")

    def chain(self, cmd):
        """ps answering: this process's parent runs `cmd`, whose parent is launchd (as test_events.chain)."""
        table = {os.getpid(): (1000, "python"), 1000: (1, cmd)}
        return mock.patch.object(events.subprocess, "run", side_effect=lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, "%6d %s\n" % table[int(argv[-1])], ""))

    def test_anchored_on_the_left(self):  # m2c.6 r1 note 6: a path ending in this project's is another project
        for cmd in (f"claude --settings /backup{self.sd}/sessions/fm-p-1.settings.json",
                    f"claude --settings=x{self.sd}/sessions/fm-p-1.settings.json"):
            with self.chain(cmd):
                self.assertNotIn("seat_ancestor", self.log.append("x", by="user"), cmd)
        with self.chain(f"claude --settings={self.sd}/sessions/fm-p-1.settings.json"):
            self.assertEqual(self.log.append("x", by="user")["seat_ancestor"], "fm-p-1")

    def test_case_folds_with_the_volume(self):
        cmd = f"claude --settings {str(self.sd).upper()}/SESSIONS/fm-p-1.settings.json"
        for fold, want in ((True, "fm-p-1"), (False, None)):
            with mock.patch.object(events.pathmatch, "FOLD", fold), self.chain(cmd):
                self.assertEqual(self.log.append("x", by="user").get("seat_ancestor"), want, fold)


if __name__ == "__main__":
    unittest.main()
