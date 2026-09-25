import os
import sys
from pathlib import Path

from foremind import batchlog
from foremind.paths import ProjectNotFound, find_project_root


def register(sub):
    p = sub.add_parser("log", help="append a section to a batch log (text from --file or stdin)")
    p.add_argument("batch_id")
    p.add_argument("--author", default=os.environ.get("FOREMIND_SESSION") or "user")
    p.add_argument("--file")
    p.set_defaults(func=_run)


def _run(args):
    try:
        text = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
        if not text.strip():
            raise ValueError("empty text")
        print(batchlog.append(find_project_root(), args.batch_id, text, author=args.author))
    except (OSError, ProjectNotFound, ValueError, batchlog.LogRewritten) as e:
        print(f"foremind log: {e}", file=sys.stderr)
        return 1
    return 0
