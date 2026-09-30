"""`foremind status [--all]` (DESIGN §13.5): batches by state, sessions (heartbeat, busy_tool; a session with exit
evidence is left out and counted), the Foremind processes running now with what they use and the controller's context
reading (REQ-4, m2d.1), open pendings, quota state (the account's, quota.state_path), approved plans edited by hand
since (the supervisor opens nothing on them), and whether `foremind supervise` runs, and on current code
(supervisor.json). REQ-10: per unfinished batch who it waits on, why and what the user runs, from the supervisor's own
judgement (Tick.waits, read only: D1), and when nothing advances by itself. `--all`: every project in the user-level
registry."""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from foremind import config, controller, handoff, heartbeat, inbox, install, review, sessions
from foremind import header as hdr
from foremind.events import EventLog
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.plan import model
from foremind.schemas import BATCH_ID
from foremind.supervisor import quota, ready, stuck, tick
from foremind.supervisor.ready import OPEN_Q
from foremind.supervisor.tick import _exit_evidence, supervisor_state

NONE = "无需操作"


def register(sub):
    p = sub.add_parser("status", help="batches, sessions, pendings and quota of this project")
    p.add_argument("--all", action="store_true", help="every enabled project (~/.config/foremind/projects)")
    p.set_defaults(func=_run)


def _json(p):
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def report(root) -> str:
    sd, out = state_dir(root), [f"项目 {root}"]
    if not sd.is_dir():
        return out[0] + "\n  没有 .foremind/（未 init 或已删除）"
    batches = []
    for p in sorted((sd / "batches").glob("*.md")):
        if re.fullmatch(BATCH_ID, p.stem):
            try:
                h, _ = hdr.parse(p.read_text(encoding="utf-8"))
            except (OSError, hdr.HeaderError):
                h = {"state": "（批次头读不了）"}
            batches.append((p.stem, h))
    counts = Counter(h.get("state", "planned") for _, h in batches)
    out.append("批次：" + (" · ".join(f"{s} {n}" for s, n in sorted(counts.items())) or "无"))
    for bid, h in batches:
        extra = f"（{h['blocked_reason']}）" if h.get("blocked_reason") else ""
        out.append(f"  {bid:<16} {h.get('state', 'planned')}{extra}  {h.get('title', '')}".rstrip())
    st, rec = supervisor_state(root)
    out += _explain(root, st)
    beats = [hb for p in sorted((sd / "heartbeats").glob("*.json")) if (hb := _json(p))]
    try:
        evs = list(handoff.history(root)) if beats else []
    except (OSError, ValueError):  # an unreadable event log hides nothing
        evs = []
    live = [hb for hb in beats if not _exit_evidence(evs, hb.get("session"))]
    closed = len(beats) - len(live)
    out.append(f"会话：{len(live) or '无'}" + (f"（另有 {closed} 个已关闭，未列出）" if closed else ""))
    for hb in live:
        busy = " busy_tool" if hb.get("tool_open") else ""
        out.append(f"  {hb.get('session')}  批次 {hb.get('batch') or '-'}  心跳 {hb.get('ts')}{busy}")
    out += _processes(root) + _controller(root)
    qs = [q for p in (sd / "decisions").glob("Q-*.json") if (q := _json(p))]
    out.append(f"未决：{sum(q.get('state', 'open') in OPEN_Q for q in qs)}")
    groups = quota.view(root)["groups"]
    out.append("额度：" + (" · ".join(f"{g} {st.get('state')}" for g, st in sorted(groups.items())
                                     if isinstance(st, dict)) or "unknown（监督进程还没跑过）"))
    for pid in model.plan_ids(root):  # finding 19: what the supervisor stopped on
        try:
            p = model.load(root, pid)
            unbound = "approved_at" in p.doc.header and p.active() and not model.is_bound(root, p)
        except (OSError, ValueError):  # PlanError too; an unreadable event log hides nothing
            continue
        if unbound:
            out.append(f"计划 {pid} 未绑定，调度已停（批准后被手改；改回原样或 foremind plan amend {pid}）")
    out.append({"stopped": "监督进程未运行", "stale": f"监督进程代码旧于当前包（pid {rec.get('pid')}）",
                "current": f"监督进程运行中（pid {rec.get('pid')}）"}[st])
    return "\n".join(out)


def _processes(root) -> list[str]:
    """REQ-4: this project's Foremind sessions running now, known by their record (sessions.live: the settings path on
    the command line, a controller's bound pid; never a process name), each with its role, batch, pid, %CPU and memory
    of its process tree; `已记关闭仍在跑` when a close was recorded as confirmed (the m2b.4 leak)."""
    try:
        procs = sessions.snapshot()
        running = sessions.live(root, procs)
    except sessions.SessionsError as e:
        return [f"存活的 Foremind 会话：未知（进程读不出：{e}），CPU 未知，内存 未知"]
    try:
        evs = list(handoff.history(root))
    except (OSError, ValueError):
        evs = []
    who = {e.get("session"): ("席位", e.get("batch")) if e["type"] == "seat_launch" else ("规划者", e.get("plan"))
           for e in evs if e["type"] in ("seat_launch", "planner_opened")}
    rows = []
    for s, ps in sorted(running.items()):
        cpu, rss = map(sum, zip(*(sessions.usage(procs, p.pid) for p in ps)))
        role, batch = who.get(s, ("总控", None))  # live() finds settings-path sessions (seats, planners) and controllers
        closed = "  已记关闭仍在跑" if _exit_evidence(evs, s) else ""
        rows.append((cpu, rss, f"  {s}  {role}  批次 {batch or '-'}  pid {','.join(str(p.pid) for p in ps)}  "
                               f"CPU {cpu:.1f}%  内存 {rss / 1024:.0f} MB{closed}"))
    return [f"存活的 Foremind 会话：{len(rows)} 个，CPU 合计 {sum(r[0] for r in rows):.1f}%，"
            f"内存合计 {sum(r[1] for r in rows) / 1024:.0f} MB"] + [r[2] for r in rows]


def _controller(root) -> list[str]:
    """REQ-4: the most recently updated controller state (its last Stop's reading, whoever runs status), against its
    budget (controller.budget)."""
    if not (sts := controller._states(root)):
        return []
    st = max(sts, key=lambda s: str(s.get("updated_at", "")))
    try:
        cfg = config.load(root)
    except config.ConfigError:
        cfg = {}  # the defaults, as controller.stop gates on them then
    soft, hard = controller.budget(cfg, st.get("model"))
    used = st.get("context_tokens")
    return [f"总控：{st.get('controller')}  context_tokens {used if used is not None else '未读到'}  "
            f"软线 {int(soft)}  硬线 {int(hard)}  更新于 {st.get('updated_at')}"]


def _reader(root):
    """A Tick over the pass's own facts, read only (D1): __init__ skipped (quota.migrate writes the user level, a
    phase import error an event); notify / emit / error do nothing (gave_up notifies, bcfg emits)."""
    t = tick.Tick.__new__(tick.Tick)
    t.root, t.cfg, t.now, t.sd = root, config.load(root), time.time(), state_dir(root)
    t.log, t.did, t.demand, t.evs, t._cfgs = EventLog(t.sd / "events.jsonl"), [], set(), [], {}
    t.qfile = quota.view(root)
    t.notify = t.emit = t.error = lambda *a, **k: None
    t.load()
    return t


def _explain(root, sv) -> list[str]:
    """REQ-10: `<batch> <state> — 在等：<who>；原因：<why>；你要做：<command>` per unfinished batch."""
    try:
        t = _reader(root)
        direct, why = t.waits()
    except config.ConfigError as e:
        return [f"等待：配置有误，解释不了（{e}）；你要做：foremind doctor"]
    except Exception as e:  # r1: a malformed event, a Tick attribute added since (D1): the other lines still print
        return [f"等待：读不了（{type(e).__name__}: {e}）"]
    halt = []
    if (root / "STOP").exists():
        halt.append("项目根有 STOP 文件，删掉它")
    if tick.paused_path(root).exists():
        halt.append("已暂停，foremind resume")
    if sv != "current":
        halt.append("监督进程" + ("未运行" if sv == "stopped" else "代码旧于当前包") + "，foremind supervise")
    halt += [f"额度 {g} 用完，等额度恢复" for g in quota.GROUPS if t.q(g)["state"] == quota.EXHAUSTED]
    tail = f"（不会自动推进：{'；'.join(halt)}）" if halt else ""
    out = []
    for bid in t.order:
        st = t.state(bid)
        if st in ready.FINISHED:
            continue
        if bid in direct:
            who, reason, todo = "你", direct[bid], _todo(t, bid, direct[bid])
        elif bid in why:
            who, reason, todo = *_not_ready(t, bid, why[bid]), NONE
        else:
            who, reason, todo = _started(t, bid, st)
        out.append(f"  {bid} {st} — 在等：{who}；原因：{reason}；你要做：{todo}{tail}")
    return ["等待："] + out if out else []


def _todo(t, bid, reason) -> str:
    """The command for a reason of Tick.waits (a batch waiting on the user): by its code (tick.Reason); a reason
    without a known one by its text, as before m2d.9."""
    cmds = {"pending": "foremind decide", "land": f"foremind land {bid}",
            "audit_held": f"看 reports/ 后 foremind audit --release {bid}", "confirm_exit": f"foremind confirm-exit {bid}",
            "approve_plan": f"foremind plan approve {t.plan_of[bid]}", "config": "foremind doctor",
            "plan_unbound": f"改回批准时的样子，或 foremind plan amend {t.plan_of[bid]}",
            "claim": f"foremind seat --user {bid}", "user": NONE,
            **dict.fromkeys(("failed", "present", "gave_up"), f"foremind run {bid}")}
    code = getattr(reason, "code", None)
    if code == "decide":
        return "；".join(f"foremind decide show {q}" for q in reason.qs)
    if code in cmds:
        return cmds[code]
    if reason.startswith("待决 "):
        return "；".join(f"foremind decide show {q}" for q in reason[3:].split("、"))
    for key, c in (("待决", "pending"), ("等你合入", "land"), ("等审计放行", "audit_held"),
                   ("confirm-exit", "confirm_exit"), ("计划待批准", "approve_plan"), ("计划批准后被改动", "plan_unbound"),
                   ("配置有误", "config"), ("认领", "claim"), ("你在做", "user")):
        if key in reason:
            return cmds[c]
    return f"foremind run {bid}"  # failed, retries used up (gave_up), 等你在场


def _not_ready(t, bid, rs) -> tuple[str, str]:
    """(who, why) of a planned / ready batch from ready.why_not_ready's reasons; none left: what open_seats waits on."""
    if rs:
        done = "批准" if t.mode(t.bcfg(bid)) == "approved" else "合入"
        text = {"after": ("上游 {}", "上游 {} 未" + done), "decision": ("决策者", "{} 决策中"),
                "overlaps": ("{}", "owns_paths 与 {} 重叠")}
        return ("、".join(dict.fromkeys(text[k][0].format(r) for k, r in rs)),
                "；".join(text[k][1].format(r) for k, r in rs))
    if t.state(bid) == "planned":
        return "监督进程", "条件已齐，下一轮转 ready"
    if x := t.leftover(bid):
        return "监督进程", f"关闭开席中断留下的 {x}"
    if bid in t.busy():
        return "监督进程", "开席中"
    q = t.q("long")["state"]
    if q in (quota.EXHAUSTED, quota.UNKNOWN) or q == quota.LOW and bid not in t.requested():
        return "额度", f"额度 {q}"
    return _held_back(t, successor=False) or ("监督进程", "下一轮开席")


def _held_back(t, successor) -> tuple[str, str] | None:
    """(who, why) of a seat the next pass would not open: no free one as Tick.free_seats counts them (open_seats: used
    and waiting successors against the cap; Tick.successor: used alone, successors go first), or REQ-6 the last
    seat_deferred (Tick.machine_ok) when no seat opened since, with its reading and when it was read. ponytail: that
    reading stays until the next seat opens (machine_ok records one per seat opened); read the machine here if a
    stale one misleads."""
    used, cap, waiting = t.free_seats()
    if (used if successor else used + waiting) >= cap:
        return "名额", f"名额已满（{used + waiting}/{cap}，其中待开继任 {waiting} 个）"
    for e in reversed(t.evs):
        if e["type"] == "seat_opened":
            return None
        if e["type"] == "seat_deferred":
            what = (f"1 分钟负载 {e.get('load1')}（{e.get('cpus')} 核 × {e.get('max_load_per_cpu')}）"
                    if e.get("reason") == "load" else
                    f"可用内存 {e.get('avail_mb')} MB（下限 {e.get('min_free_mem_mb')} MB）")
            return "监督进程", f"整机负载高，推迟开席（{what}，读于 {e['ts']}）"
    return None


def _review_gate(t, bid) -> str | None:
    """in_review as tick.gates / _gate see it: "run" while they run the gate on the newest receipt (approved, no
    review open, not answered with exit 0 or 1), "failed" once it answered 1 (the seat was told GATE_TEXT); else None
    (the reviewer's)."""
    rs = review.receipts(t.root, bid)
    if not rs or any(e.get("batch") == bid for e in t.open_intents("review_started")):
        return None
    n, path = rs[-1]
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("verdict") != "approved":
            return None
    except (OSError, ValueError, AttributeError):
        return None
    codes = {(t.result(e["dedupe_id"]) or {}).get("exit_code") for e in t.evs if e["type"] == "sv_gate"
             and e["phase"] == "intent" and e.get("batch") == bid and e.get("round") == n}
    return "failed" if 1 in codes else None if 0 in codes else "run"


def _started(t, bid, st) -> tuple[str, str, str]:
    """(who, why, command) of a started batch not waiting on the user."""
    s = t.holders.get(bid)
    if st in tick.LIVE and s:
        hb = heartbeat.read(t.root, s) or {}
        who = f"席位 {s}（心跳 {hb.get('ts') or '无'}）"
        if isinstance(hb.get("waiting_input"), dict):
            return who, "在终端里等你回答", "到这个席位的终端里回答"
        if hb.get("handoff_requested"):
            return who, "交接中，等继任", NONE
        try:
            retry = any(m.sender in stuck.retry_senders(t) for m in inbox.pending_messages(s, root=t.root))
        except (OSError, ValueError, inbox.InboxCorrupt):
            retry = False
        return who, "API 错误自动续跑中" if retry else "按必改清单修改中" if st == "changes_requested" else "在做", NONE
    if st in (*tick.LIVE, "stuck"):
        if p := t.pending_successor(bid):
            return f"继任 {p}", "等它 handoff --accept 接手", NONE
        if t.needs_successor(bid):
            held = not s and _held_back(t, successor=True)  # Tick.successor: one taking over a holder's seat opens
            return *(held or ("监督进程", "无人在做，下一轮开继任")), NONE
        if t.in_flight(bid) or t.opening(bid) or not (x := t.stuck_session(bid) or s):
            return "监督进程", "开继任中", NONE
        return "监督进程", f"卡住，关闭 {x} 中", NONE
    if st == "in_review" and (g := _review_gate(t, bid)):
        if g == "failed":
            return f"席位 {s}" if s else "席位", "门禁未通过，改完再 foremind review", NONE
        st = "approved"
    if st == "blocked" and (qs := ready.blocking(bid, t.decisions)):  # all deciding, else waits() had them
        return "决策者", f"{'、'.join(qs)} 决策中", NONE
    if st == "approved":
        ups = t.unmerged_upstream(bid)
        return "门禁", (f"等上游 {'、'.join(ups)} 合入" if ups else
                        f"门禁在跑，CI pending 时每 {tick.setting(t.cfg, 'supervisor.gate_retry_min')} 分钟重试"), NONE
    return {"review_ready": ("审查者", "等审查者启动（额度或名额）"), "in_review": ("审查者", "在审"),
            "awaiting_audit": ("审计者", "在审"), "delivered": ("监督进程", "合入重试")}.get(
        st, ("监督进程", f"状态 {st}")) + (NONE,)


def _run(args):
    if args.all:
        try:
            roots = [Path(p) for p in install.registered()]
        except install.InstallError as e:  # e.g. a registry that is not UTF-8
            print(f"foremind status: {e}", file=sys.stderr)
            return 1
        if not roots:
            print(f"没有已启用的项目（{install.registry_path()}）")
        print("\n\n".join(report(r) for r in roots))
        return 0
    try:
        print(report(find_project_root()))
    except ProjectNotFound as e:
        print(f"foremind status: {e}", file=sys.stderr)
        return 1
    return 0
