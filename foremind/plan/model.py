"""Plans on disk (DESIGN §1.3): `plans/<plan-id>/{goal.md, plan.md, plan.html}`, batch headers in `batches/<id>.md`.

goal.md header: `frozen_at` + `sha256` (of the body). plan.md header: schema `plan`. Batch files: schema `batch_header`.
Batch-header fields this package uses beyond that schema (the schema allows extra fields):
  contract          "true" marks a contract batch (§4.2 5a); its owns_paths are the contract files
  coupling          [{"batch", "score": 0..1, "reason"}] the planner's semantic coupling declaration (§4.2 5d)
  state, state_prior, blocked_reason   program-written (§20 I1)
  config_approved   sha256 of the `config` table the user approved (plan approve / amend --user-approved);
                    only counts while the plan is the one the last approval event recorded (task_config_approved)
"""
import json
import re
from dataclasses import dataclass

from foremind import header as hdr
from foremind import schemas
from foremind.events import EventLog
from foremind.fsutil import atomic_write, sha256_bytes
from foremind.paths import state_dir

RUNTIME_FIELDS = ("state", "state_prior", "blocked_reason")  # change while the plan runs: outside plan_hash
PROGRAM_FIELDS = (*RUNTIME_FIELDS, "config_approved")
UNSTARTED = (None, "planned", "ready")
_STATUS = re.compile(r"^## 状态[ \t]*$", re.M)
TERMINAL = ("merged", "cataloged", "cancelled")  # their paths are free again: left out of every check


class PlanError(ValueError):
    pass


@dataclass
class Doc:
    header: dict
    body: str


@dataclass
class Plan:
    id: str
    doc: Doc  # plan.md
    batches: dict  # batch id -> Doc, in plan order
    goal: Doc | None = None

    def active(self) -> dict:
        return {k: d for k, d in self.batches.items() if d.header.get("state") not in TERMINAL}


def plan_dir(root, plan_id):
    if not isinstance(plan_id, str) or not re.fullmatch(schemas.PLAN_ID, plan_id):
        raise PlanError(f"bad plan id {plan_id!r}")
    return state_dir(root) / "plans" / plan_id


def batch_path(root, batch_id):
    if not isinstance(batch_id, str) or not re.fullmatch(schemas.BATCH_ID, batch_id):
        raise PlanError(f"bad batch id {batch_id!r}")
    return state_dir(root) / "batches" / f"{batch_id}.md"


def text_hash(body: str) -> str:
    return sha256_bytes(body.encode("utf-8"))


def config_hash(h: dict) -> str:
    return text_hash(json.dumps(h.get("config", {}), sort_keys=True, ensure_ascii=False))


def spec(body: str) -> str:
    """A batch body without its `## 状态` section (runtime-written, DESIGN §1.3)."""
    return _STATUS.split(body, 1)[0]


def plan_hash(plan) -> str:
    """What an approval binds (§1.6): plan.md, every batch header but its runtime fields, batch bodies above
    `## 状态`. plan.md's goal_hash binds the goal."""
    batches = {b: [{k: x for k, x in d.header.items() if k not in RUNTIME_FIELDS}, spec(d.body)]
               for b, d in plan.batches.items()}
    return text_hash(json.dumps([plan.doc.header, plan.doc.body, batches], sort_keys=True, ensure_ascii=False))


def approved_hash(root, plan_id) -> str | None:
    """plan_hash recorded by the plan's last plan_approved / plan_amended event (archives included)."""
    sd, last = state_dir(root), None
    for log in [*sorted((sd / "archive").glob("events-*.jsonl")), sd / "events.jsonl"]:
        for e in EventLog(log).iter():
            if e.get("type") in ("plan_approved", "plan_amended") and e.get("plan") == plan_id:
                last = e.get("plan_hash")
    return last


def is_bound(root, plan) -> bool:
    """The plan is the one last approved or amended through the program (not edited by hand since)."""
    return plan_hash(plan) == approved_hash(root, plan.id)


def task_config_approved(root, plan, bid, *, bound=None) -> bool:
    """config.load's task_user_approved for batch `bid`: config_approved matches its config and the plan is bound
    (is_bound, or `bound` when the caller already knows), so a self-computed config_approved does not count."""
    h = plan.batches[bid].header
    return h.get("config_approved") == config_hash(h) and (is_bound(root, plan) if bound is None else bound)


def is_contract(h: dict) -> bool:
    return h.get("contract") == "true"


def parse(text: str, where="") -> Doc:
    try:
        return Doc(*hdr.parse(text))
    except hdr.HeaderError as e:
        raise PlanError(f"{where}: {e}") from None


def read(path) -> Doc:
    try:
        return parse(path.read_text(encoding="utf-8"), str(path))
    except FileNotFoundError:
        raise PlanError(f"{path}: missing") from None


def _checked(kind, doc, where):
    if errs := schemas.validate(kind, doc.header):
        raise PlanError(f"{where}: " + "; ".join(errs))
    return doc


def load(root, plan_id) -> Plan:
    d = plan_dir(root, plan_id)
    doc = _checked("plan", read(d / "plan.md"), d / "plan.md")
    if doc.header["plan_id"] != plan_id:
        raise PlanError(f"{d / 'plan.md'}: plan_id {doc.header['plan_id']!r} does not match its directory")
    batches = {b: _checked("batch_header", read(batch_path(root, b)), batch_path(root, b)) for b in doc.header["batches"]}
    goal = read(d / "goal.md") if (d / "goal.md").exists() else None
    return Plan(plan_id, doc, batches, goal)


def plan_ids(root) -> list[str]:
    base = state_dir(root) / "plans"
    return sorted(p.parent.name for p in base.glob("*/plan.md")) if base.is_dir() else []


def load_others(root, plan_id) -> list[Plan]:
    """Every other plan of the project: owns_paths disjointness is checked across plans (§1.3)."""
    return [load(root, p) for p in plan_ids(root) if p != plan_id]


def write_goal(root, plan_id, doc: Doc):
    atomic_write(plan_dir(root, plan_id) / "goal.md", hdr.render(doc.header, doc.body))


def write(root, plan: Plan):
    """Batches first, plan.md last: plan.md is what makes a batch part of the plan."""
    _checked("plan", plan.doc, "plan.md")
    for bid, d in plan.batches.items():
        _checked("batch_header", d, bid)
    for bid, d in plan.batches.items():
        atomic_write(batch_path(root, bid), hdr.render(d.header, d.body))
    atomic_write(plan_dir(root, plan.id) / "plan.md", hdr.render(plan.doc.header, plan.doc.body))
