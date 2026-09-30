import json
import os
import shutil
import sys
import unittest

from foremind.install import tomlblock
from foremind.supervisor import tick
from foremind.vendors import claude
from test_install_fixture import Env, sh


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.root = self.env.repo(self.env.tmp / "shop")
        self.wf = self.root / ".github" / "workflows"
        self.wf.mkdir(parents=True)
        (self.wf / "ci.yml").write_text("on: push\n")
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", "--root", str(self.root))
        self.assertEqual(rc, 0, out)
        self.settings = self.root / ".claude" / "settings.local.json"
        self.delivery = self.root / ".foremind" / "delivery.toml"
        self.checks = self.root / "foremind.toml"
        self.checks.write_text('[gate]\nchecks = ["make test"]\n')  # #23 unanswered = the user's: needs them

    def set(self, old, new):
        self.delivery.write_text(self.delivery.read_text().replace(old, new))

    def green(self):
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)

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

    def test_local_checks_needed_for_ci_none_or_user_push(self):
        self.checks.write_text("")
        self.assert_fails("ci = none 或 #23 归用户的仓库有 [gate].checks", "[gate] checks")  # push_pr = "user"
        self.set('push_pr = "user"', 'push_pr = "system"')
        self.green()
        self.set('ci = "required"', 'ci = "none"')
        self.assert_fails("ci = none 或 #23 归用户的仓库有 [gate].checks", "[gate] checks")
        self.checks.write_text('[gate]\nchecks = ["make test"]\n')
        self.green()

    def test_ci_required_needs_a_workflow_or_required_checks(self):
        (self.wf / "ci.yml").unlink()
        self.green()  # #23 the user's: local checks stand in for CI, whatever [gate].ci says
        self.set('push_pr = "user"', 'push_pr = "system"')
        self.assert_fails("ci = required / local_first 的仓库有 CI", "ci 改成 none")
        prop = self.root / ".foremind" / "delivery.proposal.json"
        p = json.loads(prop.read_text())
        p["repos"]["main"]["facts"]["required_checks"]["value"] = ["ext/build"]  # external CI, required
        prop.write_text(json.dumps(p))
        self.green()
        p["repos"]["main"]["facts"]["required_checks"]["value"] = "unknown"
        prop.write_text(json.dumps(p))
        self.set('ci = "required"', 'ci = "local_first"')
        self.assert_fails("ci = required / local_first 的仓库有 CI", "[gate] checks")
        self.set('ci = "local_first"', 'ci = "none"')
        self.green()

    def proposal(self, rid="main", **facts):
        prop = self.root / ".foremind" / "delivery.proposal.json"
        p = json.loads(prop.read_text())
        for k, v in facts.items():
            p["repos"][rid]["facts"][k]["value"] = v
        prop.write_text(json.dumps(p))

    def test_foremind_gate_alone_is_not_ci(self):  # m2a.3 r1: the gate never waits for its own status
        (self.wf / "ci.yml").unlink()
        self.set('push_pr = "user"', 'push_pr = "system"')
        self.proposal(required_checks=["foremind/gate"])
        self.assert_fails("ci = required / local_first 的仓库有 CI", "ci 改成 none")
        self.proposal(required_checks=["foremind/gate", "ext/build"])
        self.green()

    def test_required_checks_read_off_another_branch_warn(self):
        self.set('push_pr = "user"', 'push_pr = "system"')
        self.proposal(required_checks=["ext/build"])  # no remote: the proposal's target branch is unknown
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)  # told, not counted
        line = next(x for x in out.splitlines() if x.startswith("WARN"))
        self.assertIn("必过检查取自门禁的目标分支", line)
        self.assertIn("来源分支未知", line)
        self.assertIn("建议：", out.split(line, 1)[1].splitlines()[1])
        self.delivery.write_text(self.delivery.read_text() + 'target_branch = "develop"\n')
        self.proposal(target_branch="main")
        self.assertIn("必过检查取自 main，目标分支 develop", self.doctor()[1])
        self.proposal(target_branch="develop")
        self.assertNotIn("WARN", self.doctor()[1])
        self.proposal(target_branch="main", required_checks="unknown")  # the gate reads no list: nothing to compare
        self.assertNotIn("WARN", self.doctor()[1])

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

    def test_supervisor_on_old_code(self):  # finding 23
        p = self.root / ".foremind" / "supervisor.json"
        p.write_text(json.dumps({"pid": os.getpid(), "code": 1}))
        self.assert_fails("监督进程代码与当前包一致", "supervisor_reexec_failed")
        p.write_text(json.dumps({"pid": os.getpid(), "code": tick.code_fingerprint()}))
        self.green()

    def test_supervisor_pid_taken_by_another_process(self):  # REQ-12, D23
        p = self.root / ".foremind" / "supervisor.json"
        p.write_text(json.dumps({"pid": os.getpid(), "code": tick.code_fingerprint(),
                                 "started_at": "2000-01-01T00:00:00+00:00"}))  # this process started long after
        rc, out = self.doctor()
        self.assertEqual(rc, 0, out)  # told, not counted
        line = next(x for x in out.splitlines() if x.startswith("WARN"))
        self.assertIn("监督进程未运行", line)
        self.assertIn("foremind supervise", out.split(line, 1)[1].splitlines()[1])
        self.assertNotIn("监督进程代码与当前包一致", out)

    def test_state_dir_not_ignored(self):
        tomlblock.remove(self.root / ".git" / "info" / "exclude", "exclude", check=False)
        self.assertEqual(self.doctor()[0], 0)  # .foremind/.gitignore alone keeps it ignored
        (self.root / ".foremind" / ".gitignore").unlink()
        self.assert_fails(".foremind/ 被 git 忽略", ".gitignore")


if __name__ == "__main__":
    unittest.main()
