"""Opening a seat (DESIGN §5, §6.4, §20 I3, I8, I39, I43–I46). The caller has already decided the batch is ready.

open_seat, in order:
  model     the batch's model and its provider are not excluded (before anything is claimed)
  claim     inside the project lock: pick a session name never used before, and (fresh batch) check the batch is
            ready (anything started gets a successor, r2 SF-C) and its owns_paths are disjoint from every started batch (by state, or holding a lock) with no
            depends_on path to or from it (I43), then take the batch lock. Intent `seat_open` (dedupe = session),
            closed by a result whatever happens
  worktrees one per repo under wt/<slug>/<batch>/, a new branch starting from the fetched <remote>/<target> (I46);
            read-only ones for cross-repo upstreams, their refs recorded in `seat_opened`
  verify    branch, full SHA, open PR head (GitHub remotes, via gh), uncommitted files, command exit codes against
            the record: a fresh batch expects a clean worktree and its start commands' `expect` (default 0); a
            successor compares the last handoff section (none written: record "none", said so in the kickoff). On
            mismatch nothing is launched, a fresh claim is released and `seat_verify_failed` is written
  launch    vendor launch -> carrier.create (a name the carrier already has is an error, never reused); wait for the
            session's heartbeat (SessionStart writes the first)
  kickoff   inside the state lock re-check the batch, state -> running; program delivery of the start instruction;
            event `seat_opened` (a successor's carries the lock holder at open time as `predecessor`)
Once something may have been launched, any failure closes the session; a fresh claim is given back only on exit
evidence, or under the manual carrier, where the program itself started nothing. A successor (after a handoff or a
broken lock) skips the claim: it takes the lock itself with `handoff --accept`.
claim_for_user is `seat --user` (I3); continue_seat moves a live session on to its next batch (§5, I44).
"""
import fnmatch
import functools
import json
import os
import re
import subprocess
import time
from pathlib import Path

from foremind import config, handoff, header, inbox, job, lock, pathmatch, repos as repos_mod, schemas, worktree
from foremind.carriers import SessionExists
from foremind.defaults import TABLE
from foremind.events import EventLog
from foremind.fsutil import atomic_write, sha256_bytes, sha256_file
from foremind.paths import state_dir
from foremind.schemas import BATCH_ID, SHA
from foremind.state import BATCH, BATCH_SIDE, can_transition, transition
from foremind.vendors import get as get_vendor

STARTED = ("running", "review_ready", "in_review", "changes_requested", "approved", "awaiting_audit", "delivered")
SUCCESSOR_FROM = handoff.SUCCESSOR_FROM  # I43
_PROVIDERS = {"claude": ("claude", "anthropic"), "codex": ("codex", "openai")}  # vendor -> names exclude.providers may use


class SeatError(Exception):
    pass


def _events(root):
    return EventLog(state_dir(root) / "events.jsonl")


def setting(cfg, key):
    return cfg.get(key, TABLE[key])


def header_path(root, batch) -> Path:
    if not re.fullmatch(BATCH_ID, batch):
        raise ValueError(f"bad batch id {batch!r}")
    return state_dir(root) / "batches" / f"{batch}.md"


def read_header(root, batch) -> dict:
    """The batch header, checked against schema `batch_header` (a missing field is a SeatError, not a KeyError)."""
    p = header_path(root, batch)
    try:
        h = header.parse(p.read_text(encoding="utf-8"))[0]
    except FileNotFoundError:
        raise SeatError(f"no batch header {p}") from None
    except header.HeaderError as e:
        raise SeatError(f"{p}: {e}") from None
    errs = schemas.validate("batch_header", h) + ([] if h.get("id") == batch else [f"id: {h.get('id')!r} is not {batch}"])
    if errs:
        raise SeatError(f"{p}: " + "; ".join(errs))
    return h


def set_state(root, batch, to, *, held=False) -> None:
    """Program-only write of the header's `state` (§20 I1), checked against the batch state machine."""
    with lock.state_lock(root, held):
        p = header_path(root, batch)
        h, body = header.parse(p.read_text(encoding="utf-8"))
        frm = h.get("state", "planned")
        h["state"] = transition(BATCH, frm, to)
        atomic_write(p, header.render(h, body))
        _events(root).append("batch_state", batch=batch, frm=frm, to=to)


def project_slug(root, cfg) -> str:
    """I39: `[project].name` (else the directory name), anything outside [A-Za-z0-9_-] as "_", then "-" and the first
    6 hex digits of sha256(absolute project root): two projects of the same name never share session names."""
    root = Path(root).resolve()
    name = re.sub(r"[^A-Za-z0-9_-]", "_", str(cfg.get("project.name") or root.name))
    return f"{name}-{sha256_bytes(str(root).encode('utf-8'))[:6]}"


def session_name(slug, what, n) -> str:
    """`fm-<slug>-<batch or role>-<n>` (§1.1, I39), with anything outside [A-Za-z0-9_-] (the batch's `.`) as "_":
    tmux treats . and : as separators."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", f"fm-{slug}-{what}-{n}")


def heartbeat_path(root, session) -> Path:
    return state_dir(root) / "heartbeats" / f"{session}.json"


def settings_path(root, session) -> Path:
    """I40: under .foremind/sessions/ (#22-protected), so a seat cannot edit its own hooks."""
    return state_dir(root) / "sessions" / f"{session}.settings.json"


def _next_seq(root, prefix) -> int:
    """1 + the largest n of any `<prefix><n>` the project has seen, in events (archives included), heartbeats,
    inboxes, settings files and carrier records: a retry, a monthly rotation or a lost event never reuses a name."""
    names = [e.get("session") for e in handoff.history(root)]
    for d in ("heartbeats", "inbox", "sessions", "carriers/orca"):
        names += [p.name.split(".")[0] for p in (state_dir(root) / d).glob("*")]
    pat = re.compile(re.escape(prefix) + r"([1-9][0-9]*)")
    return 1 + max((int(m[1]) for n in names if isinstance(n, str) and (m := pat.fullmatch(n))), default=0)


# --- runtime disjointness ----------------------------------------------------

def paths_overlap(a, b) -> bool:
    """Could `<repo>:<glob>` a and b name a common file? (pathmatch.overlap; ready.py and freeze.py call it here)"""
    return pathmatch.overlap(a, b)


def _is_started(h) -> bool:
    st, prior = h.get("state", "planned"), h.get("state_prior")
    if st in BATCH_SIDE:
        return prior in STARTED if prior else True  # no recorded prior: count it, to be safe
    return st in STARTED


def _all_headers(root):
    for p in sorted((state_dir(root) / "batches").glob("*.md")):
        bid = p.name[:-3]
        if re.fullmatch(BATCH_ID, bid):
            yield bid, header.parse(p.read_text(encoding="utf-8"))[0]


def _upstreams(headers, bid) -> set:
    seen, todo = set(), list(headers.get(bid, {}).get("depends_on", []))
    while todo:
        u = todo.pop()
        if u not in seen:
            seen.add(u)
            todo += headers.get(u, {}).get("depends_on", [])
    return seen


def _claim(root, bid, session, *, reclaim=False) -> None:
    """Inside the project lock: check the batch can start and is disjoint, then take its lock for `session`.
    Batch pairs with a depends_on path either way are not compared (I43): the ready check orders them."""
    headers = dict(_all_headers(root))
    h = read_header(root, bid)
    cur = lock.holder(root, bid)
    if cur is not None and not (reclaim and cur == session):
        raise SeatError(f"{bid}: lock already held by {cur}")
    if cur is None and not can_transition(BATCH, h.get("state", "planned"), "running"):
        raise SeatError(f"{bid} is {h.get('state', 'planned')}: cannot start")
    ups = _upstreams(headers, bid)
    for other, oh in headers.items():
        if (other == bid or other in ups or bid in _upstreams(headers, other)
                or not (_is_started(oh) or lock.holder(root, other))):
            continue
        for a in h["owns_paths"]:
            for b in oh.get("owns_paths", []):
                if paths_overlap(a, b):
                    raise SeatError(f"{bid}: {a} overlaps {b} of started batch {other}")
    lock.acquire(root, bid, session, held=True)


# --- worktrees ---------------------------------------------------------------

def _batch_repos(root, cfg, h):
    by_id = {r.id: r for r in repos_mod.load_repos(root, cfg)}
    missing = [r for r in h["repos"] if r not in by_id]
    if missing:
        raise SeatError(f"batch repos not registered in [[repos]]: {missing}")
    return [by_id[r] for r in h["repos"]], by_id


def _target(cfg, repo):
    """Where new work in `repo` starts (I46, SF-6): with a remote, fetch it and take <remote>/<target branch>, or
    <remote>/HEAD when no target is configured; without one, the local target branch. Never the user's checkout."""
    target = cfg.get(f"delivery.repo.{repo.id}.target_branch") or repo.default_branch
    remote = worktree.remote_name(repo.path, repo.remote)
    if remote:
        worktree.fetch(repo.path, remote)
        ref = f"refs/remotes/{remote}/{target or 'HEAD'}"
        if worktree.git(repo.path, "rev-parse", "--verify", "--quiet", ref, check=False):
            return ref
        if target:
            raise SeatError(f"{repo.id}: {remote} has no branch {target}")
    elif target:
        return target
    raise SeatError(f"{repo.id}: no target branch and no remote default branch: set default_branch in its [[repos]] "
                    f"entry (or delivery.repo.{repo.id}.target_branch)")


def approved_heads(root, batch) -> dict | None:
    """heads of the batch's latest approving review receipt (M1-6 writes `<id>.review.r<n>.json`), even if a later
    round asked for changes: stacked downstream work keeps building on what was approved (SF-6, §1.6)."""
    rounds = []
    for p in (state_dir(root) / "batches").glob(f"{batch}.review.r*.json"):
        if m := re.fullmatch(rf"{re.escape(batch)}\.review\.r([1-9][0-9]*)\.json", p.name):
            rounds.append((int(m[1]), p))
    for _, p in sorted(rounds, reverse=True):
        r = json.loads(p.read_text(encoding="utf-8"))
        if r.get("verdict") == "approved":
            return r.get("heads")
    return None


def _start_ref(root, cfg, up, repo):
    """Start point in `repo` (§1.8, §4.2 5f): no upstream, `merged` mode, or an upstream already merged -> the target;
    `approved` -> the upstream's approved head."""
    if (up is None or setting(cfg, "delivery.depends_on") == "merged"
            or read_header(root, up).get("state") in ("merged", "cataloged")):
        return _target(cfg, repo)
    heads = approved_heads(root, up) or {}
    if repo.id not in heads:
        raise SeatError(f"upstream {up} has no approved head for repo {repo.id}")
    return heads[repo.id]


def _starts(wts) -> dict:
    """{repo: head} of worktrees a batch starts in (not a successor's): a base that stays when the batch's commits
    are pushed to the target (bounds.pushed, m2d.7 ⑥)."""
    return {rid: head for rid, (_, head) in wts.items()}


def prepare_worktrees(root, cfg, h, slug):
    """Returns ({repo id: (path, head)}, [{upstream, repo, sha, path}] of the read-only upstream worktrees)."""
    bid = h["id"]
    mine, by_id = _batch_repos(root, cfg, h)
    ups = {u: read_header(root, u) for u in h.get("depends_on", [])}
    wts = {}
    for repo in mine:
        stacked = [u for u, uh in ups.items() if repo.id in uh.get("repos", [])]
        if len(stacked) > 1 and setting(cfg, "delivery.depends_on") == "approved":
            raise SeatError(f"{bid}: cannot stack on several upstreams in repo {repo.id}: {stacked}")
        start = functools.partial(_start_ref, root, cfg, stacked[0] if stacked else None, repo)  # only if new
        wts[repo.id] = worktree.create(slug, bid, repo, branch=f"fm/{bid}", start_point=start)
    ro = []
    for u, uh in ups.items():
        for rid in uh.get("repos", []):
            if rid not in wts:
                if rid not in by_id:
                    raise SeatError(f"upstream {u} repo {rid} is not registered in [[repos]]")
                path, sha = worktree.create_readonly(slug, bid, u, by_id[rid], _start_ref(root, cfg, u, by_id[rid]))
                ro.append({"upstream": u, "repo": rid, "sha": sha, "path": str(path)})
    return wts, ro


# --- mechanical verification -------------------------------------------------

def _review_request(root, bid) -> dict | None:
    """The batch's latest review_requested when it is newer than its latest handoff section: a holder that committed
    and asked for review after its last section (then released on delivery, or gone) left that as its record."""
    last = None
    for e in handoff.history(root):
        if e.get("batch") == bid and e["type"] in ("handoff_written", "review_requested") and e["phase"] == "result":
            last = e
    return last if last and last["type"] == "review_requested" and isinstance(last.get("heads"), dict) else None


def expectations(root, h, wts, *, successor):
    """What the record says: ({repo: (branch, sha)}, [(command, exit code)], changed files or None).
    A fresh batch expects a clean worktree; None = a successor with no handoff section (record "none"): only the
    branch name and PR head can be checked. A review request newer than the section is the record then: its heads,
    committed, a clean worktree (finding 33)."""
    if successor and (req := _review_request(root, h["id"])):
        return {rid: (f"fm/{h['id']}", sha) for rid, sha in req["heads"].items()}, [], []
    sec = handoff.latest_section(root, h["id"]) if successor else None
    if sec is None:
        repos = {rid: (f"fm/{h['id']}", head) for rid, (_, head) in wts.items()}
        if successor:
            return repos, [], None
        return repos, [(c, c.get("expect", 0) if isinstance(c, dict) else 0) for c in h["start_commands"]], []
    st = sec["state"]
    last = st.get("last_test")
    return ({rid: (v["branch"], v["sha"]) for rid, v in st["repos"].items()},
            [(last["command"], last["exit_code"])] if last else [], st["changed_files"])


def _pr_heads(wt, branch) -> list[str]:
    """Heads of the open PRs from `branch`; only a GitHub remote is asked (SF-7), anything else has no PR to compare."""
    remote = worktree.remote_name(wt)
    url = worktree.git(wt, "config", "--get", f"remote.{remote}.url", check=False) if remote else ""
    if "github.com" not in url:
        return []  # ponytail: GitHub Enterprise hosts are not recognised; add a host key to [[repos]] when needed
    argv = ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "headRefOid"]
    try:
        r = subprocess.run(argv, cwd=wt, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise SeatError(f"gh pr list --head {branch}: {e}") from None
    if r.returncode:
        raise SeatError(f"gh pr list --head {branch}: {r.stderr.strip()}")
    try:
        return [pr["headRefOid"] for pr in json.loads(r.stdout)]
    except (ValueError, TypeError, KeyError):
        raise SeatError(f"gh pr list --head {branch}: unexpected output {r.stdout[:200]!r}") from None


def _wait_job(jobs, jid):
    while (st := job.status(jobs, jid))["state"] in ("starting", "running"):
        time.sleep(0.1)
    return st


def verify(root, h, wts, expect, *, timeout_s) -> list[str]:
    """Problems found ([] = matches the record). Commands run as detached jobs (§1.1 long actions)."""
    repos, commands, changed = expect
    problems = []
    for rid, (branch, sha) in repos.items():
        if rid not in wts:
            problems.append(f"{rid}: in the record but not a repo of this batch")
            continue
        wt = wts[rid][0]
        got_branch = worktree.git(wt, "symbolic-ref", "--short", "-q", "HEAD", check=False)
        if got_branch != branch:
            problems.append(f"{rid}: branch {got_branch or '(detached)'}, record says {branch}")
        if not re.fullmatch(SHA, sha):
            problems.append(f"{rid}: record SHA {sha!r} is not a full lowercase object name")
        head = worktree.git(wt, "rev-parse", "HEAD")
        if head != sha:
            problems.append(f"{rid}: HEAD {head}, record says {sha}")
        try:
            prs = _pr_heads(wt, branch)
        except SeatError as e:
            problems.append(f"{rid}: {e}")
        else:
            problems += [f"{rid}: open PR head {pr} differs from local HEAD {head}" for pr in prs if pr != head]
        if changed is not None:  # SF-4: uncommitted work the record does not mention is not silently inherited
            extra = [p for p in worktree.dirty(wt) if f"{rid}:{p}" not in changed
                     and not (len(wts) == 1 and p in changed)]
            if extra:
                problems.append(f"{rid}: uncommitted changes not in the record's changed_files: {extra[:10]}")
    missing = set(wts) - set(repos)
    if missing:
        problems.append(f"record has no state for repos {sorted(missing)}")
    paths = [p for p, _ in wts.values()]
    default_cwd = paths[0] if len(paths) == 1 else paths[0].parent  # I8: single repo -> its worktree, else batch dir
    for cmd, want in commands:
        run_, repo = (cmd, None) if isinstance(cmd, str) else (cmd["run"], cmd.get("repo"))  # I8/I45
        if repo and repo not in wts:
            problems.append(f"`{run_}`: repo {repo} is not a repo of this batch")
            continue
        cwd = wts[repo][0] if repo else default_cwd
        jobs = state_dir(root) / "jobs"
        jid = job.start(jobs, ["/bin/sh", "-c", run_], cwd=cwd, timeout_s=timeout_s)
        st = _wait_job(jobs, jid)
        got = st.get("exit_code")
        _events(root).append("verify_command", batch=h["id"], command=run_, exit_code=got, expected=want, job=jid)
        if got != want:
            problems.append(f"`{run_}` exited {got if st['state'] == 'done' else st['state']}, record says {want}")
    return problems


# --- launch ------------------------------------------------------------------

def wait_heartbeat(root, session, batch, since, timeout_s) -> dict | None:
    """The session's heartbeat for `batch` written at or after `since` (epoch seconds), within timeout_s."""
    p, deadline = heartbeat_path(root, session), time.monotonic() + timeout_s
    while True:
        try:
            if p.stat().st_mtime >= since - 1:  # 1 s slack for timestamp granularity
                hb = json.loads(p.read_text(encoding="utf-8"))
                if hb.get("session") == session and hb.get("batch") == batch:
                    return hb
        except (FileNotFoundError, ValueError):
            pass  # not written yet, or caught mid-write
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def _vendor(model):
    return "claude" if model.startswith("claude") else "codex"


def _check_model(cfg, model):
    """exclude.models and exclude.providers: no fallback or alias gets around them (§1.5, §3.2)."""
    for pat in cfg.get("exclude.models", TABLE["exclude.models"]):
        if fnmatch.fnmatchcase(model.lower(), pat.lower()) or pat.lower() in model.lower():
            raise SeatError(f"model {model} is excluded by exclude.models ({pat})")
    names = _PROVIDERS[_vendor(model)]
    for p in cfg.get("exclude.providers", TABLE["exclude.providers"]):
        if p.lower() in names:
            raise SeatError(f"model {model} ({names[1]}) is excluded by exclude.providers ({p})")


def _launch(root, cfg, h, session, wts, ro):
    model = h["tiers"]["model"]
    vendor = get_vendor(_vendor(model))
    multi = len(wts) > 1
    paths = [p for p, _ in wts.values()]
    return vendor.launch(session=session, batch=h["id"], role="seat", project_root=Path(root).resolve(),
                         settings_path=settings_path(root, session), cwd=paths[0].parent if multi else paths[0],
                         permission_mode=setting(cfg, "seat.permission_mode"),
                         add_dirs=(paths if multi else []) + [r["path"] for r in ro], multi_repo=multi, model=model,
                         effort=h["tiers"].get("effort"))


def kickoff_text(bid, wts, *, successor, record=True) -> str:
    where = "；".join(f"{rid}: {p}" for rid, (p, _) in wts.items())
    if successor and not record:
        return (f"接手批次 {bid}（worktree {where}）。没有交接段可对照：程序只核对了分支与 PR head，没有核对 SHA、"
                "改动文件和测试退出码。先用 git log 与 git status 弄清现场，读考纲，逐项确认后执行 "
                "`foremind handoff --accept` 再继续。")
    if successor:
        return (f"接手批次 {bid}（worktree {where}）。程序已核对分支、完整 SHA、PR head、未提交的改动文件与最近一次"
                "测试的退出码。读考纲与最近一段交接，逐项确认「待验证」，然后执行 `foremind handoff --accept` 再继续。")
    return (f"开工：批次 {bid}（worktree {where}）。考纲已在会话开头注入；程序已跑过起点命令并与记录一致。"
            "按交接文档执行，边做边写 D/F 记录。")


def _load(root, bid):
    h = read_header(root, bid)
    return h, config.load(root, config.task_layer(h))


def _abandon(root, carrier, bid, session, *, claimed, launched):
    """Take back a launch that gets no kickoff: close the session if one may exist, and give a claim back only on
    exit evidence, when nothing was launched, or under manual (the program started nothing; a session the user
    starts late holds no lock, so its writes are stopped). Returns (evidence, released)."""
    evidence = None
    if launched:
        try:
            evidence = carrier.close(session)
        except Exception:
            pass  # not confirmed: keep the lock (§10.4)
    released = False
    if claimed and (not launched or evidence is not None or carrier.name == "manual"):
        try:
            lock.release(root, bid, session)
            released = True
        except lock.LockError:
            pass  # someone else holds it by now: not ours to give back
    return evidence, released


def _settings_sha(root, session) -> str | None:
    """sha256 of the settings file the session was launched with (for L0 to compare later); None without one."""
    try:
        return sha256_file(settings_path(root, session))
    except OSError:
        return None


def changes_text(n, issues) -> str:
    """REQ-8: round `n`'s must-fix list as the seat gets it (tick._gate, _changes_lists): one line per issue, one the
    program lowered (review.assemble: was, filtered) showing so."""
    lines = [f"- [{i.get('severity')}" + (f"，原为 {i['was']}，{i.get('filtered')}" if i.get("was") else "")
             + f"] {i.get('location')}：{i.get('summary')}" for i in issues]
    return f"Foremind：审查第 {n} 轮要求修改：\n" + "\n".join(lines) + "\n改完提交，再执行 `foremind review`。"


def _changes_lists(root, bid) -> list[tuple[str, str]]:
    """(sender, text) of the must-fix lists a changes_requested batch's holder would have been sent (finding 34), in
    the formats of review.request_changes and tick._gate, while no seat has taken them since the batch last entered
    changes_requested: the latest receipt when it asks for changes (it is written before the move it causes), then
    the changes_requested_by lists since. Taken: a move to running (save one by a successor whose kickoff failed:
    seat_open result lists_unsent), or a successor opened with them (seat_opened lists)."""
    evs = list(handoff.history(root))
    entered = ran = was = -1
    receipt = None
    for i, e in enumerate(evs):
        if e.get("batch") != bid:
            continue
        if e["type"] == "batch_state":  # review.set_state writes state, seat.set_state to
            to = e.get("state", e.get("to"))
            if to == "changes_requested":
                entered = i
            elif to == "running":
                ran, was = i, ran
        elif e["type"] == "review_receipt":
            receipt = (i, e)
        elif e["type"] == "seat_open" and e.get("lists_unsent"):
            ran = was
        elif e["type"] == "seat_opened" and e.get("lists"):
            ran = i
    if entered < 0 or ran > entered:
        return []
    out = []
    if receipt and receipt[0] > ran and receipt[1].get("verdict") == "changes_requested":
        r = json.loads((state_dir(root) / "batches" / Path(receipt[1]["path"]).name).read_text(encoding="utf-8"))
        out.append(("supervisor", changes_text(receipt[1].get("round"), r.get("issues", []))))
    for e in evs[entered + 1:]:
        if e["type"] == "changes_requested_by" and e.get("batch") == bid:
            out.append(("controller", f"Foremind：要求修改（{e.get('by')}，{e['ts']}）：\n"
                        + "\n".join(f"- {x}" for x in e.get("items", [])) + "\n改完提交，再执行 `foremind review`。"))
    return out


def open_seat(root, bid, *, carrier, successor=False) -> dict:
    root = Path(root).resolve()
    h, cfg = _load(root, bid)
    _check_model(cfg, h["tiers"]["model"])
    slug = project_slug(root, cfg)
    ev = _events(root)
    with lock.state_lock(root):
        session = session_name(slug, bid, _next_seq(root, session_name(slug, bid, "")))
        state = read_header(root, bid).get("state", "planned")
        if successor and state not in SUCCESSOR_FROM:
            raise SeatError(f"{bid} is {state}: a successor takes over only {', '.join(SUCCESSOR_FROM)}")
        if not successor and state != "ready":  # r2 SF-C: never around the successor's verification and accept
            raise SeatError(f"{bid} is {state}: a new seat starts only a ready batch"
                            + ("; open a successor instead" if state in SUCCESSOR_FROM else ""))
        predecessor = lock.holder(root, bid) if successor else None
        if predecessor == lock.USER:  # r2 N-d: accept would move the user's batch to a session
            raise SeatError(f"{bid} is held by the user (seat --user): no successor takes it over")
        if not successor:
            _claim(root, bid, session)
        # m2b.10: whose it is (the supervisor's job, FOREMIND_JOB; None from the user's command) and the process
        # that writes its result, so the supervisor settles it as soon as that process is gone; outside a job it
        # claims the user (m2d.7 ⑦: events.append checks seat_ancestor)
        job = os.environ.get("FOREMIND_JOB") or None
        ev.append("seat_open", phase="intent", dedupe_id=f"seat_open:{session}", batch=bid, session=session,
                  successor=successor, predecessor=predecessor, job=job, pid=os.getpid(),
                  **({} if job else {"by": "user"}))
    claimed = not successor
    out = {"ok": False, "batch": bid, "session": session, "worktrees": {}, "problems": []}

    def result(ok, **fields):  # dedupe ids are shared by all event types, hence the type prefix
        ev.append("seat_open", dedupe_id=f"seat_open:{session}", batch=bid, session=session, ok=ok, **fields)

    try:
        wts, ro = prepare_worktrees(root, cfg, h, slug)
        out["worktrees"] = {rid: str(p) for rid, (p, _) in wts.items()}
        expect = expectations(root, h, wts, successor=successor)
        problems = verify(root, h, wts, expect, timeout_s=setting(cfg, "seat.verify_timeout_s"))
        spec = None if problems else _launch(root, cfg, h, session, wts, ro)
    except BaseException as e:
        _abandon(root, carrier, bid, session, claimed=claimed, launched=False)
        result(False, error=str(e))
        raise
    if problems:
        _abandon(root, carrier, bid, session, claimed=claimed, launched=False)
        ev.append("seat_verify_failed", batch=bid, session=session, successor=successor, problems=problems)
        result(False, problems=problems)
        return {**out, "problems": problems}

    timeout = setting(cfg, "seat.manual_sessionstart_timeout_s" if carrier.name == "manual"
                      else "seat.sessionstart_timeout_s")
    t0, launched = time.time(), True  # from here on a session may exist, even if create() fails
    lists, moved = [], False
    launch_id, settings_sha = f"seat_launch:{session}", _settings_sha(root, session)  # r1 #5: what it starts with
    ev.append("seat_launch", phase="intent", dedupe_id=launch_id, batch=bid, session=session, carrier=carrier.name,
              socket=getattr(carrier, "socket", None), model=h["tiers"]["model"], effort=h["tiers"].get("effort"))
    try:
        try:
            carrier.create(session, spec)
        except SessionExists:
            launched = False  # someone else's session: never close it
            raise
        hb = wait_heartbeat(root, session, bid, t0, timeout)
        ev.append("seat_launch", dedupe_id=launch_id, batch=bid, session=session, ok=hb is not None,
                  agent_session_id=(hb or {}).get("agent_session_id"), settings_sha256=settings_sha)
        if hb is None:
            problem = f"no heartbeat from {session} within {timeout} s"
            evidence, released = _abandon(root, carrier, bid, session, claimed=claimed, launched=True)
            ev.append("seat_no_heartbeat", batch=bid, session=session, closed=evidence is not None, released=released)
            result(False, problems=[problem], released=released)
            return {**out, "problems": [problem]}
        with lock.state_lock(root):  # the batch may have been paused, cancelled or taken over meanwhile
            now = read_header(root, bid).get("state", "planned")
            if claimed and lock.holder(root, bid) != session:
                raise SeatError(f"{bid}: lock no longer held by {session}")
            if successor and now not in SUCCESSOR_FROM:
                raise SeatError(f"{bid} became {now} while its successor was starting")
            # once running, tick.gates sends nothing more: the lists go to the successor's inbox here, before the
            # move (REQ-5); a failed write leaves changes_requested, a failed kickoff marks its result lists_unsent
            # (r1 #2), and the next successor gets them
            lists = _changes_lists(root, bid) if successor else []
            for sender, text in lists:
                inbox.append(session, text, sender=sender, root=root)
            if now != "running":
                set_state(root, bid, "running", held=True)
                moved = True
        carrier.deliver(session, kickoff_text(bid, wts, successor=successor, record=expect[2] is not None))
    except BaseException as e:
        ev.append("seat_launch", dedupe_id=launch_id, batch=bid, session=session, ok=False, error=str(e))  # deduped
        evidence, released = _abandon(root, carrier, bid, session, claimed=claimed, launched=launched)
        result(False, error=str(e), closed=evidence is not None, released=released,
               **({"lists_unsent": True} if lists and moved else {}))
        raise
    ev.append("seat_opened", batch=bid, session=session, successor=successor, carrier=carrier.name,
              worktrees=out["worktrees"], readonly=[{k: r[k] for k in ("upstream", "repo", "sha")} for r in ro],
              **({"lists": len(lists)} if lists else {}), **({} if successor else {"starts": _starts(wts)}),
              **({"predecessor": predecessor, "record": "none" if expect[2] is None else
                  "review_request" if _review_request(root, bid) else "handoff"}
                 if successor else {}))
    result(True)
    return {**out, "ok": True}


def claim_for_user(root, bid) -> dict:
    """`seat --user` (§20 I3): the user holds the lock, ready -> running at once, worktrees created; no session."""
    root = Path(root).resolve()
    h, cfg = _load(root, bid)
    with lock.state_lock(root):
        _claim(root, bid, lock.USER, reclaim=True)
        if read_header(root, bid).get("state", "planned") != "running":
            set_state(root, bid, "running", held=True)
    wts, ro = prepare_worktrees(root, cfg, h, project_slug(root, cfg))
    paths = {rid: str(p) for rid, (p, _) in wts.items()}
    _events(root).append("seat_user", batch=bid, worktrees=paths, starts=_starts(wts),
                         readonly=[{k: r[k] for k in ("upstream", "repo", "sha")} for r in ro])
    return paths


def _launched_with(root, session) -> dict | None:
    return next((e for e in reversed(list(handoff.history(root))) if e["type"] == "seat_launch"
                 and e["phase"] == "intent" and e.get("session") == session), None)


def continue_seat(root, session, frm, to, *, remaining_budget, carrier) -> dict:
    """Continuation (§5, I44): if enabled (`seat.continue_enabled`, default off until `/add-dir` delivery is tested),
    `to` runs on the session's model and effort, and the session's remaining effective budget covers `to`'s
    estimate, move its lock from `frm` to `to` in one project-lock step, prepare and verify `to`, deliver `/add-dir`
    for each new worktree and then the next kickoff to the same session. The caller decides the coupling ("high or
    same chain") and computes remaining_budget."""
    root = Path(root).resolve()
    h, cfg = _load(root, to)
    out = {"ok": False, "batch": to, "session": session, "worktrees": {}, "problems": []}
    if not setting(cfg, "seat.continue_enabled"):
        return {**out, "problems": ["continuation is off (seat.continue_enabled): open a new session"]}
    need = int(h["budget_estimate"])
    if remaining_budget < need:
        return {**out, "problems": [f"remaining budget {remaining_budget} < estimate {need}: open a new session"]}
    launched = _launched_with(root, session)
    if launched is None:
        raise SeatError(f"{session}: no launch record, so its model is unknown")
    want, have = (h["tiers"]["model"], h["tiers"].get("effort")), (launched.get("model"), launched.get("effort"))
    if want != have:
        return {**out, "problems": [f"{to} needs {want[0]}/{want[1]}, {session} runs {have[0]}/{have[1]}: "
                                    "open a new session"]}
    _check_model(cfg, want[0])
    ev, did = _events(root), f"seat_continue:{session}:{to}"
    with lock.state_lock(root):
        if lock.holder(root, frm) != session:
            raise SeatError(f"{frm}: {session} does not hold its lock")
        if (st := read_header(root, frm).get("state")) not in ("delivered", "merged", "cataloged"):
            raise SeatError(f"{frm} is {st}: a seat moves on only after delivering")
        if (st := read_header(root, to).get("state", "planned")) != "ready":  # r2 SF-C
            raise SeatError(f"{to} is {st}: a seat moves on only to a ready batch")
        _claim(root, to, session)
        lock.release(root, frm, session, held=True)
        ev.append("seat_continue", phase="intent", dedupe_id=did, batch=to, session=session, frm=frm)

    def result(ok, **fields):
        ev.append("seat_continue", dedupe_id=did, batch=to, session=session, ok=ok, **fields)

    try:
        wts, ro = prepare_worktrees(root, cfg, h, project_slug(root, cfg))
        out["worktrees"] = {rid: str(p) for rid, (p, _) in wts.items()}
        problems = verify(root, h, wts, expectations(root, h, wts, successor=False),
                          timeout_s=setting(cfg, "seat.verify_timeout_s"))
    except BaseException as e:
        lock.release(root, to, session)
        result(False, error=str(e))
        raise
    if problems:
        lock.release(root, to, session)
        ev.append("seat_verify_failed", batch=to, session=session, successor=False, problems=problems)
        result(False, problems=problems)
        return {**out, "problems": problems}
    sending = False
    try:
        with lock.state_lock(root):
            if lock.holder(root, to) != session:
                raise SeatError(f"{to}: lock no longer held by {session}")
            set_state(root, to, "running", held=True)
        sending = True  # from here on the session may have seen part of the kickoff
        for p in [*out["worktrees"].values(), *(r["path"] for r in ro)]:
            carrier.deliver(session, f"/add-dir {p}")  # I44【待实测】: the new worktrees join the session
        carrier.deliver(session, kickoff_text(to, wts, successor=False)
                        + f" 考纲不会自动注入：先读 {state_dir(root) / 'batches' / f'{to}.handoff.md'}，并切到上面的 worktree。")
    except BaseException as e:
        evidence, released = _abandon(root, carrier, to, session, claimed=True, launched=sending)
        result(False, error=str(e), closed=evidence is not None, released=released)
        raise
    ev.append("seat_opened", batch=to, session=session, successor=False, carrier=carrier.name,
              worktrees=out["worktrees"], readonly=[{k: r[k] for k in ("upstream", "repo", "sha")} for r in ro],
              continued_from=frm, starts=_starts(wts))
    result(True)
    return {**out, "ok": True}
