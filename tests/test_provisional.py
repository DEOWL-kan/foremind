"""m2b.4: provisional decisions PV-n, the overturn rate, precedent premises and citations (decide/pending.py,
decide/decider.py, supervisor/phases/provisional.py)."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from foremind import catalog, config, header, lock
from foremind.decide import decider, exemption, pending
from foremind.paths import state_dir
from foremind.supervisor.phases import provisional
from test_decider import OPTS, out
from test_supervisor import M, Base

H = 3600
PREC = {"question": "Q-1", "category": 3, "conclusion": "c", "scope": "s",
        "premises": [{"kind": "manual", "text": "t"}], "review_on": "2099-01-01", "supersedes": [], "needs_review": False}


class ProvisionalTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.hold("p.1", "fm-s")
        self.sd = state_dir(self.root)

    def ask(self, category=3, kind="hit", reversible=True, key=None):
        rec, _ = pending.create(self.root, config.load(self.root), question="要不要？", options=OPTS, recommended=1,
                                reason="r", category=category, blocks=["p.1"], reversible=reversible,
                                request={"kind": kind, "category": category, "batch": "p.1",
                                         "match": {"paths": ["main:p/dep.txt"]}, "approve_option": 1,
                                         **({"key": key} if key else {})})
        return rec["id"]

    def settle(self, qid, **kw):
        rec = pending.load(self.root, qid)
        o = out(qid, category=rec["category"], **kw)
        return decider.settle(self.root, {decider.PROJECT: {}}, rec, "decider", o)

    def state(self, qid):
        return pending.load(self.root, qid)["state"]

    def texts(self):
        return [m.text for m in self.pending("fm-s")]

    def test_reversible_decider_answer_is_provisional_and_applied(self):
        qid = self.ask()
        self.tick()
        [j] = [j for j in self.jobs.started if j["argv"][0] == "claude"]
        self.jobs.finish(self.jobs.started.index(j), 0, json.dumps({"type": "result", "is_error": False,
                                                                    "result": json.dumps(out(qid, confidence="medium"))}))
        self.tick()
        q = pending.load(self.root, qid)
        self.assertEqual((q["state"], q["answer"]), ("provisional", 1))
        pv = json.loads((self.sd / "decisions" / "PV-1.json").read_text())
        deadline = datetime.fromisoformat(pv.pop("deadline"))
        self.assertEqual(pv, {"id": "PV-1", "question": qid, "batch": "p.1", "category": 3,
                              "revert": "回退带 `provisional: PV-1` 的提交"})
        self.assertLess(abs(deadline - datetime.now(timezone.utc) - timedelta(hours=48)), timedelta(minutes=5))
        self.assertTrue((self.sd / "exemptions" / f"{qid}.json").exists())  # applied like an answer
        [a] = self.events("pending_answered")
        self.assertEqual((a["by"], a["answer"], a["provisional"]), ("decider", 1, "PV-1"))
        self.assertEqual(len(self.events("pending_applied")), 1)
        self.assertEqual(len(self.events("decider_decided")), 1)
        told = [t for _, t in self.carrier.sent if qid in t]
        self.assertTrue(told and "暂定（PV-1）" in told[0] and "提交信息写 `provisional: PV-1`" in told[0], told)
        self.assertIn("squash", told[0])
        self.assertEqual(self.rec.sent, [])  # nothing for the user yet
        self.assertIn("--confirm | --overturn", (self.sd / "decisions" / f"{qid}.md").read_text())

    def test_what_is_provisional_and_what_is_answered(self):
        """controller r3, goal literal: REQ-8 answers high, or medium when reversible, else escalates; REQ-10 makes the
        answer provisional when the category may be and the decision is reversible, whatever the confidence, option
        or request kind; REQ-11 step 1 only stops the provisional part."""
        cases = [(c, r, o) for c in ("high", "medium") for r in (True, False) for o in (1, 2)]  # o 1: approve
        qs = [self.ask() for _ in cases]
        for q, (c, r, o) in zip(qs, cases):
            self.settle(q, confidence=c, reversible=r, conclusion=o)
        self.assertEqual([self.state(q) for q in qs], ["provisional", "provisional", "applied", "applied",
                                                       "provisional", "provisional", "escalated", "escalated"])
        self.assertEqual([(self.sd / "exemptions" / f"{q}.json").exists() for q in qs],
                         [True, False, True, False, True, False, False, False])  # approvals that took effect
        self.assertEqual([a["provisional"] for a in self.events("pending_answered") if "provisional" in a],
                         ["PV-1", "PV-2", "PV-3", "PV-4"])
        self.assertEqual([e["reason"] for e in self.events("pending_escalated")], ["决策者：置信度 medium且不可撤回"] * 2)
        low, q15, manual = self.ask(), self.ask(15), self.ask(kind="manual")
        self.assertIn("escalated (置信度 low)", self.settle(low, confidence="low"))
        self.assertIn("answered", self.settle(q15, confidence="medium"))  # #15: the decider's, not provisional
        self.assertIn("provisional", self.settle(manual, confidence="medium"))  # no program action, still reversible
        self.assertEqual([self.state(x) for x in (low, q15, manual)], ["escalated", "applied", "provisional"])
        self.log.append("delegation_tightened", category=3, step=1)
        step1 = [self.ask(), self.ask(), self.ask()]
        self.settle(step1[0])
        self.settle(step1[1], confidence="medium")
        self.settle(step1[2], confidence="medium", reversible=False)
        self.assertEqual([self.state(x) for x in step1], ["applied", "applied", "escalated"])
        self.assertEqual(len(list((self.sd / "decisions").glob("PV-*.json"))), 5)  # none at step 1
        # step 2: taken back from the decider; a deciding one from before is escalated, a new one is the user's
        deciding = self.ask()
        self.log.append("delegation_tightened", category=3, step=2)
        self.assertIn("推翻率收回", self.settle(deciding))
        self.assertEqual(self.state(deciding), "escalated")
        new = self.ask()
        self.assertEqual(self.state(new), "open")
        self.assertEqual(self.events("pending_routed")[-1]["to"], "user")
        self.assertEqual(self.state(self.ask(13)), "deciding")  # the controller's rows are not touched

    def test_overdue_is_pushed_once_and_confirmed_by_the_user(self):
        q1, q2 = self.ask(), self.ask()
        self.settle(q1)
        self.settle(q2)
        (self.sd / "decisions" / "PV-2.json").unlink()  # no deadline to read: taken as past (fail-closed)
        self.tick()
        self.assertEqual((self.state(q1), self.state(q2)), ("provisional", "overdue"))
        self.tick(49 * H)
        self.tick(50 * H)
        self.assertEqual(self.state(q1), "overdue")
        pushed = sorted((t, b, p) for t, b, p in self.rec.sent if t.startswith("暂定到期"))
        body = "{q}（{pv}）到期未确认，仍按暂定执行\n确认：foremind decide {q} --confirm\n推翻：foremind decide {q} --overturn"
        self.assertEqual(pushed, [(f"暂定到期 {q1}", body.format(q=q1, pv="PV-1"), "P1"),
                                  (f"暂定到期 {q2}", body.format(q=q2, pv="暂定决定"), "P1")])
        self.assertEqual(pending.conclude(self.root, q1, "confirmed", note="好"), f"{q1} 已确认")
        self.assertEqual((self.state(q1), self.events("pending_confirmed")[0]["note"]), ("confirmed", "好"))
        self.assertTrue((self.sd / "exemptions" / f"{q1}.json").exists())
        for q, to in ((q1, "overturned"), (self.ask(), "confirmed")):  # confirmed is final; deciding is not provisional
            with self.assertRaisesRegex(ValueError, "不是待确认的暂定决定"):
                pending.conclude(self.root, q, to)

    def test_overturn_revokes_tells_and_tightens_step_by_step(self):
        qs = [self.ask() for _ in range(5)]
        for q in qs:
            self.settle(q)
        self.assertIn("已推翻", pending.conclude(self.root, qs[0], "overturned", note="不对"))
        self.assertFalse((self.sd / "exemptions" / f"{qs[0]}.json").exists())
        self.assertEqual(self.texts()[-1], f"{qs[0]}（PV-1）被用户推翻：按撤回方法回退：回退带 `provisional: PV-1` 的提交")
        [o] = self.events("pending_overturned")
        self.assertEqual((o["by"], o["note"]), ("user", "不对"))
        self.assertEqual(pending.tightened(self.root), {})  # 1 in 5: 20%, not over it
        pending.conclude(self.root, qs[1], "overturned")
        self.assertEqual(pending.tightened(self.root), {3: 1})
        [t] = self.events("delegation_tightened")
        self.assertEqual((t["step"], t["sample"], t["overturned"]), (1, 5, 2))
        q6 = self.ask()
        self.settle(q6)  # step 1: answered, not provisional; no new overturn: no second step
        self.assertEqual((self.state(q6), pending.tighten(self.root)), ("applied", []))
        pending.conclude(self.root, qs[2], "overturned")  # a PV from before step 1, overturned now
        self.assertEqual(pending.tightened(self.root), {3: 2})
        self.assertEqual(self.state(self.ask()), "open")
        with self.assertRaisesRegex(ValueError, "#4 没有按推翻率收紧"):
            pending.restore(self.root, 4)
        self.assertIn("#3 的委托已恢复", pending.restore(self.root, 3))
        self.assertEqual((pending.tightened(self.root), pending.tighten(self.root)), ({}, []))  # a fresh window
        self.assertEqual(self.state(self.ask()), "deciding")

    def test_tighten_window_and_what_counts(self):
        ev = lambda t, q, **kw: self.log.append(t, question=q, **kw)
        for n in range(1, 5):
            ev("pending_answered", f"Q-{n}", by="decider", category=10)
        ev("pending_overturned", "Q-1", by="user")
        ev("pending_answered", "Q-99", by="user")  # never the decider's: not counted
        self.assertEqual(pending.tighten(self.root), [])  # 4 samples
        ev("pending_answered", "Q-5", by="decider", category=10)
        self.assertEqual(pending.tighten(self.root), [])  # 1/5
        ev("pending_answered", "Q-2", by="user")  # the user changed the decider's answer
        self.assertEqual(pending.tighten(self.root), ["#10: tightened to step 1 (2/5 overturned)"])
        ev("pending_answered", "Q-6", by="decider", category=10)
        self.assertEqual(pending.tighten(self.root), [])  # still over, no new overturn
        ev("pending_overturned", "Q-3", by="user")
        self.assertEqual(len(pending.tighten(self.root)), 1)
        ev("pending_overturned", "Q-4", by="user")
        self.assertEqual((pending.tighten(self.root), pending.tightened(self.root)), ([], {10: 2}))  # at most 2
        pending.restore(self.root, 10)
        for n in range(7, 32):  # 25 after the restore, the first 5 overturned: out of the last 20
            ev("pending_answered", f"Q-{n}", by="decider", category=10)
            if n < 12:
                ev("pending_overturned", f"Q-{n}", by="user")
        self.assertEqual(pending.tighten(self.root), [])

    def test_precedent_premises_are_watched_under_a_full_block(self):
        (self.root / "a.txt").write_text("v1")
        fh = {"kind": "file_hash", "path": "a.txt", "sha256": hashlib.sha256(b"v1").hexdigest()}
        ck = {"kind": "config_key", "key": "authz.preset", "value": "balanced"}
        precs = [{**PREC, "id": "J-1", "premises": [fh, ck]}, {**PREC, "id": "J-2", "premises": [ck]},
                 {**PREC, "id": "J-3", "premises": [{**fh, "sha256": "0" * 64}], "superseded_by": "J-4"},
                 {**PREC, "id": "J-4", "premises": [{**fh, "path": "gone.txt"}]}]
        (self.sd / "precedents.md").write_text("\n".join(catalog._block(p) for p in precs))
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[authz]\npreset = "balanced"\n')
        self.ask(4)  # the user's: p.1 waits on the user, a full block
        self.assertIn("full block", self.tick())
        got = catalog._precedents(self.root)
        self.assertEqual([got[j]["needs_review"] for j in ("J-1", "J-2", "J-3", "J-4")], [False, False, False, True])
        (self.root / "a.txt").write_text("v2")
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[authz]\npreset = "conservative"\n')
        self.assertIn("full block", self.tick())
        got = catalog._precedents(self.root)
        self.assertEqual([got[j]["needs_review"] for j in ("J-1", "J-2", "J-3")], [True, True, False])
        self.assertEqual(got["J-1"]["premises"], [fh, ck])
        seen = [(e["precedent"], e["premise"]) for e in self.events("precedent_needs_review")]
        self.assertEqual(seen, [("J-4", {**fh, "path": "gone.txt"}), ("J-1", fh), ("J-1", ck), ("J-2", ck)])
        md = self.sd / "precedents.md"
        md.write_text(catalog._set(md.read_text(), "J-1", needs_review=False))
        self.assertEqual(provisional.watch(self.root, {"authz.preset": "balanced"}),
                         ["J-1: needs review (1 premise(s) changed)"])
        self.assertEqual(len(self.events("precedent_needs_review")), 4)  # the same premise: once

    def test_a_config_key_premise_at_its_default_holds_while_unset(self):  # REQ-16
        ck = lambda k, v: {"kind": "config_key", "key": k, "value": v}
        holds = lambda cfg, *x: catalog.holds(self.root, cfg, ck(*x))
        for k, v in (("authz.preset", "balanced"), ("supervisor.seat_retries", 2), ("notify.p1", "push"),
                     ("plan.coupling.high", 0.6), ("quota.low_pct", 85), ("exclude.models", [])):
            self.assertTrue(holds({}, k, v), k)
            self.assertFalse(holds({k: "set"}, k, v), k)  # set explicitly: the set value counts
        self.assertTrue(holds({"authz.preset": "balanced"}, "authz.preset", "balanced"))
        self.assertFalse(holds({}, "supervisor.seat_retries", 3))  # not the default
        self.assertFalse(holds({}, "context.abs_cap_tokens", 150000))  # two defaults in the code: none known
        # r1: per-repo keys read as review.repo_cfg does, falling back to the shared key
        self.assertTrue(holds({}, "delivery.level", "done"))
        self.assertTrue(holds({}, "delivery.repo.main.level", "done"))
        self.assertTrue(holds({"delivery.level": "merge_dev"}, "delivery.repo.main.level", "merge_dev"))
        self.assertFalse(holds({"delivery.level": "merge_dev"}, "delivery.repo.main.level", "done"))
        self.assertTrue(holds({"gate.ci": "github"}, "delivery.repo.main.ci", "github"))
        self.assertTrue(holds({}, "delivery.repo.main.ci", "none"))
        self.assertTrue(holds({}, "delivery.repo.main.push_pr", "user"))
        self.assertFalse(holds({}, "delivery.repo.main.push_pr", "system"))
        self.assertFalse(holds({}, "delivery.repo.main.target_branch", None))  # no default known
        self.assertFalse(holds({}, "no.such_key", None))
        self.assertFalse(holds(None, "authz.preset", "balanced"))  # unreadable: unknown whether it is set
        precs = [{**PREC, "id": "J-1", "premises": [ck("authz.preset", "balanced"), ck("supervisor.seat_retries", 2)]},
                 {**PREC, "id": "J-2", "premises": [ck("authz.preset", "conservative")]}]
        (self.sd / "precedents.md").write_text("\n".join(catalog._block(p) for p in precs))
        self.tick()  # the user config sets neither key
        got = catalog._precedents(self.root)
        self.assertEqual([got[j]["needs_review"] for j in ("J-1", "J-2")], [False, True])

    def test_a_failed_provisional_action_is_retried_then_pushed_once(self):  # REQ-17
        q1, q2 = self.ask(), self.ask()
        broken = {q1, q2}

        def apply_answer(root, rec, req):
            if rec["id"] in broken:
                raise RuntimeError("disk full")
            return "ok"
        with mock.patch.object(exemption, "apply_answer", apply_answer):
            self.settle(q1)
            self.settle(q2)
            broken.discard(q2)
            pending.overdue(self.root, q1)  # r1: an overdue one is retried as well (apply used to refuse it)
            # m2d.6: retry n is due supervisor.gate_retry_min (10) × 2^(n-1) min after failure n (events carry the
            # wall clock, so each is due counting from about self.now); then the push, then nothing
            for at, want in ((0, 1), (9 * M, 1), (11 * M, 2), (19 * M, 2), (21 * M, 3), (22 * M, 3), (60 * M, 3)):
                self.tick(at)
                self.assertEqual([e["question"] for e in self.events("pending_apply_failed")].count(q1), want, at)
        fails = [e["question"] for e in self.events("pending_apply_failed")]
        self.assertEqual((fails.count(q1), fails.count(q2)), (3, 1))
        self.assertEqual([e["question"] for e in self.events("pending_applied")], [q2])
        self.assertEqual((self.state(q1), self.state(q2)), ("overdue", "provisional"))
        pushed = [(t, b) for t, b, _ in self.rec.sent if t.startswith("暂定动作失败")]
        self.assertEqual(pushed, [(f"暂定动作失败 {q1}", f"{q1}（PV-1）的程序动作重试 2 次仍失败，暂定仍生效\n"
                                   f"查看：foremind decide show {q1}\n推翻：foremind decide {q1} --overturn")])

    def test_citations_are_checked_one_by_one(self):
        precs = [{**PREC, "id": "J-1"}, {**PREC, "id": "J-2", "superseded_by": "J-3"},
                 {**PREC, "id": "J-3", "needs_review": True}, {**PREC, "id": "J-4", "review_on": "2026-01-01"},
                 {k: v for k, v in {**PREC, "id": "J-5"}.items() if k != "conclusion"}]
        (self.sd / "precedents.md").write_text("\n".join(catalog._block(p) for p in precs))
        rec = {"id": "Q-4", "category": 3, "options": OPTS, "reversible": True}
        j = lambda *cited: decider.judge(self.root, {decider.PROJECT: {}}, rec, {}, "decider",
                                         out("Q-4", precedents_cited=list(cited)))
        self.assertEqual(j("J-1"), (1, ""))
        self.assertEqual(j("J-1", "J-2")[1], "援引的判例 J-2 已被 J-3 取代")
        self.assertEqual(j("J-3")[1], "援引的判例 J-3 需复核")
        self.assertEqual(j("J-4")[1], "援引的判例 J-4 复核日期 2026-01-01 已过")
        self.assertIn("援引的判例 J-5 不合 schema precedent", j("J-5")[1])
        self.assertEqual(j("J-9")[1], "援引的判例 J-9 不在 precedents.md")

    def test_a_block_that_is_not_an_object_escalates_instead_of_hanging(self):
        """controller r3 note on m2b.3: `[1]` used to raise AttributeError in after(), every pass, forever."""
        qid = self.ask(kind="manual")
        self.tick()  # the job starts on a readable precedents.md
        (self.sd / "precedents.md").write_text("## J-1\n\n```json\n[1]\n```\n")
        [j] = [j for j in self.jobs.started if j["argv"][0] == "claude"]
        self.jobs.finish(self.jobs.started.index(j), 0, json.dumps(out(qid)))
        self.tick()
        self.assertEqual(self.state(qid), "escalated")
        self.assertEqual(self.events("pending_escalated")[0]["reason"],
                         "决策者：precedents.md 读不了，援引的判例无从核对：判例 J-1 不是 JSON 对象")
        pending.void(self.root, qid, "x")  # else p.1 waits on the user: a full block starts nothing
        q2 = self.ask(kind="manual")
        self.tick()  # and at the start of a job
        self.assertEqual(self.state(q2), "escalated")
        with self.assertRaisesRegex(ValueError, "不是 JSON 对象"):
            provisional.watch(self.root, {})

    def test_an_irreversible_q_n_is_never_provisional(self):
        """r1 should_fix 2: the Q-n's own reversible counts, whatever the decider's output says."""
        q1, q2 = self.ask(reversible=False), self.ask(reversible=False)
        self.assertIn("answered", self.settle(q1, confidence="high"))  # answered (REQ-8), not provisional (REQ-10)
        self.assertIn("escalated", self.settle(q2, confidence="medium"))  # medium on an irreversible Q-n
        self.assertEqual((self.state(q1), self.state(q2)), ("applied", "escalated"))
        self.assertFalse(list((self.sd / "decisions").glob("PV-*.json")))

    def test_voiding_a_provisional_is_an_overturn(self):
        """r1 should_fix 1: the exemption goes, the lock holders revert, the overturn rate counts it."""
        qs = [self.ask() for _ in range(5)]
        for q in qs:
            self.settle(q)
        self.assertTrue((self.sd / "exemptions" / f"{qs[0]}.json").exists())
        pending.void(self.root, qs[0], "换了做法")
        self.assertFalse((self.sd / "exemptions" / f"{qs[0]}.json").exists())
        self.assertEqual(self.texts()[-1],
                         f"{qs[0]}（PV-1）已作废（换了做法）：按撤回方法回退：回退带 `provisional: PV-1` 的提交")
        self.assertEqual(pending.tightened(self.root), {})  # 1 in 5
        pending.overdue(self.root, qs[1])
        pending.void(self.root, qs[1], "x")  # an overdue one likewise
        self.assertEqual((self.state(qs[1]), pending.tightened(self.root)), ("void", {3: 1}))
        other = self.ask(4)  # not provisional: voided as before
        pending.void(self.root, other, "y")
        self.assertEqual(self.texts()[-1], f"{other} 已作废：y")

    def test_file_hash_premise_takes_a_repo_qualified_path(self):
        """r1 should_fix 3: `<repo-id>:<path>` under that repo's root, as in schemas' precedent example."""
        (self.root / "api" / "docs").mkdir(parents=True)
        (self.root / "api" / "docs" / "auth.md").write_text("v1")
        (self.root / "a.txt").write_text("v1")
        cfg = {"repos": [{"id": "main", "path": "."}, {"id": "api", "path": "api"}]}
        fh = lambda path: {"kind": "file_hash", "path": path, "sha256": hashlib.sha256(b"v1").hexdigest()}
        self.assertEqual([catalog.holds(self.root, cfg, fh(p)) for p in
                          ("api:docs/auth.md", "main:a.txt", "a.txt", "web:docs/auth.md", "api:../a.txt")],
                         [True, True, True, False, False])
        (self.root / "api" / "docs" / "auth.md").write_text("v2")
        self.assertFalse(catalog.holds(self.root, cfg, fh("api:docs/auth.md")))

    def test_revert_notice_reaches_someone_without_a_lock_holder(self):
        """r2 must_fix: a released seat (approved and on) must not swallow the revert notice; r3: nor a batch with no
        edge to changes_requested (already there, or running with its lock broken)."""
        q1, q2, q3, q4 = qs = [self.ask() for _ in range(4)]
        for q in qs:
            self.settle(q)
        self.assertIn("p.1 的持锁席位已收到回退通知", pending.conclude(self.root, q1, "overturned"))
        self.assertEqual(self.rec.sent, [])  # the holder has it: no push
        lock.release(self.root, "p.1", "fm-s")
        self.set_state("p.1", "approved")
        got = pending.void(self.root, q2, "换了做法")
        self.assertIn("p.1 无持锁席位，已退回 changes_requested、回退通知写进状态区", got)
        self.assertIn("已推送给用户", got)
        h, body = header.parse(self.hpath("p.1").read_text())
        self.assertEqual(h["state"], "changes_requested")
        self.assertIn(f"{q2}（PV-2）已作废（换了做法）：按撤回方法回退", body.split("## 状态")[1])
        [(title, text, prio)] = self.rec.sent
        self.assertEqual((title, prio), (f"暂定撤回 {q2}", "P1"))
        self.assertIn("回退带 `provisional: PV-2` 的提交", text)
        for q, st in ((q3, "changes_requested"), (q4, "running")):  # the must-fix list so far is kept
            self.set_state("p.1", st)
            self.assertIn(f"p.1 无持锁席位（{st}），回退通知已写进状态区", pending.conclude(self.root, q, "overturned"))
            h, body = header.parse(self.hpath("p.1").read_text())
            self.assertEqual((h["state"], body.count("## 状态")), (st, 1))
            self.assertIn(f"- {q}（PV-{q[2:]}）被用户推翻：按撤回方法回退", body.split("## 状态")[1])
            self.assertIn(f"{q2}（PV-2）已作废", body.split("## 状态")[1])
            self.assertEqual(self.rec.sent[-1][0], f"暂定撤回 {q}")
        self.assertEqual(len(self.rec.sent), 3)

    def test_a_request_undone_before_is_the_users_when_asked_again(self):
        """r2 should_fix (D9 reversed): same key, overturned or voided while provisional: not the decider again."""
        a, b = self.ask(key="k-a"), self.ask(key="k-b")
        self.settle(a)
        self.settle(b)
        pending.conclude(self.root, a, "overturned")
        pending.void(self.root, b, "x")
        self.assertEqual([self.state(self.ask(key=k)) for k in ("k-a", "k-b", "k-c")], ["open", "open", "deciding"])

    def test_a_cited_premise_is_checked_when_settling(self):
        """r2 should_fix: a premise that no longer holds, not yet marked by the phase, is not cited."""
        gone = {"kind": "file_hash", "path": "gone.txt", "sha256": "0" * 64}
        (self.sd / "precedents.md").write_text(catalog._block({**PREC, "id": "J-1", "premises": [gone]}))
        qid = self.ask()
        self.assertIn(f"援引的判例 J-1 的前提已不成立：{json.dumps(gone, ensure_ascii=False)}",
                      self.settle(qid, precedents_cited=["J-1"]))
        self.assertEqual(self.state(qid), "escalated")
