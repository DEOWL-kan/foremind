# Foremind 实施计划（草案，待用户批准）

状态：**执行中**。2026-09-25 由 controller 起草，依据 `DESIGN.md` v1.2；用户 2026-09-25 确认设计并同意开工（先本地、可合 main、3 个写手）。

## 0. 总体安排

- **三个里程碑**，按设计自己的规则每个计划 ≤10 批：
  - **M1 核心可用**（约 5–6 天）：单仓与多仓登记、规划校验、开席与交接、钩子、审查与门禁、基础监督进程、安装。做完就能用 Foremind 开发它自己。
  - **M2 完整治理**（约 6–8 天）：决策层、交付约定与冲突处理、审计、上下文与编目、报表与通知、规划者与总控会话、Codex、其余承载。**用 Foremind 自己来开发**，这是第一次真实验证。
  - **M3 打包与试运行**（约 3–4 天）：插件打包、文档、示例仓库、零上下文按 quickstart 从零装起、真实项目试跑。
- 合计约 2.5–3.5 周；主要不确定性是 M1-0 的实测结果、额度、以及需要用户拍板的等待。
- 并发写手默认 3 个（额度约束）；依赖满足方式用 `approved`（审查通过即可接着做）。

## 1. 代码结构（契约，M1-1 / M1-2 建立后其他批次只读）

```
foremind/               Python 包，只用标准库（3.11+）
  cli.py                argparse 入口
  fsutil.py             原子写、flock、路径工具
  header.py             机读头解析/写入
  config.py             分层合并、三类键、ceiling/value
  events.py             事件日志：意图/结果、去重、轮转
  repos.py              项目与仓库登记（多仓）
  schemas.py            全部产物的 schema 与校验器
  state.py              批次/待决/会话状态机
  plan/                 校验(S6)、耦合、排程、渲染(终端+HTML)、冻结
  seat.py lock.py handoff.py inbox.py worktree.py
  carriers/             base、orca、tmux、herdr、manual
  vendors/              claude、codex：启动交互/headless、路由与排除
  hooks/                Claude Code 钩子入口
  telemetry.py quota.py
  review.py acceptance.py gate.py delivery.py
  decide/               授权表、待决、放行凭据、暂定、判例、决策者 IO
  audit.py supervisor.py report.py catalog.py tools_scan.py
  notify/               ntfy、none
  install.py            init / doctor / upgrade / uninstall、TOML 带标记块
templates/              协议、角色卡、交接、规划模板
plugin/                 Claude Code 插件（命令、技能、hooks.json）
docs/  examples/  tests/
```

## 2. M1 核心可用

| 批次 | 内容 | 依赖 | 验收（摘要） |
|---|---|---|---|
| M1-0 实测（调研，不写产品代码） | 实测：`claude --settings` 传入的钩子是否全部生效、`--add-dir` 是否加载各仓库 CLAUDE.md、程序投递是否触发 UserPromptSubmit；Claude Code 插件项目级安装；嵌套 worktree 是否向上加载 CLAUDE.md；Codex 钩子注入与拦截语义、沙箱参数、额度读取；herdr socket API；Claude Code 钩子输入字段（Edit/Write/Bash、UserPromptSubmit） | — | 每项给出「命令 + 输出 + 结论」；结论写回 DESIGN 的【待实测】 |
| M1-1 基础（契约批） | 包骨架、cli、fsutil（原子写、项目状态锁、全局锁）、header、config（四类键、ceiling/value、任务层只能收紧、违反报错；`delivery.toml` 程序独占写）、events（意图/结果、去重、哈希链、按月只轮转已闭合）、repos（多仓登记）、`foremind _job` 包装进程、`foremind log` | — | 配置合并与授权不扩大的单测；事件幂等与轮转单测 |
| M1-2 协议与 schema（契约批） | PROTOCOL.md、7 张角色卡、交接模板、schemas.py（计划、批次头、交接、回执、验收、决策输出、凭据、编目补丁）、state.py 状态机 | — | 每个 schema 的正反例单测；状态机非法转移被拒 |
| M1-3 规划 | S6 全部校验（互不可达对不相交、跨计划）、耦合信号（ast、共改）、重叠自动加边、排程、`do` 单批计划、`plan show` 终端 + HTML、approve / amend、目标冻结哈希 | M1-1 M1-2 | 构造计划的校验单测；HTML 生成快照测试 |
| M1-4 席位与承载 | 会话身份环境变量与绑定、批次锁（破锁须先确认旧会话退出、原子转移）、心跳文件、多仓 worktree 与跨仓只读 worktree、`--settings` / `--add-dir` 启动、开工前机械核验、承载 orca / tmux / manual、厂商启动（claude）、`handoff --accept`、收件箱与游标、续派 | M1-1 M1-2 | 锁并发单测；tmux 适配器集成测试（本机）；核验不一致拒绝开工 |
| M1-5 钩子与遥测 | SessionStart 注入 L1、Stop 软/硬阈值与收件箱投递、PreToolUse（owns_paths、可写矩阵、锁持有者、硬拦截 + 凭据查找）、PostToolUse usage、UserPromptSubmit 在场、statusLine 包装、上下文占用与额度遥测 | M1-1 M1-2 | 用录制的钩子输入做单测；在本仓库真装一次验证拦截 |
| M1-6 审查、验收与门禁 | review 启动（headless、只读、排除项、按仓库记 head、fingerprint）、验收执行、gate（移植 pr_gate：已知坑与 squash/rebase merged 判定、多仓 heads、merge_after 与 depends_on、依赖段复核、本地门禁结果与推送后补写 commit status、match-head、`[gate].ci` 三种模式）、不收敛诊断、release-check | M1-1 M1-2 | 四坑单测；多仓回执失效单测；在一个测试仓库上跑通 PR → 审查 → 门禁 |
| M1-7 监督进程（基础） | tick / supervise、全局锁、就绪队列、开席、唤醒审查者、卡住与 busy_tool、额度状态机（暂停线按角色、滞回、退避、7d）、全阻塞、pause / STOP / run、ntfy 发送 | M1-3 至 M1-6 | 模拟遥测下暂停/恢复单测；全阻塞时零模型启动；重复 tick 不重复动作 |
| M1-8 安装与自举 | init（单仓/多仓登记、TOML 带标记块、钩子写 settings.local.json、交付约定的程序检测部分）、doctor、status；装到 foremind 仓库自身 | M1-5 M1-7 | 在临时目录从零 init → doctor 全绿；卸载只删自己加的 |

关键路径：M1-1 → M1-4 / M1-5 / M1-6 → M1-7 → M1-8。M1-0、M1-1、M1-2 同时开工。

**M1 期间的开发方式（自举前）**：controller 用 `agent-dispatch` 派写手（Claude Opus 5.5，档位按批次：契约批与门禁 xhigh，其余 high），每批一个 worktree；写手不 commit；controller 跑全部测试 → 派零上下文审查 → 通过后合入 main。

## 3. M2 完整治理（用 Foremind 开发）

| 批次 | 内容 | 依赖 |
|---|---|---|
| M2-1 决策层 | 授权表（#0–#23、🔒、三套预设）、待决 Q-n（排序、分组）、`decide`、放行凭据签发、暂定 PV-n（期限、推翻率、自动收紧）、决策者输入报表与输出校验、判例 J-n（两种机器前提） | M1 |
| M2-2 交付 | 交付约定提案（程序检测 + 编目员读规则文档 + 用户确认）、按约定合入与 merge_command、更新分支与冲突流程、patch-id 重绑、合入组、keep_updated、无合并权限退回 | M1 |
| M2-3 审计 | L0 全表与硬失败动作、L1 触发与去重、配额与硬上限、间隔 T、审计者 IO、P0–P3 闭环、总控轮换、`audit --canary` | M1、M2-1 |
| M2-4 上下文与编目 | L0/L1 组装与预算估算、有效预算、索引与分册、编目员补丁落盘、`ask` 答疑、交接触发全套 | M1 |
| M2-5 报表、通知与复盘 | 全部指标、晨报、ntfy 双向（回复频道、校验码、本机确认）、告警限流、`retro`、改进日志 → 规则建议、用户事件钩子 | M1、M2-1 |
| M2-6 规划者、总控与定制 | 规划者会话流程（S1–S9 提示词）、总控角色、角色提示词追加/按标题覆盖、`tools scan` 与能力清单、`[flow]` | M1 |
| M2-7 Codex | Codex 启动（沙箱）、按 M1-0 结论接钩子、额度被动判定、AGENTS.md 片段、跨厂商审查路由 | M1、M1-0 |
| M2-8 其余运行能力 | herdr 承载、在场判定与 `presence`、运行时段、多项目统一监督 | M1 |

审查：9/30 Codex 恢复后，门禁、钩子、监督进程、决策层这几块改为跨厂商复审。

## 4. M3 打包与试运行

| 批次 | 内容 |
|---|---|
| M3-1 插件与文档 | Claude Code 插件打包、upgrade / uninstall、快速上手、概念、配置参考、信任模型、排错 |
| M3-2 示例仓库 | 单仓与双仓两个示例，端到端脚本 |
| M3-3 零上下文试装 | 新会话只按 quickstart 从零安装并跑通示例（R21 验收） |
| M3-4 真实项目试跑 | 在用户指定的真实项目上跑一个小需求，记录指标与问题，修复 |

## 5. 用户决定（2026-09-25）与待定

- 代码仓库：**先只在本地**，不建远端（2026-09-25 起改为：代码以快照方式公开到 GitHub，开发过程记录不公开）。
- 交付权限：测试全过、审查通过后 controller 可把各批**合入 main**。
- 并发写手：**3 个**。
- M3-4 试跑：**作者的两个真实项目都试**。
- **CI 成本尽量低**：本地阶段不跑任何 CI，门禁用本地检查（`python3 -m unittest discover -s tests` + 各批验收命令），CI 费用为零。以后建远端时见下条建议。
- 远端建在哪（建议，届时再定）：**个人账号下的私有仓库**。理由：与公司组织的 CI 额度和权限分开，将来开源时归属清楚；公开仓库的 GitHub Actions 免费，开源后 CI 不再花钱。私有期间只在 PR 标为就绪时跑一次 CI、只用 Linux 运行器（macOS 运行器按分钟计费倍率高），草稿期与合入后不跑。
- 设计已确认（2026-09-25）。
