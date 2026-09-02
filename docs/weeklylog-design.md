# Weeklylog 设计

## 核心闭环

```text
Codex hook / 用户手动补录
          │
          ▼
       本地 SQLite
          │
          ├─ 正式记录 ──► 个人周报 / Jira 更新草稿
          │
          └─ 自动候选 ──► 一次批量确认 ──► 正式记录
                                      │
                                      └─► weekglow 周度卡片
```

## 边界

- `weeklylog` skill 负责记录、查询、候选审核和周报事实。
- `weekglow` skill 只读取正式记录并生成视觉回顾。
- Codex hooks 是自动采集入口，不是周报生成器。
- cronjob 只负责定时调用输出命令，不负责采集、确认或改写事实。
- Jira 只生成可复制的更新草稿，当前不自动访问 Jira 网络接口。

## 记录语义

用户明确要求记录的内容直接写入 `entries`。自动事件写入 `capture_candidates`，并附带有限的结构化摘要、时间、项目和来源。候选必须经过用户确认才能进入正式周报。

工作事项可选记录 `ai_turn_count`（客观 AI 往返轮数）和 `collaboration_note`（用户主动填写的协作体验原话）。两者只用于周度叙事，不转换为情感分数。

原始 prompt、完整模型回复、工具输出、diff 和密钥不落盘。Codex 原生 `Stop` 只有 `last_assistant_message` 时，仅在内存提取带有工作信号的一行短标题和摘要，再丢弃原文。没有明确时长不估算；没有明确成果不补写。

### SQLite 存储边界

SQLite 只保存正式工作事实、待确认候选和用于计算 `ai_turn_count` 的会话聚合计数。`UserPromptSubmit` 不产生逐轮记录；每个会话最多一行 `capture_sessions`，只含客户端、工作目录、起止时间、最近回合标识和轮数。旧的 `automation_sessions` / `automation_turns` 表会在升级时删除，迁移时最多保留聚合轮数。

数据库采用 4 KiB 页和 10 MiB `max_page_count`，不使用持续增长的 WAL 文件；写入前后会清理过期的会话和已处理候选，并在接近预算时压缩数据库。达到预算时 SQLite 拒绝新增，避免静默超过配额。

## 运行时

CLI 使用 TypeScript 编译为 Node.js，SQLite 使用 Node 22.5+ 自带的 `node:sqlite`。系统没有 Python 运行时、Python 脚本或外部模型依赖。
