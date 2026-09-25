"""PreToolUse rules (DESIGN §1.1 hooks and boundary, §2.3 writable matrix, §8.1–8.2, §20 I7 I8 I10 I11 I35–I37).

Two kinds of denial:
- hard blocks — system/host config (#22, fixed), built-in and configured patterns (`hard_block.patterns`).
  Exemptable per batch by an exemption of the same category (exemptions.py); an unexempted hit becomes a pending
  request.
- writable matrix — owns_paths, role, lock holder, and the rest of `.foremind/` (program state, I36). Not
  exemptable: the seat asks for a scope change (#8), a handoff, or uses the foremind command that writes the file.
Non-Foremind sessions (the user's own) get only the `.foremind/` state rule and delivery.toml: their Edit/Write of
the other #22 files goes through and PostToolUse records it (I51). Bash is checked against command patterns only: a
write through Bash cannot be tied to a path here; the gate's diff and L0 hashes catch it afterwards.
"""
import os
import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

from foremind import exemptions, lock, worktree
from foremind.paths import user_config_dir
from foremind.seat import project_slug

EDIT_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}

# #22 system and host config (§2.3 last row, §20 I36 I40). Compared lower-cased: on a case-insensitive filesystem
# claude.md is CLAUDE.md.
SYSTEM_GLOBS = ("*/foremind.toml", "*/.foremind/config.toml", "*/.foremind/delivery.toml", "*/.foremind/roles/*",
                "*/.foremind/hooks/*", "*/.foremind/adapters/*", "*/.foremind/sessions/*",
                "*/.claude/settings*.json", "*/claude.md", "*/claude.local.md", "*/agents.md", "*/.codex/*")
DELIVERY_GLOB = "*/.foremind/delivery.toml"  # program-owned: even the user's own session goes through foremind (I51)
DELIVERY_REASON = ("{path}：delivery.toml 由程序独占、整体重写，不手改；交付约定经 `foremind init` 或 "
                   "`foremind doctor --rescan` 的提案确认后写入")
STATE_GLOB = "*/.foremind/*"  # everything else there is program state (I36)
STATE_REASON = ("{path}：.foremind/ 是程序状态，只经 foremind 命令写（可写矩阵 §2.3，不可豁免）：批次日志用 "
                "`foremind log`，交接段用 `foremind handoff --write`，凭据、决策等经 `foremind decide --new` 申请")

# Always on (§8.1 rows with a hard block). Manifests carry dev and runtime deps alike, so they file under the
# stricter #4; the seat may refile as #3 through `foremind decide --new`.
DEFAULT_PATTERNS = [
    {"category": 4,
     "paths": ["*/package.json", "*/pyproject.toml", "*/requirements*.txt", "*/Pipfile", "*/setup.py", "*/setup.cfg",
               "*/go.mod", "*/Cargo.toml", "*/Gemfile", "*/pubspec.yaml", "*/build.gradle", "*/build.gradle.kts",
               "*/pom.xml", "*/Podfile", "*/composer.json"],
     "commands": ["npm install *", "npm i *", "npm add *", "yarn add *", "pnpm add *", "pip install *",
                  "pip3 install *", "python -m pip install *", "python3 -m pip install *", "uv add *",
                  "uv pip install *", "poetry add *", "cargo add *", "go get *", "gem install *", "bundle add *",
                  "flutter pub add *", "dart pub add *", "brew install *"]},
    {"category": 7, "paths": ["*/migrations/*", "*/db/migrate/*"]},
    {"category": 19, "commands": ["gh repo delete*", "gh release delete*", "terraform destroy*", "kubectl delete*",
                                  "aws s3 rm *", "aws s3 rb *"]},
    {"category": 20, "commands": ["npm publish*", "yarn publish*", "pnpm publish*", "twine upload*", "cargo publish*",
                                  "gem push*", "gh release create*", "docker push*", "kubectl apply*",
                                  "terraform apply*", "helm install*", "helm upgrade*", "fly deploy*",
                                  "firebase deploy*", "vercel*", "flutter pub publish*", "dart pub publish*"],
     # I35: side effects unknown = assumed (§12.3) until the user lists the tool in hard_block.allow_tools
     "tools": ["mcp__*"]},
]


@dataclass
class Hit:
    category: int
    kind: str  # paths | commands | tools
    values: list  # what matched: a path's absolute and qualified forms, one command segment, or the tool name
    pattern: str

    def reason(self) -> str:
        return (f"{self.values[-1]} 命中硬拦截（授权表 #{self.category}，规则 `{self.pattern}`），没有有效的放行凭据；"
                "已登记待决请求，获批签发凭据前不要绕过")


@dataclass
class Verdict:
    foremind: bool
    pending: list = field(default_factory=list)  # hard-block hits without a valid exemption
    exempted: list = field(default_factory=list)  # (exemption id, Hit)
    matrix: list = field(default_factory=list)  # writable-matrix denial reasons

    @property
    def reasons(self) -> list[str]:
        return [h.reason() for h in self.pending] + self.matrix


def allowed_tools() -> list[str]:
    """`hard_block.allow_tools` from the user config file alone (I35): no project or task layer may widen it."""
    try:
        with open(user_config_dir() / "config.toml", "rb") as f:
            return _strs(tomllib.load(f).get("hard_block", {}).get("allow_tools"))
    except Exception:  # noqa: BLE001 — unreadable = nothing allowed (I38)
        return []


def lock_holder(root, batch) -> str | None:
    try:
        return lock.holder(root, batch)
    except (OSError, ValueError):  # unreadable lock: nobody provably holds it, so nobody writes
        return "（锁文件读不到）"


_SEP = re.compile(r"&&|\|\||\$\(|[;|&\n()`]")
_PREFIX = re.compile(r"^(?:(?:sudo|env|command|exec|nohup|time)\s+|[A-Za-z_]\w*=\S*\s+)*")


def segments(cmd: str) -> list[str]:
    """The command and each simple command in it, runs of whitespace as one space, without leading sudo/env/VAR=value.
    ponytail: splits on shell operators without parsing quotes; a quoted `;` gives a spurious segment (a false hit
    at worst). Use a real shell lexer if false hits start to hurt."""
    parts = (_PREFIX.sub("", " ".join(p.split())) for p in (cmd, *_SEP.split(cmd)))
    return list(dict.fromkeys(p for p in parts if p))


def system_glob(path) -> str | None:
    """The #22 glob `path` (absolute, realpath'd) falls under, else None."""
    globs = (*SYSTEM_GLOBS, os.path.realpath(user_config_dir()).lower() + "/*")
    return next((g for g in globs if fnmatchcase(path.lower(), g)), None)


def edit_path(tool, tool_input, cwd) -> str | None:
    """The absolute path an Edit/Write-class tool writes, else None."""
    return _abs(tool_input.get(EDIT_TOOLS[tool]), cwd) if tool in EDIT_TOOLS else None


def _abs(p, cwd) -> str | None:
    if not isinstance(p, str) or not p:
        return None
    return os.path.realpath(os.path.join(os.path.expanduser(cwd or "/"), os.path.expanduser(p)))


def _rel(path, base) -> str | None:
    base = os.path.realpath(base).rstrip("/") + "/"
    return path[len(base):] if path.startswith(base) else None


def locate(path, root, batch, repos, cfg=None) -> tuple[str, str | None]:
    """("worktree", "<repo>:<path>") inside this batch's worktrees; else "other_worktree" (also the batch dir's own
    files and `_`-dirs such as `_ro/`: no repo id starts with `_`, I28), ("repo", qualified) for a main checkout,
    "project", or "outside"."""
    if batch and (rel := _rel(path, worktree.batch_dir(project_slug(root, cfg or {}), batch))) is not None:
        repo, _, sub = rel.partition("/")
        return ("worktree", f"{repo}:{sub}") if sub and not repo.startswith("_") else ("other_worktree", None)
    if _rel(path, worktree.wt_root()) is not None:
        return "other_worktree", None
    for r in repos:
        if (rel := _rel(path, r.path)) is not None:
            return "repo", f"{r.id}:{rel}"
    return ("project" if _rel(path, root) is not None else "outside"), None


def _strs(v) -> list:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def active_patterns(cfg: dict) -> list[dict]:
    """Built-in patterns, plus configured `hard_block.patterns` entries ({category, paths?, commands?, tools?}) whose
    category is a built-in one or listed in `hard_block.categories` (a batch header's hard_block adds to it)."""
    cats, pats = cfg.get("hard_block.categories"), cfg.get("hard_block.patterns")
    on = {p["category"] for p in DEFAULT_PATTERNS} | {c for c in (cats if isinstance(cats, list) else ())
                                                      if isinstance(c, int)}
    extra = [p for p in (pats if isinstance(pats, list) else ())
             if isinstance(p, dict) and isinstance(p.get("category"), int) and p["category"] in on]
    return [*DEFAULT_PATTERNS, *extra]


def _match(patterns, kind, groups) -> list[Hit]:
    hits, seen = [], set()
    for g in groups:
        for p in patterns:
            pat = next((x for x in _strs(p.get(kind)) if any(fnmatchcase(v, x) for v in g)), None)
            if pat and (tuple(g), p["category"]) not in seen:
                seen.add((tuple(g), p["category"]))
                hits.append(Hit(p["category"], kind, g, pat))
    return hits


def _matrix(path, where, qual, *, root, session, batch, role, header) -> list[str]:
    if where == "outside":
        return []
    if not batch or role not in (None, "seat"):
        who = f"角色 {role}" if role not in (None, "seat") else "没有批次的会话"
        return [f"{path}：{who}不写 worktree 与产品代码（可写矩阵 §2.3）；改动交给席位或经对应的 foremind 命令"]
    if where != "worktree":
        return [f"{path} 不在本批 {batch} 的 worktree 内；席位只写本批 worktree 里的 owns_paths"]
    out = []
    owns = _strs((header or {}).get("owns_paths"))
    if header is not None and not (qual and any(fnmatchcase(qual, p) or fnmatchcase(qual, p.rstrip("/") + "/*")
                                                for p in owns)):
        out.append(f"{qual or path} 不在本批 owns_paths（{'、'.join(owns) or '空'}）内；需要写就执行 "
                   "`foremind decide --new` 申请扩大范围（#8），获批前不要绕路")
    if (holder := lock_holder(root, batch)) != session:
        out.append(f"批次 {batch} 的锁持有者是 {holder or '（无）'}，不是本会话 {session}；只有持锁会话能写，"
                   "接手经 `foremind handoff --accept`")
    return out


def evaluate(tool, tool_input, cwd, *, root=None, session=None, batch=None, role=None, header=None, cfg=None,
             repos=(), now=None) -> Verdict:
    """`session` None = not a Foremind session. `header` None = batch header unreadable: the owns_paths check is
    skipped and no exemption applies (I38); pass {} for sessions without a batch."""
    v, hits = Verdict(session is not None), []
    path = edit_path(tool, tool_input, cwd)
    where, qual = locate(path, root, batch, repos, cfg) if path and root else ("outside", None)
    state = bool(path) and fnmatchcase(path.lower(), STATE_GLOB)
    forms = [path, qual] if qual else [path]
    if path and (g := system_glob(path)):
        state = False
        if v.foremind:
            hits.append(Hit(22, "paths", forms, g))
        elif g == DELIVERY_GLOB:
            v.matrix = [DELIVERY_REASON.format(path=path)]
    if v.foremind:
        pats = active_patterns(cfg or {})
        if path:
            hits += _match(pats, "paths", [forms])
        if tool == "Bash":
            hits += _match(pats, "commands", [[s] for s in segments(str(tool_input.get("command") or ""))])
        tool_hits = _match(pats, "tools", [[tool]])
        if tool_hits and (allow := allowed_tools()):
            tool_hits = [h for h in tool_hits if h.category != 20 or not any(fnmatchcase(tool, g) for g in allow)]
        hits += tool_hits
    for h in hits:
        q = (exemptions.find(root, batch, h.kind, h.values, category=h.category, header=header, now=now)
             if v.foremind and root and header is not None else None)  # unreadable header: maybe delivered
        if q:
            v.exempted.append((q, h))
        else:
            v.pending.append(h)
    if state:
        v.matrix = [STATE_REASON.format(path=path)]
    elif v.foremind and path:
        v.matrix = _matrix(path, where, qual, root=root, session=session, batch=batch, role=role, header=header)
    return v
