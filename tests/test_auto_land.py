"""m2f REQ-2: the supervisor's automatic land (supervisor/phases/land.py), its outcomes, and announce / waits."""
import contextlib
import io
import os
import subprocess
from unittest import mock

from foremind import config, hooks, seat, worktree
from foremind.cli import main
from foremind.commands import land, status
from foremind.paths import state_dir
from foremind.plan import model
from foremind.supervisor import tick as sv
from test_supervisor import H, Base

USER = '[quota]\nprobe_command = ["/usr/bin/true"]\n'


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class AutoLandTest(Base):
    """p.1 delivered, done, one repo without a remote, a passing gate on its heads, the plan bound, land.auto true:
    items 1-10 all met."""

    def setUp(self):
        super().setUp()
        self.user_config(USER + "[land]\nauto = true\n")
        self.plan("p", {"state": "delivered"})
        (self.root / "foremind.toml").write_text('[[repos]]\nid = "main"\npath = "main"\n')
        self.repo = self.root / "main"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        wt = worktree.batch_dir(seat.project_slug(self.root, config.load(self.root)), "p.1") / "main"
        git(self.repo, "worktree", "add", "-q", "-b", "fm/p.1", str(wt), "main")
        git(wt, "commit", "-q", "--allow-empty", "-m", "change")
        self.hd = {"main": git(wt, "rev-parse", "HEAD")}
        self.gate()

    def gate(self, verdict="pass", heads=None):
        self.log.append("gate_result", batch="p.1", heads=heads or self.hd, path="x", sha256="0" * 64, verdict=verdict,
                        failing=[] if verdict == "pass" else ["accept"], pending=[])

    def started(self):
        """[(argv after `python -m foremind`, env)] of this phase's jobs (not the tick's own `gate <batch>`)."""
        return [(c, j["env"]) for c, j in zip(self.jobs.cmds(), self.jobs.started)
                if c[:1] == ["land"] or c[:2] == ["gate", "--rerun"]]

    def lands(self):
        return [c for c, _ in self.started()]

    def ended(self, n, outcome, kind=None, code=1, **fields):
        """Job n wrote its land_result and exited."""
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome=outcome, kind=kind,
                        job=self.jobs.started[n]["id"],
                        **{"reason": f"why {outcome}", "failed": None, "out_dir": None, **fields})
        self.jobs.finish(n, code=0 if outcome == "merged" else code)

    def told(self, title):
        return [x for x in self.rec.sent if x[0] == title]

    def test_lands_as_the_supervisor(self):
        self.tick()
        [(argv, env)] = self.started()
        self.assertEqual((argv, env["FOREMIND_ROLE"]), (["land", "p.1"], "supervisor"))
        it = self.events("sv_land", "intent")[0]
        self.assertEqual((it["batch"], it["heads"], it["action"]), ("p.1", self.hd, "land"))
        self.assertEqual(self.jobs.started[0]["timeout_s"], 2 * 30 * 60 + sv.JOB_SLACK_S)  # accept + land.commands
        self.assertEqual(self.told("p.1 已交付，等你合入"), [])  # REQ-2: the supervisor merges it
        self.ended(0, "merged", merges={"main": "f" * 40})
        self.tick(H)
        [o] = self.events("auto_land_outcome")
        self.assertEqual((o["outcome"], o["land_result"]), ("merged", self.events("land_result")[0]["id"]))
        self.assertEqual((len(self.started()), self.rec.sent), (1, []))  # merged: nothing more until reconcile

    # --- the ten items, each not met alone -------------------------------------------------------------------

    def no_job(self, at=0):
        self.tick(at)
        self.assertEqual(self.lands(), [])

    def test_item_1_and_unauthorized_as_before(self):
        self.user_config(USER)
        with mock.patch.object(land, "auto_blockers", wraps=land.auto_blockers) as ab:
            self.no_job()
        ab.assert_not_called()  # no git for it either
        self.assertEqual(len(self.told("p.1 已交付，等你合入")), 1)
        self.assertEqual(status._reader(self.root).waits()[0], {"p.1": "等你合入"})
        self.user_config(USER + "[land]\nauto = 1\n")
        self.no_job()

    def test_item_2(self):
        self.set_state("p.1", "approved")
        with mock.patch.object(land, "auto_blockers", wraps=land.auto_blockers) as ab:
            self.no_job()
        ab.assert_not_called()  # state first

    def test_item_3(self):
        self.set_state("p.1", "delivered", mode="watch")
        self.log.append("plan_approved", plan="p", plan_hash=model.plan_hash(model.load(self.root, "p")))
        self.no_job()

    def test_item_4(self):
        self.user_config(USER + '[land]\nauto = true\n[delivery]\nlevel = "merge_dev"\n')
        self.no_job()

    def test_item_5(self):
        git(self.repo, "remote", "add", "origin", str(self.tmp / "nowhere.git"))
        self.no_job()

    def test_item_6(self):
        self.gate("fail")
        self.no_job()
        self.gate(heads={"main": "0" * 40})
        self.no_job()

    def test_item_7_a_bad_q_file_end_to_end(self):
        d = state_dir(self.root) / "decisions"
        d.mkdir()
        (d / "Q-1.json").write_text("{")
        self.no_job()
        (d / "Q-1.json").unlink()
        self.decision("Q-2", ["p.1"])
        self.no_job()

    def test_item_8(self):
        plan = model.plan_dir(self.root, "p") / "plan.md"
        plan.write_text(plan.read_text() + "edited\n")
        self.no_job()

    def test_item_9_one_job_in_the_project(self):
        self.jobs.start(self.root, ["x"], cwd=self.root, job_id="other")
        self.log.append("sv_land", phase="intent", dedupe_id="sv_land:x.1", batch="x.1", heads={}, action="land",
                        job="other", at=self.now)
        self.no_job()
        self.jobs.finish(0)
        self.tick()  # x.1 is no batch of this pass: its intent is left open
        self.assertEqual(self.lands(), [])
        self.log.append("sv_land", dedupe_id="sv_land:x.1", ok=True)
        self.tick()
        self.assertEqual(self.lands(), [["land", "p.1"]])
        self.tick(H)  # its own job still running
        self.assertEqual(len(self.lands()), 1)

    def test_item_10_stop_and_foremind_run(self):
        self.log.append("auto_land_stopped", batch="p.1", heads={"main": "0" * 40}, outcome="conflict")
        self.tick()  # another heads' stop
        self.assertEqual(len(self.lands()), 1)
        self.ended(0, "conflict", reason="conflict in a.py", conflicts=["a.py"])
        self.tick()
        [s] = self.events("auto_land_stopped")[1:]
        self.assertEqual((s["heads"], s["outcome"], s["kind"]), (self.hd, "conflict", None))
        [(_, body, prio)] = self.told("p.1 自动合入未完成")
        self.assertEqual(prio, "P1")
        for text in ("conflict", "conflict in a.py", "处理后手动 foremind land p.1，或 foremind run p.1 让监督进程再试"):
            self.assertIn(text, body)
        self.tick(H)
        self.assertEqual(len(self.lands()), 1)  # stopped on these heads
        self.assertEqual(len(self.told("p.1 已交付，等你合入")), 1)  # stopped: told as before
        self.assertEqual(status._reader(self.root).waits()[0], {"p.1": "等你合入"})
        sv.request_run(self.root, ["p.1"])  # `foremind run p.1`
        self.tick(H + 1)
        self.assertEqual(len(self.lands()), 2)
        self.ended(1, "conflict", reason="conflict in a.py")
        self.tick(H + 2)
        self.assertEqual(len(self.told("p.1 自动合入未完成")), 2)  # a new stop, told again

    # --- outcomes ----------------------------------------------------------------------------------------------

    def test_tests_twice_with_a_gate_rerun_between_fails_the_batch(self):
        self.tick()
        out = str(state_dir(self.root) / "land" / "p.1-x")
        self.ended(0, "tests", failed=["python3 -m unittest"], out_dir=out)
        self.tick()
        (argv, env) = self.started()[1]
        self.assertEqual((argv, env["FOREMIND_ROLE"]), (["gate", "--rerun", "--no-merge", "p.1"], "supervisor"))
        self.assertEqual(self.events("sv_land", "intent")[1]["action"], "gate")
        self.tick(H)
        self.assertEqual(len(self.started()), 2)  # the rerun runs
        self.gate()  # what the rerun wrote
        self.jobs.finish(1)
        self.tick(H)
        self.assertEqual(self.lands()[2], ["land", "p.1"])  # a pass: at once, no retry wait
        self.ended(2, "tests", failed=["python3 -m unittest", "true"], out_dir=out)
        self.tick(H)
        h = self.header("p.1")
        self.assertEqual((h["state"], h["state_prior"]), ("failed", "delivered"))
        [e] = [e for e in self.events("batch_state") if e.get("state") == "failed"]
        self.assertEqual((e["reason"], e["failed"], e["out_dir"], e["heads"]), ("auto_land_failed", 2, out, self.hd))
        st = self.hpath("p.1").read_text().split("## 状态\n", 1)[1]
        for text in ("自动合入的测试两次没过", "- python3 -m unittest\n- true", out, "处理后用 foremind run p.1 重试"):
            self.assertIn(text, st)
        [(title, body, prio)] = [x for x in self.rec.sent if "p.1" in x[0] and "全部批次" not in x[0]]
        self.assertEqual((title, prio), ("p.1 连续失败已停", "P1"))  # the report phase's one notice
        self.assertIn(f"自动合入的测试两次没过（失败 2 条，输出在 {out}）", body)
        self.assertEqual(len(self.started()), 3)
        # item 7 of the handoff: `foremind run` takes it back to ready, and the next seat reads the status in L1
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}):
            self.assertEqual(main(["run", "p.1"]), 0)
        self.assertEqual(self.header("p.1")["state"], "ready")
        self.assertIn("自动合入的测试两次没过", hooks.l1(self.root, "s", "p.1", "seat"))

    def test_rerun_not_passing_fails_the_batch(self):
        self.tick()
        self.ended(0, "tests", failed=["true"], out_dir="o")
        self.tick()
        self.gate("fail")
        self.jobs.finish(1, code=1)
        self.tick()
        self.assertEqual(self.header("p.1")["state"], "failed")
        [e] = [e for e in self.events("batch_state") if e.get("state") == "failed"]
        self.assertEqual((e["failed"], e["out_dir"]), (1, "o"))  # the land's commands, not the gate's check names
        st = self.hpath("p.1").read_text().split("## 状态\n", 1)[1]
        for text in ("门禁重跑也没通过", "失败的命令：\n- true\n输出目录：o",
                     f"门禁重跑没通过的检查：accept；输出 {state_dir(self.root) / 'jobs' / 'j2'}"):
            self.assertIn(text, st)
        self.assertEqual([s["outcome"] for s in self.events("auto_land_stopped")], ["fail"])

    def test_foremind_run_while_the_rerun_runs_keeps_the_chain(self):  # m2f.4 r2
        for verdict in ("fail", "pass"):
            with self.subTest(verdict):
                self.setUp()
                self.tick()
                self.ended(0, "tests", failed=["true"], out_dir="o")
                self.tick()
                self.assertEqual(self.events("sv_land", "intent")[1]["failed"], ["true"])
                sv.request_run(self.root, ["p.1"])
                self.gate(verdict)
                self.jobs.finish(1, code=0 if verdict == "pass" else 1)
                self.tick()
                if verdict == "pass":
                    self.assertTrue(self.events("sv_land", "intent")[2]["rerun"])
                    self.ended(2, "refused", "busy")  # a retry is still the land after the rerun
                    self.tick(11 * 60)
                    self.assertTrue(self.events("sv_land", "intent")[3]["rerun"])
                    self.ended(3, "tests", failed=["false"], out_dir="o2")
                    self.tick(11 * 60)
                [e] = [e for e in self.events("batch_state") if e.get("state") == "failed"]
                want = (1, "o") if verdict == "fail" else (1, "o2")
                self.assertEqual((self.header("p.1")["state"], (e["failed"], e["out_dir"])), ("failed", want))
                self.assertNotIn("（无）", self.hpath("p.1").read_text())

    def test_not_failed_when_no_longer_delivered_but_stopped(self):  # m2f.4 r1 note
        self.tick()
        self.ended(0, "tests", failed=["true"], out_dir="o")
        self.tick()
        self.set_state("p.1", "paused", state_prior="delivered")
        self.gate("fail")
        self.jobs.finish(1, code=1)
        self.tick()
        self.set_state("p.1", "delivered")
        self.tick(H)
        self.assertEqual((self.header("p.1")["state"], len(self.lands())), ("delivered", 2))
        self.assertEqual(len(self.told("p.1 已交付，等你合入")), 1)  # stopped: told as before

    def test_retried_without_a_cap_told_once(self):
        for kind in ("busy", "dirty", "elsewhere", "moved", None):
            with self.subTest(kind):
                self.setUp()
                self.tick()
                for n in range(3):  # more than supervisor.seat_retries
                    self.ended(n, "refused" if kind else "unregistered_keys", kind)
                    self.tick(n * 11 * 60 + 5 * 60)
                    self.assertEqual(len(self.lands()), n + 1)  # not before gate_retry_min
                    self.tick((n + 1) * 11 * 60)
                    self.assertEqual(len(self.lands()), n + 2)
                [(_, body, prio)] = self.told("p.1 自动合入受阻")
                self.assertIn("refused（" + kind if kind else "unregistered_keys", body)
                self.assertEqual((prio, self.events("auto_land_stopped")), ("P1", []))
                self.assertEqual(self.told("p.1 已交付，等你合入"), [])

    def test_refused_auto_neither_told_nor_stopped(self):
        self.tick()
        self.ended(0, "refused", "auto")
        self.tick(5 * 60)
        self.assertEqual(len(self.lands()), 1)
        self.tick(11 * 60)
        self.assertEqual(len(self.lands()), 2)
        self.ended(1, "refused", "auto")
        self.tick(23 * 60)
        self.assertEqual((len(self.lands()), self.rec.sent, self.events("auto_land_stopped")), (3, [], []))

    def test_errors_up_to_seat_retries_stop(self):
        self.tick()
        self.jobs.finish(0, code=1)  # no land_result
        self.tick()
        self.assertEqual(self.events("auto_land_outcome")[0]["outcome"], "error")
        self.assertEqual((len(self.lands()), self.rec.sent), (1, []))
        self.tick(11 * 60)
        self.assertEqual(len(self.lands()), 2)
        self.ended(1, "merged")
        self.jobs.state["j2"]["timed_out"] = True  # timed out: error whatever it wrote
        self.tick(12 * 60)
        [s] = self.events("auto_land_stopped")
        self.assertEqual((s["outcome"], s["reason"]), ("error", "合入作业超时（0）"))
        [(_, body, _)] = self.told("p.1 自动合入未完成")
        self.assertIn("foremind run p.1", body)
        self.tick(H)
        self.assertEqual(len(self.lands()), 2)

    def test_lost_job_is_an_error(self):
        self.tick()
        self.jobs.state["j1"] = {"state": "lost"}
        self.tick()
        self.assertEqual(self.events("auto_land_outcome")[0]["reason"], "合入作业丢失（lost）")

    def test_stop_kinds(self):
        for o, k in (("notes_unverified", None), ("move_failed", None), ("refused", "state"), ("refused", "gate"),
                     ("refused", "config")):
            with self.subTest(o=o, k=k):
                self.setUp()
                self.tick()
                self.ended(0, o, k)
                self.tick()
                self.assertEqual([(s["outcome"], s["kind"]) for s in self.events("auto_land_stopped")], [(o, k)])
                self.assertEqual(len(self.told("p.1 自动合入未完成")), 1)
                self.tick(H)
                self.assertEqual(len(self.lands()), 1)

    def test_own_land_result_by_job_id_else_the_last_after_the_intent(self):
        self.tick()
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome="refused", kind="dirty", reason="r",
                        job=None)
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome="refused", kind="busy", reason="r",
                        job="j1")
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome="refused", kind="moved", reason="r",
                        job=None)
        self.jobs.finish(0, code=1)
        self.tick()
        self.assertEqual(self.events("auto_land_outcome")[0]["kind"], "busy")
        self.tick(11 * 60)
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome="refused", kind="auto", reason="r",
                        job=None)
        self.jobs.finish(1, code=1)
        self.tick(11 * 60)
        self.assertEqual(self.events("auto_land_outcome")[1]["kind"], "auto")

    def test_waits_and_announce_leave_the_batch_out(self):
        self.tick()
        self.assertEqual(status._reader(self.root).waits()[0], {})
        self.assertEqual(self.told("p.1 已交付，等你合入"), [])
