import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir


class PathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop("FOREMIND_PROJECT", None)

    def test_walks_up_to_state_dir_or_toml(self):
        (self.tmp / "a" / ".foremind").mkdir(parents=True)
        (self.tmp / "a" / "b" / "c").mkdir(parents=True)
        self.assertEqual(find_project_root(self.tmp / "a" / "b" / "c"), self.tmp / "a")
        (self.tmp / "a" / "b" / "foremind.toml").write_text("")
        self.assertEqual(find_project_root(self.tmp / "a" / "b" / "c"), self.tmp / "a" / "b")
        self.assertEqual(state_dir(self.tmp), self.tmp / ".foremind")

    def test_env_wins_and_missing_raises(self):
        with self.assertRaises(ProjectNotFound):
            find_project_root(self.tmp)
        os.environ["FOREMIND_PROJECT"] = str(self.tmp)
        self.assertEqual(find_project_root(Path("/")), self.tmp)

    def test_user_config_dir_env(self):
        os.environ["FOREMIND_CONFIG_HOME"] = str(self.tmp)
        self.assertEqual(user_config_dir(), self.tmp)
        del os.environ["FOREMIND_CONFIG_HOME"]
        self.assertEqual(user_config_dir(), Path.home() / ".config" / "foremind")


if __name__ == "__main__":
    unittest.main()
