import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import __version__, batchlog, commands
from foremind.cli import main

REPO = Path(__file__).resolve().parent.parent


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(argv)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_version(self):
        self.assertEqual(run(["version"]), (0, __version__ + "\n", ""))
        out = subprocess.run([sys.executable, "-m", "foremind", "version"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), __version__)

    def test_help_hides_internal_commands(self):
        code, out, _ = run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("version", out)
        self.assertIn("log", out)
        self.assertNotIn("_job", out)

    def test_discovers_new_command_modules(self):
        (self.tmp / "zz_probe.py").write_text(
            "def register(sub):\n    sub.add_parser('zz-probe').set_defaults(func=lambda a: print('probed'))\n")
        (self.tmp / "_private.py").write_text("raise RuntimeError('must not be imported')\n")
        self.addCleanup(sys.modules.pop, "foremind.commands.zz_probe", None)
        with mock.patch.object(commands, "__path__", [*commands.__path__, str(self.tmp)]):
            self.assertEqual(run(["zz-probe"]), (0, "probed\n", ""))

    def test_log(self):
        (self.tmp / ".foremind").mkdir()
        note = self.tmp / "note.md"
        note.write_text("D1: 记录\n")
        with mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.tmp)}):
            code, out, _ = run(["log", "M1.1", "--author", "seat-1", "--file", str(note)])
            self.assertEqual(code, 0)
            self.assertEqual(len(out.strip()), 64)
            self.assertTrue(batchlog.verify(self.tmp, "M1.1"))
            with mock.patch("sys.stdin", io.StringIO("from stdin")):
                self.assertEqual(run(["log", "M1.1"])[0], 0)
            self.assertIn("from stdin", (self.tmp / ".foremind" / "batches" / "M1.1.log.md").read_text())
            code, _, err = run(["log", "../evil", "--file", str(note)])
            self.assertEqual(code, 1)
            self.assertIn("bad batch id", err)


if __name__ == "__main__":
    unittest.main()
