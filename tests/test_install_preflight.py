"""m3a.6 REQ-6: the install manifest's format_version, init's and uninstall's checks before their first write, entries
changed since install, and doctor's view of which Foremind the hooks run."""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

import foremind
from foremind.install import settings
from foremind.vendors import claude
from test_install_fixture import Env

USER_HOOK = {"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)

    def init(self, *argv) -> str:
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", *argv)
        self.assertEqual(rc, 0, out)
        return out

    def snapshot(self) -> dict:
        """Every file under the temp dir (git's own files but info/exclude left out, and the entries' admission lock,
        m3a.8), as bytes."""
        return {p: p.read_bytes() for p in self.env.tmp.rglob("*")
                if p.is_file() and p.name not in ("gh.log", "admission.lock") and "__pycache__" not in p.parts
                and (".git" not in p.parts or p.parts[-2:] == ("info", "exclude"))}

    def unchanged(self, before):
        after = self.snapshot()
        self.assertEqual(sorted(map(str, after)), sorted(map(str, before)))
        for p, b in before.items():
            self.assertEqual(after[p], b, p)

    def upgrade(self, root) -> str:
        """The user's `foremind upgrade` (m3a.8 REQ-5: a project that needs one refuses init and uninstall until then)."""
        with mock.patch.dict(os.environ):
            for k in ("FOREMIND_CONTROLLER", "FOREMIND_JOB"):  # upgrade refuses them; a land runs the tests in a job
                os.environ.pop(k, None)
            rc, out = self.env.run("upgrade", project=root)
        self.assertEqual(rc, 0, out)
        return out

    def three(self):
        root = self.env.tmp / "proj"
        api, app = self.env.repo(root / "api"), self.env.repo(root / "app")
        self.init("--repo", f"api={api}", "--repo", f"app={app}", "--root", str(root), "--name", "p")
        return root, api, app

    def manifest(self, root, d) -> Path:
        return settings.manifest_path(root, d)

    def edit(self, m, **kv):
        man = json.loads(m.read_text())
        for k, v in kv.items():
            if v is ...:
                man.pop(k, None)
            else:
                man[k] = v
        m.write_text(json.dumps(man))

    def test_current_version_is_written_as_an_integer(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        m = self.manifest(root, root)
        self.assertIs(json.loads(m.read_text())["format_version"], 1)
        ((got, (v, man)),) = settings.scan(root)
        self.assertEqual((got, v, man["path"]), (m, 1, str(settings.path(root))))

    def test_both_v0_shapes_are_read_in_memory_then_rewritten_by_init(self):
        root = self.env.repo(self.env.tmp / "shop")
        p = settings.path(root)
        p.parent.mkdir()
        p.write_text(json.dumps({"hooks": {"Stop": [USER_HOOK]}, "statusLine": {"type": "command", "command": "mine"}}))
        self.init("--root", str(root))
        m = self.manifest(root, root)
        v1 = json.loads(m.read_text())
        # v0 per value: the same without format_version
        self.edit(m, format_version=...)
        old = m.read_bytes()
        self.assertEqual(settings.scan(root)[0][1][0], 0)
        man, notes = settings.load(root, root)
        self.assertEqual(({**man, "format_version": 1}, notes), (v1, []))
        self.assertIn("ok   install manifests：1 readable (format_version 0: 1; 0 = none)",
                      self.env.run("doctor", project=root)[1])
        self.assertEqual(m.read_bytes(), old)  # load, scan, doctor: read only
        # v0 with the file's text before the first install, as an older install left the file: ours in place of theirs
        s = json.loads(p.read_text())
        s["statusLine"] = {"type": "command", "command": claude.foremind_command("statusline")}
        p.write_text(json.dumps(s))
        m.write_text(json.dumps({"path": str(p), "original": json.dumps({"hooks": {"Stop": [USER_HOOK]},
                                                                         "statusLine": {"command": "mine"}})}))
        old = m.read_bytes()
        man, notes = settings.load(root, root)
        self.assertEqual(m.read_bytes(), old)
        self.assertEqual((man["created"], man["hooks_created"], man["hooks"]["Stop"]["created"]), (False, False, False))
        self.assertTrue(man["hooks"]["PreToolUse"]["created"])
        self.assertEqual(man["hooks"]["Stop"]["groups"], claude.settings()["hooks"]["Stop"])
        self.assertEqual(man["statusLine"], s["statusLine"])
        self.assertIn("'mine'", notes[0])
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn("needs foremind upgrade", out)
        self.assertEqual(m.read_bytes(), old)
        out = self.upgrade(root)
        self.assertEqual(out.count("'mine'"), 1)  # said once, when the manifest is rewritten
        self.assertIs(json.loads(m.read_text())["format_version"], 1)
        self.assertEqual(settings.check(root), [])
        stop = json.loads(p.read_text())["hooks"]["Stop"]
        self.assertEqual(stop, [USER_HOOK, *claude.settings()["hooks"]["Stop"]])  # not doubled
        self.init("--root", str(root))  # a reinstall goes through again

    def test_illegal_and_future_versions_stop_init_and_uninstall_before_any_write(self):
        root, api, app = self.three()
        m = self.manifest(root, app)  # the last directory's
        for v, kind in ((True, "is not a format version"), (False, "is not a format version"),
                        ("1", "is not a format version"), (1.0, "is not a format version"),
                        (0, "is not a format version"), (-1, "is not a format version"),
                        (None, "is not a format version"), ([1], "is not a format version"), (2, "is newer")):
            self.edit(m, format_version=v)
            before = self.snapshot()
            for cmd in ("init", "uninstall"):
                rc, out = (self.env.run("init", "--yes", "--root", str(root)) if cmd == "init"
                           else self.env.run("uninstall", project=root))
                self.assertEqual(rc, 1, (v, cmd, out))
                self.assertIn(kind, out)
                self.assertIn("nothing was", out)
                self.unchanged(before)
            rc, out = self.env.run("doctor", project=root)
            line = next(x for x in out.splitlines() if x.startswith(f"FAIL install manifest {m.name}"))
            self.assertIn(kind, line, v)
            fix = out.split(line, 1)[1].splitlines()[1]
            if v == 2:  # told, never rewritten as an older one nor reinstalled over
                self.assertIn("use the Foremind that wrote it, or upgrade this one", fix)
                self.assertNotIn("init", fix)
            self.unchanged(before)

    def test_the_last_of_several_manifests_broken_stops_init_before_any_write(self):  # L385
        root, api, app = self.three()
        # the others are current (of format 0 they would need foremind upgrade first, m3a.8): a rerun goes through them
        (root / ".foremind" / "delivery.proposal.json").unlink()
        self.manifest(root, app).write_text("{")
        before = self.snapshot()
        rc, out = self.env.run("init", "--yes", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn("broken install manifest", out)
        self.assertIn(str(settings.path(app)), out)
        self.assertNotIn("已写入的部分保留着", out)
        self.unchanged(before)

    def test_a_manifest_of_a_directory_no_longer_registered_counts(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        orphan = root / ".foremind" / "install" / "settings-000000000000.json"
        orphan.write_text('{"path": 5}')
        before = self.snapshot()
        rc, out = self.env.run("init", "--yes", "--root", str(root))
        self.assertEqual(rc, 1, out)
        self.assertIn(f"{orphan}: broken install manifest (unknown shape)", out)
        self.unchanged(before)
        rc, out = self.env.run("uninstall", project=root)  # a broken one: left as it is, said
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{orphan}: broken install manifest (unknown shape): left as it is", out)
        self.assertTrue(orphan.exists())
        self.assertFalse(settings.path(root).exists())

    def test_an_entry_changed_or_removed_since_is_a_conflict(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        p, m = settings.path(root), self.manifest(root, root)
        installed = json.loads(p.read_text())
        changes = {"hooks.Stop": lambda s: s["hooks"]["Stop"][0]["hooks"][0].update(timeout=9),
                   "hooks.Notification": lambda s: s["hooks"].pop("Notification"),
                   "statusLine": lambda s: s["statusLine"].update(padding=3)}
        for what, change in changes.items():
            s = json.loads(json.dumps(installed))
            change(s)
            p.write_text(json.dumps(s))
            before = self.snapshot()
            rc, out = self.env.run("init", "--yes", "--root", str(root))
            self.assertEqual(rc, 1, out)
            for w in (str(p), str(m), what, "neither takes them over nor adds a second set"):
                self.assertIn(w, out)
            self.unchanged(before)
            rc, out = self.env.run("doctor", project=root)
            self.assertIn(f"FAIL install record matches {p}：{what}", out)
            self.unchanged(before)
        p.write_text(json.dumps(installed))  # put back as recorded: a reinstall goes through
        self.init("--root", str(root))

    def test_reruns_add_nothing_twice(self):
        root = self.env.repo(self.env.tmp / "shop")
        p = settings.path(root)
        p.parent.mkdir()
        p.write_text(json.dumps({"env": {"A": "1"}, "hooks": {"Stop": [USER_HOOK]}}))
        self.init("--root", str(root))
        once = p.read_bytes()
        self.init("--root", str(root))
        self.init("--root", str(root))
        self.assertEqual(p.read_bytes(), once)
        self.assertEqual(settings.check(root), [])
        self.assertEqual(json.loads(p.read_text())["hooks"]["Stop"][0], USER_HOOK)

    def test_uninstall_without_a_manifest_guesses_nothing(self):
        root = self.env.repo(self.env.tmp / "shop")
        self.init("--root", str(root))
        self.manifest(root, root).unlink()
        installed = settings.path(root).read_bytes()
        rc, out = self.env.run("uninstall", project=root)
        self.assertEqual(rc, 0, out)
        self.assertEqual(settings.path(root).read_bytes(), installed)
        self.assertIn("what was left in place is listed below", out)  # L383: not "removed everything"
        self.assertIn("看起来属于本程序的条目请自己删除", out)

    def test_from_a_source_checkout_to_this_install(self):
        """Hooks installed from another copy of the package (a source checkout, say) under a v0 manifest: doctor names
        both places although both work; upgraded() moves only the recorded entries, in memory; `foremind upgrade` does the
        same on disk (init is refused until then, m3a.8); the other copy stays."""
        root = self.env.repo(self.env.tmp / "shop")
        (root / "foremind.toml").write_text('[gate]\nchecks = ["make test"]\n')
        self.init("--root", str(root))
        src = self.env.tmp / "src"
        shutil.copytree(Path(foremind.__file__).parent, src / "foremind", ignore=shutil.ignore_patterns("__pycache__"))
        cur = claude.foremind_command("x").split(" ", 1)[0]  # PYTHONPATH=<this package's parent>
        p, m = settings.path(root), self.manifest(root, root)
        for f in (p, m):
            f.write_text(f.read_text().replace(cur, f"PYTHONPATH={src}"))
        self.edit(m, format_version=...)
        s = json.loads(p.read_text())
        s["hooks"]["Stop"].insert(0, USER_HOOK)
        p.write_text(json.dumps(s, indent=1))
        old = {f: f.read_bytes() for f in (p, m)}

        rc, out = self.env.run("doctor", project=root)
        self.assertEqual(rc, 1, out)
        line = next(x for x in out.splitlines() if x.startswith(f"FAIL hooks in {p} run this Foremind"))
        py, pkg, pp = settings.expected()
        self.assertEqual((py, pp), (sys.executable, str(Path(foremind.__file__).parents[1])))
        for w in (f"at {src / 'foremind'} with {sys.executable}, PYTHONPATH {src}",
                  f"this CLI: {py}, PYTHONPATH {pp}, package {pkg}"):
            self.assertIn(w, line)
        self.assertIn(claude.foremind_command("upgrade", "--check"), out.split(line, 1)[1].splitlines()[1])
        self.assertNotIn("FAIL 钩子已装", out)
        self.assertNotIn("FAIL install", out)

        man_text, text, notes = settings.upgraded(root, root)
        self.assertEqual({f: f.read_bytes() for f in (p, m)}, old)  # in memory only
        self.assertEqual(notes, [])
        new = json.loads(text)
        self.assertEqual(new["hooks"]["Stop"], [USER_HOOK, *claude.settings()["hooks"]["Stop"]])
        self.assertEqual(new["statusLine"]["command"], claude.foremind_command("statusline"))
        self.assertNotIn(str(src), text + man_text)
        self.assertIs(json.loads(man_text)["format_version"], 1)

        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", "--root", str(root))
        self.assertEqual((rc, {f: f.read_bytes() for f in (p, m)}), (1, old), out)
        self.upgrade(root)
        self.assertEqual(json.loads(p.read_text()), new)
        self.assertEqual(m.read_text(), man_text)
        self.assertEqual(settings.locate(root), [])
        self.assertTrue((src / "foremind" / "__init__.py").exists())
        self.assertEqual(self.env.run("doctor", project=root)[0], 0)
        self.assertIsNone(settings.upgraded(self.env.tmp, root))  # no manifest: nothing to move

    def test_target_reads_only_the_shapes_foremind_writes(self):
        for cmd, want in ((claude.hook_command("Stop"), (sys.executable, str(Path(foremind.__file__).parents[1]), True)),
                          ("/old/python -m foremind hook Stop", ("/old/python", None, False)),
                          ("PYTHONPATH=/a /p -P -m foremind statusline", ("/p", "/a", True))):
            self.assertEqual(settings.target(cmd), want, cmd)
        for cmd in ("touch x; /p -m foremind hook Stop", "/p -c 'x' -m foremind hook Stop", "/p -m foremind version",
                    "A=1 /p -m foremind hook Stop", "'unclosed -m foremind statusline"):
            self.assertIsNone(settings.target(cmd), cmd)


if __name__ == "__main__":
    unittest.main()
