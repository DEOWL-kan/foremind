"""`foremind doctor [--rescan]` (DESIGN §13.4): check every piece, with a fix command per failure; exit 0 when all
green, else 1. `--rescan` first redoes the delivery proposal (§7.5) and asks before rewriting delivery.toml;
`--rescan --yes` keeps what delivery.toml has (saying where the detection now differs) and takes the proposal for the
rest. A WARN line is told, not counted.

The packaged templates (REQ-1), the install manifests and the hooks' interpreter and package come first (REQ-6): a
hook whose python is gone or cannot import foremind fails silently in Claude Code. Each interpreter, PYTHONPATH and -P
of our command lines is probed with a fixed argv in the directory the hooks run in (settings.locate), never the line
itself, and compared with this CLI's; a working install elsewhere is reported too, with the upgrade commands of this
CLI, and the other install is left alone. A manifest is never rewritten here, an older format nor a newer one; the
hooks of a directory whose manifest a newer Foremind wrote are that one's, not checked here.
`[project].name` against running batches (§20 I39): their locks and worktrees carry the slug, so a changed name is an
error to fix by hand, never rewritten here. Checks only: nothing is created.
REQ-5: an unfinished or broken upgrade, and a project that still needs one (a manifest of format 0, hooks recorded for
another Foremind), are told with the exact commands to finish or undo it; while one is, no fix offers `foremind init`
(init is refused then, and would go round the upgrade)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import foremind
from foremind import config, gate, install, lock, review, seat, upgrade, worktree
from foremind import header as hdr
from foremind import repos as repos_mod
from foremind.defaults import TABLE
from foremind.install import detect, settings
from foremind.paths import ProjectNotFound, find_project_root, state_dir, user_config_dir
from foremind.schemas import BATCH_ID
from foremind.supervisor.tick import supervisor_state
from foremind.vendors import claude


def register(sub):
    p = sub.add_parser("doctor", help="check the installation; each failure comes with its fix")
    p.add_argument("--rescan", action="store_true", help="redo the delivery proposal and confirm delivery.toml")
    p.add_argument("--yes", action="store_true",
                   help="with --rescan: keep what delivery.toml has, take the proposal for the rest")
    p.set_defaults(func=_run)


def _check_templates(add):
    """REQ-1: the ten packaged templates there and not blank."""
    bad = []
    for name in foremind.TEMPLATES:
        try:
            foremind.template(name)
        except ValueError as e:
            bad.append(str(e))
    add("packaged templates", not bad, "; ".join(bad) or f"{len(foremind.TEMPLATES)} readable",
        "reinstall Foremind from a complete wheel: pip install --no-index --force-reinstall <wheel>")


def _check_manifests(add, root, dirs) -> set[Path]:
    """REQ-6: every install manifest readable as one this Foremind knows (read only: never rewritten here), and what
    each records still in place. Returns the manifests a newer Foremind wrote."""
    found, bad, future = settings.scan(root), 0, set()
    for m, r in found:
        if isinstance(r, settings.ManifestError):
            bad += 1
            if r.kind == "future":
                future.add(m)
            add(f"install manifest {m.name}", False, str(r),
                "use the Foremind that wrote it, or upgrade this one; this one leaves the manifest as it is"
                if r.kind == "future" else
                "fix it, or remove Foremind's hooks and status line from the settings file it is for yourself and "
                "delete it; then foremind init --yes")
    for d in dirs:
        try:
            gone = settings.conflicts(root, d)
        except (settings.SettingsError, OSError):
            continue  # an unreadable manifest above, an unreadable settings file in the hooks check
        if gone:
            bad += 1
            add(f"install record matches {settings.path(d)}", False, "; ".join(gone),
                f"put them back as recorded, or remove Foremind's remaining entries from {settings.path(d)} yourself "
                f"and delete {settings.manifest_path(root, d)}; then foremind init --yes")
    if not bad:
        vs = [r[0] for _, r in found]
        add("install manifests", True, f"{len(vs)} readable" + (
            f" (format_version {', '.join(f'{v}: {vs.count(v)}' for v in sorted(set(vs)))}; 0 = none)" if vs else ""))
    return future


def _check_hooks(add, root, dirs, newer):
    """REQ-6: which Foremind the hooks of each directory run, against this CLI's interpreter, PYTHONPATH and package.
    `newer`: a newer Foremind's manifest is there (its hooks are its own: none found is not this one's to say)."""
    py, pkg, pp = settings.expected()
    this = f"{py}, PYTHONPATH {pp}, package {pkg}"
    move = (f"{claude.foremind_command('upgrade', '--check')}, then {claude.foremind_command('upgrade')}: moves the "
            "recorded hooks to this CLI; the other installation is left as it is")
    found = False
    for d in dirs:
        try:
            if not (cmds := settings.installed_commands(d)):
                continue
        except (settings.SettingsError, OSError):
            continue  # the hooks check says why
        found, probs = True, settings.locate(d)
        unrecorded = not settings.manifest_path(root, d).exists() or any(settings.target(c) is None for c in cmds)
        add(f"hooks in {settings.path(d)} run this Foremind", not probs,
            "; ".join(probs) + f" (this CLI: {this})" if probs else this,
            "no install record covers these lines (or one is not in a shape Foremind writes): check them and remove "
            "them yourself; then foremind init --yes" if unrecorded else move)
    if not found and not newer:
        add("钩子命令", False, "没有找到已装的 foremind 钩子", "foremind init --yes")


def _check_upgrade(add, root) -> bool:
    """REQ-5: the upgrade state of the project; True when it blocks every write."""
    run, check = claude.foremind_command("upgrade"), claude.foremind_command("upgrade", "--check")
    mk, need = upgrade.marker(root), upgrade.pending(root)
    if mk and mk["broken"]:
        add("upgrade marker readable", False, f"{mk['path']}: {mk['broken']}; every write is refused until it is "
            "resolved", "\n       ".join(upgrade.recovery(root)))
    elif mk:
        add("no unfinished upgrade", False, f"{mk['path']} (transaction {mk['transaction']}, backup {mk['backup']}); "
            "every write is refused until it is finished", f"{run} (it finishes the upgrade, or says what stops it; "
            f"{check} lists it first); or undo it by hand:\n       " + "\n       ".join(upgrade.recovery(root)))
    if need:
        add("project up to date with this Foremind", False, "; ".join(need) + "; every write is refused until it is "
            "upgraded", f"{check}, then {run} (it stops nothing: stop what --check lists yourself)")
    elif not mk:
        add("project up to date with this Foremind", True)
    return bool(mk or need)


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


def _checks_branch(root, rid) -> str | None:
    """The branch the proposal read the repo's required checks off: detect_repo reads branch protection on the target
    branch it records as facts.target_branch (its evidence URL names the same branch); None when it does not say."""
    try:
        prop = json.loads((state_dir(root) / "delivery.proposal.json").read_text(encoding="utf-8"))
        v = prop["repos"][rid]["facts"]["target_branch"]["value"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return v if isinstance(v, str) and v != detect.UNKNOWN else None


def checks(root) -> list[tuple[str, bool | None, str, str]]:
    """(name, ok, detail, fix); ok None = a warning."""
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

    _check_templates(add)
    future = _check_manifests(add, root, dirs)
    live = [d for d in dirs if settings.manifest_path(root, d) not in future]  # a newer Foremind's are its own
    _check_hooks(add, root, live, bool(future))
    held = _check_upgrade(add, root)
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
    kind = (cfg or {}).get("carrier.kind", TABLE["carrier.kind"])
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
        # #23 the user's: the gate takes local checks in place of CI whatever [gate].ci says
        local = [r.id for r in repos if review.ci_mode(cfg, r.id) == "none" or not review.push_by_system(cfg, r.id)]
        bare = local if not cfg.get("gate.checks") else []
        add("ci = none 或 #23 归用户的仓库有 [gate].checks", not bare,
            f"门禁只认本地检查，而 [gate].checks 为空，永远不过：{bare}" if bare else "",
            "在 foremind.toml 或 .foremind/config.toml 写 [gate] checks = [\"<本地检查命令>\"]；"
            "或 foremind doctor --rescan：ci 改成 required / local_first，且推送与开 PR（#23）归系统")
        waits = [r for r in repos if r.id not in local and review.ci_mode(cfg, r.id) in ("required", "local_first")]
        # external CI the branch protection requires; foremind/gate is the gate's own status, which _ci never waits for
        noci = [r.id for r in waits if not any((Path(r.path) / ".github" / "workflows").glob("*.y*ml"))  # as detect
                and not set(gate.required_checks(root, r.id) or ()) - {gate.GATE_CONTEXT}]
        add("ci = required / local_first 的仓库有 CI", not noci,
            f"没检测到 .github/workflows/*.yml，分支保护也没有（foremind/gate 以外的）必过检查，门禁会一直等不来的 CI：{noci}"
            if noci else "",
            "交互运行 foremind doctor --rescan 把 ci 改成 none，并在 foremind.toml 或 .foremind/config.toml 写 "
            "[gate] checks = [\"<本地检查命令>\"]；或给仓库加 CI 工作流")
        other = []  # the gate waits for the required checks read off one branch while it delivers to another
        for r in waits:
            if gate.required_checks(root, r.id) is None:
                continue
            src, tgt = _checks_branch(root, r.id), review.repo_cfg(cfg, r.id, "target_branch") or r.default_branch
            if src is None or src != tgt:
                other.append(f"{r.id}（必过检查取自 {src or '来源分支未知'}，目标分支 {tgt or '未知'}）")
        if other:
            add("必过检查取自门禁的目标分支", None, f"门禁按这份必过检查等 CI，目标分支的保护规则可能不同：{other}",
                "核对目标分支的分支保护；foremind doctor --rescan 只读默认分支的保护规则")
        stuck = [r.id for r in repos if review.repo_cfg(cfg, r.id, "level") == "merge_dev"
                 and not (review.repo_cfg(cfg, r.id, "merge_method") or review.repo_cfg(cfg, r.id, "merge_command"))]
        add("merge_dev 的仓库有合入方式", not stuck,
            f"level = merge_dev 而没有 merge_method 或 merge_command，合入时才会报错：{stuck}" if stuck else "",
            "交互运行 foremind doctor --rescan 选 merge_method；或在 foremind.toml 的 [delivery.repo.<id>] 写 "
            "merge_command")

    for d in live:
        probs = settings.check(d)
        add(f"钩子已装 {settings.path(d)}", not probs, "; ".join(probs), "foremind init --yes" + (
            "; the status line of the project's .claude/settings.json is wrapped only with your consent: run "
            "foremind init interactively" if settings.wrap_asks(d) else ""))
        if (local := settings.local_statusline(d)) is not None:  # L384: kept as the user chose, never wrapped
            add(f"local status line in {settings.path(d)}", None,
                f"{local!r} stays; sessions in this directory report no quota telemetry",
                "nothing to do: Foremind leaves a local status line as it is")
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
    st, rec = supervisor_state(root)
    if st == "stopped" and rec:  # REQ-12: the recorded pid is gone, or another process has it now (D23)
        add("监督进程在运行", None, f"监督进程未运行（supervisor.json 记的 pid {rec.get('pid')} 已退出或已被别的进程复用）",
            "foremind supervise")
    elif st != "stopped":  # finding 23: it re-executes itself unless the new code failed to run
        add("监督进程代码与当前包一致", st == "current", f"监督进程代码旧于当前包（pid {rec.get('pid')}）" if st == "stale" else "",
            "新代码试跑失败时监督进程会留在旧代码：看 events.jsonl 的 supervisor_reexec_failed，修好后它会再试；"
            "或停掉后重新运行 foremind supervise")
    if cfg is not None:
        _check_slug(add, root, cfg)
    try:
        if str(root.resolve()) not in install.registered():
            add("已登记到 projects", False, str(install.registry_path()), "foremind init --yes")
    except (install.InstallError, OSError) as e:
        add("已登记到 projects", False, str(e), f"修好或移走 {install.registry_path()} 后 foremind init --yes")
    if held:  # init is refused while the upgrade blocks the project: never offer it as a way round
        out = [(n, ok, d, "finish the upgrade first (see above); then rerun foremind doctor" if "foremind init" in f
                else f) for n, ok, d, f in out]
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
        print(f"{'ok  ' if ok else 'WARN' if ok is None else 'FAIL'} {name}" + (f"：{detail}" if detail else ""))
        if not ok:
            bad += ok is not None
            print(f"     {'建议' if ok is None else '修复'}：{fix}")
    print("全部通过" if not bad else f"{bad} 项未通过")
    return 1 if bad else 0
