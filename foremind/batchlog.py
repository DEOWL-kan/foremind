"""Batch log: append-only through `foremind log`; each append records the file's (size, sha256) (DESIGN §1.3, §9.2).

The archive directory is fixed to `state_dir/archive`: callers of `EventLog.rotate` must pass exactly that, or
verify() cannot see rotated records.
Lock order: project_lock first, then the events log's own lock (events.lock); never the reverse.
"""
from datetime import datetime, timezone

from foremind.events import EventLog
from foremind.fsutil import append_line, project_lock, sha256_bytes
from foremind.paths import state_dir


class LogRewritten(Exception):
    pass


def _path(root, batch_id):
    if not batch_id or "/" in batch_id or batch_id.startswith("."):
        raise ValueError(f"bad batch id {batch_id!r}")
    return state_dir(root) / "batches" / f"{batch_id}.log.md"


def _events(root):
    return EventLog(state_dir(root) / "events.jsonl")


def _latest(root, batch_id):
    """The batch's record with the largest size: rotation may have moved older ones to archive/events-*.jsonl."""
    # ponytail: every append rescans all archives (O(total events)); fine for a few thousand entries, then keep a
    # per-batch (size, sha256) index that rotate() carries forward.
    logs = [_events(root), *(EventLog(p) for p in sorted((state_dir(root) / "archive").glob("events-*.jsonl")))]
    recs = [e for log in logs for e in log.iter() if e["type"] == "batch_log_appended" and e.get("batch") == batch_id]
    return max(recs, key=lambda e: e["size"], default=None)


def verify(root, batch_id) -> bool:
    path = _path(root, batch_id)
    last = _latest(root, batch_id)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return last is None
    if last is None:
        return not data
    return len(data) >= last["size"] and sha256_bytes(data[:last["size"]]) == last["sha256"]


def append(root, batch_id, text: str, *, author: str) -> str:
    if not author or "\n" in author:
        raise ValueError(f"bad author {author!r}")
    path = _path(root, batch_id)
    with project_lock(root):
        if not verify(root, batch_id):  # appending now would bless the rewrite
            raise LogRewritten(f"{path} was changed outside `foremind log`")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        append_line(path, f"### {ts} {author}\n\n{text.rstrip()}\n\n")
        data = path.read_bytes()
        digest = sha256_bytes(data)
        _events(root).append("batch_log_appended", batch=batch_id, size=len(data), sha256=digest, author=author)
    return digest
