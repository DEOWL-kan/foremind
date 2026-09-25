# Foremind

> Run an AI development team from one seat. Foremind plans work into batches, opens AI coding sessions ("seats") for them, hands work over between sessions, gets every change reviewed by a separate read-only reviewer, and only merges through a mechanical gate. Pure Python standard library, state lives in files and git. **Status: early alpha (milestone M1 of 3), macOS + Claude Code first.** Documentation is in Chinese.

Foremind 是一个让**一个人带一支 AI 开发团队**的本地开发系统：把需求拆成互不冲突的批次，为每批开一个 AI 编码会话（席位），会话满了自动交接给新会话，每次改动都由另一个只读的审查者零上下文审查，最后只能经过程序化的门禁合入。

- 只用 Python 3.11+ 标准库，没有第三方依赖。
- 状态都在文件和 git 里（项目下的 `.foremind/`），没有数据库和常驻网络服务；监督进程是本地进程。
- 首发平台：macOS + [Claude Code](https://docs.claude.com/en/docs/claude-code)；Codex 支持在路线图里。

> **现状：早期 alpha。** 已完成里程碑 M1（核心可用），M2（完整治理）与 M3（打包与试运行）尚未开始。接口和文件格式还会变，请勿用于重要项目。

## 它解决什么问题

一个人同时指挥多个 AI 编码会话时，常见的问题是：几个会话改到同一批文件；会话上下文满了、换新会话后丢了进度；AI 自己说「测试过了」但其实没跑；审查者和实现者是同一个上下文，看不出问题；一不留神就合进了主分支、推了远端、改了规则文件。

Foremind 的做法是把这些都交给程序检查，而不是靠提示词约束：

| 问题 | Foremind 的机制 |
|---|---|
| 并行会话互相踩文件 | 规划时校验：互不依赖的批次 `owns_paths` 必须不相交，重叠就自动补依赖；运行时再查一次 |
| 上下文满了丢进度 | 钩子按上下文占用提示交接、到硬阈值阻断；交接段有 schema，继任者开工前机械核对分支、SHA、测试退出码 |
| 自报「测过了」 | 门禁自己在已审代码的干净检出里跑验收命令，结果绑定完整 SHA |
| 自己审自己 | 审查者是一次性、只读（只有 Read/Grep/Glob）、无 MCP 的独立会话，回执绑定每个仓库的完整 head SHA，程序字段不能由模型填写 |
| 越权操作 | PreToolUse 钩子按授权表拦截：写规则文件、改依赖清单、调用有副作用的 MCP 工具等；要放行须经待决与凭据 |
| 没人盯着时跑偏 | 监督进程不调用模型、只做机械动作；额度按角色分暂停线；全部在等人时一个模型都不启动，只发一次汇总 |

## 概念

- **角色**：规划者、总控、席位（写代码）、审查者、审计者、决策者、编目员。长期角色是交互会话，其余是一次性只读会话。
- **批次**：一次可独立验证、独立交付的改动，带 `owns_paths`、依赖、验收命令。
- **交接**：会话之间用结构化交接段传递现场，继任者核对后才接手。
- **待决**：需要人拍板的事项，带选项、期限和可撤回的暂定决定。
- **门禁** `foremind gate`：回执、完整 SHA、验收、CI/本地检查、owns_paths、合入顺序、依赖段、交付级别全部通过才合入。
- **授权表**：哪些动作归系统、哪些归你；分层配置，下层只能收紧。
- **改进回路**：出错、返工、被纠正都进日志，归并成规则，再升级成程序检查。

完整设计见 [`DESIGN.md`](DESIGN.md)（唯一设计正文；§20 是实施中定下的细节），实施计划见 [`IMPLEMENTATION-PLAN.md`](IMPLEMENTATION-PLAN.md)。

## M1 已实现的部分

| 模块 | 内容 |
|---|---|
| 基础 | 原子写与文件锁、机读头、分层配置（普通/并集/仓库约定/授权四类键，授权只能收紧）、带哈希链的事件日志、多仓库登记、分离运行的长作业 |
| 协议与 schema | 角色卡、交接模板、15 类产物的 schema 校验、批次/待决/会话三个状态机 |
| 规划 | S6 校验（环、不相交、契约文件、预算、禁用词）、耦合信号（ast import、git 共改、语义声明）、排程与波次、终端摘要与单文件 HTML 审阅页、目标冻结、批准与修订（手改会被发现） |
| 席位与承载 | 批次锁（破锁须有旧会话已退出的证据）、worktree（放在仓库检出之外）、Claude 开席参数、tmux / Orca / 手动三种承载、收件箱与投递游标、交接接手 |
| 钩子与遥测 | SessionStart 注入考纲、Stop 交接提示与阻断、PreToolUse 越界与授权表拦截、PostToolUse 上下文占用、UserPromptSubmit 在场、状态栏串联与额度遥测；任何内部异常都不会把拦截变成放行 |
| 审查、验收与门禁 | 只读审查者启动与回执组装、验收执行、门禁全部检查、merge/squash/rebase 三种合入的判定、按 head 合入、发布前检查 |
| 监督进程 | tick / supervise、全局单实例锁、就绪队列、经作业开席与代合、卡住与继任、额度状态机、全阻塞、pause / STOP / run、ntfy 通知（只发送） |
| 安装 | `init`（单仓/多仓、TOML 带标记块、钩子合并、交付约定的程序检测部分）、`doctor`、`status`、`uninstall`（逐字节还原） |

尚未实现（M2/M3）：决策层与待决的完整流程、交付约定的更新与冲突处理、审计、上下文预算与编目、晨报与 ntfy 双向回复、规划者与总控的会话流程、Codex、herdr 承载、Claude Code 插件打包、示例仓库。

## 快速开始（从源码）

要求：macOS、Python 3.11+、git、[Claude Code](https://docs.claude.com/en/docs/claude-code)；承载任选 tmux 或 Orca；有 GitHub 远端时需要已登录的 `gh`。

```sh
git clone https://github.com/DEOWL-kan/foremind.git
cd foremind
python3 -m unittest discover -s tests          # 全部测试（约 2 分钟）

# 在你的项目里初始化（只影响这个项目；会逐项询问，加 --yes 则非交互：取检测结果，检测不到的取最严）
cd /path/to/your/project
PYTHONPATH=/path/to/foremind python3 -P -m foremind init --carrier tmux
PYTHONPATH=/path/to/foremind python3 -P -m foremind doctor
```

打包安装（`uv tool install` / Claude Code 插件）在 M3。注意 `-P`：它让当前目录里的同名包不会遮蔽已安装的 Foremind（在 Foremind 仓库里开发它自己时尤其重要）。

`init` 会写入：项目的 `.claude/settings.local.json`（钩子与状态栏，先备份、不覆盖已有条目）、`.git/info/exclude`、项目下的 `.foremind/`、用户层 `~/.config/foremind/`。`foremind uninstall` 只删自己加的内容，其余文件逐字节还原。

常用命令（下文 `foremind` 即 `PYTHONPATH=/path/to/foremind python3 -P -m foremind`）：

```
foremind do "<小需求>" --owns <仓库>:<路径> --accept "<验收命令>"   # 单批轻量计划
foremind plan show|validate|approve|amend                          # 审阅与批准计划
foremind seat <批次>            # 开席（--user：我自己来做）
foremind review                 # 席位提交就绪、请求审查
foremind gate <批次>            # 门禁：通过才批准、交付或合入
foremind supervise              # 前台监督循环（或 launchd 每分钟 foremind tick）
foremind status [--all]         # 批次、会话、待决与额度
```

## 信任模型（请务必读）

- 门禁与钩子是**防误操作的检查，不是安全沙箱**：同一用户权限下的进程可以绕过它们。
- 路径拦截只覆盖 Edit/Write 类工具；经 Bash 的写入只能事后由门禁 diff 与状态核对发现。
- 设计上合并只经 `foremind gate`；没开分支保护时，人或 agent 直接 `gh pr merge` 可以绕过。建议开分支保护。
- 发布、部署、付费调用、删除外部资源永远不在自动范围内。
- ntfy 频道名是弱认证，通知只含编号与选项，不含代码和路径。

详见 [`DESIGN.md`](DESIGN.md) §14。

## 它是怎么开发的

Foremind 本身由 AI 团队开发：一个交互式总控会话（Claude Opus）规划并派出写手会话，每批经零上下文审查、修复、复审后才合入；设计缺口由总控按「不放宽、只补机制」定下并记入 `DESIGN.md` §20。开发规则见 [`AGENTS.md`](AGENTS.md)。

如实说明：M1 的审查者与实现者是**同一厂商的同一模型**（不同的新会话），不是跨厂商复审；M1 只在 Python 3.14 / 3.13 上运行过测试，未在 3.11 上实跑。开发过程记录（审查报告、交接文档、调研）不在公开仓库中。

## 路线图

- **M2 完整治理**（将用 Foremind 自己开发）：决策层、交付约定与冲突、审计、上下文与编目、报表与通知、规划者与总控、Codex、其余运行能力。
- **M3 打包与试运行**：Claude Code 插件、文档、示例仓库、零上下文试装、真实项目试跑。

## 许可证

[MIT](LICENSE)
