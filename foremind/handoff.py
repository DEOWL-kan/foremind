"""Handoff sections and taking over a batch (DESIGN §6.4).

write_section: validate against schema `handoff_section`, require one state entry per batch repo and the author to
hold the batch lock, then append it to the batch log through batchlog as a fenced JSON block under `## 交接段`;
event `handoff_written` records the section's sha256 and the log length right after it (`log_size`).
latest_section reads back the section named by the latest `handoff_written` (M1-4 r1 SF-3): the last section block
in the log cut at `log_size`, checked against the sha256. Text appended with `foremind log` that only looks like a
section, before or after it, is never taken for the record and cannot hide it (r2 N-a). It is what the successor's
mechanical verification compares to.
accept: only the latest session the program opened as this batch's successor (event `seat_opened`, successor=True,
written after its verification passed) may take the lock, only while the batch is in SUCCESSOR_FROM (r2 N-c), and
only from the holder recorded when it was opened (`predecessor`), from nobody (lock broken) or from itself (MF-3).
The holder changes in one project-lock step, then
「已接手」 is logged. Intent/result events make a crash in between recoverable: re-running accept finishes the job.
"""
import json
import re

from foremind import batchlog, header, lock, schemas
from foremind.events import EventLog
from foremind.fsutil import project_lock, sha256_bytes
from foremind.paths import state_dir

SECTION_MARK = "## 交接段"
ACCEPT_MARK = "## 已接手"
_SECTION = re.compile(r"^## 交接段\n\n```json\n(.*?)\n```$", re.M | re.S)
SUCCESSOR_FROM = ("running", "changes_requested", "stuck")  # I43: the states a successor may take over


class HandoffError(Exception):
    pass


def _events(root):
    return EventLog(state_dir(root) / "events.jsonl")


def history(root):
    """Every event, oldest first: the monthly archives (`EventLog.rotate` moves closed entries there), then the log."""
    for p in sorted((state_dir(root) / "archive").glob("events-*.jsonl")):
        yield from EventLog(p).iter()
    yield from _events(root).iter()


def write_section(root, batch, section, *, author) -> str:
    errs = schemas.validate("handoff_section", section)
    if not errs:
        h, _ = header.parse((state_dir(root) / "batches" / f"{batch}.md").read_text(encoding="utf-8"))
        want, got = set(h.get("repos", [])), set(section["state"]["repos"])
        if want != got:
            errs.append(f"state.repos: need exactly the batch repos {sorted(want)}, got {sorted(got)}")
    if errs:
        raise HandoffError("handoff section rejected: " + "; ".join(errs))
    if (holder := lock.holder(root, batch)) != author:
        raise HandoffError(f"{batch}: only the lock holder ({holder}) writes the handoff, not {author}")
    body = json.dumps(section, ensure_ascii=False, indent=2)  # JSON escapes newlines, so no "\n```" inside
    digest = batchlog.append(root, batch, f"{SECTION_MARK}\n\n```json\n{body}\n```", author=author)
    size = next(e["size"] for e in reversed(list(history(root))) if e["type"] == "batch_log_appended"
                and e.get("batch") == batch and e.get("sha256") == digest)  # the log length this append left
    _events(root).append("handoff_written", batch=batch, session=author, log_sha256=digest, log_size=size,
                         section_sha256=sha256_bytes(body.encode("utf-8")))
    return digest


def latest_section(root, batch) -> dict | None:
    rec = None
    for e in history(root):
        if e["type"] == "handoff_written" and e.get("batch") == batch:
            rec = e
    if rec is None:
        return None
    try:
        data = (state_dir(root) / "batches" / f"{batch}.log.md").read_bytes()
    except FileNotFoundError:
        raise HandoffError(f"{batch}: a handoff was written but the batch log is gone") from None
    if not batchlog.verify(root, batch):
        raise batchlog.LogRewritten(f"{batch}: batch log was changed outside `foremind log`")
    text = data[:rec.get("log_size")].decode("utf-8")  # the log as the section's own append left it
    m = _SECTION.match(text, max(text.rfind(SECTION_MARK), 0))
    if m and sha256_bytes(m[1].encode("utf-8")) == rec.get("section_sha256"):
        return json.loads(m[1])
    raise HandoffError(f"{batch}: the recorded handoff section is not in the batch log")


def accept(root, batch, session) -> str | None:
    """Make `session` the lock holder; returns the previous holder (None if the lock had been broken)."""
    ev = _events(root)
    did = f"handoff_accept:{batch}:{session}"
    with project_lock(root):
        seen = list(history(root))
        opened = [e for e in seen if e["type"] == "seat_opened" and e.get("batch") == batch and e.get("successor")]
        if not any(e.get("session") == session for e in opened):
            raise HandoffError(f"{session} was not opened by the program as successor of {batch}")
        if opened[-1].get("session") != session:
            raise HandoffError(f"{session} is no longer {batch}'s successor: {opened[-1].get('session')} was opened "
                               "after it")
        pred, old = opened[-1].get("predecessor"), lock.holder(root, batch)
        if old not in (pred, None, session):
            raise HandoffError(f"{batch}: lock held by {old}, not by {session}'s predecessor {pred}")
        done = next((e for e in seen if e["dedupe_id"] == did and e["phase"] == "result"), None)
        if done:
            if old != session:
                raise HandoffError(f"{session} accepted {batch} before, but the lock is now held by {old}")
            return done.get("previous")
        h, _ = header.parse((state_dir(root) / "batches" / f"{batch}.md").read_text(encoding="utf-8"))
        if (st := h.get("state", "planned")) not in SUCCESSOR_FROM:
            raise HandoffError(f"{batch} is {st}: a successor takes over only {', '.join(SUCCESSOR_FROM)}")
        ev.append("handoff_accept", phase="intent", dedupe_id=did, batch=batch, session=session, predecessor=pred)
        if old is None:
            lock.acquire(root, batch, session, held=True)
        elif old != session:
            lock.transfer(root, batch, old, session, held=True)
    prev = old if old != session else next((e.get("frm") for e in reversed(seen) if e["type"] == "lock_transferred"
                                            and e.get("batch") == batch and e.get("to") == session), None)
    batchlog.append(root, batch, f"{ACCEPT_MARK}\n\n{session} 接手；前任 {prev or '无（锁已破）'}", author=session)
    ev.append("handoff_accept", dedupe_id=did, batch=batch, session=session, previous=prev)
    return prev
