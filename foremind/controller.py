"""The interactive controller's context gate, fixed handoff and takeover check (REQ-22, DESIGN §6.3, §6.4).

`controller open` launches the controller with FOREMIND_ROLE=controller and MARKER=<its session name>, never
FOREMIND_SESSION: guard, inbox, heartbeat, L1 and stuck detection treat it as any other non-Foremind session. Only
the hooks' controller branches look at MARKER: SessionStart binds (bind), Stop gates on the context the program reads
from the transcript (stop). State per agent session: `.foremind/controller/<agent_session_id>.json` (STATE_KEYS).

Events: controller_launch (session, model, effort, predecessor: written under the project lock, it takes the name),
controller_opened, controller_handoff, controller_takeover, controller_closed (m2d.1: the predecessor closed after a
takeover check with every item ok), controller_slip.
"""
import contextlib
import fnmatch
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from foremind import config as cfg_mod
from foremind import header as hdr
from foremind import carriers, heartbeat, seat, sessions, telemetry
from foremind.carriers import SessionExists
from foremind.events import EventLog
from foremind.fsutil import atomic_write, file_lock, project_lock, sha256_bytes
from foremind.handoff import history
from foremind.paths import state_dir
from foremind.schemas import BATCH_ID
from foremind.vendors import claude as claude_vendor
from foremind.vendors import get as get_vendor

MARKER = "FOREMIND_CONTROLLER"
SOFT, HARD = 200_000, 300_000  # 2026-09-28 measured
# what a key context.by_role.controller.* leaves unset takes (never the seats' context.<key>): 1M × 30% = HARD, soft at
# 20/30 of it = SOFT, no cap
DEFAULTS = {"window_tokens": 1_000_000, "hard_pct": 30, "soft_pct": 20, "abs_cap_tokens": float("inf")}
MODEL, EFFORT = "claude-opus-5-5[1m]", "high"  # routes.controller_live.model / effort
DOC = "HANDOFF-controller.md"  # in the project root
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "controller-handoff.md"
SECTIONS = ("1. 目标与核对", "2. 当前状态", "3. 流程与工具", "4. 决定与教训", "5. 未完成义务", "6. 指针")
SLIPS = ("ruling_withdrawn", "missed_event", "wrong_fact", "other")
# `batch <glob>` besides these; `test` is optional and the only one that runs a command
CHECK_KEYS = ("main_contains", "worktree_clean", "pending", "supervisor", "test")
TEST_TIMEOUT_S = 3600
# STATE_KEYS: controller (MARKER), transcript_path, started_at, updated_at, context_tokens, model; soft_prompted,
# merged_seen (id of the last `merged` batch_state seen when prompting); hard_requested_at; pid, lstart (its claude,
# bind: what sessions.live knows it by)
_READING = re.compile(r"^写交接时上下文.*（程序读.*）。\n", re.M)
_CHECK_BLOCK = re.compile(r"^```foremind-check[ \t]*\n(.*?)^```", re.S | re.M)


class ControllerError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _events(root):
    return EventLog(state_dir(root) / "events.jsonl")


# --- state and usage ----------------------------------------------------------

def state_path(root, agent_session_id) -> Path:
    return state_dir(root) / "controller" / f"{heartbeat.check_name(agent_session_id)}.json"


def _read_json(p) -> dict:
    try:
        v = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def update(root, agent_session_id, **fields) -> dict:
    """Merge `fields` into the session's state (locked, atomic) and stamp updated_at."""
    p = state_path(root, agent_session_id)
    with file_lock(p.with_suffix(".lock")):
        st = _read_json(p) or {"agent_session_id": agent_session_id, "started_at": _now()}
        st.update(fields, updated_at=_now())
        atomic_write(p, json.dumps(st, ensure_ascii=False, sort_keys=True) + "\n")
    return st


def _states(root, name=None) -> list[dict]:
    """The bound states of controller `name` (of every controller when None)."""
    return [st for p in (state_dir(root) / "controller").glob("*.json") if (st := _read_json(p))
            and isinstance(st.get("controller"), str) and (name is None or st["controller"] == name)]


def current(root, name=None) -> dict | None:
    """The last updated state of controller `name` (MARKER in the environment when None), else of any controller not
    handed off (m2e REQ-6: a predecessor's Stop hook may update its state after the successor's)."""
    if name := name or os.environ.get(MARKER) or None:
        sts = _states(root, name)
    else:
        gone = {e.get("from_session") for e in history(root) if e["type"] == "controller_handoff"}
        sts = [st for st in _states(root) if st["controller"] not in gone]
    return max(sts, key=lambda s: str(s.get("updated_at", ""))) if sts else None


def usage(transcript) -> dict | None:
    """The transcript's main chain as telemetry.context_usage counts it (records deduped by message.id, the last of
    each kept): context_tokens of the last request, calls, the four token sums and the last request's model."""
    if not (f := telemetry._open(transcript)):
        return None
    by_id, last, line_last = {}, None, None
    with f:
        for line in f:
            if r := telemetry._record(line):
                by_id[r[0] or f"#{len(by_id)}"] = last = r[1]
                line_last = line
    if last is None:
        return None
    msg = json.loads(line_last).get("message")
    s = lambda k: sum(telemetry._n(u, k) for u in by_id.values())  # noqa: E731
    return {"context_tokens": sum(telemetry._n(last, k) for k in telemetry._CONTEXT_KEYS), "calls": len(by_id),
            "input_tokens": s("input_tokens"), "cache_write_tokens": s("cache_creation_input_tokens"),
            "cache_read_tokens": s("cache_read_input_tokens"), "output_tokens": s("output_tokens"),
            "model": msg.get("model") if isinstance(msg.get("model"), str) else None}


USAGE_KEYS = ("context_tokens", "calls", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens")


def _split(u) -> dict:
    return {k: (u or {}).get(k) for k in USAGE_KEYS}


def budget(cfg, model) -> tuple[float, float]:
    """(soft, hard) in tokens: hooks.budget's formula and lookup over context.by_role.controller.<model>.<key>, then
    context.by_role.controller.<key>, then DEFAULTS (SOFT / HARD when nothing is set)."""
    from foremind import hooks
    own = {k: v for k, v in cfg.items() if k.startswith("context.by_role")}
    return hooks.budget({**own, **{f"context.{k}": v for k, v in DEFAULTS.items()}}, "controller", model)


def _launch(root, name) -> dict:
    return next((e for e in reversed(list(history(root))) if e["type"] == "controller_launch"
                 and e.get("session") == name), {})


def _model(root, cfg, name, seen) -> str | None:
    """The transcript's model, else the launch's; the first of them with a context.by_role.controller entry (as
    hooks._model: the transcript may spell it otherwise, a date suffix or no [1m])."""
    from foremind import hooks
    t = hooks.by_role_table(cfg).get("controller")
    ms = [m for m in (seen, _launch(root, name).get("model")) if isinstance(m, str) and m]
    return next((m for m in ms if isinstance(t, dict) and m in t), ms[0] if ms else None)


def _last_merged(root) -> dict | None:
    """The last merge: batch_state (review writes state, seat/tick to) or reconciled (land, the tick's catch-up)."""
    return next((e for e in reversed(list(history(root))) if e["type"] in ("batch_state", "reconciled")
                 and e.get("to", e.get("state")) == "merged"), None)


def handed_off(root, name) -> bool:
    return any(e["type"] == "controller_handoff" and e.get("from_session") == name for e in history(root))


# --- hooks ----------------------------------------------------------------------

def _claude(ps=None) -> tuple:
    """(the nearest process at or above this one whose argv[0] is claude, [(pid, lstart) of those above it]); (None, [])
    when there is none or ps cannot tell."""
    try:
        procs = sessions.snapshot(ps)
    except sessions.SessionsError:
        return None, []
    chain = [p for q in (os.getpid(), *sessions.ancestors(procs, os.getpid())) if (p := procs.get(q))]
    for i, p in enumerate(chain):
        if os.path.basename(p.command.split(" ", 1)[0]) == "claude":
            return p, [(a.pid, a.lstart) for a in chain[i + 1:]]
    return None, []


def bind(root, data, name) -> None:
    """SessionStart of a controller session: its state file (what `open` waits for), with its claude's pid and lstart
    (sessions.live, `controller check` closing it later). Left unbound, so its Stop is not gated and current() never
    reads it: a fresh start (source startup) under a name already bound, a claude started from the controller's shell
    that inherits MARKER; and (m2c.10 r2 note 2) any claude with a bound controller's claude above it, resumed or
    continued (-c) there."""
    sid = data.get("session_id")
    me, above = _claude()  # before the lock: ps is slow
    with file_lock(state_dir(root) / "controller" / f"{heartbeat.check_name(name)}.lock"):
        # ponytail: the controller itself restarted fresh in the same shell is ungated too; --resume / /clear bind
        if data.get("source") == "startup" and any(s.get("agent_session_id") != sid for s in _states(root, name)):
            return
        if any((s.get("pid"), s.get("lstart")) in above for s in _states(root)):
            return
        update(root, sid, controller=name, transcript_path=data.get("transcript_path"), source=data.get("source"),
               **({"pid": me.pid, "lstart": me.lstart} if me else {}))


def _cmd(*args) -> str:
    return claude_vendor.foremind_command("controller", *args)


def stop(root, data, name, error) -> dict | None:
    """Stop of controller `name`: soft prompt once, again after each new `merged`; block at the hard line until a
    controller_handoff of this session is recorded; never when stop_hook_active. `error(exc)` records a hook_error."""
    sid = data.get("session_id")
    if _read_json(state_path(root, sid)).get("controller") != name:
        return None  # not bound at its SessionStart (bind)
    u = usage(data.get("transcript_path"))
    if u is None:  # the message names the agent session: _error dedupes it to one hook_error per session
        error(ControllerError(f"controller {name} ({sid}): no context reading in its transcript, not gated"))
        return None
    try:
        cfg = cfg_mod.load(root)
    except Exception as e:  # noqa: BLE001 — an unreadable config gates on the defaults (I38)
        error(e)
        cfg = {}
    model = _model(root, cfg, name, u["model"])
    st = update(root, sid, controller=name, transcript_path=data.get("transcript_path"),
                context_tokens=u["context_tokens"], model=model)
    if data.get("stop_hook_active") or handed_off(root, name):
        return None
    soft, hard = budget(cfg, model)
    used = u["context_tokens"]
    # once asked, every Stop blocks until the handoff: /compact or /clear (another agent session) does not undo it
    asked = min((s["hard_requested_at"] for s in _states(root, name) if s.get("hard_requested_at")), default=None)
    if used >= hard or asked:
        if not st.get("hard_requested_at"):
            update(root, sid, hard_requested_at=asked or _now())
        line = (f"达到硬线（有效预算 {int(hard)}）" if used >= hard else
                f"硬线（有效预算 {int(hard)}）已在 {asked} 要求交接，压缩或清空上下文不解除")
        return {"decision": "block", "reason": (
            f"Foremind：总控上下文已用 {used} token（程序读数），{line}。停下手上的工作，"
            f"按 {TEMPLATE} 写 {Path(root) / DOC}，然后执行 `foremind controller handoff`（{_cmd('handoff')}）。")}
    if used < soft:
        return None
    merged = _last_merged(root)
    mid = merged and merged["id"]
    if st.get("soft_prompted") and mid == st.get("merged_seen"):
        return None
    again = f"又合入了 {merged.get('batch')}（自然断点）。" if st.get("soft_prompted") and merged else ""
    update(root, sid, soft_prompted=True, merged_seen=mid)
    return {"decision": "block", "reason": (
        f"Foremind：{again}总控上下文已用 {used} token（程序读数），过了软线 {int(soft)}（硬线 {int(hard)}）。"
        f"到下一个自然断点（合入一批之后）按 {TEMPLATE} 写 {Path(root) / DOC}，再执行 `foremind controller handoff`。")}


# --- handoff document ---------------------------------------------------------------

def parse_checks(text) -> list[tuple[str, str]]:
    """The ```foremind-check block: `key: value` per line (blank lines and `#` comments skipped)."""
    m = _CHECK_BLOCK.search(text)
    if not m:
        raise ControllerError("no ```foremind-check block")
    items = []
    for line in m[1].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, sep, v = (x.strip() for x in line.partition(":"))
        bad = (not sep or not v or k not in CHECK_KEYS and not (k.startswith("batch ") and k[6:].strip())
               or k == "worktree_clean" and v not in ("true", "false") or k == "pending" and not v.isdigit()
               or k == "supervisor" and v not in ("running", "stopped"))
        if bad:
            raise ControllerError(f"check block: cannot read {line.strip()!r} (keys: {', '.join(CHECK_KEYS)}, "
                                  "batch <id or glob>; worktree_clean true|false, pending <n>, supervisor running|stopped)")
        items.append((k, v))
    if not items:
        raise ControllerError("check block is empty")
    return items


def check_doc(text) -> list[tuple[str, str]]:
    missing = [s for s in SECTIONS if not re.search(rf"^## {re.escape(s)}", text, re.M)]
    if missing:
        raise ControllerError(f"{DOC}: sections missing: {', '.join(missing)} (see {TEMPLATE})")
    return parse_checks(text)


def with_reading(text, used, hard) -> str:
    """The program's reading as the first line of the first paragraph (after the title); a previous one replaced."""
    line = (f"写交接时上下文 {used} token（程序读数，有效预算 {int(hard)}）。\n" if used is not None
            else f"写交接时上下文未知（程序读不到，有效预算 {int(hard)}）。\n")
    text = _READING.sub("", text, count=1)
    m = re.match(r"(# [^\n]*\n)\n*", text)
    return text[:m.end()] + line + text[m.end():] if m else line + "\n" + text


# --- open / handoff -------------------------------------------------------------------

def kickoff_text(root, predecessor) -> str:
    who = f"接替 {predecessor}" if predecessor else "由 `foremind controller open` 开出"
    out = f"你是 Foremind 项目 {root} 的常驻总控（{who}）。上下文由程序按实际读数把关：到软线提醒，到硬线阻断并要求交接。"
    if predecessor:
        out += (f"\n\n先执行 `foremind controller check`（{_cmd('check')}），逐项看核对结果；"
                f"再读 {Path(root) / DOC}，按它接手。有不符先查明再动手。")
    return out


def wait_bound(root, name, since, timeout_s) -> dict | None:
    """The state a controller session `name` wrote at SessionStart at or after `since` (epoch s), within timeout_s."""
    d, deadline = state_dir(root) / "controller", time.monotonic() + timeout_s
    while True:
        for p in d.glob("*.json"):
            with contextlib.suppress(OSError):
                if p.stat().st_mtime >= since - 1 and (st := _read_json(p)).get("controller") == name:
                    return st
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def open_controller(root, *, carrier, config=None, predecessor=None) -> dict:
    """Launch a controller session, wait for its SessionStart, deliver its kickoff (planner.open_planner's steps)."""
    root = Path(root).resolve()
    cfg = config if config is not None else cfg_mod.load(root)
    model = cfg.get("routes.controller_live.model", MODEL)
    effort = cfg.get("routes.controller_live.effort", EFFORT)
    seat._check_model(cfg, model)
    slug = seat.project_slug(root, cfg)
    with project_lock(root):  # the launch record takes the name (_next_seq reads events)
        name = seat.session_name(slug, "controller", seat._next_seq(root, seat.session_name(slug, "controller", "")))
        _events(root).append("controller_launch", session=name, model=model, effort=effort, predecessor=predecessor)
    # no --settings: the project's installed hooks already run in its root, a second copy would fire twice
    spec = get_vendor(seat._vendor(model)).launch(
        session=name, batch="", role="controller", project_root=root, cwd=root, settings_path=None,
        permission_mode=seat.setting(cfg, "seat.permission_mode"), model=model, effort=effort,
        env={"FOREMIND_ROLE": "controller", MARKER: name, "FOREMIND_PROJECT": str(root)})
    timeout = seat.setting(cfg, "seat.manual_sessionstart_timeout_s" if carrier.name == "manual"
                           else "seat.sessionstart_timeout_s")
    t0, launched = time.time(), True
    try:
        try:
            carrier.create(name, spec)
        except SessionExists:
            launched = False  # someone else's session: never close it
            raise
        if wait_bound(root, name, t0, timeout) is None:
            raise ControllerError(f"no SessionStart from {name} within {timeout} s (are foremind's hooks installed "
                                  f"in {root}/.claude/settings.local.json?)")
        carrier.deliver(name, kickoff_text(root, predecessor))
    except BaseException:
        if launched:
            with contextlib.suppress(Exception):
                carrier.close(name)
        raise
    out = {"session": name, "model": model, "effort": effort, "predecessor": predecessor}
    _events(root).append("controller_opened", carrier=carrier.name, **out)
    return out


def handoff(root, *, carrier, config=None) -> dict:
    """Check HANDOFF-controller.md, write the reading into it, record controller_handoff, open the successor. A
    carrier failure leaves the document and the event: run it again."""
    root = Path(root).resolve()
    cfg = config if config is not None else cfg_mod.load(root)
    st = current(root)
    if not st:
        raise ControllerError("找不到总控会话（.foremind/controller/ 下没有未交接的总控）")
    name = st["controller"]
    if nxt := next((e["session"] for e in history(root) if e["type"] == "controller_opened"
                    and e.get("predecessor") == name), None):
        raise ControllerError(f"{name} was already handed off to {nxt}")
    path = root / DOC
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ControllerError(f"{path}: missing; write it from {TEMPLATE}") from None
    check_doc(text)
    sts = _states(root, name)
    asked = min((s["hard_requested_at"] for s in sts if s.get("hard_requested_at")), default=None)
    since = asked or min((s["started_at"] for s in sts if s.get("started_at")), default=None)
    if since and path.stat().st_mtime <= datetime.fromisoformat(since).timestamp():
        why = "the hard line asked for it" if asked else "this controller started"
        raise ControllerError(f"{path} was not changed since {why} ({since}); write it anew")
    u = usage(st.get("transcript_path"))
    _, hard = budget(cfg, _model(root, cfg, name, (u or {}).get("model")) or st.get("model"))
    text = with_reading(text, u and u["context_tokens"], hard)
    atomic_write(path, text)
    ev = _events(root).append("controller_handoff", from_session=name, agent_session_id=st.get("agent_session_id"),
                              sha256=sha256_bytes(text.encode("utf-8")), budget=int(hard), **_split(u))
    out = open_controller(root, carrier=carrier, config=cfg, predecessor=name)
    return {**out, "handoff": ev["id"], "context_tokens": ev["context_tokens"]}


# --- check / slip -----------------------------------------------------------------------

def _git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)


def _batches(root) -> dict:
    """{batch: state} of the batch headers, read as `foremind status` reads them."""
    out = {}
    for p in sorted((state_dir(root) / "batches").glob("*.md")):
        if re.fullmatch(BATCH_ID, p.stem):
            try:
                out[p.stem] = hdr.parse(p.read_text(encoding="utf-8"))[0].get("state", "planned")
            except (OSError, hdr.HeaderError):
                out[p.stem] = "（批次头读不了）"
    return out


def _one(root, key, want, run) -> tuple[bool, str]:
    """(ok, what was found) for one check item."""
    if key == "main_contains":
        r = _git(root, "merge-base", "--is-ancestor", want, "HEAD")
        return r.returncode == 0, ("主检出 HEAD 含它" if r.returncode == 0 else
                                   "不含" if r.returncode == 1 else (r.stderr.strip() or f"git exit {r.returncode}"))
    if key == "worktree_clean":
        r = _git(root, "status", "--porcelain")
        dirty = [ln for ln in r.stdout.splitlines() if ln[3:].strip('"') != DOC]  # the doc itself is being handed on
        got = "true" if r.returncode == 0 and not dirty else "false"
        return got == want, got + (f"（{'; '.join(dirty[:5])}）" if dirty else "")
    if key == "pending":
        from foremind.supervisor.ready import OPEN_Q
        n = sum(_read_json(p).get("state", "open") in OPEN_Q for p in (state_dir(root) / "decisions").glob("Q-*.json"))
        return str(n) == want, str(n)
    if key == "supervisor":
        from foremind.supervisor.tick import supervisor_state
        s, rec = supervisor_state(root)
        got = "stopped" if s == "stopped" else "running"
        return got == want, got + (f"（{s}，pid {rec.get('pid')}）" if s != "stopped" else "")
    if key == "test":
        r = run(want)
        tail = (r.stdout + r.stderr).strip().splitlines()
        return r.returncode == 0, f"exit {r.returncode}" + (f"：{tail[-1]}" if tail else "")
    pat = key[6:].strip()  # batch <glob>
    got = {b: s for b, s in _batches(root).items() if fnmatch.fnmatchcase(b, pat)}
    off = [f"{b} {s}" for b, s in got.items() if s != want]
    return bool(got) and not off, ("没有匹配的批次" if not got else "；".join(off) if off else f"{len(got)} 批 {want}")


def _run_test(root):
    return lambda cmd: subprocess.run(cmd, shell=True, cwd=root, capture_output=True, text=True,
                                      timeout=TEST_TIMEOUT_S)


def check(root, *, run=None, carrier=None) -> tuple[list[dict], dict, dict | None]:
    """Every item of the document's check block against the project now; records controller_takeover; with every item
    ok, closes the predecessor (close_predecessor; `carrier` stands in for the one it was opened on), with a mismatch
    keeps it. Returns (items: {key, want, ok, got}, the event, the predecessor's outcome: close_predecessor's answer,
    {predecessor, kept} on a mismatch, None without a predecessor)."""
    root = Path(root).resolve()
    try:
        text = (root / DOC).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ControllerError(f"{root / DOC}: missing") from None
    items = []
    for k, v in parse_checks(text):
        try:
            ok, got = _one(root, k, v, run or _run_test(root))
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            ok, got = False, f"核对失败：{type(e).__name__}: {e}"
        items.append({"key": k, "want": v, "ok": ok, "got": got})
    st = current(root) or {}
    name = st.get("controller")
    pred = _launch(root, name).get("predecessor") if name else None
    bad = [f"{i['key']}: 应 {i['want']}，实际 {i['got']}" for i in items if not i["ok"]]
    ev = _events(root).append("controller_takeover", session=name, predecessor=pred, ok=not bad, mismatches=bad,
                              **_split(usage(st.get("transcript_path"))))
    if not pred:
        return items, ev, None
    if bad:  # m2d.1 r2: a predecessor an earlier check closed stays closed
        closed = any(e["type"] == "controller_closed" and e.get("predecessor") == pred and e.get("confirmed")
                     for e in history(root))
        return items, ev, {"predecessor": pred, "kept": "前任已关闭（此前的 check）" if closed else "核对有不符，前任保留"}
    return items, ev, close_predecessor(root, name, carrier=carrier)


def close_predecessor(root, name, *, carrier=None) -> dict | None:
    """REQ-3: close the controller `name` took over from, only when Foremind launched it (controller_launch), opened it
    on a carrier (controller_opened), it handed off (controller_handoff) and `name` is the successor it opened; never
    again once confirmed. Through that carrier, whose close makes its bound claude exit (REQ-1, Carrier._settled).
    Records and returns controller_closed {session, predecessor, confirmed, how, carrier[, error]}; {predecessor,
    kept: why, for the user} when it stays; None when there is nothing to close."""
    if not (pred := _launch(root, name).get("predecessor")):
        return None
    if not _launch(root, pred):
        return {"predecessor": pred, "kept": "没有 Foremind 启动记录（controller_launch），程序不关；请你关闭它的终端"}
    evs = list(history(root))
    opened = {e.get("session"): e for e in evs if e["type"] == "controller_opened"}
    if pred not in opened or opened.get(name, {}).get("predecessor") != pred or not handed_off(root, pred):
        return {"predecessor": pred, "kept": "前任保留：没有承载层记录、没记 controller_handoff，或它开出的继任不是本会话"}
    if any(e["type"] == "controller_closed" and e.get("predecessor") == pred and e.get("confirmed") for e in evs):
        return None
    car, ev, err = carrier, None, None
    try:
        car = car or carriers.get(opened[pred].get("carrier"), root, cfg_mod.load(root))
        ev = car.close(pred)
    except (carriers.CarrierError, cfg_mod.ConfigError, OSError, ValueError) as e:
        err = str(e)
    return _events(root).append("controller_closed", session=name, predecessor=pred, confirmed=ev is not None,
                                how=ev and ev.how, carrier=car and car.name, **({"error": err} if err else {}))


def takeover_line(items, ev) -> str:
    """The line for PROGRESS.md."""
    bad = len(ev["mismatches"])
    ctx = f"{ev['context_tokens']} token、{ev['calls']} 次调用" if ev["context_tokens"] is not None else "读不到"
    return (f"- {ev['ts']} 总控 {ev['session'] or '（未知会话）'} 接手 {ev['predecessor'] or '（无前任记录）'}："
            f"核对 {len(items)} 项，" + (f"{bad} 项不符（{'；'.join(ev['mismatches'])}）" if bad else "全部 ok")
            + f"；接手时上下文 {ctx}（程序读数）")


def slip(root, kind, note) -> dict:
    """controller_slip with the context the program reads from the current controller session (null if none)."""
    if kind not in SLIPS:
        raise ControllerError(f"kind must be one of {', '.join(SLIPS)}, not {kind!r}")
    if not note.strip():
        raise ControllerError("--note is empty")
    st = current(root) or {}
    u = usage(st.get("transcript_path"))
    return _events(root).append("controller_slip", kind=kind, note=note.strip(), session=st.get("controller"),
                                agent_session_id=st.get("agent_session_id"), context_tokens=u and u["context_tokens"])
