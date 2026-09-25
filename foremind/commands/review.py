import json
import os
import sys
import time

from foremind import review
from foremind.config import ConfigError
from foremind.paths import ProjectNotFound


def register(sub):
    p = sub.add_parser("review", help="request a review of the committed heads (push and open PRs per #23)")
    p.add_argument("batch", nargs="?", default=os.environ.get("FOREMIND_BATCH"))
    p.add_argument("--run-reviewer", action="store_true",
                   help="start the reviewer now, wait for it and write the receipt (what the supervisor does)")
    p.set_defaults(func=_run)


def _run(args):
    session = os.environ.get("FOREMIND_SESSION")
    try:
        if args.run_reviewer and session and os.environ.get("FOREMIND_ROLE") != "supervisor":
            raise review.FlowError("--run-reviewer belongs to the supervisor (or the user outside any session)")
        root, cfg = review.context(args.batch)
        if args.run_reviewer:
            sid = review.start(root, args.batch, cfg, started_by=session or "user")
            while (out := review.harvest(root, args.batch, sid, cfg)) is None:
                time.sleep(0.2)
            out = {k: out[k] for k in ("round", "verdict", "issues")}
        else:
            out = review.request(root, args.batch, cfg, session=session)
    except (review.FlowError, ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind review: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"foremind review: corrupt JSON in the project state (events.jsonl or a batch file): {e}",
              file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0
