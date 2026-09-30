"""Quota state per account × role group (DESIGN §10.5): pure `step` and `probe_done`, plus `read_telemetry` (one
project's) / `read_account_telemetry` (every enabled project's) and the state file.

States: available | low | exhausted (`window` 5h|7d, `resets_at`, `pause`) | unknown (`reason`; probe `fails`, `next_try`).
Groups: long (seats and other long-lived roles) pause at 100 - quota.reserve_pct (default 10 -> 90%), oneshot
(reviewers, auditors) at quota.oneshot_pause_pct (95%). Low: the 5h window at quota.low_pct (85%) or more, or the 7d
window ahead of its per-day share; low turns available again only under quota.recover_pct (80%, hysteresis) and
back on the 7d pace. A window reading counts only while its resets_at is ahead (an older window says nothing):
missing, null or older than quota.stale_min (15) -> unknown. At a window's reset, exhausted -> unknown, never
straight to available; a 7d exhaustion waits for the 7d reset. An exhaustion records its pause line; once the user
raises the line above it (or none was recorded), exhausted -> unknown (config_changed) at once. Unknown ends with a
successful probe (probe_done; available for quota.stale_min) or a fresh reading taken after it began (with no state
yet, any fresh reading). A failed probe backs off quota.backoff_min
(15) × 2^(n-1), never later than the next reset (at most 5 hours on). A seven_day that is there but null, unreadable
or of an older window makes it unknown too (reason null_7d / reset; a 5h exhaustion still wins) until a successful
probe vouches for quota.stale_min; only a snapshot without a seven_day has no 7d checks (SF-5).
Times are epoch seconds.

The state is the account's, shared by every project (m2b.10): state_path() next to supervisor.lock, read, changed
and written under that global lock (a tick holds it for its whole pass; `foremind quota --reset` takes it), read
without it by status, `foremind quota` and the run report (atomic writes; view() folds in a project's file not yet
migrated), stepped on the strictest lines of the enabled projects (account_cfg). {"groups": {"<account>/<group>":
state}, "paused": [sessions of any project to be told "continue", each by its own project's tick], "exhausted_ended":
epoch}.
"""
import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path

from foremind import config, install
from foremind.defaults import TABLE
from foremind.events import EventLog
from foremind.fsutil import atomic_write
from foremind.paths import state_dir, user_config_dir

AVAILABLE, LOW, EXHAUSTED, UNKNOWN = "available", "low", "exhausted", "unknown"
GROUPS = ("long", "oneshot")
# ponytail: one Claude account per machine (§10.6 Claude only); key states by the account telemetry reports once
# Codex (M2-7) or a second account exists
ACCOUNT = "claude"
WINDOW_S = {"5h": 5 * 3600, "7d": 7 * 86400}
STRICT = {AVAILABLE: 0, LOW: 1, UNKNOWN: 2, EXHAUSTED: 3}


def state_path():
    return user_config_dir() / "quota.json"


def _read(path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def load() -> dict:
    """The stored state ({} when there is none or it is unreadable)."""
    return _read(state_path())


def save(q) -> None:
    atomic_write(state_path(), json.dumps(q, indent=1, sort_keys=True))


def view(root) -> dict:
    """The state as the read-only commands show it (quota, status): the stored one with the project's quota.json of
    before m2b.10 folded in as migrate does, nothing written (r1 #3: until the first tick it is the only record)."""
    q = load()
    _fold(q, _read(state_dir(root) / "quota.json"))
    return q


def migrate(root, q) -> bool:
    """Fold the project's .foremind/quota.json of before m2b.10 into `q`, the loaded state, under the global lock: per
    group the stricter state (exhausted > unknown > low > available; a tie keeps q's), paused lists joined, the later
    exhausted_ended. Saves q, then removes the old file (its merged_checked with it: at worst one merged check early)
    and writes `quota_migrated`. False when the project has no old file."""
    old_path = state_dir(root) / "quota.json"
    if not old_path.exists():
        return False
    taken = _fold(q, _read(old_path))
    save(q)
    old_path.unlink(missing_ok=True)
    EventLog(state_dir(root) / "events.jsonl").append("quota_migrated", to=str(state_path()), taken=taken)
    return True


def _fold(q, old) -> list:
    """migrate's merge of `old` into `q`; the groups taken from `old`."""
    groups = q["groups"] = q["groups"] if isinstance(q.get("groups"), dict) else {}
    taken = []
    for k, st in (old.get("groups") if isinstance(old.get("groups"), dict) else {}).items():
        cur = groups.get(k)
        if isinstance(st, dict) and st.get("state") in STRICT and (
                not isinstance(cur, dict) or STRICT[st["state"]] > STRICT.get(cur.get("state"), -1)):
            groups[k] = st
            taken.append(k)
    paused = q.get("paused") if isinstance(q.get("paused"), list) else []
    q["paused"] = paused + [s for s in (old.get("paused") if isinstance(old.get("paused"), list) else [])
                            if s not in paused]
    ended = [v for v in (q.get("exhausted_ended"), old.get("exhausted_ended"))
             if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if ended:
        q["exhausted_ended"] = max(ended)
    return taken


def _num(cfg, key):
    v = cfg.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else TABLE[key]


# the lines a project may tighten (AUTHZ with an order): which of two values is the stricter (defaults: TABLE)
LINES = {"quota.reserve_pct": max, "quota.oneshot_pause_pct": min, "quota.low_pct": min, "quota.recover_pct": min}


def lines(cfg, group) -> dict:
    v = {k: _num(cfg, k) for k in LINES}
    return {"pause": 100 - v["quota.reserve_pct"] if group == "long" else v["quota.oneshot_pause_pct"],
            # false (user 2026-09-27): no 7d daily-share slowdown
            "pace": cfg.get("quota.pace", TABLE["quota.pace"]) is not False,
            "low": v["quota.low_pct"], "recover": v["quota.recover_pct"],
            "stale": _num(cfg, "quota.stale_min") * 60, "backoff": _num(cfg, "quota.backoff_min") * 60}


def account_cfg(root, cfg) -> dict:
    """`cfg` with each of LINES at the strictest of `root`'s and every enabled project's (r2 #2): the state is shared,
    so every tick must step it on the same lines (else it flips between projects), and a reserve one project keeps
    is only kept when all stop there. A project whose config does not load is left out (its own tick says why)."""
    cfgs, here = [cfg], Path(root).resolve()
    with contextlib.suppress(install.InstallError, OSError):
        for r in install.registered():
            if Path(r).resolve() != here:
                with contextlib.suppress(config.ConfigError, OSError):
                    cfgs.append(config.load(Path(r)))
    return {**cfg, **{k: pick(_num(c, k) for c in cfgs) for k, pick in LINES.items()}}


def epoch(v) -> float | None:
    """Epoch seconds from a number (milliseconds if it looks like them) or an ISO 8601 string; None otherwise."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, str):
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _window(w):
    if not isinstance(w, dict):
        return None
    pct, resets = w.get("used_percentage", w.get("used_percent")), epoch(w.get("resets_at"))
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or resets is None:
        return None
    return {"pct": float(pct), "resets_at": resets}


def read_telemetry(root) -> dict | None:
    """The newest statusline snapshot as {"ts", "five_hour", "seven_day"} (a window is {"pct", "resets_at"} or None
    when null or unreadable; "seven_day" is left out when the snapshot has none); None when there is none."""
    best = None
    for p in (state_dir(root) / "telemetry").glob("*.statusline.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            ts = epoch(s["ts"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if ts is not None and (best is None or ts > best["ts"]):
            rl = s.get("rate_limits") if isinstance(s.get("rate_limits"), dict) else {}
            best = {"ts": ts, "five_hour": _window(rl.get("five_hour"))}
            if "seven_day" in rl:
                best["seven_day"] = _window(rl["seven_day"])
    return best


def read_account_telemetry(root) -> dict | None:
    """read_telemetry's newest snapshot of `root` and every enabled project (~/.config/foremind/projects): the state is
    the account's, and so is any of its sessions' reading (else a project without sessions turns it unknown for all)."""
    roots = {Path(root).resolve()}
    with contextlib.suppress(install.InstallError, OSError):
        roots.update(Path(r).resolve() for r in install.registered())
    return max(filter(None, map(read_telemetry, roots)), key=lambda t: t["ts"], default=None)


def pace(resets_at, now) -> float:
    """7d usage (%) allowed so far: one seventh per started day of the window."""
    day = max(0, int((now - (resets_at - WINDOW_S["7d"])) // 86400))
    return 100 * min(7, day + 1) / 7


def _live(w, now):
    return w if w is not None and w["resets_at"] > now else None


def _to(prev, state, now, **kw):
    if prev.get("state") == state and all(prev.get(k) == v for k, v in kw.items()):
        return prev
    return {"state": state, "since": now, **kw}


def _unknown(prev, now, reason, next_reset=None):
    if prev.get("state") == UNKNOWN:  # keeps since, fails, next_try and next_reset
        return prev if prev.get("reason") == reason else {**prev, "reason": reason}
    return {"state": UNKNOWN, "since": now, "reason": reason, "fails": 0, "next_try": now,
            **({"next_reset": next_reset} if next_reset else {})}


def step(prev, tel, now, cfg, group="long") -> dict:
    c = lines(cfg, group)
    # no state yet: since 0, so any fresh reading counts
    prev = prev or {"state": UNKNOWN, "since": 0, "reason": "no_data", "fails": 0, "next_try": now}
    st = prev["state"]
    if st == EXHAUSTED:
        if now >= prev["resets_at"]:
            return _unknown(prev, now, "reset", prev["resets_at"] + WINDOW_S[prev["window"]])
        if "pause" not in prev or c["pause"] > prev["pause"]:  # the user raised the line: look again
            return _unknown(prev, now, "config_changed")
        return prev
    fresh = tel is not None and now - tel["ts"] <= c["stale"]
    five = _live(tel.get("five_hour"), now) if tel else None
    if not fresh or not five or (st == UNKNOWN and tel["ts"] <= prev["since"]):
        if st in (AVAILABLE, LOW) and prev.get("ok_until", 0) > now and (tel is None or tel["ts"] <= prev["since"]):
            return prev  # a successful probe, newer than any reading
        return _unknown(prev, now, "no_data" if tel is None else "stale" if not fresh
                        else "null" if tel.get("five_hour") is None else "reset" if not five
                        else prev.get("reason"))  # a reading older than the unknown state says nothing new
    seven = _live(tel.get("seven_day"), now)
    blind7 = "seven_day" in tel and not seven  # there but null, unreadable or of an older window (SF-5)
    if seven and seven["pct"] >= c["pause"]:
        return _to(prev, EXHAUSTED, now, window="7d", resets_at=seven["resets_at"], pause=c["pause"])
    if five["pct"] >= c["pause"]:
        return _to(prev, EXHAUSTED, now, window="5h", resets_at=five["resets_at"], pause=c["pause"])
    vouched = prev.get("ok_until", 0) > now  # a successful probe covers the 7d window until then
    if blind7 and not vouched:
        return _unknown(prev, now, "null_7d" if tel["seven_day"] is None else "reset")
    ahead = c["pace"] and bool(seven) and seven["pct"] > pace(seven["resets_at"], now)
    low = ahead or five["pct"] >= (c["recover"] if st == LOW else c["low"])
    return _to(prev, LOW if low else AVAILABLE, now, **({"ok_until": prev["ok_until"]} if blind7 else {}))


def probe_due(st, now) -> bool:
    return st["state"] == UNKNOWN and now >= st.get("next_try", 0)


def probe_done(prev, ok, now, cfg, group="long", tel=None) -> dict:
    """A probe's answer to the unknown state `prev`; `tel` (read_account_telemetry's) is the newest reading. Finding
    27: a success with that reading's 7d window known and ahead of its daily share (step's check, quota.pace) is low,
    not available: 7d use only grows within its window, so an older reading ahead of pace(now) still is."""
    if prev["state"] != UNKNOWN:
        return prev
    if ok:
        c, seven = lines(cfg, group), _live(tel.get("seven_day"), now) if tel else None
        ahead = c["pace"] and seven is not None and seven["pct"] > pace(seven["resets_at"], now)
        return {"state": LOW if ahead else AVAILABLE, "since": now, "ok_until": now + c["stale"]}
    fails = prev.get("fails", 0) + 1
    cap = min([r for r in (prev.get("next_reset"),) if r and r > now] + [now + WINDOW_S["5h"]])
    return {**prev, "fails": fails, "next_try": min(now + lines(cfg, group)["backoff"] * 2 ** (fails - 1), cap)}
