from foremind import schemas
from foremind.events import EventLog
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
