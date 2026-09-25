import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import cli, handoff, header, lock, seat, worktree
from foremind.carriers import Carrier, CarrierError, SessionExists, SessionState
from foremind.carriers.manual import ManualCarrier
from foremind.events import EventLog
from foremind.lock import ExitEvidence
from foremind.state import IllegalTransition

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
# fake `gh pr list --head <branch> --state open --json headRefOid`: $FAKE_GH_HEAD empty -> no open PR
FAKE_GH = """
import json, os, sys
open(os.environ["FAKE_GH_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
head = os.environ.get("FAKE_GH_HEAD", "")
print(json.dumps([{"headRefOid": head}] if head else []))
"""


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(path, name, text):
    (Path(path) / name).parent.mkdir(parents=True, exist_ok=True)
    (Path(path) / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", f"add {name}")
    return git(path, "rev-parse", "HEAD")


class FakeCarrier(Carrier):
    """Stands in for tmux/orca: create() plays the session's SessionStart hook by writing its heartbeat."""
    name = "fake"

    def __init__(self, root, heartbeat=True):
        super().__init__(root)
        self.heartbeat, self.created, self.sent, self.closed = heartbeat, {}, [], []
        self.existing, self.evidence, self.on_create, self.send_error = set(), True, None, None

    def create(self, session, launch):
        if session in self.existing or session in self.created:
            raise SessionExists(session)
        self.created[session] = launch
        if self.on_create:
            self.on_create()
        if self.heartbeat:
            hb = seat.heartbeat_path(self.root, session)
            hb.parent.mkdir(parents=True, exist_ok=True)
            hb.write_text(json.dumps({"session": session, "batch": launch.env["FOREMIND_BATCH"], "role": "seat",
                                      "agent_session_id": "a-1", "ts": "2026-09-25T00:00:00+00:00",
                                      "event": "SessionStart", "tool_open": False, "handoff_requested": False}))

    def send(self, session, text):
        if self.send_error:
            raise self.send_error
        self.sent.append((session, text))
        return None

    def read_state(self, session):
        return SessionState(alive=session in self.created, idle=True)

    def wait_idle(self, session, timeout_s):
        return True

    def close(self, session):
        self.closed.append(session)
        return ExitEvidence(session, self.name, "absent") if self.evidence else None

    def list_sessions(self):
        return list(self.created)


class SeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "gh").write_text(f"#!{sys.executable}\n{FAKE_GH}")
        (bin_ / "gh").chmod(0o755)
        self.cfg_home = self.tmp / "cfg"
        self.gh_log = self.tmp / "gh.log"
        self.enterContext(mock.patch.dict(os.environ, {
            **GIT_ENV, "FOREMIND_WT_ROOT": str(self.tmp / "wt"), "FOREMIND_CONFIG_HOME": str(self.cfg_home),
            "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_HEAD": "", "FAKE_GH_LOG": str(self.gh_log)}))
        os.environ.pop("FOREMIND_PROJECT", None)
        self.root = self.tmp / "shop"
        (self.root / ".foremind" / "batches").mkdir(parents=True)
        self.main = {}
        for rid in ("api", "app"):
            (self.root / rid).mkdir()
            git(self.root / rid, "init", "-qb", "main")
            self.main[rid] = commit(self.root / rid, "README", rid)
        self.write_config()
        self.carrier = FakeCarrier(self.root)
        self.slug = seat.project_slug(self.root, {})

    def write_config(self, project="", user=""):
        repos = "".join(f'[[repos]]\nid = "{r}"\npath = "{r}"\ndefault_branch = "main"\n' for r in ("api", "app"))
        (self.root / "foremind.toml").write_text(repos + project)
        self.cfg_home.mkdir(exist_ok=True)
        (self.cfg_home / "config.toml").write_text(user)

    def add_remote(self, rid, github=False):
        """origin for repo `rid`, backed by a local bare clone; github=True gives it a github.com URL that git
        rewrites to the bare clone (insteadOf), so fetch works offline and the fake gh is asked."""
        bare = self.tmp / f"{rid}.git"
        git(self.tmp, "clone", "-q", "--bare", str(self.root / rid), str(bare))
        url = f"https://github.com/o/{rid}.git" if github else str(bare)
        git(self.root / rid, "remote", "add", "origin", url)
        if github:
            git(self.root / rid, "config", f"url.{bare}.insteadOf", url)
        return bare

    def write_header(self, bid, **kw):
        h = {"id": bid, "plan_id": "p", "reqs": ["REQ-1"], "repos": ["api"], "owns_paths": ["api:src/**"],
             "reads": [], "depends_on": [], "merge_after": [], "start_commands": ["test -f README"],
             "accept_commands": ["true"],
             "tiers": {"difficulty": "S", "org": "single", "review": "zero_context", "model": "claude-opus-5-5",
                       "effort": "high", "reason": "test"},
             "mode": "auto", "hard_block": [], "budget_estimate": "80000", "must_read": [], "tools": [],
             "state": "ready", **kw}
        seat.header_path(self.root, bid).write_text(header.render(h, f"# {bid}\n"))

    def state(self, bid):
        return seat.read_header(self.root, bid)["state"]

    def events(self, type_=None):
        evs = list(EventLog(self.root / ".foremind" / "events.jsonl").iter())
        return [e for e in evs if type_ is None or e["type"] == type_]

    def results(self, type_):
        return [e for e in self.events(type_) if e["phase"] == "result"]

    def name(self, bid, n):
        return f"fm-{self.slug}-{bid.replace('.', '_')}-{n}"

    def wt(self, bid, rid="api"):
        return self.tmp / "wt" / self.slug / bid / rid

    def open(self, bid, **kw):
        return seat.open_seat(self.root, bid, carrier=self.carrier, **kw)

    # --- fresh open --------------------------------------------------------

    def test_fresh_open_single_repo(self):
        self.write_header("p.1")
        res = self.open("p.1")
        self.assertTrue(res["ok"], res)
        s = res["session"]
        self.assertEqual(s, self.name("p.1", 1))
        self.assertRegex(s, r"^fm-shop-[0-9a-f]{6}-p_1-1$")
        self.assertEqual(res["worktrees"], {"api": str(self.wt("p.1"))})
        self.assertEqual((lock.holder(self.root, "p.1"), self.state("p.1")), (s, "running"))
        self.assertEqual(git(self.wt("p.1"), "symbolic-ref", "--short", "HEAD"), "fm/p.1")
        spec = self.carrier.created[s]
        self.assertEqual(spec.cwd, self.wt("p.1"))
        # I40: the settings file (the seat's hooks) lives under .foremind/sessions/, out of the seat's reach
        self.assertEqual(spec.argv[:3], ["claude", "--settings",
                                         str(self.root / ".foremind" / "sessions" / f"{s}.settings.json")])
        self.assertTrue(Path(spec.argv[2]).is_file())
        self.assertEqual(spec.argv[3:5], ["--permission-mode", "acceptEdits"])
        self.assertNotIn("--bare", spec.argv)
        self.assertNotIn("--add-dir", spec.argv)
        self.assertEqual(spec.env["FOREMIND_PROJECT"], str(self.root))
        self.assertNotIn("CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD", spec.env)
        self.assertEqual(len(self.carrier.sent), 1)
        self.assertIn("开工：批次 p.1", self.carrier.sent[0][1])
        self.assertEqual([e["exit_code"] for e in self.events("verify_command")], [0])
        self.assertEqual(self.events("seat_opened")[0]["session"], s)
        # the kickoff went out as a program delivery only after the heartbeat (the launch *result*)
        evs = self.events()
        heartbeat = next(i for i, e in enumerate(evs) if e["type"] == "seat_launch" and e["phase"] == "result")
        delivery = next(i for i, e in enumerate(evs) if e["type"] == "program_delivery")
        self.assertLess(heartbeat, delivery)
        self.assertEqual([(e["phase"], e.get("ok")) for e in self.events("seat_open")],
                         [("intent", None), ("result", True)])
        self.assertEqual({e["dedupe_id"] for e in self.events("seat_launch")}, {f"seat_launch:{s}"})  # N-b

    def test_fresh_open_only_from_ready(self):
        # SF-C: a started batch without a holder goes to a successor (verified, bound by accept), not a new seat
        for st in ("stuck", "changes_requested", "running", "paused"):
            with self.subTest(state=st):
                self.write_header("p.1", state=st)
                with self.assertRaises(seat.SeatError) as cm:
                    self.open("p.1")
                self.assertIn("only a ready batch", str(cm.exception))
                self.assertEqual((lock.holder(self.root, "p.1"), self.carrier.created), (None, {}))

    def test_lock_taken_while_starting(self):
        # SF-1: inside the state lock the claim turns out to be someone else's: no kickoff, their lock stays
        self.write_header("p.1")
        s = self.name("p.1", 1)
        self.carrier.on_create = lambda: lock.transfer(self.root, "p.1", s, "fm-other")
        with self.assertRaises(seat.SeatError) as cm:
            self.open("p.1")
        self.assertIn("lock no longer held", str(cm.exception))
        self.assertEqual(self.carrier.closed, [s])
        self.assertEqual(self.carrier.sent, [])
        self.assertEqual((lock.holder(self.root, "p.1"), self.state("p.1")), ("fm-other", "ready"))
        self.assertEqual(self.events("seat_opened"), [])

    def test_verify_mismatch_refuses_start(self):
        self.write_header("p.1", start_commands=["test -f README", {"run": "exit 3", "expect": 3}, "exit 4"])
        res = self.open("p.1")
        self.assertFalse(res["ok"])
        self.assertEqual(res["problems"], ["`exit 4` exited 4, record says 0"])  # `expect` honoured (I45)
        self.assertEqual(self.carrier.created, {})  # nothing launched, no start instruction
        self.assertEqual(self.carrier.sent, [])
        self.assertIsNone(lock.holder(self.root, "p.1"))  # the claim is given back
        self.assertEqual(self.state("p.1"), "ready")
        self.assertEqual(self.events("seat_verify_failed")[0]["problems"], res["problems"])
        self.assertEqual(self.results("seat_open")[0]["ok"], False)

    def test_dirty_fresh_worktree_refused(self):
        self.write_header("p.1", start_commands=["exit 1"])
        self.assertFalse(self.open("p.1")["ok"])  # the worktree exists now
        (self.wt("p.1") / "stray.py").write_text("left behind")
        self.write_header("p.1")
        res = self.open("p.1")
        self.assertIn("api: uncommitted changes not in the record's changed_files: ['stray.py']", res["problems"])

    def test_pr_head_checked_on_github(self):
        self.add_remote("api", github=True)
        self.write_header("p.1")
        os.environ["FAKE_GH_HEAD"] = "f" * 40
        res = self.open("p.1")
        self.assertFalse(res["ok"])
        self.assertIn(f"api: open PR head {'f' * 40} differs from local HEAD {self.main['api']}", res["problems"])
        self.assertIn("pr list --head fm/p.1 --state open --json headRefOid", self.gh_log.read_text())
        os.environ["FAKE_GH_HEAD"] = self.main["api"]
        self.assertTrue(self.open("p.1")["ok"])

    def test_no_pr_is_fine_and_other_hosts_are_not_asked(self):
        self.add_remote("api", github=True)
        self.write_header("p.1")
        self.assertTrue(self.open("p.1")["ok"])  # gh answers [] -> no PR
        self.add_remote("app")  # a plain (non-GitHub) remote: no gh at all
        self.gh_log.unlink()
        self.write_header("p.2", repos=["app"], owns_paths=["app:x.py"])
        self.assertTrue(self.open("p.2")["ok"])
        self.assertFalse(self.gh_log.exists())

    def test_start_point_is_the_fetched_remote_target(self):
        bare = self.add_remote("api")
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(bare), str(other))
        ahead = commit(other, "new.py", "x")
        git(other, "push", "-q", "origin", "main")
        self.write_header("p.1")
        res = self.open("p.1")
        self.assertTrue(res["ok"], res)
        self.assertEqual(git(self.wt("p.1"), "rev-parse", "HEAD"), ahead)  # not the stale local main
        self.assertEqual(git(self.root / "api", "rev-parse", "main"), self.main["api"])  # user's checkout untouched
        with self.assertRaises(subprocess.CalledProcessError):  # --no-track: fm/p.1 does not follow origin/main
            git(self.root / "api", "config", "--get", "branch.fm/p.1.merge")

    def test_no_target_refuses_instead_of_the_current_checkout(self):
        (self.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')  # no default_branch
        self.write_header("p.1")
        with self.assertRaises(seat.SeatError) as cm:
            self.open("p.1")
        self.assertIn("default_branch", str(cm.exception))
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.add_remote("api")  # a remote default branch will do (I46)
        git(self.root / "api", "fetch", "-q", "origin")
        git(self.root / "api", "remote", "set-head", "origin", "main")
        self.assertTrue(self.open("p.1")["ok"])

    def test_no_heartbeat_no_kickoff(self):
        self.write_config(user="[seat]\nsessionstart_timeout_s = 1\n")
        self.carrier.heartbeat = False
        self.write_header("p.1")
        res = self.open("p.1")
        self.assertFalse(res["ok"])
        self.assertIn("no heartbeat", res["problems"][0])
        self.assertEqual(self.carrier.closed, [res["session"]])
        self.assertEqual(self.carrier.sent, [])
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.state("p.1"), "ready")
        # a retry gets a new session name, never the one that failed
        self.carrier.heartbeat = True
        self.assertEqual(self.open("p.1")["session"], self.name("p.1", 2))

    def test_unconfirmed_close_keeps_the_lock(self):
        self.write_config(user="[seat]\nsessionstart_timeout_s = 1\n")
        self.carrier.heartbeat, self.carrier.evidence = False, False
        self.write_header("p.1")
        res = self.open("p.1")
        self.assertFalse(res["ok"])
        self.assertEqual(lock.holder(self.root, "p.1"), res["session"])  # the session may still be running
        self.assertEqual(self.events("seat_no_heartbeat")[0]["released"], False)

    def test_manual_no_heartbeat_gives_the_claim_back(self):
        self.write_config(user="[seat]\nmanual_sessionstart_timeout_s = 1\n")
        self.write_header("p.1")
        out = io.StringIO()
        res = seat.open_seat(self.root, "p.1", carrier=ManualCarrier(self.root, out=out))
        self.assertFalse(res["ok"])
        self.assertIn("within 1 s", res["problems"][0])
        self.assertIn(res["session"], out.getvalue())
        self.assertIsNone(lock.holder(self.root, "p.1"))  # manual starts nothing itself: safe to give back
        self.assertEqual(self.state("p.1"), "ready")

    def test_carrier_failure_closes_then_gives_the_claim_back(self):
        self.write_header("p.1")
        with mock.patch.object(self.carrier, "create", side_effect=CarrierError("tmux: no server")):
            with self.assertRaises(CarrierError):
                self.open("p.1")
        self.assertEqual(self.carrier.closed, [self.name("p.1", 1)])  # it may have started before failing
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.state("p.1"), "ready")
        self.assertEqual([e["phase"] for e in self.events("seat_launch")], ["intent", "result"])
        self.carrier.evidence = False
        with mock.patch.object(self.carrier, "create", side_effect=CarrierError("tmux: no server")):
            with self.assertRaises(CarrierError):
                self.open("p.1")
        self.assertEqual(lock.holder(self.root, "p.1"), self.name("p.1", 2))  # exit not confirmed: lock stays

    def test_existing_session_is_never_reused(self):
        self.write_header("p.1")
        self.carrier.existing.add(self.name("p.1", 1))  # e.g. another project's session on the same tmux server
        with self.assertRaises(SessionExists):
            self.open("p.1")
        self.assertEqual(self.carrier.closed, [])  # not ours: never closed
        self.assertEqual(self.carrier.sent, [])
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.results("seat_open")[0]["ok"], False)

    def test_session_names_are_unique(self):
        a, b = seat.project_slug(self.tmp / "x" / "app", {}), seat.project_slug(self.tmp / "y" / "app", {})
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("app-") and b.startswith("app-"))
        self.assertRegex(seat.project_slug(self.root, {"project.name": "My Shop!"}), r"^My_Shop_-[0-9a-f]{6}$")
        # the sequence survives rotation and counts names known only from files
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        log = EventLog(self.root / ".foremind" / "events.jsonl")
        self.assertGreater(log.rotate(self.root / ".foremind" / "archive", "9999-12"), 0)
        seat.heartbeat_path(self.root, s1).unlink()
        seat.settings_path(self.root, s1).unlink()
        prefix = seat.session_name(self.slug, "p.1", "")
        self.assertEqual(seat._next_seq(self.root, prefix), 2)  # s1 is only in the archive now
        seat.heartbeat_path(self.root, self.name("p.1", 7)).write_text("{}")
        (self.root / ".foremind" / "inbox").mkdir()
        (self.root / ".foremind" / "inbox" / f"{self.name('p.1', 9)}.md").write_text("")
        handoff.write_section(self.root, "p.1", self.section(git(self.wt("p.1"), "rev-parse", "HEAD")), author=s1)
        self.assertEqual(self.open("p.1", successor=True)["session"], self.name("p.1", 10))

    def test_delivery_failure_closes_and_gives_back(self):
        self.write_header("p.1")
        self.carrier.send_error = CarrierError("paste failed")
        with self.assertRaises(CarrierError):
            self.open("p.1")
        s = self.name("p.1", 1)
        self.assertEqual(self.carrier.closed, [s])
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.events("seat_opened"), [])
        res = self.results("seat_open")[0]
        self.assertEqual((res["ok"], res["closed"], res["released"]), (False, True, True))
        # running without a holder: the next seat is a successor (running -> running has no edge back to ready)
        self.assertEqual(self.state("p.1"), "running")

    def test_state_changed_while_starting(self):
        self.write_header("p.1")
        self.carrier.on_create = lambda: seat.set_state(self.root, "p.1", "paused")
        with self.assertRaises(IllegalTransition):
            self.open("p.1")
        self.assertEqual(self.state("p.1"), "paused")
        self.assertEqual(self.carrier.closed, [self.name("p.1", 1)])
        self.assertEqual(self.carrier.sent, [])
        self.assertIsNone(lock.holder(self.root, "p.1"))

    def test_multi_repo(self):
        self.write_header("p.1", repos=["api", "app"], owns_paths=["api:src/x.py", "app:lib/y.dart"],
                          start_commands=["test -d api && test -d app", {"run": "test -f README", "repo": "app"}])
        res = self.open("p.1")
        self.assertTrue(res["ok"], res)
        d = self.tmp / "wt" / self.slug / "p.1"
        spec = self.carrier.created[res["session"]]
        self.assertEqual(spec.cwd, d)
        i = spec.argv.index("--add-dir")
        self.assertEqual(spec.argv[i:i + 4], ["--add-dir", str(d / "api"), "--add-dir", str(d / "app")])
        self.assertEqual(spec.env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")
        self.assertNotIn("--bare", spec.argv)

    def test_excluded_model_or_provider(self):
        for user, model in (('[exclude]\nmodels = ["sonnet"]\n', "claude-sonnet-5"),
                            ('[exclude]\nproviders = ["anthropic"]\n', "claude-opus-5-5")):
            with self.subTest(user=user):
                self.write_config(user=user)
                self.write_header("p.1", tiers={"difficulty": "S", "org": "single", "review": "zero_context",
                                                "model": model, "effort": "high", "reason": "t"})
                with self.assertRaises(seat.SeatError):
                    self.open("p.1")
                self.assertIsNone(lock.holder(self.root, "p.1"))
                self.assertFalse(self.wt("p.1").exists())  # checked before anything is claimed or built
                self.assertEqual(self.carrier.created, {})

    def test_bad_header_is_a_seat_error(self):
        self.write_header("p.1")
        p = seat.header_path(self.root, "p.1")
        p.write_text(p.read_text().replace("tiers:", "tier:"))
        with self.assertRaises(seat.SeatError) as cm:
            self.open("p.1")
        self.assertIn("tiers: required", str(cm.exception))

    # --- runtime disjointness ---------------------------------------------

    def test_disjoint_with_started_batches(self):
        self.write_header("p.1", state="running")
        self.write_header("p.2", owns_paths=["api:src/auth.py"])
        with self.assertRaises(seat.SeatError):
            self.open("p.2")
        self.assertIsNone(lock.holder(self.root, "p.2"))
        self.write_header("p.3", owns_paths=["api:docs/x.md"])
        self.assertTrue(self.open("p.3")["ok"])
        self.write_header("p.4", owns_paths=["api:src/auth.py"], depends_on=["p.1"])  # stacked on p.1: allowed
        self.assertTrue(self.open("p.4")["ok"])
        # a claim counts before the state says running
        self.write_header("p.5", owns_paths=["app:x/**"], repos=["app"])
        lock.acquire(self.root, "p.5", "fm-other")
        self.write_header("p.6", owns_paths=["app:x/y.py"], repos=["app"])
        with self.assertRaises(seat.SeatError):
            self.open("p.6")
        # finished batches do not count
        self.write_header("p.7", owns_paths=["app:z.py"], repos=["app"], state="merged")
        self.write_header("p.8", owns_paths=["app:z.py"], repos=["app"])
        self.assertTrue(self.open("p.8")["ok"])

    def test_upstream_restarts_past_its_running_downstream(self):
        # I43: the exemption works both ways; an upstream back to ready is not blocked by its own downstream
        self.write_header("p.1", owns_paths=["api:src/a.py"])
        self.write_header("p.2", owns_paths=["api:src/a.py"], depends_on=["p.1"], state="running")
        self.assertTrue(self.open("p.1")["ok"])
        self.write_header("p.3", owns_paths=["api:src/a.py"])  # unrelated: still blocked
        with self.assertRaises(seat.SeatError):
            self.open("p.3")

    def test_paths_overlap(self):
        cases = [("api:src/a.py", "api:src/a.py", True), ("api:src/a.py", "app:src/a.py", False),
                 ("api:src", "api:src/a.py", True), ("api:src/a", "api:src/ab.py", False),
                 ("api:src/*.py", "api:src/a.py", True), ("api:src/**", "api:src/deep/x.md", True),
                 ("api:src/a*", "api:src/b*", False), ("api:docs/x.md", "api:src/*", False),
                 ("api:lib/*", "api:lib/sub/x.dart", True)]
        for a, b, want in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(seat.paths_overlap(a, b), want)
                self.assertEqual(seat.paths_overlap(b, a), want)

    # --- dependencies -------------------------------------------------------

    def test_cross_repo_readonly_and_stacked_start(self):
        self.write_config(project='[delivery]\ndepends_on = "approved"\n')
        git(self.root / "app", "checkout", "-qb", "fm/p.1")
        approved = commit(self.root / "app", "lib/api.dart", "v2")
        git(self.root / "app", "checkout", "-q", "main")
        self.write_header("p.1", repos=["app"], owns_paths=["app:lib/**"], state="changes_requested")
        batches = self.root / ".foremind" / "batches"
        (batches / "p.1.review.r1.json").write_text(json.dumps({"verdict": "approved", "heads": {"app": approved}}))
        # a later round asked for changes: stacked work keeps building on what was approved (SF-6)
        (batches / "p.1.review.r2.json").write_text(
            json.dumps({"verdict": "changes_requested", "heads": {"app": self.main["app"]}}))
        self.write_header("p.2", depends_on=["p.1"], owns_paths=["api:src/client.py"])
        res = self.open("p.2")
        self.assertTrue(res["ok"], res)
        ro = self.tmp / "wt" / self.slug / "p.2" / "_ro" / "p.1" / "app"
        self.assertEqual(git(ro, "rev-parse", "HEAD"), approved)
        spec = self.carrier.created[res["session"]]
        self.assertIn(str(ro), spec.argv)  # single-repo seat: the read-only upstream is added as a directory
        self.assertEqual(self.events("seat_opened")[0]["readonly"], [{"upstream": "p.1", "repo": "app",
                                                                      "sha": approved}])
        # same repo, approved mode: stacked on the upstream's reviewed head
        self.write_header("p.3", repos=["app"], depends_on=["p.1"], owns_paths=["app:lib/login.dart"])
        res = self.open("p.3")
        self.assertTrue(res["ok"], res)
        self.assertEqual(git(res["worktrees"]["app"], "rev-parse", "HEAD"), approved)

    # --- I do it myself ------------------------------------------------------

    def test_seat_user(self):
        self.write_header("p.1")
        paths = seat.claim_for_user(self.root, "p.1")
        self.assertEqual(paths, {"api": str(self.wt("p.1"))})
        self.assertEqual((lock.holder(self.root, "p.1"), self.state("p.1")), ("user", "running"))
        self.assertEqual(seat.claim_for_user(self.root, "p.1"), paths)  # again: same answer
        with self.assertRaises(seat.SeatError):
            self.open("p.1")
        self.write_header("p.2", owns_paths=["api:src/x.py"])  # the user's batch counts for disjointness
        with self.assertRaises(seat.SeatError):
            self.open("p.2")
        with self.assertRaises(seat.SeatError) as cm:  # N-d: no successor takes the user's batch
            self.open("p.1", successor=True)
        self.assertIn("held by the user", str(cm.exception))
        self.assertEqual(self.carrier.created, {})

    def test_cli(self):
        self.write_header("p.1")
        self.write_header("p.2", owns_paths=["api:docs/**"], start_commands=["exit 1"])
        self.write_config(user='[carrier]\nkind = "manual"\n')
        os.environ["FOREMIND_PROJECT"] = str(self.root)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["seat", "p.1", "--user"]), 0)
            self.assertEqual(cli.main(["seat", "p.2"]), 1)
        self.assertEqual(out.getvalue(), f"api\t{self.wt('p.1')}\n")
        self.assertIn("p.2 not started", err.getvalue())
        self.assertIn("`exit 1` exited 1", err.getvalue())

    # --- successor and continuation ---------------------------------------

    def section(self, sha, last=None, changed=()):
        s = {"goal": "g", "accept_commands": ["true"],
             "state": {"repos": {"api": {"branch": "fm/p.1", "sha": sha}}, "changed_files": list(changed)},
             "decisions": [], "failures": [], "next": ["继续"], "unverified": [],
             "pointers": {"transcript": "t.jsonl", "turns": [], "files": []}}
        if last:
            s["state"]["last_test"] = {"command": last[0], "exit_code": last[1]}
        return s

    def test_successor_verified_then_accepts(self):
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        head = commit(self.wt("p.1"), "src/a.py", "x")
        (self.wt("p.1") / "src" / "wip.py").write_text("uncommitted, but in the record")
        handoff.write_section(self.root, "p.1", self.section(head, ("test -f src/a.py && exit 4", 4),
                                                             changed=["api:src/wip.py"]), author=s1)
        res = self.open("p.1", successor=True)
        self.assertTrue(res["ok"], res)
        s2 = res["session"]
        self.assertEqual(s2, self.name("p.1", 2))
        self.assertEqual(lock.holder(self.root, "p.1"), s1)  # the successor takes it with --accept
        self.assertIn("handoff --accept", self.carrier.sent[-1][1])
        opened = self.events("seat_opened")[-1]
        self.assertEqual((opened["predecessor"], opened["record"]), (s1, "handoff"))
        self.assertEqual(handoff.accept(self.root, "p.1", s2), s1)
        self.assertEqual(lock.holder(self.root, "p.1"), s2)

    def test_successor_mismatch_not_launched(self):
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        handoff.write_section(self.root, "p.1", self.section(git(self.wt("p.1"), "rev-parse", "HEAD"), ("true", 0)),
                              author=s1)
        commit(self.wt("p.1"), "src/late.py", "written after the handoff")  # the record no longer matches
        (self.wt("p.1") / "src" / "stray.py").write_text("not in the record")
        res = self.open("p.1", successor=True)
        self.assertFalse(res["ok"])
        self.assertTrue(any(p.startswith("api: HEAD ") for p in res["problems"]), res["problems"])
        self.assertIn("api: uncommitted changes not in the record's changed_files: ['src/stray.py']", res["problems"])
        self.assertEqual(list(self.carrier.created), [s1])
        self.assertEqual(lock.holder(self.root, "p.1"), s1)
        with self.assertRaises(handoff.HandoffError):  # never opened as successor: cannot take the lock
            handoff.accept(self.root, "p.1", res["session"])

    def test_successor_without_record_says_so(self):
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        (self.wt("p.1") / "wip.py").write_text("left by a stuck session")
        lock.break_lock(self.root, "p.1", ExitEvidence(s1, "fake", "absent"))
        res = self.open("p.1", successor=True)
        self.assertTrue(res["ok"], res)
        self.assertIn("没有交接段可对照", self.carrier.sent[-1][1])
        opened = self.events("seat_opened")[-1]
        self.assertEqual((opened["predecessor"], opened["record"]), (None, "none"))
        self.assertIsNone(handoff.accept(self.root, "p.1", res["session"]))

    def test_successor_only_for_started_states(self):
        for st in ("ready", "review_ready", "paused"):
            with self.subTest(state=st):
                self.write_header("p.1", state=st)
                with self.assertRaises(seat.SeatError):
                    self.open("p.1", successor=True)
                self.assertEqual(self.carrier.created, {})

    def test_older_successor_cannot_take_the_lock_back(self):
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        handoff.write_section(self.root, "p.1", self.section(git(self.wt("p.1"), "rev-parse", "HEAD")), author=s1)
        s2 = self.open("p.1", successor=True)["session"]
        s3 = self.open("p.1", successor=True)["session"]  # s2 was slow; the supervisor tried again
        with self.assertRaises(handoff.HandoffError):  # s3 was opened after it
            handoff.accept(self.root, "p.1", s2)
        self.assertEqual(handoff.accept(self.root, "p.1", s3), s1)
        with self.assertRaises(handoff.HandoffError):
            handoff.accept(self.root, "p.1", s2)
        self.assertEqual(lock.holder(self.root, "p.1"), s3)

    def test_continuation(self):
        self.write_header("p.1")
        s1 = self.open("p.1")["session"]
        self.write_header("p.2", owns_paths=["api:src/next.py"], depends_on=["p.1"], budget_estimate="80000")
        self.write_header("p.1", state="delivered")
        res = seat.continue_seat(self.root, s1, "p.1", "p.2", remaining_budget=10 ** 6, carrier=self.carrier)
        self.assertFalse(res["ok"])  # I44: off by default
        self.assertIn("seat.continue_enabled", res["problems"][0])
        self.assertEqual(lock.holder(self.root, "p.1"), s1)
        self.write_config(user="[seat]\ncontinue_enabled = true\n")
        self.write_header("p.1", state="running")
        with self.assertRaises(seat.SeatError):  # p.1 is still running
            seat.continue_seat(self.root, s1, "p.1", "p.2", remaining_budget=10 ** 6, carrier=self.carrier)
        self.write_header("p.1", state="delivered")
        res = seat.continue_seat(self.root, s1, "p.1", "p.2", remaining_budget=50000, carrier=self.carrier)
        self.assertFalse(res["ok"])
        self.assertIn("remaining budget 50000 < estimate 80000", res["problems"][0])
        self.write_header("p.3", owns_paths=["api:src/other.py"], tiers={
            "difficulty": "S", "org": "single", "review": "zero_context", "model": "claude-opus-5-5",
            "effort": "xhigh", "reason": "t"})
        res = seat.continue_seat(self.root, s1, "p.1", "p.3", remaining_budget=10 ** 6, carrier=self.carrier)
        self.assertIn("p.3 needs claude-opus-5-5/xhigh", res["problems"][0])  # not the session's effort
        self.assertEqual((lock.holder(self.root, "p.1"), lock.holder(self.root, "p.2")), (s1, None))
        res = seat.continue_seat(self.root, s1, "p.1", "p.2", remaining_budget=100000, carrier=self.carrier)
        self.assertTrue(res["ok"], res)
        self.assertEqual((lock.holder(self.root, "p.1"), lock.holder(self.root, "p.2")), (None, s1))
        self.assertEqual(lock.held_by(self.root, s1), ["p.2"])
        self.assertEqual(self.state("p.2"), "running")
        (w1, add), (w2, text) = self.carrier.sent[-2:]
        self.assertEqual((w1, w2), (s1, s1))
        self.assertEqual(add, f"/add-dir {self.wt('p.2')}")  # the new worktree first, then the kickoff
        self.assertIn("p.2.handoff.md", text)
        self.write_header("p.4", owns_paths=["api:src/four.py"])
        with self.assertRaises(seat.SeatError):  # s1 no longer holds p.1
            seat.continue_seat(self.root, s1, "p.1", "p.4", remaining_budget=10 ** 6, carrier=self.carrier)


if __name__ == "__main__":
    unittest.main()
