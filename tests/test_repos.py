import tempfile
import unittest
from pathlib import Path

from foremind.config import ConfigError
from foremind.repos import Repo, load_repos, split_qualified


class ReposTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_single_repo_fallback(self):
        self.assertEqual(load_repos(self.root, {}), [])
        (self.root / ".git").mkdir()
        self.assertEqual(load_repos(self.root, {}), [Repo("main", self.root)])

    def test_multi_repo_registration(self):
        repos = load_repos(self.root, {"repos": [
            {"id": "api", "path": "backend", "remote": "git@x:api.git", "default_branch": "develop"},
            {"id": "app", "path": "/abs/app"},
        ]})
        self.assertEqual(repos, [Repo("api", self.root / "backend", "git@x:api.git", "develop"),
                                 Repo("app", Path("/abs/app"))])

    def test_bad_entries(self):
        for entries in ([{"id": "a", "path": "x"}, {"id": "a", "path": "y"}], [{"id": "a:b", "path": "x"}],
                        [{"path": "x"}], [{"id": "-a", "path": "x"}], [{"id": "a b", "path": "x"}],
                        [{"id": "a/b", "path": "x"}], [{"id": "a", "path": "../x"}], [{"id": "a", "path": "x/../../y"}]):
            with self.assertRaises(ConfigError):
                load_repos(self.root, {"repos": entries})

    def test_repo_path_is_normalized(self):
        repos = load_repos(self.root, {"repos": [{"id": "a_1-b", "path": "x/../y/./z"}, {"id": "up", "path": "/abs/../srv"}]})
        self.assertEqual([r.path for r in repos], [self.root / "y" / "z", Path("/srv")])

    def test_split_qualified(self):
        one = [Repo("main", self.root)]
        two = [Repo("api", self.root), Repo("app", self.root)]
        self.assertEqual(split_qualified("main:src/a.py", one), ("main", "src/a.py"))
        self.assertEqual(split_qualified("app:lib/**", two), ("app", "lib/**"))
        self.assertEqual(split_qualified("app:./lib//a.py", two), ("app", "lib/a.py"))
        self.assertEqual(split_qualified("app:docs/a:b.md", two), ("app", "docs/a:b.md"))  # first colon splits
        for p, repos in (("src/a.py", two), ("src/a.py", one), ("web:x", two), ("mian:x", one),
                         ("app:", two), ("app:/etc/passwd", two), ("app:../x", two), ("app:a/../../x", two)):
            with self.assertRaises(ValueError, msg=p):
                split_qualified(p, repos)

    def test_relative_root_is_resolved(self):
        with self.assertRaises(ConfigError):
            load_repos(".", {"repos": [{"id": "a", "path": "../x"}]})

    def test_repos_must_be_an_array_of_tables(self):
        for bad in ({"id": "a", "path": "x"}, {}, "x", ["x"]):
            with self.assertRaises(ConfigError):
                load_repos(self.root, {"repos": bad})


if __name__ == "__main__":
    unittest.main()
