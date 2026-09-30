"""Phase: audit (m2b.5, DESIGN §9.2–9.4, REQ-13, REQ-14).

L0 (l0), which tick.run calls first every pass, also under a full block, no model call (checks in
foremind/audit.py): new baselines are recorded (`l0_baseline {path, sha256}`), then a root's first reconciliation
(`l0_root {path}`); if a hard failure is not reported yet, or was reported without a `paused` after it and not
accepted, automatic actions are paused first (tick.pause; the pass ends before acting), then each new one writes
`l0_hard_failure {check, target, fingerprint, detail[, sha256], severity}` (dedupe id l0_hard_failure:<fingerprint>),
with one P0 notice naming no path (§8.7), on the user layer's channel (tick.user_cfg). While the checks raise (l0_error) no pass acts, resumed or not; so
does a pass whose l0 raises, or without this module (tick.run). `foremind resume` after
it accepts those facts; `foremind audit --accept-config` records the changed #22 files as `l0_config_accepted
{files}` and resumes. A (check, target) reported and now passing gets `l0_cleared {check, target}` first: the same
fact again is reported again.

L1: a one-shot auditor (KIND audit, oneshot.py, templates/roles/auditor.md) per trigger not audited yet, dedupe key
(trigger, object, fact fingerprint) = audit.pre_key / audit.decider_key:
  pre_delivery  a batch in awaiting_audit: facts.json (the task), goal.md, the batch header, the latest gate result
                and receipt, the D/F records of its log, a diff stat per repo
  decider       a decider_decided with sample: the Q-n without its code, the decider's output, the facts.json it got
Materials cut to audit.MAX_BYTES. Not while blocked or paused; within supervisor.max_oneshot one-shot jobs in flight
(halved while the oneshot quota is low); at most audit.daily_cap (default 10) started per local day, beyond that one
notice that day and none started. A job that fails, or leaves no findings or an invalid audit_report, is retried once;
the second failure, or an excluded auditor model, holds it (`audit_held {trigger, target, key, why}` + notice).
A report goes to reports/audit-<session>.json with `audit_done {trigger, target, key, report, findings: {severity:
n}}`. Pre-delivery without P0/P1: delivered, then the gate job as tick._gate starts it (delivered: success status,
merge_dev merges; I49, I50); with P0/P1: held, notified at that priority, `foremind audit --release <batch>` releases.
A decider sample's P0/P1 are notified; P2/P3 stay in the report.
"""
import json
import re
from collections import Counter
from datetime import datetime

from foremind import audit, config, oneshot, repos, review
from foremind.decide import pending
from foremind.defaults import TABLE
from foremind.fsutil import atomic_write
from foremind.paths import state_dir
from foremind.supervisor import quota
from foremind.supervisor.tick import fm, pause, paused_path, setting, user_cfg

KIND = "audit"
MAX_FAILS = 2
TASK = {
    "pre_delivery": "交付前审计批次 {x}：以 goal.md 与事实为准，核对该批的结论——完成度、范围、被驳回的审查意见，不重复审代码。"
                    "只输出 findings 列表（JSON 数组），每条带证据；没有偏差输出 []。",
    "decider": "抽查决策者对待决 {x} 的决定：越权、依据不足、把不可撤回当可撤回。材料是待决记录、决策输出与决策者当时拿到的 "
               "decider_facts.json。只输出 findings 列表（JSON 数组），每条带证据；没有偏差输出 []。",
}
NAMES = {"config": "配置文件", "goal": "冻结目标", "batch_log": "批次日志", "event_chain": "事件链", "l0_error": "对账出错",
         "seat_ancestor": "冒用用户"}


def run(t, blocked):
    if blocked or paused_path(t.root).exists() or not due(t):
        return
    t.load()  # as phases/decide: start_reviews wrote this pass's review_started to the log, not to t.intents
    day = _day(t.now)
    started = sum(e["type"] == f"sv_{KIND}" and _day(e.get("at", 0)) == day for e in t.intents.values())
    cap = t.cfg.get("audit.daily_cap")
    cap = cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else TABLE["audit.daily_cap"]
    slots = setting(t.cfg, "supervisor.max_oneshot")
    if t.q("oneshot")["state"] == quota.LOW:  # as tick.start_reviews
        slots = max(1, slots // 2)
    n = len(t.open_intents("review_started")) + sum(  # one-shot roles in flight (reviewers, decider, auditor)
        e["type"].startswith("sv_") and "oneshot_session" in e and d not in t.results for d, e in t.intents.items())
    for trig, target, key, src in due(t):
        with t.guard(KIND, key):
            if tries(t, key) >= MAX_FAILS:  # e.g. the job could not be started twice
                hold(t, trig, target, key, f"作业 {MAX_FAILS} 次没有起来", "审计作业没有起来")
            elif started >= cap:
                t.notify(f"audit_cap:{day}", "今日审计已达上限",
                         f"今天已启动 {started} 次触发型审计（audit.daily_cap = {cap}），其余不再启动，明天继续；"
                         "急需交付时在本机 foremind audit --release <批次> 放行。")
                return
            elif n >= slots or not t.may_call("oneshot"):
                return
            elif start(t, trig, target, key, src):
                n, started = n + 1, started + 1


def _day(ts) -> str:
    return datetime.fromtimestamp(ts).date().isoformat()  # local date


def tries(t, key) -> int:
    return sum(e["type"] == f"sv_{KIND}" and e.get("audit_key") == key for e in t.intents.values())


def due(t) -> list:
    """(trigger, target, key, source) not audited, held or in flight; pre-delivery first."""
    closed = {e.get("key") for e in t.evs if e["type"] in ("audit_done", "audit_held")}
    closed |= {e.get("audit_key") for e in t.open_intents(f"sv_{KIND}")}
    out = [("pre_delivery", b, audit.pre_key(t.evs, b), b) for b in t.order if t.state(b) == "awaiting_audit"]
    out += [("decider", e.get("question"), audit.decider_key(e), e) for e in t.evs
            if e["type"] == "decider_decided" and e.get("sample")]
    return [x for x in out if x[2] not in closed]


def current(t, trig, target, key) -> bool:
    """A pre-delivery audit still stands for the batch: awaiting_audit with the same heads."""
    return trig != "pre_delivery" or (target in t.headers and t.state(target) == "awaiting_audit"
                                      and audit.pre_key(t.evs, target) == key)


# --- L0 --------------------------------------------------------------------------------------------

def l0(t) -> bool:
    """True when this pass must not act: it paused, or the checks could not run (l0_error, every such pass)."""
    roots, new_base, fails, cleared = audit.l0(t.root, t.cfg)
    for check, target in cleared:  # put right: what was accepted of it ends here, the same fact again is new
        t.emit("l0_cleared", check=check, target=target)
    for path, sha in new_base:
        t.emit("l0_baseline", path=path, sha256=sha)
    for path in roots:  # after its baselines: a crash between leaves the root to be reconciled again
        t.emit("l0_root", path=path)
    broken = any(f["check"] == "l0_error" for f in fails)
    if broken:
        t.did.append("no action: L0 could not check")
    seen, again = audit.reported(t.evs), audit.unpaused(t.evs)
    new = [f for f in fails if f["fingerprint"] not in seen]
    shown = new or [f for f in fails if f["fingerprint"] in again]  # or reported by a pass that did not pause
    if not shown:
        return broken
    pause(t.root, True)  # before the reports: a report is never left without its pause
    t.did.append("paused: L0 hard failure")
    for f in new:
        t.emit("l0_hard_failure", dedupe_id=f"l0_hard_failure:{f['fingerprint']}", severity="P0", **f)
        t.did.append(f"L0 {f['check']} {f['target']}: {f['detail']}")
    ids = sorted({f["target"] for f in shown if f["check"] in ("goal", "batch_log")})  # plan and batch ids, no paths
    what = "、".join(f"{NAMES[k]} {n} 处" for k, n in sorted(Counter(f["check"] for f in shown).items()))
    t.notify(f"l0:{audit.fingerprint(*sorted(f['fingerprint'] for f in shown))}", "L0 对账：已暂停全部自动动作",
             f"发现未经批准的变化：{what}" + (f"（{'、'.join(ids)}）" if ids else "") + "。已暂停全部自动动作。"
             + ("对账没能做完，修好之前每轮都不做自动动作。" if broken else "") + "在本机执行 foremind audit 查看；配置变化确认无误后 foremind audit --accept-config，其余核实后 foremind resume。",
             "P0", cfg=user_cfg())  # r9: not the channel of the project files under check
    return True


# --- L1 --------------------------------------------------------------------------------------------

def start(t, trig, target, key, src) -> bool:
    mats = pre_materials(t, target) if trig == "pre_delivery" else decider_materials(t, src)
    try:
        model, effort = oneshot.route(t.cfg, "auditor")
        session, argv, env = oneshot.prepare(t.root, "auditor", t.cfg, audit.fit(mats))
    except ValueError as e:  # its model is excluded: no pass would start it
        hold(t, trig, target, key, f"审计者起不来：{e}", "审计者的模型被排除")
        return False
    return t.start_job(KIND, f"{key}:{tries(t, key) + 1}", argv, env=env,
                       timeout_s=setting(t.cfg, "oneshot.timeout_min") * 60, trigger=trig, target=target, audit_key=key,
                       oneshot_session=session, model=model, effort=effort,
                       **({"batch": target} if trig == "pre_delivery" else {"question": target})) is not None


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return f"（读不了 {path.name}）"


def pre_materials(t, bid) -> dict:
    sd, h = state_dir(t.root), t.headers[bid]
    gate = next((e for e in reversed(t.evs) if e["type"] == "gate_result" and e.get("batch") == bid), None)
    rs = review.receipts(t.root, bid)
    rx = re.compile(rf"^[ \t]*(?:[-*][ \t]+)?{re.escape(bid)}\.[DF][1-9][0-9]*(?![0-9]).*$", re.M)
    recs = [m[0].strip() for m in rx.finditer(_read(sd / "batches" / f"{bid}.log.md"))]
    return {"facts.json": {"task": TASK["pre_delivery"].format(x=bid), "batch": bid, "state": t.state(bid)},
            "goal.md": _read(sd / "plans" / h["plan_id"] / "goal.md"),
            "batch.json": h,
            "gate.json": _read(sd / "batches" / gate["path"]) if gate else "（没有门禁结果）",
            "receipt.json": _read(rs[-1][1]) if rs else "（没有审查回执）",
            "records.md": "\n".join(recs) + "\n" if recs else "（批次日志里没有 D/F 记录）",
            "diffstat.txt": diffstat(t, bid)}


def diffstat(t, bid) -> str:
    """`git diff --stat base head` per repo: heads the batch entered awaiting_audit with, bases of its last review
    request or update (§20 I48)."""
    hd = next((e.get("heads") for e in reversed(t.evs) if e["type"] == "batch_state" and e.get("batch") == bid
               and e.get("state") == "awaiting_audit"), None) or {}
    req = next((e for e in reversed(t.evs) if e["type"] in ("review_requested", "batch_updated")
                and e.get("batch") == bid), None) or {}
    out = []
    for rid, head in hd.items():
        base = (req.get("bases") or {}).get(rid)
        try:
            path = {r.id: r.path for r in repos.load_repos(t.root, t.bcfg(bid) or t.cfg)}[rid]
            out.append(f"## {rid} {base}..{head}\n" + review.git(path, "diff", "--stat", "--no-color", base, head))
        except (KeyError, TypeError, OSError, review.FlowError, config.ConfigError) as e:
            out.append(f"## {rid}：没有 diff 统计（{e}）")
    return "\n".join(out) + "\n" if out else "（没有 heads 记录）\n"


def decider_materials(t, e) -> dict:
    qid = e.get("question")
    try:
        rec = {k: v for k, v in pending.load(t.root, qid).items() if k != "code"}
    except (OSError, ValueError) as x:
        rec = f"（读不了 {qid}：{x}）"
    out, facts = "（找不到决策者的输出）", "（找不到决策者当时的 facts.json）"
    its = [i for i in t.intents.values() if i["type"] == "sv_decide" and i.get("question") == qid
           and i.get("role") == "decider" and (t.result(i["dedupe_id"]) or {}).get("ok")]
    if its:
        facts = _read(state_dir(t.root) / "oneshots" / str(its[-1].get("oneshot_session")) / "facts.json")
        try:
            out = oneshot.output(t.root, t.result(f"sv_job:{its[-1]['dedupe_id']}")["job"], "conclusion")
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return {"facts.json": {"task": TASK["decider"].format(x=qid), "question": qid,
                           "decided": {k: e.get(k) for k in ("category", "conclusion", "confidence")}},
            "pending.json": rec, "decision_output.json": out, "decider_facts.json": facts}


def hold(t, trig, target, key, why, text, priority="P1"):
    """`text` goes out (no paths or model output, §8.7); `why` stays in the event."""
    t.emit("audit_held", dedupe_id=f"audit_held:{key}", trigger=trig, target=target, key=key, why=why)
    if trig == "pre_delivery":
        t.notify(f"audit_held:{key}", f"{target} 交付前审计未放行",
                 f"{target} 停在 awaiting_audit：{text}。在本机执行 foremind audit 查看；"
                 f"确认后 foremind audit --release {target} 放行。", priority)
    else:
        t.notify(f"audit_held:{key}", f"{target} 决策抽查",
                 f"抽查决策者对 {target} 的决定：{text}。在本机执行 foremind audit 查看。", priority)
    t.did.append(f"{target}: audit held ({why})")


def after(t, it, ok, st, jid):
    key, trig, target = it["audit_key"], it["trigger"], it["target"]
    why = f"作业{'超时' if st.get('timed_out') else '失败'}（{st.get('exit_code', st['state'])}）"
    if ok and jid:
        try:
            raw = (state_dir(t.root) / "jobs" / jid / "stdout.log").read_text(encoding="utf-8", errors="replace")
            rep = audit.report(raw, session=it["oneshot_session"], model=it["model"], effort=it["effort"],
                               trigger=trig, target=target)
        except (OSError, ValueError) as e:
            why = f"输出里没有有效报告：{e}"
        else:
            settle(t, it, rep)
            return
    if tries(t, key) >= MAX_FAILS and current(t, trig, target, key):
        hold(t, trig, target, key, f"{MAX_FAILS} 次没有有效报告，最后一次：{why}", f"审计者 {MAX_FAILS} 次没有给出有效报告")
    t.did.append(f"{target}: audit job failed ({why})")


def settle(t, it, rep):
    key, trig, target = it["audit_key"], it["trigger"], it["target"]
    path = audit.report_path(t.root, it["oneshot_session"])
    atomic_write(path, json.dumps(rep, ensure_ascii=False, indent=1) + "\n")
    c = audit.counts(rep)
    t.emit("audit_done", dedupe_id=f"audit_done:{key}", trigger=trig, target=target, key=key, report=path.name,
           findings=c)
    worst = next((s for s in audit.HOLDS if c.get(s)), None)
    what = "、".join(f"{s} {n} 条" for s, n in c.items()) or "没有发现"
    if not current(t, trig, target, key):  # released by the user, or new heads, meanwhile: the report stays
        t.did.append(f"{target}: audit done ({what}), no longer awaiting it")
    elif worst:
        hold(t, trig, target, key, f"发现 {what}", f"发现 {what}", worst)
    elif trig == "pre_delivery":
        review.set_state(t.root, target, "delivered", expect=("awaiting_audit",), audit=path.name)
        t.headers[target]["state"] = "delivered"
        rs = review.receipts(t.root, target)
        n = rs[-1][0] if rs else 0
        t.start_job("gate", f"{target}:r{n}:t{int(t.now)}", fm("gate", target), env={"FOREMIND_ROLE": "supervisor"},
                    timeout_s=t.job_timeout(target, "gate"), batch=target, round=n, state="delivered")
        t.did.append(f"{target}: delivered (audit: {what})")
    else:
        t.did.append(f"{target}: decider sample audited ({what})")
