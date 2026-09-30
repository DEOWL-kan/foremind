"""m2b.1: the supervisor's runtime fixes (pending validation, gate waiting, idle seats, notices, re-exec, phases)."""
import contextlib
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from unittest import mock

from foremind import cli, header, hooks, inbox, lock, review, schemas
from foremind.commands import status, supervise
from foremind.supervisor import phases, stuck
from foremind.supervisor import tick as sv
from test_supervisor import M, Base, iso


class PendingTest(Base):
    def q(self, qid, **kw):
        rec = {**schemas.EXAMPLES["pending"], "id": qid, "blocks": ["p.1"], "state": "open", **kw}
        p = self.root / ".foremind" / "decisions" / f"{qid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({k: v for k, v in rec.items() if v is not None}))
        self.log.append("pending_created", question=qid, request={})

    def test_a_malformed_pending_costs_a_tick_error_not_the_pass(self):  # m2a.7 r1
        self.plan("p")
        self.plan("q")
        self.q("Q-1", options=None, code=None)  # push would raise KeyError
        self.q("Q-2")
        for at in (2 * M, 3 * M):
            self.tick(at)
        errs = [(e["where"], e["what"]) for e in self.events("tick_error")]
        self.assertEqual(errs, [("decision", "Q-1.json")], "once per file and first error")
        self.assertEqual([t for t, _, _ in self.rec.sent], ["待决 Q-2"], "the well-formed one is pushed")
        self.assertEqual((self.header("p.1")["state"], self.header("q.1")["state"]), ("planned", "ready"),
                         "fail-closed: the malformed one still blocks p.1; the pass went on")

    def test_wrong_types_still_block_and_the_pass_goes_on(self):  # r1: ready.blocking would raise TypeError
        self.plan("p")
        self.plan("q")
        d = self.root / ".foremind" / "decisions"
        d.mkdir(parents=True)
        for name, rec in {"Q-1": {"id": 1, "blocks": ["p.1"]}, "Q-2": {"id": "Q-2", "blocks": ["p.1"]},
                          "Q-3": {"id": "Q-3", "blocks": 5}, "Q-4": {"id": "Q-4", "blocks": True}}.items():
            (d / f"{name}.json").write_text(json.dumps({**rec, "state": "open"}))
        (d / "Q-5.json").write_text("[]")  # r2: not an object at all
        self.tick()  # none has a pending_created (never pushed): told all the same
        self.assertEqual([e["what"] for e in self.events("tick_error")],
                         ["Q-1.json", "Q-2.json", "Q-3.json", "Q-4.json", "Q-5.json"])
        self.assertEqual((self.header("p.1")["state"], self.header("q.1")["state"]), ("planned", "ready"))


class GateWaitTest(Base):
    def test_an_approved_batch_waits_for_its_upstream_without_gate_runs(self):  # finding 17
        after = [{"batch": "p.1", "reason": "r"}]
        self.plan("p", {}, {"state": "approved", "merge_after": after}, {"state": "in_review", "merge_after": after})
        self.hold("p.1", "fm-a")
        self.carrier.idle = False
        for b in ("p.2", "p.3"):
            (self.root / ".foremind" / "batches" / f"{b}.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.tick()
        self.tick(11 * M)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.3"]], "the first gate of in_review still runs")
        self.assertEqual([(e["batch"], e["upstream"]) for e in self.events("gate_waiting")], [("p.2", ["p.1"])])
        self.set_state("p.1", "merged")
        self.tick(12 * M)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.3"], ["gate", "p.2"]], "merged: the gate runs at once")

    def test_unknown_upstream_counts_as_not_merged(self):
        t = sv.Tick(self.root, {}, self.now)
        t.headers = {"a.1": {"merge_after": [{"batch": "z.9"}], "depends_on": ["a.2", "a.3"]},
                     "a.2": {"state": "cataloged"}, "a.3": {"state": "delivered"}}
        self.assertEqual(t.unmerged_upstream("a.1"), ["a.3", "z.9"])


class ReleaseTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p", {}, {})
        self.hold("p.2", "fm-b")  # keeps it from being a full block

    def test_an_idle_seat_that_only_waits_for_a_merge_is_released(self):  # finding 21
        self.hold("p.1", "fm-s", state="approved")
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.assertEqual([(e["batch"], e["session"], e["state"]) for e in self.events("seat_released")],
                         [("p.1", "fm-s", "approved")])
        self.assertEqual(self.jobs.started, [], "approved needs no successor")
        # the controller asks for changes: nobody holds it, so the list stays in `## 状态` for a successor's L1
        self.assertEqual(review.request_changes(self.root, "p.1", ["fix the off-by-one"], by="controller"),
                         "changes_requested")
        self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])
        self.assertIn("fix the off-by-one", hooks.l1(self.root, "fm-s2", "p.1", "seat"))

    def test_what_keeps_a_seat(self):
        cases = {"tool": ({"tool_open": True}, {}), "handoff": ({"handoff_requested": True}, {}),
                 "busy": ({}, {"idle": False}), "manual": ({}, {"name": "manual"})}
        for why, (beat, car) in cases.items():
            with self.subTest(why):
                self.setUp()
                self.hold("p.1", "fm-s", state="delivered", **beat)
                for k, v in car.items():
                    setattr(self.carrier, k, v)
                self.tick()
                self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], "fm-s"))
        self.setUp()
        self.hold("p.1", "fm-s", state="awaiting_audit")
        self.set_state("p.1", "awaiting_audit", mode="watch")
        self.tick()
        self.assertEqual(self.carrier.closed, [], "not auto")
        self.setUp()
        self.hold("p.1", "fm-s", state="approved")
        inbox.append("fm-s", "hi", sender="user", root=self.root)
        self.tick()
        self.assertEqual(self.carrier.closed, [], "its inbox is not empty (delivered later this pass)")
        self.assertEqual(len(self.carrier.sent), 1)
        self.setUp()
        self.log.append("sv_close", batch="p.1", session="fm-s", confirmed=False)  # r2: say, an earlier take_over
        self.hold("p.1", "fm-s", state="approved", tool_open=True)
        self.tick()
        self.assertEqual(self.carrier.closed, [], "only its own idle close skips the checks")

    def test_a_tool_left_open_is_cleared_for_any_unfinished_batch(self):  # m2d.6 REQ-18
        self.plan("w", {"mode": "watch"})
        beat = {"event": "PreToolUse", "tool_open": True, "open_tools": ["t1"]}
        self.hold("p.1", "fm-s", state="in_review", **beat)
        self.hold("w.1", "fm-w", state="in_review", **beat)
        self.hold("p.2", "fm-b", state="merged", **beat)
        self.tick(19 * M)
        self.assertEqual(self.events("tool_open_cleared"), [])
        self.carrier.idle = False
        self.tick(21 * M)
        self.assertEqual(self.events("tool_open_cleared"), [], "a busy screen: maybe still running")
        self.carrier.idle = True
        self.tick(22 * M)
        self.assertEqual(sorted((e["batch"], e["session"], e["tools"]) for e in self.events("tool_open_cleared")),
                         [("p.1", "fm-s", ["t1"]), ("w.1", "fm-w", ["t1"])])
        self.tick(23 * M)
        self.assertEqual(len(self.events("tool_open_cleared")), 2)

    def test_the_carrier_is_read_only_for_a_stale_open_tool(self):
        self.hold("p.1", "fm-s", state="in_review")
        self.hold("p.2", "fm-b", state="in_review", tool_open=True, open_tools=["t1"])
        with mock.patch.object(self.carrier, "read_state", side_effect=AssertionError("read")):
            self.tick(19 * M)  # neither is due: no tool open, or open less than stuck.remind_min

    def test_a_session_already_gone_is_released(self):  # r2: its carrier gives the exit evidence on close
        self.hold("p.1", "fm-s", state="approved")
        self.carrier.alive["fm-s"], self.carrier.idle = False, False
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.assertEqual([e["session"] for e in self.events("seat_released")], ["fm-s"])

    def test_a_carrier_that_cannot_tell_keeps_the_seat(self):  # m2b.1 r3: alive None is not gone
        self.hold("p.1", "fm-s", state="approved")
        self.carrier.alive["fm-s"] = None
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], "fm-s"))

    def test_a_session_gone_or_confirmed_gone_skips_the_idle_checks(self):  # m2b.1 r3
        busy = {"tool_open": True, "open_tools": ["t1"], "handoff_requested": True}
        self.hold("p.1", "fm-s", state="delivered", **busy)
        inbox.append("fm-s", "hi", sender="user", root=self.root)
        self.carrier.alive["fm-s"] = False
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.setUp()
        self.hold("p.1", "fm-s", state="approved", **busy)
        inbox.append("fm-s", "hi", sender="user", root=self.root)
        self.log.append("exit_confirmed", batch="p.1", session="fm-s", carrier="fake")
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], None), "evidence: no close needed")
        self.assertEqual([e["session"] for e in self.events("seat_released")], ["fm-s"])

    def test_a_tool_left_open_on_an_idle_screen_is_cleared(self):  # m2b.8 r1: not only for running batches
        self.hold("p.1", "fm-s", state="approved", event="PreToolUse", tool_open=True, open_tools=["t1"])
        self.tick(19 * M)
        self.assertEqual((self.carrier.closed, self.events("tool_open_cleared")), ([], []))
        self.tick(21 * M)
        self.assertEqual([(e["session"], e["tools"]) for e in self.events("tool_open_cleared")], [("fm-s", ["t1"])])
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))

    def test_a_turn_that_ended_is_idle_and_no_evidence_retries_without_a_notice(self):
        self.hold("p.1", "fm-s", state="approved", event="Stop")
        self.carrier.idle, self.carrier.evidence = False, False
        for at in (0, 30, 61):
            self.tick(at)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"] * 2, "fm-s"))
        self.carrier.alive["fm-s"], self.carrier.evidence = False, True  # half closed: retried all the same
        self.tick(3 * M + 2)
        self.assertEqual((len(self.carrier.closed), lock.holder(self.root, "p.1")), (3, None))
        self.assertEqual(self.rec.sent, [])

    def test_a_stop_that_let_a_delivery_in_is_no_end(self):  # r1: the Stop hook's block hands it the messages
        self.hold("p.1", "fm-s", state="approved", event="Stop")
        self.carrier.idle = False
        inbox.append("fm-s", "fix it", sender="controller", root=self.root)
        with inbox.locked("fm-s", root=self.root):
            inbox.mark_delivered("fm-s", inbox.pending_messages("fm-s", root=self.root)[-1].end, root=self.root)
        self.tick()
        self.assertEqual(self.carrier.closed, [], "its Stop came before the delivery: the turn goes on")
        self.beat("fm-s", "p.1", self.now + 5, event="Stop")  # the turn that read them ended
        self.tick(10)
        self.assertEqual(self.carrier.closed, ["fm-s"])


class NoticeTest(Base):
    def test_a_plan_edited_by_hand_is_told_once_per_hash(self):  # finding 19
        self.plan("p")
        self.plan("q")
        self.hold("p.1", "fm-s")
        p = self.root / ".foremind" / "plans" / "q" / "plan.md"
        h, body = header.parse(p.read_text())
        del h["approved_at"]
        p.write_text(header.render(h, body))  # never approved: waits() says so, not this notice
        for title in ("x", "x", "y"):
            h, body = header.parse(self.hpath("p.1").read_text())
            self.hpath("p.1").write_text(header.render({**h, "title": title}, body))
            self.tick()
        hashes = [e["plan_hash"] for e in self.events("plan_unbound")]
        self.assertEqual(([e["plan"] for e in self.events("plan_unbound")], len(set(hashes))), (["p", "p"], 2))
        self.assertEqual([t for t, _, _ in self.rec.sent], ["计划 p 未绑定"] * 2)
        out = status.report(self.root)
        self.assertIn("计划 p 未绑定，调度已停", out)
        self.assertNotIn("计划 q 未绑定", out)

    def test_delivered_for_the_user_to_merge_is_told_once_per_entry(self):  # finding 20
        self.plan("p", {"state": "delivered"}, {})
        self.hold("p.2", "fm-b")
        self.log.append("batch_state", batch="p.1", prior="approved", state="delivered")
        self.tick()
        self.tick(30)
        self.log.append("batch_state", batch="p.1", prior="approved", state="delivered")  # delivered again
        self.tick(60)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 已交付，等你合入"] * 2)

    def test_merge_dev_is_not_the_users_to_merge(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[delivery]\nlevel = "merge_dev"\n')
        self.plan("p", {"state": "delivered"})
        self.log.append("batch_state", batch="p.1", prior="approved", state="delivered")
        self.tick()
        self.assertEqual([t for t, _, _ in self.rec.sent if "已交付" in t], [])


class ReexecTest(Base):
    def setUp(self):
        super().setUp()
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        self.ticks = self.enterContext(mock.patch.object(sv, "tick"))
        self.sleeps = self.enterContext(mock.patch.object(supervise.time, "sleep"))
        self.execve = self.enterContext(mock.patch.object(supervise.os, "execve", side_effect=KeyboardInterrupt))

    def run_loop(self, prints, sleeps=99):
        self.sleeps.side_effect = [None] * (sleeps - 1) + [KeyboardInterrupt]
        with mock.patch.object(sv, "code_fingerprint", side_effect=prints), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(supervise._supervise(None), 0)

    def test_the_fingerprint_sees_a_removed_file_and_an_old_mtime(self):  # r1 note
        pkg = self.tmp / "pkg" / "foremind"
        pkg.mkdir(parents=True)
        (pkg / "a.py").write_text("a = 1\n")
        (pkg / "b.py").write_text("b = 1\n")
        os.utime(pkg / "a.py", ns=(1, 1))  # not the newest
        with mock.patch.object(sv, "PKG_PARENT", pkg.parent):
            fp = sv.code_fingerprint()
            (pkg / "a.py").unlink()
            self.assertNotEqual(sv.code_fingerprint(), fp)
            (pkg / "a.py").write_text("a = 22\n")
            os.utime(pkg / "a.py", ns=(1, 1))  # put back with its old mtime (cp -p, rsync -t)
            self.assertNotEqual(sv.code_fingerprint(), fp)

    def test_new_code_that_held_still_for_an_interval_is_tried_then_executed(self):
        self.run_loop([1, 1, 2, 2])  # start, then after each sleep
        self.assertEqual(self.ticks.call_count, 2, "the change is seen, then it held still: tried before the tick")
        rec = json.loads(sv.supervisor_path(self.root).read_text())
        self.assertEqual((rec["pid"], rec["code"], rec["argv"]), (os.getpid(), 1, sv.fm("supervise")))
        exe, argv, env = self.execve.call_args.args
        self.assertEqual((exe, argv), (sys.executable, sv.fm("supervise")))
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(sv.PKG_PARENT))
        self.assertNotIn("GIT_SSH_COMMAND", env, "as started, not as _unattended left it")
        self.assertEqual([(e["from"], e["to"]) for e in self.events("supervisor_reexec")], [(1, 2)])

    def test_new_code_that_does_not_run_is_told_once_and_the_old_code_goes_on(self):
        bad = subprocess.CompletedProcess([], 1, "", "Traceback\nSyntaxError: invalid syntax\n")
        with mock.patch.object(supervise.subprocess, "run", return_value=bad) as run:
            self.run_loop([1, 2, 3, 3, 3, 3], sleeps=5)
        tries = [c for c in run.call_args_list if c.args[0] == sv.fm(*supervise.TRY)]  # not _unattended's git config
        self.assertEqual((len(tries), self.execve.called, self.ticks.call_count), (1, False, 5))
        self.assertEqual([(e["to"], e["error"]) for e in self.events("supervisor_reexec_failed")],
                         [(3, "SyntaxError: invalid syntax")])
        self.assertEqual([t for t, _, _ in self.rec.sent], ["监督进程没能换上新代码"])

    def test_the_try_run_loads_supervise_and_the_tick(self):  # m2b.1 r2 note 5: the cli skips a broken module
        env = {**os.environ, "PYTHONPATH": str(sv.PKG_PARENT)}
        with mock.patch.object(supervise.subprocess, "run", wraps=subprocess.run) as run, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            supervise._reexec(self.root, sv.fm("supervise"), env, 1, 2)
        self.assertEqual(run.call_args.args[0], sv.fm("supervise", "--help"))
        self.assertTrue(self.execve.called, "the real try run exits 0")

    def test_a_skipped_supervise_module_is_the_error(self):  # not argparse's "invalid choice" after it
        skip = f"{cli.SKIPPED} supervise: SyntaxError: invalid syntax"
        bad = subprocess.CompletedProcess([], 2, "", f"{skip}\nusage: foremind\nforemind: error: invalid choice\n")
        with mock.patch.object(supervise.subprocess, "run", return_value=bad), \
                contextlib.redirect_stderr(io.StringIO()):
            supervise._reexec(self.root, sv.fm("supervise"), {}, 1, 2)
        self.assertEqual([e["error"] for e in self.events("supervisor_reexec_failed")], [skip])

    def test_supervisor_state_for_status(self):
        self.assertIn("监督进程未运行", status.report(self.root))
        p = sv.supervisor_path(self.root)
        p.write_text(json.dumps({"pid": os.getpid(), "code": sv.code_fingerprint()}))
        self.assertIn(f"监督进程运行中（pid {os.getpid()}）", status.report(self.root))
        p.write_text(json.dumps({"pid": os.getpid(), "code": 1}))
        self.assertIn("监督进程代码旧于当前包", status.report(self.root))
        gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        p.write_text(json.dumps({"pid": int(gone.stdout), "code": 1}))
        self.assertIn("监督进程未运行", status.report(self.root))

    def test_a_pid_given_to_another_process_is_stopped(self):  # D23
        p, at = sv.supervisor_path(self.root), datetime.now().astimezone()
        rec = {"pid": os.getpid(), "code": sv.code_fingerprint()}
        p.write_text(json.dumps({**rec, "started_at": at.isoformat(timespec="seconds")}))
        self.assertEqual(sv.supervisor_state(self.root)[0], "current", "started before its record: a re-exec")
        p.write_text(json.dumps({**rec, "started_at": (at - timedelta(days=1)).isoformat(timespec="seconds")}))
        self.assertEqual(sv.supervisor_state(self.root)[0], "stopped", "started after the record: another process")
        with mock.patch.object(sv.subprocess, "run", side_effect=OSError("no ps")):
            self.assertEqual(sv.supervisor_state(self.root)[0], "current", "ps cannot tell: as before")


class SendersTest(Base):
    def test_worked_out_again_only_for_new_intents(self):  # m2d.6: tick._deliver asks once per message
        t = sv.Tick(self.root, {}, self.now)
        t.intents = {"sv_say:apiretry:s:e1:1": {"token": "a"}, "sv_say:stuck:x": {"token": "b"}}
        self.assertEqual(stuck.retry_senders(t), {"supervisor#a"})
        t.intents["sv_say:apiretry:s:e1:1"]["token"] = "changed"  # never happens: shows the set is not worked out
        self.assertEqual(stuck.retry_senders(t), {"supervisor#a"})
        t.intents["sv_say:apiretry:s:e2:1"] = {"token": "c"}  # say added one this pass
        self.assertEqual(stuck.retry_senders(t), {"supervisor#changed", "supervisor#c"})


class GateRunsTest(Base):
    def test_exit_2_counts_only_while_a_merge_at_the_current_head_is_open(self):  # m2b.10 r3
        t = sv.Tick(self.root, {}, self.now)
        run = {"type": "sv_gate", "phase": "result", "batch": "a.1", "ok": False, "exit_code": 2, "state": "delivered"}
        t.evs = [{"type": "review_requested", "phase": "result", "batch": "a.1", "heads": {"main": "h1"}},
                 {"type": "merge", "phase": "intent", "batch": "a.1", "repo": "main", "head": "h1",
                  "dedupe_id": "merge:a.1:main:h1"},  # merge_command failed at h1, left without a result
                 run,
                 {"type": "batch_updated", "phase": "result", "batch": "a.1", "heads": {"main": "h2"}},
                 run, run]  # at h2: the batch busy, gh unreachable
        with mock.patch.object(t, "merge_dev", return_value=True):
            self.assertEqual(t.failures("a.1", "gate", state="delivered"), 1)
            t.evs[3:3] = [{"type": "merge", "phase": "intent", "batch": "a.1", "repo": "main", "head": "h2",
                           "dedupe_id": "merge:a.1:main:h2"}]  # h2's own merge failed too
            self.assertEqual(t.failures("a.1", "gate", state="delivered"), 3)

    def test_a_pass_after_the_failed_run_is_the_status_written(self):  # m2b.5 r12: the user's own `foremind gate`
        t = sv.Tick(self.root, {}, self.now)
        def run(ok):
            return {"type": "sv_gate", "phase": "result", "batch": "a.1", "state": "delivered", "ok": ok}

        def res(v):
            return {"type": "gate_result", "phase": "result", "batch": "a.1", "verdict": v}

        for evs, want in (([], False), ([run(False)], True), ([res("pass"), run(False)], True),
                          ([run(False), res("fail")], True), ([run(False), res("pass")], False),
                          ([run(False), res("pass"), run(False)], True), ([run(False), run(True)], False)):
            t.evs = evs
            self.assertEqual(t.unposted("a.1"), want, evs)


class CiWaitTest(Base):
    def result(self, at, pending):
        with mock.patch("foremind.events._now", return_value=iso(self.now + at)):
            self.log.append("gate_result", batch="p.1", heads={"main": "a" * 40}, path="p.1.gate.x.json",
                            sha256="0" * 64, verdict="fail" if pending else "pass", failing=pending, pending=pending)

    def test_the_wait_starts_after_the_last_result_not_pending_on_that_head(self):  # m2b.7 r2
        self.plan("p", {"state": "delivered"})
        self.result(0, ["ci:main"])
        self.result(60 * M, [])  # green on the same head
        self.result(100 * M, ["ci:main"])  # a CI re-run: pending again
        self.tick(130 * M)
        self.assertEqual(self.events("ci_pending_long"), [], "30 minutes into this wait, not 130")
        self.tick(221 * M)
        [e] = self.events("ci_pending_long")
        self.assertEqual(e["since"], iso(self.now + 100 * M))
        self.assertEqual([t for t, _, _ in self.rec.sent if "CI" in t], ["p.1 CI 久等"])
        # m2d.6 REQ-18: green, then pending again on the same head: a new wait, told again once it is long
        self.result(230 * M, [])
        self.result(240 * M, ["ci:main"])
        self.tick(359 * M)
        self.assertEqual(len(self.events("ci_pending_long")), 1)
        self.tick(361 * M)
        self.tick(362 * M)
        self.assertEqual([e["since"] for e in self.events("ci_pending_long")],
                         [iso(self.now + 100 * M), iso(self.now + 240 * M)])
        self.assertEqual([t for t, _, _ in self.rec.sent if "CI" in t], ["p.1 CI 久等"] * 2)


PHASES = {
    "a.py": "def run(t, blocked):\n    t.emit('phase_ran', name='a', blocked=blocked)\n",
    "b.py": "def run(t, blocked):\n    raise RuntimeError('boom')\n",
    "c.py": ("KIND = 'pjob'\n"
             "def run(t, blocked):\n"
             "    t.emit('phase_ran', name='c', blocked=blocked)\n"
             "    if not blocked:\n"
             "        t.start_job(KIND, 'k1', ['/usr/bin/true'])\n"
             "def after(t, it, ok, st, jid):\n"
             "    t.emit('phase_after', action=it['dedupe_id'], ok=ok, job=jid)\n"),
    "d.py": "raise ImportError('nope')\n",
    "_e.py": "raise RuntimeError('private: never imported')\n",
}


class PhaseTest(Base):
    def setUp(self):
        super().setUp()
        self.d = d = self.tmp / "phases"
        d.mkdir()
        for name, text in PHASES.items():
            (d / name).write_text(text)
        self.enterContext(mock.patch.object(phases, "__path__", [str(d)]))
        self.enterContext(mock.patch.object(sv, "_PHASES", None))
        self.addCleanup(lambda: [sys.modules.pop(k) for k in list(sys.modules) if k.startswith(f"{phases.__name__}.")])

    def test_order_errors_and_harvest(self):
        self.plan("p")
        self.tick(five=None)  # quota unknown: the probe comes after the phases
        types = [(e["type"], e.get("name") or e.get("what")) for e in self.events()
                 if e["type"] in ("phase_ran", "tick_error", "sv_probe", "sv_pjob")]
        self.assertEqual(types, [("tick_error", "d"), ("phase_ran", "a"), ("tick_error", "b"), ("phase_ran", "c"),
                                 ("sv_pjob", None), ("sv_probe", None)])
        self.assertEqual([e["blocked"] for e in self.events("phase_ran")], [False, False])
        self.jobs.finish(0)  # the phase's job (the probe is job 2)
        self.tick(30, five=None)
        self.assertEqual([(e["action"], e["ok"], e["job"]) for e in self.events("phase_after")],
                         [("sv_pjob:k1", True, "j1")])
        self.assertEqual([e["ok"] for e in self.events("sv_pjob", "result")], [True])
        self.assertEqual(len([e for e in self.events("tick_error")]), 2, "each error once")

    def test_a_phase_added_later_waits_for_the_re_exec(self):  # r2: REQ-1, old code goes on as it was
        self.plan("p")
        self.tick(five=None)
        (self.d / "z.py").write_text("def run(t, blocked):\n    t.emit('phase_ran', name='z', blocked=blocked)\n")
        self.tick(30, five=None)
        self.assertEqual([e["name"] for e in self.events("phase_ran")], ["a", "c", "a", "c"])

    def test_a_phase_that_exits_costs_a_tick_error(self):  # r1: sys.exit() in a phase must not end the supervisor
        for p in self.d.glob("*.py"):
            p.unlink()
        (self.d / "x.py").write_text("import sys\nKIND = 'xjob'\n"
                                     "def run(t, blocked):\n    t.start_job(KIND, 'k', ['/usr/bin/true'])\n"
                                     "    sys.exit('run')\n"
                                     "def after(t, it, ok, st, jid):\n    sys.exit('after')\n")
        (self.d / "y.py").write_text("import sys\nsys.exit('import')\n")
        self.plan("p")
        self.tick(five=None)
        self.jobs.finish(0)
        self.tick(30, five=None)
        self.assertEqual([(e["where"], e["what"]) for e in self.events("tick_error")],
                         [("phase_import", "y"), ("phase", "x"), ("harvest", "sv_xjob:k")])

    def test_blocked_under_a_full_block_unless_the_decision_is_being_decided(self):
        self.plan("p")
        self.decision("Q-1", ["p.1"])
        self.tick()
        self.assertEqual([e["blocked"] for e in self.events("phase_ran")], [True, True])
        self.assertEqual(self.jobs.started, [])
        self.decision("Q-1", ["p.1"], state="deciding")
        self.tick(30)
        self.assertEqual([e["blocked"] for e in self.events("phase_ran")][2:], [False, False],
                         "deciding: not waiting on the user")
        self.assertEqual((self.header("p.1")["state"], self.jobs.cmds()), ("planned", [["/usr/bin/true"]]),
                         "still not ready: only the phase's job")
        self.set_state("p.1", "blocked", blocked_reason="pending")  # r1: nor when its header says it waits on one
        self.tick(60)
        self.assertEqual([e["blocked"] for e in self.events("phase_ran")][4:], [False, False])
