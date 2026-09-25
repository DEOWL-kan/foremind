import contextlib
import io
import re

from foremind import schemas
from foremind.cli import main
from foremind.plan import model, render
from foremind.plan.validate import validate
from test_plan_helpers import ProjectCase, header, make_plan


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class DoTest(ProjectCase):
    def test_do_writes_a_planned_batch(self):
        code, out, err = run(["do", "修复登录超时", "--owns", "main:src/login.py", "--accept", "python3 -m unittest"])
        self.assertEqual(code, 0, err)
        self.assertIn("do-1.1 planned", out)
        plan = model.load(self.root, "do-1")  # load() checks both schemas
        h = plan.batches["do-1.1"].header
        self.assertEqual(schemas.validate("batch_header", h), [])
        self.assertEqual((h["state"], h["tiers"]["difficulty"], h["reqs"]), ("planned", "S", ["REQ-1"]))
        self.assertEqual(plan.doc.header["approved_by"], "user")
        self.assertIn("修复登录超时", plan.goal.body)
        self.assertTrue(model.is_bound(self.root, plan))
        self.assertTrue(validate(self.root, plan)["ok"])
        # a second task on overlapping paths is serialised behind the first
        code, out, err = run(["do", "登录加验证码", "--owns", "main:src/*.py", "--accept", "true"])
        self.assertEqual(code, 0, err)
        self.assertEqual(model.load(self.root, "do-2").batches["do-2.1"].header["depends_on"], ["do-1.1"])
        code, _, err = run(["do", "x", "--owns", "src/a.py", "--accept", "true"])
        self.assertEqual(code, 1)
        self.assertIn("<repo-id>:<path>", err)
        self.assertFalse((self.root / ".foremind" / "plans" / "do-3").exists())


class PlanCliTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.save(make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:*.py"]),
                                  header("p.3", ["main:c.md"], ["p.1"])], body="说明 <b>粗体</b>\n"))

    def test_validate_apply_approve(self):
        code, out, _ = run(["plan", "validate", "p"])
        self.assertEqual(code, 1)
        self.assertIn("补边 p.2 depends_on p.1", out)
        self.assertEqual(run(["plan", "approve", "p"])[0], 1)
        code, out, _ = run(["plan", "validate", "p", "--apply"])
        self.assertEqual(code, 0, out)
        self.assertIn("p.1（owns_paths 重叠）", out)
        self.assertEqual(run(["plan", "approve", "p"])[:2], (0, "p: approved\n"))
        self.assertEqual(run(["plan", "validate", "p", "--apply"])[0], 0)  # nothing to add: fine
        self.save(make_plan("q", [header("q.1", ["main:x.py"]), header("q.2", ["main:x.py"])], approved=True))
        code, _, err = run(["plan", "validate", "q", "--apply"])
        self.assertEqual(code, 1)
        self.assertIn("add the edges with plan amend", err)
        goal = self.root / "g.md"
        goal.write_text("REQ-1: 别的\n")
        code, _, err = run(["plan", "amend", "p", "--reason", "改目标", "--goal", str(goal)])
        self.assertEqual(code, 1)
        self.assertIn("approval", err)
        self.assertEqual(run(["plan", "amend", "p", "--reason", "改目标", "--goal", str(goal), "--user-approved"])[0], 0)

    def test_show_writes_self_contained_html(self):
        code, out, _ = run(["plan", "show", "p"])
        self.assertEqual(code, 0)
        self.assertIn("批次 | 波次 | 档 | 参与方式 | 状态 | 串行原因", out)
        page = (self.root / ".foremind" / "plans" / "p" / "plan.html").read_text()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIsNone(re.search(r"https?:|<script|<link|src=", page))
        self.assertIn("<svg", page)
        for b in ("p.1", "p.2", "p.3"):
            self.assertIn(f'<details id="b-{b}">', page)
            self.assertIn(f'href="#b-{b}"', page)
        self.assertIn("&lt;b&gt;粗体&lt;/b&gt;", page)
        self.assertNotIn("<b>粗体", page)

    def test_html_marks_critical_path(self):
        plan = make_plan("p", [header("p.1", ["main:a.py"]), header("p.2", ["main:b.py"], ["p.1"]),
                               header("p.3", ["main:c.py"])])
        page = render.html(plan, validate(self.root, plan))
        self.assertEqual(page.count('class="node crit"'), 2)
        self.assertEqual(page.count('<line class="crit"'), 1)
