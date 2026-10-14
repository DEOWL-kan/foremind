"""Offline project upgrade (m3a REQ-4): the install manifests and the hook lines they record, nothing else.

What changes: every `.foremind/install/settings-*.json` that is v0 or records hook lines other than this CLI's, and the
`.claude/settings.local.json` it records, as install.settings.upgraded() puts them (m3a.6). Config, frozen goals,
plans, batches, receipts, logs, events and heartbeats keep their bytes (events: appended to only).

Interface (m3a.8 wires it to the CLI and the entries; none of these prints):
- preflight(root, now=None) -> Report: reads only (no lock file, state, event, backup or marker is created).
- migrate(root, now=None) -> Report: the user's `foremind upgrade`; UpgradeError for a Foremind session.
- confirm_exit(root, token, now=None) -> list[str]: the user's `--confirm-exit <token>`; UpgradeError when refused.
- admitted(root, fd=None): the entries' shared admission (context manager, yields the lock's fd; None outside a
  Foremind project); Blocked when an upgrade runs, is unfinished or broken, or the project still needs one.
- marker(root) -> dict | None, pending(root) -> list[str], blocked(root) -> str | None, recovery(root) -> list[str].
Locks, always without waiting: `.foremind/admission.lock`, shared for every writer (admitted) and exclusive for
migrate, taken before state.lock or any other lock of the project. preflight only probes it when it exists. A caller
holding admission must not call preflight or migrate (its own shared lock reads as another writer's).

Processes (D23 as supervisor/tick.py): supervise (supervisor.json), seats and planners (`--settings` on their command
line), controllers (pid and lstart they bound), and jobs: those of a `sv_*` intent with a job id and no result in the
whole history, and those under .foremind/jobs/ or .foremind/land/*/ whose spec, pid or result changed at most
RECENT_JOB_S ago. A pid is that process unless it started more than PID_SLACK_S after its record (the job's pid file,
supervisor.json started_at, a controller's lstart); no pid within job._START_S of the spec is starting. Running,
starting and lock holders block for good; what cannot be told (ps fails, a directory that cannot be listed, a broken
or missing record, a time in the future) is unknown and blocks until the user confirms it with the token preflight
prints, which binds the project, each record's bytes and the process identity seen: any change makes it void.
Nothing is signalled, closed or settled.

A migration: exclusive admission and state.lock -> plan and processes again -> marker (`upgrade/unfinished.json`)
-> processes once more -> every file to change backed up (bytes, existence, mode, sha256, and the expected bytes) in
`upgrade/backups/<transaction>/` (0700 dirs, 0600 files, `upgrade/.gitignore` = `*`) -> files written, progress
recorded per file -> verified -> marker removed. A rerun reuses the first backup and expected bytes: a file holding
neither stops it. Success keeps the backup RETAIN_DAYS for the user to delete.
"""
import contextlib
import copy
import fcntl
import json
import os
import re
import shlex
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import foremind
from foremind import job, sessions
from foremind.controller import MARKER as CONTROLLER_MARKER
from foremind.events import EventLog
from foremind.fsutil import LockBusy, atomic_write, project_lock, sha256_bytes, sha256_file
from foremind.install import settings
from foremind.paths import state_dir
from foremind.vendors import claude

RECENT_JOB_S = 86400  # REQ-4: jobs whose spec, pid or result changed at most this long ago (inclusive)
RETAIN_DAYS = 7
PID_SLACK_S = 2  # as supervisor/tick.py's D23
PROTECTED_DIRS = ("plans", "batches", "archive", "heartbeats")  # receipts and logs are in batches/
_TX = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


class UpgradeError(Exception):
    pass


class Blocked(UpgradeError):
    """An entry may not write: an upgrade runs, is unfinished or broken, or the project needs one."""


@dataclass
class Report:
    root: Path
    refusals: list = field(default_factory=list)  # manifests that stop it, each with what to do
    blockers: list = field(default_factory=list)  # running, starting, lock holders: never confirmable
    unknown: list = field(default_factory=list)  # [{"kind", "what", "why", "fp"}] not confirmed yet
    todo: list = field(default_factory=list)  # directories whose manifest (and settings) it rewrites
    notes: list = field(default_factory=list)
    marker: dict | None = None
    status: str = "checked"  # checked | current | migrated | refused | unfinished
    backup: Path | None = None
    expires: str | None = None
    problems: list = field(default_factory=list)  # failed verification, third content

    @property
    def token(self) -> str | None:
        return _token(self.root, self.unknown) if self.unknown else None

    @property
    def ok(self) -> bool:
        return not (self.refusals or self.blockers or self.unknown or self.problems or self.marker) \
            and self.status in ("checked", "current", "migrated")

    def lines(self) -> list[str]:
        out = [f"{self.root}: {self.status}"]
        out += [f"refused: {x}" for x in self.refusals]
        out += [f"running: {x}" for x in self.blockers]
        out += [f"unknown: {u['kind']} {u['what']}: {u['why']}" for u in self.unknown]
        if self.unknown:
            out.append("once you have checked that this work has exited, run: "
                       f"foremind upgrade --confirm-exit {self.token}")
        if self.blockers:
            out.append("stop these yourself (and what starts them again), then rerun; Foremind stops nothing")
        if self.marker and not (self.problems or self.refusals) and self.status != "unfinished":
            out.append(f"an upgrade is unfinished ({self.marker['path']}): run foremind upgrade to finish it")
        out += [f"to do: {d}" for d in self.todo] + self.notes + [f"problem: {x}" for x in self.problems]
        if self.backup:
            out.append(f"backup: {self.backup}")
        if self.status == "migrated":
            out.append(f"the backup is kept until {self.expires} ({RETAIN_DAYS} days); after that delete it "
                       f"yourself: rm -rf {shlex.quote(str(self.backup))}")
        elif self.status == "unfinished":
            out += recovery(self.root)
        return out


# --- paths, records ---------------------------------------------------------------------------------------------

def upgrade_dir(root) -> Path:
    return state_dir(root) / "upgrade"


def lock_path(root) -> Path:
    return state_dir(root) / "admission.lock"


def marker_path(root) -> Path:
    return upgrade_dir(root) / "unfinished.json"


def _confirmed_path(root) -> Path:
    return upgrade_dir(root) / "exit-confirmed.json"


def _tx_dir(root, tx) -> Path:
    return upgrade_dir(root) / "backups" / tx


def _now_iso(now) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")


def _epoch(ts) -> float | None:
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return None


def _write(p, data, private=True) -> None:
    """Every write of a migration (one place for the tests' interruptions): ours 0600, a business file keeps its
    mode."""
    atomic_write(p, data)
    if private:
        os.chmod(p, 0o600)


def _drop(p) -> None:
    Path(p).unlink(missing_ok=True)


def _mkdir(p) -> Path:
    p.mkdir(mode=0o700, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def _private_dirs(root) -> None:
    for p in (upgrade_dir(root), upgrade_dir(root) / "backups"):
        _mkdir(p)
    if not (gi := upgrade_dir(root) / ".gitignore").exists():
        _write(gi, "*\n")


def marker(root) -> dict | None:
    """None when no upgrade is unfinished; else {"path", "transaction", "started_at", "backup", "broken"} where
    broken (why it cannot be read) is None for a sound one. Reads only."""
    p = marker_path(root)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"path": str(p), "transaction": None, "started_at": None, "backup": None, "broken": str(e)}
    try:
        m = json.loads(raw)
        tx = m["transaction"]
        if not (isinstance(tx, str) and _TX.fullmatch(tx)):
            raise ValueError(f"bad transaction {tx!r}")
    except (ValueError, KeyError, TypeError) as e:
        return {"path": str(p), "transaction": None, "started_at": None, "backup": None, "broken": f"unreadable: {e}"}
    return {"path": str(p), "transaction": tx, "started_at": m.get("started_at"),
            "backup": str(_tx_dir(root, tx)), "broken": None}


def _load_tx(root, tx) -> dict | None:
    """The transaction record; None when not written yet; UpgradeError when unreadable."""
    p = _tx_dir(root, tx) / "transaction.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise UpgradeError(f"{p}: {e}") from None


# --- what changes -----------------------------------------------------------------------------------------------

def _dir_of(man) -> Path:
    return Path(man["path"]).parent.parent


def _recorded(man) -> list[str]:
    cmds = [h.get("command") for r in man["hooks"].values() for g in r["groups"] if isinstance(g, dict)
            for h in g.get("hooks") or [] if isinstance(h, dict)]
    return cmds + ([man["statusLine"].get("command")] if isinstance(man["statusLine"], dict) else [])


def _current(man) -> bool:
    """A v1 manifest recording exactly this CLI's hooks and status line command."""
    want = claude.settings()["hooks"]
    return man.get("format_version") == settings.FORMAT_VERSION and set(man["hooks"]) == set(want) \
        and all(man["hooks"][ev]["groups"] == g for ev, g in want.items()) \
        and (man["statusLine"] is None or isinstance(man["statusLine"], dict)
             and man["statusLine"].get("command") == claude.foremind_command("statusline"))


def pending(root) -> list[str]:
    """Why the project needs `foremind upgrade` before any write (REQ-5): a v0 manifest, or recorded hook lines that
    are not this CLI's. Reads the manifests only; unreadable ones are preflight's and doctor's."""
    out = []
    for m, r in settings.scan(root):
        if isinstance(r, settings.ManifestError):
            continue
        v, man = r
        if v == 0:
            out.append(f"{m}: install manifest of format 0 (no format_version)")
        elif not _current(man):
            out.append(f"{m}: hooks recorded there do not run this Foremind ({claude.foremind_command()})")
    return out


def blocked(root) -> str | None:
    """Why an entry may not write now (an unfinished or broken upgrade, or one needed), else None. Reads only."""
    if m := marker(root):
        return (f"{root}: the upgrade marker {m['path']} is broken ({m['broken']}); see foremind doctor"
                if m["broken"] else f"{root}: an upgrade is unfinished ({m['path']}): run foremind upgrade")
    if why := pending(root):
        return f"{root}: needs foremind upgrade: " + "; ".join(why)
    return None


def _plan(root) -> tuple[list[dict], list[str], list[str]]:
    """([{"dir", "manifest", "manifest_text", "settings", "settings_text" or None, "old"}], refusals, notes) for
    every manifest that changes; reads only."""
    todo, refusals, notes = [], [], []
    inst = state_dir(root) / "install"
    if inst.exists() and not os.access(inst, os.R_OK | os.X_OK):
        return [], [f"{inst}: cannot be read: make it readable, then rerun"], []
    for m, r in settings.scan(root):
        if isinstance(r, settings.ManifestError):
            refusals.append(f"{r}: " + {
                "future": "upgrade with the Foremind that wrote it, or install that version",
                "illegal": "put back the manifest as Foremind wrote it, then rerun",
            }.get(r.kind, "put it back as Foremind wrote it; or remove Foremind's entries from the settings file it is "
                          "for yourself and delete it, then run foremind init"))
            continue
        v, man = r
        d = _dir_of(man)
        if Path(man["path"]).name != "settings.local.json" or settings.manifest_path(root, d) != m:
            refusals.append(f"{m}: records {man['path']}, which is not the directory this manifest is named for: "
                            "find out which directory it belongs to, put it back or delete it, then rerun")
            continue
        try:
            mtext, stext, n = settings.upgraded(root, d)
            old, _ = settings.load(root, d)
        except (settings.SettingsError, OSError) as e:
            refusals.append(str(e).replace("then rerun foremind init", "then rerun foremind upgrade, and foremind "
                                           "init after it if you deleted the manifest")
                            if isinstance(e, settings.SettingsError) else
                            f"{settings.path(d)}: cannot be read ({e}): make it readable, then rerun")
            continue
        if v == settings.FORMAT_VERSION and json.loads(mtext) == man:
            continue  # up to date (a rewrite would only move our groups behind the user's)
        notes += n
        todo.append({"dir": str(d), "manifest": str(m), "manifest_text": mtext, "old": old,
                     "settings": str(settings.path(d).resolve()), "settings_text": stext})
    if any(t["settings_text"] is not None for t in todo) and (why := _events_ok(root)):
        refusals.append(why)
    return todo, refusals, notes


def _rest(root, d, man) -> str:
    """sha256 of `d`'s settings.local.json without what `man` records (the user's entries)."""
    _, data = settings._read(settings.path(d))
    data = copy.deepcopy(data)
    if bad := settings._remove(data, man):
        raise settings.SettingsError("; ".join(bad))
    return sha256_bytes(json.dumps(data, sort_keys=True, ensure_ascii=False).encode())


def _protected(root) -> dict:
    """{path relative to root: sha256} of config and history; events.jsonl as {"size", "sha256"} of its bytes."""
    sd, out = state_dir(root), {}
    files = [Path(root) / "foremind.toml", sd / "config.toml", sd / "delivery.toml"]
    for sub in PROTECTED_DIRS:
        files += sorted((sd / sub).rglob("*"))
    for p in files:
        if p.is_file():
            out[str(p.relative_to(root))] = sha256_file(p)
    ev = sd / "events.jsonl"
    out["events.jsonl"] = {"size": ev.stat().st_size if ev.exists() else 0,
                           "sha256": sha256_file(ev) if ev.exists() else None}
    return out


def _events_kept(root, was) -> bool:
    ev = state_dir(root) / "events.jsonl"
    if not was["size"]:
        return True
    try:
        with open(ev, "rb") as f:
            head = f.read(was["size"])
    except OSError:
        return False
    return len(head) == was["size"] and sha256_bytes(head) == was["sha256"]


# --- processes --------------------------------------------------------------------------------------------------

def _proc(pid) -> tuple[bool, float | None]:
    """(whether pid lives, when it started: `ps -o lstart=` epoch, None when ps cannot tell)."""
    from foremind.supervisor import tick  # tick imports most of the package: not on the entries' path
    if not job._alive(pid):
        return False, None
    return True, tick._started(pid)


def _ps():
    return sessions.snapshot()


def _fp(root, kind, what, files=(), extra=None, pid=None, started=None) -> str:
    """The evidence: project, object, each record's bytes and mtime, the process identity seen."""
    rec = []
    for p in files:
        try:
            rec.append([str(p), sha256_file(p), Path(p).stat().st_mtime_ns])
        except OSError as e:
            rec.append([str(p), type(e).__name__])
    return sha256_bytes(json.dumps([str(Path(root).resolve()), kind, str(what), rec, extra, pid, started],
                                   sort_keys=True).encode())[:32]


def _unknown(root, kind, what, why, **fp) -> dict:
    return {"kind": kind, "what": str(what), "why": why, "fp": _fp(root, kind, what, **fp)}


def _job(root, d, now, intent=None) -> tuple[str, object] | None:
    """("running" | "starting", text) or ("unknown", item) for job dir `d`; None once it ended or never started."""
    names = ("spec.json", "pid", "result.json")
    files = [d / n for n in names]
    st = {}
    for n in names:
        try:
            st[n] = (d / n).stat()
        except FileNotFoundError:
            pass
        except OSError as e:
            return "unknown", _unknown(root, "job", d, f"{n}: {e}", files=files)
    if "result.json" in st:
        try:
            if isinstance(json.loads((d / "result.json").read_text(encoding="utf-8")), dict):
                return None
        except (OSError, ValueError):
            pass
        return "unknown", _unknown(root, "job", d, "result.json is not a job result", files=files)
    if future := [n for n, s in st.items() if s.st_mtime > now + PID_SLACK_S]:
        return "unknown", _unknown(root, "job", d, f"{', '.join(future)} modified in the future", files=files)
    if "pid" in st:
        try:
            pid = int((d / "pid").read_text().strip())
            if pid <= 0:
                raise ValueError(pid)
        except (OSError, ValueError):
            return "unknown", _unknown(root, "job", d, "its pid file is unreadable", files=files)
        alive, started = _proc(pid)
        if not alive:
            return None  # ended without a result (lost)
        if started is None:
            return "unknown", _unknown(root, "job", d, f"ps cannot tell when pid {pid} started", files=files, pid=pid)
        if started > st["pid"].st_mtime + PID_SLACK_S:
            return None  # D23: the pid belongs to a later process now
        return "running", f"job {d} (pid {pid})"
    if "spec.json" in st:
        return ("starting", f"job {d}: no pid yet") if now - st["spec.json"].st_mtime < job._START_S else None
    at = _epoch((intent or {}).get("ts"))
    if at is None or at > now + PID_SLACK_S:
        return "unknown", _unknown(root, "job", d, "no job record, and its intent has no readable time", files=files)
    if now - at < job._START_S:
        return "starting", f"job {d}: started {int(now - at)} s ago, no record yet"
    return "unknown", _unknown(root, "job", d, "its intent has no result and the job left no record", files=files,
                               extra=intent.get("id"))


def _jobs(root, now) -> tuple[dict[Path, dict | None], list[dict]]:
    """({job dir: its open intent or None}, unknown items): REQ-4's set."""
    sd, out, unknown = state_dir(root), {}, []
    try:
        from foremind import handoff  # its imports reach most of the package
        evs = list(handoff.history(root))
        if not all(isinstance(e, dict) for e in evs):
            raise ValueError("a line is not a JSON object")
    except (OSError, ValueError, KeyError) as e:
        files = [sd / "events.jsonl", *sorted((sd / "archive").glob("events-*.jsonl"))]
        unknown.append(_unknown(root, "events", sd / "events.jsonl", f"unreadable ({e}): the jobs of open intents "
                                "cannot be told", files=files))
        evs = []
    closed = {e.get("dedupe_id") for e in evs if e.get("phase") == "result"}
    links = {e.get("dedupe_id"): e.get("job") for e in evs if e.get("type") == "sv_job"}
    for e in evs:
        if e.get("phase") == "intent" and str(e.get("type", "")).startswith("sv_") \
                and e.get("dedupe_id") not in closed:
            jid = e.get("job") or links.get(f"sv_job:{e.get('dedupe_id')}")
            if isinstance(jid, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", jid) and jid not in (".", ".."):
                out[sd / "jobs" / jid] = e
    for d in [*_dirs(root, sd / "jobs", unknown), *(j for b in _dirs(root, sd / "land", unknown)
                                                    for j in _dirs(root, b, unknown))]:
        if d in out:
            continue
        times = []
        for n in ("spec.json", "pid", "result.json"):
            try:
                times.append((d / n).stat().st_mtime)
            except FileNotFoundError:
                pass
            except OSError:
                times = None  # _job tells it unknown
                break
        if times is None or times and now - max(times) <= RECENT_JOB_S:
            out[d] = None
    return out, unknown


def _ls(root, d, kind, unknown) -> list[Path]:
    """The entries of `d` ([] when it does not exist); one that cannot be listed is a `kind` unknown, never read as
    nothing there (Path.glob would say so)."""
    try:
        return [d / n for n in sorted(os.listdir(d))]
    except FileNotFoundError:
        return []
    except OSError as e:
        try:
            mtime = d.stat().st_mtime_ns
        except OSError:
            mtime = None
        unknown.append(_unknown(root, kind, d, f"cannot be listed ({e}): what it records cannot be told",
                                extra=[str(e), mtime]))
        return []


def _dirs(root, d, unknown) -> list[Path]:
    """The directories in `d`; an entry that cannot be told is kept (never read as no job)."""
    out = []
    for p in _ls(root, d, "jobs", unknown):
        try:
            if stat.S_ISDIR(p.stat().st_mode):
                out.append(p)
        except FileNotFoundError:
            pass
        except OSError:
            out.append(p)
    return out


def _supervisor(root, now) -> tuple[str, object] | None:
    p = state_dir(root) / "supervisor.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        pid = int(rec["pid"])
        if pid <= 0:
            raise ValueError(pid)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return "unknown", _unknown(root, "supervisor", p, f"unreadable ({e})", files=[p])
    alive, started = _proc(pid)
    if not alive:
        return None
    at = _epoch(rec.get("started_at"))
    if at is None or at > now + PID_SLACK_S:
        return "unknown", _unknown(root, "supervisor", p, f"pid {pid} lives and started_at is missing or in the "
                                   "future", files=[p], pid=pid, started=started)
    if started is None:
        return "unknown", _unknown(root, "supervisor", p, f"ps cannot tell when pid {pid} started", files=[p], pid=pid)
    if started > at + PID_SLACK_S:
        return None  # D23
    return "running", f"foremind supervise (pid {pid})"


def _sessions(root) -> tuple[list[str], list[dict]]:
    sd, unknown = state_dir(root), []
    ctl = [p for p in _ls(root, sd / "controller", "controller", unknown)
           if p.name.endswith(".json") and not p.name.startswith(".")]
    try:
        procs = _ps()
    except sessions.SessionsError as e:
        return [], unknown + [_unknown(root, "sessions", sd / "sessions", f"ps failed ({e}): seats, planners and "
                                       "controllers cannot be told",
                                       files=[*sorted((sd / "sessions").glob("*.settings.json")), *ctl])]
    live = sessions.live(root, procs)
    running = [f"session {s} (pid {', '.join(str(p.pid) for p in ps_)})" for s, ps_ in sorted(live.items())]
    seen = {p.pid for ps_ in live.values() for p in ps_}
    for p in ctl:  # each record by itself: sessions.live keeps one per pid and skips those without a name
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(st, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as e:
            unknown.append(_unknown(root, "controller", p, f"unreadable ({e})", files=[p]))
            continue
        pid = st.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            unknown.append(_unknown(root, "controller", p, "no process recorded for it", files=[p]))
            continue
        if pid not in procs:
            continue
        lstart, now_ = st.get("lstart"), procs[pid].lstart
        rec = sessions.lstart_epoch(lstart) if isinstance(lstart, str) else None
        started = sessions.lstart_epoch(now_)
        if lstart != now_ and rec is not None and started is not None and started > rec + PID_SLACK_S:
            continue  # D23: the pid belongs to a later process now
        if lstart != now_ and (rec is None or started is None or started < rec):
            unknown.append(_unknown(root, "controller", p, f"pid {pid} lives (started {now_}) and its recorded start "
                                    f"{lstart!r} cannot tell whether it is that controller", files=[p], pid=pid,
                                    started=now_))
        elif pid not in seen:
            running.append(f"controller {st.get('controller') or 'of ' + p.name} (pid {pid})")
    return running, unknown


def _lock_held(root) -> str | None:
    """Whether a writer holds the admission lock: probed without creating the file."""
    try:
        fd = os.open(lock_path(root), os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return f"a Foremind process holds {lock_path(root)}"
        return None
    finally:
        os.close(fd)


def _confirmed(root) -> set[str]:
    try:
        return set(json.loads(_confirmed_path(root).read_text(encoding="utf-8"))["confirmed"])
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _processes(root, now, probe_lock=True) -> tuple[list[str], list[dict]]:
    """(blockers, unknown items not confirmed) of the project now."""
    blockers, unknown = [], []
    if probe_lock and (held := _lock_held(root)):
        blockers.append(held)
    jobs, unknown = _jobs(root, now)
    for r in [_supervisor(root, now), *(_job(root, d, now, it) for d, it in sorted(jobs.items()))]:
        if r and r[0] == "unknown":
            unknown.append(r[1])
        elif r:
            blockers.append(r[1])
    running, u = _sessions(root)
    blockers += running
    ok = _confirmed(root)
    return blockers, [x for x in unknown + u if x["fp"] not in ok]


def _token(root, unknown) -> str:
    return sha256_bytes(json.dumps([str(Path(root).resolve()), sorted(u["fp"] for u in unknown)]).encode())[:16]


def _identity() -> None:
    """REQ-4: only the user migrates or confirms; never a seat, a one-shot role, the controller or a job."""
    for k in ("FOREMIND_SESSION", "FOREMIND_ROLE", CONTROLLER_MARKER, "FOREMIND_JOB"):
        if os.environ.get(k):
            raise UpgradeError(f"only the user runs this, not a Foremind session or job ({k} is set)")


# --- the interface ----------------------------------------------------------------------------------------------

def preflight(root, now=None) -> Report:
    """What `foremind upgrade` would do and what stops it; reads only."""
    root, now = Path(root).resolve(), time.time() if now is None else now
    rep = Report(root, marker=marker(root))
    todo, rep.refusals, rep.notes = _plan(root)
    rep.todo = [t["dir"] for t in todo]
    rep.blockers, rep.unknown = _processes(root, now)
    rep.status = "current" if not todo and not rep.marker else "checked"
    return rep


def confirm_exit(root, token, now=None) -> list[str]:
    """The user says the work behind the unknown evidence `token` names has exited (REQ-4). Valid only for that
    evidence as it is now; it never settles running, starting or lock-holding work, and writes no event."""
    _identity()
    root, now = Path(root).resolve(), time.time() if now is None else now
    blockers, unknown = _processes(root, now)
    if not unknown:
        raise UpgradeError(f"{root}: no unknown exit evidence to confirm")
    if token != _token(root, unknown):
        raise UpgradeError(f"{root}: {token!r} does not match the evidence now (it changed, or the token is of "
                           "another project): run foremind upgrade --check and use the token it prints")
    _private_dirs(root)
    _write(_confirmed_path(root), json.dumps({"confirmed": sorted(_confirmed(root) | {u["fp"] for u in unknown})}))
    out = [f"confirmed exited: {u['kind']} {u['what']}" for u in unknown]
    return out + [f"still running (not confirmable): {b}" for b in blockers]


@contextlib.contextmanager
def admitted(root, fd=None):
    """REQ-5: an entry's shared admission, held until the block ends; without waiting. `fd`: one inherited (a
    re-exec, a job's runner) instead of opening the lock anew; the caller keeps closing it. Blocked when an upgrade
    holds the lock, is unfinished or broken, or the project needs one."""
    root = Path(root)
    if fd is None and not state_dir(root).is_dir():
        yield None  # no Foremind project here: nothing to protect
        return
    own = fd is None
    if own:
        fd = os.open(lock_path(root), os.O_RDWR | os.O_CREAT, 0o644)
    else:
        a, b = os.fstat(fd), lock_path(root).stat() if lock_path(root).exists() else None
        if b is None or (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise Blocked(f"fd {fd} is not {lock_path(root)}")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked(f"{root}: foremind upgrade or its check holds {lock_path(root)}; retry once it is done") \
                from None
        if why := blocked(root):
            raise Blocked(why)
        yield fd
    finally:
        if own:
            os.close(fd)


@contextlib.contextmanager
def _exclusive(root):
    fd = os.open(lock_path(root), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy(str(lock_path(root))) from None
        yield
    finally:
        os.close(fd)


def _by() -> dict:
    py, pkg, pp = settings.expected()
    return {"version": foremind.__version__, "python": py, "package": pkg, "pythonpath": pp}


def migrate(root, now=None) -> Report:
    """`foremind upgrade` for this project (REQ-4). Report.status: current (nothing to do), migrated, refused
    (nothing changed, or an unfinished one stopped again with its marker kept) or unfinished (verification failed or
    a file holds a third content: marker kept, Report.lines() carries the recovery steps)."""
    _identity()
    root, now = Path(root).resolve(), time.time() if now is None else now
    if not marker(root):
        rep = preflight(root, now)
        if not rep.ok or rep.status == "current":
            rep.status = "refused" if not rep.ok else "current"
            return rep
    rep = Report(root)
    try:
        with _exclusive(root), project_lock(root, blocking=False):
            return _migrate(root, now, rep)
    except LockBusy as e:
        rep.status, rep.marker = "refused", marker(root)
        rep.blockers.append(f"{e} is held: a Foremind process or another upgrade runs")
        return rep


def _migrate(root, now, rep) -> Report:
    mk = rep.marker = marker(root)
    if mk and mk["broken"]:
        rep.status = "refused"
        rep.refusals.append(f"the upgrade marker {mk['path']} is broken ({mk['broken']}): Foremind will not guess "
                            "which upgrade it was; restore it from a backup's transaction, or recover by hand")
        return rep
    tx = _load_tx(root, mk["transaction"]) if mk else None
    if tx and tx.get("by") != _by():
        rep.status = "refused"
        rep.refusals.append(f"{mk['transaction']} was begun by Foremind {tx.get('by')}: finish it with that one, "
                            "or recover by hand (foremind doctor shows how)")
        return rep
    if not (tx and tx.get("backup_complete")):  # nothing written yet: plan (again) from the files as they are
        todo, rep.refusals, rep.notes = _plan(root)
        rep.todo = [t["dir"] for t in todo]
        if rep.refusals or not todo:
            rep.status = "refused" if rep.refusals else "current"
            if mk and not rep.refusals:  # an earlier attempt that found nothing to write
                _archive(root, mk)
                rep.marker = None
            return rep
    rep.blockers, rep.unknown = _processes(root, now, probe_lock=False)
    if rep.blockers or rep.unknown:
        rep.status = "refused"
        return rep
    if not mk:
        txid = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        _private_dirs(root)
        _write(marker_path(root), json.dumps({"transaction": txid, "started_at": _now_iso(now)}))
        mk = rep.marker = marker(root)
        rep.blockers, rep.unknown = _processes(root, now, probe_lock=False)  # an old program, a job's runner
        if rep.blockers or rep.unknown:
            rep.status = "refused"
            return rep
    rep.backup = _tx_dir(root, mk["transaction"])
    if not (tx and tx.get("backup_complete")):
        tx = _backup(root, mk, todo, now)
    if third := _third(tx):
        rep.status, rep.problems = "unfinished", third
        return rep
    if any(f["kind"] == "settings" and _sha(f["path"]) != f["expected_sha256"] for f in tx["files"]) \
            and (why := _events_ok(root)):
        rep.status, rep.problems = "unfinished", [why]
        return rep
    _apply(root, tx, now)
    if problems := _verify(root, tx):
        tx.setdefault("verify_failures", []).append({"at": _now_iso(now), "problems": problems})
        _write(rep.backup / "transaction.json", json.dumps(tx, indent=1))
        rep.status, rep.problems = "unfinished", problems
        return rep
    tx["completed_at"] = _now_iso(now)
    tx["expires_at"] = rep.expires = _now_iso(now + RETAIN_DAYS * 86400)
    _write(rep.backup / "transaction.json", json.dumps(tx, indent=1))
    _drop(marker_path(root))
    rep.marker, rep.status, rep.todo = None, "migrated", sorted({f["dir"] for f in tx["files"]})
    return rep


def _archive(root, mk) -> None:
    _private_dirs(root)
    d = _mkdir(_tx_dir(root, mk["transaction"]))
    _write(d / "unfinished.json", marker_path(root).read_bytes())
    _drop(marker_path(root))


def _backup(root, mk, todo, now) -> dict:
    """Every file to change, whole, before any is written; the record says so last (backup_complete)."""
    d = _mkdir(_tx_dir(root, mk["transaction"]))
    files = []
    for t in todo:
        for kind, path, text in (("manifest", t["manifest"], t["manifest_text"]),
                                 ("settings", t["settings"], t["settings_text"])):
            if text is None:
                continue
            n, p = len(files), Path(path)
            raw = p.read_bytes() if p.exists() else None
            if raw is not None:
                _write(d / f"{n}.orig", raw)
            _write(d / f"{n}.new", text)
            files.append({"path": path, "kind": kind, "dir": t["dir"], "existed": raw is not None,
                          "mode": p.stat().st_mode & 0o7777 if raw is not None else None,
                          "original_sha256": sha256_bytes(raw) if raw is not None else None,
                          "original": f"{n}.orig" if raw is not None else None,
                          "expected_sha256": sha256_bytes(text.encode()), "expected": f"{n}.new", "written_at": None})
    olds = [o for t in todo if (o := t["old"])]
    tx = {"transaction": mk["transaction"], "project": str(root), "started_at": mk["started_at"], "by": _by(),
          "files": files, "rest": {t["dir"]: _rest(root, t["dir"], t["old"]) for t in todo if t["old"]},
          "old_targets": sorted({str(x) for o in olds for c in _recorded(o) if (x := settings.target(c))}),
          "protected": _protected(root), "backup_complete": False}
    _write(d / "transaction.json", json.dumps(tx, indent=1))
    tx["backup_complete"] = True
    _write(d / "transaction.json", json.dumps(tx, indent=1))
    return tx


def _sha(path) -> str | None:
    try:
        return sha256_file(path)
    except FileNotFoundError:
        return None


def _third(tx) -> list[str]:
    return [f"{f['path']} holds neither its content before the upgrade nor the upgrade's (sha256 {s}): not "
            "overwritten" for f in tx["files"] if (s := _sha(f["path"])) not in (f["original_sha256"],
                                                                                 f["expected_sha256"])]


def _events_ok(root) -> str | None:
    """Why settings.record() could not append to events.jsonl (append opens events.lock read-write, reads every line,
    then appends), else None."""
    p = state_dir(root) / "events.jsonl"
    why = None
    for f in (p, p.with_suffix(".lock")):
        if not os.access(x := f if f.exists() else f.parent, os.W_OK):
            why = f"{x} is not writable"
            break
    if why is None:
        try:
            for e in EventLog(p).iter():
                e["dedupe_id"], e["phase"], e["hash"]  # what append() reads of each line
        except (OSError, ValueError, KeyError, TypeError) as e:
            why = f"{p} cannot be read ({type(e).__name__}: {e})"
    return why and f"{why}, and each write of settings.local.json is recorded in {p} first: fix that, then rerun"


def _apply(root, tx, now) -> None:
    d = _tx_dir(root, tx["transaction"])
    for f in tx["files"]:
        if _sha(f["path"]) != f["expected_sha256"]:
            data = (d / f["expected"]).read_bytes()
            if sha256_bytes(data) != f["expected_sha256"]:
                raise UpgradeError(f"{d / f['expected']}: the backup's expected content changed")
            if f["kind"] == "settings":
                settings.record(root, f["path"], data.decode("utf-8"))  # L0: the program's write (#22)
            _write(f["path"], data, private=False)
        if not f["written_at"]:
            f["written_at"] = _now_iso(now)
            _write(d / "transaction.json", json.dumps(tx, indent=1))


def _verify(root, tx) -> list[str]:
    """REQ-4's success, item by item: current manifests, Foremind's entries in place, the recorded interpreter and
    package import this version (a fixed argv), the user's entries, config and history as before."""
    probs, by = [], tx["by"]
    for d in sorted({f["dir"] for f in tx["files"]}):
        try:
            v, man = settings.parse(settings.manifest_path(root, d))
            if v != settings.FORMAT_VERSION or not _current(man):
                probs.append(f"{settings.manifest_path(root, d)}: not this Foremind's current manifest")
                continue
            probs += settings.conflicts(root, d)
            probs += [x for x in settings.check(d) if man["statusLine"] is not None or ": no statusLine" not in x]
            if d in tx["rest"] and _rest(root, d, man) != tx["rest"][d]:
                probs.append(f"{settings.path(d)}: entries other than Foremind's differ from before the upgrade")
        except (settings.SettingsError, OSError) as e:
            probs.append(str(e))
            continue
        r = settings._probe(settings.target(claude.foremind_command("statusline")), d)
        if r.get("absent") or "error" in r or not r.get("new_enough") or "import" in r or r.get("missing"):
            probs.append(f"{d}: the recorded {by['python']} does not run Foremind: {r}")
        elif os.path.realpath(r.get("package", "")) != os.path.realpath(by["package"]) \
                or r.get("version") != by["version"]:
            probs.append(f"{d}: the recorded hooks run Foremind {r.get('version')} at {r.get('package')}, not "
                         f"{by['version']} at {by['package']}")
    now_ = _protected(root)
    for k, sha in tx["protected"].items():
        if k == "events.jsonl":
            if not _events_kept(root, sha):
                probs.append(f"{state_dir(root) / 'events.jsonl'}: its earlier entries changed")
        elif now_.get(k) != sha:
            probs.append(f"{root / k}: changed during the upgrade")
    probs += [f"{root / k}: appeared during the upgrade" for k in now_.keys() - tx["protected"].keys()]
    return probs


def recovery(root) -> list[str]:
    """The manual steps for the unfinished upgrade of `root` (REQ-4), with its paths; [] without one."""
    mk = marker(root)
    if not mk:
        return []
    if mk["broken"]:
        return [f"the marker {mk['path']} cannot be read ({mk['broken']}); look for the transaction it belongs to "
                f"under {upgrade_dir(root) / 'backups'} and follow the steps below with its transaction.json"]
    d = _tx_dir(root, mk["transaction"])
    try:
        tx = _load_tx(root, mk["transaction"])
    except UpgradeError as e:
        return [f"{e}: the backup record is unreadable; restore by hand from the *.orig files in {d}"]
    out = ["to undo the upgrade by hand:",
           "1. stop every Foremind process of this project (foremind supervise, seats, planners, the controller, "
           "jobs) and what starts them again; foremind upgrade --check lists those it sees"]
    q = lambda x: shlex.quote(str(x))  # noqa: E731 (paths with spaces)
    if not (tx and tx.get("backup_complete")):
        return out + ["2. no file was changed yet: rerun foremind upgrade, or drop this attempt: "
                      f"mkdir -p {q(d)} && mv {q(mk['path'])} {q(d / 'unfinished.json')}"]
    for i, f in enumerate(tx["files"]):
        save = f"cp -p {q(f['path'])} {q(d / f'conflict-{i}')}"
        if f["existed"]:
            out.append(f"2.{i} {f['path']}: if `shasum -a 256` shows neither {f['original_sha256']} nor "
                       f"{f['expected_sha256']}, first save it ({save}); then cp {q(d / f['original'])} "
                       f"{q(f['path'])} && chmod {f['mode']:o} {q(f['path'])}")
        else:
            out.append(f"2.{i} {f['path']}: did not exist before; rm {q(f['path'])} only if `shasum -a 256` shows "
                       f"{f['expected_sha256']} (else save it first: {save}, and leave it)")
    out += ["3. check each restored file: shasum -a 256 <path> (or sha256sum) shows its original sha256 above",
            f"4. once all are restored: mv {q(mk['path'])} {q(d / 'unfinished.json')}",
            "5. if you go back to the Foremind you ran before, reinstall it offline with pip install --no-index "
            "--force-reinstall <its wheel> (the hooks used " + (", ".join(tx.get("old_targets") or []) or "?")
            + "), then run foremind doctor; do not start it before step 4: it does not know the marker"]
    return out
