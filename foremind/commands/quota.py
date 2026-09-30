"""`foremind quota [--reset [--group long|oneshot]]` (DESIGN §10.5): the quota state of each role group, the
account's (quota.state_path, shared by every project). `--reset` drops the stored state of both groups, or of one
(the `paused` list stays), so the next tick judges afresh; event `quota_reset` in this project. Dropping an exhausted
long group sets `exhausted_ended`, as the tick does when exhaustion ends. The command is the user's: refused inside a
Foremind session. `--reset` holds the global lock, as a tick does while it reads and writes the state, and first
folds in this project's quota.json of before m2b.10 (quota.migrate), which would otherwise bring the state back."""
import json
import os
import sys
import time

from foremind.events import EventLog
from foremind.fsutil import LockBusy, global_lock
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.supervisor import quota


def register(sub):
    p = sub.add_parser("quota", help="quota state per role group; --reset drops it")
    p.add_argument("--reset", action="store_true", help="drop the stored state so the next tick judges afresh")
    p.add_argument("--group", choices=quota.GROUPS, help="with --reset: only this group (default: both)")
    p.set_defaults(func=_run)


def _fail(e) -> int:
    print(f"foremind quota: {e}", file=sys.stderr)
    return 1


def _run(args):
    if args.group and not args.reset:
        return _fail("--group needs --reset")
    if os.environ.get("FOREMIND_SESSION"):  # the whole command is the user's (#18), showing included
        return _fail("only the user runs foremind quota, not a Foremind session")
    try:
        root = find_project_root()
    except ProjectNotFound as e:
        return _fail(e)
    keys = [f"{quota.ACCOUNT}/{g}" for g in ([args.group] if args.group else quota.GROUPS)]
    if not args.reset:
        groups = quota.view(root)["groups"]  # with the project's file of before m2b.10, until a tick migrates it
        for k in keys:
            st = groups.get(k)
            print(f"{k}: {json.dumps(st, ensure_ascii=False, sort_keys=True) if st else '没有记录（unknown）'}")
        return 0
    try:
        with global_lock():
            q = quota.load()
            quota.migrate(root, q)
            groups = q["groups"] = q.get("groups") if isinstance(q.get("groups"), dict) else {}
            for k in keys:
                if (groups.pop(k, None) or {}).get("state") == quota.EXHAUSTED and k.endswith("/long"):
                    q["exhausted_ended"] = time.time()  # as tick.qset: stuck clocks restart, resume keys are new
            quota.save(q)
            EventLog(state_dir(root) / "events.jsonl").append("quota_reset", groups=keys)
    except LockBusy:
        return _fail("a supervisor pass holds the global lock; try again in a moment")
    print(f"已重置 {'、'.join(keys)}；下一次 tick 重新判定")
    return 0
