"""`foremind land <batch>`: the controller's (or the user's) merge of a delivered batch, by hand, never automatic.

Merges each repo's batch branch onto its target branch's current commit in a throwaway detached worktree under
wt_root()/_land-* (hooks off; the directory itself for a single-repo batch, <dir>/<repo id> for a multi-repo one),
runs the batch's accept_commands where acceptance runs them (a command's repo checkout, else the directory) and
`land.commands` in every repo's checkout (each limited to acceptance.timeout_min, output kept under
state_dir/land/<batch>-<stamp>/). Only when all pass, and every repo's main checkout is clean, its target still at the
start and not checked out in another worktree, it moves the targets one by one: `merge --ff-only` in the main
checkout when it is on the target, else `update-ref` against the old value. A failed move puts the repos already
moved back (`update-ref <start> <merge>`, or `reset --keep` on a checkout on the target): all or nothing.
In a multi-repo batch, a repo whose batch head is already in its target is checked out and checked like the others
(REQ-20 asks it of every repo), but not merged or moved.
The batch state is not written: L0 reconcile records merged, but gate.batch_merged
compares with review.target_ref, the remote's target when the repo has a remote: land moves only the local branch,
so such a batch is recorded merged once the user pushes; without a remote, at once.
Refused in a Foremind session, `--notes` included (REQ-8).
Prints every `## 交付说明` of the batch log (schema delivery_notes), deduplicated; `--notes` prints only that.
Config key read here: land.commands (default: the full unittest run), besides review's delivery.repo.* keys.
"""
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from foremind import batchlog, job, review, worktree
from foremind.acceptance import command_repo, command_text, wait
from foremind.commands.log import NOTES, delivery_notes
from foremind.config import ConfigError
from foremind.defaults import TABLE
from foremind.paths import ProjectNotFound, state_dir

def register(sub):
    p = sub.add_parser("land", help="merge a delivered batch onto its target branch after re-running its checks")
    p.add_argument("batch")
    p.add_argument("--notes", action="store_true", help="only print the batch's delivery notes; merge nothing")
    p.set_defaults(func=_run)


def _run(args):
    try:
        if os.environ.get("FOREMIND_SESSION"):
            raise review.FlowError("合入只归总控或用户手动执行，Foremind 会话不能执行 land（含 --notes）")
        root, cfg = review.context(args.batch)
        if not args.notes:
            land(root, args.batch, cfg)
        print("\n".join(notes_summary(root, args.batch)))
    except (review.FlowError, ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind land: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"foremind land: corrupt JSON in the project state: {e}", file=sys.stderr)
        return 1
    return 0


def notes_summary(root, batch_id) -> list[str]:
    """Every `## 交付说明` section in the registered prefix of the batch log (what batchlog recorded, not bytes
    appended since): entries listed once each, tagged with the sections (1-based) they appear in."""
    if not batchlog.verify(root, batch_id):
        return [f"交付说明：{batch_id} 的批次日志在 `foremind log` 之外被改过（batchlog.verify 不通过），不汇总；请先核对日志"]
    last = batchlog._latest(root, batch_id)
    path = state_dir(root) / "batches" / f"{batch_id}.log.md"
    text = path.read_bytes()[:last["size"]].decode("utf-8") if last else ""
    found = delivery_notes(text)
    if not found:
        return [f"交付说明：{batch_id} 的批次日志里没有 `## 交付说明`"]
    rec = re.compile(r"^### ([0-9]{4}-[0-9]{2}-[0-9]{2}T\S+ .+)$", re.M)  # batchlog.append's record heading
    heads = [(m.start(), m[1]) for m in rec.finditer(text)]
    where = [next((h for at, h in reversed(heads) if at < m.start()), "?") for m in NOTES.finditer(text)]
    out = [f"交付说明（{batch_id}）："]
    out.append("  段号按 `## 交付说明` 在日志里出现的次序：" + "；".join(f"第 {k} 段 = {w}" for k, w in enumerate(where, 1)))
    out += [f"  第 {k} 段不合格，未计入：{'; '.join(errs[:3])}" for k, (_, errs) in enumerate(found, 1) if errs]
    for title, key, fmt in (
            ("新配置键", "config_keys",
             lambda x: f"{x['key']} [{x['merge_class']}]"
                       + (f" 默认 {json.dumps(x['default'], ensure_ascii=False)}" if "default" in x else "")
                       + f"：{x['why']}"),
            ("DESIGN 补项", "design", lambda x: f"{x['where']}：{x['text']}"),
            ("转遗留", "leftovers", lambda x: f"{x['item']}：{x['why']}")):
        seen = {}  # canonical JSON -> (entry, [section numbers]); dicts keep first-seen order
        for k, (notes, errs) in enumerate(found, 1):
            for x in [] if errs else notes[key]:
                seen.setdefault(json.dumps(x, sort_keys=True, ensure_ascii=False), (x, []))[1].append(k)
        out.append(f"  {title}：" + ("" if seen else "无"))
        out += [f"    - {fmt(x)}（第 {'、'.join(map(str, ks))} 段）" for x, ks in seen.values()]
    return out


def land(root, batch_id, cfg) -> dict:
    """Returns {repo id: merge commit now at the tip of its target branch}; raises FlowError on any refusal or
    failure, with every target branch back at its start. The Foremind-session refusal is _run's."""
    h = review.load_batch(root, batch_id)
    if h.get("state") != "delivered":
        raise review.FlowError(f"{batch_id} 是 {h.get('state')}，land 只合 delivered 的批次")
    pairs = review.batch_repos(root, h, cfg)
    hd = review.heads(pairs)
    gate = next((e for e in reversed(review.all_events(root))
                 if e["type"] == "gate_result" and e.get("batch") == batch_id), None)
    if not gate or gate.get("heads") != hd or gate.get("verdict") != "pass":
        raise review.FlowError(f"{batch_id} 当前 heads 没有通过的门禁结果（最近一次："
                               f"{gate and gate.get('verdict')}，heads {gate and gate.get('heads')}）；先跑 foremind gate")
    ids = [r.id for r, _ in pairs]
    extra = cfg.get("land.commands", TABLE["land.commands"])
    if not isinstance(extra, list) or not all(isinstance(c, str) and c.strip() for c in extra):
        raise review.FlowError(f"配置 land.commands 须是非空命令字符串的列表，现在是 {extra!r}")
    accept = [(command_text(c), command_repo(c, ids)) for c in h["accept_commands"]]
    if not all(isinstance(t, str) and t.strip() and (rid is None or rid in ids) for t, rid in accept):
        raise review.FlowError(f"bad command in accept_commands: {h['accept_commands']}")
    # acceptance's cwd rule: a command's repo checkout, else (multi-repo only) the directory holding the checkouts;
    # land.commands run once in every repo's checkout
    todo = accept + [(c, rid) for rid in ids for c in extra]
    # local target only: land moves the local branch and never pushes, so a remote is the user's to sync
    rs = [{"repo": r, "single": len(ids) == 1, "br": review.branch(wt), "target": (t := review.target_branch(r, cfg)),
           "start": review.git(r.path, "rev-parse", "--verify", f"refs/heads/{t}^{{commit}}")} for r, wt in pairs]
    for x in rs:  # multi-repo only, a repo already in the target (merged by hand, say): checked out as is, never moved
        x["merged"] = len(ids) > 1 and review.is_ancestor(x["repo"].path, hd[x["repo"].id], x["start"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = state_dir(root) / "land" / f"{batch_id}-{stamp}"
    worktree.wt_root().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"_land-{batch_id}-", dir=worktree.wt_root()))  # `_`: no batch, no repo id
    co = {rid: tmp if len(ids) == 1 else tmp / rid for rid in ids}
    try:
        for x in rs:
            _merge(x, co[x["repo"].id], hd[x["repo"].id])
        timeout = int(cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"])) * 60
        failed = []
        for i, (c, rid) in enumerate(todo, 1):
            jid = job.start(out_dir, ["/bin/sh", "-c", c], cwd=co[rid] if rid else tmp, timeout_s=timeout,
                            job_id=str(i))
            code = wait(out_dir, jid).get("exit_code", -1)
            where = "" if len(ids) == 1 else f"[{rid or '批次目录'}] "
            print(f"{'ok  ' if code == 0 else 'FAIL'} {where}{c} (exit {code}) · {out_dir / jid}")
            failed += [c] if code else []
        if failed:
            raise review.FlowError(f"{len(failed)} 条命令没通过，目标分支没动；输出在 {out_dir}")
        for x in rs:  # every repo checked before any moves: one refusal leaves them all untouched
            _check(x)
        movers = [x for x in rs if not x["merged"]]
        tried = []
        try:
            # update-ref ones first: a failure there leaves every main checkout alone
            for x in sorted(movers, key=lambda x: x["on_target"]):
                tried.append(x)
                _move(x)
        except BaseException as e:  # Ctrl-C between two moves as well
            if len(rs) == 1:
                raise
            report = f"移动目标分支时失败，各仓库：\n{_undo(rs, tried, e)}"
            if not isinstance(e, review.FlowError):
                print(f"foremind land: {report}", file=sys.stderr)
                raise
            raise review.FlowError(report) from None
    finally:
        for r, _ in pairs:
            review.run(["git", "worktree", "remove", "--force", str(co[r.id])], r.path, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    for x in rs:
        print(f"{batch_id}: {x['br']} " + ("已在" if x["merged"] else "已合入") + f" {x['target']}"
              + ("" if len(ids) == 1 else f"（{x['repo'].id}）")
              + (f" 里，没动它 · 起点 {x['new']}" if x["merged"] else f" · 合并提交 {x['new']}"))
    return {x["repo"].id: x["new"] for x in rs}


def _merge(x, tmp, head):
    """Merges the batch branch onto the target's start in a detached worktree at tmp (hooks off); sets x["new"]
    (the start itself when the branch is already in the target)."""
    repo, br, target = x["repo"], x["br"], x["target"]
    review.git(repo.path, "-c", "core.hooksPath=/dev/null", "worktree", "add", "-q", "--detach", str(tmp), x["start"])
    if x["merged"]:
        x["new"] = x["start"]
        return
    m = review.run(["git", "-c", "core.hooksPath=/dev/null", "merge", "-q", "--no-ff", "--no-edit", br], tmp,
                   check=False)
    if m.returncode:
        conflicts = review.git(tmp, "diff", "--name-only", "--diff-filter=U").split()
        review.run(["git", "merge", "--abort"], tmp, check=False)
        raise review.FlowError(_tag(x, f"{br} 合到 {target} 冲突：{conflicts}" if conflicts
                                    else f"git merge {br}: {m.stderr.strip() or m.stdout.strip()}"))
    x["new"] = review.git(tmp, "rev-parse", "HEAD")
    if review.git(tmp, "rev-parse", "HEAD^2") != head:
        raise review.FlowError(_tag(x, f"{br} 在合并期间移动了（门禁通过的是 {head}）；重跑门禁后再 land"))


def _check(x):
    repo, target = x["repo"], x["target"]
    if dirty := review.git(repo.path, "status", "--porcelain"):
        raise review.FlowError(_tag(x, f"主检出 {repo.path} 有未提交改动，目标分支没动：\n{dirty}"))
    if review.git(repo.path, "rev-parse", f"refs/heads/{target}") != x["start"]:
        raise review.FlowError(_tag(x, f"{target} 在验证期间移动了（起点 {x['start']}），没动它；重新 land"))
    x["on_target"] = review.git(repo.path, "symbolic-ref", "-q", "HEAD", check=False) == f"refs/heads/{target}"
    if any(f"branch refs/heads/{target}" in (lines := e.splitlines()) and lines[0].startswith("worktree ")
           and not _same(lines[0][len("worktree "):], repo.path)  # only the main checkout's own entry is exempt (REQ-20)
           for e in review.git(repo.path, "worktree", "list", "--porcelain").split("\n\n")):
        raise review.FlowError(_tag(x, f"{target} 检出在另一个 worktree 里，没动它；" + (
            f"那个 worktree 离开 {target} 后重新 land" if x["merged"] or x["on_target"]
            else f"在那里执行 git merge --ff-only {x['new']}")))


def _same(a, b) -> bool:
    """One directory (samefile: case-insensitive volumes); unreadable falls back to comparing resolved paths."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return Path(a).resolve() == Path(b).resolve()


def _move(x):
    if x["on_target"]:
        review.git(x["repo"].path, "merge", "-q", "--ff-only", x["new"])
    else:
        review.git(x["repo"].path, "update-ref", f"refs/heads/{x['target']}", x["new"], x["start"])


def _undo(rs, tried, err) -> str:
    """After a failed move: puts each tried target back at its start where it is still at our merge commit (compare
    and swap; on a main checkout still on the target, reset --keep). One line per repo, in header order."""
    out = []
    for x in rs:
        repo, ref, start = x["repo"], f"refs/heads/{x['target']}", x["start"]
        head = f"移动时出错（{str(err) or type(err).__name__}）；" if tried and x is tried[-1] else ""
        try:
            if x["merged"] or not any(x is y for y in tried):
                state = "已在目标里，没动它" if x["merged"] else "没移动，仍在起点"
            elif (cur := review.git(repo.path, "rev-parse", ref)) == start:
                state = "仍在起点"
            elif cur != x["new"]:
                raise review.FlowError(f"{x['target']} 在 {cur}，不是本次合并提交 {x['new']}")
            elif x["on_target"]:
                if review.git(repo.path, "symbolic-ref", "-q", "HEAD", check=False) != ref:
                    raise review.FlowError(f"主检出已不在 {x['target']}")
                review.git(repo.path, "reset", "-q", "--keep", start)
                state = "已退回起点"
            else:
                review.git(repo.path, "update-ref", ref, start, x["new"])
                state = "已退回起点"
        except review.FlowError as e:
            state = f"退回失败，需手动处理：{e}；起点 {start}"
        out.append(f"  {repo.id}：{head}{state}")
    return "\n".join(out)


def _tag(x, msg) -> str:
    return msg if x.get("single") else f"{x['repo'].id}: {msg}"
