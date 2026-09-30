"""Claude Code hooks behind `foremind hook <event>` (DESIGN §1.1, §6.1–6.4, §8.2, §10.3, §10.8, §20 I31 I35–I42).

main() never raises and never blocks because of its own failure: an internal error is logged as a `hook_error`
event and the call goes through. Only explicit rules block: PreToolUse with JSON permissionDecision "deny" (never
exit code 2, which echoes the hook command line to the model), Stop with decision "block" unless the input's
stop_hook_active is already true.
Failure boundaries (I38): each handler decides first and keeps its books after (events, heartbeat, telemetry, inbox
cursor); a bookkeeping failure is logged and never changes the decision. Unreadable config, environment or batch
header degrade the checks that depend on them, not the whole hook.
A Foremind session has FOREMIND_SESSION (plus FOREMIND_BATCH, FOREMIND_ROLE, FOREMIND_PROJECT); other sessions get
only the `.foremind/` guard, presence records and `user_config_edit` records of their #22 edits (I51), and one with
FOREMIND_CONTROLLER (opened by `controller open`) foremind.controller's SessionStart bind and Stop context gate.
Outside every Foremind project the hooks do nothing (I41).

Events: session_bound; pending_needed (hard-block hit without an exemption; decide.pending.from_hit then opens a
Q-n for it, pushed by a detached job and never from the hook, or reuses the unresolved one of the same key; none when
approving could not release the call: the writable matrix denies it too, the session has no batch, or the target is
outside every repo; the deny reason then says no Q-n was filed and points to `foremind decide --new`);
hook_denied (writable-matrix denial); exemption_used; user_present; user_config_edit (a non-Foremind session's
Edit/Write of a #22 file, with the file's sha256 after the write, I51); session_waiting (Notification of a type that
waits on someone at the terminal; heartbeat waiting_input until the next turn hook); tool_after_stop (a PreToolUse
after a Stop that ended the turn whose tool_use is not in the transcript's tail: not counted as an open tool, finding
26); tool_after_stop_confirmed (its PostToolUse came after all: the first tool of a turn going on, m2c.7.F2);
handoff_requested (a seat's first request at the hard line, Stop or mid-turn, REQ-10); api_error (StopFailure: the
turn ended on an API error, heartbeat api_error{type, at}, REQ-11); seat_prompt_external (a seat's prompt that
matches no program_delivery, the same match as presence, m2e REQ-14); hook_error.
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foremind import batchlog, config, handoff, heartbeat, inbox, lock, telemetry
from foremind import header as hdr
from foremind.defaults import TABLE
from foremind.plan import model as plan_model
from foremind import repos as repos_mod
from foremind.events import EventLog
from foremind.fsutil import LockBusy, sha256_bytes
from foremind.hooks import guard
from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir
from foremind.schemas import BATCH_ID, PLAN_ID

# REQ-8: a seat whose status line reports a window this large gets these defaults instead (soft 200k, hard 300k at 1M)
BIG_WINDOW, BIG_SOFT_PCT, BIG_HARD_PCT = 1_000_000, 20, 30
LINE_EVERY = 5  # REQ-9: a mid-turn reminder at a line again every this many tool calls
AFTER_STOP_KEEP = 20  # REQ-12: tool_after_stop ids kept per session for PostToolUse to confirm
GIT_TIMEOUT_S = 10
INBOX_WAIT_S = 3  # Stop waits this long for the inbox lock, then delivers next turn (the supervisor waits for it)
# §10.8: carriers write this event (text hash) right before delivering; a matching prompt is not the user
DELIVERY_EVENT, DELIVERY_HASH, DELIVERY_WINDOW = "program_delivery", "text_sha256", timedelta(minutes=10)
DF_FULL = 10  # §6.1: the last 10 D/F records in full, older ones as id + gist
L1_MAX_BYTES = 12_000  # §6.1 L1 ≤ 4k tokens; UTF-8 bytes, since a CJK character (3 bytes) can be a token or more


@dataclass
class Ctx:
    event: str
    data: dict
    root: Path
    session: str | None
    batch: str | None
    role: str | None
    stack: contextlib.ExitStack | None = None  # held until the output is written (the inbox lock)
    after: list = field(default_factory=list)  # run once the output is written (the inbox cursor)

    @property
    def foremind(self) -> bool:
        return self.session is not None

    def beat(self, **fields) -> dict:
        return heartbeat.update(self.root, self.session, batch=self.batch, role=self.role,
                                agent_session_id=self.data.get("session_id"), event=self.event,
                                **self.transcript(), **fields)

    def transcript(self) -> dict:
        """The input's transcript_path as a heartbeat field, when it is a string (stuck.py reads its last API
        error, m2c.2)."""
        tp = self.data.get("transcript_path")
        return {"transcript_path": tp} if isinstance(tp, str) else {}


def main(event: str, raw: str, emit=None) -> str | None:
    """Run hook `event` on its stdin text. The output (JSON, or None) is passed to `emit` and returned; the inbox
    cursor moves only after `emit` returned, so a failed write re-delivers rather than loses a message."""
    root = session = None
    try:
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook input is not a JSON object")
        root = _root(data.get("cwd"))
        handler = HANDLERS.get(event)
        if root is None or handler is None:
            return None
        session = os.environ.get("FOREMIND_SESSION") or None
        with contextlib.ExitStack() as stack:
            ctx = Ctx(event, data, root, session, _batch(root, event, session), os.environ.get("FOREMIND_ROLE") or None,
                      stack)
            out = handler(ctx)
            text = json.dumps(out) if out else None  # ASCII only: a non-UTF-8 stdout cannot turn a deny into a crash
            if text and emit:
                emit(text)
            for fn in ctx.after:
                _quiet(ctx, fn)
        return text
    except Exception as e:  # noqa: BLE001 — a hook failure must never turn into a block
        _error(root or _root(None), event, session, e)
        return None


def _root(cwd) -> Path | None:
    try:
        return find_project_root(Path(os.path.expanduser(cwd)) if cwd else None)
    except (ProjectNotFound, OSError):
        return None


def _batch(root, event, session) -> str | None:
    """The one batch whose lock the session holds (after a continuation it is not FOREMIND_BATCH), else
    FOREMIND_BATCH; a malformed one is logged and the session goes on without a batch (I38)."""
    held = []
    if session:
        try:
            held = lock.held_by(root, session)
        except (OSError, ValueError) as e:
            _error(root, event, session, e)
    if len(held) == 1:
        return held[0]
    batch = os.environ.get("FOREMIND_BATCH") or None
    if batch and not re.fullmatch(BATCH_ID, batch):
        _error(root, event, session, ValueError(f"bad FOREMIND_BATCH {batch!r}: going on without a batch"))
        return None
    return batch


def _event(root, type, **fields):
    EventLog(state_dir(root) / "events.jsonl").append(type, **fields)


def _error(root, event, session, e):
    msg = f"{type(e).__name__}: {e}"[:500]
    print(f"foremind hook {event}: {msg}", file=sys.stderr)
    if root:
        try:  # deduped: a persistent error (say a bad config) is logged once, not on every tool call
            _event(root, "hook_error", dedupe_id="hook_error:" + _sha(f"{event}|{session}|{msg}")[:16],
                   hook=event, session=session, error=msg)
        except Exception:  # noqa: BLE001 — nowhere left to report it
            pass


def _quiet(ctx, fn, *a, **kw):
    """Bookkeeping: a failure is logged, never undoes a decision (I38)."""
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        _error(ctx.root, ctx.event, ctx.session, e)
        return None


def _raw_config(root, cats) -> dict:
    """What survives a config that cannot be merged (I38), read file by file: `[[repos]]` (the last file that sets it
    wins), hard_block.categories of every file plus the batch header's `cats`, hard_block.patterns of the user file
    (only the user layer may set them)."""
    out = {"repos": [], "hard_block.categories": list(cats), "hard_block.patterns": []}
    for i, p in enumerate((user_config_dir() / "config.toml", Path(root) / "foremind.toml",
                           state_dir(root) / "config.toml", state_dir(root) / "delivery.toml")):  # config.load's order
        try:
            with open(p, "rb") as f:
                data = tomllib.load(f)
        except (OSError, ValueError):
            continue
        out["repos"] = data.get("repos", out["repos"])
        hb = data.get("hard_block") if isinstance(data.get("hard_block"), dict) else {}
        out["hard_block.categories"] += hb.get("categories") if isinstance(hb.get("categories"), list) else []
        if i == 0:
            out["hard_block.patterns"] = hb.get("patterns", [])
    return out


def _load(ctx) -> tuple[dict | None, dict, list]:
    """(batch header, merged config, repos); every failure is logged and degrades, keeping every hard block (I38).
    An unreadable header gives None (only owns_paths goes unchecked); a bad config is retried with only the header's
    hard_block categories as the task layer, then read file by file (_raw_config)."""
    header = {}
    if ctx.batch:
        try:
            header = hdr.parse((state_dir(ctx.root) / "batches" / f"{ctx.batch}.md").read_text(encoding="utf-8"))[0]
        except Exception as e:  # noqa: BLE001 — any read failure degrades (I38)
            header = None
            _error(ctx.root, ctx.event, ctx.session, e)
    hard = (header or {}).get("hard_block")
    cats = [c for c in hard if isinstance(c, int)] if isinstance(hard, list) else []
    for task in ([header] if header else []) + [None]:
        try:
            cfg = config.load(ctx.root, config.task_layer(task) if task else {"hard_block.categories": cats})
            return header, cfg, repos_mod.load_repos(ctx.root, cfg)
        except Exception as e:  # noqa: BLE001
            _error(ctx.root, ctx.event, ctx.session, e)
    cfg = _raw_config(ctx.root, cats)
    try:
        return header, cfg, repos_mod.load_repos(ctx.root, cfg)
    except Exception as e:  # noqa: BLE001
        _error(ctx.root, ctx.event, ctx.session, e)
        return header, cfg, []


# --- SessionStart ------------------------------------------------------------

def _read(p) -> str | None:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _status(raw) -> str | None:
    """The batch file's `## 状态` section, cut where plan.model.spec cuts it (a `## 状态` line in a fenced block is
    text), up to the next `## ` heading."""
    try:
        body = hdr.parse(raw)[1]
    except hdr.HeaderError:
        return None
    rest = body[len(plan_model.spec(body)):]
    if not rest:
        return None
    section = rest.split("\n", 1)[1] if "\n" in rest else ""
    return re.split(r"^## ", section, maxsplit=1, flags=re.M)[0]


def _last_handoff(root, session, batch) -> str | None:
    try:
        sec = handoff.latest_section(root, batch)
    except batchlog.LogRewritten as e:  # never silently: the seat must know it starts without its handoff
        _error(root, "SessionStart", session, e)
        return f"批次日志 .foremind/batches/{batch}.log.md 未通过校验，交接段未注入。先向总控报告，不要按日志内容接手。"
    except (OSError, ValueError):
        return None
    return json.dumps(sec, ensure_ascii=False, indent=1) if sec else None


def _cut(s, n) -> str:
    return s.encode()[:max(n, 0)].decode("utf-8", "ignore")


def _df_index(log, batch) -> str | None:
    rx = re.compile(rf"^[ \t]*(?:[-*][ \t]+)?({re.escape(batch)}\.[DF][1-9][0-9]*)\b.*$", re.M)
    recs = {}
    for m in rx.finditer(log):
        recs.setdefault(m.group(1), m.group(0).strip())
    items = list(recs.items())
    old = [f"{i} · {line.split(i, 1)[1].strip(' ·:-').split('·')[0].strip()[:60]}" for i, line in items[:-DF_FULL]]
    return "\n".join(old + [line for _, line in items[-DF_FULL:]]) or None


def _verified_df(root, session, batch) -> str | None:
    """The D/F index of a log that passes batchlog.verify, read only up to its recorded length (text added after the
    last `foremind log` is not taken) and checked again against that record's sha256 (m2b.8 r3: a rewrite between
    the two reads); a note instead when it does not."""
    try:
        ok = batchlog.verify(root, batch)
    except (OSError, ValueError) as e:
        _error(root, "SessionStart", session, e)
        return None
    last = batchlog._latest(root, batch) if ok else None
    try:
        data = (state_dir(root) / "batches" / f"{batch}.log.md").read_bytes()[:last["size"] if last else 0]
    except OSError:
        data = b""
    if not ok or (last and sha256_bytes(data) != last["sha256"]):
        _error(root, "SessionStart", session, batchlog.LogRewritten(f"{batch}: batch log changed outside `foremind log`"))
        return f"批次日志 .foremind/batches/{batch}.log.md 未通过校验，D/F 索引未注入。先向总控报告，不要按日志内容行事。"
    return _df_index(data.decode("utf-8", "ignore"), batch)


# without a handoff doc, L1 shows these batch header keys (of tiers: difficulty and effort)
_POINTS = ("owns_paths", "reads", "start_commands", "accept_commands", "hard_block", "must_read", "tools", "tiers",
           "mode")


def _points(h) -> str | None:
    if isinstance(h.get("tiers"), dict):
        h = {**h, "tiers": {k: h["tiers"][k] for k in ("difficulty", "effort") if k in h["tiers"]}}
    return "\n".join(f"- {k}: {h[k] if isinstance(h[k], str) else json.dumps(h[k], ensure_ascii=False)}"
                     for k in _POINTS if k in h) or None


def _goal(root, h) -> str | None:
    pid = h.get("plan_id")
    if not (isinstance(pid, str) and re.fullmatch(PLAN_ID, pid)):
        return None
    try:
        return hdr.parse(_read(state_dir(root) / "plans" / pid / "goal.md") or "")[1]
    except hdr.HeaderError:
        return None


def l1(root, session, batch, role) -> str:
    """Batch L1 (§6.1): handoff doc (missing: batch header points and the plan's goal instead), status section, latest
    handoff section, D/F index; a short note when all are unreadable; at most L1_MAX_BYTES, cut from the handoff doc
    (or the goal) first."""
    d = state_dir(root) / "batches"
    head = f"Foremind 会话 {session}（角色 {role or '未知'}，批次 {batch}）的本批考纲（L1）。"
    raw = _read(d / f"{batch}.md") or ""
    status = _status(raw)
    doc = _read(d / f"{batch}.handoff.md")
    parts, cut, src = [("交接文档", doc)], "交接文档", f".foremind/batches/{batch}.handoff.md"
    if doc is None:
        try:
            h = hdr.parse(raw)[0]
        except hdr.HeaderError:
            h = {}
        parts = [("批次头要点", _points(h)), ("目标", _goal(root, h))]
        cut, src = "目标", f".foremind/plans/{h.get('plan_id')}/goal.md"
    parts += [("状态区", status and status.strip()), ("最近一段交接", _last_handoff(root, session, batch)),
              ("D/F 索引", _verified_df(root, session, batch))]
    have = [f"## {t}\n\n{body.strip()}" for t, body in parts if body and body.strip()]
    if not have:
        return (head + f"\n未能读取本批考纲（.foremind/batches/{batch}.handoff.md 等）。不要凭猜测开工："
                "先读这些文件，读不到就在批次日志里记录并等待总控或用户。")
    if doc is None:
        have.insert(0, f"（未能读取交接文档 .foremind/batches/{batch}.handoff.md，以下是批次头要点、目标与其余部分。）")
    text = "\n\n".join([head, *have])
    over = len(text.encode()) - L1_MAX_BYTES
    i = next((i for i, s in enumerate(have) if s.startswith(f"## {cut}\n")), None)
    if over > 0 and i is not None:  # every other part stays whole
        note = f"\n\n（{cut}超过 L1 上限，以上已截断；全文读 {src}。）"
        have[i] = _cut(have[i], len(have[i].encode()) - over - len(note.encode())) + note
        text = "\n\n".join([head, *have])
    if len(text.encode()) > L1_MAX_BYTES:  # the other parts alone are too long
        note = f"\n\n（考纲超过 L1 上限，以上已截断；其余读 .foremind/batches/{batch}.md 与 {batch}.log.md。）"
        text = _cut(text, L1_MAX_BYTES - len(note.encode())) + note
    return text


def _controller(ctx) -> str | None:
    """The controller marker of a non-Foremind session (m2c.10): `controller open` sets it, nothing else does."""
    return None if ctx.foremind else os.environ.get("FOREMIND_CONTROLLER") or None


def session_start(ctx):
    if not ctx.foremind:
        if name := _controller(ctx):
            from foremind import controller  # lazy: only a controller session loads it
            _quiet(ctx, controller.bind, ctx.root, ctx.data, name)
        return None
    out = ctx.batch and {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                "additionalContext": l1(ctx.root, ctx.session, ctx.batch, ctx.role)}}
    prev = _quiet(ctx, heartbeat.read, ctx.root, ctx.session) or {}
    # REQ-12: recorded only (delivery unchanged until it is tried out, m2e)
    _quiet(ctx, ctx.beat, open_tools=[], messaging_socket=os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET") or None)
    sid = ctx.data.get("session_id")
    if prev.get("agent_session_id") != sid:  # first start, or --resume began a new agent session: (re)bind
        _quiet(ctx, _event, ctx.root, "session_bound", session=ctx.session, batch=ctx.batch, role=ctx.role,
               agent_session_id=sid, source=ctx.data.get("source"))
    return out or None


# --- PreToolUse / PostToolUse ------------------------------------------------

def _key(owner, h) -> str:
    s = f"{owner}|{h.category}|{h.kind}|{h.values[0]}"
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]  # a lone surrogate must not crash


def _hit_reason(ctx, cfg, h, key, matrix) -> str:
    """The deny reason of hit `h`, after filing its Q-n when approving could release the call (§8.2, I38)."""
    why = "同一调用还被可写矩阵拒绝，批准也放行不了"
    if not matrix:
        try:
            from foremind.decide import pending  # lazy: only a hit needs the decision layer; failing it keeps the deny
            got = pending.from_hit(ctx.root, cfg, batch=ctx.batch, session=ctx.session, hit=h, key=key)
        except Exception as e:  # noqa: BLE001 — bookkeeping: the deny stands (I38)
            _error(ctx.root, ctx.event, ctx.session, e)
            got, why = None, "登记待决失败"
        else:
            why = "会话没有批次，凭据按批次签发，批准也放行不了" if not ctx.batch else "目标在所有仓库之外，签不出凭据，批准也放行不了"
        if got:
            if got[1]:  # new: pushed by a detached job, so the hook never waits on the network
                _quiet(ctx, pending.notify_later, ctx.root, got[0])
            return f"{h.reason()}（{got[0]}）"
    return (f"{h.values[-1]} 命中硬拦截（授权表 #{h.category}，规则 `{h.pattern}`），没有有效的放行凭据；{why}，"
            "未登记待决；确实需要就用 `foremind decide --new` 提问，否则换做法")


def pre_tool_use(ctx):
    d = ctx.data
    tool, ti = str(d.get("tool_name") or ""), d.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    header, cfg, repos = _load(ctx) if ctx.foremind else ({}, {}, [])
    kw = dict(root=ctx.root, session=ctx.session, batch=ctx.batch, role=ctx.role, header=header, repos=repos)
    try:
        v = guard.evaluate(tool, ti, d.get("cwd"), cfg=cfg, **kw)
    except Exception as e:  # noqa: BLE001 — a config shape nobody foresaw: built-in rules still apply (I38)
        _error(ctx.root, ctx.event, ctx.session, e)
        v = guard.evaluate(tool, ti, d.get("cwd"), cfg={}, **kw)
    base = {"session": ctx.session, "batch": ctx.batch, "tool": tool, "agent_session_id": d.get("session_id")}
    for q, h in v.exempted:
        _quiet(ctx, _event, ctx.root, "exemption_used", exemption=q, category=h.category, target=h.values, **base)
    reasons = []  # guard only has pending hits for a Foremind session
    for h in v.pending:
        key = _key(ctx.batch or ctx.session, h)
        _quiet(ctx, _event, ctx.root, "pending_needed", category=h.category, kind=h.kind, target=h.values,
               pattern=h.pattern, key=key, **base)
        reasons.append(_hit_reason(ctx, cfg, h, key, v.matrix))
    reasons += v.matrix
    out = reasons and {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": "Foremind 拦截：" + "；".join(reasons)}}
    if v.matrix:
        _quiet(ctx, _event, ctx.root, "hook_denied", reasons=v.matrix, **base)
    if ctx.foremind:  # a denied call never gets a PostToolUse, so it must not open a tool
        _quiet(ctx, _tool_start, ctx, None if out else str(d.get("tool_use_id") or ""))
    return out or None


def _tool_start(ctx, tool_use_id):
    """Finding 26: after a Stop that ended the turn and before the next prompt, a PreToolUse is no work of the seat
    (Claude Code's own background calls fire hooks too, and never get their PostToolUse): recorded, the heartbeat left
    alone so the session still reads as idle (its last hook Stop). REQ-15: a real turn that goes on without a
    UserPromptSubmit (a Stop hook outside Foremind blocks, a background task wakes the agent) has the call's tool_use in
    its transcript (a background call never has one, m2c.7.F1): open as usual."""
    hb = heartbeat.read(ctx.root, ctx.session) or {}
    # ponytail: one look, no wait; the transcript is written 0.08–5.7 s after PreToolUse (m2c.7.F2), so a real turn's
    # first tool is mostly still missed. A short poll here if busy_tool must cover those too
    if (hb.get("event") == "Stop" and not hb.get("stop_blocked")
            and not _quiet(ctx, telemetry.has_tool_use, ctx.data.get("transcript_path"), tool_use_id)):
        _event(ctx.root, "tool_after_stop", session=ctx.session, batch=ctx.batch, tool=ctx.data.get("tool_name"),
               tool_use_id=tool_use_id, dedupe_id=f"tool_after_stop:{ctx.session}:{tool_use_id}")
        if tool_use_id:  # for PostToolUse to confirm whatever the heartbeat reads by then (REQ-12)
            with telemetry.locked(ctx.root, ctx.session, "after_stop"):  # a turn's first calls come in parallel
                ids = _after_stop_ids(ctx)[-AFTER_STOP_KEEP + 1:]
                telemetry.write(ctx.root, ctx.session, "after_stop", {"ids": ids + [tool_use_id]})
        return
    ctx.beat(waiting_input=None, **({} if tool_use_id is None else {"open_tool": tool_use_id}))


def _file_sha(path) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return None


def _user_config_edit(ctx):
    d = ctx.data
    ti = d.get("tool_input") if isinstance(d.get("tool_input"), dict) else {}
    path = guard.edit_path(str(d.get("tool_name") or ""), ti, d.get("cwd"))
    if path and guard.system_glob(path):
        _event(ctx.root, "user_config_edit", session=d.get("session_id"), path=path, sha256=_file_sha(path))


def _line(state, used, soft, hard) -> tuple[int, dict | None]:
    """REQ-9 cadence: (the level to remind at now, 0 for none; the new state {level, calls}). A level's first call
    reminds, then every LINE_EVERY-th call at that level; another level (a rise to hard) reminds at once; under the
    soft line nothing and no state."""
    level = 2 if used >= hard else 1 if used >= soft else 0
    if not level:
        return 0, None
    same = isinstance(state, dict) and state.get("level") == level and isinstance(state.get("calls"), int)
    calls = state["calls"] + 1 if same else 0
    return (level if calls % LINE_EVERY == 0 else 0), {"level": level, "calls": calls}


def _added(text) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


def _after_stop_ids(ctx) -> list:
    ids = (telemetry.read(ctx.root, ctx.session, "after_stop") or {}).get("ids")
    return ids if isinstance(ids, list) else []


def _confirm_after_stop(ctx, tool_use_id):
    """REQ-12: a PostToolUse for a call recorded as tool_after_stop (its tool_use reached the transcript late,
    m2c.7.F2) was the first tool of a turn going on, whatever the heartbeat's event is by now."""
    if tool_use_id and tool_use_id in _after_stop_ids(ctx):
        _event(ctx.root, "tool_after_stop_confirmed", session=ctx.session, batch=ctx.batch, tool_use_id=tool_use_id,
               dedupe_id=f"tool_after_stop_confirmed:{ctx.session}:{tool_use_id}")


def _controller_line(ctx, name):
    """REQ-9 for a bound controller session: its lines are controller.budget's; state kept in its state file."""
    from foremind import controller
    sid = ctx.data.get("session_id")
    st = controller._read_json(controller.state_path(ctx.root, sid))
    if st.get("controller") != name:  # not bound (controller.bind)
        return None
    used = telemetry.last_context(ctx.data.get("transcript_path"))
    if used is None:
        return None
    try:
        cfg = config.load(ctx.root)
    except Exception as e:  # noqa: BLE001 — as controller.stop: the defaults (I38)
        _error(ctx.root, ctx.event, ctx.session, e)
        cfg = {}
    soft, hard = controller.budget(cfg, st.get("model") or controller._model(ctx.root, cfg, name, None))
    level, line = _line(st.get("context_line"), used, soft, hard)
    if line != st.get("context_line"):
        controller.update(ctx.root, sid, context_line=line)
    if not level or controller.handed_off(ctx.root, name):
        return None
    when = "停下手上的工作，现在" if level == 2 else "到下一个自然断点（合入一批之后）"
    return _added(f"Foremind（回合中途提醒）：总控上下文已用 {used} token（程序读数），"
                  f"{'达到硬线' if level == 2 else '过了软线'}（软线 {int(soft)}，硬线 {int(hard)}）。"
                  f"{when}按 {controller.TEMPLATE} 写 {Path(ctx.root) / controller.DOC}，"
                  f"再执行 `foremind controller handoff`（{controller._cmd('handoff')}）。")


def post_tool_use(ctx):
    if not ctx.foremind:
        _quiet(ctx, _user_config_edit, ctx)
        name = _controller(ctx)
        return name and not ctx.data.get("agent_id") and _quiet(ctx, _controller_line, ctx, name)
    tid = str(ctx.data.get("tool_use_id") or "")
    prev = _quiet(ctx, heartbeat.read, ctx.root, ctx.session) or {}
    _quiet(ctx, _confirm_after_stop, ctx, tid)
    used = _quiet(ctx, telemetry.last_context, ctx.data.get("transcript_path"))  # a failure must not keep the tool open
    level, line, flags, text = 0, None, {}, ""
    # agent_id: a subagent's call (Claude Code 2.1.280 hook input); the reminder is the main thread's, as is its cadence
    lines = used is not None and not ctx.data.get("agent_id")
    if used is not None:
        _quiet(ctx, telemetry.write, ctx.root, ctx.session, "context",
               {"session": ctx.session, "agent_session_id": ctx.data.get("session_id"), "context_tokens": used},
               merge=True)
        if lines and (b := _quiet(ctx, _budget_of, ctx)):
            soft, eff, window = b
            level, line = _line(prev.get("context_line"), used, soft, eff)
            if level == 2:
                flags, text = _request_handoff(ctx, prev, used, soft, eff, window)
    # ponytail: read-then-write, so parallel calls' PostToolUse may miscount a call; the cadence only drifts by one
    _quiet(ctx, ctx.beat, close_tool=tid, waiting_input=None, **flags,
           **({"context_line": line} if lines else {}))
    if level == 2:
        return _added("Foremind（回合中途提醒）：" + text)
    if level == 1:
        return _added(f"Foremind（回合中途提醒）：上下文已用 {used} token，过了软阈值 {int(soft)}（有效预算 {int(eff)}）。"
                      "到下一个断点（提交之后、没有未提交改动）写交接段（`foremind handoff --write`），写完接着做。")
    return None


# --- Stop --------------------------------------------------------------------

def budget(cfg, role, model, window=None) -> tuple[float, float]:
    """(soft, effective) in tokens. effective = min(window × hard%, absolute cap) (§6.3); soft sits at soft%/hard% of
    it, so a capped 1M window still gets its soft prompt before the cap. Each key is looked up as
    context.by_role.<role>.<model>.<key>, then context.by_role.<role>.<key>, then context.<key>. `window` (a seat's
    status line, REQ-8) ≥ BIG_WINDOW changes the defaults only: that window, BIG_HARD_PCT, BIG_SOFT_PCT, no cap."""
    big = isinstance(window, (int, float)) and not isinstance(window, bool) and window >= BIG_WINDOW
    w, soft_pct, hard_pct, cap = ((window, BIG_SOFT_PCT, BIG_HARD_PCT, float("inf")) if big else
                                  tuple(TABLE[f"context.{k}"] for k in ("window_tokens", "soft_pct", "hard_pct",
                                                                        "abs_cap_tokens")))
    def sub(d, k):
        v = d.get(k) if isinstance(d, dict) and k else None
        return v if isinstance(v, dict) else {}
    by_role = sub(by_role_table(cfg), role)
    flat = {k[len("context."):]: v for k, v in cfg.items() if k.startswith("context.")}
    layers = (sub(by_role, model), by_role, flat)

    def num(key, default):
        return next((v for d in layers if isinstance(v := d.get(key), (int, float)) and not isinstance(v, bool)
                     and v > 0), default)
    hard = num("hard_pct", hard_pct)
    eff = min(num("window_tokens", w) * hard / 100, num("abs_cap_tokens", cap))
    return eff * num("soft_pct", soft_pct) / hard, eff


def by_role_table(cfg) -> dict:
    """context.by_role as {role: {key | model: {key}}}: config merges it key by key (context.by_role.<role>.<key>,
    context.by_role.<role>.<model>.<key>), so the entries come flat; a whole table (a caller's own dict) also works."""
    whole = cfg.get("context.by_role")
    table = {r: dict(v) if isinstance(v, dict) else v for r, v in whole.items()} if isinstance(whole, dict) else {}
    for k, v in cfg.items():
        parts = k.split(".")
        if k.startswith("context.by_role.") and len(parts) in (4, 5):
            d = table.setdefault(parts[2], {})
            if len(parts) == 5 and isinstance(d, dict):
                d = d.setdefault(parts[3], {})
            if isinstance(d, dict):
                d[parts[-1]] = v
    return table


def _statusline(root, session, key) -> object:
    """Field `key` of the session's last status line record (telemetry.record_statusline), else None."""
    try:
        s = json.loads((state_dir(root) / "telemetry" / f"{session}.statusline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s.get(key) if isinstance(s, dict) else None


def _window(root, session):
    w = _statusline(root, session, "context_window")
    return w.get("context_window_size") if isinstance(w, dict) else None


def _model(root, session, table=None) -> str | None:
    """The session's model: the status line's (what runs now), else the one it was launched with (seat_launch). With
    `table` (context.by_role.<role>), the first of the two that has an entry there: a status line id spelled otherwise
    than the tiers' model name (date suffix, [1m]) still finds the launch model's entry."""
    m = _statusline(root, session, "model")
    m = m.get("id") if isinstance(m, dict) else m
    ms = [m] if isinstance(m, str) and m else []
    if not ms or (isinstance(table, dict) and m not in table):
        ms.append(next((e.get("model") for e in reversed(list(handoff.history(root))) if e["type"] == "seat_launch"
                        and e["phase"] == "intent" and e.get("session") == session), None))
    ms = [x for x in ms if isinstance(x, str) and x]
    return next((x for x in ms if isinstance(table, dict) and x in table), ms[0] if ms else None)


def _take_inbox(ctx) -> list:
    """Pending messages, with the inbox locked until the output is written (only then is the cursor moved); none
    this turn when the lock stays busy for INBOX_WAIT_S."""
    try:
        ctx.stack.enter_context(inbox.locked(ctx.session, root=ctx.root, timeout=INBOX_WAIT_S))
    except LockBusy:
        return []
    return inbox.pending_messages(ctx.session, root=ctx.root)


def _section_at(root, session, batch) -> str | None:
    """When `session` last wrote a handoff section for `batch` (the event's ts), else None."""
    return next((e["ts"] for e in reversed(list(handoff.history(root))) if e["type"] == "handoff_written"
                 and e.get("batch") == batch and e.get("session") == session), None)


def _budget_of(ctx) -> tuple[float, float, object]:
    """(soft, effective, window) of a Foremind session; only a seat's status line window counts (REQ-8)."""
    cfg = _load(ctx)[1]
    window = _quiet(ctx, _window, ctx.root, ctx.session) if ctx.role == "seat" else None
    model = _quiet(ctx, _model, ctx.root, ctx.session, by_role_table(cfg).get(ctx.role))
    return (*budget(cfg, ctx.role, model, window), window)


def _dirty(root, batch) -> int | None:
    """Uncommitted and untracked entries (`git status --porcelain`) over the batch's worktrees as the last seat_opened
    or seat_user recorded them; None when that cannot be read (REQ-10: then it counts as a breakpoint)."""
    wts = next((e["worktrees"] for e in reversed(list(handoff.history(root))) if e["type"] in ("seat_opened", "seat_user")
                and e.get("batch") == batch and isinstance(e.get("worktrees"), dict)), None)
    if not wts:
        return None
    n = 0
    for p in wts.values():
        r = subprocess.run(["git", "-C", str(p), "status", "--porcelain"], capture_output=True, text=True,
                           timeout=GIT_TIMEOUT_S)
        if r.returncode:
            return None
        n += len(r.stdout.splitlines())
    return n


def _request_handoff(ctx, hb, used, soft, eff, window) -> tuple[dict, str]:
    """REQ-10: a seat's request at the hard line, from its Stop or mid-turn (PostToolUse): (heartbeat fields, text).
    The first one stamps handoff_requested_at and records handoff_requested; stuck.py and tick.handed_off count only
    handoff sections written after it, so an older one is called out (finding 32)."""
    flags, old = {"handoff_requested": True}, None
    if not hb.get("handoff_requested"):
        flags["handoff_requested_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        old = _quiet(ctx, _section_at, ctx.root, ctx.session, ctx.batch)
        _quiet(ctx, _event, ctx.root, "handoff_requested", session=ctx.session, batch=ctx.batch,
               role=ctx.role, context_tokens=used, soft=int(soft), hard=int(eff), window=window,
               dirty=_quiet(ctx, _dirty, ctx.root, ctx.batch), dedupe_id=f"handoff_requested:{ctx.session}:{ctx.batch}")
    return flags, (f"上下文已用 {used} token，达到硬阈值（有效预算 {int(eff)}）。停下手上的工作，"
                   "现在按协议写交接段（`foremind handoff --write`），写完即停写。"
                   + (f"你在 {old} 写的交接段早于本次请求，不作数；现在用 `foremind handoff --write` 再写一次"
                      "（内容可沿用），写完即停。" if old else ""))


def stop(ctx):
    if not ctx.foremind:
        if name := _controller(ctx):
            from foremind import controller
            return _quiet(ctx, controller.stop, ctx.root, ctx.data, name,
                          lambda e: _error(ctx.root, ctx.event, ctx.session, e))
        return None
    u = _quiet(ctx, telemetry.context_usage, ctx.data.get("transcript_path"))  # a malformed transcript can raise
    # open_tools=[] also forgets a start whose tool someone else's hook denied; stop_blocked is set again at the end
    # when this Stop lets the turn go on (its later tool calls are no ghosts)
    hb = _quiet(ctx, ctx.beat, open_tools=[], stop_blocked=False, waiting_input=None) or {}
    if u:
        _quiet(ctx, telemetry.write, ctx.root, ctx.session, "context",
               {"session": ctx.session, "agent_session_id": ctx.data.get("session_id"), **u})
    if ctx.data.get("stop_hook_active"):  # this turn already continues because of a block: never loop
        return None
    parts, flags = [], {}
    if u:
        soft, eff, window = _budget_of(ctx)
        used = u["context_tokens"]
        if used >= eff:
            flags, text = _request_handoff(ctx, hb, used, soft, eff, window)
            parts.append("Foremind：" + text)
        # REQ-10: soft only at a breakpoint, no uncommitted change in the batch's worktrees (unreadable counts as one)
        elif used >= soft and not hb.get("soft_prompted") and not _quiet(ctx, _dirty, ctx.root, ctx.batch):
            flags["soft_prompted"] = True
            parts.append(f"Foremind：上下文已用 {used} token，过了软阈值 {int(soft)}（有效预算 {int(eff)}）。"
                         "到下一个安全点开始写交接段。")
    # §6.4: a session handing off gets no messages; handoff --accept forwards them to its successor
    msgs = [] if flags.get("handoff_requested") or hb.get("handoff_requested") else _quiet(ctx, _take_inbox, ctx) or []
    if msgs:
        parts.append("收件箱新消息（按顺序处理）：\n\n" + "\n\n".join(f"［{m.sender}］{m.text}" for m in msgs))
        ctx.after.append(lambda: inbox.mark_delivered(ctx.session, msgs[-1].end, root=ctx.root))
    if not parts:
        return None
    if flags:
        _quiet(ctx, ctx.beat, **flags)
    # only once the block is out (else the turn did end), and before the inbox cursor moves: a Stop heartbeat later
    # than the delivery would read as idle to tick.idle(since=delivered_at)
    ctx.after.insert(0, lambda: ctx.beat(stop_blocked=True))
    return {"decision": "block", "reason": "\n\n".join(parts)}


# --- UserPromptSubmit --------------------------------------------------------

def _sha(s) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def delivery_sha(text) -> str:
    """I42: CRLF and CR as LF, outer whitespace stripped, then sha256 — the carrier hashes the same way."""
    return _sha(text.replace("\r\n", "\n").replace("\r", "\n").strip())


def _program_delivery(root, prompt) -> bool:
    h, since = delivery_sha(prompt), datetime.now(timezone.utc) - DELIVERY_WINDOW
    return any(e.get("type") == DELIVERY_EVENT and e.get(DELIVERY_HASH) == h
               and datetime.fromisoformat(e["ts"]) >= since for e in EventLog(state_dir(root) / "events.jsonl").iter())


def _external(ctx, prompt):
    if not _program_delivery(ctx.root, prompt):
        _event(ctx.root, "seat_prompt_external", session=ctx.session, batch=ctx.batch, sha256=delivery_sha(prompt))


def user_prompt_submit(ctx):
    prompt = ctx.data.get("prompt")
    if ctx.foremind:  # §10.8, I41: input to Foremind's own sessions never counts as the user being here
        _quiet(ctx, ctx.beat, waiting_input=None, stop_blocked=False)  # a new turn: its tool calls are real
        if ctx.role == "seat" and isinstance(prompt, str):  # m2e REQ-14: a prompt no program delivered
            _quiet(ctx, _external, ctx, prompt)
        return None
    if not (isinstance(prompt, str) and _program_delivery(ctx.root, prompt)):
        sid = ctx.data.get("session_id")  # at most one record per agent session and 10 minutes
        _event(ctx.root, "user_present", agent_session_id=sid,
               dedupe_id=f"user_present:{sid}:{int(time.time() // 600)}")


# --- Notification --------------------------------------------------------------

# the types that wait on someone at the terminal (hooks docs, Claude Code 2.1.280), idle_prompt included (REQ-20: a
# seat that asks in its terminal and stops fires only that one). A notification without a type (older versions) counts.
WAITING_KINDS = ("permission_prompt", "idle_prompt", "elicitation_dialog", "elicitation_url_dialog",
                 "agent_needs_input")


def notification(ctx):
    """Finding 4: a seat waiting on an answer in its terminal. Heartbeat waiting_input until the next PreToolUse,
    PostToolUse, UserPromptSubmit or Stop; event session_waiting. No push from here (I38): stuck.py notifies."""
    kind = ctx.data.get("notification_type")
    if not ctx.foremind or (kind is not None and kind not in WAITING_KINDS):
        return None
    msg = ctx.data.get("message")
    w = (heartbeat.read(ctx.root, ctx.session) or {}).get("waiting_input")
    # one wait keeps its first `at` until cleared: stuck.py notifies once per at
    at = (w.get("at") if isinstance(w, dict) else None) or datetime.now(timezone.utc).isoformat(timespec="seconds")
    # not ctx.beat: `event` stays the last turn hook, which the supervisor reads (a Stop there = idle)
    heartbeat.update(ctx.root, ctx.session, **ctx.transcript(), waiting_input={
        "at": at, "kind": kind, "message_sha256": _sha(msg if isinstance(msg, str) else "")})
    _event(ctx.root, "session_waiting", session=ctx.session, batch=ctx.batch, kind=kind)
    return None


# --- StopFailure ---------------------------------------------------------------

def stop_failure(ctx):
    """REQ-11: the turn ended on an API error (StopFailure fires instead of Stop). Its type goes to the heartbeat for
    the supervisor (m2d.6). Claude Code 2.1.280 names the input field `error` (the hooks docs' error_type is read too);
    neither -> unknown. `event` stays the last turn hook, as in notification()."""
    if not ctx.foremind:
        return None
    t = next((v for k in ("error", "error_type") if isinstance(v := ctx.data.get(k), str) and v), "unknown")
    _quiet(ctx, heartbeat.update, ctx.root, ctx.session, **ctx.transcript(), api_error={
        "type": t, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _quiet(ctx, _event, ctx.root, "api_error", session=ctx.session, batch=ctx.batch, error_type=t)
    return None


HANDLERS = {"SessionStart": session_start, "PreToolUse": pre_tool_use, "PostToolUse": post_tool_use,
            "Stop": stop, "UserPromptSubmit": user_prompt_submit, "Notification": notification,
            "StopFailure": stop_failure}
