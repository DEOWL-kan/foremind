import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from foremind import carriers, cli, fsutil, gate, header, inbox, job, lock, notify, review
from foremind.carriers import Carrier, SessionState
from foremind.events import EventLog
from foremind.lock import ExitEvidence
from foremind.plan import model
from foremind.supervisor import tick as sv
from foremind.vendors import claude

REPO = Path(__file__).resolve().parent.parent
REAL_START, REAL_STATUS = job.start, job.status
M, H = 60, 3600


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


class FakeJobs:
    """job.start / job.status stand-ins: jobs run until the test finishes them."""

    def __init__(self):
        self.started, self.state, self.dirs = [], {}, {}

    def start(self, job_dir, argv, *, cwd, env=None, timeout_s=None):
        jid = f"j{len(self.started) + 1}"
        self.started.append({"id": jid, "argv": list(argv), "env": env or {}, "timeout_s": timeout_s})
        self.state[jid], self.dirs[jid] = {"state": "running"}, Path(job_dir) / jid
        return jid

    def status(self, job_dir, jid):
        return self.state[jid]

    def finish(self, n, code=0, stdout=""):
        jid = self.started[n]["id"]
        self.dirs[jid].mkdir(parents=True, exist_ok=True)
        (self.dirs[jid] / "stdout.log").write_text(stdout)
        self.state[jid] = {"state": "done", "exit_code": code, "timed_out": False}

    def cmds(self):
        """argv after `python -m foremind` for our own commands (a successor job as `_successor`), else the whole
        argv."""
        n, s = len(sv.FM), len(sv.SUCCESSOR)
        return [j["argv"][n:] if j["argv"][:n] == sv.FM
                else ["_successor", *j["argv"][s:]] if j["argv"][:s] == sv.SUCCESSOR else j["argv"]
                for j in self.started]


class FakeCarrier(Carrier):
    name = "fake"

    def __init__(self, root):
        super().__init__(root)
        self.sent, self.closed, self.alive, self.idle, self.evidence = [], [], {}, True, True

    def send(self, session, text):
        self.sent.append((session, text))
        return None

    def read_state(self, session):
        return SessionState(alive=self.alive.get(session, True), idle=self.idle)

    def close(self, session):
        self.closed.append(session)
        return ExitEvidence(session, self.name, "absent") if self.evidence else None

    def list_sessions(self):
        return [s for s, a in self.alive.items() if a]


class Recorder:
    name = "rec"

    def __init__(self):
        self.sent = []

    def send(self, title, body, priority="P1"):
        self.sent.append((title, body, priority))
        return True


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.tmp / "proj"
        (self.root / ".foremind" / "batches").mkdir(parents=True)
        self.cfg_home = self.tmp / "cfg"
        self.cfg_home.mkdir()
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n')
        (self.tmp / "gitconfig").write_text("")
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(self.cfg_home),
                                                       "FOREMIND_WT_ROOT": str(self.tmp / "wt"),
                                                       "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"),
                                                       "GIT_CONFIG_NOSYSTEM": "1"}))
        for k in ("FOREMIND_PROJECT", "FOREMIND_SESSION", "FOREMIND_ROLE", "FOREMIND_BATCH", "GIT_SSH_COMMAND",
                  "GIT_SSH"):
            os.environ.pop(k, None)
        self.enterContext(mock.patch("os.dup2"))  # the commands point fd 0 at /dev/null: not the test runner's
        self.now = time.time()
        self.jobs = FakeJobs()
        self.enterContext(mock.patch.object(job, "start", self.jobs.start))
        self.enterContext(mock.patch.object(job, "status", self.jobs.status))
        self.carrier = FakeCarrier(self.root)
        self.enterContext(mock.patch.object(carriers, "get", lambda kind, root, cfg=None: self.carrier))
        self.rec = Recorder()
        self.enterContext(mock.patch.object(notify, "get", lambda root, cfg: self.rec))
        self.enterContext(mock.patch.object(review, "TIMEOUT_S", review.TIMEOUT_S))  # the commands set it

    def user_config(self, text):
        (self.cfg_home / "config.toml").write_text(text)

    @property
    def log(self):
        return EventLog(self.root / ".foremind" / "events.jsonl")

    def events(self, type=None, phase=None):
        return [e for e in self.log.iter() if (type is None or e["type"] == type)
                and (phase is None or e["phase"] == phase)]

    def telemetry(self, five, ts, *, seven=None):
        p = self.root / ".foremind" / "telemetry" / "s.statusline.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"ts": iso(ts), "rate_limits": {
            "five_hour": None if five is None else {"used_percentage": five, "resets_at": self.now + 3 * H},
            **({} if seven is None else {"seven_day": seven})}}))

    def plan(self, pid, *batches):
        docs = {}
        for i, kw in enumerate(batches or ({},), 1):
            bid = f"{pid}.{i}"
            docs[bid] = model.Doc({
                "id": bid, "plan_id": pid, "reqs": ["REQ-1"], "repos": ["main"], "owns_paths": [f"main:{pid}/{i}"],
                "reads": [], "depends_on": [], "merge_after": [], "start_commands": [], "accept_commands": ["true"],
                "tiers": {"difficulty": "S", "org": "exec_review", "review": "zero_context", "model": "claude-opus-5-5",
                          "effort": "medium", "reason": "t"},
                "mode": "auto", "hard_block": [], "budget_estimate": "1000", "must_read": [], "tools": [],
                "state": "planned", **kw}, "## 状态\n")
        p = model.Plan(pid, model.Doc({"plan_id": pid, "goal_hash": "0" * 64, "approved_at": "2026-09-25T00:00:00+00:00",
                                       "approved_by": "user", "batches": list(docs), "revisions": []}, "plan\n"), docs)
        model.write(self.root, p)
        self.log.append("plan_approved", plan=pid, plan_hash=model.plan_hash(p))

    def hpath(self, bid):
        return self.root / ".foremind" / "batches" / f"{bid}.md"

    def header(self, bid):
        return header.parse(self.hpath(bid).read_text())[0]

    def set_state(self, bid, state, **extra):
        h, body = header.parse(self.hpath(bid).read_text())
        h.update(state=state, **extra)
        self.hpath(bid).write_text(header.render(h, body))

    def hold(self, bid, session, state="running", **beat):
        """What a seat job leaves behind: state, lock, seat_opened, a first heartbeat."""
        self.set_state(bid, state)
        lock.acquire(self.root, bid, session)
        self.log.append("seat_opened", batch=bid, session=session, successor=False, carrier="fake", worktrees={})
        self.beat(session, bid, self.now, **beat)

    def beat(self, session, bid, ts, **kw):
        p = self.root / ".foremind" / "heartbeats" / f"{session}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"session": session, "batch": bid, "ts": iso(ts), "tool_open": False,
                                 "handoff_requested": False, **kw}))

    def decision(self, qid, blocks, state="open"):
        p = self.root / ".foremind" / "decisions" / f"{qid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"id": qid, "blocks": blocks, "state": state}))

    def tick(self, at=0, *, five=10):
        """One pass at now + at; `five`: a fresh 5h reading (None: leave telemetry alone)."""
        if five is not None:
            self.telemetry(five, self.now + at)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = sv.tick(self.root, now=self.now + at)
        self.assertEqual(code, 0)
        return out.getvalue()

    def pending(self, session):
        return inbox.pending_messages(session, root=self.root)


class LockAndSwitchesTest(Base):
    def test_global_lock_held_by_another_process_skips_the_pass(self):
        self.plan("p", {"mode": "watch"})  # becomes ready, never opened automatically
        env = {**os.environ, "FOREMIND_PROJECT": str(self.root), "PYTHONPATH": str(REPO)}
        argv = [sys.executable, "-m", "foremind", "tick"]
        before = len(self.events())
        with fsutil.global_lock():
            r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual((r.returncode, len(r.stdout.splitlines())), (0, 1), r.stderr)
        self.assertIn("skipped", r.stdout)
        self.assertEqual((len(self.events()), self.header("p.1")["state"]), (before, "planned"))
        r = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.header("p.1")["state"], "ready")

    def test_ssh_command_keeps_the_users_own(self):  # S-6
        from foremind.commands import supervise
        self.assertEqual(supervise._ssh_command(), "ssh -o BatchMode=yes")
        os.environ["GIT_SSH"] = "/opt/my ssh"
        self.assertEqual(supervise._ssh_command(), "'/opt/my ssh' -o BatchMode=yes")
        (self.tmp / "gitconfig.inc").write_text("[core]\n\tsshCommand = ssh -i ~/.ssh/work\n")
        (self.tmp / "gitconfig").write_text(f"[include]\n\tpath = {self.tmp / 'gitconfig.inc'}\n")
        self.assertEqual(supervise._ssh_command(), "ssh -i ~/.ssh/work -o BatchMode=yes", "includes are followed")
        os.environ["GIT_SSH_COMMAND"] = "ssh -p 2222"
        self.assertEqual(supervise._ssh_command(), "ssh -p 2222 -o BatchMode=yes")

    def test_successor_job_runs_this_module_outside_the_command_list(self):
        env = {**os.environ, "FOREMIND_PROJECT": str(self.root), "PYTHONPATH": str(REPO)}
        r = subprocess.run([sys.executable, "-W", "error", *sv.SUCCESSOR[1:], "p.9"], env=env, capture_output=True,
                           text=True, timeout=120)
        self.assertEqual((r.returncode, r.stdout), (1, ""), r.stderr)
        self.assertIn("foremind successor: no batch header", r.stderr)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.main(["_successor", "p.9"])
        self.assertNotIn("_successor", err.getvalue().split("choose from")[1], "not a `foremind` command")

    def test_jobs_are_not_shadowed_by_a_foremind_in_the_project_root(self):  # jobs run there; §20 I53①
        self.assertEqual((sv.FM[1], sv.SUCCESSOR[1], sv.PKG_PARENT), ("-P", "-P", claude._PKG_PARENT))
        (self.root / "foremind").mkdir()
        (self.root / "foremind" / "__init__.py").write_text("raise ImportError('shadowed')\n")
        r = subprocess.run(sv.fm("version"), cwd=self.root, env={**os.environ, "PYTHONPATH": str(sv.PKG_PARENT)},
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stop_file_and_pause(self):
        self.plan("p")
        (self.root / "STOP").write_text("")
        self.assertIn("STOP", self.tick())
        self.assertEqual((self.header("p.1")["state"], self.jobs.started), ("planned", []))
        (self.root / "STOP").unlink()
        os.environ["FOREMIND_PROJECT"] = str(self.root)  # undone by setUp's patch.dict
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["pause"]), 0)
        self.assertIn("paused", self.tick())
        self.assertEqual((self.header("p.1")["state"], self.jobs.started), ("planned", []))
        self.assertEqual(self.events()[-1]["type"], "paused")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["resume"]), 0)
        self.telemetry(10, self.now)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.main(["run", "p.9"]), 1)
            self.assertEqual(cli.main(["run", "p.1"]), 0)
        self.assertIn("p.9", err.getvalue())
        self.assertEqual(self.events("run_requested")[-1]["batches"], ["p.1"])
        self.assertEqual((self.header("p.1")["state"], self.jobs.cmds()), ("ready", [["seat", "p.1"]]))


class OnceTest(Base):
    def test_seat_is_opened_once(self):
        self.plan("p")
        self.tick()
        self.assertEqual(self.header("p.1")["state"], "ready")
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"]])
        env = self.jobs.started[0]["env"]
        self.assertEqual((env["FOREMIND_PROJECT"], env["FOREMIND_SESSION"], env["FOREMIND_BATCH"]),
                         (str(self.root), "", ""), "no identity inherited from the caller's session")
        self.assertGreater(self.jobs.started[0]["timeout_s"], 600 + 600, "SF-8: verify + manual start + slack")
        self.tick(30)
        self.tick(60)
        self.assertEqual(len(self.jobs.started), 1, "job still running")
        self.hold("p.1", "fm-p-1")  # what `foremind seat` did
        self.jobs.finish(0)
        self.tick(90)
        self.tick(120)
        self.assertEqual(len(self.jobs.started), 1)
        self.assertEqual([e["ok"] for e in self.events("sv_seat", "result")], [True])

    def test_failed_seat_is_retried_then_left_to_the_user_with_one_notice(self):
        self.plan("p")
        self.tick()
        self.jobs.finish(0, code=1)
        self.tick(30)
        self.assertEqual(len(self.jobs.started), 2, "one retry")
        self.jobs.finish(1, code=1)
        self.tick(60)
        self.tick(90)
        self.assertEqual(len(self.jobs.started), 2)
        self.assertEqual(len([s for s in self.rec.sent if "开席失败" in s[0]]), 1)
        sv.request_run(self.root, ["p.1"])
        self.tick(120)
        self.assertEqual(self.jobs.cmds()[-1], ["seat", "p.1"])

    def test_inbox_is_delivered_once(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        self.carrier.idle = False
        self.tick()
        self.assertEqual(self.carrier.sent, [], "busy: left to the Stop hook")
        self.carrier.idle = True
        self.tick(30)
        self.tick(60)
        self.assertEqual(len(self.carrier.sent), 1)
        self.assertIn("［user］hello", self.carrier.sent[0][1])
        self.assertEqual(self.pending("fm-s"), [])
        self.assertEqual(len(self.events("program_delivery", "intent")), 1)

    def test_run_request_ends_with_the_seat_it_opened(self):
        self.plan("p", {"mode": "watch"})
        sv.request_run(self.root, ["p.1"])
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"]])
        self.hold("p.1", "fm-s")
        self.jobs.finish(0)
        self.tick(30)
        lock.release(self.root, "p.1", "fm-s")
        self.set_state("p.1", "ready")  # back to ready later (failed -> ready)
        self.tick(60)
        self.assertEqual(len(self.jobs.started), 1, "watch mode again: presence or a new `foremind run`")

    def test_no_seat_on_a_plan_changed_since_approval(self):  # SF-9
        self.plan("p", {"state": "ready"}, {})
        self.hold("p.2", "fm-b")  # keeps it from being a full block
        self.log.append("plan_approved", plan="p", plan_hash="0" * 64)  # what is on disk is no longer approved
        self.tick()
        self.assertEqual(self.jobs.started, [])

    def test_no_delivery_while_quota_is_unknown(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        self.tick(five=None)
        self.assertEqual((self.carrier.sent, self.jobs.cmds()), ([], [["/usr/bin/true"]]), "a probe first")

    def test_the_session_of_a_merged_batch_is_closed_with_backoff(self):  # S-4
        self.plan("p")
        self.hold("p.1", "fm-s")
        self.set_state("p.1", "merged")
        self.carrier.evidence = False
        for at in (0, 30, 61, 2 * M, 3 * M + 2):
            self.tick(at)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"] * 3, "fm-s"),
                         "no evidence: retried after 1 and 2 minutes, the lock stays")
        for at in (8, 16, 32, 64):  # tries 4 to 7 (waits of 4, 8, 16, then 30 minutes)
            self.tick(at * M)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 旧会话未确认退出"],
                         "down to one try per 30 minutes: the user is told once")
        self.assertIn("foremind confirm-exit p.1", self.rec.sent[0][1])
        self.carrier.evidence = True
        self.tick(95 * M)
        self.assertEqual((len(self.carrier.closed), lock.holder(self.root, "p.1")), (8, None))

    def test_the_session_of_a_merged_batch_under_manual_is_asked_once(self):  # S-4
        self.plan("p")
        self.hold("p.1", "fm-s")
        self.set_state("p.1", "merged")
        self.carrier.evidence, self.carrier.name = False, "manual"
        self.tick()
        self.tick(30 * M)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], "fm-s"))
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 旧会话未确认退出"])

    def test_watch_mode_is_left_to_the_stop_hook(self):
        self.plan("p", {"mode": "watch"})
        self.hold("p.1", "fm-s")
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        self.tick()
        self.assertEqual(self.carrier.sent, [])

    def test_reviewer_is_woken_once(self):
        self.plan("p", {"state": "review_ready"})
        calls = []

        def start(root, bid, cfg, **kw):
            calls.append(bid)
            review.set_state(root, bid, "in_review", expect=("review_ready",))
            review.events(root).append("review_started", phase="intent", dedupe_id=f"review:{bid}:rev-1", batch=bid,
                                       reviewer_session="rev-1", heads={"main": "a" * 40})
            return "rev-1"

        with mock.patch.object(review, "start", start), mock.patch.object(review, "harvest", return_value=None) as hv:
            self.tick()
            self.tick(30)
        self.assertEqual(calls, ["p.1"])
        self.assertEqual(hv.call_args.args[1:3], ("p.1", "rev-1"), "the running reviewer is harvested")

    def test_gate_runs_once_after_an_approved_receipt_and_reports_failures_once(self):
        self.plan("p", {"state": "in_review"})
        lock.acquire(self.root, "p.1", "fm-s")
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.carrier.idle = False
        self.tick()
        self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]])
        self.assertEqual(self.jobs.started[0]["env"]["FOREMIND_ROLE"], "supervisor")
        self.jobs.finish(0, code=1, stdout="ok   receipt\nFAIL acceptance: failed: ['true']\n")
        self.tick(60)
        self.tick(90)
        msgs = self.pending("fm-s")
        self.assertEqual(len(msgs), 1)
        self.assertIn("FAIL acceptance", msgs[0].text)
        self.assertEqual(len(self.jobs.started), 1, "no rerun for the same receipt")

    def test_gate_error_is_retried_after_the_retry_interval(self):
        self.plan("p", {"state": "in_review"})
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.tick()
        self.jobs.finish(0, code=2)  # e.g. another gate run held the batch
        self.tick(60)
        self.assertEqual(len(self.jobs.started), 1)
        self.tick(10 * M + 1)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]] * 2)

    def test_reviewer_that_cannot_start_tells_the_seat_once(self):
        self.plan("p", {"state": "review_ready"})
        lock.acquire(self.root, "p.1", "fm-s")
        self.beat("fm-s", "p.1", self.now)
        self.carrier.idle = False
        with mock.patch.object(review, "start", side_effect=review.FlowError("heads moved; run `foremind review`")):
            self.tick()
            self.tick(30)
        msgs = self.pending("fm-s")
        self.assertEqual(len(msgs), 1)
        self.assertIn("heads moved", msgs[0].text)

    def test_changes_requested_goes_to_the_seat_once_and_the_seat_is_watched_again(self):
        self.plan("p", {"state": "changes_requested"})
        lock.acquire(self.root, "p.1", "fm-s")
        self.beat("fm-s", "p.1", self.now)
        (self.root / ".foremind" / "batches" / "p.1.review.r2.json").write_text(json.dumps({
            "verdict": "changes_requested", "issues": [{"severity": "must_fix", "location": "a.py:3",
                                                        "summary": "off by one"}]}))
        self.carrier.idle = False
        self.tick()
        self.tick(30)
        msgs = self.pending("fm-s")
        self.assertEqual(len(msgs), 1)
        self.assertIn("第 2 轮", msgs[0].text)
        self.assertIn("a.py:3", msgs[0].text)
        self.assertEqual(self.header("p.1")["state"], "running", "SF-1: fixing is running")
        self.carrier.alive["fm-s"] = False  # the seat dies while fixing
        self.tick(60)
        self.assertEqual(self.header("p.1")["state"], "stuck")
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_reconcile_catches_up_merged_and_flags_a_skipped_gate(self):
        self.plan("p", {"state": "delivered"}, {"state": "approved"}, {"state": "running"}, {"state": "planned"},
                  {"state": "in_review"})
        with mock.patch.object(gate, "batch_merged", side_effect=lambda root, bid, cfg: bid != "p.5") as merged:
            self.tick()
            self.tick(30)
            self.assertEqual(sorted(c.args[1] for c in merged.call_args_list), ["p.1", "p.2", "p.3", "p.5"],
                             "started batches only, each once within supervisor.merged_check_min (fetch, gh)")
            self.tick(5 * M + 1)
            self.assertEqual([c.args[1] for c in merged.call_args_list][4:], ["p.5"], "then again")
        self.assertEqual([self.header(f"p.{i}")["state"] for i in (1, 2, 3, 4, 5)],
                         ["merged"] * 3 + ["ready", "in_review"])
        rec = {e["batch"]: e["skipped"] for e in self.events("reconciled")}
        self.assertEqual(rec, {"p.1": [], "p.2": ["awaiting_audit", "delivered"], "p.3": [
            "review_ready", "in_review", "changes_requested", "approved", "awaiting_audit", "delivered"]})
        self.assertEqual(sorted(e["batch"] for e in self.events("l0_hard_failure")), ["p.2", "p.3"])
        self.assertEqual(sorted((t, p) for t, _, p in self.rec.sent if "未经门禁" in t),
                         [("p.2 未经门禁已合入", "P0"), ("p.3 未经门禁已合入", "P0")])

    def test_delivered_merge_dev_is_retried_and_is_not_waiting_on_the_user(self):  # SF-7
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[delivery]\nlevel = "merge_dev"\n')
        self.plan("p", {"state": "delivered"})
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved"}))
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]])
        self.jobs.finish(0, code=2)
        self.tick(60)
        self.tick(10 * M + 1)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]] * 2)
        self.assertEqual(self.rec.sent, [], "not a full block: the supervisor itself merges it")
        self.jobs.finish(1, code=1)
        for at in (11, 21, 31):
            self.tick(at * M)
        self.assertEqual(len(self.jobs.started), 2, "retries used up (supervisor.seat_retries)")
        self.assertEqual([t for t, _, _ in self.rec.sent if "合入" in t], ["p.1 合入失败"])
        sv.request_run(self.root, ["p.1"])
        self.tick(32 * M)
        self.assertEqual(self.jobs.cmds(), [["gate", "p.1"]] * 3, "`foremind run` starts over")


class RecoveryTest(Base):
    """An intent without a result (a crash in between) is settled by the facts on the next tick, not redone."""

    def test_delivery_sent_before_the_crash_is_marked_not_resent(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        inbox.append("fm-s", "hi", sender="user", root=self.root)
        end = self.pending("fm-s")[-1].end
        self.log.append("sv_deliver", phase="intent", dedupe_id=f"sv_deliver:fm-s:{end}:1", session="fm-s", upto=end)
        self.log.append("program_delivery", phase="intent", dedupe_id="pd-1", session="fm-s")
        self.log.append("program_delivery", dedupe_id="pd-1", session="fm-s", ok=True)
        self.tick()
        self.assertEqual((self.carrier.sent, self.pending("fm-s")), ([], []))
        r = self.events("sv_deliver", "result")
        self.assertEqual([(e["ok"], e["recovered"]) for e in r], [(True, True)])

    def test_delivery_that_never_went_out_is_recorded_as_such(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        inbox.append("fm-s", "hi", sender="user", root=self.root)
        end = self.pending("fm-s")[-1].end
        self.log.append("sv_deliver", phase="intent", dedupe_id=f"sv_deliver:fm-s:{end}:1", session="fm-s", upto=end)
        self.tick()
        self.assertEqual([(e.get("ok"), e.get("recovered")) for e in self.events("sv_deliver", "result")],
                         [(False, True), (True, None)], "settled as not sent, then its first real delivery")
        self.assertEqual(len(self.carrier.sent), 1)

    def test_notification_cut_short_is_never_resent_and_listed_as_maybe_unsent(self):
        self.log.append("notify", phase="intent", dedupe_id="notify:k", key="k", title="p.1 卡住")
        self.tick(five=None)
        self.assertEqual([e.get("sent", "?") for e in self.events("notify", "result")], [None])
        self.assertEqual([(e["key"], e["title"], e["unknown"]) for e in self.events("notify_unsent")],
                         [("k", "p.1 卡住", True)], "SF-11: the morning report sees it")
        self.assertIsNone(notify.notify(self.root, {}, "k", "t", "b"))
        self.assertEqual(self.rec.sent, [])

    def test_a_corrupt_inbox_costs_its_intent_a_tick_error_not_the_pass(self):  # SF-3
        self.plan("p")
        self.log.append("sv_say", phase="intent", dedupe_id="sv_say:k", session="fm-x", token="t0k", text="hello")
        (self.root / ".foremind" / "inbox").mkdir(parents=True)
        (self.root / ".foremind" / "inbox" / "fm-x.md").write_text("garbage\nnot a message\n")
        self.tick()
        self.assertEqual([e["where"] for e in self.events("tick_error")], ["recover"])
        self.assertEqual((self.header("p.1")["state"], self.jobs.cmds()), ("ready", [["seat", "p.1"]]),
                         "the rest of the pass went on")

    def test_a_seat_job_ending_after_the_pass_read_the_events_is_not_taken_back(self):  # MF-2
        self.plan("p")
        self.tick()
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"]])
        self.log.append("seat_open", phase="intent", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s")
        lock.acquire(self.root, "p.1", "fm-s")

        def reconcile(t):  # the seat job kicks off and exits after load() read the events
            self.hold("p.1", "fm-s")
            self.log.append("seat_open", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s", ok=True)
            self.jobs.finish(0)

        with mock.patch.object(sv.Tick, "reconcile", reconcile):
            self.tick(30)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], "fm-s"))
        self.assertEqual([e.get("recovered") for e in self.events("seat_open", "result")], [None])

    def seat_job_died(self, session):
        self.log.append("sv_seat", phase="intent", dedupe_id="sv_seat:p.1:1", batch="p.1", at=self.now)
        self.log.append("sv_job", dedupe_id="sv_job:sv_seat:p.1:1", action="sv_seat:p.1:1", job="j0")
        self.jobs.state["j0"] = {"state": "lost"}
        self.log.append("seat_open", phase="intent", dedupe_id=f"seat_open:{session}", batch="p.1", session=session)

    def test_seat_open_that_did_kick_off_is_recorded_ok_without_reopening(self):
        self.plan("p", {"state": "ready"})
        self.seat_job_died("fm-s")
        self.hold("p.1", "fm-s")  # the kickoff went out before the crash
        self.tick()
        self.assertEqual([(e["ok"], e["recovered"]) for e in self.events("seat_open", "result")], [(True, True)])
        self.assertEqual((self.jobs.started, self.carrier.closed), ([], []))

    def test_seat_open_without_kickoff_is_closed_and_its_claim_given_back(self):
        self.plan("p", {"state": "ready"})
        self.seat_job_died("fm-s")
        lock.acquire(self.root, "p.1", "fm-s")  # claimed, never kicked off
        self.tick()
        self.assertEqual(self.carrier.closed, ["fm-s"])
        self.assertEqual([e["ok"] for e in self.events("seat_open", "result")], [False])
        self.assertEqual(lock.holder(self.root, "p.1"), None)
        self.assertEqual(self.jobs.cmds(), [["seat", "p.1"]], "a fresh attempt, the old session closed first")

    def test_a_seat_open_of_no_job_is_settled_after_the_seat_time_limit(self):  # S-1
        self.plan("p", {"state": "ready", "mode": "watch"})  # e.g. `foremind seat p.1` in a terminal since closed
        self.log.append("seat_open", phase="intent", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s")
        lock.acquire(self.root, "p.1", "fm-s")
        self.tick(30 * M)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], "fm-s"), "may still be opening")
        self.tick(36 * M)  # 10 + 10 + 15 minutes
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.assertEqual([(e["ok"], e["recovered"]) for e in self.events("seat_open", "result")], [(False, True)])

    def test_a_seat_job_is_harvested_only_after_its_open_is_settled(self):  # S-2
        self.plan("p", {"state": "ready"})
        self.seat_job_died("fm-s")
        lock.acquire(self.root, "p.1", "fm-s")
        with mock.patch.object(lock, "break_lock", side_effect=OSError("disk")):
            self.tick()
        self.assertEqual((self.events("sv_seat", "result"), [e["where"] for e in self.events("tick_error")]),
                         ([], ["harvest"]))
        self.tick(30)
        self.assertEqual([e["ok"] for e in self.events("seat_open", "result")], [False])
        self.assertEqual([e["ok"] for e in self.events("sv_seat", "result")], [False])
        self.assertIsNone(lock.holder(self.root, "p.1"))

    def test_an_unexpected_error_settling_a_job_skips_only_that_job(self):  # note 1
        self.plan("p", {"state": "ready"})
        self.plan("q", {"mode": "watch"})
        self.seat_job_died("fm-s")
        lock.acquire(self.root, "p.1", "fm-s")
        with mock.patch.object(lock, "break_lock", side_effect=RuntimeError("bug")):
            self.tick()
        self.assertEqual((self.events("sv_seat", "result"), [e["where"] for e in self.events("tick_error")]),
                         ([], ["harvest"]))
        self.assertEqual(self.header("q.1")["state"], "ready", "the rest of the pass went on")
        self.tick(30)
        self.assertEqual([e["ok"] for e in self.events("sv_seat", "result")], [False])

    def test_an_interrupted_seat_claim_under_manual_is_told_once(self):  # S-A
        self.plan("p", {"state": "ready", "mode": "watch"})
        self.plan("q")
        self.hold("q.1", "fm-q")  # another batch moves on: no full block summary
        self.log.append("seat_open", phase="intent", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s")
        lock.acquire(self.root, "p.1", "fm-s")
        self.carrier.evidence, self.carrier.name = False, "manual"
        for at in (36, 40, 50):
            self.beat("fm-q", "q.1", self.now + at * M)
            self.tick(at * M)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], "fm-s"))
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 开席中断"])
        self.assertIn("foremind confirm-exit p.1", self.rec.sent[0][1])

    def test_a_job_of_a_batch_whose_plan_does_not_read_waits(self):  # S-2
        self.plan("p", {"state": "ready"})
        self.seat_job_died("fm-s")
        with mock.patch.object(model, "load", side_effect=model.PlanError("broken")):
            self.tick()
        self.assertEqual(self.events("sv_seat", "result"), [])
        self.tick(30)
        self.assertEqual([e["ok"] for e in self.events("sv_seat", "result")], [False])

    def test_running_without_a_holder_gets_a_successor_once(self):
        self.plan("p", {"state": "running"})
        self.log.append("seat_open", phase="intent", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s")
        self.log.append("seat_open", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s", ok=False)
        self.tick()
        self.tick(30)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_inbox_message_written_before_the_crash_is_not_written_twice(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        self.log.append("sv_say", phase="intent", dedupe_id="sv_say:k", session="fm-s", token="t0k", text="hello")
        inbox.append("fm-s", "hello", sender="supervisor#t0k", root=self.root)
        self.carrier.idle = False
        self.tick()
        self.assertEqual(len(self.pending("fm-s")), 1)
        self.assertEqual([e["appended"] for e in self.events("sv_say", "result")], [False])


class QuotaTickTest(Base):
    def test_unknown_quota_probes_first_then_opens(self):
        self.plan("p")
        self.tick(five=None)  # no telemetry at all
        self.assertEqual(self.jobs.cmds(), [["/usr/bin/true"]], "a probe, no seat")
        self.tick(30, five=None)
        self.assertEqual(len(self.jobs.started), 1, "probe still running")
        self.jobs.finish(0)
        self.tick(60, five=None)
        self.assertEqual(self.jobs.cmds(), [["/usr/bin/true"], ["seat", "p.1"]])

    def test_no_probe_on_an_excluded_model(self):
        self.user_config('[exclude]\nmodels = ["claude-opus-*"]\n')  # the default probe: the reviewer's model
        self.plan("p")
        self.tick(five=None)
        self.assertEqual(self.jobs.started, [])
        self.assertEqual([(e["where"], e["what"]) for e in self.events("tick_error")], [("probe", "model")])

    def test_failed_probe_backs_off(self):
        self.plan("p")
        self.tick(five=None)
        self.jobs.finish(0, code=1)
        self.tick(60, five=None)
        self.tick(10 * M, five=None)
        self.assertEqual(len(self.jobs.started), 1)
        self.tick(16 * M + 60, five=None)
        self.assertEqual(self.jobs.cmds(), [["/usr/bin/true"]] * 2)

    def test_low_quota_asks_seats_to_hand_off_once_and_opens_only_requested_seats(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_seats = 4\n')  # low: 2
        self.plan("p", {}, {})
        self.hold("p.1", "fm-s")
        self.carrier.idle = False
        self.tick(five=87)
        self.tick(30, five=88)
        self.assertEqual(len([e for e in self.events("sv_say", "result") if "quota_low" in e["dedupe_id"]]), 1)
        self.assertEqual((self.header("p.2")["state"], self.jobs.started), ("ready", []))
        sv.request_run(self.root, ["p.2"])
        self.tick(60, five=88)
        self.assertEqual(self.jobs.cmds(), [["seat", "p.2"]])

    def test_exhaustion_pauses_then_continue_goes_out_one_session_per_tick(self):
        self.plan("p", {}, {})
        self.hold("p.1", "fm-a")
        self.hold("p.2", "fm-b")
        self.carrier.idle = False
        self.tick(five=95)
        q = json.loads((self.root / ".foremind" / "quota.json").read_text())
        self.assertEqual((q["groups"]["claude/long"]["state"], q["paused"]), ("exhausted", ["fm-a", "fm-b"]))
        self.assertEqual(q["groups"]["claude/oneshot"]["state"], "exhausted")
        self.tick(2 * H, five=None)
        self.assertEqual(self.jobs.started, [], "exhausted: nothing, not even a probe")
        self.tick(3 * H + 1, five=None)  # the 5h window resets: unknown, and a probe for the paused sessions
        self.assertEqual(self.jobs.cmds(), [["/usr/bin/true"]])
        self.jobs.finish(0)
        self.tick(3 * H + 60, five=None)
        self.tick(3 * H + 90, five=None)
        said = [(m.text, m.sender.startswith("supervisor#")) for s in ("fm-a", "fm-b") for m in self.pending(s)]
        self.assertEqual(said, [(sv.RESUME_TEXT, True)] * 2)
        resumes = [e["session"] for e in self.events("sv_say", "result") if "resume:" in e["dedupe_id"]]
        self.assertEqual(resumes, ["fm-a", "fm-b"], "in priority order, one per tick")


class StuckTickTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.carrier.idle = False  # keep the supervisor's messages in the inbox

    def test_remind_ask_then_stuck_and_take_over(self):
        self.hold("p.1", "fm-s")
        self.tick(19 * M)
        self.assertEqual(self.pending("fm-s"), [])
        self.tick(21 * M)
        self.tick(30 * M)
        self.assertEqual(len(self.pending("fm-s")), 1)
        self.tick(41 * M)
        self.assertEqual(len(self.pending("fm-s")), 2)
        self.assertIn("foremind log", self.pending("fm-s")[1].text)
        self.tick(62 * M)
        self.assertEqual(self.header("p.1")["state"], "stuck")
        self.assertEqual(self.header("p.1")["state_prior"], "running")
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 卡住"])
        self.tick(63 * M)
        self.assertEqual(len(self.jobs.started), 1)
        # MF-1: the successor is open (running again) but has not run `handoff --accept`: no second one
        self.set_state("p.1", "running")
        self.log.append("seat_opened", batch="p.1", session="fm-s2", successor=True, predecessor=None, worktrees={})
        self.beat("fm-s2", "p.1", self.now + 63 * M)
        self.jobs.finish(0)
        self.tick(64 * M)
        self.tick(70 * M)
        self.assertEqual(len(self.jobs.started), 1, "one successor at a time")
        # it never accepts: watched by its heartbeat like any seat, closed with evidence before the next one
        for at in (84, 104):
            self.tick(at * M)
        self.assertEqual((len(self.pending("fm-s2")), len(self.jobs.started)), (2, 1))
        self.tick(125 * M)
        self.assertEqual(self.carrier.closed, ["fm-s", "fm-s2"])
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]] * 2)

    def test_no_exit_evidence_no_lock_break_until_the_close_is_confirmed(self):
        self.hold("p.1", "fm-s")
        self.carrier.evidence = False
        for at in (21, 41, 62, 63):
            self.tick(at * M)
        self.assertEqual((self.header("p.1")["state"], lock.holder(self.root, "p.1")), ("stuck", "fm-s"))
        self.assertEqual(self.jobs.started, [])
        stuck_notes = [b for t, b, _ in self.rec.sent if t.endswith("卡住")]
        self.assertEqual(len(stuck_notes), 1)
        self.assertIn("foremind confirm-exit p.1", stuck_notes[0])
        self.assertEqual(self.carrier.closed, ["fm-s", "fm-s"], "SF-2: tried again after a minute")
        self.tick(64 * M)
        self.assertEqual(len(self.carrier.closed), 2, "then after two")
        self.carrier.evidence = True  # e.g. claude took its time to exit
        self.tick(65 * M)
        self.assertEqual((len(self.carrier.closed), lock.holder(self.root, "p.1")), (3, None))
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_manual_carrier_waits_for_confirm_exit(self):  # §20 I52③
        self.hold("p.1", "fm-s")
        self.carrier.evidence, self.carrier.name = False, "manual"
        for at in (21, 41, 62, 70, 90):
            self.tick(at * M)
        self.assertEqual((self.carrier.closed, self.jobs.started), (["fm-s"], []), "manual: asked once")
        self.assertIn("p.1：卡住，需确认旧会话已退出（foremind confirm-exit p.1）", self.rec.sent[-1][1],
                      "the only batch waits on the user")
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        self.telemetry(10, time.time())
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-q"}):
                self.assertEqual(cli.main(["confirm-exit", "p.1"]), 1, "M-1: not from inside a session")
            self.assertEqual(self.events("exit_confirmed"), [])
            self.assertIn("only the user", err.getvalue())
            self.assertEqual(cli.main(["confirm-exit", "p.1"]), 0)
            self.assertEqual(cli.main(["confirm-exit", "p.1"]), 1, "nothing left to confirm")
        self.assertIn("confirming that fm-s has exited", out.getvalue().splitlines()[0])
        self.assertIn("exit of fm-s confirmed", out.getvalue())
        self.assertEqual([e["how"] for e in self.events("lock_broken")], ["user_confirmed"])
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_a_successor_left_behind_by_a_user_claim_is_not_watched_but_closed(self):  # S-3
        self.plan("q")
        self.hold("q.1", "fm-q")  # keeps it from being a full block (stuck.py runs)
        self.set_state("p.1", "running")
        self.log.append("seat_opened", batch="p.1", session="fm-s2", successor=True, predecessor=None, worktrees={})
        self.beat("fm-s2", "p.1", self.now)
        lock.acquire(self.root, "p.1", lock.USER)  # `seat --user` while fm-s2 had not accepted
        self.carrier.evidence = False
        for at in (21, 41, 62):
            self.tick(at * M)
            self.beat("fm-q", "q.1", self.now + at * M)
        self.assertEqual((self.header("p.1")["state"], self.pending("fm-s2")), ("running", []))
        self.assertEqual(self.carrier.closed, ["fm-s2"] * 3, "an orphan: closed, retried with backoff")
        self.assertEqual(lock.holder(self.root, "p.1"), lock.USER)

    def test_busy_tool_gets_reminders_only_for_two_hours(self):
        self.hold("p.1", "fm-s", tool_open=True)
        for at in (21, 45, 90, 119):
            self.tick(at * M)
        self.assertEqual(len(self.pending("fm-s")), 1)
        self.tick(121 * M)
        self.assertEqual(len(self.pending("fm-s")), 2)
        self.assertEqual(self.header("p.1")["state"], "running")

    def test_a_session_gone_from_its_carrier_is_taken_over_at_once(self):
        self.hold("p.1", "fm-s")
        self.carrier.alive["fm-s"] = False
        self.tick(60)
        self.assertEqual(self.header("p.1")["state"], "stuck")
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])

    def test_waiting_on_a_decision_is_not_stuck(self):
        self.hold("p.1", "fm-s")
        self.decision("Q-1", ["p.1"])
        for at in (21, 41, 62):
            self.tick(at * M)
        self.assertEqual((self.pending("fm-s"), self.header("p.1")["state"]), ([], "running"))

    def test_context_handoff_opens_one_successor_then_closes_the_predecessor(self):
        self.hold("p.1", "fm-s", handoff_requested=True)
        self.log.append("handoff_written", batch="p.1", session="fm-s")
        self.tick(60)
        self.tick(90)
        self.assertEqual(self.jobs.cmds(), [["_successor", "p.1"]])
        self.log.append("seat_opened", batch="p.1", session="fm-s2", successor=True, predecessor="fm-s", worktrees={})
        self.jobs.finish(0)
        self.tick(120)
        self.assertEqual((len(self.jobs.started), self.carrier.closed), (1, []), "old session holds until accept")
        lock.transfer(self.root, "p.1", "fm-s", "fm-s2")  # handoff --accept
        self.carrier.evidence = False
        for at in (150, 180, 211):
            self.beat("fm-s2", "p.1", self.now + at)
            self.tick(at)
        self.assertEqual(self.carrier.closed, ["fm-s"] * 2, "no evidence: retried after a minute")
        self.carrier.evidence = True
        for at in (340, 600):
            self.beat("fm-s2", "p.1", self.now + at)
            self.tick(at)
        self.assertEqual(self.carrier.closed, ["fm-s"] * 3, "closed for good: not tried again")


class FullBlockTest(Base):
    def test_full_block_starts_no_model_and_sends_one_summary(self):
        self.user_config("")  # the default probe: `claude`, faked below
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        calls = self.tmp / "claude.calls"
        (bin_ / "claude").write_text(f"#!/bin/sh\necho called >> {calls}\n")
        (bin_ / "claude").chmod(0o755)
        os.environ["PATH"] = f"{bin_}{os.pathsep}{os.environ['PATH']}"
        # p.4 waits on Q-1 in review_ready: without the full block its reviewer would want a model (and so a probe)
        self.plan("p", {"state": "failed"}, {"depends_on": ["p.1"]}, {}, {"state": "review_ready"})
        self.decision("Q-1", ["p.3", "p.4"])
        with mock.patch.object(job, "start", REAL_START), mock.patch.object(job, "status", REAL_STATUS):
            for at in (0, 30, 60):
                self.tick(at, five=None)  # quota unknown: a probe would call claude
            self.assertFalse(calls.exists())
            self.assertFalse((self.root / ".foremind" / "jobs").exists(), "no job of any kind")
            self.assertEqual([t for t, _, _ in self.rec.sent], ["全部批次都在等你"])
            self.assertIn("p.1：失败，等你决定", self.rec.sent[0][1])
            self.assertIn("p.3：待决 Q-1", self.rec.sent[0][1])
            self.assertIn("p.4：待决 Q-1", self.rec.sent[0][1])
            self.assertNotIn("p.2", self.rec.sent[0][1], "p.2 only waits behind p.1")

            self.decision("Q-1", ["p.3", "p.4"], state="answered")  # leaves the full block: p.3 may start
            self.tick(90, five=None)
            specs = [json.loads(p.read_text()) for p in (self.root / ".foremind" / "jobs").glob("*/spec.json")]
            self.assertEqual([s["argv"][0] for s in specs], ["claude"], "control: now one probe for both groups")

            self.decision("Q-1", ["p.3", "p.4"], state="open")  # the same reasons again
            self.tick(120, five=None)
            self.assertEqual(len(self.rec.sent), 1, "same set of reasons: not sent again")
            self.decision("Q-2", ["p.3"])
            self.tick(150, five=None)
            self.assertEqual(len(self.rec.sent), 2, "a new set of reasons is a new summary")


if __name__ == "__main__":
    unittest.main()
