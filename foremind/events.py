"""Append-only event log: intent/result, dedupe, hash chain, monthly rotation of closed entries (DESIGN §1.4).

Refer to other events by `id`, never by `hash`: rotate() re-chains the entries it keeps, so their hashes change.
Dedupe on (dedupe_id, phase) only sees the current file; entries already rotated to the archive are not consulted.
"""
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from foremind.fsutil import append_line, atomic_write, file_lock, sha256_bytes

_RESERVED = {"id", "ts", "prev", "hash"}


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
