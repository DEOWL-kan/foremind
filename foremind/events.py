"""Append-only event log: intent/result, dedupe, hash chain, monthly rotation of closed entries (DESIGN §1.4).

Refer to other events by `id`, never by `hash`: rotate() re-chains the entries it keeps, so their hashes change.
Dedupe on (dedupe_id, phase) only sees the current file; entries already rotated to the archive are not consulted.

An event claiming the user (one of CLAIMS is "user", a USER_TYPES type or USER_VIA: what the user's own commands
write) gets `seat_ancestor` (REQ-11 ④, m2c.6): the session whose settings file is on an ancestor's command line
(seat_ancestor), "unknown" when ps cannot tell; no field otherwise. audit.l0 and supervisor/phases/bounds.py act on it.
"""
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from foremind import pathmatch
from foremind.fsutil import append_line, atomic_write, file_lock, sha256_bytes

_RESERVED = {"id", "ts", "prev", "hash"}
CLAIMS = ("approved_by", "by", "author", "requested_by")
# written only by what the user runs (guard's foremind whitelist leaves it out) with none of CLAIMS: resume / pause,
# confirm-exit, audit --accept-config, decide --confirm, quota --reset, seat --user, init / uninstall
# (install.settings.record), plan new, and `hook` in a session that is not Foremind's (a #22 edit it approves)
USER_TYPES = {"resumed", "paused", "exit_confirmed", "l0_config_accepted", "pending_confirmed", "quota_reset",
              "seat_user", "program_config_write", "planner_opened", "user_config_edit"}
USER_VIA = ("audit --release",)  # batch_state's `via`; the program's own state changes carry none
PS = ("/bin/ps", "/usr/bin/ps")  # the first that exists; not from PATH: a ps put first there could answer for the chain
ANCESTORS = 32


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(event):
    body = {k: v for k, v in event.items() if k != "hash"}
    return sha256_bytes(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _dump(event):
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _open_intents(events):
    closed = {e["dedupe_id"] for e in events if e["phase"] == "result"}
    return [e for e in events if e["phase"] == "intent" and e["dedupe_id"] not in closed]


def seat_ancestor(sessions_dir) -> str | None:
    """The session `<sessions_dir>/<session>.settings.json` names on the command line of this process or one of its
    ancestors (a seat's claude carries it: vendors/claude.launch), walking `ps` up to ANCESTORS levels; "unknown" when
    ps fails or the chain is longer; None when no ancestor names one (another project's sessions do not count, one
    whose path ends in this one's included: the directory starts a word or follows `=`). Case-insensitive where the
    volume is (pathmatch.FOLD)."""
    dirs = "|".join(map(re.escape, {str(sessions_dir), os.path.realpath(sessions_dir)}))
    rx = re.compile(rf"(?<![^\s=])(?:{dirs})/([A-Za-z0-9_-]+)\.settings\.json", re.I if pathmatch.FOLD else 0)
    pid, ps = os.getpid(), next(filter(os.path.exists, PS), PS[0])
    for _ in range(ANCESTORS):
        try:
            r = subprocess.run([ps, "-o", "ppid=,command=", "-p", str(pid)], capture_output=True, text=True,
                               errors="replace", timeout=10)
            ppid, _, cmd = r.stdout.strip().partition(" ")
            pid = int(ppid)
        except (OSError, subprocess.SubprocessError, ValueError):  # no ps, no answer, or the process is gone
            return "unknown"
        if m := rx.search(cmd):
            return m[1]
        if pid <= 1:
            return None
    return "unknown"


class EventLog:
    def __init__(self, path):
        self.path = Path(path)

    def _lock(self):
        return file_lock(self.path.with_suffix(".lock"))

    def _new(self, type, phase, dedupe_id, prev, fields):
        if bad := _RESERVED & fields.keys():
            raise ValueError(f"reserved event fields: {sorted(bad)}")
        e = {"id": uuid.uuid4().hex, "ts": _now(), "type": type, "phase": phase,
             "dedupe_id": dedupe_id, "prev": prev, **fields}
        e["hash"] = _hash(e)
        return e

    def append(self, type: str, *, phase: str = "result", dedupe_id: str | None = None, **fields) -> dict:
        if phase not in ("intent", "result"):
            raise ValueError(f"phase must be 'intent' or 'result', not {phase!r}")
        if phase == "intent" and dedupe_id is None:
            raise ValueError("intent events need a dedupe_id so a result can close them")
        if (type in USER_TYPES or fields.get("via") in USER_VIA or any(fields.get(k) == "user" for k in CLAIMS)) and \
                (s := seat_ancestor(self.path.parent / "sessions")) is not None:  # before the lock: ps is slow
            fields["seat_ancestor"] = s
        with self._lock():
            prev = ""
            # ponytail: full scan per append; add an index if events.jsonl outgrows a month of events
            for e in self.iter():
                if dedupe_id is not None and e["dedupe_id"] == dedupe_id and e["phase"] == phase:
                    return e
                prev = e["hash"]
            e = self._new(type, phase, dedupe_id, prev, fields)
            append_line(self.path, _dump(e))
            return e

    def _lines(self):
        try:
            f = open(self.path, encoding="utf-8")
        except FileNotFoundError:
            return
        with f:
            for n, line in enumerate(f, 1):
                if line.strip():
                    yield n, line

    def iter(self):
        for _, line in self._lines():
            yield json.loads(line)

    def open_intents(self) -> list[dict]:
        return _open_intents(list(self.iter()))

    def verify_chain(self) -> list[str]:
        problems, prev, first = [], "", True
        for n, line in self._lines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"line {n}: not valid JSON")
                prev, first = None, False
                continue
            if first and e.get("type") == "rotated":
                prev = e.get("prev")  # anchor: hash of the last entry moved to the archive
            first = False
            if e.get("prev") != prev:
                problems.append(f"line {n}: prev does not match the previous entry's hash")
            if e.get("hash") != _hash(e):
                problems.append(f"line {n}: hash does not match content")
            prev = e.get("hash")
        return problems

    def rotate(self, archive_dir: Path, before: str) -> int:
        """Move closed entries with ts earlier than `before` (YYYY-MM) to archive_dir/events-YYYY-MM.jsonl."""
        archive_dir = Path(archive_dir)
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", before):
            raise ValueError(f"before must be YYYY-MM, got {before!r}")
        with self._lock():
            if problems := self.verify_chain():
                raise ValueError(f"{self.path}: refusing to rotate a broken chain: {problems[0]}")
            events = list(self.iter())
            still_open = {e["id"] for e in _open_intents(events)}
            moved = [e for e in events if e["ts"][:7] < before and e["id"] not in still_open]
            if not moved:
                return 0
            moved_ids = {e["id"] for e in moved}
            for month in sorted({e["ts"][:7] for e in moved}):
                archive = archive_dir / f"events-{month}.jsonl"
                have = {e["id"] for e in EventLog(archive).iter()}  # a crashed earlier rotate may have copied some
                for e in moved:
                    if e["ts"][:7] == month and e["id"] not in have:
                        append_line(archive, _dump(e))
            rotated = self._new("rotated", "result", None, moved[-1]["hash"], {"before": before, "moved": len(moved)})
            kept, prev = [], rotated["hash"]
            for e in events:
                if e["id"] not in moved_ids:  # re-chain what stays behind the rotation marker
                    e["prev"] = prev
                    e["hash"] = prev = _hash(e)
                    kept.append(e)
            atomic_write(self.path, "".join(_dump(e) + "\n" for e in [rotated, *kept]))
            return len(moved)
