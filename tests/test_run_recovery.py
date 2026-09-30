"""m2b.10: quota state at the user level, and the crash windows of seat jobs, start_job and job ids."""
import contextlib
import json
import subprocess
import sys
from unittest import mock

from foremind import gate, install, job, lock, review
from foremind.supervisor import quota
from foremind.supervisor import tick as sv
from test_supervisor import REAL_STATUS, H, M, Base


class Crash(BaseException):
    """The supervisor process dying at this point."""


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class QuotaAtUserLevelTest(Base):
    @contextlib.contextmanager
    def at(self, root):
        """Run the Base helpers against another project."""
        (root / ".foremind" / "batches").mkdir(parents=True, exist_ok=True)
        mine, self.root = self.root, root
        try:
            yield
        finally:
            self.root = mine

    def test_projects_share_the_state_and_each_resumes_only_its_own_sessions(self):
        other = self.tmp / "other"
        self.plan("p")
        self.hold("p.1", "fm-a")
        with self.at(other):
            self.plan("q")
            self.hold("q.1", "fm-b")
        self.carrier.idle = False  # keep the messages in the inboxes
        self.tick(five=95)
        self.assertEqual(quota.load()["paused"], ["fm-a"])
        with self.at(other):
            self.tick(60, five=None)  # no reading of its own: the account is exhausted all the same
            self.assertEqual(self.jobs.started, [])
        self.assertEqual(quota.load()["paused"], ["fm-a", "fm-b"], "each project adds its own")
        self.tick(3 * H + 1, five=None)  # the 5h reset: unknown, a probe
        self.jobs.finish(0)
        self.tick(3 * H + 60, five=None)
        self.assertEqual([m.text for m in self.pending("fm-a")], [sv.RESUME_TEXT])
        self.assertEqual(quota.load()["paused"], ["fm-b"], "a session this project does not know stays")
        with self.at(other):
            self.tick(3 * H + 90, five=None)  # the probe answered for the account
            self.assertEqual([m.text for m in self.pending("fm-b")], [sv.RESUME_TEXT])
        self.assertEqual(quota.load()["paused"], [])
        self.assertFalse((self.root / ".foremind" / "quota.json").exists())

    def test_a_reading_of_any_enabled_project_counts(self):
        other = self.tmp / "other"
        self.plan("p")
        self.hold("p.1", "fm-a")
        with self.at(other):
            self.plan("q")
        for r in (self.root, other):
            install.set_registered(r, True)
        self.tick(five=10)
        with self.at(other):
            self.tick(30, five=None)
        self.assertEqual(quota.load()["groups"]["claude/long"]["state"], quota.AVAILABLE)
        install.set_registered(self.root, False)  # control: its sessions' reading is not seen any more
        with self.at(other):
            self.tick(60, five=None)
        self.assertEqual(quota.load()["groups"]["claude/long"]["state"], quota.UNKNOWN)

    def test_the_state_moves_on_the_strictest_line_of_the_enabled_projects(self):  # r2 #2
        other = self.tmp / "other"
        self.plan("p")
        with self.at(other):
            self.plan("q")
        (other / "foremind.toml").write_text("[quota]\nreserve_pct = 20\n")  # pause at 80, not 90
        for r in (self.root, other):
            install.set_registered(r, True)
        self.tick(five=85)
        with self.at(other):
            self.tick(30, five=None)
            self.assertEqual(self.events("quota_state"), [], "nothing to change")
        self.tick(60, five=85)
        st = quota.load()["groups"]["claude/long"]
        self.assertEqual((st["state"], st["pause"]), (quota.EXHAUSTED, 80))
        self.assertEqual([e["to"] for e in self.events("quota_state") if e["group"] == "long"], [quota.EXHAUSTED])

    def test_the_old_project_file_is_folded_in_on_the_first_pass(self):
        self.plan("p", {"state": "ready", "mode": "watch"})
        old = self.root / ".foremind" / "quota.json"
        old.write_text(json.dumps({"groups": {"claude/long": {"state": "exhausted", "window": "5h",
                                                              "resets_at": self.now + H, "pause": 90}},
                                   "merged_checked": {"p.1": self.now}}))
        self.tick(five=10)  # a reading that alone would make it available
        self.assertEqual(quota.load()["groups"]["claude/long"]["state"], quota.EXHAUSTED, "the stricter one")
        self.assertFalse(old.exists())
        self.assertEqual([e["taken"] for e in self.events("quota_migrated")], [["claude/long"]])

    def test_one_probe_for_the_account_across_projects(self):  # r1 #4
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n')
        other = self.tmp / "other"
        self.plan("p", {"state": "ready"})
        with self.at(other):
            self.plan("q", {"state": "ready"})
        self.tick(five=None)
        self.assertEqual(self.jobs.cmds(), [["/usr/bin/true"]])
        with self.at(other):
            self.tick(30, five=None)
        self.assertEqual(len(self.jobs.started), 1, "the other project's probe is in flight")
        self.jobs.finish(0, code=1)
        self.tick(60, five=None)
        st = quota.load()["groups"]["claude/long"]
        self.assertEqual((st["fails"], st["next_try"]), (1, self.now + 60 + 15 * M), "backoff from its answer")

    def test_merged_checks_stay_in_the_project(self):
        self.plan("p", {"state": "running"})
        with mock.patch.object(gate, "batch_merged", return_value=False) as merged:
            self.tick()
            self.tick(60)
        self.assertEqual(merged.call_count, 1, "supervisor.merged_check_min")
        checked = json.loads((self.root / ".foremind" / "merged_checked.json").read_text())
        self.assertEqual(list(checked), ["p.1"])
        self.assertNotIn("merged_checked", quota.load())


class SeatOpenCrashTest(Base):
    def seat_job(self, jid, state):
        self.log.append("sv_seat", phase="intent", dedupe_id="sv_seat:p.1:1", batch="p.1", at=self.now, job=jid)
        self.log.append("sv_job", dedupe_id="sv_job:sv_seat:p.1:1", action="sv_seat:p.1:1", job=jid)
        self.jobs.state[jid] = {"state": state}

    def seat_open(self, session, **fields):
        self.log.append("seat_open", phase="intent", dedupe_id=f"seat_open:{session}", batch="p.1", session=session,
                        **fields)

    def test_a_seat_job_settles_only_its_own_seat_open(self):  # M1-7-r3 note 2
        self.plan("p", {"state": "ready"})
        self.seat_job("j0", "lost")  # it lost the claim race to the user's `foremind seat` and exited
        self.seat_open("fm-s", job="j0")
        self.seat_open("fm-u", job=None, pid=sv.os.getpid())  # the user's, still opening
        lock.acquire(self.root, "p.1", "fm-u")
        self.tick()
        self.assertEqual(self.carrier.closed, ["fm-s"])
        self.assertEqual([e["session"] for e in self.events("seat_open", "result")], ["fm-s"])
        self.assertEqual(lock.holder(self.root, "p.1"), "fm-u")

    def test_a_seat_open_whose_process_is_gone_is_settled_at_once(self):  # M1-7-r3 note 3
        self.plan("p", {"state": "ready", "mode": "watch"})
        self.seat_open("fm-s", job=None, pid=dead_pid())  # `foremind seat` in a terminal since closed
        lock.acquire(self.root, "p.1", "fm-s")
        self.tick()
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), (["fm-s"], None))
        self.assertEqual([(e["ok"], e["recovered"]) for e in self.events("seat_open", "result")], [(False, True)])

    def test_a_seat_open_whose_process_lives_waits_for_the_time_limit(self):
        self.plan("p", {"state": "ready", "mode": "watch"})
        self.seat_open("fm-s", job=None, pid=sv.os.getpid())
        lock.acquire(self.root, "p.1", "fm-s")
        self.tick(30 * M)
        self.assertEqual(self.carrier.closed, [], "still opening")
        self.tick(36 * M)  # a reused pid would keep it open for ever: the time limit still holds
        self.assertEqual(self.carrier.closed, ["fm-s"])

    def test_what_a_gone_process_wrote_during_the_pass_is_read_before_settling(self):  # r1 #1
        self.plan("p", {"state": "running", "mode": "watch"})
        self.seat_open("fm-s", job=None, pid=dead_pid())
        lock.acquire(self.root, "p.1", "fm-s")

        def opened_meanwhile(*_):  # `foremind seat` finishes and exits while reconcile fetches
            self.log.append("seat_opened", batch="p.1", session="fm-s", successor=False, carrier="fake", worktrees={})
            self.log.append("seat_open", dedupe_id="seat_open:fm-s", batch="p.1", session="fm-s", ok=True)
            return False

        with mock.patch.object(gate, "batch_merged", side_effect=opened_meanwhile) as merged:
            self.tick()
        self.assertEqual(merged.call_count, 1)
        self.assertEqual((self.carrier.closed, lock.holder(self.root, "p.1")), ([], "fm-s"))
        self.assertEqual([e.get("recovered") for e in self.events("seat_open", "result")], [None])

    def test_a_gone_process_is_settled_even_while_its_job_is_in_flight(self):
        self.plan("p", {"state": "ready"})
        self.seat_job("j0", "running")  # its runner has not written the result yet
        self.seat_open("fm-s", job="j0", pid=dead_pid())
        lock.acquire(self.root, "p.1", "fm-s")
        self.tick()
        self.assertEqual(self.carrier.closed, ["fm-s"])
        self.assertEqual(self.events("sv_seat", "result"), [], "the job itself is harvested once it ends")


class StartJobCrashTest(Base):
    def test_a_crash_before_the_job_link_loses_no_job(self):  # M1-7-r1:138
        self.plan("p")
        real = sv.Tick.emit

        def emit(t, type, **kw):
            if type == "sv_job":
                raise Crash
            return real(t, type, **kw)

        with mock.patch.object(sv.Tick, "emit", emit), self.assertRaises(Crash):
            self.tick()
        it = self.events("sv_seat", "intent")[0]
        self.assertEqual((it["job"], self.events("sv_job")), (self.jobs.started[0]["job_id"], []))
        self.tick(30)
        self.assertEqual((self.events("sv_seat", "result"), len(self.jobs.started)), ([], 1), "still running")
        self.jobs.finish(0, code=1)
        self.tick(60)
        self.assertEqual([(e["job_state"], e["exit_code"]) for e in self.events("sv_seat", "result")], [("done", 1)])

    def test_a_crash_before_the_job_existed_is_lost(self):
        self.plan("p", {"state": "ready", "mode": "watch"})
        self.log.append("sv_seat", phase="intent", dedupe_id="sv_seat:p.1:1", batch="p.1", at=self.now, job="f00")
        with mock.patch.object(job, "status", REAL_STATUS):
            self.tick()
        self.assertEqual([(e["ok"], e["job_state"]) for e in self.events("sv_seat", "result")], [(False, "lost")])


class MergeRetriesTest(Base):
    def test_a_failed_merge_counts_though_the_gate_exits_2(self):  # r1 #2, r2 #1
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[delivery]\nlevel = "merge_dev"\n'
                         '[supervisor]\nseat_retries = 3\n')
        self.plan("p", {"state": "delivered"})
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved"}))

        def merge(repo, ok):  # what gate._merge_one writes: the intent once per head (EventLog dedupe)
            did = f"merge:p.1:{repo}:abc"
            self.log.append("merge", phase="intent", dedupe_id=did, batch="p.1", repo=repo, head="abc", via="gh")
            if ok:
                self.log.append("merge", dedupe_id=did, batch="p.1", repo=repo, head="abc")

        merge("c", False)  # c waits in the host's merge queue: no failed merge
        self.log.append("merge_queued", batch="p.1", repo="c", head="abc")
        self.tick()
        self.jobs.finish(0, code=2)  # the batch busy: no merge tried
        self.tick(11 * M)
        self.assertEqual(len(self.jobs.started), 2)
        merge("a", True)  # a group cut short: a merged, b failed (FlowError, exit 2)
        merge("b", False)
        self.jobs.finish(1, code=2)
        self.tick(22 * M)
        self.assertEqual(len(self.jobs.started), 3)
        self.assertEqual(self.rec.sent, [])
        merge("b", False)  # the same head fails again: nothing new in the log
        self.jobs.finish(2, code=2)
        self.tick(33 * M)
        self.assertEqual(len(self.jobs.started), 4)
        self.jobs.finish(3, code=-15)  # timed out (SF-8)
        self.tick(44 * M)
        self.tick(55 * M)
        self.assertEqual(len(self.jobs.started), 4, "retries used up (supervisor.seat_retries)")
        self.assertEqual([t for t, _, _ in self.rec.sent if "合入" in t], ["p.1 合入失败"])
        sv.request_run(self.root, ["p.1"])  # the user starts over; the merge fails at the same head again
        for n, at in enumerate((56, 67, 78), 4):
            self.tick(at * M)
            self.jobs.finish(n, code=2)
        self.tick(89 * M)
        self.assertEqual(len(self.jobs.started), 7, "counted though its intent was written before the restart")


class OneshotSlotsTest(Base):
    def test_reviews_and_decisions_share_supervisor_max_oneshot(self):  # m2b.3 r1 note
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_oneshot = 1\n')
        self.plan("p", {"state": "review_ready"})
        self.log.append("sv_decide", phase="intent", dedupe_id="sv_decide:Q-1:1", question="Q-1", role="decider",
                        oneshot_session="s-1", job="jd")
        self.jobs.state["jd"] = {"state": "running"}
        with mock.patch.object(review, "start", return_value="r-1") as start:
            self.tick()
            self.assertEqual(start.call_count, 0, "the decider's job holds the only slot")
            self.jobs.state["jd"] = {"state": "done", "exit_code": 0}
            self.tick(30)
            self.assertEqual(start.call_count, 1)
