import json
import tomllib
import unittest

from foremind.config import ConfigError
from foremind.install import detect
from foremind.repos import Repo
from test_install_fixture import Env, sh

UNKNOWN = detect.UNKNOWN


def facts(**v):
    base = {"can_merge": UNKNOWN, "can_push": UNKNOWN, "target_branch": "main", "allowed_merge_methods": UNKNOWN,
            "linear_history": UNKNOWN, "required_checks": UNKNOWN, "ci_workflows": [],
            "history": {"merge": 0, "squash": 0, "rebase": 0, "direct": 0}}
    return {k: detect.item(x, "test") for k, x in {**base, **v}.items()}


def repo(f):
    return {"facts": f, "proposal": detect.propose(f)}

REPO_INFO = {"full_name": "o/r", "default_branch": "main", "allow_squash_merge": True, "allow_merge_commit": False,
             "allow_rebase_merge": False, "delete_branch_on_merge": True, "permissions": {"push": True}}
PROTECTION = {"required_status_checks": {"strict": True, "contexts": ["ci/test"], "checks": [{"context": "ci/test"}]},
              "required_linear_history": {"enabled": True}, "allow_force_pushes": {"enabled": False},
              "required_pull_request_reviews": {"required_approving_review_count": 1}}
RULES = [{"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
         {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "lint"}],
                                                           "strict_required_status_checks_policy": False}}]


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.path = self.env.repo(self.env.tmp / "r")

    def commit(self, name, env=None):
        (self.path / name).write_text(name)
        sh(self.path, "git", "add", "-A")
        sh(self.path, "git", "commit", "-q", "-m", name, env=env)

    def with_origin(self):
        sh(self.env.tmp, "git", "init", "-q", "--bare", "origin.git")
        sh(self.path, "git", "remote", "add", "origin", str(self.env.tmp / "origin.git"))

    def values(self, part):
        return {k: v["value"] for k, v in part.items()}

    def test_history_counts_each_merge_kind(self):
        git = lambda *a, **kw: sh(self.path, "git", *a, **kw)  # noqa: E731
        git("checkout", "-q", "-b", "a")
        self.commit("a1")
        git("checkout", "-q", "main")
        git("merge", "-q", "--no-ff", "a", "-m", "Merge branch a")
        for b, msg in (("b", ["-m", "feature b (#3)"]), ("c", ["--no-edit"])):  # GitHub title / git's squash message
            git("checkout", "-q", "-b", b)
            self.commit(f"{b}1")
            git("checkout", "-q", "main")
            git("merge", "-q", "--squash", b)
            git("commit", "-q", *msg)
        git("checkout", "-q", "-b", "d")
        old = {"GIT_AUTHOR_DATE": "2020-01-01T00:00:00", "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"}
        self.commit("d1", env=old)
        self.commit("d2", env=old)
        git("checkout", "-q", "main")
        self.commit("m1")
        git("checkout", "-q", "d")
        git("rebase", "-q", "main")
        git("checkout", "-q", "main")
        git("merge", "-q", "--ff-only", "d")
        self.assertEqual(detect.history(self.path, "main"), {"merge": 1, "squash": 2, "rebase": 1, "direct": 2})

    def test_nothing_detectable_takes_the_strictest(self):
        self.with_origin()
        self.env.gh()  # every gh api call fails
        prop = {"repos": {"main": detect.detect_repo(Repo("main", self.path))}}
        facts = self.values(prop["repos"]["main"]["facts"])
        self.assertEqual({k for k, v in facts.items() if v == detect.UNKNOWN},
                         {"allowed_merge_methods", "default_merge_method", "delete_branch_on_merge", "can_push",
                          "can_merge", "target_branch", *detect.PROTECTION})
        self.assertIn("HTTP 404", prop["repos"]["main"]["facts"]["can_push"]["evidence"])
        resolved = detect.resolve(prop)
        self.assertEqual(resolved, {"main": {"level": "done", "push_pr": "user", "ci": "required"}})
        self.assertEqual(tomllib.loads(detect.delivery_toml(resolved)),
                         {"delivery": {"repo": {"main": {"level": "done", "push_pr": "user", "ci": "required"}}}})

    def test_settings_protection_and_rulesets(self):
        self.with_origin()
        self.env.gh(api={"repos/{owner}/{repo}": {"out": REPO_INFO},
                         "repos/o/r/branches/main/protection": {"out": PROTECTION},
                         "repos/o/r/rules/branches/main": {"out": RULES}})
        r = detect.detect_repo(Repo("main", self.path))
        facts = self.values(r["facts"])
        self.assertEqual(facts["allowed_merge_methods"], ["squash"])
        self.assertEqual(facts["target_branch"], "main")
        self.assertEqual(facts["can_push"], True)
        self.assertEqual((facts["required_checks"], facts["require_up_to_date"], facts["linear_history"],
                          facts["required_approvals"], facts["force_push_allowed"]),
                         (["ci/test", "lint"], True, True, 2, False))
        self.assertEqual(self.values(r["proposal"]),
                         {"level": detect.UNKNOWN, "push_pr": detect.UNKNOWN, "target_branch": "main",
                          "merge_method": "squash", "update_method": "merge", "ci": "required"})
        resolved = detect.resolve({"repos": {"main": r}}, {"main": {"level": "merge_dev"}})
        self.assertEqual(resolved["main"]["level"], "merge_dev")
        self.assertEqual(resolved["main"]["push_pr"], "user")

    def test_unreadable_protection_turns_a_no_into_unknown(self):
        self.with_origin()
        self.env.gh(api={"repos/{owner}/{repo}": {"out": {**REPO_INFO, "permissions": {"push": False}}},
                         "repos/o/r/branches/main/protection": {"err": "Resource not accessible (HTTP 403)"},
                         "repos/o/r/rules/branches/main": {"out": []}})
        r = detect.detect_repo(Repo("main", self.path))
        facts = self.values(r["facts"])
        self.assertEqual((facts["required_checks"], facts["linear_history"], facts["force_push_allowed"]),
                         (detect.UNKNOWN,) * 3)
        prop = self.values(r["proposal"])
        self.assertEqual((prop["level"], prop["push_pr"], prop["ci"]), ("done", "user", detect.UNKNOWN))
        self.assertEqual(detect.resolve({"repos": {"main": r}})["main"]["ci"], "required")

    def test_no_protection_at_all(self):
        self.with_origin()
        self.env.gh(api={"repos/{owner}/{repo}": {"out": REPO_INFO},
                         "repos/o/r/branches/main/protection": {"err": "Branch not protected (HTTP 404)"},
                         "repos/o/r/rules/branches/main": {"out": []}})
        r = detect.detect_repo(Repo("main", self.path))
        facts = self.values(r["facts"])
        self.assertEqual((facts["required_checks"], facts["linear_history"], facts["force_push_allowed"]),
                         ([], False, True))
        self.assertEqual(r["proposal"]["ci"]["value"], "none")
        (self.path / ".github" / "workflows").mkdir(parents=True)
        (self.path / ".github" / "workflows" / "ci.yml").write_text("on: push\n")
        self.assertEqual(detect.detect_repo(Repo("main", self.path))["proposal"]["ci"]["value"], "required")

    def test_build_writes_the_proposal(self):
        root = self.env.tmp / "r"
        prop = detect.build(root, [Repo("main", self.path)])
        self.assertEqual(json.loads((root / ".foremind" / "delivery.proposal.json").read_text()), prop)

    def test_settings_left_out_of_the_answer_are_unknown(self):
        self.with_origin()
        info = {k: v for k, v in REPO_INFO.items() if not k.startswith(("allow_", "delete_"))}  # no write access
        self.env.gh(api={"repos/{owner}/{repo}": {"out": info}})
        f = self.values(detect.detect_repo(Repo("main", self.path))["facts"])
        self.assertEqual((f["allowed_merge_methods"], f["default_merge_method"], f["delete_branch_on_merge"]),
                         (UNKNOWN,) * 3)

    def test_a_thin_history_leaves_the_merge_method_unknown(self):
        for n, want in ((2, UNKNOWN), (3, "rebase")):
            mm = detect.propose(facts(history={"merge": 0, "squash": 1, "rebase": n, "direct": 9}))["merge_method"]
            self.assertEqual(mm["value"], want)
            self.assertIn("样本", mm["evidence"])

    def test_answers_stand_until_a_fact_rules_them_out(self):
        confirmed = {"main": {"level": "merge_dev", "push_pr": "system", "target_branch": "dev", "merge_method": "merge",
                              "ci": "none"}}
        self.assertEqual(detect.resolve({"repos": {"main": repo(facts())}}, confirmed)["main"],
                         {**confirmed["main"], "update_method": "merge"})
        tight = facts(can_merge=False, can_push=False, linear_history=True, required_checks=["ci/test"],
                      allowed_merge_methods=["merge", "squash"])
        self.assertEqual(detect.resolve({"repos": {"main": repo(tight)}}, confirmed)["main"],
                         {"level": "done", "push_pr": "user", "target_branch": "dev", "merge_method": "squash",
                          "update_method": "merge", "ci": "required"})
        # update_method follows an answered merge method
        self.assertEqual(detect.resolve({"repos": {"main": repo(facts())}}, {"main": {"merge_method": "rebase"}})[
            "main"]["update_method"], "rebase")

    def test_confirmed_reads_delivery_toml_back(self):
        root = self.env.tmp / "r"
        detect.write_delivery(root, {"main": {"level": "merge_dev", "merge_method": "squash", "update_method": "merge"}})
        self.assertEqual(detect.confirmed(root), {"main": {"level": "merge_dev", "merge_method": "squash"}})
        (root / ".foremind" / "delivery.toml").write_text("not toml [")
        self.assertEqual(detect.confirmed(root), {})

    def test_write_delivery_keeps_the_old_file_when_the_config_rejects_it(self):
        root = self.env.tmp / "r"
        self.env.config.mkdir()
        (self.env.config / "config.toml").write_text('[delivery.repo.main]\nlevel = {ceiling = "done"}\n')
        p = detect.write_delivery(root, {"main": {"level": "done"}})
        old = p.read_bytes()
        with self.assertRaises(ConfigError):
            detect.write_delivery(root, {"main": {"level": "merge_dev"}})
        self.assertEqual(p.read_bytes(), old)
        p.unlink()
        with self.assertRaises(ConfigError):
            detect.write_delivery(root, {"main": {"level": "merge_dev"}})
        self.assertFalse(p.exists())
        detect.write_delivery(root, {"main": {"level": "done", "target_branch": "a\x7fb"}})  # escaped, still TOML
        self.assertEqual(detect.confirmed(root)["main"]["target_branch"], "a\x7fb")


if __name__ == "__main__":
    unittest.main()
