# weeklylog

`weeklylog` 是一个 local-first 的每周工作记录工具：数据保存在本机 SQLite，支持深度周报、年度长篇总结、成长海报，以及 Codex/其他 AI 客户端的 hook 自动采集。

由于 npm 上已有同名包，本项目的 npx 包名是 `weeklylog-cli`，命令名仍然是 `weeklylog`。

## 安装

需要 Node.js 18+ 和 Python 3。

直接使用，不写入全局文件：

```bash
npx weeklylog-cli doctor
npx weeklylog-cli init
npx weeklylog-cli add --content "完成登录页重构" --minutes 90 --status done
npx weeklylog-cli report --detail
```

将 Codex skill 安装到 `~/.codex/skills/weeklylog`：

```bash
npx weeklylog-cli install
```

目标已存在时，使用 `--force` 更新；也可以指定安装目录：

```bash
npx weeklylog-cli install --force
npx weeklylog-cli install --target /path/to/.codex/skills/weeklylog
```

默认数据库是 `~/.codex/data/weeklylog/worklog.sqlite3`，也可以通过 `--db PATH` 或 `WEEKLYLOG_DB` 覆盖。

## 常用命令

```bash
npx weeklylog-cli add --content "排查线上超时" --start 21:30 --end 23:45 --project "支付服务"
npx weeklylog-cli list --week 2026-08-31 --json
npx weeklylog-cli report --week 2026-08-31 --detail
npx weeklylog-cli reflect --week 2026-08-31 --mood 4.2 --hard-moment "联调卡了三个小时"
npx weeklylog-cli poster --week 2026-08-31 --output weeklylog.png
npx weeklylog-cli annual-report --year 2026 --output weeklylog-2026.md
```

## Codex hook 集成

生成配置片段：

```bash
npx weeklylog-cli hook config --client codex > /tmp/weeklylog-codex-hooks.json
```

将输出中的 `hooks` 数组**合并**到 `~/.codex/hooks.json`，不要覆盖已有 hook；然后在 Codex CLI 中运行 `/hooks` 审核并信任。

默认流程是：`SessionStart` 记录会话、`UserPromptSubmit` 记录回合开始、`Stop` 生成候选、`SessionEnd` 收尾。候选不会直接计入正式周报：

```bash
npx weeklylog-cli hook list --week 2026-08-31
npx weeklylog-cli hook promote 12
npx weeklylog-cli hook ignore 13
```

如果确认希望自动直接入账：

```bash
npx weeklylog-cli hook config --client codex --auto-approve
```

Codex hook 的详细事件、字段和隐私边界见 [`skill/references/integrations.md`](skill/references/integrations.md)。

## 其他 AI 客户端

客户端只需将标准 JSON 事件通过 stdin 传给 `hook ingest`：

```bash
printf '%s\n' '{
  "event": "task_completed",
  "id": "session-001-turn-009",
  "client": "my-ai-client",
  "title": "排查线上超时",
  "summary": "定位连接池配置并完成回归验证",
  "project": "支付服务",
  "started_at": "2026-08-31T21:30:00+08:00",
  "ended_at": "2026-08-31T23:45:00+08:00"
}' | npx weeklylog-cli hook ingest
```

提供稳定的 `id` 可保证重放幂等。工具原始输出和完整 transcript 不会写入数据库，自动耗时代表 AI 回合的可观测时长，不等同于全部人工投入。

## 开发

```bash
npm run check
npm run pack:check
```

Python CLI 和 Codex skill 位于 [`skill/`](skill/)，Node wrapper 只负责 npx 分发、skill 安装和运行时检查。
