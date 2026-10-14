"""Is a Claude Code session ready for typed input (REQ-9, m3a.4)? tmux and Orca ask before every send.

Every rule here is read off Claude Code v2.1.280 screens: watch's choice markers (controller-8, 2026-10-10, m2f.6),
handoff/m3a4-screens-2026-10-13.md (S1-S5, accept edits) and handoff/m3a4-screens-modes-2026-10-13.md (S-A*, S-B*:
auto and bypassPermissions), both over tmux. A dialog anywhere on screen refuses at once: Enter would pick its default
(S1's "No, exit") or approve a command. Ready needs the whole input area, never `❯` alone: S1's "❯ No, exit" and S4's
"❯ 1. Yes" have one, and the busy screen draws the same input box (S3).
"""
import re
import time

from foremind.carriers import CarrierError

# AskUserQuestion and confirmation dialogs (watch). "Esc to cancel" alone would match too much.
CHOICE_MARKERS = ("Enter to select", "Enter to confirm")
TRUST_MARKERS = ("Quick safety check", "Yes, I trust this folder", "No, exit")  # S1
PERMISSION_MARKERS = ("This command requires approval", "Do you want to proceed?")  # S4: footer has no CHOICE_MARKERS
DIALOG_MARKERS = CHOICE_MARKERS + TRUST_MARKERS + PERMISSION_MARKERS
# a running turn: the spinner line above the input box, "<glyph> <verb>…" then maybe "(9s · ↓ 222 tokens)" (S3, S3a);
# v2.1.280 shows no "esc to interrupt". A finished turn's "✻ Crunched for 24s · done 1:03 AM" has no such "…".
# "*" was not seen on macOS; other terminals are said to draw it for one frame: counted as busy (refusing side)
BUSY = re.compile(r"[·✢✳✶✻✽*] [^·(…]+…")
# the input box: a full-width rule, the empty prompt, a rule (S2, S2b, S5); below it only indented lines (a statusLine
# of the user's own, the mode line) and among them the mode line of a permission mode seen idle. Known block: the
# other permission modes config allows (default's "? for shortcuts", plan, dontAsk, manual) were not captured, so a
# session in them is refused every time until their v2.1.280 screens are added here
RULE = re.compile(r"─{20,}")
PROMPT = "❯"
MODE_LINE = re.compile(r"\s+⏵⏵ (accept edits|auto mode|bypass permissions) on\b")
# in the mode line only when no background task runs: a background shell shows "· 1 shell ·" there instead, also after
# the turn ended ("· 1 shell still running", S-A4, S-B4), and Claude Code starts a turn itself when it ends (S-A5): busy
IDLE_HINT = "(shift+tab to cycle)"
FIRST_WAIT_S = 30  # only the first send after the carrier's own create() waits for the TUI to start
POLL_S = 0.5
TAIL_LINES = 8  # screen lines quoted in a refusal
IO_TIMEOUT_S = 5  # one presence or screen read
DIALOG = ("Claude Code is showing a trust or permission dialog, which Foremind does not answer; "
          "handle it in that session first, then retry")


class NotReady(CarrierError):
    pass


def classify(lines) -> str:
    """"dialog", "busy", "ready" or "unknown" for a rendered screen."""
    s = [x.rstrip() for x in lines]
    if any(m in x for x in s for m in DIALOG_MARKERS):
        return "dialog"
    while s and not s[-1]:
        s.pop()
    rules = [i for i, x in enumerate(s) if RULE.fullmatch(x)]
    if len(rules) < 2 or rules[-1] - rules[-2] != 2 or s[rules[-2] + 1] != PROMPT:
        return "unknown"
    under = s[rules[-1] + 1:]
    mode = next((x for x in under if MODE_LINE.match(x)), None)
    if mode is None or any(x and not x[0].isspace() for x in under):
        return "unknown"
    return "busy" if IDLE_HINT not in mode or any(BUSY.match(x) for x in s[:rules[-2]]) else "ready"


def wait(session, probe, wait_s=0):
    """Return once the screen is ready, else raise NotReady. `probe()` -> (alive, rendered lines or None when the screen
    cannot be read). A dialog, an exit or an unreadable screen fail at once; busy and unknown are looked at again every
    POLL_S only within `wait_s`: one look when 0 (tick, an existing session, say --direct). A look started before the
    deadline still runs its reads (two of IO_TIMEOUT_S at most): the first send gives up within about wait_s + 10 s."""
    deadline = time.monotonic() + wait_s
    while True:
        lines = _look(session, probe)
        state = classify(lines)
        if state == "ready":
            return
        if state == "dialog":
            raise _refused(session, DIALOG, lines)
        if time.monotonic() >= deadline:
            why = "busy: a turn or a background task is running" if state == "busy" else (
                "no empty Claude Code input box with a known mode line (accept edits, auto mode, bypass permissions)")
            raise _refused(session, f"not ready for input within {wait_s} s: {why}" if wait_s else
                           f"not ready for input: {why}", lines)
        time.sleep(POLL_S)


def no_dialog(session, probe):
    """tmux, after the paste and before Enter: NotReady when the session exited, its screen cannot be read or a dialog
    came up meanwhile; the pasted text then stays unsubmitted."""
    lines = _look(session, probe, after="the pasted text was not submitted")
    if classify(lines) == "dialog":
        raise _refused(session, DIALOG, lines, after="the pasted text was not submitted")


def _look(session, probe, after="nothing sent"):
    try:
        alive, lines = probe()
    except CarrierError as e:
        raise NotReady(f"{session}: screen unavailable ({e}); {after}") from None
    if not alive:
        raise _refused(session, "the session has exited", lines or [], after=after)
    if lines is None:
        raise NotReady(f"{session}: screen unavailable; {after}")
    return lines


def _refused(session, why, lines, after="nothing sent"):
    tail = [x.rstrip() for x in lines if x.strip()][-TAIL_LINES:]
    return NotReady(f"{session}: {why}; {after}. Last screen lines:\n" + ("\n".join(f"  | {x}" for x in tail)
                                                                         or "  (none)"))
