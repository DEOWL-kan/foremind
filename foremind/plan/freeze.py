"""Goal freeze, plan approval and amendment (DESIGN §4.2 S2, S8, S9; §1.5; §20 I1, I26).

Each takes the project state lock, writes files, then the event (lock order: state lock, then the events lock).
A frozen goal only changes through `amend(goal=..., user_approved=True)` (#9). Amendments re-run S6 and bind the goal
hash in a revision. A started batch (running and later, supervisor.ready.started) only changes with the user's approval
and then only in STARTED_AMENDABLE, owns_paths only growing; `expand_scope` widens one after a #8 decision. Finished
batches are never amended (§4.2 S9: they only get notes).

An amendment writes several files (goal.md, batch files, plan.md last): before them a `plan_amend` intent lists each
file with its sha256 before and after (and the new text of those that change); plan_amended is its result. The next
amend / expand_scope of that plan finishes an intent left without a result (_recover): the files still at their
`before` hash get the new text (none if one not yet written changed since), and plan_amended is written if the plan
then has the intended plan_hash; otherwise plan_amend_failed closes the intent and the report (or the error that ends
the call) says so: the plan still bound (nothing of it written: the last approval stands, REQ-18), or no longer.
"""
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from foremind import header as hdr
from foremind import lock, seat
from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock, sha256_bytes
from foremind.paths import state_dir
from foremind.plan import model
from foremind.plan import validate as v
from foremind.plan.model import PROGRAM_FIELDS, Doc, PlanError
from foremind.schemas import MODES  # auto < watch < accompany < user: the user takes part more
from foremind.state import BATCH, IllegalTransition, transition
from foremind.supervisor import ready

AMENDABLE = ("planned", "ready")
STARTED_AMENDABLE = ("owns_paths", "accept_commands", "reads", "must_read", "tools")


class Rejected(PlanError):
    """S6 failed; `report` says why."""

    def __init__(self, report):
        super().__init__("; ".join(report["errors"]) or "owns_paths overlap across plans")
        self.report = report


@contextmanager
def _reporting():
    """The list _recover's notes go into: the caller puts them on its report, and a PlanError that ends the call
    carries them (Rejected: in its errors too), since their intent is closed and nothing says them again."""
    notes = []
    try:
        yield notes
    except PlanError as e:
        if notes:
            if isinstance(e, Rejected):
                e.report["errors"][:0] = notes
            e.args = ("\n".join([*notes, str(e)]),)
        raise


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
            # who: a planner's plan submit freezes too; neither a role nor a session is the user (m2d.7 ⑦)
            _event(root, "goal_frozen", plan=plan_id, goal_hash=new.header["sha256"],
                   by=os.environ.get("FOREMIND_ROLE") or os.environ.get("FOREMIND_SESSION") or "user")
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
    with project_lock(root), _reporting() as notes:
        notes += _recover(root, plan_id)
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
            body = doc.body
            if old is not None and old.header.get("state", "planned") not in AMENDABLE:
                _started_change(bid, old, doc, user_approved=user_approved)
                body = old.body  # its `## 状态` section is the seat's
            h = {k: x for k, x in doc.header.items() if k not in PROGRAM_FIELDS}
            if approved and old is not None and not user_approved:
                _only_tightens(bid, old.header, h)
            h |= {k: old.header[k] for k in PROGRAM_FIELDS if old is not None and k in old.header}
            if approved:
                h.setdefault("state", "planned")
            if old is None:
                plan.doc.header["batches"].append(bid)
            plan.batches[bid] = Doc(h, body)
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
        changed = [doc.header.get("id") for doc in batches]
        # the user approved the batches this revision shows; the other stamps only count on a bound plan
        report = _revise(root, plan, reason=reason, approved_by="user" if user_approved else "controller",
                         bound=bound, stamp=changed if user_approved else (), user_approved=user_approved,
                         config=config, goal_changed=plan.goal is not old_goal, changed=changed, dropped=list(drop))
        report["warnings"][:0] = notes
        return report


def expand_scope(root, batch, add_paths, *, decision, approved_by, config=None) -> dict:
    """Widen a started batch's owns_paths (#8, §4.2 S9) after decision `decision` (Q-n). The new paths may not
    overlap a started batch it has no depends_on path to or from (PlanError); an unstarted one gets a depends_on edge
    through S6. Adds a revision carrying the decision and a plan_amended event, so the plan stays bound. Only on a
    bound plan whose goal.md is the bound goal, whoever approved it: a #8 answer approves the widening, nothing else."""
    with project_lock(root), _reporting() as notes:
        plan_id = model.read(model.batch_path(root, batch)).header["plan_id"]
        notes += _recover(root, plan_id)  # an apply cut short between the files and plan_amended: bound again
        plan = model.load(root, plan_id)
        if not model.is_bound(root, plan):
            raise PlanError(f"{plan_id} changed outside plan amend since its last approval; restore it, or plan amend "
                            "--user-approved, before widening a batch")
        if plan.goal is None or plan.goal.header.get("sha256") != plan.doc.header["goal_hash"]:
            raise PlanError("goal.md is not the goal plan.md is bound to; restore goal.md, or plan amend --goal "
                            "--user-approved")
        if (doc := plan.batches.get(batch)) is None:
            raise PlanError(f"{batch}: not a batch of {plan_id}")
        if not ready.started(doc.header):
            raise PlanError(f"{batch} is {doc.header.get('state', 'planned')}, not started; widen it with plan amend")
        owns = doc.header["owns_paths"]
        new = [p for p in dict.fromkeys(add_paths) if p not in owns]
        others = model.load_others(root, plan_id)
        if not new:
            if any(r.get("decision") == decision for r in plan.doc.header["revisions"]):  # a retried apply: done
                report = v.validate(root, plan, others=others, config=config, config_bound=True)
                report["warnings"][:0] = notes
                return report
            raise PlanError(f"{batch} already owns {list(add_paths)}")
        heads = {b: d.header for p in (plan, *others) for b, d in p.active().items()}
        ups = ready.upstreams(heads, batch)
        for o, oh in heads.items():  # a seat being opened holds the lock while still ready (seat._claim)
            if (o == batch or o in ups or batch in ready.upstreams(heads, o)
                    or not (ready.started(oh) or lock.holder(root, o))):
                continue
            if hits := [(a, b) for a in new for b in oh["owns_paths"] if seat.paths_overlap(a, b)]:
                raise PlanError(f"{batch}: {hits[0][0]} overlaps {hits[0][1]} of started batch {o}; the two cannot "
                                "run in parallel (§4.2 S9): wait for it, or stop one of them")
        doc.header["owns_paths"] = owns + new
        report = _revise(root, plan, reason=f"扩大 {batch} 的范围（#8）：{'、'.join(new)}", approved_by=approved_by,
                         bound=True, config=config, decision=decision, changed=[batch])
        report["warnings"][:0] = notes
        return report


def _revise(root, plan, *, reason, approved_by, bound, stamp=(), user_approved=False, config=None, decision=None,
            goal_changed=False, changed=(), dropped=()) -> dict:
    """S6 with its suggested edges, then the revision, the files and plan_amended; under the project lock.
    With `user_approved`, S6 runs as `plan approve` does (the widenings the approval grants are listed), then again
    once the batches in `stamp` carry the approval: it covers those, every other batch stands on its own stamp."""
    for bid, d in plan.batches.items():
        if bid in stamp or not bound:
            d.header.pop("config_approved", None)
    if plan.goal is not None:
        plan.doc.header["goal_hash"] = plan.goal.header["sha256"]
    others = model.load_others(root, plan.id)
    kw = dict(others=others, config=config, config_bound=True)  # each batch by its own stamp, bound once written
    report = v.validate(root, plan, **kw, user_approved=user_approved)
    edges = report["suggested_edges"]
    if v.apply_edges(plan, report):
        report = v.validate(root, plan, **kw, user_approved=user_approved)
    for bid in stamp:
        if "config" in plan.batches[bid].header:
            plan.batches[bid].header["config_approved"] = model.config_hash(plan.batches[bid].header)
    if report["ok"] and user_approved and not (gate := v.validate(root, plan, **kw))["ok"]:
        report = gate
    if not report["ok"]:
        raise Rejected(report)
    revs = plan.doc.header["revisions"]
    revs.append({"n": len(revs) + 1, "at": _now(), "reason": reason.strip(), "goal_hash": plan.doc.header["goal_hash"],
                 "approved_by": approved_by, **({"decision": decision} if decision else {})})
    result = dict(plan=plan.id, revision=len(revs), approved_by=approved_by, decision=decision,
                  goal_hash=plan.doc.header["goal_hash"], changed=list(changed), dropped=list(dropped),
                  added_edges=edges, plan_hash=model.plan_hash(plan))
    files = {model.batch_path(root, b): hdr.render(d.header, d.body) for b, d in plan.batches.items()}
    if goal_changed:
        files[model.plan_dir(root, plan.id) / "goal.md"] = hdr.render(plan.goal.header, plan.goal.body)
    files[model.plan_dir(root, plan.id) / "plan.md"] = hdr.render(plan.doc.header, plan.doc.body)
    did = f"plan_amend:{plan.id}:{uuid.uuid4().hex}"
    _event(root, "plan_amend", phase="intent", dedupe_id=did, plan=plan.id, revision=len(revs),
           files=_intent_files(root, files), result=result)
    if goal_changed:
        model.write_goal(root, plan.id, plan.goal)
    model.write(root, plan)
    _event(root, "plan_amended", dedupe_id=did, **result)
    return report


def _hash(path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def _intent_files(root, files) -> list[dict]:
    """[{path (under .foremind/), before, sha256, text?}]: the text only where the file changes (events stay small)."""
    out = []
    for p, text in files.items():
        f = {"path": p.relative_to(state_dir(root)).as_posix(), "before": _hash(p),
             "sha256": sha256_bytes(text.encode("utf-8"))}
        out.append(f if f["before"] == f["sha256"] else {**f, "text": text})
    return out


def _written(path, f, dropped) -> bool:
    """`path` holds intent file `f`'s text but for what plan_hash leaves out (a batch's runtime fields, `## 状态`).
    A batch the amend cancels (`dropped`) keeps its state in the comparison: `cancelled` is its whole change."""
    skip = [k for k in model.RUNTIME_FIELDS if not (dropped and k == "state")]

    def bound(text):
        h, body = hdr.parse(text)
        if not f["path"].startswith("batches/"):
            return h, body
        return {k: x for k, x in h.items() if k not in skip}, model.spec(body)
    try:
        return bound(path.read_text(encoding="utf-8")) == bound(f["text"])
    except (OSError, ValueError):  # gone, or a header nobody can read: not written
        return False


def _recover(root, plan_id) -> list[str]:
    """Finish `plan_id`'s plan_amend intents that have no result (the amend stopped between its files): when every
    file this amend changes and has not yet written is still at its `before` hash, write their new text (one changed
    since by anyone else: write none; one written and since changed only in its runtime fields or `## 状态` counts as
    written, unless it is a batch the amend cancels and its state is not `cancelled`), then plan_amended if the plan now has the intended plan_hash (runtime fields and `## 状态` do not
    count), else plan_amend_failed. Either closes the intent. Returns what it did, for the report."""
    log, sd, out = EventLog(state_dir(root) / "events.jsonl"), state_dir(root), []
    for it in log.open_intents():
        if it["type"] != "plan_amend" or it.get("plan") != plan_id:
            continue
        res, todo, left = it["result"], [], []
        dropped = {f"batches/{b}.md" for b in res.get("dropped", ())}
        for f in it["files"]:
            if (h := _hash(sd / f["path"])) == f["sha256"] or "text" not in f:
                continue  # at its new hash, or not one this amend changes: the plan_hash below judges it
            if h == f["before"]:
                todo.append(f)
            elif not _written(sd / f["path"], f, f["path"] in dropped):
                left.append(f)
        for f in [] if left else todo:  # all or none: never half of it over someone else's change
            atomic_write(sd / f["path"], f["text"])
        left = [f["path"] for f in left]
        try:
            plan = model.load(root, plan_id)
            done, bound = model.plan_hash(plan) == res["plan_hash"], model.is_bound(root, plan)
        except PlanError:
            done = bound = False
        if done:
            _event(root, "plan_amended", dedupe_id=it["dedupe_id"], recovered=True, **res)
            out.append(f"上次修订 {res['revision']} 写到一半中断，已按意图记录的哈希补完")
        else:
            _event(root, "plan_amend_failed", dedupe_id=it["dedupe_id"], plan=plan_id, revision=res["revision"],
                   left=left)
            why = f"上次修订 {res['revision']} 写到一半中断，补不完：{'、'.join(left) or '计划'} 在中断后又被改过；"
            # REQ-18: stopped before any file of it (what changed since is runtime only): the last approval stands
            out.append(why + (f"修订 {res['revision']} 一个文件也没写，没有生效，计划仍绑定；需要就重新 amend" if bound
                              else "计划不再绑定，核对后经 plan amend --user-approved 重新批准"))
    return out


def _started_change(bid, old, new, *, user_approved):
    """A started batch changes only with the user's approval, only in STARTED_AMENDABLE, owns_paths only growing."""
    st = old.header["state"]
    if not ready.started(old.header):
        raise PlanError(f"{bid} is {st}: neither unstarted nor started, not amended (finished batches only get notes)")
    if not user_approved:
        raise PlanError(f"{bid} is {st}: changing a started batch needs the user's approval")
    fixed = (*STARTED_AMENDABLE, *PROGRAM_FIELDS)
    if diff := sorted(k for k in old.header.keys() | new.header.keys()
                      if k not in fixed and old.header.get(k) != new.header.get(k)):
        raise PlanError(f"{bid} is {st}: only {', '.join(STARTED_AMENDABLE)} of a started batch change; not {diff}")
    if lost := [p for p in old.header["owns_paths"] if p not in new.header.get("owns_paths", [])]:
        raise PlanError(f"{bid} is {st}: owns_paths of a started batch only grow; {lost} would go")
    if model.spec(old.body) != model.spec(new.body):
        raise PlanError(f"{bid} is {st}: its body above `## 状态` does not change")


def _only_tightens(bid, old, new):
    if lost := [c for c in old.get("hard_block", []) if c not in new.get("hard_block", [])]:
        raise PlanError(f"{bid}: removing hard_block {lost} needs the user's approval")
    if MODES.index(new.get("mode", "auto")) < MODES.index(old.get("mode", "auto")):
        raise PlanError(f"{bid}: mode {old.get('mode')} -> {new.get('mode')} is looser; needs the user's approval")
