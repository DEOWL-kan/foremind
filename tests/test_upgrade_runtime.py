"""m3a.8 REQ-5: `foremind upgrade` and the entries' admission: the CLI and its opens, the direct successor, job start
and runner, the supervisor's lifetime lock and re-exec, the hooks per kind of session, doctor, and a project after its
upgrade (old history read, uninstall keeping the user's entries)."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from unittest import mock

from foremind import install, job, upgrade
from foremind.commands import doctor, supervise
from foremind.events import EventLog
from foremind.hooks import guard
from foremind.install import settings
from foremind.supervisor import tick as sv
from test_upgrade_core import USER_HOOK, Base

TX = "20261013T000000Z-abcdef"
SLEEP = time.sleep  # SupervisorTest patches time.sleep itself: supervise.time is the time module


class RuntimeBase(Base):
    def mark(self, root, broken=False):
        p = upgrade.marker_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{" if broken else json.dumps({"transaction": TX, "started_at": "2026-10-13T00:00:00+00:00"}))

    def fm(self, root, *argv):
        return self.env.run(*argv, project=root)

    @contextlib.contextmanager
    def upgrading(self, root):
        """`foremind upgrade` holding the exclusive admission lock."""
        fd = os.open(upgrade.lock_path(root), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def events(self, root) -> bytes:
        p = self.sd(root) / "events.jsonl"
        return p.read_bytes() if p.exists() else b""


class UpgradeCommandTest(RuntimeBase):
    def test_a_current_project_checks_and_upgrades_to_0_writing_nothing(self):
        root = self.project(v0=False)  # a new project's first init is no migration
        before = self.files(root)
        self.assertEqual(self.fm(root, "upgrade", "--check")[0], 0)
        rc, out = self.fm(root, "upgrade")
        self.assertEqual((rc, out.splitlines()[0]), (0, f"{root}: current"))
        self.assertEqual(self.files(root), before)  # no lock file, state, event, backup or marker

    def test_a_v0_project_without_marker_reads_but_writes_only_after_upgrade(self):
        root = self.project()
        self.assertIsNone(upgrade.marker(root))
        for argv in (("pause",), ("init", "--yes", "--carrier", "tmux", "--root", str(root)), ("uninstall",)):
            rc, out = self.fm(root, *argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("needs foremind upgrade", out)
        self.assertFalse((self.sd(root) / "paused").exists())
        self.assertEqual(self.fm(root, "status")[0], 0, "diagnostics stay")
        rc, out = self.fm(root, "upgrade", "--check")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"to do: {root}", out)
        self.assertEqual(self.fm(root, "upgrade")[0], 0)
        self.assertEqual(self.fm(root, "pause")[0], 0)

    def test_check_all_reports_each_project_once_and_never_skips_a_broken_entry(self):
        a, b = self.project(v0=False), self.project(v0=False)  # init registered both
        install.registry_path().write_text("\n".join([str(a), str(a) + "/", "relative/p", str(self.env.tmp / "gone"),
                                                      str(b)]) + "\n")
        rc, out = self.fm(b, "upgrade", "--check", "--all")
        self.assertEqual(rc, 1, out)
        self.assertEqual([x for x in out.splitlines() if x.endswith(": current")], [f"{a}: current", f"{b}: current"])
        self.assertEqual(out.count("is not the path of a Foremind project"), 2)
        install.registry_path().write_text(f"{a}\n")
        self.assertEqual(self.fm(b, "upgrade", "--check", "--all")[0], 0)

    def test_options_exclude_each_other(self):
        root = self.project(v0=False)
        for argv in (("--check", "--confirm-exit", "t"), ("--all",), ("--confirm-exit", "t", "--all")):
            self.assertEqual(self.fm(root, "upgrade", *argv)[0], 2, argv)

    def test_only_the_user_confirms_or_migrates(self):
        root = self.project()
        (self.sd(root) / "supervisor.json").write_text("{")  # unknown evidence: a token to confirm
        rc, out = self.fm(root, "upgrade", "--check")
        self.assertEqual(rc, 1)
        token = out.split("--confirm-exit ")[1].split()[0]
        for k in ("FOREMIND_SESSION", "FOREMIND_ROLE", "FOREMIND_CONTROLLER", "FOREMIND_JOB"):
            with mock.patch.dict(os.environ, {k: "x"}):
                for argv in (("--confirm-exit", token), ()):
                    rc, out = self.fm(root, "upgrade", *argv)
                    self.assertEqual(rc, 1, (k, argv))
                    self.assertIn(f"only the user runs this, not a Foremind session or job ({k} is set)", out)
        self.assertFalse(upgrade.upgrade_dir(root).exists())
        self.assertEqual(self.fm(root, "upgrade", "--confirm-exit", "nope")[0], 1)
        rc, out = self.fm(root, "upgrade", "--confirm-exit", token)
        self.assertEqual(rc, 0, out)
        self.assertIn("confirmed exited: supervisor", out)
        self.assertEqual(self.fm(root, "upgrade", "--check")[0], 0)
        self.assertIsNotNone(upgrade.marker(root) or upgrade.pending(root), "confirming migrated nothing")

    def test_the_guard_denies_upgrade_to_every_session_role(self):  # its whitelist; none added for upgrade
        root = self.project(v0=False)
        for role in ("seat", "planner", "controller", "reviewer", None):
            for cmd in ("foremind upgrade", "foremind upgrade --confirm-exit abc", "foremind upgrade --check"):
                v = guard.evaluate("Bash", {"command": cmd}, str(root), root=root, session="fm-p-b_1-1", role=role,
                                   header={}, cfg={})
                self.assertTrue(v.matrix, (role, cmd))


class EntryTest(RuntimeBase):
    def test_an_unfinished_or_broken_marker_refuses_every_write_entry(self):
        root = self.project(v0=False)
        for broken, says in ((False, "an upgrade is unfinished"), (True, "is broken")):
            self.mark(root, broken)
            with mock.patch("foremind.seat.open_seat", side_effect=AssertionError("ran")), \
                    mock.patch("foremind.controller.open_controller", side_effect=AssertionError("ran")):
                for argv in (("seat", "b.1"), ("plan", "new", "x"), ("controller", "open"), ("resume",),
                             ("uninstall",), ("init", "--yes", "--carrier", "tmux", "--root", str(root)),
                             ("plan", "show", "p"), ("decide", "Q-1", "1"), ("doctor", "--rescan", "--yes")):
                    rc, out = self.fm(root, *argv)
                    self.assertEqual(rc, 1, (argv, out))
                    self.assertIn(says, out, argv)
            for argv in (("status",), ("version",), ("decide",), ("upgrade", "--check")):  # the check reports it
                self.assertNotIn(f"foremind {argv[0]}: ", self.fm(root, *argv)[1], argv)
        self.assertEqual(json.loads(settings.path(root).read_text())["hooks"]["Stop"][0], USER_HOOK)

    def test_a_write_holds_the_shared_lock_and_fails_at_once_while_upgrade_runs(self):
        root = self.project(v0=False)
        seen = []
        with mock.patch.object(sv, "pause", side_effect=lambda r, on: seen.append(upgrade._lock_held(root))):
            self.assertEqual(self.fm(root, "pause")[0], 0)
            self.assertIn("holds", seen[0], "held while the command ran")
            with self.upgrading(root):
                rc, out = self.fm(root, "pause")
            self.assertEqual((rc, len(seen)), (1, 1))
            self.assertIn("foremind upgrade or its check holds", out)
        self.assertIsNone(upgrade._lock_held(root), "released once it returned")

    def test_the_direct_successor_takes_admission_itself(self):
        root = self.project(v0=False)
        self.mark(root)
        with mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(root)}), \
                mock.patch.object(supervise.seat, "open_seat", side_effect=AssertionError("ran")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(supervise._successor("b.1"), 1)
        self.assertIn("an upgrade is unfinished", err.getvalue())
        upgrade.marker_path(root).unlink()
        seen = []
        with mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(root)}), \
                mock.patch.object(supervise.seat, "read_header", return_value={}), \
                mock.patch.object(supervise.seat, "open_seat", side_effect=lambda *a, **k: seen.append(
                    upgrade._lock_held(root)) or {"ok": True, "session": "s", "worktrees": {}}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(supervise._successor("b.1"), 0)
        self.assertIn("holds", seen[0])


class JobTest(RuntimeBase):
    def wait(self, jobs, jid):
        for _ in range(200):
            if (st := job.status(jobs, jid))["state"] == "done":
                return st
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_start_refuses_before_the_spec_and_the_runner_keeps_the_lock_until_it_ends(self):
        root = self.project(v0=False)
        jobs = self.sd(root) / "jobs"
        with self.upgrading(root), self.assertRaises(job.Refused):  # an OSError, as tick.start_job catches
            job.start(jobs, ["/bin/true"], cwd=root, job_id="j1")
        self.assertFalse((jobs / "j1").exists(), "no spec written")
        jid = job.start(jobs, ["/bin/sh", "-c", "sleep 1"], cwd=root)
        self.assertIsNotNone(upgrade._lock_held(root), "start returned: its runner holds the same lock")
        self.assertEqual(self.wait(jobs, jid)["exit_code"], 0)
        for _ in range(100):
            if upgrade._lock_held(root) is None:
                break
            time.sleep(0.05)
        self.assertIsNone(upgrade._lock_held(root), "released once the runner ended")
        self.mark(root)
        with self.assertRaises(job.Refused):
            job.start(jobs, ["/bin/true"], cwd=root, job_id="j2")
        self.assertFalse((jobs / "j2").exists())

    def test_a_runner_takes_admission_before_its_pid(self):
        root = self.project(v0=False)
        d = self.sd(root) / "jobs" / "j"
        d.mkdir(parents=True)
        (d / "spec.json").write_text(json.dumps({"argv": ["/bin/true"], "cwd": str(root), "env": {},
                                                 "timeout_s": None}))
        self.mark(root)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(job.run(d / "spec.json"), 1)
            with mock.patch.dict(os.environ, {job.ADMISSION_FD: "999"}):  # not the lock's descriptor
                upgrade.marker_path(root).unlink()
                self.assertEqual(job.run(d / "spec.json"), 1)
                self.assertNotIn(job.ADMISSION_FD, os.environ, "never passed on to the command")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["spec.json"], "no pid, no result")

    def test_a_job_outside_every_project_runs_as_before(self):
        jobs = self.env.tmp / "plain" / "jobs"
        jid = job.start(jobs, ["/bin/sh", "-c", "true"], cwd=self.env.tmp)
        self.assertEqual(self.wait(jobs, jid)["exit_code"], 0)
        self.assertIsNone(job.project_of(jobs))


class SupervisorTest(RuntimeBase):
    def setUp(self):
        super().setUp()
        self.root = self.project(v0=False)
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}))
        self.enterContext(mock.patch.object(supervise, "_unattended"))
        self.enterContext(mock.patch.object(supervise.notifier, "notify"))
        self.enterContext(mock.patch.object(supervise.time, "sleep", side_effect=KeyboardInterrupt))

    def test_it_holds_admission_for_its_life_and_does_not_start_while_blocked(self):
        seen = []
        with mock.patch.object(sv, "tick", side_effect=lambda r: seen.append(upgrade._lock_held(r))):
            self.assertEqual(supervise._supervise(None), 0)
        self.assertIn("holds", seen[0])
        self.assertIsNone(upgrade._lock_held(self.root))
        sv.supervisor_path(self.root).unlink()
        self.mark(self.root)
        with mock.patch.object(sv, "tick") as tick, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(supervise._supervise(None), 1)
            self.assertEqual(supervise._supervise(Namespace(try_run=True)), 1, "the try run of new code")
        self.assertIn("an upgrade is unfinished", err.getvalue())
        self.assertFalse(tick.called or sv.supervisor_path(self.root).exists())

    def test_re_exec_carries_the_lock_and_is_refused_while_blocked(self):
        ok = subprocess.CompletedProcess([], 0, "", "")
        with upgrade.admitted(self.root) as fd, \
                mock.patch.object(supervise.subprocess, "run", return_value=ok) as run, \
                mock.patch.object(supervise.os, "execve", side_effect=KeyboardInterrupt) as ex, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                supervise._reexec(self.root, sv.fm("supervise"), {"A": "1"}, 1, 2, fd)
            self.assertEqual(run.call_args.args[0], sv.fm("supervise", "--try"))
            self.assertEqual(ex.call_args.args[2], {"A": "1", job.ADMISSION_FD: str(fd)})
            self.assertTrue(os.get_inheritable(fd))
            # the new code takes the same descriptor over
            os.environ[job.ADMISSION_FD] = str(fd)
            seen = []
            with mock.patch.object(sv, "tick", side_effect=lambda r: seen.append(
                    (upgrade._lock_held(r), os.get_inheritable(fd), job.ADMISSION_FD in os.environ))):
                self.assertEqual(supervise._supervise(None), 0)
            self.assertEqual((bool(seen[0][0]), seen[0][1:]), (True, (False, False)))
            self.mark(self.root)
            run.reset_mock()
            ex.reset_mock()
            supervise._reexec(self.root, sv.fm("supervise"), {}, 1, 3, fd)
            self.assertFalse(run.called or ex.called, "no try run, no switch")
        errs = [e for e in EventLog(self.sd(self.root) / "events.jsonl").iter()
                if e["type"] == "supervisor_reexec_failed"]
        self.assertIn("an upgrade is unfinished", errs[-1]["error"])

    def test_the_lock_survives_a_real_exec(self):
        script = self.env.tmp / "exec.py"
        script.write_text(
            "import os, sys\nfrom foremind import upgrade\n"
            "with upgrade.admitted(sys.argv[1]) as fd:\n"
            "    os.set_inheritable(fd, True)\n"
            "    os.execv(sys.executable, [sys.executable, '-c', 'import time; time.sleep(2)'])\n")
        p = subprocess.Popen([sys.executable, str(script), str(self.root)],
                             env={**os.environ, "PYTHONPATH": str(sv.PKG_PARENT)})
        self.addCleanup(p.wait)
        held = False
        for _ in range(100):
            if held := bool(upgrade._lock_held(self.root)):
                break
            SLEEP(0.02)
        self.assertTrue(held)
        SLEEP(0.5)  # well past the exec
        self.assertTrue(upgrade._lock_held(self.root))


class HookTest(RuntimeBase):
    def hook(self, root, event, **data):
        from foremind import hooks
        with mock.patch.object(hooks, "_error", side_effect=AssertionError("through the catch-all")):
            out = hooks.main(event, json.dumps({"cwd": str(root), "session_id": "a", **data}))
        return json.loads(out) if out else None

    def test_a_foremind_session_is_denied_the_users_session_goes_on_and_nobody_keeps_books(self):
        root = self.project(v0=False)
        self.mark(root)
        before = self.events(root)
        bash = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "t"}
        for env in ({"FOREMIND_SESSION": "fm-p-b_1-1", "FOREMIND_ROLE": "seat"}, {"FOREMIND_CONTROLLER": "c"}):
            with mock.patch.dict(os.environ, env):
                out = self.hook(root, "PreToolUse", **bash)
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", env)
                self.assertIn("only the user runs foremind upgrade", out["hookSpecificOutput"]
                              ["permissionDecisionReason"])
                for ev in ("SessionStart", "PostToolUse", "Stop", "UserPromptSubmit", "Notification"):
                    self.assertIsNone(self.hook(root, ev, **bash, prompt="hi"), (env, ev))
        self.assertIsNone(self.hook(root, "PreToolUse", **bash), "the user's own session")
        self.assertIsNone(self.hook(root, "UserPromptSubmit", prompt="hi"))
        out = self.hook(root, "PreToolUse", tool_name="Edit",
                        tool_input={"file_path": str(self.sd(root) / "events.jsonl")})  # config.toml is the user's
        self.assertIn(".foremind/", out["hookSpecificOutput"]["permissionDecisionReason"], "the state guard stays")
        self.assertEqual(self.events(root), before, "no user_present, hook_denied or other event")
        self.assertFalse((self.sd(root) / "heartbeats").exists() and any((self.sd(root) / "heartbeats").iterdir()))

    def test_a_check_that_fails_holds_a_foremind_session(self):
        root = self.project(v0=False)
        with mock.patch.object(upgrade, "blocked", side_effect=OSError("disk")), \
                mock.patch.dict(os.environ, {"FOREMIND_SESSION": "fm-p-b_1-1"}):
            out = self.hook(root, "PreToolUse", tool_name="Read", tool_input={"file_path": str(root / "x")})
        self.assertIn("cannot tell whether an upgrade blocks it", out["hookSpecificOutput"]["permissionDecisionReason"])


class DoctorTest(RuntimeBase):
    def fixes(self, root) -> dict:
        return {n: (ok, d, f) for n, ok, d, f in doctor.checks(root)}

    def test_old_format_unfinished_and_broken_markers_with_their_commands_and_no_init(self):
        root = self.project()
        c = self.fixes(root)["project up to date with this Foremind"]
        self.assertFalse(c[0])
        self.assertIn("format 0", c[1])
        self.assertIn("upgrade --check", c[2])
        self.mark(root)
        c = self.fixes(root)["no unfinished upgrade"]
        self.assertIn(TX, c[1])
        self.assertIn("upgrade", c[2])
        self.assertIn("to undo the upgrade by hand", c[2])
        self.mark(root, broken=True)
        c = self.fixes(root)["upgrade marker readable"]
        self.assertIn("cannot be read", c[2])
        self.assertFalse([f for f in self.fixes(root).values() if "foremind init" in f[2]])


class AfterUpgradeTest(RuntimeBase):
    def test_old_history_reads_the_flow_goes_on_and_uninstall_keeps_the_users_entries(self):
        root = self.project()
        EventLog(self.sd(root) / "events.jsonl").append("batch_retried", batch="b.1", by="user")
        (self.sd(root) / "batches").mkdir(exist_ok=True)
        log = self.sd(root) / "batches" / "b.1.log.md"
        log.write_text("### old\n\nkept\n")
        rc, out = self.fm(root, "upgrade")
        self.assertEqual(rc, 0, out)
        self.assertEqual(log.read_text(), "### old\n\nkept\n")
        self.assertIn("batch_retried", [e["type"] for e in EventLog(self.sd(root) / "events.jsonl").iter()])
        for argv in (("pause",), ("resume",), ("status",)):
            self.assertEqual(self.fm(root, *argv)[0], 0, argv)
        rc, out = self.fm(root, "uninstall")
        self.assertEqual(rc, 0, out)
        data = json.loads(settings.path(root).read_text())
        self.assertEqual((data["hooks"], data["env"]), ({"Stop": [USER_HOOK]}, {"TOKEN": "s3cret"}))
