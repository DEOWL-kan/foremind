"""Pending decisions Q-n (DESIGN §8.2, §8.6, §20 I1 I6 I30).

`.foremind/decisions/Q-<n>.json` holds the whole record (schema `pending`, state and answer included); `Q-<n>.md`
next to it is a rendering for people. The state moves only along state.PENDING: open → answered → applied, and an
unresolved one may go to void. Ids are the highest existing number + 1, taken under the state lock.

Provisional (§8.4, m2b.4): the decider answers a deciding Q-n with provisional=True when its output and the Q-n
are both reversible, its row may be done provisionally and the category is not tightened (decider.settle, REQ-10).
Then `decisions/PV-<n>.json` (schema provisional; batch = blocks[0], deadline now + DEADLINE, revert "回退带
`provisional: PV-<n>` 的提交") is written before the Q-n goes deciding → provisional; apply runs as for an answer and
the state stays provisional, and the lock holders are told to do it in its own commit (`provisional: PV-<n>`) or
behind a switch on a squash-merging repo. The phase supervisor/phases/provisional.py turns one past its PV deadline
overdue (never confirmed by itself). The user confirms or overturns it (conclude); overturning, or voiding it (r1),
removes its exemptions/<Q-n>.json and sends the revert notice (_revert: the lock holders; a batch without one goes
back to changes_requested with it in `## 状态`, or, from a state with no such edge, has it added to `## 状态` in
place; and the user is pushed). A request whose key was so undone before is the user's when asked again (create, r2).
Overturn rate (tighten): per category, the decider's answers (pending_answered by decider, provisional ones included;
written under the lock with the state) since its last delegation_restored, the last WINDOW of them; one counts as
overturned when overturned, voided while provisional, or answered by the user afterwards. With at least MIN_SAMPLE
and more than 20% overturned: step 1 when untightened, else one step more only after a new overturn since the last
step. Step 1: not provisional (the decider answers as REQ-8); step 2: the decider's rows of that category go to
the user (create, and decider.judge escalates).
Loosening is the user's alone (`decide --restore k`, restore).

What a Q-n asks for is the `request` of its `pending_created` event (the schema takes no extra fields):
  kind            hit (hard-block hit) | scope (#8 widening) | manual | catalog
  category, batch, session
  match           {paths?, commands?, tools?}: what was hit or asked for (optional)
  key             the hook's dedupe key, or `decide --new`'s (category, batch, match) hash (commands/decide.py key,
                  REQ-16): while a Q-n created with it is open or answered-but-unapplied, it is reused, so a failed
                  approval does not ask and push again; a provisional or overdue one only while its failed program
                  action is still retried (apply_failures, supervisor.seat_retries) or its exemption is still valid
                  (r2: an expired one asks again, as a new Q-n) (optional)
  approve_option  the option that approves (optional: without it no answer triggers a program action)

Handler contract (apply): when the answer equals request.approve_option, the module HANDLERS[request.kind] (manual
has none) is imported lazily and `apply_answer(root, record: dict, request: dict) -> str` is called, outside the
state lock; it returns one line for people. ImportError: stays answered ("这一类待决还没有程序动作"); the module
raises: stays answered, event pending_apply_failed; otherwise → applied. Approved or not, the lock holders (not
`user`) of the batches in `blocks` get an inbox message "Q-n 已答复：<option>；<result>" from sender `decide`.
Answering an answered Q-n again with the same option retries apply.

Routing (§2.6, §8.3): a Q-n whose row (table.row, the configured preset) belongs to the decider or the controller is
created `deciding`, unless it is a catalog request, and the phase supervisor/phases/decide.py hands it to that one-shot
role once the project's and every blocked batch's config agree on it; it ends answered (by = decider | controller,
expect = deciding) or escalated to the user (escalate). Anything else is the user's.

Events (`question` = the Q-n): pending_created {request}; pending_routed {category, owner, to} (the row's owner is
not the user; to = decider | controller, or user for a rule row or a catalog request); pending_escalated {reason};
pending_answered {answer, by, note, category, provisional?: PV-n} (note: free text, data only, §20 I30);
pending_applied {result}; pending_apply_failed {error}; pending_void {reason, by}; pending_overdue; pending_confirmed
{note}; pending_overturned {by, note}; delegation_tightened {category, step, sample, overturned}; delegation_restored
{category, step, by}. Every Q-n that waits on the user (open, or escalated) is
pushed at P1 (push; a deciding one is not): the run report is not there yet (§20 I52②); the push carries the id, the
options (the approving one marked) and the code only (§8.7). create() does not push: `decide --new` pushes right
after it, a hook starts `foremind decide --notify Q-n` as a detached job (notify_later) and never waits on the
network. With an unreadable
config nothing is pushed and the notify key stays unhandled, so `decide --notify Q-n` can push it later. Wherever
options are shown, the one that triggers a program action is marked (APPROVE_MARK), so an option worded like a
refusal cannot hide an approval.
"""
import importlib
import json
import os
import re
import secrets
import string
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foremind import config, header, inbox, job, lock, notify, state
from foremind.decide import exemption, table
from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock
from foremind.paths import state_dir
from foremind.plan import model
from foremind.schemas import PV_ID, Q_ID, validate
from foremind.supervisor.ready import OPEN_Q

DEADLINE = timedelta(hours=48)  # also a PV's (§8.4; "before a milestone depending on it" is not tracked)
WINDOW, MIN_SAMPLE = 20, 5  # §8.4 overturn rate: the last 20 decider decisions of a category, judged from 5 on
TITLE = "暂定决定已撤回"  # the revert notice's head in `## 状态`
PV_TELL = "先做；相关改动单独提交，提交信息写 `provisional: {pv}`；仓库用 squash 合入时改用开关隔离（§8.4）"
DEFERRED = "已记下，进下一次推送或运行报告"  # a push notify() held or deferred (notify.deferred; REQ-16)
NOTIFY_TIMEOUT_S = 60  # the detached push job; a send is bounded by the notifier's own timeouts
PKG_PARENT = Path(__file__).resolve().parents[2]
CODE_CHARS = string.ascii_uppercase + string.digits
HANDLERS = {"hit": "foremind.decide.exemption", "scope": "foremind.decide.scope", "catalog": "foremind.catalog"}
HIT_OPTIONS = ["批准（本批范围内放行）", "不批准"]
APPROVE_MARK = {"hit": "签发放行凭据", "scope": "扩大本批范围", "catalog": "改编目"}
DELEGATES = ("decider", "controller")  # the one-shot roles a row may be delegated to (§2.6)


def _dir(root):
    return state_dir(root) / "decisions"


def _log(root):
    return EventLog(state_dir(root) / "events.jsonl")


def load(root, qid) -> dict:
    if not isinstance(qid, str) or not re.fullmatch(Q_ID, qid):
        raise ValueError(f"bad pending id {qid!r} (Q-<n>)")
    try:
        rec = json.loads((_dir(root) / f"{qid}.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"没有待决 {qid}") from None
    if errs := validate("pending", rec):
        raise ValueError(f"decisions/{qid}.json 不合 schema：{errs[0]}")
    return rec


def requests(root) -> dict:
    """{Q-n: request} of the pending_created events in the current event file.
    ponytail: the current file only; a Q-n still unresolved when its month is rotated out loses its request (apply
    then says so). Read the archive too once rotation runs."""
    return {e.get("question"): e.get("request") for e in _log(root).iter() if e["type"] == "pending_created"}


def request(root, qid) -> dict | None:
    return requests(root).get(qid)


def mark(req, i) -> str:
    """The note on option `i` when choosing it triggers a program action (APPROVE_MARK), else ""."""
    act = APPROVE_MARK.get((req or {}).get("kind"))
    return f"【选此项即{act}】" if act and (req or {}).get("approve_option") == i else ""


def render(rec, req=None) -> str:
    st = rec.get("state", "open")
    out = [f"# {rec['id']}（{st}）", "", rec["question"], ""]
    for i, o in enumerate(rec["options"], 1):
        out.append(f"{i}. {o}{mark(req, i)}" + ("  ← 推荐" if i == rec["recommended"] else "") +
                   ("  ← 已选" if i == rec.get("answer") else ""))
    out += ["", f"推荐理由：{rec['reason']}",
            f"类别 #{rec['category']} · {'可撤回' if rec['reversible'] else '不可撤回'} · "
            f"挡住：{'、'.join(rec['blocks']) or '无'} · 期限 {rec['deadline']} · 校验码 {rec['code']}"]
    if req:
        out.append(f"请求：{json.dumps(req, ensure_ascii=False)}")
    if st in OPEN_Q:
        out += ["", f"回答：foremind decide {rec['id']} <选项号> [--note 文字]"]
    elif st in ("provisional", "overdue"):
        out += ["", f"暂定决定：foremind decide {rec['id']} --confirm | --overturn [--note 文字]"]
    return "\n".join(out) + "\n"


def _save(root, rec, req):
    if errs := validate("pending", rec):
        raise ValueError("; ".join(errs))
    atomic_write(_dir(root) / f"{rec['id']}.json", json.dumps(rec, ensure_ascii=False, indent=1) + "\n")
    atomic_write(_dir(root) / f"{rec['id']}.md", render(rec, req))


def _undone_keys(root) -> set:
    """Request keys of the Q-ns the user overturned, or voided while provisional: asked again, they are the user's."""
    key, pvs, out = {}, set(), set()
    for e in _log(root).iter():
        t, q = e["type"], e.get("question")
        if t == "pending_created":
            key[q] = (e.get("request") or {}).get("key")
        elif t == "pending_answered" and e.get("provisional"):
            pvs.add(q)
        elif (t == "pending_overturned" or t == "pending_void" and q in pvs) and key.get(q):
            out.add(key[q])
    return out


def _by_key(root, cfg, key) -> dict | None:
    evs = list(_log(root).iter())
    for e in evs:
        if e["type"] == "pending_created" and (e.get("request") or {}).get("key") == key:
            try:
                rec = load(root, e.get("question"))
            except (OSError, ValueError):
                continue
            if (st := rec.get("state", "open")) in (*OPEN_Q, "answered") or st in ("provisional", "overdue") and (
                    _retrying(cfg, evs, rec["id"]) or _exempted(root, rec["id"])):
                return rec
    return None


def apply_failures(evs, qid) -> int:
    """How many applies of Q-n `qid` failed when its last one did (pending_apply_failed after any pending_applied),
    else 0."""
    fails, last = 0, None
    for e in evs:
        if e["type"] in ("pending_applied", "pending_apply_failed") and e.get("question") == qid:
            fails, last = fails + (e["type"] == "pending_apply_failed"), e["type"]
    return fails if last == "pending_apply_failed" else 0


def _retrying(cfg, evs, qid) -> bool:
    """Whether the supervisor still retries Q-n `qid`'s failed program action (phases/provisional.retry)."""
    from foremind.supervisor.tick import setting  # lazy: tick imports this module
    return 0 < apply_failures(evs, qid) <= setting(cfg or {}, "supervisor.seat_retries")


def _exempted(root, qid) -> bool:
    """Whether exemptions/<qid>.json is still valid by its expires_at (schema exemption; exemptions.find)."""
    try:
        ex = json.loads((state_dir(root) / "exemptions" / f"{qid}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return not validate("exemption", ex) and datetime.fromisoformat(ex["expires_at"]) > datetime.now(timezone.utc)


def create(root, cfg, *, question, options, recommended, reason, category, blocks, reversible,
           request) -> tuple[dict, bool]:
    """(record, created): a new Q-n (open, or deciding when its row is delegated), or the unresolved one created with
    the same request `key` (see the module docstring, `key`)."""
    row = table.row(cfg, category)  # an unusable preset (hands_off) fails before anything is written
    # a catalog request (a superseded precedent, a loosened rule) is the user's whatever its row (§20 I32, I63): a
    # delegate must not replace the precedents it cites
    to = row.owner if row.owner in DELEGATES and request.get("kind") != "catalog" else "user"
    if to == "decider" and tightened(root).get(category, 0) >= 2:  # taken back by the overturn rate (§8.4)
        to = "user"
    if to in DELEGATES and request.get("key") and request["key"] in _undone_keys(root):  # r2: not the delegate again
        to = "user"
    with lock.state_lock(root):
        if request.get("key") and (old := _by_key(root, cfg, request["key"])):
            return old, False
        n = 1 + max((int(p.stem[2:]) for p in _dir(root).glob("Q-*.json") if re.fullmatch(Q_ID, p.stem)), default=0)
        rec = {"id": f"Q-{n}", "question": question, "options": list(options), "recommended": recommended,
               "reason": reason, "blocks": list(blocks), "reversible": reversible,
               "deadline": (datetime.now(timezone.utc) + DEADLINE).isoformat(timespec="seconds"),
               "code": "".join(secrets.choice(CODE_CHARS) for _ in range(6)), "category": category,
               "state": "open" if to == "user" else "deciding"}
        _save(root, rec, request)  # file before event: a crash in between leaves no request, never a reused id
        _log(root).append("pending_created", question=rec["id"], request=request)
    if row.owner != "user":
        _log(root).append("pending_routed", question=rec["id"], category=category, owner=row.owner, to=to)
    return rec, True


def push(root, cfg, rec, req) -> bool | None:
    """Push Q-n `rec` once (notify key pending:Q-n) at P1. None when pushed before, no longer unresolved, or with a
    delegate (deciding: pushed if it is escalated)."""
    if rec.get("state", "open") not in (*OPEN_Q, "answered") or rec.get("state") == "deciding":
        return None
    body = "\n".join([rec["id"], *(f"{i}. {o}{mark(req, i)}" for i, o in enumerate(rec["options"], 1)),
                      f"校验码 {rec['code']}"])
    return notify.notify(root, cfg, f"pending:{rec['id']}", f"待决 {rec['id']}", body, "P1")


def notify_later(root, qid) -> str:
    """Start `foremind decide --notify <qid>` as a detached job; returns the job id. A hook must not push itself: an
    unreachable ntfy (DNS is not bounded by the send timeout) can hold it past its timeout, and a timed-out
    PreToolUse hook blocks nothing (§20 I38)."""
    path = os.pathsep.join(filter(None, [str(PKG_PARENT), os.environ.get("PYTHONPATH")]))
    env = {"FOREMIND_SESSION": "", "FOREMIND_BATCH": "", "FOREMIND_ROLE": "", "FOREMIND_PROJECT": str(root),
           "PYTHONPATH": path}  # a job is nobody's session (as supervisor/tick.py start_job)
    return job.start(state_dir(root) / "jobs", [sys.executable, "-P", "-m", "foremind", "decide", "--notify", qid],
                     cwd=root, env=env, timeout_s=NOTIFY_TIMEOUT_S)


def from_hit(root, cfg, *, batch, session, hit, key) -> tuple[str, bool] | None:
    """(Q-n, created) for a hard-block hit without an exemption (hook PreToolUse, guard.Hit). None when approving
    could not release it: no batch (exemptions are per batch), or nothing an exemption may name (a path outside every
    repo). The hook does not call it when the same call is also denied by the writable matrix."""
    match = {hit.kind: list(hit.values)}
    if not batch or not exemption.narrow(match):
        return None
    req = {"kind": "hit", "category": hit.category, "batch": batch, "session": session, "match": match, "key": key,
           "approve_option": 1}
    rec, created = create(root, cfg, question=f"批次 {batch} 请求 #{hit.category}：{hit.values[-1]}", options=HIT_OPTIONS,
                    recommended=2, reason=f"命中硬拦截规则 `{hit.pattern}`；确认本批确实需要再批准",
                    category=hit.category, blocks=[batch], reversible=hit.category not in table.LOCKED, request=req)
    return rec["id"], created


def answer(root, qid, option: int, *, by="user", note=None, expect=None, provisional=False) -> str:
    """Record the answer and apply it; returns apply's result line. With `expect` (a delegate passes deciding) the
    Q-n must be in that state, checked under the lock: one the user answered or voided meanwhile is left alone.
    `provisional`: deciding → provisional with its PV-n instead of answered (§8.4)."""
    to = "provisional" if provisional else "answered"
    with lock.state_lock(root):
        rec = load(root, qid)
        st = rec.get("state", "open")
        if expect and st != expect:
            raise ValueError(f"{qid} 当前是 {st}，不是 {expect}，{by} 不能答复")
        if not 1 <= option <= len(rec["options"]):
            raise ValueError(f"{qid} 只有 1–{len(rec['options'])} 号选项")
        if not (st == "answered" and rec["answer"] == option):  # the same answer again: retry apply
            if not state.can_transition(state.PENDING, st, to):
                raise ValueError(f"{qid} 当前是 {st}" + (f"（已选 {rec['answer']}）" if "answer" in rec else "") +
                                 "，不能再答复")
            pv = _new_pv(root, rec) if provisional else None  # first: a crash leaves an orphan PV, never a PV-less Q-n
            rec.update(state=to, answer=option)
            _save(root, rec, request(root, qid))
            _log(root).append("pending_answered", question=qid, answer=option, by=by, note=note,
                              category=rec["category"], **({"provisional": pv} if pv else {}))
    return apply(root, qid)


def _new_pv(root, rec) -> str:
    """Write decisions/PV-<highest + 1>.json for Q-n `rec` (under the state lock); returns the PV-n."""
    n = 1 + max((int(p.stem[3:]) for p in _dir(root).glob("PV-*.json") if re.fullmatch(PV_ID, p.stem)), default=0)
    pv = {"id": f"PV-{n}", "question": rec["id"], **({"batch": rec["blocks"][0]} if rec["blocks"] else {}),
          "category": rec["category"], "revert": f"回退带 `provisional: PV-{n}` 的提交",
          "deadline": (datetime.now(timezone.utc) + DEADLINE).isoformat(timespec="seconds")}
    if errs := validate("provisional", pv):
        raise ValueError("; ".join(errs))
    atomic_write(_dir(root) / f"{pv['id']}.json", json.dumps(pv, ensure_ascii=False, indent=1) + "\n")
    return pv["id"]


def provisionals(root) -> dict:
    """{Q-n: its PV record} of decisions/PV-<n>.json; an unreadable one, or one failing schema provisional, is left
    out."""
    out = {}
    for p in _dir(root).glob("PV-*.json"):
        try:
            pv = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not validate("provisional", pv):
            out[pv["question"]] = pv
    return out


def apply(root, qid) -> str:
    rec, req = load(root, qid), request(root, qid)
    if (st := rec.get("state", "open")) not in ("answered", "provisional", "overdue"):  # overdue: provisional in effect
        raise ValueError(f"{qid} 当前是 {st}，不是 answered、provisional 或 overdue")
    done, result = True, "无程序动作"
    if req is None:
        done, result = False, "找不到请求记录（pending_created 事件），没有执行程序动作"
    elif rec["answer"] == req.get("approve_option") and req.get("kind") != "manual":
        try:
            mod = importlib.import_module(HANDLERS[req.get("kind")])
        except (ImportError, KeyError):
            done, result = False, "这一类待决还没有程序动作"
        else:
            try:
                result = mod.apply_answer(root, rec, req)
            except Exception as e:  # noqa: BLE001 — any handler failure leaves the answer standing, unapplied
                done, result = False, f"执行失败：{type(e).__name__}: {e}"
                _log(root).append("pending_apply_failed", question=qid, error=result)
    if done:
        with lock.state_lock(root):
            rec = load(root, qid)
            if rec.get("state") == st:  # a concurrent retry may have applied it already
                if st == "answered":  # a provisional one stays so (§8.4)
                    rec["state"] = state.transition(state.PENDING, "answered", "applied")
                    _save(root, rec, req)
                _log(root).append("pending_applied", question=qid, result=result)
    opt = rec["options"][rec["answer"] - 1]
    if st in ("provisional", "overdue"):
        pv = (provisionals(root).get(qid) or {}).get("id", "PV-?")
        _tell(root, rec["blocks"], f"{qid} 暂定（{pv}）：{opt}；{result}。{PV_TELL.format(pv=pv)}")
    else:
        _tell(root, rec["blocks"], f"{qid} 已答复：{opt}；{result}")
    return result


def escalate(root, qid, reason) -> None:
    """deciding -> escalated: the delegate could not settle it, so it goes to the user and is pushed. A failed push
    is left to the supervisor's repush (an escalated Q-n is unresolved and its notify key unhandled)."""
    with lock.state_lock(root):
        rec = load(root, qid)
        st = rec.get("state", "open")
        if not state.can_transition(state.PENDING, st, "escalated"):
            raise ValueError(f"{qid} 当前是 {st}，不能上交")
        rec["state"] = "escalated"
        req = request(root, qid)
        _save(root, rec, req)
        _log(root).append("pending_escalated", question=qid, reason=reason)
    try:
        push(root, config.load(root), rec, req)
    except Exception as e:  # noqa: BLE001 — escalated stands; repush retries
        print(f"foremind decide: {qid} 已上交，推送没发出：{e}", file=sys.stderr)


def void(root, qid, reason, *, by="user") -> str:
    with lock.state_lock(root):
        rec = load(root, qid)
        st = rec.get("state", "open")
        if not state.can_transition(state.PENDING, st, "void"):
            raise ValueError(f"{qid} 当前是 {st}，不能作废")
        if pv := st in ("provisional", "overdue"):  # r1: as an overturn (conclude)
            (state_dir(root) / "exemptions" / f"{qid}.json").unlink(missing_ok=True)
        rec["state"] = "void"
        _save(root, rec, request(root, qid))
        _log(root).append("pending_void", question=qid, reason=reason, by=by)
    if pv:
        return f"{qid} 已作废，放行凭据已撤；{_revert(root, rec, f'已作废（{reason}）')}"
    _tell(root, rec["blocks"], f"{qid} 已作废：{reason}")
    return f"{qid} 已作废"


def overdue(root, qid) -> bool:
    """provisional → overdue (past its PV deadline: still provisional in effect, never confirmed by itself); False
    when it is no longer provisional."""
    with lock.state_lock(root):
        rec = load(root, qid)
        if rec.get("state") != "provisional":
            return False
        rec["state"] = "overdue"
        _save(root, rec, request(root, qid))
        _log(root).append("pending_overdue", question=qid)
    return True


def conclude(root, qid, to, *, note=None) -> str:
    """The user's provisional | overdue → confirmed | overturned. Overturning removes the exemption the Q-n issued
    (before the state moves: a crash leaves it provisional, not overturned with a live exemption), tells the lock
    holders to revert and checks the overturn rate again."""
    with lock.state_lock(root):
        rec = load(root, qid)
        st = rec.get("state", "open")
        if st not in ("provisional", "overdue") or not state.can_transition(state.PENDING, st, to):
            raise ValueError(f"{qid} 当前是 {st}，不是待确认的暂定决定")
        if to == "overturned":
            (state_dir(root) / "exemptions" / f"{qid}.json").unlink(missing_ok=True)
        rec["state"] = to
        _save(root, rec, request(root, qid))
        if to == "overturned":
            _log(root).append("pending_overturned", question=qid, by="user", note=note)
        else:
            _log(root).append("pending_confirmed", question=qid, note=note)
    if to == "confirmed":
        return f"{qid} 已确认"
    return f"{qid} 已推翻，放行凭据已撤；{_revert(root, rec, '被用户推翻')}"


def _revert(root, rec, what) -> str:
    """A provisional Q-n undone (overturned, or voided): the revert notice goes to each blocked batch's lock holder;
    a batch with none (its seat released at approved, awaiting_audit or delivered) goes back to changes_requested
    with the notice as its `## 状态` section, for the supervisor's successor (review.request_changes); from any other
    state the notice is added to its `## 状态` in place (_add_status). Unless every batch had a holder (no batch at all
    included) the user is pushed too (P1, key provisional_revert:Q-n). Then the overturn rate is checked again.
    Returns what was delivered where, for the user."""
    from foremind import review  # lazy: review pulls in the seat and worktree machinery

    pv = provisionals(root).get(rec["id"]) or {}
    text = (f"{rec['id']}（{pv.get('id', '暂定决定')}）{what}：按撤回方法回退：" +
            pv.get("revert", "回退这个暂定决定带来的改动"))
    done, lost = [], not rec["blocks"]
    for b in rec["blocks"]:
        try:
            if (s := lock.holder(root, b)) and s != lock.USER:
                inbox.append(s, text, sender="decide", root=root)
                done.append(f"{b} 的持锁席位已收到回退通知")
                continue
            lost = True
            try:
                review.request_changes(root, b, [text], by="user", reason="provisional_revert", title=TITLE)
                done.append(f"{b} 无持锁席位，已退回 changes_requested、回退通知写进状态区")
            except review.FlowError:  # r3: no edge to changes_requested (already there, running, ...): write it anyway
                done.append(f"{b} 无持锁席位（{_add_status(root, b, text)}），回退通知已写进状态区")
        except Exception as e:  # noqa: BLE001 — the undoing stands; the user is pushed below
            lost = True
            done.append(f"{b} 的回退通知没送到：{e}")
    if lost:
        try:
            sent = notify.notify(root, config.load(root), f"provisional_revert:{rec['id']}",
                                 f"暂定撤回 {rec['id']}", text, "P1")
        except Exception:  # noqa: BLE001 — reported below
            sent = False
        done.append("已推送给用户" if sent else DEFERRED if notify.deferred(root, f"provisional_revert:{rec['id']}")
                    else "推送没发出，请手动通知回退")
    tighten(root)
    return "；".join(done)


def _add_status(root, batch, text) -> str:
    """Add the revert notice `text` under batch's `## 状态` section, kept (it may hold a must-fix list a successor
    has not read), without a transition; returns the batch's state."""
    path = model.batch_path(root, batch)
    with project_lock(root):
        h, body = header.parse(path.read_text(encoding="utf-8"))
        spec = model.spec(body)
        old = body[len(spec):].strip() or "## 状态"
        note = f"{TITLE}（user，{datetime.now(timezone.utc).isoformat(timespec='seconds')}）：\n- {text}"
        atomic_write(path, header.render(h, "\n\n".join(x for x in (spec.rstrip("\n"), old, note + "\n") if x)))
    return h.get("state", "planned")


# ponytail: the current event file only, like requests(); tightening must survive rotation (loosening is the
# user's alone), so read the archive too once rotation runs
def tightened(root) -> dict:
    """{category: step} of the overturn-rate tightenings in force (1 not provisional, 2 the user's)."""
    out = {}
    for e in _log(root).iter():
        if e["type"] == "delegation_tightened":
            out[e.get("category")] = max(out.get(e.get("category"), 0), e.get("step", 2))
        elif e["type"] == "delegation_restored":
            out.pop(e.get("category"), None)
    return out


def tighten(root) -> list[str]:
    """Take the next tightening step for each category over the overturn rate (docstring above); one line each."""
    with lock.state_lock(root):
        cat, win, bad, step, struck, pvs = {}, {}, set(), {}, set(), set()  # struck: a new overturn since the last step
        for e in _log(root).iter():
            t, c, q = e["type"], e.get("category"), e.get("question")
            if t == "pending_answered" and e.get("by") == "decider" and c is not None:  # no category: before m2b.4
                cat[q] = c
                win.setdefault(c, []).append(q)
                if e.get("provisional"):
                    pvs.add(q)
            elif (t == "pending_overturned" or t == "pending_answered" and e.get("by") == "user" or
                  t == "pending_void" and q in pvs) and q in cat:
                bad.add(q)
                struck.add(cat[q])
            elif t == "delegation_tightened":
                step[c] = max(step.get(c, 0), e.get("step", 2))
                struck.discard(c)
            elif t == "delegation_restored":
                step.pop(c, None)
                win[c] = []
                struck.discard(c)
        out = []
        for c, qs in win.items():
            last, cur = qs[-WINDOW:], step.get(c, 0)
            k = sum(q in bad for q in last)
            if cur < 2 and len(last) >= MIN_SAMPLE and 5 * k > len(last) and (not cur or c in struck):  # > 20%
                _log(root).append("delegation_tightened", category=c, step=cur + 1, sample=len(last), overturned=k)
                out.append(f"#{c}: tightened to step {cur + 1} ({k}/{len(last)} overturned)")
        return out


def restore(root, category) -> str:
    """`decide --restore k`, the user's only: lift the overturn-rate tightening of #k."""
    with lock.state_lock(root):
        if not (step := tightened(root).get(category)):
            raise ValueError(f"#{category} 没有按推翻率收紧，无需恢复")
        _log(root).append("delegation_restored", category=category, step=step, by="user")
    return f"#{category} 的委托已恢复（原收紧到第 {step} 级）"


def _tell(root, blocks, text):
    for b in blocks:
        try:
            s = lock.holder(root, b)
            if s and s != lock.USER:
                inbox.append(s, text, sender="decide", root=root)
        except Exception as e:  # noqa: BLE001 — the answer stands; the seat still sees it in `foremind decide show`
            print(f"foremind decide: 收件箱消息没送到批次 {b} 的持锁会话：{e}", file=sys.stderr)


def unresolved(root, *, provisional=False) -> list[dict]:
    """Open and answered-but-unapplied Q-n, with `provisional` also provisional and overdue ones (`decide`'s list,
    REQ-17): most blocked batches first, then the longest waiting (§8.6; every deadline is creation + DEADLINE, so the
    earliest deadline has waited longest). Unreadable files are skipped."""
    out, states = [], (*OPEN_Q, "answered", *(("provisional", "overdue") if provisional else ()))
    for p in _dir(root).glob("Q-*.json"):
        try:
            rec = load(root, p.stem)
        except (OSError, ValueError):
            continue
        if rec.get("state", "open") in states:
            out.append(rec)
    return sorted(out, key=lambda r: (-len(r["blocks"]), datetime.fromisoformat(r["deadline"]), int(r["id"][2:])))
