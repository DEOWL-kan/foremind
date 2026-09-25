import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from foremind import carriers, config
from foremind.carriers.manual import ManualCarrier
from foremind.events import EventLog
from foremind.fsutil import sha256_bytes
from foremind.lock import ExitEvidence
from foremind.vendors import Launch

# fake `orca terminal <verb> ... --json`, answering in the envelope M1-0 recorded; state in $FAKE_ORCA_STATE.
# Like Claude Code, the program in a new terminal rewrites its title at once.
FAKE_ORCA = r'''
import json, os, sys
state_path, events = os.environ["FAKE_ORCA_STATE"], os.environ["FAKE_EVENTS"]
st = json.load(open(state_path)) if os.path.exists(state_path) else {"n": 0, "terms": [], "log": []}
args = sys.argv[2:]
verb = args[0]
opt = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
st["log"].append(args)
shell = os.environ.get("FAKE_ORCA_SHELL") == "1"
term = next((t for t in st["terms"] if t["handle"] == opt.get("--terminal")), None)
def out(ok, result=None, code=None):
    json.dump(st, open(state_path, "w"))
    print(json.dumps({"id": "1", "ok": ok, **({"result": result} if ok else {"error": {"code": code, "message": code}}),
                      "_meta": {"runtimeId": "r"}}))
    sys.exit(0)
if verb == "list":  # $FAKE_ORCA_LIST_KEY: another container key than the `terminals` seen live
    out(True, {os.environ.get("FAKE_ORCA_LIST_KEY", "terminals"):
               [{"handle": t["handle"], "title": t["title"]} for t in st["terms"]]})
if verb == "create":
    st["n"] += 1
    st["terms"].append({"handle": f"term_{st['n']}", "title": "✳ Claude Code", "asked_title": opt["--title"],
                        "command": opt["--command"], "worktree": opt["--worktree"]})
    out(True, {"terminal": {"handle": f"term_{st['n']}", "surface": "visible"}})
if term is None:
    out(False, code="terminal_handle_stale")
if verb == "send":
    n = sum("program_delivery" in line for line in open(events)) if os.path.exists(events) else 0
    term.setdefault("sent", []).append({"text": opt["--text"], "events_before": n})
    out(True, {"send": {"prompt": {"stages": ["input_accepted"] if shell else ["input_accepted", "turn_started"]}}})
if verb == "read":
    out(True, {"terminal": {"status": "running", "tail": ["", "> ready"], "nextCursor": 2}})
if verb == "wait":
    out(True, {"wait": {"satisfied": True}}) if not shell else out(False, code="timeout")
if verb == "close":
    st["terms"].remove(term)
    out(False, code="terminal_handle_stale") if term.get("stale") else out(True, {"closeMode": "tab", "ptyKilled": False})
'''

# fake `tmux -S <socket> <verb> ...`: $FAKE_TMUX picks the answer, every call is logged to $FAKE_TMUX_LOG
FAKE_TMUX = r'''
import os, signal, sys, time
open(os.environ["FAKE_TMUX_LOG"], "a").write(" ".join(sys.argv[1:]) + "\n")
mode, verb = os.environ.get("FAKE_TMUX", "alive"), sys.argv[3]
if mode == "empty":  # exit 0, no output: read as absent
    sys.exit(0)
if mode == "gone":
    sys.exit(sys.stderr.write("can't find session: =fm-x\n") and 1)
if mode == "noserver":
    sys.exit(sys.stderr.write("no server running on /tmp/tmux-501/foremind\n") and 1)
if mode == "denied":
    sys.exit(sys.stderr.write("error connecting to /tmp/tmux-501/foremind (Permission denied)\n") and 1)
if mode == "duplicate" and verb == "new-session":
    sys.exit(sys.stderr.write("duplicate session: fm-x\n") and 1)
if verb == "list-panes":
    print(f"0 {int(time.time()) - 100} {os.environ['FAKE_TMUX_PID']}")
if verb == "kill-session" and mode == "alive":
    os.kill(int(os.environ["FAKE_TMUX_PID"]), signal.SIGTERM)
'''


class OrcaCarrierTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "orca").write_text(f"#!{sys.executable}\n{FAKE_ORCA}")
        (bin_ / "orca").chmod(0o755)
        self.state = self.tmp / "orca.json"
        self.events = self.tmp / ".foremind" / "events.jsonl"
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
                                                       "FAKE_ORCA_STATE": str(self.state),
                                                       "FAKE_EVENTS": str(self.events)}))
        self.c = carriers.get("orca", self.tmp)
        self.launch = Launch(["claude", "--settings", "/s.json"], {"FOREMIND_SESSION": "fm-p-p_1-1"}, self.tmp / "wt")

    def st(self):
        return json.loads(self.state.read_text())

    def test_five_actions_by_handle_after_the_title_changed(self):
        self.c.create("fm-p-p_1-1", self.launch)
        terms = self.st()["terms"]
        self.assertEqual(len(terms), 1)
        self.assertEqual((terms[0]["asked_title"], terms[0]["title"]), ("fm-p-p_1-1", "✳ Claude Code"))
        self.assertEqual(terms[0]["worktree"], f"path:{self.tmp / 'wt'}")
        self.assertIn("FOREMIND_SESSION=fm-p-p_1-1 claude --settings /s.json", terms[0]["command"])
        rec = json.loads((self.tmp / ".foremind" / "carriers" / "orca" / "fm-p-p_1-1.json").read_text())
        self.assertEqual(rec, {"session": "fm-p-p_1-1", "handle": "term_1"})
        with self.assertRaises(carriers.SessionExists):  # never created twice, never reused
            self.c.create("fm-p-p_1-1", self.launch)
        self.assertEqual(len(self.st()["terms"]), 1)
        self.assertEqual(self.c.list_sessions(), ["fm-p-p_1-1"])  # mapped back through the record

        self.assertTrue(self.c.deliver("fm-p-p_1-1", "  开工\r\n第二行\r"))
        sent = self.st()["terms"][0]["sent"]
        self.assertEqual(sent, [{"text": "开工\n第二行", "events_before": 1}])  # intent on disk before sending
        ev = [e for e in EventLog(self.events).iter() if e["type"] == "program_delivery"]
        self.assertEqual([e["phase"] for e in ev], ["intent", "result"])
        self.assertEqual(ev[0]["text_sha256"], sha256_bytes("开工\n第二行".encode()))  # I42: normalized first
        self.assertEqual((ev[1]["ok"], ev[1]["confirmed"]), (True, True))

        state = self.c.read_state("fm-p-p_1-1")
        self.assertEqual((state.alive, state.idle, state.tail), (True, True, ["> ready"]))
        self.assertTrue(self.c.wait_idle("fm-p-p_1-1", 5))
        self.assertEqual(self.c.close("fm-p-p_1-1"), ExitEvidence("fm-p-p_1-1", "orca", "absent"))
        self.assertEqual(self.c.list_sessions(), [])
        self.assertEqual(self.c.read_state("fm-p-p_1-1").alive, False)

    def test_unknown_session_confirms_nothing(self):
        self.c.create("fm-p-p_1-1", self.launch)
        self.assertIsNone(self.c.close("fm-p-p_1-2"))  # no record: not "absent", just unknown
        self.assertIsNone(self.c.read_state("fm-p-p_1-2").alive)
        self.assertEqual(len(self.st()["terms"]), 1)

    def test_shell_terminal_is_unconfirmed_and_never_idle(self):
        os.environ["FAKE_ORCA_SHELL"] = "1"
        self.c.create("fm-p-p_1-1", self.launch)
        self.assertIsNone(self.c.send("fm-p-p_1-1", "echo hi"))
        self.assertFalse(self.c.wait_idle("fm-p-p_1-1", 0.1))

    def test_close_stale_handle_counts_as_closed(self):
        self.c.create("fm-p-p_1-1", self.launch)
        st = self.st()
        st["terms"][0]["stale"] = True  # listed, but close answers terminal_handle_stale
        self.state.write_text(json.dumps(st))
        self.assertEqual(self.c.close("fm-p-p_1-1").how, "absent")

    def test_unreadable_list_is_never_evidence(self):
        # MF-A: a list under another key must not read as "gone"
        self.c.create("fm-p-p_1-1", self.launch)
        os.environ["FAKE_ORCA_LIST_KEY"] = "items"
        with self.assertRaises(carriers.CarrierError):
            self.c.read_state("fm-p-p_1-1")
        with self.assertRaises(carriers.CarrierError):
            self.c.close("fm-p-p_1-1")
        self.assertEqual(self.st()["log"][-2][0], "close")  # close was still asked, before the list

    def test_close_unconfirmed_while_still_listed(self):
        self.c.create("fm-p-p_1-1", self.launch)
        self.c.exit_timeout_s = 0.2
        with mock.patch.object(self.c, "_listed", return_value={"term_1"}):
            self.assertIsNone(self.c.close("fm-p-p_1-1"))

    def test_send_to_unknown_session_fails_and_is_recorded(self):
        with self.assertRaises(carriers.CarrierError):
            self.c.deliver("fm-nobody", "hi")
        ev = [e for e in EventLog(self.events).iter() if e["type"] == "program_delivery"]
        self.assertEqual([(e["phase"], e.get("ok")) for e in ev], [("intent", None), ("result", False)])


class TmuxCarrierTest(unittest.TestCase):
    """tmux answers faked: what counts as absent, what is an error, and the fixed socket (MF-1, SF-9)."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "tmux").write_text(f"#!{sys.executable}\n{FAKE_TMUX}")
        (bin_ / "tmux").chmod(0o755)
        self.log = self.tmp / "tmux.log"
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
                                                       "FAKE_TMUX_LOG": str(self.log), "FAKE_TMUX_PID": "1",
                                                       "HOME": str(self.tmp), "FOREMIND_CONFIG_HOME": str(self.tmp)}))
        self.c = carriers.get("tmux", self.tmp)
        self.default_sock = self.tmp / ".local" / "state" / "foremind" / "tmux" / "foremind.sock"

    def mode(self, m):
        os.environ["FAKE_TMUX"] = m

    def test_absent_answers(self):
        for m in ("empty", "gone", "noserver"):
            with self.subTest(mode=m):
                self.mode(m)
                self.assertEqual(self.c.read_state("fm-x").alive, False)
                self.assertFalse(self.c.wait_idle("fm-x", 0))
                self.assertEqual(self.c.close("fm-x"), ExitEvidence("fm-x", "tmux", "absent"))
        self.mode("noserver")
        self.assertEqual(self.c.list_sessions(), [])

    def test_other_errors_are_never_evidence(self):
        self.mode("denied")
        for call in (lambda: self.c.read_state("fm-x"), lambda: self.c.close("fm-x"), self.c.list_sessions):
            with self.assertRaises(carriers.CarrierError):
                call()

    def test_fixed_socket_and_list_panes(self):
        self.mode("gone")
        self.c.read_state("fm-x")
        (self.tmp / "foremind.toml").write_text(f'[carrier.tmux]\nsocket = "{self.tmp}/s/fm.sock"\n')
        other = carriers.get("tmux", self.tmp, config.load(self.tmp))  # SF-B: the table as config.load keeps it
        other.close("fm-x")
        # SF-A: a session goes to the socket its seat_launch recorded, not to today's config
        EventLog(self.tmp / ".foremind" / "events.jsonl").append(
            "seat_launch", phase="intent", dedupe_id="seat_launch:fm-y", session="fm-y", carrier="tmux",
            socket="/rec/fm.sock")
        other.close("fm-y")
        calls = self.log.read_text().splitlines()
        self.assertTrue(calls[0].startswith(f"-S {self.default_sock} list-panes -t =fm-x: -F "), calls[0])
        self.assertTrue(calls[1].startswith(f"-S {self.tmp}/s/fm.sock list-panes -t =fm-x:"), calls[1])
        self.assertTrue(calls[2].startswith("-S /rec/fm.sock list-panes -t =fm-y:"), calls[2])
        with self.assertRaises(carriers.CarrierError):  # a relative path would depend on the cwd
            carriers.get("tmux", self.tmp, {"carrier.tmux": {"socket": "fm.sock"}})

    def test_existing_session_is_not_reused(self):
        self.mode("duplicate")
        with self.assertRaises(carriers.SessionExists):
            self.c.create("fm-x", Launch(["sh"], {}, self.tmp))
        self.assertTrue(self.default_sock.parent.is_dir())  # created for tmux -S

    def test_close_waits_for_the_pane_process(self):
        self.mode("alive")
        p = subprocess.Popen(["sleep", "30"])
        threading.Thread(target=p.wait, daemon=True).start()  # reap it, or the zombie looks alive
        os.environ["FAKE_TMUX_PID"] = str(p.pid)
        self.assertTrue(self.c.read_state("fm-x").alive)
        self.assertEqual(self.c.close("fm-x"), ExitEvidence("fm-x", "tmux", "pid_exited"))
        q = subprocess.Popen(["sleep", "30"])
        self.addCleanup(q.wait)
        self.addCleanup(q.kill)
        os.environ["FAKE_TMUX_PID"] = str(q.pid)
        self.mode("stubborn")  # kill-session does not end it
        self.c.exit_timeout_s = 0.3
        self.assertIsNone(self.c.close("fm-x"))

    def test_timeout_is_a_carrier_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(["tmux"], 60)):
            with self.assertRaises(carriers.CarrierError):
                self.c.list_sessions()


class ManualCarrierTest(unittest.TestCase):
    def test_prints_and_confirms_nothing(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        out = io.StringIO()
        c = ManualCarrier(tmp, out=out)
        c.create("fm-p-p_1-1", Launch(["claude", "--permission-mode", "acceptEdits"], {"FOREMIND_BATCH": "p.1"}, tmp))
        self.assertIsNone(c.deliver("fm-p-p_1-1", "开工"))
        self.assertIsNone(c.close("fm-p-p_1-1"))  # the user has to confirm the exit
        self.assertIsNone(c.list_sessions())
        self.assertEqual(c.read_state("fm-p-p_1-1").alive, None)
        text = out.getvalue()
        self.assertIn(f"cd {tmp} && env FOREMIND_BATCH=p.1 claude --permission-mode acceptEdits", text)
        self.assertIn("开工", text)
        with self.assertRaises(carriers.CarrierError):
            carriers.get("herdr", tmp)


@unittest.skipUnless(os.environ.get("FOREMIND_IT") == "1", "tmux integration test: set FOREMIND_IT=1")
class TmuxCarrierIT(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.name = f"fm-it-{os.getpid()}"
        self.c = carriers.get("tmux", self.tmp, {"carrier.tmux": {"socket": str(self.tmp / "tmux" / "it.sock")}})
        self.c.quiet_s = 1
        self.addCleanup(subprocess.run, ["tmux", "-S", self.c.socket, "kill-server"], capture_output=True)

    def wait_for(self, cond, limit=10):
        deadline = time.monotonic() + limit
        while not cond():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.1)

    def test_lifecycle(self):
        self.assertEqual(self.c.close(self.name).how, "absent")  # no server on the socket yet
        self.assertEqual(self.c.list_sessions(), [])
        script = 'echo "$FOREMIND_SESSION" > env.txt; while read l; do echo "got:$l" >> out.txt; done'
        launch = Launch(["sh", "-c", script], {"FOREMIND_SESSION": self.name}, self.tmp)
        self.c.create(self.name, launch)
        with self.assertRaises(carriers.SessionExists):
            self.c.create(self.name, launch)
        self.wait_for(lambda: (self.tmp / "env.txt").exists())
        self.assertEqual((self.tmp / "env.txt").read_text().strip(), self.name)
        self.assertIn(self.name, self.c.list_sessions())
        self.assertTrue(self.c.read_state(self.name).alive)
        self.assertIsNone(self.c.deliver(self.name, "hello world"))  # tmux cannot confirm
        self.c.deliver(self.name, "line a\nline b")
        out = self.tmp / "out.txt"
        self.wait_for(lambda: out.exists() and out.read_text().count("got:") == 3)
        self.assertEqual(out.read_text().splitlines(), ["got:hello world", "got:line a", "got:line b"])
        self.assertTrue(self.c.wait_idle(self.name, 10))
        self.assertEqual(self.c.close(self.name), ExitEvidence(self.name, "tmux", "pid_exited"))
        self.assertNotIn(self.name, self.c.list_sessions())
        self.assertFalse(self.c.read_state(self.name).alive)
        self.assertEqual(self.c.close(self.name).how, "absent")


if __name__ == "__main__":
    unittest.main()
