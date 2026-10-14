# Foremind：依赖、安装与升级（中文）

本页是 [README.md](README.md) 中 Requirements、Install、Upgrade 三段的中文对应内容；其余介绍见 README.md，设计正文见中文的 [`DESIGN.md`](DESIGN.md)。

## 依赖

- Python 3.11 或更新（只用标准库，Foremind 没有运行依赖），以及 `git`。
- [Claude Code](https://docs.claude.com/en/docs/claude-code)：以 `claude` 在 `PATH` 中，且已登录。席位、规划者、审查者等角色都是 Claude Code 会话，真实批次会消耗你的额度。
- 交互会话的承载：`tmux` 或 Orca（或 `manual`，每个会话由你自己启动）。首发平台是 macOS。
- `gh`（已 `gh auth login`）：登记的仓库有 GitHub 远端，或有仓库把推送与 PR 交给 Foremind（`push_pr = "system"`）时需要：此时 `foremind doctor` 要求它已登录，`init` 与 `doctor --rescan` 通过 `gh api` 读取远端设置和分支保护。两者都没有时不用。
- 构建 wheel 需要声明的构建后端 `setuptools>=61`。

第一批开跑前，`foremind doctor` 的检查要先通过，其中包括：

- **门禁检查**：ci 为 `none`，或推送与 PR 归你时，门禁只认本地检查：在 `foremind.toml` 或 `.foremind/config.toml` 写 `[gate] checks = ["<你的测试命令>"]`。列表为空永远不过。

以下各项 `foremind doctor` 不检查，需要你自己确认（它显示「全部通过」不代表这些已满足）：

- **无远端仓库的目标分支**：没有远端可读默认分支时要写明：在 `foremind.toml` 写 `[delivery.repo.<id>] target_branch = "main"`（或在该仓库的 `[[repos]]` 条目写 `default_branch`），否则开第一个席位时报错。
- **git 身份**：席位要提交，`git config user.name`、`user.email` 必须设置（全局或仓库内均可）。
- **信任目录**：Claude Code 对每个目录询问一次是否信任。Foremind 从不替你回答该对话框（或权限确认）：遇到这种屏幕它拒绝输入并提示你；在那个会话里回答后再重试。
- **权限模式**：席位与规划者以 `seat.permission_mode` 作为 `--permission-mode` 启动，未设置时为 `acceptEdits`；长期总控用 `controller.permission_mode`，未设置时同席位。Foremind 不替你改这些设置。

另外须知：

- `foremind land` 在项目锁内移动、回滚目标分支时，这些 Git 命令禁用钩子（命令行上的 `core.hooksPath=/dev/null`，不改你的 Git 配置）。你的 `post-merge`、`reference-transaction` 等钩子在这些移动中**不会**执行，你的主检出上也一样；依赖它们时请自己运行。

测试与 CI 使用假的 `claude`、`tmux`、`gh`：它们证明安装、钩子与提示正确，不代表真实会话。真实批次需要上面的真实工具和已登录的 Claude Code。

## 安装

Foremind 没有发布到 PyPI。从检出构建 wheel，再安装这个文件：

```sh
git clone https://github.com/DEOWL-kan/foremind.git
cd foremind
python3 -m pip wheel --no-deps -w dist .          # 或：uv build --wheel
python3 -m venv ~/.venvs/foremind                  # 检出目录之外的任意目录
~/.venvs/foremind/bin/pip install --no-index dist/foremind-*.whl
~/.venvs/foremind/bin/foremind version             # 把 ~/.venvs/foremind/bin 加进 PATH，或按路径调用
```

用 uv 代替 pip：`uv pip install --offline --python ~/.venvs/foremind/bin/python dist/foremind-*.whl`。

然后在你的项目里：

```sh
cd /path/to/your/project
foremind init --carrier tmux        # 逐项询问；--yes 取检测结果，检测不到的取最严
foremind doctor                     # 每个 FAIL 都附修复方法
```

`init` 写入：项目的 `.claude/settings.local.json`（钩子与状态栏，你自己的条目保留）、`.git/info/exclude`、项目下的 `.foremind/`、用户层 `~/.config/foremind/`。钩子用你所运行的 `foremind` 的解释器并带 `-P`，会话工作目录里的 `foremind/` 包（例如 Foremind 自己的检出）不能顶替已安装的包。`foremind uninstall` 只移除仍与安装记录一致的钩子、状态栏、配置块与登记行；记录之后被改过的条目留在原处并列出；`.foremind/` 数据保留。

## 升级

**同版本的新 wheel**：pip 与 uv 会保留已装的同版本包，必须强制重装：

```sh
~/.venvs/foremind/bin/pip install --no-index --force-reinstall dist/foremind-*.whl
# uv：uv pip install --offline --reinstall --python ~/.venvs/foremind/bin/python dist/foremind-*.whl
```

版本号没变不能证明包已被替换。

**从旧包首次升级**（旧 CLI 没有 `upgrade` 命令，也不认识新的升级标记）：

1. 把候选 wheel 装进独立的临时 venv：`python3 -m venv /tmp/fm-candidate && /tmp/fm-candidate/bin/pip install --no-index <新 wheel>`。
2. 用候选 CLI 检查所有受影响的项目：`cd <项目> && /tmp/fm-candidate/bin/foremind upgrade --check --all`（`--all`：用户注册表中的全部项目及当前项目）。它不写任何东西，列出仍在运行的进程（`foremind supervise`、席位、规划者、总控、作业）；无法判断的证据会打印 `foremind upgrade --confirm-exit <token>`：用候选 CLI 在该项目目录执行（`cd <项目> && /tmp/fm-candidate/bin/foremind upgrade --confirm-exit <token>`；`PATH` 上的旧 `foremind` 没有 `upgrade`），且只在你核实相关工作已退出后执行。
3. 自己停止这些进程，以及会再次启动它们的来源（例如运行 `foremind tick` 的 launchd 任务）。`foremind pause` 不够：它只停自动动作，不停进程。Foremind 自己不停止任何进程。再次检查，直到通过。
4. 用新 wheel 强制重装目标安装（pip 或 uv，见上）。
5. 在每个项目里用目标 CLI 执行 `foremind upgrade`（只迁移当前项目；不调用 pip、不下载、不停止或恢复任何进程）。
6. 验证：`foremind upgrade --check` 与 `foremind doctor` 通过。
7. 恢复运行（`foremind supervise`、你的会话、自动启动）。

**从源码运行转为 wheel，或有两个安装**：你运行的 `foremind` 就是目标，请明确选择（写全路径，或确认 `PATH` 中排第一的是它）。`foremind doctor` 逐目录报告钩子运行的是另一个位置（源码检出、另一个 venv、已消失的解释器），即使两处都能导入也报告，并给出用你所运行 CLI 的 `foremind upgrade --check` 与 `foremind upgrade`。升级只改安装记录仍匹配的钩子条目。源码检出和另一个安装都会保留，不再需要时自己删除。

**备份、中断与恢复**：写入之前，`foremind upgrade` 把每个将改写的文件（字节、路径、存在性、权限、sha256）备份到 `.foremind/upgrade/backups/<事务>/`（目录 0700、文件 0600，被 git 忽略），并打印该路径。成功后备份保留七天，到它打印的日期为止，之后由你自己删除（`rm -rf <备份目录>`）。升级未完成时，Foremind 的写命令、作业启动与开会话一律拒绝（只读诊断、`--confirm-exit` 与重跑 `foremind upgrade` 保留）。中断的升级再次执行 `foremind upgrade` 即可完成：它复用第一次的备份；某个文件既不是原内容也不是升级结果时，它停下且不覆盖。验证反复失败时，`foremind doctor` 与 `foremind upgrade` 会打印带本项目路径的恢复步骤：

1. 停止本项目的所有 Foremind 进程及会再次启动它们的来源。
2. 另存出现第三种内容的文件（`cp -p <文件> <备份目录>/conflict-<n>`）。
3. 原来存在的文件从备份中的 `<n>.orig` 恢复（连同权限）；原来不存在的文件，仅当它仍等于升级结果时删除。
4. 逐个核对恢复后文件的 sha256（`shasum -a 256 <文件>`）与打印的原始值一致。
5. 全部恢复后，把标记移进备份目录：`mv .foremind/upgrade/unfinished.json <备份目录>/unfinished.json`。
6. 此后才可回到原来的 Foremind：用 `pip install --no-index --force-reinstall <它的 wheel>` 离线强制重装并运行 `foremind doctor`。第 5 步之前不要启动旧包：它不认识该标记。
