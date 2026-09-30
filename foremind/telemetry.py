"""Context-usage and quota telemetry under `.foremind/telemetry/` (DESIGN §1.1, §6.3, §6.8, §10.5).

  <session>.context.json      Stop writes context_usage; PostToolUse refreshes only context_tokens (last_context)
  <session>.statusline.json   written by `foremind statusline`: rate_limits and context_window copied verbatim
                              (null stays null = unknown, never "available")
  <session>.after_stop.json   {ids}: the last tool_use_ids a PreToolUse recorded as tool_after_stop, for PostToolUse
                              to confirm (hooks, REQ-12); updated under <session>.after_stop.lock
<session> is FOREMIND_SESSION, or the agent session id outside Foremind sessions.
"""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from foremind.fsutil import atomic_write, file_lock
from foremind.heartbeat import check_name
from foremind.paths import ProjectNotFound, find_project_root, state_dir

STALE_DAYS = 7  # a non-Foremind session's statusline reading older than this goes when another one is written
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


def _backwards(transcript, chunk):
    """The transcript's lines from its end (none when it cannot be opened)."""
    if not (f := _open(transcript)):
        return
    with f:
        end, carry = f.seek(0, os.SEEK_END), b""
        while end > 0:
            start = max(0, end - chunk)
            f.seek(start)
            lines = (f.read(end - start) + carry).split(b"\n")
            carry = lines.pop(0) if start else b""  # maybe a partial line: finish it with the next chunk
            yield from reversed(lines)
            end = start


def last_context(transcript, chunk=1 << 16) -> int | None:
    """context_tokens of the last main-chain request, reading the transcript backwards from its end."""
    for line in _backwards(transcript, chunk):
        if r := _record(line):
            return sum(_n(r[1], k) for k in _CONTEXT_KEYS)
    return None


def has_tool_use(transcript, tool_use_id, tail=1 << 20, chunk=1 << 16) -> bool:
    """Whether a main-chain assistant record in the transcript's last `tail` bytes has a tool_use block with this id
    (REQ-15: a PreToolUse after Stop is a real turn going on, not one of Claude Code's own background calls)."""
    if not isinstance(tool_use_id, str) or not tool_use_id:
        return False
    key, seen = tool_use_id.encode(), 0
    for line in _backwards(transcript, chunk):
        if (seen := seen + len(line) + 1) > tail:
            return False
        if key not in line:  # cheap filter before parsing
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        msg = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" and not e.get("isSidechain") \
            else None
        c = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id
                                       for b in c):
            return True
    return False


# the record's `error` (Claude Code 2.1.280: server_error, rate_limit, authentication_failed; the StopFailure hook's
# error_type values besides); another or none -> apiErrorStatus, then the text (a status only as "API Error: 403 …",
# never a port such as 127.0.0.1:443)
_RETRY_ERRORS = {"server_error", "overloaded", "unknown"}
_FINAL_ERRORS = {"rate_limit", "authentication_failed", "billing_error", "invalid_request", "max_output_tokens"}
_FINAL_TEXT = re.compile(r"limit|auth|log ?in|too long|API Error: 4\d\d\b", re.I)
_RETRY_TEXT = re.compile(r"ECONN|EADDR|ETIMEDOUT|EPIPE|connect|socket|timed? ?out|overloaded|went to sleep"
                         r"|API Error: 5\d\d\b", re.I)


def _retryable(e, text) -> bool:
    if isinstance(err := e.get("error"), str) and err in _RETRY_ERRORS | _FINAL_ERRORS:
        return err in _RETRY_ERRORS
    if isinstance(status := e.get("apiErrorStatus"), int) and not isinstance(status, bool):
        return status >= 500
    return not _FINAL_TEXT.search(text) and bool(_RETRY_TEXT.search(text))


def last_api_error(transcript, chunk=1 << 16) -> dict | None:
    """The API error a Claude Code transcript's main chain ends on (its last user or assistant record is one: an
    assistant record with isApiErrorMessage true), else None. {id, text, retryable, at: epoch or 0, run: ids of the
    API errors since the last good assistant reply, newest first}."""
    out = None
    for line in _backwards(transcript, chunk):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("type") not in ("user", "assistant") or e.get("isSidechain"):
            continue
        if e.get("type") == "assistant" and e.get("isApiErrorMessage") is True:
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            eid = str(e.get("uuid") or msg.get("id"))
            if out is None:
                c = msg.get("content")
                blocks = c if isinstance(c, list) else [{"text": c}]
                text = next((b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)), "")
                try:
                    at = datetime.fromisoformat(e["timestamp"]).timestamp()
                except (KeyError, TypeError, ValueError):
                    at = 0.0
                out = {"id": eid, "text": text, "retryable": _retryable(e, text), "at": at, "run": []}
            out["run"].append(eid)
        elif out is None or e.get("type") == "assistant":
            break  # the chain went on after its last error, or this is the good reply before the run
    return out


def path(root, session, kind) -> Path:
    return state_dir(root) / "telemetry" / f"{check_name(session)}.{kind}.json"


def read(root, session, kind) -> dict | None:
    try:
        s = json.loads(path(root, session, kind).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None


def locked(root, session, kind):
    """The lock for a read-then-write of one snapshot (parallel hooks of one session)."""
    return file_lock(path(root, session, kind).with_suffix(".lock"))


def write(root, session, kind, snapshot: dict, *, merge=False) -> Path:
    """Replace the snapshot, or with merge=True update the fields of the existing one."""
    p = path(root, session, kind)
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
        p = write(root, session, "statusline", {
            "session": os.environ.get("FOREMIND_SESSION") or None,
            "agent_session_id": data.get("session_id"),
            "model": data.get("model"),
            "rate_limits": data.get("rate_limits"),
            "context_window": data.get("context_window"),
        })
        if not os.environ.get("FOREMIND_SESSION"):
            _prune_stale(p)
        return p
    except (ProjectNotFound, OSError, ValueError, AttributeError, TypeError):  # TypeError: e.g. a number as cwd
        return None


def _prune_stale(keep: Path):
    """Delete other non-Foremind sessions' statusline readings not written for STALE_DAYS (named by agent session id,
    they pile up). Ours only: the name is the content's agent_session_id and its session is null."""
    cutoff = time.time() - STALE_DAYS * 86400
    for q in keep.parent.glob("*.statusline.json"):
        try:
            if q == keep or q.stat().st_mtime >= cutoff:
                continue
            s = json.loads(q.read_text(encoding="utf-8"))
            if (isinstance(s, dict) and "session" in s and s["session"] is None
                    and s.get("agent_session_id") == q.name.removesuffix(".statusline.json")
                    and q.stat().st_mtime < cutoff):  # not rewritten while we read it
                q.unlink()
        except (OSError, ValueError):
            continue
