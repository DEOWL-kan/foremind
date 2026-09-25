"""Stuck seats (DESIGN §10.3, §10.4): `judge` is pure, `last_change` reads a worktree, `check` acts for one tick.

The session watched is the batch's successor while it has not run `handoff --accept` yet (MF-1), else its holder.
A running batch is quiet since the newest of that session's heartbeat and opening, the batch's last move to running,
its worktrees' last change (HEAD commit time, mtimes of modified and untracked files) and the end of the last quota
exhaustion. Quiet stuck.remind_min (20) -> inbox reminder; stuck.ask_min (20) after the reminder -> ask for the state
in the batch log; stuck.mark_min (20) after that -> `stuck`, notify, and take over: close the session through its
carrier, break its lock (if it holds it) with the exit evidence, open a successor. No evidence (manual carrier, exit
not confirmed) -> notify only; the supervisor retries the close, or the user runs `foremind confirm-exit`.
A session with a tool call open (busy_tool) gets reminders only until stuck.busy_tool_min (120). A session the
carrier reports gone skips straight to `stuck`. Waiting on a decision or on exhausted quota is not being stuck.
Any sign of life moves the quiet mark, and the stages start over.
"""
from datetime import datetime

from foremind import heartbeat, lock, worktree
from foremind.supervisor import quota, ready

REMIND = ("Foremind：{m} 分钟没有看到你的心跳或 worktree 改动。还在工作就继续；遇到卡点按协议记 D/F，"
          "或用 `foremind decide --new` 提交待决。")
ASK = ("Foremind：仍然没有进展。现在用 `foremind log` 把现场写进批次日志：在做什么、卡在哪、下一步。"
       "再无回应，批次将标为卡住并交给继任会话。")


def _min(cfg, key, default):
    v = cfg.get(key)
    return 60 * (v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default)


def judge(quiet_s, now, done, *, tool_open, alive, cfg) -> str | None:
    """Next step for a running batch: "remind" | "ask" | "stuck" | None. `done` = {stage: time taken} of the
    stages already taken since the quiet mark."""
    if alive is False:
        return "stuck"
    if "remind" not in done:
        return "remind" if quiet_s >= _min(cfg, "stuck.remind_min", 20) else None
    if "ask" not in done:
        busy_ok = not tool_open or quiet_s >= _min(cfg, "stuck.busy_tool_min", 120)
        return "ask" if busy_ok and now - done["remind"] >= _min(cfg, "stuck.ask_min", 20) else None
    return "stuck" if now - done["ask"] >= _min(cfg, "stuck.mark_min", 20) else None


def last_change(path) -> float | None:
    """Newest of HEAD's commit time and the mtimes of modified and untracked files; None if unreadable."""
    # ponytail: a deletion leaves no file to stat; the parent directory's mtime is not looked at
    try:
        times = [float(worktree.git(path, "log", "-1", "--format=%ct") or 0)]
        files = worktree.git(path, "ls-files", "-m", "-o", "--exclude-standard", "-z").split("\0")
    except worktree.WorktreeError:
        return None
    for f in filter(None, files):
        try:
            times.append((path / f).stat().st_mtime)
        except OSError:
            pass
    return max(times)


def _ts(v) -> float:
    try:
        return datetime.fromisoformat(v).timestamp()
    except (TypeError, ValueError):
        return 0.0


def check(t, bid) -> None:
    """One tick for running batch `bid` (t: the tick in progress)."""
    session = t.pending_successor(bid) or t.holders.get(bid)
    if not session or session == lock.USER or ready.blocking(bid, t.decisions):
        return
    if t.q("long")["state"] == quota.EXHAUSTED or session in t.qfile.get("paused", []):
        return  # waiting for quota is not stuck
    hb = heartbeat.read(t.root, session) or {}
    try:
        alive = t.carrier_for(bid, session).read_state(session).alive
    except t.ERRORS as e:
        t.error("carrier", bid, e)
        alive = None
    if hb.get("handoff_requested") and t.handed_off(bid, session):
        # context handoff (§6.4): a successor verifies against the section and takes the lock at `handoff --accept`,
        # then close_predecessors closes this session
        if not t.succeeded(bid, session):
            t.successor(bid, "handoff")
        return
    marks = [_ts(hb.get("ts")), t.qfile.get("exhausted_ended", 0), t.opened_at(session)]
    marks += [_ts(e["ts"]) for e in t.evs if e["type"] == "batch_state" and e.get("batch") == bid
              and "running" in (e.get("to"), e.get("state"))][-1:]  # e.g. back from changes_requested (SF-1)
    marks += [last_change(p) or 0 for p in t.worktrees(bid)]
    since = int(max(marks))
    key = f"stuck:{bid}:{session}:{since}"
    done = {s: e["at"] for s in ("remind", "ask") if (e := t.result(f"sv_say:{key}:{s}"))}
    step = judge(t.now - since, t.now, done, tool_open=bool(hb.get("tool_open")), alive=alive, cfg=t.bcfg(bid) or t.cfg)
    if step == "remind":
        t.say(session, REMIND.format(m=int((t.now - since) // 60)), f"{key}:remind")
    elif step == "ask":
        t.say(session, ASK, f"{key}:ask")
    elif step == "stuck":
        why = "session gone" if alive is False else "no response"
        if t.write_state(bid, "stuck", expect=("running",), prior="running", reason=why, session=session):
            t.did.append(f"{bid}: stuck ({why})")
            t.take_over(bid, session)
