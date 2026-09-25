import shutil
import sys
import unittest

from foremind.install import tomlblock
from foremind.vendors import claude
from test_install_fixture import Env, sh


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.root = self.env.repo(self.env.tmp / "shop")
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", "--root", str(self.root))
        self.assertEqual(rc, 0, out)
        self.settings = self.root / ".claude" / "settings.local.json"

    def doctor(self, *argv):
        return self.env.run("doctor", *argv, project=self.root)

    def assert_fails(self, check, fix):
        rc, out = self.doctor()
        self.assertEqual(rc, 1, out)
        line = next((x for x in out.splitlines() if x.startswith("FAIL") and check in x), None)
        self.assertIsNotNone(line, out)
        self.assertIn(fix, out.split(line, 1)[1].splitlines()[1])

    def test_green_after_init(self):
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)
        self.assertIn("全部通过", out)

    def test_missing_interpreter(self):
        self.settings.write_text(self.settings.read_text().replace(sys.executable, "/nonexistent/python3"))
        self.assert_fails("钩子解释器 /nonexistent/python3", "foremind init --yes")
        self.assert_fails("钩子已装", "foremind init --yes")  # no longer the expected command either

    def test_package_path_that_cannot_import_foremind(self):
        bad = self.env.tmp / "shadow"
        (bad / "foremind").mkdir(parents=True)
        (bad / "foremind" / "__init__.py").write_text("raise ImportError('shadowed')\n")
        pkg = claude.foremind_command("x").split(" ", 1)[0].split("=", 1)[1]
        self.settings.write_text(self.settings.read_text().replace(f"PYTHONPATH={pkg}", f"PYTHONPATH={bad}"))
        self.assert_fails("钩子能导入 foremind", "重装钩子")

    def test_hooks_without_P_are_shadowed_by_a_foremind_in_their_cwd(self):
        (self.root / "foremind").mkdir()
        (self.root / "foremind" / "__init__.py").write_text("raise ImportError('shadowed')\n")
        self.assertEqual(self.doctor()[0], 0)  # -P: the hooks' cwd does not matter
        self.settings.write_text(self.settings.read_text().replace(" -P -m foremind", " -m foremind"))
        self.assert_fails("钩子能导入 foremind", "-P")

    def test_tracked_settings(self):
        sh(self.root, "git", "add", "-f", ".claude/settings.local.json")
        self.assert_fails("settings.local.json 未被 git 跟踪", "git rm --cached")

    def test_ci_none_needs_local_checks(self):
        d = self.root / ".foremind" / "delivery.toml"
        d.write_text(d.read_text().replace('ci = "required"', 'ci = "none"'))
        self.assert_fails("ci = none 的仓库有 [gate].checks", "[gate] checks")
        (self.root / "foremind.toml").write_text('[gate]\nchecks = ["make test"]\n')
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)

    def test_checks_create_nothing(self):
        other = self.env.repo(self.env.tmp / "other")
        (other / "foremind.toml").write_text("")
        shutil.rmtree(self.env.config)
        rc, out = self.env.run("doctor", project=other)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL 状态目录可写", out)
        self.assertIn("ok   supervisor.lock 可取", out)  # a missing lock can be taken
        self.assertFalse((other / ".foremind").exists())
        self.assertFalse(self.env.config.exists())

    def test_merge_dev_needs_a_way_to_merge(self):
        d = self.root / ".foremind" / "delivery.toml"
        d.write_text(d.read_text().replace('level = "done"', 'level = "merge_dev"'))
        self.assertNotIn("merge_method", d.read_text())
        self.assert_fails("merge_dev 的仓库有合入方式", "merge_method")
        d.write_text(d.read_text() + 'merge_method = "squash"\n')
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)

    def test_project_name_changed_under_a_running_batch(self):
        b = self.root / ".foremind" / "batches"
        b.mkdir()
        (b / "p.1.md").write_text("---\nid: p.1\nstate: running\n---\n")
        (b / "p.1.lock").write_text("fm-oldname-abcdef-p_1-1\n")
        self.assert_fails("[project].name 与在途批次一致", "改回")

    def test_missing_delivery_then_rescan(self):
        (self.root / ".foremind" / "delivery.toml").unlink()
        self.assert_fails("delivery.toml 存在", "foremind doctor --rescan")
        rc, out = self.doctor("--rescan", "--yes")
        self.assertEqual(rc, 0, out)

    def test_state_dir_not_ignored(self):
        tomlblock.remove(self.root / ".git" / "info" / "exclude", "exclude", check=False)
        self.assertEqual(self.doctor()[0], 0)  # .foremind/.gitignore alone keeps it ignored
        (self.root / ".foremind" / ".gitignore").unlink()
        self.assert_fails(".foremind/ 被 git 忽略", ".gitignore")


if __name__ == "__main__":
    unittest.main()
