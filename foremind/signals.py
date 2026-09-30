"""REQ-22 (DESIGN §11.3): suggested values recomputed from what was measured, for the run report only. Pure: the
events (handoff.history, the monthly archives too), the current config (flat keys) and the batches' review receipts
in, items out; nothing is written, no config, decision or batch state. A sample with a field missing or null is not
counted; below an item's threshold it says 样本不足（n/门槛）. Quality signals only tighten; a looser value is written
as 可考虑放宽 with its grounds, for the user to decide. The rules are the initial ones of the m2d.10 handoff."""
import math
import statistics

from foremind import controller, defaults, hooks, review, schemas

PRICE = review.PRICE  # token prices relative to input: token equivalents, no dollars (one place, REQ-2)
LINE_MIN, LINE_MAX = 150_000, 400_000  # the controller's suggested lines
HANDOFFS_MIN, SLIPS_MIN, SLIP_PCT = 10, 5, 20
AB_MIN, RECALL_MIN = 10, 0.9
RUN_MIN, RUN_PCT, RUN_X = 20, 95, 1.5
BATCH_MIN, BATCH_PCT, BATCH_X = 10, 90, 1.2
LATE_ROUND, LATE_MIN, LATE_OK = 3, 20, 0.3
REMINDS_MIN, RETRIES_MIN, RETRY_OK = 10, 10, 0.95


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def pct(xs, p) -> float:
    """The p-th percentile (inclusive method); xs has at least two values."""
    return statistics.quantiles(xs, n=100, method="inclusive")[p - 1]


def _10k(x, floor=False) -> int:
    return int((math.floor if floor else round)(x / 10_000) * 10_000)


def _short(n, need) -> str:
    return f"样本不足（{n}/{need}）"


def _item(name, current, n, stats, suggest, rule) -> dict:
    return {"name": name, "current": current, "n": n, "stats": stats, "suggest": suggest, "rule": rule}


def _diff(new, cur) -> str:
    return f"{new}（{'+' if new - cur >= 0 else ''}{int(new - cur)}）"


def priced(e) -> float | None:
    """The six usage fields of `e` as input-token equivalents; None when one is missing."""
    if not all(_num(e.get(k)) for k in controller.USAGE_KEYS):
        return None
    return sum(e[k] * w for k, w in PRICE.items())


def knee(takeovers, handoffs) -> dict | None:
    """The cost knee c* = c0 + 10·H/K, rounded to 10 000: c0, H the medians of the successors' context and priced
    cost of taking over, K half the predecessors' median calls (each call past c0 costs ~0.1 token per context token
    more than a fresh session's, so K calls at c* repay H). None when K is 0."""
    c0 = statistics.median(e["context_tokens"] for e in takeovers)
    h = statistics.median(priced(e) for e in takeovers)
    k = statistics.median(e["calls"] for e in handoffs) / 2
    return {"c0": int(c0), "H": int(h), "K": k, "c": _10k(c0 + 10 * h / k)} if k else None


def _usage(evs, type) -> list:
    return [e for e in evs if e["type"] == type and e["phase"] == "result" and priced(e) is not None]


def _calls(evs, type) -> list:
    return [e for e in evs if e["type"] == type and e["phase"] == "result" and _num(e.get("calls"))]


def _knee_item(name, current, hard, takeovers, handoffs, extra="", clamp=True) -> dict:
    """The soft line at the knee; flagged when it is not below the current hard line `hard` (r1 note)."""
    n = min(len(takeovers), len(handoffs))
    rule = "c* = c0 + 10·H/K，取整到 1 万（c0、H：接手时上下文与按价格比折算花费的中位数；K：交接时调用数中位数 ÷ 2）"
    rule += f"；限 {LINE_MIN}–{LINE_MAX}" if clamp else ""
    stats = f"接手 {len(takeovers)}、交接 {len(handoffs)}{extra}"
    if n < HANDOFFS_MIN:
        return _item(name, current, n, stats, _short(n, HANDOFFS_MIN), rule)
    k = knee(takeovers, handoffs)
    if k is None:
        return _item(name, current, n, stats + "；交接调用数中位数为 0", "维持", rule)
    c = min(max(k["c"], LINE_MIN), LINE_MAX) if clamp else k["c"]
    return _item(name, current, n, stats + f"；c0 {k['c0']}，H {k['H']}，K {k['K']:g}，c* {k['c']}",
                 _diff(c, current) + (f"，不低于当前硬线 {hard}，需一并调硬线" if c >= hard else ""), rule)


def controller_lines(evs, cfg) -> list:
    soft, hard = map(int, controller.budget(cfg, cfg.get("routes.controller_live.model") or controller.MODEL))
    out = [_knee_item("总控软线", soft, hard, _usage(evs, "controller_takeover"), _calls(evs, "controller_handoff"))]
    slips = sorted(e["context_tokens"] for e in evs if e["type"] == "controller_slip" and _num(e.get("context_tokens")))
    rule = (f"带读数的失误满 {SLIPS_MIN} 条时取 min(当前硬线, 失误上下文第 {SLIP_PCT} 百分位向下取整到 1 万并限 "
            f"{LINE_MIN}–{LINE_MAX})；只收紧，从不高于当前硬线")
    if len(slips) < SLIPS_MIN:
        sug = f"维持（{_short(len(slips), SLIPS_MIN)}）"
    else:  # the range bounds the slips' value, never the current line: a hard line below it is kept (r1)
        sug = _diff(min(hard, min(max(_10k(pct(slips, SLIP_PCT), floor=True), LINE_MIN), LINE_MAX)), hard)
    stats = f"失误 {len(slips)} 条" + (f"，最低 {slips[0]}，第 {SLIP_PCT} 百分位 {int(pct(slips, SLIP_PCT))}"
                                     if len(slips) >= 2 else "")
    return out + [_item("总控硬线", hard, len(slips), stats, sug, rule)]


def _dirty(evs, type) -> str:
    xs = [e["dirty"] for e in evs if e["type"] == type and _num(e.get("dirty"))]
    return f"{sum(x > 0 for x in xs)}/{len(xs)}" if xs else "无读数"


def seat_lines(evs, cfg) -> list:
    """Seat lines as the latest seat handoff_requested applied them (REQ-10 records soft and hard: the seat's model and
    window included); none recorded, hooks.budget's with no model and window. The knee as the controller's, by
    handoff_accept (successor) and handoff_written (predecessor)."""
    seat = [e for e in evs if e["type"] != "handoff_requested" or e.get("role") == "seat"]  # a planner's are its own
    reqs = [e for e in seat if e["type"] == "handoff_requested" and _num(e.get("context_tokens"))]
    last = next((e for e in reversed(reqs) if _num(e.get("soft")) and _num(e.get("hard"))), None)
    soft, hard = (last["soft"], last["hard"]) if last else map(int, hooks.budget(cfg, "seat", None))
    extra = f"；有未提交改动的占比：交接请求 {_dirty(seat, 'handoff_requested')}，写交接 {_dirty(evs, 'handoff_written')}"
    it = _knee_item("席位软线", soft, hard, _usage(evs, "handoff_accept"), _calls(evs, "handoff_written"), extra,
                    clamp=False)
    written = [e for e in evs if e["type"] == "handoff_written" and isinstance(e.get("requested"), bool)]
    stats = (f"到交接点的请求 {len(reqs)} 次" +
             (f"，请求时上下文中位数 {int(statistics.median(e['context_tokens'] for e in reqs))}" if reqs else "") +
             f"；写交接 {len(written)} 次，其中因请求而写 {sum(e['requested'] for e in written)}")
    return [it, _item("席位交接点", hard, len(reqs), stats, "维持", "只按失误收紧；席位没有失误记录，只报读数")]


# --- reviews -----------------------------------------------------------------

def _receipts(evs) -> dict:
    """{batch: [review_receipt events]} in order; rebound copies (`foremind update`) are no review."""
    out = {}
    for e in evs:
        if e["type"] == "review_receipt" and "rebound_from" not in e:
            out.setdefault(e.get("batch"), []).append(e)
    return out


def batches(evs) -> dict:
    """{batch: {key: (difficulty, security, effort), rounds: to the first approved or None, usd: review dollars up to
    it (all when never approved), costs: runs with one, tokens / counts: the same in input-equivalent tokens (review.
    tokens)}}; key from the review that approved it, else the last one."""
    started = {e.get("reviewer_session"): e for e in evs if e["type"] == "review_started" and e["phase"] == "intent"
               and isinstance(e.get("difficulty"), str) and isinstance(e.get("security"), bool)
               and isinstance(e.get("effort"), str)}
    out = {}
    for b, rs in _receipts(evs).items():
        i = next((i for i, r in enumerate(rs) if r.get("verdict") == "approved"), None)
        s = started.get(rs[i if i is not None else -1].get("reviewer_session"))
        if s is None:
            continue
        upto = rs[i]["id"] if i is not None else None
        usd, tok = [], []
        for e in evs:  # ponytail: one pass over the log per batch; index by batch if reports get slow
            if e.get("batch") == b and e["type"] in ("review_receipt", "review_failed") and "rebound_from" not in e:
                usd += [e["cost_usd"]] if _num(e.get("cost_usd")) else []
                tok += [t] if (t := review.tokens(e)) is not None else []
            if e["id"] == upto:
                break
        out[b] = {"key": (s["difficulty"], s["security"], s["effort"]), "rounds": None if i is None else i + 1,
                  "usd": round(sum(usd), 6), "costs": len(usd), "tokens": round(sum(tok), 6), "counts": len(tok)}
    return out


def _effort(cfg, difficulty, security) -> str:
    keys = (["routes.reviewer.effort_security"] if security else []) + \
        [f"routes.reviewer.effort_{difficulty.lower()}", "routes.reviewer.effort"]
    return next((cfg[k] for k in keys if cfg.get(k) is not None), "xhigh")  # review.route's order, no header needed


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _r(x):  # for the text; the rules compare the unrounded values
    return "—" if x is None else round(x, 2)


def _recall(e, receipts, ab_files):
    """(matched, base must_fix) of one ab_review, REQ-1: paired again from its ab.json and base receipt, the event's
    own matched when either is missing (matched None when neither)."""
    a = ab_files.get(e.get("reviewer_session"))
    base = next((r for r in receipts.get(e.get("batch"), []) if r.get("round") == e.get("base_round")), None)
    if isinstance(a, dict) and isinstance(a.get("issues"), list) and base is not None:
        n = sum(i.get("was", i.get("severity")) == "must_fix" for i in base.get("issues", []))
        return review.matched(a["issues"], base.get("issues", []), a.get("heads") or {}), n
    return (e["matched"] if _num(e.get("matched")) else None), e.get("base_must_fix")


def strength(evs, cfg, bs, receipts, ab_files) -> list:
    """One item per difficulty × security, with or without reviews (r1: the item never drops out). Recall counts only
    the comparisons whose base ran at the group's current effort: the arms are measured against that tier (r1).
    `ab_files`: {comparison reviewer_session: its ab.json}."""
    groups = {}
    for b, d in bs.items():
        groups.setdefault(d["key"], []).append(d)
    pair_of = {b: d["key"][:2] for b, d in bs.items()}
    recalls, lost = {}, {}
    for e in evs:
        if e["type"] == "ab_review" and "error" not in e and (p := pair_of.get(e.get("batch"))) \
                and isinstance(e.get("effort"), str) and isinstance(e.get("base_effort"), str):
            m, n = _recall(e, receipts, ab_files)
            if not _num(n) or n <= 0:
                continue
            k = (*p, e["base_effort"])
            if m is None:
                lost[k] = lost.get(k, 0) + 1
            else:
                recalls.setdefault(k, {}).setdefault(e["effort"], []).append(m / n)
    rounds = {k: _mean([d["rounds"] for d in ds if d["rounds"] is not None]) for k, ds in groups.items()}
    rule = (f"非安全组：取每臂满 {AB_MIN} 次、召回（按位置配对的 must_fix ÷ 基准 must_fix，基准为当前档）≥ {RECALL_MIN}、"
            "平均轮数不升的最低一档；安全组维持")
    out = []
    for d_, sec in ((d, s) for d in ("S", "M", "L") for s in (False, True)):
        cur = _effort(cfg, d_, sec)
        rows = []
        for (d2, s2, eff), ds in sorted(groups.items()):
            if (d2, s2) == (d_, sec):
                costs = [d["usd"] for d in ds if d["costs"]]
                rows.append(f"{eff}：{len(ds)} 批，到 approved 平均 {_r(rounds[(d2, s2, eff)])} 轮，"
                            f"每批 ${_r(_mean(costs))}")
        arms = recalls.get((d_, sec, cur), {})
        rows += [f"对照 {eff}（基准 {cur}）：{len(r)} 次，召回 {_r(_mean(r))}" for eff, r in sorted(arms.items())]
        rows += [f"对照（基准 {cur}）无法复算 {n_} 次" for n_ in [lost.get((d_, sec, cur))] if n_]
        n = sum(len(ds) for k, ds in groups.items() if k[:2] == (d_, sec))
        name = f"审查强度（{d_}·{'安全' if sec else '非安全'}）"
        stats = "；".join(rows) or "无审查记录"
        if sec:
            out.append(_item(name, cur, n, stats, "维持", rule))
            continue
        lower = [e for e in schemas.EFFORTS if cur in schemas.EFFORTS and
                 schemas.EFFORTS.index(e) < schemas.EFFORTS.index(cur)]
        full = [e for e in lower if len(arms.get(e, [])) >= AB_MIN]
        base = rounds.get((d_, sec, cur))
        ok = [e for e in full if _mean(arms[e]) >= RECALL_MIN and base is not None
              and (r := rounds.get((d_, sec, e))) is not None and r <= base]
        if ok:
            sug = (f"可考虑放宽到 {ok[0]}（对 {cur} 的召回 {_r(_mean(arms[ok[0]]))}，"
                   f"平均轮数 {_r(rounds[(d_, sec, ok[0])])} ≤ {_r(base)}）")
        elif not full:
            sug = f"维持（对照{_short(max((len(arms.get(e, [])) for e in lower), default=0), AB_MIN)}）"
        else:
            sug = "维持"
        out.append(_item(name, cur, n, stats, sug, rule))
    return out


def dollars(evs, cfg, bs) -> list:
    runs = [e["cost_usd"] for e in evs if e["type"] in ("review_receipt", "review_failed", "ab_review")
            and "rebound_from" not in e and _num(e.get("cost_usd"))]
    cur = cfg.get("review.max_budget_usd")
    rule = f"cost_usd 第 {RUN_PCT} 百分位 × {RUN_X}"
    if len(runs) < RUN_MIN:
        out = [_item("审查单次美元上限", cur, len(runs), f"{len(runs)} 次", _short(len(runs), RUN_MIN), rule)]
    else:
        p = pct(runs, RUN_PCT)
        out = [_item("审查单次美元上限", cur, len(runs), f"第 {RUN_PCT} 百分位 ${round(p, 4)}，最高 ${round(max(runs), 4)}",
                     round(p * RUN_X, 2), rule)]
    for d_ in ("S", "M", "L"):
        xs = [d["usd"] for d in bs.values() if d["key"][0] == d_ and d["rounds"] is not None and d["costs"]]
        key = f"review.cost_cap_usd_{d_.lower()}"
        rule = f"已 approved 批次（到 approved 为止）审查花费第 {BATCH_PCT} 百分位 × {BATCH_X}"
        if len(xs) < BATCH_MIN:
            out.append(_item(f"审查每批美元上限（{d_}）", cfg.get(key), len(xs), f"{len(xs)} 批",
                             _short(len(xs), BATCH_MIN), rule))
        else:
            p = pct(xs, BATCH_PCT)
            out.append(_item(f"审查每批美元上限（{d_}）", cfg.get(key), len(xs),
                             f"第 {BATCH_PCT} 百分位 ${round(p, 4)}，最高 ${round(max(xs), 4)}", round(p * BATCH_X, 2), rule))
    for d_ in ("S", "M", "L"):  # REQ-2: the same rule in input-equivalent tokens
        xs = [d["tokens"] for d in bs.values() if d["key"][0] == d_ and d["rounds"] is not None and d["counts"]]
        name, cur = f"审查每批 token 上限（{d_}）", cfg.get(f"review.cost_cap_tokens_{d_.lower()}")
        rule = f"已 approved 批次（到 approved 为止）审查输入等价 token 第 {BATCH_PCT} 百分位 × {BATCH_X}"
        if len(xs) < BATCH_MIN:
            out.append(_item(name, cur, len(xs), f"{len(xs)} 批", _short(len(xs), BATCH_MIN), rule))
        else:
            p = pct(xs, BATCH_PCT)
            out.append(_item(name, cur, len(xs), f"第 {BATCH_PCT} 百分位 {round(p)}，最高 {round(max(xs))}",
                             round(p * BATCH_X), rule))
    return out


def late_must_fix(evs, receipts) -> tuple[int, int]:
    """(accepted, raised): must_fix of the rounds counted LATE_ROUND or later since the latest approved (review.counted,
    as diagnose) whose next receipt has `resolved` (REQ-16; older receipts have none: no sample); accepted when that
    one lists it resolved and it was never withdrawn. Rebound copies are skipped both ways: their resolved is the
    original's, against the original's predecessor."""
    acc = raised = 0
    for b, rs in receipts.items():
        gone = review.withdrawn(evs, b)
        own = [(i, r) for i, r in enumerate(rs) if "rebound_from" not in r]
        for (i, r), (_, nxt) in zip(own, own[1:]):
            if not isinstance(nxt.get("resolved"), list) or review.counted(rs[:i + 1]) < LATE_ROUND:
                continue
            for it in r.get("issues", []):
                if it.get("severity") == "must_fix":
                    raised += 1
                    fp = it.get("fingerprint")
                    acc += fp in nxt["resolved"] and fp not in gone
    return acc, raised


def rounds_cap(evs, cfg, receipts) -> dict:
    cur = cfg.get("review.max_rounds", defaults.table()["review.max_rounds"])
    acc, n = late_must_fix(evs, receipts)
    rule = (f"第 {LATE_ROUND} 轮起提出的 must_fix 被接受（下一轮 resolved，且未被撤回）的比例 < {LATE_OK:.0%} 时减 1，"
            "最低 2；只收紧")
    stats = f"被接受 {acc}/{n}" + (f"（{acc / n:.0%}）" if n else "")
    if n < LATE_MIN:
        return _item("审查轮次上限", cur, n, stats, _short(n, LATE_MIN), rule)
    return _item("审查轮次上限", cur, n, stats, max(2, cur - 1) if acc / n < LATE_OK and cur > 2 else "维持", rule)


def remind(evs, cfg) -> dict:
    code = defaults.table()["stuck.remind_min"]
    v = cfg.get("stuck.remind_min")
    cur = v if _num(v) and v > 0 else code
    sent = sum(e["type"] == "sv_say" and e["phase"] == "result" and e.get("ok") is True
               and str(e.get("dedupe_id") or "").startswith("sv_say:stuck:") and e["dedupe_id"].endswith(":remind")
               for e in evs)
    # r1 note: an ask's recovery is the ask's doing; the remind interval is judged by what the remind alone brought
    rec = [e.get("stage") for e in evs if e["type"] == "stuck_recovered"]
    ok = rec.count("remind")
    lo, hi = code * 0.5, code * 2
    rule = (f"提醒后即恢复（stuck_recovered 的 stage 为 remind）占比 ≥ 80% 可考虑放宽到 1.5 倍，≤ 20% 收紧到 0.75 倍；"
            f"限 {lo:g}–{hi:g} 分钟，限值不改方向（已越过限值时维持）")
    stats = f"提醒 {sent}，恢复 {len(rec)}，其中提醒后即恢复 {ok}" + (f"（{ok / sent:.0%}）" if sent else "")
    if sent < REMINDS_MIN:
        return _item("卡住提醒间隔（分钟）", cur, sent, stats, _short(sent, REMINDS_MIN), rule)
    r = ok / sent
    if r >= 0.8 and (to := min(cur * 1.5, hi)) > cur:
        sug = f"可考虑放宽到 {to:g}（恢复占比 {r:.0%}）"
    elif r <= 0.2 and (to := max(cur * 0.75, lo)) < cur:
        sug = f"{to:g}"
    else:
        sug = "维持" + (f"（已在 {lo:g}–{hi:g} 之外或限值上）" if r >= 0.8 or r <= 0.2 else "")
    return _item("卡住提醒间隔（分钟）", cur, sent, stats, sug, rule)


def retries(evs, cfg) -> dict:
    v = cfg.get("stuck.api_retry_max")
    cur = v if _num(v) and v >= 0 else defaults.table()["stuck.api_retry_max"]
    ok = sorted(e["tries"] for e in evs if e["type"] == "api_retry_result" and e.get("ok") is True
                and _num(e.get("tries")))
    rule = f"成功续跑里累计占比 ≥ {RETRY_OK:.0%} 的最小次数"
    if len(ok) < RETRIES_MIN:
        return _item("API 续跑次数", cur, len(ok), f"成功 {len(ok)} 串", _short(len(ok), RETRIES_MIN), rule)
    need = ok[math.ceil(RETRY_OK * len(ok)) - 1]
    return _item("API 续跑次数", cur, len(ok), f"成功 {len(ok)} 串，最多 {ok[-1]} 次", need, rule)


def compute(evs, cfg, receipts, ab_files=None) -> list:
    """Every item, in the report's order. `receipts`: {batch: its receipts in round order (review.load_receipts)};
    `ab_files`: {reviewer_session: reviews/<session>/ab.json} of the comparisons it could read."""
    bs = batches(evs)
    return [*controller_lines(evs, cfg), *seat_lines(evs, cfg), *strength(evs, cfg, bs, receipts, ab_files or {}),
            *dollars(evs, cfg, bs), rounds_cap(evs, cfg, receipts), remind(evs, cfg), retries(evs, cfg)]
