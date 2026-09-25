import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from foremind import __version__, vendors
from foremind.vendors import claude


class ClaudeLaunchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def make(self, **kw):
        base = dict(session="fm-proj-p_1-1", batch="p.1", role="seat", project_root=self.tmp / "proj",
                    settings_path=self.tmp / "wt" / "fm-proj-p_1-1.settings.json", cwd=self.tmp / "wt" / "api",
                    permission_mode="acceptEdits")
        return claude.launch(**{**base, **kw})

    def test_single_repo(self):
        spec = self.make(model="claude-opus-5-5", effort="high")
        self.assertEqual(spec.argv[:5], ["claude", "--settings", str(self.tmp / "wt" / "fm-proj-p_1-1.settings.json"),
                                         "--permission-mode", "acceptEdits"])
        self.assertNotIn("--bare", spec.argv)
        self.assertNotIn("--add-dir", spec.argv)
        self.assertEqual(spec.argv[5:], ["--model", "claude-opus-5-5", "--effort", "high"])
        self.assertEqual(spec.env, {"FOREMIND_SESSION": "fm-proj-p_1-1", "FOREMIND_BATCH": "p.1",
                                    "FOREMIND_ROLE": "seat", "FOREMIND_PROJECT": str(self.tmp / "proj")})
        self.assertEqual(spec.cwd, self.tmp / "wt" / "api")

    def test_multi_repo(self):
        dirs = [self.tmp / "wt" / "api", self.tmp / "wt" / "app"]
        spec = self.make(cwd=self.tmp / "wt", add_dirs=dirs, multi_repo=True)
        self.assertNotIn("--bare", spec.argv)
        i = spec.argv.index("--add-dir")
        self.assertEqual(spec.argv[i:i + 4], ["--add-dir", str(dirs[0]), "--add-dir", str(dirs[1])])
        self.assertEqual(spec.env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")
        self.assertIn("FOREMIND_SESSION=fm-proj-p_1-1", spec.shell())
        self.assertTrue(spec.shell().startswith(f"cd {self.tmp / 'wt'} && env "))

    def test_settings_file_hooks(self):
        spec = self.make()
        hooks = json.loads(Path(spec.argv[2]).read_text())["hooks"]
        self.assertEqual(sorted(hooks), sorted(claude.HOOK_EVENTS))
        for event, entries in hooks.items():
            cmd = entries[0]["hooks"][0]
            self.assertEqual(cmd["type"], "command")
            self.assertTrue(cmd["command"].endswith(f"-m foremind hook {event}"), cmd["command"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "*")

    def test_hook_command_finds_the_package_from_any_cwd(self):
        # same command line with `version` instead of `hook <event>` (hooks themselves are M1-5)
        cmd = claude.hook_command("SessionStart").replace("hook SessionStart", "version")
        out = subprocess.run(cmd, shell=True, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), __version__, out.stderr)

    def test_foremind_command(self):
        self.assertEqual(claude.hook_command("Stop"), claude.foremind_command("hook", "Stop"))
        cmd = claude.foremind_command("version")
        self.assertTrue(cmd.startswith("PYTHONPATH="), cmd)
        out = subprocess.run(cmd, shell=True, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), __version__, out.stderr)

    def test_a_foremind_package_in_the_cwd_does_not_shadow_ours(self):
        (self.tmp / "foremind").mkdir()
        (self.tmp / "foremind" / "__init__.py").write_text("raise ImportError('shadowed')\n")
        out = subprocess.run(claude.foremind_command("version"), shell=True, cwd=self.tmp, capture_output=True,
                             text=True)
        self.assertEqual(out.stdout.strip(), __version__, out.stderr)

    def test_codex_not_yet(self):
        with self.assertRaises(NotImplementedError):
            vendors.get("codex")
        self.assertIs(vendors.get("claude"), claude)


if __name__ == "__main__":
    unittest.main()
