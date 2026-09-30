import json
import os
import sys
import time

from foremind import controller, review
from foremind.config import ConfigError
from foremind.paths import ProjectNotFound


def register(sub):
    p = sub.add_parser("review", help="request a review of the committed heads (push and open PRs per #23)")
    p.add_argument("batch", nargs="?", default=os.environ.get("FOREMIND_BATCH"))
    p.add_argument("--run-reviewer", action="store_true",
                   help="start the reviewer now, wait for it and write the receipt (what the supervisor does)")
    p.add_argument("--request-changes", action="store_true",
                   help="send the batch back with a must-fix list (the controller or the user, never a seat)")
    p.add_argument("--item", action="append", default=[], help="one must-fix item (repeatable, with --request-changes)")
    p.add_argument("--ab", metavar="EFFORT", choices=review.schemas.EFFORTS,
                   help="rerun the latest full review once at this effort and compare, outside the rounds and the gate "
                        "(REQ-14; the controller or the user, refused in a Foremind session)")
    p.add_argument("--withdraw", metavar="FINGERPRINT",
                   help="rule a reported issue out: from the next receipt on, a must_fix with this fingerprint is a note "
                        "(REQ-15; the controller or the user, refused in a Foremind session)")
    p.add_argument("--reason", help="why the issue is withdrawn (with --withdraw)")
    p.set_defaults(func=_run)


def actor(session=None) -> str:
    """Who sends a batch back or records a defect (m2e REQ-14): the session; else controller when FOREMIND_ROLE is
    controller or FOREMIND_CONTROLLER is set (the controller runs without FOREMIND_SESSION); else the user."""
    if session:
        return session
    return "controller" if os.environ.get("FOREMIND_ROLE") == "controller" or os.environ.get(controller.MARKER) \
        else "user"


def _run(args):
    session = os.environ.get("FOREMIND_SESSION")
    try:
        if args.run_reviewer and session and os.environ.get("FOREMIND_ROLE") != "supervisor":
            raise review.FlowError("--run-reviewer belongs to the supervisor (or the user outside any session)")
        if args.request_changes and session and os.environ.get("FOREMIND_ROLE") != "controller":
            raise review.FlowError("--request-changes belongs to the controller (or the user outside any session)")
        if args.item and not args.request_changes:
            raise review.FlowError("--item goes with --request-changes")
        if args.ab and session:  # REQ-14; the controller runs without FOREMIND_SESSION (controller.py)
            raise review.FlowError("--ab is refused inside a Foremind session (the controller or the user runs it)")
        if args.ab and (args.run_reviewer or args.request_changes):
            raise review.FlowError("--ab goes alone")
        if args.withdraw is not None and session:  # REQ-15, as --ab
            raise review.FlowError("--withdraw is refused inside a Foremind session (the controller or the user runs it)")
        if args.withdraw is not None and (args.run_reviewer or args.request_changes or args.ab):
            raise review.FlowError("--withdraw goes alone")
        if args.reason is not None and args.withdraw is None:
            raise review.FlowError("--reason goes with --withdraw")
        root, cfg = review.context(args.batch)
        if args.request_changes:
            out = {"state": review.request_changes(root, args.batch, args.item, by=actor(session))}
        elif args.withdraw is not None:
            ev = review.withdraw(root, args.batch, args.withdraw, args.reason,
                                 by=os.environ.get("FOREMIND_ROLE") or "user")
            out = {k: ev[k] for k in ("batch", "fingerprint", "reason", "by")}
        elif args.ab:
            out = review.ab(root, args.batch, cfg, args.ab, by=session or "user")
        elif args.run_reviewer:
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
