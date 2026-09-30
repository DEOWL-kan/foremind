"""Development precision and efficiency (m2e REQ-14, DESIGN §11.5): per batch a precision line and an efficiency line,
per plan a cumulative section, the same for the run report and `foremind report --plan`.

collect(root, evs, bids, now) is the one place that reads files (review receipts, decisions, heartbeats and the
transcripts they point at, reviewers' transcripts) and hands them to compute(), pure over the events; lines() writes the
Markdown. What cannot be read is "unknown", an event missing a field is counted as「未计 n 次」: neither is 0.
Durations in hours (one decimal); tokens are input-equivalent tokens (review.PRICE, the one place)."""
import json
import re
import statistics
from collections import Counter
from datetime import datetime

from foremind import controller, heartbeat, review
from foremind.decide.pending import DELEGATES
from foremind.install.settings import _claude_home
from foremind.paths import state_dir
from foremind.plan import model
from foremind.schemas import BATCH_ID

UNKNOWN = "unknown"
TERMINAL = ("merged", "cataloged", "cancelled")
AFTER = ("approved", "awaiting_audit", "delivered")  # a send-back from these is "after approved"
EXTRA = ("blocked", "stuck", "paused", "failed", "updating")  # stays listed only when not 0
STUCK = "sv_say:stuck:"
NOTE = ("外来提示与退回的来源自 m2e.10 合入起才有记录；漏出缺陷只含用 `foremind defect` 记下的（可补记）；"
        "m2d.4 之前的审查没有美元与 token，读不到 transcript 的计入「未计」。")


def _t(ts) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _to(e):
    return "cancelled" if e["type"] == "plan_amended" else e.get("to", e.get("state"))


def _receipts(bev):
    return [e for e in bev if e["type"] == "review_receipt" and "rebound_from" not in e]


def _musts(r) -> tuple[int, int, int]:
    """(the reviewer's must_fix, lowered by the program, withdrawn) of one receipt file."""
    its = [i for i in r.get("issues") or [] if isinstance(i, dict) and "must_fix" in (i.get("severity"), i.get("was"))]
    return (len(its), sum(i.get("was") == "must_fix" and i.get("filtered") not in (None, "withdrawn") for i in its),
            sum(i.get("filtered") == "withdrawn" for i in its))


def group(evs) -> dict:
    """{batch: its events}: the batch field, plan_amended under each batch it dropped, a stuck reminder's sv_say
    result (no batch field) under the batch its dedupe_id names."""
    by = {}
    for e in evs:
        if e["type"] == "plan_amended":
            for b in e.get("dropped") or []:
                by.setdefault(b, []).append(e)
        elif b := e.get("batch"):
            by.setdefault(b, []).append(e)
        elif e["type"] == "sv_say" and str(e.get("dedupe_id", "")).startswith(STUCK):
            by.setdefault(e["dedupe_id"].split(":")[2], []).append(e)
    return by


def _pending(evs) -> dict:
    """{Q-n: [created ts, answered or void ts or None, answered by, routed to a delegate]}."""
    out = {}
    for e in evs:
        q, t = e.get("question"), e["type"]
        if t == "pending_created":
            out.setdefault(q, [None, None, None, False])[0] = _t(e["ts"])
        elif q in out and t in ("pending_answered", "pending_void") and out[q][1] is None:
            out[q][1:3] = _t(e["ts"]), e.get("by") if t == "pending_answered" else None
        elif q in out and t == "pending_routed" and e.get("to") in DELEGATES:
            out[q][3] = True
    return out


def _tokens(vals) -> tuple[float, int, int]:
    """(sum, how many are None, how many)."""
    return sum(v for v in vals if v is not None), sum(v is None for v in vals), len(vals)


def row(bev, now, io, pend) -> dict:
    """One batch's figures from its events (group()), `io` (collect()'s reads for it) and _pending()."""
    rec = _receipts(bev)
    verdicts = [e.get("verdict") for e in rec]
    files = io["receipts"]
    if files != UNKNOWN:
        files = [r for r in files if isinstance(r, dict) and "rebound_from" not in r]
        files = [_musts(r) for r in files] if len(files) == len(rec) else UNKNOWN
    rets = Counter()
    for e in bev:
        if e["type"] == "batch_state" and _to(e) == "changes_requested" and e.get("reason") == "requested":
            # by is user, controller or a session: --request-changes in a session is the controller's only
            who = "user" if e.get("by") == "user" else "controller"
            rets["after" if e.get("prior") in AFTER else "during", who] += 1
    moves = sorted(((_t(e["ts"]), _to(e)) for e in bev if e["type"] in ("batch_state", "reconciled", "plan_amended")),
                   key=lambda m: m[0])
    stay = Counter()
    for (t, s), nxt in zip(moves, moves[1:] + [None]):
        if nxt or s not in TERMINAL:
            stay[s] += (nxt[0] if nxt else now) - t

    def at(s, last=False):
        return next((t for t, x in (reversed(moves) if last else moves) if x == s), None)
    ms = [at("running"), at("review_ready"), at("approved", True), at("delivered", True), at("merged")]
    qs, wait, wait_miss = io["qs"], 0.0, 0
    if qs != UNKNOWN:
        for q in qs:
            if q in pend and pend[q][0] is not None:
                wait += (pend[q][1] or now) - pend[q][0]
            else:
                wait_miss += 1
    n = Counter(e["type"] for e in bev)
    reviews = [e for e in bev if e["type"] in ("review_receipt", "review_failed") and "rebound_from" not in e]
    ab = [e for e in bev if e["type"] == "ab_review"]
    usd = lambda es: _tokens([e["cost_usd"] if review._num(e.get("cost_usd")) else None for e in es])  # noqa: E731
    stuck = Counter(e["dedupe_id"].rsplit(":", 1)[1] for e in bev
                    if e["type"] == "sv_say" and e.get("ok") is True and e["phase"] == "result")
    retries = [e for e in bev if e["type"] == "api_retry_result"]
    return {
        "verdicts": verdicts, "musts": files, "returns": rets,
        "gate_fail": sum(e["type"] == "gate_result" and e.get("verdict") == "fail" for e in bev),
        "defects": Counter(e.get("source") for e in bev if e["type"] == "defect_found"),
        "violations": n["hook_denied"] + n["bounds_violation"],
        "moves": moves, "stay": stay, "ms": ms,
        # a later milestone before the earlier one (sent back after delivered and approved again, not yet delivered;
        # approved and reconciled to merged): that stretch is not reached
        "gaps": [b - a if a is not None and b is not None and b >= a else None for a, b in zip(ms, ms[1:])],
        "wait_user": UNKNOWN if qs == UNKNOWN else (wait, wait_miss),
        "writer": _tokens([review.tokens(io["writer"].get(e.get("session")) or {}) for e in bev
                           if e["type"] == "seat_opened" and "continued_from" not in e]),
        "review_tok": _tokens([review.tokens(e) if review.tokens(e) is not None else
                               review.tokens(io["reviewer"].get(e.get("reviewer_session")) or {}) for e in reviews]),
        "review_usd": usd(reviews), "ab_tok": _tokens([review.tokens(e) for e in ab]), "ab_usd": usd(ab),
        "seats": len({e.get("session") for e in bev if e["type"] == "seat_opened"}), "seat_user": n["seat_user"],
        "successors": sum(e["type"] == "seat_opened" and e.get("successor") is True for e in bev),
        "handoffs": sum(e["type"] == "handoff_accept" and e["phase"] == "result" for e in bev),
        "lock_broken": n["lock_broken"],
        "stuck": {"remind": stuck["remind"], "ask": stuck["ask"], "to_stuck": sum(s == "stuck" for _, s in moves),
                  "pattern": n["stuck_pattern"]},
        "retry": (len(retries), sum(e.get("tries") or 0 for e in retries), sum(e.get("ok") is False for e in retries)),
        "withdrawn": n["review_withdrawn"], "external": n["seat_prompt_external"],
    }


def _med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def plan_sum(pid, rows, budgets, evs, now, pend) -> dict:
    """A plan's cumulative figures over its batches' rows and the events in its span."""
    start = next((_t(e["ts"]) for e in evs if e["type"] == "plan_approved" and e.get("plan") == pid), None)
    done = [r["moves"] for r in rows.values() if r["moves"] and r["moves"][-1][1] in TERMINAL]
    ends = [t for m in done for t, x in m if x in ("merged", "cancelled")]
    end = max(ends) if ends and len(done) == len(rows) else now  # a batch not yet done: the span runs to now
    inside = [] if start is None else [e for e in evs if start <= _t(e["ts"]) <= end]
    merged = {b: r for b, r in rows.items() if r["ms"][4] is not None}
    notify = {e.get("dedupe_id"): e.get("priority") for e in inside if e["type"] == "notify" and e["phase"] == "intent"}
    qs = [e.get("question") for e in inside if e["type"] == "pending_created"]
    answered = [pend[q][1] - pend[q][0] for q in qs if q in pend and pend[q][2] == "user"]
    budget = [float(budgets[b]) for b in merged if _isnum(budgets.get(b))]
    return {
        "span": None if start is None else (start, end), "l0": sum(e["type"] == "l0_hard_failure" for e in inside),
        "merged": len(merged), "budget": (sum(budget), len(merged) - len(budget)),
        "serial": (sum(r["ms"][4] - r["ms"][0] for r in merged.values() if r["ms"][0] is not None),
                   sum(r["ms"][0] is None for r in merged.values())),
        "pending": (len(qs), sum(q in pend and pend[q][3] for q in qs)),
        "answer": (_med(answered), max(answered, default=None)),
        "bother": Counter(notify.get(e.get("dedupe_id")) for e in inside if e["type"] == "notify"
                          and e["phase"] == "result" and e.get("sent") is True and e.get("channel") != "none"),
        "slips": sum(e["type"] == "controller_slip" for e in inside),
        "ctl_handoffs": sum(e["type"] == "controller_handoff" for e in inside),
    }


def _isnum(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def compute(evs, plans, budgets, io, now) -> dict:
    """{"batches": {batch: row}, "plans": {plan: (its batches, plan_sum)}}: `plans` {plan: [batch]}, every batch of
    `io`'s figured; `io` {receipts: {batch: [file] | unknown}, qs: {batch: [Q-n] | unknown}, writer: {session: usage |
    None}, reviewer: {reviewer_session: usage | None}}."""
    by, pend = group(evs), _pending(evs)
    rows = {b: row(by.get(b, []), now, {"receipts": io["receipts"].get(b, UNKNOWN), "qs": io["qs"].get(b, UNKNOWN),
                                        "writer": io["writer"], "reviewer": io["reviewer"]}, pend)
            for b in io["receipts"]}
    return {"batches": rows,
            "plans": {p: (bs, plan_sum(p, {b: rows[b] for b in bs}, budgets, evs, now, pend))
                      for p, bs in plans.items()}}


def _usage_of(path, read):
    try:
        return read(path) if path else None
    except (OSError, ValueError):
        return None


def collect(root, evs, bids, now, read=controller.usage) -> dict:
    """compute() for `bids` and the plans they belong to (all of those plans' batches), the files read here."""
    # ponytail: every call reads whole transcripts of every seat and reviewer (tokens missing) of those plans; the
    # supervisor makes a report only when a run comes to rest, so this is rare; the way out when slow is recording a
    # session's usage in an event when it closes (m2f)
    plans, budgets, failed = {}, {}, {}
    for pid in model.plan_ids(root):
        try:
            batches = model.load(root, pid).batches
        except (ValueError, OSError) as e:
            failed[pid] = str(e)
            continue
        if any(b in batches for b in bids):
            plans[pid] = list(batches)
            budgets.update({b: d.header.get("budget_estimate") for b, d in batches.items()})
    try:
        dec = [json.loads(p.read_text(encoding="utf-8")) for p in (state_dir(root) / "decisions").glob("Q-*.json")]
    except (OSError, ValueError):
        dec = UNKNOWN
    by = group(evs)
    io = {"receipts": {}, "qs": {}, "writer": {}, "reviewer": {}}
    for b in dict.fromkeys([*bids, *(b for bs in plans.values() for b in bs)]):
        try:
            io["receipts"][b] = review.load_receipts(root, b)
        except (review.FlowError, OSError):
            io["receipts"][b] = UNKNOWN
        io["qs"][b] = UNKNOWN if dec == UNKNOWN else [q.get("id") for q in dec if b in (q.get("blocks") or [])]
        for e in by.get(b, []):
            if e["type"] == "seat_opened" and "continued_from" not in e and (s := e.get("session")):
                try:
                    tp = (heartbeat.read(root, s) or {}).get("transcript_path")
                except ValueError:
                    tp = None
                io["writer"][s] = _usage_of(tp, read)
            elif e["type"] in ("review_receipt", "review_failed") and "rebound_from" not in e \
                    and review.tokens(e) is None and isinstance(rs := e.get("reviewer_session"), str):
                found = sorted(_claude_home().glob(f"projects/*/{rs}.jsonl"))
                io["reviewer"][rs] = _usage_of(found[0] if found else None, read)
    m = compute(evs, plans, budgets, io, now)
    for b in bids:  # a batch of no loaded plan: its plan's section is unknown, never left out
        if not any(b in bs for bs in plans.values()):
            m["plans"].setdefault(pid := _plan_of(root, b), failed.get(pid, f"没有计划 {pid}"))
    return m


def _plan_of(root, bid) -> str:
    """The batch header's plan_id; the batch id without its `.<n>` when the header cannot be read."""
    try:
        pid = model.read(model.batch_path(root, bid)).header.get("plan_id") if re.fullmatch(BATCH_ID, bid) else None
    except (ValueError, OSError):
        pid = None
    return pid if isinstance(pid, str) and pid else bid.rpartition(".")[0] or bid


def broken(pid, why) -> str:
    return f"### 计划 {pid} 累计：unknown（{why}）"


# --- Markdown -------------------------------------------------------------------------------------------------------

def _h(s) -> str:
    return "—" if s is None else f"{s / 3600:.1f}h"


def _pct(a, b) -> str:
    return f"{a}/{b}（{a * 100 // b}%）" if b else "—"


def _tok(t, what="次") -> str:
    """A (sum, missing, count) of _tokens(): unknown when all are missing; writers' sessions say「≥」."""
    total, miss, n = t
    if n and miss == n:
        return UNKNOWN + ("" if what == "会话" else f"（未计 {miss} 次）")
    s = f"{round(total):,}"
    if not miss:
        return s
    return f"≥ {s}（{miss} 个会话读不到）" if what == "会话" else f"{s}（未计 {miss} 次）"


def _usd(t) -> str:
    total, miss, n = t
    return UNKNOWN + f"（未计 {miss} 次）" if n and miss == n else f"${total:.2f}" + (f"（未计 {miss} 次）" if miss else "")


def _rets(c, kind) -> str:
    return f"{c[kind, 'controller'] + c[kind, 'user']}（总控 {c[kind, 'controller']}、用户 {c[kind, 'user']}）"


def _defects(c) -> str:
    return f"{sum(c.values())}" + (f"（{'、'.join(f'{k} {v}' for k, v in sorted(c.items(), key=str))}）" if c else "")


def _first(r) -> str:
    v = r["verdicts"]
    if not v:
        return "首轮 —；轮数 —"
    ok = v.index("approved") + 1 if "approved" in v else "—"
    return f"首轮{'通过' if v[0] == 'approved' else '未过'}；轮数 {ok}（回执 {len(v)}）"


def _stays(st) -> str:
    out = [f"写手 {_h(st['running'] + st['changes_requested'])}", f"等审查 {_h(st['review_ready'] + st['in_review'])}",
           f"等开席 {_h(st['ready'])}", f"等合入 {_h(st['delivered'])}"]
    return " · ".join(out + [f"{k} {_h(st[k])}" for k in EXTRA if st[k]])


GAPS = ("开席→提审", "提审→approved", "approved→delivered", "delivered→merged")


def batch_lines(b, r) -> list:
    m = r["musts"]
    musts = UNKNOWN if m == UNKNOWN else " · ".join(f"r{i} {a}（降级 {lo}、撤回 {w}）" for i, (a, lo, w) in
                                                   enumerate(m, 1)) or "—"
    w = r["wait_user"]
    wait = UNKNOWN if w == UNKNOWN else _h(w[0]) + (f"（未计 {w[1]} 次）" if w[1] else "")
    st, (rn, rt, rf) = r["stuck"], r["retry"]
    rets = sum(r["returns"].values())
    return [
        f"- {b} 精度：{_first(r)}；must_fix {musts}；退回 审查中 {_rets(r['returns'], 'during')} · approved 后 "
        f"{_rets(r['returns'], 'after')}；门禁失败 {r['gate_fail']}；漏出缺陷 {_defects(r['defects'])}；"
        f"协议违反 {r['violations']}",
        f"- {b} 效率：{' · '.join(f'{k} {_h(g)}' for k, g in zip(GAPS, r['gaps']))}；{_stays(r['stay'])}；"
        f"等用户 {wait}；写手 token {_tok(r['writer'], '会话')}；审查 {_usd(r['review_usd'])}、token "
        f"{_tok(r['review_tok'])}；席位 {r['seats']}（用户自做 {r['seat_user']}）· 交接 {r['handoffs']} · 破锁 "
        f"{r['lock_broken']}；卡住提醒 remind {st['remind']}、ask {st['ask']}，转 stuck {st['to_stuck']}，模式 "
        f"{st['pattern']}；API 续跑 {rn} 串 {rt} 次、失败 {rf}；总控介入 退回 {rets} · 撤回 {r['withdrawn']} · "
        f"外来提示 {r['external']}",
    ]


def _add(ts) -> tuple:
    return tuple(map(sum, zip(*ts))) if ts else (0, 0, 0)


def _avg(a, b, pct=False) -> str:
    return "—" if not b else f"{a * 100 // b}%" if pct else f"{a / b:.2f}"


def plan_lines(pid, rows, s) -> list:
    rs = list(rows.values())
    got = [r for r in rs if r["verdicts"]]
    to_ok = [r["verdicts"].index("approved") + 1 for r in got if "approved" in r["verdicts"]]
    musts = [x for r in rs if r["musts"] != UNKNOWN for x in r["musts"]]
    mf, lo, wd = (sum(x[i] for x in musts) for i in range(3))
    unread = sum(r["musts"] == UNKNOWN and bool(r["verdicts"]) for r in rs)
    rets, defects = sum((r["returns"] for r in rs), Counter()), sum((r["defects"] for r in rs), Counter())
    span = s["span"]
    long = span[1] - span[0] if span else None
    per = lambda x: f"{x * 86400 / long:.2f}/天" if long else "—"  # noqa: E731
    stays = [r["stay"] for r in rs if r["moves"]]
    med = [f"{k} {_h(_med([sum(st[x] for x in xs) for st in stays]))}" for k, xs in (
        ("写手", ("running", "changes_requested")), ("等审查", ("review_ready", "in_review")), ("等开席", ("ready",)),
        ("等合入", ("delivered",)))]
    med += [f"{k} {_h(_med([st[k] for st in stays if st[k]]))}" for k in EXTRA if any(st[k] for st in stays)]
    gaps = [f"{k} {_h(_med([r['gaps'][i] for r in rs]))}" for i, k in enumerate(GAPS)]
    budget, miss = s["budget"]
    serial, no_open = s["serial"]
    tot = lambda k: _add([r[k] for r in rs])  # noqa: E731
    succ, handoffs = sum(r["successors"] for r in rs), sum(r["handoffs"] for r in rs)
    rn, rt, rf = (sum(r["retry"][i] for r in rs) for i in range(3))
    st = {k: sum(r["stuck"][k] for r in rs) for k in ("remind", "ask", "to_stuck", "pattern")}
    (n_q, routed), (a_med, a_max), bother = s["pending"], s["answer"], s["bother"]
    first = sum(r["verdicts"][0] == "approved" for r in got)
    return [
        f"### 计划 {pid} 累计（{len(rs)} 批）",
        f"- 精度：首轮通过率 {_pct(first, len(got))}；到 approved 平均 {_avg(sum(to_ok), len(to_ok))} 轮；每轮平均 "
        f"must_fix {_avg(mf, len(musts))}，降级占 {_avg(lo, mf, True)}、撤回占 {_avg(wd, mf, True)}"
        + (f"（{unread} 批回执文件读不到）" if unread else "") +
        f"；退回 审查中 {_rets(rets, 'during')} · approved 后 {_rets(rets, 'after')}；门禁失败 "
        f"{sum(r['gate_fail'] for r in rs)}；漏出缺陷 {_defects(defects)}；协议违反 "
        f"{sum(r['violations'] for r in rs)}；L0 硬失败 {s['l0'] if span else UNKNOWN}",
        f"- 效率：跨度 {_h(long) if span else UNKNOWN}；吞吐 {s['merged']} 批合入、{per(s['merged'])}；归一吞吐 "
        f"{per(budget)}" + (f"（{miss} 批无 budget_estimate）" if miss else "") +
        f"；并行对照 串行所需 {_h(serial)}" + (f"（未计 {no_open} 次）" if no_open else "") +
        f"、为跨度的 {_avg(serial, long or 0)} 倍",
        f"- 中位数：{' · '.join(gaps)}；{' · '.join(med)}",
        f"- 花费：写手 token {_tok(tot('writer'), '会话')}、美元 unknown（无事件来源）；审查 {_usd(tot('review_usd'))}、token "
        f"{_tok(tot('review_tok'))}；强度对照 {_usd(tot('ab_usd'))}、token {_tok(tot('ab_tok'))}",
        f"- 席位 {sum(r['seats'] for r in rs)}（用户自做 {sum(r['seat_user'] for r in rs)}）· 交接 {handoffs}、成功率 "
        f"{_pct(handoffs, succ)} · 破锁 {sum(r['lock_broken'] for r in rs)}；卡住提醒 remind {st['remind']}、ask "
        f"{st['ask']}，转 stuck {st['to_stuck']}，模式 {st['pattern']}；API 续跑 {rn} 串 {rt} 次、失败 {rf}",
        f"- 用户投入：待决 {n_q if span else UNKNOWN}（交给代理 {routed}）；答复耗时 中位 {_h(a_med)}、最长 {_h(a_max)}；"
        f"打扰 {sum(bother.values())}" + (f"（{'、'.join(f'{k} {v}' for k, v in sorted(bother.items(), key=str))}）"
                                        if bother else ""),
        f"- 总控介入：退回 {sum(rets.values())} · 撤回 {sum(r['withdrawn'] for r in rs)} · 外来提示 "
        f"{sum(r['external'] for r in rs)} · controller_slip {s['slips']} · controller_handoff {s['ctl_handoffs']}",
    ]


def lines(m, bids) -> list:
    """Markdown: `bids`' two lines each, then each plan's cumulative section (compute()'s order)."""
    out = [x for b in bids if b in m["batches"] for x in batch_lines(b, m["batches"][b])]
    for pid, v in m["plans"].items():
        if isinstance(v, str):  # why the plan could not be read
            out += ["", broken(pid, v)]
        else:
            out += ["", *plan_lines(pid, {b: m["batches"][b] for b in v[0]}, v[1])]
    return out
