"""Phase: sessions (m2d.1, REQ-2 [防误操作]): Foremind seats still running although their batch is finished or someone
took over from them. Every supervisor.merged_check_min (per process, as bounds._REMOTE_AT) one ps snapshot, the
running sessions known by their record (foremind/sessions.py: the settings path on the command line, never a process
name). A seat is leaked when all of these hold:

  - this project launched it: a seat_launch intent names the session, its batch and carrier
  - its one process is running, and its identity checks out (sessions.doubt: started after the launch record)
  - its batch is merged, cataloged or cancelled (ready.FINISHED; t.state, else the last batch_state of a batch no plan
    lists): why `finished`; or it was taken over (`taken_over`): after its launch another session of the batch ran
    `handoff --accept`, or its lock was broken on exit evidence (lock_broken) and the batch has a new holder

`session_leak` {session, batch, why, pid} once; then closed as REQ-1 says, through its carrier (t.close, why "leaked",
retried per t.close_due): the carrier reports the terminal closed, Carrier._settled SIGTERMs the process if it runs on
and gives the evidence only once it is gone. `session_closed` {session, batch, pid, confirmed} once, after the first
try; later tries are the sv_close records. A close recorded as confirmed before m2d.1 does not stop any of this:
those records are how the m2b.4 seats leaked (m2d.1.D1). No model call, so it runs under a full block too.
Sessions named `fm-<slug>-` on a carrier this project launched on that no launch record (seat_launch,
controller_launch, planner_opened) names, nor a settings file (sessions.launched_at: a planner has one before its
planner_opened): `session_unknown` {session} once, nothing else.

Never closed, nor sent anything:
  - a session or process no seat_launch of this project names: the user's own claude, another project's seats,
    planners (planner_opened), controllers (`foremind controller check` closes a predecessor), reviewers and other
    one-shot jobs, the `session_unknown` ones
  - a seat of a batch the user claimed (seat --user, a seat_user record)
  - a session that holds a batch lock, or is a batch's pending successor
  - a session being opened (a seat_open or seat_continue intent naming it without its result)
  - a seat of a batch not finished that nobody took over from: working, idle or stuck (tick's release paths own those)
  - a seat of a batch neither a plan nor the event log knows the state of
  - a session whose identity does not check out: more than one innermost process, started before its launch record,
    this process or one of its ancestors
  - under the manual carrier, anything beyond t.close's request to the user (`foremind confirm-exit`)
  - anything when ps fails: a tick_error, never "nothing runs"
"""
from foremind import carriers, lock, seat, sessions
from foremind.supervisor import ready
from foremind.supervisor.tick import setting

_AT = {}  # root: last look. ponytail: this process only, as bounds._REMOTE_AT; a new one looks at once
_OPENING = ("seat_open", "seat_continue")  # as Tick.opening
_LAUNCHES = ("seat_launch", "controller_launch", "planner_opened")


def run(t, blocked):
    every = setting(t.cfg, "supervisor.merged_check_min") * 60
    if t.now - _AT.get(t.root, 0) < every:
        return
    _AT[t.root] = t.now
    unknown(t)
    if not (cands := leaked_if_running(t)):
        return
    procs = sessions.snapshot()
    running = sessions.live(t.root, procs)
    for s, (bid, why) in cands.items():
        if (left := running.get(s)) and not sessions.doubt(t.root, s, left, procs):
            with t.guard("sessions", s):
                close(t, bid, s, why, left[0].pid)


def leaked_if_running(t) -> dict:
    """{session: (batch, why)} of the seats this project launched that would be leaked if still running."""
    launched, accepts, broken, claimed, last = {}, [], set(), set(), {}
    for i, e in enumerate(t.evs):
        ty = e["type"]
        if ty == "seat_launch" and e["phase"] == "intent" and e.get("session") and e.get("batch"):
            launched[e["session"]] = (e["batch"], i)
        elif ty == "handoff_accept" and e["phase"] == "result":
            accepts.append((i, e.get("batch"), e.get("session")))
        elif ty == "lock_broken":
            broken.add(e.get("session"))
        elif ty == "seat_user":
            claimed.add(e.get("batch"))
        elif ty == "batch_state":
            last[e.get("batch")] = e.get("to", e.get("state"))
    busy = {*t.holders.values(), *(t.pending_successor(b) for b in t.headers)}
    out = {}
    for s, (bid, i) in launched.items():
        if s in busy or bid in claimed or any(t.open_intents(k, session=s) for k in _OPENING):
            continue
        if (t.state(bid) if bid in t.headers else last.get(bid)) in ready.FINISHED:
            out[s] = (bid, "finished")
        elif (any(j > i and b == bid and a != s for j, b, a in accepts)
              or s in broken and t.holders.get(bid) not in (None, s, lock.USER)):
            out[s] = (bid, "taken_over")
    return out


def unknown(t):
    """session_unknown: carrier sessions named fm-<slug>- that no launch record names, nor a settings file:
    vendors.launch writes one before the carrier starts a planner, whose planner_opened comes later."""
    known = {e.get("session") for e in t.evs if e["type"] in _LAUNCHES}
    kinds = {e.get("carrier") for e in t.evs if e["type"] in (*_LAUNCHES, "controller_opened") and e.get("carrier")}
    prefix = f"fm-{seat.project_slug(t.root, t.cfg)}-"
    for kind in sorted(kinds - {"manual"}):
        with t.guard("sessions", kind):
            for s in carriers.get(kind, t.root, t.cfg).list_sessions() or ():
                if s.startswith(prefix) and s not in known and sessions.launched_at(t.root, s) is None:
                    t.emit("session_unknown", dedupe_id=f"session_unknown:{s}", session=s)


def close(t, bid, s, why, pid):
    t.emit("session_leak", dedupe_id=f"session_leak:{s}", session=s, batch=bid, why=why, pid=pid)
    if not t.close_due(s):
        return
    ev = t.close(bid, s, "leaked")
    t.emit("session_closed", dedupe_id=f"session_closed:{s}", session=s, batch=bid, pid=pid, confirmed=ev is not None)
    t.did.append(f"{bid}: leaked seat {s} ({why}) " + ("closed" if ev is not None else "close not confirmed"))
