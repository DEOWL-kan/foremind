"""Batch, pending (Q-n) and session state machines (DESIGN §1.6).

Contract: `can_transition(machine, frm, to, **ctx)` / `transition(...)` / `reconcile(...)`.
This module only knows which edges exist. Entry conditions (gate passed, receipt present, session
alive, ...) are checked by the caller before it asks for the transition.
Context keys used by BATCH side branches:
  reason         entering or leaving `blocked`: one of BLOCK_REASONS
  prior          leaving `blocked` (non-violation) or `paused`: main state before the branch
  session_alive  leaving `paused` when prior == "running": True -> running, False -> ready
The caller must persist `prior` and `reason` in the batch header (`state_prior`, `blocked_reason`,
DESIGN §20 I1) when entering a side branch, and pass them back in when leaving it.
"""
from typing import Callable, NamedTuple


class IllegalTransition(ValueError):
    pass


class Machine(NamedTuple):
    name: str
    states: frozenset
    terminal: frozenset
    allowed: Callable[[str, str, dict], bool]
    path: tuple = ()  # main path in order; `reconcile` may only jump forward along it
    facts: frozenset = frozenset()  # states verifiable independently of the record (`reconcile` targets)


def can_transition(machine: Machine, frm: str, to: str, **ctx) -> bool:
    if frm == to or frm not in machine.states or to not in machine.states or frm in machine.terminal:
        return False
    return machine.allowed(frm, to, ctx)


def transition(machine: Machine, frm: str, to: str, **ctx) -> str:
    if not can_transition(machine, frm, to, **ctx):
        raise IllegalTransition(f"{machine.name}: {frm} -> {to} not allowed" + (f" (ctx {ctx})" if ctx else ""))
    return to


def reconcile(machine: Machine, current: str, fact: str) -> tuple[str, tuple[str, ...]]:
    """Catch the record up to a fact verifiable outside it (§20 I2); returns `(fact, skipped)`.

    Only states in `machine.facts` (batch: `merged`, by hosting-provider status or the merge-tree criterion) may
    be entered this way, and only by a forward jump along the main path; approved, delivered etc. are decided
    by program gates and never reconciled in. Anything else raises IllegalTransition.
    While on a side branch (updating, paused, ...) pass the header's `state_prior` as `current`.
    `skipped` is the tuple of main-path states jumped over; the caller writes a `reconciled` event carrying it.
    If `skipped` contains `approved` or `delivered` (merged without review or gate), or contains `awaiting_audit`
    while the batch's tier enables the pre-delivery audit, the caller treats it as an L0 hard failure: trigger
    an audit, notify the user, and do not run the cataloger on that batch until the audit is over.
    """
    p = machine.path
    if fact not in machine.facts or current not in p or fact not in p or p.index(fact) <= p.index(current):
        raise IllegalTransition(f"{machine.name}: {current} -> {fact} cannot be reconciled (not a verifiable "
                                "fact, or not a forward jump on the main path)")
    return fact, tuple(p[p.index(current) + 1:p.index(fact)])


# --- batch -----------------------------------------------------------------

BATCH_MAIN = ("planned", "ready", "running", "review_ready", "in_review", "changes_requested",
              "approved", "awaiting_audit", "delivered", "merged", "cataloged", "cancelled")
BATCH_SIDE = ("updating", "blocked", "stuck", "paused", "failed")
BLOCK_REASONS = ("pending", "quota", "dependency", "violation")

_BATCH_EDGES = {
    "planned": ("ready",),
    "ready": ("running",),
    "running": ("review_ready",),
    "review_ready": ("in_review",),
    "in_review": ("changes_requested", "approved", "review_ready"),  # review_ready: head changed, new round
    "changes_requested": ("running",),
    "approved": ("awaiting_audit", "delivered", "changes_requested"),
    "awaiting_audit": ("delivered", "changes_requested"),
    "delivered": ("merged", "changes_requested"),
    "merged": ("cataloged",),
}
_ACTIVE = ("ready", "running", "review_ready", "in_review", "changes_requested",
           "approved", "awaiting_audit", "delivered")
# main states from which each side branch may be entered
_SIDE_FROM = {
    "updating": ("approved", "delivered"),
    "blocked": _ACTIVE,
    "stuck": ("running",),
    "paused": _ACTIVE,
    "failed": ("running", "review_ready", "in_review", "changes_requested"),
}


def _batch(frm: str, to: str, ctx: dict) -> bool:
    if to == "cancelled":  # plan revision drops it, or abandoned via a pending answer
        return frm != "merged"
    prior = ctx.get("prior")
    if frm == "updating":  # re-verified -> approved; conflict or failed re-verify -> seat
        return to in ("approved", "changes_requested")
    if frm == "stuck":
        return to == "running"
    if frm == "failed":
        return to == "ready"
    if frm == "blocked":
        if ctx.get("reason") == "violation":
            return to == "running"
        return ctx.get("reason") in BLOCK_REASONS and prior in _ACTIVE and to == prior
    if frm == "paused":
        if prior not in _ACTIVE:
            return False
        if prior == "running":
            alive = ctx.get("session_alive")
            return (alive is True and to == "running") or (alive is False and to == "ready")
        return to == prior
    if to in _SIDE_FROM:
        return frm in _SIDE_FROM[to] and (to != "blocked" or ctx.get("reason") in BLOCK_REASONS)
    return to in _BATCH_EDGES.get(frm, ())  # "I do it myself" mode goes ready -> running like the rest (§20 I3)


_BATCH_PATH = ("planned", "ready", "running", "review_ready", "in_review", "changes_requested",
               "approved", "awaiting_audit", "delivered", "merged", "cataloged")
BATCH = Machine("batch", frozenset(BATCH_MAIN + BATCH_SIDE), frozenset({"cataloged", "cancelled"}), _batch, _BATCH_PATH,
                frozenset({"merged"}))


# --- pending Q-n -----------------------------------------------------------

PENDING_STATES = ("open", "deciding", "escalated", "awaiting_local_confirm", "answered", "applied",
                  "provisional", "overdue", "confirmed", "overturned", "reverted", "void")
# §20 I6; "when the linked batch is cancelled" is checked by the caller, not here
_VOIDABLE = ("open", "deciding", "escalated", "awaiting_local_confirm", "answered", "provisional", "overdue")
_PENDING_EDGES = {
    "open": ("deciding", "answered", "awaiting_local_confirm"),
    "deciding": ("answered", "provisional", "escalated"),
    "escalated": ("answered", "awaiting_local_confirm"),
    "awaiting_local_confirm": ("answered",),
    "answered": ("applied",),
    "provisional": ("confirmed", "overturned", "overdue"),
    "overdue": ("confirmed", "overturned"),
    "overturned": ("reverted",),
}

PENDING = Machine(
    "pending", frozenset(PENDING_STATES), frozenset({"applied", "confirmed", "reverted", "void"}),
    lambda frm, to, ctx: to in _PENDING_EDGES.get(frm, ()) or (to == "void" and frm in _VOIDABLE),
)


# --- session ---------------------------------------------------------------

SESSION_STATES = ("starting", "active", "busy_tool", "handoff_requested", "handed_off", "lost", "closed")
_SESSION_EDGES = {
    "starting": ("active",),
    "active": ("busy_tool", "handoff_requested"),
    "busy_tool": ("active", "handoff_requested"),
    # §20 I23: a tool call can start while handoff is requested; the caller keeps the handoff-requested flag in
    # the heartbeat file so it survives the busy_tool -> active round trip (I31)
    "handoff_requested": ("handed_off", "busy_tool"),
    "lost": ("active",),  # §20 I23: the session came back
}

SESSION = Machine(
    "session", frozenset(SESSION_STATES), frozenset({"closed"}),
    lambda frm, to, ctx: to in _SESSION_EDGES.get(frm, ()) or to in ("lost", "closed"),
)
