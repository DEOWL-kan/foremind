"""`foremind report` (DESIGN §11.3): the run report since the last one, printed and written to
.foremind/reports/ (foremind/report.py). `--plan <plan>` (repeatable, m2e REQ-14): each plan's precision and efficiency
(foremind/metrics.py), printed only: no report file, no event; a plan that does not load gets an unknown section
and exit code 1."""
import re
import sys
import time

from foremind import metrics, report
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.plan import model
from foremind.schemas import PLAN_ID


def register(sub):
    p = sub.add_parser("report", help="run report since the last one (also written to .foremind/reports/)")
    p.add_argument("--plan", action="append", metavar="PLAN",
                   help="print this plan's precision and efficiency instead (repeatable; writes nothing)")
    p.set_defaults(func=_run)


def _run(args):
    try:
        root = find_project_root()
        if args.plan:
            return _plans(root, args.plan)
        path, text, _ = report.generate(root, report.last(report.events(root)))
    except (ProjectNotFound, OSError, ValueError) as e:
        print(f"foremind report: {e}", file=sys.stderr)
        return 1
    print(text)
    print(f"（已写入 {state_dir(root) / path}）")
    return 0


def _plans(root, pids):
    """Exit 1 when a plan's directory is not there (nothing printed) or a plan does not load (its section unknown)."""
    base = state_dir(root) / "plans"
    if missing := [p for p in pids if not (re.fullmatch(PLAN_ID, p) and (base / p).is_dir())]:
        print(f"foremind report: no plan {'、'.join(missing)}", file=sys.stderr)
        return 1
    evs, now, out, code = report.events(root), time.time(), [metrics.NOTE], 0
    for pid in pids:
        out += ["", f"## 计划 {pid}"]
        try:
            bids = list(model.load(root, pid).batches)
        except (ValueError, OSError) as e:
            out.append(metrics.broken(pid, e))
            code = 1
            continue
        out += metrics.lines(metrics.collect(root, evs, bids, now), bids)
    print("\n".join(out))
    return code
