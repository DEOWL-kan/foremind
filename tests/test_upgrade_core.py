"""m3a.7 REQ-4: the offline upgrade core (foremind/upgrade.py) on temp projects: what it migrates, process evidence,
the user's exit confirmation, admission, backups, interruptions at every write and the recovery it prints."""
import json
import os
import shlex
import stat
import time
import unittest
from pathlib import Path
from unittest import mock

from foremind import sessions, upgrade
from foremind.install import settings
from foremind.vendors import claude
from test_install_fixture import Env

USER_HOOK = {"matcher": "", "hooks": [{"type": "command", "command": "say done"}]}
LSTART = "Mon Jan  5 00:00:00 2026"
ROOT = os.geteuid() == 0  # chmod 0 does not stop root


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        for k in ("FOREMIND_CONTROLLER", "FOREMIND_JOB"):
            self.enterContext(mock.patch.dict(os.environ, {k: ""}))
            os.environ.pop(k)
        self.procs = {}  # pid -> (alive, started epoch)
        self.enterContext(mock.patch.object(upgrade, "_proc", lambda pid: self.procs.get(pid, (False, None))))
        self.ps = {1: sessions.Proc(1, 0, 0.0, 0, LSTART, "/sbin/launchd")}
        self.enterContext(mock.patch.object(upgrade, "_ps", lambda: self.ps))
        self.now = time.time()
        self.n = 0

    def project(self, *, v0=True, name=None) -> Path:
        self.n += 1
        root = self.env.repo(self.env.tmp / (name or f"p{self.n}"))
        p = settings.path(root)
        p.parent.mkdir()
        p.write_text(json.dumps({"hooks": {"Stop": [USER_HOOK]}, "env": {"TOKEN": "s3cret"}}))
        rc, out = self.env.run("init", "--yes", "--carrier", "tmux", "--root", str(root))
        self.assertEqual(rc, 0, out)
        if v0:  # and run by another interpreter: both files change
            m = settings.manifest_path(root, root)
            man = json.loads(m.read_text())
            del man["format_version"]
            m.write_text(json.dumps(man))
            self.old(root)
        return root

    def old(self, root):
        mine = json.dumps(claude.foremind_command("statusline").split()[1])[1:-1]
        for p in (settings.path(root), settings.manifest_path(root, root)):
            p.write_text(p.read_text().replace(mine, "/old/bin/python3"))

    def files(self, root) -> dict:
        return {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}

    def sd(self, root) -> Path:
        return root / ".foremind"


class MigrateTest(Base):
    def test_check_reads_only_then_migrate_keeps_user_entries_and_backs_up(self):
        root = self.project()
        sp, m = settings.path(root), settings.manifest_path(root, root)
        before = self.files(root)
        rep = upgrade.preflight(root, self.now)
        self.assertTrue(rep.ok, rep.lines())
        self.assertEqual((rep.status, rep.todo), ("checked", [str(root)]))
        self.assertEqual(self.files(root), before)  # no lock file, marker, backup or event
        self.assertFalse(upgrade.lock_path(root).exists())
        self.assertIn("format 0", upgrade.pending(root)[0])
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "migrated", rep.lines())
        self.assertTrue(rep.ok)
        data = json.loads(sp.read_text())
        self.assertEqual(data["env"], {"TOKEN": "s3cret"})
        self.assertEqual(data["hooks"]["Stop"][0], USER_HOOK)
        self.assertEqual(settings.parse(m)[0], 1)
        self.assertEqual((upgrade.pending(root), upgrade.marker(root), upgrade.blocked(root)), ([], None, None))
        # the backup: whole, private, ignored, and nothing of the settings in the record or the output
        d = rep.backup
        self.assertEqual(d.parent, upgrade.upgrade_dir(root) / "backups")
        for x in (upgrade.upgrade_dir(root), d.parent, d):
            self.assertEqual(stat.S_IMODE(x.stat().st_mode), 0o700, x)
        for x in d.iterdir():
            self.assertEqual(stat.S_IMODE(x.stat().st_mode), 0o600, x)
        self.assertEqual((upgrade.upgrade_dir(root) / ".gitignore").read_text(), "*\n")
        tx = json.loads((d / "transaction.json").read_text())
        origs = {f["path"]: (d / f["original"]).read_bytes() for f in tx["files"]}
        self.assertEqual(origs, {str(m): before[m], str(sp.resolve()): before[sp]})
        self.assertNotIn("s3cret", (d / "transaction.json").read_text() + "\n".join(rep.lines()))
        out = "\n".join(rep.lines())
        self.assertIn(f"backup: {d}", out)
        self.assertIn(f"rm -rf {d}", out)
        self.assertAlmostEqual(upgrade._epoch(rep.expires) - self.now, 7 * 86400, delta=2)
        # a rerun finds nothing to do and writes nothing
        after = self.files(root)
        rep = upgrade.migrate(root, self.now)
        self.assertEqual((rep.status, rep.ok), ("current", True), rep.lines())
        self.assertEqual(self.files(root), after)

    def test_another_cli_s_hook_lines_are_migrated(self):
        root = self.project(v0=False)
        sp, m = settings.path(root), settings.manifest_path(root, root)
        self.old(root)
        self.assertIn("do not run this Foremind", upgrade.pending(root)[0])
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "migrated", rep.lines())
        self.assertNotIn("/old/bin/python3", sp.read_text() + m.read_text())
        self.assertIn("/old/bin/python3", json.dumps(json.loads((rep.backup / "transaction.json").read_text())
                                                     ["old_targets"]))

    def test_future_illegal_broken_and_conflict_refuse_before_any_write(self):
        cases = {"future": lambda m, sp: m.write_text(json.dumps({**json.loads(m.read_text()), "format_version": 2})),
                 "illegal": lambda m, sp: m.write_text(json.dumps({**json.loads(m.read_text()),
                                                                   "format_version": True})),
                 "broken": lambda m, sp: m.write_text("{"),
                 "conflict": lambda m, sp: sp.write_text(json.dumps({"hooks": {"Stop": [USER_HOOK]}})),
                 "unreadable install dir": lambda m, sp: os.chmod(m.parent, 0),
                 "unreadable settings": lambda m, sp: os.chmod(sp, 0),
                 "another directory's": lambda m, sp: m.write_text(json.dumps({**json.loads(m.read_text()), "path": str(
                     sp.parent.parent.parent / "other" / ".claude" / "settings.local.json")}))}
        for name, spoil in cases.items():
            with self.subTest(name):
                if ROOT and name.startswith("unreadable"):
                    self.skipTest("chmod 0 does not stop root")
                root = self.project()
                m, sp = settings.manifest_path(root, root), settings.path(root)
                modes, pre = {p: stat.S_IMODE(p.stat().st_mode) for p in (m.parent, sp)}, self.files(root)
                spoil(m, sp)
                for p, mode in modes.items():
                    self.addCleanup(os.chmod, p, mode)
                before = pre if name.startswith("unreadable") else self.files(root)
                rep = upgrade.migrate(root, self.now)
                for p, mode in modes.items():
                    os.chmod(p, mode)
                self.assertEqual(rep.status, "refused")
                self.assertEqual(len(rep.refusals), 1, rep.lines())
                self.assertEqual(self.files(root), before)
                self.assertFalse(upgrade.lock_path(root).exists())  # refused by the read-only check first
                self.assertIsNone(upgrade.marker(root))
                if name == "conflict":
                    self.assertIn("then rerun foremind upgrade, and foremind init after it", rep.refusals[0])

    @unittest.skipIf(ROOT, "chmod does not stop root")
    def test_an_events_log_that_cannot_be_appended_to_refuses(self):
        root = self.project()
        ev = self.sd(root) / "events.jsonl"
        os.chmod(ev, 0o444)
        self.addCleanup(os.chmod, ev, 0o644)
        before = self.files(root)
        rep = upgrade.preflight(root, self.now)  # the check says what migrate will
        self.assertEqual((rep.ok, len(rep.refusals)), (False, 1), rep.lines())
        self.assertIn(f"{ev} is not writable", rep.refusals[0])
        self.assertEqual(upgrade.migrate(root, self.now).status, "refused")
        self.assertEqual(self.files(root), before)
        os.chmod(ev, 0o644)
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")

    def test_a_broken_events_log_refuses_before_writing_settings(self):
        root = self.project()
        ev = self.sd(root) / "events.jsonl"
        with ev.open("a") as f:
            f.write("{\n")
        rep = upgrade.preflight(root, self.now)
        self.assertEqual([u["kind"] for u in rep.unknown], ["events"])
        self.assertIn("events.jsonl cannot be read", rep.refusals[0])
        upgrade.confirm_exit(root, rep.token, self.now)
        before = {p: p.read_bytes() for p in (settings.path(root), settings.manifest_path(root, root), ev)}
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "refused", rep.lines())
        self.assertIn("events.jsonl cannot be read", rep.refusals[0])
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertIsNone(upgrade.marker(root))
        self.assertEqual(list((upgrade.upgrade_dir(root) / "backups").iterdir()), [])

    def test_only_the_user_migrates_or_confirms(self):
        root = self.project()
        for k in ("FOREMIND_SESSION", "FOREMIND_ROLE", "FOREMIND_CONTROLLER", "FOREMIND_JOB"):
            with self.subTest(k), mock.patch.dict(os.environ, {k: "x"}):
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.migrate(root, self.now)
                with self.assertRaises(upgrade.UpgradeError):
                    upgrade.confirm_exit(root, "0" * 16, self.now)
        self.assertIsNone(upgrade.marker(root))


class InterruptTest(Base):
    def counting(self, fail_at=None):
        calls, real = [], (upgrade._write, upgrade._drop, settings.record)

        def wrap(f):
            def g(*a, **k):
                calls.append(a[0] if f is not settings.record else a[1])
                if fail_at is not None and len(calls) == fail_at:
                    raise OSError(f"injected at write {fail_at}")
                return f(*a, **k)
            return g
        return calls, [mock.patch.object(upgrade, "_write", wrap(real[0])),
                       mock.patch.object(upgrade, "_drop", wrap(real[1])),
                       mock.patch.object(settings, "record", wrap(real[2]))]

    def run_with(self, root, fail_at=None):
        calls, patches = self.counting(fail_at)
        for p in patches:
            self.enterContext(p)
        try:
            return upgrade.migrate(root, self.now), calls
        finally:
            for p in patches:
                p.stop()

    def test_every_write_point_interrupted_then_rerun(self):
        ref = self.project()
        sp0, m0 = settings.path(ref), settings.manifest_path(ref, ref)
        rep, calls = self.run_with(ref)
        self.assertEqual(rep.status, "migrated", rep.lines())
        want = {"settings": sp0.read_bytes(), "manifest": m0.read_text().replace(str(ref), "<root>")}
        self.assertGreater(len(calls), 8)
        for k in range(1, len(calls) + 1):
            with self.subTest(write=k, at=calls[k - 1]):
                root = self.project()
                sp, m = settings.path(root), settings.manifest_path(root, root)
                orig = {sp: sp.read_bytes(), m: m.read_bytes()}
                with self.assertRaises(OSError):
                    self.run_with(root, k)
                for p, b in orig.items():  # each business file: its original or the expected content
                    self.assertIn(p.read_bytes(), (b, *([want["settings"].replace(
                        str(ref).encode(), str(root).encode())] if p == sp else [])) if p == sp else (
                        b, want["manifest"].replace("<root>", str(root)).encode()))
                rep, _ = self.run_with(root)
                self.assertEqual(rep.status, "migrated", rep.lines())
                self.assertEqual(m.read_text().replace(str(root), "<root>"), want["manifest"])
                self.assertEqual(json.loads(sp.read_text())["hooks"]["Stop"][0], USER_HOOK)
                tx = json.loads((rep.backup / "transaction.json").read_text())
                self.assertEqual({f["path"]: (rep.backup / f["original"]).read_bytes() for f in tx["files"]},
                                 {str(p.resolve()): b for p, b in orig.items()})  # never the half-done ones
                self.assertTrue(all(f["written_at"] for f in tx["files"]))
                self.assertIsNone(upgrade.marker(root))

    def test_backup_failure_writes_no_business_file(self):
        root = self.project()
        before = self.files(root)
        real = upgrade._write
        with mock.patch.object(upgrade, "_write", lambda p, d, private=True: (_ for _ in ()).throw(
                OSError("disk full")) if str(p).endswith(".orig") else real(p, d, private)):
            with self.assertRaises(OSError):
                upgrade.migrate(root, self.now)
        for p in (settings.path(root), settings.manifest_path(root, root)):
            self.assertEqual(p.read_bytes(), before[p])
        self.assertIsNotNone(upgrade.marker(root))
        self.assertIsNotNone(upgrade.blocked(root))
        self.assertIn("no file was changed yet", "\n".join(upgrade.recovery(root)))
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")

    def interrupted(self, name=None):
        """A project whose upgrade stopped after the manifest was written, before settings.local.json."""
        root = self.project(name=name)
        real = upgrade._write
        with mock.patch.object(upgrade, "_write", lambda p, d, private=True: (_ for _ in ()).throw(
                OSError("cut")) if not private and Path(p).name == "settings.local.json" else real(p, d, private)):
            with self.assertRaises(OSError):
                upgrade.migrate(root, self.now)
        return root

    def test_third_content_is_not_overwritten_and_recovery_is_printed(self):
        root = self.interrupted("p 1")  # its commands quote the paths
        sp = settings.path(root)
        sp.write_text('{"mine": 1}')
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "unfinished", rep.lines())
        self.assertIn("neither", rep.problems[0])
        self.assertEqual(sp.read_text(), '{"mine": 1}')
        self.assertIsNotNone(upgrade.marker(root))
        out = "\n".join(rep.lines())
        d = rep.backup
        q = lambda x: shlex.quote(str(x))  # noqa: E731
        for s in (f"backup: {d}", f"cp -p {q(sp.resolve())} {q(d / 'conflict-1')}",
                  f"cp {q(d / '1.orig')} {q(sp.resolve())}", f"mv {q(upgrade.marker_path(root))} "
                  f"{q(d / 'unfinished.json')}", "--force-reinstall", "foremind doctor", "stop every Foremind process"):
            self.assertIn(s, out)

    def test_verification_failure_keeps_the_marker_then_a_rerun_finishes(self):
        root = self.project()
        with mock.patch.object(settings, "_probe", return_value={"absent": True}):
            rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "unfinished", rep.lines())
        self.assertFalse(rep.ok)
        self.assertIsNotNone(upgrade.marker(root))
        tx = json.loads((rep.backup / "transaction.json").read_text())
        self.assertEqual(len(tx["verify_failures"]), 1)
        self.assertIn(f"backup: {rep.backup}", rep.lines())
        self.assertIn("unfinished", upgrade.blocked(root))
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")

    def test_a_changed_protected_file_fails_verification(self):
        root = self.project()
        real = upgrade._apply

        def apply(r, tx, now):
            real(r, tx, now)
            (self.sd(root) / "config.toml").write_text("# edited\n")
        with mock.patch.object(upgrade, "_apply", apply):
            rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "unfinished")
        self.assertTrue(any("config.toml: changed" in x for x in rep.problems), rep.problems)

    def test_recheck_after_the_marker_keeps_it_and_writes_nothing(self):
        root = self.project()
        before = self.files(root)
        real, n = upgrade._processes, []

        def procs(*a, **k):
            n.append(1)
            return (["job x (pid 9)"], []) if len(n) == 3 else real(*a, **k)
        with mock.patch.object(upgrade, "_processes", procs):
            rep = upgrade.migrate(root, self.now)
        self.assertEqual((rep.status, rep.blockers), ("refused", ["job x (pid 9)"]))
        self.assertIsNotNone(upgrade.marker(root))
        for p in (settings.path(root), settings.manifest_path(root, root)):
            self.assertEqual(p.read_bytes(), before[p])
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")

    def test_another_foremind_may_not_finish_it(self):
        root = self.interrupted()
        with mock.patch.object(upgrade, "_by", return_value={"version": "0.0.0"}):
            rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "refused")
        self.assertIn("finish it with that one", rep.refusals[0])
        self.assertNotIn("run foremind upgrade to finish it", "\n".join(rep.lines()))

    def test_a_broken_events_log_stops_before_the_settings_write(self):
        root = self.interrupted()
        sp, ev = settings.path(root), self.sd(root) / "events.jsonl"
        good, before = ev.read_bytes(), sp.read_bytes()
        ev.write_bytes(good + b"[]\n")
        upgrade.confirm_exit(root, upgrade.preflight(root, self.now).token, self.now)
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "unfinished", rep.lines())
        self.assertIn("events.jsonl cannot be read", rep.problems[0])
        self.assertIn("to undo the upgrade by hand:", rep.lines())
        self.assertEqual(sp.read_bytes(), before)
        ev.write_bytes(good)
        if not ROOT:  # read-only: the same stop, no exception
            os.chmod(ev, 0o444)
            self.addCleanup(os.chmod, ev, 0o644)
            rep = upgrade.migrate(root, self.now)
            self.assertEqual(rep.status, "unfinished", rep.lines())
            self.assertIn("is not writable", rep.problems[0])
            self.assertEqual(sp.read_bytes(), before)
            os.chmod(ev, 0o644)
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")


class EvidenceTest(Base):
    def setUp(self):
        super().setUp()
        self.root = self.project(v0=False)

    def job(self, base, jid, age, *, spec=True, pid=None, result=None, future=False) -> Path:
        d = self.sd(self.root) / base / jid
        d.mkdir(parents=True)
        t = self.now + 3600 if future else self.now - age
        for name, text in (("spec.json", "{}" if spec else None), ("pid", pid), ("result.json", result)):
            if text is not None:
                (d / name).write_text(str(text))
                os.utime(d / name, (t, t))
        return d

    def archive(self, *events):
        p = self.sd(self.root) / "archive" / "events-2026-01.jsonl"
        p.parent.mkdir(exist_ok=True)
        with p.open("a") as f:
            for e in events:
                f.write(json.dumps({"id": os.urandom(4).hex(), "ts": "2026-01-02T00:00:00+00:00", "prev": "",
                                    "hash": "", **e}) + "\n")

    def check(self):
        rep = upgrade.preflight(self.root, self.now)
        return rep.blockers, [(u["kind"], Path(u["what"]).name, u["why"]) for u in rep.unknown]

    def test_recent_set_bounds(self):
        self.procs = {11: (True, self.now - 86400), 12: (True, self.now - 86401)}
        self.job("jobs", "edge", 86400, pid=11)  # changed exactly RECENT_JOB_S ago: in
        self.job("jobs", "old", 86401, pid=12)  # older, no intent: out, even with a live pid
        self.job("jobs", "done", 10, pid=13, result='{"exit_code": 0}')
        self.job("land", "b1-20260101T000000Z/1", 100, pid=14)  # land's own jobs, ended
        blockers, unknown = self.check()
        self.assertEqual((blockers, unknown), ([f"job {self.sd(self.root) / 'jobs' / 'edge'} (pid 11)"], []))

    def test_open_intents_of_any_age_and_archived_events(self):
        self.procs = {21: (True, self.now - 10 * 86400)}
        self.job("jobs", "a", 10 * 86400, pid=21)  # old, but its intent is open (in the archive)
        self.job("jobs", "b", 10 * 86400, pid=22)
        self.job("jobs", "c", 10 * 86400, pid=21)  # closed: its result is archived too
        self.archive({"type": "sv_gate", "phase": "intent", "dedupe_id": "sv_gate:a", "job": "a"},
                     {"type": "sv_seat", "phase": "intent", "dedupe_id": "sv_seat:b"},
                     {"type": "sv_job", "phase": "result", "dedupe_id": "sv_job:sv_seat:b", "job": "b"},
                     {"type": "sv_gate", "phase": "intent", "dedupe_id": "sv_gate:c", "job": "c"},
                     {"type": "sv_gate", "phase": "result", "dedupe_id": "sv_gate:c"})
        blockers, unknown = self.check()
        self.assertEqual((blockers, unknown), ([f"job {self.sd(self.root) / 'jobs' / 'a'} (pid 21)"], []))

    def test_pid_identity_starting_bad_times_and_missing_spec(self):
        self.procs = {31: (True, self.now - 50), 32: (True, self.now - 5), 33: (True, None)}
        self.job("jobs", "reused", 100, pid=31)  # started 50 s after its pid file: another process
        self.job("jobs", "noted", 100, pid=33)  # ps cannot tell
        self.job("jobs", "starting", 30)  # spec, no pid, inside the start window
        self.job("jobs", "never", 61)  # spec, no pid, past it: never started
        self.job("jobs", "future", 0, pid=32, future=True)
        self.job("jobs", "badpid", 5, pid="x")
        self.job("jobs", "badresult", 5, result="{")
        self.archive({"type": "sv_seat", "phase": "intent", "dedupe_id": "sv_seat:gone", "job": "gone"})
        with self.sd(self.root).joinpath("events.jsonl").open("a") as f:
            f.write(json.dumps({"id": "x1", "ts": upgrade._now_iso(self.now - 10), "type": "sv_gate",
                                "phase": "intent", "dedupe_id": "sv_gate:soon", "job": "soon", "prev": "",
                                "hash": ""}) + "\n")
        blockers, unknown = self.check()
        jobs = self.sd(self.root) / "jobs"
        self.assertEqual(sorted(blockers), sorted([f"job {jobs / 'starting'}: no pid yet",
                                                   f"job {jobs / 'soon'}: started 10 s ago, no record yet"]))
        self.assertEqual(sorted(unknown), sorted([
            ("job", "badpid", "its pid file is unreadable"),
            ("job", "badresult", "result.json is not a job result"),
            ("job", "future", "spec.json, pid modified in the future"),
            ("job", "gone", "its intent has no result and the job left no record"),
            ("job", "noted", "ps cannot tell when pid 33 started")]))

    @unittest.skipIf(ROOT, "chmod 0 does not stop root")
    def test_unreadable_listings_and_job_dirs_are_unknown(self):
        jobs, land, ctl = (self.sd(self.root) / x for x in ("jobs", "land", "controller"))
        self.job("jobs", "x", 10, pid=71)
        self.job("land", "b2-20260101T000000Z/1", 10, pid=72)
        ctl.mkdir()
        (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 51, "lstart": LSTART}))
        self.ps[51] = sessions.Proc(51, 1, 0.0, 0, LSTART, "claude")
        for d in (jobs / "x", land / "b2-20260101T000000Z", jobs, ctl):  # cleanups run in reverse: jobs first
            self.addCleanup(os.chmod, d, 0o755)
        os.chmod(jobs / "x", 0)
        self.assertEqual([(k, n) for k, n, _ in self.check()[1]], [("job", "x")])
        os.chmod(jobs, 0)
        os.chmod(land / "b2-20260101T000000Z", 0)
        os.chmod(ctl, 0)  # a live controller behind it: never read as none
        blockers, unknown = self.check()
        self.assertEqual(blockers, [])
        self.assertEqual(sorted((k, n) for k, n, _ in unknown),
                         [("controller", "controller"), ("jobs", "b2-20260101T000000Z"), ("jobs", "jobs")])
        self.assertTrue(all("cannot be listed" in why for _, _, why in unknown), unknown)

    def test_supervisor_record(self):
        p = self.sd(self.root) / "supervisor.json"
        at = int(self.now) - 100  # started_at is kept to the second

        def rec(**kv):
            p.write_text(json.dumps({"pid": 41, "started_at": upgrade._now_iso(at), "code": "x", **kv}))
        rec()
        self.assertEqual(self.check(), ([], []))  # pid gone
        self.procs = {41: (True, at - 30)}
        self.assertEqual(self.check()[0], ["foremind supervise (pid 41)"])  # a re-exec keeps the older start
        self.procs = {41: (True, at + 3)}
        self.assertEqual(self.check(), ([], []))  # D23: started after the record, beyond the slack
        self.procs = {41: (True, at + 2)}
        self.assertEqual(self.check()[0], ["foremind supervise (pid 41)"])
        for why, spoil in (("ps cannot tell", lambda: self.procs.update({41: (True, None)})),
                           ("started_at is missing", lambda: rec(started_at=None)),
                           ("unreadable", lambda: p.write_text("{"))):
            with self.subTest(why):
                spoil()
                blockers, unknown = self.check()
                self.assertEqual(blockers, [])
                self.assertIn(why, unknown[0][2])

    def test_sessions_and_controllers(self):
        sess = self.sd(self.root) / "sessions" / "fm-s1.settings.json"
        self.ps[50] = sessions.Proc(50, 1, 0.0, 0, LSTART, f"claude --settings {sess}")
        self.assertEqual(self.check()[0], ["session fm-s1 (pid 50)"])
        del self.ps[50]
        ctl = self.sd(self.root) / "controller"
        ctl.mkdir()
        (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 51, "lstart": LSTART}))
        (ctl / "b.json").write_text(json.dumps({"controller": "c2", "pid": 52}))
        (ctl / "c.json").write_text("[")
        (ctl / "d.json").write_text(json.dumps({"controller": "c4"}))
        self.ps[51] = sessions.Proc(51, 1, 0.0, 0, LSTART, "claude")
        self.ps[52] = sessions.Proc(52, 1, 0.0, 0, LSTART, "claude")
        blockers, unknown = self.check()
        self.assertEqual(blockers, ["session c1 (pid 51)"])
        self.assertEqual([u[1] for u in unknown], ["b.json", "c.json", "d.json"])
        self.ps[51] = sessions.Proc(51, 1, 0.0, 0, "Tue Jan  6 00:00:00 2026", "claude")  # pid taken since
        self.assertEqual(self.check()[0], [])
        with mock.patch.object(upgrade, "_ps", side_effect=sessions.SessionsError("no ps")):
            self.assertEqual(self.check()[1], [("sessions", "sessions", "ps failed (no ps): seats, planners and "
                                                "controllers cannot be told")])
        for p in ctl.glob("[bcd].json"):
            p.unlink()
        (ctl / "e.json").write_text(json.dumps({"pid": 53, "lstart": LSTART}))  # no name: sessions.live skips it
        (ctl / "f.json").write_text(json.dumps({"pid": 54}))
        self.ps[53] = sessions.Proc(53, 1, 0.0, 0, LSTART, "claude")
        self.ps[54] = sessions.Proc(54, 1, 0.0, 0, LSTART, "claude")
        blockers, unknown = self.check()
        self.assertEqual((blockers, [u[1] for u in unknown]), (["controller of e.json (pid 53)"], ["f.json"]))

    def test_controller_records_by_d23(self):
        ctl = self.sd(self.root) / "controller"
        ctl.mkdir()
        self.ps[51] = sessions.Proc(51, 1, 0.0, 0, LSTART, "claude")

        def only(lstart):
            for p in ctl.iterdir():
                p.unlink()
            (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 51, "lstart": lstart}))
            with mock.patch.object(sessions, "live", return_value={}):  # upgrade does not lean on it
                blockers, unknown = self.check()
            return blockers, [why for _, _, why in unknown]
        self.assertEqual(only(LSTART), (["controller c1 (pid 51)"], []))
        self.assertEqual(only("Sun Jan  4 23:59:58 2026"), (["controller c1 (pid 51)"], []))  # within the slack
        self.assertEqual(only("Sun Jan  4 23:59:57 2026"), ([], []))  # started later: the pid was reused
        for lstart in ("Tue Jan  6 00:00:00 2026", "garbage", 7):  # started before its record, or unreadable
            with self.subTest(lstart):
                blockers, why = only(lstart)
                self.assertEqual(blockers, [])
                self.assertIn("cannot tell whether it is that controller", why[0])
        # a stale record of the same reused pid does not hide the live one
        (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 51, "lstart": LSTART}))
        (ctl / "z.json").write_text(json.dumps({"controller": "c0", "pid": 51, "lstart": "Sun Jan  4 00:00:00 2026"}))
        blockers, unknown = self.check()
        self.assertEqual((len(blockers), unknown), (1, []))
        self.assertRegex(blockers[0], r"c1 \(pid 51\)")

    def test_a_ps_failure_confirmation_binds_the_records(self):
        ctl = self.sd(self.root) / "controller"
        ctl.mkdir()
        (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 51, "lstart": LSTART}))
        self.enterContext(mock.patch.object(upgrade, "_ps", side_effect=sessions.SessionsError("no ps")))
        token = upgrade.preflight(self.root, self.now).token
        upgrade.confirm_exit(self.root, token, self.now)
        self.assertTrue(upgrade.preflight(self.root, self.now).ok)
        (ctl / "a.json").write_text(json.dumps({"controller": "c1", "pid": 52, "lstart": LSTART}))  # rebound
        rep = upgrade.preflight(self.root, self.now)
        self.assertEqual([u["kind"] for u in rep.unknown], ["sessions"])
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.confirm_exit(self.root, token, self.now)


class ConfirmTest(Base):
    def test_token_binds_the_evidence_and_never_settles_running_work(self):
        root = self.project()
        p = self.sd(root) / "supervisor.json"
        p.write_text("{")
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "refused")
        token = rep.token
        self.assertIn(f"foremind upgrade --confirm-exit {token}", "\n".join(rep.lines()))
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.confirm_exit(root, "0" * 16, self.now)
        before = list(self.sd(root).joinpath("events.jsonl").read_bytes().splitlines())
        upgrade.confirm_exit(root, token, self.now)
        self.assertEqual(self.sd(root).joinpath("events.jsonl").read_bytes().splitlines(), before)  # no event
        for _ in range(2):  # a rerun of the check: still confirmed
            self.assertTrue(upgrade.preflight(root, self.now).ok)
        p.write_text("{ ")  # the evidence changed: void
        rep = upgrade.preflight(root, self.now)
        self.assertEqual(len(rep.unknown), 1)
        self.assertNotEqual(rep.token, token)
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.confirm_exit(root, token, self.now)
        upgrade.confirm_exit(root, rep.token, self.now)
        # a live supervisor is never confirmed away
        p.write_text(json.dumps({"pid": 61, "started_at": upgrade._now_iso(self.now - 5)}))
        self.procs = {61: (True, self.now - 6)}
        rep = upgrade.preflight(root, self.now)
        self.assertEqual((rep.blockers, rep.unknown, rep.token), (["foremind supervise (pid 61)"], [], None))
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.confirm_exit(root, _ := upgrade._token(root, []), self.now)
        self.assertEqual(upgrade.migrate(root, self.now).status, "refused")
        self.procs = {}
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")

    def test_token_is_bound_to_the_project(self):
        a, b = self.project(), self.project()
        for r in (a, b):
            (self.sd(r) / "supervisor.json").write_text("{")
        ta = upgrade.preflight(a, self.now).token
        self.assertNotEqual(ta, upgrade.preflight(b, self.now).token)
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.confirm_exit(b, ta, self.now)


class SlackTest(unittest.TestCase):
    def test_pid_slack_is_tick_s(self):
        from foremind.supervisor import tick
        self.assertEqual(upgrade.PID_SLACK_S, tick.PID_SLACK_S)


class AdmissionTest(Base):
    def test_entries_share_the_lock_and_an_upgrade_takes_it_alone(self):
        root = self.project()
        with self.assertRaises(upgrade.Blocked):  # v0: needs foremind upgrade first
            with upgrade.admitted(root):
                pass
        self.assertEqual(upgrade.migrate(root, self.now).status, "migrated")
        with upgrade.admitted(root) as fd, upgrade.admitted(root, fd=(dup := os.dup(fd))) as fd2:
            self.assertEqual(fd2, dup)
            rep = upgrade.preflight(root, self.now)
            self.assertEqual(rep.blockers, [f"a Foremind process holds {upgrade.lock_path(root)}"])
            self.assertEqual(upgrade.migrate(root, self.now).status, "refused")
            self.assertIsNone(upgrade.marker(root))
        os.close(dup)
        self.assertTrue(upgrade.preflight(root, self.now).ok)
        upgrade.marker_path(root).write_text("{")
        self.assertIn("broken", upgrade.blocked(root))
        with self.assertRaises(upgrade.Blocked):
            with upgrade.admitted(root):
                pass
        rep = upgrade.migrate(root, self.now)
        self.assertEqual(rep.status, "refused")
        self.assertIn("is broken", rep.refusals[0])

    def test_outside_a_foremind_project_nothing_is_made(self):
        d = self.env.tmp / "plain"
        d.mkdir()
        with upgrade.admitted(d) as fd:
            self.assertIsNone(fd)
        self.assertEqual(list(d.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
