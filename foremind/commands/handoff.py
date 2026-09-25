import json
import os
import sys
from pathlib import Path

from foremind import batchlog, handoff, lock
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    p = sub.add_parser("handoff", help="write a handoff section (--write) or take over a batch (--accept)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true", help="take the batch lock as this session")
    g.add_argument("--write", action="store_true", help="append a handoff section (JSON from --file or stdin)")
    p.add_argument("batch_id", nargs="?", help="default: the one batch whose lock this session holds")
    p.add_argument("--file")
    p.set_defaults(func=_run)


def _batch(args, root, who):
    """The batch `who` holds the lock of, when it is exactly one (FOREMIND_BATCH goes stale after a continuation,
    M1-4 r1 SF-5); a successor about to accept holds none yet and uses the batch it was opened for."""
    if args.batch_id:
        return args.batch_id
    held = lock.held_by(root, who)
    if len(held) == 1:
        return held[0]
    if args.accept and not held and os.environ.get("FOREMIND_BATCH"):
        return os.environ["FOREMIND_BATCH"]
    raise ValueError(f"no batch: pass it ({who} holds the lock of {', '.join(held) or 'no batch'})")


def _run(args):
    session = os.environ.get("FOREMIND_SESSION")
    try:
        root = find_project_root()
        if args.accept:
            if not session:
                raise ValueError("--accept runs inside the successor session (FOREMIND_SESSION is not set)")
            batch = _batch(args, root, session)
            prev = handoff.accept(root, batch, session)
            print(f"{batch}: {session} holds the lock (previous: {prev or 'none'})")
        else:
            batch = _batch(args, root, session or lock.USER)
            text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
            print(handoff.write_section(root, batch, json.loads(text), author=session or lock.USER))
    except (OSError, ProjectNotFound, ValueError, handoff.HandoffError, lock.LockError, batchlog.LogRewritten) as e:
        print(f"foremind handoff: {e}", file=sys.stderr)
        return 1
    return 0
