import subprocess
import tempfile
import unittest
from pathlib import Path

from foremind.plan import coupling
from foremind.plan.validate import validate
from test_plan_helpers import ProjectCase, header, make_plan


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def commit(repo, files: dict, msg):
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", msg)


class CouplingTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git(self.repo, "init", "-q")
        commit(self.repo, {"pkg/__init__.py": "", "pkg/a.py": "from .b import f\n", "pkg/b.py": "def f(): pass\n"}, "1")
        commit(self.repo, {"pkg/c.py": "import os\n"}, "2")
        commit(self.repo, {"pkg/a.py": "from .b import f\nf()\n", "pkg/b.py": "def f(): return 1\n"}, "3")
        self.plan = make_plan("x", [
            header("x.1", ["main:pkg/a.py"], coupling=[{"batch": "x.2", "score": 1, "reason": "同一流程的两端"}]),
            header("x.2", ["main:pkg/b.py"]),
            header("x.3", ["main:pkg/c.py"], coupling=[{"batch": "x.2", "score": 1, "reason": "共享配置格式"}]),
        ])

    def pairs(self, config=None):
        return {(c["a"], c["b"]): c for c in coupling.analyze(self.plan, {"main": self.repo}, config)}

    def test_three_tiers(self):
        p = self.pairs()
        # a imports b: 1 of the 2 .py files; commits 1 and 3 touch both, none touches only one
        self.assertEqual({k: p[("x.1", "x.2")][k] for k in ("ref", "cochange", "semantic", "tier")},
                         {"ref": 0.5, "cochange": 1.0, "semantic": 1, "tier": "high"})
        self.assertAlmostEqual(p[("x.1", "x.2")]["score"], 0.8)
        self.assertEqual((p[("x.2", "x.3")]["score"], p[("x.2", "x.3")]["tier"]), (0.3, "medium"))
        self.assertEqual((p[("x.1", "x.3")]["score"], p[("x.1", "x.3")]["tier"]), (0.0, "low"))

    def test_config_overrides_thresholds(self):
        self.assertEqual(self.pairs({"plan.coupling.high": 0.9})[("x.1", "x.2")]["tier"], "medium")
        self.assertEqual(self.pairs({"plan.coupling.w_semantic": 0})[("x.2", "x.3")]["tier"], "low")

    def test_validate_warns_on_parallel_coupled_pairs(self):
        r = validate(self.root, self.plan, coupling=coupling.analyze(self.plan, {"main": self.repo}))
        self.assertTrue(r["ok"], r["errors"])
        self.assertEqual(len(r["warnings"]), 2)
        self.assertIn("high coupling", r["warnings"][0])
        self.assertIn("needs merge_after", r["warnings"][1])

    def test_no_repo_no_signal(self):
        p = coupling.analyze(self.plan, {}, None)
        self.assertEqual([c["ref"] + c["cochange"] for c in p], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
