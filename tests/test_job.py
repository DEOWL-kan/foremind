import gc
import os
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
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

    def test_dropped_runner_does_not_warn(self):  # m2e.3: no "subprocess … is still running" ResourceWarning
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            jid = self.run_py("import time; time.sleep(1)")
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(self.wait(jid)["exit_code"], 0)

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
        old = time.time() - job._START_S - 1
        os.utime(d / "spec.json", (old, old))  # M1-sonnet-fix N3: still no pid long after: it never started
        self.assertEqual(job.status(self.jobs, "young"), {"state": "lost"})

    def test_job_id_and_foremind_job(self):  # m2b.10: the caller may choose the id; the command sees it
        jid = self.run_py("import os; print(os.environ['FOREMIND_JOB'])", env={"FOREMIND_JOB": "not mine"},
                          job_id="chosen")
        self.assertEqual(jid, "chosen")
        self.wait(jid)
        self.assertEqual((self.jobs / jid / "stdout.log").read_text().split(), ["chosen"])

    def test_a_failed_pid_write_leaves_a_running_job(self):  # N3: the runner writes its own pid
        real = job.atomic_write

        def no_pid(path, data):  # this process's pid write fails; the runner is another process
            if path.name == "pid":
                raise OSError("disk")
            real(path, data)

        with mock.patch.object(job, "atomic_write", no_pid):
            jid = self.run_py("import time; time.sleep(1)")
        self.assertEqual(self.wait(jid)["exit_code"], 0)
        self.assertTrue((self.jobs / jid / "pid").exists())

    def test_what_a_command_leaves_in_its_group_is_ended_with_it(self):  # M1-1-r1
        bg = self.tmp / "bg.pid"
        jid = job.start(self.jobs, ["/bin/sh", "-c", f"sleep 60 & echo $! > {bg}"], cwd=self.tmp)
        self.assertEqual(self.wait(jid)["exit_code"], 0)
        pid, deadline = int(bg.read_text()), time.monotonic() + 10
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "the background child outlived its job")
            time.sleep(0.05)

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
