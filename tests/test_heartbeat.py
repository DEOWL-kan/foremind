import tempfile
import unittest
from pathlib import Path

from foremind import heartbeat


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_flags_survive_tool_round_trip(self):  # §20 I31
        s = "fm-shop-auth.2-1"
        heartbeat.update(self.root, s, batch="auth.2", role="seat", event="SessionStart")
        heartbeat.update(self.root, s, handoff_requested=True, event="Stop")
        heartbeat.update(self.root, s, open_tool="A", event="PreToolUse")  # a long Bash
        heartbeat.update(self.root, s, open_tool="B", event="PreToolUse")  # parallel call
        heartbeat.update(self.root, s, close_tool="B", event="PostToolUse")
        self.assertEqual(heartbeat.read(self.root, s)["open_tools"], ["A"])
        self.assertTrue(heartbeat.read(self.root, s)["tool_open"])  # A is still running
        hb = heartbeat.update(self.root, s, close_tool="A", event="PostToolUse")
        self.assertEqual({k: hb[k] for k in ("session", "batch", "role", "event", "tool_open", "handoff_requested")},
                         {"session": s, "batch": "auth.2", "role": "seat", "event": "PostToolUse", "tool_open": False,
                          "handoff_requested": True})
        self.assertEqual(heartbeat.read(self.root, s), hb)
        heartbeat.update(self.root, s, open_tool="C", event="PreToolUse")
        self.assertFalse(heartbeat.update(self.root, s, open_tools=[], event="Stop")["tool_open"])  # Stop forgets all

    def test_names_and_bad_files(self):
        for bad in ("../x", ".hidden", "a/b", "", None):
            with self.assertRaises(ValueError):
                heartbeat.path(self.root, bad)
        self.assertIsNone(heartbeat.read(self.root, "nobody"))
        heartbeat.path(self.root, "s1").parent.mkdir(parents=True)
        heartbeat.path(self.root, "s1").write_text("{half")
        self.assertIsNone(heartbeat.read(self.root, "s1"))
        self.assertEqual(heartbeat.update(self.root, "s1", event="Stop")["tool_open"], False)


if __name__ == "__main__":
    unittest.main()
