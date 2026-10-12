import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foremind import carriers, cli  # noqa: E402
from foremind.commands import watch  # noqa: E402
from test_carriers import FAKE_TMUX  # noqa: E402
from test_supervisor import Base  # noqa: E402

S = "fm-p-p_1-1"
REAL_GET = carriers.get  # test_supervisor.Base replaces it with its fake carrier

# fake `orca terminal read --terminal <h> --screen --json`: $FAKE_ORCA_SOURCE picks the source ("fail": ok false);
# argv goes to $FAKE_ORCA_LOG
FAKE_ORCA = r'''
import json, os, sys
open(os.environ["FAKE_ORCA_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
src = os.environ["FAKE_ORCA_SOURCE"]
if src == "fail":
    print(json.dumps({"ok": False, "error": {"code": "terminal_handle_stale"}}))
else:
    print(json.dumps({"ok": True, "result": {"source": src, "terminal": {
        "handle": "term_1", "status": "running", "tail": ["", "> q", "Enter to confirm · Esc to cancel"]}}}))
'''


def fake_bins(test, tmp):
    """Fake orca and tmux first on PATH, both logging argv to tmp/log; an Orca record for S (handle term_1)."""
    bin_ = tmp / "bin"
    bin_.mkdir()
    for name, code in (("orca", FAKE_ORCA), ("tmux", FAKE_TMUX)):
        (bin_ / name).write_text(f"#!{sys.executable}\n{code}")
        (bin_ / name).chmod(0o755)
    test.enterContext(mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
                                                   "FAKE_ORCA_LOG": str(tmp / "log"), "FAKE_TMUX_LOG": str(tmp / "log"),
                                                   "FAKE_ORCA_SOURCE": "screen", "FAKE_TMUX_PID": "1"}))
    rec = tmp / ".foremind" / "carriers" / "orca" / f"{S}.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(json.dumps({"session": S, "handle": "term_1"}))
    return tmp / "log"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(["watch", *argv])
    return rc, out.getvalue().splitlines(), err.getvalue()


class FilterTest(unittest.TestCase):
    def test_key_events(self):
        def r(**e):
            return watch.render(json.dumps({"ts": "2026-10-12T03:04:05+00:00", "phase": "result", **e}))

        self.assertEqual(r(type="gate_result", batch="p.1", verdict="pass", heads={"main": "x"}),
                         '03:04:05 gate_result {"batch": "p.1", "verdict": "pass"}')
        for e in ({"type": "land_result", "outcome": "merged"}, {"type": "seat_open", "ok": True},
                  {"type": "batch_state", "to": "delivered"}, {"type": "batch_state", "to": "changes_requested"},
                  {"type": "batch_state", "prior": "approved", "state": "delivered"},  # review.set_state's form
                  {"type": "batch_state", "prior": "in_review", "state": "changes_requested"},
                  {"type": "batch_state", "prior": "failed", "state": "ready"},
                  {"type": "review_start_failed"}, {"type": "config_error"}, {"type": "sv_say", "error": "x"},
                  {"type": "paused", "seat_ancestor": "unknown"}, {"type": "batch_state", "to": "running",
                                                                  "seat_ancestor": S}):
            self.assertIsNotNone(r(**e), e)
        for e in ({"type": "seat_open", "phase": "intent"}, {"type": "batch_state", "to": "running"},
                  {"type": "batch_state", "prior": "running", "state": "review_ready"},
                  {"type": "sv_say", "ok": True}, {"type": "heartbeat"}):
            self.assertIsNone(r(**e), e)
        self.assertEqual(len(r(type="stuck", reason="x" * 400)), 250)
        self.assertEqual(r(type="stuck", reason="理由"), '03:04:05 stuck {"reason": "理由"}')
        self.assertEqual(watch.render("{not json"), "BAD LINE")
        self.assertEqual(watch.render("[1]"), "BAD LINE")

    def test_follow_rereads_a_shorter_file(self):
        p = Path(self.enterContext(tempfile.TemporaryDirectory())) / "events.jsonl"
        self.assertEqual(watch.follow(p, 0), ([], 0))
        p.write_text('{"type": "stuck"}\n{"type": "heartbeat"}\nbad\n{"type": "job_fa')  # last line still coming
        out, off = watch.follow(p, 0)
        self.assertEqual(out, [' stuck {}', "BAD LINE"])
        with p.open("a") as f:
            f.write('iled"}\n')
        out, off = watch.follow(p, off)
        self.assertEqual(out, [" job_failed {}"])
        p.write_text('{"type": "land_result"}\n')  # rotated: shorter than the offset
        self.assertEqual(watch.follow(p, off)[0], [" land_result {}"])

    def test_interval_and_not_in_a_project(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(os.environ, {}))
        os.environ.pop("FOREMIND_PROJECT", None)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(tmp)
        rc, out, err = run("--once")
        self.assertEqual((rc, out), (1, []))
        self.assertIn("foremind watch:", err)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["watch", "--interval", "9"])


class CheckTest(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict(os.environ, {"FOREMIND_PROJECT": str(self.root)}))
        self.sd = self.root / ".foremind"
        self.plan("p", {"state": "running"}, {"state": "delivered"}, {"state": "delivered"})
        (self.sd / "batches" / "p.1.lock").write_text(S)
        (self.sd / "decisions").mkdir()
        (self.sd / "decisions" / "Q-1.json").write_text(json.dumps({"id": "Q-1", "state": "open", "blocks": ["p.3"]}))
        (self.sd / "decisions" / "Q-2.json").write_text(json.dumps({"id": "Q-2", "state": "answered",
                                                                    "blocks": ["p.2"]}))
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{S}", session=S, carrier="fake")
        self.heartbeat("2026-01-01T00:00:00+00:00")
        self.log.append("sv_say", dedupe_id="sv_say:x", session=S, ok=True)

    def heartbeat(self, ts, **kw):
        (self.sd / "heartbeats").mkdir(exist_ok=True)
        (self.sd / "heartbeats" / f"{S}.json").write_text(json.dumps(
            {"session": S, "ts": ts, "tool_open": True, "handoff_requested": False, **kw}))

    def snapshot(self):
        return {p: p.read_bytes() for p in self.sd.rglob("*") if p.is_file()}

    def test_hits_read_only(self):
        self.carrier.screen = lambda s: ["", "  Enter to select · ↑/↓ to navigate · Esc to cancel"]
        before = self.snapshot()
        rc, out, _ = run("--once")
        self.assertEqual(rc, 0)
        self.assertEqual(out, [
            "PATROL: p.2 delivered 待合入",
            f"PATROL: {S}（p.1）在等终端里的选择题或确认",
            f"PATROL: {S} 终端空闲但心跳卡在 tool_open（2026-01-01T00:00:00+00:00），其后 "
            f"{self.events('sv_say')[-1]['ts']} 有投递未送达（发现 26）"])
        self.assertEqual(self.snapshot(), before)

    def test_misses(self):
        self.carrier.screen = lambda s: None  # screen-unavailable
        self.carrier.idle = False
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])
        self.carrier.idle = True
        self.carrier.screen = lambda s: ["Esc to cancel"]
        for kw in ({"ts": "2099-01-01T00:00:00+00:00"}, {"handoff_requested": True}, {"tool_open": False}):
            self.heartbeat(**{"ts": "2026-01-01T00:00:00+00:00", **kw})
            self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"], kw)
        self.heartbeat("2026-01-01T00:00:00")  # no time zone: not comparable, skipped
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])
        del self.carrier.screen  # a carrier with no screen (manual), one that fails: skipped
        self.carrier.read_state = mock.Mock(side_effect=carriers.CarrierError("boom"))
        self.heartbeat("2026-01-01T00:00:00+00:00")
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])
        (self.sd / "batches" / "p.1.lock").write_text("user")
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])

    def test_files_of_unexpected_shape(self):
        self.carrier.screen = lambda s: None
        self.carrier.idle = False
        d = self.sd / "decisions"
        (d / "Q-3.json").write_text(json.dumps({"id": 3, "state": "open", "blocks": 5}))
        (d / "Q-4.json").write_text(json.dumps({"id": "Q-4", "state": "open", "blocks": "p.2x"}))  # not a substring
        with (self.sd / "events.jsonl").open("a") as f:
            f.write('{"type": "plan_appr')  # half appended: the plan is still read
        rc, out, err = run("--once")
        self.assertEqual((rc, out, err), (0, ["PATROL: p.2 delivered 待合入"], ""))
        (d / "Q-5.json").write_text(json.dumps({"id": "Q-5", "state": "open", "blocks": "p.2"}))
        self.assertEqual(run("--once")[1], [])
        with mock.patch.object(watch.lock, "holder", side_effect=RuntimeError("boom")):
            rc, out, err = run("--once")
        self.assertEqual((rc, out), (0, []))
        self.assertIn("check failed: RuntimeError: boom", err)

    def test_carrier_from_the_launch_record_else_the_config(self):
        self.enterContext(mock.patch.object(carriers, "get", REAL_GET))
        log = fake_bins(self, self.root)
        self.heartbeat("2099-01-01T00:00:00+00:00")
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])  # carrier "fake": unknown, skipped
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{S}:2", session=S, carrier="orca")
        self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入", f"PATROL: {S}（p.1）在等终端里的选择题或确认"])
        self.assertEqual(log.read_text(), "terminal read --terminal term_1 --screen --json\n")
        self.log.append("seat_launch", phase="intent", dedupe_id=f"seat_launch:{S}:3", session=S, carrier="tmux")
        with mock.patch.dict(os.environ, {"FAKE_TMUX": "gone"}):
            self.assertEqual(run("--once")[1], ["PATROL: p.2 delivered 待合入"])
        self.assertIn(f"capture-pane -p -t ={S}:", log.read_text())
        s2 = "fm-p-p_1-2"  # no launch record: carrier.kind
        (self.sd / "batches" / "p.1.lock").write_text(s2)
        self.user_config('[quota]\nprobe_command = ["/usr/bin/true"]\n[carrier]\nkind = "orca"\n')
        (self.sd / "carriers" / "orca" / f"{s2}.json").write_text(json.dumps({"session": s2, "handle": "term_1"}))
        self.assertEqual(run("--once")[1][1:], [f"PATROL: {s2}（p.1）在等终端里的选择题或确认"])

    def test_follows_from_the_end_until_interrupted(self):
        self.log.append("stuck", batch="p.1", session=S)  # before the start: not shown
        self.carrier.screen = lambda s: None
        self.carrier.idle = False
        calls = []

        def sleep(_):
            calls.append(1)
            if len(calls) > 1:
                raise KeyboardInterrupt
            self.log.append("land_result", batch="p.2", outcome="merged")

        with mock.patch.object(watch.time, "sleep", sleep):
            rc, out, _ = run("--interval", "10")
        self.assertEqual(rc, 0)
        self.assertEqual(out[0], "PATROL: p.2 delivered 待合入")
        self.assertRegex(out[1], r'^\d\d:\d\d:\d\d land_result \{"batch": "p.2", "outcome": "merged"\}$')
        self.assertEqual(len(out), 2)


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.logf = fake_bins(self, self.tmp)

    def test_orca_reads_the_rendered_screen(self):
        c = carriers.get("orca", self.tmp)
        with mock.patch.dict(os.environ, {"FAKE_ORCA_SOURCE": "screen"}):
            self.assertEqual(c.screen(S), ["", "> q", "Enter to confirm · Esc to cancel"])
        self.assertEqual(self.logf.read_text(), "terminal read --terminal term_1 --screen --json\n")
        for src in ("screen-unavailable", "stream", "fail"):
            with mock.patch.dict(os.environ, {"FAKE_ORCA_SOURCE": src}):
                self.assertIsNone(c.screen(S), src)
        self.assertIsNone(c.screen("fm-p-p_1-9"))  # no record: not ours

    def test_tmux_captures_the_pane(self):
        c = carriers.get("tmux", self.tmp, {"carrier.tmux": {"socket": str(self.tmp / "s.sock")}})
        with mock.patch.dict(os.environ, {"FAKE_TMUX": "alive"}):
            self.assertEqual(c.screen(S), [])
        self.assertIn(f"capture-pane -p -t ={S}:", self.logf.read_text())
        with mock.patch.dict(os.environ, {"FAKE_TMUX": "gone"}):
            self.assertIsNone(c.screen(S))


if __name__ == "__main__":
    unittest.main()
