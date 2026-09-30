"""Authority table #0–#23 (DESIGN §8.1) and its presets, picked by the AUTHZ key `authz.preset`
(conservative | balanced | hands_off, strictest first). balanced is the §8.1 table; conservative is its mechanical
tightening: every non-🔒 row owned by the controller or the decider goes to the user, nothing else changes (only
tightens, no user approval needed; controller decision on m2a.2 r1). hands_off is balanced with the user's rows listed
in the AUTHZ key `authz.hands_off_delegate` (category numbers, picked by the user) moved to the decider, never a 🔒
row; without that key it raises.

Row fields: owner (user | seat | controller | decider | rule), provisional (may be done provisionally first),
notify (none | report | push; report = the next run report, §11.3), locked (🔒: fixed to the user in every preset), hard (the row's hard block matches
paths, commands or tools, so an approval can be released by an exemption; §8.2).
"""
from typing import NamedTuple

from foremind.defaults import TABLE


class Row(NamedTuple):
    owner: str
    provisional: bool
    notify: str
    locked: bool = False
    hard: bool = False


LOCKED = frozenset({6, 7, 9, 17, 19, 20, 22})

BALANCED = {
    0: Row("user", False, "push"),  # intent confirmation
    1: Row("seat", False, "none"),  # implementation details
    2: Row("seat", False, "none"),  # approach within existing deps (seat writes a D record)
    3: Row("decider", True, "report", hard=True),  # new dev dependency
    4: Row("user", False, "push", hard=True),  # new runtime dependency or third-party service
    5: Row("controller", False, "report", hard=True),  # internal interface / contract (contract batch)
    6: Row("user", False, "push", True, True),  # external interface / public contract
    7: Row("user", False, "push", True, True),  # database schema / migration
    8: Row("controller", True, "report"),  # scope within a batch (owns_paths, split, merge)
    9: Row("user", False, "push", True),  # scope touching the frozen goal or acceptance (goal hash, not a hook)
    10: Row("decider", True, "report"),  # ambiguity with a precedent, or reversible and small
    11: Row("user", False, "push"),  # other ambiguity
    12: Row("user", False, "push"),  # product behaviour, copy, visuals (provisional only for small copy: not here)
    13: Row("controller", True, "none"),  # priority not affecting a milestone
    14: Row("user", False, "push"),  # priority affecting a milestone or date
    15: Row("decider", False, "report"),  # review dispute arbitration
    16: Row("decider", True, "report"),  # cost within budget
    17: Row("user", False, "push", True, True),  # over budget, paid calls
    18: Row("user", False, "push", hard=True),  # money, security, permissions, auth
    19: Row("user", False, "push", True, True),  # deleting data or external resources
    20: Row("user", False, "push", True, True),  # publish, deploy, send out, side-effect tools
    21: Row("user", False, "none"),  # delivery level (enforced by the gate, §7.5)
    22: Row("user", False, "push", True, True),  # this system's and the host's config
    23: Row("rule", False, "none", hard=True),  # push, open PR: by the repo's delivery convention (§7.5)
}
CONSERVATIVE = {k: r._replace(owner="user") if r.owner in ("controller", "decider") and not r.locked else r
                for k, r in BALANCED.items()}
PRESETS = {"conservative": CONSERVATIVE, "balanced": BALANCED}
ORDER = ["conservative", "balanced", "hands_off"]  # strictest first: the AUTHZ_ORDERS entry for authz.preset


def row(cfg, category: int) -> Row:
    """The row of `category` under the configured preset (default balanced; anything unknown, a non-string included,
    reads as conservative, the strictest). hands_off without a well-formed authz.hands_off_delegate raises
    ValueError."""
    p = cfg.get("authz.preset", TABLE["authz.preset"])
    if p == "hands_off":
        d = cfg.get("authz.hands_off_delegate")
        if not isinstance(d, list) or not all(type(k) is int and k in BALANCED for k in d):
            raise ValueError("authz.preset = hands_off 需要 authz.hands_off_delegate（交给决策者的类别号列表，0–23，"
                             "由用户确认）；没设或写错时请用 balanced 或 conservative")
        r = BALANCED[category]
        return r._replace(owner="decider") if category in d and r.owner == "user" and not r.locked else r
    return PRESETS.get(p if isinstance(p, str) else "", CONSERVATIVE)[category]


def owner(cfg, category: int) -> str:
    return row(cfg, category).owner
