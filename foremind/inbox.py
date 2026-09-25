"""Per-session inbox (DESIGN §2.5): `inbox/<session>.md` is append-only; `inbox/<session>.cursor` holds the byte
offset delivered so far, so a message is neither delivered twice nor skipped.

Each message is `<!-- fm-msg <id> <ts> <sender> <nbytes> -->\n<text>\n`; the byte count keeps any text unambiguous.
A deliverer (Stop hook, supervisor) holds locked(session) from pending_messages() to mark_delivered(), so two of
them never deliver the same message; a crash after delivering but before marking re-delivers it (at least once).
A crash mid-append leaves a torn tail: readers stop before it, and the next append (under the lock) cuts it off.
`root` defaults to the project found from the environment (FOREMIND_PROJECT inside Foremind sessions).
"""
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from foremind.fsutil import append_line, atomic_write, file_lock
from foremind.paths import find_project_root, state_dir

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_MARK = b"<!-- fm-msg "
_HEAD = re.compile(rb"<!-- fm-msg ([0-9a-f]{32}) (\S+) (\S+) ([0-9]+) -->\n")


class InboxCorrupt(Exception):
    pass


@dataclass
class Message:
    id: str
    ts: str
    sender: str
    text: str
    end: int  # byte offset just past this message: pass the last one's to mark_delivered


def _paths(session, root):
    if not isinstance(session, str) or not _NAME.fullmatch(session):
        raise ValueError(f"bad session name {session!r}")
    d = state_dir(Path(root) if root else find_project_root()) / "inbox"
    return d / f"{session}.md", d / f"{session}.cursor"


def locked(session, *, root=None):
    return file_lock(_paths(session, root)[0].with_suffix(".lock"))


def _cursor(cur):
    try:
        return int(cur.read_text())
    except FileNotFoundError:
        return 0


def _scan(data, pos, path):
    """Whole messages from byte `pos` on, and the offset just past the last one. A torn tail (a header or text cut
    short by a crash mid-append) ends the scan; anything else that is not a message raises InboxCorrupt."""
    out = []
    while pos < len(data):
        m = _HEAD.match(data, pos)
        if not m:
            rest = data[pos:]
            if b"\n" not in rest and (rest.startswith(_MARK) or _MARK.startswith(rest)):
                break  # header cut short
            raise InboxCorrupt(f"{path}: no message header at byte {pos}")
        start = m.end()
        end = start + int(m[4])
        if end >= len(data):
            break  # text or its closing newline cut short
        if data[end:end + 1] != b"\n":
            raise InboxCorrupt(f"{path}: message at byte {pos} does not end where its length says")
        out.append(Message(m[1].decode(), m[2].decode(), m[3].decode(), data[start:end].decode("utf-8"), end + 1))
        pos = end + 1
    return out, pos


def append(session, text, *, sender, root=None) -> str:
    if not isinstance(sender, str) or not sender or any(c.isspace() for c in sender):
        raise ValueError(f"bad sender {sender!r}")
    if not text.strip():
        raise ValueError("empty message")
    path, cur = _paths(session, root)
    mid, ts = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(timespec="seconds")
    with locked(session, root=root):  # one writer at a time, so messages never interleave
        if path.exists():
            data = path.read_bytes()
            whole = _scan(data, _cursor(cur), path)[1]
            if whole < len(data):
                os.truncate(path, whole)  # a torn tail from a crashed append (N-7): nobody can have read it
        # explicit separator: append_line adds none when the text already ends with a newline
        append_line(path, f"<!-- fm-msg {mid} {ts} {sender} {len(text.encode('utf-8'))} -->\n{text}\n")
    return mid


def pending_messages(session, *, root=None) -> list[Message]:
    path, cur = _paths(session, root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    return _scan(data, _cursor(cur), path)[0]


def mark_delivered(session, upto, *, root=None) -> None:
    """Advance the cursor to `upto`, the end of a pending message (Message.end); never moves it back. Call inside
    locked()."""
    if not isinstance(upto, int) or isinstance(upto, bool):
        raise ValueError(f"cursor {upto!r} is not a byte offset")
    path, cur = _paths(session, root)
    done = _cursor(cur)
    if upto <= done:
        return
    data = path.read_bytes() if path.exists() else b""
    if upto not in {m.end for m in _scan(data, done, path)[0]}:
        raise ValueError(f"cursor {upto} is not the end of a pending message in {path}")
    atomic_write(cur, str(upto))
