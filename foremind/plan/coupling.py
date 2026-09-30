"""Coupling between the batches of a plan (DESIGN §4.2 5d).

Signals per pair, each 0..1, or None when there is nothing to analyse (unknown is not "no coupling"). ref and
cochange only count a shared repo where both batches have existing (tracked) files: with one side still to be
written, there is nothing to measure against.
  ref        Python `ast` import edges: share of the two batches' existing .py files that import the other batch;
             None when no such repo has a .py file of either
  cochange   `git log --name-only`: commits touching both batches' owns_paths / commits touching either (Jaccard);
             None when no commit in such a repo's history touches either
  semantic   the planner's declaration in the batch header `coupling` (the larger of the two directions; 0 if none)
  contract   both change the same contract file (hard rule: that change belongs in the contract batch) -> tier high
C = the weighted mean of the known signals (weights w_ref, w_cochange, w_semantic renormalised over them), so with
only the declaration known C is the declared score; tier high if C >= high, medium if C >= medium, else low.
Weights, thresholds and the history depth come from config `plan.coupling.*` (defaults: defaults.TABLE, §4.2 5d).
Imports and history only exist inside one repo; across repos only contract and semantic count (§1.8).
"""
import ast
import subprocess
from pathlib import PurePosixPath

from foremind import pathmatch
from foremind.defaults import TABLE
from foremind.plan.model import is_contract
from foremind.plan.validate import owns_overlap


def _git(repo, *args) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""  # not a git repo: no history and no tracked files to read


def _imports(repo, f) -> set[str]:
    """Repo-relative module files `f` may import (`a/b.py`, `a/b/__init__.py`)."""
    try:
        tree = ast.parse((repo / f).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return set()
    pkg, out = PurePosixPath(f).parent.parts, set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name.split(".") for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = list(pkg[:max(0, len(pkg) - node.level + 1)]) if node.level else []
            base += node.module.split(".") if node.module else []
            mods = [base, *(base + [a.name] for a in node.names)]  # `from a import b`: b may be a module
        else:
            continue
        out |= {f"{'/'.join(m)}{end}" for m in mods if m for end in (".py", "/__init__.py")}
    return out


def _importers(repo, src, dst, cache) -> int:
    """How many files of `src` import a file of `dst`."""
    # ponytail: module -> file by path suffix (covers src/ layouts, may match a same-named module elsewhere);
    # read sys.path roots from the project if that misfires
    n = 0
    for f in src:
        if f not in cache:
            cache[f] = _imports(repo, f)
        n += any(t == m or t.endswith("/" + m) for m in cache[f] for t in dst)
    return n


def _commits(repo, depth) -> list[set]:
    out = _git(repo, "log", f"-n{depth}", "--name-only", "--format=%x00")
    return [set(filter(None, c.splitlines())) for c in out.split("\0") if c.strip()]


def _declared(h, other) -> float:
    cs = h.get("coupling", [])
    return max((c["score"] for c in cs if isinstance(c, dict) and c.get("batch") == other
                and isinstance(c.get("score"), (int, float)) and not isinstance(c.get("score"), bool)), default=0)


def _r(x):
    return None if x is None else round(x, 3)


def analyze(plan, repos: dict, config: dict | None = None) -> list[dict]:
    """`repos`: {repo id: working tree Path}. Returns [{a, b, ref, cochange, semantic, contract, score, tier}] for
    each pair of the plan's active batches, a before b in plan order."""
    k = {x[len("plan.coupling."):]: (config or {}).get(x, v) for x, v in TABLE.items()
         if x.startswith("plan.coupling.")}
    heads = {b: d.header for b, d in plan.active().items()}
    pats = {b: {} for b in heads}
    for b, h in heads.items():
        for q in h["owns_paths"]:
            rid, _, p = q.partition(":")
            pats[b].setdefault(rid, []).append(p)
    used = {r for m in pats.values() for r in m if r in repos}
    files = {r: [f for f in _git(repos[r], "ls-files", "-z").split("\0") if f] for r in used}
    history = {r: _commits(repos[r], int(k["history"])) for r in used}
    contract_files = [q for h in heads.values() if is_contract(h) for q in h["owns_paths"]]
    ids, out, cache = list(heads), [], {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            nr = dr = nc = dc = 0
            for r in pats[a].keys() & pats[b].keys() & used:
                fa = [f for f in files[r] if pathmatch.owns(f, pats[a][r])]
                fb = [f for f in files[r] if pathmatch.owns(f, pats[b][r])]
                if not fa or not fb:  # one side has nothing yet: unknown here, not a known 0
                    continue
                pa, pb = [f for f in fa if f.endswith(".py")], [f for f in fb if f.endswith(".py")]
                c = cache.setdefault(r, {})
                nr += _importers(repos[r], pa, fb, c) + _importers(repos[r], pb, fa, c)
                dr += len(pa) + len(pb)
                for files_changed in history[r]:
                    ta = any(pathmatch.owns(f, pats[a][r]) for f in files_changed)
                    tb = any(pathmatch.owns(f, pats[b][r]) for f in files_changed)
                    nc, dc = nc + (ta and tb), dc + (ta or tb)
            ref, co = (nr / dr if dr else None), (nc / dc if dc else None)
            sem = max(_declared(heads[a], b), _declared(heads[b], a))
            contract = any(owns_overlap(heads[a]["owns_paths"], [cf]) and owns_overlap(heads[b]["owns_paths"], [cf])
                           for cf in contract_files)
            known = [(k[w], x) for w, x in (("w_ref", ref), ("w_cochange", co), ("w_semantic", sem)) if x is not None]
            total = sum(w for w, _ in known)
            score = round(sum(w * x for w, x in known) / total, 6) if total else 0.0
            tier = "high" if contract or score >= k["high"] else "medium" if score >= k["medium"] else "low"
            out.append({"a": a, "b": b, "ref": _r(ref), "cochange": _r(co), "semantic": sem,
                        "contract": contract, "score": score, "tier": tier})
    return out
