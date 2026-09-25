import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import worktree
from foremind.repos import Repo

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(path, name, text):
    (Path(path) / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-qm", f"add {name}")
    return git(path, "rev-parse", "HEAD")


def make_repo(path, rid):
    Path(path).mkdir(parents=True)
    git(path, "init", "-qb", "main")
    commit(path, "README", rid)
    return Repo(rid, Path(path))


class WorktreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ, {**GIT_ENV, "FOREMIND_WT_ROOT": str(self.tmp / "wt")}))
        self.api = make_repo(self.tmp / "proj" / "api", "api")
        self.app = make_repo(self.tmp / "proj" / "app", "app")

    def test_root_default_and_override(self):
        self.assertEqual(worktree.batch_dir("proj", "p.1"), self.tmp / "wt" / "proj" / "p.1")
        with mock.patch.dict(os.environ):
            del os.environ["FOREMIND_WT_ROOT"]
            self.assertEqual(worktree.wt_root(), Path.home() / ".local" / "share" / "foremind" / "wt")

    def test_multi_repo_layout(self):
        base = {r.id: git(r.path, "rev-parse", "HEAD") for r in (self.api, self.app)}
        for r in (self.api, self.app):
            path, head = worktree.create("proj", "p.1", r, branch="fm/p.1", start_point="main")
            self.assertEqual(path, self.tmp / "wt" / "proj" / "p.1" / r.id)
            self.assertEqual(head, base[r.id])
            self.assertEqual(git(path, "symbolic-ref", "--short", "HEAD"), "fm/p.1")
            self.assertEqual((path / "README").read_text(), r.id)
        # again (retry or successor): the same worktree, untouched
        new = commit(self.tmp / "wt" / "proj" / "p.1" / "api", "work.py", "x")
        self.assertEqual(worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point="main"),
                         (self.tmp / "wt" / "proj" / "p.1" / "api", new))

    def test_existing_branch_is_checked_out_not_recreated(self):
        user_wt = self.tmp / "elsewhere"
        git(self.api.path, "worktree", "add", "-q", "-b", "mine", str(user_wt))
        shutil.rmtree(user_wt)  # e.g. on a disk that is not mounted now: not ours to forget (N-5)
        path, _ = worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point="main")
        work = commit(path, "work.py", "x")
        shutil.rmtree(path)  # directory lost; the branch survives
        start = mock.Mock(side_effect=AssertionError("no start point needed for an existing branch"))
        path, head = worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point=start)
        self.assertEqual(head, work)
        self.assertIn(str(user_wt), git(self.api.path, "worktree", "list"))

    def test_start_point_is_resolved_only_for_a_new_branch(self):
        start = mock.Mock(return_value="main")
        worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point=start)
        worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point=start)
        start.assert_called_once_with()

    def test_dirty(self):
        path, _ = worktree.create("proj", "p.1", self.api, branch="fm/p.1", start_point="main")
        self.assertEqual(worktree.dirty(path), [])
        (path / "README").write_text("changed")
        (path / "new dir").mkdir()
        (path / "new dir" / "a b.py").write_text("x")
        git(path, "mv", "README", "README.md")
        self.assertEqual(sorted(worktree.dirty(path)), ["README.md", "new dir/a b.py"])

    def test_readonly_upstream_follows_ref(self):
        path, sha = worktree.create_readonly("proj", "p.2", "p.1", self.api, "main")
        self.assertEqual(path, self.tmp / "wt" / "proj" / "p.2" / "_ro" / "p.1" / "api")
        self.assertEqual(git(path, "rev-parse", "HEAD"), sha)
        detached = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"]).returncode == 1
        self.assertTrue(detached)
        moved = commit(self.api.path, "contract.md", "v2")
        self.assertEqual(worktree.create_readonly("proj", "p.2", "p.1", self.api, "main"), (path, moved))
        self.assertEqual((path / "contract.md").read_text(), "v2")
        with self.assertRaises(worktree.WorktreeError):
            worktree.create_readonly("proj", "p.2", "p.1", self.api, "no-such-ref")
        (path / "contract.md").write_text("edited where nobody should write")
        commit(self.api.path, "later.md", "v3")
        with self.assertRaises(worktree.WorktreeError):  # N-6: local changes are not carried to the new ref
            worktree.create_readonly("proj", "p.2", "p.1", self.api, "main")
        self.assertEqual((path / "contract.md").read_text(), "edited where nobody should write")

    def test_remove(self):
        for r in (self.api, self.app):
            worktree.create("proj", "p.2", r, branch="fm/p.2", start_point="main")
        worktree.create_readonly("proj", "p.2", "p.1", self.api, "main")
        d = self.tmp / "wt" / "proj" / "p.2"
        (d / "app" / "README").write_text("uncommitted")
        with self.assertRaises(worktree.WorktreeError):  # never drop uncommitted work silently
            worktree.remove("proj", "p.2", [self.api, self.app])
        self.assertTrue((d / "app" / "README").exists())
        worktree.remove("proj", "p.2", [self.api, self.app], force=True)
        self.assertFalse(d.exists())
        self.assertNotIn(str(d), git(self.api.path, "worktree", "list"))
        self.assertTrue(git(self.app.path, "rev-parse", "--verify", "fm/p.2"))  # branches stay


if __name__ == "__main__":
    unittest.main()
