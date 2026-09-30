"""Foremind sessions as processes (m2d.1, REQ-1, REQ-2, REQ-4; DESIGN §5): which process is which session, what it
uses, and making a closed one exit.

Identity is a record, never a process name (m2d.1.D2). A seat's or planner's claude carries `--settings
<project>/.foremind/sessions/<session>.settings.json` (vendors/claude.launch); when an outer shell carries the same
argument (orca types `cd … && env … claude …` into a shell, tmux may run its command through one), the innermost
process (no descendant carrying it) is the session. A controller has no --settings: controller.bind records its
claude's pid and lstart, and a live process with both is that controller. Either way the process must not have
started before the session's launch record (launched_at).

One `ps -Ao` snapshot per look, from /bin/ps or /usr/bin/ps (events.PS, not PATH's), LC_ALL=C for a fixed lstart;
`ps=` takes a callable returning that text instead (tests). ps failing raises SessionsError: never read as "no
process".
settle() is REQ-1 once a carrier reported the terminal closed (Carrier._settled): SIGTERM to the session's one
process if it is still there, never to a process group, never SIGKILL, then wait. When its identity cannot be checked
it sends nothing and the carrier's evidence stands, as before m2d.1, with session_close_unverified {session, why}
once (m2d.1.D6).
"""
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime

from foremind.events import ANCESTORS, PS, EventLog
from foremind.paths import state_dir

EXIT_TIMEOUT_S = 15  # as the carriers' exit_timeout_s


class SessionsError(Exception):
    pass


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    cpu: float  # %CPU as ps reports it
    rss_kb: int
    lstart: str
    command: str


def _ps() -> str:
    ps = next(filter(os.path.exists, PS), PS[0])
    try:
        r = subprocess.run([ps, "-Ao", "pid=,ppid=,pcpu=,rss=,lstart=,command="], capture_output=True, text=True,
                           errors="replace", timeout=30, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as e:
        raise SessionsError(f"{ps}: {e}") from None
    if r.returncode:
        raise SessionsError(f"{ps}: exit {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


def snapshot(ps=None) -> dict[int, Proc]:
    """{pid: Proc} of every process now; SessionsError when ps fails or lists nothing."""
    out = {}
    for line in (ps or _ps)().splitlines():
        f = line.split(None, 9)  # pid ppid pcpu rss, lstart in five words, command
        try:
            p = Proc(int(f[0]), int(f[1]), float(f[2]), int(f[3]), " ".join(f[4:9]), f[9] if len(f) > 9 else "")
        except (IndexError, ValueError):
            continue  # an unreadable line is nobody's session
        out[p.pid] = p
    if not out:
        raise SessionsError("ps listed no process")
    return out


def ancestors(procs, pid) -> list[int]:
    """pid's ancestors in the snapshot, nearest first, at most ANCESTORS."""
    out = []
    while len(out) < ANCESTORS and (p := procs.get(pid)) and 0 < p.ppid != pid:
        pid = p.ppid
        out.append(pid)
    return out


def _settings_rx(root):
    d = state_dir(root) / "sessions"
    dirs = "|".join(map(re.escape, sorted({str(d), os.path.realpath(d)})))
    return re.compile(rf"(?:^|\s)--settings (?:{dirs})/([A-Za-z0-9_-]+)\.settings\.json(?=\s|$)")


def _bound(root) -> dict[int, tuple[str, str]]:
    """{pid: (controller, lstart)} as controller.bind recorded them."""
    from foremind import controller  # controller imports this module
    return {st["pid"]: (st["controller"], st.get("lstart")) for st in controller._states(root)
            if isinstance(st.get("pid"), int)}


def live(root, procs) -> dict[str, list[Proc]]:
    """{session: its innermost processes} of this project's sessions running in the snapshot."""
    rx, found = _settings_rx(root), {}
    for p in procs.values():
        if m := rx.search(p.command):
            found.setdefault(m[1], []).append(p)
    above = {a for ps_ in found.values() for p in ps_ for a in ancestors(procs, p.pid)}
    out = {s: [p for p in ps_ if p.pid not in above] for s, ps_ in found.items()}
    for pid, (name, lstart) in _bound(root).items():
        if (p := procs.get(pid)) and p.lstart == lstart and p not in out.get(name, []):
            out.setdefault(name, []).append(p)
    return out


def usage(procs, pid) -> tuple[float, int]:
    """(%CPU, RSS in KiB) of pid and its descendants."""
    kids = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p.pid)
    cpu, rss, todo, seen = 0.0, 0, [pid], set()
    while todo:
        q = todo.pop()
        if q in seen or q not in procs:
            continue
        seen.add(q)
        cpu, rss = cpu + procs[q].cpu, rss + procs[q].rss_kb
        todo += kids.get(q, [])
    return round(cpu, 1), rss


def lstart_epoch(lstart) -> float | None:
    """Epoch seconds of a ps lstart (local time, LC_ALL=C); None when unreadable."""
    try:
        return time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
    except (ValueError, OverflowError):
        return None


def launched_at(root, session) -> float | None:
    """Epoch seconds of the session's launch record: its seat_launch intent or controller_launch, both written before
    the carrier starts it. A planner's planner_opened comes after its start, so the settings file vendors.launch
    writes before carrier.create stands in for it. None: no record."""
    from foremind import handoff  # handoff's imports reach carriers, which import this module
    for e in handoff.history(root):
        if e.get("session") == session and (e["type"] == "controller_launch"
                                            or e["type"] == "seat_launch" and e["phase"] == "intent"):
            try:
                return datetime.fromisoformat(e["ts"]).timestamp()
            except (KeyError, TypeError, ValueError):
                return None
    try:
        return (state_dir(root) / "sessions" / f"{session}.settings.json").stat().st_mtime
    except OSError:
        return None


def doubt(root, session, left, procs) -> str | None:
    """Why `left` (the session's innermost processes) cannot be taken for the session; None when it can."""
    if len(left) > 1:
        return f"{len(left)} processes carry it: {', '.join(str(p.pid) for p in left)}"
    p = left[0]
    if p.pid in (os.getpid(), *ancestors(procs, os.getpid())):
        return f"pid {p.pid} is this process or its ancestor"
    if (since := launched_at(root, session)) is None:
        return "no launch record"
    if (started := lstart_epoch(p.lstart)) is None or started < int(since):
        return f"pid {p.pid} started {p.lstart}, before its launch record"
    return None


def settle(root, session, *, ps=None, kill=os.kill, timeout_s=EXIT_TIMEOUT_S) -> bool:
    """REQ-1, after the carrier reported the terminal closed: whether its exit evidence stands. No process left: True.
    Still there: SIGTERM to that one pid, then up to timeout_s for it to go: True once gone, False while it runs (no
    evidence: the lock stays, the close is retried). Its identity unsure (ps fails, doubt, not ours to signal):
    nothing sent, True, and session_close_unverified {session, why} once."""
    try:
        procs = snapshot(ps)
        if not (left := live(root, procs).get(session)):
            return True
        if not (why := doubt(root, session, left, procs)):
            p = left[0]
            try:
                kill(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                return True  # gone between the snapshot and now
            except PermissionError:
                why = f"pid {p.pid}: not ours to signal"
            else:
                deadline = time.monotonic() + timeout_s
                while (q := snapshot(ps).get(p.pid)) and q.lstart == p.lstart:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(0.2)
                return True
    except SessionsError as e:
        why = f"ps: {e}"
    EventLog(state_dir(root) / "events.jsonl").append(
        "session_close_unverified", dedupe_id=f"session_close_unverified:{session}", session=session, why=why)
    return True
