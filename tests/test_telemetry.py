import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from foremind import telemetry
from foremind.commands import statusline

REPO = Path(__file__).resolve().parent.parent


def rec(mid, *, inp=2, create=0, read=0, out=10, sidechain=False, type="assistant"):
    return json.dumps({"type": type, "isSidechain": sidechain, "message": {"id": mid, "role": "assistant", "usage": {
        "input_tokens": inp, "cache_creation_input_tokens": create, "cache_read_input_tokens": read,
        "output_tokens": out, "cache_creation": {"ephemeral_5m_input_tokens": 0}}}})


class ContextUsageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def usage(self, lines):
        p = self.tmp / "t.jsonl"
        p.write_text("\n".join(lines) + "\n")
        return telemetry.context_usage(str(p))

    def test_dedupe_and_sidechain(self):
        u = self.usage([
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            rec("m1", inp=3, create=900, read=100, out=40),
            rec("m1", inp=3, create=900, read=100, out=40),  # same request split per content block
            rec("s1", read=999_999, out=7, sidechain=True),  # subagent chain: not this context
            "{broken json with \"usage\"",
            rec("m2", inp=2, create=50, read=4000, out=5),
            rec("m2", inp=2, create=50, read=4000, out=5),
            rec("s2", read=888_888, sidechain=True),  # the last line, still ignored
        ])
        self.assertEqual(u, {"context_tokens": 4052, "requests": 2, "output_tokens": 45,
                             "first_usage": {"input_tokens": 3, "cache_creation_input_tokens": 900,
                                             "cache_read_input_tokens": 100}})

    def test_last_record_per_id_and_zero_usage_skipped(self):  # N-2
        u = self.usage([
            rec("m1", inp=3, read=100, out=1),
            rec("m1", inp=3, read=100, out=40),  # the same request's final record
            rec("m2", inp=2, read=5000, out=5),
            rec("err", inp=0, read=0, out=0),  # synthetic record of an API error: says nothing about the context
        ])
        self.assertEqual((u["context_tokens"], u["requests"], u["output_tokens"]), (5002, 2, 45))

    def test_last_context_reads_the_tail(self):  # N-1
        p = self.tmp / "t.jsonl"
        lines = [rec(f"m{i}", read=1000 + i, out=1) for i in range(200)]
        p.write_text("\n".join([*lines, rec("s", read=9, sidechain=True), rec("e", inp=0), ""]))
        for chunk in (64, 1 << 16):  # records straddle chunk borders, or the whole file is one chunk
            self.assertEqual(telemetry.last_context(str(p), chunk=chunk), 2 + 1199)
        self.assertEqual(telemetry.last_context(str(p)), telemetry.context_usage(str(p))["context_tokens"])
        p.write_text(json.dumps({"type": "user"}) + "\n")
        self.assertIsNone(telemetry.last_context(str(p)))
        self.assertIsNone(telemetry.last_context(None))

    def test_write_merge(self):
        root = self.tmp
        telemetry.write(root, "s1", "context", {"context_tokens": 1, "requests": 3})
        telemetry.write(root, "s1", "context", {"context_tokens": 2}, merge=True)
        snap = json.loads((root / ".foremind" / "telemetry" / "s1.context.json").read_text())
        self.assertEqual((snap["context_tokens"], snap["requests"]), (2, 3))
        telemetry.write(root, "s1", "context", {"context_tokens": 5})
        self.assertNotIn("requests", json.loads((root / ".foremind" / "telemetry" / "s1.context.json").read_text()))

    def test_nothing_yet(self):
        self.assertIsNone(self.usage([json.dumps({"type": "user", "message": {"content": "hi"}})]))
        self.assertIsNone(telemetry.context_usage(str(self.tmp / "missing.jsonl")))
        self.assertIsNone(telemetry.context_usage(None))


class StatuslineTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(os.path.realpath(self.enterContext(tempfile.TemporaryDirectory())))
        self.root = tmp / "proj"
        (self.root / ".foremind").mkdir(parents=True)
        self.cfg = tmp / "cfg"
        self.cfg.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("FOREMIND_")}
        self.env = {**env, "FOREMIND_CONFIG_HOME": str(self.cfg)}
        self.input = {"session_id": "abc-123", "cwd": str(self.root), "model": {"id": "claude-opus-5-5"},
                      "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": "2026-09-25T18:00:00Z"},
                                      "seven_day": None},
                      "context_window": {"context_window_size": 200000, "used_percentage": 12}}

    def statusline(self, **env):
        return subprocess.run([sys.executable, "-m", "foremind", "statusline"], cwd=REPO, capture_output=True,
                              input=json.dumps(self.input).encode(), env={**self.env, **env})

    def snap(self, name):
        return json.loads((self.root / ".foremind" / "telemetry" / f"{name}.statusline.json").read_text())

    def test_wraps_original_and_records(self):
        orig = 'import sys, json; print("ORIG:" + json.load(sys.stdin)["model"]["id"], end=" |x|\\n")'
        (self.cfg / "config.toml").write_text(
            f"[statusline]\ncommand = {json.dumps(shlex.join([sys.executable, '-c', orig]))}\n")
        r = self.statusline()
        self.assertEqual((r.returncode, r.stdout), (0, b"ORIG:claude-opus-5-5 |x|\n"))  # unchanged, byte for byte
        s = self.snap("abc-123")
        self.assertEqual(s["rate_limits"], self.input["rate_limits"])  # null windows stay null (= unknown)
        self.assertEqual(s["context_window"], self.input["context_window"])
        self.assertEqual((s["session"], s["agent_session_id"]), (None, "abc-123"))
        r = self.statusline(FOREMIND_SESSION="fm-proj-p.1-1")
        self.assertEqual(self.snap("fm-proj-p.1-1")["session"], "fm-proj-p.1-1")

    def test_without_original_or_project(self):
        r = self.statusline()
        self.assertEqual((r.returncode, r.stdout), (0, b""))
        self.assertTrue((self.root / ".foremind" / "telemetry" / "abc-123.statusline.json").exists())
        self.input["cwd"] = "/"
        self.assertEqual(self.statusline().returncode, 0)
        self.assertIsNone(telemetry.record_statusline(b"garbage"))

    def test_wrong_types_are_ignored(self):
        self.enterContext(mock.patch.dict(os.environ, self.env, clear=True))
        for data in ({"cwd": 5, "session_id": "x"}, {"cwd": ["a"]}, {"cwd": str(self.root), "session_id": 7}, [1]):
            self.assertIsNone(telemetry.record_statusline(json.dumps(data).encode()), data)

    def test_command_runs_in_its_own_process_group(self):
        self.assertNotEqual(int(statusline.run_wrapped("ps -o pgid= -p $$", b"")), os.getpgrp())

    def test_timeout_kills_what_the_command_started(self):
        pid_file, t = self.root / "pid", time.monotonic()
        with mock.patch.object(statusline, "TIMEOUT_S", 0.5):  # the shell exits, its background child keeps stdout
            self.assertIsNone(statusline.run_wrapped(f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}", b""))
        self.assertLess(time.monotonic() - t, 5)
        pid = int(pid_file.read_text())
        for _ in range(50):  # killed, then reaped by init
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, 9)
            self.fail("the command's child outlived the timeout")

    def test_stale_readings_of_other_sessions_go(self):
        tel = self.root / ".foremind" / "telemetry"
        tel.mkdir()
        files = {"old-1": {"session": None, "agent_session_id": "old-1"},  # the only one to go
                 "new-1": {"session": None, "agent_session_id": "new-1"},
                 "fm-p-p.1-1": {"session": "fm-p-p.1-1", "agent_session_id": "old-2"},
                 "renamed": {"session": None, "agent_session_id": "old-3"},
                 "held": {"session": "fm-p-p.1-1", "agent_session_id": "held"},
                 "nokey": {"agent_session_id": "nokey"}}
        old = time.time() - (telemetry.STALE_DAYS + 1) * 86400
        for name, s in files.items():
            (tel / f"{name}.statusline.json").write_text(json.dumps(s))
            if name != "new-1":
                os.utime(tel / f"{name}.statusline.json", (old, old))
        (tel / "junk.statusline.json").write_text("not json")
        os.utime(tel / "junk.statusline.json", (old, old))
        self.statusline(FOREMIND_SESSION="fm-p-p.2-1")  # a Foremind session's reading deletes nothing
        self.assertTrue((tel / "old-1.statusline.json").exists())
        self.statusline()
        left = sorted(p.name.removesuffix(".statusline.json") for p in tel.glob("*.statusline.json"))
        self.assertEqual(left, sorted(["abc-123", "fm-p-p.2-1", "junk", *files.keys() - {"old-1"}]))

    def test_project_config_cannot_set_the_command(self):
        (self.root / "foremind.toml").write_text('[statusline]\ncommand = "echo PWNED"\n')
        self.assertEqual(self.statusline().stdout, b"")


if __name__ == "__main__":
    unittest.main()
