"""Automatic land (m2f REQ-2): a delivered done batch the user's own land.auto (true) lets the supervisor merge gets
one `foremind land <batch>` job at a time, as the supervisor; land checks REQ-2 items 1-8 itself again. Calls no
model, so it runs under a full block too.

A batch is taken (items 1-10) when auto() finds items 1-8 (commands.land.auto_blockers, the authorization and its
state looked at first so no other batch costs a git call) and 10 (no `auto_land_stopped` on its current heads since
its latest `foremind run`), and no job of this phase runs in the project (item 9). Each ended job is settled once
(`auto_land_outcome`, the land_result it wrote, or the gate rerun's gate_result), by its outcome:
- merged: nothing more; L0 reconcile records merged.
- tests: the first on these heads -> `foremind gate --rerun --no-merge <batch>`; a pass -> land once more; tests again,
  or the rerun not passing -> failed (reason auto_land_failed, `## 状态` with the land's failed commands and output
  directory, and the rerun's failing checks; the report phase's one failed notice tells it), and stopped on these
  heads.
- conflict, notes_unverified, move_failed, refused state / gate / config: stopped on these heads, one P1.
- unregistered_keys, refused dirty / elsewhere / moved / busy: one P1 per (batch, heads, outcome, kind), retried
  every supervisor.gate_retry_min minutes.
- refused auto: no notice, no stop; retried as above once the batch qualifies again.
- error (also a job timed out, lost, or with no land_result): retried as above; supervisor.seat_retries of them on
  these heads stop it, with one P1.
Every count and stop is since the batch's latest `foremind run` (run_requested): that run starts it over.
Tick.announce and Tick.waits leave out a batch auto() takes: no 等你合入 for it.
"""
from foremind import review
from foremind.commands import land as cmd
from foremind.defaults import TABLE
from foremind.supervisor.tick import ERRORS, JOB_SLACK_S, fm, setting

KIND = "land"
STOP = ("conflict", "notes_unverified", "move_failed")  # + refused of STOP_KINDS: stopped, told once
STOP_KINDS = ("state", "gate", "config")
RETRY_KINDS = ("dirty", "elsewhere", "moved", "busy")  # + unregistered_keys: told once, retried without a cap
RUN_AGAIN = "处理后手动 foremind land {b}，或 foremind run {b} 让监督进程再试"


def _since(t, bid) -> int:
    """Index in t.evs just past the batch's latest `foremind run` (0: none)."""
    return next((i + 1 for i in range(len(t.evs) - 1, -1, -1)
                 if t.evs[i]["type"] == "run_requested" and bid in t.evs[i].get("batches", [])), 0)


def _mine(t, bid, hd) -> list:
    """The batch's events on heads hd since its latest `foremind run`."""
    return [e for e in t.evs[_since(t, bid):] if e.get("batch") == bid and e.get("heads") == hd]


def auto(t, bid) -> dict | None:
    """REQ-2 items 1-8 and 10 for `bid`: its current heads when the land phase takes it, else None (also when they
    cannot be read: as without the authorization)."""
    # ponytail: auto_blockers reads the whole event log and runs git per call, three times a pass per delivered batch
    # (announce, waits, run) while land.auto is on; cache per pass if that shows
    if t.state(bid) != "delivered" or not cmd._authorized() or (cfg := t.bcfg(bid)) is None:
        return None
    try:
        hd = cmd._heads(t.root, bid, cfg)
        if not hd or any(e["type"] == "auto_land_stopped" for e in _mine(t, bid, hd)):
            return None
        return None if cmd.auto_blockers(t.root, bid) else hd
    except ERRORS:
        return None


def run(t, blocked):
    if t.open_intents(f"sv_{KIND}"):  # item 9: one land or gate rerun at a time, whichever batch
        return
    for bid in t.order:
        with t.guard("land", bid):
            if (hd := auto(t, bid)) and _start(t, bid, hd):
                return


def _start(t, bid, hd) -> bool:
    """Starts what the batch's last outcome on these heads calls for, if anything. The tests -> rerun -> land chain
    rides on the intents (m2f.4 r2): a gate rerun carries the failed commands and output directory of the tests it
    reruns, a land after a passing rerun (or retrying one) `rerun`; a `foremind run` while a job of it runs does
    not cut it."""
    last = next((e for e in reversed(_mine(t, bid, hd)) if e["type"] == "auto_land_outcome"), None)
    action, more = "land", {}
    if last is None:
        pass
    elif last["action"] == "gate":
        if last["outcome"] != "pass":
            return False  # a rerun not passing has failed the batch
        more = {"rerun": True}
    elif last["outcome"] == "merged":
        return False  # waits for reconcile
    elif _class(last["outcome"], last["kind"]) == "tests":
        if last.get("rerun"):
            return False  # tests again after the rerun: the batch has failed
        action, more = "gate", {"failed": last.get("failed"), "out_dir": last.get("out_dir")}
    else:  # retry, auto, error (a stop keeps auto() off); the attempt may predate the latest `foremind run`
        at = max(e["at"] for e in t.intents.values() if e["type"] == f"sv_{KIND}" and e.get("batch") == bid
                 and e.get("heads") == hd)
        if t.now - at < setting(t.cfg, "supervisor.gate_retry_min") * 60:
            return False
        more = {"rerun": True} if last.get("rerun") else {}
    n = 1 + sum(e["type"] == f"sv_{KIND}" and e.get("batch") == bid and e.get("heads") == hd
                for e in t.intents.values())
    if action == "gate":
        argv, timeout = fm("gate", "--rerun", "--no-merge", bid), t.job_timeout(bid, "gate")
    else:
        cfg, h = t.bcfg(bid) or t.cfg, t.headers[bid]
        extra = cfg.get("land.commands", TABLE["land.commands"])
        n_cmds = len(h.get("accept_commands", [])) + (len(extra) if isinstance(extra, list) else 0)
        argv = fm("land", bid)
        timeout = int(cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"])) * 60 * max(1, n_cmds) \
            + JOB_SLACK_S
    return t.start_job(KIND, f"{bid}:{review.heads_hash(hd)}:{n}", argv, env={"FOREMIND_ROLE": "supervisor"},
                       timeout_s=timeout, batch=bid, heads=hd, action=action, **more) is not None


def _outcome(t, it, st, jid) -> dict:
    """What the ended job came to: a land job's land_result (its own by job id, else the batch's last one after the
    intent; error when timed out, lost or none), a gate rerun's pass or fail on the intent's heads."""
    bid, hd = it["batch"], it["heads"]
    after = t.evs[t.evs.index(it) + 1:]
    if it.get("action") == "gate":
        g = next((e for e in reversed(after) if e["type"] == "gate_result" and e.get("batch") == bid
                  and e.get("heads") == hd), None)
        ok = bool(g) and g.get("verdict") == "pass"
        return {"outcome": "pass" if ok else "fail", "kind": None, "failing": None if ok else (g or {}).get("failing"),
                "out_dir": str(t.sd / "jobs" / jid) if jid else None,
                "reason": None if ok else f"门禁重跑{'没有结果' if g is None else '没有通过'}（作业 {st['state']}）"}
    r = None
    if st["state"] != "lost" and not st.get("timed_out"):
        lands = [e for e in after if e["type"] == "land_result"]
        r = next((e for e in lands if jid and e.get("job") == jid), None) or \
            next((e for e in reversed(lands) if e.get("batch") == bid), None)
    if r is None:
        why = "超时" if st.get("timed_out") else "丢失" if st["state"] == "lost" else "没写 land_result"
        return {"outcome": "error", "kind": None, "failed": None, "out_dir": None,
                "reason": f"合入作业{why}（{st.get('exit_code', st['state'])}）", "land_result": None}
    return {**{k: r.get(k) for k in ("outcome", "kind", "failed", "out_dir", "reason")}, "land_result": r["id"]}


def _class(o, k) -> str:
    """merged, tests, stop, retry, auto or error (also what land never writes) for a land_result's outcome, kind."""
    if o in ("merged", "tests"):
        return o
    if o in STOP or (o == "refused" and k in STOP_KINDS):
        return "stop"
    if o == "unregistered_keys" or (o == "refused" and k in RETRY_KINDS):
        return "retry"
    return "auto" if o == "refused" and k == "auto" else "error"


def after(t, it, ok, st, jid):
    bid, hd, action = it["batch"], it["heads"], it.get("action", "land")
    out = _outcome(t, it, st, jid)
    t.emit("auto_land_outcome", dedupe_id=f"auto_land_outcome:{it['dedupe_id']}", batch=bid, heads=hd, action=action,
           job=jid, rerun=bool(it.get("rerun")), **out)
    o, k = out["outcome"], out["kind"]
    if action == "gate":
        if o != "pass":  # the commands are those of the tests it reran (its intent)
            _fail(t, it, {"failed": it.get("failed"), "out_dir": it.get("out_dir")}, out)
        return
    c = _class(o, k)
    errors = sum(e["type"] == "auto_land_outcome" and e["action"] == "land" and _class(e["outcome"], e["kind"]) == "error"
                 for e in _mine(t, bid, hd))
    if c == "tests" and it.get("rerun"):
        _fail(t, it, out)
    elif c == "stop" or (c == "error" and errors >= setting(t.cfg, "supervisor.seat_retries")):
        _stop(t, it, out)
    elif c == "retry":
        mins = setting(t.cfg, "supervisor.gate_retry_min")
        t.notify(f"auto_land:{bid}:{review.heads_hash(hd)}:{o}:{k}", f"{bid} 自动合入受阻",
                 f"{bid} 自动合入没成：{o}" + (f"（{k}）" if k else "") + f"；{out['reason']}\n"
                 f"监督进程每 {mins} 分钟再试，处理后它自己合入；也可手动 foremind land {bid}。")


def _stop(t, it, out):
    """Item 10's record for the job's heads, and its one P1 (per stop: a `foremind run` starts over, m2f.4 D2)."""
    bid, hd, o, k = it["batch"], it["heads"], out["outcome"], out["kind"]
    i = _since(t, bid)
    t.emit("auto_land_stopped", dedupe_id=f"auto_land_stopped:{it['dedupe_id']}", batch=bid, heads=hd, outcome=o,
           kind=k, reason=out["reason"])
    t.notify(f"auto_land:{bid}:{review.heads_hash(hd)}:{o}:{k}:{t.evs[i - 1]['id'] if i else '-'}",
             f"{bid} 自动合入未完成",
             f"{bid} 自动合入未完成：{o}" + (f"（{k}）" if k else "") + f"；{out['reason']}\n{RUN_AGAIN.format(b=bid)}。")


def _fail(t, it, land, rerun=None):
    """tests twice on these heads, or the rerun after the first did not pass (`rerun`, its outcome): failed with the
    failed commands and output directory of the land's tests outcome `land`; the report phase tells it. Stopped on
    these heads too (m2f.4 r1): a batch that is not delivered here is not failed, and must not be taken again on
    the same heads without a `foremind run`."""
    bid, hd = it["batch"], it["heads"]
    t.emit("auto_land_stopped", dedupe_id=f"auto_land_stopped:{it['dedupe_id']}", batch=bid, heads=hd,
           outcome=(rerun or land).get("outcome"), kind=None, reason=(rerun or land).get("reason"))
    if t.state(bid) != "delivered":
        return
    cmds = land.get("failed") or []
    status = "\n".join([f"自动合入的测试{'没过，门禁重跑也没通过' if rerun else '两次没过'}（auto_land_failed）。",
                        "失败的命令：", *(f"- {c}" for c in cmds or ["（无）"]), f"输出目录：{land.get('out_dir')}",
                        *([f"门禁重跑没通过的检查：{'、'.join(rerun.get('failing') or ['（没有结果）'])}；"
                           f"输出 {rerun.get('out_dir')}"] if rerun else []),
                        f"处理后用 foremind run {bid} 重试。"])
    review.set_state(t.root, bid, "failed", expect=("delivered",), reason="auto_land_failed", status=status,
                     failed=len(cmds), out_dir=land.get("out_dir"), heads=hd)
    t.headers[bid]["state"] = "failed"
