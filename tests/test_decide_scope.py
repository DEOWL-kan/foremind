import contextlib
import io
import os
from unittest import mock

from foremind import cli
from foremind import header as hdr
from foremind.decide import pending
from foremind.events import EventLog
from foremind.fsutil import atomic_write
from foremind.plan import freeze, model
from foremind.plan.model import Doc
from test_plan_helpers import ProjectCase, header, make_plan


class ScopeTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"]),
                                  header("p.3", ["main:c.py"])]))
        freeze.approve(self.root, "p")
        plan = model.load(self.root, "p")
        for b in ("p.1", "p.2"):
            plan.batches[b].header["state"] = "running"  # runtime field: the plan stays bound
        model.write(self.root, plan)

    def ask(self, paths):
        req = {"kind": "scope", "category": 8, "batch": "p.1", "session": "s", "match": {"paths": paths},
               "approve_option": 1}
        rec, _ = pending.create(self.root, {}, question="p.1 要写更多文件", options=["批准", "不批准"], recommended=1,
                                reason="越界", category=8, blocks=["p.1"], reversible=True, request=req)
        return rec["id"]

    def events(self, type):
        return [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == type]

    def test_approved_scope_widens_and_serialises_unstarted_batch(self):
        qid = self.ask(["main:c.py"])
        self.assertIn("main:c.py", pending.answer(self.root, qid, 1))
        self.assertEqual(pending.load(self.root, qid)["state"], "applied")
        plan = model.load(self.root, "p")
        self.assertEqual(plan.batches["p.1"].header["owns_paths"], ["main:a.py", "main:c.py"])
        self.assertEqual(plan.batches["p.3"].header["depends_on"], ["p.1"])
        rev = plan.doc.header["revisions"][-1]
        self.assertEqual((rev["decision"], rev["approved_by"]), (qid, "user"))
        self.assertEqual(self.events("plan_amended")[-1]["decision"], qid)
        self.assertTrue(model.is_bound(self.root, plan))

    def test_overlap_with_started_batch_leaves_it_answered(self):
        qid = self.ask(["main:b.py"])
        self.assertIn("started batch p.2", pending.answer(self.root, qid, 1))
        self.assertEqual(pending.load(self.root, qid)["state"], "answered")
        self.assertEqual(len(self.events("pending_apply_failed")), 1)
        self.assertEqual(model.load(self.root, "p").batches["p.1"].header["owns_paths"], ["main:a.py"])

    def test_budget_is_checked_against_the_project_config(self):
        with (self.root / "foremind.toml").open("a") as f:
            f.write("\n[context]\nabs_cap_tokens = 60000\n")  # half: 30000 < budget_estimate 50000
        qid = self.ask(["main:x.py"])
        self.assertIn("budget_estimate", pending.answer(self.root, qid, 1))
        self.assertEqual(pending.load(self.root, qid)["state"], "answered")

    def test_decide_shows_the_finishing_report_of_an_interrupted_amend(self):  # REQ-18
        def write(root, plan):  # an amend that stops after its first batch file
            (bid, d), = list(plan.batches.items())[:1]
            atomic_write(model.batch_path(root, bid), hdr.render(d.header, d.body))
            raise KeyboardInterrupt
        with mock.patch.object(model, "write", write), self.assertRaises(KeyboardInterrupt):
            freeze.amend(self.root, "p", reason="p.3 多写 d.py",
                         batches=[Doc(header("p.3", ["main:c.py", "main:d.py"]), "")])
        qid = self.ask(["main:x.py"])
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": ""}), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["decide", qid, "1"]), 0)
        self.assertEqual(out.getvalue(), f"{qid} 已答复：批次 p.1 的 owns_paths 已加上 main:x.py（计划修订带 {qid}）；"
                                         "上次修订 1 写到一半中断，已按意图记录的哈希补完\n")
        self.assertIn("main:d.py", model.load(self.root, "p").batches["p.3"].header["owns_paths"])

    def test_retried_apply_is_done(self):
        # the handler ran but the Q-n was not marked applied (interrupted): the retry must not fail on "already owns"
        from foremind.decide import scope
        qid = self.ask(["main:x.py"])
        pending.answer(self.root, qid, 1)
        n = len(self.events("plan_amended"))
        req = {"kind": "scope", "batch": "p.1", "match": {"paths": ["main:x.py"]}}
        self.assertIn("main:x.py", scope.apply_answer(self.root, pending.load(self.root, qid), req))
        self.assertEqual(len(self.events("plan_amended")), n)
