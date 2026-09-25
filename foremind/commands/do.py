"""`foremind do "<需求>"`: the S-tier fast path (DESIGN §4.1, §4.2 S1). Writes a frozen one-line goal and a
one-batch plan whose batch is `planned`; the user's command is both the intent (#0) and the approval. Opening the
seat is the supervisor's job (M1-4 / M1-7), not this command's."""
import re
import sys
from datetime import datetime, timezone

from foremind import config as cfg
from foremind import repos as rp
from foremind import schemas
from foremind.events import EventLog
from foremind.fsutil import project_lock
from foremind.paths import ProjectNotFound, find_project_root, state_dir
from foremind.plan import model, render
from foremind.plan import validate as v
from foremind.plan.freeze import Rejected
from foremind.plan.model import Doc, Plan, PlanError

MODEL = "claude-opus-5-5"  # §3.2 「强」 for Claude; S-tier seat effort is medium


def register(sub):
    p = sub.add_parser("do", help="S-tier fast path: a one-batch plan, planned (starts no session)")
    p.add_argument("requirement")
    p.add_argument("--owns", action="append", required=True, metavar="REPO:PATH", help="owns_paths (repeatable)")
    p.add_argument("--accept", action="append", required=True, metavar="CMD", help="acceptance command (repeatable)")
    p.add_argument("--id", dest="plan_id", help="plan id (default do-<n>)")
    p.add_argument("--mode", choices=schemas.MODES, default="auto")
    p.add_argument("--model", default=MODEL)
    p.set_defaults(func=_run)


def _next_id(root):
    ns = [int(m[1]) for p in model.plan_ids(root) if (m := re.fullmatch(r"do-([0-9]+)", p))]
    return f"do-{max(ns, default=0) + 1}"


def create(root, requirement, *, owns, accept, plan_id=None, mode="auto", model_name=MODEL, config=None) -> Plan:
    requirement = requirement.strip()
    if not requirement or "\n" in requirement:
        raise PlanError("the requirement is one non-empty line")
    config = config if config is not None else cfg.load(root)
    known = rp.load_repos(root, config)
    repos = sorted({rp.split_qualified(q, known)[0] for q in owns})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with project_lock(root):
        plan_id = plan_id or _next_id(root)
        if (model.plan_dir(root, plan_id) / "plan.md").exists():
            raise PlanError(f"plan {plan_id} already exists")
        body = f"# 目标\n\nREQ-1: {requirement}\n\n验收命令：\n" + "".join(f"- `{c}`\n" for c in accept)
        goal = Doc({"frozen_at": now, "sha256": model.text_hash(body)}, body)
        bid = f"{plan_id}.1"
        header = {
            "id": bid, "plan_id": plan_id, "reqs": ["REQ-1"], "repos": repos, "owns_paths": list(owns), "reads": [],
            "depends_on": [], "merge_after": [], "start_commands": [], "accept_commands": list(accept),
            "tiers": {"difficulty": "S", "org": "exec_review", "review": "zero_context", "model": model_name,
                      "effort": "medium", "reason": "S 档快速路径：用户用 foremind do 直接下达"},
            "mode": mode, "hard_block": [], "budget_estimate": str(v.effective_budget(config) // 2),
            "must_read": [], "tools": [], "state": "planned",
        }
        plan = Plan(plan_id, Doc({"plan_id": plan_id, "goal_hash": goal.header["sha256"], "approved_at": now,
                                  "approved_by": "user", "batches": [bid], "revisions": []},
                                 "由 `foremind do` 生成的单批轻量计划（S 档），目标见 goal.md。\n"),
                    {bid: Doc(header, "## 状态\n")}, goal)
        others = model.load_others(root, plan_id)
        kw = dict(others=others, config=config, user_approved=True, config_bound=True)  # bound once written
        report = v.validate(root, plan, **kw)
        if v.apply_edges(plan, report):  # overlapping another plan's batch: run after it (§4.2 5d)
            report = v.validate(root, plan, **kw)
        if not report["ok"]:
            raise Rejected(report)
        model.write_goal(root, plan_id, goal)
        model.write(root, plan)
        EventLog(state_dir(root) / "events.jsonl").append(
            "plan_approved", plan=plan_id, approved_by="user", via="do", goal_hash=goal.header["sha256"],
            plan_hash=model.plan_hash(plan))
    return plan


def _run(args):
    try:
        root = find_project_root()
        plan = create(root, args.requirement, owns=args.owns, accept=args.accept, plan_id=args.plan_id,
                      mode=args.mode, model_name=args.model)
    except Rejected as e:
        print(render.rejected(e.report), file=sys.stderr, end="")
        return 1
    except (ProjectNotFound, PlanError, cfg.ConfigError, OSError, ValueError) as e:
        print(f"foremind do: {e}", file=sys.stderr)
        return 1
    bid = next(iter(plan.batches))
    print(f"{plan.id}: {bid} planned ({model.batch_path(root, bid)})")
    return 0
