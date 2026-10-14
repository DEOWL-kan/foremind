"""m3a REQ-8: land moves and rolls back targets with hooks off under the project lock; a batch already in its targets
lands as merged / already_merged with no new commit and no drafts; the automatic land takes no batch a merged
land_result on its current heads has landed, as long as git bears it out."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

import test_land
from foremind import config, job, review, seat, worktree
from foremind.commands import land
from foremind.paths import state_dir
from foremind.supervisor import tick as sv
from foremind.supervisor.phases import land as phase
from test_gate_fixture import Project, sh
from test_supervisor import H, REAL_START, REAL_STATUS, REPO, Base as TickBase

LIMIT = 60  # s: these lands take a few; one whose hook waits on the project lock it holds never ends
HOOK = """
import fcntl, os, sys
sys.stdin.read()
fd = os.open(LOCK, os.O_RDWR | os.O_CREAT)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = "free"
except BlockingIOError:
    state = "held"
with open(LOG, "a") as f:
    f.write(os.path.basename(sys.argv[0]) + " " + state + "\\n")
fcntl.flock(fd, fcntl.LOCK_EX)  # as a hook calling foremind: waits for the project lock
"""


def gate(p):
    review.events(p.root).append("gate_result", batch="shop.1", heads={r: p.head(r) for r in p.repo_ids}, path="x",
                                 sha256="0" * 64, verdict="pass", failing=[], pending=[])


def drafts(root) -> list:
    return sorted((state_dir(root) / "land").glob("shop.1-*/drafts.md"))


def last_result(root) -> dict:
    return [e for e in review.all_events(root) if e["type"] == "land_result"][-1]


class HooksTest(unittest.TestCase):
    """Main checkouts api and web on the target (merge --ff-only, reset --keep back), app on a side branch (update-ref
    both ways); every repo's post-merge, reference-transaction and post-index-change take the project lock, as a hook
    calling foremind would; land.commands touch api's and web's README, so the final check's status writes their index
    back. `foremind land` runs as a process of its own: a deadlock is a timeout, not a hung test run."""

    def setUp(self):
        self.p = p = Project(self, ("api", "app", "web"))
        p.batch(state="delivered")
        for r in p.repo_ids:
            p.commit(r)
        sh(p.root / "app", "git", "checkout", "-q", "-b", "side")
        (p.root / "foremind.toml").write_text("".join(f'[[repos]]\nid = "{r}"\npath = "{r}"\n' for r in p.repo_ids)
                                              + "".join(f'[delivery.repo.{r}]\ntarget_branch = "main"\n'
                                                        for r in p.repo_ids))
        (p.tmp / "config").mkdir(exist_ok=True)
        touch = f"touch '{p.root / 'api' / 'README.md'}' '{p.root / 'web' / 'README.md'}'"
        (p.tmp / "config" / "config.toml").write_text(f'[land]\ncommands = ["{touch}"]\n')
        gate(p)
        self.log = p.tmp / "hooks.log"
        lock = state_dir(p.root) / "state.lock"
        for r in p.repo_ids:
            for name in ("post-merge", "reference-transaction", "post-index-change"):
                f = p.root / r / ".git" / "hooks" / name
                f.parent.mkdir(exist_ok=True)
                f.write_text(f"#!{sys.executable}\nLOCK, LOG = {str(lock)!r}, {str(self.log)!r}\n{HOOK}")
                f.chmod(0o755)
            sh(p.root / r, "git", "update-ref", "refs/heads/probe", "main")  # the hooks do run here
            sh(p.root / r, "git", "update-ref", "-d", "refs/heads/probe")
            os.utime(p.root / r / "README.md")
            sh(p.root / r, "git", "status", "--porcelain")
        for name in ("reference-transaction", "post-index-change"):
            self.assertIn(f"{name} free", self.log.read_text())
        self.log.unlink()
        self.start = self.tips()

    def tips(self):
        return {r: sh(self.p.root / r, "git", "rev-parse", "main") for r in self.p.repo_ids}

    def ran(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def land(self):
        try:
            r = subprocess.run([sys.executable, "-m", "foremind", "land", "shop.1"], cwd=REPO, capture_output=True,
                               text=True, timeout=LIMIT, env={**os.environ, "PYTHONPATH": str(REPO)})
        except subprocess.TimeoutExpired:
            self.fail(f"land still running after {LIMIT} s; hooks run: {self.ran()!r}")
        self.assertNotIn("held", self.ran())  # no hook ran under the project lock
        self.assertNotIn("post-merge", self.ran())  # the main checkouts' own post-merge does not run either
        return r

    def test_moves_with_hooks_off(self):
        r = self.land()
        self.assertEqual(r.returncode, 0, r.stderr)
        new = self.tips()
        for rid in self.p.repo_ids:
            self.assertEqual(sh(self.p.root / rid, "git", "rev-parse", f"{new[rid]}^1", f"{new[rid]}^2").split(),
                             [self.start[rid], self.p.head(rid)])
        self.assertTrue((self.p.root / "api" / "src" / "app.py").exists())  # api's checkout moved with its branch
        self.assertEqual(last_result(self.p.root)["merges"], new)

    def test_rollback_with_hooks_off(self):
        (self.p.root / "web" / ".git" / "index.lock").touch()  # web, moved last, fails; app and api go back
        r = self.land()
        self.assertEqual(r.returncode, 1)
        for line in ("  api：已退回起点", "  app：已退回起点", "  web：移动时出错"):
            self.assertIn(line, r.stderr)
        self.assertEqual(self.tips(), self.start)
        self.assertFalse((self.p.root / "api" / "src").exists())  # reset --keep took api's checkout back
        self.assertEqual(last_result(self.p.root)["outcome"], "move_failed")


class SingleTest(test_land.Base):
    """One repo: already in its target (landed before, merged by hand), still checked, no new commit or drafts."""

    def drafts(self):
        return drafts(self.p.root)

    def test_second_land_is_already_merged(self):
        self.gate()
        new, _ = self.land()
        [d] = self.drafts()
        text = d.read_text()
        again, out = self.land()
        self.assertEqual((again, self.tip()), (new, new))
        r = self.results()[-1]
        self.assertEqual({k: r[k] for k in land.RESULT},
                         {"heads": {"api": self.p.head()}, "outcome": "merged", "kind": "already_merged",
                          "reason": None, "failed": None, "conflicts": None, "out_dir": None, "merges": {"api": new},
                          "drafts": None})
        self.assertEqual((self.drafts(), d.read_text()), ([d], text))
        self.assertEqual(len(list((state_dir(self.p.root) / "land").iterdir())), 1)  # no command ran again
        self.assertIn(f"shop.1: already in main at {new}", out)
        self.assertNotIn("合入草稿", out)

    def test_merged_by_hand(self):
        self.gate()
        for how in ("--ff-only", "--no-ff"):
            sh(self.repo, "git", "merge", "-q", how, "--no-edit", "fm/shop.1")
            tip = self.tip()
            new, _ = self.land()  # no HEAD^2 asked of a merge that made nothing
            self.assertEqual((new, self.tip(), self.outcome()), (tip, tip, ("merged", "already_merged")))
            sh(self.repo, "git", "reset", "-q", "--keep", self.start)
        self.assertFalse((state_dir(self.p.root) / "land").exists())  # no command, no drafts
        self.assertEqual(self.leftovers(), [])

    def test_already_merged_is_still_checked(self):
        sh(self.repo, "git", "merge", "-q", "--ff-only", "fm/shop.1")
        self.start = self.tip()
        self.refused("没有通过的门禁结果")
        self.gate()
        self.p.write_header("shop.1", state="in_review")
        self.refused("land 只合 delivered")
        self.p.write_header("shop.1", state="delivered")
        (self.repo / "README.md").write_text("edited\n")
        self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "dirty"))
        sh(self.repo, "git", "checkout", "-q", "README.md")
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": "supervisor"}):
            self.refused("不满足自动合入的条件")
        self.assertEqual(self.outcome(), ("refused", "auto"))
        real = land.project_lock

        def moved(root):  # the target moves before the final check
            sh(self.repo, "git", "commit", "-q", "--allow-empty", "-m", "later")
            return real(root)
        with mock.patch.object(land, "project_lock", moved):
            self.assertIsInstance(self.attempt(), land.Stop)
        self.assertEqual(self.outcome(), ("refused", "moved"))
        self.assertIsNone(self.attempt())
        self.assertEqual(self.outcome(), ("merged", "already_merged"))
        self.assertEqual(self.results()[-1]["merges"], {"api": self.tip()})
        self.assertEqual(self.drafts(), [])

    def test_by_hand_then_by_the_supervisor_twice(self):
        self.gate()
        new, _ = self.land()
        self.authorize()
        for _ in range(2):
            self.assertIsNone(self.attempt())
            r = self.results()[-1]
            self.assertEqual((r["outcome"], r["kind"], r["by"], r["merges"]),
                             ("merged", "already_merged", "supervisor", {"api": new}))
        self.assertEqual((self.tip(), len(self.drafts())), (new, 1))

    def test_supervisor_then_by_hand(self):
        self.authorize()
        self.assertIsNone(self.attempt())
        new = self.tip()
        with mock.patch.dict(os.environ, {"FOREMIND_ROLE": ""}):
            self.assertIsNone(self.attempt())
        self.assertEqual((self.outcome(), self.results()[-1]["by"]), (("merged", "already_merged"), "user"))
        self.assertEqual((self.tip(), len(self.drafts())), (new, 1))


class MultiTest(unittest.TestCase):
    def setUp(self):
        self.p = p = Project(self, ("api", "app", "web"))
        p.batch(state="delivered")
        for r in p.repo_ids:
            p.commit(r)
        p.cfg["land.commands"] = ["true"]
        gate(p)

    def tips(self):
        return {r: sh(self.p.root / r, "git", "rev-parse", "main") for r in self.p.repo_ids}

    def land(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return land.land(self.p.root, "shop.1", self.p.cfg)

    def test_all_in_their_targets(self):
        api, app, web = (self.p.root / r for r in self.p.repo_ids)
        sh(api, "git", "merge", "-q", "--ff-only", "fm/shop.1")
        sh(app, "git", "checkout", "-q", "-b", "side")
        sh(app, "git", "update-ref", "refs/heads/main", "fm/shop.1")
        sh(web, "git", "merge", "-q", "--no-ff", "--no-edit", "fm/shop.1")
        before = self.tips()
        self.assertEqual(self.land(), before)
        r = last_result(self.p.root)
        self.assertEqual((r["outcome"], r["kind"], r["merges"], r["drafts"], r["out_dir"]),
                         ("merged", "already_merged", before, None, None))
        self.assertEqual(self.tips(), before)
        self.assertFalse((state_dir(self.p.root) / "land").exists())

    def test_partly_in_their_targets_then_again(self):
        web = self.p.root / "web"
        sh(web, "git", "merge", "-q", "--no-ff", "--no-edit", "fm/shop.1")
        by_hand = sh(web, "git", "rev-parse", "main")
        new = self.land()
        self.assertEqual((new["web"], last_result(self.p.root)["kind"]), (by_hand, None))
        for r in ("api", "app"):
            self.assertEqual(sh(self.p.root / r, "git", "rev-parse", f"{new[r]}^2"), self.p.head(r))
        [d] = drafts(self.p.root)
        self.assertEqual(self.land(), new)
        self.assertEqual(last_result(self.p.root)["kind"], "already_merged")
        self.assertEqual((self.tips(), drafts(self.p.root)), (new, [d]))


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class AutoTest(TickBase):
    """The land phase (L393): p.1 delivered, one repo, land.auto true, a passing gate on its heads, as in
    test_auto_land; land.commands true, so `foremind land` itself can run here."""

    def setUp(self):
        super().setUp()
        (self.tmp / "gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.test\n")
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[land]\nauto = true\ncommands = ["true"]\n')
        self.plan("p", {"state": "delivered"})
        (self.root / "foremind.toml").write_text('[[repos]]\nid = "main"\npath = "main"\ndefault_branch = "main"\n')
        self.repo = self.root / "main"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        self.wt = worktree.batch_dir(seat.project_slug(self.root, config.load(self.root)), "p.1") / "main"
        git(self.repo, "worktree", "add", "-q", "-b", "fm/p.1", str(self.wt), "main")
        self.change()

    def change(self):
        git(self.wt, "commit", "-q", "--allow-empty", "-m", "change")
        self.hd = {"main": git(self.wt, "rev-parse", "HEAD")}
        self.log.append("gate_result", batch="p.1", heads=self.hd, path="x", sha256="0" * 64, verdict="pass",
                        failing=[], pending=[])

    def by_hand(self):
        """`foremind land p.1` by the user, with real jobs for its commands."""
        with mock.patch.object(job, "start", REAL_START), mock.patch.object(job, "status", REAL_STATUS), \
                contextlib.redirect_stdout(io.StringIO()):
            land.land(self.root, "p.1")
        return self.events("land_result")[-1]

    def lands(self):
        return [c for c in self.jobs.cmds() if c[:1] == ["land"]]

    def ended(self, n, **fields):
        self.log.append("land_result", batch="p.1", heads=self.hd, job=self.jobs.started[n]["id"],
                        **{"kind": None, "reason": None, "failed": None, "out_dir": None, **fields})
        self.jobs.finish(n)

    def test_class(self):
        self.assertEqual(phase._class("merged", "already_merged"), "merged")

    def test_by_hand_on_these_heads_takes_no_job(self):
        r = self.by_hand()
        self.assertEqual((r["outcome"], r["heads"]), ("merged", self.hd))
        self.tick()
        sv.request_run(self.root, ["p.1"])  # `foremind run p.1` before reconcile
        self.tick(H)
        self.assertEqual((self.lands(), self.events("auto_land_outcome"), self.rec.sent), ([], [], []))

    def test_success_on_old_heads_holds_back_no_new_heads(self):
        self.by_hand()
        self.change()  # new heads, gate passed on them
        self.tick()
        [it] = self.events("sv_land", "intent")
        self.assertEqual((len(self.lands()), it["heads"]), (1, self.hd))

    def test_success_event_git_does_not_bear_out(self):
        self.log.append("land_result", batch="p.1", heads=self.hd, outcome="merged", kind=None,
                        merges={"main": "f" * 40}, job=None)  # the target never moved
        self.tick()
        self.assertEqual(len(self.lands()), 1)
        self.ended(0, outcome="merged", merges={"main": "f" * 40})  # nor does the job's
        self.tick()
        [o] = self.events("auto_land_outcome")
        self.assertEqual((o["outcome"], o["land_result"]), ("error", self.events("land_result")[-1]["id"]))
        self.assertIn("do not hold", o["reason"])
        self.tick(11 * 60)  # retried as an error
        self.assertEqual(len(self.lands()), 2)

    def test_merged_record_git_no_longer_bears_out_lands_again(self):
        self.tick()
        start = git(self.repo, "rev-parse", "main")
        git(self.repo, "merge", "-q", "--ff-only", "fm/p.1")
        self.ended(0, outcome="merged", merges={"main": self.hd["main"]})
        self.tick()
        self.assertEqual(self.events("auto_land_outcome")[0]["outcome"], "merged")
        self.tick(5 * 60)
        self.assertEqual(len(self.lands()), 1)  # waits for reconcile
        git(self.repo, "reset", "-q", "--keep", start)  # undone since
        self.tick(6 * 60)
        self.assertEqual(len(self.lands()), 1)  # as a retry: gate_retry_min after the last attempt
        self.tick(11 * 60)
        self.assertEqual(len(self.lands()), 2)

    def test_merged_job_whose_worktree_is_gone(self):
        self.tick()
        git(self.repo, "merge", "-q", "--ff-only", "fm/p.1")
        self.ended(0, outcome="merged", merges={"main": self.hd["main"]})
        git(self.repo, "worktree", "remove", "--force", str(self.wt))
        self.tick()
        self.assertEqual(self.events("auto_land_outcome")[0]["outcome"], "merged")  # the main checkout tells

    def test_job_already_merged_waits_for_reconcile(self):
        self.tick()
        git(self.repo, "merge", "-q", "--ff-only", "fm/p.1")
        self.ended(0, outcome="merged", kind="already_merged", merges={"main": self.hd["main"]})
        self.tick()
        [o] = self.events("auto_land_outcome")
        self.assertEqual((o["outcome"], o["kind"]), ("merged", "already_merged"))
        self.tick(H)
        self.assertEqual((len(self.lands()), self.rec.sent, self.events("auto_land_stopped")), (1, [], []))


if __name__ == "__main__":
    unittest.main()
