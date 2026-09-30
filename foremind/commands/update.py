import json
import os
import sys

from foremind import delivery, review
from foremind.config import ConfigError
from foremind.paths import ProjectNotFound

UPDATERS = ("controller", "supervisor")  # and the user outside any session; never a seat (§7.6)


def register(sub):
    p = sub.add_parser("update", help="bring an approved batch's branches up to date with their targets (§7.6)")
    p.add_argument("batch")
    p.set_defaults(func=_run)


def _run(args):
    session, role = os.environ.get("FOREMIND_SESSION"), os.environ.get("FOREMIND_ROLE")
    try:
        if role == "seat" or (session and role not in UPDATERS):
            raise review.FlowError("update belongs to the user, the controller or the supervisor, never a seat")
        root, cfg = review.context(args.batch)
        out = delivery.update(root, args.batch, cfg, by=session or role or "user")
    except (review.FlowError, ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind update: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"foremind update: corrupt JSON in the project state (events.jsonl or a batch file): {e}",
              file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False))
    return 1 if out["outcome"] in ("conflict", "rereview") else 0  # busy is not a failure
