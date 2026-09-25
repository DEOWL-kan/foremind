import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from foremind import lock
from foremind.events import EventLog

REPO = Path(__file__).resolve().parents[1]
# every child waits for the go file, then races for the same batch lock
_CHILD = """
import os, sys, time
from foremind import lock
root, go, op, me = sys.argv[1:5]
while not os.path.exists(go):
    time.sleep(0.002)
try:
    lock.acquire(root, "p.1", me) if op == "acquire" else lock.transfer(root, "p.1", "fm-old", me)
    print("won")
except lock.LockError:
    print("lost")
"""


class LockTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def events(self):
        return [e["type"] for e in EventLog(self.root / ".foremind" / "events.jsonl").iter()]

    def race(self, op, n=8):
        go = self.root / "go"
        procs = [subprocess.Popen([sys.executable, "-c", _CHILD, str(self.root), str(go), op, f"fm-s{i}"],
                                  cwd=REPO, stdout=subprocess.PIPE, text=True) for i in range(n)]
        go.touch()
        out = [p.communicate(timeout=60)[0].strip() for p in procs]
        winners = [f"fm-s{i}" for i, o in enumerate(out) if o == "won"]
        self.assertEqual(len(winners), 1, out)
        self.assertEqual(out.count("lost"), n - 1, out)
        return winners[0]

    def test_acquire_release(self):
        self.assertIsNone(lock.holder(self.root, "p.1"))
        lock.acquire(self.root, "p.1", "fm-a")
        lock.acquire(self.root, "p.1", "fm-a")  # same holder: no-op
        with self.assertRaises(lock.LockError):
            lock.acquire(self.root, "p.1", "fm-b")
        self.assertEqual(lock.held_by(self.root, "fm-a"), ["p.1"])
        with self.assertRaises(lock.LockError):
            lock.release(self.root, "p.1", "fm-b")
        lock.release(self.root, "p.1", "fm-a")
        self.assertIsNone(lock.holder(self.root, "p.1"))
        self.assertEqual(self.events(), ["lock_acquired", "lock_released"])
        for bad in ("../x", "p", "p.1/x"):
            with self.assertRaises(ValueError):
                lock.acquire(self.root, bad, "fm-a")
        with self.assertRaises(ValueError):
            lock.acquire(self.root, "p.1", "fm a\n")

    def test_concurrent_acquire_one_winner(self):
        winner = self.race("acquire")
        self.assertEqual(lock.holder(self.root, "p.1"), winner)
        self.assertEqual(self.events(), ["lock_acquired"])

    def test_concurrent_transfer_is_atomic(self):
        lock.acquire(self.root, "p.1", "fm-old")
        winner = self.race("transfer")
        self.assertEqual(lock.holder(self.root, "p.1"), winner)
        self.assertEqual(self.events(), ["lock_acquired", "lock_transferred"])
        with self.assertRaises(lock.LockError):  # the old holder no longer holds it
            lock.transfer(self.root, "p.1", "fm-old", "fm-x")
        self.assertEqual(list((self.root / ".foremind" / "batches").glob(".*.tmp")), [])  # no half-written file

    def test_break_needs_evidence(self):
        lock.acquire(self.root, "p.1", "fm-a")
        for bad in (None, "fm-a", {"session": "fm-a"},
                    lock.ExitEvidence("fm-a", "tmux", "i-think-so"),
                    lock.ExitEvidence("fm-a", "manual", "absent"),  # manual cannot see; only the user confirms
                    lock.ExitEvidence("fm-b", "tmux", "pid_exited")):  # evidence about another session
            with self.subTest(bad=bad), self.assertRaises(lock.LockError):
                lock.break_lock(self.root, "p.1", bad)
        self.assertEqual(lock.holder(self.root, "p.1"), "fm-a")
        lock.break_lock(self.root, "p.1", lock.ExitEvidence("fm-a", "manual", "user_confirmed"))
        self.assertIsNone(lock.holder(self.root, "p.1"))
        broken = [e for e in EventLog(self.root / ".foremind" / "events.jsonl").iter() if e["type"] == "lock_broken"]
        self.assertEqual((broken[0]["session"], broken[0]["how"]), ("fm-a", "user_confirmed"))


if __name__ == "__main__":
    unittest.main()
