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
m2b.8: a seat waiting on an answer in its terminal (heartbeat waiting_input, from the Notification hook) is not stuck
either: notified once per wait instead. A tool left open (finding 26: a call that never got its PostToolUse) whose
heartbeat has been quiet for stuck.remind_min while the carrier shows the session alive and idle is closed in the
heartbeat (`tool_open_cleared`), so deliveries and the stuck stages see an idle session again.
m2c.2 (finding 29): an alive, idle session whose transcript (heartbeat transcript_path) ends on a retryable API error
gets an inbox message to go on, stuck.api_retry_min (2) × 2^(n-1) minutes after the error or retry n-1, at most
stuck.api_retry_max (3) times per run of errors (reset by a good reply); no waiting notice or stuck stage meanwhile.
Only where tick._deliver sends it: an auto batch whose screen is idle, with no tool open (an API error ends the
turn with StopFailure, never Stop: Claude Code 2.1.280). A session handing off (§6.4) gets retries too, and nothing
else (Q-25), none while its inbox holds other messages (one behind them would never go out), and none once it wrote
its section (r3). A full block (`retries_only`, r3) gets them too; a due retry takes the pending inbox with it (r4).
Used up, not retryable or not sendable: the waiting notice names the error. A batch waiting on a decision, or a full
block, gets its retries and that notice (r2, r4), still nothing else.
m2d.6: the heartbeat's api_error (StopFailure, REQ-11) later than the transcript's last good reply is classified by
its type (api_error); a run of errors ends in api_retry_result (a good reply after its retries: ok, used up: not ok).
An alive session, not waiting in its terminal nor handing off, whose last 20 main-chain records go round in circles
(pattern: repeat_error, alternate, text_only) gets one reminder per fingerprint and `stuck_pattern`; nothing else
(REQ-17, A8). A quiet mark that moved after a remind or ask: `stuck_recovered`. Open tools are cleared here only
(clear_tool; tick calls it for the other holders, REQ-18).
"""
import itertools
import json
import os
from datetime import datetime

from foremind import heartbeat, inbox, lock, telemetry, worktree
from foremind.defaults import TABLE
from foremind.fsutil import sha256_bytes
from foremind.supervisor import quota, ready

REMIND = ("Foremind：{m} 分钟没有看到你的心跳或 worktree 改动。还在工作就继续；遇到卡点按协议记 D/F，"
          "或用 `foremind decide --new` 提交待决。")
ASK = ("Foremind：仍然没有进展。现在用 `foremind log` 把现场写进批次日志：在做什么、卡在哪、下一步。"
       "再无回应，批次将标为卡住并交给继任会话。")
RETRY = ("Foremind：上一轮因 API 错误中断（{e}），这是第 {n} 次自动续跑。从中断处接着做：先看收件箱与批次日志的最后状态。")
PATTERN = ("Foremind：你最近{what}，像在原地打转。换个思路；确实卡住就按协议写 F 记录，"
           "再用 `foremind decide --new` 提交待决。")
WHAT = {"repeat_error": "把同一条 Bash 命令连续跑了 {n} 次、报同样的错", "alternate": "在两个动作之间来回交替了 {n} 轮",
        "text_only": "连续 {n} 个回合只输出文字、没有调用工具"}


def _min(cfg, key):
    v = cfg.get(key)
    return 60 * (v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else TABLE[key])


def retry_senders(t) -> set:
    """Senders of the API-retry messages (by their token, as tick.say signs them); worked out again only when t has
    new intents (tick._deliver asks once per message)."""
    if getattr(t, "_senders", (None,))[0] != len(t.intents):
        t._senders = (len(t.intents), {f"supervisor#{e.get('token')}" for d, e in t.intents.items()
                                       if d.startswith("sv_say:apiretry:")})
    return t._senders[1]


def retries_first(t, msgs) -> list:
    """The API-retry messages `msgs` starts with."""
    ours = retry_senders(t)
    return list(itertools.takewhile(lambda m: m.sender in ours, msgs))


def _sent(t, session) -> list:
    """(error id, n, result event) of the API-error retries told to `session`, oldest first."""
    pre = f"sv_say:apiretry:{session}:"
    return [(*d[len(pre):].rsplit(":", 1), e) for d, e in t.results.items() if d.startswith(pre)]


def _records(transcript):
    """A transcript's main-chain user and assistant records, newest first."""
    for line in telemetry._backwards(transcript, 1 << 16):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("type") in ("user", "assistant") and not e.get("isSidechain"):
            yield e


def _last_reply(transcript) -> float | None:
    """When the transcript's main chain last got a good assistant reply (0.0: never); None if it cannot be read."""
    if not isinstance(transcript, str) or not os.path.isfile(os.path.expanduser(transcript)):
        return None
    return next((_ts(e.get("timestamp")) for e in _records(transcript)
                 if e["type"] == "assistant" and e.get("isApiErrorMessage") is not True), 0.0)


def api_error(t, session, hb) -> dict | None:
    """REQ-11: the API error `session` stopped on, as telemetry.last_api_error. The heartbeat's api_error later than
    the transcript's last good reply is classified by its type (one of neither list: as the transcript says, no
    record there: not retryable); with no error record in the transcript its id is hb:<at>, and the run also holds
    the retries sent since that reply (hb: ids chain no other way). No such api_error: the transcript alone."""
    tp, h = hb.get("transcript_path"), hb.get("api_error")
    err = telemetry.last_api_error(tp)
    at = _ts(h.get("at")) if isinstance(h, dict) else 0.0
    if not at or (last := _last_reply(tp)) is None or at <= last:
        return err
    typ = h.get("type")
    if err is None:
        err = {"id": f"hb:{h['at']}", "text": f"StopFailure {typ}", "retryable": False, "at": at,
               "run": [f"hb:{h['at']}"]}
    if isinstance(typ, str) and typ in telemetry._RETRY_ERRORS | telemetry._FINAL_ERRORS:
        err["retryable"] = typ in telemetry._RETRY_ERRORS
    err["run"] += [eid for eid, _, e in _sent(t, session) if e["at"] > last and eid not in err["run"]]
    return err


def _retry_result(t, bid, session, retries, error_id, ok) -> None:
    """api_retry_result once per run of errors, by the error id its first retry was for (`retries`: its retries)."""
    eid = retries[0][0] if retries else error_id
    did = f"api_retry_result:{session}:{eid}"
    if t.result(did) is None:
        t.emit("api_retry_result", dedupe_id=did, batch=bid, session=session, error_id=eid, tries=len(retries), ok=ok)


def retry_ended(t, bid, session) -> None:
    """ok: a good reply after the last retry of the latest run. tick.deliver asks for the holder and pending successor
    of every unfinished batch, busy or idle: a seat that went on after a retry may finish its batch in that turn."""
    if not (sent := _sent(t, session)):
        return
    start = next((i for i in range(len(sent) - 1, -1, -1) if sent[i][1] == "1"), 0)
    if t.result(f"api_retry_result:{session}:{sent[start][0]}") is not None:
        return
    if (_last_reply((heartbeat.read(t.root, session) or {}).get("transcript_path")) or 0) > sent[-1][2]["at"]:
        _retry_result(t, bid, session, sent[start:], None, True)


def _retry(t, bid, session, err, cfg, *, handoff, full) -> bool:
    """Retries of the run `err` ends: send the next when due; False once stuck.api_retry_max are used up, or when
    `handoff` and the inbox holds other messages (tick._deliver then gives only the retries the inbox starts with)."""
    run = [r for r in _sent(t, session) if r[0] in err["run"]]
    sent = [e["at"] for _, _, e in run]
    n, cap = len(sent) + 1, cfg.get("stuck.api_retry_max")
    if n > (cap if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0 else TABLE["stuck.api_retry_max"]):
        _retry_result(t, bid, session, run, err["run"][-1], False)
        return False
    msgs = inbox.pending_messages(session, root=t.root)
    if handoff and len(retries_first(t, msgs)) < len(msgs):
        return False
    # a message still waiting in the inbox is a retry on its way (deliver runs after check, and waits only for quota);
    # in a full block only a retry is, and tick._deliver sends the rest with it (r4)
    if full:
        msgs = [m for m in msgs if m.sender in retry_senders(t)]
    if t.now - max(sent, default=err["at"]) >= _min(cfg, "stuck.api_retry_min") * 2 ** (n - 1) and not msgs:
        t.say(session, RETRY.format(e=err["text"][:200], n=n), f"apiretry:{session}:{err['id']}:{n}")
    return True


def judge(quiet_s, now, done, *, tool_open, alive, cfg) -> str | None:
    """Next step for a running batch: "remind" | "ask" | "stuck" | None. `done` = {stage: time taken} of the
    stages already taken since the quiet mark."""
    if alive is False:
        return "stuck"
    if "remind" not in done:
        return "remind" if quiet_s >= _min(cfg, "stuck.remind_min") else None
    if "ask" not in done:
        busy_ok = not tool_open or quiet_s >= _min(cfg, "stuck.busy_tool_min")
        return "ask" if busy_ok and now - done["remind"] >= _min(cfg, "stuck.ask_min") else None
    return "stuck" if now - done["ask"] >= _min(cfg, "stuck.mark_min") else None


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


def _blocks(e, kind) -> list:
    c = e.get("message", {}).get("content") if isinstance(e.get("message"), dict) else None
    return [b for b in c if isinstance(b, dict) and b.get("type") == kind] if isinstance(c, list) else []


def _key(*parts) -> str:
    return json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)


def pattern(recs, repeat=3, *, ended=True, told=lambda kind, basis: False) -> tuple | None:
    """REQ-17: (kind, count, basis of its fingerprint) in main-chain records `recs` (oldest first), else None; the
    last run of the first kind found (an earlier one still in the window was told already) whose (kind, basis) is
    not `told` (m2e REQ-9: a kind told already does not hold back the next):
    repeat_error, the same Bash command `repeat` times in a row with the same error; alternate, two calls taking turns
    3 rounds or more; text_only, 3 turns in a row with text and no tool call (a turn starts at a prompt, a user record
    with no tool_result, that follows a reply; API-error records are no reply; the last turn counts once `ended`;
    basis: the uuid of the run's first prompt).
    The same idea as OpenHands' stuck_detector, on Claude Code's records."""
    calls, results, turns = [], {}, []
    for e in recs:
        if e.get("type") == "user":
            res = _blocks(e, "tool_result")
            results.update((b.get("tool_use_id"), b) for b in res)
            if not res and not (turns and not turns[-1][1]):  # prompts with no reply between: one turn
                turns.append([e.get("uuid"), 0, 0])  # [prompt, replies, tool calls]
        elif e.get("isApiErrorMessage") is not True:
            uses = _blocks(e, "tool_use")
            calls += uses
            if turns:
                turns[-1][1] += 1
                turns[-1][2] += len(uses)

    def failed(c):  # a Bash call that failed: its command and output
        r = results.get(c.get("id"))
        if c.get("name") == "Bash" and isinstance(c.get("input"), dict) and r and r.get("is_error") is True:
            return _key(c["input"].get("command"), r.get("content"))
        return None

    errs = [failed(c) for c in calls]
    if (r := _last_run(errs, lambda x, n: x is not None and n >= repeat)) and not told("repeat_error", errs[r[0]]):
        return "repeat_error", r[1], errs[r[0]]
    acts = [_key(c.get("name"), c.get("input")) for c in calls]
    found, i = None, 0
    while i < len(acts) - 1:
        pair, n = acts[i:i + 2], 2
        while pair[0] != pair[1] and i + n < len(acts) and acts[i + n] == pair[n % 2]:
            n += 1
        found, i = ((n // 2, _key(*sorted(pair))), i + n - 1) if n >= 6 else (found, i + 1)
    if found and not told("alternate", found[1]):
        return "alternate", *found
    done = turns if ended else turns[:-1]
    flags = [t[1] > 0 and not t[2] for t in done]
    if (r := _last_run(flags, lambda x, n: x and n >= 3)) and not told("text_only", done[r[0]][0]):
        return "text_only", r[1], done[r[0]][0]
    return None


def _last_run(xs, ok) -> tuple | None:
    """(start, length) of the last run of equal items in xs that ok(item, length) takes."""
    found, i = None, 0
    while i < len(xs):
        n = _streak(xs, i)
        found = (i, n) if ok(xs[i], n) else found
        i += n
    return found


def _streak(xs, i) -> int:
    """How many items from xs[i] on equal it."""
    return next((j for j in range(i, len(xs)) if xs[j] != xs[i]), len(xs)) - i


def clear_tool(t, bid, session, hb, idle=None) -> dict:
    """REQ-18 (finding 26): the tools left open in `session`'s heartbeat `hb` are closed (`tool_open_cleared`) once it
    has been quiet stuck.remind_min while its carrier shows it alive and idle (`idle`, if the caller read that; else
    read here, and only then). Returns the heartbeat."""
    if not hb.get("tool_open") or t.now - _ts(hb.get("ts")) < _min(t.bcfg(bid) or t.cfg, "stuck.remind_min"):
        return hb
    if idle is None:
        st = t.carrier_for(bid, session).read_state(session)
        idle = st.alive and st.idle
    if not idle:
        return hb
    tools = hb.get("open_tools") or []
    for tid in tools:  # by id: a tool the session opens meanwhile stays open (heartbeat.update merges under lock)
        hb = heartbeat.update(t.root, session, close_tool=tid)
    t.emit("tool_open_cleared", dedupe_id=f"tool_open_cleared:{session}:{hb.get('ts')}", batch=bid, session=session,
           tools=tools)
    return hb


def _pattern(t, bid, session, hb, cfg, ended) -> None:
    """A reminder once per fingerprint, which takes the session in (a successor going round the same way is told
    again, D3); text_only's is the last tool call before the run (none in the transcript: None)."""
    recs = list(itertools.islice(_records(hb.get("transcript_path")), 20))[::-1]
    v = cfg.get("stuck.pattern_repeat")
    v = v if isinstance(v, int) and not isinstance(v, bool) and v >= 2 else TABLE["stuck.pattern_repeat"]

    def fp(kind, basis):
        if kind == "text_only":  # the run's start moves as the window slides: the tool call before it does not
            recs = _records(hb.get("transcript_path"))
            any(e.get("uuid") == basis for e in recs)  # skip to the run's first prompt
            basis = next((b["id"] for e in recs for b in _blocks(e, "tool_use")), None)
        return sha256_bytes(_key(session, kind, basis).encode())[:16]

    if not (p := pattern(recs, v, ended=ended, told=lambda k, b: t.result(f"stuck_pattern:{fp(k, b)}") is not None)):
        return
    kind, count, basis = p
    f = fp(kind, basis)
    t.say(session, PATTERN.format(what=WHAT[kind].format(n=count)), f"pattern:{f}")
    t.emit("stuck_pattern", dedupe_id=f"stuck_pattern:{f}", batch=bid, session=session, kind=kind, count=count,
           fingerprint=f)


def _recovered(t, bid, session, hb) -> None:
    """stuck_recovered for each earlier quiet mark of this session that got a remind or ask (it never reached stuck:
    the batch is still running). The quiet mark is worked out only when one is still unrecorded."""
    pre = f"sv_say:stuck:{bid}:{session}:"
    stages = {}
    for d, e in t.results.items():
        if d.startswith(pre) and (m := d[len(pre):].rsplit(":", 1))[0].isdigit() \
                and t.result(f"stuck_recovered:{bid}:{session}:{m[0]}") is None:
            stages.setdefault(int(m[0]), {})[m[1]] = e["at"]
    since = _since(t, bid, session, hb) if stages else 0
    for old, done in stages.items():
        did = f"stuck_recovered:{bid}:{session}:{old}"
        if old < since:
            stage = "ask" if "ask" in done else "remind"
            t.emit("stuck_recovered", dedupe_id=did, batch=bid, session=session, stage=stage,
                   after_s=max(0, since - int(done[stage])))


def _since(t, bid, session, hb) -> int:
    """The quiet mark: the newest sign of life (module docstring)."""
    marks = [_ts(hb.get("ts")), t.qfile.get("exhausted_ended", 0), t.opened_at(session)]
    marks += [_ts(e["ts"]) for e in t.evs if e["type"] == "batch_state" and e.get("batch") == bid
              and "running" in (e.get("to"), e.get("state"))][-1:]  # e.g. back from changes_requested (SF-1)
    marks += [last_change(p) or 0 for p in t.worktrees(bid)]
    return int(max(marks))


def check(t, bid, *, retries_only=False) -> None:
    """One tick for running batch `bid` (t: the tick in progress); `retries_only` (a full block): the API-error
    retries and their waiting notice alone."""
    session = t.pending_successor(bid) or t.holders.get(bid)
    if not session or session == lock.USER:
        return
    hb = heartbeat.read(t.root, session) or {}
    _recovered(t, bid, session, hb)  # before every return below: a decide --new, a handoff are signs of life too
    if t.q("long")["state"] == quota.EXHAUSTED or session in t.qfile.get("paused", []):
        return  # waiting for quota is not stuck
    try:
        st = t.carrier_for(bid, session).read_state(session)
        alive, idle = st.alive, st.idle
    except t.ERRORS as e:
        t.error("carrier", bid, e)
        alive = idle = None
    # waiting on a decision is not stuck: only the API-error steps below
    blocked = retries_only or ready.blocking(bid, t.decisions)
    if hb.get("handoff_requested") and t.handed_off(bid, session, hb.get("handoff_requested_at")):
        # context handoff (§6.4): a successor verifies against the section and takes the lock at `handoff --accept`,
        # then close_predecessors closes this session; never told to go on (r3). Only a section written since the
        # request counts (a heartbeat from before handoff_requested_at existed: any section)
        if not blocked and not t.succeeded(bid, session):
            t.successor(bid, "handoff")
        return
    cfg = t.bcfg(bid) or t.cfg
    err = None
    if alive and (idle or hb.get("event") == "Stop" or isinstance(hb.get("waiting_input"), dict)):
        err = api_error(t, session, hb)
        # only where tick._deliver carries it (an auto batch, idle screen or Stop, no open tool): a retry it never
        # sends never reaches the cap either; the rest go on to the waiting notice or the stuck stages
        if (err and err["retryable"] and (idle or hb.get("event") == "Stop") and not hb.get("tool_open")
                and t.headers[bid].get("mode") == "auto"
                and _retry(t, bid, session, err, cfg, handoff=bool(hb.get("handoff_requested")), full=retries_only)):
            return
    w = hb.get("waiting_input")
    if alive and not retries_only and not hb.get("handoff_requested") and not isinstance(w, dict):
        _pattern(t, bid, session, hb, cfg, bool(idle or hb.get("event") == "Stop"))  # a decision wait too (r1)
    if blocked and not (err and isinstance(w, dict)):  # the notice for an API error is no model call (r4)
        return
    if alive is not False and isinstance(w, dict):
        t.notify(f"waiting:{session}:{w.get('at')}", f"{bid} 在终端里等你回答",
                 f"会话 {session}（{w.get('kind') or '提示'}）停在终端里等回答，回到它的终端处理。"
                 + (f"最后一条是 API 错误：{err['text'][:200]}" if err else ""))
        return
    hb = clear_tool(t, bid, session, hb, bool(alive and idle))
    since = _since(t, bid, session, hb)
    key = f"stuck:{bid}:{session}:{since}"
    done = {s: e["at"] for s in ("remind", "ask") if (e := t.result(f"sv_say:{key}:{s}"))}
    step = judge(t.now - since, t.now, done, tool_open=bool(hb.get("tool_open")), alive=alive, cfg=cfg)
    if step == "remind":
        t.say(session, REMIND.format(m=int((t.now - since) // 60)), f"{key}:remind")
    elif step == "ask":
        t.say(session, ASK, f"{key}:ask")
    elif step == "stuck":
        why = "session gone" if alive is False else "no response"
        if t.write_state(bid, "stuck", expect=("running",), prior="running", reason=why, session=session):
            t.did.append(f"{bid}: stuck ({why})")
            t.take_over(bid, session)
