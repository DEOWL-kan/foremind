"""Phase: bounds (m2c.6, REQ-11 [防对抗]): writes, pushes and user claims a seat made by ways the guard does not see
(wrappers, interpreters, variables, eval). It finds and reports; it stops nothing (§14: the guard and the audit are
no sandbox). No model call, so it runs under a full block too.

  worktree  each repo of a batch in LIVE: the commits since the merge-base of HEAD with the local target ref (no
            fetch; an upstream's approved head too, for a stacked batch) and what is uncommitted or untracked, each
            path outside owns_paths, or hit by the guard's path patterns (#4 manifests, #7 migrations, configured
            ones) or a #22 glob without a valid exemption; of what git ignores, #22 files only -> `bounds_violation
            {batch, kind: "worktree", paths: [{path, why}], fingerprint}` once per fingerprint, the list with what to
            do to the lock holder's inbox (a seat's), one P1
  remote    each such repo whose push_pr is the user's (#23), every supervisor.merged_check_min: a remote branch of
            any name whose head is one of the batch's commits (also counted from where its seat started it, which a
            push to the target does not move), or the batch branch (ls-remote), or a PR whose head is
            (gh pr list, GitHub remotes only) -> `bounds_violation {batch, kind: "remote", repos: [{repo, branch,
            on_remote: [remote branch], prs}], fingerprint}`, one P1
  claims    an event claiming the user whose `seat_ancestor` (events.EventLog.append) is "unknown" -> one P1 per
            event. One naming a session is an L0 hard failure (audit.l0, before any action of the pass)
A worktree or remote check of a batch that raises (git, gh, config) is no pass: `bounds_error {batch, kind, error,
fingerprint}`, one P1 per fingerprint. In a worktree with a merge or rebase stopped midway (update's conflict,
resolved by the seat there) all is checked but what a merge staged (entries with a staged side: the target brings
them in) and a rebasing repo's remote (HEAD is detached; the next check takes it). A repo midway more than
MIDWAY_PASSES passes in a row is a bounds_error. Notices carry batch ids and counts, no paths (§8.7).
"""
import json
import os
from datetime import datetime, timezone

from foremind import audit, exemptions, lock, pathmatch, repos, review, seat, worktree
from foremind.hooks import guard
from foremind.supervisor.tick import setting

LIVE = ("running", "changes_requested", "review_ready", "in_review", "approved")
NAMES = {"worktree": "worktree 改动", "remote": "远端分支与 PR"}
# git ignores these but #22 does not: each SYSTEM_GLOBS "*/x" as a pathspec from the worktree root (a trailing /*
# takes the tree, as fnmatch's * does); guard.system_glob still decides
SYSTEM_SPECS = [":(glob,icase)**/" + (g[2:-1] + "**" if g.endswith("/*") else g[2:]) for g in guard.SYSTEM_GLOBS]
_REMOTE_AT = {}  # (root, batch): last remote check. ponytail: this process only; a new one checks at once
MIDWAY_PASSES = 3
PR_LIMIT = 200
_MIDWAY = {}  # (root, batch, repo): passes in a row found midway. ponytail: this process only, as _REMOTE_AT


def run(t, blocked):
    claims(t)
    every = setting(t.cfg, "supervisor.merged_check_min") * 60
    for bid in t.order:
        if t.state(bid) not in LIVE:
            continue
        found(t, bid, "worktree", outside)
        if t.now - _REMOTE_AT.get((t.root, bid), 0) >= every:
            found(t, bid, "remote", pushed)
            _REMOTE_AT[(t.root, bid)] = t.now


def found(t, bid, kind, check):
    """Tells first and records last, each once per key: a pass cut short between them tells no one twice."""
    try:
        facts = check(t, bid)
    except Exception as e:  # whatever it is, the check did not pass
        return error(t, bid, kind, f"{type(e).__name__}: {e}"[:300])
    fp = audit.fingerprint("bounds_violation", bid, kind, json.dumps(facts, sort_keys=True))
    if not facts or f"bounds_violation:{fp}" in t.results:
        return
    if kind == "worktree" and (s := t.holders.get(bid)) and s != lock.USER:  # the user has no inbox
        t.say(s, "Foremind：本批 worktree 里有越界改动（owns_paths 之外，或依赖清单、迁移、#22 文件而本批没有有效凭据）：\n"
              + "\n".join(f"- {x['path']}（{x['why']}）" for x in facts)
              + "\n处理：撤回这些改动；确实要改，执行 `foremind decide --new` 申请扩大范围（#8）或对应类别的凭据，获批前不要绕路。",
              f"bounds:{fp}")
    t.notify(f"bounds:{fp}", f"{bid} 越界",
             f"{bid} 的 {NAMES[kind]}有 {len(facts)} 处越出本批边界"
             + ("，已告知席位撤回或申请 #8 / 凭据。" if kind == "worktree" else "：推送分支与开 PR 归你（#23）。")
             + "详情见 .foremind/events.jsonl 的 bounds_violation。")
    t.emit("bounds_violation", dedupe_id=f"bounds_violation:{fp}", batch=bid, kind=kind, fingerprint=fp,
           **{"paths" if kind == "worktree" else "repos": facts})


def error(t, bid, kind, err, body=None):
    """bounds_error once per fingerprint, told before it is recorded (as found)."""
    fp = audit.fingerprint("bounds_error", bid, kind, err)
    if f"bounds_error:{fp}" not in t.results:
        t.notify(f"bounds_error:{fp}", f"{bid} 越界核对出错",
                 (body or f"{bid} 的 {NAMES[kind]}核对没能做完（git、gh 或配置读不出），不算通过；下一轮再核对。")
                 + "详情见 .foremind/events.jsonl 的 bounds_error。")
        t.emit("bounds_error", dedupe_id=f"bounds_error:{fp}", batch=bid, kind=kind, error=err, fingerprint=fp)


def _repos(t, bid):
    """[(repo, worktree, "merge" / "rebase" stopped there or None, HEAD, the rebase's onto file)] of the batch's seat,
    and its config (the batch's; the project's on a config error). A rebase is known by its onto: `git am` has none,
    and its HEAD stays on the branch."""
    cfg = t.bcfg(bid) or t.cfg
    by_id = {r.id: r for r in repos.load_repos(t.root, cfg)}
    out = []
    for wt in t.worktrees(bid):  # worktree.batch_dir(…)/<repo id>
        head, merge, *ontos = review.git(wt, "rev-parse", "HEAD", "--git-path", "MERGE_HEAD", "--git-path",
                                         "rebase-merge/onto", "--git-path", "rebase-apply/onto").splitlines()
        onto = next((wt / o for o in ontos if (wt / o).exists()), None)
        out.append((by_id[wt.name], wt, "merge" if (wt / merge).exists() else "rebase" if onto else None, head, onto))
    return out, cfg


def outside(t, bid) -> list:
    """[{path: "<repo>:<path>", why}] of the paths the batch changed outside its bounds."""
    h = t.headers[bid]
    pairs, cfg = _repos(t, bid)
    for repo, _, how, head, _ in pairs:  # each repo on its own: one error per repo and HEAD
        n = _MIDWAY[(t.root, bid, repo.id)] = _MIDWAY.get((t.root, bid, repo.id), 0) + 1 if how else 0
        if n > MIDWAY_PASSES:
            error(t, bid, "worktree", f"midway for more than {MIDWAY_PASSES} passes: {repo.id} {how} at {head[:12]}",
                  f"{bid} 的 worktree 有仓库连续 {MIDWAY_PASSES} 轮以上停在"
                  + ("合并中，其间合并暂存的改动不核" if how == "merge" else "变基中，其间该仓库的远端不核")
                  + "，不算通过；请核实席位是否卡住。")
    owns = [p for p in h.get("owns_paths") or [] if isinstance(p, str)]
    pats, now, out = guard.active_patterns(cfg), datetime.fromtimestamp(t.now, timezone.utc), []
    for repo, wt, how, _, onto in pairs:
        paths, hidden = changed(t, bid, repo, wt, cfg, how, onto)
        for rel in paths + hidden:
            path, qual = os.path.realpath(wt / rel), f"{repo.id}:{rel}"
            forms = [path, qual]
            why = [] if rel in hidden or pathmatch.owns(qual, owns) else ["owns_paths"]  # as guard._matrix
            # the guard's own Edit/Write path hits; of an ignored file (build output, dependencies) #22 only
            hits = [] if rel in hidden else guard._match(pats, "paths", [(forms, forms, None)])
            # a symlink by its target and by its own name (m2d.7 ⑤: CLAUDE.local.md -> notes.txt)
            if g := guard.system_glob(path) or guard.system_glob(os.path.join(os.path.realpath(wt), rel)):
                hits.append(guard.Hit(22, "paths", forms, g))
            why += [f"#{x.category}" for x in hits
                    if not exemptions.find(t.root, bid, "paths", x.values, category=x.category, header=h, now=now)]
            if why:
                out.append({"path": qual, "why": "、".join(dict.fromkeys(why))})
    return out


def changed(t, bid, repo, wt, cfg, how=None, onto=None) -> tuple[list[str], list[str]]:
    """(repo paths committed since the base (renames as their two sides), uncommitted and untracked ones; ignored
    ones a #22 glob may take). While a merge is stopped (`how`) what has a staged side may be the target's: of git
    status the unstaged-only and untracked entries. While a rebase is (its `onto` file) HEAD is detached over onto: the
    commits are the branch's as it was (orig-head) and those on HEAD since onto; its index and tree are the seat's."""
    refs = _refs(t, bid, repo, wt, cfg)
    diff = ["git", "diff", "--name-only", "--no-renames", "-z"]
    tip = onto.with_name("orig-head").read_text().strip() if onto else "HEAD"
    base = review.git(wt, "merge-base", tip, *refs)  # the newest common ancestor with any of them
    committed = review.run([*diff, base, tip], wt).stdout
    if onto:  # from HEAD's newest common ancestor with onto or a ref: the seat may move HEAD off onto, or onto may
        # sit below the fork (`rebase -i HEAD~N`), the target's commits then fast-forwarded onto HEAD (m2c.6 r5)
        start = review.git(wt, "merge-base", "HEAD", onto.read_text().strip(), *refs)
        committed += review.run([*diff, start, "HEAD"], wt).stdout
    status = review.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames"], wt).stdout
    ignored = review.run(["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--",
                          *SYSTEM_SPECS], wt).stdout
    mine = [x[3:] for x in status.split("\0") if how != "merge" or x.startswith((" ", "?"))]  # XY: X the staged side
    return sorted({*committed.split("\0"), *mine} - {""}), sorted(set(ignored.split("\0")) - {""})


def _refs(t, bid, repo, wt, cfg) -> list:
    """What the batch's commits in `repo` start from: the target (where seat._target starts it) and its approved
    upstreams' heads."""
    tb = review.repo_cfg(cfg, repo.id, "target_branch") or repo.default_branch
    rem = worktree.remote_name(wt, repo.remote)
    if not rem and not tb:
        raise review.FlowError(f"{repo.id}: no target branch and no remote")
    return [f"refs/remotes/{rem}/{tb or 'HEAD'}" if rem else f"refs/heads/{tb}",
            *(hd[repo.id] for u in t.headers[bid].get("depends_on") or []
              if repo.id in (hd := seat.approved_heads(t.root, u) or {}))]


def _fixed(t, bid, repo) -> list:
    """Bases of the batch in `repo` no push moves (m2d.7 ⑥): where its first seat started it (seat_opened, seat_user
    `starts`) and what `foremind update` brought it onto (batch_updated `bases`). Only the first start: a later open
    (the batch sent back to ready, seat --user, a continuation) reuses the branch, its start already holding the
    batch's commits (r2). None recorded (opened before m2d.7): []."""
    evs = [e for e in t.evs if e.get("batch") == bid]
    first = next((s for e in evs if e["type"] in ("seat_opened", "seat_user")
                  if (s := (e.get("starts") or {}).get(repo.id))), None)
    bases = [(e.get("bases") or {}).get(repo.id) for e in evs if e["type"] == "batch_updated"]
    return list(dict.fromkeys(x for x in [first, *bases] if x))


def pushed(t, bid) -> list:
    """[{repo, branch, on_remote, prs}] of the batch's repos whose push is the user's and whose commits reached the
    remote (m2d.7 ⑥): `on_remote` the remote branches, any name, whose head is one of the batch's commits (since
    _refs, and HEAD's first parents since _fixed) or that are the batch branch; `prs` the PRs (any state) whose head
    commit is, or whose branch is the batch's. A repo whose rebase is stopped (HEAD detached) waits for the next check.
    ponytail: the newest PR_LIMIT PRs only; page with `gh api` if a repo opens more than that between two checks.
    ponytail: a seat's own rebase onto the target (update's conflict, update_method rebase), or a fast-forward to it
    before its first commit (`git pull`), puts the target's commits on HEAD's first parents: a remote branch or PR at
    one of them is reported until an update records a new base."""
    pairs, cfg = _repos(t, bid)
    out = []
    for repo, wt, how, _, _ in pairs:
        if how == "rebase" or review.push_by_system(cfg, repo.id) or not (rem := worktree.remote_name(wt, repo.remote)):
            continue
        br, refs = review.branch(wt), _refs(t, bid, repo, wt, cfg)
        mine = set(review.git(wt, "rev-list", "HEAD", "--not", *refs).split())
        if fixed := _fixed(t, bid, repo):  # a push to the target (and a fetch after it) moves refs onto the batch's
            # commits; where the batch started does not move. First parents: a target merged in is not the batch's
            mine |= set(review.git(wt, "rev-list", "--first-parent", "HEAD", "--not", *fixed, *refs[1:]).split())
        heads = [x.partition("\trefs/heads/") for x in review.git(wt, "ls-remote", "--heads", rem).splitlines()]
        on = sorted(n for sha, _, n in heads if sha in mine or n == br)
        prs = []
        if "github.com" in review.git(wt, "remote", "get-url", rem):  # as seat._pr_heads: no other host has PRs here
            prs = [p["number"] for p in json.loads(review.gh(wt, "pr", "list", "--state", "all", "--limit",
                                                             str(PR_LIMIT), "--json", "number,headRefOid,headRefName"))
                   if p["headRefOid"] in mine or p["headRefName"] == br]
        if on or prs:
            out.append({"repo": repo.id, "branch": br, "on_remote": on, "prs": prs})
    return out


def claims(t):
    for e in t.evs:
        if e.get("seat_ancestor") != "unknown" or f"notify:seat_ancestor_unknown:{e['id']}" in t.results:
            continue  # a session's is audit.l0's; this one sent, deferred or held before
        t.notify(f"seat_ancestor_unknown:{e['id']}", "记作用户所为的事件无法核对",
                 f"一条 {e['type']} 事件记作用户所为，但读不出写入它的进程祖先，核对不了是否由 Foremind 会话代写；"
                 "请核实（.foremind/events.jsonl 里带 seat_ancestor 的事件）。")
