# weeklylog

`weeklylog` 是一个 local-first 的工作记录工具：数据保存在本机 SQLite，支持深度周报、年度长篇总结、成长海报，以及 Codex/其他 AI 客户端的 hook 自动采集。视觉回顾已经拆成两个独立 skill：`weekglow` 负责每周多卡片，`yearglow` 负责年度多页记忆。

由于 npm 上已有同名包，本项目的 npx 包名是 `weeklylog-cli`，命令名仍然是 `weeklylog`。

## 安装

需要 Node.js 18+ 和 Python 3。

直接使用，不写入全局文件：

```bash
npx weeklylog-cli doctor
npx weeklylog-cli init
npx weeklylog-cli init --json
npx weeklylog-cli add --content "完成登录页重构" --minutes 90 --status done
npx weeklylog-cli report --detail
```

一次安装三个 Codex skill（`weeklylog`、`weekglow`、`yearglow`）：

```bash
npx weeklylog-cli install
```

目标已存在时，使用 `--force` 更新；也可以指定 skills 根目录：

```bash
npx weeklylog-cli install --force
npx weeklylog-cli install --skills-dir /path/to/.codex/skills
```

兼容旧版的精确目标参数仍然可用（只安装 `weeklylog`）：

```bash
npx weeklylog-cli install --target /path/to/.codex/skills/weeklylog
```

默认数据库是 `~/.codex/data/weeklylog/worklog.sqlite3`，也可以通过 `--db PATH` 或 `WEEKLYLOG_DB` 覆盖。

`init --json` 会返回当前 schema 版本和迁移备份路径。首次打开旧版数据库时，weeklylog 会在改变数据结构前自动创建经过隐私裁剪的 `worklog.sqlite3.pre-migration-*` 备份，并把位置写到标准错误；历史迁移备份中的自动化兼容字段也会一并清理。迁移成功后再次运行不会重复创建备份。旧版自动采集的会话 prompt/回复兼容字段会在迁移时清空，候选和已确认工作记录会保留；后续新事件只保留有界的短摘要。

## 常用命令

```bash
npx weeklylog-cli add --content "排查线上超时" --start 21:30 --end 23:45 --project "支付服务"
npx weeklylog-cli list --week 2026-08-31 --json
npx weeklylog-cli report --week 2026-08-31 --detail
npx weeklylog-cli reflect --week 2026-08-31 --mood 4.2 --hard-moment "联调卡了三个小时"
npx weeklylog-cli poster --week 2026-08-31 --output weeklylog.png
npx weeklylog-cli annual-report --year 2026 --output weeklylog-2026.md
npx weeklylog-cli dataset --week 2026-08-31 --json
npx weeklylog-cli weekglow --week 2026-08-31 --output-dir ./weekglow-2026-08-31 --format both
npx weeklylog-cli yearglow --year 2026 --output-dir ./yearglow-2026 --format both
```

## 两个视觉 skill

`weekglow` 默认输出 6 张卡片：这一周留下多少工作痕迹、最晚干到几点、最耗时的问题、扛住的时刻、小小的胜利，以及给自己的话。

`yearglow` 默认输出 8 张页面：全年总览、月份节奏、最大投入、深夜记录、项目星座、艰辛时刻、开心瞬间和写给未来的自己。

两者都采用“少字、多图、事实叠加”的方式：SVG 负责准确数字和用户原话，渐变、光圈和节奏图形负责情绪。需要更个性化的插画或照片背景时，可在 Codex 中让 `imagegen` 生成无文字背景，再叠加事实层；缺失字段会明确显示“还没有记录”，不会把估算当成事实。

如果只需要可检索的数据，可用 `dataset` 导出 JSON；如果需要 Markdown 细节，继续使用 `report --detail` 或 `annual-report`。

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

Python CLI 和 `weeklylog` skill 位于 [`skill/`](skill/)，视觉 skills 位于 [`skills/`](skills/)。Node wrapper 负责 npx 分发、三套 skill 安装和运行时检查。
