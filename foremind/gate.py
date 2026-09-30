"""`foremind gate` — the only releaser (DESIGN §7.3, §1.6, §1.8, §4.2 5f, §7.5, §8.2) — and `release-check`.

run() re-derives every check from files, git and the host: the latest receipt (approved; full lowercase SHAs equal
to every repo's current head; reviewer != implementer; content hash matches a program-started review), acceptance
and [gate].checks bound to the same heads and the same (repo, command) list (run by the program when missing or
stale), [gate].ci per repo (every page of the host's checks, and the branch's required checks from the delivery
proposal), owns_paths, merge_after / depends_on merged, dependency-manifest sections, delivery level, and for merges
the caller role, a pushed and up-to-date branch. A remote branch that is neither the reviewed head nor an ancestor of
it (someone pushed something else; a head `foremind update` replaced is only stale) voids the receipt. One gate or
update run per batch at a time (batches/<id>.gate.lock, not waited for). The result is written to
batches/<id>.gate.<heads-hash>.json first. Then the state the run will end in is worked out, and the commit status
foremind/gate is written for every head that is on its remote as reviewed (for #23-user repos: once the user
pushed): success only when the batch ends delivered or merged, pending while it waits for the pre-delivery audit
(§20 I49; whoever passes the audit writes success); failing merge prechecks alone write none. Then the batch moves:
in_review -> approved -> awaiting_audit | delivered -> merged (merge_dev only), merging through `merge_command`
(checked before and after) or `gh pr merge --match-head-commit`. Right before the group's first merge each gh repo's
PR must be mergeable to the host (mergeStateStatus in MERGEABLE, else check `mergeable` fails and nothing merges); a
PR the merge queue took is `merge_queued` and the batch stays delivered until a later run (or L0) sees it merged.
"""
import contextlib
import json
import re
import time
import tomllib
from datetime import datetime, timezone

from foremind import acceptance, job, pathmatch, review, schemas
from foremind.defaults import TABLE
from foremind.fsutil import LockBusy, atomic_write, file_lock, sha256_bytes
from foremind.manifests import SECTIONS, is_manifest
from foremind.paths import state_dir
from foremind.review import FlowError, bases, gh, git, is_ancestor, run as _run

GATE_CONTEXT = "foremind/gate"
APPROVAL = ("receipt", "reviewer", "receipt_event", "acceptance", "checks")  # + ci:<repo>, approval phase (§1.6)
MERGE_ROLES = ("seat", "supervisor", "user")  # controller, planner and one-shot roles never merge (§2.3)
APPROVED_Q = ("answered", "applied", "provisional", "overdue", "confirmed")  # overdue: still provisional (REQ-21)
MERGE_PRE = {"role", "merge_way", "up_to_date", "pushed", "mergeable"}  # merge-only: never a commit status (N9)
MAX_PAGES = 50  # of a host list (100 per page)
# GitHub's MergeStateStatus values `gh pr merge` goes ahead on; BEHIND, BLOCKED, DIRTY, DRAFT, UNKNOWN and any value
# added later stop the whole merge group (N3)
MERGEABLE = ("CLEAN", "HAS_HOOKS", "UNSTABLE")
SETTLE_TRIES, SETTLE_S = 3, 5

_DEPISH = re.compile(r"depend|requires|overrides|resolutions", re.I)
_MISSING = object()


# --- git criteria --------------------------------------------------------------

def _own_changes(repo_dir, base, head) -> bool:
    b, h = git(repo_dir, "rev-parse", f"{base}^{{tree}}", f"{head}^{{tree}}").split()
    return b != h


def is_merged(repo_dir, target, head, base) -> bool:
    """§1.6 + §20 I48: `head` has changes of its own against `base` (the merge-base recorded when review was
    requested), and merging it into `target` leaves the target tree unchanged — true after merge, squash and rebase
    merges alike; trees compare byte-exact, so an indentation-only difference is not merged. Without the first
    condition a fresh worktree, or one the target moved past, would read as merged."""
    # ponytail: later target commits rewriting the same lines read as unmerged (fails closed); host PR state first
    if not _own_changes(repo_dir, base, head):
        return False
    p = _run(["git", "merge-tree", "--write-tree", target, head], repo_dir, check=False)
    return p.returncode == 0 and p.stdout.split()[0] == git(repo_dir, "rev-parse", f"{target}^{{tree}}")


def patch_id(repo_dir, rev_range) -> str:
    """`git diff <range> | git patch-id --verbatim` ('' for an empty diff). Whitespace counts: indentation is
    meaningful in Python and YAML. §7.6 rebind (M2-2) compares `<old base>...<reviewed head>` with
    `<new target>...<new head>`. The context is pinned: diff.context=0 in the repo config would let a target change
    right next to the batch's lines through."""
    diff = _run(["git", "diff", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", "-U3",
                 "--inter-hunk-context=0", rev_range], repo_dir, text=False).stdout
    out = _run(["git", "patch-id", "--verbatim"], repo_dir, input=diff, text=False).stdout.split()
    return out[0].decode() if out else ""


def merged(repo, wt, head, cfg, base) -> bool:
    """One repo's §1.6 merged criterion: the host's PR state when there is a PR, else merge-tree on the target.
    Without a recorded `base` (review never requested) only the host's PR state counts (§20 I48)."""
    if base is not None and not _own_changes(wt, base, head):
        return False
    if review.push_by_system(cfg, repo.id):
        pr = review.pr_view(wt, review.branch(wt))
        if pr and pr.get("state") == "MERGED" and pr.get("headRefOid") == head:
            return True
    return base is not None and is_merged(wt, review.target_ref(repo, wt, cfg), head, base)


def batch_merged(root, batch_id, cfg) -> bool:
    """Every repo of the batch merged at its current worktree head (for L0 reconcile, §20 I2)."""
    pairs = review.batch_repos(root, review.load_batch(root, batch_id), cfg)
    hd = review.heads(pairs)
    bs = bases(root, batch_id, pairs, hd)
    return all(merged(r, wt, hd[r.id], cfg, bs.get(r.id)) for r, wt in pairs)


def pre_delivery_audit(header, cfg) -> bool:
    """Whether the batch's tier audits before delivery (§9.3); shared by the gate and L0 reconcile (§20 I2, I49)."""
    # ponytail: tiers.org alone; cfg is there for when the M tier (planner acting as controller) is settled (C5)
    return header["tiers"]["org"] == "controller_seats"


# --- dependency manifests -------------------------------------------------------

def _leaves(d, prefix=""):
    out = {}
    for k, v in d.items():
        out.update(_leaves(v, f"{prefix}{k}.") if isinstance(v, dict) and v else {f"{prefix}{k}": v})
    return out


def _parse(name, text):
    if text is None:
        return {}
    doc = json.loads(text) if name == "package.json" else tomllib.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("not a table")
    return doc


def manifest_need(path, old, new) -> int:
    """Authorization category a manifest change needs: 0 none, 3 dev dependencies, 4 runtime (or unparsable)."""
    name = path.rsplit("/", 1)[-1]
    if name not in SECTIONS:
        return 0 if old == new else 4
    try:
        a, b = _leaves(_parse(name, old)), _leaves(_parse(name, new))
    except ValueError:
        return 4
    runtime, dev = SECTIONS[name]
    under = lambda key, secs: any(key == s or key.startswith(s + ".") for s in secs)  # noqa: E731
    need = 0
    for key in a.keys() | b.keys():
        if a.get(key, _MISSING) == b.get(key, _MISSING):
            continue
        if under(key, runtime) or (not under(key, dev) and _DEPISH.search(key)):
            return 4
        if under(key, dev):
            need = 3
    return need


def _approved_categories(root, batch_id, qpath) -> set:
    """Categories of approved decisions whose unexpired exemption for this batch covers `qpath` (§8.2)."""
    # ponytail: an exemption is only issued on approval, so exemption + decision state = approved; precedents: M2-1
    cats, now = set(), datetime.now(timezone.utc)
    for p in sorted((state_dir(root) / "exemptions").glob("Q-*.json")):
        try:
            ex = json.loads(p.read_text(encoding="utf-8"))
            q = json.loads((state_dir(root) / "decisions" / p.name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (schemas.validate("exemption", ex) or schemas.validate("pending", q) or ex["batch"] != batch_id
                or datetime.fromisoformat(ex["expires_at"]) <= now):
            continue
        if q.get("state") in APPROVED_Q and pathmatch.owns(qpath, ex["match"].get("paths", [])):
            cats.add(q["category"])
    return cats


def _blob(wt, rev, path) -> str:
    """Raw content (no textconv); the caller knows from the diff status that the path exists at `rev`."""
    return _run(["git", "cat-file", "blob", f"{rev}:{path}"], wt, text=False).stdout.decode("utf-8", "replace")


# --- checks ----------------------------------------------------------------------

def _implementers(root, batch_id, evs) -> set:
    out = {e.get("requested_by") for e in evs if e["type"] == "review_requested" and e.get("batch") == batch_id}
    try:
        text = (state_dir(root) / "batches" / f"{batch_id}.lock").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    try:
        lock = json.loads(text)
    except ValueError:
        lock = text
    # ponytail: the lock format belongs to lock.py (M1-4); accept plain text or {"holder" | "session": ...}
    out |= {lock.get("holder"), lock.get("session")} if isinstance(lock, dict) else {lock}
    return out - {None, ""}


def _receipt_checks(root, batch_id, hd, diverged, add) -> bool:
    rs = review.receipts(root, batch_id)
    r = None
    if rs:
        n, path = rs[-1]
        data = path.read_bytes()
        try:
            r = json.loads(data)
        except ValueError:
            pass
        errs = schemas.validate("review_receipt", r) if isinstance(r, dict) else ["not a JSON object"]
    if not rs or errs:
        why = f"r{n}: {'; '.join(errs[:3])}" if rs else "no review receipt"
        for name in ("receipt", "reviewer", "receipt_event"):
            add(name, False, why)
        return False
    probs = [] if r["batch"] == batch_id else [f"receipt is for {r['batch']}"]
    if r["verdict"] != "approved":
        probs.append(f"verdict {r['verdict']}")
    if moved := sorted(k for k in hd.keys() | r["heads"].keys() if r["heads"].get(k) != hd.get(k)):
        probs.append(f"reviewed heads differ from the current heads in {moved}")
    if r["scope"] == "delta" and not any(x["scope"] == "full" and x["verdict"] == "approved"
                                         for x in review.load_receipts(root, batch_id)[:-1]):
        probs.append("delta approval without an approved full review")
    if r["scope"] == "incremental":  # REQ-6; a rebound copy (§7.6) is checked as the receipt it copies (r1 #1)
        chain = review.load_receipts(root, batch_id)
        i = len(chain) - 1
        if "rebound_from" in r:
            i = next((j for j in range(i - 1, -1, -1) if chain[j]["heads"] == r["rebound_from"]), 0)
        if i < 1 or r["delta_from"] != chain[i - 1]["heads"]:
            probs.append("incremental receipt whose delta_from is not the previous receipt's heads")
        elif not review.full_behind(chain):
            probs.append("incremental receipt with no full review down its delta_from chain")
    if diverged:  # §7.5: the remote holds something else than what was reviewed
        probs.append(f"the remote branch diverged from the reviewed head in {diverged}: receipt void; merge the "
                     "remote branch into the worktree (a new head), then request the review again")
    add("receipt", not probs, f"r{n}: " + ("; ".join(probs) or "approved"))
    evs = review.all_events(root)
    impl = r["reviewer_session"] in _implementers(root, batch_id, evs)
    add("reviewer", not impl, f"reviewer {r['reviewer_session']}" + (" also implemented this batch" if impl else ""))
    ok = receipt_backed(evs, batch_id, path.name, data, r)
    add("receipt_event", ok, "" if ok else "receipt does not match a review started by the program")
    return not probs and not impl and ok


def receipt_backed(evs, batch_id, name, data, r) -> bool:
    """The receipt file `name` (bytes `data`, parsed `r`) has its review_receipt event (path, sha256, reviewer), and
    that reviewer's review was started by the program on the heads the receipt was reviewed at (`rebound_from` for
    a rebound copy, §7.6)."""
    sha = sha256_bytes(data)
    return any(e["type"] == "review_receipt" and e.get("batch") == batch_id and e.get("path") == name
               and e.get("sha256") == sha and e.get("reviewer_session") == r["reviewer_session"] for e in evs) \
        and any(e["type"] == "review_started" and e.get("phase") == "intent" and e.get("batch") == batch_id
                and e.get("reviewer_session") == r["reviewer_session"]
                and e.get("heads") == r.get("rebound_from", r["heads"]) for e in evs)


def _local_check(root, batch_id, cfg, h, hd, kind, add, *, may_run, rerun):
    name = "acceptance" if kind == "accept" else "checks"
    cmds = acceptance.commands(h, cfg, kind)
    if not cmds:
        return add(name, True, "no [gate].checks configured")
    want = [(acceptance.command_text(c), acceptance.command_repo(c, h["repos"])) for c in cmds]
    ran = lambda res: [(c.get("command"), c.get("repo")) for c in res.get("commands", [])]  # noqa: E731
    path = acceptance.result_path(root, batch_id, kind, hd)

    def read():
        """(bytes, parsed, recorded): recorded = the latest `<kind>_run` event for the file carries its hash."""
        try:
            data = path.read_bytes()
            res = json.loads(data)
        except (OSError, ValueError):
            return None, None, False
        runs = [e for e in review.all_events(root) if e["type"] == f"{kind}_run" and e.get("path") == path.name]
        return data, res, bool(runs) and runs[-1].get("sha256") == sha256_bytes(data)

    data, res, recorded = read()
    # a file without its run record (a crash between the two, or an edit) is stale too: rerunning is safe; so is one
    # from before results recorded their repo
    stale = not recorded or not isinstance(res, dict) or ran(res) != want
    if may_run and (rerun or stale):
        acceptance.run(root, batch_id, cfg, kind)
        data, res, recorded = read()
    if data is None:
        return add(name, False, "not run on these heads" + ("" if may_run else " (waits for an approved receipt)"))
    errs = schemas.validate("acceptance_result", res)
    probs = errs[:3]
    if not errs:
        if res["heads"] != hd:
            probs.append("heads differ")
        if ran(res) != want:
            probs.append("commands or their repos changed since the run")
        if failed := [c["command"] for c in res["commands"] if c["exit_code"] != 0]:
            probs.append(f"failed: {failed}")
    if not recorded:
        probs.append("file does not match the program's run record")
    add(name, not probs, "; ".join(probs) or f"{len(want)} passed")


def required_checks(root, repo_id) -> list[str] | None:
    """Required check names on the repo's target branch, as `init` / `doctor --rescan` detected them into
    delivery.proposal.json (install.detect); None when unknown (no proposal, unreadable, or "unknown")."""
    try:
        prop = json.loads((state_dir(root) / "delivery.proposal.json").read_text(encoding="utf-8"))
        v = prop["repos"][repo_id]["facts"]["required_checks"]["value"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return v if isinstance(v, list) and all(isinstance(x, str) for x in v) else None


def _pages(wt, url, key=None) -> list:
    """Every item of a paged host list: `?per_page=100&page=n` until a page comes back short; past MAX_PAGES full
    pages FlowError ends the gate run (and releases its lock) instead of paging on."""
    out = []
    for n in range(1, MAX_PAGES + 1):
        page = json.loads(gh(wt, "api", f"{url}?per_page=100&page={n}") or ("{}" if key else "[]"))
        items = (page.get(key, []) if isinstance(page, dict) else None) if key else page
        if not isinstance(items, list):
            raise ValueError(f"{url} page {n}: not a list")
        out += items
        if len(items) < 100:
            return out
    raise FlowError(f"{url}: more than {MAX_PAGES} pages of 100")


def _ci(wt, sha, required=None) -> tuple[str, str]:
    """('success' | 'pending' | 'failure', detail) of the host's checks on this exact SHA; per check name only the
    latest run counts (a re-run turning red to green on the same SHA passes). Our own foremind/gate is ignored.
    A `required` check name (branch protection) that has not reported on the SHA yet is pending."""
    base = f"repos/{{owner}}/{{repo}}/commits/{sha}"
    try:
        runs = _pages(wt, f"{base}/check-runs", "check_runs")
        stats = _pages(wt, f"{base}/statuses")
    except ValueError as e:
        return "failure", f"unreadable CI answer: {e}"
    latest = {}
    for c in runs:
        st = "pending" if c.get("status") != "completed" else \
            "success" if c.get("conclusion") in ("success", "neutral", "skipped") else "failure"
        latest.setdefault(f"check {c.get('name')}", []).append((c.get("id", 0), st))
    for s in stats:
        if s.get("context") != GATE_CONTEXT:
            st = s.get("state") if s.get("state") in ("success", "pending") else "failure"
            latest.setdefault(f"status {s.get('context')}", []).append((s.get("id", 0), st))
    now = {k: max(v)[1] for k, v in latest.items()}
    if bad := sorted(k for k, v in now.items() if v == "failure"):
        return "failure", f"failed: {bad}"
    seen = {c.get("name") for c in runs} | {s.get("context") for s in stats}
    if missing := sorted(set(required or ()) - seen - {GATE_CONTEXT}):  # foremind/gate is written after this
        return "pending", f"required checks not reported yet: {missing}"
    if not now:
        return "pending", "no CI results yet"
    if wait := sorted(k for k, v in now.items() if v == "pending"):
        return "pending", f"waiting: {wait}"
    return "success", f"{len(now)} green"


def _ci_check(root, cfg, repo, wt, sha, phase, has_checks) -> tuple:
    """(name, ok, detail, pending) for one repo's [gate].ci (DESIGN §7.3)."""
    mode, name = review.ci_mode(cfg, repo.id), f"ci:{repo.id}"
    if mode not in ("local_first", "required", "none"):
        return name, False, f"unknown [gate].ci {mode!r}", False
    if mode == "none":
        detail = "none: local checks only" if has_checks else "[gate].ci none needs [gate].checks"
        return name, has_checks, detail, False
    if not review.push_by_system(cfg, repo.id):  # N13: nothing to stand in without [gate].checks
        detail = "#23 is the user's: local checks stand in for CI" if has_checks else \
            "#23 is the user's: local checks stand in for CI, and [gate].checks is empty"
        return name, has_checks, detail, False
    if mode == "local_first" and phase == "approval":
        return name, True, "local_first: local checks during review rounds", False
    br = review.branch(wt)
    if mode == "local_first" and (pr := review.pr_view(wt, br)) and pr.get("isDraft"):
        gh(wt, "pr", "ready", br)  # the one CI run, now that the last round passed
    st, detail = _ci(wt, sha, required_checks(root, repo.id))
    return name, st == "success", f"{mode}: {detail}", st == "pending"


def _upstream(root, h) -> list[str]:
    """merge_after and depends_on batches not merged yet (§7.3, §4.2 5f)."""
    waiting = []
    for b in sorted({m["batch"] for m in h["merge_after"]} | set(h["depends_on"])):
        try:
            s = review.load_batch(root, b).get("state", "planned")
        except FlowError:
            s = "unknown"
        if s not in ("merged", "cataloged"):
            waiting.append(f"{b} ({s})")
    return waiting


def _diffs(pairs, hd, trefs) -> dict:
    """{repo: (merge-base, {path: status letter})} of each batch branch against its target."""
    out = {}
    for r, wt in pairs:
        base = git(wt, "merge-base", trefs[r.id], hd[r.id])
        toks = _run(["git", "diff", "--name-status", "--no-renames", "-z", base, hd[r.id]], wt).stdout.split("\0")
        out[r.id] = (base, dict(zip(toks[1::2], toks[0::2])))
    return out


def _dependency_problems(root, batch_id, pairs, hd, diffs) -> list[str]:
    probs = []
    for r, wt in pairs:
        base, files = diffs[r.id]
        for f, status in files.items():
            if not is_manifest(f):
                continue
            try:  # absent = added / deleted per the diff; any other read failure counts as unparsable: #4
                need = manifest_need(f, None if status == "A" else _blob(wt, base, f),
                                     None if status == "D" else _blob(wt, hd[r.id], f))
            except FlowError:
                need = 4
            if need and not _approved_categories(root, batch_id, f"{r.id}:{f}") & ({4} if need == 4 else {3, 4}):
                kind = "runtime (or unparsable)" if need == 4 else "dev"
                probs.append(f"{r.id}:{f} changes {kind} dependencies without an approved #{need} decision")
    return probs


# --- merge ---------------------------------------------------------------------------

def _merge_way(cfg, repo) -> tuple | None:
    """("command", merge_command), else ("gh", method) when there is a PR and a merge_method; None = cannot merge."""
    if cmd := review.repo_cfg(cfg, repo.id, "merge_command"):
        return "command", cmd
    method = review.repo_cfg(cfg, repo.id, "merge_method")
    return ("gh", method) if review.push_by_system(cfg, repo.id) and method in ("squash", "merge", "rebase") else None


def _merge_one(root, batch_id, repo, wt, head, cfg, base):
    br, rem = review.branch(wt), review.remote(repo, wt)
    now = review.remote_head(wt, rem, br) if rem else git(wt, "rev-parse", f"refs/heads/{br}")
    if now != head:
        raise FlowError(f"{repo.id}: {'remote' if rem else 'local'} {br} is at {now}, the reviewed head is {head}")
    way = _merge_way(cfg, repo)
    if not way:
        raise FlowError(f"{repo.id}: no merge_command, and no PR with a merge_method")
    ev, dedupe = review.events(root), f"merge:{batch_id}:{repo.id}:{head}"
    kind, value = way
    ev.append("merge", phase="intent", dedupe_id=dedupe, batch=batch_id, repo=repo.id, head=head, via=kind)
    if kind == "command":
        jobs = state_dir(root) / "jobs"
        env = {"FOREMIND_BATCH": batch_id, "FOREMIND_REPO": repo.id, "FOREMIND_REPO_PATH": str(repo.path),
               "FOREMIND_HEAD": head, "FOREMIND_BRANCH": br, "FOREMIND_TARGET": review.target_branch(repo, cfg)}
        jid = job.start(jobs, ["/bin/sh", "-c", value], cwd=wt, env=env,
                        timeout_s=int(cfg.get("oneshot.timeout_min", TABLE["oneshot.timeout_min"])) * 60)
        if (code := acceptance.wait(jobs, jid).get("exit_code")) != 0:
            raise FlowError(f"{repo.id}: merge_command exited {code} (output in {jobs / jid})")
    else:
        gh(wt, "pr", "merge", br, f"--{value}", "--match-head-commit", head)
    if not merged(repo, wt, head, cfg, base):
        # N4: where the target requires a merge queue, `gh pr merge` only queues the PR (or turns auto-merge on until
        # the required checks pass): a PR still open at the reviewed head waits there, it did not fail to merge
        if kind == "gh" and _open_at(review.pr_view(wt, br), head):
            ev.append("merge_queued", batch=batch_id, repo=repo.id, head=head)
            return False
        ev.append("merge_unverified", batch=batch_id, repo=repo.id, head=head, severity="P0")
        raise FlowError(f"{repo.id}: the merge reported success but {head} is not in the target (P0)")
    ev.append("merge", dedupe_id=dedupe, batch=batch_id, repo=repo.id, head=head)
    return True


def _open_at(pr, head) -> bool:
    return bool(pr) and pr.get("state") == "OPEN" and pr.get("headRefOid") == head


def _queued(root, batch_id, pairs, hd) -> set:
    """Repos a gate run left in the host's merge queue at their current head (`merge_queued`) whose PR is still open
    there: past their merge, waiting on the host; a later run or L0 reconcile sees them merged."""
    # ponytail: a PR the queue dropped (red checks) still reads as queued; gh pr view has no queue field
    # (isInMergeQueue is GraphQL only): ask `gh api graphql` if that bites
    ids = {(e.get("repo"), e.get("head")) for e in review.all_events(root)
           if e["type"] == "merge_queued" and e.get("batch") == batch_id}
    return {r.id for r, wt in pairs if (r.id, hd[r.id]) in ids and _open_at(review.pr_view(wt, review.branch(wt)),
                                                                             hd[r.id])}


def _unmergeable(cfg, pairs) -> list[str]:
    """N3: repos merging through `gh pr merge` whose PR the host does not find mergeable (mergeStateStatus), each
    with its status. Asked after foremind/gate is written: a branch protection requiring it reads BLOCKED before.
    The host recomputes the status after that write: UNKNOWN or BLOCKED is asked again SETTLE_TRIES times in all."""
    bad = []
    for r, wt in pairs:
        if (_merge_way(cfg, r) or ("",))[0] != "gh":
            continue
        for n in range(SETTLE_TRIES):
            if n:
                time.sleep(SETTLE_S)
            p = _run(["gh", "pr", "view", review.branch(wt), "--json", "mergeStateStatus"], wt, check=False)
            try:
                st = json.loads(p.stdout).get("mergeStateStatus") if p.returncode == 0 else None
            except (ValueError, AttributeError):
                st = None
            if st not in ("UNKNOWN", "BLOCKED"):
                break
        if st not in MERGEABLE:
            bad.append(f"{r.id} {st or p.stderr.strip() or 'no answer'}")
    return bad


def _merge_group(root, batch_id, pairs, hd, cfg, bs, skip, pre) -> list[str]:
    """A batch's repos are one merge group (§1.8): all checks passed before the first merge; a failure after some
    merged is recorded as partially_merged (P0) and never rolled back automatically. `skip`: repos merged or queued
    before this run (a rerun finishes a group cut short); a repo without a recorded base checks its merge against
    `pre`, its merge-base with the target taken before this run merged anything. Returns the repos it queued."""
    done, queued = [], []
    for r, wt in pairs:
        try:
            if r.id not in skip and not _merge_one(root, batch_id, r, wt, hd[r.id], cfg, bs.get(r.id) or pre[r.id]):
                queued.append(r.id)
        except FlowError as e:
            if done:
                review.events(root).append("merge_group_partial", batch=batch_id, merged=done, failed=r.id,
                                           error=str(e), severity="P0")
            raise
        done.append(r.id)
    return queued


def _remote_heads(pairs) -> dict:
    """{repo: SHA of its batch branch on the remote, None while not pushed}; repos without a remote are left out."""
    return {r.id: review.remote_head(wt, rem, review.branch(wt)) for r, wt in pairs if (rem := review.remote(r, wt))}


def _post_statuses(pairs, hd, remote, status, description) -> list[str]:
    warnings = []
    for r, wt in pairs:
        if remote.get(r.id) != hd[r.id]:
            continue  # not pushed (yet): gate.<hash>.json stands, the status follows once the head is pushed
        try:
            gh(wt, "api", "-X", "POST", f"repos/{{owner}}/{{repo}}/statuses/{hd[r.id]}", "-f", f"state={status}",
               "-f", f"context={GATE_CONTEXT}", "-f", f"description={description[:140]}")
        except FlowError as e:
            warnings.append(f"{r.id}: commit status not written: {e}")
    return warnings


# --- the gate -------------------------------------------------------------------------

def may_merge(role, session) -> bool:
    """§2.3; a session that does not say its role is refused: only no session at all is the user."""
    return role in MERGE_ROLES or (not role and not session)


def batch_lock(root, batch_id):
    """batches/<id>.gate.lock, not waited for (LockBusy): one gate or `foremind update` run per batch at a time."""
    return file_lock(review.batch_path(root, batch_id).with_name(f"{batch_id}.gate.lock"), blocking=False)


def run(root, batch_id, cfg, **kw) -> dict:
    """kw: role and session (the caller's FOREMIND_ROLE / FOREMIND_SESSION), rerun, merge."""
    with contextlib.ExitStack() as stack:
        try:  # two gates on one batch could both merge, or overwrite each other's acceptance results
            stack.enter_context(batch_lock(root, batch_id))
        except LockBusy:
            raise FlowError(f"{batch_id}: another gate run holds this batch; try again when it is done") from None
        return _gate(root, batch_id, cfg, **kw)


def _gate(root, batch_id, cfg, *, role=None, session=None, rerun=False, merge=True) -> dict:
    h = review.load_batch(root, batch_id)
    st = h.get("state", "planned")
    if st not in ("in_review", "approved", "awaiting_audit", "delivered"):
        raise FlowError(f"{batch_id} is {st}; the gate runs from in_review on")
    pairs = review.batch_repos(root, h, cfg)
    hd = review.heads(pairs)
    trefs = {r.id: review.target_ref(r, wt, cfg) for r, wt in pairs}
    remote = _remote_heads(pairs)
    wts = {r.id: wt for r, wt in pairs}
    # a remote still at a head our own `foremind update` replaced holds nothing new: stale, not diverged (§7.6)
    replaced = {(rid, sha) for e in review.all_events(root)
                if e["type"] == "batch_updated" and e.get("batch") == batch_id
                for rid, sha in (e.get("prior_heads") or {}).items()}
    diverged = sorted(rid for rid, sha in remote.items()
                      if sha and (rid, sha) not in replaced and not is_ancestor(wts[rid], sha, hd[rid]))
    checks, pending = [], set()

    def add(name, ok, detail="", wait=False):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        if wait:
            pending.add(name)

    receipt_ok = _receipt_checks(root, batch_id, hd, diverged, add)
    for kind in ("accept", "checks"):
        _local_check(root, batch_id, cfg, h, hd, kind, add, may_run=receipt_ok, rerun=rerun)
    diffs = _diffs(pairs, hd, trefs)
    outside = [f"{rid}:{f}" for rid, (_, files) in diffs.items() for f in files
               if not pathmatch.owns(f"{rid}:{f}", h["owns_paths"])]
    add("owns_paths", not outside, f"outside owns_paths: {outside}" if outside else "")
    waiting = _upstream(root, h)
    add("merge_after", not waiting, f"not merged yet: {waiting}" if waiting else "")
    deps = _dependency_problems(root, batch_id, pairs, hd, diffs)
    add("dependencies", not deps, "; ".join(deps))
    levels = {r.id: review.repo_cfg(cfg, r.id, "level", "delivery.level", TABLE["delivery.level"])
              for r, _ in pairs}
    add("delivery", all(v in ("done", "merge_dev") for v in levels.values()), json.dumps(levels))

    has_checks = bool(cfg.get("gate.checks"))
    ci = {r.id: _ci_check(root, cfg, r, wt, hd[r.id], "approval", has_checks) for r, wt in pairs}
    if st == "in_review" and all(c["ok"] for c in checks if c["name"] in APPROVAL) and all(c[1] for c in ci.values()):
        review.set_state(root, batch_id, "approved", expect=("in_review",), heads=hd)
        st = "approved"
    if st != "in_review" and all(c["ok"] for c in checks):  # local_first: its one CI run starts only now
        for r, wt in pairs:
            if review.ci_mode(cfg, r.id) == "local_first":
                ci[r.id] = _ci_check(root, cfg, r, wt, hd[r.id], "delivery", has_checks)
    for name, ok, detail, wait in ci.values():
        add(name, ok, detail, wait)
    merging = merge and all(v == "merge_dev" for v in levels.values())
    if merging:  # a batch's repos are one merge group: everything is checked before the first merge (§1.8)
        bs = bases(root, batch_id, pairs, hd)
        # repos merged by an earlier run are past these checks (behind the target, branch deleted by the host)
        already = {r.id for r, wt in pairs if merged(r, wt, hd[r.id], cfg, bs.get(r.id))}
        queued = _queued(root, batch_id, [(r, wt) for r, wt in pairs if r.id not in already], hd)
        todo = [(r, wt) for r, wt in pairs if r.id not in already | queued]
        who = role or (f"none, in session {session}" if session else "user")
        add("role", may_merge(role, session), f"caller role {who}")
        stuck = [r.id for r, _ in todo if not _merge_way(cfg, r)]
        add("merge_way", not stuck, f"no merge_command, and no PR with a merge_method: {stuck}" if stuck else "")
        behind = [r.id for r, wt in todo if not is_ancestor(wt, trefs[r.id], hd[r.id])]
        add("up_to_date", not behind,
            f"behind the target: run `foremind update {batch_id}` first (§7.6): {behind}" if behind else "")
        unpushed = sorted(rid for rid, sha in remote.items() if rid not in already | queued and sha != hd[rid])
        add("pushed", not unpushed, f"push the reviewed head first: {unpushed}" if unpushed else "")

    result, path = _record(root, batch_id, hd, checks, pending)

    # §20 I49: the state this run ends in decides the status, so success never precedes the pre-delivery audit
    failing = [c["name"] for c in checks if not c["ok"]]
    after = st
    if not failing and st == "approved":
        after = "awaiting_audit" if pre_delivery_audit(h, cfg) else "delivered"
    final = "merged" if not failing and after == "delivered" and merging else after
    status = None  # merge prechecks alone leave the status as it is: a success stays (N9)
    if gating := [f for f in failing if f not in MERGE_PRE]:
        status = ("pending" if set(gating) <= pending else "failure"), f"failing: {', '.join(failing)}"
    elif not failing:
        status = ("success", "passed") if final in ("delivered", "merged") else \
            ("pending", "awaiting pre-delivery audit")
    warnings = _post_statuses(pairs, hd, remote, *status) if status else []

    if after != st:
        review.set_state(root, batch_id, after, expect=(st,), heads=hd)
    if final != after:
        if bad := _unmergeable(cfg, todo):  # N3: none of the group merges while one cannot
            checks.append({"name": "mergeable", "ok": False, "detail": f"the host finds these not mergeable: {bad}"})
            result, path = _record(root, batch_id, hd, checks, pending)
            return {**result, "state": after, "path": str(path), "warnings": warnings}
        waiting = sorted(queued | set(_merge_group(root, batch_id, pairs, hd, cfg, bs, already | queued,
                                                   {rid: d[0] for rid, d in diffs.items()})))
        if waiting:  # stays delivered; the next gate run or L0 reconcile finds the queue's merge
            warnings.append(f"in the host's merge queue, not merged yet: {waiting}")
            return {**result, "state": after, "path": str(path), "warnings": warnings}
        review.set_state(root, batch_id, final, expect=(after,), heads=hd)
    return {**result, "state": final, "path": str(path), "warnings": warnings}


def _record(root, batch_id, hd, checks, pending) -> tuple:
    """Writes batches/<id>.gate.<heads-hash>.json and its `gate_result` event, which also names the failing checks
    and the pending ones (the update phase reads them)."""
    result = {"batch": batch_id, "heads": hd, "checks": checks,
              "verdict": "pass" if all(c["ok"] for c in checks) else "fail"}
    if errs := schemas.validate("gate_result", result):
        raise FlowError(f"gate result fails schema: {errs[:3]}")
    data = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    path = state_dir(root) / "batches" / f"{batch_id}.gate.{review.heads_hash(hd)}.json"
    atomic_write(path, data)
    review.events(root).append("gate_result", batch=batch_id, heads=hd, path=path.name,
                               sha256=sha256_bytes(data.encode()), verdict=result["verdict"],
                               failing=[c["name"] for c in checks if not c["ok"]], pending=sorted(pending))
    return result, path


# --- release-check ------------------------------------------------------------------------

UNCONFIRMED = ("provisional", "overdue", "overturned")  # overturned stays until reverted
SHIPPING = ("delivered", "merged", "cataloged", "unknown")


def release_check(root, repo_dir, rev_range) -> list[str]:
    """Problems that block releasing `rev_range` ([] = clean): unconfirmed provisional decisions (§8.4, §20 I1).

    A decision in UNCONFIRMED blocks when a commit in the range carries `provisional: PV-n` for it, when a batch
    it is tied to (Q.blocks, PV.batch) has been delivered or merged, or when it is tied to no batch at all.
    Provisional records are read from decisions/PV-<n>.json (schema `provisional`).
    """
    ddir = state_dir(root) / "decisions"

    def load(pattern):
        out = {}
        for p in sorted(ddir.glob(pattern)):
            try:
                out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                out[p.stem] = None
        return out

    qs, pvs = load("Q-*.json"), load("PV-*.json")
    probs = [f"decisions/{k}.json is unreadable" for k, v in {**qs, **pvs}.items() if not isinstance(v, dict)]
    msgs = git(repo_dir, "log", "--format=%B", "--end-of-options", rev_range)  # a range never reads as an option
    marked = set(re.findall(r"provisional:\s*(PV-[1-9][0-9]*)\b", msgs))
    for pv in sorted(marked):
        if not isinstance(pvs.get(pv), dict) or pvs[pv].get("question") not in qs:
            probs.append(f"{pv}: a commit in {rev_range} is marked provisional but has no decision record")

    def state_of(b):
        try:
            return review.load_batch(root, b).get("state", "planned")
        except FlowError:
            return "unknown"

    for qid, q in qs.items():
        if not isinstance(q, dict) or q.get("state") not in UNCONFIRMED:
            continue
        linked = {k: v for k, v in pvs.items() if isinstance(v, dict) and v.get("question") == qid}
        in_range = sorted(k for k in linked if k in marked)
        tied = set(q.get("blocks") or []) | {v["batch"] for v in linked.values() if v.get("batch")}
        shipped = sorted(b for b in tied if state_of(b) in SHIPPING)
        if in_range or shipped or not tied:
            why = f"commits {in_range}" if in_range else f"batches {shipped}" if shipped else "not tied to a batch"
            probs.append(f"{qid} is {q['state']}, an unconfirmed provisional decision ({why})")
    return probs
