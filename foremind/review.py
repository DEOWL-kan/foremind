"""`foremind review` and the one-shot reviewer (DESIGN §7.1, §7.4, §1.6, §20 I9 I25 I33).

Seat side: `request()` checks every repo's head is committed, pushes and opens PRs where #23 belongs to the system,
and moves the batch to review_ready. Supervisor side: `start()` checks the requested heads out detached into
reviews/<session>/<repo> (§20 I50) and launches a read-only headless `claude -p` on them through job.start;
`harvest()` deletes the checkouts and turns its stdout into `batches/<id>.review.r<n>.json`. The reviewer only supplies `verdict`
and `issues[].{severity, location, summary, disputed}` and a must_fix's basis (REQ-15); everything else is written by the
program, which lowers the must_fix it cannot back and sets the verdict from what is left (REQ-15, REQ-16).

Also the shared plumbing of review / acceptance / gate: batch header, repos and worktrees, heads, git and gh.
Config keys read here (flat, merged by config.load): delivery.repo.<id>.{target_branch, push_pr, ci}, gate.ci,
routes.reviewer.{model, effort, effort_s, effort_m, effort_l, effort_security}, exclude.models, exclude.providers,
oneshot.timeout_min, oneshot.exclude_dynamic_prompt, review.max_rounds, review.max_failures, review.max_budget_usd,
review.cost_cap_usd_{s, m, l}, review.cost_cap_tokens_{s, m, l}, review.new_must_fix_max, quota.stale_min.
"""
import json
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path

from foremind import config, defaults, header, inbox, job, lock, repos, schemas, seat, state, worktree
from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock, sha256_bytes
from foremind.paths import find_project_root, state_dir
from foremind.plan import model
from foremind.supervisor import quota

_ROLE_CARD = Path(__file__).resolve().parent.parent / "templates" / "roles" / "reviewer.md"
READ_ONLY_TOOLS = "Read,Grep,Glob"
# the reviewer's output (`--json-schema`, REQ-13): the fields assemble() takes; a must_fix's basis (REQ-15) is
# checked there, not here, so a must_fix without one is lowered and kept rather than failing the review
REVIEW_SCHEMA = {"type": "object", "required": ["verdict", "issues"], "properties": {
    "verdict": {"enum": ["approved", "changes_requested"]},
    "issues": {"type": "array", "items": {
        "type": "object", "required": ["severity", "location", "summary"],
        "properties": {"severity": {"enum": list(schemas.SEVERITIES)}, "location": {"type": "string"},
                       "summary": {"type": "string"}, "disputed": {"type": "boolean"},
                       "basis": {"enum": list(schemas.REVIEW_BASES)}, "req": {"type": "string"},
                       "quote": {"type": "string"}, "form": {"enum": ["listed", "new"]}, "broken": {"type": "string"},
                       "category": {"type": "integer", "minimum": schemas.CATEGORIES[0],
                                    "maximum": schemas.CATEGORIES[-1]}}}}}}


class FlowError(Exception):
    pass


# --- processes ---------------------------------------------------------------

TIMEOUT_S = None  # default bound of run(); the supervisor sets one for its git / gh calls (M1-7 SF-4)


def run(argv, cwd, *, check=True, input=None, text=True, timeout=None):
    timeout = timeout or TIMEOUT_S
    try:
        p = subprocess.run(argv, cwd=cwd, input=input, capture_output=True, text=text, timeout=timeout)
    except OSError as e:
        raise FlowError(f"{argv[0]}: {e}") from None
    except subprocess.TimeoutExpired:
        raise FlowError(f"{' '.join(map(str, argv[:4]))}: no answer within {timeout} s") from None
    if check and p.returncode:
        err = p.stderr if text else p.stderr.decode(errors="replace")
        raise FlowError(f"{' '.join(map(str, argv[:4]))}: {err.strip() or f'exit {p.returncode}'}")
    return p


def git(cwd, *args, check=True) -> str:
    return run(["git", *args], cwd, check=check).stdout.strip()


def gh(cwd, *args, check=True) -> str:
    return run(["gh", *args], cwd, check=check).stdout.strip()


def events(root) -> EventLog:
    return EventLog(state_dir(root) / "events.jsonl")


def all_events(root) -> list[dict]:
    """Archived months first, then the current file: chronological."""
    archived = sorted((state_dir(root) / "archive").glob("events-*.jsonl"))
    return [e for log in (*map(EventLog, archived), events(root)) for e in log.iter()]


# --- batch header and state ----------------------------------------------------

def batch_path(root, batch_id) -> Path:
    if not isinstance(batch_id, str) or not re.fullmatch(schemas.BATCH_ID, batch_id):
        raise FlowError(f"bad batch id {batch_id!r}")
    return state_dir(root) / "batches" / f"{batch_id}.md"


def load_batch(root, batch_id) -> dict:
    try:
        h, _ = header.parse(batch_path(root, batch_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FlowError(f"no batch {batch_id}") from None
    except header.HeaderError as e:
        raise FlowError(f"{batch_id}: {e}") from None
    if errs := schemas.validate("batch_header", h):
        raise FlowError(f"{batch_id}: invalid header: {'; '.join(errs[:3])}")
    return h


def context(batch_id) -> tuple:
    """(project root, merged config with the batch's task layer) for the CLI commands."""
    if not batch_id:
        raise FlowError("no batch: pass one or set FOREMIND_BATCH")
    root = find_project_root()
    return root, config.load(root, config.task_layer(load_batch(root, batch_id)))


def set_state(root, batch_id, to, *, expect=None, before=None, status=None, **fields) -> str:
    """Main-path transition under the project lock; `before(header)` runs inside the lock, after the checks and
    ahead of the write (it may refuse by raising); `status` replaces the body's `## 状态` section in the same write.
    Entering a side branch records `state_prior` (§20 I1), leaving one drops it. Returns the previous state."""
    path = batch_path(root, batch_id)
    with project_lock(root):
        h, body = header.parse(path.read_text(encoding="utf-8"))
        cur = h.get("state", "planned")
        if expect is not None and cur not in expect:
            raise FlowError(f"{batch_id} is {cur}, expected one of {list(expect)}")
        try:
            state.transition(state.BATCH, cur, to)
        except state.IllegalTransition as e:
            raise FlowError(str(e)) from None
        if before:
            before(h)
        h["state"] = to
        if to in state.BATCH_SIDE:
            h["state_prior"] = cur
        elif cur in state.BATCH_SIDE:
            h.pop("state_prior", None)
            h.pop("blocked_reason", None)
        if status is not None:  # the section is the body's last (plan.model.spec)
            body = "\n\n".join(x for x in (model.spec(body).rstrip("\n"), f"## 状态\n{status.strip()}\n") if x)
        atomic_write(path, header.render(h, body))
        events(root).append("batch_state", batch=batch_id, prior=cur, state=to, **fields)
    return cur


STATUS_MAX_LINES = 40


def request_changes(root, batch_id, items, *, by, reason="requested", title="总控要求修改") -> str:
    """The controller's (or the user's) must-fix list (§1.6): -> changes_requested, event changes_requested_by, the
    items as the batch file's `## 状态` section (outside plan_hash, in L1), at most STATUS_MAX_LINES lines. A holder
    (not user) gets them in its inbox and the batch moves on to running; with none it stays changes_requested for the
    supervisor's successor, who reads them in L1. Returns the state it ends in. `foremind update` sends its conflict
    and re-review lists the same way, under its own `reason` and `title`."""
    lines = [ln.strip() for i in items for ln in str(i).splitlines() if ln.strip()]
    if not lines:
        raise FlowError("request-changes needs at least one --item")
    head = f"{title}（{by}，{datetime.now(timezone.utc).isoformat(timespec='seconds')}）："
    body = [f"- {ln}" for ln in lines]
    if len(body) > STATUS_MAX_LINES - 1:
        body = body[:STATUS_MAX_LINES - 2] + [f"- ……另有 {len(body) - STATUS_MAX_LINES + 2} 行未写入"]
    status = "\n".join([head, *body])
    s = lock.holder(root, batch_id)
    s = None if s == lock.USER else s

    def tell(_h):  # after the transition checks, before the write: a failed send changes nothing and may be retried
        if s:
            inbox.append(s, f"Foremind：{status}\n改完提交，再执行 `foremind review`。", sender="controller", root=root)

    set_state(root, batch_id, "changes_requested", before=tell, status=status, reason=reason, by=by)
    events(root).append("changes_requested_by", batch=batch_id, by=by, items=lines)
    if not s:
        return "changes_requested"
    set_state(root, batch_id, "running", expect=("changes_requested",))
    return "running"


def withdraw(root, batch_id, fp, reason, *, by) -> dict:
    """REQ-15: the controller (or the user) rules a reported issue out: review_withdrawn{batch, fingerprint, reason,
    by}. From the next receipt on, a must_fix with that fingerprint is a note (filtered withdrawn); receipts already
    written stay as they are. The fingerprint must be one of the batch's receipts' (a typo would withdraw nothing)."""
    if not isinstance(fp, str) or not re.fullmatch(schemas.FINGERPRINT, fp):
        raise FlowError(f"bad fingerprint {fp!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise FlowError("--withdraw needs --reason")
    if not any(isinstance(r, dict) and fp in (i.get("fingerprint") for i in r.get("issues", []))
               for r in load_receipts(root, batch_id)):
        raise FlowError(f"{batch_id}: no review receipt has an issue with fingerprint {fp}")
    return events(root).append("review_withdrawn", batch=batch_id, fingerprint=fp, reason=reason.strip(), by=by)


def withdrawn(evs, batch_id, session=None) -> dict:
    """{fingerprint: reason} of the batch's review_withdrawn events, the latest reason winning; with `session`, only
    those before that review started: what its materials listed, and the same at every harvest of it (m2d.5 r1)."""
    out = {}
    for e in evs:
        if session and e["type"] == "review_started" and e.get("reviewer_session") == session:
            break
        if e["type"] == "review_withdrawn" and e.get("batch") == batch_id:
            out[e["fingerprint"]] = e.get("reason")
    return out


# --- repos, worktrees, heads -----------------------------------------------------

def batch_repos(root, h, cfg, *, main=False) -> list[tuple]:
    """[(Repo, worktree)] in the header's `repos` order, at the seat's worktree paths (I39). `main`: a worktree that
    is gone (the batch delivered and cleaned up) is replaced by the repo's main checkout, where its commits are still
    reachable (REQ-14)."""
    known = {r.id: r for r in repos.load_repos(root, cfg)}
    if missing := [r for r in h["repos"] if r not in known]:
        raise FlowError(f"{h['id']}: repos not registered: {missing}")
    d = worktree.batch_dir(seat.project_slug(root, cfg), h["id"])
    out = [(known[r], d / r if not main or (d / r).is_dir() else known[r].path) for r in h["repos"]]
    if gone := [str(wt) for _, wt in out if not wt.is_dir()]:
        raise FlowError(f"{h['id']}: no worktree at {gone}")
    return out


def heads(pairs) -> dict:
    out = {r.id: git(wt, "rev-parse", "HEAD") for r, wt in pairs}
    if bad := [r for r, sha in out.items() if not re.fullmatch(schemas.SHA, sha)]:
        raise FlowError(f"not a full lowercase SHA: {bad}")
    return out


def heads_hash(hd: dict) -> str:
    return sha256_bytes(json.dumps(hd, sort_keys=True).encode())[:16]


def dirty(pairs) -> list[str]:
    return [r.id for r, wt in pairs if git(wt, "status", "--porcelain")]


def branch(wt) -> str:
    b = git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False)
    if not b:
        raise FlowError(f"{wt}: detached HEAD, no branch to push")
    return b


def remote(repo, wt) -> str | None:
    """`[[repos]].remote` is a remote name (default origin); None when the repo has no such remote (local only)."""
    name = repo.remote or "origin"
    return name if run(["git", "remote", "get-url", name], wt, check=False).returncode == 0 else None


def remote_head(wt, rem, br) -> str | None:
    out = git(wt, "ls-remote", rem, f"refs/heads/{br}").split()
    return out[0] if out else None


def trees(pairs, hd) -> dict:
    """{repo: the tree of its head in hd}; None for a commit git does not have (never equal to a real tree)."""
    out = {}
    for r, wt in pairs:
        sha = hd.get(r.id)
        ok = isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40,64}", sha)
        out[r.id] = ok and git(wt, "rev-parse", "--verify", "-q", f"{sha}^{{tree}}", check=False) or None
    return out


def is_ancestor(wt, a, b) -> bool:
    """False as well when `a` is not in the local object store (someone else's commit, never fetched)."""
    return run(["git", "merge-base", "--is-ancestor", a, b], wt, check=False).returncode == 0


def bases(root, batch_id, pairs, hd) -> dict:
    """{repo: merge-base} recorded by the latest `foremind review` (§20 I48) or `foremind update` (§7.6), kept only
    where the head recorded with it is the current head `hd` or an ancestor of it: a worktree reset elsewhere (onto
    the target, say) has no base. {} if review was never requested."""
    e = next((e for e in reversed(all_events(root))
              if e["type"] in ("review_requested", "batch_updated") and e.get("batch") == batch_id), None)
    if not e:
        return {}
    rec, old = e.get("bases") or {}, e.get("heads") or {}
    return {r.id: rec[r.id] for r, wt in pairs
            if rec.get(r.id) and old.get(r.id) and is_ancestor(wt, old[r.id], hd[r.id])}


def repo_cfg(cfg, repo_id, key, fallback=None, default=None):
    v = cfg.get(f"delivery.repo.{repo_id}.{key}")
    return v if v is not None else cfg.get(fallback, default) if fallback else default


def push_by_system(cfg, repo_id) -> bool:
    # #23; unanswered = user (DESIGN §7.5)
    return repo_cfg(cfg, repo_id, "push_pr", default=defaults.TABLE["delivery.repo.*.push_pr"]) == "system"


def ci_mode(cfg, repo_id) -> str:
    return repo_cfg(cfg, repo_id, "ci", "gate.ci", defaults.TABLE["gate.ci"])


def target_branch(repo, cfg) -> str:
    t = repo_cfg(cfg, repo.id, "target_branch") or repo.default_branch
    if not t:
        raise FlowError(f"{repo.id}: target branch unknown; set delivery.repo.{repo.id}.target_branch")
    return t


def target_ref(repo, wt, cfg) -> str:
    """Ref of the target branch, freshly fetched when the repo has a remote."""
    t = target_branch(repo, cfg)
    if rem := remote(repo, wt):
        git(wt, "fetch", "-q", rem, f"+refs/heads/{t}:refs/remotes/{rem}/{t}")
        return f"refs/remotes/{rem}/{t}"
    return f"refs/heads/{t}"


def checkout(pairs, hd, dest):
    """§20 I50: the heads checked out detached at dest/<repo> with hooks off (no post-checkout, LFS, lefthook):
    acceptance runs there and the reviewer reads there, never in the seat's live worktree. Tracked files only;
    commands prepare their own dependencies. drop_checkouts() removes them."""
    for r, wt in pairs:
        git(wt, "-c", "core.hooksPath=/dev/null", "worktree", "add", "-q", "--detach", str(dest / r.id), hd[r.id])


def drop_checkouts(pairs, dest):
    """Unregisters and deletes our own checkouts only (no global `git worktree prune`)."""
    # ponytail: a SIGKILLed run leaves its checkout registered until git's own gc prunes it
    for r, wt in pairs:
        run(["git", "worktree", "remove", "--force", str(dest / r.id)], wt, check=False)
        shutil.rmtree(dest / r.id, ignore_errors=True)


def pr_view(wt, br) -> dict | None:
    p = run(["gh", "pr", "view", br, "--json", "number,url,state,isDraft,headRefOid,mergeCommit"], wt, check=False)
    return json.loads(p.stdout) if p.returncode == 0 else None


# --- seat: foremind review ---------------------------------------------------------

def _last_started(evs, batch_id):
    return next((e for e in reversed(evs) if e["type"] == "review_started" and e.get("batch") == batch_id), None)


def _last_pushed(evs, batch_id, repo_id) -> str | None:
    """Head this program last pushed for the repo: its latest review_push event (older logs: the latest
    review_requested event that has its PR)."""
    for e in reversed(evs):
        if e.get("batch") != batch_id:
            continue
        if e["type"] == "review_push" and e.get("repo") == repo_id:
            return e.get("head")
        if e["type"] == "review_requested" and repo_id in (e.get("prs") or {}):
            return e["heads"].get(repo_id)
    return None


def _push(root, evs, batch_id, r, wt, rem, br, head, git_opts=()):
    """The lease names the expected SHA itself: a background fetch refreshing refs/remotes/ cannot void it. Each
    push is recorded right away, so a crash before review_requested cannot make our own push look foreign.
    `git_opts` go before `push` (`foremind update` turns hooks off with them)."""
    now = remote_head(wt, rem, br)
    if now == head:
        return
    if now is None or is_ancestor(wt, now, head):
        git(wt, *git_opts, "push", "-q", rem, f"{head}:refs/heads/{br}")
    elif now == _last_pushed(evs, batch_id, r.id):  # the seat rewrote what we pushed
        git(wt, *git_opts, "push", "-q", f"--force-with-lease=refs/heads/{br}:{now}", rem, f"{head}:refs/heads/{br}")
    else:
        raise FlowError(f"{r.id}: remote {br} is at {now}, pushed by someone else (not in {head}, not our last "
                        "push); fetch and integrate it, then request the review again")
    events(root).append("review_push", batch=batch_id, repo=r.id, head=head)


def _pr_at(r, wt, br, head) -> dict:
    """The PR once the host shows `head`: headRefOid lags a push by a moment."""
    for delay in (0, 1, 2, 4):
        time.sleep(delay)
        if (pr := pr_view(wt, br)) and pr.get("headRefOid") == head:
            return pr
    raise FlowError(f"{r.id}: PR head {pr and pr.get('headRefOid')} != local head {head}")


def _open_prs(root, h, cfg, pairs, hd, evs) -> dict:
    urls, created = {}, False
    for r, wt in pairs:
        if not push_by_system(cfg, r.id):
            continue
        br, rem = branch(wt), remote(r, wt)
        if not rem:
            raise FlowError(f"{r.id}: #23 is the system's but the repo has no remote {r.remote or 'origin'!r}")
        _push(root, evs, h["id"], r, wt, rem, br, hd[r.id])
        if pr_view(wt, br) is None:
            args = ["pr", "create", "--head", br, "--base", target_branch(r, cfg), "--title", f"{h['id']} ({r.id})",
                    "--body", f"Foremind batch {h['id']}."]
            gh(wt, *args, *(["--draft"] if ci_mode(cfg, r.id) == "local_first" else []))
            created = True
        urls[r.id] = _pr_at(r, wt, br, hd[r.id])["url"]
    if created and len(urls) > 1:  # PRs of one batch link each other (DESIGN §1.8)
        body = f"Foremind batch {h['id']}; PRs: " + ", ".join(f"{k} {u}" for k, u in urls.items())
        for r, wt in pairs:
            if r.id in urls:
                gh(wt, "pr", "edit", branch(wt), "--body", body)
    return urls


def request(root, batch_id, cfg, *, session=None) -> dict:
    h = load_batch(root, batch_id)
    cur = h.get("state", "planned")
    approved = ("approved", "awaiting_audit", "delivered")
    if cur not in ("running", "changes_requested", "review_ready", "in_review", *approved):
        raise FlowError(f"{batch_id} is {cur}; review is requested from running")
    pairs = batch_repos(root, h, cfg)
    if bad := dirty(pairs):
        raise FlowError(f"uncommitted changes in {bad}; commit before `foremind review`")
    hd = heads(pairs)
    evs = all_events(root)
    last = _last_started(evs, batch_id)
    if cur in ("in_review", *approved) and last and last.get("heads") == hd:
        if cur != "in_review":
            raise FlowError(f"{batch_id} is {cur} at these heads; nothing new to review")
        return {"state": cur, "heads": hd, "prs": {}, "unchanged": True}
    # N9 (REQ-19): asking again on the same heads would only swap the reviewer until one approves. Same heads = same
    # trees, so an empty or message-only commit is no change (m2b.7 r3)
    now = trees(pairs, hd)
    seen = [r for r in load_receipts(root, batch_id) if isinstance(r, dict) and isinstance(r.get("heads"), dict)
            and r["heads"].keys() == hd.keys() and trees(pairs, r["heads"]) == now]
    if seen:
        raise FlowError(f"{batch_id}: these heads already have review receipt r{seen[-1].get('round')} "
                        f"({seen[-1].get('verdict')}); change the code first (a commit that changes files), or have "
                        f"the controller run `foremind review {batch_id} --request-changes`")
    prior, bs = bases(root, batch_id, pairs, hd), {}
    for r, wt in pairs:  # §20 I48
        b = git(wt, "merge-base", target_ref(r, wt, cfg), hd[r.id])
        # merged with a merge commit, a repo's head is its merge-base: the recorded base still finds its changes
        bs[r.id] = prior[r.id] if b == hd[r.id] and r.id in prior else b
    prs = _open_prs(root, h, cfg, pairs, hd, evs)
    if cur in approved:  # a new head voids the approval (§1.6): a new round
        set_state(root, batch_id, "changes_requested", expect=(cur,), reason="head changed after approval")
        cur = "changes_requested"
    if cur == "changes_requested":  # the seat is already fixing: record the implied running step
        set_state(root, batch_id, "running", expect=(cur,))
        cur = "running"
    if cur != "review_ready":
        set_state(root, batch_id, "review_ready", expect=(cur,))
    events(root).append("review_requested", batch=batch_id, heads=hd, bases=bs, requested_by=session or "user",
                        prs=prs)
    return {"state": "review_ready", "heads": hd, "prs": prs}


# --- supervisor: start and harvest the reviewer --------------------------------------

def receipts(root, batch_id) -> list[tuple[int, Path]]:
    out = []
    for p in (state_dir(root) / "batches").glob(f"{batch_id}.review.r*.json"):
        if m := re.fullmatch(rf"{re.escape(batch_id)}\.review\.r([1-9][0-9]*)\.json", p.name):
            out.append((int(m[1]), p))
    return sorted(out)


def next_round(evs, batch_id) -> int:
    """Number of the next receipt, r<n>: reviews (every review_started, so a failed one never hands its number on)
    and rebound receipts (review_receipt events with rebound_from, `foremind update`) draw from one sequence, so the
    newest receipt always has the highest number."""
    return 1 + sum(e.get("batch") == batch_id and (e["type"] == "review_started" or
                                                   (e["type"] == "review_receipt" and "rebound_from" in e))
                   for e in evs)


def load_receipts(root, batch_id) -> list[dict]:
    try:
        return [json.loads(p.read_text(encoding="utf-8")) for _, p in receipts(root, batch_id)]
    except ValueError as e:
        raise FlowError(f"{batch_id}: unreadable receipt: {e}") from None


_GUARDED = re.compile(r"^(REQ-[1-9][0-9]*)[:：]\s*\[(?:防误操作|防对抗)\]", re.M)
_ADVERSARIAL = re.compile(r"^(REQ-[1-9][0-9]*)[:：]\s*\[防对抗\]", re.M)
# REQ-15: a REQ's text runs from its line to the next line starting REQ- or 约束：, or the end
_REQ_TEXT = re.compile(r"^(REQ-[1-9][0-9]*)[:：].*?(?=^REQ-|^约束[:：]|\Z)", re.M | re.S)


def goal(root, h) -> str | None:
    """The batch's frozen goal.md, None when unreadable."""
    try:
        return (state_dir(root) / "plans" / h["plan_id"] / "goal.md").read_text(encoding="utf-8")
    except (OSError, TypeError):
        return None


def req_texts(text) -> dict:
    """{REQ-n: its text in goal.md} (REQ-15)."""
    return {m[1]: m[0] for m in _REQ_TEXT.finditer(text)}


def security(root, h) -> bool:
    """The batch covers a goal.md REQ marked [防误操作] or [防对抗]; an unreadable goal.md counts as one (REQ-7)."""
    text = goal(root, h)
    return text is None or not set(h["reqs"]).isdisjoint(_GUARDED.findall(text))


def route(cfg, h=None, root=None) -> tuple[str, str]:
    """Reviewer model and effort; exclusions are never bypassed (DESIGN §1.5). Given the batch header (and the
    project root, for its goal.md) the effort goes by tier (REQ-7), first one configured: effort_security for a
    batch covering a [防误操作] / [防对抗] REQ, effort_s / _m / _l by tiers.difficulty, effort, then xhigh."""
    # ponytail: Claude only; cross-vendor routing comes with vendors/ (M1-4) and Codex (M2-7)
    model = cfg.get("routes.reviewer.model", "claude-opus-5-5")
    keys = []
    if h is not None:
        keys += ["routes.reviewer.effort_security"] if security(root, h) else []
        keys.append(f"routes.reviewer.effort_{h['tiers']['difficulty'].lower()}")
    effort = next((cfg[k] for k in (*keys, "routes.reviewer.effort") if cfg.get(k) is not None), "xhigh")
    if h is not None and effort not in schemas.EFFORTS:  # r1 #3: tick.probes asks for the model only
        raise FlowError(f"reviewer effort {effort!r} (routes.reviewer.*) is not one of {list(schemas.EFFORTS)}")
    if "claude" in cfg.get("exclude.providers", defaults.TABLE["exclude.providers"]):
        raise FlowError("reviewer vendor claude is excluded")
    for pat in cfg.get("exclude.models", defaults.TABLE["exclude.models"]):
        if any(fnmatchcase(name.lower(), pat.lower()) for name in (model, f"claude:{model}")):
            raise FlowError(f"reviewer model {model} is excluded by {pat!r}")
    return model, effort


def full_behind(rs) -> bool:
    """The last of the receipts `rs` reaches a full review (REQ-6): an incremental one goes on to the latest earlier
    receipt at its delta_from heads (a rebound copy keeps its original's scope and delta_from); delta does not."""
    i = len(rs) - 1
    while i >= 0 and rs[i]["scope"] == "incremental":
        i = next((j for j in range(i - 1, -1, -1) if rs[j]["heads"] == rs[i].get("delta_from")), -1)
    return i >= 0 and rs[i]["scope"] == "full"


def _prior(root, batch_id, pairs, hd, evs) -> dict | None:
    """REQ-6: the latest receipt when this round can be an incremental review on from its heads, else None (full).
    It holds when those heads are ancestors of `hd` in every repo, this round's review_requested has the merge-bases
    recorded with them (review_requested, or batch_updated for a rebound copy, which chains by its own heads), and
    its chain reaches a full review, as the gate checks."""
    rs = load_receipts(root, batch_id)
    if not rs or not full_behind(rs):
        return None
    ph = rs[-1]["heads"]
    if ph.keys() != hd.keys() or not all(is_ancestor(wt, ph[r.id], hd[r.id]) for r, wt in pairs):
        return None

    def recorded(at, types):
        return next((e.get("bases") for e in reversed(evs)
                     if e["type"] in types and e.get("batch") == batch_id and e.get("heads") == at), None)

    now = recorded(hd, ("review_requested",))
    return rs[-1] if now and now == recorded(ph, ("review_requested", "batch_updated")) else None


def _log_since(evs, batch_id, session) -> int:
    """Size of the batch log when the review `session` started: its batch_log_appended records up to then."""
    size = 0
    for e in evs:
        if e.get("batch") != batch_id:
            continue
        if e["type"] == "batch_log_appended":
            size = e["size"]
        elif e["type"] == "review_started" and e.get("reviewer_session") == session:
            return size
    return 0


# m2d.5 r2: paths in the reviewer's diffs as they are (not quoted and octal-escaped), so a location copied from them
# matches the changed files screen_rules reads with -z
_RAW_PATHS = ("-c", "core.quotePath=false")


def _materials(root, h, pairs, hd, cfg, mat, n, scope, prev=None, evs=(), at=None):
    """The full diff per repo, or for an incremental round (`prev` = the receipt it goes on from) the diff since
    prev's heads, the batch's changed files and the log since prev's review started (REQ-6). `at` (REQ-14) = a full
    receipt whose materials are rebuilt: the receipt before it, the log up to its start, the merge-bases recorded
    with its heads."""
    mat.mkdir(parents=True)
    checkout(pairs, hd, mat)  # §20 I50: mat/<repo>, removed by harvest()
    bdir = state_dir(root) / "batches"
    lines = [f"# 审查：批次 {h['id']}，第 {n} 轮（{scope}）", "",
             f"owns_paths: {json.dumps(h['owns_paths'], ensure_ascii=False)}",
             f"验收命令: {json.dumps(h['accept_commands'], ensure_ascii=False)}", "", "## 材料（只读）"]
    rs = [x for x in receipts(root, h["id"]) if not at or x[0] < at["round"]]
    log = bdir / f"{h['id']}.log.md"
    for name, src in (("goal.md", state_dir(root) / "plans" / h["plan_id"] / "goal.md"),  # the frozen goal
                      ("batch.md", bdir / f"{h['id']}.md"), ("handoff.md", bdir / f"{h['id']}.handoff.md"),
                      ("log.md", None if prev or at else log), ("previous-receipt.json", rs[-1][1] if rs else None)):
        if src and src.is_file():
            shutil.copyfile(src, mat / name)
            lines.append(f"- ./{name}")
    found = req_texts(goal(root, h) or "")  # REQ-15: what a must_fix's quote is checked against
    if texts := [found[r].rstrip() for r in h["reqs"] if r in found]:
        (mat / "reqs.md").write_text("\n\n".join(texts) + "\n", encoding="utf-8")
        lines.append("- ./reqs.md：本批覆盖的各 REQ 在 goal.md 里的原文；must_fix 以 REQ 为依据时，quote 从这里逐字摘")
    if gone := withdrawn(evs, h["id"], at and at["reviewer_session"]):
        said = {i.get("fingerprint"): i for r in load_receipts(root, h["id"]) for i in r.get("issues", [])}
        (mat / "withdrawn.md").write_text("".join(
            f"- {fp}（{said.get(fp, {}).get('location')}：{said.get(fp, {}).get('summary')}）撤回理由：{why}\n"
            for fp, why in gone.items()), encoding="utf-8")
        lines.append("- ./withdrawn.md：总控或用户已撤回（裁定不成立）的问题；同一问题不要再报为 must_fix")
    if prev and log.is_file():
        (mat / "log-since.md").write_bytes(log.read_bytes()[_log_since(evs, h["id"], prev["reviewer_session"]):])
        lines.append("- ./log-since.md：上一轮审查开始以来追加的批次日志（席位的答复与 D/F 记录）")
    if at and log.is_file():
        (mat / "log.md").write_bytes(log.read_bytes()[:_log_since(evs, h["id"], at["reviewer_session"])])
        lines.append("- ./log.md")
    recorded = at and next((e.get("bases") for e in reversed(evs) if e["type"] == "review_requested"
                            and e.get("batch") == h["id"] and e.get("heads") == hd), None) or {}
    for r, wt in pairs:
        base = recorded.get(r.id) or git(wt, "merge-base", target_ref(r, wt, cfg), hd[r.id])
        if prev:
            old = prev["heads"][r.id]
            diff = git(wt, *_RAW_PATHS, "diff", "--no-ext-diff", "--no-color", "--no-textconv", old, hd[r.id])
            (mat / f"{r.id}.delta.diff").write_text(diff + "\n", encoding="utf-8")
            files = git(wt, *_RAW_PATHS, "diff", "--no-ext-diff", "--no-renames", "--name-only", base, hd[r.id])
            (mat / f"{r.id}.files.txt").write_text(files + "\n", encoding="utf-8")
            lines += [f"- ./{r.id}.delta.diff：仓库 {r.id} 上一轮已审 head {old} → 本轮 head {hd[r.id]} 的 diff，"
                      f"本轮 head 的干净检出在 ./{r.id}/",
                      f"- ./{r.id}.files.txt：本批在仓库 {r.id} 改动的全部文件（base {base} → head {hd[r.id]}）"]
            continue
        diff = git(wt, *_RAW_PATHS, "diff", "--no-ext-diff", "--no-color", "--no-textconv", base, hd[r.id])
        (mat / f"{r.id}.diff").write_text(diff + "\n", encoding="utf-8")
        lines.append(f"- ./{r.id}.diff：仓库 {r.id} 的完整 diff（base {base} → head {hd[r.id]}），已审 head 的干净检出在 ./{r.id}/")
    card = _ROLE_CARD.read_text(encoding="utf-8") if _ROLE_CARD.is_file() else "你是零上下文、只读的审查者。"
    top = (f"本轮是增量重审，前一轮 heads 为 {json.dumps(prev['heads'])}；按角色卡「增量轮」一节审。\n\n"
           if prev else "")
    (mat / "prompt.md").write_text(top + card + "\n\n" + "\n".join(lines) + "\n\n" + _SCREENING + "\n",
                                   encoding="utf-8")


# REQ-15, REQ-16 as assemble() applies them; here rather than in the role card, which is capped at 1200 characters
_SCREENING = (
    "## 程序对 must_fix 的核对\n\n"
    "以下 must_fix 降为 note，仍留在回执里（记原 severity 与原因）：没写依据或依据核不上（REQ 不在本批、quote 去掉空白后"
    "不是该 REQ 原文的一段、授权表类别不存在、没写被破坏行为的位置）；[防对抗] REQ 的新形式（覆盖范围外，留给后续批次）；"
    "总控或用户已撤回的问题（./withdrawn.md）。第 3 轮起，新报的 must_fix 若不在上一份回执以来改动的文件里，降为 "
    "should_fix；配置了每轮新 must_fix 上限时，超出的部分按你给出的顺序降为 should_fix，所以最要紧的排在前面。verdict "
    "最后由程序按降级后是否仍有 must_fix 定。")


def start(root, batch_id, cfg, *, started_by="supervisor") -> str:
    """review_ready -> in_review; launches the reviewer detached and returns its session id."""
    h = load_batch(root, batch_id)
    if h.get("state") != "review_ready":  # checked again under the lock below
        raise FlowError(f"{batch_id} is {h.get('state', 'planned')}, not review_ready")
    model, effort = route(cfg, h, root)
    cap = cfg.get("review.new_must_fix_max")  # REQ-16; fixed for the round here, as the effort is
    if cap is not None and not re.fullmatch(r"[0-9]+", str(cap)):
        raise FlowError(f"review.new_must_fix_max {cap!r} is not a whole number >= 0")
    pairs = batch_repos(root, h, cfg)
    hd = heads(pairs)
    evs = all_events(root)
    n = next_round(evs, batch_id)
    prev = _prior(root, batch_id, pairs, hd, evs)
    # r1 #2: a review still running (asked again while in_review) lands a receipt after this round's `prev`; the
    # increment goes on from that receipt, so it waits until the supervisor has harvested the running one
    done = {e["dedupe_id"] for e in evs if e["phase"] == "result"}
    if prev and (busy := [e["reviewer_session"] for e in evs if e["type"] == "review_started" and
                          e["phase"] == "intent" and e.get("batch") == batch_id and e["dedupe_id"] not in done]):
        raise FlowError(f"{batch_id}: review {busy[-1]} is still running; the incremental round goes on from its "
                        "receipt and starts once it is harvested")
    scope = "incremental" if prev else "full"
    session = str(uuid.uuid4())
    mat = state_dir(root) / "reviews" / session
    meta = {"batch": batch_id, "round": n, "scope": scope, "heads": hd, "reviewer_session": session,
            "model": model, "effort": effort, **({"delta_from": prev["heads"]} if prev else {}),
            "difficulty": h["tiers"]["difficulty"], "security": security(root, h),
            **({"new_must_fix_max": int(cap)} if cap is not None else {}), "quota_start": reading(root, cfg)}

    def begin(_h):
        """Under the state lock: one of two concurrent starts wins, and only on the heads the seat asked to have
        reviewed (and pushed); a commit after `foremind review` needs a new request."""
        req = next((e for e in reversed(all_events(root))
                    if e["type"] == "review_requested" and e.get("batch") == batch_id), None)
        if not req or req.get("heads") != hd:
            raise FlowError(f"{batch_id}: heads {hd} are not the ones review was requested for; "
                            "run `foremind review` again")
        events(root).append("review_started", phase="intent", dedupe_id=f"review:{batch_id}:{session}",
                            started_by=started_by, **meta)

    try:
        _materials(root, h, pairs, hd, cfg, mat, n, scope, prev, evs)
        set_state(root, batch_id, "in_review", expect=("review_ready",), before=begin)
    except BaseException:
        drop_checkouts(pairs, mat)
        raise
    try:
        meta["job_id"] = _launch(root, batch_id, cfg, model, effort, session, pairs)
    except Exception as e:
        drop_checkouts(pairs, mat)
        _failed(root, batch_id, session, hd, cfg, f"reviewer did not start: {e}", infra=True)
        raise FlowError(f"{batch_id}: reviewer did not start: {e}") from None
    atomic_write(mat / "meta.json", json.dumps(meta))
    return session


def _launch(root, batch_id, cfg, model, effort, session, pairs) -> str:
    """Starts the reviewer on reviews/<session> (materials written) and returns its job id. §20 I47: --restricted
    drops the command tools and ignores user/project settings (defaultMode, allow rules); --tools is the whitelist.
    REQ-13: the output schema, and a dollar cap per review once review.max_budget_usd is set; A10: the per-machine
    system prompt sections move out once oneshot.exclude_dynamic_prompt is on."""
    mat = state_dir(root) / "reviews" / session
    budget = cfg.get("review.max_budget_usd")
    argv = ["claude", "-p", "--restricted", "--tools", READ_ONLY_TOOLS, "--permission-mode", "plan",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--model", model, "--effort", effort, "--output-format", "json", "--session-id", session,
            "--json-schema", json.dumps(REVIEW_SCHEMA), *(["--max-budget-usd", str(budget)] if budget is not None else []),
            *(["--exclude-dynamic-system-prompt-sections"] if cfg.get("oneshot.exclude_dynamic_prompt") is True else []),
            *(x for r, _ in pairs for x in ("--add-dir", str(mat / r.id))),
            "--", "Read ./prompt.md and follow it. Output only the JSON object it asks for."]
    env = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
           "FOREMIND_SESSION": session, "FOREMIND_ROLE": "reviewer", "FOREMIND_BATCH": batch_id}
    return job.start(state_dir(root) / "jobs", argv, cwd=mat, env=env,
                     timeout_s=int(cfg.get("oneshot.timeout_min", defaults.TABLE["oneshot.timeout_min"])) * 60)


def fingerprint(location: str, summary: str) -> str:
    loc = re.sub(r":\d+(?:-\d+)?$", "", location.strip())  # line numbers move between rounds
    words = " ".join(re.findall(r"\w+", summary.lower()))
    return sha256_bytes(f"{loc}\n{words}".encode())[:16]


def parse_output(raw: str):
    """The model's JSON object, from `claude -p --output-format json` stdout (or bare text)."""
    try:
        obj = json.loads(raw)
    except ValueError:
        obj = None
    if isinstance(obj, dict) and "verdict" in obj:
        return obj
    if isinstance(obj, dict) and isinstance(obj.get("structured_output"), dict) and "verdict" in obj["structured_output"]:
        return obj["structured_output"]  # --json-schema
    if isinstance(obj, dict) and isinstance(obj.get("result"), str):
        if obj.get("is_error"):
            raise ValueError(f"reviewer reported an error: {obj['result'][:200]}")
        raw = obj["result"]
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", raw):  # braces in the prose around it must not break parsing
        try:
            found = dec.raw_decode(raw, m.start())[0]
        except ValueError:
            continue
        if isinstance(found, dict) and "verdict" in found:
            return found
    raise ValueError("no JSON object with a verdict in reviewer output")


COST_KEYS = ("cost_usd", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "num_turns")


def cost(raw: str, capped=False) -> tuple[dict, bool]:
    """REQ-13: (COST_KEYS from `claude -p --output-format json`, null where not reported; whether the run stopped at
    --max-budget-usd). The error text only counts when a cap was set (`capped`): an API error may mention a budget."""
    try:
        top = json.loads(raw)
    except ValueError:
        top = None
    top = top if isinstance(top, dict) else {}
    use = top.get("usage") if isinstance(top.get("usage"), dict) else {}
    got = dict(zip(COST_KEYS, (top.get("total_cost_usd"), use.get("input_tokens"),
                               use.get("cache_creation_input_tokens"), use.get("cache_read_input_tokens"),
                               use.get("output_tokens"), top.get("num_turns"))))
    return got, top.get("subtype") == "error_max_budget_usd" or (capped and top.get("is_error") is True and "budget"
                                                                 in json.dumps([top.get("errors"), top.get("result")]).lower())


def reading(root, cfg) -> dict:
    """REQ-2: {five_hour_pct, seven_day_pct, ts} of the account's newest status line reading, recorded as is, never
    acted on; a window null when there is no reading, it is older than quota.stale_min or the window has reset since."""
    tel, now = quota.read_account_telemetry(root), time.time()
    fresh = tel is not None and now - tel["ts"] <= quota.lines(cfg, "oneshot")["stale"]

    def pct(k):
        w = tel.get(k) if fresh else None
        return w["pct"] if w and w["resets_at"] > now else None
    return {"five_hour_pct": pct("five_hour"), "seven_day_pct": pct("seven_day"), "ts": tel and tel["ts"]}


def _squash(s) -> str:
    return re.sub(r"\s+", "", s) if isinstance(s, str) else ""


def _unbacked(it, fp, rules) -> str | None:
    """REQ-15: why the must_fix `it` (as the reviewer gave it) is lowered to a note, None when it stands. Without
    goal.md (rules texts None) nothing is lowered for what only its text could tell, as security() is strict then."""
    basis, texts = it.get("basis"), rules["texts"]
    if basis == "req":
        req, quote = it.get("req"), _squash(it.get("quote"))
        if req not in rules["reqs"] or not quote or texts is not None and quote not in _squash(texts.get(req)):
            return "no_basis"
        if req in rules["adversarial"] and it.get("form") == "new":
            return "outside_coverage"
    elif basis == "authz":
        if type(it.get("category")) is not int or it["category"] not in schemas.CATEGORIES:
            return "no_basis"
    elif basis != "regression" or not _squash(it.get("broken")):
        return "no_basis"
    return "withdrawn" if fp in rules["withdrawn"] else None


_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"', re.S)
_ESC = {b"a": 7, b"b": 8, b"t": 9, b"n": 10, b"v": 11, b"f": 12, b"r": 13, b'"': 34, b"\\": 92}


def _unquote(body: str) -> str | None:
    """git's C-style quoted path (between the quotes) as it is: escapes and three-digit octal bytes, UTF-8; None when
    it does not decode."""
    try:
        return re.sub(rb"\\([0-7]{3}|.)", lambda m: bytes([int(m[1], 8) if len(m[1]) == 3 else _ESC[m[1]]]),
                      body.encode(), flags=re.S).decode()
    except (KeyError, ValueError):  # an unknown escape, an octal over 377, not UTF-8
        return None


def locate(location, repos) -> tuple[str | None, str, int | None]:
    """REQ-7: a review location as (repo, path, line). The repo prefix counts only when it is one of `repos`; the path
    is git's quoted form unquoted (`main:"a\\"b.py":12`; one that does not unquote stays as written), else the rest up
    to its first `:<digit>` (`x:a.py:12` with no repo x → path `x:a.py`), else up to its first `:`; a leading ./
    dropped; the line is the number right after the path (`:12-20`, `:12 (fn)` → 12), else None."""
    loc = str(location).strip()
    repo, sep, rest = loc.partition(":")
    if not sep or repo not in repos:
        repo, rest = None, loc
    if (m := _QUOTED.match(rest)) and (path := _unquote(m[1])) is not None:
        tail = rest[m.end():]
    elif m := re.search(r":\d", rest):
        path, tail = rest[:m.start()], rest[m.start():]
    else:
        path, tail = rest.partition(":")[0], ""
    line = re.match(r":(\d+)", tail)
    return repo, path.removeprefix("./"), int(line[1]) if line else None


def _in_delta(location, delta) -> bool:
    """`location` names a changed file (locate); without a known repo prefix it is tried in every repo. An unquoted
    path may itself hold a `:`: the location then matches a changed file followed by the end or `:` (m2d.5 r1)."""
    repo, path, _ = locate(location, delta)
    loc = str(location).strip()
    raw = (loc.partition(":")[2] if repo else loc).removeprefix("./")
    return any(path == f or raw == f or raw.startswith(f + ":")
               for files in ([delta[repo]] if repo else delta.values()) for f in files)


# ponytail: a line window pairs two different defects within 30 lines of one file and misses one defect cited at
# different places; the way out is m2f's model judging "the same defect", run in --ab only and costed to that comparison
PAIR_LINES = 30


def matched(issues, base_issues, repos) -> int:
    """REQ-1: must_fix pairs between a comparison's issues and its base receipt's, each side the reviewer's own must_fix
    (`was` for those the program lowered). Paired by locate(): the same path (and repo when both name one), lines at
    most PAIR_LINES apart when both have one; one to one, nearest lines first (no line last), then the base's order,
    then the comparison's."""
    def must(xs):
        return [locate(i.get("location"), repos) for i in xs
                if isinstance(i, dict) and i.get("was", i.get("severity")) == "must_fix"]
    mine, theirs = must(issues), must(base_issues)
    cands = []
    for b, (rb, pb, lb) in enumerate(theirs):
        for a, (ra, pa, la) in enumerate(mine):
            if pa == pb and (ra is None or rb is None or ra == rb):
                d = None if la is None or lb is None else abs(la - lb)
                if d is None or d <= PAIR_LINES:
                    cands.append((d is None, d or 0, b, a))
    used_b, used_a = set(), set()
    for _, _, b, a in sorted(cands):
        if b not in used_b and a not in used_a:
            used_b.add(b)
            used_a.add(a)
    return len(used_b)


def assemble(out, meta, previous, rules=None) -> dict:
    """Receipt from the reviewer output: only verdict and issues[].{severity, location, summary, disputed, basis,
    req} are taken; a verdict that contradicts the reviewer's own severities is invalid output. `previous` = the
    receipts before this round. `rules` (harvest; ab() compares raw output and passes none, see screen_rules):
    must_fix lowered with was and filtered (REQ-15 to note; REQ-16 new ones outside `delta` or over `cap` to
    should_fix), the verdict set from the must_fix left, resolved / unresolved against the previous receipt."""
    if not isinstance(out, dict) or not isinstance(out.get("issues"), list):
        raise ValueError("reviewer output needs an object with an `issues` list")
    seen = {i.get("fingerprint") for r in previous for i in r.get("issues", [])}
    issues = []
    for k, it in enumerate(out["issues"], 1):
        if not isinstance(it, dict):
            raise ValueError(f"issues[{k - 1}] is not an object")
        fp = fingerprint(str(it.get("location")), str(it.get("summary")))
        status = "disputed" if it.get("disputed") is True else "repeat" if fp in seen else "new"
        issues.append({"id": str(k), "fingerprint": fp, "severity": it.get("severity"), "status": status,
                       "location": it.get("location"), "summary": it.get("summary"),
                       **({"basis": it["basis"]} if it.get("basis") in schemas.REVIEW_BASES else {}),
                       **({"req": it["req"]} if it.get("basis") == "req" and isinstance(it.get("req"), str)
                          and re.fullmatch(schemas.REQ_ID, it["req"]) else {})})
    verdict = "changes_requested" if any(x["severity"] == "must_fix" for x in issues) else "approved"
    if out.get("verdict") != verdict:
        raise ValueError(f"reviewer verdict {out.get('verdict')!r} contradicts its issues (must be {verdict!r})")
    if rules:
        new = 0
        for it, x in zip(out["issues"], issues):
            if x["severity"] != "must_fix":
                continue
            why, to = _unbacked(it, x["fingerprint"], rules), "note"
            if not why and x["status"] == "new":
                to = "should_fix"
                if rules["delta"] is not None and not _in_delta(x["location"], rules["delta"]):
                    why = "outside_delta"
                elif rules["cap"] is not None and (new := new + 1) > rules["cap"]:
                    why = "over_cap"
            if why:
                x.update(severity=to, was="must_fix", filtered=why)
        verdict = "changes_requested" if any(x["severity"] == "must_fix" for x in issues) else "approved"
    keep = ("batch", "scope", "heads", "reviewer_session", "model", "effort", "round", "delta_from")
    receipt = {**{k: meta[k] for k in keep if k in meta}, "verdict": verdict, "issues": issues}
    if previous:  # REQ-16
        now = {x["fingerprint"] for x in issues}
        before = dict.fromkeys(i.get("fingerprint") for i in previous[-1].get("issues", [])
                               if i.get("severity") in ("must_fix", "should_fix"))
        receipt["resolved"] = [fp for fp in before if fp not in now]
        receipt["unresolved"] = [fp for fp in before if fp in now]
    return receipt


def screen_rules(root, h, meta, cfg, previous) -> dict:
    """What assemble() screens a round's must_fix by: the batch's reqs and their goal.md texts (None when goal.md is
    unreadable), those marked [防对抗], the withdrawn fingerprints (REQ-15); `delta` = {repo: files changed since
    the previous receipt's heads} from the third counted round on when those heads are ancestors of this round's,
    else None = not limited; `cap` = review.new_must_fix_max as start() fixed it (REQ-16). Every harvest of a review
    gets the same rules (a re-harvest after a crash must write the same bytes, m2d.5 r1): withdrawals up to its start,
    and a git error raises (the supervisor harvests again) rather than lifting the limit."""
    text = goal(root, h)
    delta, hd = None, meta["heads"]
    ph = previous[-1]["heads"] if previous else None
    if ph and counted(previous) + 1 >= 3:  # this round, a full or incremental one, counts too
        pairs = batch_repos(root, h, cfg, main=True)
        if ph.keys() == hd.keys() == {r.id for r, _ in pairs} and \
                all(is_ancestor(wt, ph[r.id], hd[r.id]) for r, wt in pairs):
            # -z: paths as they are, not quoted and octal-escaped (non-ASCII, specials)
            delta = {r.id: set(git(wt, "diff", "--no-renames", "--name-only", "-z", ph[r.id], hd[r.id]).split("\0"))
                     - {""} for r, wt in pairs}
    return {"reqs": h["reqs"], "texts": None if text is None else req_texts(text),
            "adversarial": set(_ADVERSARIAL.findall(text or "")),
            "withdrawn": set(withdrawn(all_events(root), h["id"], meta["reviewer_session"])), "delta": delta,
            "cap": meta.get("new_must_fix_max")}


def counted(rs) -> int:
    """Rounds of the receipts `rs` since the latest approved one that count against review.max_rounds: full and
    incremental; delta rounds and rebound receipts (copies, §7.6) do not (N8, REQ-6)."""
    since = max((i for i, r in enumerate(rs) if r["verdict"] == "approved"), default=-1) + 1
    return sum(r["scope"] != "delta" and "rebound_from" not in r for r in rs[since:])


def diagnose(receipts_, max_rounds=defaults.TABLE["review.max_rounds"]) -> list[str]:
    """§7.4 actions once the counted rounds (counted()) reach the cap without approval."""
    if not receipts_ or receipts_[-1]["verdict"] == "approved" or counted(receipts_) < max_rounds:
        return []
    c = Counter(i["status"] for i in receipts_[-1]["issues"])
    out = ["arbitrate"] if c["disputed"] else []  # #15; P0 categories go to the user (decided there)
    if c["repeat"] and c["repeat"] >= c["new"]:
        out.append("escalate_fixer")
    elif c["new"]:
        out.append("freeze_scope_or_split")
    return out


def _current(root, batch_id, session, at, cfg):
    """The batch header when it is in_review for this very session at `at`, which are still the heads; else None
    (a later session or a new head owns the batch now)."""
    h = load_batch(root, batch_id)
    last = _last_started(all_events(root), batch_id)
    if h.get("state") != "in_review" or not last or last.get("reviewer_session") != session or at is None:
        return None
    return h if at == heads(batch_repos(root, h, cfg)) else None


MAX_TIMEOUTS = 3  # consecutive reviewer timeouts on one set of heads; the knob that helps is oneshot.timeout_min


def _failed(root, batch_id, session, at, cfg, error, *, infra=False, timed_out=False, reason=None, usd=None):
    """Record a failed review (`at` = the heads it started on) and hand the batch back: review_ready for another
    try, or failed after review.max_failures consecutive failures of the reviewer on the same heads. `infra`
    failures (never started, runner lost, out of quota, timed out) are the machine's, not the model's: not counted;
    consecutive timeouts are counted on their own against MAX_TIMEOUTS, since a review that outgrows
    oneshot.timeout_min would otherwise rerun on those heads for as long as quota lasts (the update phase tells the
    user once). Both counts start over at the user's `foremind run <batch>`. `reason` budget: stopped at
    review.max_budget_usd, the model's; `usd` = what the run cost (REQ-13)."""
    events(root).append("review_failed", dedupe_id=f"review:{batch_id}:{session}", batch=batch_id,
                        reviewer_session=session, heads=at, error=error, infra=infra, timed_out=timed_out,
                        **({"reason": reason} if reason else {}), **(usd or {}), quota_end=reading(root, cfg))
    if _current(root, batch_id, session, at, cfg) is None:
        return
    n = t = 0
    streak = True  # timeouts count while consecutive (m2b.7 r3)
    for e in reversed(all_events(root)):
        if e["type"] == "run_requested" and batch_id in (e.get("batches") or []):
            break  # the user's retry (`foremind run`) starts over, as tick.failures does
        if e.get("batch") != batch_id or e["type"] not in ("review_failed", "review_receipt"):
            continue
        if e["type"] == "review_receipt" or e.get("heads") != at:
            break
        n += not e.get("infra")
        streak = streak and e.get("timed_out") is True
        t += streak
    cap = int(cfg.get("review.max_failures", defaults.TABLE["review.max_failures"]))
    if n >= cap or t >= MAX_TIMEOUTS:  # a model (or a review) that keeps failing must not burn quota forever
        set_state(root, batch_id, "failed", expect=("in_review",), reason="review_failures" if n >= cap
                  else "review_timeouts", failures=n if n >= cap else t, heads=at)
    else:
        set_state(root, batch_id, "review_ready", expect=("in_review",))


def _since_run(evs, batch_id):
    """The batch's review_receipt and review_failed events since the user's latest `foremind run` of it."""
    for e in reversed(evs):
        if e["type"] == "run_requested" and batch_id in (e.get("batches") or []):
            break
        if e.get("batch") == batch_id and e["type"] in ("review_receipt", "review_failed"):
            yield e


def spent(evs, batch_id) -> float:
    """Dollars the batch's reviews reported (REQ-13) since the user's latest `foremind run` of it."""
    return round(sum(e["cost_usd"] for e in _since_run(evs, batch_id) if _num(e.get("cost_usd"))), 6)


# token prices relative to input: "input-equivalent tokens", the one place (REQ-2; signals and the report use it)
PRICE = {"input_tokens": 1, "cache_write_tokens": 1.25, "cache_read_tokens": 0.1, "output_tokens": 5}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def tokens(e) -> float | None:
    """One review's usage (COST_KEYS' four token counts) as input-equivalent tokens; None when one is not a number."""
    return sum(e[k] * w for k, w in PRICE.items()) if all(_num(e.get(k)) for k in PRICE) else None


def spent_tokens(evs, batch_id) -> tuple[float, int]:
    """REQ-2: (input-equivalent tokens, reviews left out for a missing count) as spent() counts; rebound copies are no
    reviews of their own."""
    ts = [tokens(e) for e in _since_run(evs, batch_id) if "rebound_from" not in e]
    return round(sum(t for t in ts if t is not None), 6), ts.count(None)


def _settle(root, batch_id, receipt, cfg) -> dict:
    """What follows a written receipt; idempotent, so a harvest cut short by a crash is finished by the next one.
    A5 (REQ-13, REQ-2): changes requested once the batch's reviews cost review.cost_cap_usd_<difficulty> or
    review.cost_cap_tokens_<difficulty> fail it instead (reason review_cost_cap; dollars when both are reached,
    `unit` says which), reported as not converging; the failed notice goes by that reason (phases/report)."""
    h = None
    if receipt["verdict"] == "changes_requested":
        h = _current(root, batch_id, receipt["reviewer_session"], receipt["heads"], cfg)
    d = h["tiers"]["difficulty"].lower() if h else None
    cap = cfg.get(f"review.cost_cap_usd_{d}") if h else None
    tcap = cfg.get(f"review.cost_cap_tokens_{d}") if h else None
    evs = all_events(root) if cap is not None or tcap is not None else []
    usd = spent(evs, batch_id)
    tok, uncounted = spent_tokens(evs, batch_id)
    over = None
    if cap is not None and usd >= float(cap):
        over = {"unit": "usd", "cost_usd": usd, "cap": float(cap)}
    elif tcap is not None and float(tcap) > 0 and tok >= float(tcap):
        over = {"unit": "tokens", "tokens": tok, "cap": float(tcap), "cost_usd": usd, "uncounted": uncounted}
    actions = diagnose(load_receipts(root, batch_id),
                       int(cfg.get("review.max_rounds", defaults.TABLE["review.max_rounds"])))
    if actions or over:  # ahead of the move: a crash in between is finished by the next harvest (deduped)
        events(root).append("review_not_converging", dedupe_id=f"nonconv:{batch_id}:r{receipt['round']}",
                            batch=batch_id, round=receipt["round"], actions=actions,
                            **({"reason": "cost_cap", **over} if over else {}))
    if over:
        set_state(root, batch_id, "failed", expect=("in_review",), reason="review_cost_cap", unit=over["unit"],
                  **({"tokens": tok} if over["unit"] == "tokens" else {}), cost_usd=usd, heads=receipt["heads"])
    elif h:
        set_state(root, batch_id, "changes_requested", expect=("in_review",))
    return receipt


_QUOTA = re.compile(r"rate[ _-]?limit|usage limit|session limit|quota|hit your \w+ limit", re.I)


def _out_of_quota(root, meta) -> bool:
    """The reviewer's account ran out (e.g. Claude's "You've hit your session limit"): the machine's failure, not
    the model's (§10.5: interrupted review, discard and rerun)."""
    if meta is None:
        return False
    d = state_dir(root) / "jobs" / meta["job_id"]
    return any(_QUOTA.search(p.read_text(encoding="utf-8", errors="replace"))
               for p in (d / "stdout.log", d / "stderr.log") if p.is_file())


def harvest(root, batch_id, session, cfg=None) -> dict | None:
    """None while the reviewer runs; the receipt when done. A failed or invalid run raises FlowError after
    recording it and re-queueing the batch (DESIGN §10.5: an interrupted review is discarded and rerun)."""
    cfg = cfg or {}
    mat = state_dir(root) / "reviews" / session
    dedupe = f"review:{batch_id}:{session}"
    try:
        meta = json.loads((mat / "meta.json").read_text())
    except FileNotFoundError:
        meta = None
    if meta is not None:
        path = state_dir(root) / "batches" / f"{batch_id}.review.r{meta['round']}.json"
        if path.exists():  # harvested before
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt.get("reviewer_session") != session:
                raise FlowError(f"{path.name} belongs to another review")
            return _settle(root, batch_id, receipt, cfg)
        try:
            st = job.status(state_dir(root) / "jobs", meta["job_id"])
        except OSError as e:
            st = {"state": "lost", "error": str(e)}
        if st["state"] in ("starting", "running"):
            return None
    try:
        drop_checkouts(batch_repos(root, load_batch(root, batch_id), cfg), mat)
    except FlowError:
        pass  # ponytail: the batch's worktrees are gone; git's own gc prunes the checkouts' registrations
    # OSError (never started, runner or its files lost, timed out) or no verdict for want of quota = the machine's
    # failure: not counted against review.max_failures
    out, usd, budget = None, {}, False
    try:
        if meta is None:
            raise OSError("reviewer was never started")
        if st["state"] != "done":
            raise OSError(f"reviewer job {st}")
        if st.get("timed_out"):  # cut off by oneshot.timeout_min: the machine's, like a lost runner (§10.5)
            raise OSError(f"reviewer job timed out: {st}")
        raw = (state_dir(root) / "jobs" / meta["job_id"] / "stdout.log").read_text(encoding="utf-8", errors="replace")
        usd, budget = cost(raw, cfg.get("review.max_budget_usd") is not None)
        atomic_write(mat / "meta.json", json.dumps({**meta, **usd}))
        if budget:
            raise ValueError(f"reviewer stopped at review.max_budget_usd {cfg.get('review.max_budget_usd')}")
        if st["exit_code"] != 0:
            raise ValueError(f"reviewer job {st}")
        out = parse_output(raw)
        prev = [r for r in load_receipts(root, batch_id) if r.get("round", 0) < meta["round"]]
        receipt = assemble(out, meta, prev, screen_rules(root, load_batch(root, batch_id), meta, cfg, prev))
        if errs := schemas.validate("review_receipt", receipt):
            raise ValueError("receipt fails schema: " + "; ".join(errs[:3]))
    except (ValueError, OSError) as err:
        # no meta.json: start() was cut short after in_review; its review_started event still has the heads
        began = meta or next((e for e in reversed(all_events(root)) if e["type"] == "review_started"
                              and e.get("batch") == batch_id and e.get("reviewer_session") == session), None)
        _failed(root, batch_id, session, began and began.get("heads"), cfg, str(err),
                infra=not budget and (isinstance(err, OSError) or (out is None and _out_of_quota(root, meta))),
                timed_out=meta is not None and st.get("timed_out") is True, reason="budget" if budget else None,
                usd=usd)
        raise FlowError(f"{batch_id} review {session}: {err}") from None
    data = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    sha = sha256_bytes(data.encode())
    # event first: a crash before the file leaves an event that a re-harvest (same bytes, deduped) completes
    ev = events(root).append("review_receipt", dedupe_id=dedupe, batch=batch_id, round=meta["round"],
                             path=path.name, sha256=sha, verdict=receipt["verdict"], reviewer_session=session, **usd,
                             quota_end=reading(root, cfg))
    if ev["type"] != "review_receipt" or ev.get("sha256") != sha:
        raise FlowError(f"{batch_id} review {session} was already recorded as {ev['type']} with other content")
    atomic_write(path, data)
    return _settle(root, batch_id, receipt, cfg)


# --- controller: effort comparison ---------------------------------------------------

def ab(root, batch_id, cfg, effort, *, by="user") -> dict:
    """REQ-14: the batch's latest full review once more at `effort`, waited for here, with that review's model on the
    same materials as that round (see _materials `at`); outside the rounds: no review_started (next_round counts
    those), no receipt, the gate never sees it. Records ab_review: both verdicts and must-fix counts, `overlap` =
    must-fix fingerprints in both, `matched` = must-fix paired by location (REQ-1), the cost; reviews/<session>/ab.json
    has it with the issues, meta.json ab: true."""
    h = load_batch(root, batch_id)
    base = next((r for r in reversed(load_receipts(root, batch_id)) if r.get("scope") == "full"), None)
    if base is None:
        raise FlowError(f"{batch_id}: no full review receipt to compare with")
    # the same model, or it compares more than the effort; exclusions still apply (route)
    model, _ = route({**cfg, **({"routes.reviewer.model": base["model"]} if base.get("model") else {})}, h, root)
    pairs = batch_repos(root, h, cfg, main=True)
    session = str(uuid.uuid4())
    mat = state_dir(root) / "reviews" / session
    rec = {"batch": batch_id, "heads": base["heads"], "base_round": base["round"],
           "base_session": base["reviewer_session"], "base_effort": base["effort"], "reviewer_session": session,
           "model": model, "effort": effort, "started_by": by, "quota_start": reading(root, cfg)}
    jobs = state_dir(root) / "jobs"
    try:
        _materials(root, h, pairs, base["heads"], cfg, mat, base["round"], "full", evs=all_events(root), at=base)
        meta = {**rec, "ab": True, "job_id": _launch(root, batch_id, cfg, model, effort, session, pairs)}
        atomic_write(mat / "meta.json", json.dumps(meta))
        while (st := job.status(jobs, meta["job_id"]))["state"] in ("starting", "running"):
            time.sleep(0.2)
    finally:
        drop_checkouts(pairs, mat)
    usd, budget = dict.fromkeys(COST_KEYS), False
    rec["quota_end"] = reading(root, cfg)
    try:
        raw = (jobs / meta["job_id"] / "stdout.log").read_text(encoding="utf-8", errors="replace")
        usd, budget = cost(raw, cfg.get("review.max_budget_usd") is not None)
        atomic_write(mat / "meta.json", json.dumps({**meta, **usd}))
        if budget or st["state"] != "done" or st.get("timed_out") or st.get("exit_code") != 0:
            raise ValueError(f"reviewer job {st}{' stopped at review.max_budget_usd' if budget else ''}")
        r = assemble(parse_output(raw), {**rec, "scope": "full", "round": base["round"]}, [])
        if errs := schemas.validate("review_receipt", r):
            raise ValueError("review fails schema: " + "; ".join(errs[:3]))
    except (ValueError, OSError) as e:
        events(root).append("ab_review", **rec, **usd, error=str(e), **({"reason": "budget"} if budget else {}))
        raise FlowError(f"{batch_id} effort comparison {session}: {e}") from None
    # the reviewer's own must_fix on both sides: the comparison is unscreened, the base receipt was (REQ-15, `was`)
    mine, theirs = ({i["fingerprint"] for i in x["issues"] if i.get("was", i["severity"]) == "must_fix"}
                    for x in (r, base))
    out = {**rec, "verdict": r["verdict"], "base_verdict": base["verdict"], "must_fix": len(mine),
           "base_must_fix": len(theirs), "overlap": len(mine & theirs),
           "matched": matched(r["issues"], base["issues"], base["heads"]), **usd}
    atomic_write(mat / "ab.json", json.dumps({**out, "issues": r["issues"]}, ensure_ascii=False, indent=2) + "\n")
    events(root).append("ab_review", **out)
    return out
