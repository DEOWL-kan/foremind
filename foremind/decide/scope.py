"""Handler of approved `scope` pendings (pending.py contract, #8): widen request.batch by request.match.paths through
plan.freeze.expand_scope, the revision carrying the Q-n and who answered it (its last pending_answered: the user, or
the one-shot controller, §20 I26). Anything it refuses raises, so the Q-n stays answered. The result line
carries expand_scope's warnings, the finishing report of an interrupted amend first (REQ-18)."""
from foremind import config
from foremind.decide import pending
from foremind.plan.freeze import expand_scope


def apply_answer(root, record: dict, request: dict) -> str:
    batch, paths = request.get("batch"), (request.get("match") or {}).get("paths") or []
    if not batch or not paths:
        raise ValueError("请求里没有批次或路径（request.batch、request.match.paths），无从扩大范围")
    by = [e.get("by") for e in pending._log(root).iter()
          if e["type"] == "pending_answered" and e.get("question") == record["id"]][-1:]
    if by not in (["user"], ["controller"]):
        raise ValueError(f"{record['id']} 的答复者是 {by[0] if by else '（找不到答复事件）'}：扩大范围只能由用户或总控批准")
    report = expand_scope(root, batch, paths, decision=record["id"], approved_by=by[0], config=config.load(root))
    # REQ-18: its warnings open with what finishing an interrupted amend did (freeze._recover), for `decide` to show
    return "；".join([f"批次 {batch} 的 owns_paths 已加上 {'、'.join(paths)}（计划修订带 {record['id']}）",
                     *report["warnings"]])
