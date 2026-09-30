"""Phase: delegated Q-n (m2b.3, DESIGN §8.3, §2.6). Each deciding Q-n without a job in flight gets a one-shot job of
the role its row now belongs to under the project's config and those of every blocked batch and of the batch its
request is for (decider or controller; they disagree, one is unreadable, or anything else: escalated), within
supervisor.max_oneshot one-shot jobs in flight (reviews included, those started this pass too; halved while the oneshot
quota is low). Escalating calls no model, so it is not held back by a full block, the quota or the slots. The
materials are one reproducible facts.json: the Q-n (without its code), its request, its authority row, the precedents
(unreadable: escalated), the blocked batches' state and review rounds, the quota states. decide/decider.py settles the
output; a job that fails or leaves no JSON object is retried once, the second failure escalates; so does a role whose
model is excluded.
"""
from foremind import catalog, oneshot, review
from foremind.decide import decider, pending, table
from foremind.defaults import TABLE
from foremind.supervisor import quota
from foremind.supervisor.tick import setting

KIND = "decide"
MAX_FAILS = 2
TASK = {
    "decider": "处理待决 {q}：只输出一个 decision_output；question 写 {q}，category 与 options 照抄待决（options 逐字）；"
               "只援引 precedents 里没被取代、不需复核的判例。",
    "controller": "处理待决 {q}：只输出一个 controller_decision；item 写 {q}，decision 逐字抄一个选项；scope_change 只在"
                  "批准 #8 扩大范围请求时给出，batch 与 add_owns_paths 照抄请求；plan_amend 不会被执行，写了整条交用户。",
}


def tries(t, qid) -> int:
    return sum(e["type"] == f"sv_{KIND}" and e.get("question") == qid for e in t.intents.values())


def configs(t, rec, req) -> dict:
    """decider.settle's `cfgs`: the project config and each blocked batch's, and that of request.batch, which a
    credential or a widening is for (r2#1) (None: unreadable, or no such batch this pass)."""
    bs = [*rec["blocks"], *([b] if (b := (req or {}).get("batch")) is not None else [])]
    return {decider.PROJECT: t.cfg, **{f"批次 {b}": t.bcfg(b) if b in t.headers else None for b in bs}}


def run(t, blocked):
    if not any(q.get("state") == "deciding" for q in t.decisions):
        return
    t.load()  # r2#2: start_reviews wrote this pass's review_started to the log, not to t.intents
    cap = setting(t.cfg, "supervisor.max_oneshot")
    if t.q("oneshot")["state"] == quota.LOW:  # as tick.start_reviews
        cap = max(1, cap // 2)
    n = len(t.open_intents("review_started")) + len(t.open_intents(f"sv_{KIND}"))
    for q in t.decisions:
        qid = q["id"]
        if q.get("state") != "deciding" or t.open_intents(f"sv_{KIND}", question=qid):
            continue
        with t.guard(KIND, qid):
            rec, req = pending.load(t.root, qid), pending.request(t.root, qid)
            who, why = decider.owner(configs(t, rec, req), rec["category"])
            if tries(t, qid) >= MAX_FAILS:  # e.g. the job could not be started twice
                pending.escalate(t.root, qid, f"一次性作业失败 {MAX_FAILS} 次")
            elif who not in pending.DELEGATES:  # r2#3: before the block, quota and slot checks
                pending.escalate(t.root, qid, why or f"#{rec['category']} 在当前授权设置下归 {who}")
            elif not (blocked or n >= cap or not t.may_call("oneshot")):
                n += start(t, rec, req, who)


def start(t, rec, req, role) -> bool:
    qid = rec["id"]
    try:
        precedents = catalog._precedents(t.root)
    except (OSError, ValueError) as e:  # r2#4: every pass would fail the same way while the Q-n blocks
        pending.escalate(t.root, qid, f"precedents.md 读不了：{e}")
        return False
    row = table.row(t.cfg, rec["category"])
    facts = {
        "task": TASK[role].format(q=qid),
        "pending": {k: v for k, v in rec.items() if k != "code"},  # the code proves an answer came from the user
        "request": req,
        "authz_row": {"category": rec["category"], "preset": t.cfg.get("authz.preset", TABLE["authz.preset"]),
                      **row._asdict()},
        "precedents": precedents,
        "batches": {b: {"state": t.state(b), "review_rounds": review.next_round(t.evs, b) - 1}
                    for b in rec["blocks"] if b in t.headers},
        "quota": {g: t.q(g)["state"] for g in quota.GROUPS},
    }
    try:
        session, argv, env = oneshot.prepare(t.root, role, t.cfg, {"facts.json": facts})
    except ValueError as e:  # its model is excluded: no pass would start it
        pending.escalate(t.root, qid, f"{decider.NAME[role]}起不来：{e}")
        return False
    return t.start_job(KIND, f"{qid}:{tries(t, qid) + 1}", argv, env=env,
                       timeout_s=setting(t.cfg, "oneshot.timeout_min") * 60, question=qid, role=role,
                       oneshot_session=session) is not None


def after(t, it, ok, st, jid):
    qid, role = it["question"], it["role"]
    try:
        rec = pending.load(t.root, qid)
    except ValueError:
        return  # unreadable: the tick reports the file; nothing to settle
    if rec.get("state") != "deciding":
        return  # the user answered or voided it meanwhile
    why = f"作业{'超时' if st.get('timed_out') else '失败'}（{st.get('exit_code', st['state'])}）"
    if ok and jid:
        try:
            out = oneshot.output(t.root, jid, decider.KEY[role])
        except (OSError, ValueError) as e:
            why = f"输出里没有结论：{e}"
        else:
            t.did.append(decider.settle(t.root, configs(t, rec, pending.request(t.root, qid)), rec, role, out))
            return
    if tries(t, qid) >= MAX_FAILS:
        pending.escalate(t.root, qid, f"{decider.NAME[role]}{MAX_FAILS} 次没有给出结论，最后一次：{why}")
    t.did.append(f"{qid}: {role} job failed ({why})")
