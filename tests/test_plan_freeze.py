from unittest import mock

from foremind import header as hdr
from foremind import lock, schemas
from foremind.events import EventLog
from foremind.fsutil import atomic_write
from foremind.plan import freeze, model
from foremind.plan.model import Doc, PlanError
from foremind.plan.validate import validate
from test_plan_helpers import ProjectCase, goal_doc, header, make_plan


class FreezeTest(ProjectCase):
    def events(self, type):
        return [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == type]

    def test_freeze_goal(self):
        h = freeze.freeze_goal(self.root, "p", "REQ-1: 登录\n")
        self.assertEqual(h, model.text_hash("REQ-1: 登录\n"))
        self.assertEqual(freeze.freeze_goal(self.root, "p"), h)  # unchanged: no new event
        self.assertEqual(len(self.events("goal_frozen")), 1)
        with self.assertRaisesRegex(PlanError, "frozen"):
            freeze.freeze_goal(self.root, "p", "REQ-1: 登录和注册\n")
        path = self.root / ".foremind" / "plans" / "p" / "goal.md"
        path.write_text(path.read_text() + "偷改\n")
        with self.assertRaisesRegex(PlanError, "frozen"):
            freeze.freeze_goal(self.root, "p")
        with self.assertRaisesRegex(PlanError, "REQ-n"):
            freeze.freeze_goal(self.root, "q", "没有编号\n")

    def test_approve(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"], ["p.1"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        self.assertEqual(plan.doc.header["approved_by"], "user")
        self.assertIn("approved_at", plan.doc.header)
        self.assertEqual([d.header["state"] for d in plan.batches.values()], ["planned", "planned"])
        self.assertEqual(self.events("plan_approved")[0]["goal_hash"], plan.doc.header["goal_hash"])
        with self.assertRaisesRegex(PlanError, "already approved"):
            freeze.approve(self.root, "p")

    def test_approve_refuses_overlap(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:*.py"])]))
        with self.assertRaises(freeze.Rejected) as cm:
            freeze.approve(self.root, "p")
        self.assertEqual(cm.exception.report["suggested_edges"], [{"batch": "p.2", "depends_on": "p.1"}])
        self.assertNotIn("approved_at", model.load(self.root, "p").doc.header)

    def test_amend_goal_needs_user_approval(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        with self.assertRaisesRegex(PlanError, "user's approval"):
            freeze.amend(self.root, "p", reason="改目标", goal="REQ-1: 新目标\n")
        freeze.amend(self.root, "p", reason="用户改了目标", goal="REQ-1: 新目标\n", user_approved=True)
        plan = model.load(self.root, "p")
        self.assertEqual(plan.doc.header["goal_hash"], model.text_hash("REQ-1: 新目标\n"))
        self.assertEqual(plan.doc.header["revisions"][0]["approved_by"], "user")
        self.assertEqual(plan.doc.header["revisions"][0]["goal_hash"], plan.doc.header["goal_hash"])
        self.assertEqual(plan.goal.header["sha256"], plan.doc.header["goal_hash"])

    def test_amend_before_approval_does_not_bind(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.amend(self.root, "p", reason="改目标", goal="REQ-1: 新目标\n", user_approved=True)
        self.assertFalse(model.is_bound(self.root, model.load(self.root, "p")))  # the supervisor must not promote it
        freeze.approve(self.root, "p")
        self.assertTrue(model.is_bound(self.root, model.load(self.root, "p")))

    def test_amend_widening_task_authorisation(self):
        self.cfg_home.mkdir()
        (self.cfg_home / "config.toml").write_text('[delivery]\nlevel = {ceiling = "merge_dev", value = "done"}\n')
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        wide = Doc(header("p.1", ["main:a.py"], config={"delivery": {"level": "merge_dev"}}), "## 状态\n")
        with self.assertRaises(freeze.Rejected) as cm:
            freeze.amend(self.root, "p", reason="提高交付级别", batches=[wide])
        self.assertIn("needs user approval", cm.exception.report["errors"][0])
        self.assertNotIn("config", model.load(self.root, "p").batches["p.1"].header)
        freeze.amend(self.root, "p", reason="用户同意提高交付级别", batches=[wide], user_approved=True)
        plan = model.load(self.root, "p")
        h = plan.batches["p.1"].header
        self.assertTrue(model.task_config_approved(self.root, plan, "p.1"))
        self.assertEqual(h["state"], "planned")  # program fields survive the replacement
        ceiling = Doc(header("p.1", ["main:a.py"], config={"delivery": {"level": {"ceiling": "merge_dev2"}}}), "")
        with self.assertRaises(freeze.Rejected):  # never allowed, approved or not
            freeze.amend(self.root, "p", reason="r", batches=[ceiling], user_approved=True)

    def test_bound_goal_is_not_swapped(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        g1 = model.load(self.root, "p").doc.header["goal_hash"]
        path = self.root / ".foremind" / "plans" / "p" / "goal.md"
        text = path.read_text()
        path.unlink()
        with self.assertRaisesRegex(PlanError, "user's approval"):  # plan.md's goal_hash is the goal
            freeze.freeze_goal(self.root, "p", "REQ-1: 新目标\n")
        path.write_text(text.replace("frozen_at", "x_frozen_at"))
        with self.assertRaisesRegex(PlanError, "user's approval"):
            freeze.freeze_goal(self.root, "p", "REQ-1: 新目标\n")
        self.assertEqual(freeze.freeze_goal(self.root, "p", "REQ-1: 做一件事\n"), g1)  # restoring is fine
        new = "REQ-1: 新目标\n"  # goal.md rewritten by hand with a matching sha256 line
        model.write_goal(self.root, "p", goal_doc(new))
        self.assertIn("restore goal.md", "".join(validate(self.root, model.load(self.root, "p"))["errors"]))
        with self.assertRaisesRegex(PlanError, "restore goal.md"):
            freeze.amend(self.root, "p", reason="x")
        self.assertEqual(model.load(self.root, "p").doc.header["goal_hash"], g1)
        freeze.amend(self.root, "p", reason="用户认可新目标", user_approved=True)
        self.assertEqual(model.load(self.root, "p").doc.header["goal_hash"], model.text_hash(new))

    def test_plan_hash_binds_batches(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        self.assertEqual(self.events("plan_approved")[0]["plan_hash"], model.plan_hash(plan))
        self.assertTrue(model.is_bound(self.root, plan))
        h = plan.batches["p.1"].header
        h |= {"state": "running", "blocked_reason": "quota"}  # runtime fields and the status section are not bound
        plan.batches["p.1"].body += "进行中\n"
        model.write(self.root, plan)
        self.assertTrue(model.is_bound(self.root, model.load(self.root, "p")))
        h["owns_paths"] = ["main:*"]  # edited by hand after approval
        model.write(self.root, plan)
        self.assertFalse(model.is_bound(self.root, model.load(self.root, "p")))
        h["owns_paths"] = ["main:a.py"]
        h["config"] = {"delivery": {"level": "merge_dev"}}  # a self-computed stamp does not count
        h["config_approved"] = model.config_hash(h)
        model.write(self.root, plan)
        self.assertFalse(model.task_config_approved(self.root, model.load(self.root, "p"), "p.1"))

    def test_bound_survives_rotation(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        EventLog(self.root / ".foremind" / "events.jsonl").rotate(self.root / ".foremind" / "archive", "2099-01")
        self.assertEqual(self.events("plan_approved"), [])
        self.assertTrue(model.is_bound(self.root, model.load(self.root, "p")))

    def test_hand_edited_plan_is_not_rebound(self):
        new = "REQ-1: 新目标\n"

        def drop_hard_block(plan):
            plan.batches["p.1"].header["hard_block"] = []

        def swap_goal(plan):
            model.write_goal(self.root, "p", goal_doc(new))
            plan.doc.header["goal_hash"] = model.text_hash(new)

        for edit in (drop_hard_block, swap_goal):
            with self.subTest(edit.__name__):
                self.setUp()
                self.save(make_plan("p", [header("p.1", ["main:a.py"], mode="watch", hard_block=[3])]))
                freeze.approve(self.root, "p")
                plan = model.load(self.root, "p")
                edit(plan)
                model.write(self.root, plan)
                self.assertIn("changed outside plan amend", "".join(validate(self.root, model.load(self.root, "p"))
                                                                    ["warnings"]))
                with self.assertRaisesRegex(PlanError, "changed outside plan amend"):
                    freeze.amend(self.root, "p", reason="x")
                self.assertEqual(len(self.events("plan_amended")), 0)
                freeze.amend(self.root, "p", reason="用户认可", user_approved=True)
                self.assertTrue(model.is_bound(self.root, model.load(self.root, "p")))

    def test_user_approval_stamps_only_the_batches_it_shows(self):
        self.cfg_home.mkdir()
        (self.cfg_home / "config.toml").write_text('[delivery]\nlevel = {ceiling = "merge_dev", value = "done"}\n')
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        wide = {"delivery": {"level": "merge_dev"}}
        freeze.amend(self.root, "p", reason="用户同意 p.1 提高交付级别",
                     batches=[Doc(header("p.1", ["main:a.py"], config=wide), "")], user_approved=True)
        freeze.amend(self.root, "p", reason="controller 改 p.2", batches=[Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])
        plan = model.load(self.root, "p")
        self.assertTrue(model.task_config_approved(self.root, plan, "p.1"))  # the user's stamp survives
        h = plan.batches["p.2"].header  # widened by hand, with a self-computed stamp
        h |= {"config": wide, "config_approved": model.config_hash({"config": wide})}
        model.write(self.root, plan)
        with self.assertRaises(freeze.Rejected) as cm:  # approving p.1 does not approve p.2
            freeze.amend(self.root, "p", reason="用户同意改 p.1", batches=[Doc(header("p.1", ["main:a.py"], config=wide), "")],
                         user_approved=True)
        self.assertIn("p.2: task config needs user approval", "".join(cm.exception.report["errors"]))
        only_p2 = [Doc(header("p.2", ["main:b.py", "main:c.py"], config=wide), "")]
        with self.assertRaises(freeze.Rejected) as cm:  # the hand edit unbound the plan: p.1's stamp lapsed too
            freeze.amend(self.root, "p", reason="用户同意 p.2", batches=only_p2, user_approved=True)
        self.assertIn("p.1: task config needs user approval", "".join(cm.exception.report["errors"]))
        freeze.amend(self.root, "p", reason="用户同意 p.1 和 p.2", user_approved=True,
                     batches=[Doc(header("p.1", ["main:a.py"], config=wide), ""), *only_p2])
        plan = model.load(self.root, "p")
        self.assertTrue(model.task_config_approved(self.root, plan, "p.1"))
        self.assertTrue(model.task_config_approved(self.root, plan, "p.2"))

    def test_revision_reason_is_not_prose(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"])]))
        freeze.approve(self.root, "p")
        freeze.amend(self.root, "p", reason="去掉 v1 兼容层")
        freeze.amend(self.root, "p", reason="再改一次")
        self.assertEqual(len(model.load(self.root, "p").doc.header["revisions"]), 2)

    def test_amend_never_edits_a_started_batch(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        plan.batches["p.2"].header["state"] = "running"
        model.write(self.root, plan)
        freeze.amend(self.root, "p", reason="p.1 也要改 b.py", batches=[Doc(header("p.1", ["main:a.py", "main:b.py"]),
                                                                            "## 状态\n")])
        plan = model.load(self.root, "p")
        self.assertEqual(plan.batches["p.1"].header["depends_on"], ["p.2"])
        self.assertEqual(plan.batches["p.2"].header["depends_on"], [])

    def test_user_approved_amend_of_a_started_batch(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        plan.batches["p.1"].header["state"] = "running"
        plan.batches["p.1"].body = "## 状态\n席位写的\n"
        model.write(self.root, plan)
        wider = Doc(header("p.1", ["main:a.py", "main:d.py"], accept_commands=["make test"],
                           tools=[{"name": "rg", "step": "找"}]), "## 状态\n")
        with self.assertRaisesRegex(PlanError, "needs the user's approval"):
            freeze.amend(self.root, "p", reason="r", batches=[wider])
        for bad, msg in ((header("p.1", ["main:d.py"]), "only grow"), (header("p.1", ["main:a.py"], mode="watch"),
                                                                        r"not \['mode'\]")):
            with self.assertRaisesRegex(PlanError, msg):
                freeze.amend(self.root, "p", reason="r", batches=[Doc(bad, "## 状态\n")], user_approved=True)
        with self.assertRaisesRegex(PlanError, "body above"):
            freeze.amend(self.root, "p", reason="r", batches=[Doc(header("p.1", ["main:a.py"]), "新说明\n")],
                         user_approved=True)
        freeze.amend(self.root, "p", reason="用户同意 p.1 多写 d.py", batches=[wider], user_approved=True)
        d = model.load(self.root, "p").batches["p.1"]
        self.assertEqual((d.header["owns_paths"], d.header["state"]), (["main:a.py", "main:d.py"], "running"))
        self.assertEqual(d.body, "## 状态\n席位写的\n")

    def test_expand_scope(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"], ["p.1"]),
                                  header("p.3", ["main:c.py"])]))
        freeze.approve(self.root, "p")
        with self.assertRaisesRegex(PlanError, "not started"):
            freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-1", approved_by="user")
        plan = model.load(self.root, "p")
        for b in ("p.1", "p.2", "p.3"):
            plan.batches[b].header["state"] = "running"
        plan.batches["p.3"].header |= {"state": "blocked", "state_prior": "running", "blocked_reason": "quota"}
        model.write(self.root, plan)
        with self.assertRaisesRegex(PlanError, "started batch p.3"):  # a side branch of a started state still holds
            freeze.expand_scope(self.root, "p.1", ["main:c.py"], decision="Q-1", approved_by="user")
        with self.assertRaisesRegex(PlanError, "already owns"):
            freeze.expand_scope(self.root, "p.1", ["main:a.py"], decision="Q-1", approved_by="user")
        # p.2 depends on p.1: they are serial, the overlap is S6's to judge (and it lets a dependency pass)
        freeze.expand_scope(self.root, "p.1", ["main:b.py"], decision="Q-2", approved_by="controller")
        plan = model.load(self.root, "p")
        self.assertEqual(plan.batches["p.1"].header["owns_paths"], ["main:a.py", "main:b.py"])
        self.assertEqual(plan.doc.header["revisions"][-1]["decision"], "Q-2")
        self.assertTrue(model.is_bound(self.root, plan))
        goal = self.root / ".foremind" / "plans" / "p" / "goal.md"
        text = goal.read_text()
        model.write_goal(self.root, "p", goal_doc("REQ-1: 做两件事\n"))  # re-frozen by hand: a #8 answer never binds it (#9)
        with self.assertRaisesRegex(PlanError, "goal.md is not the goal"):
            freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-3", approved_by="user")
        goal.write_text(text)
        path = model.batch_path(self.root, "p.3")
        path.write_text(path.read_text().replace("main:c.py", "main:cc.py"))  # hand edit: unbound
        for by in ("controller", "user"):  # a #8 answer does not approve edits nobody showed the user
            with self.assertRaisesRegex(PlanError, "outside plan amend"):
                freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-3", approved_by=by)
        self.assertEqual(model.load(self.root, "p").batches["p.1"].header["owns_paths"], ["main:a.py", "main:b.py"])

    def test_finished_batch_is_not_amended_even_with_approval(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        for st in ("merged", "cataloged", "cancelled"):
            plan = model.load(self.root, "p")
            plan.batches["p.1"].header["state"] = st
            model.write(self.root, plan)
            with self.assertRaisesRegex(PlanError, f"p.1 is {st}: .*not amended"):
                freeze.amend(self.root, "p", reason="r", batches=[Doc(header("p.1", ["main:a.py", "main:d.py"]),
                                                                      "## 状态\n")], user_approved=True)

    def test_controller_amend_only_tightens(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"], mode="watch", hard_block=[3]),
                                  header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        for kw, msg in (({"mode": "watch", "hard_block": []}, "hard_block"), ({"mode": "auto", "hard_block": [3]}, "looser")):
            with self.assertRaisesRegex(PlanError, msg):
                freeze.amend(self.root, "p", reason="r", batches=[Doc(header("p.1", ["main:a.py"], **kw), "")])
        freeze.amend(self.root, "p", reason="收紧", batches=[Doc(header("p.1", ["main:a.py"], mode="user",
                                                                       hard_block=[3, 4]), "")])
        freeze.amend(self.root, "p", reason="用户同意放宽", batches=[Doc(header("p.1", ["main:a.py"]), "")],
                     user_approved=True)
        self.assertEqual(model.load(self.root, "p").batches["p.1"].header["mode"], "auto")
        plan = model.load(self.root, "p")
        plan.batches["p.2"].header["state"] = "running"
        model.write(self.root, plan)
        with self.assertRaisesRegex(PlanError, "started batch needs the user's approval"):
            freeze.amend(self.root, "p", reason="r", drop=["p.2"])
        freeze.amend(self.root, "p", reason="用户放弃 p.2", drop=["p.2"], user_approved=True)
        self.assertEqual(model.load(self.root, "p").batches["p.2"].header["state"], "cancelled")

    def test_amend_adds_edges_drops_and_keeps_started_batches(self):
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        new = Doc(header("p.3", ["main:a.py"]), "## 状态\n")
        report = freeze.amend(self.root, "p", reason="加一批", batches=[new], drop=["p.2"])
        self.assertTrue(report["ok"])
        plan = model.load(self.root, "p")
        self.assertEqual(plan.batches["p.3"].header["depends_on"], ["p.1"])
        self.assertEqual(plan.batches["p.3"].header["state"], "planned")
        self.assertEqual(plan.batches["p.2"].header["state"], "cancelled")
        self.assertEqual(self.events("plan_amended")[0]["added_edges"], [{"batch": "p.3", "depends_on": "p.1"}])
        for d in plan.batches.values():
            self.assertEqual(schemas.validate("batch_header", d.header), [])
        plan.batches["p.1"].header["state"] = "running"
        model.write(self.root, plan)
        with self.assertRaisesRegex(PlanError, "started batch"):
            freeze.amend(self.root, "p", reason="改在跑的批", batches=[Doc(header("p.1", ["main:z.py"]), "")])
        with self.assertRaisesRegex(PlanError, "reason"):
            freeze.amend(self.root, "p", reason=" ")


class AmendIntentTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.log = EventLog(self.root / ".foremind" / "events.jsonl")
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")

    def events(self, type):
        return [e for e in self.log.iter() if e["type"] == type]

    def crash_amend(self, **kw):
        """An amend that stops after its first batch file (the intent is written, plan.md and plan_amended are not)."""
        real = model.write

        def write(root, plan):
            (bid, d), = list(plan.batches.items())[:1]
            atomic_write(model.batch_path(root, bid), hdr.render(d.header, d.body))
            raise KeyboardInterrupt

        with mock.patch.object(model, "write", write), self.assertRaises(KeyboardInterrupt):
            freeze.amend(self.root, "p", **kw)
        self.assertIs(model.write, real)
        self.assertEqual(len(self.log.open_intents()), 1)

    def test_intent_then_result(self):
        freeze.amend(self.root, "p", reason="p.2 多写 c.py", batches=[Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])
        (it,), (res,) = self.events("plan_amend"), self.events("plan_amended")
        self.assertEqual((it["phase"], res["phase"], res["dedupe_id"]), ("intent", "result", it["dedupe_id"]))
        self.assertEqual(self.log.open_intents(), [])
        files = {f["path"]: f for f in it["files"]}
        self.assertEqual(sorted(files), ["batches/p.1.md", "batches/p.2.md", "plans/p/plan.md"])
        self.assertNotIn("text", files["batches/p.1.md"])  # unchanged: its hash only
        p2 = self.root / ".foremind" / "batches" / "p.2.md"
        self.assertEqual(files["batches/p.2.md"]["text"], p2.read_text())
        self.assertEqual(files["batches/p.2.md"]["sha256"], model.text_hash(p2.read_text()))
        self.assertEqual(it["result"]["plan_hash"], res["plan_hash"])

    def test_next_amend_finishes_an_interrupted_one(self):
        self.crash_amend(reason="p.1 多写 c.py", batches=[Doc(header("p.1", ["main:a.py", "main:c.py"]), "## 状态\n")],
                         drop=["p.2"])
        self.assertFalse(model.is_bound(self.root, model.load(self.root, "p")))  # p.1 written, plan.md not
        report = freeze.amend(self.root, "p", reason="再改 p.1", batches=[Doc(header("p.1", ["main:a.py", "main:c.py", "main:d.py"]), "")])
        self.assertIn("已按意图记录的哈希补完", report["warnings"][0])
        plan = model.load(self.root, "p")
        self.assertEqual([r["reason"] for r in plan.doc.header["revisions"]], ["p.1 多写 c.py", "再改 p.1"])
        self.assertEqual(plan.batches["p.2"].header["state"], "cancelled")
        self.assertTrue(model.is_bound(self.root, plan))
        self.assertTrue(self.events("plan_amended")[0]["recovered"])
        self.assertEqual(self.log.open_intents(), [])

    def test_changed_since_is_left_alone_and_reported(self):
        self.crash_amend(reason="r", batches=[Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])
        path = model.batch_path(self.root, "p.2")  # not written yet; changed by someone else since the crash
        path.write_text(path.read_text().replace("main:b.py", "main:bb.py"))
        plan_md = (self.root / ".foremind" / "plans" / "p" / "plan.md").read_text()
        with self.assertRaisesRegex(PlanError, r"补不完：batches/p\.2\.md 在中断后又被改过(.|\n)*outside plan amend"):
            freeze.amend(self.root, "p", reason="x")
        (failed,) = self.events("plan_amend_failed")
        self.assertEqual(failed["left"], ["batches/p.2.md"])
        self.assertIn("main:bb.py", path.read_text())  # not overwritten, and nothing else written either
        self.assertEqual((self.root / ".foremind" / "plans" / "p" / "plan.md").read_text(), plan_md)
        self.assertEqual(self.log.open_intents(), [])  # reported once
        report = freeze.amend(self.root, "p", reason="用户核对后重批", user_approved=True)
        self.assertEqual(self.events("plan_amend_failed"), [failed])
        self.assertNotIn("写到一半", "".join(report["warnings"]))
        self.assertTrue(model.is_bound(self.root, model.load(self.root, "p")))

    def set_runtime(self, bid, state):
        """What the supervisor and a seat write after the crash: state, and the `## 状态` section."""
        path = model.batch_path(self.root, bid)
        d = model.read(path)
        d.header["state"] = state
        atomic_write(path, hdr.render(d.header, d.body + "## 状态\n进行中\n"))

    def test_stopped_before_any_file_the_plan_stays_bound(self):  # REQ-18
        with mock.patch.object(model, "write", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            freeze.amend(self.root, "p", reason="r", batches=[Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])
        self.set_runtime("p.2", "ready")  # nothing of the amend written; the supervisor moved p.2 on since
        report = freeze.amend(self.root, "p", reason="再改", batches=[Doc(header("p.1", ["main:a.py", "main:d.py"]), "")])
        self.assertEqual(report["warnings"][0], "上次修订 1 写到一半中断，补不完：batches/p.2.md 在中断后又被改过；"
                                                "修订 1 一个文件也没写，没有生效，计划仍绑定；需要就重新 amend")
        self.assertEqual(self.events("plan_amend_failed")[0]["left"], ["batches/p.2.md"])
        plan = model.load(self.root, "p")
        self.assertEqual((plan.batches["p.1"].header["owns_paths"], plan.batches["p.2"].header["owns_paths"]),
                         (["main:a.py", "main:d.py"], ["main:b.py"]))
        self.assertTrue(model.is_bound(self.root, plan))

    def test_written_then_changed_at_runtime_still_finishes(self):
        self.crash_amend(reason="r", batches=[Doc(header("p.1", ["main:a.py", "main:c.py"]), ""),
                                              Doc(header("p.2", ["main:b.py", "main:d.py"]), "")])
        self.set_runtime("p.1", "running")  # p.1 was written before the crash
        report = freeze.amend(self.root, "p", reason="再改", batches=[Doc(header("p.2", ["main:b.py", "main:e.py"]), "")])
        self.assertIn("已按意图记录的哈希补完", report["warnings"][0])
        plan = model.load(self.root, "p")
        self.assertEqual(plan.batches["p.1"].header["state"], "running")
        self.assertIn("main:c.py", plan.batches["p.1"].header["owns_paths"])
        self.assertTrue(model.is_bound(self.root, plan))

    def test_a_dropped_batch_that_started_since_is_not_written(self):
        self.crash_amend(reason="r", batches=[Doc(header("p.1", ["main:a.py", "main:c.py"]), "")], drop=["p.2"])
        self.set_runtime("p.2", "ready")  # p.2 not yet cancelled when it crashed; the supervisor moved it on since
        with self.assertRaisesRegex(PlanError, r"补不完：batches/p\.2\.md"):
            freeze.amend(self.root, "p", reason="x")
        self.assertEqual(self.events("plan_amend_failed")[0]["left"], ["batches/p.2.md"])
        self.assertEqual(model.load(self.root, "p").batches["p.2"].header["state"], "ready")
        self.assertEqual(self.events("plan_amended"), [])

    def test_a_file_the_amend_does_not_change_may_drift(self):
        self.crash_amend(reason="r", batches=[Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])
        self.set_runtime("p.1", "ready")  # p.1: hash only in the intent
        report = freeze.amend(self.root, "p", reason="再改", batches=[Doc(header("p.2", ["main:b.py", "main:d.py"]), "")])
        self.assertIn("已按意图记录的哈希补完", report["warnings"][0])
        self.assertEqual(self.events("plan_amend_failed"), [])

    def test_user_approved_amend_lists_the_widening(self):
        self.cfg_home.mkdir()
        (self.cfg_home / "config.toml").write_text('[delivery]\nlevel = {ceiling = "merge_dev", value = "done"}\n')
        wide = Doc(header("p.1", ["main:a.py"], config={"delivery": {"level": "merge_dev"}}), "")
        report = freeze.amend(self.root, "p", reason="用户同意", batches=[wide], user_approved=True)
        self.assertIn("批准将放宽 p.1 的 delivery.level", report["warnings"])
        plan = model.load(self.root, "p")
        self.assertTrue(model.task_config_approved(self.root, plan, "p.1"))
        # a controller amend approves nothing: no listing
        self.assertEqual(freeze.amend(self.root, "p", reason="r", batches=[
            Doc(header("p.2", ["main:b.py", "main:c.py"]), "")])["warnings"], [])


class ExpandScopeTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        plan.batches["p.1"].header["state"] = "running"
        plan.batches["p.2"].header["state"] = "ready"
        model.write(self.root, plan)

    def test_seat_being_opened_counts_as_started(self):
        lock.acquire(self.root, "p.2", "fm-x-p_2-1")  # seat._claim took the lock; the state is still ready
        with self.assertRaisesRegex(PlanError, "started batch p.2"):
            freeze.expand_scope(self.root, "p.1", ["main:b.py"], decision="Q-1", approved_by="user")
        lock.release(self.root, "p.2", "fm-x-p_2-1")
        freeze.expand_scope(self.root, "p.1", ["main:b.py"], decision="Q-1", approved_by="user")
        self.assertEqual(model.load(self.root, "p").batches["p.2"].header["depends_on"], ["p.1"])

    def test_retry_of_the_same_decision_is_done(self):
        freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-1", approved_by="user")
        n = len(model.load(self.root, "p").doc.header["revisions"])
        self.assertTrue(freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-1", approved_by="user")["ok"])
        self.assertEqual(len(model.load(self.root, "p").doc.header["revisions"]), n)
        with self.assertRaisesRegex(PlanError, "already owns"):  # another decision asking for owned paths
            freeze.expand_scope(self.root, "p.1", ["main:x.py"], decision="Q-2", approved_by="user")
