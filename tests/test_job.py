import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from foremind import job


class JobTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jobs = self.tmp / "jobs"

    def wait(self, job_id, limit=30):
        deadline = time.monotonic() + limit
        while (st := job.status(self.jobs, job_id))["state"] in ("running", "starting"):
            self.assertLess(time.monotonic(), deadline, "job did not finish")
            time.sleep(0.05)
        return st

    def run_py(self, code, **kw):
        return job.start(self.jobs, [sys.executable, "-c", code], cwd=self.tmp, **kw)

    def test_normal_exit(self):
        jid = self.run_py("import os; print(os.environ['FM_X'], os.getcwd())", env={"FM_X": "hi"})
        st = self.wait(jid)
        self.assertEqual((st["state"], st["exit_code"], st["timed_out"]), ("done", 0, False))
        self.assertTrue(st["started_at"] <= st["finished_at"])
        out = (self.jobs / jid / "stdout.log").read_text().split()
        self.assertEqual(out, ["hi", str(self.tmp.resolve())])
        self.assertNotIn("PATH", (self.jobs / jid / "spec.json").read_text())  # only the overlay is stored

    def test_nonzero_exit(self):
        st = self.wait(self.run_py("import sys; sys.stderr.write('boom'); sys.exit(3)"))
        self.assertEqual((st["exit_code"], st["timed_out"]), (3, False))

    def test_timeout_kills_group(self):
        t0 = time.monotonic()
        jid = self.run_py("import time; time.sleep(30)", timeout_s=1)
        st = self.wait(jid)
        self.assertTrue(st["timed_out"])
        self.assertNotEqual(st["exit_code"], 0)
        self.assertLess(time.monotonic() - t0, 20)

    def test_timeout_kills_children_that_ignore_sigterm(self):
        pidfile = self.tmp / "child.pid"
        child = ("import os, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                 f"open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)")
        # the leader dies on SIGTERM, the grandchild does not: only SIGKILL to the group gets it
        jid = self.run_py(f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); "
                          "time.sleep(60)", timeout_s=4)
        st = self.wait(jid)
        self.assertTrue(st["timed_out"])
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 10
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "grandchild survived the timeout")
            time.sleep(0.05)

    def test_env_values_become_strings(self):
        jid = self.run_py("import os; print(os.environ['FM_N'], os.environ['FM_B'])", env={"FM_N": 3, "FM_B": True})
        self.wait(jid)
        self.assertEqual((self.jobs / jid / "stdout.log").read_text().split(), ["3", "True"])

    def test_starting_before_pid_file(self):
        d = self.jobs / "young"
        d.mkdir(parents=True)
        (d / "spec.json").write_text("{}")
        self.assertEqual(job.status(self.jobs, "young"), {"state": "starting"})
        with self.assertRaises(FileNotFoundError):  # an unknown job is still an error
            job.status(self.jobs, "nobody")

    def test_missing_command(self):
        st = self.wait(job.start(self.jobs, [str(self.tmp / "nope")], cwd=self.tmp))
        self.assertEqual(st["exit_code"], 127)

    def test_popen_failure_removes_job_dir(self):
        with mock.patch("subprocess.Popen", side_effect=OSError("no fork")):
            with self.assertRaises(OSError):
                job.start(self.jobs, ["true"], cwd=self.tmp)
        self.assertEqual(list(self.jobs.iterdir()), [])

    def test_lost(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        d = self.jobs / "gone"
        d.mkdir(parents=True)
        (d / "pid").write_text(str(dead.pid))
        self.assertEqual(job.status(self.jobs, "gone"), {"state": "lost"})


if __name__ == "__main__":
    unittest.main()
