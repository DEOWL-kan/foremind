"""`foremind init` (DESIGN §13.2–13.3, §1.3, §1.8, §7.5): detect the environment, register the repos, propose the
delivery conventions and write delivery.toml once confirmed, create .foremind/, merge the hooks, add the project to
the user-level registry. Interactive; every question has a flag, and `--yes` takes the defaults, keeps what delivery.toml
has and takes the proposal for the rest (unanswered = strictest). Only the "this project" scope for now: global is M3-1, `--codex` M2-7.
A rerun (doctor's fix) starts from what is registered (§20 I53③): name, repos, carrier, notify and delivery.toml."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

from foremind import install
from foremind.config import ConfigError
from foremind.install import detect, settings, tomlblock
from foremind.paths import ProjectNotFound, find_project_root
from foremind.repos import Repo, load_repos
from foremind.schemas import REPO_ID

CARRIERS = ("orca", "tmux", "manual")  # herdr is detected, no adapter yet
NOTIFY = ("none", "ntfy")


def register(sub):
    p = sub.add_parser("init", help="set Foremind up in a project: repos, delivery conventions, hooks")
    p.add_argument("--repo", action="append", default=[], metavar="ID=PATH",
                   help="register a repo (repeatable); default: the git repo around the current directory as `main`")
    p.add_argument("--root", help="project root (default: the set-up project around the current directory, else the "
                                  "repo's root with one repo, else the current directory)")
    p.add_argument("--name", help="[project].name, the project slug's source (default: the registered one, else the "
                                  "root directory's name)")
    p.add_argument("--scope", choices=["project", "global"], default="project")
    p.add_argument("--carrier", choices=[*CARRIERS, "herdr"])
    p.add_argument("--notify", choices=NOTIFY)
    p.add_argument("--codex", action="store_true", help="also install the Codex integration (not yet: M2-7)")
    p.add_argument("--yes", action="store_true", help="non-interactive: the defaults; delivery.toml keeps what it has, "
                                                      "the rest from the proposal")
    p.set_defaults(func=_run)


def ask(interactive, question, default, choices=None):
    """`default` None = may be skipped (returns None)."""
    if not interactive:
        return default
    while True:
        try:
            a = input(f"{question} [{default if default is not None else '跳过'}]: ").strip() or default
        except EOFError:
            raise install.InstallError("输入已结束（EOF）：非交互运行请加 --yes") from None
        if a is None or not choices or a in choices:
            return a
        print(f"  可选：{', '.join(choices)}")


def _which(names) -> list[str]:
    return [n for n in names if shutil.which(n)]


def _gh_auth() -> str:
    if not shutil.which("gh"):
        return "未安装"
    try:
        rc = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL).returncode
    except (OSError, subprocess.TimeoutExpired):
        rc = 1
    return "已登录" if rc == 0 else "未登录（gh auth login）"


def _repos(args) -> tuple[Path, list[Repo] | None]:
    """(project root, repos from --repo, or None = from the user's config / the current repo)."""
    specs = []
    for s in args.repo:
        rid, sep, path = s.partition("=")
        if not sep or not re.fullmatch(REPO_ID, rid) or not path:
            raise install.InstallError(f"--repo {s!r}: write ID=PATH, ID matching [A-Za-z0-9][A-Za-z0-9_-]*")
        p = Path(path).expanduser().resolve()
        if install.toplevel(p) != p:
            raise install.InstallError(f"--repo {s!r}: {p} is not the root of a git repo")
        if rid in [r.id for r in specs]:
            raise install.InstallError(f"--repo: id {rid!r} twice")
        specs.append(Repo(rid, p, "origin" if install.git(p, "remote", "get-url", "origin") is not None else None))
    if args.root:
        root = Path(args.root).expanduser().resolve()
    elif len(specs) == 1:
        root = specs[0].path
    elif specs:
        root = Path.cwd().resolve()
    else:
        try:
            root = find_project_root()  # a rerun, maybe from inside one of the repos
        except ProjectNotFound:
            root = install.toplevel(Path.cwd()) or Path.cwd().resolve()
    return root, specs or None


def confirm_delivery(root, repos, interactive) -> dict:
    """Detect, show the proposal with its evidence, take the answers (what delivery.toml has are the defaults), write
    delivery.toml unless the user says no. Returns the proposal."""
    prop = detect.build(root, repos)
    answers = detect.confirmed(root)
    for rid, r in prop["repos"].items():
        print(f"\n交付约定提案 · {rid}（证据见 .foremind/delivery.proposal.json）")
        for k, x in {**r["facts"], **r["proposal"]}.items():
            print(f"  {k}: {x['value']}  ← {x['evidence']}")
        a = answers.setdefault(rid, {})
        if not interactive:
            continue
        f, cur = r["facts"], detect.resolve({"repos": {rid: r}}, {rid: a})[rid]
        can = lambda k: f[k]["value"] is not False  # noqa: E731
        a["level"] = ask(True, f"{rid} 交付级别（done=做完即可 / merge_dev=合入开发分支）", cur["level"],
                         ["done", "merge_dev"] if can("can_merge") else ["done"])
        a["push_pr"] = ask(True, f"{rid} 推送与开 PR（#23）归谁（user / system）", cur["push_pr"],
                           ["user", "system"] if can("can_push") else ["user"])
        a["target_branch"] = ask(True, f"{rid} 目标分支 target_branch", cur.get("target_branch"))
        a["merge_method"] = ask(True, f"{rid} 合入方式 merge_method", cur.get("merge_method"),
                                detect.allowed_methods(f))
        a["ci"] = ask(True, f"{rid} CI（required=等 CI / local_first=审查轮次先跑本地检查 / none=只跑 [gate].checks）",
                      cur["ci"], list(detect.CI_MODES))
    resolved = detect.resolve(prop, answers)
    for rid, a in answers.items():
        for k, x in a.items():
            if x is None or rid not in resolved:
                continue
            if resolved[rid].get(k) != x:
                print(f"注意：{rid}.{k} = {x!r} 与检测到的事实不符，改为 {resolved[rid].get(k)!r}")
            elif not interactive and (y :=prop["repos"][rid]["proposal"].get(k, {}).get(
                    "value", detect.UNKNOWN)) not in (detect.UNKNOWN, x):
                print(f"保留 {rid}.{k} = {x}（这次检测到 {y}）；要改请交互运行 foremind doctor --rescan")
    if interactive and ask(True, "写入 .foremind/delivery.toml？(y/n)", "y", ["y", "n"]) != "y":
        print("没有写 delivery.toml；之后用 `foremind doctor --rescan` 重新生成")
        return prop
    print(f"\n写入 {detect.write_delivery(root, resolved)}")
    return prop


def _run(args):
    if args.scope == "global":
        print("foremind init: 全局范围（用户级钩子、所有项目自动启用）在 M3-1 实现；现在只支持 --scope project",
              file=sys.stderr)
        return 2
    if args.codex:
        print("foremind init: Codex 集成（钩子信任、AGENTS 片段）在 M2-7 实现；去掉 --codex 只装 Claude Code 部分",
              file=sys.stderr)
        return 2
    if args.carrier == "herdr":
        print("foremind init: herdr 承载还没有适配器；请选 orca、tmux 或 manual", file=sys.stderr)
        return 2
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        print("foremind init: 非交互运行请加 --yes（每个问题都有对应参数）", file=sys.stderr)
        return 2
    if not shutil.which("git"):
        print("foremind init: 找不到 git", file=sys.stderr)
        return 1
    started = False
    try:
        root, specs = _repos(args)
        own, prev = install.user_part(root), install.our_block(root)
        if specs and "repos" in own:
            raise install.InstallError("你的项目配置里已有 [[repos]]：去掉 --repo，或在那里改")
        kept = None
        if not specs and "repos" not in own:
            if prev.get("repos"):
                kept = prev["repos"]  # registered by an earlier init
                specs = load_repos(root, {"repos": kept})
            elif install.toplevel(root) != root:
                raise install.InstallError(f"{root} 不是 git 仓库的根：用 --repo ID=PATH 登记仓库")
            else:
                specs = [Repo("main", root, "origin" if install.git(root, "remote", "get-url", "origin") is not None
                              else None)]
        repos = specs or load_repos(root, own)
        for r in repos:  # a moved repo: hooks written at the old path would leave doctor green and nothing guarded
            if install.toplevel(r.path) != r.path.resolve():
                raise install.InstallError(f"仓库 {r.id} 不在 {r.path}：用 --repo 重新登记（或改 [[repos]]）")
        dirs = install.settings_dirs(root, repos)
        if tracked := install.tracked_settings(dirs):
            raise install.InstallError(
                f"{', '.join(map(str, tracked))} 已被 git 跟踪：钩子里有本机的解释器路径，写进去会被提交给别人。"
                "先 git rm --cached 它并提交（本地文件留着），再重跑 init")
        for d in dirs:  # an unreadable file stops us before anything is written
            settings.installed_commands(d)
        settings.wrapped()
        install.registered()

        def registered(table, key, default, choices=None):
            v = next((d[table][key] for d in (own, prev) if isinstance(d.get(table), dict) and key in d[table]), None)
            return v if v is not None and (choices is None or v in choices) else default

        carriers = _which(("orca", "tmux", "herdr"))
        print(f"项目根：{root}")
        for r in repos:
            flows = r.path / ".github" / "workflows"
            print(f"  仓库 {r.id}: {r.path} · 远端 {r.remote or '无'} · CI {'有' if flows.is_dir() else '无'} .github/workflows")
        print(f"gh：{_gh_auth()} · agent：{', '.join(_which(('claude', 'codex'))) or '无'} · 承载：{', '.join(carriers) or '无'}")
        if not shutil.which("claude"):
            print("  注意：PATH 里没有 claude（Claude Code），席位开不起来")
        name = args.name or ask(interactive, "项目名 [project].name（决定项目 slug，批次开跑后别再改）",
                                registered("project", "name", root.name))
        carrier = args.carrier or ask(interactive, "承载（orca / tmux / manual）", registered(
            "carrier", "kind", next((c for c in carriers if c in CARRIERS), "manual"), CARRIERS), CARRIERS)
        notify = args.notify or ask(interactive, "通知（none / ntfy）", registered("notify", "channel", "none", NOTIFY),
                                    NOTIFY)

        started = True
        install.write_gitignore(root)  # before delivery.proposal.json: .foremind/ is ignored even if we stop midway
        prop = confirm_delivery(root, repos, interactive)
        entries = kept if kept is not None else None if not specs else [
            install.repo_entry(root, r.id, r.path, r.remote,
                               t if (t := prop["repos"][r.id]["facts"]["target_branch"]["value"]) != detect.UNKNOWN
                               else None) for r in specs]
        notes = install.write_project(root, name=name, carrier=carrier, notify=notify, repos=entries)
        install.write_excludes(root, dirs)
        consent = (lambda cmd, src: ask(True, f"状态栏 {cmd!r}（来自 {src}）要串联进用户层 [statusline] command，"
                                              "它会成为所有项目的状态栏；同意？(y/n)", "n", ["y", "n"]) == "y"
                   ) if interactive else None
        for d in dirs:
            notes += settings.install(root, d, consent)
        install.set_registered(root, True)
    except (install.InstallError, settings.SettingsError, tomlblock.BlockError, ConfigError, OSError) as e:
        print(f"foremind init: {e}", file=sys.stderr)
        if started:
            print("  已写入的部分保留着：修好后重跑 foremind init，或运行 foremind uninstall 移除", file=sys.stderr)
        return 1
    for n in notes:
        print(f"注意：{n}")
    print(f"钩子已装进 {', '.join(str(settings.path(d)) for d in dirs)}；项目已登记到 {install.registry_path()}")
    print("下一步：foremind doctor")
    return 0
