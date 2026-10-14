"""`foremind watch` (REQ-6, m2f.6): the controller's patrol, replacing the out-of-project patrol.sh. Read-only: no
file written, no event recorded, no lock taken.

It follows events.jsonl from its end at start (from the top again when the file got shorter) and prints the key
events, one line each; every --interval seconds (and once at start) it checks every batch of the project: a lock
holder's rendered screen showing a choice or confirmation (CHOICE_MARKERS), a delivered batch waiting to be landed,
and finding 26 (heartbeat stuck at tool_open, terminal idle, a supervisor message after it). `--once`: one check,
no events. The guard's whitelist keeps Foremind sessions out; the user and the controller run it.
"""
import argparse
import json
import sys
import time
from datetime import datetime

from foremind import carriers, config, heartbeat, lock, review
from foremind.carriers.ready import CHOICE_MARKERS  # one definition with the carriers' send check (REQ-9)
from foremind.defaults import TABLE
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.plan import model
from foremind.supervisor import ready
KEY_TYPES = {"pending_created", "pending_needed", "review_receipt", "gate_result", "l0_hard_failure", "job_failed",
             "stuck", "reconciled", "review_not_converging", "sv_successor", "bounds_violation", "bounds_error",
             "session_leak", "session_closed", "session_close_unverified", "session_unknown", "controller_closed",
             "supervisor_reexec", "land_result", "review_conflict_rejected"}
KEY_STATES = ("failed", "ready", "delivered", "changes_requested")
FIELDS = ("batch", "ok", "error", "reason", "frm", "to", "state", "verdict", "round", "severity", "actions", "kind",
          "paths", "seat_ancestor", "check", "session", "why", "confirmed", "pid", "outcome", "category")
WIDTH = 250


def register(sub):
    p = sub.add_parser("watch", help="patrol the project: key events, choices on screen, batches waiting to land")
    p.add_argument("--interval", type=_interval, default=120, help="seconds between checks (at least 10)")
    p.add_argument("--once", action="store_true", help="check once and exit; no events")
    p.set_defaults(func=_run)


def _interval(s):
    try:
        n = int(s)
    except ValueError:
        n = 0
    if n < 10:
        raise argparse.ArgumentTypeError(f"{s!r}: an integer of at least 10")
    return n


def key(e) -> bool:
    """A key event (REQ-6). A seat_open intent never is; any other event not picked by type falls to the last rule."""
    t = str(e.get("type", ""))
    if t == "seat_open":
        return e.get("phase") != "intent"
    # seat and tick write batch_state as frm/to, review.set_state as prior/state (report._to)
    if t in KEY_TYPES or (t == "batch_state" and e.get("to", e.get("state")) in KEY_STATES):
        return True
    return "fail" in t or "error" in t or "error" in e or "seat_ancestor" in e


def render(raw) -> str | None:
    """The output line for one events.jsonl line, None when it is no key event."""
    try:
        e = json.loads(raw)
    except ValueError:
        return "BAD LINE"
    if not isinstance(e, dict):
        return "BAD LINE"
    if not key(e):
        return None
    fields = json.dumps({k: e[k] for k in FIELDS if k in e}, ensure_ascii=False)
    return f"{str(e.get('ts', ''))[11:19]} {e.get('type')} {fields}"[:WIDTH]


def follow(path, off) -> tuple[list[str], int]:
    """(output lines, new offset) for the complete lines added since byte `off`; from 0 when the file got shorter."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            if f.tell() < off:
                off = 0
            f.seek(off)
            data = f.read()
    except FileNotFoundError:
        return [], 0
    data = data[:data.rfind(b"\n") + 1]  # a line still being appended waits for its newline
    out = [r for line in data.splitlines() if line.strip()
           if (r := render(line.decode("utf-8", "replace"))) is not None]
    return out, off + len(data)


def _events(root):
    """Every readable event, archives first; bad lines are skipped (the follower reports them)."""
    sd = state_dir(root)
    for p in [*sorted((sd / "archive").glob("events-*.jsonl")), sd / "events.jsonl"]:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e


def _batches(root, evs):
    """[(batch, header, merged config or None)] of every plan that loads (one stderr line for one that does not);
    config as tick.bcfg reads it. Bound as model.is_bound, from `evs`: its EventLog.iter stops at a bad line."""
    approved, out = {}, []
    for e in evs:
        if e.get("type") in ("plan_approved", "plan_amended"):
            approved[e.get("plan")] = e.get("plan_hash")
    for pid in model.plan_ids(root):
        try:
            p = model.load(root, pid)
        except (model.PlanError, OSError, ValueError) as e:
            print(f"foremind watch: plan {pid}: {e}", file=sys.stderr, flush=True)
            continue
        bound = "approved_at" in p.doc.header and model.plan_hash(p) == approved.get(pid)
        for bid, d in p.batches.items():
            try:
                cfg = config.load(root, config.task_layer(d.header),
                                  task_user_approved=model.task_config_approved(root, p, bid, bound=bound))
            except config.ConfigError:
                cfg = None
            out.append((bid, d.header, cfg))
    return out


def _decisions(root):
    out = []
    for p in sorted((state_dir(root) / "decisions").glob("Q-*.json")):
        try:
            q = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(q, dict):  # id and blocks as tick.load leaves a file that fails its schema: still blocks
            b = q.get("blocks") or []
            out.append({**q, "id": str(q.get("id", "?")),
                        "blocks": [str(x) for x in (b if isinstance(b, list) else [b])]})
    return out


def merge_dev(h, cfg) -> bool:
    """Same as tick.merge_dev: every repo delivers at merge_dev (the supervisor merges it)."""
    return cfg is not None and all(review.repo_cfg(cfg, r, "level", "delivery.level", TABLE["delivery.level"])
                                   == "merge_dev" for r in h.get("repos", []))


def _ts(s):
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else None  # a naive time cannot be compared with the events' UTC ones


def check(root) -> list[str]:
    evs = list(_events(root))
    batches, decisions = _batches(root, evs), _decisions(root)
    out, held = [], {}
    for bid, h, cfg in batches:
        if h.get("state") == "delivered" and not merge_dev(h, cfg) and not ready.blocking(bid, decisions):
            out.append(f"PATROL: {bid} delivered 待合入")
        if (s := lock.holder(root, bid)) and s != lock.USER:
            held.setdefault(s, []).append((bid, cfg))
    for s, bs in held.items():
        names = "、".join(b for b, _ in bs)
        kind = next((e.get("carrier") for e in reversed(evs) if e.get("type") == "seat_launch"
                     and e.get("phase") == "intent" and e.get("session") == s), None)
        cfg = bs[0][1] or {}
        try:
            car = carriers.get(kind or cfg.get("carrier.kind", TABLE["carrier.kind"]), root, cfg)
            screen = getattr(car, "screen", None)  # manual has none: skipped
            lines = screen(s) if screen else None
            if lines and any(m in x for x in lines for m in CHOICE_MARKERS):
                out.append(f"PATROL: {s}（{names}）在等终端里的选择题或确认")
            hb = heartbeat.read(root, s) or {}
            hb_at = _ts(hb.get("ts"))
            if not (hb.get("tool_open") and not hb.get("handoff_requested") and hb_at):
                continue
            said = next((e for e in reversed(evs) if e.get("type") == "sv_say" and e.get("phase") == "result"
                         and e.get("session") == s and e.get("ok")), None)
            said_at = _ts(said and said.get("ts"))
            if not (said_at and said_at > hb_at):
                continue
            st = car.read_state(s)
            if st.alive and st.idle:
                out.append(f"PATROL: {s} 终端空闲但心跳卡在 tool_open（{hb['ts']}），其后 {said['ts']} 有投递未送达"
                           "（发现 26）")
        except (carriers.CarrierError, OSError, ValueError):
            continue  # the carrier cannot read it: skip its carrier checks
    return out


def _run(args):
    try:
        root = find_project_root()
    except ProjectNotFound as e:
        print(f"foremind watch: {e}", file=sys.stderr)
        return 1
    path = state_dir(root) / "events.jsonl"
    off = path.stat().st_size if path.exists() else 0
    try:
        _print(_check(root))
        if args.once:
            return 0
        due = time.monotonic() + args.interval
        while True:
            time.sleep(1)
            lines, off = follow(path, off)
            _print(lines)
            if time.monotonic() >= due:
                _print(_check(root))
                due += args.interval
    except KeyboardInterrupt:
        return 0


def _check(root):
    """check(), which a file of unexpected shape must not end: the patrol runs until interrupted (one stderr line)."""
    try:
        return check(root)
    except Exception as e:  # noqa: BLE001
        print(f"foremind watch: check failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return []


def _print(lines):
    for x in lines:
        print(x, flush=True)
