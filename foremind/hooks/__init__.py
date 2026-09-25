"""Claude Code hooks behind `foremind hook <event>` (DESIGN §1.1, §6.1–6.4, §8.2, §10.3, §10.8, §20 I31 I35–I42).

main() never raises and never blocks because of its own failure: an internal error is logged as a `hook_error`
event and the call goes through. Only explicit rules block: PreToolUse with JSON permissionDecision "deny" (never
exit code 2, which echoes the hook command line to the model), Stop with decision "block" unless the input's
stop_hook_active is already true.
Failure boundaries (I38): each handler decides first and keeps its books after (events, heartbeat, telemetry, inbox
cursor); a bookkeeping failure is logged and never changes the decision. Unreadable config, environment or batch
header degrade the checks that depend on them, not the whole hook.
A Foremind session has FOREMIND_SESSION (plus FOREMIND_BATCH, FOREMIND_ROLE, FOREMIND_PROJECT); other sessions get
only the `.foremind/` guard, presence records and `user_config_edit` records of their #22 edits (I51). Outside
every Foremind project the hooks do nothing (I41).

Events: session_bound; pending_needed (hard-block hit without an exemption; M2-1 turns it into a pending);
hook_denied (writable-matrix denial); exemption_used; user_present; user_config_edit (a non-Foremind session's
Edit/Write of a #22 file, with the file's sha256 after the write, I51); hook_error.
"""
import contextlib
import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from foremind import batchlog, config, handoff, heartbeat, inbox, lock, telemetry
from foremind import header as hdr
from foremind import repos as repos_mod
from foremind.events import EventLog
from foremind.hooks import guard
from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir
from foremind.schemas import BATCH_ID

# §6.3 defaults (【经验值】); config keys context.window_tokens / soft_pct / hard_pct / abs_cap_tokens override
WINDOW_TOKENS, SOFT_PCT, HARD_PCT, ABS_CAP_TOKENS = 200_000, 65, 80, 180_000
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
                                agent_session_id=self.data.get("session_id"), event=self.event, **fields)


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


_STATUS = re.compile(r"^## 状态[^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)


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


def l1(root, session, batch, role) -> str:
    """Batch L1 (§6.1): handoff doc, status section, latest handoff section, D/F index; a short note when unreadable;
    at most L1_MAX_BYTES, cut from the handoff doc first."""
    d = state_dir(root) / "batches"
    head = f"Foremind 会话 {session}（角色 {role or '未知'}，批次 {batch}）的本批考纲（L1）。"
    status = _STATUS.search(_read(d / f"{batch}.md") or "")
    parts = [("交接文档", _read(d / f"{batch}.handoff.md")), ("状态区", status and status.group(1).strip()),
             ("最近一段交接", _last_handoff(root, session, batch)),
             ("D/F 索引", _df_index(_read(d / f"{batch}.log.md") or "", batch))]
    have = [f"## {t}\n\n{body.strip()}" for t, body in parts if body and body.strip()]
    if not have:
        return (head + f"\n未能读取本批考纲（.foremind/batches/{batch}.handoff.md 等）。不要凭猜测开工："
                "先读这些文件，读不到就在批次日志里记录并等待总控或用户。")
    if parts[0][1] is None:
        have.insert(0, f"（未能读取交接文档 .foremind/batches/{batch}.handoff.md，以下只是其余部分。）")
    text = "\n\n".join([head, *have])
    over = len(text.encode()) - L1_MAX_BYTES
    if over > 0 and have[0].startswith("## 交接文档"):  # status, latest handoff and D/F index stay whole
        note = f"\n\n（交接文档超过 L1 上限，以上已截断；全文读 .foremind/batches/{batch}.handoff.md。）"
        have[0] = _cut(have[0], len(have[0].encode()) - over - len(note.encode())) + note
        text = "\n\n".join([head, *have])
    if len(text.encode()) > L1_MAX_BYTES:  # the other parts alone are too long
        note = f"\n\n（考纲超过 L1 上限，以上已截断；其余读 .foremind/batches/{batch}.md 与 {batch}.log.md。）"
        text = _cut(text, L1_MAX_BYTES - len(note.encode())) + note
    return text


def session_start(ctx):
    if not ctx.foremind:
        return None
    out = ctx.batch and {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                "additionalContext": l1(ctx.root, ctx.session, ctx.batch, ctx.role)}}
    prev = _quiet(ctx, heartbeat.read, ctx.root, ctx.session) or {}
    _quiet(ctx, ctx.beat, open_tools=[])
    sid = ctx.data.get("session_id")
    if prev.get("agent_session_id") != sid:  # first start, or --resume began a new agent session: (re)bind
        _quiet(ctx, _event, ctx.root, "session_bound", session=ctx.session, batch=ctx.batch, role=ctx.role,
               agent_session_id=sid, source=ctx.data.get("source"))
    return out or None


# --- PreToolUse / PostToolUse ------------------------------------------------

def _key(owner, h) -> str:
    s = f"{owner}|{h.category}|{h.kind}|{h.values[0]}"
    return hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:16]  # a lone surrogate must not crash


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
    out = v.reasons and {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                "permissionDecisionReason": "Foremind 拦截：" + "；".join(v.reasons)}}
    base = {"session": ctx.session, "batch": ctx.batch, "tool": tool, "agent_session_id": d.get("session_id")}
    for q, h in v.exempted:
        _quiet(ctx, _event, ctx.root, "exemption_used", exemption=q, category=h.category, target=h.values, **base)
    if v.foremind:
        for h in v.pending:
            _quiet(ctx, _event, ctx.root, "pending_needed", category=h.category, kind=h.kind, target=h.values,
                   pattern=h.pattern, key=_key(ctx.batch or ctx.session, h), **base)
    if v.matrix:
        _quiet(ctx, _event, ctx.root, "hook_denied", reasons=v.matrix, **base)
    if ctx.foremind:  # a denied call never gets a PostToolUse, so it must not open a tool
        _quiet(ctx, ctx.beat, **({} if out else {"open_tool": str(d.get("tool_use_id") or "")}))
    return out or None


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


def post_tool_use(ctx):
    if not ctx.foremind:
        _quiet(ctx, _user_config_edit, ctx)
    else:
        _quiet(ctx, ctx.beat, close_tool=str(ctx.data.get("tool_use_id") or ""))
        used = telemetry.last_context(ctx.data.get("transcript_path"))
        if used is not None:
            _quiet(ctx, telemetry.write, ctx.root, ctx.session, "context",
                   {"session": ctx.session, "agent_session_id": ctx.data.get("session_id"), "context_tokens": used},
                   merge=True)


# --- Stop --------------------------------------------------------------------

def budget(cfg) -> tuple[float, float]:
    """(soft, effective) in tokens. effective = min(window × hard%, absolute cap) (§6.3); soft sits at soft%/hard% of
    it, so a capped 1M window still gets its soft prompt before the cap."""
    def num(key, default):
        v = cfg.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else default
    hard = num("context.hard_pct", HARD_PCT)
    eff = min(num("context.window_tokens", WINDOW_TOKENS) * hard / 100, num("context.abs_cap_tokens", ABS_CAP_TOKENS))
    return eff * num("context.soft_pct", SOFT_PCT) / hard, eff


def _take_inbox(ctx) -> list:
    """Pending messages, with the inbox locked until the output is written (only then is the cursor moved)."""
    ctx.stack.enter_context(inbox.locked(ctx.session, root=ctx.root))
    return inbox.pending_messages(ctx.session, root=ctx.root)


def stop(ctx):
    if not ctx.foremind:
        return None
    u = telemetry.context_usage(ctx.data.get("transcript_path"))
    hb = _quiet(ctx, ctx.beat, open_tools=[]) or {}  # also forgets a start whose tool someone else's hook denied
    if u:
        _quiet(ctx, telemetry.write, ctx.root, ctx.session, "context",
               {"session": ctx.session, "agent_session_id": ctx.data.get("session_id"), **u})
    if ctx.data.get("stop_hook_active"):  # this turn already continues because of a block: never loop
        return None
    parts, flags = [], {}
    if u:
        used, (soft, eff) = u["context_tokens"], budget(_load(ctx)[1])
        if used >= eff:
            flags["handoff_requested"] = True
            parts.append(f"Foremind：上下文已用 {used} token，达到硬阈值（有效预算 {int(eff)}）。停下手上的工作，"
                         "现在按协议写交接段（`foremind handoff --write`），写完即停写。")
        elif used >= soft and not hb.get("soft_prompted"):
            flags["soft_prompted"] = True
            parts.append(f"Foremind：上下文已用 {used} token，过了软阈值 {int(soft)}（有效预算 {int(eff)}）。"
                         "到下一个安全点开始写交接段。")
    msgs = _quiet(ctx, _take_inbox, ctx) or []
    if msgs:
        parts.append("收件箱新消息（按顺序处理）：\n\n" + "\n\n".join(f"［{m.sender}］{m.text}" for m in msgs))
        ctx.after.append(lambda: inbox.mark_delivered(ctx.session, msgs[-1].end, root=ctx.root))
    if not parts:
        return None
    if flags:
        _quiet(ctx, ctx.beat, **flags)
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


def user_prompt_submit(ctx):
    if ctx.foremind:  # §10.8, I41: input to Foremind's own sessions never counts as the user being here
        return None
    prompt = ctx.data.get("prompt")
    if not (isinstance(prompt, str) and _program_delivery(ctx.root, prompt)):
        _event(ctx.root, "user_present", session=ctx.session, agent_session_id=ctx.data.get("session_id"))


HANDLERS = {"SessionStart": session_start, "PreToolUse": pre_tool_use, "PostToolUse": post_tool_use,
            "Stop": stop, "UserPromptSubmit": user_prompt_submit}
