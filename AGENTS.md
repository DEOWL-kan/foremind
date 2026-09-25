# Foremind — 开发规则（Codex / Claude 必读）

Foremind = 让一个人带一支 AI 开发团队的自动化开发系统。设计唯一正文：`DESIGN.md`（用户 2026-09-25 确认；`SPEC.md` 只保留需求表）；实施计划：`IMPLEMENTATION-PLAN.md`；进度：`PROGRESS.md`。`SPEC.md`、`PROGRESS.md` 与开发过程记录只在作者本地，不在公开仓库。

- 语言：代码与标识符英文；文档、提交正文、与用户沟通用中文。
- 运行时只用 Python 3.11+ 标准库（不用 3.12+ 才有的语法与 API）；不引入第三方依赖，除非 SPEC 明确写入。
- 状态真相在文件与 git；不引入数据库服务或常驻网络服务（监督器是本地进程，见 SPEC）。
- 每批只改交接文档 `owns_paths` 列出的路径；越界先停下报告。
- 每个非平凡逻辑留一个可运行测试（`python3 -m unittest discover -s tests`），全部通过才算完成。
- 不自行 commit/push/合并（除非交接文档明确授权）；不联网安装依赖；不读取或打印密钥。
- 模型：Claude 只用 Opus（具体型号），禁 Sonnet；effort 按档：small=medium、standard=high、complex=xhigh。
