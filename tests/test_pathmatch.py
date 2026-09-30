import unittest
from unittest import mock

from foremind import pathmatch
from foremind.pathmatch import norm, overlap, owns


class PathmatchTest(unittest.TestCase):  # REQ-19
    def test_norm(self):
        for q, want in (("main:./a//b/./c", "main:a/b/c"), ("main:src/", "main:src/"), ("main:.//src//", "main:src/"),
                        ("/abs//x/./y", "/abs/x/y"), ("a/./b", "a/b"), ("main:*.py", "main:*.py"),
                        ("main:../x", "main:../x")):
            self.assertEqual(norm(q), want, q)

    def test_owns(self):
        yes = [("main:src/a.py", ["main:src/a.py"]), ("main:src/a/b.py", ["main:src/"]),
               ("main:src/a/x.py", ["main:src/*.py"]),  # fnmatch: * crosses /
               ("main:./src//a.py", ["main:src/a.py"]), ("main:src/a.py", ["main:./src//"]),
               ("main:data/file1", ["main:data/file[0-9]"]), ("api:x", ["main:y", "api:*"])]
        no = [("main:src/a.py", ["app:src/a.py"]), ("main:src/a.py/x", ["main:src/a.py"]),
              ("main:srcx/a.py", ["main:src/"]), ("main:src/a.md", ["main:src/*.py"]), ("main:a", [])]
        for fold in (True, False):
            with mock.patch.object(pathmatch, "FOLD", fold):
                for q, ps in yes:
                    self.assertTrue(owns(q, ps), (q, ps))
                for q, ps in no:
                    self.assertFalse(owns(q, ps), (q, ps))

    def test_case_follows_the_volume(self):
        with mock.patch.object(pathmatch, "FOLD", True):
            self.assertTrue(owns("main:Foremind/x.py", ["main:foremind/*"]))
            self.assertTrue(overlap("main:SRC/a.py", "main:src/"))
        with mock.patch.object(pathmatch, "FOLD", False):
            self.assertFalse(owns("main:Foremind/x.py", ["main:foremind/*"]))
            self.assertFalse(overlap("main:SRC/a.py", "main:src/"))

    def test_overlap(self):  # the cases of both former paths_overlap (plan/validate.py and seat.py; m2c.9.D8)
        yes = [("main:src/x.py", "main:src/x.py"), ("main:src/*.py", "main:src/a/x.py"),
               ("main:src/**", "main:src/a/*.py"), ("main:src/", "main:src/a.py"), ("main:src/a*", "main:src/*b"),
               ("main:data/file[0-9]", "main:data/*1"), ("api:src", "api:src/a.py"), ("main:doc/x", "main:doc/x/y.md"),
               ("api:src/**", "api:src/deep/x.md"), ("api:lib/*", "api:lib/sub/x.dart"),
               ("main:./src//a.py", "main:src/a.py")]
        no = [("main:src/x.py", "app:src/x.py"), ("main:src/a/*.py", "main:src/b/*.py"),
              ("main:src/*.py", "main:src/*.md"), ("main:src/x.py", "main:src/y.py"),
              ("main:src/*.py", "main:doc/x.md"),
              ("api:src/a", "api:src/ab.py"), ("api:src/a*", "api:src/b*"), ("api:docs/x.md", "api:src/*"),
              ("main:w.md", "main:*.py")]  # a literal owns itself alone (owns)
        for a, b in yes:
            self.assertTrue(overlap(a, b) and overlap(b, a), (a, b))
        for a, b in no:
            self.assertFalse(overlap(a, b) or overlap(b, a), (a, b))

    def test_what_a_pattern_owns_overlaps_it(self):
        pats = ["main:src/", "main:src/*.py", "main:src/a.py", "main:data/file[0-9]", "main:src/a*"]
        files = ["main:src/a.py", "main:src/b/c.py", "main:data/file1", "main:src/abc.md", "main:x.py"]
        for p in pats:
            for f in files:
                if owns(f, [p]):
                    self.assertTrue(overlap(f, p), (f, p))


if __name__ == "__main__":
    unittest.main()
