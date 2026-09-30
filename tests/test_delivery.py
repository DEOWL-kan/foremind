"""`foremind update` (DESIGN §7.6) on temp projects with real git: the target moved elsewhere (rebind), onto the same
lines (conflict), next to the batch's lines (patch-id differs, re-review), or not at all."""
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from foremind import carriers, delivery, gate, heartbeat, inbox, lock, review
from foremind.cli import main
from foremind.fsutil import file_lock
from foremind.paths import state_dir
from test_gate_fixture import Project, sh
from test_supervisor import Base as TickBase, M, iso

BIG = [f"line {i}\n" for i in range(1, 31)]
MERGE = 'git -C "$FOREMIND_REPO_PATH" merge -q --no-ff -m merge "$FOREMIND_HEAD"'
SEAT = "fm-shop-shop_1-2"


def edit(n, text, lines=BIG):
    return "".join(text if i == n else x for i, x in enumerate(lines, 1))


def lines(d):
    return (d / "src/big.py").read_text().splitlines(True)


class FakeCarrier:
    def __init__(self, alive, idle):
        self.st = carriers.SessionState(alive=alive, idle=idle)

    def read_state(self, session):
        return self.st


class Base(unittest.TestCase):
    def project(self, *, origin=False, cfg=None):
        """shop.1 changed line 25 of src/big.py, was reviewed and went through the gate (delivered at level done)."""
        p = Project(self, origin=origin)
        self.on_target(p, "src/big.py", "".join(BIG))  # before the batch branches off
        p.cfg.update({"gate.checks": ["true"], **(cfg or {})})
        p.batch()
        p.commit(path="src/big.py", text=edit(25, "line 25 by shop.1\n"))
        p.run_review()
        gate.run(p.root, "shop.1", p.cfg)
        return p

    def on_target(self, p, path, text, r="api"):
        d = p.root / r
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_text(text)
        sh(d, "git", "add", "-A")
        sh(d, "git", "commit", "-q", "-m", f"target: {path}")
        if sh(d, "git", "remote"):
            sh(d, "git", "push", "-q", "origin", "main")

    def update(self, p, bid="shop.1", by="user"):
        return delivery.update(p.root, bid, p.cfg, by=by)

    def header(self, p, bid="shop.1"):
        return review.load_batch(p.root, bid)

    def clean(self, p, bid="shop.1"):
        wt = p.wt(bid=bid)
        self.assertEqual(sh(wt, "git", "status", "--porcelain"), "")
        for d in ("rebase-merge", "rebase-apply", "MERGE_HEAD"):
            self.assertFalse((wt / sh(wt, "git", "rev-parse", "--git-path", d)).exists(), d)


class RebindTest(Base):
    def test_target_moved_elsewhere_in_the_same_file(self):
        for method in ("merge", "rebase"):
            with self.subTest(method):
                p = self.project(cfg={"delivery.repo.api.update_method": method})
                old, (n, _) = p.head(), review.receipts(p.root, "shop.1")[-1]
                self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
                tip = sh(p.root / "api", "git", "rev-parse", "main")
                res = self.update(p)
                self.assertEqual((res["outcome"], res["state"], p.state()), ("rebound", "approved", "approved"))
                new = p.head()
                self.assertNotEqual(new, old)
                parents = sh(p.wt(), "git", "log", "-1", "--format=%P").split()
                self.assertEqual(parents, [old, tip] if method == "merge" else [tip])
                self.assertNotIn("state_prior", self.header(p))
                # the rebound copy is the newest receipt, bound to the new head, from the heads its review started on
                m, path = review.receipts(p.root, "shop.1")[-1]
                r = json.loads(path.read_text())
                self.assertEqual((m, res["receipt"], r["round"]), (n + 1, path.name, n + 1))
                self.assertEqual((r["heads"], r["rebound_from"], r["verdict"]),
                                 ({"api": new}, {"api": old}, "approved"))
                ev = p.events("review_receipt")[-1]
                self.assertEqual((ev["path"], ev["rebound_from"]), (path.name, {"api": old}))
                moves = [(e.get("prior"), e["state"], e.get("reason")) for e in p.events("batch_state")][-2:]
                self.assertEqual(moves, [("delivered", "updating", "update"), ("updating", "approved", "rebound")])
                up = p.events("batch_updated")[-1]
                self.assertEqual((up["prior_heads"], up["heads"], up["outcome"]),
                                 ({"api": old}, {"api": new}, "rebound"))
                self.assertEqual(gate.bases(p.root, "shop.1", *self.pairs_heads(p)), {"api": tip})
                # the next gate reruns acceptance on the new head and delivers again
                runs = len(p.events("accept_run"))
                g = gate.run(p.root, "shop.1", p.cfg)
                self.assertEqual((g["verdict"], g["state"], len(p.events("accept_run"))),
                                 ("pass", "delivered", runs + 1))
                self.clean(p)

    def pairs_heads(self, p):
        pairs = review.batch_repos(p.root, self.header(p), p.cfg)
        return pairs, review.heads(pairs)

    def test_rebound_copy_drops_the_originals_reconciliation(self):  # it reconciled the original's predecessor
        p = self.project()
        latest = delivery._latest_receipt
        self.on_target(p, "src/big.py", edit(3, "moved\n", lines(p.root / "api")))
        with mock.patch.object(delivery, "_latest_receipt",
                               lambda *a: {**latest(*a), "resolved": ["fp1"], "unresolved": ["fp2"]}):
            res = self.update(p)
        r = json.loads(review.receipts(p.root, "shop.1")[-1][1].read_text())
        self.assertEqual((res["outcome"], r["verdict"]), ("rebound", "approved"))
        self.assertNotIn("resolved", r)
        self.assertNotIn("unresolved", r)

    def test_chained_rebind_keeps_the_reviewed_heads_and_rounds_stay_unique(self):
        p = self.project()
        old = p.head()
        for n, line in ((2, 3), (3, 5)):
            self.on_target(p, "src/big.py", edit(line, "moved\n", lines(p.root / "api")))
            self.assertEqual(self.update(p)["receipt"], f"shop.1.review.r{n}.json")
        r = json.loads(review.receipts(p.root, "shop.1")[-1][1].read_text())
        self.assertEqual(r["rebound_from"], {"api": old})  # the heads review_started saw, which the gate checks
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["state"], "delivered")
        p.commit(path="src/big.py", text=edit(25, "line 25 again\n", lines(p.wt())))
        self.assertEqual(p.run_review()["round"], 4)  # the next review never lands on a rebound receipt's number
        self.assertEqual(review.receipts(p.root, "shop.1")[-1][0], 4)

    def test_system_push_is_pushed_and_a_replaced_user_push_is_stale(self):
        for push in ("system", "user"):
            with self.subTest(push):
                p = self.project(origin=True, cfg={"delivery.repo.api.push_pr": push,
                                                   "delivery.repo.api.update_method": "rebase"})
                if push == "user":
                    sh(p.wt(), "git", "push", "-q", "origin", "fm/shop.1")  # the user pushed the reviewed head
                old = p.head()
                self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
                hook = p.root / "api" / sh(p.root / "api", "git", "rev-parse", "--git-path", "hooks") / "pre-push"
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text("#!/bin/sh\nexit 1\n")
                hook.chmod(0o755)  # hooks are off for the update's own push
                self.assertEqual(self.update(p)["outcome"], "rebound")
                remote = review.remote_head(p.wt(), "origin", "fm/shop.1")
                self.assertEqual(remote, p.head() if push == "system" else old)
                g = gate.run(p.root, "shop.1", p.cfg)  # a rebased head the user has not pushed yet is not "diverged"
                self.assertEqual((g["verdict"], g["state"]), ("pass", "delivered"))

    def test_a_merged_then_b_updated_and_merged(self):
        """Two batches branched off one target: after A merges, B is behind and cannot merge until updated."""
        p = Project(self)
        self.on_target(p, "src/big.py", "".join(BIG))
        p.cfg.update({"gate.checks": ["true"], "delivery.level": "merge_dev", "delivery.repo.api.merge_command": MERGE})
        for bid, line in (("shop.1", 5), ("shop.2", 25)):
            p.batch(bid)
            p.commit(path="src/big.py", text=edit(line, f"line {line} by {bid}\n"), bid=bid)
        for bid in ("shop.1", "shop.2"):
            p.run_review(bid=bid, session=f"fm-shop-{bid}-1")
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["state"], "merged")
        g = gate.run(p.root, "shop.2", p.cfg)
        self.assertEqual(p.state("shop.2"), "approved")
        self.assertEqual([c["name"] for c in g["checks"] if not c["ok"]], ["up_to_date"])
        self.assertIn("`foremind update shop.2`", next(c for c in g["checks"] if c["name"] == "up_to_date")["detail"])
        self.assertEqual(self.update(p, "shop.2")["outcome"], "rebound")
        g = gate.run(p.root, "shop.2", p.cfg)
        self.assertEqual((g["verdict"], g["state"]), ("pass", "merged"))
        target = (p.root / "api" / "src/big.py").read_text()
        self.assertIn("line 5 by shop.1", target)
        self.assertIn("line 25 by shop.2", target)


class PartlyMergedTest(Base):
    def test_a_merged_repo_is_skipped_and_the_others_updated(self):  # m2a.8 r2
        p = Project(self, ("api", "app"))
        for r in ("api", "app"):
            self.on_target(p, "src/big.py", "".join(BIG), r)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        for r in ("api", "app"):
            p.commit(r, path="src/big.py", text=edit(25, f"line 25 by shop.1 in {r}\n"))
        p.run_review()
        self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["state"], "delivered")
        old = {r: p.head(r) for r in ("api", "app")}
        base = p.events("review_requested")[-1]["bases"]
        for r in ("api", "app"):
            self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"), r)
        sh(p.root / "app", "git", "merge", "-q", "--no-ff", "-m", "user merges app", "fm/shop.1")  # behind, yet merged
        res = self.update(p)
        self.assertEqual((res["outcome"], res["merged"], list(res["methods"]), p.state()),
                         ("rebound", ["app"], ["api"], "approved"))
        self.assertNotEqual(p.head("api"), old["api"])
        self.assertEqual(p.head("app"), old["app"])  # left as it is
        up = p.events("batch_updated")[-1]
        self.assertEqual(up["bases"]["app"], base["app"])  # not the merge-base with a target that holds its head
        self.assertEqual(up["bases"]["api"], sh(p.root / "api", "git", "rev-parse", "main"))
        p.cfg.update({"delivery.level": "merge_dev", "delivery.repo.api.merge_command": MERGE})
        g = gate.run(p.root, "shop.1", p.cfg)  # app already merged: only api merges
        self.assertEqual((g["verdict"], g["state"]), ("pass", "merged"))
        self.assertEqual([e["repo"] for e in p.events("merge") if e["phase"] == "result"], ["api"])


class OtherOutcomeTest(Base):
    def test_target_not_moved(self):
        p = self.project()
        n = len(p.events("batch_state"))
        self.assertEqual(self.update(p)["outcome"], "current")
        self.assertEqual((p.state(), len(p.events("batch_state"))), ("delivered", n))
        sh(p.root / "api", "git", "merge", "-q", "--no-ff", "-m", "user merges", "fm/shop.1")  # level done: the user's
        self.assertEqual(self.update(p)["outcome"], "merged")  # behind, but not to be bounced back to the seat
        self.assertEqual((p.state(), len(p.events("batch_state"))), ("delivered", n))

    def test_conflict_is_aborted_and_goes_to_the_seat(self):
        for method in ("merge", "rebase"):
            with self.subTest(method):
                p = self.project(cfg={"delivery.repo.api.update_method": method})
                old = p.head()
                self.on_target(p, "src/big.py", edit(25, "line 25 on the target\n"))
                res = self.update(p)
                self.assertEqual((res["outcome"], res["conflicts"], p.state()),
                                 ("conflict", {"api": ["src/big.py"]}, "changes_requested"))
                self.assertEqual(p.head(), old)
                self.clean(p)
                body = (state_dir(p.root) / "batches" / "shop.1.md").read_text()
                self.assertIn("更新分支有冲突", body)
                self.assertIn(f"`git {method} refs/heads/main`", body)
                self.assertNotIn("owns_paths 之外", body)
                self.assertNotIn("state_prior", self.header(p))
                self.assertEqual(p.events("batch_state")[-1]["reason"], "update_conflict")
                self.assertEqual(p.events("batch_updated"), [])

    def test_conflict_outside_owns_paths_goes_to_the_holder(self):
        p = Project(self)
        p.cfg["gate.checks"] = ["true"]
        p.batch()
        p.commit(path="README.md", text="api, by the batch\n")  # outside api:src/*
        p.run_review()
        gate.run(p.root, "shop.1", p.cfg)  # owns_paths fails: stays approved
        self.assertEqual(p.state(), "approved")
        lock.acquire(p.root, "shop.1", SEAT)
        self.on_target(p, "README.md", "api, on the target\n")
        with mock.patch.object(carriers, "get", return_value=FakeCarrier(True, True)):
            res = self.update(p)
        self.assertEqual((res["outcome"], res["state"], p.state()), ("conflict", "running", "running"))
        text = inbox.pending_messages(SEAT, root=p.root)[-1].text
        self.assertIn("['api:README.md']", text)
        self.assertIn("foremind decide --new", text)

    def test_patch_changed_next_to_the_batch_lines_asks_for_a_review(self):
        p = self.project()
        old = p.head()
        sh(p.root / "api", "git", "config", "diff.context", "0")  # patch_id pins its own context
        self.on_target(p, "src/big.py", edit(23, "line 23 on the target\n"))  # clean merge, other context lines
        res = self.update(p)
        self.assertEqual((res["outcome"], p.state()), ("rereview", "changes_requested"))
        self.assertIn("patch-id", res["why"])
        self.assertNotEqual(p.head(), old)  # the update stays, for the new review
        self.assertEqual(len(review.receipts(p.root, "shop.1")), 1)
        self.assertEqual(p.events("batch_updated")[-1]["outcome"], "rereview")
        self.assertEqual(review.request(p.root, "shop.1", p.cfg)["state"], "review_ready")

    def test_a_receipt_the_program_did_not_write_is_never_rebound(self):
        p = self.project()
        path = review.receipts(p.root, "shop.1")[-1][1]
        path.write_text(path.read_text().replace('"approved"', '"approved" '))  # same receipt, other bytes
        self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
        res = self.update(p)
        self.assertEqual((res["outcome"], p.state()), ("rereview", "changes_requested"))
        self.assertIn("对不上", res["why"])
        self.assertEqual(len(review.receipts(p.root, "shop.1")), 1)

    def test_an_existing_receipt_file_is_never_written_over(self):
        p = self.project()
        old, (n, path) = p.head(), review.receipts(p.root, "shop.1")[-1]
        before = path.read_bytes()
        self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
        with mock.patch.object(review, "next_round", return_value=n), \
                self.assertRaisesRegex(review.FlowError, "no event numbers it"):
            self.update(p)
        self.assertEqual((path.read_bytes(), p.head(), p.state()), (before, old, "approved"))

    def test_receipt_not_bound_to_the_pre_update_head_asks_for_a_review(self):
        p = self.project()
        p.commit(path="src/big.py", text=edit(25, "line 25 after approval\n"))  # never reviewed
        self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
        res = self.update(p)
        self.assertEqual((res["outcome"], p.state()), ("rereview", "changes_requested"))
        self.assertIn("不是更新前的 head", res["why"])


class GuardTest(Base):
    def test_busy_is_not_a_failure(self):
        p = self.project()
        self.on_target(p, "src/big.py", edit(3, "moved\n"))
        with file_lock(state_dir(p.root) / "batches" / "shop.1.gate.lock"):
            self.assertEqual(self.update(p)["outcome"], "busy")
        lock.acquire(p.root, "shop.1", SEAT)
        for alive, idle, tool, busy in ((True, False, False, True), (True, True, True, True), (None, None, False, True),
                                        (True, True, False, False)):
            heartbeat.update(p.root, SEAT, **({"open_tool": "t1"} if tool else {"close_tool": "t1"}))
            with self.subTest(alive=alive, idle=idle, tool=tool), \
                    mock.patch.object(carriers, "get", return_value=FakeCarrier(alive, idle)):
                self.assertEqual(self.update(p)["outcome"], "busy" if busy else "rebound")
        self.assertEqual(p.state(), "approved")
        self.assertEqual(self.update(p)["outcome"], "current")  # no carrier asked: nothing to update

    def test_ended_seat_or_user_holder(self):
        for holder in (SEAT, lock.USER):
            with self.subTest(holder):
                p = self.project()
                lock.acquire(p.root, "shop.1", holder)
                heartbeat.update(p.root, SEAT, open_tool="t1")  # a stale heartbeat of a session that is gone
                self.on_target(p, "src/big.py", edit(3, "moved\n"))
                with mock.patch.object(carriers, "get", return_value=FakeCarrier(False, None)):
                    self.assertEqual(self.update(p)["outcome"], "rebound")

    def test_only_approved_or_delivered(self):
        p = Project(self)
        p.batch()
        self.assertEqual(self.update(p)["outcome"], "busy")  # running

    def test_failure_rolls_back_to_approved(self):
        p = self.project(cfg={"delivery.repo.api.update_method": "rebase"})
        old = p.head()
        self.on_target(p, "src/big.py", edit(3, "moved\n"))
        with mock.patch.object(gate, "patch_id", side_effect=review.FlowError("boom")), \
                self.assertRaisesRegex(review.FlowError, "boom"):
            self.update(p)
        self.assertEqual((p.head(), p.state()), (old, "approved"))
        self.assertNotIn("state_prior", self.header(p))
        self.clean(p)
        self.assertEqual(self.update(p)["outcome"], "rebound")

    def test_rollback_keeps_an_edit_made_meanwhile(self):
        p = self.project()
        self.on_target(p, "src/big.py", edit(3, "moved\n"))

        def seat_writes(*_):
            (p.wt() / "src/big.py").write_text("the seat's edit\n")
            raise review.FlowError("boom")

        with mock.patch.object(gate, "patch_id", side_effect=seat_writes), \
                self.assertRaisesRegex(review.FlowError, "local changes"):
            self.update(p)
        self.assertEqual(((p.wt() / "src/big.py").read_text(), p.state()), ("the seat's edit\n", "updating"))

    def test_a_killed_run_is_picked_up(self):
        p = self.project()
        review.set_state(p.root, "shop.1", "updating", expect=("delivered",))  # killed before touching git
        self.assertEqual(self.header(p)["state_prior"], "delivered")
        self.on_target(p, "src/big.py", edit(3, "moved\n"))
        self.assertEqual(self.update(p)["outcome"], "rebound")
        self.assertEqual([e.get("reason") for e in p.events("batch_state")][-3:],
                         ["update_resumed", "update", "rebound"])
        review.set_state(p.root, "shop.1", "updating", expect=("approved",))
        p.commit(path="src/big.py", text=edit(25, "line 25 moved while updating\n",
                                              lines(p.wt())))
        res = self.update(p)
        self.assertEqual((res["outcome"], p.state()), ("rereview", "changes_requested"))
        self.assertEqual(p.events("batch_state")[-1]["reason"], "update_interrupted")

    def test_bad_update_method(self):
        p = self.project(cfg={"delivery.repo.api.update_method": "squash"})
        self.on_target(p, "src/big.py", edit(3, "moved\n"))
        with self.assertRaisesRegex(review.FlowError, "neither rebase nor merge"):
            self.update(p)
        self.assertEqual(p.state(), "delivered")


TOML = ('gate.checks = ["true"]\n\n[[repos]]\nid = "api"\npath = "api"\n\n'
        '[delivery.repo.api]\ntarget_branch = "main"\n')


class CliTest(Base):
    def test_user_and_supervisor_update_through_the_command(self):  # m2a.8 r2, acceptance
        """`python -P -m foremind update` as the supervisor's job runs it and as the user runs it: no FOREMIND_*
        variable at all for the user (HOME points the worktree root at the test's), the job's for the supervisor."""
        for who, extra in (("user", {}), ("supervisor", {"FOREMIND_ROLE": "supervisor", "FOREMIND_SESSION": "",
                                                          "FOREMIND_BATCH": ""})):
            with self.subTest(who):
                p = self.project()
                (p.root / "foremind.toml").write_text(TOML)
                old = p.head()
                self.on_target(p, "src/big.py", edit(3, "line 3 on the target\n"))
                home = p.tmp / "home"
                (home / ".local" / "share" / "foremind").mkdir(parents=True)
                (home / ".local" / "share" / "foremind" / "wt").symlink_to(p.tmp / "wt")
                env = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_")}
                env.update(HOME=str(home), PYTHONPATH=str(Path(delivery.__file__).resolve().parent.parent), **extra)
                if who == "supervisor":
                    env["FOREMIND_PROJECT"] = str(p.root)
                r = subprocess.run([sys.executable, "-P", "-m", "foremind", "update", "shop.1"], cwd=p.root, env=env,
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(json.loads(r.stdout)["outcome"], "rebound")
                self.assertEqual((p.state(), p.events("batch_updated")[-1]["by"]), ("approved", who))
                self.assertNotEqual(p.head(), old)
                self.assertEqual(gate.run(p.root, "shop.1", p.cfg)["state"], "delivered")

    def run_cli(self, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["update", "shop.1"])
        return code, out.getvalue(), err.getvalue()

    def test_seats_are_refused(self):
        p = self.project()
        (p.root / "foremind.toml").write_text(TOML)
        for env in ({"FOREMIND_SESSION": "fm-x-1", "FOREMIND_ROLE": "seat"}, {"FOREMIND_SESSION": "fm-x-1"},
                    {"FOREMIND_ROLE": "seat"}):
            code, _, err = self.run_cli(env)
            self.assertEqual(code, 2)
            self.assertIn("never a seat", err)
        self.assertEqual(self.run_cli({"FOREMIND_SESSION": "fm-c-1", "FOREMIND_ROLE": "controller"})[0], 0)  # current
        self.on_target(p, "src/big.py", edit(25, "line 25 on the target\n"))
        code, out, _ = self.run_cli({"FOREMIND_SESSION": "fm-c-1", "FOREMIND_ROLE": "controller"})
        self.assertEqual((code, json.loads(out)["outcome"]), (1, "conflict"))
        self.assertEqual(p.events("changes_requested_by")[-1]["by"], "fm-c-1")


class UpdatePhaseTest(TickBase):
    """phases/update.py in the supervisor's pass (fake jobs): one `foremind update` per set of heads."""
    H1, H2 = {"main": "a" * 40}, {"main": "b" * 40}

    def setUp(self):
        super().setUp()
        self.plan("p", {"state": "approved"})

    def merge_dev(self, on=True):
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n' + ('[delivery]\nlevel = "merge_dev"\n' if on else ""))

    def gate_result(self, heads, failing=("up_to_date",), pending=(), at=0):
        with mock.patch("foremind.events._now", return_value=iso(self.now + at)):
            self.log.append("gate_result", batch="p.1", heads=heads, path="p.1.gate.x.json", sha256="0" * 64,
                            verdict="fail" if failing else "pass", failing=list(failing), pending=list(pending))

    def told(self, word):
        return [t for t, _, _ in self.rec.sent if word in t]

    def updates(self):
        return [j for j in self.jobs.started if self.jobs.cmds()[self.jobs.started.index(j)] == ["update", "p.1"]]

    def test_once_per_heads_as_the_supervisor(self):
        self.gate_result(self.H1)
        self.tick()
        self.assertEqual(self.updates(), [], "level done: nothing to merge")
        self.merge_dev()
        self.gate_result(self.H1, ("up_to_date", "ci:main"))
        self.tick(M)
        self.assertEqual(self.updates(), [], "not up_to_date alone")
        self.gate_result(self.H1)
        self.tick(2 * M)
        self.tick(3 * M)  # still running: not again
        [j] = self.updates()
        self.assertEqual((j["env"]["FOREMIND_ROLE"], j["env"]["FOREMIND_SESSION"], j["timeout_s"]),
                         ("supervisor", "", 1800))
        self.assertEqual(self.events("sv_update", "intent")[0]["heads"], self.H1)
        self.jobs.finish(self.jobs.started.index(j), 0, '{"batch": "p.1", "outcome": "rebound"}\n')
        self.tick(4 * M)
        self.gate_result(self.H1)
        self.tick(5 * M)
        self.assertEqual(len(self.updates()), 1, "these heads had their update")
        self.gate_result(self.H2)
        self.tick(6 * M)
        self.assertEqual(len(self.updates()), 2)
        self.assertEqual(self.events("update_busy"), [])

    def test_busy_is_tried_again_after_the_next_gate_result(self):
        self.merge_dev()
        self.gate_result(self.H1)
        self.tick()
        self.jobs.finish(0, 0, '{"batch": "p.1", "outcome": "busy", "why": "seat fm-x is at work"}\n')
        self.tick(M)
        self.tick(2 * M)
        self.assertEqual((len(self.updates()), self.events("update_busy")[0]["why"]), (1, "seat fm-x is at work"))
        self.gate_result(self.H1)
        self.tick(3 * M)
        self.assertEqual(len(self.updates()), 2)

    def test_a_failed_update_is_told_once_a_conflict_is_not(self):
        self.merge_dev()
        self.gate_result(self.H1)
        self.tick()
        self.jobs.finish(0, 1, '{"batch": "p.1", "outcome": "conflict"}\n')  # sent to the seat
        self.tick(M)
        self.gate_result(self.H2)
        self.tick(2 * M)
        self.jobs.finish(1, 2)
        self.tick(3 * M)
        self.tick(4 * M)
        self.assertEqual([t for t, _, _ in self.rec.sent], ["p.1 自动更新失败"])

    def test_blocked_starts_nothing_but_tells_about_ci(self):
        self.merge_dev()
        self.decision("Q-1", ["p.1"])  # every unfinished batch waits on the user
        self.gate_result(self.H1)
        self.log.append("ci_pending_long", dedupe_id="ci_pending_long:p.1:main:" + "a" * 40, batch="p.1", repo="main",
                        head="a" * 40, since="2026-01-01T00:00:00+00:00")
        self.tick()
        self.tick(M)
        self.assertEqual(self.updates(), [])
        self.assertEqual(self.told("CI"), ["p.1 CI 久等"])

    def test_ci_pending_past_the_limit_is_told_once_per_head(self):  # r1 #1: one gate run per receipt in_review
        self.set_state("p.1", "in_review")
        ci = ("ci:main",)
        self.gate_result(self.H1, ci, ci)
        self.tick(119 * M)
        self.gate_result(self.H1, ci, ci, at=119 * M)  # a later result keeps the first one's time
        self.tick(119 * M)
        self.assertEqual(self.events("ci_pending_long"), [])
        self.tick(121 * M)
        self.tick(122 * M)
        [e] = self.events("ci_pending_long")
        self.assertEqual((e["batch"], e["repo"], e["head"], e["since"]), ("p.1", "main", "a" * 40, iso(self.now)))
        self.assertEqual(self.told("CI"), ["p.1 CI 久等"])
        self.gate_result(self.H2, ci, ci, at=123 * M)  # a new head starts over
        self.tick(124 * M)
        self.gate_result(self.H2, (), at=200 * M)  # green before the limit
        self.tick(250 * M)
        self.set_state("p.1", "running")  # a pending result the batch has moved past
        self.gate_result(self.H2, ci, ci, at=260 * M)
        self.tick(500 * M)
        self.assertEqual((len(self.events("ci_pending_long")), self.told("CI")), (1, ["p.1 CI 久等"]))

    def test_a_merge_queue_wait_past_the_limit_is_told_once(self):  # r1 #3: a dropped PR still reads as queued
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n\n[gate]\nci_pending_max_min = 30\n')
        self.set_state("p.1", "delivered")
        self.gate_result(self.H1, ())
        self.log.append("merge_queued", batch="p.1", repo="main", head="a" * 40)
        self.tick(29 * M)
        self.assertEqual(self.told("合入队列"), [])
        self.tick(31 * M)
        self.tick(32 * M)
        self.assertEqual(self.told("合入队列"), ["p.1 合入队列久等"])

    def test_reviews_stopped_on_timeouts_are_told_once(self):  # r2 #2; m2c.7: one P1, not two (m2b.6 r3)
        self.set_state("p.1", "failed")
        self.log.append("batch_state", batch="p.1", prior="in_review", state="failed", reason="review_timeouts",
                        failures=3, heads=self.H1)
        self.tick()
        self.tick(M)
        [(title, body, prio)] = [x for x in self.rec.sent if "p.1" in x[0]]
        self.assertEqual((title, prio), ("p.1 连续失败已停", "P1"))
        self.assertIn("连续 3 次超时", body)
        self.assertIn("oneshot.timeout_min", body)
        self.assertIn("foremind run p.1", body)


if __name__ == "__main__":
    unittest.main()
