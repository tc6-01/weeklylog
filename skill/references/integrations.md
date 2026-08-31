# Weeklylog 自动采集集成

## 设计边界

自动采集分为两层：

1. 客户端 hook 把生命周期事件送入 `hook ingest`。
2. weeklylog 将可识别的工作回合保存为候选；确认后才写入正式 `entries`。

这样可以自动留下“做了什么、何时开始/结束、哪个项目、客户端提供的结构化结果摘要”，又不会把每一次问天气、查命令或工具噪声直接算进周报。`--auto-approve` 可绕过候选层，但应先观察几天采集质量。

自动时长是 hook 可观测的会话/回合时长，不是完整人工投入时长。没有明确的开始或结束时间时，字段留空或只记录事件发生时刻，不做推测。

## Codex

Codex 的命令 hook 从 stdin 接收一个 JSON 对象。它支持 `SessionStart`、`UserPromptSubmit`、`Stop`、`SessionEnd` 等生命周期事件；命令 hook 首次启用前需要在 CLI 的 `/hooks` 中审核和信任。详见[官方 Hooks 文档](https://learn.chatgpt.com/codex/hooks)。

先生成配置片段：

```bash
SCRIPT=/Users/roubao/.codex/skills/weeklylog/scripts/weeklylog.py
python3 "$SCRIPT" hook config --client codex > /tmp/weeklylog-codex-hooks.json
```

将输出中的 `hooks` 合并到已有 `~/.codex/hooks.json`，不要整文件覆盖其他 hook。要直接入账则改用：

```bash
python3 "$SCRIPT" hook config --client codex --auto-approve
```

合并后，在 Codex CLI 里运行 `/hooks`，审核新 hook；如果修改了命令路径或参数，需要重新信任新的 hook 定义。命令使用绝对脚本路径，避免 Codex 从子目录启动时找不到文件。

### 事件对应关系

| Codex 事件 | weeklylog 行为 |
| --- | --- |
| `SessionStart` | 记录会话开始、工作目录和客户端 |
| `UserPromptSubmit` | 只保存本回合标识和开始时间，不保存原始提示词 |
| `Stop` | 优先读取客户端提供的 `title`/`summary`，旧客户端可回退读取 `last_assistant_message`/`output` 并在内存中限长后生成候选，同时计算回合时长 |
| `SessionEnd` | 记录会话结束；不凭空生成工作成果 |

默认 hook 使用同步执行，确保回合结束时的候选不会因会话关闭而丢失；`SessionEnd` 按 Codex 约定同步执行且超时应保持很短。脚本只做本地 SQLite 写入，脚本成功时输出 `{}`，不会把采集内容注入对话上下文。

## 通用 JSON 协议

其他 AI 客户端无需理解 SQLite，只要把一个事件对象通过 stdin 交给脚本：

```bash
printf '%s\n' '{
  "event": "task_completed",
  "id": "client-session-turn-001",
  "client": "my-ai-client",
  "session_id": "session-001",
  "turn_id": "turn-009",
  "title": "排查线上超时",
  "summary": "定位到连接池配置并完成回归验证",
  "project": "支付服务",
  "started_at": "2026-08-31T21:30:00+08:00",
  "ended_at": "2026-08-31T23:45:00+08:00",
  "tags": ["线上", "性能"]
}' | python3 "$SCRIPT" hook ingest --json
```

支持的常用字段：

- 事件：`event` 或 `hook_event_name`。`session_start`、`session_end`、`prompt`、`turn_end`、`task_completed` 会被处理；其他事件安全忽略。
- 标识：`id`/`event_id`、`session_id`、`turn_id`。提供稳定的 `id` 可保证重放不会产生重复记录。
- 内容：优先使用 `title`/`content`、`summary`/`result`。旧客户端的 `last_assistant_message`/`assistant_message`/`output` 仅作为有界兼容摘要在内存中裁剪；`prompt` 事件只用于开始计时，不会写入 SQLite；不要发送完整 prompt 或工具原始输出。
- 时间：`started_at`、`ended_at` 或 `duration_minutes`。时间接受 ISO 8601。
- 上下文：`client`、`project`、`category`、`status`、`tags`、`cwd`。

候选审核：

```bash
python3 "$SCRIPT" hook list --week 2026-08-31
python3 "$SCRIPT" hook promote 12 --status done
python3 "$SCRIPT" hook ignore 13
```

### 客户端适配器建议

客户端自身的 hook 只负责读取事件、生成短小的结构化 `title`/`summary` 并调用 weeklylog；不要把完整 transcript、API key、工具原始输出写入数据库。对于只有“会话结束”事件的客户端，发送 `task_completed` 并附上客户端生成的短摘要；对于只有命令生命周期的客户端，建议先记录候选，不要把每条命令都当成工作事项。

## 故障与隐私

- hook 写库失败不应阻断 AI 客户端主流程；客户端适配器应吞掉非零错误并保留原事件以便重试。
- 候选与正式记录都保存在本机 SQLite；weeklylog 不上传或同步数据。
- 若不想自动记录某个客户端，移除对应客户端的 hook 配置即可；历史候选可用 `hook ignore` 处理。
