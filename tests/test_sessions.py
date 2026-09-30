import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foremind import seat, sessions  # noqa: E402
from foremind.events import EventLog  # noqa: E402
from foremind.lock import ExitEvidence  # noqa: E402
from foremind.supervisor.phases import sessions as phase  # noqa: E402
from test_carriers import NOBODY, Seat, lstart  # noqa: E402
from test_supervisor import Base  # noqa: E402

T0 = "Mon Sep 28 10:00:00 2026"


def row(pid, ppid, cmd, cpu=0.0, rss=1024, lstart=T0):
    return f"{pid:>6} {ppid:>6} {cpu:4.1f} {rss:>8} {lstart} {cmd}\n"


class IdentityTest(unittest.TestCase):
    """Which process is which session: the record (--settings path, bound pid + lstart), never a name (D2)."""

    def setUp(self):
        self.root = Path("/p/shop")
        self.sd = "/p/shop/.foremind/sessions"
        self.enterContext(mock.patch.object(sessions, "_bound", lambda root: {}))

    def test_snapshot_reads_ps(self):
        procs = sessions.snapshot(lambda: row(10, 1, "claude --model a b", cpu=3.5, rss=4096)
                                  + "garbage line\n" + row(11, 10, ""))
        self.assertEqual(procs[10], sessions.Proc(10, 1, 3.5, 4096, T0, "claude --model a b"))
        self.assertEqual(procs[11].command, "")
        self.assertEqual(set(procs), {10, 11})
        with self.assertRaises(sessions.SessionsError):  # nothing listed is a failed ps, not "no process"
            sessions.snapshot(lambda: "")

    def test_innermost_carrier_of_the_settings_path(self):
        s = f"--settings {self.sd}/fm-a.settings.json"
        procs = sessions.snapshot(lambda: NOBODY
                                  + row(20, 1, f"zsh -c cd /wt && env X=1 claude {s} --model m")  # orca/tmux shell
                                  + row(21, 20, f"claude {s} --model m")
                                  + row(22, 21, f"cat {self.sd}/fm-a.settings.json")  # a tool: no --settings
                                  + row(23, 21, "node mcp-server")
                                  + row(30, 1, f"claude --settings {self.sd}/fm-b.settings.json.bak")
                                  + row(31, 1, "claude --settings /q/other/.foremind/sessions/fm-c.settings.json")
                                  + row(32, 1, "claude --model m"))  # a claude, but no record says whose
        live = sessions.live(self.root, procs)
        self.assertEqual({k: [p.pid for p in v] for k, v in live.items()}, {"fm-a": [21]})

    def test_bound_controller_by_pid_and_lstart(self):
        procs = sessions.snapshot(lambda: NOBODY + row(40, 1, "claude --model m[1m]")
                                  + row(41, 1, "claude", lstart="Tue Sep 29 09:00:00 2026"))
        with mock.patch.object(sessions, "_bound", lambda root: {40: ("fm-c-1", T0), 41: ("fm-c-0", T0)}):
            live = sessions.live(self.root, procs)
        self.assertEqual({k: [p.pid for p in v] for k, v in live.items()}, {"fm-c-1": [40]}, "41: pid reused")

    def test_usage_sums_the_tree(self):
        procs = sessions.snapshot(lambda: NOBODY + row(50, 1, "claude", 2.0, 1000) + row(51, 50, "node", 1.5, 500)
                                  + row(52, 51, "rg", 0.5, 100) + row(53, 1, "other", 9.0, 9000))
        self.assertEqual(sessions.usage(procs, 50), (4.0, 1600))


class SettleTest(unittest.TestCase):
    """REQ-1: after the carrier reported the terminal closed; its evidence stands unless the process runs on."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.log = EventLog(self.root / ".foremind" / "events.jsonl")
        self.log.append("seat_launch", phase="intent", dedupe_id="seat_launch:fm-a", batch="p.1", session="fm-a")
        self.cmd = f"claude --settings {self.root}/.foremind/sessions/fm-a.settings.json"
        self.enterContext(mock.patch.object(sessions, "_bound", lambda root: {}))
        self.alive, self.sent, self.started = {60}, [], lstart(time.time() + 1)

    def ps(self):
        return (NOBODY + row(os.getpid(), os.getppid(), "python3 -m unittest")
                + "".join(row(pid, 1, self.cmd, lstart=self.started) for pid in sorted(self.alive)))

    def kill(self, pid, sig):
        self.sent.append((pid, sig))
        self.alive.discard(pid)

    def settle(self, session="fm-a", **kw):
        return sessions.settle(self.root, session, ps=self.ps, kill=self.kill, timeout_s=0.3, **kw)

    def unverified(self):
        return {e["session"]: e["why"] for e in self.log.iter() if e["type"] == "session_close_unverified"}

    def test_sigterm_to_that_pid_then_gone(self):
        self.assertTrue(self.settle())
        self.assertEqual(self.sent, [(60, signal.SIGTERM)])
        self.assertTrue(self.settle(), "nothing left: nothing sent")
        self.assertEqual((len(self.sent), self.unverified()), (1, {}))

    def test_identity_unsure_nothing_sent_evidence_stands(self):
        self.alive = {60, 61}  # two innermost processes carry it: which one is the session?
        self.assertTrue(self.settle())
        self.assertTrue(self.settle(), "recorded once")
        self.alive = {os.getppid()}  # this process's own ancestor
        self.assertTrue(self.settle())
        self.alive, self.started = {60}, T0  # started before its launch record
        self.assertTrue(self.settle())
        cmd, self.cmd = self.cmd, self.cmd.replace("fm-a", "fm-b")  # no launch record at all
        self.assertTrue(self.settle("fm-b"))
        self.cmd = cmd

        def broken():
            raise sessions.SessionsError("/bin/ps: exit 1")
        self.assertTrue(sessions.settle(self.root, "fm-c", ps=broken, kill=self.kill))
        self.assertEqual(self.sent, [])
        self.assertEqual(self.unverified(), {"fm-a": "2 processes carry it: 60, 61", "fm-b": "no launch record",
                                             "fm-c": "ps: /bin/ps: exit 1"})
        self.assertEqual(len([e for e in self.log.iter() if e["type"] == "session_close_unverified"]), 3)
        self.alive, self.started = {os.getppid()}, lstart(time.time() + 1)
        self.assertIn("ancestor", sessions.doubt(self.root, "fm-a", sessions.live(self.root, sessions.snapshot(
            self.ps))["fm-a"], sessions.snapshot(self.ps)))
        self.alive, self.started = {60}, T0
        self.assertIn("before its launch record", sessions.doubt(self.root, "fm-a", sessions.live(
            self.root, sessions.snapshot(self.ps))["fm-a"], sessions.snapshot(self.ps)))

    def test_still_running_after_sigterm(self):
        self.kill = lambda pid, sig: self.sent.append(pid)
        self.assertFalse(self.settle(), "no evidence: the lock stays, the close is retried")
        self.assertEqual(self.sent, [60], "once; never SIGKILL")

        def denied(pid, sig):
            raise PermissionError
        self.kill = denied
        self.assertTrue(self.settle(), "not ours to signal: identity unsure")
        self.assertEqual(self.unverified(), {"fm-a": "pid 60: not ours to signal"})


class LeakPhaseTest(Base):
    """REQ-2: seats running although their batch is finished or someone took over, closed as REQ-1 says; the rest
    untouched."""

    def setUp(self):
        super().setUp()
        self.plan("p", {}, {}, {})
        self.procs = {}  # session -> Seat, or fake pids (started now)
        self.enterContext(mock.patch.object(sessions, "_ps", self.ps))

    def ps(self):
        out = NOBODY
        for s, x in self.procs.items():
            cmd = f"claude --settings {self.root}/.foremind/sessions/{s}.settings.json"
            if isinstance(x, Seat):
                out += x.ps().replace(x.cmd, cmd) if x.p.poll() is None else ""
            else:
                out += "".join(row(pid, 1, cmd, lstart=lstart(time.time() + 1)) for pid in (x if isinstance(x, list)
                                                                                             else [x]))
        return out

    def launched(self, bid, s):
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{s}", batch=bid, session=s,
                        carrier="fake")

    def closes(self, why="leaked"):
        return [e["session"] for e in self.events("sv_close") if e.get("why") == why]

    def leaks(self):
        return {e["session"]: (e["batch"], e["why"], e["pid"]) for e in self.events("session_leak")}

    def closed(self):
        return {e["session"]: (e["batch"], e["pid"], e["confirmed"]) for e in self.events("session_closed")}

    def test_finished_and_taken_over_seats_are_closed(self):
        self.set_state("p.1", "merged")
        self.launched("p.1", "fm-x-p_1-1")  # finished batch, holds nothing
        self.launched("p.2", "fm-x-p_2-1")
        self.launched("p.2", "fm-x-p_2-2")  # took over from -1: holds the lock, works
        self.hold("p.2", "fm-x-p_2-2")
        self.log.append("handoff_accept", dedupe_id="handoff_accept:p.2:fm-x-p_2-2", batch="p.2",
                        session="fm-x-p_2-2", previous="fm-x-p_2-1")
        self.launched("p.2", "fm-x-p_2-9")  # launched after that accept: not taken over by it; not running anyway
        self.launched("p.3", "fm-x-p_3-1")  # a successor that never accepted, left behind when the lock was broken
        self.launched("p.3", "fm-x-p_3-2")
        self.log.append("lock_broken", batch="p.3", session="fm-x-p_3-1", carrier="orca", how="absent")
        self.hold("p.3", "fm-x-p_3-2")  # the batch's new holder
        self.procs = {"fm-x-p_1-1": 101, "fm-x-p_2-1": 102, "fm-x-p_2-2": 103, "fm-x-p_3-1": 104,
                      "fm-x-p_3-2": 105, "fm-other-1": 106}
        # an old close recorded as confirmed does not stop it (the m2b.4 records)
        self.log.append("sv_close", batch="p.1", session="fm-x-p_1-1", carrier="orca", confirmed=True, how="absent",
                        at=self.now - 3600)
        self.tick()
        self.assertEqual(sorted(self.closes()), ["fm-x-p_1-1", "fm-x-p_2-1", "fm-x-p_3-1"])
        self.assertEqual(self.leaks(), {"fm-x-p_1-1": ("p.1", "finished", 101),
                                        "fm-x-p_2-1": ("p.2", "taken_over", 102),
                                        "fm-x-p_3-1": ("p.3", "taken_over", 104)})
        self.assertEqual(self.closed(), {"fm-x-p_1-1": ("p.1", 101, True), "fm-x-p_2-1": ("p.2", 102, True),
                                         "fm-x-p_3-1": ("p.3", 104, True)})
        self.tick(60)  # within supervisor.merged_check_min: no look
        self.assertEqual(len(self.closes()), 3)

    def test_carrier_closes_then_the_process_is_made_to_exit(self):
        """REQ-1 end to end: the carrier reports the tab closed while the claude runs on (an orphaned Orca pty)."""
        self.set_state("p.1", "cancelled")
        seat_ = Seat(self, self.root, "fm-x-p_1-1")
        self.procs = {"fm-x-p_1-1": seat_}
        car = self.carrier
        car.close = lambda s: car._settled(s, ExitEvidence(s, car.name, "absent"))
        car.ps = self.ps
        self.tick()
        self.assertEqual(seat_.p.wait(5), -signal.SIGTERM)
        self.assertEqual(self.closed(), {"fm-x-p_1-1": ("p.1", seat_.p.pid, True)})
        self.assertEqual(self.events("session_close_unverified"), [])

    def test_unconfirmed_close_sends_nothing_and_is_retried(self):
        self.set_state("p.1", "cancelled")
        self.launched("p.1", "fm-x-p_1-1")
        self.procs = {"fm-x-p_1-1": 101}
        self.carrier.evidence = False  # the carrier did not report the terminal closed
        with mock.patch.object(sessions, "settle") as settle:
            self.tick()
            self.tick(3600)
            settle.assert_not_called()
        self.assertEqual(self.closes(), ["fm-x-p_1-1"] * 2, "retried per t.close_due")
        self.assertEqual(self.closed(), {"fm-x-p_1-1": ("p.1", 101, False)}, "once")
        self.assertEqual(len(self.events("session_leak")), 1)

    def test_nothing_sent_under_manual_or_without_ps(self):
        self.set_state("p.1", "merged")
        self.launched("p.1", "fm-x-p_1-1")
        self.procs = {"fm-x-p_1-1": 101}
        self.carrier.evidence, self.carrier.name = False, "manual"
        self.tick()
        self.tick(3600)
        self.assertEqual(self.closes(), ["fm-x-p_1-1"], "the request to the user, once")

        def broken():
            raise sessions.SessionsError("ps: denied")
        self.carrier.name = "fake"
        self.launched("p.1", "fm-x-p_1-2")
        self.procs = {"fm-x-p_1-2": 102}
        with mock.patch.object(sessions, "_ps", broken):
            self.tick(7200)
        self.assertEqual(len(self.closes()), 1)
        self.assertTrue(any("ps: denied" in e["error"] for e in self.events("tick_error")))

    def test_never_closed(self):
        self.set_state("p.1", "merged")
        self.launched("p.1", "fm-x-p_1-1")
        self.log.append("seat_user", batch="p.1", worktrees={})  # the user claimed the batch
        self.launched("p.2", "fm-x-p_2-1")  # lock broken on exit evidence, but nobody holds the batch since
        self.log.append("lock_broken", batch="p.2", session="fm-x-p_2-1", carrier="orca", how="absent")
        self.set_state("p.3", "merged")
        self.launched("p.3", "fm-x-p_3-1")  # being opened
        self.log.append("seat_open", phase="intent", dedupe_id="seat_open:fm-x-p_3-1", batch="p.3",
                        session="fm-x-p_3-1")
        self.launched("p.3", "fm-x-p_3-2")  # identity does not check out: two innermost processes
        self.procs = {"fm-x-p_1-1": 101, "fm-x-p_2-1": 102, "fm-x-p_3-1": 103, "fm-x-p_3-2": [104, 105]}
        self.tick()
        self.assertEqual((self.closes(), self.leaks()), ([], {}))

    def test_unknown_sessions_are_recorded_not_closed(self):
        slug = seat.project_slug(self.root, {})
        self.launched("p.1", f"fm-{slug}-p_1-1")
        planner = f"fm-{slug}-plan-1"  # being opened: its settings file is written, planner_opened not yet (m2e.3)
        (self.root / ".foremind" / "sessions").mkdir(parents=True, exist_ok=True)
        (self.root / ".foremind" / "sessions" / f"{planner}.settings.json").write_text("{}")
        self.carrier.alive = {f"fm-{slug}-p_1-1": True, f"fm-{slug}-p_9-1": True, "fm-elsewhere-p_1-1": True,
                              planner: True}
        self.tick()
        self.tick(3600)
        self.assertEqual([e["session"] for e in self.events("session_unknown")], [f"fm-{slug}-p_9-1"])
        self.assertEqual(self.carrier.closed, [])

    def test_candidates(self):
        t = mock.Mock(evs=[{"type": "seat_launch", "phase": "intent", "session": s, "batch": "p.1"}
                           for s in ("a", "b", "c")], holders={"p.1": "a"}, headers={"p.1": {}})
        t.state.return_value = "merged"
        t.pending_successor.side_effect = lambda b: "c"
        t.open_intents.return_value = []
        self.assertEqual(phase.leaked_if_running(t), {"b": ("p.1", "finished")}, "a holds, c is pending")
        t.headers, t.holders = {}, {}  # a batch no plan lists: its last batch_state, either shape
        t.evs.append({"type": "batch_state", "phase": "result", "batch": "p.1", "state": "running"})
        self.assertEqual(phase.leaked_if_running(t), {})
        t.evs.append({"type": "batch_state", "phase": "result", "batch": "p.1", "frm": "delivered", "to": "cataloged"})
        self.assertEqual(set(phase.leaked_if_running(t)), {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
