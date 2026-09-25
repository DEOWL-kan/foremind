"""Quota state per account × role group (DESIGN §10.5): pure `step` and `probe_done`, plus `read_telemetry`.

States: available | low | exhausted (`window` 5h|7d, `resets_at`) | unknown (`reason`; probe `fails`, `next_try`).
Groups: long (seats and other long-lived roles) pause at 100 - quota.reserve_pct (default 10 -> 90%), oneshot
(reviewers, auditors) at quota.oneshot_pause_pct (95%). Low: the 5h window at quota.low_pct (85%) or more, or the 7d
window ahead of its per-day share; low turns available again only under quota.recover_pct (80%, hysteresis) and
back on the 7d pace. A window reading counts only while its resets_at is ahead (an older window says nothing):
missing, null or older than quota.stale_min (15) -> unknown. At a window's reset, exhausted -> unknown, never
straight to available; a 7d exhaustion waits for the 7d reset. Unknown ends with a successful probe (probe_done;
available for quota.stale_min) or a fresh reading taken after it began (with no state yet, any fresh reading). A failed probe backs off quota.backoff_min
(15) × 2^(n-1), never later than the next reset (at most 5 hours on). A seven_day that is there but null, unreadable
or of an older window makes it unknown too (reason null_7d / reset; a 5h exhaustion still wins) until a successful
probe vouches for quota.stale_min; only a snapshot without a seven_day has no 7d checks (SF-5).
Times are epoch seconds.
"""
import json
from datetime import datetime, timezone

from foremind.paths import state_dir

AVAILABLE, LOW, EXHAUSTED, UNKNOWN = "available", "low", "exhausted", "unknown"
GROUPS = ("long", "oneshot")
# ponytail: one Claude account per machine (§10.6 Claude only); key states by the account telemetry reports once
# Codex (M2-7) or a second account exists
ACCOUNT = "claude"
WINDOW_S = {"5h": 5 * 3600, "7d": 7 * 86400}


def _num(cfg, key, default):
    v = cfg.get(key)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def lines(cfg, group) -> dict:
    pause = (100 - _num(cfg, "quota.reserve_pct", 10) if group == "long"
             else _num(cfg, "quota.oneshot_pause_pct", 95))
    return {"pause": pause, "low": _num(cfg, "quota.low_pct", 85), "recover": _num(cfg, "quota.recover_pct", 80),
            "stale": _num(cfg, "quota.stale_min", 15) * 60, "backoff": _num(cfg, "quota.backoff_min", 15) * 60}


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
        if now < prev["resets_at"]:
            return prev
        return _unknown(prev, now, "reset", prev["resets_at"] + WINDOW_S[prev["window"]])
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
        return _to(prev, EXHAUSTED, now, window="7d", resets_at=seven["resets_at"])
    if five["pct"] >= c["pause"]:
        return _to(prev, EXHAUSTED, now, window="5h", resets_at=five["resets_at"])
    vouched = prev.get("ok_until", 0) > now  # a successful probe covers the 7d window until then
    if blind7 and not vouched:
        return _unknown(prev, now, "null_7d" if tel["seven_day"] is None else "reset")
    ahead = bool(seven) and seven["pct"] > pace(seven["resets_at"], now)
    low = ahead or five["pct"] >= (c["recover"] if st == LOW else c["low"])
    return _to(prev, LOW if low else AVAILABLE, now, **({"ok_until": prev["ok_until"]} if blind7 else {}))


def probe_due(st, now) -> bool:
    return st["state"] == UNKNOWN and now >= st.get("next_try", 0)


def probe_done(prev, ok, now, cfg, group="long") -> dict:
    if prev["state"] != UNKNOWN:
        return prev
    if ok:
        return {"state": AVAILABLE, "since": now, "ok_until": now + lines(cfg, group)["stale"]}
    fails = prev.get("fails", 0) + 1
    cap = min([r for r in (prev.get("next_reset"),) if r and r > now] + [now + WINDOW_S["5h"]])
    return {**prev, "fails": fails, "next_try": min(now + lines(cfg, group)["backoff"] * 2 ** (fails - 1), cap)}
