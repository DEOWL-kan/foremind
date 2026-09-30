"""`foremind update`: bring an approved batch's branches up to date with their targets before merging (DESIGN §7.6).

update() runs on approved or delivered batches whose lock holder is none, the user, or a seat that has ended or sits
idle with no tool open (else "busy", not a failure), holding batches/<id>.gate.lock so no gate runs meanwhile. The
batch goes to the `updating` side branch; every repo behind its freshly fetched target, except one merged into it
already (left as it is, keeping its base, for L0 reconcile; "merged" when that is all of them), is updated in the
batch worktree by `delivery.repo.<id>.update_method` (rebase, or merge = the default), hooks off. Then:
  conflict  every repo goes back to its pre-update head; -> changes_requested with the conflicted files (outside
            owns_paths: `foremind decide --new`, #8 or #5), sent like a must-fix list (review.request_changes)
  rebound   the latest receipt is approved, backed by the program's events as the gate checks them
            (gate.receipt_backed) and bound to the pre-update heads, and every updated repo's
            `git patch-id --verbatim` of <old base>...<old head> equals <new target>...<new head>: #23-system repos
            are pushed, and a copy of the receipt with the new heads becomes the newest one (r<n> from
            review.next_round, never written over; rebound_from = the heads its review started on) with its
            review_receipt event;
            -> approved, and the next gate reruns acceptance on the new heads
  rereview  otherwise: the updated heads stay; -> changes_requested asking for `foremind review`
rebound and rereview record `batch_updated {prior_heads, heads, bases, outcome}`: gate.bases() takes the new bases
from it, and the gate reads a remote still at a replaced head as stale, not diverged. Any other failure (Ctrl-C too)
aborts, resets every repo to its pre-update head (reset --keep: an edit made meanwhile stops it, and the batch stays
updating) and goes back to approved before re-raising. Merge, rebase, abort, reset and push run with hooks off. A run
that finds the batch still updating (the last one was killed) aborts what git has in progress, then goes back to
approved when the latest receipt is approved at the heads now, else asks for a re-review.
"""
import contextlib
import json

from foremind import carriers, gate, heartbeat, lock, pathmatch, review, schemas, seat
from foremind.defaults import TABLE
from foremind.fsutil import LockBusy, atomic_write, sha256_bytes
from foremind.paths import state_dir
from foremind.review import FlowError, git, is_ancestor

HOOKS_OFF = ("-c", "core.hooksPath=/dev/null")
GIT = ("git", *HOOKS_OFF, "-c", "rerere.enabled=false")  # no hooks, no replayed resolutions


class _Busy(Exception):
    pass


def update(root, batch_id, cfg, *, by) -> dict:
    """{"batch", "outcome": busy | current | merged | rebound | rereview | conflict, "state", ...}; FlowError on
    failure."""
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(gate.batch_lock(root, batch_id))
        except LockBusy:
            return {"batch": batch_id, "outcome": "busy", "why": "a gate or update run holds this batch"}
        try:
            return _update(root, batch_id, cfg, by)
        except _Busy as e:
            return {"batch": batch_id, "outcome": "busy", "why": str(e)}


def _seat_idle(root, cfg, batch_id):
    """§7.6: the holder is none, the user, or a seat that ended or is idle with no tool open; else _Busy."""
    s = lock.holder(root, batch_id)
    if s is None or s == lock.USER:
        return
    kind = next((e.get("carrier") for e in reversed(review.all_events(root)) if e["type"] == "seat_launch"
                 and e.get("phase") == "intent" and e.get("session") == s), None)
    try:
        st = carriers.get(kind or seat.setting(cfg, "carrier.kind"), root, cfg).read_state(s)
    except carriers.CarrierError as e:
        raise _Busy(f"seat {s}: {e}") from None
    if st.alive is False:
        return
    if not (st.alive and st.idle) or (heartbeat.read(root, s) or {}).get("tool_open"):
        raise _Busy(f"seat {s} is {'at work' if st.alive else 'in a state the carrier cannot tell'}")


def _method(cfg, repo_id) -> str:
    m = review.repo_cfg(cfg, repo_id, "update_method")
    if m in (None, "unknown"):
        return TABLE["delivery.repo.*.update_method"]  # merge: rewrites no pushed history
    if m not in ("rebase", "merge"):
        raise FlowError(f"{repo_id}: update_method {m!r} is neither rebase nor merge")
    return m


def _abort(wt):
    for op in ("rebase", "merge"):  # whichever is in progress; the other one refuses harmlessly
        review.run([*GIT, op, "--abort"], wt, check=False)


def _reset(pairs, hd):
    for r, wt in pairs:
        _abort(wt)
        # --keep, not --hard: the worktree was clean before the update, so a local change is someone's (a seat woken
        # meanwhile) and makes it refuse rather than lose it; the batch then stays updating for the next run
        if (p := review.run([*GIT, "reset", "-q", "--keep", hd[r.id]], wt, check=False)).returncode:
            raise FlowError(f"{r.id}: cannot go back to {hd[r.id]} without dropping local changes made meanwhile "
                            f"({p.stderr.strip()}); the batch stays updating: commit or remove them, then run "
                            "`foremind update` again")


def _apply(wt, tref, method) -> list[str]:
    """[] when the update went through, else the conflicted files (the update aborted); any other failure raises."""
    args = ["rebase", "--no-update-refs", tref] if method == "rebase" else ["merge", "--no-edit", "--ff", tref]
    p = review.run([*GIT, *args], wt, check=False)
    if p.returncode == 0:
        return []
    files = [f for f in git(wt, "diff", "--name-only", "--diff-filter=U", "-z").split("\0") if f]
    _abort(wt)
    if not files:
        raise FlowError(f"{wt}: git {method} {tref}: {(p.stderr or p.stdout).strip()}")
    return files


def _latest_receipt(root, batch_id) -> dict | None:
    """The newest receipt when it is valid and backed by the program's events as the gate requires
    (gate.receipt_backed), else None: its rebound copy gets a fresh event, which must never vouch for a file no
    review of ours wrote."""
    if not (rs := review.receipts(root, batch_id)):
        return None
    try:
        data = rs[-1][1].read_bytes()
        r = json.loads(data)
    except (OSError, ValueError):
        return None
    ok = isinstance(r, dict) and not schemas.validate("review_receipt", r) and r["batch"] == batch_id \
        and gate.receipt_backed(review.all_events(root), batch_id, rs[-1][1].name, data, r)
    return r if ok else None


def _rebind_blocker(r, pairs, hd, nd, trefs, bs) -> str | None:
    """Why the receipt `r` cannot follow the update to the new heads `nd` (None: it can)."""
    if not r:
        return "最新回执缺失，或与程序记录的审查事件对不上"
    if r["verdict"] != "approved":
        return "最新回执不是 approved"
    if r["heads"] != hd:
        return "最新回执绑定的 head 不是更新前的 head"
    for x, wt in pairs:
        if nd[x.id] == hd[x.id]:
            continue
        if x.id not in bs:
            return f"{x.id} 没有记录的 base"
        if gate.patch_id(wt, f"{bs[x.id]}...{hd[x.id]}") != gate.patch_id(wt, f"{trefs[x.id]}...{nd[x.id]}"):
            return f"{x.id} 更新后的累计改动与已审改动不再逐字节一致（patch-id 不同）"
    return None


def _resume(root, batch_id, pairs, by) -> bool:
    """The last update was killed midway: True once back at approved (the latest receipt still fits the heads)."""
    for _, wt in pairs:
        _abort(wt)
    hd = review.heads(pairs)
    r = _latest_receipt(root, batch_id)
    if r and r["verdict"] == "approved" and r["heads"] == hd:
        review.set_state(root, batch_id, "approved", expect=("updating",), reason="update_resumed", heads=hd)
        return True
    review.request_changes(root, batch_id, ["上一次 `foremind update` 被中断，worktree 的 head 已不是已批准回执绑定的 head。",
                                            "核对 worktree（git status、git log）后重新请求审查。"],
                           by=by, reason="update_interrupted", title="更新中断")
    return False


def _update(root, batch_id, cfg, by) -> dict:
    h = review.load_batch(root, batch_id)
    st = h.get("state", "planned")
    if st not in ("approved", "delivered", "updating"):
        raise _Busy(f"{batch_id} is {st}; update runs on approved or delivered batches")
    pairs = review.batch_repos(root, h, cfg)
    if st == "updating":
        _seat_idle(root, cfg, batch_id)
        if not _resume(root, batch_id, pairs, by):
            return {"batch": batch_id, "outcome": "rereview", "state": review.load_batch(root, batch_id)["state"]}
        st = "approved"
    if bad := review.dirty(pairs):
        raise FlowError(f"uncommitted changes in {bad}; commit or remove them first")
    methods = {r.id: _method(cfg, r.id) for r, _ in pairs}
    for _, wt in pairs:
        review.branch(wt)  # a detached HEAD has no branch to update
    hd = review.heads(pairs)
    trefs = {r.id: review.target_ref(r, wt, cfg) for r, wt in pairs}
    behind = [r.id for r, wt in pairs if not is_ancestor(wt, trefs[r.id], hd[r.id])]
    if not behind:
        return {"batch": batch_id, "outcome": "current", "state": st, "heads": hd}
    bs = gate.bases(root, batch_id, pairs, hd)  # before anything moves
    # a behind repo the user merged already stays as it is (L0 reconcile's); the rest are updated
    done = [x.id for x, wt in pairs if x.id in behind and x.id in bs
            and gate.is_merged(wt, trefs[x.id], hd[x.id], bs[x.id])]
    behind = [rid for rid in behind if rid not in done]
    if not behind:
        return {"batch": batch_id, "outcome": "merged", "state": st, "heads": hd, "merged": done}
    review.set_state(root, batch_id, "updating", expect=(st,), before=lambda _h: _seat_idle(root, cfg, batch_id),
                     reason="update", by=by, heads=hd)
    conflicts, nd, r, why = {}, hd, None, None
    try:
        for x, wt in pairs:
            if x.id in behind and (files := _apply(wt, trefs[x.id], methods[x.id])):
                conflicts[x.id] = files
        if conflicts:
            _reset(pairs, hd)
        else:
            nd = review.heads(pairs)
            r = _latest_receipt(root, batch_id)
            if not (why := _rebind_blocker(r, pairs, hd, nd, trefs, bs)):
                n = review.next_round(review.all_events(root), batch_id)
                path = state_dir(root) / "batches" / f"{batch_id}.review.r{n}.json"
                if path.exists():  # no receipt of ours has that number yet: never write over someone else's file
                    raise FlowError(f"{path.name} exists but no event numbers it; remove it, then update again")
                # ponytail: a push failing after another repo's went through leaves that remote ahead of the reset
                # worktree (the gate reads it as diverged) until the next update pushes over it
                evs = review.all_events(root)
                for x, wt in pairs:
                    if nd[x.id] != hd[x.id] and review.push_by_system(cfg, x.id):
                        if not (rem := review.remote(x, wt)):
                            raise FlowError(f"{x.id}: #23 is the system's but the repo has no remote")
                        review._push(root, evs, batch_id, x, wt, rem, review.branch(wt), nd[x.id], git_opts=HOOKS_OFF)
    except BaseException:
        _reset(pairs, hd)
        if review.load_batch(root, batch_id).get("state") == "updating":
            review.set_state(root, batch_id, "approved", expect=("updating",), reason="update_failed", heads=hd)
        raise
    out = {"batch": batch_id, "heads": nd, "methods": {k: methods[k] for k in behind},
           **({"merged": done} if done else {})}
    if conflicts:
        lines = [f"目标分支已前进，更新有冲突，已中止并退回更新前的 head {hd}："]
        for rid, files in conflicts.items():
            lines.append(f"{rid}：冲突文件 {files}；在 worktree 里执行 `git {methods[rid]} {trefs[rid]}` 并解决冲突，"
                         "只改冲突涉及的文件")
        if outside := [f"{rid}:{f}" for rid, fs in conflicts.items() for f in fs
                       if not pathmatch.owns(f"{rid}:{f}", h["owns_paths"])]:
            lines.append(f"冲突落在 owns_paths 之外：{outside}；先执行 `foremind decide --new` 申请（#8 扩大范围，"
                         "契约文件为 #5），获批后再改")
        return {**out, "outcome": "conflict", "conflicts": conflicts, "state": review.request_changes(
            root, batch_id, lines, by=by, reason="update_conflict", title="更新分支有冲突")}
    outcome = "rereview" if why else "rebound"
    # a merged repo keeps its base: the merge-base with a target holding its head is the head itself, and against that
    # base gate.merged() would find no changes of its own
    review.events(root).append("batch_updated", batch=batch_id, by=by, prior_heads=hd, heads=nd, outcome=outcome,
                               bases={x.id: bs[x.id] if x.id in done else git(wt, "merge-base", trefs[x.id], nd[x.id])
                                      for x, wt in pairs})
    if why:
        lines = [f"目标分支已前进，已更新并提交（新 head {nd}），但不能沿用已批准的回执：{why}。",
                 "核对更新结果后执行 `foremind review` 重新审查。"]
        return {**out, "outcome": outcome, "why": why, "state": review.request_changes(
            root, batch_id, lines, by=by, reason="update_changed", title="更新后需重审")}
    # resolved/unresolved reconcile the original against its predecessor: not this copy's to carry (§7.6)
    new = {**{k: v for k, v in r.items() if k not in ("resolved", "unresolved")},
           "heads": nd, "round": n, "rebound_from": r.get("rebound_from", r["heads"])}
    data = json.dumps(new, ensure_ascii=False, indent=2) + "\n"
    # event first, as harvest does: a file on disk always has its event (a resumed run relies on it)
    review.events(root).append("review_receipt", batch=batch_id, round=n, path=path.name,
                               sha256=sha256_bytes(data.encode()), verdict=new["verdict"],
                               reviewer_session=new["reviewer_session"], rebound_from=new["rebound_from"])
    atomic_write(path, data)
    review.set_state(root, batch_id, "approved", expect=("updating",), reason="rebound", heads=nd)
    return {**out, "outcome": outcome, "receipt": path.name, "state": "approved"}
