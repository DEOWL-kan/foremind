"""Delivery-convention proposal, the program's part (DESIGN §7.5): GitHub settings, branch protection and rulesets,
the user's permission (all through `gh api`), and how the target branch's history was merged.

Every item is {"value", "evidence"}; what cannot be read is "unknown". Reading the repo's rule documents is the
cataloger's (M2-2), so the delivery level and #23 are proposed as the strictest ("done", "user") whenever the facts
do not force them there anyway; the user may widen them when confirming. An unanswered unknown with an order takes
the strictest (STRICTEST); one without (target branch, merge method) is left out of delivery.toml.
A rerun takes what delivery.toml already has as the answers (§20 I53③); a fact that rules an answer out wins.
"""
import json
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from foremind import config
from foremind.fsutil import atomic_write
from foremind.install.tomlblock import value as toml_value
from foremind.paths import state_dir

UNKNOWN = "unknown"
METHODS = ("merge", "squash", "rebase")  # GitHub's own order on the merge button
CI_MODES = ("required", "local_first", "none")
STRICTEST = {"level": "done", "push_pr": "user", "ci": "required"}
DELIVERY_KEYS = ("level", "push_pr", "target_branch", "merge_method", "update_method", "ci")
HISTORY_LIMIT = 500
MIN_SAMPLES = 3  # fewer merges of the most common kind in the history: the method stays unknown
TIMEOUT_S = 30
_SQUASH = re.compile(r"\(#\d+\)\s*$")


def item(value, evidence) -> dict:
    return {"value": value, "evidence": evidence}


def _run(argv, cwd) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S, stdin=subprocess.DEVNULL,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr.strip()


def _git(cwd, *args) -> str | None:
    rc, out, _ = _run(["git", *args], cwd)
    return out.strip() if rc == 0 else None


def _gh(cwd, url) -> tuple[object, str]:
    """(parsed JSON or None, evidence)."""
    rc, out, err = _run(["gh", "api", url], cwd)
    ev = f"gh api {url}"
    if rc:
        return None, f"{ev}: {err or f'exit {rc}'}"
    try:
        return json.loads(out), ev
    except ValueError:
        return None, f"{ev}: not JSON"


def classify(parents, author, committer, subject, body) -> str:
    """merge | squash | rebase | direct for one first-parent commit of a target branch.
    ponytail: heuristics — squash = GitHub's "(#n)" title or git's squash message, rebase = replayed (committer
    or commit time differs from the author's); a cherry-pick or amend on the branch also counts as rebase."""
    if len(parents) > 1:
        return "merge"
    if _SQUASH.search(subject) or "Squashed commit of the following" in f"{subject}\n{body}":
        return "squash"
    return "rebase" if author != committer else "direct"


def history(path, ref, limit=HISTORY_LIMIT) -> dict:
    """How the last `limit` first-parent commits of `ref` came in; a run of consecutive replayed commits is one
    rebase merge."""
    rc, out, _ = _run(["git", "log", "--first-parent", f"-n{limit}",  # raw: str.strip() eats \x1f too
                       "--format=%P%x1f%an%x1f%ae%x1f%at%x1f%cn%x1f%ce%x1f%ct%x1f%s%x1f%b%x1e", ref, "--"], path)
    counts = dict.fromkeys((*METHODS, "direct"), 0)
    if rc:
        return counts
    prev = None
    for rec in filter(None, (r.strip("\n") for r in out.split("\x1e"))):
        p, an, ae, at, cn, ce, ct, subject, body = (rec.split("\x1f") + [""] * 9)[:9]
        kind = classify(p.split(), (an, ae, at), (cn, ce, ct), subject, body)
        if not (kind == "rebase" and prev == "rebase"):
            counts[kind] += 1
        prev = kind
    return counts


PROTECTION = ("required_checks", "require_up_to_date", "linear_history", "required_approvals", "force_push_allowed")


def _protection(path, slug, branch) -> dict:
    """Branch protection and rulesets on `branch`, the stricter of both. Classic protection needs admin to read,
    rulesets only read access. With one of them unreadable a "no" is unknown (it may be set there), a "yes" stands."""
    prot, pev = _gh(path, f"repos/{slug}/branches/{branch}/protection")
    if prot is None and "Branch not protected" in pev:
        prot = {}
    rules, rev = _gh(path, f"repos/{slug}/rules/branches/{branch}")
    prot, rules = (prot if isinstance(prot, dict) else None), (rules if isinstance(rules, list) else None)
    ev = f"{pev}; {rev}"
    if prot is None and rules is None:
        return {k: item(UNKNOWN, ev) for k in PROTECTION}
    partial, p, by = prot is None or rules is None, prot or {}, {}
    for r in rules or []:
        if isinstance(r, dict):
            by.setdefault(r.get("type"), []).append(r.get("parameters") or {})

    def fact(v, none):
        return item(v if v else UNKNOWN if partial else none, ev)
    sc = p.get("required_status_checks") or {}
    rsc = by.get("required_status_checks", [])
    checks = sorted({*(sc.get("contexts") or []), *(c.get("context") for c in sc.get("checks") or []),
                     *(c.get("context") for x in rsc for c in x.get("required_status_checks") or [])} - {None})
    forbid = "non_fast_forward" in by or bool(p) and not (p.get("allow_force_pushes") or {}).get("enabled")
    return {
        "required_checks": fact(checks, []),
        "require_up_to_date": fact(bool(sc.get("strict") or any(x.get("strict_required_status_checks_policy")
                                                                for x in rsc)), False),
        "linear_history": fact(bool((p.get("required_linear_history") or {}).get("enabled")
                                    or "required_linear_history" in by), False),
        "required_approvals": fact(max([(p.get("required_pull_request_reviews") or {}).get(
            "required_approving_review_count") or 0, *(x.get("required_approving_review_count") or 0
                                                      for x in by.get("pull_request", []))]), 0),
        "force_push_allowed": item(False, ev) if forbid else item(UNKNOWN if partial else True, ev),
    }


def detect_repo(repo) -> dict:
    path = repo.path
    facts = {}
    remote = repo.remote or "origin"
    has_remote = _git(path, "remote", "get-url", remote) is not None
    info, iev = _gh(path, "repos/{owner}/{repo}") if has_remote else (None, f"没有远端 {remote}")
    info = info if isinstance(info, dict) else None
    slug = info.get("full_name") if info else None
    if info:
        keys = {m: f"allow_{'merge_commit' if m == 'merge' else m + '_merge'}" for m in METHODS}
        if any(k in info for k in keys.values()):
            allowed = [m for m, k in keys.items() if info.get(k)]
            facts["allowed_merge_methods"] = item(allowed, iev)
            facts["default_merge_method"] = item(allowed[0] if allowed else UNKNOWN,
                                                 iev + "（GitHub 没有默认方式设置；页面无记忆时取第一个允许的）")
        else:  # left out of the answer without admin or write access
            facts["allowed_merge_methods"] = facts["default_merge_method"] = item(UNKNOWN, iev + "：没有 allow_* 字段")
        facts["delete_branch_on_merge"] = (item(info["delete_branch_on_merge"], iev)
                                           if "delete_branch_on_merge" in info else
                                           item(UNKNOWN, iev + "：没有 delete_branch_on_merge 字段"))
        perms = info.get("permissions") if isinstance(info.get("permissions"), dict) else None
        facts["can_push"] = item(bool(perms.get("push")), iev + " permissions") if perms else item(UNKNOWN, iev)
        facts["can_merge"] = facts["can_push"]  # merging a PR needs write access; protection may still ask more
    else:
        for k in ("allowed_merge_methods", "default_merge_method", "delete_branch_on_merge", "can_push", "can_merge"):
            facts[k] = item(UNKNOWN, iev)

    target = info.get("default_branch") if info else None
    if target:
        facts["target_branch"] = item(target, iev + " default_branch")
    elif has_remote and (h := _git(path, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")):
        target = h.split("/", 1)[1]
        facts["target_branch"] = item(target, f"refs/remotes/{remote}/HEAD")
    else:
        facts["target_branch"] = item(UNKNOWN, f"{iev}；也没有 refs/remotes/{remote}/HEAD")

    facts.update(_protection(path, slug, target) if slug and target else
                 {k: item(UNKNOWN, "仓库或目标分支未知") for k in PROTECTION})

    ref = next((r for r in (f"refs/remotes/{remote}/{target}", f"refs/heads/{target}") if target
                and _git(path, "rev-parse", "-q", "--verify", r)), "HEAD")
    facts["history"] = item(history(path, ref), f"git log --first-parent -n{HISTORY_LIMIT} {ref}")
    wf = Path(path) / ".github" / "workflows"
    flows = sorted(p.name for p in wf.glob("*.y*ml")) if wf.is_dir() else []
    facts["ci_workflows"] = item(flows, str(wf))
    return {"facts": facts, "proposal": propose(facts)}


def allowed_methods(f) -> list[str]:
    """Merge methods the facts leave open (all three when the settings are unknown)."""
    a = f["allowed_merge_methods"]["value"]
    return [m for m in (a if isinstance(a, list) else METHODS)
            if not (m == "merge" and f["linear_history"]["value"] is True)]


def update_for(mm) -> dict:
    return (item(UNKNOWN, "合入方式未定") if mm == UNKNOWN else
            item("rebase", "按 rebase 合入：分支需线性") if mm == "rebase" else
            item("merge", f"按 {mm} 合入：把目标分支合进来即可，不改写已推送的历史"))


def ruled_out(f, k, x) -> bool:
    """Whether the facts rule value `x` of key `k` out (no merge right, no push right, a method not allowed, required
    checks under `ci = none`)."""
    v = {n: y["value"] for n, y in f.items()}
    return (k == "level" and x == "merge_dev" and v["can_merge"] is False
            or k == "push_pr" and x == "system" and v["can_push"] is False
            or k == "merge_method" and x not in allowed_methods(f)
            or k == "ci" and x == "none" and v["required_checks"] not in (UNKNOWN, []))


def propose(f) -> dict:
    v = {k: x["value"] for k, x in f.items()}
    out = {}
    out["level"] = (item("done", "没有合入权限：只能做完即可") if v["can_merge"] is False else
                    item(UNKNOWN, "规则文档未读（编目员 M2-2），不能排除人工合入要求：请确认"))
    out["push_pr"] = (item("user", "没有推送权限") if v["can_push"] is False else
                      item(UNKNOWN, "推送与开 PR 归谁（#23）：请确认"))
    out["target_branch"] = f["target_branch"]

    allowed, hist = allowed_methods(f), v["history"]
    seen = sorted((m for m in allowed if hist[m]), key=lambda m: -hist[m])
    if len(allowed) == 1:
        out["merge_method"] = item(allowed[0], "唯一允许的合入方式")
    elif seen and hist[seen[0]] >= MIN_SAMPLES and (len(seen) == 1 or hist[seen[0]] > hist[seen[1]]):
        out["merge_method"] = item(seen[0], f"目标分支历史（{seen[0]} 样本 {hist[seen[0]]}）：{hist}，允许 {allowed}")
    else:
        out["merge_method"] = item(UNKNOWN, f"允许 {allowed}，历史 {hist}（最多的样本需 ≥{MIN_SAMPLES} 且唯一）：定不下来")
    out["update_method"] = update_for(out["merge_method"]["value"])
    checks = v["required_checks"]
    if checks not in (UNKNOWN, []) or v["ci_workflows"]:
        out["ci"] = item("required", f"必过检查 {checks}，工作流 {v['ci_workflows']}")
    elif checks == []:
        out["ci"] = item("none", "没有工作流，分支保护也没有必过检查")
    else:
        out["ci"] = item(UNKNOWN, "读不到分支保护：可能有外部必过检查")
    return out


def build(root, repos) -> dict:
    """Detect every repo and write `.foremind/delivery.proposal.json`."""
    prop = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repos": {r.id: detect_repo(r) for r in repos}}
    atomic_write(state_dir(root) / "delivery.proposal.json", json.dumps(prop, indent=2, ensure_ascii=False) + "\n")
    return prop


def confirmed(root) -> dict:
    """{repo id: {key: value}} of the delivery.toml in place, as answers for a rerun ({} if missing or unreadable:
    it gets rewritten). update_method is left out: it follows the merge method."""
    try:
        with open(state_dir(root) / "delivery.toml", "rb") as f:
            got = tomllib.load(f)["delivery"]["repo"]
        return {rid: {k: x for k, x in v.items() if k in DELIVERY_KEYS and k != "update_method"}
                for rid, v in got.items() if isinstance(v, dict)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def resolve(prop, answers=None) -> dict:
    """{repo id: {key: value}} to write: the answers, else the proposal, else the strictest; unknowns without an
    order are left out. An answer the facts rule out gives way to the proposal; update_method, unless answered,
    follows the merge method."""
    answers = answers or {}
    out = {}
    for rid, r in prop["repos"].items():
        vals, a = {}, answers.get(rid, {})
        for k in DELIVERY_KEYS:
            x = a.get(k)
            if x is None or ruled_out(r["facts"], k, x):
                x = (update_for(vals.get("merge_method", UNKNOWN)) if k == "update_method" else r["proposal"][k])["value"]
            if x == UNKNOWN:
                x = STRICTEST.get(k)
            if x is not None:
                vals[k] = x
        out[rid] = vals
    return out


def delivery_toml(resolved) -> str:
    lines = ["# 交付约定（DESIGN §7.5）：程序独占、整体重写，不要手改；经 `foremind init` 或 `foremind doctor --rescan` 更新", ""]
    for rid, vals in resolved.items():
        lines.append(f"[delivery.repo.{rid}]")
        lines += [f"{k} = {toml_value(v)}" for k, v in vals.items()]
        lines.append("")
    return "\n".join(lines)


def write_delivery(root, resolved) -> Path:
    """Write delivery.toml if it parses and the whole config still loads with it (a user-level ceiling may forbid a
    value); else the previous file stays and ConfigError says why."""
    p = state_dir(root) / "delivery.toml"
    text = delivery_toml(resolved)
    try:
        tomllib.loads(text)
    except ValueError as e:
        raise config.ConfigError(f"{p}: the generated content is not TOML ({e}); nothing written") from None
    old = p.read_bytes() if p.exists() else None
    atomic_write(p, text)
    try:
        config.load(root)
    except config.ConfigError as e:
        if old is None:
            p.unlink()
        else:
            atomic_write(p, old)
        raise config.ConfigError(f"{p}: 写入后配置不合法，已还原之前的内容：{e}") from None
    return p
