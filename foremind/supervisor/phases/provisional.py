"""Phase: provisional decisions and precedent premises (m2b.4, DESIGN §8.4, §8.5). It calls no model, so it runs
under a full block too.

- A provisional Q-n past the deadline of its decisions/PV-<n>.json (none readable: taken as past) goes overdue; an
  overdue one is pushed once at P1 (notify key provisional_overdue:Q-n; ids and the two commands only, §8.7). It
  stays provisional in effect: never confirmed by itself, and release-check keeps holding it.
- REQ-17: a provisional or overdue Q-n whose program action failed (its last apply ended pending_apply_failed) is
  applied again (pending.apply) supervisor.gate_retry_min × 2^(n-1) minutes after its n-th failure (m2d.6 REQ-18),
  until supervisor.seat_retries retries have failed; then it is pushed once at P1 (notify key
  provisional_apply_failed:Q-n). It stays provisional either way.
- The overturn rate is checked again (pending.tighten).
- Each precedent in precedents.md that is neither superseded nor needing review already, and fits schema precedent,
  has its machine-checked premises compared with the facts now (catalog.holds, on the merged project config). Any
  that no longer holds marks the precedent needs_review (catalog.mark_review) and writes precedent_needs_review
  {precedent: J-n, premise} once per premise (dedupe_id; `id` is an event field of its own).
"""
import json
from datetime import datetime, timezone

from foremind import catalog, notify
from foremind.decide import pending
from foremind.fsutil import sha256_bytes
from foremind.schemas import validate
from foremind.supervisor.tick import _epoch, setting


def run(t, blocked):
    now = datetime.fromtimestamp(t.now, timezone.utc)
    pvs = pending.provisionals(t.root)
    for q in t.decisions:
        qid, st = q.get("id"), q.get("state")
        if st not in ("provisional", "overdue"):
            continue
        with t.guard("provisional", qid):
            pv = pvs.get(qid)
            if st == "provisional" and (pv is None or datetime.fromisoformat(pv["deadline"]) <= now):
                if pending.overdue(t.root, qid):
                    st = "overdue"
                    t.did.append(f"{qid}: overdue")
            if st == "overdue":
                body = "\n".join([f"{qid}（{pv['id'] if pv else '暂定决定'}）到期未确认，仍按暂定执行",
                                  f"确认：foremind decide {qid} --confirm", f"推翻：foremind decide {qid} --overturn"])
                if notify.notify(t.root, t.cfg, f"provisional_overdue:{qid}", f"暂定到期 {qid}", body, "P1"):
                    t.did.append(f"{qid}: overdue pushed")
            retry(t, qid, pv)
    with t.guard("tighten", "overturn rate"):
        t.did += pending.tighten(t.root)
    with t.guard("precedents", "precedents.md"):
        t.did += watch(t.root, t.cfg)


def retry(t, qid, pv) -> None:
    if not (fails := pending.apply_failures(t.evs, qid)):
        return
    if fails <= setting(t.cfg, "supervisor.seat_retries"):
        last = max(_epoch(e["ts"]) for e in t.evs if e["type"] == "pending_apply_failed" and e.get("question") == qid)
        if t.now - last >= setting(t.cfg, "supervisor.gate_retry_min") * 60 * 2 ** (fails - 1):
            t.did.append(f"{qid}: provisional action retried ({pending.apply(t.root, qid)})")
        return
    body = "\n".join([f"{qid}（{pv['id'] if pv else '暂定决定'}）的程序动作重试 {fails - 1} 次仍失败，暂定仍生效",
                      f"查看：foremind decide show {qid}", f"推翻：foremind decide {qid} --overturn"])
    if notify.notify(t.root, t.cfg, f"provisional_apply_failed:{qid}", f"暂定动作失败 {qid}", body, "P1"):
        t.did.append(f"{qid}: provisional action failed, pushed")


def watch(root, cfg) -> list[str]:
    out = []
    for j, p in catalog._precedents(root).items():
        if p.get("superseded_by") or p.get("needs_review") or validate("precedent", p):
            continue
        if bad := [x for x in p["premises"] if not catalog.holds(root, cfg, x)]:
            catalog.mark_review(root, j)
            for x in bad:
                key = sha256_bytes(json.dumps(x, sort_keys=True, ensure_ascii=False).encode())[:16]
                catalog._log(root).append("precedent_needs_review", dedupe_id=f"precedent_needs_review:{j}:{key}",
                                          precedent=j, premise=x)
            out.append(f"{j}: needs review ({len(bad)} premise(s) changed)")
    return out
