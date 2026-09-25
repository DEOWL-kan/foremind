"""Ready queue and full block (DESIGN §1.6, §10.2, §20 I43): pure functions over batch headers and decisions.

A planned batch may become ready when every depends_on is at the `[delivery].depends_on` mode (approved: approved or
later; merged: merged or cataloged), no unanswered decisions/Q-n.json lists it in `blocks`, and its owns_paths are
disjoint from every started (or `busy`: being opened) batch it has no depends_on path to or from (I43).
Reasons are (kind, ref) pairs: ("after", batch), ("decision", Q-n), ("overlaps", batch).
"""
from foremind import seat
from foremind.state import BATCH_SIDE

DONE = ("merged", "cataloged")
FINISHED = (*DONE, "cancelled")
UPSTREAM_OK = {"approved": ("approved", "awaiting_audit", "delivered", *DONE), "merged": DONE}
OPEN_Q = ("open", "deciding", "escalated", "awaiting_local_confirm")  # not answered yet: still in the way


def blocking(bid, decisions) -> list[str]:
    return sorted(q.get("id", "?") for q in decisions
                  if q.get("state", "open") in OPEN_Q and bid in (q.get("blocks") or []))


def started(h) -> bool:
    """Holds its owns_paths (same rule as seat's claim): a started state, or a side branch entered from one."""
    st, prior = h.get("state", "planned"), h.get("state_prior")
    if st in BATCH_SIDE:
        return prior in seat.STARTED if prior else True
    return st in seat.STARTED


def upstreams(headers, bid) -> set:
    seen, todo = set(), list(headers.get(bid, {}).get("depends_on", []))
    while todo:
        if (u := todo.pop()) not in seen:
            seen.add(u)
            todo += headers.get(u, {}).get("depends_on", [])
    return seen


def why_not_ready(bid, headers, decisions, mode, busy=()) -> list[tuple]:
    """[] when `bid` may start; `mode` is approved | merged (anything else counts as merged)."""
    h, ok = headers[bid], UPSTREAM_OK.get(mode, DONE)
    out = [("after", d) for d in h.get("depends_on", []) if headers.get(d, {}).get("state", "planned") not in ok]
    out += [("decision", q) for q in blocking(bid, decisions)]
    ups = upstreams(headers, bid)
    for other, oh in headers.items():
        if other == bid or other in ups or bid in upstreams(headers, other) or not (started(oh) or other in busy):
            continue
        if any(seat.paths_overlap(a, b) for a in h["owns_paths"] for b in oh.get("owns_paths", [])):
            out.append(("overlaps", other))
    return out


def full_block(headers, direct, why) -> dict | None:
    """Every unfinished batch waits on the user (§10.2): `direct` {batch: reason} waits itself; a planned or ready
    batch whose every not-ready reason points at a waiting batch (`why`, from why_not_ready) waits behind it.
    Returns the root reasons {batch: reason} when all wait, else None (also when nothing is unfinished)."""
    todo = [b for b, h in headers.items() if h.get("state", "planned") not in FINISHED]
    waiting, grew = set(direct), True
    while grew:
        grew = False
        for b in todo:
            rs = why.get(b)
            if b not in waiting and rs and all(k in ("after", "overlaps") and ref in waiting for k, ref in rs):
                waiting.add(b)
                grew = True
    if not todo or any(b not in waiting for b in todo):
        return None
    return {b: direct[b] for b in todo if b in direct}
