"""Goal freeze, plan approval and amendment (DESIGN §4.2 S2, S8, S9; §1.5; §20 I1, I26).

Each takes the project state lock, writes files, then the event (lock order: state lock, then the events lock).
A frozen goal only changes through `amend(goal=..., user_approved=True)` (#9). Amendments re-run S6, bind the goal
hash in a revision, and never touch started batches (running and later; §4.2 S9: finished ones only get notes).
"""
from datetime import datetime, timezone

from foremind.events import EventLog
from foremind.fsutil import project_lock
from foremind.paths import state_dir
from foremind.plan import model
from foremind.plan import validate as v
from foremind.plan.model import PROGRAM_FIELDS, Doc, PlanError
from foremind.schemas import MODES  # auto < watch < accompany < user: the user takes part more
from foremind.state import BATCH, IllegalTransition, transition

AMENDABLE = ("planned", "ready")


class Rejected(PlanError):
    """S6 failed; `report` says why."""

    def __init__(self, report):
        super().__init__("; ".join(report["errors"]) or "owns_paths overlap across plans")
        self.report = report


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _event(root, type, **fields):
    EventLog(state_dir(root) / "events.jsonl").append(type, **fields)


def _goal(old, body, bound, *, user_approved):
    """The goal Doc for `body`: `old` if it is that goal, frozen intact. `bound` is the hash the goal is bound to
    (plan.md's goal_hash, else goal.md's frozen sha256): anything else needs the user's approval."""
    h = model.text_hash(body)
    if bound is not None and h != bound and not user_approved:
        raise PlanError("the goal is frozen; changing it needs the user's approval (#9: plan amend --goal "
                        "--user-approved)")
    if old is not None and "frozen_at" in old.header and old.header.get("sha256") == h == model.text_hash(old.body):
        return old
    return Doc({"frozen_at": _now(), "sha256": h}, body)


def freeze_goal(root, plan_id, body: str | None = None) -> str:
    """Freeze `body` (default: goal.md's current text) as the plan's goal; returns its hash. Once plan.md exists
    its goal_hash is the goal: freezing only restores that one."""
    d = model.plan_dir(root, plan_id)
    path = d / "goal.md"
    with project_lock(root):
        old = model.read(path) if path.exists() else None
        if body is None and old is None:
            raise PlanError(f"{path}: missing")
        body = old.body if body is None else body
        if not v.REQ.search(body):
            raise PlanError("goal.md needs at least one REQ-n")
        if (d / "plan.md").exists():
            bound = model.read(d / "plan.md").header.get("goal_hash")
        else:
            bound = old.header.get("sha256") if old is not None and "frozen_at" in old.header else None
        new = _goal(old, body, bound, user_approved=False)
        if new is not old:
            model.write_goal(root, plan_id, new)
            _event(root, "goal_frozen", plan=plan_id, goal_hash=new.header["sha256"])
        return new.header["sha256"]


def approve(root, plan_id, *, config=None) -> dict:
    """The user's approval (§4.2 S8): approved_at / approved_by, batches planned, task configs approved as written."""
    with project_lock(root):
        plan = model.load(root, plan_id)
        if "approved_at" in plan.doc.header:
            raise PlanError(f"{plan_id} is already approved; change it with plan amend")
        report = v.validate(root, plan, others=model.load_others(root, plan_id), config=config, user_approved=True)
        if not report["ok"]:
            raise Rejected(report)
        plan.doc.header.update(approved_at=_now(), approved_by="user")
        for d in plan.active().values():
            d.header.setdefault("state", "planned")
            if "config" in d.header:
                d.header["config_approved"] = model.config_hash(d.header)
        model.write(root, plan)
        _event(root, "plan_approved", plan=plan_id, approved_by="user", goal_hash=plan.doc.header["goal_hash"],
               plan_hash=model.plan_hash(plan))
        return report


def amend(root, plan_id, *, reason: str, batches=(), drop=(), goal: str | None = None, user_approved=False,
          config=None) -> dict:
    """Replace or add batch files (`batches`: Docs), cancel `drop`, change the goal; re-runs S6 and adds the
    depends_on edges it suggests. Without `user_approved` the revision is the controller's: goal unchanged, no
    started batch cancelled, and on an approved plan hard_block and mode only tighten (§4.2 5c)."""
    if not reason or not reason.strip():
        raise PlanError("amend needs a reason")
    with project_lock(root):
        plan = model.load(root, plan_id)
        approved = "approved_at" in plan.doc.header
        bound = model.is_bound(root, plan)  # config_approved stamps only count on a plan nobody edited by hand
        if approved and not bound and not user_approved:
            raise PlanError(f"{plan_id} changed outside plan amend since its last approval; restore it, or plan amend "
                            "--user-approved")
        old_goal, goal_hash = plan.goal, plan.doc.header["goal_hash"]
        if goal is not None:
            plan.goal = _goal(plan.goal, goal, goal_hash, user_approved=user_approved)
        elif plan.goal is not None and plan.goal.header.get("sha256") != goal_hash and not user_approved:
            raise PlanError("goal.md is not the goal plan.md is bound to; restore goal.md, or plan amend --goal "
                            "--user-approved")
        for doc in batches:
            bid = doc.header.get("id")
            old = plan.batches.get(bid)
            if old is not None and old.header.get("state", "planned") not in AMENDABLE:
                raise PlanError(f"{bid} is {old.header['state']}: a started batch is not amended")
            h = {k: x for k, x in doc.header.items() if k not in PROGRAM_FIELDS}
            if approved and old is not None and not user_approved:
                _only_tightens(bid, old.header, h)
            h |= {k: old.header[k] for k in PROGRAM_FIELDS if old is not None and k in old.header}
            if approved:
                h.setdefault("state", "planned")
            if old is None:
                plan.doc.header["batches"].append(bid)
            plan.batches[bid] = Doc(h, doc.body)
        for bid in drop:
            if bid not in plan.batches:
                raise PlanError(f"{bid}: not a batch of {plan_id}")
            h = plan.batches[bid].header
            if h.get("state") not in model.UNSTARTED and not user_approved:
                raise PlanError(f"{bid} is {h['state']}: cancelling a started batch needs the user's approval")
            try:
                h["state"] = transition(BATCH, h["state"], "cancelled") if "state" in h else "cancelled"
            except IllegalTransition as e:
                raise PlanError(str(e)) from None
        changed = {doc.header.get("id") for doc in batches}
        for bid, d in plan.batches.items():  # the user approved the batches this revision shows; the other stamps
            if user_approved and bid in changed and "config" in d.header:  # only count on a bound plan
                d.header["config_approved"] = model.config_hash(d.header)
            elif not bound:
                d.header.pop("config_approved", None)
        if plan.goal is not None:
            plan.doc.header["goal_hash"] = plan.goal.header["sha256"]
        others = model.load_others(root, plan_id)
        kw = dict(others=others, config=config, config_bound=True)  # each batch by its own stamp, bound once written
        report = v.validate(root, plan, **kw)
        edges = report["suggested_edges"]
        if v.apply_edges(plan, report):
            report = v.validate(root, plan, **kw)
        if not report["ok"]:
            raise Rejected(report)
        revs = plan.doc.header["revisions"]
        revs.append({"n": len(revs) + 1, "at": _now(), "reason": reason.strip(),
                     "goal_hash": plan.doc.header["goal_hash"], "approved_by": "user" if user_approved else "controller"})
        if plan.goal is not old_goal:
            model.write_goal(root, plan_id, plan.goal)
        model.write(root, plan)
        _event(root, "plan_amended", plan=plan_id, revision=len(revs), approved_by=revs[-1]["approved_by"],
               goal_hash=plan.doc.header["goal_hash"], changed=[d.header.get("id") for d in batches], dropped=list(drop),
               added_edges=edges, plan_hash=model.plan_hash(plan))
        return report


def _only_tightens(bid, old, new):
    if lost := [c for c in old.get("hard_block", []) if c not in new.get("hard_block", [])]:
        raise PlanError(f"{bid}: removing hard_block {lost} needs the user's approval")
    if MODES.index(new.get("mode", "auto")) < MODES.index(old.get("mode", "auto")):
        raise PlanError(f"{bid}: mode {old.get('mode')} -> {new.get('mode')} is looser; needs the user's approval")
