"""m2b.5: L0 fact checks and the one-shot auditor (foremind/audit.py, supervisor/phases/audit.py, commands/audit.py)."""
import contextlib
import io
import json
import os
import unittest
from datetime import date, datetime, time as dtime
from unittest import mock

from foremind import audit, batchlog, cli, config, lock, notify, schemas
from foremind.commands import gate as gate_cmd
from foremind.events import EventLog
from foremind.fsutil import LockBusy, file_lock, sha256_bytes
from foremind.paths import state_dir
from foremind.plan import model
from foremind.supervisor import tick as sv
from foremind.vendors import claude
from test_supervisor import Base

HD = {"main": "a" * 40}


def finding(sev="P2", evidence=("events.jsonl#gate_result p.1",), batches=("p.1",)):
    return {"severity": sev, "summary": "s", "evidence": list(evidence), "batches": list(batches)}


def wrapped(obj, prose="审计结论："):
    return json.dumps({"type": "result", "is_error": False, "result": prose + json.dumps(obj, ensure_ascii=False)})


class AuditBase(Base):
    def setUp(self):
        super().setUp()
        self.sd = state_dir(self.root)

    def cli(self, *argv, session=""):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": session, "FOREMIND_PROJECT": str(self.root)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["audit", *argv])
        return code, out.getvalue(), err.getvalue()

    def goal(self, pid, body="# 目标\n\nREQ-1: 做完\n"):
        """A frozen goal bound by plan.md, approved again (Base.plan writes none)."""
        p = model.load(self.root, pid)
        h = model.text_hash(body)
        p.goal, p.doc.header["goal_hash"] = model.Doc({"frozen_at": "2026-09-25T00:00:00+00:00", "sha256": h}, body), h
        model.write_goal(self.root, pid, p.goal)
        model.write(self.root, p)
        self.log.append("plan_approved", plan=pid, goal_hash=h, plan_hash=model.plan_hash(p))

    def paused(self):
        return sv.paused_path(self.root).exists()

    def failures(self):
        return [(e["check"], e["target"]) for e in self.events("l0_hard_failure") if "fingerprint" in e]

    def auditors(self):
        return [j for j in self.jobs.started if j["env"].get("FOREMIND_ROLE") == "auditor"]

    def mat(self, n=0):
        return self.sd / "oneshots" / self.auditors()[n]["env"]["FOREMIND_SESSION"]

    def reply(self, n, raw, code=0):
        self.jobs.finish(self.jobs.started.index(self.auditors()[n]), code, raw)

    def invalid_tick(self, at):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return sv.tick(self.root, now=self.now + at)


class SeatAncestorTest(AuditBase):
    def test_a_claim_of_the_user_written_under_a_seat_pauses_before_any_action(self):  # m2c.6, REQ-11 ④
        self.plan("p")  # a pass that acts opens its seat
        e = self.log.append("pending_answered", question="Q-1", answer=1, by="user", seat_ancestor="fm-p-1")
        self.log.append("batch_retried", batch="p.1", by="user", seat_ancestor="unknown")  # no failure: bounds tells it
        self.tick()
        self.assertEqual((self.failures(), self.paused(), self.jobs.cmds()), ([("seat_ancestor", e["id"])], True, []))
        [f] = self.events("l0_hard_failure")
        self.assertEqual(f["severity"], "P0")
        self.assertIn("fm-p-1", f["detail"])
        self.assertLess(max(i for i, x in enumerate(self.events()) if x["type"] == "paused"),
                        self.events().index(f))  # paused before the report
        [(title, body, prio)] = [s for s in self.rec.sent if s[2] == "P0"]
        self.assertIn("冒用用户 1 处", body)
        self.assertNotIn("fm-p-1", body)
        self.tick(30)
        self.assertEqual((len(self.failures()), self.jobs.cmds()), (1, []))  # paused: nothing runs
        sv.pause(self.root, False)  # the user looked: accepted
        self.tick(60)
        self.assertEqual((len(self.failures()), self.paused(), self.jobs.cmds()), (1, False, [["seat", "p.1"]]))


class L0Test(AuditBase):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.decision("Q-1", ["p.1"])  # every pass is a full block: L0 still runs

    def test_config_baseline_unapproved_change_pause_and_resume_accepts(self):
        f = self.root / "CLAUDE.md"
        f.write_text("a")
        self.tick()
        path = os.path.realpath(f)
        self.assertIn((path, sha256_bytes(b"a")), [(e["path"], e["sha256"]) for e in self.events("l0_baseline")])
        self.assertEqual((self.failures(), self.paused()), ([], False))
        f.write_text("b")
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([("config", path)], True))
        [(title, body, prio)] = [s for s in self.rec.sent if s[0].startswith("L0")]
        self.assertEqual(prio, "P0")
        self.assertIn("配置文件 1 处", body)
        self.assertNotIn(str(self.root), body)
        self.assertNotIn("CLAUDE", body)
        self.assertIn("paused", self.tick(60))  # nothing runs while paused
        sv.pause(self.root, False)  # the user looked: accepted
        self.tick(90)
        self.assertEqual((len(self.failures()), self.paused()), (1, False))
        self.assertEqual(self.events("l0_baseline")[-1]["sha256"], sha256_bytes(b"b"))
        f.write_text("c")  # the user's own session
        self.log.append("user_config_edit", session="u", path=path, sha256=sha256_bytes(b"c"))
        self.tick(120)
        self.assertEqual((len(self.failures()), self.events("l0_baseline")[-1]["sha256"]), (1, sha256_bytes(b"c")))
        f.write_text("b")  # accepted once, from a: from c it is another change
        self.tick(150)
        self.assertEqual((len(self.failures()), self.paused()), (2, True))
        sv.pause(self.root, False)
        self.tick(180)
        f.write_text("c")  # its approval was used up by the baseline c
        self.tick(210)
        self.assertEqual((len(self.failures()), self.paused()), (3, True))

    def test_approvals_and_deletion(self):
        s = self.root / ".claude" / "settings.json"
        seat = self.sd / "sessions" / "fm-a.settings.json"
        agents = self.root / "AGENTS.md"
        for p, text in ((s, "x"), (seat, "s1"), (agents, "g")):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        self.tick()
        s.write_text("y")  # the user's own session (PostToolUse, I51)
        self.log.append("user_config_edit", session="u", path=os.path.realpath(s), sha256=sha256_bytes(b"y"))
        seat.write_text("s2")  # a launch rewrote it (m2b.10)
        self.log.append("seat_launch", dedupe_id="seat_launch:fm-a", session="fm-a", ok=True,
                        settings_sha256=sha256_bytes(b"s2"))
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([], False))
        s.write_text("z")  # an edit record for other content approves nothing
        self.log.append("user_config_edit", session="u", path=os.path.realpath(s), sha256=sha256_bytes(b"w"))
        agents.unlink()
        self.tick(60)
        self.assertEqual(sorted(self.failures()), sorted([("config", os.path.realpath(s)), ("config", str(agents))]))
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertIn(f"L0 config {agents}: deleted（已报告）", out)
        self.assertEqual(self.cli("--accept-config", session="fm-x")[0], 2)  # a Foremind session: refused
        self.assertTrue(self.paused())
        code, out, _ = self.cli("--accept-config")
        self.assertEqual((code, out.splitlines()[-1], self.paused()), (0, "resumed", False))
        [acc] = self.events("l0_config_accepted")
        self.assertEqual({f["path"]: f["sha256"] for f in acc["files"]},
                         {os.path.realpath(s): sha256_bytes(b"z"), str(agents): None})
        self.tick(90)
        self.assertEqual((len(self.failures()), self.paused()), (2, False))

    def test_init_and_uninstall_record_what_they_write(self):  # m2b.5 r2 note: else a re-init pauses with a P0
        from foremind import install
        from foremind.install import settings as inst
        local, proj = inst.path(self.root), install.project_config(self.root)
        self.tick()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude-home")}):
            inst.install(self.root, self.root)
            install.write_project(self.root, name="shop", carrier="tmux", notify="none", repos=None)
            self.assertTrue(local.is_file() and proj.is_file())
            self.tick(30)
            self.assertEqual((self.failures(), self.paused()), ([], False))
            install.uninstall(self.root)
        self.assertFalse(local.exists())
        self.tick(60)
        self.assertEqual((self.failures(), self.paused()), ([], False))
        self.assertTrue(self.events("program_config_write"))
        local.parent.mkdir(exist_ok=True)
        local.write_text("{}")  # by hand: still a change without approval
        self.tick(90)
        self.assertEqual(self.failures(), [("config", str(local))])

    def test_files_new_after_the_first_reconciliation_need_an_approval(self):
        self.tick()
        self.assertEqual([e["path"] for e in self.events("l0_root")], [os.path.realpath(self.root)])
        new = [self.root / "CLAUDE.md", self.root / ".foremind" / "roles" / "sub" / "x.md"]  # a subtree too
        ok = self.root / ".claude" / "settings.json"
        for p in (*new, ok):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        self.log.append("user_config_edit", session="u", path=os.path.realpath(ok), sha256=sha256_bytes(b"x"))
        self.tick(30)
        self.assertEqual(sorted(self.failures()), sorted(("config", os.path.realpath(p)) for p in new))
        self.assertEqual(self.events("l0_hard_failure")[-1]["detail"], "new without approval")
        self.assertIn(os.path.realpath(ok), [e["path"] for e in self.events("l0_baseline")])
        self.assertEqual(len(self.events("l0_root")), 1)
        sv.pause(self.root, False)
        self.tick(60)
        agents, local, outside = (self.root / "AGENTS.md", self.root / ".claude" / "settings.local.json",
                                  self.root / "notes.md")
        agents.write_text("g")
        self.log.append("user_config_edit", session="u", path=os.path.realpath(agents), sha256=sha256_bytes(b"g"))
        self.tick(90)
        n = len(self.failures())
        outside.write_text("anything")
        (self.root / "CLAUDE.md").unlink()
        (self.root / "CLAUDE.md").symlink_to("AGENTS.md")  # the same content as a place already baselined
        local.symlink_to(outside)  # a target outside the #22 places
        self.tick(120)
        self.assertEqual(sorted(self.failures()[n:]), sorted([("config", str(self.root / "CLAUDE.md")),
                                                               ("config", str(local))]))
        sv.pause(self.root, False)
        self.tick(150)
        agents.write_text("h")  # the user edits the target: the link's content changes with it
        self.log.append("user_config_edit", session="u", path=os.path.realpath(agents), sha256=sha256_bytes(b"h"))
        local.unlink()
        self.tick(180)
        self.assertEqual(self.failures()[n + 2:], [("config", str(local))])  # a link removed is a deletion

    def test_seat_settings_are_judged_by_their_launch(self):
        sessions = self.sd / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        self.log.append("seat_launch", dedupe_id="seat_launch:fm-a", session="fm-a", ok=True,
                        settings_sha256=sha256_bytes(b"launched"))
        (sessions / "fm-a.settings.json").write_text("edited before the first pass")
        self.tick()  # the first reconciliation takes the rest, not settings its launch recorded otherwise
        self.assertEqual(self.failures(), [("config", os.path.realpath(sessions / "fm-a.settings.json"))])
        sv.pause(self.root, False)
        self.log.append("seat_launch", dedupe_id="seat_launch:fm-b", session="fm-b", ok=True)  # before m2b.10
        self.log.append("planner_opened", session="fm-c", plan="p")
        for s in ("fm-b", "fm-c", "fm-d"):
            (sessions / f"{s}.settings.json").write_text(s)
        planner = sessions / "fm-e.settings.json"  # a planner that got no heartbeat: no record, the program's content
        claude.launch(session="fm-e", batch="", role="planner", project_root=self.root, settings_path=planner,
                      cwd=self.root, permission_mode="default")
        os.utime(planner, (0, 0))
        self.tick(30)
        base = {e["path"] for e in self.events("l0_baseline")}
        self.assertEqual({s for s in ("fm-b", "fm-c", "fm-d", "fm-e")
                          if os.path.realpath(sessions / f"{s}.settings.json") in base}, {"fm-b", "fm-c", "fm-e"})
        self.assertEqual((len(self.failures()), self.paused()), (1, False))  # fm-d: its launch may still record it
        d = sessions / "fm-d.settings.json"
        os.utime(d, (0, 0))  # long past any heartbeat wait, and no launch of fm-d
        self.tick(60)
        self.assertEqual((self.failures()[-1], self.paused()), (("config", os.path.realpath(d)), True))
        sv.pause(self.root, False)
        self.tick(90)
        a = sessions / "fm-a.settings.json"
        a.write_text("launched")  # what its launch recorded, but before the baseline since accepted
        self.tick(120)
        self.assertEqual((self.failures()[-1], self.paused()), (("config", os.path.realpath(a)), True))

    def test_goal_batch_log_and_event_chain(self):
        self.goal("p")
        batchlog.append(self.root, "p.1", "p.1.D1 · a · b · c · #1 · d", author="s")
        self.tick()
        self.assertEqual(self.failures(), [])
        g = self.sd / "plans" / "p" / "goal.md"
        g.write_text(g.read_text() + "REQ-2: 偷偷加的\n")
        log = self.sd / "batches" / "p.1.log.md"
        log.write_text(log.read_text().replace("#1", "#2"))
        lines = (self.sd / "events.jsonl").read_text().splitlines()
        lines[0] = lines[0].replace('"plan": "p"', '"plan": "q"')  # hash no longer matches its content
        (self.sd / "events.jsonl").write_text("\n".join(lines) + "\n")
        self.tick(30)
        self.assertEqual(sorted(self.failures()), [("batch_log", "p.1"), ("event_chain", "events.jsonl"),
                                                   ("goal", "p")])
        [(_, body, _)] = [s for s in self.rec.sent if s[0].startswith("L0")]
        self.assertIn("（p、p.1）", body)
        code, _, err = self.cli("--accept-config")  # resuming would accept the goal, the log and the chain unseen
        self.assertEqual((code, self.paused(), self.events("l0_config_accepted")), (2, True, []))
        self.assertIn("foremind resume", err)
        self.assertIn("L0 goal p", err)
        sv.pause(self.root, False)
        self.tick(60)
        self.assertEqual((len(self.failures()), self.paused()), (3, False))  # the same facts: accepted
        g.write_text(g.read_text() + "REQ-3: 又加的\n")  # the same places tampered with again, other content
        log.write_text(log.read_text().replace("#2", "#3"))
        lines = (self.sd / "events.jsonl").read_text().splitlines()
        lines[0] = lines[0].replace('"plan": "q"', '"plan": "r"')  # the same problem text on the same line
        (self.sd / "events.jsonl").write_text("\n".join(lines) + "\n")
        self.tick(90)
        self.assertEqual((sorted(self.failures()[3:]), self.paused()),
                         ([("batch_log", "p.1"), ("event_chain", "events.jsonl"), ("goal", "p")], True))

    def test_a_fact_put_right_then_repeated_is_reported_again(self):
        self.goal("p")
        batchlog.append(self.root, "p.1", "p.1.D1 · a · b · c · #1 · d", author="s")
        c = self.root / "CLAUDE.md"
        c.write_text("a")
        self.tick()
        g, log, ev = self.sd / "plans" / "p" / "goal.md", self.sd / "batches" / "p.1.log.md", self.sd / "events.jsonl"
        text = {g: g.read_text(), log: log.read_text()}

        def line1(a, b):
            lines = ev.read_text().splitlines()
            lines[0] = lines[0].replace(a, b)
            ev.write_text("\n".join(lines) + "\n")

        def tamper():  # a seat's Bash
            g.unlink()
            log.write_text(text[log].replace("#1", "#2"))
            line1('"plan": "p"', '"plan": "q"')
            c.unlink()

        tamper()
        self.tick(30)
        self.assertEqual((len(self.failures()), self.paused()), (4, True))
        c.write_text("a")  # put right, then resumed
        sv.pause(self.root, False)  # the user looked: the rest accepted
        self.tick(60)
        self.assertEqual((len(self.failures()), self.paused()), (4, False))
        for p, t in text.items():  # resumed, then put right
            p.write_text(t)
        line1('"plan": "q"', '"plan": "p"')
        self.tick(90)
        self.assertEqual(sorted((e["check"], e["target"]) for e in self.events("l0_cleared")), sorted(self.failures()))
        tamper()  # the very same facts again
        self.tick(120)
        self.assertEqual((sorted(self.failures()[4:]), self.paused()), (sorted(self.failures()[:4]), True))
        self.assertEqual([p for t, _, p in self.rec.sent if t.startswith("L0")], ["P0", "P0"])

    def test_a_goal_frozen_before_its_plan_md_is_no_failure(self):
        body = "# 目标\n\nREQ-1: 做完\n"
        (self.sd / "plans" / "q").mkdir(parents=True)
        model.write_goal(self.root, "q", model.Doc({"frozen_at": "2026-09-25T00:00:00+00:00",
                                                    "sha256": model.text_hash(body)}, body))
        self.log.append("goal_frozen", plan="q", goal_hash=model.text_hash(body))  # plan freeze: plan.md comes later
        self.tick()
        self.assertEqual((self.failures(), self.paused()), ([], False))
        g = self.sd / "plans" / "q" / "goal.md"
        g.write_text(g.read_text() + "REQ-2: 偷偷加的\n")
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([("goal", "q")], True))

    def test_a_config_or_event_log_that_does_not_load_pauses_with_one_p0(self):
        (self.root / "foremind.toml").write_text("broken = [")  # e.g. a seat's Bash
        self.assertEqual(self.invalid_tick(0), 1)
        self.assertEqual((self.failures(), self.paused()), ([("config_invalid", "config")], True))
        sv.pause(self.root, False)  # the user looked: accepted, still no pass acts
        self.assertEqual(self.invalid_tick(30), 1)
        self.assertEqual((len(self.failures()), self.paused()), (1, False))
        (self.root / "foremind.toml").unlink()
        with open(self.sd / "events.jsonl", "a") as f:
            f.write("not json\n")
        self.assertEqual(self.invalid_tick(60), 1)
        self.assertEqual(self.invalid_tick(90), 0)  # paused: once
        self.assertTrue(self.paused())
        sent = [(b[:4], p) for t, b, p in self.rec.sent if t.startswith("L0")]
        self.assertEqual(sent, [("配置读不", "P0"), ("事件日志", "P0")])

    def test_a_line_that_is_no_event_pauses_with_one_p0(self):  # JSON, but the tick and append read its fields
        ev = self.sd / "events.jsonl"
        lines = ('{"type": "user_config_edit", "path": "x", "sha256": "y"}', "{}", "[1]", "null")
        for n, line in enumerate(lines):
            good = ev.read_text()
            with open(ev, "a") as f:
                f.write(line + "\n")
            self.assertEqual(self.invalid_tick(30 * n), 1)
            self.assertEqual((self.invalid_tick(30 * n + 10), self.paused()), (0, True))  # paused: once
            ev.write_text(good)
            sv.pause(self.root, False)
        self.assertEqual([(b[:4], p) for t, b, p in self.rec.sent if t.startswith("L0")], [("事件日志", "P0")] * 4)

    def test_a_frozen_plan_that_does_not_load_fails_its_goal(self):
        self.goal("p")
        self.tick()
        g = self.sd / "plans" / "p" / "goal.md"
        g.write_text("---\nsha256: \"x\"\n\nREQ-2: 偷偷改的\n")  # the header left open
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([("goal", "p")], True))
        self.assertIn("plan unreadable", self.events("l0_hard_failure")[-1]["detail"])
        sv.pause(self.root, False)
        self.tick(60)
        (self.sd / "plans" / "p" / "plan.md").unlink()  # plan_ids no longer lists it
        self.tick(90)
        self.assertEqual((self.failures(), self.paused()), ([("goal", "p")] * 2, True))

    def test_a_change_accepted_from_one_baseline_is_not_accepted_from_another(self):
        f = self.root / "CLAUDE.md"
        f.write_text("a")
        self.tick()
        f.unlink()  # a seat's Bash
        self.tick(30)
        sv.pause(self.root, False)  # the user looked: the deletion is the baseline
        self.tick(60)
        f.write_text("a")  # restored and approved: a baseline with the same content as the first
        self.log.append("user_config_edit", session="u", path=os.path.realpath(f), sha256=sha256_bytes(b"a"))
        self.tick(90)
        self.assertEqual((len(self.failures()), self.paused()), (1, False))
        f.unlink()  # the same pair of hashes again
        self.tick(120)
        self.assertEqual((len(self.failures()), self.paused()), (2, True))

    def test_a_report_without_its_pause_pauses_again(self):
        self.log.append("l0_root", path=os.path.realpath(self.root))
        (self.root / "CLAUDE.md").write_text("x")
        with mock.patch("foremind.supervisor.phases.audit.pause", side_effect=OSError("disk full")):
            self.assertEqual(self.invalid_tick(0), 1)
        # the pause first: the phase reported nothing without it (the tick's fallback paused, r8)
        self.assertEqual((self.failures(), self.paused()), ([("tick_crash", "tick")], True))
        sv.pause(self.root, False)
        [f] = audit.l0(self.root, config.load(self.root))[2]
        self.log.append("l0_hard_failure", dedupe_id=f"l0_hard_failure:{f['fingerprint']}", severity="P0",
                        **f)  # a pass that died before its pause
        self.tick()
        self.assertEqual((self.failures()[1:], self.paused()), ([("config", f["target"])], True))
        self.assertEqual([p for t, _, p in self.rec.sent if t.startswith("L0")], ["P0", "P0"])

    def test_an_unreadable_batch_log_fails_alone(self):
        (self.sd / "batches" / "p.1.log.md").mkdir(parents=True)
        self.log.append("l0_root", path=os.path.realpath(self.root))
        (self.root / "CLAUDE.md").write_text("x")
        self.tick()  # the other checks still run
        self.assertEqual((sorted(self.failures()), self.paused()),
                         ([("batch_log", "p.1"), ("config", os.path.realpath(self.root / "CLAUDE.md"))], True))
        self.assertEqual(self.events("l0_hard_failure")[0]["detail"], "unreadable (IsADirectoryError)")

    def test_every_batch_log_is_checked_whether_a_plan_loads_or_lists_it(self):
        batchlog.append(self.root, "p.1", "p.1.D1 · a", author="s")
        batchlog.append(self.root, "x.1", "x.1.D1 · a", author="s")  # in no plan
        self.tick()
        self.assertEqual(self.failures(), [])
        (self.sd / "plans" / "p" / "plan.md").unlink()  # no plan lists p.1 any more
        b = self.sd / "batches"
        (b / "p.1.log.md").write_text((b / "p.1.log.md").read_text().replace("D1", "D2"))
        (b / "x.1.log.md").unlink()  # only its records are left
        (b / "y.1.log.md").write_text("y.1.D1 · 伪造\n")  # no record at all
        self.tick(30)
        self.assertEqual((sorted(self.failures()), self.paused()),
                         ([("batch_log", "p.1"), ("batch_log", "x.1"), ("batch_log", "y.1")], True))

    def test_goal_is_the_one_last_frozen_or_approved(self):
        self.goal("p")
        self.tick()
        g = self.sd / "plans" / "p" / "goal.md"
        text = g.read_text()
        g.unlink()
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([("goal", "p")], True))
        sv.pause(self.root, False)
        g.write_text(text)
        p = model.load(self.root, "p")  # all three rewritten alike, no approval
        body = p.goal.body + "REQ-2: 偷偷加的\n"
        p.goal, p.doc.header["goal_hash"] = model.Doc({**p.goal.header, "sha256": model.text_hash(body)}, body), \
            model.text_hash(body)
        model.write_goal(self.root, "p", p.goal)
        model.write(self.root, p)
        self.tick(60)
        self.assertEqual((self.failures()[-1], len(self.failures()), self.paused()), (("goal", "p"), 2, True))

    def locked(self, fn):
        """fn()'s result, and whether each event log it read was read under the events lock."""
        real, held = EventLog.iter, []

        def iter(log):
            try:
                with file_lock(self.sd / "events.lock", blocking=False):
                    held.append(False)
            except LockBusy:
                held.append(True)
            return real(log)

        with mock.patch.object(EventLog, "iter", iter):
            return fn(), set(held)

    def test_events_are_read_under_their_lock(self):  # hooks append under it alone: a half line is no l0_error
        # the history, and batchlog.verify's records
        self.assertEqual(self.locked(lambda: audit.l0(self.root, config.load(self.root))[2]), ([], {True}))
        t = sv.Tick(self.root, config.load(self.root), self.now)
        self.assertEqual(self.locked(t.load)[1], {True})  # r9: nor a tick_crash

    def test_an_l0_p0_takes_the_user_layers_channel(self):  # r9: the project layer is a #22 file under check
        self.l0_channel('[notify]\nchannel = "ntfy"\n')

    def test_an_l0_p0_takes_ntfy_when_the_user_layer_has_its_server_alone(self):  # r10: the standard install
        self.l0_channel('[notify.ntfy]\nserver = "https://ntfy.example"\n')
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n')
        self.assertEqual(sv.user_cfg().get("notify.channel", "none"), "none")  # neither: none

    def l0_channel(self, user_notify):
        got = []

        class Chan:
            name = "rec"

            def __init__(self, cfg):
                self.channel = cfg.get("notify.channel", "none")

            def send(self, title, body, priority="P1"):
                got.append((title[:2], self.channel))
                return True

        self.enterContext(mock.patch.object(notify, "get", lambda root, cfg: Chan(cfg)))
        self.enterContext(mock.patch.object(notify, "P0_MAX", 3))  # Q-23: the channel is under test, not m2b.6's limit
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n\n' + user_notify)
        c = self.sd / "config.toml"
        c.write_text('[notify]\nchannel = "ntfy"\n')
        self.tick()
        c.write_text('[notify]\nchannel = "none"\n')  # a seat's Bash silences the P0 about itself
        self.tick(30)
        sv.pause(self.root, False)
        with mock.patch.object(sv.Tick, "load", side_effect=RuntimeError("boom")):
            self.assertEqual(self.invalid_tick(60), 1)
        c.write_text("broken = [")
        sv.pause(self.root, False)
        self.assertEqual(self.invalid_tick(90), 1)
        self.assertEqual(self.failures(), [("config", os.path.realpath(c)), ("tick_crash", "tick"),
                                           ("config_invalid", "config")])
        self.assertEqual([ch for t, ch in got if t == "L0"], ["ntfy"] * 3)

    def test_audit_lists_what_the_tick_reported_until_a_pass_clears_it(self):  # r9: resuming it unseen
        with mock.patch.object(sv.Tick, "load", side_effect=RuntimeError("boom")):
            self.assertEqual(self.invalid_tick(0), 1)
        self.assertEqual(self.cli(), (0, "L0 tick_crash tick: RuntimeError: boom（已报告）\n", ""))
        code, _, err = self.cli("--accept-config")
        self.assertEqual((code, self.paused()), (2, True))
        self.assertIn("foremind resume", err)
        sv.pause(self.root, False)
        self.tick(30)  # a pass through L0 clears it
        self.assertEqual(self.cli(), (0, "nothing to report\n", ""))

    def test_audit_lists_bounds_events(self):  # m2d.9 (m2c.6 D): the phase records them, the user reads them here
        self.tick()
        self.log.append("bounds_violation", dedupe_id="bounds_violation:f1", batch="p.1", kind="worktree",
                        fingerprint="f1", paths=[{"path": "x.py", "why": "owns_paths 之外"}])
        self.log.append("bounds_violation", dedupe_id="bounds_violation:f2", batch="p.1", kind="remote",
                        fingerprint="f2", repos=[{"repo": "main", "branch": "fm/p.1", "on_remote": ["o/b"], "prs": [7]}])
        self.log.append("bounds_error", dedupe_id="bounds_error:f3", batch="p.2", kind="worktree", error="git: boom",
                        fingerprint="f3")
        code, out, _ = self.cli()
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 3, out)
        self.assertRegex(lines[0], r"^越界 p\.1 \(worktree\) \S+: x\.py（owns_paths 之外）$")
        self.assertRegex(lines[1], r"^越界 p\.1 \(remote\) \S+: main fm/p\.1：远端 o/b PR #7$")
        self.assertRegex(lines[2], r"^越界核对出错 p\.2 \(worktree\) \S+: git: boom$")

    def test_a_goal_hash_that_is_not_a_string_matches_nothing(self):
        self.goal("p")
        self.tick()
        p = model.load(self.root, "p")
        body = p.goal.body + "REQ-2: 偷偷加的\n"
        model.write_goal(self.root, "p", model.Doc({**p.goal.header, "sha256": []}, body))
        self.assertEqual(model.load(self.root, "p").goal.header["sha256"], [])
        self.tick(30)
        self.assertEqual((self.failures(), self.paused()), ([("goal", "p")], True))


class PreDeliveryTest(AuditBase):
    def setUp(self):
        super().setUp()
        self.plan("p", {}, {})
        self.goal("p")
        self.hold("p.2", "fm-s")  # something else runs: no full block
        self.set_state("p.1", "awaiting_audit")
        self.log.append("batch_state", batch="p.1", prior="approved", state="awaiting_audit", heads=HD)
        batchlog.append(self.root, "p.1", "p.1.D1 · 选 a · 没选 b · 因为 c · #1 · x.py\n其他文字", author="s")
        (self.sd / "batches" / "p.1.review.r1.json").write_text('{"verdict": "approved"}')
        (self.sd / "batches" / "p.1.gate.x.json").write_text('{"verdict": "pass"}')
        self.log.append("gate_result", batch="p.1", heads=HD, path="p.1.gate.x.json", verdict="pass")

    def test_materials_once_then_pass_delivers_and_runs_the_gate(self):
        self.tick()
        self.tick(30)  # in flight: not started again
        [j] = self.auditors()
        mat = self.mat()
        self.assertIn("p.1", json.loads((mat / "facts.json").read_text())["task"])
        self.assertIn("REQ-1: 做完", (mat / "goal.md").read_text())
        self.assertEqual((mat / "records.md").read_text(), "p.1.D1 · 选 a · 没选 b · 因为 c · #1 · x.py\n")
        self.assertEqual(json.loads((mat / "receipt.json").read_text()), {"verdict": "approved"})
        self.assertEqual(json.loads((mat / "gate.json").read_text()), {"verdict": "pass"})
        self.assertEqual(json.loads((mat / "batch.json").read_text())["id"], "p.1")
        self.assertTrue((mat / "prompt.md").read_text().startswith("# 角色卡：审计者"))
        self.reply(0, wrapped([finding("P2"), finding("P0", evidence=[" "])], prose="先说 [] 不对，结论："))
        self.tick(60)
        self.assertEqual(self.header("p.1")["state"], "delivered")
        [done] = self.events("audit_done")
        self.assertEqual((done["trigger"], done["target"], done["findings"]), ("pre_delivery", "p.1", {"P2": 1}))
        rep = json.loads((self.sd / "reports" / done["report"]).read_text())
        self.assertEqual(schemas.validate("audit_report", rep), [])
        self.assertEqual((rep["auditor_session"], [f["severity"] for f in rep["findings"]]),
                         (j["env"]["FOREMIND_SESSION"], ["P2"]))  # the P0 without evidence was dropped
        [g] = [x for x in self.jobs.started if x["argv"][len(sv.FM):] == ["gate", "p.1"]]
        self.assertEqual(g["env"]["FOREMIND_ROLE"], "supervisor")
        self.assertEqual(self.events("sv_gate", "intent")[-1]["state"], "delivered")
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 已交付，等你合入"])  # the tick's, no audit notice

    def gates(self):
        return [i for i, j in enumerate(self.jobs.started) if j["argv"][len(sv.FM):] == ["gate", "p.1"]]

    def test_a_failed_gate_after_the_audit_is_retried_until_it_passes(self):  # r11: else the status stays pending
        self.tick()
        self.reply(0, "[]")
        self.tick(30)
        [g] = self.gates()
        self.jobs.finish(g, 2)  # an infrastructure error
        self.tick(60)
        self.assertEqual(len(self.gates()), 1)  # not before supervisor.gate_retry_min
        self.tick(630)
        self.jobs.finish(self.gates()[1], 0)
        self.tick(700)
        self.tick(1300)
        self.assertEqual(len(self.gates()), 2)

    def test_a_gate_after_the_audit_that_keeps_failing_is_given_up_with_one_notice(self):
        self.tick()
        self.reply(0, "[]")
        self.tick(30)
        self.jobs.finish(self.gates()[0], 2)
        self.tick(630)
        self.jobs.finish(self.gates()[1], 1)
        self.tick(700)
        self.tick(1300)
        self.assertEqual(len(self.gates()), 2)  # supervisor.seat_retries
        self.assertEqual([t for t, _, _ in self.rec.sent].count("p.1 补写门禁失败"), 1)

    def test_p0_holds_until_the_user_releases(self):
        self.tick()
        self.reply(0, json.dumps([finding("P0"), finding("P1")]))
        self.tick(30)
        self.assertEqual(self.header("p.1")["state"], "awaiting_audit")
        self.assertEqual([(e["target"], e["key"]) for e in self.events("audit_held")],
                         [("p.1", audit.pre_key(list(self.log.iter()), "p.1"))])
        [(title, body, prio)] = self.rec.sent
        self.assertEqual((title, prio), ("p.1 交付前审计未放行", "P0"))
        self.assertIn("foremind audit --release p.1", body)
        self.tick(60)
        self.assertEqual(len(self.auditors()), 1)  # held: not audited again
        code, out, _ = self.cli()
        self.assertIn("p.1 (pre_delivery): 发现 P0 1 条、P1 1 条 · ", out)
        self.assertEqual(self.cli("--release", "p.1", session="fm-x")[0], 2)
        with mock.patch.object(gate_cmd, "_run", return_value=0) as gate:
            code, out, _ = self.cli("--release", "p.1")
        self.assertEqual((code, self.header("p.1")["state"]), (0, "delivered"))
        self.assertEqual(gate.call_args[0][0].batch, "p.1")
        self.assertEqual(self.events("batch_state")[-1]["via"], "audit --release")

    def test_the_only_unfinished_batch_is_audited_then_waits_on_the_user_once_held(self):
        self.set_state("p.2", "merged")
        self.tick()
        self.assertEqual(len(self.auditors()), 1)  # awaiting_audit alone is no full block
        self.reply(0, json.dumps([finding("P1")]))
        self.tick(30)
        self.tick(60)
        self.assertIn(("全部批次都在等你", "p.1：等审计放行"), [(t, b) for t, b, _ in self.rec.sent])

    def test_an_l0_failure_ends_the_pass_before_any_action(self):
        self.plan("q")  # would be made ready and get a seat this pass
        self.log.append("l0_root", path=os.path.realpath(self.root))  # reconciled before
        (self.root / "CLAUDE.md").write_text("written by a seat's Bash")
        self.tick()
        self.assertEqual((self.failures(), self.paused(), self.jobs.cmds(), self.header("q.1")["state"]),
                         ([("config", os.path.realpath(self.root / "CLAUDE.md"))], True, [], "planned"))

    def test_a_batch_the_user_holds_is_audited_under_a_full_block(self):
        lock.acquire(self.root, "p.1", lock.USER)
        self.set_state("p.2", "failed")  # the other one waits on the user
        self.tick()
        self.assertEqual(len(self.auditors()), 1)
        self.assertNotIn("全部批次都在等你", [t for t, _, _ in self.rec.sent])

    def crashes(self):
        """r8: whatever raises before L0 lets the pass act fails closed: paused, one P0, no auditor started."""
        self.assertEqual((self.invalid_tick(0), self.paused(), self.jobs.cmds()), (1, True, []))
        self.assertEqual(self.invalid_tick(10), 0)  # paused: once
        self.assertEqual([(b[:7], p) for t, b, p in self.rec.sent if t.startswith("L0")], [("监督器本轮出错", "P0")])

    def test_an_event_whose_id_is_a_list_fails_closed(self):  # every key there: the load's set takes no list
        with open(self.sd / "events.jsonl", "a") as f:
            f.write('{"id": [], "ts": "", "type": "x", "phase": "result", "dedupe_id": null, "prev": "", "hash": ""}\n')
        self.crashes()
        self.assertEqual(self.failures(), [("tick_crash", "tick")])
        self.assertTrue(self.events("l0_hard_failure")[0]["detail"].startswith("TypeError: "))
        sv.pause(self.root, False)  # the user looked: accepted by type and message, and still no pass acts
        self.assertEqual((self.invalid_tick(20), self.paused(), len(self.failures()), self.jobs.cmds()),
                         (1, False, 1, []))

    def test_a_goal_md_that_is_not_utf8_fails_closed(self):  # the load before L0 raises
        with open(self.sd / "plans" / "p" / "goal.md", "ab") as f:
            f.write(b"\xff")
        self.crashes()
        self.assertTrue(self.events("l0_hard_failure")[0]["detail"].startswith("UnicodeDecodeError: "))

    def test_an_event_log_that_cannot_be_read_fails_closed(self):  # no event goes in: the pause file and a P0
        ev = self.sd / "events.jsonl"
        ev.rename(self.sd / "events.bak")
        ev.mkdir()  # IsADirectoryError, as chmod 000 gives PermissionError
        self.crashes()
        ev.rmdir()
        (self.sd / "events.bak").rename(ev)
        self.assertEqual(self.failures(), [])

    def test_the_l0_phase_raising_fails_closed(self):  # e.g. its pause or report
        with mock.patch("foremind.supervisor.phases.audit.l0", side_effect=OSError("disk full")):
            self.crashes()
        self.assertEqual(self.events("l0_hard_failure")[0]["detail"], "OSError: disk full")

    def test_l0_raising_fails_closed(self):
        self.plan("q")  # would be made ready and get a seat in a pass that acts
        with mock.patch("foremind.audit.config_files", side_effect=RuntimeError("boom")):  # nothing checked
            self.tick(30)
            self.assertEqual((self.failures(), self.paused()), ([("l0_error", "l0")], True))
            self.assertEqual(self.events("l0_hard_failure")[-1]["detail"], "RuntimeError: boom")
            code, out, err = self.cli("--accept-config")
            self.assertEqual((code, self.paused()), (2, True))
            self.assertIn("L0 l0_error l0: RuntimeError: boom", err)
            sv.pause(self.root, False)  # the user looked: still no pass acts
            self.tick(60)
            self.assertEqual((len(self.failures()), self.paused(), self.jobs.cmds(), self.header("q.1")["state"]),
                             (1, False, [], "planned"))
        [(_, body, prio)] = [s for s in self.rec.sent if s[0].startswith("L0")]
        self.assertEqual(prio, "P0")
        self.assertIn("对账出错 1 处", body)
        self.assertNotIn("boom", body)
        self.tick(90)  # fixed: the pass acts again
        self.assertNotEqual(self.header("q.1")["state"], "planned")
        with mock.patch("foremind.audit.config_files", side_effect=RuntimeError("boom")):  # the same error again
            self.tick(120)
        self.assertEqual((len(self.failures()), self.paused()), (2, True))

    def test_without_the_audit_phase_nothing_acts(self):
        self.plan("q")
        real = sv.Tick.load_phases
        with mock.patch.object(sv.Tick, "load_phases",
                               lambda t: [m for m in real(t) if not m.__name__.endswith(".audit")]):
            self.tick()  # its module did not import
            self.assertEqual((self.failures(), self.paused()), ([("l0_error", "l0")], True))
            sv.pause(self.root, False)  # the user looked: still no pass acts
            self.tick(30)
            self.assertEqual((len(self.failures()), self.paused(), self.jobs.cmds(), self.header("q.1")["state"]),
                             (1, False, [], "planned"))
        self.assertEqual([p for t, _, p in self.rec.sent if t.startswith("L0")], ["P0"])
        self.tick(60)  # back
        self.assertNotEqual(self.header("q.1")["state"], "planned")
        self.assertEqual([(e["check"], e["target"]) for e in self.events("l0_cleared")], [("l0_error", "l0")])

    def test_failed_or_invalid_output_is_retried_once_then_held(self):
        self.tick()
        self.reply(0, "", code=1)
        self.tick(30)
        self.assertEqual(len(self.auditors()), 2)
        self.reply(1, json.dumps([finding("P5")]))  # not an audit_report
        self.tick(60)
        self.assertEqual((len(self.auditors()), self.header("p.1")["state"]), (2, "awaiting_audit"))
        [held] = self.events("audit_held")
        self.assertIn("2 次没有有效报告", held["why"])
        [(title, body, prio)] = self.rec.sent
        self.assertEqual((title, prio), ("p.1 交付前审计未放行", "P1"))
        self.assertNotIn("P5", body)  # no model output in a notice

    def test_daily_cap(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n\n[audit]\ndaily_cap = 1\n')
        # local noon: two ticks 30s apart must not straddle local midnight (the cap counts per local day)
        self.now = datetime.combine(date.today(), dtime(12)).timestamp()
        self.plan("q")
        self.set_state("q.1", "awaiting_audit")
        self.tick()
        self.tick(30)
        self.assertEqual(len(self.auditors()), 1)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["今日审计已达上限"])


class DeciderSampleTest(AuditBase):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.hold("p.1", "fm-s")
        d = self.sd / "decisions"
        d.mkdir()
        (d / "Q-3.json").write_text(json.dumps({**schemas.EXAMPLES["pending"], "id": "Q-3", "state": "applied",
                                                "answer": 2}))
        (self.sd / "oneshots" / "S").mkdir(parents=True)
        (self.sd / "oneshots" / "S" / "facts.json").write_text('{"task": "decide"}')
        (self.sd / "jobs" / "jd").mkdir(parents=True)
        (self.sd / "jobs" / "jd" / "stdout.log").write_text('{"conclusion": 2, "confidence": "high"}')
        self.log.append("sv_decide", phase="intent", dedupe_id="sv_decide:Q-3:1", question="Q-3", role="decider",
                        oneshot_session="S")
        self.log.append("sv_job", dedupe_id="sv_job:sv_decide:Q-3:1", action="sv_decide:Q-3:1", job="jd")
        self.log.append("sv_decide", dedupe_id="sv_decide:Q-3:1", ok=True)
        for q, sample in (("Q-3", True), ("Q-4", False)):
            self.log.append("decider_decided", question=q, category=4, conclusion=2, confidence="high", sample=sample)

    def test_sampled_decision_is_audited_with_what_the_decider_had(self):
        self.tick()
        [j] = self.auditors()
        mat = self.mat()
        self.assertNotIn("code", json.loads((mat / "pending.json").read_text()))
        self.assertEqual(json.loads((mat / "decision_output.json").read_text())["conclusion"], 2)
        self.assertEqual((mat / "decider_facts.json").read_text(), '{"task": "decide"}')
        self.assertIn("Q-3", json.loads((mat / "facts.json").read_text())["task"])
        self.reply(0, json.dumps([finding("P1", batches=[])]))
        self.tick(30)
        self.assertEqual([(t, p) for t, _, p in self.rec.sent], [("Q-3 决策抽查", "P1")])
        self.assertEqual(self.events("audit_done")[0]["findings"], {"P1": 1})
        self.tick(60)
        self.assertEqual(len(self.auditors()), 1)

    def test_not_started_under_a_full_block(self):
        self.set_state("p.1", "planned")
        self.decision("Q-1", ["p.1"])
        self.tick()
        self.assertEqual(self.auditors(), [])


class ParseTest(unittest.TestCase):
    def test_findings_and_fit(self):
        f = [finding()]
        for raw in (json.dumps(f), json.dumps({"findings": f}), wrapped(f), "结论 {\"x\": 1} [] 然后 " + json.dumps(f),
                    wrapped({"findings": f})):
            self.assertEqual(audit.findings(raw), f, raw)
        for raw in (" []\n", wrapped([], prose=""), json.dumps({"findings": []})):
            self.assertEqual(audit.findings(raw), [], raw)
        self.assertEqual(audit.findings(json.dumps(f) + " 其余没有：[] " + json.dumps(f)), f)  # an [] never overrides
        broken = json.dumps([finding("P0")]).replace('"s"', '"a "quoted" s"')  # an unescaped quote in a summary
        for raw, why in (("我拿不准", "no findings"), ("没有偏差：[]", "only as the whole answer"),
                         (json.dumps({"result": "rate limited", "is_error": True}), "rate limited"),
                         (json.dumps([finding("P0")]) + " 例如：" + json.dumps(f), "2 different findings lists"),
                         (broken, "does not parse"), (json.dumps([finding("P0")])[:-20], "does not parse")):  # cut off
            with self.assertRaisesRegex(ValueError, why):
                audit.findings(raw)
        out = audit.fit({"a": "x" * 10, "b": {"k": "中" * 100}, "c": "y" * 50}, budget=100)
        self.assertEqual(out["a"], "x" * 10)
        self.assertIn("截断", out["b"])
        self.assertLessEqual(len(out["b"].encode()), 90)
        self.assertIn("截断", out["c"])  # nothing left for it


if __name__ == "__main__":
    unittest.main()
