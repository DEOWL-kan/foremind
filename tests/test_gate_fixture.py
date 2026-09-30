"""Shared fixture for the review / acceptance / gate tests (no tests here).

A temp project `shop` with real git repos under shop/<repo>, batch worktrees at the seat's path
$FOREMIND_WT_ROOT/<project slug>/<batch>/<repo>
on branch fm/<batch>, optional bare `origin` remotes, and fake `gh` / `claude` on PATH — the real ones are never run.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from foremind import header, review, seat, worktree
from foremind.paths import state_dir

FAKE_GH = r'''
import json, os, subprocess, sys
path, a = os.environ["FAKE_GH_STATE"], sys.argv[1:]
st = json.load(open(path)) if os.path.exists(path) else {}
with open(os.environ["FAKE_GH_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
prs = st.setdefault("prs", {})  # keyed "<repo>:<branch>": gh runs in the repo's worktree
def pr(branch):
    return os.path.basename(os.getcwd()) + ":" + branch
def save():
    json.dump(st, open(path, "w"))
def remote_head(branch):
    out = subprocess.run(["git", "ls-remote", "origin", "refs/heads/" + branch], capture_output=True, text=True).stdout.split()
    return out[0] if out else None
if a[:2] == ["pr", "view"]:
    if pr(a[2]) not in prs:
        sys.exit("no pull requests found for branch " + a[2])
    print(json.dumps({"mergeStateStatus": "CLEAN", **prs[pr(a[2])], "headRefOid": remote_head(a[2])}))
elif a[:2] == ["pr", "create"]:
    head = a[a.index("--head") + 1]
    n = len(prs) + 1
    prs[pr(head)] = {"number": n, "url": "https://example.test/pr/%d" % n, "state": "OPEN", "isDraft": "--draft" in a,
                     "mergeCommit": None}
    save()
    print(prs[pr(head)]["url"])
elif a[:2] == ["pr", "ready"]:
    prs[pr(a[2])]["isDraft"] = False
    save()
elif a[:2] == ["pr", "merge"]:
    if remote_head(a[2]) != a[a.index("--match-head-commit") + 1]:
        sys.exit("head changed")
    if st.get("merge_queue"):  # the target requires a merge queue: queued (auto-merge on), still open
        prs[pr(a[2])]["autoMergeRequest"] = {"enabledAt": "2026-01-01T00:00:00Z"}
    else:
        prs[pr(a[2])].update(state="MERGED", mergeCommit={"oid": "f" * 40})
    save()
elif a[:1] == ["api"]:
    url = next(x for x in a[1:] if x.startswith("repos/"))
    if "/statuses/" in url and "-X" in a:
        st.setdefault("posted", []).append(a)
        save()
        print("{}")
    else:
        from urllib.parse import parse_qs, urlsplit
        sha, q = url.split("/commits/")[1].split("/")[0], parse_qs(urlsplit(url).query)
        n, per = int(q.get("page", ["1"])[0]), int(q.get("per_page", ["30"])[0])
        page = lambda items: items[(n - 1) * per:n * per]
        if "/check-runs" in url:
            print(json.dumps({"check_runs": page(st.get("check_runs", {}).get(sha, []))}))
        else:
            print(json.dumps(page(st.get("statuses", {}).get(sha, []))))
'''

FAKE_CLAUDE = r'''
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "role": os.environ.get("FOREMIND_ROLE")}) + "\n")
print(json.dumps({"type": "result", "is_error": False, "result": open(os.environ["FAKE_REVIEW"]).read(),
                  "session_id": sys.argv[sys.argv.index("--session-id") + 1]}))
'''

APPROVED = {"verdict": "approved", "issues": [{"severity": "note", "location": "api:src/app.py:1", "summary": "ok"}]}
TIERS = {"difficulty": "S", "org": "exec_review", "review": "zero_context", "model": "claude-opus-5-5",
         "effort": "high", "reason": "test"}


def sh(cwd, *args) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Project:
    def __init__(self, case, repo_ids=("api",), *, origin=False):
        self.case = case
        self.tmp = Path(case.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.tmp / "shop"
        (self.root / ".foremind").mkdir(parents=True)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        for name, code in (("gh", FAKE_GH), ("claude", FAKE_CLAUDE)):
            (bin_ / name).write_text(f"#!{sys.executable}\n{code}")
            (bin_ / name).chmod(0o755)
        (self.tmp / "gitconfig").write_text("")
        env = {"FOREMIND_WT_ROOT": str(self.tmp / "wt"), "FOREMIND_PROJECT": str(self.root),
               "FOREMIND_CONFIG_HOME": str(self.tmp / "config"), "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}",
               "GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
               "FAKE_GH_STATE": str(self.tmp / "gh.json"), "FAKE_GH_LOG": str(self.tmp / "gh.log"),
               "FAKE_CLAUDE_LOG": str(self.tmp / "claude.log"), "FAKE_REVIEW": str(self.tmp / "review.txt")}
        case.enterContext(mock.patch.dict(os.environ, env))
        for k in ("FOREMIND_SESSION", "FOREMIND_ROLE", "FOREMIND_BATCH"):
            os.environ.pop(k, None)
        self.review_output(APPROVED)
        self.cfg = {"repos": [{"id": r, "path": r} for r in repo_ids]}
        for r in repo_ids:
            d = self.root / r
            d.mkdir()
            sh(d, "git", "init", "-q", "-b", "main")
            (d / "README.md").write_text(f"{r}\n")
            sh(d, "git", "add", "-A")
            sh(d, "git", "commit", "-q", "-m", "init")
            self.cfg[f"delivery.repo.{r}.target_branch"] = "main"
            if origin:
                sh(self.tmp, "git", "init", "-q", "--bare", f"{r}.git")
                sh(d, "git", "remote", "add", "origin", str(self.tmp / f"{r}.git"))
                sh(d, "git", "push", "-q", "origin", "main")

    @property
    def repo_ids(self):
        return [r["id"] for r in self.cfg["repos"]]

    def write_header(self, bid, **fields):
        h = {"id": bid, "plan_id": bid.split(".")[0], "reqs": ["REQ-1"], "repos": self.repo_ids,
             "owns_paths": [f"{r}:src/*" for r in self.repo_ids], "reads": [], "depends_on": [], "merge_after": [],
             "start_commands": [], "accept_commands": ["true"], "tiers": TIERS, "mode": "auto", "hard_block": [],
             "budget_estimate": "1000", "must_read": [], "tools": [], "state": "running", **fields}
        path = state_dir(self.root) / "batches" / f"{bid}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header.render(h, "## 状态\n"), encoding="utf-8")
        return h

    def batch(self, bid="shop.1", **fields):
        h = self.write_header(bid, **fields)
        for r in self.repo_ids:
            sh(self.root / r, "git", "worktree", "add", "-q", "-b", f"fm/{bid}", str(self.wt(r, bid)), "main")
        return h

    def wt(self, r="api", bid="shop.1") -> Path:
        return worktree.batch_dir(seat.project_slug(self.root, self.cfg), bid) / r

    def commit(self, r="api", path="src/app.py", text="x = 1\n", bid="shop.1", msg="change") -> str:
        f = self.wt(r, bid) / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        sh(self.wt(r, bid), "git", "add", "-A")
        sh(self.wt(r, bid), "git", "commit", "-q", "-m", msg)
        return self.head(r, bid)

    def head(self, r="api", bid="shop.1") -> str:
        return sh(self.wt(r, bid), "git", "rev-parse", "HEAD")

    def state(self, bid="shop.1"):
        return review.load_batch(self.root, bid).get("state")

    def review_output(self, out):
        (self.tmp / "review.txt").write_text(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))

    def harvest(self, session, bid="shop.1", limit=30):
        deadline = time.monotonic() + limit
        while (r := review.harvest(self.root, bid, session, self.cfg)) is None:
            self.case.assertLess(time.monotonic(), deadline, "reviewer did not finish")
            time.sleep(0.05)
        return r

    def run_review(self, out=APPROVED, bid="shop.1", session="fm-shop-shop.1-1"):
        """Seat requests, supervisor starts and harvests: the receipt."""
        review.request(self.root, bid, self.cfg, session=session)
        self.review_output(out)
        return self.harvest(review.start(self.root, bid, self.cfg), bid)

    def events(self, type_):
        return [e for e in review.all_events(self.root) if e["type"] == type_]

    def gh_log(self) -> list:
        p = self.tmp / "gh.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def pr_state(self, r="api", branch="fm/shop.1", **fields):
        """Change what the fake host says about a repo's PR (state, mergeStateStatus, ...)."""
        st = self.gh_state()
        st["prs"][f"{r}:{branch}"].update(fields)
        self.gh_state(**st)

    def gh_state(self, **update) -> dict:
        p = self.tmp / "gh.json"
        st = json.loads(p.read_text()) if p.exists() else {}
        if update:
            st.update(update)
            p.write_text(json.dumps(st))
        return st

    def claude_calls(self) -> list:
        p = self.tmp / "claude.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []
