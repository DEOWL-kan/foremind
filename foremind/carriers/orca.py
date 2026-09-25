"""Orca carrier (DESIGN §5, M1-0 item 12): `orca terminal create|send|read|wait|close|list --json`.

Claude Code rewrites the terminal title, so a session is never looked up by title (M1-4 r1 MF-2): create() keeps
the handle Orca returns in `.foremind/carriers/orca/<session>.json`, every later action goes by that handle, and
presence is the handle showing up in `list`. A restarted supervisor maps listed handles back to session names
through these records. Without a record the program never created the session: close() cannot confirm anything
and returns None. A record is kept after close, so the name is never created twice.
send() is confirmed when Orca reports `turn_started`; `wait --for tui-idle` only works for a recognised agent TUI
(a plain shell times out); closing an already exited terminal answers `terminal_handle_stale`; close reports
ptyKilled:false even when the process exits, so exit is confirmed by the handle leaving `list`. close() always asks
Orca to close first: a handle missing from a `list` alone is never exit evidence (M1-4 r2 MF-A).
"""
import json
import time

from foremind.carriers import Carrier, CarrierError, SessionExists, SessionState, run
from foremind.fsutil import atomic_write
from foremind.lock import ExitEvidence
from foremind.paths import state_dir


def _dig(obj, key):
    """First value of `key` anywhere in obj: M1-0 recorded the fields, not every level of nesting."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        obj = list(obj.values())
    if isinstance(obj, list):
        for v in obj:
            if (found := _dig(v, key)) is not None:
                return found
    return None


class OrcaCarrier(Carrier):
    name = "orca"
    exit_timeout_s = 15

    def _orca(self, *args, timeout=60):
        r = run(["orca", "terminal", *args, "--json"], timeout=timeout)
        try:
            return json.loads(r.stdout)
        except ValueError:
            raise CarrierError(f"orca terminal {args[0]}: {(r.stderr or r.stdout).strip()}") from None

    def _ok(self, j, what):
        if not j.get("ok"):
            raise CarrierError(f"orca terminal {what}: {j.get('error')}")
        return {} if j.get("result") is None else j["result"]

    def _listed(self) -> set:
        """Listed handles: only a bare list or {"terminals": [...]} (seen live: result.terminals), each entry with a
        string handle. Anything else raises, so an unreadable list never reads as "gone" (MF-A)."""
        res = self._ok(self._orca("list"), "list")
        terms = res if isinstance(res, list) else res.get("terminals") if isinstance(res, dict) else None
        if not isinstance(terms, list) or not all(isinstance(t, dict) and isinstance(t.get("handle"), str)
                                                  for t in terms):
            raise CarrierError(f"orca terminal list: unexpected result {str(res)[:200]}")
        return {t["handle"] for t in terms}

    def _dir(self):
        return state_dir(self.root) / "carriers" / "orca"

    def _handle(self, session):
        try:
            return json.loads((self._dir() / f"{session}.json").read_text(encoding="utf-8"))["handle"]
        except FileNotFoundError:
            return None

    def create(self, session, launch):
        if old := self._handle(session):
            raise SessionExists(f"orca: session {session} was created before (handle {old})")
        res = self._ok(self._orca("create", "--worktree", f"path:{launch.cwd}", "--title", session,
                                  "--command", launch.shell()), "create")
        h = _dig(res, "handle")
        if not isinstance(h, str) or not h:
            raise CarrierError(f"orca terminal create: no terminal handle in {res}")
        atomic_write(self._dir() / f"{session}.json", json.dumps({"session": session, "handle": h}) + "\n")

    def send(self, session, text):
        h = self._handle(session)
        if not h:
            raise CarrierError(f"orca: no terminal recorded for {session!r}")
        res = self._ok(self._orca("send", "--terminal", h, "--text", text, "--enter", "--wait-submit", "10"), "send")
        return True if "turn_started" in (_dig(res, "stages") or ()) else None

    def read_state(self, session):
        h = self._handle(session)
        if not h:
            return SessionState(alive=None, idle=None)  # not ours: cannot tell
        if h not in self._listed():
            return SessionState(alive=False, idle=None)
        term = _dig(self._ok(self._orca("read", "--terminal", h), "read"), "terminal") or {}
        alive = term.get("status") == "running"
        return SessionState(alive=alive, idle=self.wait_idle(session, 0.5) if alive else None,
                            tail=[x for x in term.get("tail", []) if x.strip()][-20:])

    def wait_idle(self, session, timeout_s):
        h = self._handle(session)
        if not h or h not in self._listed():
            return False
        j = self._orca("wait", "--terminal", h, "--for", "tui-idle", "--timeout-ms", str(int(timeout_s * 1000)),
                       timeout=timeout_s + 30)
        return bool(j.get("ok") and _dig(j.get("result"), "satisfied"))

    def close(self, session):
        h = self._handle(session)
        if not h:
            return None  # never created by the program: nothing it can confirm
        j = self._orca("close", "--terminal", h, "--tab")
        if not j.get("ok"):
            if _dig(j.get("error"), "code") == "terminal_handle_stale":
                return ExitEvidence(session, self.name, "absent")
            raise CarrierError(f"orca terminal close: {j.get('error')}")
        deadline = time.monotonic() + self.exit_timeout_s
        while h in self._listed():
            if time.monotonic() >= deadline:
                return None  # still listed: not confirmed
            time.sleep(0.5)
        return ExitEvidence(session, self.name, "absent")

    def list_sessions(self):
        live = self._listed()
        recs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self._dir().glob("*.json"))]
        return [r["session"] for r in recs if r.get("handle") in live]
