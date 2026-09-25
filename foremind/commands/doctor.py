"""`foremind doctor [--rescan]` (DESIGN §13.4): check every piece, with a fix command per failure; exit 0 when all
green, else 1. `--rescan` first redoes the delivery proposal (§7.5) and asks before rewriting delivery.toml;
`--rescan --yes` keeps what delivery.toml has (saying where the detection now differs) and takes the proposal for the
rest.

The hook interpreter and its package path come first: a hook whose python is gone or cannot import foremind fails
silently in Claude Code. They are probed with the installed command line itself (`version` for the subcommand), in
the directory the hooks run in. `[project].name` against running batches (§20 I39): their locks and worktrees carry
the slug, so a changed name is an error to fix by hand, never rewritten here. Checks only: nothing is created."""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib

from foremind import config, install, lock, review, seat, worktree
from foremind import header as hdr
from foremind import repos as repos_mod
from foremind.install import detect, settings
from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir
from foremind.schemas import BATCH_ID


def register(sub):
    p = sub.add_parser("doctor", help="check the installation; each failure comes with its fix")
    p.add_argument("--rescan", action="store_true", help="redo the delivery proposal and confirm delivery.toml")
    p.add_argument("--yes", action="store_true",
                   help="with --rescan: keep what delivery.toml has, take the proposal for the rest")
    p.set_defaults(func=_run)


def _py(cmd) -> tuple[str, dict]:
    """(interpreter, extra env) of one of our hook command lines: `PYTHONPATH=<dir> <python> -m foremind …`."""
    words, env = shlex.split(cmd), {}
    while words and "=" in words[0] and not words[0].startswith("/"):
        k, _, v = words.pop(0).partition("=")
        env[k] = v
    return (words[0] if words else ""), env


def _probe(cmd, **kw) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, **kw)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def _check_interpreters(add, dirs):
    runs = {}  # our command line with `version` for its subcommand -> a directory it runs in
    for d in dirs:
        for c in _quiet(settings.installed_commands, d):
            runs.setdefault(settings.OURS.sub(" -m foremind version", c), d)
    pys = {}
    for run, d in sorted(runs.items()):
        py, env = _py(run)
        if py not in pys:
            pys[py] = _probe([py, "-c", "import sys; print(sys.version.split()[0]); sys.exit(sys.version_info < (3, 11))"])
            add(f"钩子解释器 {py}", pys[py][0], pys[py][1] or "不存在",
                "foremind init --yes（用 3.11+ 的解释器运行 foremind 重装钩子）")
        if pys[py][0]:
            ok, out = _probe(run, shell=True, cwd=d)
            add(f"钩子能导入 foremind（{env.get('PYTHONPATH', '无 PYTHONPATH')}）", ok,
                "" if ok else (out.splitlines() or [""])[-1][-300:],
                "foremind init --yes（包路径变了，或钩子没带 -P 被当前目录的 foremind/ 遮蔽：从现在的安装位置重装钩子）")
    if not runs:
        add("钩子命令", False, "没有找到已装的 foremind 钩子", "foremind init --yes")


def _quiet(fn, *a):
    try:
        return fn(*a)
    except (settings.SettingsError, OSError):
        return []


def _started(root) -> list[tuple[str, dict]]:
    out = []
    for p in sorted((state_dir(root) / "batches").glob("*.md")):
        if not re.fullmatch(BATCH_ID, p.stem):
            continue
        try:
            h, _ = hdr.parse(p.read_text(encoding="utf-8"))
        except (OSError, hdr.HeaderError):
            continue
        if h.get("state") in seat.STARTED or h.get("state") in ("updating", "blocked", "stuck", "paused"):
            out.append((p.stem, h))
    return out


def _check_slug(add, root, cfg):
    slug = seat.project_slug(root, cfg)
    suffix = slug.rsplit("-", 1)[1]
    bad = []
    for bid, _ in _started(root):
        holder = lock.holder(root, bid)
        if holder not in (None, lock.USER) and not holder.startswith(f"fm-{slug}-"):
            bad.append(f"{bid}（锁持有者 {holder}）")
        elif not worktree.batch_dir(slug, bid).exists() and \
                [d for d in worktree.wt_root().glob(f"*-{suffix}/{bid}") if d.parent.name != slug]:
            bad.append(f"{bid}（worktree 在别的 slug 下）")
    add("[project].name 与在途批次一致", not bad, f"slug {slug}；不一致：{bad}" if bad else f"slug {slug}",
        "把 [project].name 改回在途批次开跑时的名字（不自动改：会话名、worktree 路径都依赖它）")


def checks(root) -> list[tuple[str, bool, str, str]]:
    out = []

    def add(name, ok, detail="", fix=""):
        out.append((name, ok, detail, fix))

    cfg = None
    try:
        cfg = config.load(root)
        add("配置能解析", True)
    except config.ConfigError as e:
        add("配置能解析", False, str(e), "按报错改对应的配置文件")
    repos = []
    if cfg is not None:
        try:
            repos = repos_mod.load_repos(root, cfg)
            add("仓库登记", bool(repos), ", ".join(f"{r.id}={r.path}" for r in repos) or "没有登记仓库",
                "foremind init --repo ID=PATH")
        except config.ConfigError as e:
            add("仓库登记", False, str(e), "改 [[repos]]")
    dirs = install.settings_dirs(root, repos)

    _check_interpreters(add, dirs)
    add("git", bool(shutil.which("git")), shutil.which("git") or "PATH 里没有", "安装 git")
    github = any("github.com" in (install.git(r.path, "remote", "get-url", r.remote or "origin") or "") for r in repos)
    if github or cfg and any(v == "system" for k, v in cfg.items() if k.endswith(".push_pr")):
        try:
            ok = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=30,
                                stdin=subprocess.DEVNULL).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        add("gh 已登录", ok, "", "安装 gh 并运行 gh auth login")
    else:
        add("gh 已登录", True, "不需要：没有 GitHub 远端，推送与开 PR 也不归系统")
    add("claude 在 PATH", bool(shutil.which("claude")), shutil.which("claude") or "",
        "安装 Claude Code：https://docs.claude.com/claude-code")
    kind = (cfg or {}).get("carrier.kind", seat.DEFAULTS["carrier.kind"])
    add(f"承载 {kind} 在 PATH", kind == "manual" or bool(shutil.which(kind)), shutil.which(kind) or "",
        f"安装 {kind}，或在 .foremind/config.toml 的 [carrier] kind 换一个")

    dp = state_dir(root) / "delivery.toml"
    try:
        with open(dp, "rb") as f:
            got = tomllib.load(f).get("delivery", {}).get("repo", {})
        missing = [r.id for r in repos if not {"level", "push_pr"} <= set(got.get(r.id, {}))]
        extra = sorted(set(got) - {r.id for r in repos})
        odd = [f"{rid}.{k}" for rid, v in got.items() for k in v if k not in detect.DELIVERY_KEYS]
        add("delivery.toml 合法", not (missing or extra or odd),
            "; ".join(x for x in (missing and f"缺仓库 {missing}", extra and f"未登记的仓库 {extra}",
                                  odd and f"未知键 {odd}") if x), "foremind doctor --rescan")
    except FileNotFoundError:
        add("delivery.toml 存在", False, str(dp), "foremind doctor --rescan")
    except (OSError, ValueError, AttributeError) as e:
        add("delivery.toml 合法", False, str(e), "foremind doctor --rescan")

    if cfg is not None:
        bare = [r.id for r in repos if review.ci_mode(cfg, r.id) == "none"] if not cfg.get("gate.checks") else []
        add("ci = none 的仓库有 [gate].checks", not bare, f"ci = none 而没有本地检查，门禁永远不过：{bare}" if bare else "",
            "在 foremind.toml 或 .foremind/config.toml 写 [gate] checks = [\"<本地检查命令>\"]；"
            "或 foremind doctor --rescan 把 ci 改成 required / local_first")
        stuck = [r.id for r in repos if review.repo_cfg(cfg, r.id, "level") == "merge_dev"
                 and not (review.repo_cfg(cfg, r.id, "merge_method") or review.repo_cfg(cfg, r.id, "merge_command"))]
        add("merge_dev 的仓库有合入方式", not stuck,
            f"level = merge_dev 而没有 merge_method 或 merge_command，合入时才会报错：{stuck}" if stuck else "",
            "交互运行 foremind doctor --rescan 选 merge_method；或在 foremind.toml 的 [delivery.repo.<id>] 写 "
            "merge_command")

    for d in dirs:
        probs = settings.check(d)
        add(f"钩子已装 {settings.path(d)}", not probs, "; ".join(probs),
            "foremind init --yes（状态栏来自项目或本地 settings 时：交互运行 foremind init 并同意串联）")
    tracked = install.tracked_settings(dirs)
    add("settings.local.json 未被 git 跟踪", not tracked, ", ".join(map(str, tracked)),
        "git rm --cached <文件> 并提交（钩子里有本机路径，不该共享；本地文件留着）")
    if install.toplevel(root) is not None:
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", ".foremind/"],
                                 capture_output=True).returncode == 0
        add(".foremind/ 被 git 忽略", ignored, "", "foremind init --yes（写 .foremind/.gitignore 与 .git/info/exclude）")

    sd = state_dir(root)
    add("状态目录可写", sd.is_dir() and os.access(sd, os.W_OK | os.X_OK), str(sd),
        f"不存在就 foremind init --yes；否则检查 {sd} 的权限")
    lp = user_config_dir() / "supervisor.lock"  # not taken: a supervisor starting now must not see it held
    add("supervisor.lock 可取", not lp.exists() or os.access(lp, os.R_OK | os.W_OK), str(lp),
        f"检查 {lp} 的权限")
    if cfg is not None:
        _check_slug(add, root, cfg)
    try:
        if str(root.resolve()) not in install.registered():
            add("已登记到 projects", False, str(install.registry_path()), "foremind init --yes")
    except (install.InstallError, OSError) as e:
        add("已登记到 projects", False, str(e), f"修好或移走 {install.registry_path()} 后 foremind init --yes")
    return out


def _run(args):
    try:
        root = find_project_root()
    except ProjectNotFound as e:
        print(f"foremind doctor: {e}；先运行 foremind init", file=sys.stderr)
        return 1
    if args.rescan:
        from foremind.commands.init import confirm_delivery
        interactive = not args.yes
        if interactive and not sys.stdin.isatty():
            print("foremind doctor --rescan: 非交互运行请加 --yes", file=sys.stderr)
            return 2
        try:
            confirm_delivery(root, install.project_repos(root), interactive)
        except (config.ConfigError, install.InstallError, OSError) as e:
            print(f"foremind doctor: {e}", file=sys.stderr)
            return 1
    bad = 0
    for name, ok, detail, fix in checks(root):
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + (f"：{detail}" if detail else ""))
        if not ok:
            bad += 1
            print(f"     修复：{fix}")
    print("全部通过" if not bad else f"{bad} 项未通过")
    return 1 if bad else 0
