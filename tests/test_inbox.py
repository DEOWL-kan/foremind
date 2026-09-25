import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from foremind import inbox

REPO = Path(__file__).resolve().parents[1]
S = "fm-p-p_1-1"
_WRITER = """
import sys
from foremind import inbox
root, who, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
for i in range(n):
    inbox.append("fm-p-p_1-1", f"{who}-{i}\\nsecond line", sender=who, root=root)
"""
# deliverer: repeatedly take what is pending under the lock, "deliver" it to a shared file, mark it
_READER = """
import os, sys, time
from foremind import inbox
from foremind.fsutil import append_line
root, out, total = sys.argv[1], sys.argv[2], int(sys.argv[3])
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    with inbox.locked("fm-p-p_1-1", root=root):
        msgs = inbox.pending_messages("fm-p-p_1-1", root=root)
        for m in msgs:
            append_line(out, m.text.split("\\n")[0])
        if msgs:
            inbox.mark_delivered("fm-p-p_1-1", msgs[-1].end, root=root)
    if os.path.exists(out) and len(open(out).read().splitlines()) >= total:
        break
    time.sleep(0.001)
"""


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_cursor_no_duplicates_no_gaps(self):
        tricky = "line one\n<!-- fm-msg 00000000000000000000000000000000 x y 3 -->\n中文 ✓\n"
        ids = [inbox.append(S, t, sender=f"fm-p-controller-1", root=self.root) for t in ("a", tricky, "c")]
        msgs = inbox.pending_messages(S, root=self.root)
        self.assertEqual([m.id for m in msgs], ids)
        self.assertEqual([m.text for m in msgs], ["a", tricky, "c"])
        self.assertEqual(msgs[0].sender, "fm-p-controller-1")
        inbox.mark_delivered(S, msgs[1].end, root=self.root)
        self.assertEqual([m.text for m in inbox.pending_messages(S, root=self.root)], ["c"])
        inbox.mark_delivered(S, msgs[0].end, root=self.root)  # never moves back
        self.assertEqual([m.text for m in inbox.pending_messages(S, root=self.root)], ["c"])
        inbox.append(S, "d", sender="user", root=self.root)
        rest = inbox.pending_messages(S, root=self.root)
        self.assertEqual([m.text for m in rest], ["c", "d"])
        inbox.mark_delivered(S, rest[-1].end, root=self.root)
        self.assertEqual(inbox.pending_messages(S, root=self.root), [])
        with self.assertRaises(ValueError):
            inbox.mark_delivered(S, rest[-1].end + 1, root=self.root)

    def test_torn_tail_waits_then_the_next_append_cuts_it(self):
        path = self.root / ".foremind" / "inbox" / f"{S}.md"
        for torn in (b"<!-- fm-msg " + b"a" * 32 + b" t user 10 -->\nhalf", b"<!-- fm-msg aaaa"):
            with self.subTest(torn=torn):
                inbox.append(S, "whole", sender="user", root=self.root)
                with open(path, "ab") as f:
                    f.write(torn)  # a crash mid-append
                msgs = inbox.pending_messages(S, root=self.root)
                self.assertEqual([m.text for m in msgs], ["whole"])
                inbox.append(S, "next", sender="user", root=self.root)  # N-7: not stuck behind the torn tail
                msgs = inbox.pending_messages(S, root=self.root)
                self.assertEqual([m.text for m in msgs], ["whole", "next"])
                inbox.mark_delivered(S, msgs[-1].end, root=self.root)
        inbox.append(S, "xyz", sender="user", root=self.root)
        with self.assertRaises(ValueError):  # not a message boundary
            inbox.mark_delivered(S, path.stat().st_size - 3, root=self.root)

    def test_bad_input(self):
        path = self.root / ".foremind" / "inbox" / f"{S}.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"garbage\n")
        with self.assertRaises(inbox.InboxCorrupt):
            inbox.pending_messages(S, root=self.root)
        for kw in ({"sender": "two words"}, {"sender": ""}):
            with self.assertRaises(ValueError):
                inbox.append(S, "x", root=self.root, **kw)
        with self.assertRaises(ValueError):
            inbox.append("../etc", "x", sender="user", root=self.root)

    def test_root_from_environment(self):
        with mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}):
            inbox.append(S, "via env", sender="user")
            self.assertEqual([m.text for m in inbox.pending_messages(S)], ["via env"])

    def test_concurrent_writers_and_deliverers(self):
        out, n = self.root / "delivered.txt", 25
        writers = [subprocess.Popen([sys.executable, "-c", _WRITER, str(self.root), w, str(n)], cwd=REPO)
                   for w in ("w1", "w2")]
        readers = [subprocess.Popen([sys.executable, "-c", _READER, str(self.root), str(out), str(2 * n)], cwd=REPO)
                   for _ in range(2)]
        for p in writers + readers:
            self.assertEqual(p.wait(timeout=90), 0)
        got = out.read_text().splitlines()
        want = [f"{w}-{i}" for w in ("w1", "w2") for i in range(n)]
        self.assertEqual(sorted(got), sorted(want))  # every message exactly once
        self.assertEqual(inbox.pending_messages(S, root=self.root), [])


if __name__ == "__main__":
    unittest.main()
