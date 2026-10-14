"""m3a REQ-2 (and REQ-12's commands): the wheel, installed offline into a clean venv outside the source tree, is what
runs, and works there on its own: CLI, init, doctor, the installed hooks (a same-name package in their cwd does not
stand in for it), every role's full prompt, a same-version reinstall, a move from the source or another install, and
two shapes of v0 project upgraded, run and uninstalled.

Building keeps the real HOME and UV_CACHE_DIR (test_packaging.build_env); installing and running isolate HOME,
FOREMIND_CONFIG_HOME, CLAUDE_CONFIG_DIR, FOREMIND_WT_ROOT and git's identity in a temp dir and drop every FOREMIND_*
mark and PYTHONPATH. claude, tmux and gh are fakes: no model is called. Only a missing tool (no offline backend, no way
to install into the venv) skips, and FOREMIND_REQUIRE_BUILD=1 makes that a failure; anything after is a failure.
"""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import foremind
from test_packaging import PRIVATE, REQUIRE, ROOT, BuildFailed, ToolMissing, build, build_env, preflight, source_copy

SKIPPED = "foremind: skipped command module"  # foremind.cli.SKIPPED, as the installed CLI prints it
USER_HOOK = {"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}
USER_SETTINGS = {"hooks": {"Stop": [USER_HOOK]}, "env": {"TOKEN": "s3cret"}}
USER_CONFIG = "# my own notes: Foremind leaves this file alone\n"
_T = 300

FAKE_CLAUDE = r'''
import json, os, re, subprocess, sys, uuid
a = sys.argv[1:]
rec = {"argv": a, "cwd": os.getcwd(), "role": os.environ.get("FOREMIND_ROLE"),
       "session": os.environ.get("FOREMIND_SESSION")}
if "--" in a and (m := re.search(r"Read (\S+) and follow it", a[-1])):
    rec["prompt"] = open(os.path.join(os.getcwd(), m[1])).read()
if "--settings" in a:  # an interactive session: Claude Code runs its SessionStart hooks
    st = json.load(open(a[a.index("--settings") + 1]))
    for g in st["hooks"]["SessionStart"]:
        for h in g["hooks"]:
            r = subprocess.run(["/bin/sh", "-c", h["command"]], capture_output=True, text=True, input=json.dumps(
                {"session_id": str(uuid.uuid4()), "cwd": os.getcwd(), "hook_event_name": "SessionStart",
                 "source": "startup"}))
            rec.setdefault("hooks", []).append({"command": h["command"], "rc": r.returncode, "err": r.stderr})
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\n")
if "-p" in a:
    out = {"verdict": "approved", "issues": []} if rec["role"] == "reviewer" else {}
    print(json.dumps({"type": "result", "is_error": False, "result": json.dumps(out),
                      "session_id": a[a.index("--session-id") + 1] if "--session-id" in a else ""}))
'''

# runs in the installed venv (python -P, cwd = the project): each role's material entry, launches on the fake claude
DRIVER = r'''
import json, subprocess, sys, time, os
from pathlib import Path
import foremind
from foremind import config, controller, job, oneshot, planner
from foremind.carriers import Carrier, SessionState
from foremind.decide import pending
from foremind.supervisor import tick
from foremind.supervisor.phases import audit as audit_phase, decide as decide_phase

root = Path(sys.argv[1]).resolve()
cfg = config.load(root)
out = {"file": foremind.__file__, "templates": {n: str(foremind.template_path(n)) for n in foremind.TEMPLATES}}


class Fake(Carrier):  # the carrier's part only: the session is the fake claude, its hooks the installed ones
    name = "fake"
    sent = {}

    def create(self, session, launch):
        subprocess.run(launch.argv, env={**os.environ, **launch.env}, cwd=launch.cwd, check=True)

    def send(self, session, text):
        self.sent[session] = text
        return True

    def read_state(self, session):
        return SessionState(alive=False, idle=None)

    def close(self, session):
        return None

    def list_sessions(self):
        return []


p = planner.open_planner(root, "Add a greeting file", carrier=Fake(root), tier="M", config=cfg)
out["planner"] = Fake.sent[p["session"]]
rec = lambda cat: pending.create(root, cfg, question="Go on?", options=["yes", "no"], recommended=1, reason="r",
                                 category=cat, blocks=[sys.argv[2]], reversible=True,
                                 request={"kind": "manual", "category": cat, "batch": sys.argv[2]})[0]
t = tick.Tick(root, cfg, time.time())
t.load()
q1, q2 = rec(3), rec(4)
jd = root / ".foremind" / "jobs"
before = set(os.listdir(jd))
out["started"] = [decide_phase.start(t, q1, pending.request(root, q1["id"]), "decider"),
        decide_phase.start(t, q2, pending.request(root, q2["id"]), "controller"),
        audit_phase.start(t, "decider", q1["id"], f"smoke:{q1['id']}",
                          {"question": q1["id"], "category": 3, "conclusion": 1, "confidence": "high"})]
jobs = sorted(set(os.listdir(jd)) - before)
for _ in range(600):
    if all(job.status(jd, j)["state"] == "done" for j in jobs):
        break
    time.sleep(0.1)
out["jobs"] = [job.status(jd, j).get("exit_code") for j in jobs]
session, argv, env = oneshot.prepare(root, "cataloger", cfg, {"request.json": {"kind": "smoke"}})
out["cataloger"] = (root / ".foremind" / "oneshots" / session / "prompt.md").read_text(encoding="utf-8")
out["controller_template"] = controller._template()
print(json.dumps(out))
'''

SHADOW = "import os\nopen(os.environ['SHADOW_MARK'], 'a').write(__name__ + '\\n')\n"


def _sh(cwd, *argv, env=None) -> str:
    return subprocess.run(argv, cwd=cwd, env=env, check=True, capture_output=True, text=True, timeout=_T).stdout


class Smoke(unittest.TestCase):
    """setUpClass builds once and makes venv A; each test gets its own isolated user layer and projects."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="foremind-smoke-")).resolve()
        try:
            cls.sdist, cls.wheel = build(cls.tmp / "dist")
            cls.tool = preflight(cls.tmp / "pre")
            cls.venv_a = cls.venv(cls.tmp / "venv-a")
            cls.install(cls.venv_a, cls.wheel)
        except ToolMissing as e:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            if REQUIRE:
                raise AssertionError(f"FOREMIND_REQUIRE_BUILD=1 but a tool is missing: {e}") from None
            raise unittest.SkipTest(f"no offline build or install tool: {e}") from None
        except BaseException:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- tools -------------------------------------------------------------------------------------------------

    @classmethod
    def install_env(cls) -> dict:
        """Installing is isolated as running is: a temp HOME and XDG dirs, no pip.conf / uv.toml of the user's, no
        inherited PIP_*, UV_*, git, Claude or Foremind variables; offline."""
        home = cls.tmp / "install-home"
        home.mkdir(exist_ok=True)
        env = {k: v for k, v in build_env().items() if not k.startswith(("FOREMIND_", "GIT_", "CLAUDE", "PIP_", "UV_",
                                                                          "XDG_")) and k not in ("HOME", "VIRTUAL_ENV")}
        return {**env, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
                "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "UV_NO_CONFIG": "1", "UV_OFFLINE": "1"}

    @classmethod
    def venv(cls, d) -> Path:
        """A venv of this interpreter, no system site-packages; without ensurepip, a bare one for uv to fill. Only a
        missing ensurepip (it does not import) is a missing tool; one that is there and fails is a failure."""
        has = subprocess.run([sys.executable, "-c", "import ensurepip"], capture_output=True, env=cls.install_env(),
                             timeout=_T).returncode == 0
        if not has and not shutil.which("uv"):
            raise ToolMissing("no ensurepip for the venv and no uv to install into it")
        _sh(None, sys.executable, "-m", "venv", *([] if has else ["--without-pip"]), str(d), env=cls.install_env())
        assert "include-system-site-packages = false" in (d / "pyvenv.cfg").read_text()
        return d

    @classmethod
    def install(cls, venv, wheel, *flags):
        py = str(venv / "bin" / "python")
        if (venv / "bin" / "pip").exists():
            argv = [py, "-m", "pip", "install", "--no-index", "--no-deps", *flags, str(wheel)]
        else:  # the uv way: --reinstall is pip's --force-reinstall
            argv = ["uv", "pip", "install", "--offline", "--no-deps", "--python", py,
                    *["--reinstall" for f in flags if f == "--force-reinstall"], str(wheel)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=_T, env=cls.install_env(), cwd=cls.tmp)
        if r.returncode:
            raise BuildFailed(f"{' '.join(argv)}:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
        return r.stdout + r.stderr

    def setUp(self):
        self.t = Path(tempfile.mkdtemp(prefix="t-", dir=self.tmp))
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)
        bin_ = self.t / "bin"
        bin_.mkdir()
        (bin_ / "claude").write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
        (bin_ / "tmux").write_text("#!/bin/sh\nexit 0\n")
        (bin_ / "gh").write_text("#!/bin/sh\nexit 1\n")
        for p in bin_.iterdir():
            p.chmod(0o755)
        for d in ("home", "config", "claude-home", "wt"):
            (self.t / d).mkdir()
        (self.t / "gitconfig").write_text("[user]\n\tname = Smoke\n\temail = smoke@example.test\n")
        (self.t / "config" / "config.toml").write_text(USER_CONFIG)
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("FOREMIND_", "GIT_", "CLAUDE"))
                and k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "HOME", "PATH")}
        self.env = {**keep, "HOME": str(self.t / "home"), "FOREMIND_CONFIG_HOME": str(self.t / "config"),
                    "CLAUDE_CONFIG_DIR": str(self.t / "claude-home"), "FOREMIND_WT_ROOT": str(self.t / "wt"),
                    "GIT_CONFIG_GLOBAL": str(self.t / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
                    "FAKE_CLAUDE_LOG": str(self.t / "claude.log"),
                    "PATH": os.pathsep.join([str(bin_), str(Path(shutil.which("git")).parent), "/usr/bin", "/bin"])}

    def run_(self, argv, cwd, env=None, check=None) -> tuple[int, str]:
        r = subprocess.run(argv, cwd=cwd, env={**self.env, **(env or {})}, capture_output=True, text=True,
                           timeout=_T, stdin=subprocess.DEVNULL)
        out = r.stdout + r.stderr
        self.assertNotIn(SKIPPED, out, argv)
        if check is not None:
            self.assertEqual(r.returncode, check, f"{argv}\n{out}")
        return r.returncode, out

    def fm(self, venv, *argv, cwd, check=0, env=None) -> str:
        """The installed console script, as a user runs it."""
        return self.run_([str(venv / "bin" / "foremind"), *argv], cwd, env, check)[1]

    def project(self, name, venv=None, *, cli=None) -> Path:
        """A git repo with the user's own settings and [gate].checks (a new project has none: the fixture's, not a
        product default), set up by `foremind init` of `venv` (or of the argv `cli`)."""
        root = self.t / name
        root.mkdir()
        _sh(root, "git", "init", "-q", "-b", "main", env=self.env)
        (root / "README.md").write_text("x\n")
        _sh(root, "git", "add", "-A", env=self.env)
        _sh(root, "git", "commit", "-q", "-m", "init", env=self.env)
        (root / ".claude").mkdir()
        (root / ".claude" / "settings.local.json").write_text(json.dumps(USER_SETTINGS))
        init = ["init", "--yes", "--carrier", "tmux", "--root", str(root)]
        if cli:
            self.run_([*cli, *init], root, check=0)
        else:
            self.fm(venv, *init, cwd=root)
        # no remote to read a default branch from: the fixture names the target branch too
        (root / "foremind.toml").write_text('[gate]\nchecks = ["true"]\n\n[delivery.repo.main]\ntarget_branch = "main"\n')
        return root

    def settings(self, root) -> dict:
        return json.loads((root / ".claude" / "settings.local.json").read_text())

    def hooks_python(self, root) -> set:
        return {h["command"].split()[1] for gs in self.settings(root)["hooks"].values() for g in gs
                for h in g["hooks"] if "-m foremind hook" in h["command"]}

    def site(self, venv) -> Path:
        return Path(self.run_([str(venv / "bin" / "python"), "-P", "-c", "import foremind, os; "
                               "print(os.path.dirname(os.path.dirname(foremind.__file__)))"], self.t, check=0)[1]
                    .strip()).resolve()

    # --- the artifacts -----------------------------------------------------------------------------------------

    def test_sdist_members_and_a_wheel_rebuilt_from_it(self):
        with tarfile.open(self.sdist, "r:gz") as t:  # no extractall(filter=): 3.11's first releases lack it
            top = self.sdist.name[:-len(".tar.gz")]
            members = t.getmembers()
            src = self.t / "from-sdist"
            for m in members:
                rel = Path(m.name)
                self.assertFalse(rel.is_absolute() or ".." in rel.parts or rel.parts[0] != top, m.name)
                self.assertTrue(m.isfile() or m.isdir(), f"{m.name}: neither file nor directory")
                if m.isfile():
                    dst = src / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(t.extractfile(m).read())
        names = {m.name[len(top) + 1:] for m in members if m.isfile()}
        for n in foremind.TEMPLATES:
            self.assertEqual((src / top / "foremind" / "templates" / n).read_bytes(),
                             (ROOT / "foremind" / "templates" / n).read_bytes(), n)
        self.assertEqual((src / top / "LICENSE").read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertIn(f"Version: {foremind.__version__}\n", (src / top / "PKG-INFO").read_text())
        self.assertFalse([n for n in names if any(n == p or n.startswith(p) for p in PRIVATE)])
        _, again = build(self.t / "rebuilt", src / top)
        read = lambda w: {n: z.read(n) for z in [zipfile.ZipFile(w)] for n in z.namelist() if not n.endswith("RECORD")}
        self.assertEqual(read(again), read(self.wheel), "the wheel from the sdist differs")
        self.assertEqual(again.name, self.wheel.name)

    # --- the installed wheel -----------------------------------------------------------------------------------

    def test_what_runs_is_the_installed_wheel(self):
        probe = ("import foremind, sys, json, importlib.metadata as m; print(json.dumps({'file': foremind.__file__, "
                 "'path': sys.path, 'version': m.version('foremind'), 'dist': str(m.distribution('foremind')"
                 ".locate_file('')), 'prefix': sys.prefix, 'base': sys.base_prefix}))")
        info = json.loads(self.run_([str(self.venv_a / "bin" / "python"), "-c", probe], self.t, check=0)[1])
        venv = self.venv_a.resolve()
        self.assertTrue(Path(info["file"]).resolve().is_relative_to(venv), info["file"])
        self.assertTrue(Path(info["dist"]).resolve().is_relative_to(venv))
        self.assertEqual(info["version"], foremind.__version__)
        self.assertNotEqual(info["prefix"], info["base"])
        self.assertFalse([p for p in info["path"] if p and Path(p).resolve().is_relative_to(ROOT)], info["path"])
        self.assertEqual(self.fm(self.venv_a, "version", cwd=self.t).strip(), foremind.__version__)
        self.assertIn("upgrade", self.fm(self.venv_a, "--help", cwd=self.t))

    def test_init_doctor_and_the_installed_hooks_beside_a_same_name_package(self):
        root = self.project("shop", self.venv_a)
        out = self.fm(self.venv_a, "doctor", cwd=root)
        self.assertIn("全部通过", out)
        self.assertIn("packaged templates", out)
        site = self.site(self.venv_a)
        cmds = [h["command"] for gs in self.settings(root)["hooks"].values() for g in gs for h in g["hooks"]
                if "-m foremind hook" in h["command"]]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn(f" -P -m foremind hook ", c)
            self.assertIn(str(site), c)
            self.assertNotIn(str(ROOT), c)
        self.assertEqual(self.settings(root)["hooks"]["Stop"][0], USER_HOOK)
        shadow = root / "work"  # a checkout of foremind itself, say
        (shadow / "foremind").mkdir(parents=True)
        for n in ("__init__.py", "__main__.py"):
            (shadow / "foremind" / n).write_text(SHADOW)
        mark = self.t / "shadow-ran"
        pre = next(c for c in cmds if c.endswith("hook PreToolUse"))
        seat = {"FOREMIND_SESSION": "fm-shop-x_1-1", "FOREMIND_ROLE": "seat", "FOREMIND_PROJECT": str(root),
                "SHADOW_MARK": str(mark)}
        data = json.dumps({"session_id": "s", "cwd": str(shadow), "hook_event_name": "PreToolUse", "tool_name": "Edit",
                           "tool_input": {"file_path": str(root / ".foremind" / "events.jsonl")}, "tool_use_id": "t"})
        r = subprocess.run(["/bin/sh", "-c", pre], cwd=shadow, input=data, capture_output=True, text=True, timeout=_T,
                           env={**self.env, **seat})
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", r.stderr)
        self.assertFalse(mark.exists(), "the same-name package ran")
        r = subprocess.run(["/bin/sh", "-c", pre.replace(" -P ", " ")], cwd=shadow, input=data, capture_output=True,
                           text=True, timeout=_T, env={**self.env, **seat})
        self.assertTrue(mark.exists(), "control: without -P the same-name package does stand in")

    def test_every_role_gets_its_full_packaged_prompt(self):
        root = self.project("shop", self.venv_a)
        out = self.fm(self.venv_a, "do", "Add a greeting", "--owns", "main:hello.txt", "--accept", "true", "--mode",
                      "user", cwd=root)
        bid = next(w for w in out.split() if w.startswith("do-") and w.endswith(".1"))
        self.fm(self.venv_a, "tick", cwd=root)  # planned -> ready; mode user: it opens no seat
        wt = Path(self.fm(self.venv_a, "seat", bid, "--user", cwd=root).split()[-1])
        (wt / "hello.txt").write_text("hi\n")
        _sh(wt, "git", "add", "-A", env=self.env)
        _sh(wt, "git", "commit", "-q", "-m", "hello", env=self.env)
        self.fm(self.venv_a, "review", bid, cwd=root)
        self.fm(self.venv_a, "review", bid, "--run-reviewer", cwd=root)
        driver = self.t / "driver.py"
        driver.write_text(DRIVER)
        got = json.loads(self.run_([str(self.venv_a / "bin" / "python"), "-P", str(driver), str(root), bid], root,
                                   check=0)[1])
        venv = self.venv_a.resolve()
        self.assertTrue(Path(got["file"]).resolve().is_relative_to(venv))
        wheel = zipfile.ZipFile(self.wheel)
        card = lambda n: wheel.read(f"foremind/templates/{n}").decode("utf-8")
        for n, p in got["templates"].items():
            self.assertTrue(Path(p).resolve().is_relative_to(venv), p)
        self.assertIn(card("roles/planner.md").strip(), got["planner"])
        self.assertIn(card("handoff.md").strip(), got["planner"])
        log = [json.loads(x) for x in (self.t / "claude.log").read_text().splitlines()]
        planner = next(x for x in log if x["role"] == "planner")
        self.assertTrue(planner["hooks"] and all(h["rc"] == 0 and " -P " in h["command"] for h in planner["hooks"]),
                        planner)
        prompts = {x["role"]: x["prompt"] for x in log if "prompt" in x}
        self.assertEqual(sorted(prompts), ["auditor", "controller", "decider", "reviewer"])
        for role, text in prompts.items():
            self.assertTrue(text.startswith(card(f"roles/{role}.md")), role)
        self.assertEqual((got["started"], got["jobs"]), ([True] * 3, [0] * 3))
        self.assertTrue(got["cataloger"].startswith(card("roles/cataloger.md")))
        self.assertEqual(Path(got["controller_template"]).read_text(encoding="utf-8"), card("controller-handoff.md"))
        self.assertTrue(Path(got["controller_template"]).resolve().is_relative_to(venv))

    # --- replacing an install ----------------------------------------------------------------------------------

    def test_a_same_version_wheel_replaces_only_with_force_reinstall(self):
        src = source_copy(self.t / "src2")
        card = src / "foremind" / "templates" / "roles" / "cataloger.md"
        card.write_text(card.read_text(encoding="utf-8") + "\n<!-- smoke rebuild -->\n", encoding="utf-8")
        _, wheel2 = build(self.t / "dist2", src)
        self.assertEqual(wheel2.name, self.wheel.name, "same version")
        v = self.venv(self.t / "venv-c")
        self.install(v, self.wheel)
        installed = self.site(v) / "foremind" / "templates" / "roles" / "cataloger.md"
        self.install(v, wheel2)  # the same version number: pip and uv keep what is there
        self.assertNotIn("smoke rebuild", installed.read_text(encoding="utf-8"))
        self.install(v, wheel2, "--force-reinstall")
        self.assertIn("smoke rebuild", installed.read_text(encoding="utf-8"))

    def test_from_the_source_and_between_two_installs_the_cli_run_is_the_target(self):
        self.env["PYTHONPATH"] = str(ROOT)
        root = self.project("shop", cli=[sys.executable, "-P", "-m", "foremind"])  # run from the source checkout
        del self.env["PYTHONPATH"]
        self.assertEqual(self.hooks_python(root), {sys.executable})
        rc, out = self.run_([str(self.venv_a / "bin" / "foremind"), "doctor"], root)
        self.assertEqual(rc, 1, out)
        self.assertIn(str(self.venv_a / "bin" / "python"), out)  # what it expects: the CLI run now
        self.assertIn("upgrade --check", out)
        self.assertIn("needs foremind upgrade", self.run_([str(self.venv_a / "bin" / "foremind"), "pause"], root,
                                                          check=1)[1])
        self.fm(self.venv_a, "upgrade", "--check", cwd=root)
        self.assertIn(": migrated", self.fm(self.venv_a, "upgrade", cwd=root))
        self.assertEqual(self.hooks_python(root), {str(self.venv_a / "bin" / "python")})
        self.assertIn("全部通过", self.fm(self.venv_a, "doctor", cwd=root))
        self.assertTrue((ROOT / "foremind" / "__init__.py").is_file(), "the source checkout stays")
        b = self.venv(self.t / "venv-b")
        self.install(b, self.wheel)
        rc, out = self.run_([str(b / "bin" / "foremind"), "doctor"], root)  # two valid installs
        self.assertEqual(rc, 1, out)
        self.assertIn(str(b / "bin" / "python"), out)
        self.assertIn(": migrated", self.fm(b, "upgrade", cwd=root))
        self.assertEqual(self.hooks_python(root), {str(b / "bin" / "python")})
        self.assertIn("全部通过", self.fm(b, "doctor", cwd=root))
        self.assertEqual(self.fm(self.venv_a, "version", cwd=self.t).strip(), foremind.__version__, "A stays")

    # --- v0 projects -------------------------------------------------------------------------------------------

    def v0(self, root, shape):
        """As an older Foremind left the project: its manifest without format_version (per value, or the settings'
        text before the install as `original`), the hooks run by another interpreter."""
        m = next((root / ".foremind" / "install").glob("settings-*.json"))
        sp = root / ".claude" / "settings.local.json"
        man = json.loads(m.read_text())
        old = "/old/bin/python3"
        py = str(self.venv_a / "bin" / "python")
        man = {"path": man["path"], "original": json.dumps(USER_SETTINGS)} if shape == "original" else \
            {k: v for k, v in man.items() if k != "format_version"}
        m.write_text(json.dumps(man).replace(json.dumps(py)[1:-1], old))
        sp.write_text(sp.read_text().replace(json.dumps(py)[1:-1], old))

    def test_two_v0_shapes_upgrade_keep_history_run_and_uninstall(self):
        for shape in ("per-value", "original"):
            with self.subTest(shape):
                self.upgrade_v0(shape)

    def upgrade_v0(self, shape):
        a = self.venv_a
        root = self.project(f"old-{shape}", a)
        sd = root / ".foremind"
        self.fm(a, "pause", cwd=root)
        self.fm(a, "resume", cwd=root)
        (sd / "batches").mkdir(exist_ok=True)
        (sd / "batches" / "b.1.log.md").write_text("### old\n\nkept\n")
        (sd / "config.toml").write_text((sd / "config.toml").read_text() + "# the user's project note\n")
        self.v0(root, shape)
        kept = {p: p.read_bytes() for p in (sd / "config.toml", sd / "batches" / "b.1.log.md", root / "foremind.toml",
                                            self.t / "config" / "config.toml")}
        events = (sd / "events.jsonl").read_bytes()
        self.assertIn("needs foremind upgrade", self.run_([str(a / "bin" / "foremind"), "pause"], root, check=1)[1])
        self.assertIn("only the user runs this", self.run_([str(a / "bin" / "foremind"), "upgrade"], root, check=1,
                                                           env={"FOREMIND_SESSION": "fm-x-b_1-1"})[1])
        # the stop contract: a supervise that runs blocks the check and the upgrade; nothing is written
        sv = subprocess.Popen(["/bin/sleep", "120"])
        self.addCleanup(lambda: sv.poll() is None and sv.kill())
        (sd / "supervisor.json").write_text(json.dumps(
            {"pid": sv.pid, "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}))
        before = self.files(root)
        out = self.run_([str(a / "bin" / "foremind"), "upgrade", "--check"], root, check=1)[1]
        self.assertIn(f"running: foremind supervise (pid {sv.pid})", out)
        self.assertIn("refused", self.run_([str(a / "bin" / "foremind"), "upgrade"], root, check=1)[1])
        self.assertEqual(self.files(root), before)
        sv.send_signal(signal.SIGTERM)
        sv.wait()
        self.fm(a, "upgrade", "--check", cwd=root)
        if shape == "per-value" and os.geteuid():  # interrupted at its first write; chmod does not stop root
            inst = sd / "install"
            mode = stat.S_IMODE(inst.stat().st_mode)
            inst.chmod(0o500)
            try:
                self.run_([str(a / "bin" / "foremind"), "upgrade"], root, check=1)
            finally:
                inst.chmod(mode)
            self.assertTrue((sd / "upgrade" / "unfinished.json").exists())
            self.assertIn("an upgrade is unfinished",
                          self.run_([str(a / "bin" / "foremind"), "pause"], root, check=1)[1])
            self.assertIn("to undo the upgrade by hand", self.run_([str(a / "bin" / "foremind"), "doctor"], root,
                                                                   check=1)[1])
        out = self.fm(a, "upgrade", cwd=root)
        self.assertIn(": migrated", out)
        self.assertIn("days); after that delete it yourself", out)
        self.assertEqual(len(list((sd / "upgrade" / "backups").iterdir())), 1, "one backup, the first")
        self.assertFalse((sd / "upgrade" / "unfinished.json").exists())
        self.assertEqual({p: p.read_bytes() for p in kept}, kept)
        self.assertTrue((sd / "events.jsonl").read_bytes().startswith(events))
        s = self.settings(root)
        self.assertEqual((s["hooks"]["Stop"][0], s["env"]), (USER_HOOK, USER_SETTINGS["env"]))
        self.assertEqual(self.hooks_python(root), {str(a / "bin" / "python")})
        self.assertIn("全部通过", self.fm(a, "doctor", cwd=root))
        for argv in (("status",), ("pause",), ("resume",)):
            self.fm(a, *argv, cwd=root)
        self.fm(a, "uninstall", cwd=root)
        self.assertEqual(self.settings(root), USER_SETTINGS)
        self.assertEqual((sd / "batches" / "b.1.log.md").read_text(), "### old\n\nkept\n")
        self.assertEqual((self.t / "config" / "config.toml").read_text(), USER_CONFIG)
        reg = self.t / "config" / "projects"
        self.assertFalse(reg.exists() and str(root) in reg.read_text(), "still registered")

    def files(self, root) -> dict:
        return {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


if __name__ == "__main__":
    unittest.main()
