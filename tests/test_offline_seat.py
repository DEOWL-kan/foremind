"""REQ-7: opening a seat when the remote cannot be fetched. Local bare remotes only; no network."""
import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foremind import lock, seat, worktree  # noqa: E402
from test_seat import SeatBase, commit, git  # noqa: E402


class OfflineSeatTest(SeatBase):
    def setUp(self):
        super().setUp()
        self.enterContext(contextlib.redirect_stderr(io.StringIO()))  # the warnings (asserted where they matter)

    def offline(self, rid="api"):
        """origin of `rid` fetched once (refs/remotes/origin/main cached), then pointed at an address that is gone;
        returns the cached commit."""
        self.add_remote(rid)
        git(self.root / rid, "fetch", "-q", "origin")
        git(self.root / rid, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        return git(self.root / rid, "rev-parse", "refs/remotes/origin/main")

    def corrupt(self, rid, sha):
        obj = self.root / rid / ".git" / "objects" / sha[:2] / sha[2:]
        obj.chmod(0o644)
        obj.write_bytes(b"corrupt")

    def branch(self, bid, rid="api"):
        return git(self.root / rid, "branch", "--list", f"fm/{bid}")

    def refused(self, bid, *words):
        with self.assertRaises(seat.SeatError) as cm:
            self.open(bid)
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertIsNone(lock.holder(self.root, bid))
        self.assertEqual(self.branch(bid), "")
        self.assertEqual(self.events("seat_cached_start"), [])
        [res] = self.results("seat_open")  # the reason is in the event log, not only on the job's stdout
        self.assertEqual(res["error"], str(cm.exception))
        return str(cm.exception)

    def test_failed_fetch_starts_from_the_cached_commit(self):
        cached = self.offline()
        commit(self.root / "api", "local.py", "x")  # the user's main moved on: not a start point
        self.write_header("p.1")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            res = self.open("p.1")
        self.assertTrue(res["ok"], res)
        self.assertEqual(git(self.wt("p.1"), "rev-parse", "HEAD"), cached)
        [e] = self.events("seat_cached_start")
        self.assertEqual(res["cached"], [{k: e[k] for k in ("repo", "remote", "ref", "sha", "error")}])
        # told at once, in English, to whoever ran it and to the seat: never called the latest
        warning = (f"Warning: api: fetching origin failed: {e['error'].rstrip('.')}. Continuing from the cached commit {cached} "
                   "of refs/remotes/origin/main. The remote's latest state is not verified: it may have moved on.")
        self.assertEqual(err.getvalue(), warning + "\n")
        [(_, kickoff)] = self.carrier.sent
        self.assertTrue(kickoff.endswith("\n" + warning), kickoff)
        self.assertEqual({k: e[k] for k in ("batch", "repo", "remote", "ref", "sha")},
                         {"batch": "p.1", "repo": "api", "remote": "origin", "ref": "refs/remotes/origin/main",
                          "sha": cached})
        self.assertIn("gone.git", e["error"])  # git's own words: an address error is not called a network outage
        self.assertNotIn("upstream", e)

    def test_seat_user_records_it_too(self):
        cached = self.offline()
        self.write_header("p.1")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            seat.claim_for_user(self.root, "p.1")
        self.assertIn(f"Continuing from the cached commit {cached}", err.getvalue())
        self.assertEqual([e["sha"] for e in self.events("seat_cached_start")], [cached])

    def test_a_ref_the_failed_fetch_moved_is_not_used(self):
        cached = self.offline()
        moved = commit(self.root / "api", "moved.py", "x")

        def half_fetch(path, remote):
            git(path, "update-ref", "refs/remotes/origin/main", moved)
            raise worktree.WorktreeError("git fetch --quiet origin: fatal: early EOF")

        self.write_header("p.1")
        with mock.patch.object(worktree, "fetch", half_fetch):
            self.assertTrue(self.open("p.1")["ok"])
        self.assertEqual(git(self.wt("p.1"), "rev-parse", "HEAD"), cached)
        self.assertEqual(self.events("seat_cached_start")[0]["error"], "git fetch --quiet origin: fatal: early EOF")

    def test_timeout(self):
        self.add_remote("api")
        git(self.root / "api", "fetch", "-q", "origin")
        git(self.root / "api", "remote", "set-url", "origin", "ssh://example.invalid/api.git")
        self.write_header("p.1")
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "sleep 2;"}), \
                mock.patch.object(worktree, "FETCH_TIMEOUT_S", 0.5):
            self.assertTrue(self.open("p.1")["ok"])
        [e] = self.events("seat_cached_start")
        self.assertIn("no answer within 0.5 s", e["error"])

    def test_no_cache_refuses(self):
        self.add_remote("api")  # never fetched: no refs/remotes/origin/main
        git(self.root / "api", "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        self.write_header("p.1")
        self.refused("p.1", "no usable cached commit", "refs/remotes/origin/main", "gone.git")

    def test_corrupt_cached_commit_refuses(self):
        cached = self.offline()
        self.corrupt("api", cached)
        self.write_header("p.1")
        self.refused("p.1", "no usable cached commit")

    def test_corrupt_object_under_the_cached_commit_leaves_nothing(self):
        cached = self.offline()
        self.corrupt("api", git(self.root / "api", "rev-parse", f"{cached}:README"))
        self.write_header("p.1")
        with self.assertRaises(worktree.WorktreeError):
            self.open("p.1")
        self.assertEqual(self.branch("p.1"), "")  # the next try must not reuse a branch made from it
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.events("seat_cached_start"), [])

    def test_cached_ref_that_is_not_a_commit_refuses(self):
        self.offline()
        blob = git(self.root / "api", "rev-parse", "main:README")
        git(self.root / "api", "update-ref", "refs/remotes/origin/main", blob)
        self.write_header("p.1")
        self.refused("p.1", "no usable cached commit")

    def no_target(self):
        (self.root / "foremind.toml").write_text('[[repos]]\nid = "api"\npath = "api"\n')  # no default_branch
        self.write_header("p.1")

    def test_no_target_takes_the_remote_head_only(self):
        cached = self.offline()
        git(self.root / "api", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.no_target()
        self.assertTrue(self.open("p.1")["ok"])
        self.assertEqual(git(self.wt("p.1"), "rev-parse", "HEAD"), cached)
        self.assertEqual(self.events("seat_cached_start")[0]["ref"], "refs/remotes/origin/HEAD")

    def test_no_target_without_the_remote_head_refuses(self):
        self.offline()  # refs/remotes/origin/main is there, but nothing says it is origin's default branch
        git(self.root / "api", "update-ref", "--no-deref", "-d", "refs/remotes/origin/HEAD")  # git ≥ 2.48 makes one
        self.no_target()
        self.refused("p.1", "refs/remotes/origin/HEAD")

    def test_another_remotes_cache_refuses(self):
        self.offline()
        other = self.tmp / "other.git"
        git(self.tmp, "clone", "-q", "--bare", str(self.root / "api"), str(other))
        git(self.root / "api", "remote", "add", "other", str(other))
        git(self.root / "api", "fetch", "-q", "other")
        git(self.root / "api", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/other/main")
        self.no_target()
        self.refused("p.1", "refs/remotes/origin/HEAD")

    def test_a_configured_remote_the_repo_lacks_gets_no_cache(self):
        self.offline()  # origin has a cache, but the configuration names another remote
        self.write_config()
        cfg = (self.root / "foremind.toml").read_text().replace('path = "api"\n', 'path = "api"\nremote = "upstream"\n')
        (self.root / "foremind.toml").write_text(cfg)
        self.write_header("p.1")
        self.refused("p.1", "configured remote upstream is not in the repo", "gone.git")

    def test_existing_branch_needs_no_fetch(self):
        self.add_remote("api")  # no cache at all
        git(self.root / "api", "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        git(self.root / "api", "branch", "fm/p.1", "main")
        self.write_header("p.1")
        self.assertTrue(self.open("p.1")["ok"])
        self.assertEqual(self.events("seat_cached_start"), [])

    def test_approved_upstream_uses_the_receipt_heads(self):
        self.write_config(project='[delivery]\ndepends_on = "approved"\n')
        self.add_remote("app")
        git(self.root / "app", "remote", "set-url", "origin", str(self.tmp / "gone.git"))  # no cache either
        git(self.root / "app", "checkout", "-qb", "fm/p.1")
        approved = commit(self.root / "app", "lib/api.dart", "v2")
        git(self.root / "app", "checkout", "-q", "main")
        self.write_header("p.1", repos=["app"], owns_paths=["app:lib/**"], state="approved")
        (self.root / ".foremind" / "batches" / "p.1.review.r1.json").write_text(
            json.dumps({"verdict": "approved", "heads": {"app": approved}}))
        self.write_header("p.2", repos=["app"], depends_on=["p.1"], owns_paths=["app:lib/login.dart"])
        with mock.patch.object(worktree, "fetch", side_effect=AssertionError("no fetch for an approved head")):
            res = self.open("p.2")
        self.assertTrue(res["ok"], res)
        self.assertEqual(git(res["worktrees"]["app"], "rev-parse", "HEAD"), approved)

    def test_readonly_upstream_from_the_cache(self):
        cached = self.offline("app")
        self.write_header("p.1", repos=["app"], owns_paths=["app:lib/**"], state="approved")
        self.write_header("p.2", depends_on=["p.1"], owns_paths=["api:src/client.py"])
        res = self.open("p.2")  # merged mode (the default): the read-only upstream is app's target
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.events("seat_opened")[0]["readonly"], [{"upstream": "p.1", "repo": "app",
                                                                      "sha": cached}])
        [e] = self.events("seat_cached_start")
        self.assertEqual((e["batch"], e["repo"], e["upstream"], e["sha"]), ("p.2", "app", "p.1", cached))


if __name__ == "__main__":
    unittest.main()
