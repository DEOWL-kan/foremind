"""`foremind review` and the one-shot reviewer (DESIGN §7.1, §7.4, §1.6, §20 I9 I25 I33).

Seat side: `request()` checks every repo's head is committed, pushes and opens PRs where #23 belongs to the system,
and moves the batch to review_ready. Supervisor side: `start()` checks the requested heads out detached into
reviews/<session>/<repo> (§20 I50) and launches a read-only headless `claude -p` on them through job.start;
`harvest()` deletes the checkouts and turns its stdout into `batches/<id>.review.r<n>.json`. The reviewer only supplies `verdict`
and `issues[].{severity, location, summary, disputed}`; everything else is written by the program.

Also the shared plumbing of review / acceptance / gate: batch header, repos and worktrees, heads, git and gh.
Config keys read here (flat, merged by config.load): delivery.repo.<id>.{target_branch, push_pr, ci}, gate.ci,
routes.reviewer.{model, effort}, exclude.models, exclude.providers, oneshot.timeout_min, review.max_rounds,
review.max_failures.
"""
import json
import re
import shutil
import subprocess
import time
import uuid
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path

from foremind import config, header, job, repos, schemas, seat, state, worktree
from foremind.events import EventLog
from foremind.fsutil import atomic_write, project_lock, sha256_bytes
from foremind.paths import find_project_root, state_dir

_ROLE_CARD = Path(__file__).resolve().parent.parent / "templates" / "roles" / "reviewer.md"
READ_ONLY_TOOLS = "Read,Grep,Glob"


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


def set_state(root, batch_id, to, *, expect=None, before=None, **fields) -> str:
    """Main-path transition under the project lock; `before(header)` runs inside the lock, after the checks and
    ahead of the write (it may refuse by raising). Returns the previous state."""
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
        atomic_write(path, header.render(h, body))
        events(root).append("batch_state", batch=batch_id, prior=cur, state=to, **fields)
    return cur


# --- repos, worktrees, heads -----------------------------------------------------

def batch_repos(root, h, cfg) -> list[tuple]:
    """[(Repo, worktree)] in the header's `repos` order, at the seat's worktree paths (I39)."""
    known = {r.id: r for r in repos.load_repos(root, cfg)}
    if missing := [r for r in h["repos"] if r not in known]:
        raise FlowError(f"{h['id']}: repos not registered: {missing}")
    d = worktree.batch_dir(seat.project_slug(root, cfg), h["id"])
    out = [(known[r], d / r) for r in h["repos"]]
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


def is_ancestor(wt, a, b) -> bool:
    """False as well when `a` is not in the local object store (someone else's commit, never fetched)."""
    return run(["git", "merge-base", "--is-ancestor", a, b], wt, check=False).returncode == 0


def repo_cfg(cfg, repo_id, key, fallback=None, default=None):
    v = cfg.get(f"delivery.repo.{repo_id}.{key}")
    return v if v is not None else cfg.get(fallback, default) if fallback else default


def push_by_system(cfg, repo_id) -> bool:
    return repo_cfg(cfg, repo_id, "push_pr", default="user") == "system"  # #23; unanswered = user (DESIGN §7.5)


def ci_mode(cfg, repo_id) -> str:
    return repo_cfg(cfg, repo_id, "ci", "gate.ci", "none")


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


def _push(root, evs, batch_id, r, wt, rem, br, head):
    """The lease names the expected SHA itself: a background fetch refreshing refs/remotes/ cannot void it. Each
    push is recorded right away, so a crash before review_requested cannot make our own push look foreign."""
    now = remote_head(wt, rem, br)
    if now == head:
        return
    if now is None or is_ancestor(wt, now, head):
        git(wt, "push", "-q", rem, f"{head}:refs/heads/{br}")
    elif now == _last_pushed(evs, batch_id, r.id):  # the seat rewrote what we pushed
        git(wt, "push", "-q", f"--force-with-lease=refs/heads/{br}:{now}", rem, f"{head}:refs/heads/{br}")
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
    bases = {r.id: git(wt, "merge-base", target_ref(r, wt, cfg), hd[r.id]) for r, wt in pairs}  # §20 I48
    prs = _open_prs(root, h, cfg, pairs, hd, evs)
    if cur in approved:  # a new head voids the approval (§1.6): a new round
        set_state(root, batch_id, "changes_requested", expect=(cur,), reason="head changed after approval")
        cur = "changes_requested"
    if cur == "changes_requested":  # the seat is already fixing: record the implied running step
        set_state(root, batch_id, "running", expect=(cur,))
        cur = "running"
    if cur != "review_ready":
        set_state(root, batch_id, "review_ready", expect=(cur,))
    events(root).append("review_requested", batch=batch_id, heads=hd, bases=bases, requested_by=session or "user",
                        prs=prs)
    return {"state": "review_ready", "heads": hd, "prs": prs}


# --- supervisor: start and harvest the reviewer --------------------------------------

def receipts(root, batch_id) -> list[tuple[int, Path]]:
    out = []
    for p in (state_dir(root) / "batches").glob(f"{batch_id}.review.r*.json"):
        if m := re.fullmatch(rf"{re.escape(batch_id)}\.review\.r([1-9][0-9]*)\.json", p.name):
            out.append((int(m[1]), p))
    return sorted(out)


def load_receipts(root, batch_id) -> list[dict]:
    try:
        return [json.loads(p.read_text(encoding="utf-8")) for _, p in receipts(root, batch_id)]
    except ValueError as e:
        raise FlowError(f"{batch_id}: unreadable receipt: {e}") from None


def route(cfg) -> tuple[str, str]:
    """Reviewer model and effort; exclusions are never bypassed (DESIGN §1.5)."""
    # ponytail: Claude only; cross-vendor routing comes with vendors/ (M1-4) and Codex (M2-7)
    model = cfg.get("routes.reviewer.model", "claude-opus-5-5")
    effort = cfg.get("routes.reviewer.effort", "xhigh")
    if "claude" in cfg.get("exclude.providers", []):
        raise FlowError("reviewer vendor claude is excluded")
    for pat in cfg.get("exclude.models", []):
        if any(fnmatchcase(name.lower(), pat.lower()) for name in (model, f"claude:{model}")):
            raise FlowError(f"reviewer model {model} is excluded by {pat!r}")
    return model, effort


def _materials(root, h, pairs, hd, cfg, mat, n, scope):
    mat.mkdir(parents=True)
    checkout(pairs, hd, mat)  # §20 I50: mat/<repo>, removed by harvest()
    bdir = state_dir(root) / "batches"
    lines = [f"# 审查：批次 {h['id']}，第 {n} 轮（{scope}）", "",
             f"owns_paths: {json.dumps(h['owns_paths'], ensure_ascii=False)}",
             f"验收命令: {json.dumps(h['accept_commands'], ensure_ascii=False)}", "", "## 材料（只读）"]
    prev = receipts(root, h["id"])
    for name, src in (("batch.md", bdir / f"{h['id']}.md"), ("handoff.md", bdir / f"{h['id']}.handoff.md"),
                      ("log.md", bdir / f"{h['id']}.log.md"), ("previous-receipt.json", prev[-1][1] if prev else None)):
        if src and src.is_file():
            shutil.copyfile(src, mat / name)
            lines.append(f"- ./{name}")
    for r, wt in pairs:
        base = git(wt, "merge-base", target_ref(r, wt, cfg), hd[r.id])
        diff = git(wt, "diff", "--no-ext-diff", "--no-color", "--no-textconv", base, hd[r.id])
        (mat / f"{r.id}.diff").write_text(diff + "\n", encoding="utf-8")
        lines.append(f"- ./{r.id}.diff：仓库 {r.id} 的完整 diff（base {base} → head {hd[r.id]}），已审 head 的干净检出在 ./{r.id}/")
    card = _ROLE_CARD.read_text(encoding="utf-8") if _ROLE_CARD.is_file() else "你是零上下文、只读的审查者。"
    (mat / "prompt.md").write_text(card + "\n\n" + "\n".join(lines) + "\n", encoding="utf-8")


def start(root, batch_id, cfg, *, scope="full", started_by="supervisor") -> str:
    """review_ready -> in_review; launches the reviewer detached and returns its session id."""
    h = load_batch(root, batch_id)
    if h.get("state") != "review_ready":  # checked again under the lock below
        raise FlowError(f"{batch_id} is {h.get('state', 'planned')}, not review_ready")
    model, effort = route(cfg)
    pairs = batch_repos(root, h, cfg)
    hd = heads(pairs)
    evs = all_events(root)
    n = 1 + sum(e["type"] == "review_started" and e.get("batch") == batch_id for e in evs)  # unique even if one fails
    session = str(uuid.uuid4())
    mat = state_dir(root) / "reviews" / session
    meta = {"batch": batch_id, "round": n, "scope": scope, "heads": hd, "reviewer_session": session,
            "model": model, "effort": effort}

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
        _materials(root, h, pairs, hd, cfg, mat, n, scope)
        set_state(root, batch_id, "in_review", expect=("review_ready",), before=begin)
    except BaseException:
        drop_checkouts(pairs, mat)
        raise
    # §20 I47: --restricted drops the command tools and ignores user/project settings (defaultMode, allow rules);
    # --tools is the whitelist
    argv = ["claude", "-p", "--restricted", "--tools", READ_ONLY_TOOLS, "--permission-mode", "plan",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--model", model, "--effort", effort, "--output-format", "json", "--session-id", session,
            *(x for r, _ in pairs for x in ("--add-dir", str(mat / r.id))),
            "--", "Read ./prompt.md and follow it. Output only the JSON object it asks for."]
    env = {"CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD": "1",
           "FOREMIND_SESSION": session, "FOREMIND_ROLE": "reviewer", "FOREMIND_BATCH": batch_id}
    try:
        meta["job_id"] = job.start(state_dir(root) / "jobs", argv, cwd=mat, env=env,
                                   timeout_s=int(cfg.get("oneshot.timeout_min", 30)) * 60)
    except Exception as e:
        drop_checkouts(pairs, mat)
        _failed(root, batch_id, session, hd, cfg, f"reviewer did not start: {e}", infra=True)
        raise FlowError(f"{batch_id}: reviewer did not start: {e}") from None
    atomic_write(mat / "meta.json", json.dumps(meta))
    return session


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


def assemble(out, meta, previous) -> dict:
    """Receipt from the reviewer output: only verdict and issues[].{severity, location, summary, disputed} are taken."""
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
                       "location": it.get("location"), "summary": it.get("summary")})
    keep = ("batch", "scope", "heads", "reviewer_session", "model", "effort", "round")
    return {**{k: meta[k] for k in keep}, "verdict": out.get("verdict"), "issues": issues}


def diagnose(receipts_, max_rounds=3) -> list[str]:
    """§7.4 actions once the full-scope rounds reach the cap without approval; delta rounds do not count."""
    if not receipts_ or receipts_[-1]["verdict"] == "approved":
        return []
    if sum(r["scope"] == "full" for r in receipts_) < max_rounds:
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


def _failed(root, batch_id, session, at, cfg, error, *, infra=False):
    """Record a failed review (`at` = the heads it started on) and hand the batch back: review_ready for another
    try, or failed after review.max_failures consecutive failures of the reviewer on the same heads. `infra`
    failures (never started, runner lost, out of quota) are the machine's, not the model's: not counted."""
    events(root).append("review_failed", dedupe_id=f"review:{batch_id}:{session}", batch=batch_id,
                        reviewer_session=session, heads=at, error=error, infra=infra)
    if _current(root, batch_id, session, at, cfg) is None:
        return
    n = 0
    for e in reversed(all_events(root)):
        if e.get("batch") != batch_id or e["type"] not in ("review_failed", "review_receipt"):
            continue
        if e["type"] == "review_receipt" or e.get("heads") != at:
            break
        n += not e.get("infra")
    if n >= int(cfg.get("review.max_failures", 2)):  # a model that keeps failing must not burn quota forever
        set_state(root, batch_id, "failed", expect=("in_review",), reason="review_failures", failures=n, heads=at)
    else:
        set_state(root, batch_id, "review_ready", expect=("in_review",))


def _settle(root, batch_id, receipt, cfg) -> dict:
    """What follows a written receipt; idempotent, so a harvest cut short by a crash is finished by the next one."""
    if receipt["verdict"] == "changes_requested" and \
            _current(root, batch_id, receipt["reviewer_session"], receipt["heads"], cfg):
        set_state(root, batch_id, "changes_requested", expect=("in_review",))
    if actions := diagnose(load_receipts(root, batch_id), int(cfg.get("review.max_rounds", 3))):
        events(root).append("review_not_converging", dedupe_id=f"nonconv:{batch_id}:r{receipt['round']}",
                            batch=batch_id, round=receipt["round"], actions=actions)
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
    # OSError (never started, runner or its files lost) or no verdict for want of quota = the machine's failure:
    # not counted against review.max_failures
    out = None
    try:
        if meta is None:
            raise OSError("reviewer was never started")
        if st["state"] != "done":
            raise OSError(f"reviewer job {st}")
        if st["exit_code"] != 0 or st["timed_out"]:
            raise ValueError(f"reviewer job {st}")
        raw = (state_dir(root) / "jobs" / meta["job_id"] / "stdout.log").read_text(encoding="utf-8", errors="replace")
        out = parse_output(raw)
        receipt = assemble(out, meta, load_receipts(root, batch_id))
        if errs := schemas.validate("review_receipt", receipt):
            raise ValueError("receipt fails schema: " + "; ".join(errs[:3]))
    except (ValueError, OSError) as err:
        # no meta.json: start() was cut short after in_review; its review_started event still has the heads
        began = meta or next((e for e in reversed(all_events(root)) if e["type"] == "review_started"
                              and e.get("batch") == batch_id and e.get("reviewer_session") == session), None)
        _failed(root, batch_id, session, began and began.get("heads"), cfg, str(err),
                infra=isinstance(err, OSError) or (out is None and _out_of_quota(root, meta)))
        raise FlowError(f"{batch_id} review {session}: {err}") from None
    data = json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    sha = sha256_bytes(data.encode())
    # event first: a crash before the file leaves an event that a re-harvest (same bytes, deduped) completes
    ev = events(root).append("review_receipt", dedupe_id=dedupe, batch=batch_id, round=meta["round"],
                             path=path.name, sha256=sha, verdict=receipt["verdict"], reviewer_session=session)
    if ev["type"] != "review_receipt" or ev.get("sha256") != sha:
        raise FlowError(f"{batch_id} review {session} was already recorded as {ev['type']} with other content")
    atomic_write(path, data)
    return _settle(root, batch_id, receipt, cfg)
