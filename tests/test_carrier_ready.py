"""m3a.4 REQ-9: tmux and Orca send only to a Claude Code input box that is ready; dialogs get nothing, at once.

Screens are Claude Code v2.1.280. S1-S5: handoff/m3a4-screens-2026-10-13.md (tmux 200x50, capture-pane -p, trailing
blanks stripped and blank runs merged there; the input line is "❯ " as captured; RULE stands for a full-width rule).
S-A1..S-B5: handoff/m3a4-screens-modes-2026-10-13.md (the same capture in auto and bypassPermissions). ORCA_SHELL: the
bottom of an Orca `read --screen` of this project's exited seat (m3a.4, 2026-10-13). CHOICE: watch's AskUserQuestion
line (controller-8).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from foremind import carriers, inbox  # noqa: E402
from foremind.carriers import orca, ready, tmux  # noqa: E402
from foremind.commands import watch  # noqa: E402
from foremind.events import EventLog  # noqa: E402
from foremind.vendors import Launch  # noqa: E402
from test_carriers import FAKE_ORCA, FAKE_TMUX, NOBODY  # noqa: E402
from test_supervisor import Base  # noqa: E402


def screen(text, width=200):
    return ["─" * width if x == "RULE" else x for x in text.split("\n")]


BANNER = """
 ▐▛███▛█   Claude Code v2.1.280
▝▜██████▀  Opus 5.5 with high effort · Claude Max
  ▝▝ ▝▝    /private/tmp/claude-501/-Users-me-project/ec411d43-536d-4989-a195-f403e17f9239/scratchpad/probe-repo
"""
BOX = "RULE\n❯ \nRULE\n"  # the input line as captured, with its trailing blank
MODE = "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"
S1 = screen("""
RULE
 Accessing workspace:

 /private/tmp/claude-501/-Users-me-project/ec411d43-536d-4989-a195-f403e17f9239/scratchpad/probe-repo

 Quick safety check: Is this a project you created or one you trust? (Like your own code, a well-known open source \
project, or work from your team). If not, take a moment to review what's in this
 folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel""")
S2 = screen(BANNER + """
""" + " " * 178 + """● high · /effort
""" + BOX + """  effort high | quota unknown
""" + MODE)
TURN = """
❯ Use the Bash tool to run exactly: sleep 12 && ls -la
"""
DONE = TURN + """
  Ran 1 shell command

⏺ I ran the command and it finished without errors. After the 12-second wait, the directory listing showed only two \
entries:

  - .git/
  - README.md (8 bytes, not yet tracked by git)

✻ Crunched for 24s · done 1:03 AM
"""
STATUS = "  ctx 5% | effort high | five_hour used 23% | seven_day used 64%\n"
S3 = screen(BANNER + TURN + """
  Waiting 12 seconds, then listing directory contents · 5s
  ⎿  $ sleep 12 && ls -la (6s)
     (ctrl+b ctrl+b (twice) to run in background)

· Boogieing… (15s · ↓ 229 tokens)

""" + BOX + STATUS + MODE)
S3A = screen(BANNER + TURN + """
✽ Fermenting…
""" + " " * 98 + """tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf and reattach for focus tracking
""" + BOX + """  effort high | quota unknown
""" + MODE)
S2B = screen(BANNER + DONE + "\n" + BOX + STATUS + MODE)
S4 = screen(BANNER + DONE + """
❯ Use the Bash tool to run exactly: git add README.md

⏺ Staging README.md
  ⎿  $ git add README.md

RULE
 Bash command
 Tip: auto mode handles these prompts for you — choose "switch to auto mode" below

   git add README.md
   Stage README.md

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: git add *
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No

 Esc to cancel · Tab to amend""")
S5 = screen(BANNER + DONE + """
❯ Use the Bash tool to run exactly: git add README.md

  Ran 1 shell command
  ⎿  Interrupted · What should Claude do instead?

✻ Cooked for 2s · done 1:04 AM

""" + " " * 178 + "● high · /effort\n" + BOX + STATUS + MODE)


def banner(repo):
    return BANNER.replace("probe-repo", repo)


def trust(repo):
    return [x.replace("probe-repo", repo) for x in S1]


# S-A*, S-B*: handoff/m3a4-screens-modes-2026-10-13.md (auto and bypassPermissions, same capture as S1-S5). With a
# background shell the mode line drops "(shift+tab to cycle)" for "· 1 shell ·", even after the turn ended (S-A4,
# S-B4): Claude Code starts a turn of its own when the shell ends (S-A5), so that state counts as busy
FOCUS = (" " * 98 + "tmux focus-events off · add 'set -g focus-events on' to ~/.tmux.conf and reattach for focus"
         " tracking\n")
STATUS_15 = "  ctx 5% | effort high | five_hour used 15% | seven_day used 73%\n"
STATUS_16 = STATUS_15.replace("used 15%", "used 16%")
AUTO, AUTO_BG = "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents", "  ⏵⏵ auto mode on · 1 shell · ← for agents"
BYPASS = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
BYPASS_BG = "  ⏵⏵ bypass permissions on · 1 shell · ← for agents"
LS = """  total 8
  drwxr-xr-x@  4 user  wheel  128 Oct 13 14:28 .
  drwx------@ 18 user  wheel  576 Oct 13 14:28 ..
  drwxr-xr-x@  9 user  wheel  288 Oct 13 14:28 .git
  -rw-r--r--@  1 user  wheel    8 Oct 13 14:28 README.md

  The directory holds only .git and an 8-byte README.md.
"""
A_STARTED = TURN + """
⏺ The tool blocks a foreground sleep, so I'll run the exact command in the background.
"""
A_ENDED = A_STARTED + """
  Ran 1 shell command

⏺ I started sleep 12 && ls -la exactly as written. It's running in the background because this tool won't run sleep \
in the foreground. The ls -la output will show up here when it finishes in about 12
  seconds.

✻ Sautéed for 7s · done 2:29 PM"""
B_ENDED = TURN + """
  Ran 1 shell command

⏺ I'm running sleep 12 && ls -la exactly as written, but in the background, because this environment blocks sleep \
when it runs in the foreground. It's still going, and I'll post the ls -la output when
  it finishes in about 12 seconds.

✻ Crunched for 8s · done 2:29 PM"""
BG_DONE = """

⏺ Background command "Wait 12 seconds then list directory contents" completed (exit code 0)

  Read 1 file

"""
SA1, SB1 = trust("probe-auto"), trust("probe-bypassPermissions")
SA2 = screen(banner("probe-auto") + "\n" + " " * 178 + "● high · /effort\n" + BOX + "  effort high | quota unknown\n"
             + AUTO)
SB2 = screen(banner("probe-bypassPermissions") + "\n" + " " * 178 + "● high · /effort\n" + BOX
             + "  effort high | quota unknown\n" + BYPASS)
SA3 = screen(banner("probe-auto") + A_STARTED + """
⏺ Waiting 12 seconds then listing directory contents
  ⎿  $ sleep 12 && ls -la

✳ Transmuting… (5s · ↓ 298 tokens · thought for 1s)
""" + FOCUS + BOX + STATUS_15 + AUTO_BG)
SB3 = screen(banner("probe-bypassPermissions") + TURN + """
⏺ Waiting 12 seconds then listing directory contents
  ⎿  $ sleep 12 && ls -la

· Channeling… (5s · ↓ 175 tokens · thought for 2s)
""" + FOCUS + BOX + "  effort high | quota unknown\n" + BYPASS_BG)
SA4 = screen(banner("probe-auto") + A_ENDED + " · 1 shell still running\n\n" + BOX + STATUS_15 + AUTO_BG)
SB4 = screen(banner("probe-bypassPermissions") + B_ENDED + " · 1 shell still running\n\n" + BOX + STATUS_15 + BYPASS_BG)
SA5 = screen(banner("probe-auto") + A_ENDED + BG_DONE + "⏺ The command finished successfully (exit code 0):\n\n" + LS
             + "\n✻ Cogitated for 4s · done 2:29 PM\n\n" + BOX + STATUS_16 + AUTO)
SB5 = screen(banner("probe-bypassPermissions") + B_ENDED + BG_DONE
             + "⏺ sleep 12 && ls -la finished without errors (exit code 0):\n\n" + LS
             + "\n✻ Churned for 5s · done 2:29 PM\n\n" + BOX + STATUS_16 + BYPASS)
ORCA_SHELL = ["Resume this session with:", "claude --resume cda1072e-193f-4807-8fbe-4c488f4c4b4a",
              "user@MacBook-Pro main %"]  # claude exited, the Orca terminal (status running) shows its shell
CHOICE = ["", "  Enter to select · ↑/↓ to navigate · Esc to cancel"]


class ClassifyTest(unittest.TestCase):
    def test_screens(self):
        for name, scr, want in [
                ("S1", S1, "dialog"), ("S4", S4, "dialog"), ("CHOICE", CHOICE, "dialog"),
                ("S-A1", SA1, "dialog"), ("S-B1", SB1, "dialog"),
                ("S2", S2, "ready"), ("S2B", S2B, "ready"), ("S5", S5, "ready"), ("S-A2", SA2, "ready"),
                ("S-A5", SA5, "ready"), ("S-B2", SB2, "ready"), ("S-B5", SB5, "ready"),
                ("S3", S3, "busy"), ("S3A", S3A, "busy"), ("S-A3", SA3, "busy"), ("S-B3", SB3, "busy"),
                ("S-A4 background shell", SA4, "busy"), ("S-B4 background shell", SB4, "busy"),
                ("'*' frame, never seen here: refused as busy", [x.replace("· Boogieing", "* Boogieing") for x in S3],
                 "busy"),
                ("ORCA_SHELL", ORCA_SHELL, "unknown"), ("banner only", screen(BANNER), "unknown"),
                ("empty", [], "unknown"),
                ("prompt alone", ["❯ "], "unknown"),
                ("typed text in the box", [x.replace("❯ ", "❯ half typed") for x in S2], "unknown"),
                ("no mode line", S2[:-1], "unknown"),
                ("default mode, never captured", S2[:-1] + ["  ? for shortcuts"], "unknown"),
                ("an input box in the history, then a shell", S2 + ["", "user@host main %"], "unknown"),
                ("trailing blank rows", S2 + ["", "   ", ""], "ready")]:
            with self.subTest(name):
                self.assertEqual(ready.classify(scr), want)

    def test_one_definition_with_watch(self):
        self.assertIs(watch.CHOICE_MARKERS, ready.CHOICE_MARKERS)
        self.assertEqual(ready.CHOICE_MARKERS, ("Enter to select", "Enter to confirm"))


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


class Cases:
    """Both carriers, through deliver() (the program's path); self.c is the carrier under test."""

    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.scr = self.tmp / "screens.json"
        self.clock = Clock()
        self.enterContext(mock.patch.object(ready, "time", self.clock))
        self.enterContext(mock.patch.object(carriers.Carrier, "ps", staticmethod(lambda: NOBODY)))
        self.events = self.tmp / ".foremind" / "events.jsonl"
        self.n = 0

    def fake(self, name, code, env):
        (self.bin / name).write_text(f"#!{sys.executable}\n{code}")
        (self.bin / name).chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", **env}))

    def screens(self, *seq):
        self.scr.write_text(json.dumps(list(seq)))

    def new(self):
        """A session this carrier instance created: its first send may wait."""
        self.n += 1
        s = f"fm-p-p_1-{self.n}"
        self.c.create(s, Launch(["claude"], {}, self.tmp))
        return s

    def results(self):
        return [e for e in EventLog(self.events).iter() if e["type"] == "program_delivery" and e["phase"] == "result"]

    def refused(self, s, *needles):
        with self.assertRaises(ready.NotReady) as e:
            self.c.deliver(s, "go")
        msg = str(e.exception)
        for n in (s, *needles):
            self.assertIn(n, msg)
        self.assertEqual(self.results()[-1]["ok"], False)
        return msg

    def test_dialogs_get_nothing_at_once(self):
        for scr in (S1, S4, CHOICE):
            with self.subTest(scr[-1]):
                self.screens(scr)
                s, looks = self.new(), self.looks()
                msg = self.refused(s, ready.DIALOG, "nothing sent", "Last screen lines:\n")
                tail = msg.split("Last screen lines:\n")[1].splitlines()
                self.assertEqual(tail, [f"  | {x.rstrip()}" for x in scr if x.strip()][-8:])
                self.assertEqual((self.typed(), self.clock.slept, self.looks() - looks), ([], [], 1))

    def test_first_send_after_create_waits_for_the_start_once(self):
        s = self.new()
        self.screens([], screen(BANNER), S3A, S2)
        self.c.deliver(s, "go")
        self.assertEqual(self.clock.slept, [ready.POLL_S] * 3)
        self.assertEqual(len(self.typed()), self.SENT)
        self.screens(screen(BANNER), S2)
        looks = self.looks()
        self.refused(s, "not ready for input: no empty Claude Code input box")  # used up: one look
        self.assertEqual((self.looks() - looks, self.clock.slept), (1, [ready.POLL_S] * 3))

    def test_first_wait_is_bounded(self):
        s = self.new()
        self.screens(S3)
        self.refused(s, "not ready for input within 30 s: busy")
        self.assertEqual((len(self.clock.slept), self.clock.t), (60, 30))
        self.assertEqual(self.typed(), [])

    def test_a_failed_first_send_does_not_wait_again(self):
        s = self.new()
        self.screens(S4)
        self.refused(s, ready.DIALOG)
        self.screens(ORCA_SHELL, S2)
        self.refused(s, "not ready for input:")
        self.assertEqual((self.clock.slept, self.typed()), ([], []))

    def test_existing_session_one_look(self):
        s = self.existing()
        for scr, why in ((S3, "busy: a turn or a background task"), (SA4, "busy:"), (ORCA_SHELL, "input box")):
            with self.subTest(why):
                self.screens(scr, S2)
                looks = self.looks()
                self.refused(s, why)
                self.assertEqual((self.looks() - looks, self.clock.slept, self.typed()), (1, [], []))
        self.screens(S5)
        self.c.deliver(s, "go")
        self.assertEqual(len(self.typed()), self.SENT)

    def test_unreadable_screen(self):
        s = self.new()
        self.screens(None, S2)
        self.refused(s, "screen unavailable")
        self.assertEqual((self.clock.slept, self.typed()), ([], []))

    def test_probe_io_is_short(self):
        s = self.existing()
        self.screens(S2)
        seen = []
        real = self.module.run

        def run(argv, input=None, timeout=60):
            seen.append((argv[3] if argv[0] == "tmux" else argv[2], timeout))
            return real(argv, input=input, timeout=timeout)
        with mock.patch.object(self.module, "run", run):
            self.c.deliver(s, "go")
        probes = [t for verb, t in seen if verb in ("list-panes", "capture-pane", "list", "read")]
        self.assertTrue(probes)
        self.assertEqual(set(probes), {ready.IO_TIMEOUT_S})


class TmuxTest(Cases, unittest.TestCase):
    module, SENT = tmux, 3

    def setUp(self):
        super().setUp()
        self.log = self.tmp / "tmux.log"
        self.fake("tmux", FAKE_TMUX, {"FAKE_TMUX_LOG": str(self.log), "FAKE_TMUX_PID": "1",
                                      "FAKE_TMUX_SCREENS": str(self.scr), "FAKE_TMUX": "alive"})
        self.enterContext(mock.patch.object(tmux, "_PASTE_SETTLE_S", 0))
        self.c = carriers.get("tmux", self.tmp, {"carrier.tmux": {"socket": str(self.tmp / "s.sock")}})

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def typed(self):
        return [c for c in self.calls() if c.split()[2] in ("load-buffer", "paste-buffer", "send-keys")]

    def looks(self):
        return sum(c.split()[2] == "capture-pane" for c in self.calls())

    def existing(self):
        return "fm-x"  # tmux has it, this instance never created it

    def test_dialog_after_the_paste_gets_no_enter(self):
        s = self.existing()
        self.screens(S2, S4)
        self.refused(s, ready.DIALOG, "the pasted text was not submitted")
        self.assertEqual([c.split()[2] for c in self.typed()], ["load-buffer", "paste-buffer"])

    def test_exited(self):
        s = self.new()
        os.environ["FAKE_TMUX"] = "gone"
        self.refused(s, "the session has exited")
        self.assertEqual((self.clock.slept, self.looks()), ([], 0))


class OrcaTest(Cases, unittest.TestCase):
    module, SENT = orca, 1

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "orca.json"
        self.fake("orca", FAKE_ORCA, {"FAKE_ORCA_STATE": str(self.state), "FAKE_EVENTS": str(self.events),
                                      "FAKE_ORCA_SCREENS": str(self.scr)})
        self.c = carriers.get("orca", self.tmp)

    def log(self):
        return json.loads(self.state.read_text())["log"] if self.state.exists() else []

    def typed(self):
        return [a for a in self.log() if a[0] == "send"]

    def looks(self):
        return sum(a[0] == "read" and "--screen" in a for a in self.log())

    def existing(self):
        self.n += 1
        s = f"fm-p-p_1-{self.n}"
        carriers.get("orca", self.tmp).create(s, Launch(["claude"], {}, self.tmp))  # another instance created it
        return s

    def test_confirmation_and_no_wait_idle(self):
        s = self.existing()
        self.screens(SB5)
        self.assertTrue(self.c.deliver(s, "go"))  # turn_started, as before
        self.assertNotIn("wait", [a[0] for a in self.log()])

    def test_exited(self):
        s = self.new()
        self.screens("exited")
        self.refused(s, "the session has exited")
        st = json.loads(self.state.read_text())
        st["terms"] = []  # gone from list
        self.state.write_text(json.dumps(st))
        self.screens(S2)
        looks = self.looks()
        self.refused(s, "the session has exited")
        self.assertEqual((self.clock.slept, self.typed(), self.looks() - looks), ([], [], 0))


class TickTest(Base):
    """A refused inbox delivery: sv_deliver failed, the cursor stays, one look; delivered once the screen is ready."""

    def test_refused_then_delivered(self):
        self.plan("p")
        self.hold("p.1", "fm-s")
        tmp = self.tmp
        scr, state = tmp / "screens.json", tmp / "orca.json"
        bin_ = tmp / "bin"
        bin_.mkdir()
        (bin_ / "orca").write_text(f"#!{sys.executable}\n{FAKE_ORCA}")
        (bin_ / "orca").chmod(0o755)
        self.enterContext(mock.patch.dict(os.environ, {
            "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}", "FAKE_ORCA_STATE": str(state),
            "FAKE_EVENTS": str(self.root / ".foremind" / "events.jsonl"), "FAKE_ORCA_SCREENS": str(scr)}))
        clock = Clock()
        self.enterContext(mock.patch.object(ready, "time", clock))
        orca.OrcaCarrier(self.root).create("fm-s", Launch(["claude"], {}, self.root))
        self.carrier = orca.OrcaCarrier(self.root)  # what tick gets: never created anything
        inbox.append("fm-s", "hello", sender="user", root=self.root)
        scr.write_text(json.dumps([S4]))
        self.tick()
        [r] = self.events("sv_deliver", "result")
        self.assertEqual(r["ok"], False)
        self.assertIn(ready.DIALOG, r["error"])
        self.assertEqual(len(inbox.pending_messages("fm-s", root=self.root)), 1)
        log = json.loads(state.read_text())["log"]
        self.assertEqual(([a for a in log if a[0] == "send"], sum("--screen" in a for a in log), clock.slept),
                         ([], 1, []))
        scr.write_text(json.dumps([SA3]))
        self.tick(30)
        self.assertEqual(len(inbox.pending_messages("fm-s", root=self.root)), 1)
        self.assertEqual(clock.slept, [])
        scr.write_text(json.dumps([SA5]))
        self.tick(60)
        self.assertEqual(inbox.pending_messages("fm-s", root=self.root), [])
        self.assertEqual([e["ok"] for e in self.events("sv_deliver", "result")], [False, False, True])


if __name__ == "__main__":
    unittest.main()
