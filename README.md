# weeklylog

`weeklylog` 是一个面向个人开发者的本地工作记录工具：Codex 自动捕捉工作线索，用户确认后形成正式记录，再生成个人周报和 Jira 更新草稿。数据只保存在本机 SQLite；周度视觉回顾由独立的 `weekglow` skill 消费。

运行时只需要 Node.js 22.5+，不依赖 Python。

## 一次初始化

```bash
npx weeklylog-cli init
```

终端向导一次完成：

1. 选择 AI 客户端（当前支持 Codex）
2. 安装 `weeklylog` 和 `weekglow` skill
3. 将自动记录 hooks 合并到 `~/.codex/hooks.json`
4. 初始化 `~/.codex/data/weeklylog/worklog.sqlite3`

脚本化环境可直接指定 Codex：

```bash
npx weeklylog-cli init --client codex --json
```

已有配置会被保留，hooks 幂等合并。`--force` 只更新 weeklylog 自己管理的 skill 文件；不覆盖其他 hooks。

## 日常记录

```bash
weeklylog add --content "完成登录页重构" \
  --project "移动端" --status done \
  --result "减少重复组件" --minutes 90 --ai-turn-count 4 \
  --collaboration-note "来回解释了几次，最后才对齐" --tags "前端,重构"

weeklylog list --week 2026-08-31 --json
weeklylog update 12 --result "已上线并通过回归" --minutes 135 --json
weeklylog reflect --week 2026-08-31 --mood 4.2 \
  --happy-moment "顺利上线"
```

用户明确要求记录的事实直接进入正式 `entries`，不会再经过候选审核。没有明确提供的成果、工时或 Jira 关联不会被猜测。

`ai_turn_count` 只表示这项工作涉及的 AI 往返轮数；`collaboration_note` 只保存用户主动填写的体验原话。系统不把它们转换成情感分数，也不自动推断“沟通质量”。

## 自动记录与候选审核

Codex hooks 调用本地命令：

```bash
node ~/.codex/skills/weeklylog/scripts/weeklylog.js hook ingest --hook-output
```

`SessionStart`、`UserPromptSubmit`、`Stop`、`SessionEnd` 会记录会话边界和结构化工作摘要。原始 prompt、回复、工具输出不写入 SQLite；自动结果先进入候选区。

SQLite 不是对话数据库：每个活动会话最多只保留一行聚合计数（AI 往返轮数、时间边界和工作目录），不会为每轮对话建行。数据库使用 4 KiB 页、10 MiB 硬上限，并定期清理过期候选和会话元数据；达到上限时拒绝新增，避免继续膨胀。

```bash
weeklylog hook list --week 2026-08-31 --json
weeklylog hook promote-batch 12 13 --json
weeklylog hook ignore 14 --json
```

只有确认后的记录才会进入周报和 Jira 草稿。

## 周报与周度视觉回顾

```bash
weeklylog report --week 2026-08-31 --detail
weeklylog report --week 2026-08-31 --detail --output ./weeklylog-2026-08-31.md
weeklylog dataset --week 2026-08-31 --json
weeklylog weekglow --week 2026-08-31 --output-dir ./weekglow-2026-08-31 --json
```

`report` 输出个人复盘和 Jira 更新所需事实；不使用上级汇报口吻，也不会自动写 Jira。`weekglow` 输出六张少字多图 SVG 卡片，只读正式记录。

后续 cronjob 只需要定时调用 `report` 或 `weekglow`，不参与记录和候选确认。

## 命令

```text
init                         一次性初始化 Codex、skill、hooks 和 SQLite
add                          写入一条正式工作记录
list                         查询正式工作记录
update                       修改记录
reflect                      保存或查看每周复盘
report                       生成 Markdown 周报
dataset                      导出周报/视觉使用的数据集
hook ingest                  接收 Codex hook 事件
hook list/promote/ignore     审核自动采集候选
weekglow                    生成周度视觉卡片
doctor                       检查 Node 和数据库路径
```
