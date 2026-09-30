import unittest

from foremind.pathmatch import overlap as paths_overlap
from foremind.plan import schedule
from foremind.plan.validate import apply_edges, validate
from test_plan_helpers import ProjectCase, header, make_plan


class PathsOverlapTest(unittest.TestCase):
    def test_cases(self):
        yes = [("main:src/x.py", "main:src/x.py"), ("main:src/*.py", "main:src/a/x.py"),
               ("main:src/**", "main:src/a/*.py"), ("main:src/", "main:src/a.py"), ("main:src/a*", "main:src/*b"),
               ("main:data/file[0-9]", "main:data/*1")]  # both match data/file1
        no = [("main:src/x.py", "app:src/x.py"), ("main:src/a/*.py", "main:src/b/*.py"),
              ("main:src/*.py", "main:src/*.md"), ("main:src/x.py", "main:src/y.py"), ("main:src/*.py", "main:doc/x.md")]
        for a, b in yes:
            self.assertTrue(paths_overlap(a, b) and paths_overlap(b, a), (a, b))
        for a, b in no:
            self.assertFalse(paths_overlap(a, b) or paths_overlap(b, a), (a, b))


class ValidateTest(ProjectCase):
    def check(self, plan, **kw):
        return validate(self.root, plan, **kw)

    def test_valid_plan_report(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"], ["p.1"]),
                               header("p.3", ["main:c.py"])])
        r = self.check(plan)
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["critical_path"], ["p.1", "p.2"])
        self.assertEqual((r["critical_path_length"], r["max_width"]), (2, 2))
        self.assertEqual(r["serial_reasons"], [{"batch": "p.2", "depends_on": "p.1", "reason": "声明的依赖"}])

    def test_cycle(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"], ["p.3"]), header("p.2", ["main:b.py"], ["p.1"]),
                               header("p.3", ["main:c.py"], ["p.2"])])
        r = self.check(plan)
        self.assertFalse(r["ok"])
        self.assertTrue(any(e.startswith("dependency cycle: ") for e in r["errors"]), r["errors"])

    def test_unreachable_overlap_gets_an_edge(self):
        plan = make_plan("p", [header("p.1", ["main:src/x.py"]), header("p.2", ["main:y.py"], ["p.1"]),
                               header("p.3", ["main:src/*.py"]), header("p.4", ["main:src/x.py"], ["p.2"])])
        r = self.check(plan)
        self.assertFalse(r["ok"])
        self.assertEqual(r["errors"], [])
        # p.1 -> p.2 -> p.4 is reachable, so only p.3 is serialised; after p.3 -> p.1 was added, p.3 / p.4 stays a
        # parallel pair and gets its own edge
        self.assertEqual(r["suggested_edges"], [{"batch": "p.3", "depends_on": "p.1"},
                                                {"batch": "p.4", "depends_on": "p.3"}])
        self.assertEqual(apply_edges(plan, r), 2)
        r = self.check(plan)
        self.assertTrue(r["ok"], r)
        self.assertIn({"batch": "p.3", "depends_on": "p.1", "reason": "owns_paths 重叠"}, r["serial_reasons"])

    def test_edge_direction(self):
        # the unstarted batch waits, whatever the topological order; two started ones are an error
        plan = make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:a.py"], state="running")],
                         approved=True)
        self.assertEqual(self.check(plan)["suggested_edges"], [{"batch": "p.1", "depends_on": "p.2"}])
        plan.batches["p.1"].header["state"] = "in_review"
        r = self.check(plan)
        self.assertEqual(r["suggested_edges"], [])
        self.assertTrue(any("have both started" in e for e in r["errors"]), r["errors"])
        # across plans: this plan's batch waits; if only it has started, the other plan has to be amended
        other = make_plan("q", [header("q.1", ["main:a.py"])], approved=True)
        plan = make_plan("p", [header("p.1", ["main:a.py"])], approved=True)
        self.assertEqual(self.check(plan, others=[other])["suggested_edges"], [{"batch": "p.1", "depends_on": "q.1"}])
        plan.batches["p.1"].header["state"] = "running"
        self.assertIn("q.1 (plan q) must wait for p.1", self.check(plan, others=[other])["errors"][0])

    def test_draft_plans_only_warn(self):
        other = make_plan("q", [header("q.1", ["main:src/x.py"])])
        r = self.check(make_plan("p", [header("p.1", ["main:src/*.py"])]), others=[other])
        self.assertEqual((r["ok"], r["suggested_edges"]), (True, []))
        self.assertEqual(r["warnings"], ["p.1: owns_paths overlap q.1 of draft plan q"])

    def test_depends_on_a_draft_batch_warns(self):
        other = make_plan("q", [header("q.1", ["main:x.py"])])
        r = self.check(make_plan("p", [header("p.1", ["main:a.py"], ["q.1"])]), others=[other])
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(r["warnings"], ["p.1: depends_on q.1 of draft plan q, which waits until that plan is approved"])

    def test_draft_batches_have_no_state(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"], state="merged"), header("p.2", ["main:b.py"])])
        self.assertIn("p.1: state 'merged' before the plan is approved", self.check(plan)["errors"])
        plan.batches["p.1"].header["state"] = "cancelled"
        self.assertTrue(self.check(plan)["ok"])

    def test_finished_batches_still_cover_and_count(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"], state="merged"),
                               header("p.2", ["main:b.py"], reqs=["REQ-2"])], goal="REQ-1 a\nREQ-2 b\n", approved=True)
        self.assertTrue(self.check(plan)["ok"], self.check(plan)["errors"])
        plan.batches["p.1"].header["state"] = "cancelled"
        self.assertIn("REQ-1: not covered by any batch", self.check(plan)["errors"])
        many = make_plan("p", [header(f"p.{i}", [f"main:{i}.py"], **({"state": "merged"} if i < 4 else {}))
                               for i in range(1, 14)], approved=True)
        self.assertIn("13 batches > 10: split into milestones (several plans)", self.check(many)["errors"])

    def test_cross_plan_overlap(self):
        other = make_plan("q", [header("q.1", ["main:src/x.py"])], approved=True)
        plan = make_plan("p", [header("p.1", ["main:src/*.py"])])
        r = self.check(plan, others=[other])
        self.assertEqual(r["suggested_edges"], [{"batch": "p.1", "depends_on": "q.1"}])
        apply_edges(plan, r)
        self.assertTrue(self.check(plan, others=[other])["ok"])
        other.batches["q.1"].header["state"] = "merged"  # finished work frees its paths
        self.assertTrue(self.check(make_plan("p", [header("p.1", ["main:src/*.py"])]), others=[other])["ok"])
        other.batches["q.1"].header["state"] = "cancelled"
        r = self.check(plan, others=[other])
        self.assertEqual(r["warnings"], ["p.1: depends_on cancelled batch q.1"])
        self.assertIn("p.1: depends_on unknown batch q.1", self.check(plan)["errors"])

    def test_contract_files(self):
        plan = make_plan("p", [header("p.1", ["main:api/schema.py"], contract="true"),
                               header("p.2", ["main:api/schema.py", "main:x.py"], ["p.1"])])
        errs = self.check(plan)["errors"]
        self.assertEqual(len(errs), 1)
        self.assertIn("contract batch p.1", errs[0])
        other = make_plan("q", [header("q.1", ["main:api/*.py"])], approved=True)
        plan = make_plan("p", [header("p.1", ["main:api/schema.py"], contract="true")])
        self.assertIn("q.1: owns", self.check(plan, others=[other])["errors"][0])

    def test_forbidden_words_only_in_prose(self):
        ok = make_plan("p", [header("p.1", ["main:api/v1/users.py"], accept_commands=["grep -r TODO api/v1/"])],
                       body="改 api/v1/users 与 `v1` 接口，版本 v1.2，见 foo_v1。\n```\n# TODO\n```\n")
        self.assertTrue(self.check(ok)["ok"], self.check(ok)["errors"])
        for body in ("先出简化版。\n", "先做 v1。\n", "todo: 以后补\n", "占位\n", "这一版 V1\n"):
            self.assertTrue(any("forbidden word" in e for e in self.check(make_plan("p", [header(
                "p.1", ["main:a.py"])], body=body))["errors"]), body)
        bad = header("p.1", ["main:a.py"], must_read=[{"path": "main:a.py", "why": "以后再做"}])
        self.assertIn("p.1: forbidden word '以后再做' in prose", self.check(make_plan("p", [bad]))["errors"])
        # revision history and the runtime-written status section are not the plan's prose
        plan = make_plan("p", [header("p.1", ["main:a.py"])])
        plan.doc.header["revisions"] = [{"n": 1, "at": "2026-09-25T00:00:00+00:00", "reason": "去掉 v1 兼容层",
                                         "goal_hash": plan.doc.header["goal_hash"], "approved_by": "controller"}]
        plan.batches["p.1"].body = "说明\n\n## 状态\n\nTODO: 补测试\n"
        self.assertTrue(self.check(plan)["ok"], self.check(plan)["errors"])
        plan.batches["p.1"].body = "TODO 说明\n\n## 状态\n"
        self.assertIn("p.1: forbidden word 'TODO' in prose", self.check(plan)["errors"])

    def test_allow_word_marker_exempts_one_body_line(self):
        body = "引用旧文：先做 v1 <!-- fm-allow-word -->\n正文\n"
        self.assertTrue(self.check(make_plan("p", [header("p.1", ["main:a.py"])], body=body))["ok"])
        self.assertIn("plan.md: forbidden word 'TODO' in prose", self.check(make_plan(
            "p", [header("p.1", ["main:a.py"])], body=body + "TODO\n"))["errors"])
        bad = header("p.1", ["main:a.py"], must_read=[{"path": "main:a.py", "why": "占位 <!-- fm-allow-word -->"}])
        self.assertIn("p.1: forbidden word '占位' in prose", self.check(make_plan("p", [bad]))["errors"])

    def test_unittest_module_form_needs_a_package(self):
        (self.root / "tests").mkdir()
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "__init__.py").write_text("")
        cmds = ["python3 -m unittest tests.test_x", "python3 -m unittest -v pkg.test_y tests/test_z.py",
                "python3 -m unittest discover -s tests -p 'test_x.py'", "cd x && python3 -m unittest -k a.b"]
        plan = make_plan("p", [header("p.1", ["main:a.py"], accept_commands=cmds)])
        r = self.check(plan, config={"repos": [{"id": "main", "path": "."}]})
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len(r["warnings"]), 1, r["warnings"])
        self.assertIn("tests/ has no __init__.py", r["warnings"][0])
        self.assertIn("discover -s tests", r["warnings"][0])

    def test_user_approval_warns_what_it_widens(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"], config={"delivery": {"level": "merge_dev"}}),
                               header("p.2", ["main:b.py"], config={"delivery": {"level": "done"}})])
        self.assertIn("批准将放宽 p.1 的 delivery.level", self.check(plan, user_approved=True)["warnings"])
        self.assertFalse(any("批准将放宽" in w for w in self.check(plan)["warnings"]))
        self.assertFalse(any("p.2" in w for w in self.check(plan, user_approved=True)["warnings"]))

    def test_reqs_count_budget_goal(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"], reqs=["REQ-1", "REQ-9"])], goal="REQ-1 a\nREQ-2 b\n")
        errs = self.check(plan)["errors"]
        self.assertIn("REQ-2: not covered by any batch", errs)
        self.assertIn("p.1: REQ-9 not in goal.md", errs)
        many = make_plan("p", [header(f"p.{i}", [f"main:{i}.py"]) for i in range(1, 12)])
        self.assertIn("11 batches > 10: split into milestones (several plans)", self.check(many)["errors"])
        big = make_plan("p", [header("p.1", ["main:a.py"], budget_estimate="80001")])
        self.assertIn("budget_estimate 80001 > half", self.check(big)["errors"][0])  # m2d.9: min(160000, 180000) / 2
        self.assertTrue(self.check(make_plan("p", [header("p.1", ["main:a.py"], budget_estimate="80000")]))["ok"])
        wide = {"context.window_tokens": 250_000, "context.abs_cap_tokens": 200_000}
        self.assertTrue(self.check(big, config=wide)["ok"])  # min(200000, 200000)
        self.assertFalse(self.check(big, config={**wide, "context.hard_pct": 60})["ok"])  # min(150000, 200000)
        plan = make_plan("p", [header("p.1", ["main:a.py"])])
        plan.goal.body = "REQ-1 changed\n"
        self.assertIn("goal.md: changed after it was frozen", self.check(plan)["errors"][0])

    def test_schema_errors_stop_early(self):
        plan = make_plan("p", [header("p.1", ["a.py"], accept_commands=[])])
        errs = self.check(plan)["errors"]
        self.assertTrue(any("accept_commands" in e for e in errs) and any("owns_paths[0]" in e for e in errs), errs)


class ScheduleTest(unittest.TestCase):
    def test_waves_order_and_width(self):
        hs = {h["id"]: h for h in [
            header("p.1", ["main:a"]),
            header("p.2", ["main:b"], tiers={**header("x.1", [])["tiers"], "difficulty": "L"}),
            header("p.3", ["main:c"], contract="true"),
            header("p.4", ["main:d"], ["p.1"]),
            header("p.5", ["main:e"], ["p.4"]),
        ]}
        # contract first, then risk (L), then the longer tail (p.1 heads p.4 -> p.5)
        self.assertEqual(schedule.waves(hs), [["p.3", "p.2", "p.1"], ["p.4"], ["p.5"]])
        self.assertEqual(schedule.waves(hs, 2), [["p.3", "p.2"], ["p.1"], ["p.4"], ["p.5"]])
        self.assertEqual(schedule.critical_path(hs), ["p.1", "p.4", "p.5"])
        hs["p.1"]["depends_on"] = ["p.5"]
        with self.assertRaises(schedule.CycleError):
            schedule.waves(hs)


if __name__ == "__main__":
    unittest.main()
