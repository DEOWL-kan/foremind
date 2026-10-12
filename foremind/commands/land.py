"""`foremind land <batch>`: the merge of a delivered batch, by the controller or the user by hand, or by the
supervisor's job (FOREMIND_ROLE=supervisor, m2f REQ-2) when the user's own config sets land.auto to true.

Before it merges or runs anything it checks, in this order, and stops at the first failure with nothing touched
(m2f REQ-1): `.foremind/land.lock`, not waited for and held to the end (busy); as the supervisor, auto_blockers
(auto); the batch is delivered (state); the current heads have a passing gate_result (gate); land.commands and
accept_commands are well formed (config); the batch log passes batchlog.verify (notes_unverified); every config key
of its delivery notes (the sections `--notes` sums up) is registered in config.KEY_CLASSES (unregistered_keys);
every repo's main checkout is clean (dirty) and its target checked out in no other worktree (elsewhere).
Then it merges each repo's batch branch onto its target branch's current commit in a throwaway detached worktree under
wt_root()/_land-* (hooks off; the directory itself for a single-repo batch, <dir>/<repo id> for a multi-repo one),
runs the batch's accept_commands where acceptance runs them (a command's repo checkout, else the directory) and
`land.commands` in every repo's checkout (each limited to acceptance.timeout_min, output kept under
state_dir/land/<batch>-<stamp>/). Only when all pass, holding the project state lock (the one send-backs, new Q-n,
state changes and lock transfers take), it checks again with git and file reads only: still delivered, the heads
still the merged ones and the gate still passing on them, clean, not checked out elsewhere, every target still at its
start (moved), and as the supervisor auto_blockers. In that same hold it moves the targets one by one: `merge
--ff-only` in the main checkout when it is on the target, else `update-ref` against the old value. A failed move puts
the repos already moved back (`update-ref <start> <merge>`, or `reset --keep` on a checkout on the target): all or
nothing.
In a multi-repo batch, a repo whose batch head is already in its target is checked out and checked like the others
(REQ-20 asks it of every repo), but not merged or moved.
Every land (not `--notes`, not one refused in a Foremind session) records exactly one land_result; a merged one also
writes drafts.md next to the command output (a DESIGN §20 I line, leftovers and config keys from the delivery notes)
for the controller to confirm and apply: land writes nothing outside .foremind but the target branches.
The batch state is not written: L0 reconcile records merged, but gate.batch_merged
compares with review.target_ref, the remote's target when the repo has a remote: land moves only the local branch,
so such a batch is recorded merged once the user pushes; without a remote, at once.
Refused in a Foremind session, `--notes` included (REQ-8).
Prints every `## 交付说明` of the batch log (schema delivery_notes), deduplicated; `--notes` prints only that.
Config keys read here: land.commands (default: the full unittest run) and land.auto (the user layer's file alone),
besides review's delivery.repo.* keys.
"""
import contextlib
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from datetime import datetime, timezone

from foremind import batchlog, config, job, review, schemas, worktree
from foremind.acceptance import command_repo, command_text, wait
from foremind.commands.log import NOTES, delivery_notes
from foremind.commands.review import actor
from foremind.config import ConfigError
from foremind.defaults import TABLE
from foremind.fsutil import LockBusy, atomic_write, file_lock, project_lock
from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir
from foremind.plan import model

# REQ-2 item 7: a Q-n in one of these states that blocks the batch keeps it from the automatic land
AUTO_BLOCKING = ("open", "deciding", "escalated", "awaiting_local_confirm", "provisional", "overdue", "overturned")
RESULT = ("heads", "outcome", "kind", "reason", "failed", "conflicts", "out_dir", "merges", "drafts")  # + by, job


class Stop(review.FlowError):
    """A land ending with land_result outcome `outcome` (and `kind` when refused); `fields` go into the record."""

    def __init__(self, outcome, msg, kind=None, **fields):
        super().__init__(msg)
        self.outcome, self.kind, self.fields = outcome, kind, fields


def _refused(kind, msg) -> Stop:
    return Stop("refused", msg, kind)


def register(sub):
    p = sub.add_parser("land", help="merge a delivered batch onto its target branch after re-running its checks")
    p.add_argument("batch")
    p.add_argument("--notes", action="store_true", help="only print the batch's delivery notes; merge nothing")
    p.set_defaults(func=_run)


def _run(args):
    try:
        if os.environ.get("FOREMIND_SESSION"):
            raise review.FlowError("合入只归总控或用户手动执行，Foremind 会话不能执行 land（含 --notes）")
        if args.notes:
            root, _ = review.context(args.batch)
            print("\n".join(notes_summary(root, args.batch)))
        else:
            land(find_project_root(), args.batch)  # its config is loaded inside: a bad one is recorded too
    except (review.FlowError, ConfigError, ProjectNotFound, OSError) as e:
        print(f"foremind land: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"foremind land: corrupt JSON in the project state: {e}", file=sys.stderr)
        return 1
    return 0


def _logged(root, batch_id) -> str:
    """The registered prefix of the batch log: what batchlog recorded, not bytes appended since."""
    last = batchlog._latest(root, batch_id)
    path = state_dir(root) / "batches" / f"{batch_id}.log.md"
    return path.read_bytes()[:last["size"]].decode("utf-8") if last else ""


def _entries(found, key) -> list[tuple]:
    """[(entry, [section numbers, 1-based])] of `key` over the sections that pass the schema (delivery_notes output),
    identical entries once, first seen first."""
    seen = {}  # canonical JSON -> (entry, [section numbers]); dicts keep first-seen order
    for k, (notes, errs) in enumerate(found, 1):
        for x in [] if errs else notes[key]:
            seen.setdefault(json.dumps(x, sort_keys=True, ensure_ascii=False), (x, []))[1].append(k)
    return list(seen.values())


def notes_summary(root, batch_id) -> list[str]:
    """Every `## 交付说明` section in the registered prefix of the batch log (what batchlog recorded, not bytes
    appended since): entries listed once each, tagged with the sections (1-based) they appear in."""
    if not batchlog.verify(root, batch_id):
        return [f"交付说明：{batch_id} 的批次日志在 `foremind log` 之外被改过（batchlog.verify 不通过），不汇总；请先核对日志"]
    text = _logged(root, batch_id)
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
        seen = _entries(found, key)
        out.append(f"  {title}：" + ("" if seen else "无"))
        out += [f"    - {fmt(x)}（第 {'、'.join(map(str, ks))} 段）" for x, ks in seen]
    return out


def _authorized() -> bool:
    """REQ-2 item 1: land.auto in the user layer's own file is the boolean true, written plainly (a {value, ceiling}
    table is not: whether it counts is the user's, #18). Not the merged value: an AUTHZ key with no order merges by
    ==, so the user's 1 under a project's true comes out True. Unreadable: not authorized."""
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            return config._flatten(tomllib.load(f)).get("land.auto") is True
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, ConfigError):
        return False


def _gate(root, batch_id, hd) -> tuple:
    """(the batch's latest gate_result or None, whether it is a pass on heads hd)."""
    g = next((e for e in reversed(review.all_events(root))
              if e["type"] == "gate_result" and e.get("batch") == batch_id), None)
    return g, bool(g) and g.get("verdict") == "pass" and g.get("heads") == hd


def auto_blockers(root, batch_id) -> list[str]:
    """REQ-2 items 1-8 for the supervisor's automatic land of batch_id: one line, with its item number, per item not
    met; [] when all are. Takes no lock: land calls it holding the project lock (flock is not re-entrant)."""
    out = [] if _authorized() else ["第 1 条：用户层配置文件里的 land.auto 不是布尔 true"]
    try:
        h = review.load_batch(root, batch_id)
    except review.FlowError as e:
        return out + [f"第 2–8 条：读不出批次头（{e}）"]
    if h.get("state") != "delivered":
        out.append(f"第 2 条：状态是 {h.get('state')}，不是 delivered")
    if h.get("mode") != "auto":
        out.append(f"第 3 条：mode 是 {h.get('mode')}，不是 auto")
    try:
        cfg = config.load(root, config.task_layer(h))
        pairs = review.batch_repos(root, h, cfg)
        remotes = [r.id for r, wt in pairs if review.remote(r, wt)]
        passed = _gate(root, batch_id, review.heads(pairs))[1]
    except (ConfigError, review.FlowError, OSError, ValueError) as e:
        out.append(f"第 4–6 条：读不出配置、仓库或 heads（{e}）")
    else:
        levels = {r.id: review.repo_cfg(cfg, r.id, "level", "delivery.level", TABLE["delivery.level"]) for r, _ in pairs}
        if list(levels.values()) != ["done"]:
            out.append(f"第 4 条：要只有一个仓库、交付级别为 done，现在是 {levels}")
        if remotes:
            out.append(f"第 5 条：仓库有远端：{'、'.join(remotes)}")
        if not passed:
            out.append("第 6 条：本批最近一条 gate_result 不是当前 heads 上的 pass")
    bad, holding = [], []
    for p in sorted((state_dir(root) / "decisions").glob("Q-*.json")):  # not Tick.load's: it skips unreadable ones
        try:
            q = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = None
        if not isinstance(q, dict) or schemas.validate("pending", q):
            bad.append(p.name)
        elif batch_id in q["blocks"] and (st := q.get("state", "open")) in AUTO_BLOCKING:
            holding.append(f"{q['id']}（{st}）")
    if bad:
        out.append(f"第 7 条：待决文件读不出：{'、'.join(bad)}")
    if holding:
        out.append(f"第 7 条：挡着本批的待决：{'、'.join(holding)}")
    try:
        plan = model.load(root, h["plan_id"])
        if "approved_at" not in plan.doc.header:
            out.append(f"第 8 条：计划 {plan.id} 没批准")
        elif batch_id not in plan.batches or not model.is_bound(root, plan):
            out.append(f"第 8 条：计划 {plan.id} 当前没绑定（批准后改过，或不含本批）")
    except (OSError, ValueError) as e:  # model.PlanError, a corrupt event line
        out.append(f"第 8 条：读不出计划 {h['plan_id']}（{e}）")
    return out


def land(root, batch_id, cfg=None) -> dict:
    """Returns {repo id: merge commit now at the tip of its target branch}; raises FlowError on any refusal or
    failure, with every target branch back at its start. Records exactly one land_result whatever the end, land.lock
    (when taken) still held; cfg None loads the batch's config inside that record. The Foremind-session refusal is
    _run's."""
    res = dict.fromkeys(RESULT)
    with contextlib.ExitStack() as held:  # left after the finally below: the record is part of the land
        try:
            if cfg is None:
                cfg = config.load(root, config.task_layer(review.load_batch(root, batch_id)))
            res["heads"] = _heads(root, batch_id, cfg)  # refusals before _land reads them get heads too (REQ-2)
            try:
                held.enter_context(file_lock(state_dir(root) / "land.lock", blocking=False))
            except LockBusy:
                raise _refused("busy", f"另一个 foremind land 正在跑（{state_dir(root) / 'land.lock'} 被占），"
                                       "它结束后再试") from None
            return _land(root, batch_id, cfg, res)
        except Stop as e:
            res.update(e.fields, outcome=e.outcome, kind=e.kind, reason=str(e))
            raise
        except BaseException as e:  # after the move (merged), or move_failed with its report, keeps what _land set
            res.update(outcome=res["outcome"] or "error", reason=res["reason"] or str(e) or type(e).__name__)
            raise
        finally:
            review.events(root).append(
                "land_result", batch=batch_id, **res, job=os.environ.get("FOREMIND_JOB") or None,
                by="supervisor" if os.environ.get("FOREMIND_ROLE") == "supervisor" else actor())


def _heads(root, batch_id, cfg) -> dict | None:
    """The batch's current heads for its land_result; None when they cannot be read (header, repos, worktrees)."""
    try:
        return review.heads(review.batch_repos(root, review.load_batch(root, batch_id), cfg))
    except (review.FlowError, ConfigError, OSError):
        return None


def _land(root, batch_id, cfg, res) -> dict:
    """land's work under land.lock; fills res (heads, out_dir, outcome and merges once moved)."""
    auto = os.environ.get("FOREMIND_ROLE") == "supervisor"
    if auto and (no := auto_blockers(root, batch_id)):
        raise _refused("auto", "不满足自动合入的条件，目标分支没动：" + "；".join(no))
    h = review.load_batch(root, batch_id)
    if h.get("state") != "delivered":
        raise _refused("state", f"{batch_id} 是 {h.get('state')}，land 只合 delivered 的批次")
    pairs = review.batch_repos(root, h, cfg)
    res["heads"] = hd = review.heads(pairs)
    gate, ok = _gate(root, batch_id, hd)
    if not ok:
        raise _refused("gate", f"{batch_id} 当前 heads 没有通过的门禁结果（最近一次："
                               f"{gate and gate.get('verdict')}，heads {gate and gate.get('heads')}）；先跑 foremind gate")
    ids = [r.id for r, _ in pairs]
    extra = cfg.get("land.commands", TABLE["land.commands"])
    # the list itself non-empty as well (controller's ruling on REQ-1 item 4, m2f.1 r1)
    if not isinstance(extra, list) or not extra or not all(isinstance(c, str) and c.strip() for c in extra):
        raise _refused("config", f"配置 land.commands 须是非空列表、每条是非空命令字符串，现在是 {extra!r}")
    accept = [(command_text(c), command_repo(c, ids)) for c in h["accept_commands"]]
    if not all(isinstance(t, str) and t.strip() and (rid is None or rid in ids) for t, rid in accept):
        raise _refused("config", f"bad command in accept_commands: {h['accept_commands']}")
    if not batchlog.verify(root, batch_id):
        raise Stop("notes_unverified", f"{batch_id} 的批次日志在 `foremind log` 之外被改过（batchlog.verify 不通过），"
                                       "目标分支没动；请先核对日志")
    found = delivery_notes(_logged(root, batch_id))
    keys = (x["key"] for x, _ in _entries(found, "config_keys"))
    if bad := list(dict.fromkeys(k for k in keys if config._lookup(config.KEY_CLASSES, k) is None)):
        raise Stop("unregistered_keys", f"交付说明里的新配置键没在 config.KEY_CLASSES 登记：{'、'.join(bad)}；"
                                        "登记后再 land，目标分支没动")
    # acceptance's cwd rule: a command's repo checkout, else (multi-repo only) the directory holding the checkouts;
    # land.commands run once in every repo's checkout
    todo = accept + [(c, rid) for rid in ids for c in extra]
    # local target only: land moves the local branch and never pushes, so a remote is the user's to sync
    rs = [{"repo": r, "single": len(ids) == 1, "br": review.branch(wt), "target": (t := review.target_branch(r, cfg)),
           "start": review.git(r.path, "rev-parse", "--verify", f"refs/heads/{t}^{{commit}}")} for r, wt in pairs]
    for x in rs:  # multi-repo only, a repo already in the target (merged by hand, say): checked out as is, never moved
        x["merged"] = len(ids) > 1 and review.is_ancestor(x["repo"].path, hd[x["repo"].id], x["start"])
    _ready(rs)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = state_dir(root) / "land" / f"{batch_id}-{stamp}"
    worktree.wt_root().mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f"_land-{batch_id}-", dir=worktree.wt_root()))  # `_`: no batch, no repo id
    co = {rid: tmp if len(ids) == 1 else tmp / rid for rid in ids}
    try:
        for x in rs:
            _merge(x, co[x["repo"].id], hd[x["repo"].id])
        timeout = int(cfg.get("acceptance.timeout_min", TABLE["acceptance.timeout_min"])) * 60
        res["out_dir"] = str(out_dir)
        failed = []
        for i, (c, rid) in enumerate(todo, 1):
            jid = job.start(out_dir, ["/bin/sh", "-c", c], cwd=co[rid] if rid else tmp, timeout_s=timeout,
                            job_id=str(i))
            code = wait(out_dir, jid).get("exit_code", -1)
            where = "" if len(ids) == 1 else f"[{rid or '批次目录'}] "
            print(f"{'ok  ' if code == 0 else 'FAIL'} {where}{c} (exit {code}) · {out_dir / jid}")
            failed += [c] if code else []
        if failed:
            raise Stop("tests", f"{len(failed)} 条命令没通过，目标分支没动；输出在 {out_dir}", failed=failed)
        with project_lock(root):  # the final check and the move in one hold: no send-back or new Q-n in between
            _final(root, batch_id, pairs, hd, rs, auto)
            movers = [x for x in rs if not x["merged"]]
            tried = []
            try:
                # update-ref ones first: a failure there leaves every main checkout alone
                for x in sorted(movers, key=lambda x: x["on_target"]):
                    tried.append(x)
                    _move(x)
            except BaseException as e:  # Ctrl-C between two moves as well
                res["outcome"] = "move_failed"
                if len(rs) == 1:
                    raise
                res["reason"] = report = f"移动目标分支时失败，各仓库：\n{_undo(rs, tried, e)}"
                if not isinstance(e, review.FlowError):
                    print(f"foremind land: {report}", file=sys.stderr)
                    raise
                raise review.FlowError(report) from None
            res.update(outcome="merged", merges={x["repo"].id: x["new"] for x in rs})
    finally:
        for r, _ in pairs:
            review.run(["git", "worktree", "remove", "--force", str(co[r.id])], r.path, check=False)
        shutil.rmtree(tmp, ignore_errors=True)
    for x in rs:
        print(f"{batch_id}: {x['br']} " + ("已在" if x["merged"] else "已合入") + f" {x['target']}"
              + ("" if len(ids) == 1 else f"（{x['repo'].id}）")
              + (f" 里，没动它 · 起点 {x['new']}" if x["merged"] else f" · 合并提交 {x['new']}"))
    print("\n".join(notes_summary(root, batch_id)))
    try:
        path = out_dir / "drafts.md"
        atomic_write(path, _drafts(root, h, found, res["merges"]))
        res["drafts"] = str(path)
        print(f"合入草稿（总控确认后落盘）：{path}")
    except OSError as e:  # the targets have moved: merged stands, with no drafts
        print(f"foremind land: 合入草稿没写成：{e}", file=sys.stderr)
    return res["merges"]


def _drafts(root, h, found, merges) -> str:
    """drafts.md (REQ-1) from the delivery notes summed up as `--notes` does."""
    bid = h["id"]
    try:
        ns = [int(n) for n in re.findall(r"^\| I(\d+) \|", (Path(root) / "DESIGN.md").read_text(encoding="utf-8"),
                                         re.M)]
    except (OSError, UnicodeDecodeError):
        ns = []
    try:
        first = (state_dir(root) / "batches" / f"{bid}.handoff.md").read_text(encoding="utf-8").split("\n", 1)[0]
        title = first.removeprefix("# ").removeprefix(bid).strip()
    except (OSError, UnicodeDecodeError):
        title = ""
    design = [x for x, _ in _entries(found, "design")]
    nums = [chr(0x2460 + i) if i < 20 else f"({i + 1})" for i in range(len(design))]  # ①…⑳, then (21)
    row = (f"| I{max(ns) + 1 if ns else '?'} | {title}（{bid}，{'、'.join(h['reqs'])}；"
           f"补 {'、'.join(dict.fromkeys(x['where'] for x in design))}） | "
           + " ".join(f"{n} {x['where']}：{x['text']}" for n, x in zip(nums, design)) + " |")
    return "\n".join([
        f"# {bid} 合入草稿（{datetime.now(timezone.utc).isoformat(timespec='seconds')}）",
        "新提交：" + "；".join(f"{r} {c}" for r, c in merges.items()),
        "", "## DESIGN §20 I 行草稿", row if design else "无",
        "", "## 遗留追加草稿", *([f"- {bid} 交付说明：{x['item']}（{x['why']}）" for x, _ in _entries(found, "leftovers")]
                              or ["无"]),
        "", "## 新配置键", *([f"- {x['key']} [{x['merge_class']}]：{x['why']}" for x, _ in _entries(found, "config_keys")]
                          or ["无"])]) + "\n"


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
        if conflicts:
            raise Stop("conflict", _tag(x, f"{br} 合到 {target} 冲突：{conflicts}"), conflicts=conflicts)
        raise review.FlowError(_tag(x, f"git merge {br}: {m.stderr.strip() or m.stdout.strip()}"))
    x["new"] = review.git(tmp, "rev-parse", "HEAD")
    if review.git(tmp, "rev-parse", "HEAD^2") != head:
        raise _refused("gate", _tag(x, f"{br} 在合并期间移动了（门禁通过的是 {head}）；重跑门禁后再 land"))


def _ready(rs, final=False):
    """REQ-1 items 7 (dirty) and 8 (elsewhere), each over every repo; final (after the commands, x["new"] set): then
    every target still at its start (moved). Sets x["on_target"]."""
    for x in rs:
        if dirty := review.git(x["repo"].path, "status", "--porcelain"):
            raise _refused("dirty", _tag(x, f"主检出 {x['repo'].path} 有未提交改动，目标分支没动：\n{dirty}"))
    for x in rs:
        repo, target = x["repo"], x["target"]
        x["on_target"] = review.git(repo.path, "symbolic-ref", "-q", "HEAD", check=False) == f"refs/heads/{target}"
        if any(f"branch refs/heads/{target}" in (lines := e.splitlines()) and lines[0].startswith("worktree ")
               and not _same(lines[0][len("worktree "):], repo.path)  # only the main checkout's own entry is exempt
               for e in review.git(repo.path, "worktree", "list", "--porcelain").split("\n\n")):  # (REQ-20)
            raise _refused("elsewhere", _tag(x, f"{target} 检出在另一个 worktree 里，没动它；" + (
                f"那个 worktree 离开 {target} 后重新 land" if not final or x["merged"] or x["on_target"]
                else f"在那里执行 git merge --ff-only {x['new']}")))
    for x in rs if final else ():
        if review.git(x["repo"].path, "rev-parse", f"refs/heads/{x['target']}") != x["start"]:
            raise _refused("moved", _tag(x, f"{x['target']} 在验证期间移动了（起点 {x['start']}），没动它；重新 land"))


def _final(root, batch_id, pairs, hd, rs, auto):
    """REQ-1's check after the commands, in the caller's project-lock hold: git and file reads only, no lock taken
    (flock is not re-entrant), no command run."""
    if (st := review.load_batch(root, batch_id).get("state")) != "delivered":
        raise _refused("state", f"{batch_id} 在 land 期间变成了 {st}，目标分支没动")
    if (now := review.heads(pairs)) != hd or not _gate(root, batch_id, hd)[1]:
        raise _refused("gate", f"{batch_id} 在 land 期间 heads 变了或门禁不再通过（合并的是 {hd}，现在 {now}），"
                               "目标分支没动；重跑门禁后再 land")
    _ready(rs, final=True)
    if auto and (no := auto_blockers(root, batch_id)):
        raise _refused("auto", "持锁再核时不满足自动合入的条件，目标分支没动：" + "；".join(no))


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
