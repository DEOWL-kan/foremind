"""m2b.3: delegated Q-n go to a one-shot decider or controller (supervisor/phases/decide.py, decide/decider.py,
oneshot.py)."""
import hashlib
import json
import unittest
from unittest import mock

from foremind import catalog, config, oneshot
from foremind.decide import decider, pending, scope
from foremind.paths import state_dir
from foremind.supervisor import tick as sv
from test_supervisor import Base

OPTS = ["要", "不要"]


def out(qid, **kw):
    return {"question": qid, "category": 3, "options": OPTS, "conclusion": 1, "confidence": "high", "facts": ["f"],
            "precedents_cited": [], "reversible": True, **kw}


def ctl(qid, **kw):
    return {"item": qid, "decision": "要", "reasons": ["r"], **kw}


class OneshotTest(Base):
    def test_prepare_route_and_output(self):
        session, argv, env = oneshot.prepare(self.root, "decider", {}, {"facts.json": {"b": 1, "a": "中"}})
        mat = self.root / ".foremind" / "oneshots" / session
        self.assertEqual(env, {"FOREMIND_SESSION": session, "FOREMIND_ROLE": "decider"})
        self.assertEqual(argv[:9], ["claude", "-p", "--restricted", "--tools", "Read,Grep,Glob", "--permission-mode",
                                    "plan", "--strict-mcp-config", "--mcp-config"])
        self.assertEqual(argv[argv.index("--model") + 1:argv.index("--model") + 4],
                         ["claude-opus-5-5", "--effort", "xhigh"])
        self.assertEqual(argv[-2:], ["--", f"Read {mat / 'prompt.md'} and follow it. Output only the JSON object it "
                                           "asks for."])
        self.assertEqual((mat / "facts.json").read_text(), '{\n "a": "中",\n "b": 1\n}\n')  # reproducible
        prompt = (mat / "prompt.md").read_text()
        self.assertTrue(prompt.startswith("# 角色卡：决策者"))
        self.assertIn(f"- {mat / 'facts.json'}", prompt)
        cfg = {"routes.controller.model": "claude-opus-5-5", "routes.controller.effort": "high"}
        self.assertEqual(oneshot.route(cfg, "controller"), ("claude-opus-5-5", "high"))
        for cfg in ({"exclude.models": ["*OPUS*"]}, {"exclude.providers": ["claude"]}):
            with self.assertRaisesRegex(ValueError, "excluded"):
                oneshot.prepare(self.root, "decider", cfg, {})
        jobs = self.root / ".foremind" / "jobs"
        obj = '{"conclusion": 2, "n": {}}'
        for jid, raw in (("a", json.dumps({"result": f'x {{"y" {obj} z', "is_error": False})), ("b", obj),
                         ("c", "note {\n" + obj)):
            (jobs / jid).mkdir(parents=True)
            (jobs / jid / "stdout.log").write_text(raw)
            self.assertEqual(oneshot.output(self.root, jid, "conclusion"), {"conclusion": 2, "n": {}}, jid)
        (jobs / "d").mkdir()
        (jobs / "d" / "stdout.log").write_text(json.dumps({"result": "rate limited", "is_error": True}))
        with self.assertRaisesRegex(ValueError, "rate limited"):
            oneshot.output(self.root, "d", "conclusion")
        with self.assertRaisesRegex(ValueError, "no JSON object"):
            oneshot.output(self.root, "c", "decision")


class JudgeTest(Base):
    def qrec(self, category=3, **kw):
        return {"id": "Q-4", "category": category, "options": OPTS, "reversible": True, **kw}

    def test_decider_checks(self):
        prec = {"id": "J-1", "question": "Q-1", "category": 3, "conclusion": "c", "scope": "s",
                "premises": [{"kind": "manual", "text": "t"}], "review_on": "2027-01-01", "supersedes": [],
                "needs_review": False}
        (state_dir(self.root) / "precedents.md").write_text(
            catalog._block(prec) + catalog._block({**prec, "id": "J-2", "superseded_by": "J-3"}) +
            catalog._block({**prec, "id": "J-3", "needs_review": True}))
        j = lambda o, cfg={}, rec=self.qrec(): decider.judge(self.root, {"项目": cfg}, rec, {}, "decider", o)
        self.assertEqual(j(out("Q-4", precedents_cited=["J-1"])), (1, ""))
        self.assertEqual(j(out("Q-4", confidence="medium", conclusion=2)), (2, ""))
        bad = {"不合 decision_output": out("Q-4", conclusion=3), "与待决不一致": out("Q-5"),
               "不逐字": out("Q-4", options=["要 ", "不要"])}
        bad.update({f"判例 {x}": out("Q-4", precedents_cited=[x]) for x in ("J-2", "J-3", "J-9")})
        for why, o in bad.items():
            self.assertIn(why, j(o)[1], why)
        self.assertEqual(j(out("Q-4", confidence="low")), (None, "置信度 low"))
        self.assertEqual(j(out("Q-4", confidence="medium", reversible=False)), (None, "置信度 medium且不可撤回"))
        self.assertEqual(j(out("Q-4", confidence="medium"), rec=self.qrec(reversible=False))[0], None)
        self.assertIn("归 user", j(out("Q-4"), {"authz.preset": "conservative"})[1])  # tightened meanwhile
        self.assertIn("授权设置读不了", j(out("Q-4"), {"authz.preset": "hands_off"})[1])
        # a blocked batch's task layer took the delegation back, or cannot be read (§2.6)
        judge = lambda b: decider.judge(self.root, {"项目": {}, "批次 p.1": b}, self.qrec(), {}, "decider", out("Q-4"))
        self.assertEqual(judge({"authz.preset": "conservative"}),
                         (None, "#3 在项目的授权设置下归 decider，在批次 p.1归 user"))
        self.assertEqual(judge(None), (None, "批次 p.1的授权设置读不了：配置读不了"))
        self.assertEqual(judge({}), (1, ""))

    def test_controller_checks(self):
        scope = {"kind": "scope", "batch": "p.1", "match": {"paths": ["main:a", "main:b"]}, "approve_option": 1}
        j = lambda o, req=scope, rec=self.qrec(8, recommended=1): decider.judge(self.root, {"项目": {}}, rec, req,
                                                                             "controller", o)
        sc = lambda paths, batch="p.1": {"batch": batch, "add_owns_paths": paths}
        self.assertEqual(j(ctl("Q-4", decision="不要"), rec=self.qrec(8, recommended=2)), (2, ""))
        self.assertEqual(j(ctl("Q-4", decision="不要", reasons=["a.py 只有本批用", "不影响别的批次"])),  # REQ-17
                         (None, "选了 2（不要），与推荐的 1 不同，交用户定；总控的理由：a.py 只有本批用；不影响别的批次"))
        self.assertEqual(j(ctl("Q-4", scope_change=sc(["main:b", "main:a"]))), (1, ""))
        bad = {"不合 controller_decision": ctl("Q-4", reasons=[]), "不是 Q-4": ctl("Q-5"),
               "任何一个选项": ctl("Q-4", decision="要吧")}
        for why, o in bad.items():  # m2e REQ-5: not a valid answer, no reasons to pass on
            self.assertIn(why, j(o)[1], why)
            self.assertNotIn("总控的理由", j(o)[1], why)
        escalated = {"plan_amend": ctl("Q-4", plan_amend=[{"batch": "p.1", "changes": {"x": 1}}]),
                     "一部分": ctl("Q-4", scope_change=sc(["main:a"])),
                     "超出": ctl("Q-4", scope_change=sc(["main:a", "main:c"])),
                     "超出了请求的批次": ctl("Q-4", scope_change=sc(["main:a", "main:b"], "p.2")),
                     "随批准": ctl("Q-4", decision="不要", scope_change=sc(["main:a", "main:b"]))}
        for why, o in escalated.items():  # m2e REQ-5: each escalation of a valid answer ends with the reasons
            self.assertIn(why, j({**o, "reasons": ["x", "y"]})[1], why)
            self.assertTrue(j({**o, "reasons": ["x", "y"]})[1].endswith("；总控的理由：x；y"), why)
        self.assertIn("随批准", j(ctl("Q-4", scope_change=sc(["main:a"])), {**scope, "kind": "manual"})[1])
        self.assertIn("不归一次性总控",
                      decider.judge(self.root, {"项目": {}}, self.qrec(3), scope, "controller", ctl("Q-4"))[1])

    def test_sample(self):
        for q in ("Q-1", "Q-2", "Q-3", "Q-17"):
            want = int(hashlib.sha256(q.encode()).hexdigest()[:8], 16) / 2 ** 32 < 0.2
            self.assertEqual(decider.sampled(q, {}), want, q)
            self.assertEqual([decider.sampled(q, {"decider.audit_ratio": r}) for r in (0, 1, "x")], [False, True, True])


class PhaseTest(Base):
    def setUp(self):
        super().setUp()
        self.plan("p")
        self.hold("p.1", "fm-s")

    def ask(self, category=3, blocks=("p.1",), **req):
        rec, _ = pending.create(self.root, config.load(self.root), question="要不要？", options=OPTS, recommended=1,
                                reason="r", category=category, blocks=list(blocks), reversible=True,
                                request={"kind": "manual", "category": category, "batch": blocks[0], **req})
        return rec["id"]

    def oneshots(self):
        return [j for j in self.jobs.started if j["argv"][0] == "claude"]

    def reply(self, n, obj, code=0):
        raw = obj if isinstance(obj, str) else json.dumps({"type": "result", "is_error": False,
                                                            "result": "结论：" + json.dumps(obj, ensure_ascii=False)})
        self.jobs.finish(self.jobs.started.index(self.oneshots()[n]), code, raw)

    def state(self, qid):
        return pending.load(self.root, qid)["state"]

    def test_decider_answers_with_audit_sample(self):
        qid = self.ask()
        self.assertEqual(self.state(qid), "deciding")
        self.tick()
        self.tick()  # in flight: not started again
        [j] = self.oneshots()
        self.assertEqual((j["env"]["FOREMIND_ROLE"], j["argv"][j["argv"].index("--session-id") + 1]),
                         ("decider", j["env"]["FOREMIND_SESSION"]))
        mat = self.root / ".foremind" / "oneshots" / j["env"]["FOREMIND_SESSION"]
        facts = json.loads((mat / "facts.json").read_text())
        self.assertNotIn("code", facts["pending"])
        self.assertEqual((facts["request"]["kind"], facts["authz_row"]["owner"], facts["batches"], facts["precedents"]),
                         ("manual", "decider", {"p.1": {"state": "running", "review_rounds": 0}}, {}))
        self.assertIn(qid, facts["task"])
        self.reply(0, out(qid, reversible=False))  # m2b.4: reversible on #3 would be provisional
        self.tick()
        self.assertEqual(self.state(qid), "applied")
        [a] = self.events("pending_answered")
        self.assertEqual((a["answer"], a["by"]), (1, "decider"))
        [d] = self.events("decider_decided")
        self.assertEqual({k: d[k] for k in ("question", "category", "conclusion", "confidence", "sample")},
                         {"question": qid, "category": 3, "conclusion": 1, "confidence": "high",
                          "sample": decider.sampled(qid, {})})
        self.assertTrue(any(f"{qid} 已答复：要" in text for _, text in self.carrier.sent), self.carrier.sent)
        self.assertEqual(self.rec.sent, [])

    def test_second_failure_escalates_and_pushes(self):
        qid = self.ask()
        self.tick()
        self.reply(0, "", code=1)
        self.tick()  # failed once: started again
        self.assertEqual((len(self.oneshots()), self.state(qid)), (2, "deciding"))
        self.reply(1, "我拿不准")
        self.tick()
        self.assertEqual((len(self.oneshots()), self.state(qid)), (2, "escalated"))
        [e] = self.events("pending_escalated")
        self.assertIn("2 次没有给出结论", e["reason"])
        self.assertEqual([t for t, _, _ in self.rec.sent if t.startswith("待决")], [f"待决 {qid}"])

    def test_low_confidence_escalates_and_the_user_can_preempt(self):
        q1, q2 = self.ask(), self.ask()
        self.tick()
        self.reply(0, out(q1, confidence="low"))
        self.reply(1, out(q2))
        with mock.patch.dict("os.environ", {"FOREMIND_SESSION": ""}):
            pending.answer(self.root, q2, 2)  # the user answered while the decider ran
        self.tick()
        self.assertEqual((self.state(q1), self.state(q2)), ("escalated", "applied"))
        self.assertEqual(self.events("pending_escalated")[0]["reason"], "决策者：置信度 low")
        self.assertEqual([(a["by"], a["answer"]) for a in self.events("pending_answered")], [("user", 2)])
        self.assertEqual(self.events("decider_decided"), [])

    def test_controller_scope_change_goes_through_scope_handler(self):
        qid = self.ask(8, kind="scope", match={"paths": ["main:p/x"]}, approve_option=1)
        self.tick()
        [j] = self.oneshots()
        self.assertEqual(j["env"]["FOREMIND_ROLE"], "controller")
        self.reply(0, ctl(qid, scope_change={"batch": "p.1", "add_owns_paths": ["main:p/x"]}))
        with mock.patch("foremind.decide.scope.expand_scope") as widen:
            self.tick()
        self.assertEqual((self.state(qid), widen.call_args.kwargs["approved_by"]), ("applied", "controller"))  # I26
        self.assertEqual(self.events("pending_answered")[0]["by"], "controller")
        self.assertEqual(self.events("decider_decided"), [])
        with self.assertRaisesRegex(ValueError, "找不到答复事件"):  # nobody's approval widens nothing
            scope.apply_answer(self.root, {"id": "Q-99"}, {"batch": "p.1", "match": {"paths": ["main:p/y"]}})

    def test_controller_going_against_the_recommendation_is_escalated(self):  # REQ-17
        qid = self.ask(13)
        self.tick()
        self.reply(0, ctl(qid, decision="不要", reasons=["还没到时候"]))
        self.tick()
        self.assertEqual((self.state(qid), self.events("pending_answered")), ("escalated", []))
        self.assertEqual(self.events("pending_escalated")[0]["reason"],
                         "一次性总控：选了 2（不要），与推荐的 1 不同，交用户定；总控的理由：还没到时候")
        self.assertEqual([t for t, _, _ in self.rec.sent if t.startswith("待决")], [f"待决 {qid}"])

    def test_one_shot_cap_and_tightened_preset(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_oneshot = 1\n')
        q1, q2 = self.ask(), self.ask()
        self.tick()
        self.assertEqual(len(self.oneshots()), 1)
        self.reply(0, out(q1))
        self.tick()  # q1 settled; q2 takes the slot
        self.assertEqual((self.state(q1), len(self.oneshots())), ("provisional", 2))  # m2b.4: #3 reversible
        q3 = self.ask()
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[authz]\npreset = "conservative"\n')
        self.tick()  # #3 is the user's now: escalated without a job
        self.assertEqual((self.state(q3), len(self.oneshots())), ("escalated", 2))
        self.reply(1, out(q2))
        self.tick()
        self.assertEqual(self.state(q2), "escalated")
        self.assertIn("归 user", self.events("pending_escalated")[-1]["reason"])

    def test_catalog_request_and_batch_task_layer_stay_with_the_user(self):
        self.assertEqual(self.state(self.ask(10, kind="catalog", precedent={})), "open")  # §20 I32, I63
        [r] = self.events("pending_routed")
        self.assertEqual((r["owner"], r["to"]), ("decider", "user"))
        self.plan("c", {"config": {"authz": {"preset": "conservative"}}})
        self.plan("x", {"config": "not a table"})
        q1, q2 = self.ask(blocks=("p.1", "c.1")), self.ask(blocks=("x.1",))
        q3 = self.ask(blocks=("p.1", "nope.1"))
        q4 = self.ask(batch="c.1")  # blocks p.1 only, but a credential would be c.1's (r2#1)
        self.assertEqual({self.state(q) for q in (q1, q2, q3, q4)}, {"deciding"})  # created on the project config
        self.tick()
        self.assertEqual(([self.state(q) for q in (q1, q2, q3, q4)], self.oneshots()), (["escalated"] * 4, []))
        self.assertEqual([e["reason"] for e in self.events("pending_escalated")],
                         ["#3 在项目的授权设置下归 decider，在批次 c.1归 user",
                          "批次 x.1的授权设置读不了：配置读不了", "批次 nope.1的授权设置读不了：配置读不了",
                          "#3 在项目的授权设置下归 decider，在批次 c.1归 user"])

    def test_reviews_started_this_pass_fill_the_slots_and_escalating_needs_none(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[supervisor]\nmax_oneshot = 1\n')
        self.plan("c", {"config": {"authz": {"preset": "conservative"}}})
        q1, q2 = self.ask(), self.ask(batch="c.1")

        def start_reviews(t):  # what review.start writes: the log only (r2#2)
            t.log.append("review_started", phase="intent", dedupe_id="review:p.1:r", batch="p.1")
        with mock.patch.object(sv.Tick, "start_reviews", start_reviews):
            self.tick()
        self.assertEqual(([self.state(q) for q in (q1, q2)], self.oneshots()), (["deciding", "escalated"], []))  # r2#3

    def test_unreadable_precedents_escalate(self):
        (state_dir(self.root) / "precedents.md").write_text("## J-1\n\n```json\n{broken\n```\n")
        q1 = self.ask()
        self.tick()
        self.assertEqual((self.state(q1), self.oneshots()), ("escalated", []))
        self.assertIn("precedents.md 读不了", self.events("pending_escalated")[0]["reason"])
        (state_dir(self.root) / "precedents.md").unlink()
        pending.void(self.root, q1, "x")  # else p.1 waits on the user: a full block starts nothing
        q2 = self.ask()
        self.tick()
        (state_dir(self.root) / "precedents.md").write_text("## J-1\n\n```json\n{broken\n```\n")
        self.reply(0, out(q2, precedents_cited=["J-1"]))
        self.tick()  # r2#4: settled, not retried every pass
        self.assertEqual(self.state(q2), "escalated")
        self.assertIn("援引的判例无从核对", self.events("pending_escalated")[-1]["reason"])

    def test_excluded_model_escalates_without_a_job(self):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[exclude]\nmodels = ["*opus*"]\n')
        qid = self.ask(13)
        self.tick()
        self.assertEqual((self.state(qid), self.oneshots()), ("escalated", []))
        self.assertIn("一次性总控起不来", self.events("pending_escalated")[0]["reason"])


if __name__ == "__main__":
    unittest.main()
