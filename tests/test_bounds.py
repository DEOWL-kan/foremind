"""m2c.6: the bounds phase (supervisor/phases/bounds.py, REQ-11 [防对抗])."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from unittest import mock

from foremind import lock, review
from foremind.supervisor import tick as sv
from foremind.supervisor.phases import bounds
from test_supervisor import M, Base

S = "fm-p-1"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


class BoundsBase(Base):
    def setUp(self):
        super().setUp()
        bounds._REMOTE_AT.clear()
        bounds._MIDWAY.clear()
        git(self.root, "init", "-q", "-b", "main")
        self.commit(self.root, "README")
        (self.root / ".foremind" / "config.toml").write_text(
            '[[repos]]\nid = "main"\npath = "."\ndefault_branch = "main"\n')

    def commit(self, cwd, *paths):
        for p in paths:
            (cwd / p).parent.mkdir(parents=True, exist_ok=True)
            (cwd / p).write_text(p + "\n")
        git(cwd, "add", "--", *paths)
        git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
        return git(cwd, "rev-parse", "HEAD")

    def seat(self, bid, owns, *, start="main", holder=S, state="running", **kw):
        """A plan of one batch with a seat working in its own worktree (seat_opened names it)."""
        self.plan(bid.split(".")[0], {"owns_paths": owns, **kw})
        wt = self.tmp / "wt" / bid / "main"
        git(self.root, "worktree", "add", "-q", "-b", f"fm/{bid}", str(wt), start)
        self.set_state(bid, state)
        lock.acquire(self.root, bid, holder)
        self.log.append("seat_opened", batch=bid, session=holder, successor=False, carrier="fake",
                        worktrees={"main": str(wt)}, starts={"main": git(wt, "rev-parse", "HEAD")})
        self.beat(holder, bid, self.now)
        return wt

    def found(self, kind="worktree"):
        return [e for e in self.events("bounds_violation") if e["kind"] == kind]

    def told(self, word):
        return [x for x in self.rec.sent if word in x[0]]


class WorktreeTest(BoundsBase):
    def test_outside_owns_committed_and_untracked(self):
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py", "other.py")
        (wt / "notes.txt").write_text("n\n")
        self.commit(self.root, "later.py")  # the target moving on is no change of the batch
        self.tick()
        [e] = self.found()
        self.assertEqual(e["batch"], "p.1")
        self.assertEqual(e["paths"], [{"path": "main:notes.txt", "why": "owns_paths"},
                                      {"path": "main:other.py", "why": "owns_paths"}])
        [msg] = self.pending(S)
        self.assertIn("main:other.py", msg.text)
        self.assertIn("foremind decide --new", msg.text)
        [(title, body, prio)] = self.told("越界")
        self.assertEqual((title, prio), ("p.1 越界", "P1"))
        self.assertNotIn("other.py", body)  # §8.7: no paths in a notice
        self.tick(M)
        self.assertEqual((len(self.found()), len(self.events("sv_say", "result")), len(self.told("越界"))), (1, 1, 1))
        (wt / "more.txt").write_text("m\n")  # a new fact: a new fingerprint
        self.tick(2 * M)
        self.assertEqual(len(self.found()), 2)
        self.assertEqual(len(self.told("越界")), 2)

    def test_within_owns_is_quiet(self):
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py")
        (wt / "src" / "b.py").write_text("b\n")
        self.tick()
        self.assertEqual(self.events("bounds_violation") + self.events("bounds_error"), [])
        self.assertEqual(self.pending(S), [])

    def test_manifest_migration_and_system_files_need_an_exemption(self):
        wt = self.seat("p.1", ["main:*"])
        self.commit(wt, "package.json", "db/migrations/001.sql", "AGENTS.md", "src/a.py")
        self.tick()
        [e] = self.found()
        self.assertEqual(e["paths"], [{"path": "main:AGENTS.md", "why": "#22"},
                                      {"path": "main:db/migrations/001.sql", "why": "#7"},
                                      {"path": "main:package.json", "why": "#4"}])
        ex = self.root / ".foremind" / "exemptions" / "Q-1.json"
        ex.parent.mkdir()
        until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        ex.write_text(json.dumps({"batch": "p.1", "category": 4, "match": {"paths": ["main:package.json"]},
                                  "expires_at": until}))
        self.tick(M)
        self.assertEqual(self.found()[-1]["paths"], [{"path": "main:AGENTS.md", "why": "#22"},
                                                     {"path": "main:db/migrations/001.sql", "why": "#7"}])

    def test_outside_owns_and_a_hit_both_named(self):
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "requirements.txt")
        self.tick()
        self.assertEqual(self.found()[0]["paths"], [{"path": "main:requirements.txt", "why": "owns_paths、#4"}])

    def test_stacked_on_an_approved_upstream(self):
        """Its upstream's approved head counts as a base too: the upstream's files are not this batch's."""
        self.plan("u", {"owns_paths": ["main:up"]})
        git(self.root, "checkout", "-q", "-b", "fm/u.1")
        up = self.commit(self.root, "up/x.py")
        git(self.root, "checkout", "-q", "main")
        (self.root / ".foremind" / "batches" / "u.1.review.r1.json").write_text(
            json.dumps({"verdict": "approved", "heads": {"main": up}}))
        wt = self.seat("p.1", ["main:src/"], start=up, depends_on=["u.1"])
        self.commit(wt, "src/a.py")
        self.tick()
        self.assertEqual(self.events("bounds_violation") + self.events("bounds_error"), [])

    def test_a_user_held_batch_is_checked_too_finished_ones_not(self):
        wt = self.seat("p.1", ["main:src/"], holder=lock.USER)
        self.commit(wt, "other.py")
        wt2 = self.seat("q.1", ["main:src/"], state="delivered")
        self.commit(wt2, "other.py")
        self.tick()
        [e] = self.found()
        self.assertEqual((e["batch"], e["paths"]), ("p.1", [{"path": "main:other.py", "why": "owns_paths"}]))
        self.assertEqual(self.events("sv_say"), [])  # the user has no inbox
        self.assertEqual(len(self.told("越界")), 1)
        self.assertEqual(self.events("bounds_error"), [])

    def test_ignored_system_files_count_other_ignored_files_do_not(self):
        (self.root / ".gitignore").write_text(".foremind/\nbuild/\nCLAUDE.local.md\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        wt = self.seat("p.1", ["main:src/"])
        for p in (".foremind/config.toml", ".foremind/roles/seat.md", "CLAUDE.local.md", "build/CLAUDE.md",
                  "build/package.json", "build/out.js"):
            (wt / p).parent.mkdir(parents=True, exist_ok=True)
            (wt / p).write_text("x\n")
        self.tick()
        [e] = self.found()
        self.assertEqual(e["paths"], [{"path": f"main:{p}", "why": "#22"} for p in (
            ".foremind/config.toml", ".foremind/roles/seat.md", "CLAUDE.local.md", "build/CLAUDE.md")])

    def test_a_system_name_linking_elsewhere_counts(self):  # m2d.7 ⑤: by the link's own path, not only its target
        (self.root / ".gitignore").write_text(".foremind/\nCLAUDE.local.md\n")
        git(self.root, "add", ".gitignore")
        git(self.root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        wt = self.seat("p.1", ["main:src/"])
        (wt / "src").mkdir()
        (wt / "src" / "notes.txt").write_text("n\n")
        (wt / "CLAUDE.local.md").symlink_to("src/notes.txt")
        self.tick()
        [e] = self.found()
        self.assertEqual(e["paths"], [{"path": "main:CLAUDE.local.md", "why": "#22"}])

    def test_an_interactive_rebase_below_the_fork_is_not_the_batch(self):  # m2d.7 item 8 (m2c.6 r5)
        """`rebase -i HEAD~2` onto below the fork, stopped at an `edit` of the target's commit (fast-forwarded onto
        HEAD): that commit's later.py is not the batch's."""
        self.commit(self.root, "later.py")
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py")
        subprocess.run(["git", "-C", str(wt), "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                        "sequence.editor=sed -i.bak 1s/^pick/edit/", "rebase", "-i", "HEAD~2"],
                       check=True, capture_output=True)
        self.assertEqual(git(wt, "show", "--name-only", "--format=", "HEAD"), "later.py")  # stopped there
        self.tick()
        self.assertEqual(self.events("bounds_violation") + self.events("bounds_error"), [])

    def test_a_check_that_fails_is_no_pass(self):
        wt = self.seat("p.1", ["main:src/"])
        shutil.rmtree(wt)
        self.tick()
        errs = self.events("bounds_error")
        self.assertEqual({e["kind"] for e in errs}, {"worktree", "remote"})
        self.assertEqual({e["batch"] for e in errs}, {"p.1"})
        self.assertEqual(len(self.told("越界核对出错")), 2)
        self.tick(M)  # the same error: told once
        self.assertEqual((len(self.events("bounds_error")), len(self.told("越界核对出错"))), (2, 2))
        self.assertEqual(self.found(), [])

    def test_unknown_target_is_an_error(self):
        (self.root / ".foremind" / "config.toml").write_text('[[repos]]\nid = "main"\npath = "."\n')
        self.seat("p.1", ["main:src/"])
        self.tick()
        [e] = [e for e in self.events("bounds_error") if e["kind"] == "worktree"]
        self.assertIn("no target branch", e["error"])


class RemoteTest(BoundsBase):
    def setUp(self):
        super().setUp()
        origin = self.tmp / "github.com" / "o.git"  # a local bare repo whose url looks like GitHub's
        git(self.tmp, "init", "-q", "--bare", str(origin))
        git(self.root, "remote", "add", "origin", str(origin))
        git(self.root, "push", "-q", "origin", "main")
        git(self.root, "fetch", "-q", "origin")
        self.prs = "[]"
        self.gh = self.enterContext(mock.patch.object(review, "gh", side_effect=lambda *a, **k: self.prs))

    def test_pushed_branch_and_pr_every_check_interval(self):
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py")
        self.tick()
        self.assertEqual(self.events("bounds_violation") + self.events("bounds_error"), [])
        self.assertEqual(self.gh.call_args.args[1:], ("pr", "list", "--state", "all", "--limit", "200",
                                                      "--json", "number,headRefOid,headRefName"))
        git(wt, "push", "-q", "origin", "fm/p.1")
        self.prs = self.pr(7, git(wt, "rev-parse", "HEAD"))
        self.tick(M)  # within supervisor.merged_check_min (5)
        self.assertEqual(self.found("remote"), [])
        self.tick(6 * M)
        [e] = self.found("remote")
        self.assertEqual(e["repos"], [{"repo": "main", "branch": "fm/p.1", "on_remote": ["fm/p.1"], "prs": [7]}])
        [(title, body, prio)] = self.told("越界")
        self.assertEqual(prio, "P1")
        self.assertIn("#23", body)
        self.assertEqual(self.pending(S), [])  # the remote fact goes to the user only
        self.tick(12 * M)
        self.assertEqual(len(self.found("remote")), 1)

    @staticmethod
    def pr(number, head, branch="x"):
        return json.dumps([{"number": number, "headRefOid": head, "headRefName": branch},
                           {"number": 99, "headRefOid": "f" * 40, "headRefName": "someone-else"}])

    def test_any_branch_name_and_a_pr_by_its_head(self):  # m2d.7 ⑥
        wt = self.seat("p.1", ["main:src/"])
        first = self.commit(wt, "src/a.py")
        self.commit(wt, "src/b.py")
        git(self.root, "push", "-q", "origin", "main:elsewhere")  # the target's commits are not the batch's
        self.tick()
        self.assertEqual(self.found("remote"), [])
        git(wt, "push", "-q", "origin", f"{first}:refs/heads/hidden")
        self.prs = self.pr(8, first)
        self.tick(6 * M)
        [e] = self.found("remote")
        self.assertEqual(e["repos"], [{"repo": "main", "branch": "fm/p.1", "on_remote": ["hidden"], "prs": [8]}])

    def test_a_push_to_the_target_moves_no_base(self):  # m2d.7 ⑥ r1: the push moves origin/main onto the batch
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py")
        self.commit(self.root, "other.py")  # someone else's, on the target
        git(self.root, "push", "-q", "origin", "main")
        git(wt, "fetch", "-q", "origin")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-edit", "origin/main")
        self.tick()
        self.assertEqual(self.found("remote"), [])  # the target merged in is not the batch's
        git(wt, "push", "-q", "origin", "HEAD:main")
        self.commit(wt, "src/b.py")
        self.tick(6 * M)
        [e] = self.found("remote")
        self.assertEqual(e["repos"], [{"repo": "main", "branch": "fm/p.1", "on_remote": ["main"], "prs": []}])

    def test_a_reopen_on_the_branch_keeps_the_first_start(self):  # m2d.7 ⑥ r2: its start holds the pushed commit
        wt = self.seat("p.1", ["main:src/"])
        self.commit(wt, "src/a.py")
        git(wt, "push", "-q", "origin", "HEAD:main")  # not caught before the batch went back to ready
        git(self.root, "fetch", "-q", "origin")
        self.log.append("seat_opened", batch="p.1", session=S, successor=False, carrier="fake",
                        worktrees={"main": str(wt)}, starts={"main": git(wt, "rev-parse", "HEAD")})
        self.commit(wt, "src/b.py")
        self.tick()
        [e] = self.found("remote")
        self.assertEqual(e["repos"], [{"repo": "main", "branch": "fm/p.1", "on_remote": ["main"], "prs": []}])

    def test_system_push_is_not_checked(self):
        (self.root / ".foremind" / "delivery.toml").write_text('[delivery.repo.main]\npush_pr = "system"\n')
        wt = self.seat("p.1", ["main:src/"])
        git(wt, "push", "-q", "origin", "fm/p.1")
        self.tick()
        self.assertEqual(self.found("remote"), [])
        self.gh.assert_not_called()

    def change(self, cwd, text, path="src/a.py"):
        (cwd / path).parent.mkdir(exist_ok=True)
        (cwd / path).write_text(text)
        git(cwd, "add", path)
        git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", text)

    def midway(self):
        """A seat worktree stopped on update's conflict: `stop(how)` runs merge or rebase with the target. The seat
        committed other.py after the conflicting commit and left stray.txt untracked, both outside its bounds."""
        (self.root / ".git" / "info" / "exclude").write_text("CLAUDE.local.md\n")
        wt = self.seat("p.1", ["main:src/"])
        self.change(wt, "seat\n")
        self.commit(wt, "other.py")  # a stopped rebase has not put it on HEAD yet
        self.commit(self.root, "later.py", "package.json")  # the target's, staged in the worktree by the merge
        self.change(self.root, "main\n")
        (wt / "stray.txt").write_text("s\n")

        def stop(how, cwd=wt):
            got = subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", how, "main"],
                                 capture_output=True, text=True)
            self.assertNotEqual(got.returncode, 0)  # stopped on the conflict
        return wt, stop

    OUT = [{"path": "main:other.py", "why": "owns_paths"}, {"path": "main:stray.txt", "why": "owns_paths"}]

    def test_a_merge_midway_waits_only_for_what_it_staged(self):
        """What has a staged side may be the target's (package.json, the conflict); the commits, the unstaged-only,
        untracked and ignored entries are the seat's, and HEAD is still the branch's."""
        wt, stop = self.midway()
        git(wt, "push", "-q", "origin", "fm/p.1")
        stop("merge")
        (wt / "CLAUDE.local.md").write_text("x\n")
        (wt / "README").write_text("r\n")  # " M": the merge left it alone
        self.prs = self.pr(7, "0" * 40, "fm/p.1")
        self.tick()
        [e] = self.found()
        self.assertEqual(e["paths"], [{"path": "main:README", "why": "owns_paths"}, *self.OUT,
                                      {"path": "main:CLAUDE.local.md", "why": "#22"}])
        [r] = self.found("remote")
        self.assertEqual(r["repos"], [{"repo": "main", "branch": "fm/p.1", "on_remote": ["fm/p.1"], "prs": [7]}])
        git(wt, "merge", "--abort")
        self.tick(M)
        self.assertEqual(len(self.found()), 1)  # nothing was left out

    def test_a_rebase_midway_checks_all_but_the_remote_and_more_than_three_passes_is_an_error(self):
        """HEAD is detached over onto: the branch as it was (orig-head), what is on HEAD since onto and all of git
        status are the seat's."""
        wt, stop = self.midway()
        stop("rebase")
        git(wt, "update-ref", "refs/heads/fm/p.1", "main")  # the branch moved meanwhile: what is rebased is orig-head
        self.tick()
        [e] = self.found()
        self.assertEqual(e["paths"], self.OUT)  # other.py: on orig-head, not replayed yet
        (wt / "src" / "a.py").write_text("both\n")
        (wt / "hidden.py").write_text("h\n")
        git(wt, "add", "src/a.py", "hidden.py")
        git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "at the stop")  # on the detached HEAD
        (wt / "staged.py").write_text("s\n")
        git(wt, "add", "staged.py")
        for n in range(1, bounds.MIDWAY_PASSES):
            self.tick(n * M)
        self.gh.assert_not_called()  # HEAD is detached
        self.assertEqual(self.found()[-1]["paths"], [{"path": f"main:{p}", "why": "owns_paths"}
                                                     for p in ("hidden.py", "other.py", "staged.py", "stray.txt")])
        self.assertEqual(self.events("bounds_error"), [])
        self.tick(3 * M)
        self.tick(4 * M)
        [e] = self.events("bounds_error")
        self.assertEqual((e["kind"], e["batch"]), ("worktree", "p.1"))
        self.assertIn("main rebase at", e["error"])
        [(title, body, prio)] = self.told("越界核对出错")
        self.assertIn("变基中", body)
        self.assertEqual(prio, "P1")
        git(wt, "rebase", "--abort")
        self.tick(5 * M)  # its remote waited for the next check
        self.gh.assert_called()
        stop("merge")  # a new stop counts from one again
        for n in range(6, 6 + bounds.MIDWAY_PASSES):
            self.tick(n * M)
        self.assertEqual(len(self.events("bounds_error")), 1)

    def test_a_rebase_in_one_repo_skips_only_its_remote(self):
        """The other repos' remotes keep to merged_check_min; each repo midway counts on its own, one error per HEAD."""
        lib, origin = self.tmp / "lib", self.tmp / "github.com" / "lib.git"
        git(self.tmp, "init", "-q", "-b", "main", str(lib))
        self.commit(lib, "README")
        git(self.tmp, "init", "-q", "--bare", str(origin))
        git(lib, "remote", "add", "origin", str(origin))
        git(lib, "push", "-q", "origin", "main")
        git(lib, "fetch", "-q", "origin")
        with (self.root / ".foremind" / "config.toml").open("a") as f:
            f.write(f'[[repos]]\nid = "lib"\npath = "{lib}"\ndefault_branch = "main"\n')
        wt, stop = self.midway()
        lwt = self.tmp / "wt" / "p.1" / "lib"
        git(lib, "worktree", "add", "-q", "-b", "fm/p.1", str(lwt), "main")
        self.log.append("seat_opened", batch="p.1", session=S, successor=False, carrier="fake",
                        worktrees={"main": str(wt), "lib": str(lwt)})
        self.change(lwt, "seat\n", "f")
        for w in (wt, lwt):
            git(w, "push", "-q", "origin", "fm/p.1")
        stop("rebase")
        for n in range(bounds.MIDWAY_PASSES + 3):
            self.tick(n * M)
        [r] = self.found("remote")
        self.assertEqual(r["repos"], [{"repo": "lib", "branch": "fm/p.1", "on_remote": ["fm/p.1"], "prs": []}])
        self.assertEqual(self.gh.call_count, 2)  # lib's, at 0 and 5 M (merged_check_min), not every pass
        self.change(lib, "main\n", "f")
        stop("merge", lwt)  # lib's passes count from one; main's error, same HEAD, is not told again
        for n in range(6, 6 + bounds.MIDWAY_PASSES + 1):
            self.tick(n * M)
        self.assertEqual([e["error"].split(": ")[1].split(" at ")[0] for e in self.events("bounds_error")],
                         ["main rebase", "lib merge"])
        self.assertEqual(len(self.told("越界核对出错")), 2)
        git(wt, "rebase", "--abort")
        self.tick(10 * M)  # the next check takes main's remote too
        self.assertEqual([[x["repo"] for x in e["repos"]] for e in self.found("remote")], [["lib"], ["lib", "main"]])

    def test_gh_failure_is_an_error(self):
        self.gh.side_effect = review.FlowError("gh pr list: not logged in")
        self.seat("p.1", ["main:src/"])
        self.tick()
        [e] = self.events("bounds_error")
        self.assertEqual(e["kind"], "remote")
        self.assertIn("not logged in", e["error"])


class ClaimsTest(Base):
    """A session's seat_ancestor is audit.l0's (test_audit.SeatAncestorTest)."""

    def test_unknown_ancestry_is_told_once(self):
        self.log.append("batch_retried", batch="p.1", by="user", seat_ancestor="unknown")
        self.tick()
        self.tick(M)
        self.assertFalse(sv.paused_path(self.root).exists())
        self.assertEqual([(x[0], x[2]) for x in self.rec.sent], [("记作用户所为的事件无法核对", "P1")])
        self.assertEqual(self.events("l0_hard_failure"), [])
