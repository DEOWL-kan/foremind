"""Session heartbeat `.foremind/heartbeats/<session>.json` (DESIGN §1.3, §10.3; format shared with M1-4).

{"session", "batch", "role", "agent_session_id", "ts", "event", "tool_open", "handoff_requested"} plus hook-private
`open_tools` (tool_use_ids started and not finished; tool_open = any of them) and `soft_prompted` (the
soft-threshold prompt was given). Only the session's own hooks write it. update() merges under a per-file lock, so
flags survive the busy_tool round trip (§20 I31).
"""
import json
import re
from datetime import datetime, timezone

from foremind.fsutil import atomic_write, file_lock
from foremind.paths import state_dir

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def check_name(name) -> str:
    """Session names and agent session ids become file names: no separators, no leading dot."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"bad session name {name!r}")
    return name


def path(root, session):
    return state_dir(root) / "heartbeats" / f"{check_name(session)}.json"


def read(root, session) -> dict | None:
    try:
        hb = json.loads(path(root, session).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):  # a mangled file starts over rather than wedging every later update
        return None
    return hb if isinstance(hb, dict) else None


def update(root, session, *, open_tool: str | None = None, close_tool: str | None = None, **fields) -> dict:
    """Merge `fields` into the heartbeat and stamp ts. open_tool / close_tool = the tool_use_id a PreToolUse starts or
    a PostToolUse finishes; `open_tools=[]` in fields forgets them all (Stop, SessionStart)."""
    p = path(root, session)
    with file_lock(p.with_suffix(".lock")):
        hb = read(root, session) or {"session": session, "handoff_requested": False}
        hb.update(fields, ts=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        tools = [t for t in hb.get("open_tools") or [] if t != close_tool]
        if open_tool is not None and open_tool not in tools:
            tools.append(open_tool)
        hb.update(open_tools=tools, tool_open=bool(tools))
        atomic_write(p, json.dumps(hb, ensure_ascii=False, sort_keys=True))
    return hb
