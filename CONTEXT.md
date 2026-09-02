# Weeklylog 当前产品上下文

## 核心宗旨

Weeklylog 服务个人开发者的日常工作记录、个人周报和 Jira 补充，不是给上级做绩效汇报的系统。

唯一评判标准是：好不好用。

## 用户承诺

- 第一次 `init` 在终端完成一次性配置；之后可以直接使用，不需要手工复制或二次接线。
- 正常工作不被打断。自动采集先留下候选，周报前一次批量确认即可。
- 用户明确说“记录一下”时，直接写正式记录。
- 周报第一次生成就应该基本可用；用户可以自行修改，但不需要整篇重写。
- 每个工作事项生成一句简短的 Jira 进展/结果草稿，只复制或确认，不自动修改 Jira。
- 没有事实就明确显示缺失，不靠模型猜成果、工时、情绪或计划。

## 当前范围

### 保留

- TypeScript CLI
- Node.js `node:sqlite` 本地数据库
- Codex 一次性初始化向导
- `weeklylog` 记录与候选审核 skill
- `weekglow` 周度视觉回顾 skill
- Codex `SessionStart`、`UserPromptSubmit`、`Stop`、`SessionEnd` hooks
- Markdown 周报、结构化数据集和周度 SVG 卡片
- 工作事项级 `ai_turn_count` 和 `collaboration_note`

### 删除或暂不实现

- Python 运行时和 Python 脚本
- 年度报告和年度视觉 skill
- 其他 AI 客户端的一站式适配
- Jira 网络写入
- 自动确认候选
- 复杂 Web 配置界面
- cronjob 自动安装（后续只配置定时调用）

## 数据流

```text
Codex hook ─┐
            ├─► SQLite 自动候选 ─► 一次批量确认 ─► 正式记录 ─► 周报/Jira/周度卡片
手动记录 ───┘
```

## 事实边界

- `entries` 是正式事实来源。
- `capture_candidates` 只能作为待确认线索。
- 自动 hook 不保存完整 prompt、模型回复、工具输出、diff 或密钥。
- SQLite 不是对话存档：每个会话只保留一行聚合计数，不保存逐轮 prompt/reply/tool output；数据库预算为 10 MiB，达到上限时拒绝新增自动候选。
- 没有明确投入时间不估算；AI 可观测时长不等于完整人工工时。
- `ai_turn_count` 只统计客户端提供或当前会话可见的客观 AI 往返轮数；`collaboration_note` 只保存用户原话，不做 sentiment score。
- cronjob 只读正式数据并生成输出，不采集、不确认、不覆盖用户已编辑的文件。

## 术语

**正式记录**：用户明确提供或确认、可进入周报的工作事项。

**候选记录**：Codex hook 根据结构化事件留下、尚未确认的工作线索。

**一次性初始化**：首次运行 `init` 时完成数据库、skill 和 hooks 的安装与接线。

**单次生成**：用户发起一次周报生成后得到首稿；之后可以自由编辑，系统不后台反复重写。
