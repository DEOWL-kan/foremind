"""Run report (DESIGN §11.3, the scripted part; §11.5 first-pass rate and review rounds): facts(root, since) reads the
facts since the last `report_generated` (`since`: that event, None for the whole log), anything it cannot read being
"unknown", the precision and efficiency section of the batches that moved and their plans (metrics.py, m2e REQ-14)
and the suggested values recomputed from all events (signals.compute, REQ-22); generate() writes them as
Markdown to .foremind/reports/<local YYYYmmdd-HHMMSS>.md and records `report_generated{path, since}` (path relative to .foremind/, since the previous report's ts or None); summary() is the
push: ids and counts only (§8.7), never the suggestions. Calls no model; writes no config."""
import json
import time
from datetime import datetime, timezone

from foremind import catalog, config, defaults, metrics, review, signals
from foremind.decide import pending
from foremind.events import EventLog
from foremind.handoff import history
from foremind.paths import state_dir
from foremind.plan import model
from foremind.supervisor import quota
from foremind.supervisor.ready import OPEN_Q

UNKNOWN = "unknown"
DUE_S = 24 * 3600


def events(root) -> list:
    """Every event, the monthly archives' first (handoff.history)."""
    return list(history(root))


def last(evs) -> dict | None:
    return next((e for e in reversed(evs) if e["type"] == "report_generated"), None)


def after(evs, since) -> list:
    """The events after `since` (an event of evs), all of them when None."""
    if since is None:
        return evs
    i = next((i for i, e in enumerate(evs) if e["id"] == since["id"]), -1)
    return evs[i + 1:]


def _to(e):
    return e.get("to", e.get("state"))  # seat and tick write frm/to, review prior/state


def moves(evs) -> list:
    """The batch state changes in evs, [{batch, to}]: batch_state, reconciled (the tick's catch-up to merged, a batch
    the user merged) and plan_amended's dropped (plan amend cancels with no batch_state)."""
    out = []
    for e in evs:
        if e["type"] in ("batch_state", "reconciled"):
            out.append({"batch": e.get("batch"), "to": _to(e)})
        elif e["type"] == "plan_amended":
            out += [{"batch": b, "to": "cancelled"} for b in e.get("dropped") or []]
    return out


def _awaiting_merge(root) -> list:
    """Delivered batches the supervisor does not merge (not merge_dev), their config read as tick.bcfg does; one whose
    config does not load is listed (the tick takes no action on it either)."""
    out = []
    for pid in model.plan_ids(root):
        plan = model.load(root, pid)
        bound = model.is_bound(root, plan)
        for bid, d in plan.batches.items():
            if d.header.get("state") != "delivered":
                continue
            try:
                cfg = config.load(root, config.task_layer(d.header),
                                  task_user_approved=model.task_config_approved(root, plan, bid, bound=bound))
            except config.ConfigError:
                cfg = None
            level = defaults.TABLE["delivery.level"]
            if cfg is None or not all(review.repo_cfg(cfg, r, "level", "delivery.level", level) == "merge_dev"
                                      for r in d.header.get("repos", [])):
                out.append(bid)
    return sorted(out)


def _decisions(root) -> list:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((state_dir(root) / "decisions").glob("Q-*.json"))]


def _pv(root, now) -> dict:
    pvs, due, over = pending.provisionals(root), [], []
    for q in _decisions(root):
        if q.get("state") == "overdue":
            over.append(q["id"])
        elif q.get("state") == "provisional":
            pv = pvs.get(q["id"])
            if pv is None or datetime.fromisoformat(pv["deadline"]).timestamp() - now <= DUE_S:
                due.append(q["id"])
    return {"due": sorted(due), "overdue": sorted(over)}


def _reviews(evs, new) -> dict:
    """The batches with a receipt in `new` (since the last report), each over all its receipts in order; rebound
    copies (`foremind update`) are no review. Not the round numbers: a failed or timed-out review takes one too
    (review.next_round)."""
    recent = {e.get("batch") for e in new if e["type"] == "review_receipt" and "rebound_from" not in e}
    got = {}
    for e in evs:
        if e["type"] == "review_receipt" and "rebound_from" not in e and e.get("batch") in recent:
            got.setdefault(e.get("batch"), []).append(e.get("verdict"))
    first = sum(v[0] == "approved" for v in got.values())
    return {"first": first, "batches": len(got),
            "avg_rounds": round(sum(map(len, got.values())) / len(got), 2) if got else UNKNOWN}


def _get(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a fact that cannot be read is "unknown", never a failed report
        return UNKNOWN


def _signals(root, evs, cfg) -> list:
    receipts = {b: review.load_receipts(root, b) for b in dict.fromkeys(
        e.get("batch") for e in evs if e["type"] == "review_receipt")}
    ab_files = {}
    for e in evs:  # REQ-1: the report pairs every comparison's must_fix again; one it cannot read keeps the event's
        if e["type"] == "ab_review" and "error" not in e and isinstance(s := e.get("reviewer_session"), str):
            try:
                ab_files[s] = json.loads((state_dir(root) / "reviews" / s / "ab.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    return signals.compute(evs, config.load(root) if cfg is None else cfg, receipts, ab_files)


def facts(root, since, now=None, cfg=None) -> dict:
    """`cfg`: the config the suggestions compare with (config.load(root) when None)."""
    now = time.time() if now is None else now
    evs = _get(lambda: events(root))
    new = UNKNOWN if evs == UNKNOWN else after(evs, since)

    def of(*types):
        return lambda: [{"key": e.get("key"), "title": e.get("title")} for e in new if e["type"] in types]

    return {
        "since": since["ts"] if since else None,
        "changes": _get(lambda: moves(new)),
        "awaiting_merge": _get(lambda: _awaiting_merge(root)),
        "unsent": _get(of("notify_unsent")), "held": _get(of("notify_held")), "deferred": _get(of("notify_deferred")),
        "provisional": _get(lambda: _pv(root, now)),
        "needs_review": _get(lambda: sorted(j for j, p in catalog._precedents(root).items()
                                            if p.get("needs_review") and not p.get("superseded_by"))),
        "open_q": _get(lambda: sum(q.get("state", "open") in OPEN_Q for q in _decisions(root))),
        "quota": _get(lambda: {g: st.get("state") for g, st in sorted(quota.view(root)["groups"].items())
                               if isinstance(st, dict)} or UNKNOWN),
        "reviews": _get(lambda: _reviews(evs, new)),
        "signals": UNKNOWN if evs == UNKNOWN else _get(lambda: _signals(root, evs, cfg)),
        "metrics": UNKNOWN if evs == UNKNOWN else _get(lambda: _metrics(root, evs, new, now)),
    }


def _metrics(root, evs, new, now) -> dict:
    """m2e REQ-14: the batches that moved in `new` and the plans they belong to, over all events."""
    bids = list(dict.fromkeys(c["batch"] for c in moves(new) if c["batch"]))
    return {"bids": bids, "m": metrics.collect(root, evs, bids, now)}


def _n(v):
    return UNKNOWN if v == UNKNOWN else len(v)


def render(f, at, block=()) -> str:
    out = [f"# 运行报告 {at}", "", f"自 {f['since'] or '开始'} 以来。", ""]
    if block:
        out += ["## 全阻塞：全部未完成批次都在等你", *(f"- {x}" for x in block), ""]
    out.append(f"## 批次状态变化：{_n(f['changes'])}")
    if f["changes"] != UNKNOWN:
        out += [f"- {c['batch']} → {c['to']}" for c in f["changes"]]
    out += ["", f"## 等你合入：{_n(f['awaiting_merge'])}"]
    if f["awaiting_merge"] != UNKNOWN:
        out += [f"- {b}" for b in f["awaiting_merge"]]
    out += ["", "## 通知"]
    for k, text in (("unsent", "未发出"), ("held", "P0 被限流"), ("deferred", "P1 改走运行报告")):
        out.append(f"- {text}：{_n(f[k])}")
        if f[k] != UNKNOWN:
            out += [f"  - {x['key']}：{x['title']}" for x in f[k]]
    pv = f["provisional"]
    out += ["", "## 暂定决定",
            f"- 24 小时内到期：{UNKNOWN if pv == UNKNOWN else '、'.join(pv['due']) or '无'}",
            f"- 已 overdue：{UNKNOWN if pv == UNKNOWN else '、'.join(pv['overdue']) or '无'}",
            "", f"## 需复核判例：{UNKNOWN if f['needs_review'] == UNKNOWN else '、'.join(f['needs_review']) or '无'}",
            "", f"## 未决待决：{f['open_q']}", ""]
    q = f["quota"]
    out.append("## 额度：" + (UNKNOWN if q == UNKNOWN else " · ".join(f"{g} {s}" for g, s in q.items())))
    r = f["reviews"]
    if r == UNKNOWN or not r["batches"]:
        rate = UNKNOWN
    else:
        rate = f"{r['first']}/{r['batches']}（{r['first'] * 100 // r['batches']}%）"
    out += ["", "## 审查（本段有回执的批次，按各自全部回执计）", f"- 首轮通过率：{rate}",
            f"- 平均审查轮数：{UNKNOWN if r == UNKNOWN else r['avg_rounds']}", ""]
    m = f.get("metrics", UNKNOWN)
    out += ["## 开发精度与效率" + ("：unknown" if m == UNKNOWN else ""), metrics.NOTE]
    if m != UNKNOWN:
        out += metrics.lines(m["m"], m["bids"])
    out.append("")
    s = f.get("signals", UNKNOWN)
    out += ["## 按实测复算的建议值（全部事件；只是建议，不改配置）" + ("：unknown" if s == UNKNOWN else "")]
    if s != UNKNOWN:
        out += [f"- {x['name']}：当前 {'未设置' if x['current'] is None else x['current']}；样本 {x['n']}；"
                f"{x['stats']}；建议 {x['suggest']}；规则：{x['rule']}" for x in s]
    return "\n".join(out + [""])


def summary(f) -> str:
    """The non-zero counts the user may act on, with ids where short; '' when there are none."""
    parts = []
    if f["awaiting_merge"] not in (UNKNOWN, []):
        parts.append(f"等你合入 {len(f['awaiting_merge'])}（{'、'.join(f['awaiting_merge'])}）")
    for k, text in (("unsent", "未发出"), ("held", "被限流"), ("deferred", "被延后")):
        if f[k] not in (UNKNOWN, []):
            parts.append(f"{text} {len(f[k])}")
    pv = f["provisional"]
    if pv != UNKNOWN and pv["due"] + pv["overdue"]:
        parts.append(f"暂定到期 {'、'.join(pv['due'] + pv['overdue'])}")
    if f["needs_review"] not in (UNKNOWN, []):
        parts.append(f"需复核 {'、'.join(f['needs_review'])}")
    if f["open_q"] not in (UNKNOWN, 0):
        parts.append(f"待决 {f['open_q']}")
    return " · ".join(parts)


def generate(root, since, now=None, block=(), dedupe_id=None, f=None) -> tuple[str, str, dict]:
    """Write the report (of `f`, facts(root, since, now) read already); (path relative to .foremind/, text, facts)."""
    f = facts(root, since, now) if f is None else f
    at = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).astimezone()
    d = state_dir(root) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    stem, n = at.strftime("%Y%m%d-%H%M%S"), 1
    while (p := d / f"{stem}{'' if n == 1 else f'-{n}'}.md").exists():
        n += 1
    text = render(f, at.isoformat(timespec="seconds"), block)
    p.write_text(text, encoding="utf-8")
    rel = str(p.relative_to(state_dir(root)))
    EventLog(state_dir(root) / "events.jsonl").append("report_generated", dedupe_id=dedupe_id, path=rel,
                                                      since=f["since"])
    return rel, text, f
