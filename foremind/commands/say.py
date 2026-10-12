"""`foremind say <batch> "<text>"` (finding 22): a message into the inbox of the session working on a batch,
delivered by its Stop hook or the supervisor. Recipient: the successor while the holder hands off (heartbeat
handoff_requested) and a successor was opened for it, or while the lock is broken and one was opened; else the lock
holder; neither: an error. Sender `say:<role or user>`. The guard lets only the controller (and the user, outside
sessions) run it (I66).
`--direct` (m2f REQ-5): straight into the lock holder's terminal through its carrier, no inbox. Refused (exit 1) with
no session holding the batch, a holder handing off or one with a successor opening or opened; WAIT (exit 3) while a
handoff the holder wrote may be on its way to review. Steps 1-3 are checked again under the project state lock, which
lock.transfer and seat opening take too, and a `controller_delivery` intent is written in the same hold; the send
runs after it (up to ~15 s, never under the lock). A holder that changed meanwhile: result holder_changed, exit 4."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

from foremind import carriers, config, fsutil, handoff, heartbeat, inbox, lock, seat
from foremind.commands import review as review_cmd
from foremind.events import EventLog
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.supervisor.tick import _exit_evidence

CONTROLLER_DELIVERY = "controller_delivery"  # hooks: a seat's prompt matching its intent is seat_prompt_controller
RECEIPT_WAIT = timedelta(seconds=120)


def register(sub):
    p = sub.add_parser("say", help="send a message to the session working on a batch")
    p.add_argument("batch_id")
    p.add_argument("text")
    p.add_argument("--direct", action="store_true", help="type it into the lock holder's terminal (REQ-5)")
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


class Refused(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _successor(evs, batch, s):
    """A live successor of `s`: a seat_open intent with predecessor `s` and no result yet (opening), or a seat_opened
    successor of `s` neither gone (exit evidence) nor accepted. A failed open does not count."""
    results = {e["dedupe_id"]: e for e in evs if e["type"] == "seat_open" and e["phase"] == "result"}
    for e in evs:
        if e.get("batch") != batch or e.get("predecessor") != s:
            continue
        n = e.get("session")
        if e["type"] == "seat_open" and e["phase"] == "intent" and e["dedupe_id"] not in results:
            return n
        failed = results.get(f"seat_open:{n}", {}).get("ok") is False
        if e["type"] == "seat_opened" and e.get("successor") and not failed and _exit_evidence(evs, n) is None \
                and not any(a["type"] == "handoff_accept" and a["phase"] == "result" and a.get("batch") == batch
                            and a.get("session") == n for a in evs):
            return n
    return None


def _holder(root, batch) -> str:
    """REQ-5 steps 1-3: the session to type into, else Refused."""
    s = lock.holder(root, batch)
    if s is None:
        raise Refused(1, f"foremind say: {batch}: no session holds it")
    if s == lock.USER:
        raise Refused(1, f"foremind say: {batch} is held by the user: no session to type into")
    if (heartbeat.read(root, s) or {}).get("handoff_requested"):
        raise Refused(1, f"REFUSED：{s} 正在交接；改用不带 --direct 的 foremind say（经收件箱转给继任）")
    if succ := _successor(list(handoff.history(root)), batch, s):
        raise Refused(1, f"REFUSED：继任 {succ} 已在开")
    return s


def direct(root, batch, text) -> tuple[int, str]:
    """REQ-5: (exit code, line to print). 0 and 4 went out; 1 refused or failed; 3 is WAIT."""
    s = _holder(root, batch)
    evs = list(handoff.history(root))
    if any(e["type"] == "handoff_written" and e.get("session") == s for e in evs):
        r = next((e for e in reversed(evs) if e["type"] == "review_receipt" and e.get("batch") == batch), None)
        if r and datetime.now(timezone.utc) - datetime.fromisoformat(r["ts"]) < RECEIPT_WAIT:
            raise Refused(3, f"WAIT：{s} 写过交接段，{r['ts']} 刚落下回执；2 分钟后再试")
    cfg = config.load(root)
    kind = (seat._launched_with(root, s) or {}).get("carrier") or seat.setting(cfg, "carrier.kind")
    carrier = carriers.get(kind, root, cfg)
    text, did = carriers.normalize(text), uuid.uuid4().hex
    log = EventLog(state_dir(root) / "events.jsonl")
    with fsutil.project_lock(root):
        if (now := _holder(root, batch)) != s:
            raise Refused(1, f"REFUSED：锁已转给 {now}；请重新发")
        log.append(CONTROLLER_DELIVERY, phase="intent", dedupe_id=did, session=s, batch=batch, carrier=carrier.name,
                   text_sha256=fsutil.sha256_bytes(text.encode("utf-8")), by=review_cmd.actor())
    try:
        if carrier.name == "manual":
            raise carriers.CarrierError("the manual carrier cannot type into a terminal")
        confirmed = carrier.send(s, text)
    except Exception as e:
        log.append(CONTROLLER_DELIVERY, dedupe_id=did, session=s, batch=batch, ok=False, error=str(e))
        raise Refused(1, f"foremind say: {e}") from None
    moved = (now := lock.holder(root, batch)) != s
    log.append(CONTROLLER_DELIVERY, dedupe_id=did, session=s, batch=batch, ok=True, confirmed=confirmed,
               **({"holder_changed": True} if moved else {}))
    if moved:
        return 4, f"WARN：发送期间锁已转给 {now or '无人（已释放）'}；这条可能送给了前任，请对新持锁者再发"
    return 0, f"-> {s}（{'已确认送达' if confirmed else '已发送，未确认'}）"


def _run(args):
    if args.direct:
        try:
            code, line = direct(find_project_root(), args.batch_id, args.text)
        except Refused as e:
            code, line = e.code, str(e)
        except (OSError, ProjectNotFound, ValueError, config.ConfigError, carriers.CarrierError) as e:
            code, line = 1, f"foremind say: {e}"
        print(line, file=sys.stdout if code == 0 else sys.stderr)
        return code

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
