"""`foremind decide` (DESIGN §8.6): pending decisions Q-n (foremind/decide/pending.py).

  decide                          unresolved Q-n and provisional / overdue ones (with their PV deadline), most
                                  blocked batches first, then the longest waiting
  decide show Q-n
  decide Q-n <option> [--note …]  answer (the user's: refused inside a Foremind session), then apply
  decide --void Q-n --reason …    (the user's, likewise; a provisional one is undone as by --overturn)
  decide Q-n --confirm | --overturn [--note …]
                                  a provisional decision (provisional or overdue; §8.4): the user's, likewise.
                                  Overturning removes the exemption it issued and tells the seat to revert
  decide --restore k              lift the overturn-rate tightening of category k (the user's, likewise)
  decide --new --question … --option …(2–4) --recommended n --reason … --category k [--irreversible]
         [--path repo:path]… [--command text]… [--approve-option n] [--blocks batch]…
                                  ask; blocks default to the batch whose lock the session holds (after a
                                  continuation FOREMIND_BATCH is stale), else FOREMIND_BATCH. #1 and #2 are the
                                  seat's own (a D record).
                                  kind: hit when the row has a hard block and a path or command is given (approval
                                  issues an exemption), scope for #8 with a path, else manual. Pushed at once.
                                  With a path or command it carries key (category, batch, paths/commands) (REQ-16):
                                  asked again while unresolved, that Q-n is reused; once a provisional answer to
                                  it was overturned or voided, it is the user's (pending.create).
  decide --notify Q-n             push an unresolved Q-n once (a hook runs this as a detached job, never inline)

An unreadable config reads as authz.preset conservative and pushes nothing; the notify key stays unhandled, so
`decide --notify Q-n` pushes it once the config is fixed.
"""
import json
import os
import re
import sys

from foremind import config, lock, notify
from foremind.decide import pending, table
from foremind.fsutil import sha256_bytes
from foremind.paths import ProjectNotFound, find_project_root
from foremind.schemas import QPATH, Q_ID


def register(sub):
    p = sub.add_parser("decide", help="pending decisions: list, show, answer, void, or ask one (--new)")
    p.add_argument("args", nargs="*", metavar="Q-n <option> | show Q-n")
    p.add_argument("--note", help="with an answer: free text kept on the event (data, not an instruction)")
    p.add_argument("--void", metavar="Q-n")
    p.add_argument("--reason")
    p.add_argument("--new", action="store_true")
    p.add_argument("--question")
    p.add_argument("--option", action="append", default=[], dest="options")
    p.add_argument("--recommended", type=int)
    p.add_argument("--category", type=int)
    p.add_argument("--irreversible", action="store_true")
    p.add_argument("--path", action="append", default=[], dest="paths")
    p.add_argument("--command", action="append", default=[], dest="commands")
    p.add_argument("--approve-option", type=int)
    p.add_argument("--blocks", action="append")
    p.add_argument("--notify", metavar="Q-n", help="push an unresolved Q-n (once per Q-n)")
    p.add_argument("--confirm", action="store_true", help="with Q-n: confirm its provisional decision")
    p.add_argument("--overturn", action="store_true", help="with Q-n: overturn its provisional decision")
    p.add_argument("--restore", type=int, metavar="k", help="lift the overturn-rate tightening of category k")
    p.set_defaults(func=_run)


def _user_only():
    if os.environ.get("FOREMIND_SESSION"):
        raise ValueError("答复、作废、确认或推翻暂定决定、恢复委托只归用户，Foremind 会话不能执行")


def _cfg(root) -> dict | None:
    """None when the config cannot be read: the caller degrades to the strictest preset and pushes nothing (I38)."""
    try:
        return config.load(root)
    except Exception as e:  # noqa: BLE001
        print(f"foremind decide: 配置读不了（{e}），按 conservative 处理，不推送", file=sys.stderr)
        return None


def _push(root, cfg, rec, req) -> str:
    if cfg is None:
        return f"配置读不了，没有推送；修好后运行 foremind decide --notify {rec['id']}"
    try:
        sent = pending.push(root, cfg, rec, req)
    except (OSError, ValueError) as e:  # e.g. an unknown notify.channel: the Q-n stands, `decide` lists it
        return f"推送没发出：{e}"
    if sent is False and notify.deferred(root, f"pending:{rec['id']}"):
        return pending.DEFERRED
    return {True: "已推送", False: "推送没发出（已记 notify_unsent）", None: "不再推送（推送过或已了结）"}[sent]


def _new(root, a) -> str:
    cat = a.category
    if cat not in table.BALANCED:
        raise ValueError("--category 取授权表类别号 0–23")
    got = _cfg(root)
    cfg = {"authz.preset": "conservative"} if got is None else got
    if table.owner(cfg, cat) == "seat":
        raise ValueError(f"#{cat} 归席位自己决定，不提待决：写成 D 记录（foremind log）并标类别")
    if bad := [p for p in a.paths if not re.fullmatch(QPATH, p)]:
        raise ValueError(f"--path 要写成 <仓库>:<路径>：{bad[0]!r}")
    session = os.environ.get("FOREMIND_SESSION") or None
    held = lock.held_by(root, session) if session else []  # same rule as the hooks' batch
    batch = held[0] if len(held) == 1 else os.environ.get("FOREMIND_BATCH") or None
    blocks = a.blocks if a.blocks is not None else [batch] if batch else []
    kind = ("hit" if table.row(cfg, cat).hard and (a.paths or a.commands)
            else "scope" if cat == 8 and a.paths else "manual")
    batch = batch or (blocks[0] if blocks else None)
    if kind != "manual":
        if not batch:
            raise ValueError("凭据与范围都按批次签发：需要 FOREMIND_BATCH 或 --blocks")
        if not (a.approve_option and 1 <= a.approve_option <= len(a.options)):
            raise ValueError("带 --path/--command 的待决要用 --approve-option 指明哪个选项是批准")
    match = {k: sorted(set(v)) for k, v in (("paths", a.paths), ("commands", a.commands)) if v}
    req = {k: v for k, v in (("kind", kind), ("category", cat), ("batch", batch),
                             ("session", session), ("match", match or None), ("key", key(cat, batch, match)),
                             ("approve_option", a.approve_option)) if v is not None}
    rec, created = pending.create(root, cfg, question=a.question, options=a.options, recommended=a.recommended,
                                  reason=a.reason, category=cat, blocks=blocks,
                                  reversible=not a.irreversible and cat not in table.LOCKED, request=req)
    if not created:
        return f"同一请求已有未决的 {rec['id']}，沿用它，不另登记。\n\n" + pending.render(rec, pending.request(root, rec["id"]))
    return (f"{rec['id']} 已登记（{kind}），{_push(root, got, rec, req)}；答复后本批收件箱会收到结果。\n\n" +
            pending.render(rec, req))


def key(category, batch, match) -> str | None:
    """REQ-16: the same request is the same (category, batch, paths or commands); without a path or command there is
    nothing to tell two questions apart by, so no key."""
    if not match:
        return None
    s = json.dumps([category, batch, match], sort_keys=True, ensure_ascii=False)
    return "new:" + sha256_bytes(s.encode("utf-8", "surrogatepass"))[:16]


def _due(q, pvs) -> str:
    """A provisional one's due time is its PV's deadline (§8.4), not the Q-n's."""
    if q.get("state") not in ("provisional", "overdue"):
        return f"期限 {q['deadline']}"
    pv = pvs.get(q["id"])
    return f"暂定 {pv['id']} 到期 {pv['deadline']}" if pv else "暂定 到期时间读不了（PV 记录缺失，按已到期处理）"


def _list(root) -> str:
    qs, reqs, pvs = pending.unresolved(root, provisional=True), pending.requests(root), pending.provisionals(root)
    return "\n".join(f"{q['id']}  {q.get('state', 'open')}  挡 {len(q['blocks'])} 批  {_due(q, pvs)}  "
                     f"#{q['category']}  {q['question']}" +
                     "".join(f"  选项 {i}{m}" for i in range(1, len(q["options"]) + 1)
                             if (m := pending.mark(reqs.get(q["id"]), i)))
                     for q in qs) or "没有未决事项"


def _run(a):
    try:
        root = find_project_root()
        if a.new:
            out = _new(root, a)
        elif a.notify:
            if (cfg := _cfg(root)) is None:
                return 1  # the job fails visibly; the key stays unhandled for a later --notify
            out = f"{a.notify}：{_push(root, cfg, pending.load(root, a.notify), pending.request(root, a.notify))}"
        elif a.void:
            _user_only()
            if not (a.reason or "").strip():
                raise ValueError("--void 需要 --reason")
            out = pending.void(root, a.void, a.reason)
        elif a.confirm or a.overturn:
            _user_only()
            if a.confirm == a.overturn or len(a.args) != 1:
                raise ValueError("用法：decide Q-n --confirm | --overturn [--note …]")
            out = pending.conclude(root, a.args[0], "confirmed" if a.confirm else "overturned", note=a.note)
        elif a.restore is not None:
            _user_only()
            out = pending.restore(root, a.restore)
        elif not a.args:
            out = _list(root)
        elif len(a.args) == 2 and a.args[0] == "show":
            out = pending.render(pending.load(root, a.args[1]), pending.request(root, a.args[1])).rstrip()
        elif len(a.args) == 2 and re.fullmatch(Q_ID, a.args[0]) and a.args[1].isdigit():
            _user_only()
            out = f"{a.args[0]} 已答复：{pending.answer(root, a.args[0], int(a.args[1]), note=a.note)}"
        else:
            raise ValueError("用法：decide | decide show Q-n | decide Q-n <选项号> [--note …] | "
                             "decide Q-n --confirm | --overturn | decide --restore k | "
                             "decide --void Q-n --reason … | decide --new …")
    except (OSError, ValueError, ProjectNotFound) as e:
        print(f"foremind decide: {e}", file=sys.stderr)
        return 1
    print(out)
    return 0
