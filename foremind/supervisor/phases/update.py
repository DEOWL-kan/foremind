"""Automatic update (DESIGN §7.6, m2b.7): an approved merge_dev batch whose latest gate result failed on up_to_date
alone gets one `foremind update <batch>` job per set of heads, as the supervisor (never while blocked, nor while a
gate or update job of the batch runs). A run that found the batch busy (`update_busy`) is tried once more after the
next gate result; an update job that fails (not a conflict or re-review, which go to the seat) is told once.

Also tells the user once, blocked or not, past gate.ci_pending_max_min minutes (default 120):
- a repo whose CI the batch's latest gate result still finds pending, since the first pending result on that head
  after its last result not pending there (`ci_pending_long`, written here: an in_review batch gets one gate run per
  receipt, so no later run looks again; once per wait, m2d.6: pending again after a result that was not is a new one);
- a repo a gate run left in the host's merge queue (`merge_queued`) while the batch is still delivered at that head
  (a PR the queue dropped stays open, and the gate keeps reading it as queued).
A batch failed on review.MAX_TIMEOUTS reviewer timeouts (`review_timeouts`) is told by the report phase's one failed
notice, with the timeouts in it (m2c.7: it was two P1s, m2b.6 r3).
"""
import json

from foremind import review
from foremind.defaults import TABLE
from foremind.supervisor.tick import _epoch, fm

KIND = "update"
WAITING = ("in_review", "approved", "delivered")  # the states a gate result can be pending in


def run(t, blocked):
    last = _tell(t)
    if blocked:
        return
    for bid in t.order:
        g = last.get(bid)
        if not g or g.get("failing") != ["up_to_date"] or t.state(bid) != "approved" \
                or t.in_flight(bid, ("gate", KIND)):
            continue
        tries = [e for e in t.evs if e["type"] == f"sv_{KIND}" and e["phase"] == "intent" and e.get("batch") == bid
                 and e.get("heads") == g["heads"]]
        with t.guard("update", bid):
            if not t.merge_dev(bid) or any(e["dedupe_id"] not in t.results or not _busy(t, e) for e in tries):
                continue  # once per heads, unless it only found the batch busy
            t.start_job(KIND, f"{bid}:{review.heads_hash(g['heads'])}:{g['id'][:12]}", fm("update", bid),
                        env={"FOREMIND_ROLE": "supervisor"},
                        timeout_s=int(t.cfg.get("oneshot.timeout_min", TABLE["oneshot.timeout_min"])) * 60,
                        batch=bid, heads=g["heads"])


def _limit(t, bid) -> float:
    v = (t.bcfg(bid) or t.cfg).get("gate.ci_pending_max_min")
    return (v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
            else TABLE["gate.ci_pending_max_min"]) * 60


def _tell(t) -> dict:
    """The long waits of the module docstring; returns {batch: its latest gate_result event}."""
    last, pending, queued = {}, {}, {}
    for e in t.evs:
        b = e.get("batch")
        if e["type"] == "gate_result":
            last[b] = e
            ci, hd = {n[3:] for n in e.get("pending") or [] if n.startswith("ci:")}, e.get("heads") or {}
            for rid in ci | set(hd):
                if rid in ci:
                    pending.setdefault((b, rid, hd.get(rid)), e["ts"])
                else:  # m2b.7 r2: the wait on this head starts again at its next pending result
                    # ponytail: a result that did not ask CI (local_first before its delivery run) ends it too; the
                    # notice comes later then, never early
                    pending.pop((b, rid, hd.get(rid)), None)
        elif e["type"] == "merge_queued":
            queued.setdefault((b, e.get("repo"), e.get("head")), e["ts"])
    def long(bid, rid, head, since, states):
        g = last.get(bid) or {}
        return bid in t.headers and t.state(bid) in states and (g.get("heads") or {}).get(rid) == head \
            and t.now - _epoch(since) > _limit(t, bid)

    for (bid, rid, head), since in pending.items():
        if long(bid, rid, head, since, WAITING) and f"ci:{rid}" in last[bid].get("pending", []):
            t.emit("ci_pending_long", dedupe_id=f"ci_pending_long:{bid}:{rid}:{head}:{since}", batch=bid, repo=rid,
                   head=head, since=since)
    told = {e["dedupe_id"] for e in t.evs if e["type"] == "notify"}
    for e in t.evs:
        if e["type"] == "ci_pending_long" and f"notify:ci_pending_long:{e['id']}" not in told:
            t.notify(f"ci_pending_long:{e['id']}", f"{e.get('batch')} CI 久等",
                     f"{e.get('batch')} 的仓库 {e.get('repo')} 从 {e.get('since')} 起 CI 一直 pending；看一下 CI 是否卡住。"
                     f"CI 已有结果而批次没动的，执行 foremind gate {e.get('batch')} 重跑门禁。")
    for (bid, rid, head), since in queued.items():
        if f"notify:merge_queue_long:{bid}:{rid}:{head}" not in told and long(bid, rid, head, since, ("delivered",)):
            t.notify(f"merge_queue_long:{bid}:{rid}:{head}", f"{bid} 合入队列久等",
                     f"{bid} 的仓库 {rid} 从 {since} 起在合入队列里，还没合入。看 PR 时间线：被队列移出的 PR 仍是 OPEN，"
                     "门禁会一直当它在排队；处理后在 PR 页面重新加入队列。")
    return last


def _busy(t, it) -> bool:
    return any(e["type"] == "update_busy" and e.get("action") == it["dedupe_id"] for e in t.evs)


def after(t, it, ok, st, jid):
    try:
        out = json.loads((t.sd / "jobs" / jid / "stdout.log").read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError, TypeError):
        out = {}
    bid = it["batch"]
    if isinstance(out, dict) and out.get("outcome") == "busy":
        t.emit("update_busy", batch=bid, action=it["dedupe_id"], why=out.get("why"))
    elif not ok and st.get("exit_code") != 1:  # 1: conflict or re-review, already sent to the seat
        t.notify(f"update_failed:{it['dedupe_id']}", f"{bid} 自动更新失败",
                 f"{bid} 落后目标分支，自动更新没成功（{st.get('exit_code', st['state'])}）；处理后执行 foremind update {bid}。")
