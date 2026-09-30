"""`foremind plan new|submit|validate|show|approve|amend|freeze` (DESIGN §4.2 S2, S6, S8, S9). `plan new` opens a
planner session and `plan submit` takes its drafts in (foremind.planner); DESIGN §13.5 writes `plan "<需求>"`, which
would collide with the subcommands."""
import sys
import webbrowser
from pathlib import Path

from foremind import carriers
from foremind import config as cfg
from foremind import planner, seat
from foremind.fsutil import atomic_write, project_lock
from foremind.paths import ProjectNotFound, find_project_root
from foremind.plan import freeze, model, render
from foremind.plan import validate as v
from foremind.state import IllegalTransition


def register(sub):
    p = sub.add_parser("plan", help="open a planner, submit, validate, show, approve, amend plans; freeze goals")
    ps = p.add_subparsers(dest="plan_command", required=True, metavar="<subcommand>")
    s = ps.add_parser("new", help="open a planner session on a requirement")
    s.add_argument("requirement")
    s.add_argument("--id", dest="plan_id", help="plan id (default p-<n>)")
    s.add_argument("--tier", choices=("S", "M", "L"), default="M")
    s.set_defaults(func=_guard(_new))
    s = ps.add_parser("submit", help="S6 on a planner's drafts; written only if it passes")
    s.add_argument("plan_id")
    s.add_argument("--dir", required=True, help="the draft directory")
    s.set_defaults(func=_guard(_submit))
    s = ps.add_parser("validate", help="S6 machine checks")
    s.add_argument("plan_id")
    s.add_argument("--apply", action="store_true", help="write the depends_on edges that serialise overlapping batches")
    s.set_defaults(func=_guard(_validate))
    s = ps.add_parser("show", help="summary table and the read-only HTML page")
    s.add_argument("plan_id")
    s.add_argument("--width", type=int, help="max batches per wave")
    s.add_argument("--open", action="store_true", help="open plan.html in the browser")
    s.set_defaults(func=_guard(_show))
    s = ps.add_parser("approve", help="the user's approval: batches become planned")
    s.add_argument("plan_id")
    s.set_defaults(func=_guard(_approve))
    s = ps.add_parser("amend", help="revise a plan (re-runs S6)")
    s.add_argument("plan_id")
    s.add_argument("--reason", required=True)
    s.add_argument("--batch", action="append", default=[], metavar="FILE", help="a full batch file to add or replace")
    s.add_argument("--drop", action="append", default=[], metavar="BATCH", help="cancel a batch")
    s.add_argument("--goal", metavar="FILE", help="new goal text (needs --user-approved once frozen)")
    s.add_argument("--user-approved", action="store_true", help="the user approved this revision")
    s.set_defaults(func=_guard(_amend))
    s = ps.add_parser("freeze", help="freeze goal.md (frozen_at + content hash)")
    s.add_argument("plan_id")
    s.add_argument("--file", help="goal text to freeze (default: goal.md as it is)")
    s.set_defaults(func=_guard(_freeze))


def _guard(fn):
    def run(args):
        try:
            root = find_project_root()
            return fn(args, root, cfg.load(root))
        except freeze.Rejected as e:
            print(render.rejected(e.report), file=sys.stderr, end="")
            return 1
        except (ProjectNotFound, model.PlanError, cfg.ConfigError, IllegalTransition, seat.SeatError,
                carriers.CarrierError, OSError, ValueError) as e:
            print(f"foremind plan: {e}", file=sys.stderr)
            return 1
    return run


def _new(args, root, config):
    carrier = carriers.get(seat.setting(config, "carrier.kind"), root, config)
    out = planner.open_planner(root, args.requirement, carrier=carrier, plan_id=args.plan_id, tier=args.tier,
                               config=config)
    print(f"{out['plan']}: 规划者会话 {out['session']}（{out['model']} / {out['effort']}），草稿目录 {out['draft']}")
    return 0


def _submit(args, root, config):
    plan, report = planner.submit(root, args.plan_id, args.dir, config=config)
    print(render.summary(plan, report) + f"已写入 {plan.id}（未批准）。审阅：foremind plan show {plan.id} --open；"
          f"批准：foremind plan approve {plan.id}")
    return 0


def _validate(args, root, config):
    with project_lock(root):
        plan = model.load(root, args.plan_id)
        report = planner.report(root, config, plan)
        if args.apply and report["suggested_edges"]:
            if "approved_at" in plan.doc.header:
                raise model.PlanError(f"{plan.id} is approved: add the edges with plan amend")
            v.apply_edges(plan, report)
            model.write(root, plan)
            report = planner.report(root, config, plan)
    print(render.summary(plan, report), end="")
    return 0 if report["ok"] else 1


def _show(args, root, config):
    plan = model.load(root, args.plan_id)
    report = planner.report(root, config, plan, width=args.width)
    path = model.plan_dir(root, plan.id) / "plan.html"
    atomic_write(path, render.html(plan, report))
    print(render.summary(plan, report) + f"审阅页：{path}")
    if args.open:
        webbrowser.open(path.resolve().as_uri())
    return 0


def _approve(args, root, config):
    report = freeze.approve(root, args.plan_id, config=config)
    print(f"{args.plan_id}: approved" + "".join(f"\n提示：{w}" for w in report["warnings"]))
    return 0


def _amend(args, root, config):
    batches = [model.parse(Path(f).read_text(encoding="utf-8"), f) for f in args.batch]
    goal = Path(args.goal).read_text(encoding="utf-8") if args.goal else None
    report = freeze.amend(root, args.plan_id, reason=args.reason, batches=batches, drop=args.drop, goal=goal,
                          user_approved=args.user_approved, config=config)
    print(f"{args.plan_id}: amended" + "".join(f"\n提示：{w}" for w in report["warnings"]))
    return 0


def _freeze(args, root, config):
    body = Path(args.file).read_text(encoding="utf-8") if args.file else None
    print(freeze.freeze_goal(root, args.plan_id, body))
    return 0
