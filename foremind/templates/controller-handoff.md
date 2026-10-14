# 交接：<项目> controller（<阶段一句话>）→ 新会话

写于 <时间（带时区）>，由会话 `<总控会话名 / agent session id>`（<模型>）写。<为什么在此处交接：到线 / 自然断点>。上一份交接见 <git 提交或路径>。
（上下文读数不要手写：`foremind controller handoff` 会把「写交接时上下文 N token（程序读数，有效预算 M）」写到本段开头。）

## 1. 目标与核对（先做）

- 目标：<当前计划与需求范围，goal 路径>。
- 用户授权（仍有效）：<逐条；推送、合入、并发数、额度等>。
- 继任先执行 `foremind controller check`：程序按下面的核对块逐项核对现场，报 ok / 不符并记 `controller_takeover`。

```foremind-check
# 逐行 `键: 值`；# 开头为注释。main_contains、batch 可多行
main_contains: <main 应含的提交>
worktree_clean: true
batch <批次 id 或通配，如 m2c.*>: <状态>
pending: <未决数>
supervisor: running
# 可选：写了才跑，退出码 0 为 ok
test: python3 -m unittest discover -s tests
```

## 2. 当前状态

- 本会话合入与衔接：<批次、提交、登记的配置键、DESIGN 补项>。
- 在跑的批次：<批次 · 档位 · 轮次 · 在等什么>。

## 3. 流程与工具

- <合入、巡查、给席位发消息、退回、抢先批准等的具体命令与注意事项>。

## 4. 决定与教训（勿重议）

- <已定的做法与本会话的失误教训；失误用 `foremind controller slip` 记>。

## 5. 未完成义务（按优先级）

1. <最优先一条>
2. <…>

## 6. 指针

- 进度、发现、遗留、设计的路径；本会话 transcript；规则与记忆目录。
