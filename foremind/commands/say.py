"""`foremind say <batch> "<text>"` (finding 22): a message into the inbox of the session working on a batch,
delivered by its Stop hook or the supervisor. Recipient: the successor while the holder hands off (heartbeat
handoff_requested) and a successor was opened for it, or while the lock is broken and one was opened; else the lock
holder; neither: an error. Sender `say:<role or user>`. The guard lets only the controller (and the user, outside
sessions) run it (I66)."""
import os
import sys

from foremind import handoff, heartbeat, inbox, lock
from foremind.paths import ProjectNotFound, find_project_root
from foremind.supervisor.tick import _exit_evidence


def register(sub):
    p = sub.add_parser("say", help="send a message to the session working on a batch")
    p.add_argument("batch_id")
    p.add_argument("text")
    p.set_defaults(func=_run)


def recipient(root, batch) -> str:
    holder = lock.holder(root, batch)
    if holder == lock.USER:
        raise ValueError(f"{batch} is held by the user: no session to write to")
    # the successor opened for the current holder (`predecessor` = the holder when it was opened; None: lock broken)
    # while it still can take the lock: the last seat opened for the batch, not accepted, not gone (tick._unaccepted)
    evs = list(handoff.history(root))
    last = next((e for e in reversed(evs) if e["type"] == "seat_opened" and e.get("batch") == batch), None)
    succ = last.get("session") if last and last.get("successor") and last.get("predecessor") == holder else None
    if succ and (_exit_evidence(evs, succ) is not None or any(
            e["type"] == "handoff_accept" and e["phase"] == "result" and e.get("batch") == batch
            and e.get("session") == succ for e in evs)):
        succ = None
    if succ and (holder is None or (heartbeat.read(root, holder) or {}).get("handoff_requested")):
        return succ
    if holder:
        return holder
    raise ValueError(f"{batch}: no session holds it and no successor is open")


def _run(args):
    role = os.environ.get("FOREMIND_ROLE") if os.environ.get("FOREMIND_SESSION") else None
    sender = f"say:{role or 'user'}"
    try:
        root = find_project_root()
        to = recipient(root, args.batch_id)
        inbox.append(to, args.text, sender=sender, root=root)
    except (OSError, ProjectNotFound, ValueError, inbox.InboxCorrupt) as e:
        print(f"foremind say: {e}", file=sys.stderr)
        return 1
    print(f"-> {to}")
    return 0
