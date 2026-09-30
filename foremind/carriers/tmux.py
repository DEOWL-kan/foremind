"""tmux carrier (DESIGN §5, M1-0 item 7). Targets use `=name` so tmux never prefix-matches another session.

Every command runs on Foremind's own server, `tmux -S <absolute socket path>` (`[carrier.tmux] socket`, default
~/.local/state/foremind/tmux/foremind.sock), so neither another tmux nor TMUX_TMPDIR changes which server is asked
(M1-4 r1 SF-9, r2 SF-A). seat records the path in `seat_launch`; a session's later commands go to the socket it
was opened on, whatever the config says now.
Presence is read with `list-panes`, which fails when the target is missing. Only tmux's own "can't find session" /
"no server running" (or no socket file at all) count as absent; any other failure raises, it is never exit
evidence (MF-1). Empty output from a successful call is read as absent too.
Text goes in as a bracketed paste (load-buffer + paste-buffer -p) and then Enter, so multi-line messages are not
submitted line by line; tmux cannot confirm delivery, so send() returns None. Idle is a rough read: no window
activity for quiet_s seconds (§10.3). close() watches the pane processes, then the session's own process
(Carrier._settled): a claude under a pane shell may outlive the pane.
"""
import os
import shlex
import time

from foremind.carriers import Carrier, CarrierError, SessionExists, SessionState, run
from foremind.handoff import history
from foremind.lock import ExitEvidence

_PASTE_SETTLE_S = 0.3  # calibration knob: let the TUI take the paste before Enter arrives


def _absent(stderr) -> bool:
    s = stderr.lower()
    return ("can't find session" in s or "no server running" in s
            or ("error connecting to" in s and "no such file or directory" in s))  # no socket: no server either


class TmuxCarrier(Carrier):
    name = "tmux"
    quiet_s = 3

    def __init__(self, root, cfg=None):
        super().__init__(root, cfg)
        # config.load keeps [carrier.tmux] whole under `carrier.tmux` (`carrier.*` is one registered key, SF-B)
        sock = os.path.expanduser((self.cfg.get("carrier.tmux") or {}).get(
            "socket", "~/.local/state/foremind/tmux/foremind.sock"))
        if not os.path.isabs(sock):
            raise CarrierError(f"carrier.tmux.socket must be an absolute path, not {sock!r}")
        self.socket = sock
        self._opened_on = {}

    def _sock(self, session):
        """The socket `session` was opened on (its `seat_launch` intent), else this carrier's own."""
        if session not in self._opened_on:
            rec = None
            for e in history(self.root):
                if (e["type"] == "seat_launch" and e.get("phase") == "intent" and e.get("session") == session
                        and e.get("carrier") == self.name and e.get("socket")):
                    rec = e["socket"]
            if rec is None:
                return self.socket  # not cached: a launch may still be recorded
            self._opened_on[session] = rec
        return self._opened_on[session]

    def _tmux(self, *args, sock=None, input=None, check=True):
        r = run(["tmux", "-S", sock or self.socket, *args], input=input)
        if check and r.returncode:
            raise CarrierError(f"tmux {args[0]}: {r.stderr.strip()}")
        return r

    def _panes(self, session):
        """[(dead, window_activity, pid)] of the session's window; [] when tmux says the session is not there."""
        r = self._tmux("list-panes", "-t", f"={session}:", "-F", "#{pane_dead} #{window_activity} #{pane_pid}",
                       sock=self._sock(session), check=False)
        if r.returncode:
            if _absent(r.stderr):
                return []
            raise CarrierError(f"tmux list-panes -t ={session}: {r.stderr.strip()}")
        panes = [line.split() for line in r.stdout.splitlines() if line.strip()]
        if not all(len(p) == 3 and all(x.isdigit() for x in p) for p in panes):
            raise CarrierError(f"tmux list-panes -t ={session}: unexpected output {r.stdout[:200]!r}")
        return [(d == "1", int(a), int(pid)) for d, a, pid in panes]

    def create(self, session, launch):
        env = [a for k, v in launch.env.items() for a in ("-e", f"{k}={v}")]
        os.makedirs(os.path.dirname(self.socket), mode=0o700, exist_ok=True)  # tmux -S does not create it
        r = self._tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", str(launch.cwd), *env,
                       shlex.join(launch.argv), check=False)
        if r.returncode:
            if "duplicate session" in r.stderr:
                raise SessionExists(f"tmux: session {session} already exists on socket {self.socket}")
            raise CarrierError(f"tmux new-session: {r.stderr.strip()}")

    def send(self, session, text):
        pane, buf, sock = f"={session}:", f"fm-{session}", self._sock(session)
        self._tmux("load-buffer", "-b", buf, "-", sock=sock, input=text)
        self._tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane, sock=sock)
        time.sleep(_PASTE_SETTLE_S)
        self._tmux("send-keys", "-t", pane, "Enter", sock=sock)
        return None

    def read_state(self, session):
        panes = self._panes(session)
        if not panes:
            return SessionState(alive=False, idle=None)
        dead, activity, _ = panes[0]
        screen = self._tmux("capture-pane", "-p", "-t", f"={session}:", sock=self._sock(session), check=False).stdout
        return SessionState(alive=not dead, idle=time.time() - activity >= self.quiet_s,
                            tail=[x for x in screen.splitlines() if x.strip()][-20:])

    def wait_idle(self, session, timeout_s):
        deadline = time.monotonic() + timeout_s
        while True:
            st = self.read_state(session)
            if not st.alive or st.idle:
                return bool(st.alive)
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)

    def close(self, session):
        panes = self._panes(session)
        if not panes:
            return self._settled(session, ExitEvidence(session, self.name, "absent"))
        pids = [pid for _, _, pid in panes]
        r = self._tmux("kill-session", "-t", f"={session}", sock=self._sock(session), check=False)
        if r.returncode and not _absent(r.stderr):
            raise CarrierError(f"tmux kill-session -t ={session}: {r.stderr.strip()}")
        deadline = time.monotonic() + self.exit_timeout_s
        while time.monotonic() < deadline:
            if not any(_running(pid) for pid in pids):
                return self._settled(session, ExitEvidence(session, self.name, "pid_exited"))
            time.sleep(0.2)
        return None  # still running: not confirmed, so no lock break (§10.4)

    def list_sessions(self):
        r = self._tmux("list-sessions", "-F", "#{session_name}", check=False)
        if r.returncode:
            if _absent(r.stderr):
                return []  # no server = no sessions
            raise CarrierError(f"tmux list-sessions: {r.stderr.strip()}")
        return r.stdout.split()


def _running(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True
