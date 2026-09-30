"""Planner sessions (DESIGN §4, REQ-8, REQ-9): `plan new` opens one, `plan submit` takes its drafts in.

Drafts live next to the worktree root, `drafts/<slug>/<plan>/`: outside the project and every repo, where the guard
lets a session write, while the planner writes neither .foremind/ (§20 I36) nor a repo. Layout: see LAYOUT.
submit runs S6 on the drafts in memory and writes nothing unless it passes; then it freezes the goal, writes the plan
(plan.md's header is the program's; batch headers lose PROGRAM_FIELDS) and the handoff documents.
"""
import contextlib
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path

from foremind import config as cfg_mod
from foremind import repos as rp
from foremind import schemas, seat, worktree
from foremind.carriers import SessionExists
from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock
from foremind.paths import state_dir
from foremind.plan import coupling, freeze, model
from foremind.plan import validate as v
from foremind.plan.model import Doc, Plan, PlanError
from foremind.vendors import get as get_vendor

ROLE_CARD = Path(__file__).resolve().parent.parent / "templates" / "roles" / "planner.md"
MODEL = "claude-opus-5-5"
EFFORT = {"S": "high", "M": "xhigh", "L": "xhigh"}  # §3.2
SUBMITTERS = (None, "planner", "controller")  # FOREMIND_ROLE; unset is the user
LAYOUT = """布局：
- goal.md：需求 REQ-<n>，每条至少一个 WHEN … THEN …，能写成命令的给命令；不写头，程序冻结
- intent.md（可选）：S1 复述
- batches/<id>.md：批次头（schema batch_header）+ 正文；id 为 <计划>.<n>；state 等程序字段不写
- batches/<id>.handoff.md：该批交接文档，每批一份"""


def _events(root):
    return EventLog(state_dir(root) / "events.jsonl")


def drafts_dir(slug, plan_id) -> Path:
    return worktree.wt_root().parent / "drafts" / slug / plan_id


def report(root, config, plan, **kw) -> dict:
    """S6 with the coupling analysis: what `plan validate` prints and `plan submit` gates on."""
    repos = {r.id: r.path for r in rp.load_repos(root, config)}
    bad = schemas.validate("plan", plan.doc.header) or any(schemas.validate("batch_header", d.header)
                                                          for d in plan.batches.values())
    return v.validate(root, plan, others=model.load_others(root, plan.id), config=config,  # it reports bad headers
                      coupling=None if bad else coupling.analyze(plan, repos, config), **kw)


def _approved(root, plan_id) -> bool:
    p = model.plan_dir(root, plan_id) / "plan.md"
    return p.exists() and "approved_at" in model.read(p).header


def _next_id(root, slug):
    names = model.plan_ids(root) + [p.name for p in (worktree.wt_root().parent / "drafts" / slug).glob("*")]
    return f"p-{1 + max((int(m[1]) for n in names if (m := re.fullmatch(r'p-([0-9]+)', n))), default=0)}"


def kickoff_text(plan_id, requirement, draft, tier, root) -> str:
    d = shlex.quote(str(draft))
    return (f"你是计划 {plan_id} 的规划者（{tier} 档）。下面是你的角色卡，按它走。\n\n"
            f"{ROLE_CARD.read_text(encoding='utf-8').strip()}\n\n## 需求原文\n\n{requirement}\n\n"
            f"## 项目\n\n项目根（主检出，只读；工作目录是草稿目录，项目的规则与代码到这里读）：{root}\n\n"
            f"## 草稿\n\n目录：{draft}\n{LAYOUT}\n\n"
            f"提交：`foremind plan submit {plan_id} --dir {d}`。不通过会列出原因且不写任何文件，改完再提交，"
            f"最多 3 轮。通过后请用户执行 `foremind plan show {plan_id} --open` 审阅，再 "
            f"`foremind plan approve {plan_id}` 批准。")


def open_planner(root, requirement, *, carrier, plan_id=None, tier="M", config=None) -> dict:
    """Launch a planner session on a fresh or unapproved plan, wait for its heartbeat, deliver the kickoff."""
    root = Path(root).resolve()
    requirement = requirement.strip()
    if not requirement:
        raise PlanError("the requirement is empty")
    cfg = config if config is not None else cfg_mod.load(root)
    model_name = cfg.get("routes.planner.model", MODEL)
    effort = cfg.get("routes.planner.effort", EFFORT[tier])
    seat._check_model(cfg, model_name)
    slug = seat.project_slug(root, cfg)
    with project_lock(root):
        plan_id = plan_id or _next_id(root, slug)
        if _approved(root, plan_id):  # plan_dir also checks the id
            raise PlanError(f"{plan_id} is approved; change it with plan amend")
        draft = drafts_dir(slug, plan_id)
        (draft / "batches").mkdir(parents=True, exist_ok=True)
        what = f"plan-{plan_id}"
        session = seat.session_name(slug, what, seat._next_seq(root, seat.session_name(slug, what, "")))
        # cwd is the draft; the user's main checkout is readable through --add-dir, its CLAUDE.md loaded (multi_repo).
        # Writes there: guard._matrix denies; git add/commit: not in the planner's allowlist, and the guard denies them
        # (a cd into the checkout does not help)
        spec = get_vendor(seat._vendor(model_name)).launch(  # writes the settings file: the name is taken
            session=session, batch="", role="planner", project_root=root, cwd=draft, add_dirs=[root], multi_repo=True,
            settings_path=seat.settings_path(root, session), permission_mode=seat.setting(cfg, "seat.permission_mode"),
            model=model_name, effort=effort)
    timeout = seat.setting(cfg, "seat.manual_sessionstart_timeout_s" if carrier.name == "manual"
                           else "seat.sessionstart_timeout_s")
    t0, launched = time.time(), True
    try:
        try:
            carrier.create(session, spec)
        except SessionExists:
            launched = False  # someone else's session: never close it
            raise
        if seat.wait_heartbeat(root, session, None, t0, timeout) is None:
            raise seat.SeatError(f"no heartbeat from {session} within {timeout} s")
        carrier.deliver(session, kickoff_text(plan_id, requirement, draft, tier, root))
    except BaseException:
        if launched:
            with contextlib.suppress(Exception):
                carrier.close(session)
        raise
    out = {"plan": plan_id, "session": session, "draft": str(draft), "model": model_name, "effort": effort}
    _events(root).append("planner_opened", tier=tier, carrier=carrier.name, **out)
    return out


def _check_owner(root, plan_id, bid):
    p = model.batch_path(root, bid)  # checks the id
    if p.exists() and model.read(p).header.get("plan_id") != plan_id:
        raise PlanError(f"{bid} belongs to another plan; pick another batch id")


def _read(path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PlanError(f"{path}: missing") from None


def _revisions(root, plan_id) -> list:
    """plan.md's revisions (a #9 approval among them), kept across submits; a draft's own are program fields."""
    p = model.plan_dir(root, plan_id) / "plan.md"
    return model.read(p).header.get("revisions", []) if p.exists() else []


def load_draft(root, plan_id, draft) -> tuple[Plan, dict, str | None]:
    """(plan in memory, batch id -> handoff text, intent text) from the draft directory."""
    draft = Path(draft)
    goal_body = model.read(draft / "goal.md").body  # a header, if the planner wrote one, is dropped
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    goal = Doc({"frozen_at": now, "sha256": model.text_hash(goal_body)}, goal_body)
    files = [p for p in (draft / "batches").glob("*.md") if not p.name.endswith(".handoff.md")]
    batches, handoffs = {}, {}
    for p in files:
        bid = p.name[:-3]
        _check_owner(root, plan_id, bid)
        doc = model.parse(_read(p), str(p))
        if doc.header.get("id") != bid:
            raise PlanError(f"{p}: id {doc.header.get('id')!r} does not match the file name")
        for k in model.PROGRAM_FIELDS:
            doc.header.pop(k, None)
        batches[bid] = doc
        handoffs[bid] = _read(p.with_name(f"{bid}.handoff.md"))
    order = sorted(batches, key=lambda b: (b.rsplit(".", 1)[0], int(b.rsplit(".", 1)[1])))
    doc = Doc({"plan_id": plan_id, "goal_hash": goal.header["sha256"], "batches": order,
               "revisions": _revisions(root, plan_id)},
              "由规划者会话起草、经 `foremind plan submit` 提交的计划，目标见 goal.md。\n")
    intent = draft / "intent.md"
    return (Plan(plan_id, doc, {b: batches[b] for b in order}, goal), handoffs,
            intent.read_text(encoding="utf-8") if intent.exists() else None)


def submit(root, plan_id, draft, *, config=None) -> tuple[Plan, dict]:
    """S6 on the drafts; freeze.Rejected (nothing written) or the plan written unapproved. Returns (plan, report)."""
    role = os.environ.get("FOREMIND_ROLE") or None
    if role not in SUBMITTERS:
        raise PlanError(f"plan submit is for the user, the planner or the controller, not role {role}")
    root = Path(root).resolve()
    config = config if config is not None else cfg_mod.load(root)
    if _approved(root, plan_id):
        raise PlanError(f"{plan_id} is approved; change it with plan amend")
    plan, handoffs, intent = load_draft(root, plan_id, draft)
    rep = report(root, config, plan)
    if not rep["ok"]:
        raise freeze.Rejected(rep)
    # ponytail: S6 ran outside the lock (freeze_goal takes it and flock is not reentrant); another plan written in
    # between is caught by the next plan validate / approve, which re-run S6. Batch file ownership is re-checked
    # under the lock below, so a concurrent plan's batch files are never overwritten or deleted.
    freeze.freeze_goal(root, plan_id, plan.goal.body)
    pdir = model.plan_dir(root, plan_id)
    with project_lock(root):
        prev = model.read(pdir / "plan.md").header if (pdir / "plan.md").exists() else None
        if prev and "approved_at" in prev:
            raise PlanError(f"{plan_id} was approved meanwhile; change it with plan amend")
        if prev:  # re-read under the lock: an amend may have added one since load_draft
            plan.doc.header["revisions"] = prev.get("revisions", [])
        stale = set(prev["batches"] if prev else ()) - set(plan.batches)  # dropped from an unapproved draft
        for bid in [*plan.batches, *stale]:
            _check_owner(root, plan_id, bid)
        for bid, text in handoffs.items():  # before plan.md, which model.write writes last
            atomic_write(state_dir(root) / "batches" / f"{bid}.handoff.md", text)
        if intent is not None:
            atomic_write(pdir / "intent.md", intent)
        model.write(root, plan)
        for bid in stale:
            model.batch_path(root, bid).unlink(missing_ok=True)
            (state_dir(root) / "batches" / f"{bid}.handoff.md").unlink(missing_ok=True)
        _events(root).append("plan_submitted", plan=plan_id, batches=list(plan.batches), role=role or "user",
                             goal_hash=plan.doc.header["goal_hash"], plan_hash=model.plan_hash(plan))
    return plan, rep
