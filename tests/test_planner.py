import json
import os
from pathlib import Path
from unittest import mock

from foremind import config as cfg_mod
from foremind import header as hdr
from foremind import planner, seat
from foremind.carriers import Carrier
from foremind.commands import do
from foremind.events import EventLog
from foremind.plan import freeze, model
from foremind.plan.model import PlanError
from test_plan_cli import run
from test_plan_helpers import ProjectCase, header


class FakeCarrier(Carrier):
    """create() plays SessionStart: the hook records batch None for a session without FOREMIND_BATCH."""
    name = "fake"

    def __init__(self, root, heartbeat=True):
        super().__init__(root)
        self.heartbeat, self.created, self.sent, self.closed = heartbeat, {}, [], []

    def create(self, session, launch):
        self.created[session] = launch
        if self.heartbeat:
            p = seat.heartbeat_path(self.root, session)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"session": session, "batch": launch.env["FOREMIND_BATCH"] or None}))

    def send(self, session, text):
        self.sent.append((session, text))

    def close(self, session):
        self.closed.append(session)


class PlannerCase(ProjectCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_WT_ROOT": str(self.root / "share" / "wt")}))
        os.environ.pop("FOREMIND_ROLE", None)  # the user; restored with the patch above
        self.state = self.root / ".foremind"

    def events(self, type_):
        return [e for e in EventLog(self.state / "events.jsonl").iter() if e["type"] == type_]


class SubmitTest(PlannerCase):
    def draft(self, headers, goal="REQ-1: 做一件事\n\nWHEN 跑测试 THEN 通过\n"):
        d = self.root / "draft"
        (d / "batches").mkdir(parents=True, exist_ok=True)
        (d / "goal.md").write_text(goal)
        for h in headers:
            (d / "batches" / f"{h['id']}.md").write_text(hdr.render(h, "## 做什么\n\n说明\n"))
            (d / "batches" / f"{h['id']}.handoff.md").write_text(f"# {h['id']} 交接\n")
        return d

    def test_pass_writes_plan_goal_and_handoffs(self):
        d = self.draft([header("p-1.2", ["main:b.py"], ["p-1.1"]),
                        header("p-1.1", ["main:a.py"], state="running", config_approved="x" * 64)])
        (d / "intent.md").write_text("复述\n")
        code, out, err = run(["plan", "submit", "p-1", "--dir", str(d)])
        self.assertEqual(code, 0, err)
        self.assertIn("plan approve p-1", out)
        plan = model.load(self.root, "p-1")
        self.assertEqual(plan.doc.header["batches"], ["p-1.1", "p-1.2"])
        self.assertNotIn("approved_at", plan.doc.header)
        for k in model.PROGRAM_FIELDS:  # stripped from the planner's header
            self.assertNotIn(k, plan.batches["p-1.1"].header)
        self.assertIn("frozen_at", plan.goal.header)
        self.assertEqual(plan.doc.header["goal_hash"], model.text_hash(plan.goal.body))
        self.assertEqual((self.state / "batches" / "p-1.2.handoff.md").read_text(), "# p-1.2 交接\n")
        self.assertEqual((self.state / "plans" / "p-1" / "intent.md").read_text(), "复述\n")
        self.assertEqual(len(self.events("plan_submitted")), 1)
        # a second round drops p-1.2; approval then closes submit
        (d / "batches" / "p-1.2.md").unlink()
        self.assertEqual(run(["plan", "submit", "p-1", "--dir", str(d)])[0], 0)
        self.assertFalse((self.state / "batches" / "p-1.2.md").exists())
        self.assertFalse((self.state / "batches" / "p-1.2.handoff.md").exists())
        self.assertEqual(run(["plan", "approve", "p-1"])[0], 0)
        code, _, err = run(["plan", "submit", "p-1", "--dir", str(d)])
        self.assertEqual(code, 1)
        self.assertIn("plan amend", err)

    def test_resubmit_keeps_revisions(self):
        d = self.draft([header("p-1.1", ["main:a.py"])])
        planner.submit(self.root, "p-1", d)
        freeze.amend(self.root, "p-1", reason="用户批准改目标", goal="REQ-1: 做两件事\n", user_approved=True)  # #9
        (d / "goal.md").write_text("REQ-1: 做两件事\n")
        (d / "plan.md").write_text(hdr.render({"revisions": [{"n": 1, "reason": "草稿自带"}]}, ""))  # not read
        planner.submit(self.root, "p-1", d)
        revs = model.load(self.root, "p-1").doc.header["revisions"]
        self.assertEqual([(r["reason"], r["approved_by"]) for r in revs], [("用户批准改目标", "user")])

    def test_guard_still_denies_planner_repo_writes(self):
        from foremind import repos as rp
        from foremind.hooks import guard
        repos = rp.load_repos(self.root, cfg_mod.load(self.root))
        kw = dict(root=self.root.resolve(), session="fm-x-plan-p-1-1", batch=None, role="planner", header={},
                  cfg={}, repos=repos)
        v = guard.evaluate("Write", {"file_path": str(self.root / "a.py")}, str(self.root), **kw)
        self.assertIn("角色 planner不写", "".join(v.matrix))

    def test_rejected_writes_nothing(self):
        d = self.draft([header("p-1.1", ["main:a.py"]), header("p-1.2", ["main:a.py"])])  # overlap, no edge
        code, _, err = run(["plan", "submit", "p-1", "--dir", str(d)])
        self.assertEqual(code, 1)
        self.assertIn("p-1.2", err)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [])
        (d / "batches" / "p-1.2.handoff.md").unlink()
        self.assertRaisesRegex(PlanError, "handoff.md: missing", planner.submit, self.root, "p-1", d)

    def test_malformed_header_is_rejected_not_raised(self):
        bad = header("p-1.1", ["main:a.py"], coupling="高")
        del bad["owns_paths"]
        d = self.draft([bad])
        code, _, err = run(["plan", "submit", "p-1", "--dir", str(d)])
        self.assertEqual(code, 1)
        self.assertIn("owns_paths", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), [])

    def test_batch_of_another_plan_is_refused(self):
        d = self.draft([header("p-1.1", ["main:a.py"])])
        planner.submit(self.root, "p-1", d)
        d2 = self.root / "d2"
        (d2 / "batches").mkdir(parents=True)
        (d2 / "goal.md").write_text("REQ-1: 别的\n")
        (d2 / "batches" / "p-1.1.md").write_text(hdr.render(header("p-1.1", ["main:z.py"], plan_id="p-2"), ""))
        (d2 / "batches" / "p-1.1.handoff.md").write_text("x\n")
        self.assertRaisesRegex(PlanError, "another plan", planner.submit, self.root, "p-2", d2)
        self.assertEqual(model.load(self.root, "p-1").batches["p-1.1"].header["owns_paths"], ["main:a.py"])

    def test_only_user_planner_controller(self):
        d = self.draft([header("p-1.1", ["main:a.py"])])
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "seat"}):
            self.assertRaisesRegex(PlanError, "not role seat", planner.submit, self.root, "p-1", d)
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "planner"}):
            planner.submit(self.root, "p-1", d)
        self.assertEqual(self.events("plan_submitted")[0]["role"], "planner")

    def test_rejected_goal_does_not_freeze(self):
        d = self.draft([header("p-1.1", ["main:a.py"])], goal="没有编号\n")
        self.assertRaises(freeze.Rejected, planner.submit, self.root, "p-1", d)
        self.assertFalse((self.state / "plans").exists())


class NewTest(PlannerCase):
    def test_opens_session_and_delivers_kickoff(self):
        carrier = FakeCarrier(self.root)
        out = planner.open_planner(self.root, "做一个登录页", carrier=carrier, tier="S", config={})
        self.assertEqual(out["plan"], "p-1")
        self.assertEqual((out["model"], out["effort"]), ("claude-opus-5-5", "high"))
        draft = Path(out["draft"])
        self.assertEqual(draft.parent.parent, self.root / "share" / "drafts")
        self.assertTrue((draft / "batches").is_dir())
        launch = carrier.created[out["session"]]
        self.assertIn("-plan-p-1-1", out["session"])
        self.assertEqual((launch.env["FOREMIND_ROLE"], launch.env["FOREMIND_BATCH"]), ("planner", ""))
        # the draft is the cwd, the main checkout only added: a git commit from the cwd has no repo to land in
        self.assertEqual(launch.cwd, draft)
        self.assertEqual(launch.argv[launch.argv.index("--add-dir") + 1], str(self.root.resolve()))
        self.assertEqual(launch.env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")  # the project's CLAUDE.md
        (session, text), = carrier.sent
        self.assertIn("做一个登录页", text)
        self.assertIn(f"foremind plan submit p-1 --dir {draft}", text)
        self.assertIn(f"项目根（主检出，只读；工作目录是草稿目录，项目的规则与代码到这里读）：{self.root.resolve()}", text)
        self.assertIn("## 会话内怎么做", text)  # the role card, in full
        self.assertEqual(self.events("planner_opened")[0]["session"], session)
        # the next plan gets the next id and xhigh on M
        out = planner.open_planner(self.root, "另一件", carrier=carrier,
                                   config={"routes.planner.model": "claude-opus-5-5"})
        self.assertEqual((out["plan"], out["effort"]), ("p-2", "xhigh"))

    def test_no_heartbeat_closes(self):
        carrier = FakeCarrier(self.root, heartbeat=False)
        cfg = {"seat.sessionstart_timeout_s": 0}
        self.assertRaisesRegex(seat.SeatError, "no heartbeat", planner.open_planner, self.root, "x",
                               carrier=carrier, config=cfg)
        self.assertEqual(carrier.closed, list(carrier.created))
        self.assertEqual(carrier.sent, [])

    def test_excluded_model(self):
        carrier = FakeCarrier(self.root)
        cfg = {"routes.planner.model": "claude-sonnet-5", "exclude.models": ["*sonnet*"]}
        self.assertRaisesRegex(seat.SeatError, "excluded", planner.open_planner, self.root, "x", carrier=carrier,
                               config=cfg)
        self.assertEqual(carrier.created, {})


class DoRouteTest(PlannerCase):
    def tiers(self, plan_id):
        return model.load(self.root, plan_id).batches[f"{plan_id}.1"].header["tiers"]

    def test_routes_and_exclusions(self):
        base = cfg_mod.load(self.root)
        do.create(self.root, "a", owns=["main:a.py"], accept=["true"], config=base)
        self.assertEqual((self.tiers("do-1")["model"], self.tiers("do-1")["effort"]), ("claude-opus-5-5", "medium"))
        routed = {**base, "routes.seat.model": "claude-opus-9", "routes.seat.effort": "high"}
        do.create(self.root, "b", owns=["main:b.py"], accept=["true"], config=routed)
        self.assertEqual((self.tiers("do-2")["model"], self.tiers("do-2")["effort"]), ("claude-opus-9", "high"))
        do.create(self.root, "c", owns=["main:c.py"], accept=["true"], model_name="claude-opus-5-5", config=routed)
        self.assertEqual(self.tiers("do-3")["model"], "claude-opus-5-5")  # --model wins
        ex = {**base, "routes.seat.model": "claude-sonnet-5", "exclude.models": ["*sonnet*"]}
        self.assertRaisesRegex(seat.SeatError, "excluded", do.create, self.root, "d", owns=["main:d.py"],
                               accept=["true"], config=ex)
        (self.root / "foremind.toml").write_text('exclude = { providers = ["anthropic"] }\n[[repos]]\nid = "main"\n'
                                                 'path = "."\n')
        code, _, err = run(["do", "e", "--owns", "main:e.py", "--accept", "true", "--model", "claude-opus-5-5"])
        self.assertEqual(code, 1)
        self.assertIn("exclude.providers", err)
        self.assertFalse((self.state / "plans" / "do-4").exists())
