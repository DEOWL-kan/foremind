import contextlib
import json
import shutil
import tomllib
import unittest
from unittest import mock

from foremind import config, install, seat
from foremind.install import settings, tomlblock
from foremind.vendors import claude
from test_install_fixture import Env, sh

USER_SETTINGS = ('{\n    "permissions": {"allow": ["Bash(ls)"]},\n'
                 '    "hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}]},\n'
                 '    "statusLine": {"type": "command", "command": "my-status --short", "padding": 1}\n}')
USER_TOML = "# mine\n[gate]\nchecks = [\"make test\"]"  # no trailing newline on purpose


class TomlBlockTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.p = self.env.tmp / "c.toml"

    def test_add_remove_round_trip(self):
        for original in ("a = 1\n", "a = 1", ""):
            self.p.write_text(original)
            tomlblock.add(self.p, "x", "[t]\nk = \"v\"")
            text = self.p.read_text()
            self.assertEqual(tomllib.loads(text)["t"], {"k": "v"})
            self.assertEqual(tomlblock.get(text, "x"), "[t]\nk = \"v\"")
            tomlblock.add(self.p, "x", "[t]\nk = \"w\"")  # replaced, not doubled
            self.assertEqual(self.p.read_text().count(">>> foremind x >>>"), 1)
            self.assertTrue(tomlblock.remove(self.p, "x"))
            self.assertEqual(self.p.read_text(), original, repr(original))
            self.assertFalse(tomlblock.remove(self.p, "x"))

    def test_new_file_from_template_goes_with_its_block(self):
        tomlblock.add(self.p, "x", "k = 1", template="# head\n")
        self.assertEqual(self.p.read_text(), "# head\n# >>> foremind x >>>\nk = 1\n# <<< foremind x <<<\n")
        tomlblock.remove(self.p, "x", template="# head\n")
        self.assertFalse(self.p.exists())

    def test_broken_toml_is_rolled_back(self):
        self.p.write_text(USER_TOML)
        with self.assertRaises(tomlblock.BlockError):
            tomlblock.add(self.p, "x", "[gate]\nci = \"none\"")  # [gate] defined twice
        self.assertEqual(self.p.read_text(), USER_TOML)
        self.assertEqual(len(list((self.env.config / "backups").iterdir())), 1)
        with self.assertRaises(tomlblock.BlockError):
            tomlblock.add(self.env.tmp / "new.toml", "x", "k = ")
        self.assertFalse((self.env.tmp / "new.toml").exists())

    def test_value(self):
        v = {"s": 'a "b"\\ \n ü \x7f \x1f', "b": True, "n": 3, "l": ["x", 1]}
        text = "\n".join(f"{k} = {tomlblock.value(x)}" for k, x in v.items())
        self.assertEqual(tomllib.loads(text), v)

    def test_symlink_stays_a_link(self):
        target = self.env.tmp / "dots.toml"
        target.write_text("a = 1\n")
        self.p.symlink_to(target)
        tomlblock.add(self.p, "x", "k = 1")
        self.assertTrue(self.p.is_symlink())
        self.assertIn("k = 1", target.read_text())
        tomlblock.remove(self.p, "x")
        self.assertTrue(self.p.is_symlink())
        self.assertEqual(target.read_text(), "a = 1\n")

    def test_not_utf8_or_a_bom_is_left_alone(self):
        for raw in ("a = 'é'\n".encode("latin-1"), b"\xef\xbb\xbfa = 1\n"):
            self.p.write_bytes(raw)
            with self.assertRaises(tomlblock.BlockError):
                tomlblock.add(self.p, "x", "k = 1")
            self.assertEqual(self.p.read_bytes(), raw)


class InitUninstallTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def init(self, *argv):
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", *argv)
        self.assertEqual(rc, 0, out)
        return out

    def fails(self, root) -> list[str]:
        """doctor's failed checks."""
        rc, out = self.env.run("doctor", project=root)
        names = [x[5:].split("：")[0] for x in out.splitlines() if x.startswith("FAIL")]
        self.assertEqual(rc, 1 if names else 0, out)
        return names

    def doctor(self, root):
        self.assertEqual(self.fails(root), [])

    def claude_settings(self, cmd, padding=None):
        (self.env.home / ".claude").mkdir(exist_ok=True)
        sl = {"type": "command", "command": cmd, **({"padding": padding} if padding else {})}
        (self.env.home / ".claude" / "settings.json").write_text(json.dumps({"statusLine": sl}))

    def test_single_repo_keeps_and_restores_user_files(self):
        root = self.env.repo(self.env.tmp / "shop")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.local.json").write_text(USER_SETTINGS)
        (root / "foremind.toml").write_text(USER_TOML)
        exclude = root / ".git" / "info" / "exclude"
        before = {p: p.read_bytes() for p in (root / ".claude" / "settings.local.json", root / "foremind.toml", exclude)}

        self.init("--repo", f"main={root}")
        self.init("--repo", f"main={root}")  # rerun: idempotent
        s = json.loads((root / ".claude" / "settings.local.json").read_text())
        self.assertEqual(s["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(s["hooks"]["Stop"][0]["hooks"][0]["command"], "say done")  # the user's entry stays first
        self.assertEqual([h["hooks"][0]["command"] for h in s["hooks"]["Stop"][1:]], [claude.hook_command("Stop")])
        # a local status line would become every project's: not without consent, --yes keeps it (I53②)
        self.assertEqual(s["statusLine"], json.loads(USER_SETTINGS)["statusLine"])
        self.assertFalse((self.env.config / "config.toml").exists())
        self.assertEqual((root / "foremind.toml").read_bytes(), before[root / "foremind.toml"])
        self.assertIn("/.foremind/", exclude.read_text())
        self.assertEqual(install.registered(), [str(root)])
        cfg = config.load(root)
        self.assertEqual(cfg["repos"], [{"id": "main", "path": "."}])
        self.assertEqual((cfg["project.name"], cfg["carrier.kind"], cfg["notify.channel"]), ("shop", "tmux", "none"))
        self.assertEqual(cfg["gate.checks"], ["make test"])
        self.assertEqual(cfg["delivery.repo.main.level"], "done")
        self.assertEqual(cfg["delivery.repo.main.push_pr"], "user")
        self.assertTrue((root / ".foremind" / "delivery.proposal.json").is_file())
        self.assertEqual(self.fails(root), [f"钩子已装 {root / '.claude' / 'settings.local.json'}"])  # the status line

        rc, out = self.env.run("uninstall", project=root)
        self.assertEqual(rc, 0, out)
        for p, b in before.items():
            self.assertEqual(p.read_bytes(), b, p)
        self.assertFalse((self.env.config / "config.toml").exists())
        self.assertFalse((root / ".foremind" / "config.toml").exists())
        self.assertEqual(install.registered(), [])
        self.assertTrue((root / ".foremind" / "delivery.toml").exists())  # data stays
        self.assertNotIn(".foremind", sh(root, "git", "status", "--porcelain", "--untracked-files=all"))  # still ignored

    def test_two_repos_from_scratch(self):
        root = self.env.tmp / "proj"
        api, app = self.env.repo(root / "api"), self.env.repo(root / "app")
        self.init("--repo", f"api={api}", "--repo", f"app={app}", "--root", str(root), "--name", "p")
        self.assertEqual(config.load(root)["repos"], [{"id": "api", "path": "api"}, {"id": "app", "path": "app"}])
        for d in (root, api, app):
            self.assertEqual(settings.check(d), [])
        self.assertIn("/.claude/settings.local.json", (api / ".git" / "info" / "exclude").read_text())
        self.doctor(root)
        registered = (root / ".foremind" / "config.toml").read_bytes()
        with contextlib.chdir(api):
            self.init()  # doctor's fix, run inside one of the repos: the same project, nothing reset (I53③)
        self.assertEqual((root / ".foremind" / "config.toml").read_bytes(), registered)
        self.assertFalse((api / ".foremind").exists())
        self.doctor(root)
        rc, out = self.env.run("uninstall", project=root)
        self.assertEqual(rc, 0, out)
        for d in (root, api, app):
            self.assertFalse((d / ".claude").exists())
        self.assertNotIn("foremind", (api / ".git" / "info" / "exclude").read_text())

    def test_rerun_keeps_what_was_registered(self):
        root = self.env.repo(self.env.tmp / "shop")
        rc, out = self.env.run("init", "--yes", "--carrier", "manual", "--notify", "ntfy", "--name", "foo",
                               "--repo", f"app={root}")
        self.assertEqual(rc, 0, out)
        slug = seat.project_slug(root, config.load(root))
        d = root / ".foremind" / "delivery.toml"
        d.write_text(d.read_text().replace('"done"', '"merge_dev"').replace('"user"', '"system"'))  # confirmed since
        rc, out = self.env.run("init", "--yes", "--root", str(root))  # doctor's fix
        self.assertEqual(rc, 0, out)
        cfg = config.load(root)
        self.assertEqual((cfg["project.name"], cfg["carrier.kind"], cfg["notify.channel"]), ("foo", "manual", "ntfy"))
        self.assertEqual(cfg["repos"], [{"id": "app", "path": "."}])
        self.assertEqual(seat.project_slug(root, cfg), slug)
        self.assertEqual((cfg["delivery.repo.app.level"], cfg["delivery.repo.app.push_pr"]), ("merge_dev", "system"))

    def test_status_line_from_the_users_claude_settings_is_wrapped(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.claude_settings("g", padding=2)
        self.init("--root", str(root))  # no --repo: the root is the repo, id main
        self.assertEqual(settings.wrapped(), ("g", True))
        self.assertEqual(settings.check(root), [])
        self.assertEqual(json.loads(settings.path(root).read_text())["statusLine"],
                         {"type": "command", "command": claude.foremind_command("statusline"), "padding": 2})
        self.assertEqual(config.load(root)["repos"], [{"id": "main", "path": "."}])
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        self.assertFalse((root / ".claude").exists())
        self.assertEqual(settings.wrapped(), (None, False))

    def test_status_line_from_the_project_is_not_wrapped_without_consent(self):
        root = self.env.repo(self.env.tmp / "shop")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text('{"statusLine": {"type": "command", "command": "./st.sh"}}')
        self.claude_settings("g")  # the project's is the one in effect
        out = self.init("--root", str(root))
        self.assertIn("没有同意就不串联", out)
        self.assertEqual(settings.wrapped(), (None, False))
        self.assertFalse((self.env.config / "config.toml").exists())
        self.assertNotIn("statusLine", json.loads(settings.path(root).read_text()))
        self.assertEqual(self.fails(root), [f"钩子已装 {settings.path(root)}"])

    def test_a_second_project_with_another_status_line(self):
        a, b = self.env.repo(self.env.tmp / "a"), self.env.repo(self.env.tmp / "b")
        self.claude_settings("g")
        self.init("--root", str(a))
        self.claude_settings("h")
        out = self.init("--root", str(b))
        self.assertIn("无法再串联 'h'", out)
        self.assertEqual(settings.wrapped(), ("g", True))
        self.assertEqual(self.fails(a), [])
        self.assertEqual(self.fails(b), [f"钩子已装 {settings.path(b)}"])

    def test_a_statusline_table_without_command(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.env.config.mkdir()
        (self.env.config / "config.toml").write_text("[statusline]\ntimeout = 3\n")
        self.claude_settings("g")
        out = self.init("--root", str(root))
        self.assertIn("已有 [statusline] 表但没有 command", out)
        self.assertEqual((self.env.config / "config.toml").read_text(), "[statusline]\ntimeout = 3\n")

    def test_symlinked_files_stay_links(self):
        root = self.env.repo(self.env.tmp / "shop")
        dots = self.env.tmp / "dots"
        dots.mkdir()
        (dots / "settings.local.json").write_text("{}")
        (dots / "config.toml").write_text("# mine\n")
        (root / ".claude").mkdir()
        self.env.config.mkdir()
        links = (settings.path(root), self.env.config / "config.toml")
        for p in links:
            p.symlink_to(dots / p.name)
        self.claude_settings("g")
        self.init("--root", str(root))
        self.assertTrue(all(p.is_symlink() for p in links))
        self.assertEqual(settings.check(root), [])
        self.assertEqual(settings.wrapped(), ("g", True))
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        self.assertTrue(all(p.is_symlink() for p in links))
        self.assertEqual([(dots / p.name).read_text() for p in links], ["{}", "# mine\n"])

    def test_a_tracked_settings_file_is_refused(self):
        root = self.env.repo(self.env.tmp / "shop")
        settings.path(root).parent.mkdir()
        settings.path(root).write_text("{}")
        sh(root, "git", "add", ".claude/settings.local.json")
        sh(root, "git", "commit", "-q", "-m", "s")
        rc, out = self.env.run("init", "--yes", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn("git rm --cached", out)
        self.assertFalse((root / ".foremind").exists())

    def test_unreadable_settings_stop_before_anything_is_written(self):
        root = self.env.repo(self.env.tmp / "shop")
        settings.path(root).parent.mkdir()
        for raw, word in (('{"a": 1}'.encode("utf-16"), "UTF-8"), (b"\xef\xbb\xbf{}", "byte order mark")):
            settings.path(root).write_bytes(raw)
            rc, out = self.env.run("init", "--yes", "--root", str(root))
            self.assertEqual(rc, 1, out)
            self.assertIn(word, out)
            self.assertEqual(len(out.strip().splitlines()), 1, out)
            self.assertFalse((root / ".foremind").exists())

    def test_eof_on_a_question(self):
        root = self.env.repo(self.env.tmp / "shop")
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", side_effect=EOFError):
            rc, out = self.env.run("init", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn("--yes", out)

    def test_scope_and_codex_say_why(self):
        root = self.env.repo(self.env.tmp / "shop")
        for argv, word in ((("--scope", "global"), "M3-1"), (("--codex",), "M2-7"), (("--carrier", "herdr"), "herdr")):
            rc, out = self.env.run("init", "--yes", "--repo", f"main={root}", *argv)
            self.assertEqual(rc, 2)
            self.assertIn(word, out)
        with mock.patch("sys.stdin.isatty", return_value=False):
            rc, out = self.env.run("init", "--repo", f"main={root}")
        self.assertEqual(rc, 2)
        self.assertIn("--yes", out)
        self.assertFalse((root / ".foremind").exists())

    def interactive(self, root, answers):
        answers = iter(answers)
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", lambda _: next(answers)):
            rc, out = self.env.run("init", "--root", str(root))
        self.assertEqual(rc, 0, out)
        self.assertEqual(next(answers, None), None, "unused answers")
        return out

    def test_interactive_answers(self):
        root = self.env.repo(self.env.tmp / "shop")
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.json").write_text('{"statusLine": {"type": "command", "command": "./st.sh"}}')
        # name, carrier (a wrong one first), notify, level, push_pr, target branch (skipped), merge method, ci,
        # write delivery.toml, wrap the project's status line
        self.interactive(root, ["", "herdr", "manual", "", "merge_dev", "", "", "squash", "", "y", "y"])
        cfg = config.load(root)
        self.assertEqual((cfg["project.name"], cfg["carrier.kind"]), ("shop", "manual"))
        self.assertEqual((cfg["delivery.repo.main.level"], cfg["delivery.repo.main.push_pr"],
                          cfg["delivery.repo.main.merge_method"], cfg["delivery.repo.main.update_method"],
                          cfg["delivery.repo.main.ci"]), ("merge_dev", "user", "squash", "merge", "required"))
        self.assertNotIn("delivery.repo.main.target_branch", cfg)
        self.assertEqual(settings.wrapped(), ("./st.sh", True))
        self.assertEqual(settings.check(root), [])

    def test_interactive_within_the_facts(self):
        root = self.env.repo(self.env.tmp / "shop")
        sh(self.env.tmp, "git", "init", "-q", "--bare", "origin.git")
        sh(root, "git", "remote", "add", "origin", str(self.env.tmp / "origin.git"))
        self.env.gh(api={"repos/{owner}/{repo}": {"out": {
            "full_name": "o/r", "default_branch": "main", "allow_merge_commit": True, "allow_squash_merge": True,
            "allow_rebase_merge": True, "delete_branch_on_merge": False, "permissions": {"push": False}}},
            "repos/o/r/branches/main/protection": {"err": "Branch not protected (HTTP 404)"},
            "repos/o/r/rules/branches/main": {"out": []}})
        # name, carrier, notify, level (merge_dev refused: no merge right), push_pr (system refused: no push right),
        # then the detected target branch, merge method and ci changed, write
        out = self.interactive(root, ["", "", "", "merge_dev", "", "system", "", "develop", "rebase", "local_first", "y"])
        self.assertIn("可选：done\n", out)
        self.assertIn("可选：user\n", out)
        cfg = config.load(root)
        self.assertEqual({k: cfg[f"delivery.repo.main.{k}"] for k in
                          ("level", "push_pr", "target_branch", "merge_method", "update_method", "ci")},
                         {"level": "done", "push_pr": "user", "target_branch": "develop", "merge_method": "rebase",
                          "update_method": "rebase", "ci": "local_first"})

    def test_uninstall_without_manifests(self):
        root = self.env.tmp / "proj"
        api = self.env.repo(root / "api")
        self.init("--repo", f"api={api}", "--root", str(root), "--name", "p")
        shutil.rmtree(root / ".foremind" / "install")
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        for d in (root, api):
            self.assertEqual(settings.installed_commands(d), [])

    def test_a_foremind_looking_hook_of_the_users(self):
        root = self.env.repo(self.env.tmp / "shop")
        old = json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "/old/python -m foremind hook Stop"}]}]}}, indent=1)
        settings.path(root).parent.mkdir()
        settings.path(root).write_text(old)
        self.init("--root", str(root))
        self.assertEqual(settings.check(root), [])  # replaced by ours, not doubled
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        self.assertEqual(settings.path(root).read_text(), old)

    def test_check_finds_doubled_and_missing_hooks(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        s = json.loads(settings.path(root).read_text())
        s["hooks"]["Stop"] *= 2
        del s["hooks"]["PreToolUse"]
        settings.path(root).write_text(json.dumps(s))
        probs = settings.check(root)
        self.assertEqual(len(probs), 2, probs)
        self.assertIn("PreToolUse", probs[0])
        self.assertIn("Stop", probs[1])

    def test_a_moved_repo_is_not_reinstalled_at_its_old_path(self):  # S1
        root = self.env.tmp / "proj"
        api, app = self.env.repo(root / "api"), self.env.repo(root / "app")
        self.init("--repo", f"api={api}", "--repo", f"app={app}", "--root", str(root), "--name", "p")
        shutil.move(app, self.env.tmp / "app2")
        rc, out = self.env.run("init", "--yes", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn(f"仓库 app 不在 {app}", out)
        self.assertFalse(app.exists())

    def test_rescan_yes_says_what_it_keeps(self):  # S2
        root = self.env.repo(self.env.tmp / "shop")
        sh(self.env.tmp, "git", "init", "-q", "--bare", "origin.git")
        sh(root, "git", "remote", "add", "origin", str(self.env.tmp / "origin.git"))
        self.env.gh(api={"repos/{owner}/{repo}": {"out": {"full_name": "o/r", "default_branch": "main"}}})
        self.init("--root", str(root))
        d = root / ".foremind" / "delivery.toml"
        self.assertIn('target_branch = "main"', d.read_text())
        d.write_text(d.read_text().replace('target_branch = "main"', 'target_branch = "master"'))
        rc, out = self.env.run("doctor", "--rescan", "--yes", project=root)
        self.assertIn("保留 main.target_branch = master（这次检测到 main）；要改请交互运行 foremind doctor --rescan", out)
        self.assertNotIn("保留 main.level", out)  # the proposal for it is unknown
        self.assertIn('target_branch = "master"', d.read_text())

    def test_state_dir_is_ignored_even_when_init_stops_midway(self):  # note 1
        root = self.env.repo(self.env.tmp / "shop")
        answers = iter(["", "", ""])  # name, carrier, notify; then EOF on the delivery questions

        def ask(_):
            try:
                return next(answers)
            except StopIteration:
                raise EOFError from None
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            rc, out = self.env.run("init", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertTrue((root / ".foremind" / "delivery.proposal.json").exists())
        self.assertNotIn(".foremind", sh(root, "git", "status", "--porcelain", "--untracked-files=all"))

    def test_a_dangling_symlink_is_left_dangling(self):  # note 2
        root = self.env.repo(self.env.tmp / "shop")
        target = self.env.tmp / "dots" / "settings.local.json"
        target.parent.mkdir()
        settings.path(root).parent.mkdir()
        settings.path(root).symlink_to(target)
        self.init("--root", str(root))
        self.assertTrue(target.exists())
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        self.assertTrue(settings.path(root).is_symlink())
        self.assertFalse(target.exists())

    def test_unreadable_user_files_stop_before_anything_is_written(self):  # note 4
        root = self.env.repo(self.env.tmp / "shop")
        self.env.config.mkdir()
        for name, raw in (("config.toml", b"\xef\xbb\xbf# mine\n"), ("projects", b"/a/\xe9\n")):
            (self.env.config / name).write_bytes(raw)
            rc, out = self.env.run("init", "--yes", "--root", str(root))
            self.assertEqual(rc, 1, out)
            self.assertNotIn("Traceback", out)
            self.assertIn(str(self.env.config / name), out)
            self.assertFalse((root / ".foremind").exists())
            (self.env.config / name).unlink()

    def test_uninstall_checks_everything_before_changing_anything(self):  # note 4
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        installed = settings.path(root).read_bytes()
        (manifest,) = (root / ".foremind" / "install").glob("settings-*.json")
        good = manifest.read_bytes()
        for p, raw in ((manifest, b"{"), (self.env.config / "projects", b"\xe9\n"),
                       (root / ".git" / "info" / "exclude", b"\xe9\n")):
            before = p.read_bytes()
            p.write_bytes(raw)
            rc, out = self.env.run("uninstall", project=root)
            self.assertEqual(rc, 1, out)
            self.assertIn(str(p), out)
            self.assertEqual(settings.path(root).read_bytes(), installed)
            self.assertTrue((root / ".foremind" / "config.toml").exists())
            p.write_bytes(before)
        self.assertEqual(manifest.read_bytes(), good)
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)

    def test_uninstall_keeps_what_the_user_added_since(self):
        root = self.env.repo(self.env.tmp / "shop")
        p = root / ".claude" / "settings.local.json"
        p.parent.mkdir()
        p.write_text(USER_SETTINGS)
        self.init("--root", str(root))
        s = json.loads(p.read_text())
        s["env"] = {"A": "1"}
        p.write_text(json.dumps(s))
        self.assertEqual(self.env.run("uninstall", project=root)[0], 0)
        self.assertEqual(json.loads(p.read_text()), {**json.loads(USER_SETTINGS), "env": {"A": "1"}})


if __name__ == "__main__":
    unittest.main()
