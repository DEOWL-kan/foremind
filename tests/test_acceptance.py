import json
import unittest
from pathlib import Path

from foremind import acceptance, review
from foremind.paths import state_dir
from test_gate_fixture import Project, sh


def output(res, i):
    return Path(res["commands"][i]["output_path"]).read_text().strip()


class AcceptanceTest(unittest.TestCase):
    def test_single_repo_checkout_and_latest_run_counts(self):
        p = Project(self)
        green = p.tmp / "green"
        p.batch(accept_commands=[f"pwd -P; git rev-parse HEAD; test -f {green}"])
        head = p.commit()
        red = acceptance.run(p.root, "shop.1", p.cfg)
        self.assertEqual(red["heads"], {"api": head})
        self.assertEqual(red["commands"][0]["repo"], "api")  # where it ran, for the gate to compare
        self.assertNotEqual(red["commands"][0]["exit_code"], 0)
        cwd, checked_out = output(red, 0).splitlines()
        self.assertEqual((Path(cwd).name, checked_out), ("api", head))  # single-repo batch: that repo (§20 I8)
        self.assertNotEqual(cwd, str(p.wt()))
        self.assertFalse(Path(cwd).exists())  # the throwaway checkout is gone...
        self.assertEqual(len(sh(p.wt(), "git", "worktree", "list").splitlines()), 2)  # ...and unregistered
        path = state_dir(p.root) / "batches" / f"shop.1.accept.{review.heads_hash({'api': head})}.json"
        self.assertEqual(json.loads(path.read_text()), red)
        green.touch()
        self.assertEqual(acceptance.run(p.root, "shop.1", p.cfg)["commands"][0]["exit_code"], 0)  # same SHA, now green
        self.assertEqual(json.loads(path.read_text())["commands"][0]["exit_code"], 0)
        self.assertEqual([e["ok"] for e in p.events("accept_run")], [False, True])

    def test_multi_repo_directories(self):
        p = Project(self, ("api", "app"))
        p.batch(accept_commands=["ls"])
        p.commit("api")
        p.commit("app")
        self.assertEqual(output(acceptance.run(p.root, "shop.1", p.cfg), 0).split(), ["api", "app"])  # batch dir
        p.cfg["gate.checks"] = ["pwd -P", {"run": "pwd -P", "repo": "app"}]
        res = acceptance.run(p.root, "shop.1", p.cfg, kind="checks")
        self.assertEqual([c.get("repo") for c in res["commands"]], [None, "app"])  # the batch dir has no repo
        top, app = Path(output(res, 0)), Path(output(res, 1))
        self.assertEqual((app.parent, app.name), (top, "app"))
        self.assertTrue(acceptance.result_path(p.root, "shop.1", "checks", res["heads"]).name.startswith("shop.1.checks."))

    def test_live_worktree_changes_are_not_seen(self):
        p = Project(self)
        p.batch(accept_commands=["grep -qx 'x = 1' src/app.py && test ! -e src/new.py"])
        p.commit()
        (p.wt() / "src" / "app.py").write_text("dirty\n")  # the seat is still editing
        (p.wt() / "src" / "new.py").write_text("y = 2\n")
        self.assertEqual(acceptance.run(p.root, "shop.1", p.cfg)["commands"][0]["exit_code"], 0)
        self.assertEqual((p.wt() / "src" / "app.py").read_text(), "dirty\n")  # and left untouched
        hook = p.root / "api" / ".git" / "hooks" / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {p.tmp / 'hooked'}\n")
        hook.chmod(0o755)
        acceptance.run(p.root, "shop.1", p.cfg)
        self.assertFalse((p.tmp / "hooked").exists())  # the throwaway checkout runs no repo hooks (LFS, lefthook)
        p.cfg["gate.checks"] = [{"run": "true", "repo": "nope"}]
        with self.assertRaisesRegex(review.FlowError, "bad checks command"):
            acceptance.run(p.root, "shop.1", p.cfg, kind="checks")


if __name__ == "__main__":
    unittest.main()
