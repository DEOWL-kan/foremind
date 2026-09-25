import json
import os
import sys

from foremind import gate, review
from foremind.config import ConfigError
from foremind.paths import ProjectNotFound


def register(sub):
    p = sub.add_parser("gate", help="run every gate check; approve, deliver or merge the batch when they pass")
    p.add_argument("batch")
    p.add_argument("--rerun", action="store_true", help="rerun acceptance and [gate].checks even if results exist")
    p.add_argument("--no-merge", action="store_true", help="never merge, even at delivery level merge_dev")
    p.set_defaults(func=_run)


def _run(args):
    try:
        root, cfg = review.context(args.batch)
        res = gate.run(root, args.batch, cfg, role=os.environ.get("FOREMIND_ROLE"),
                       session=os.environ.get("FOREMIND_SESSION"), rerun=args.rerun, merge=not args.no_merge)
    except (review.FlowError, ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind gate: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"foremind gate: corrupt JSON in the project state (events.jsonl or a batch file): {e}", file=sys.stderr)
        return 2
    for c in res["checks"]:
        print(f"{'ok  ' if c['ok'] else 'FAIL'} {c['name']}" + (f": {c['detail']}" if c["detail"] else ""))
    for w in res["warnings"]:
        print(f"warn {w}")
    print(f"{res['verdict']} · {args.batch} is {res['state']} · {res['path']}")
    return 0 if res["verdict"] == "pass" else 1
