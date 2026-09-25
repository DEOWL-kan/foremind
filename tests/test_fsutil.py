import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import fsutil


class FsutilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_atomic_write_leaves_no_temp_files(self):
        target = self.tmp / "sub" / "f.txt"
        fsutil.atomic_write(target, "一\n")
        fsutil.atomic_write(target, b"two\n")
        self.assertEqual(target.read_bytes(), b"two\n")
        self.assertEqual(os.listdir(target.parent), ["f.txt"])

    def test_atomic_write_fsyncs_directory(self):
        with mock.patch("os.fsync", wraps=os.fsync) as fsync:
            fsutil.atomic_write(self.tmp / "f", "x")
        self.assertEqual(fsync.call_count, 2)  # the file, then its directory

    def test_atomic_write_keeps_mode(self):
        target = self.tmp / "x.sh"
        target.write_text("old")
        target.chmod(0o755)
        fsutil.atomic_write(target, "new")
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_append_line_adds_newline(self):
        p = self.tmp / "log"
        fsutil.append_line(p, "a")
        fsutil.append_line(p, "b\n")
        self.assertEqual(p.read_text(), "a\nb\n")

    def test_lock_busy(self):
        lock = self.tmp / "l.lock"
        with fsutil.file_lock(lock):
            with self.assertRaises(fsutil.LockBusy):
                with fsutil.file_lock(lock, blocking=False):
                    pass
        with fsutil.file_lock(lock, blocking=False):  # released after the with block
            pass

    def test_project_and_global_lock_paths(self):
        with mock.patch.dict(os.environ, {"FOREMIND_CONFIG_HOME": str(self.tmp / "cfg")}):
            with fsutil.global_lock():
                self.assertTrue((self.tmp / "cfg" / "supervisor.lock").exists())
                with self.assertRaises(fsutil.LockBusy):
                    with fsutil.global_lock():
                        pass
        with fsutil.project_lock(self.tmp):
            self.assertTrue((self.tmp / ".foremind" / "state.lock").exists())

    def test_sha256(self):
        p = self.tmp / "h"
        p.write_bytes(b"abc")
        expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertEqual(fsutil.sha256_bytes(b"abc"), expected)
        self.assertEqual(fsutil.sha256_file(p), expected)


if __name__ == "__main__":
    unittest.main()
