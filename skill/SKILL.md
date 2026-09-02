---
name: weeklylog
description: 记录和查询个人工作事实，审核 Codex 自动采集候选，并生成个人周报与 Jira 更新草稿。用户说“记录一下”、补充工作、查看本周、整理候选或写周报时使用；周度视觉回顾转交 $weekglow。
---

# Weeklylog

`weeklylog` 是事实记录 skill。它只使用随 skill 安装的 TypeScript CLI 和本地 SQLite，不直接调用网络、不上传数据，也不把完整 prompt、模型回复或工具输出写入数据库。

## 事实规则

- 用户明确要求记录的内容，直接作为正式记录写入 SQLite，不再进入候选审核。
- Codex hook 自动采集的内容只能写入候选；用户批量确认后才进入正式记录。
- `content` 是工作事项，`result` 是可验证成果或影响，不能用模型推测补全。
- 一句话包含多个独立事项时，拆成多条记录。
- 没有明确投入时间时不估算；没有记录的成果、计划或 Jira 关联不补写。
- `ai_turn_count` 是客观的 AI 往返轮数：优先使用客户端提供值，否则统计当前会话中可见回合；`collaboration_note` 只保存用户主动填写的协作体验原话。
- 不把协作体验转换成 sentiment score，也不从语气自动推断情绪结论。
- 正式记录面向个人复盘和 Jira 补充，不使用上级汇报口吻。

## 脚本位置

初始化后脚本默认位于 `$HOME/.codex/skills/weeklylog/scripts/weeklylog.js`。调用时使用绝对路径，数据库默认是 `$HOME/.codex/data/weeklylog/worklog.sqlite3`。

```bash
CLI="$HOME/.codex/skills/weeklylog/scripts/weeklylog.js"
```

## 记录和查询

```bash
node "$CLI" add --content "完成登录页重构" --project "移动端" --status done \
  --result "减少重复组件" --minutes 90 --ai-turn-count 4 \
  --collaboration-note "来回解释了几次，最后才对齐" --tags "前端,重构" --json

node "$CLI" list --week 2026-08-31 --json
node "$CLI" update 12 --result "已上线并通过回归" --minutes 135 --json
```

成功后向用户确认日期、事项和记录 ID。删除不是日常流程，除非用户明确确认具体记录，否则不执行删除。

## 候选审核

```bash
node "$CLI" hook list --week 2026-08-31 --json
node "$CLI" hook promote-batch 12 13 --json
node "$CLI" hook ignore 14 --json
```

候选默认不进入周报。审核时只做一次批量确认；无法判断的候选宁可忽略或保留，不要自动入账。

## 周报和 Jira 草稿

```bash
node "$CLI" report --week 2026-08-31 --detail
node "$CLI" report --week 2026-08-31 --detail --output ./weeklylog-2026-08-31.md
node "$CLI" dataset --week 2026-08-31 --json
```

周报只使用正式记录，并明确显示待确认候选数量。Jira 草稿以每个已确认工作事项的一句“进展/结果”短句为主；只输出草稿，不自动修改 Jira。

## Codex 自动采集

`init` 会把四个 Codex 生命周期 hook 接到本地命令：

```bash
node "$CLI" hook ingest --hook-output
```

`SessionStart` 和 `SessionEnd` 记录会话边界，`UserPromptSubmit` 只递增当前会话的一行聚合轮数，`Stop` 根据结构化标题、摘要和时间生成候选。原始 prompt、回复和工具输出不会落盘，也不会建立逐轮对话表。

SQLite 设有 10 MiB 硬存储预算：会话元数据只保留最近的少量聚合行，已忽略或已确认的候选会过期清理，字段也有长度上限；达到预算时不再写入新的自动候选。

如需查看客户端配置：

```bash
node "$CLI" hook config --client codex
```

## 初始化

首次运行：

```bash
npx weeklylog-cli init
```

终端向导会一次完成 Codex、`weeklylog`、`weekglow` 和 hooks 配置。脚本化环境使用：

```bash
npx weeklylog-cli init --client codex --json
```

初始化完成后日常直接使用；周报或周度视觉卡片的 cronjob 另行配置。
