---
id: <plan-id>.<n>
plan_id: <plan-id>
reqs: ["REQ-<n>"]
repos: ["<仓库id>"]
owns_paths: ["<仓库id>:<路径或窄 glob>"]
reads: ["<仓库id>:<契约路径>"]
depends_on: ["<plan-id>.<n>"]
merge_after: [{"batch": "<plan-id>.<n>", "reason": "<先合 A、B 还没合时谁会坏>"}]
start_commands: ["<开工后先跑的命令>"]
accept_commands: ["<可直接执行的验收命令>"]
tiers: {"difficulty": "S|M|L", "org": "single|exec_review|controller_seats", "review": "self|zero_context|cross_vendor|user", "model": "<模型>", "effort": "medium|high|xhigh", "reason": "<三档理由>"}
mode: auto|watch|accompany|user
hard_block: []
budget_estimate: <上下文预算估计，token>
must_read: [{"path": "<指针>", "why": "<为什么读>"}]
tools: [{"name": "<工具>", "step": "<用在哪一步>"}]
---

# <plan-id>.<n> <一句话标题>

批次头为机读字段（schema `batch_header`），由规划者与总控写、程序校验；席位只读。本文件中的批次头是程序渲染的副本，正本在 `batches/<id>.md`；头里的标量一律是字符串（如 `budget_estimate: 80000`），列表与对象写 JSON。

## 目标

<做成什么样算完；覆盖哪些 REQ；不做什么。>

## 起点命令

开工后先跑，结果应与下方「当前状态」一致（开席机械核验用同一套）：

```
<命令>
```

## 验收命令

全部通过才算完成：

```
<命令>
```

## 边界

- 只写 `owns_paths`；越界先 `foremind decide --new` 申请扩大范围（#8）。
- 只读 `reads`；跨仓库依赖从程序挂的只读 worktree 读。
- 禁用项：硬拦截类别 <#k…>；禁止动作 <…>；排除的模型与厂商 <…>。

## 必读清单

- `<指针>`：<为什么读>

## 推荐工具

- `<工具>`：<用在哪一步>

## 交接段

席位到阈值或交付后写，经 `foremind handoff` 提交（schema `handoff_section`，≤1.5k token），程序校验后追加进批次日志。`last_test` 可选：还没跑过测试就整项省略。

```json
{
  "goal": "<1. 目标>",
  "accept_commands": ["<1. 可直接执行的验收命令>"],
  "state": {
    "repos": {"<仓库id>": {"branch": "<2. 分支>", "sha": "<2. 完整 SHA>"}},
    "changed_files": ["<仓库id>:<路径>"],
    "last_test": {"command": "<2. 最近一次测试命令>", "exit_code": 0}
  },
  "decisions": ["<plan-id>.<n>.D<n>"],
  "failures": ["<plan-id>.<n>.F<n>"],
  "next": ["<5. 最优先的下一步>", "<其余未完成义务>"],
  "unverified": ["<6. 待验证：…>"],
  "pointers": {"transcript": "<7. transcript 路径>", "turns": ["<关键轮次>"], "files": ["<仓库id>:<路径>:<行>"]}
}
```
