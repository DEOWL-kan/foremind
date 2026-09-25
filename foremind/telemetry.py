"""Context-usage and quota telemetry under `.foremind/telemetry/` (DESIGN §1.1, §6.3, §6.8, §10.5).

  <session>.context.json      Stop writes context_usage; PostToolUse refreshes only context_tokens (last_context)
  <session>.statusline.json   written by `foremind statusline`: rate_limits and context_window copied verbatim
                              (null stays null = unknown, never "available")
<session> is FOREMIND_SESSION, or the agent session id outside Foremind sessions.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from foremind.fsutil import atomic_write
from foremind.heartbeat import check_name
from foremind.paths import ProjectNotFound, find_project_root, state_dir

_CONTEXT_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _n(u, k):
    v = u.get(k)
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _record(line: bytes):
    """(message id, usage) of a main-chain assistant record, else None. Records whose three context counts are all 0
    (synthetic ones written on API errors) say nothing about the context size and are skipped."""
    if b'"usage"' not in line:  # cheap filter before parsing
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    if not isinstance(e, dict) or e.get("type") != "assistant" or e.get("isSidechain"):
        return None
    msg = e.get("message")
    u = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(u, dict) or not sum(_n(u, k) for k in _CONTEXT_KEYS):
        return None
    return msg.get("id"), u


def _open(transcript):
    try:
        return open(os.path.expanduser(transcript), "rb")
    except (OSError, TypeError):
        return None


def context_usage(transcript) -> dict | None:
    """Usage of a Claude Code transcript's main chain; None when there is none yet.

    context_tokens = input + cache_creation + cache_read of the last assistant request; records with isSidechain
    are skipped; one request is split into several records sharing message.id (the last one of each id is kept), so
    counts and output_tokens are per distinct id. first_usage is the opening request (startup tokens and cache hits,
    §6.8).
    """
    # ponytail: reads the whole transcript; only Stop calls it (PostToolUse reads the tail, last_context)
    if not (f := _open(transcript)):
        return None
    by_id, last = {}, None
    with f:
        for line in f:
            if r := _record(line):
                by_id[r[0] or f"#{len(by_id)}"] = last = r[1]
    if last is None:
        return None
    first = next(iter(by_id.values()))
    return {
        "context_tokens": sum(_n(last, k) for k in _CONTEXT_KEYS),
        "first_usage": {k: _n(first, k) for k in _CONTEXT_KEYS},
        "requests": len(by_id),
        "output_tokens": sum(_n(u, "output_tokens") for u in by_id.values()),
    }


def last_context(transcript, chunk=1 << 16) -> int | None:
    """context_tokens of the last main-chain request, reading the transcript backwards from its end."""
    if not (f := _open(transcript)):
        return None
    with f:
        end, carry = f.seek(0, os.SEEK_END), b""
        while end > 0:
            start = max(0, end - chunk)
            f.seek(start)
            lines = (f.read(end - start) + carry).split(b"\n")
            carry = lines.pop(0) if start else b""  # maybe a partial line: finish it with the next chunk
            for line in reversed(lines):
                if r := _record(line):
                    return sum(_n(r[1], k) for k in _CONTEXT_KEYS)
            end = start
    return None


def write(root, session, kind, snapshot: dict, *, merge=False) -> Path:
    """Replace the snapshot, or with merge=True update the fields of the existing one."""
    p = state_dir(root) / "telemetry" / f"{check_name(session)}.{kind}.json"
    old = {}
    if merge:
        try:
            old = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write(p, json.dumps({**(old if isinstance(old, dict) else {}), **snapshot, "ts": ts},
                               ensure_ascii=False, sort_keys=True))
    return p


def record_statusline(raw: bytes) -> Path | None:
    """Store the statusline input's rate_limits and context_window; never raises (the status bar must still draw)."""
    try:
        data = json.loads(raw)
        cwd = data.get("cwd") or (data.get("workspace") or {}).get("current_dir")
        root = find_project_root(Path(os.path.expanduser(cwd)) if cwd else None)
        session = os.environ.get("FOREMIND_SESSION") or data.get("session_id")
        return write(root, session, "statusline", {
            "session": os.environ.get("FOREMIND_SESSION") or None,
            "agent_session_id": data.get("session_id"),
            "model": data.get("model"),
            "rate_limits": data.get("rate_limits"),
            "context_window": data.get("context_window"),
        })
    except (ProjectNotFound, OSError, ValueError, AttributeError):
        return None
