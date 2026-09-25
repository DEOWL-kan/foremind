"""Batch write lock (DESIGN §10.4): `batches/<id>.lock` holds only the holder's session name.

Only the program rewrites it, always inside the project state lock, by atomic replace; no lease (stuck is judged
by heartbeats, §10.3). Pass held=True when the caller already holds project_lock: flock is not re-entrant across
descriptors, so taking it twice in one process deadlocks.
Breaking a lock needs ExitEvidence that the holder has exited: from Carrier.close, or the user's confirmation
under the manual carrier (§5).
"""
import contextlib
import re
from dataclasses import dataclass

from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock
from foremind.paths import state_dir
from foremind.schemas import BATCH_ID

USER = "user"  # holder of an "I do it myself" batch (§20 I3)
EXIT_PROOFS = ("pid_exited", "absent", "user_confirmed")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


class LockError(Exception):
    pass


@dataclass(frozen=True)
class ExitEvidence:
    session: str
    carrier: str
    how: str  # one of EXIT_PROOFS


def state_lock(root, held=False):
    """The project state lock, or nothing if the caller already holds it."""
    return contextlib.nullcontext() if held else project_lock(root)


def _path(root, batch):
    if not isinstance(batch, str) or not re.fullmatch(BATCH_ID, batch):
        raise ValueError(f"bad batch id {batch!r}")
    return state_dir(root) / "batches" / f"{batch}.lock"


def _event(root, type, **fields):
    EventLog(state_dir(root) / "events.jsonl").append(type, **fields)


def holder(root, batch) -> str | None:
    try:
        return _path(root, batch).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def held_by(root, session) -> list[str]:
    """Batches whose lock `session` holds (after a continuation this differs from its FOREMIND_BATCH)."""
    d = state_dir(root) / "batches"
    return sorted(b for b in (p.name[:-5] for p in d.glob("*.lock")) if re.fullmatch(BATCH_ID, b)
                  and holder(root, b) == session)


def acquire(root, batch, session, *, held=False) -> None:
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise ValueError(f"bad session name {session!r}")
    with state_lock(root, held):
        cur = holder(root, batch)
        if cur == session:
            return
        if cur is not None:
            raise LockError(f"{batch}: lock held by {cur}")
        atomic_write(_path(root, batch), session + "\n")
        _event(root, "lock_acquired", batch=batch, session=session)


def transfer(root, batch, frm, to, *, held=False) -> None:
    """Atomically hand the lock from `frm` to `to` (handoff --accept, continuation; §10.4)."""
    if not isinstance(to, str) or not _SESSION.fullmatch(to):
        raise ValueError(f"bad session name {to!r}")
    with state_lock(root, held):
        cur = holder(root, batch)
        if cur == to:
            return
        if cur != frm:
            raise LockError(f"{batch}: lock held by {cur}, not {frm}")
        atomic_write(_path(root, batch), to + "\n")
        _event(root, "lock_transferred", batch=batch, frm=frm, to=to)


def release(root, batch, session, *, held=False) -> None:
    with state_lock(root, held):
        cur = holder(root, batch)
        if cur is None:
            return
        if cur != session:
            raise LockError(f"{batch}: lock held by {cur}, not {session}")
        _path(root, batch).unlink()
        _event(root, "lock_released", batch=batch, session=session)


def break_lock(root, batch, evidence: ExitEvidence, *, held=False) -> None:
    """Supervisor only: clear a lock whose holder is confirmed gone. No or mismatched evidence -> LockError."""
    if not isinstance(evidence, ExitEvidence) or evidence.how not in EXIT_PROOFS:
        raise LockError(f"{batch}: breaking a lock needs ExitEvidence that the old session exited")
    if evidence.carrier == "manual" and evidence.how != "user_confirmed":
        raise LockError(f"{batch}: under the manual carrier only the user can confirm the old session exited")
    with state_lock(root, held):
        cur = holder(root, batch)
        if cur is None:
            return
        if cur != evidence.session:
            raise LockError(f"{batch}: evidence is for {evidence.session}, lock held by {cur}")
        _path(root, batch).unlink()
        _event(root, "lock_broken", batch=batch, session=cur, carrier=evidence.carrier, how=evidence.how)
