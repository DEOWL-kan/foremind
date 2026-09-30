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
        self.assertEqual(hooks["Notification"][0]["matcher"], "")  # m2b.8: every type; the hook picks the waiting ones

    def test_settings_allowlist(self):  # m2a.1 item 1: read-only and test commands, never push, install or rm
        allow = json.loads(Path(self.make().argv[2]).read_text())["permissions"]["allow"]
        for rule in ("Read", "Grep", "Glob", "Bash(git status:*)", "Bash(git diff:*)", "Bash(python3 -m unittest:*)",
                     "Bash(foremind log:*)", "Bash(foremind decide:*)"):
            self.assertIn(rule, allow)
        for bad in ("push", "install", "rm", "pip", "npm", "brew", "find", "rg"):  # find -delete, rg --pre (r1)
            self.assertFalse([r for r in allow if bad in r.replace("(", " ").replace(":", " ").split()], bad)
        self.assertFalse([r for r in allow if r.startswith("Bash(git branch")])  # branch -D (r1)
        self.assertIn("Bash(git commit:*)", allow)
        for c in ("log", "handoff", "review", "decide", "status"):  # REQ-12: also as the program spells them
            self.assertIn(f"Bash({claude.foremind_command(c)}:*)", allow)
        self.assertIn("StopFailure", json.loads(Path(self.make().argv[2]).read_text())["hooks"])  # REQ-11
        # the planner commits nothing (m2b.9 r1, Q-21): the rest of the list is the seat's
        planner = json.loads(Path(self.make(role="planner").argv[2]).read_text())["permissions"]["allow"]
        self.assertEqual([r for r in allow if r not in planner], ["Bash(git add:*)", "Bash(git commit:*)"])

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
