"""`foremind land` on temp projects: merge in a throwaway worktree, verify, then move the target (REQ-8, REQ-9)."""
import contextlib
import io
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from foremind import batchlog, config, header, review, worktree
from foremind.cli import main
from foremind.commands import land
from foremind.fsutil import LockBusy, file_lock, project_lock
from foremind.paths import state_dir
from foremind.plan import model
from foremind.schemas import EXAMPLES
from test_gate_fixture import Project, sh

NOTES = "## 交付说明\n\n```json\n" + json.dumps(EXAMPLES["delivery_notes"], ensure_ascii=False) + "\n```\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.p = p = Project(self)
        for k in ("FOREMIND_JOB", "FOREMIND_CONTROLLER"):  # a gate job's, a controller's: by and job are asserted
            os.environ.pop(k, None)  # (the fixture's patch.dict puts them back)
        p.batch(state="delivered")
        p.commit()
        p.cfg["land.commands"] = ["true"]
        self.repo = p.root / "api"
        self.start = self.tip()

    def tip(self, ref="main"):
        return sh(self.repo, "git", "rev-parse", ref)

    def gate(self, verdict="pass", heads=None):
        review.events(self.p.root).append("gate_result", batch="shop.1", heads=heads or {"api": self.p.head()},
                                          path="x", sha256="0" * 64, verdict=verdict, failing=[], pending=[])

    def land(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            new = land.land(self.p.root, "shop.1", self.p.cfg)
        self.assertEqual(list(new), ["api"])
        return new["api"], out.getvalue()

    def refused(self, pattern):
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(review.FlowError, pattern):
            land.land(self.p.root, "shop.1", self.p.cfg)
        self.assertEqual(self.tip(), self.start)
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertEqual(self.leftovers(), [])

    def leftovers(self):
        wts = sh(self.repo, "git", "worktree", "list", "--porcelain")
        return [p.name for p in worktree.wt_root().glob("_land-*")] + [x for x in wts.splitlines() if "_land-" in x]

    def results(self):
        return [e for e in review.all_events(self.p.root) if e["type"] == "land_result"]

    def outcome(self):
        return self.results()[-1]["outcome"], self.results()[-1]["kind"]

    def attempt(self, cfg=True):
        """land, quiet: the FlowError it raised, or None."""
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                land.land(self.p.root, "shop.1", self.p.cfg if cfg else None)
            except review.FlowError as e:
                return e

    def log(self, notes, check=True):
        text = "## 交付说明\n\n```json\n" + json.dumps(notes, ensure_ascii=False) + "\n```\n"
        if not check:  # past `foremind log`'s schema check
            return batchlog.append(self.p.root, "shop.1", text, author="user")
        (f := self.p.tmp / "n.md").write_text(text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["log", "shop.1", "--file", str(f)]), 0)

    def during_commands(self, act):
        """Runs act once, while land waits for its first command."""
        real, done = land.wait, []

        def wait(*a, **kw):
            if not done:
                done.append(act() or True)
            return real(*a, **kw)
        return mock.patch.object(land, "wait", wait)

    def approve(self):
        review.events(self.p.root).append("plan_approved", plan="shop",
                                          plan_hash=model.plan_hash(model.load(self.p.root, "shop")))

    def authorize(self, plan_header=None):
        """REQ-2 items 1-8 met, as the supervisor: the user's land.auto, repos on disk, the plan approved, the gate."""
        (self.p.tmp / "config").mkdir(exist_ok=True)
        (self.p.tmp / "config" / "config.toml").write_text("[land]\nauto = true\n")
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        path = model.plan_dir(self.p.root, "shop") / "plan.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header.render(plan_header or {
            "plan_id": "shop", "goal_hash": "0" * 64, "approved_at": "2026-10-01T00:00:00+00:00",
            "approved_by": "user", "batches": ["shop.1"], "revisions": []}, "plan\n"))
        self.approve()
        self.gate()
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_ROLE": "supervisor"}))

    def q(self, name="Q-1.json", raw=None, **fields):
        d = state_dir(self.p.root) / "decisions"
        d.mkdir(exist_ok=True)
        (d / name).write_text(raw if raw is not None else json.dumps(
            {**EXAMPLES["pending"], "id": name.removesuffix(".json"), "blocks": ["shop.1"], **fields}))


class LandTest(Base):
    def test_merges_in_main_checkout_on_target(self):
        self.gate()
        new, out = self.land()
        self.assertEqual(self.tip(), new)
        self.assertEqual(sh(self.repo, "git", "rev-parse", f"{new}^1", f"{new}^2").split(), [self.start, self.p.head()])
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")
        self.assertTrue((self.repo / "src" / "app.py").exists())  # the checkout moved with the branch
        self.assertEqual(self.leftovers(), [])
        runs = sorted((state_dir(self.p.root) / "land").glob("shop.1-*/*/stdout.log"))
        self.assertEqual(len(runs), 2)  # accept_commands ["true"] + land.commands ["true"]
        self.assertIn("ok   true (exit 0)", out)
        self.assertEqual(self.p.state(), "delivered")  # merged is L0's to record

    def test_update_ref_when_checkout_elsewhere_and_target_ahead_before_land(self):
        sh(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "ahead")
        sh(self.repo, "git", "checkout", "-q", "-b", "side")
        self.start = self.tip()
        self.gate()
        new, _ = self.land()
        self.assertEqual(self.tip(), new)
        self.assertEqual(sh(self.repo, "git", "rev-parse", f"{new}^1"), self.start)
        self.assertEqual(sh(self.repo, "git", "symbolic-ref", "--short", "HEAD"), "side")

    def test_conflict_aborts(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("x = 2\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "clash")
        self.start = self.tip()
        self.gate()
        self.refused(r"冲突：\['src/app.py'\]")

    def test_failing_command_keeps_output(self):
        self.p.cfg["land.commands"] = ["echo boom; exit 3"]
        self.gate()
        self.refused("1 条命令没通过")
        log = next((state_dir(self.p.root) / "land").glob("shop.1-*/2/stdout.log"))
        self.assertEqual(log.read_text(), "boom\n")

    def test_target_moved_during_verification(self):
        sh(self.repo, "git", "checkout", "-q", "-b", "side")
        sh(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "elsewhere")
        moved = self.tip("side")
        self.p.cfg["land.commands"] = [f"git -C '{self.repo}' branch -f main side"]  # after merge, before the ff
        self.gate()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(review.FlowError, "验证期间移动了"):
            land.land(self.p.root, "shop.1", self.p.cfg)
        self.assertEqual((self.tip(), self.tip("HEAD"), self.leftovers()), (moved, moved, []))
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain"), "")

    def test_target_checked_out_in_another_worktree(self):
        sh(self.repo, "git", "checkout", "-q", "-b", "side")
        sh(self.repo, "git", "worktree", "add", "-q", str(self.p.tmp / "other"), "main")
        self.gate()
        self.refused("检出在另一个 worktree 里")
        self.assertEqual(sh(self.p.tmp / "other", "git", "status", "--porcelain"), "")

    def test_target_also_checked_out_elsewhere_while_main_checkout_on_it(self):  # REQ-20: only its own entry exempt
        sh(self.repo, "git", "worktree", "add", "-q", "--force", str(self.p.tmp / "other"), "main")
        self.gate()
        self.refused("检出在另一个 worktree 里，没动它；那个 worktree 离开 main 后重新 land")
        self.assertEqual(sh(self.p.tmp / "other", "git", "status", "--porcelain"), "")

    def test_repo_path_in_other_case_is_still_the_main_checkout(self):  # samefile, not resolve (§13.5)
        if not (self.p.root / "API").exists():
            self.skipTest("case-sensitive volume")
        self.p.cfg["repos"] = [{**r, "path": r["path"].upper()} if r["id"] == "api" else r for r in self.p.cfg["repos"]]
        self.gate()
        new, _ = self.land()
        self.assertEqual(self.tip(), new)

    def test_dirty_main_checkout(self):
        self.gate()
        (self.repo / "README.md").write_text("edited\n")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(review.FlowError, "未提交改动"):
            land.land(self.p.root, "shop.1", self.p.cfg)
        self.assertEqual((self.tip(), self.leftovers()), (self.start, []))

    def test_refusals(self):
        self.refused("没有通过的门禁结果")
        self.gate(verdict="fail")
        self.refused("没有通过的门禁结果")
        self.gate(heads={"api": self.start})
        self.refused("没有通过的门禁结果")
        self.gate()
        self.p.write_header("shop.1", state="in_review")
        self.refused("land 只合 delivered")
        self.p.write_header("shop.1", state="delivered")
        for bad in ("true", ["true", ""], [1], []):  # a string would run letter by letter; [] would run none
            self.p.cfg["land.commands"] = bad
            self.refused("配置 land.commands 须是")
            self.assertEqual(self.outcome(), ("refused", "config"))

    def test_session_refused_notes_included(self):
        self.gate()
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1"}):
            for argv in (["land", "shop.1"], ["land", "shop.1", "--notes"]):
                with contextlib.redirect_stdout(io.StringIO()) as out, \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(main(argv), 1)
                self.assertIn("Foremind 会话不能执行", err.getvalue())
                self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.tip(), self.start)


class ChecksTest(Base):
    """m2f REQ-1: the checks before anything runs, in order; the final one under the project lock; land_result."""

    def test_checks_in_order_before_anything_runs(self):
        self.log({"config_keys": [{"key": "land.nope", "merge_class": "plain", "why": "x"}], "design": [],
                  "leftovers": []})
        path = state_dir(self.p.root) / "batches" / "shop.1.log.md"
        good = path.read_text()
        path.write_text(good.replace("land.nope", "land.nop!", 1))  # rewritten outside `foremind log`
        self.p.write_header("shop.1", state="in_review")
        self.p.cfg["land.commands"] = "true"
        (self.repo / "README.md").write_text("edited\n")
        sh(self.repo, "git", "worktree", "add", "-q", "--force", str(self.p.tmp / "other"), "main")
        busy = contextlib.ExitStack()
        busy.enter_context(file_lock(state_dir(self.p.root) / "land.lock"))
        steps = [
            (("refused", "busy"), busy.close),
            (("refused", "state"), lambda: self.p.write_header("shop.1", state="delivered")),
            (("refused", "gate"), self.gate),
            (("refused", "config"), lambda: self.p.cfg.update({"land.commands": ["true"]})),
            (("notes_unverified", None), lambda: path.write_text(good)),
            (("unregistered_keys", None),
             lambda: self.enterContext(mock.patch.dict(config.KEY_CLASSES, {"land.nope": config.PLAIN}))),
            (("refused", "dirty"), lambda: sh(self.repo, "git", "checkout", "-q", "README.md")),
            (("refused", "elsewhere"), lambda: sh(self.repo, "git", "worktree", "remove", str(self.p.tmp / "other")))]
        for want, fix in steps:
            self.assertIsInstance(self.attempt(), land.Stop)
            self.assertEqual(self.outcome(), want)
            self.assertEqual((self.tip(), self.leftovers()), (self.start, []))
            self.assertFalse((state_dir(self.p.root) / "land").exists())  # no command ran
            fix()
        self.assertIn("land.nope", self.results()[5]["reason"])
        self.assertIsNone(self.attempt())
        self.assertEqual(self.outcome(), ("merged", None))
        self.assertEqual(len(self.results()), 9)  # one per land
        self.assertEqual([r["heads"] for r in self.results()], [{"api": self.p.head()}] * 9)  # busy and state too

    def test_bad_accept_commands_is_config(self):  # the header schema refuses them first; land checks again
        self.gate()
        with mock.patch.object(land, "command_repo", return_value="nope"):
            self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "config"))

    def test_only_sections_passing_the_schema_count(self):
        self.log({"config_keys": [{"key": "land.nope", "merge_class": "oops", "why": "x"}], "design": [],
                  "leftovers": []}, check=False)
        self.gate()
        self.assertIsNone(self.attempt())

    def test_sent_back_while_commands_run(self):
        self.gate()
        with self.during_commands(lambda: review.request_changes(self.p.root, "shop.1", ["改"], by="controller")):
            self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "state"))
        self.assertIsNotNone(self.results()[-1]["out_dir"])  # the commands ran
        self.assertEqual((self.tip(), self.leftovers()), (self.start, []))

    def test_supervisor_lands_when_authorized(self):
        self.authorize()
        self.assertEqual(land.auto_blockers(self.p.root, "shop.1"), [])
        self.assertIsNone(self.attempt())
        self.assertEqual((self.outcome(), self.results()[-1]["by"]), (("merged", None), "supervisor"))

    def test_supervisor_refused_auto_when_a_blocking_q_appears_while_commands_run(self):
        self.authorize()
        with self.during_commands(self.q):
            self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "auto"))
        self.assertIn("第 7 条：挡着本批的待决：Q-1（open）", self.results()[-1]["reason"])
        self.assertIsNotNone(self.results()[-1]["out_dir"])
        self.assertEqual(self.tip(), self.start)

    def test_supervisor_refused_auto_when_a_bad_q_appears_while_commands_run(self):
        self.authorize()
        with self.during_commands(lambda: self.q("Q-2.json", raw="{bad")):
            self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "auto"))
        self.assertIn("第 7 条：待决文件读不出：Q-2.json", self.results()[-1]["reason"])
        self.assertEqual(self.tip(), self.start)

    def test_supervisor_checks_auto_first(self):
        self.authorize()
        self.p.write_header("shop.1", state="in_review")  # state's own check comes after
        self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "auto"))
        self.assertIn("第 2 条", self.results()[-1]["reason"])

    def test_result_when_merged(self):
        self.gate()
        new, out = self.land()
        r = self.results()[-1]
        self.assertEqual({k: r[k] for k in land.RESULT if k not in ("out_dir", "drafts")},
                         {"heads": {"api": self.p.head()}, "outcome": "merged", "kind": None, "reason": None,
                          "failed": None, "conflicts": None, "merges": {"api": new}})
        self.assertEqual((r["by"], r["job"]), ("user", None))
        self.assertEqual(Path(r["drafts"]).parent, Path(r["out_dir"]))
        self.assertEqual(out.rstrip().splitlines()[-1], f"合入草稿（总控确认后落盘）：{r['drafts']}")
        self.assertIn("## DESIGN §20 I 行草稿\n无\n\n## 遗留追加草稿\n无\n\n## 新配置键\n无\n",
                      Path(r["drafts"]).read_text())

    def test_result_per_end(self):
        self.p.cfg["land.commands"] = ["echo boom; exit 3"]
        self.gate()
        self.attempt()
        self.assertEqual((self.outcome(), self.results()[-1]["failed"]), (("tests", None), ["echo boom; exit 3"]))
        self.p.cfg["land.commands"] = ["true"]
        shutil.rmtree(state_dir(self.p.root) / "land")  # job ids repeat within one second
        with mock.patch.object(land, "_move", side_effect=review.FlowError("boom")):
            self.assertIsNotNone(self.attempt())
        self.assertEqual((self.outcome(), self.results()[-1]["reason"]), (("move_failed", None), "boom"))
        self.assertEqual(self.tip(), self.start)
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "controller", "FOREMIND_JOB": "j1"}), \
                mock.patch.object(land, "_merge", side_effect=review.FlowError("boom")):
            self.attempt()
        r = self.results()[-1]
        self.assertEqual((r["outcome"], r["by"], r["job"]), ("error", "controller", "j1"))
        self.assertEqual(len(self.results()), 3)

    def test_result_on_conflict(self):
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("x = 2\n")
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", "clash")
        self.gate()
        self.attempt()
        r = self.results()[-1]
        self.assertEqual((r["outcome"], r["conflicts"], r["out_dir"]), ("conflict", ["src/app.py"], None))

    def test_cli_records_a_config_error_but_not_a_session_or_notes(self):
        self.gate()
        (self.p.root / "foremind.toml").write_text("repos = [\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["land", "shop.1"]), 1)
        self.assertEqual(self.outcome(), ("error", None))
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["land", "shop.1", "--notes"]), 0)
        with mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-shop-shop.1-1"}), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["land", "shop.1"]), 1)
        self.assertEqual(len(self.results()), 1)

    def test_drafts(self):
        design = [{"where": "§13.5" if i % 2 else "§1.3", "text": f"t{i}"} for i in range(21)]
        self.log({"config_keys": [{"key": "land.auto", "merge_class": "authz", "default": False, "why": "授权"}],
                  "design": design, "leftovers": [{"item": "遗留一", "why": "以后做"}]})
        (self.p.root / "DESIGN.md").write_text("| I7 | a |\n| I12 | b |\nx | I99 | not at a line start\n")
        (state_dir(self.p.root) / "batches" / "shop.1.handoff.md").write_text("# shop.1 合入核对\n\n正文\n")
        self.gate()
        new, _ = self.land()
        lines = Path(self.results()[-1]["drafts"]).read_text(encoding="utf-8").splitlines()
        self.assertRegex(lines[0], r"^# shop\.1 合入草稿（\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00）$")
        self.assertEqual(lines[1], f"新提交：api {new}")
        row = lines[lines.index("## DESIGN §20 I 行草稿") + 1]
        self.assertTrue(row.startswith("| I13 | 合入核对（shop.1，REQ-1；补 §1.3、§13.5） | ① §1.3：t0 ② §13.5：t1 ③"), row)
        self.assertTrue(row.endswith(" ⑳ §13.5：t19 (21) §1.3：t20 |"), row)
        self.assertEqual(lines[lines.index("## 遗留追加草稿") + 1:lines.index("## 新配置键")],
                         ["- shop.1 交付说明：遗留一（以后做）", ""])
        self.assertEqual(lines[lines.index("## 新配置键") + 1:], ["- land.auto [authz]：授权"])
        (self.p.root / "DESIGN.md").unlink()
        self.assertEqual(land._drafts(self.p.root, {"id": "x.1", "reqs": ["REQ-2"]},
                                      [({"config_keys": [], "design": design[:1], "leftovers": []}, [])],
                                      {}).splitlines()[4], "| I? | （x.1，REQ-2；补 §1.3） | ① §1.3：t0 |")

    def test_moves_holding_the_project_lock(self):
        self.gate()
        real = land._move

        def move(x):
            with self.assertRaises(LockBusy), project_lock(self.p.root, blocking=False):
                pass
            real(x)
        with mock.patch.object(land, "_move", move):
            self.assertIsNone(self.attempt())

    def test_records_still_holding_land_lock(self):
        self.gate()

        def actor():  # called for the record's `by`
            with self.assertRaises(LockBusy), file_lock(state_dir(self.p.root) / "land.lock", blocking=False):
                pass
            return "user"
        with mock.patch.object(land, "actor", actor):
            self.assertIsNone(self.attempt())
        self.assertEqual(self.outcome(), ("merged", None))

    def test_same_falls_back_to_resolved_paths(self):  # m2e.7 r1 note: samefile cannot read a missing path
        gone = self.p.tmp / "gone"
        self.assertTrue(land._same(gone / "a" / "..", gone))
        self.assertFalse(land._same(gone / "a", gone))


class AutoTest(Base):
    """m2f REQ-2 items 1-8 (auto_blockers): each not met alone, one line with its number."""

    def setUp(self):
        super().setUp()
        self.authorize()

    def items(self):
        return [x.split("：")[0] for x in land.auto_blockers(self.p.root, "shop.1")]

    def user(self, text):
        (self.p.tmp / "config" / "config.toml").write_text(text)

    def test_authorization_is_the_user_layers_boolean_true(self):
        self.assertEqual(self.items(), [])
        self.user("[land]\nauto = 1\n")
        (self.p.root / "foremind.toml").write_text('land.auto = true\n[[repos]]\nid = "api"\npath = "api"\n')
        self.assertEqual(self.items(), ["第 1 条"])  # merged, the two come out True (1 == True)
        self.user("")  # the project layer alone: config.load refuses it as well
        self.assertEqual(self.items()[0], "第 1 条")
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        for text in ("[land]\nauto = false\n", '[land]\nauto = "true"\n',
                     "[land.auto]\nvalue = true\n", "[land.auto]\nceiling = true\n"):  # tables: only plain true
            self.user(text)
            self.assertEqual(self.items(), ["第 1 条"], text)
        self.user("[land\n")  # unreadable
        self.assertEqual(self.items()[0], "第 1 条")
        (self.p.tmp / "config" / "config.toml").unlink()
        self.assertEqual(self.items(), ["第 1 条"])

    def test_items_2_to_6(self):
        self.p.write_header("shop.1", state="in_review")
        self.assertEqual(self.items(), ["第 2 条"])
        self.p.write_header("shop.1", state="delivered", mode="watch")
        self.approve()
        self.assertEqual(self.items(), ["第 3 条"])
        self.p.write_header("shop.1", state="delivered")
        self.approve()
        toml = (self.p.root / "foremind.toml").read_text()
        (self.p.root / "foremind.toml").write_text(toml + '[delivery.repo.api]\nlevel = "merge_dev"\n')
        self.assertEqual(self.items(), ["第 4 条"])
        (self.p.root / "foremind.toml").write_text(toml)
        sh(self.repo, "git", "remote", "add", "origin", str(self.p.tmp / "api.git"))
        self.assertEqual(self.items(), ["第 5 条"])
        sh(self.repo, "git", "remote", "remove", "origin")
        self.gate(verdict="fail")
        self.assertEqual(self.items(), ["第 6 条"])
        self.gate(heads={"api": self.start})
        self.assertEqual(self.items(), ["第 6 条"])

    def test_item_7(self):
        self.q("Q-1.json", blocks=["shop.2"])  # another batch's
        self.q("Q-2.json", state="void")
        self.q("Q-3.json", state="answered", answer=1)
        self.assertEqual(self.items(), [])
        for state in land.AUTO_BLOCKING:
            self.q("Q-4.json", state=state, **({"answer": 1} if state != "open" else {}))
            self.assertEqual(land.auto_blockers(self.p.root, "shop.1"), [f"第 7 条：挡着本批的待决：Q-4（{state}）"])
        (state_dir(self.p.root) / "decisions" / "Q-4.json").unlink()
        for raw in ("{bad", "[]", json.dumps({**EXAMPLES["pending"], "id": "Q-5", "code": "?"})):
            self.q("Q-5.json", raw=raw)
            self.assertEqual(land.auto_blockers(self.p.root, "shop.1"), ["第 7 条：待决文件读不出：Q-5.json"], raw)

    def test_item_8(self):
        plan = model.plan_dir(self.p.root, "shop") / "plan.md"
        plan.write_text(plan.read_text().replace("plan\n", "plan, edited\n"))
        self.assertEqual(self.items(), ["第 8 条"])
        self.approve()
        self.assertEqual(self.items(), [])
        self.authorize({"plan_id": "shop", "goal_hash": "0" * 64, "batches": ["shop.1"], "revisions": []})
        self.assertEqual(land.auto_blockers(self.p.root, "shop.1"), ["第 8 条：计划 shop 没批准"])


class MultiTest(unittest.TestCase):
    """Main checkouts: api and web on the target (ff / reset --keep), app on a side branch (update-ref).
    Moves run app (update-ref first), api, web."""

    def setUp(self):
        self.p = p = Project(self, ("api", "app", "web"))
        p.batch(state="delivered", accept_commands=['basename "$PWD"', {"run": 'basename "$PWD"', "repo": "app"}])
        for r in p.repo_ids:
            p.commit(r)
        p.cfg["land.commands"] = ['basename "$PWD"']
        sh(p.root / "app", "git", "checkout", "-q", "-b", "side")
        self.start = self.tips()
        review.events(p.root).append("gate_result", batch="shop.1", heads={r: p.head(r) for r in p.repo_ids},
                                     path="x", sha256="0" * 64, verdict="pass", failing=[], pending=[])

    def tips(self):
        return {r: sh(self.p.root / r, "git", "rev-parse", "main") for r in self.p.repo_ids}

    def land(self, move=None):
        shutil.rmtree(state_dir(self.p.root) / "land", ignore_errors=True)  # job ids repeat within one second
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(land, "_move", move or land._move):
            return land.land(self.p.root, "shop.1", self.p.cfg), out.getvalue()

    def refused(self, pattern, move=None):
        with self.assertRaisesRegex(review.FlowError, pattern) as cm:
            self.land(move)
        self.assertEqual(self.tips(), self.start)
        for r in self.p.repo_ids:
            self.assertEqual(sh(self.p.root / r, "git", "status", "--porcelain"), "")
            self.assertNotIn("_land-", sh(self.p.root / r, "git", "worktree", "list"))
        self.assertFalse((self.p.root / "api" / "src").exists())  # api's checkout went back with its branch
        self.assertEqual(list(worktree.wt_root().glob("_land-*")), [])
        return str(cm.exception)

    def failing_at(self, repo, exc=review.FlowError("boom"), before=lambda: None):
        real = land._move

        def move(x):
            if x["repo"].id == repo:
                before()
                raise exc
            real(x)
        return move

    def test_merges_all_and_runs_where_acceptance_does(self):
        new, out = self.land()
        self.assertEqual(new, self.tips())
        for r in self.p.repo_ids:
            self.assertEqual(sh(self.p.root / r, "git", "rev-parse", f"{new[r]}^1", f"{new[r]}^2").split(),
                             [self.start[r], self.p.head(r)])
        self.assertTrue((self.p.root / "api" / "src" / "app.py").exists())
        self.assertEqual(sh(self.p.root / "app", "git", "symbolic-ref", "--short", "HEAD"), "side")
        logs = {d.name: (d / "stdout.log").read_text().strip()
                for d in next((state_dir(self.p.root) / "land").glob("shop.1-*")).iterdir() if d.is_dir()}
        self.assertTrue(logs.pop("1").startswith("_land-shop.1-"))  # no repo: the directory holding the checkouts
        self.assertEqual(logs, {"2": "app", "3": "api", "4": "app", "5": "web"})  # land.commands in every checkout
        self.assertIn("ok   [api] basename", out)
        self.assertIn("已合入 main（app）", out)
        self.assertEqual(list(worktree.wt_root().glob("_land-*")), [])

    def test_repo_already_in_target_is_left_alone(self):
        web = self.p.root / "web"
        sh(web, "git", "merge", "-q", "--no-edit", "fm/shop.1")  # merged by hand
        merged = sh(web, "git", "rev-parse", "main")
        new, out = self.land()
        self.assertEqual(new["web"], merged)
        self.assertEqual(self.tips(), {**new, "web": merged})
        self.assertIn(f"已在 main（web） 里，没动它 · 起点 {merged}", out)
        self.assertIn("已合入 main（api）", out)

    def test_repo_already_in_target_still_checked_for_its_start(self):
        web = self.p.root / "web"
        sh(web, "git", "merge", "-q", "--no-edit", "fm/shop.1")
        self.p.cfg["land.commands"] = [f"git -C '{web}' reset -q --keep {self.start['web']}"]  # undone
        with self.assertRaisesRegex(review.FlowError, "web: main 在验证期间移动了"):
            self.land()
        self.assertEqual(self.tips(), self.start)

    def test_repo_already_in_target_still_checked_like_the_others(self):  # REQ-20: every repo, merged or not
        web, app = self.p.root / "web", self.p.root / "app"
        sh(web, "git", "merge", "-q", "--no-edit", "fm/shop.1")
        sh(app, "git", "update-ref", "refs/heads/main", "fm/shop.1")
        before = self.tips()
        (web / "README.md").write_text("edited\n")
        with self.assertRaisesRegex(review.FlowError, "web: 主检出 .* 有未提交改动"):
            self.land()
        sh(web, "git", "checkout", "-q", "README.md")
        sh(app, "git", "worktree", "add", "-q", str(self.p.tmp / "other"), "main")
        with self.assertRaisesRegex(review.FlowError, "app: main 检出在另一个 worktree 里，没动它；那个 worktree 离开"):
            self.land()
        self.assertEqual(self.tips(), before)

    def test_one_repo_not_ready_moves_none(self):
        (self.p.root / "app" / "README.md").write_text("edited\n")
        with self.assertRaisesRegex(review.FlowError, "app: 主检出 .* 有未提交改动"):
            self.land()
        self.assertEqual(self.tips(), self.start)
        sh(self.p.root / "app", "git", "checkout", "-q", "README.md")
        sh(self.p.root / "app", "git", "worktree", "add", "-q", str(self.p.tmp / "other"), "main")
        self.refused("app: main 检出在另一个 worktree 里")

    def test_failed_move_puts_moved_repos_back(self):
        msg = self.refused("移动目标分支时失败", self.failing_at("web"))
        self.assertEqual(msg.splitlines()[1:], ["  api：已退回起点", "  app：已退回起点",  # reset --keep, update-ref
                                                "  web：移动时出错（boom）；仍在起点"])
        msg = self.refused("移动目标分支时失败", self.failing_at("api"))
        self.assertIn("  web：没移动，仍在起点", msg)

    def test_ctrl_c_between_moves_puts_moved_repos_back(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(KeyboardInterrupt):
            self.land(self.failing_at("web", KeyboardInterrupt()))
        self.assertEqual(self.tips(), self.start)
        self.assertIn("web：移动时出错（KeyboardInterrupt）", err.getvalue())
        r = [e for e in review.all_events(self.p.root) if e["type"] == "land_result"][-1]
        self.assertEqual(r["outcome"], "move_failed")
        self.assertIn("web：移动时出错（KeyboardInterrupt）", r["reason"])

    def test_undo_reports_every_repo_and_never_overwrites_a_later_move(self):
        app = self.p.root / "app"
        elsewhere = lambda: sh(app, "git", "update-ref", "refs/heads/main", "fm/shop.1")  # someone else moves app
        with self.assertRaises(review.FlowError) as cm:
            self.land(self.failing_at("web", before=elsewhere))
        lines = str(cm.exception).splitlines()
        self.assertEqual(lines[1], "  api：已退回起点")
        self.assertRegex(lines[2], rf"  app：退回失败，需手动处理：main 在 \w+，不是本次合并提交 \w+；"
                                   rf"起点 {self.start['app']}")
        self.assertEqual(lines[3], "  web：移动时出错（boom）；仍在起点")
        self.assertEqual(self.tips(), {**self.start, "app": self.p.head("app")})


class NotesTest(Base):
    def log(self, text):
        f = self.p.tmp / "notes.md"
        f.write_text(text)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["log", "shop.1", "--file", str(f)])
        return code, err.getvalue()

    def notes(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["land", "shop.1", "--notes"]), 0)
        return out.getvalue()

    def test_log_checks_notes(self):
        bad = NOTES.replace('"plain"', '"oops"')
        code, err = self.log(bad)
        self.assertEqual(code, 1)
        self.assertIn("merge_class", err)
        self.assertIn("no ```json block", self.log("## 交付说明\n\n没有\n")[1])
        self.assertFalse((state_dir(self.p.root) / "batches" / "shop.1.log.md").exists())
        self.assertEqual(self.log("plain text\n"), (0, ""))
        self.assertEqual(self.log(NOTES)[0], 0)

    def test_notes_summary(self):
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')
        self.assertIn("没有 `## 交付说明`", self.notes())
        self.log(NOTES)
        empty = json.dumps({"config_keys": [], "design": [], "leftovers": []})
        self.log(f"## 交付说明\n\n```json\n{empty}\n```\n")
        out = self.notes()
        self.assertIn("land.commands [plain] 默认 [\"python3 -m unittest discover -s tests\"]", out)  # every section
        self.assertIn("（第 1 段）", out)
        self.log("### not a record\n\n" + NOTES)  # a heading in the body is not a record's
        path = state_dir(self.p.root) / "batches" / "shop.1.log.md"
        other = json.dumps({"config_keys": [], "design": [], "leftovers": [{"item": "unregistered", "why": "x"}]})
        with path.open("a") as f:  # bytes past the registered size: verify passes, the summary ignores them
            f.write(f"## 交付说明\n\n```json\n{other}\n```\n")
        out = self.notes()
        self.assertEqual(out.count("land.commands [plain]"), 1)  # identical entries once, with their sections
        self.assertIn("默认 [\"python3 -m unittest discover -s tests\"]：", out)
        self.assertIn("（第 1、3 段）", out)
        self.assertRegex(out, r"(?m)次序：第 1 段 = \S+ user；第 2 段 = \S+ user；第 3 段 = \S+ user$")
        self.assertIn("§13.5：foremind land", out)
        self.assertIn("转遗留：无", out)
        self.assertNotIn("unregistered", out)
        self.assertEqual(self.tip(), self.start)  # --notes merges nothing
        path.write_text(path.read_text().replace("land.commands", "land.cmds", 1))
        self.assertIn("batchlog.verify 不通过", self.notes())

    def test_land_prints_notes(self):
        self.log(NOTES)
        self.gate()
        (self.p.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n\n'
                                                   '[delivery.repo.api]\ntarget_branch = "main"\n')
        user = self.p.tmp / "config" / "config.toml"  # land.commands is not registered yet: user layer only
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text('[land]\ncommands = ["true"]\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["land", "shop.1"]), 0)
        self.assertIn("已合入 main", out.getvalue())
        self.assertIn("交付说明（shop.1）", out.getvalue())
        self.assertNotEqual(self.tip(), self.start)


if __name__ == "__main__":
    unittest.main()
