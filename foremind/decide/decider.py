"""Settle a delegate's output on a deciding Q-n (DESIGN §8.3, §1.7, §2.6): answer it (by = the role), or escalate it
to the user with the reason. Nothing in the output takes effect unless every check passes; what the answer then does
is the pending.py handler contract (a scope approval goes through decide/scope.py).

`cfgs` {where: config, None when unreadable}: the project's (PROJECT) and each blocked batch's, whose task layer may
take a delegation back (§2.6); the row must belong to the role under every one of them, or the Q-n is escalated.

decider (decision_output): its row still belongs to the decider (cfgs) and was not taken back by the overturn rate
  (pending.tightened step 2); question, category and options (verbatim) are the Q-n's; every precedent cited is in
  precedents.md, fits schema precedent, is neither superseded nor needing review, its review_on is not past and its
  machine-checked premises hold now (catalog.holds on the project config: the phase may not have marked it yet)
  (the escalation names the first that fails and why; precedents.md unreadable, a block not an object included:
  nothing can be checked, escalated); confidence high, or medium with the output and the Q-n both reversible
  (REQ-8). The answer is provisional (§8.4, pending.py; REQ-10) when the output and the Q-n are both reversible, the
  row may be done provisionally under every config and the category is not tightened (REQ-11 step 1: not
  provisional), whatever the confidence or option; else a plain answer. Each answer writes decider_decided {question,
  category, conclusion, confidence, sample}: sample when the first 8 hex digits of sha256(Q-n) / 2^32 <
  decider.audit_ratio (default 0.2; unreadable: every one is sampled).
controller (controller_decision): its row still belongs to the controller (cfgs); item is the Q-n; decision is one option
  verbatim; plan_amend is not written in m2b (escalated); scope_change only with a scope request, choosing its
  approve_option, for its batch and exactly its paths (scope.py widens by the whole request, so a part cannot be
  granted alone); and the option is the recommended one: another is escalated with the controller's reasons
  (REQ-17), so the controller may approve a hit (recommended: 不批准) only through the user.
"""
import json
from datetime import date

from foremind import catalog
from foremind.decide import pending, table
from foremind.defaults import TABLE
from foremind.events import EventLog
from foremind.fsutil import sha256_bytes
from foremind.paths import state_dir
from foremind.schemas import validate

KEY = {"decider": "conclusion", "controller": "decision"}  # the field that marks the role's object in its output
NAME = {"decider": "决策者", "controller": "一次性总控"}
PROJECT = "项目"  # the key of the project config in `cfgs`


def sampled(qid, cfg) -> bool:
    try:
        ratio = float(cfg.get("decider.audit_ratio", TABLE["decider.audit_ratio"]))
    except (TypeError, ValueError):
        ratio = 1.0
    return int(sha256_bytes(qid.encode())[:8], 16) / 2 ** 32 < ratio


def owner(cfgs, category) -> tuple[str | None, str]:
    """(owner, "") of `category`'s row when every config in `cfgs` agrees on it, else (None, why)."""
    first = None
    for where, cfg in cfgs.items():
        try:
            if cfg is None:
                raise ValueError("配置读不了")
            got = table.owner(cfg, category)
        except ValueError as e:  # an unusable preset (hands_off without its list): nothing is delegated
            return None, f"{where}的授权设置读不了：{e}"
        if first and got != first[1]:
            return None, f"#{category} 在{first[0]}的授权设置下归 {first[1]}，在{where}归 {got}"
        first = first or (where, got)
    return first[1], ""


def judge(root, cfgs, rec, req, role, out) -> tuple[int | None, str]:
    """(option, "") to answer with, or (None, why) to escalate."""
    who, why = owner(cfgs, rec["category"])
    if who is None:
        return None, why
    if who != role:
        return None, f"#{rec['category']} 在当前授权设置下归 {who}，不归{NAME[role]}"
    if role == "decider" and pending.tightened(root).get(rec["category"], 0) >= 2:
        return None, f"#{rec['category']} 的委托已按推翻率收回，归用户（foremind decide --restore {rec['category']} 恢复）"
    if role == "decider":
        return _decider(root, rec, req or {}, out, cfgs.get(PROJECT))
    return _controller(root, rec, req or {}, out)


def unusable(p, today) -> str:
    """Why precedent `p` (None: missing) may not be cited on `today` (YYYY-MM-DD), "" when it may."""
    if p is None:
        return "不在 precedents.md"
    if errs := validate("precedent", p):
        return f"不合 schema precedent：{errs[0]}"
    if p.get("superseded_by"):
        return f"已被 {p['superseded_by']} 取代"
    if p["needs_review"]:
        return "需复核"
    return f"复核日期 {p['review_on']} 已过" if p["review_on"] < today else ""


def _decider(root, rec, req, out, cfg):
    if errs := validate("decision_output", out):
        return None, f"输出不合 decision_output：{errs[0]}"
    if (out["question"], out["category"]) != (rec["id"], rec["category"]):
        return None, f"输出的 question/category（{out['question']} #{out['category']}）与待决不一致"
    if out["options"] != rec["options"]:
        return None, "输出的 options 与待决不逐字相同"
    try:
        precs = catalog._precedents(root)
    except (OSError, ValueError) as e:  # r2#4: unchecked is not passed
        return None, f"precedents.md 读不了，援引的判例无从核对：{e}"
    today = date.today().isoformat()
    for j in out["precedents_cited"]:
        if why := unusable(precs.get(j), today):
            return None, f"援引的判例 {j} {why}"
        if bad := [x for x in precs[j]["premises"] if not catalog.holds(root, cfg, x)]:  # r2: not marked yet
            return None, f"援引的判例 {j} 的前提已不成立：{json.dumps(bad[0], ensure_ascii=False)}"
    c = out["confidence"]
    if c == "high" or c == "medium" and out["reversible"] and rec["reversible"]:
        return out["conclusion"], ""
    return None, f"置信度 {c}" + ("且不可撤回" if c == "medium" else "")


def _controller(root, rec, req, out):
    if errs := validate("controller_decision", out):
        return None, f"输出不合 controller_decision：{errs[0]}"
    if out["item"] != rec["id"]:
        return None, f"输出的 item {out['item']!r} 不是 {rec['id']}"
    if out["decision"] not in rec["options"]:
        return None, "输出的 decision 不逐字等于任何一个选项"
    opt = rec["options"].index(out["decision"]) + 1
    why = ""
    if out.get("plan_amend"):
        why = "带 plan_amend：批次头修订要由用户决定（一次性总控的修订暂不落盘）"
    elif sc := out.get("scope_change"):
        paths = (req.get("match") or {}).get("paths") or []
        if req.get("kind") != "scope" or opt != req.get("approve_option"):
            why = "scope_change 只能随批准一个 #8 扩大范围请求一起给出"
        elif sc["batch"] != req.get("batch") or not set(sc["add_owns_paths"]) <= set(paths):
            why = "scope_change 超出了请求的批次或路径"
        elif set(sc["add_owns_paths"]) != set(paths):
            why = "scope_change 只批了请求路径的一部分，程序只能按整个请求扩大"
    if not why and opt != rec["recommended"]:  # REQ-17: going against the recommendation is the user's call
        why = f"选了 {opt}（{out['decision']}），与推荐的 {rec['recommended']} 不同，交用户定"
    if why:  # m2e REQ-5: every escalation of a valid answer carries the controller's reasons
        return None, f"{why}；总控的理由：" + "；".join(out["reasons"])
    return opt, ""


def settle(root, cfgs, rec, role, out) -> str:
    """Answer or escalate deciding Q-n `rec` on the role's output; returns one line for the tick's summary."""
    qid = rec["id"]
    req = pending.request(root, qid)
    opt, why = judge(root, cfgs, rec, req, role, out)
    if opt is None:
        pending.escalate(root, qid, f"{NAME[role]}：{why}")
        return f"{qid}: escalated ({why})"
    pv = (role == "decider" and out["reversible"] and rec["reversible"]  # REQ-10, REQ-11 step 1
          and not pending.tightened(root).get(rec["category"])
          and all(table.row(c, rec["category"]).provisional for c in cfgs.values()))
    result = pending.answer(root, qid, opt, by=role, expect="deciding", provisional=pv)
    # ponytail: a crash between the answer and this event loses the decision's audit record (the Q-n is no longer
    # deciding when the job is harvested again); backfill from pending_answered.by = decider if audits ever miss one
    if role == "decider":
        EventLog(state_dir(root) / "events.jsonl").append(
            "decider_decided", question=qid, category=rec["category"], conclusion=opt, confidence=out["confidence"],
            sample=sampled(qid, cfgs[PROJECT]))
    return f"{qid}: {'provisional' if pv else 'answered'} {opt} by {role} ({result})"
