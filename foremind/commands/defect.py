"""`foremind defect <batch> --source <source> --note <note>` (m2e REQ-14, DESIGN §11.1): a defect found in a merged
batch, defect_found{batch, source, note, by}; the controller or the user, refused in a Foremind session."""
import json
import os
import re
import sys

from foremind import review
from foremind.commands.review import actor
from foremind.paths import ProjectNotFound, find_project_root
from foremind.plan import model
from foremind.schemas import BATCH_ID

SOURCES = ("review", "controller", "user", "run", "test")


def register(sub):
    p = sub.add_parser("defect", help="record a defect found in a merged batch (the controller or the user)")
    p.add_argument("batch")
    p.add_argument("--source", required=True, choices=SOURCES)
    p.add_argument("--note", required=True)
    p.set_defaults(func=_run)


def _run(args):
    try:
        if os.environ.get("FOREMIND_SESSION"):
            raise ValueError("refused inside a Foremind session (the controller or the user runs it)")
        if not args.note.strip():
            raise ValueError("--note is empty")
        if not re.fullmatch(BATCH_ID, args.batch):
            raise ValueError(f"bad batch id {args.batch!r}")
        root = find_project_root()
        st = model.read(model.batch_path(root, args.batch)).header.get("state")
        if st not in ("merged", "cataloged"):
            raise ValueError(f"{args.batch} is {st}, not merged: send a batch not yet merged back with "
                             "`foremind review <batch> --request-changes`")
        ev = review.events(root).append("defect_found", batch=args.batch, source=args.source, note=args.note.strip(),
                                        by=actor())
    except (ValueError, OSError, ProjectNotFound) as e:
        print(f"foremind defect: {e}", file=sys.stderr)
        return 1
    print(json.dumps({k: ev[k] for k in ("batch", "source", "note", "by")}, ensure_ascii=False))
    return 0
