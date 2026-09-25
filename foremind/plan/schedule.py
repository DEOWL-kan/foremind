"""Topological order and waves (DESIGN §4.2 5e): contract batches, then risk and unknowns, then critical path, then value.

Works on `{batch_id: header}` in plan order. Dependencies outside that dict (other plans, finished batches) do not
hold a wave back; the supervisor checks them at run time. Risk = tiers.difficulty (L > M > S); value = plan order.
"""
from foremind.plan.model import is_contract

_RISK = {"L": 2, "M": 1, "S": 0}


class CycleError(ValueError):
    pass


def _deps(batches):
    return {b: [d for d in h["depends_on"] if d in batches] for b, h in batches.items()}


def topo(deps: dict) -> list[str]:
    """`deps`: {batch: [dependencies]}; dependencies first, otherwise in insertion order. Raises CycleError."""
    order, state = [], {}

    def visit(b, stack):
        if state.get(b) == "done":
            return
        if state.get(b) == "open":
            raise CycleError(" -> ".join(stack[stack.index(b):] + [b]))
        state[b] = "open"
        for d in deps[b]:
            visit(d, stack + [b])
        state[b] = "done"
        order.append(b)

    for b in deps:
        visit(b, [])
    return order


def _tail(batches):
    """Longest chain (in batches) from each batch to the end of the plan, and the next batch on it."""
    deps, tail, nxt = _deps(batches), dict.fromkeys(batches, 1), {}
    for b in reversed(topo(deps)):  # dependents first, so tail[b] is final when b is reached
        for d in deps[b]:
            if tail[b] + 1 > tail.get(d, 1):
                tail[d], nxt[d] = tail[b] + 1, b
    return tail, nxt


def critical_path(batches) -> list[str]:
    if not batches:
        return []
    tail, nxt = _tail(batches)
    b = max(batches, key=lambda x: (tail[x], -list(batches).index(x)))
    path = [b]
    while path[-1] in nxt:
        path.append(nxt[path[-1]])
    return path


def waves(batches, width: int | None = None) -> list[list[str]]:
    """Raises CycleError on a dependency cycle. `width` caps batches per wave (seats, quota)."""
    if width is not None and width < 1:
        raise ValueError("width must be >= 1")
    tail, _ = _tail(batches)
    deps, rank = _deps(batches), {b: i for i, b in enumerate(batches)}
    done, out = set(), []
    while len(done) < len(batches):
        ready = sorted((b for b in batches if b not in done and set(deps[b]) <= done),
                       key=lambda b: (not is_contract(batches[b]), -_RISK[batches[b]["tiers"]["difficulty"]],
                                      -tail[b], rank[b]))
        wave = ready[:width]
        out.append(wave)
        done.update(wave)
    return out
