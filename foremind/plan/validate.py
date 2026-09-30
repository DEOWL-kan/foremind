"""S6 machine checks (DESIGN §4.2 S6); no model calls.

validate() returns a report:
  ok               no errors and no unresolved overlaps
  errors, warnings [str]
  overlaps         [{"a", "b", "paths": [[pa, pb], ...]}] unreachable batch pairs whose owns_paths overlap
  suggested_edges  [{"batch", "depends_on"}] the depends_on edges that serialise them (§4.2 5d hard rule);
                   apply_edges() writes those on this plan's batches into the headers
  waves, critical_path, critical_path_length, max_width, serial_reasons [{"batch", "depends_on", "reason"}]
  coupling         the pairs from coupling.analyze(), when given
Batches in a terminal state (merged, cataloged, cancelled) take part in the path, budget and wording checks no more;
REQ coverage and the batch count still include all but cancelled ones. Approved plans' batches are checked against
this plan's: disjointness runs across plans (§1.3); an overlap with a draft plan is only a warning.
"""
import re
import shlex

from foremind import config as cfg
from foremind import defaults
from foremind import pathmatch
from foremind import repos as rp
from foremind import schemas
from foremind.plan import schedule
from foremind.plan.model import UNSTARTED, is_bound, is_contract, spec, task_config_approved, text_hash
from foremind.state import BATCH

MAX_BATCHES = 10
FORBIDDEN = ("占位", "简化版", "以后再做", "后续再做", "TODO", "FIXME", "TBD", "placeholder", "v1")
# ASCII words by word boundary where path and version characters count as part of the word: `api/v1/`, `v1.2`
# and `foo_v1` are not hits, "这是 v1。" and "v1." at a sentence end are
_WORD = r"(?<![\w/.:-]){}(?![\w/-]|\.\w)"
_FORBIDDEN = re.compile("|".join(_WORD.format(re.escape(w)) if w.isascii() else re.escape(w) for w in FORBIDDEN),
                        re.I | re.ASCII)
_PROSE_KEYS = ("reason", "why", "step")  # header strings that are prose; commands and paths are not checked
ALLOW_WORD = "<!-- fm-allow-word -->"  # a body line carrying it is not checked for forbidden words; header fields always are
REQ = re.compile(r"\bREQ-[1-9][0-9]*\b")


def effective_budget(config: dict) -> int:
    """§6.3: min(window × hard threshold, absolute cap); unset keys as foremind/defaults.py has them (the runtime's)."""
    d = defaults.table()
    return int(min(config.get("context.window_tokens", d["context.window_tokens"])
                   * config.get("context.hard_pct", d["context.hard_pct"]) / 100,
                   config.get("context.abs_cap_tokens", d["context.abs_cap_tokens"])))


def owns_overlap(xs, ys) -> list[list[str]]:
    return [[x, y] for x in xs for y in ys if pathmatch.overlap(x, y)]


def _prose(doc):
    body = re.sub(r"```.*?```|~~~.*?~~~", "", spec(doc.body), flags=re.S)  # the status section is runtime-written
    body = "\n".join(line for line in body.splitlines() if ALLOW_WORD not in line)
    yield re.sub(r"`[^`\n]*`", "", body)
    stack = [(None, doc.header)]
    while stack:
        key, v = stack.pop()
        if key == "revisions":
            continue  # history, not the plan
        if isinstance(v, dict):
            stack += v.items()
        elif isinstance(v, list):
            stack += [(key, x) for x in v]
        elif isinstance(v, str) and key in _PROSE_KEYS:
            yield v


def _ancestors(deps):
    anc = {}
    for b in schedule.topo(deps):
        anc[b] = set().union(*({d} | anc[d] for d in deps[b]))
    return anc


def _unittest_names(cmd):
    """Dotted names `python3 -m unittest` is given in `cmd` (not discover)."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return []
    out = []
    for i in range(len(words) - 1):
        if words[i:i + 2] != ["-m", "unittest"]:
            continue
        args = words[i + 2:]
        if args[:1] == ["discover"]:
            continue
        for prev, w in zip(["unittest", *args], args):
            if w in ("&&", "||", ";", "|"):
                break
            if prev != "-k" and not w.startswith("-") and "." in w and "/" not in w and not w.endswith(".py"):
                out.append(w)
    return out


def _unittest_warnings(b, h, repo_paths):
    """`python3 -m unittest a.b` does not find a/b.py when a/ is not a package (M2 pilot finding 6)."""
    out = []
    for cmd in h["accept_commands"]:
        for name in _unittest_names(cmd):
            top = name.split(".")[0]
            if any((p / top).is_dir() and not (p / top / "__init__.py").exists()
                   for r, p in repo_paths.items() if r in h["repos"]):
                out.append(f"{b}: accept command {cmd!r}: {top}/ has no __init__.py, so `-m unittest {name}` may not "
                           f"load it; write python3 -m unittest discover -s {top} -p '<file>.py'")
    return out


def _widened(root, h):
    """Task config keys that load only with the user's approval (§1.5)."""
    out = []
    for k, x in cfg.task_layer(h).items():
        try:
            cfg.load(root, {k: x})
        except cfg.ConfigError:
            out.append(k)
    return out


def _coupling_ok(c, b, plan):
    s = c.get("score") if isinstance(c, dict) else None
    return (isinstance(c, dict) and c.get("batch") in plan.batches and c["batch"] != b
            and isinstance(s, (int, float)) and not isinstance(s, bool) and 0 <= s <= 1
            and isinstance(c.get("reason"), str) and bool(c["reason"].strip()))


def _check_batches(root, plan, mine, config, user_approved, config_bound, errors, warnings):
    goal_reqs = set()
    if plan.goal is None:
        errors.append("goal.md: missing; freeze the goal first")
    else:
        g, h = plan.goal.header, text_hash(plan.goal.body)
        if "frozen_at" not in g:
            errors.append("goal.md: not frozen")
        elif g.get("sha256") != h:
            errors.append("goal.md: changed after it was frozen; changing a frozen goal needs user approval (#9)")
        elif plan.doc.header["goal_hash"] != h:
            errors.append("plan.md: goal_hash is not the frozen goal's hash; restore goal.md, "
                          "or plan amend --goal --user-approved")
        goal_reqs = set(REQ.findall(plan.goal.body))
        if not goal_reqs:
            errors.append("goal.md: no REQ-n")
    live = {b: d for b, d in plan.batches.items() if d.header.get("state") != "cancelled"}  # finished ones count
    if len(live) > MAX_BATCHES:
        errors.append(f"{len(live)} batches > {MAX_BATCHES}: split into milestones (several plans)")
    covered = set().union(*(d.header["reqs"] for d in live.values()))
    errors += [f"{r}: not covered by any batch" for r in sorted(goal_reqs - covered)]
    half = effective_budget(config) // 2
    approved_plan = "approved_at" in plan.doc.header
    if not approved_plan:  # states are the program's, from approval on; amend --drop cancels
        errors += [f"{b}: state {d.header['state']!r} before the plan is approved" for b, d in plan.batches.items()
                   if d.header.get("state", "cancelled") != "cancelled"]
    for where, doc in [("plan.md", plan.doc), *mine.items()]:
        hits = {m.group() for text in _prose(doc) for m in _FORBIDDEN.finditer(text)}
        errors += [f"{where}: forbidden word {w!r} in prose" for w in sorted(hits)]
    repo_paths = {r.id: r.path for r in rp.load_repos(root, config)}
    for b, d in mine.items():
        h = d.header
        warnings += _unittest_warnings(b, h, repo_paths)
        if h["plan_id"] != plan.id:
            errors.append(f"{b}: plan_id {h['plan_id']!r} is not {plan.id!r}")
        if h.get("state", "planned") not in BATCH.states:
            errors.append(f"{b}: unknown state {h['state']!r}")
        if h.get("contract", "false") not in ("true", "false"):
            errors.append(f"{b}: contract must be true or false")
        if goal_reqs and (unknown := sorted(set(h["reqs"]) - goal_reqs)):
            errors.append(f"{b}: {', '.join(unknown)} not in goal.md")
        if int(h["budget_estimate"]) > half:
            errors.append(f"{b}: budget_estimate {h['budget_estimate']} > half the effective budget ({half})")
        cs = h.get("coupling", [])
        for i, c in enumerate(cs if isinstance(cs, list) else [None]):
            if not _coupling_ok(c, b, plan):
                errors.append(f"{b}: coupling[{i}] needs batch (another batch of this plan), score 0..1 and reason")
        if "config" in h:  # §1.5: a task may only tighten authorisation keys unless the user approved it
            stamped = task_config_approved(root, plan, b, bound=config_bound)
            try:
                cfg.load(root, cfg.task_layer(h), task_user_approved=user_approved or stamped)
            except cfg.ConfigError as e:
                try:
                    cfg.load(root, cfg.task_layer(h), task_user_approved=True)
                except cfg.ConfigError as e2:
                    errors.append(f"{b}: task config: {e2}")
                else:
                    msg = f"{b}: task config needs user approval (plan approve / amend --user-approved): {e}"
                    (errors if approved_plan else warnings).append(msg)
            else:
                if user_approved and not stamped:  # this approval is what lets it load
                    warnings += [f"批准将放宽 {b} 的 {k}" for k in _widened(root, h)]


def validate(root, plan, *, others=(), config=None, coupling=None, user_approved=False, width=None,
             config_bound=None) -> dict:
    """`config_bound`: whether the plan's config_approved stamps count (model.is_bound); default: ask the events."""
    config = config or {}
    report = {"ok": False, "errors": [], "warnings": [], "overlaps": [], "suggested_edges": [], "waves": [],
              "critical_path": [], "critical_path_length": 0, "max_width": 0, "serial_reasons": [],
              "coupling": coupling or []}
    errors, warnings = report["errors"], report["warnings"]
    errors += [f"plan.md: {e}" for e in schemas.validate("plan", plan.doc.header)]
    errors += [f"{b}: {e}" for b, d in plan.batches.items() for e in schemas.validate("batch_header", d.header)]
    if errors:
        return report  # the checks below need well-formed headers
    if plan.doc.header["plan_id"] != plan.id or plan.doc.header["batches"] != list(plan.batches):
        errors.append("plan.md: plan_id / batches do not match the plan's batch files")
    mine = plan.active()
    bound = is_bound(root, plan) if config_bound is None else config_bound
    if "approved_at" in plan.doc.header and not bound:
        warnings.append(f"{plan.id} changed outside plan amend since its last approval: its revisions and task "
                        "config approvals no longer count; restore it, or plan amend --user-approved")
    _check_batches(root, plan, mine, config, user_approved, bound, errors, warnings)

    known = {b: d.header.get("state") for p in (plan, *others) for b, d in p.batches.items()}
    approved = [p for p in others if "approved_at" in p.doc.header]  # drafts may never run: no edges to them
    drafts = {b: p.id for p in others if p not in approved for b in p.batches}
    for p in others:
        if p not in approved:
            warnings += [f"{b}: owns_paths overlap {o} of draft plan {p.id}" for b, d in mine.items()
                         for o, od in p.active().items() if owns_overlap(d.header["owns_paths"], od.header["owns_paths"])]
    allb = {b: d.header for p in approved for b, d in p.active().items()} | {b: d.header for b, d in mine.items()}
    for b, doc in mine.items():
        errors += [f"{b}: depends_on unknown batch {d}" for d in doc.header["depends_on"] if d not in known]
        warnings += [f"{b}: depends_on cancelled batch {d}" for d in doc.header["depends_on"]
                     if known.get(d) == "cancelled"]
        warnings += [f"{b}: depends_on {d} of draft plan {drafts[d]}, which waits until that plan is approved"
                     for d in doc.header["depends_on"] if d in drafts]
    deps = {b: {d for d in h["depends_on"] if d in allb} for b, h in allb.items()}
    try:
        pos = {b: i for i, b in enumerate(schedule.topo(deps))}
    except schedule.CycleError as e:
        errors.append(f"dependency cycle: {e}")
        return report

    anc, ids = _ancestors(deps), list(allb)
    pairs = [(x, y) for i, x in enumerate(ids) for y in ids[i + 1:] if x in mine or y in mine]
    for x, y in pairs:
        cx, cy = is_contract(allb[x]), is_contract(allb[y])
        hits = owns_overlap(allb[x]["owns_paths"], allb[y]["owns_paths"])
        if not hits:
            continue
        if cx != cy:  # only the contract batch changes a contract file, whatever the order
            (other, c), p = ((x, y) if cy else (y, x)), hits[0]
            errors.append(f"{other}: owns {p[0] if cy else p[1]} overlaps contract file {p[1] if cy else p[0]} of "
                          f"contract batch {c}; move that change into the contract batch")
            continue
        if x in anc[y] or y in anc[x]:
            continue
        # x and y are unreachable from each other, so either edge keeps the graph acyclic: the one that waits is
        # an unstarted batch, then one of this plan, then the later in topological order
        later, earlier = sorted((x, y), key=lambda b: (allb[b].get("state") not in UNSTARTED, b not in mine, -pos[b]))
        report["overlaps"].append({"a": earlier, "b": later, "paths": hits})
        if allb[later].get("state") not in UNSTARTED:
            errors.append(f"{later} and {earlier} have both started and overlap on {hits[0]}; stop one of them")
        elif later not in mine:  # an unstarted batch of another plan must wait for a started one of this plan
            errors.append(f"{later} (plan {allb[later]['plan_id']}) must wait for {earlier}; amend plan "
                          f"{allb[later]['plan_id']}")
        else:
            report["suggested_edges"].append({"batch": later, "depends_on": earlier})
            deps[later].add(earlier)
            anc = _ancestors(deps)

    tiers = {frozenset((c["a"], c["b"])): c for c in coupling or []}
    auto = {(e["batch"], e["depends_on"]) for e in report["suggested_edges"]}
    for b in mine:
        for d in sorted(deps[b]):
            reason = ("owns_paths 重叠（自动补边）" if (b, d) in auto
                      else "owns_paths 重叠" if owns_overlap(allb[b]["owns_paths"], allb[d]["owns_paths"])
                      else "依赖契约批" if is_contract(allb[d])
                      else "高耦合" if tiers.get(frozenset((b, d)), {}).get("tier") == "high"
                      else "声明的依赖")
            report["serial_reasons"].append({"batch": b, "depends_on": d, "reason": reason})
    for c in coupling or []:
        a, b = c["a"], c["b"]
        if a not in mine or b not in mine or a in anc[b] or b in anc[a]:
            continue
        unknown = [s for s in ("ref", "cochange") if c[s] is None]
        at = f"C={c['score']:.2f}" + (f" ({', '.join(unknown)} unknown)" if unknown else "")
        if c["tier"] == "high":
            warnings.append(f"{a} / {b}: {at}, high coupling: one seat in sequence, or merge them")
        elif c["tier"] == "medium" and not ({m["batch"] for m in mine[a].header["merge_after"]} & {b}
                                             or {m["batch"] for m in mine[b].header["merge_after"]} & {a}):
            warnings.append(f"{a} / {b}: {at}, parallel but needs merge_after with a reason")

    sub = {b: {**allb[b], "depends_on": sorted(deps[b])} for b in mine}
    report["waves"] = schedule.waves(sub, width)
    report["critical_path"] = schedule.critical_path(sub)
    report["critical_path_length"] = len(report["critical_path"])
    report["max_width"] = max(map(len, report["waves"]), default=0)
    report["ok"] = not errors and not report["overlaps"]
    return report


def apply_edges(plan, report) -> int:
    """Write the suggested depends_on edges into this plan's headers (in memory); returns how many were added."""
    n = 0
    for e in report["suggested_edges"]:
        h = plan.batches[e["batch"]].header
        if e["depends_on"] not in h["depends_on"]:
            h["depends_on"].append(e["depends_on"])
            n += 1
    return n
