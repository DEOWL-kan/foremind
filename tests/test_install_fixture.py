"""Shared fixture for the install / doctor / status tests (no tests here).

A temp HOME, FOREMIND_CONFIG_HOME and FOREMIND_WT_ROOT; PATH = fake gh, claude, tmux first, then only git's own
directory and /bin. Fake gh answers from $FAKE_GH_STATE: {"auth": exit code of `gh auth status`,
"api": {url: {"out": json} | {"err": text}}}; an unknown url fails with HTTP 404.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from foremind.cli import main

FAKE_GH = r'''
import json, os, sys
a = sys.argv[1:]
st = json.load(open(os.environ["FAKE_GH_STATE"])) if os.path.exists(os.environ["FAKE_GH_STATE"]) else {}
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
if a[:2] == ["auth", "status"]:
    sys.exit(st.get("auth", 0))
if a[:1] == ["api"]:
    r = st.get("api", {}).get(a[1])
    if r is None or "err" in r:
        sys.exit("gh: " + (r or {}).get("err", "Not Found (HTTP 404)"))
    print(json.dumps(r["out"]))
    sys.exit(0)
sys.exit("fake gh: unsupported " + " ".join(a))
'''


def sh(cwd, *args, env=None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


class Env:
    def __init__(self, case):
        self.case = case
        self.tmp = Path(case.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.config = self.tmp / "config"
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(f"#!{sys.executable}\n{FAKE_GH}")
        for name in ("claude", "tmux"):
            (bin_ / name).write_text("#!/bin/sh\nexit 0\n")
        for p in bin_.iterdir():
            p.chmod(0o755)
        (self.tmp / "gitconfig").write_text("")
        env = {"HOME": str(self.home), "FOREMIND_CONFIG_HOME": str(self.config),
               "FOREMIND_WT_ROOT": str(self.tmp / "wt"),
               "PATH": os.pathsep.join([str(bin_), str(Path(shutil.which("git")).parent), "/bin"]),
               "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
               "FAKE_GH_STATE": str(self.tmp / "gh.json"), "FAKE_GH_LOG": str(self.tmp / "gh.log")}
        case.enterContext(mock.patch.dict(os.environ, env))
        for k in ("FOREMIND_SESSION", "FOREMIND_ROLE", "FOREMIND_BATCH", "FOREMIND_PROJECT", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(k, None)

    def gh(self, **state):
        (self.tmp / "gh.json").write_text(json.dumps(state))

    def repo(self, path, *, commit=True) -> Path:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        sh(path, "git", "init", "-q", "-b", "main")
        if commit:
            (path / "README.md").write_text("x\n")
            sh(path, "git", "add", "-A")
            sh(path, "git", "commit", "-q", "-m", "init")
        return path

    def run(self, *argv, project=None) -> tuple[int, str]:
        """`foremind <argv>`; (exit code, stdout + stderr)."""
        out = io.StringIO()
        env = {"FOREMIND_PROJECT": str(project)} if project else {}
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                rc = main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue()
