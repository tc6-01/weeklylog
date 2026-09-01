---
name: weeklylog
description: 记录和查询日常工作，保存每周幸福指数、艰辛时刻与开心瞬间，并从本地 SQLite 生成深度周报、年度长篇总结或基础成长海报；也可接收 Codex 或其他 AI 客户端的 hook 自动采集候选。用户要求记录工作、回顾一周、补充复盘、查询历史事项、撰写周报、分析投入时间或接入自动记录时使用；少字多图的周度/年度视觉回顾请转交 $weekglow 或 $yearglow。
---

# Weeklylog

使用 `scripts/weeklylog.py` 管理唯一事实来源：本地 SQLite 工作日志与每周复盘。调用时先解析出相对于本 `SKILL.md` 的脚本绝对路径，避免依赖当前工作目录。

## 默认行为

- 数据库默认位于 `~/.codex/data/weeklylog/worklog.sqlite3`。用户指定其他数据库时传 `--db PATH`；也可使用 `WEEKLYLOG_DB`。
- 未指定日期时记录为本地当天；周范围为周一至周日。
- 每个可独立查询的工作事项保存为一条记录。保留用户原意，不把推测写入数据库。
- 状态使用 `done`、`in-progress`、`blocked`、`planned` 或 `other`。根据明确语义选择；语义不明确时使用 `done`。
- `content` 写工作事项，`result` 写可验证的成果或影响；项目、分类、结果和标签均可留空。
- 可选记录 `--start`、`--end` 和 `--minutes`；时间支持 `HH:MM` 或 `YYYY-MM-DD HH:MM`。同时提供开始和结束时自动计算分钟数，跨午夜的纯时钟结束时间按次日处理。
- 标签传入逗号分隔文本，例如 `--tags "性能,客户端"`。

## 操作

先运行 `python3 <script> --help` 或子命令的 `--help` 获取参数细节。

初始化或升级数据库：

```bash
python3 <script> init --json
```

输出包含 schema 版本和迁移备份路径。首次打开旧版数据库时，脚本会在改变数据结构前创建经过隐私裁剪的 `worklog.sqlite3.pre-migration-*` 备份；历史迁移备份中的自动化兼容字段也会一并清理。迁移成功后再次运行不会重复创建备份。旧版自动采集的会话 prompt/回复兼容字段会在迁移时清空，候选和已确认工作记录会保留；后续新事件只保留有界的短摘要。

记录工作：

```bash
python3 <script> add --date 2026-08-31 --content "完成登录页重构" --project "移动端" --category "开发" --status done --result "减少重复组件" --tags "前端,重构" --start 09:30 --end 11:45 --json
```

成功后向用户确认日期、事项和记录 ID。若一句话包含多个独立事项，分别执行 `add`。

查询记录：

```bash
python3 <script> list --week 2026-08-31 --json
python3 <script> list --from-date 2026-08-01 --to-date 2026-08-31 --project "移动端" --tag "前端" --json
```

回答历史问题时以查询结果为准。日期或周未指定时使用当前周；“上周”按本地日期计算上一自然周。

修正记录：

```bash
python3 <script> update 12 --status done --result "已上线并通过回归" --minutes 135 --json
```

只有用户明确确认删除具体记录后，才运行 `delete ID --yes`。

生成周报：

```bash
python3 <script> report --week 2026-08-31
```

标准周报可直接使用脚本的 Markdown 输出。用户要求特定风格、结构或更精炼表达时，先用 `list --week DATE --json` 读取事实，再改写；不得新增数据库中不存在的成果、数字或结论。没有记录时明确说明，并邀请用户补录。

生成深度周报，回答“这一周最晚干到几点”“什么问题最耗时”“每天投入了多久”等问题：

```bash
python3 <script> report --week 2026-08-31 --detail
python3 <script> report --week 2026-08-31 --detail --output /path/to/weeklylog-2026-08-31.md
```

深度周报包含：一眼看懂的工作量、最早开始和最晚结束、已记录总投入、按天时间线、最耗时事项、项目投入、阻塞事项和复盘。明确标注未填写耗时的记录；不要把未记录的时间估算成事实。

## 幸福复盘与海报

用户可以随时补充一周的幸福指数、最难时刻、开心瞬间和写给自己的话：

```bash
python3 <script> reflect --week 2026-08-31 --mood 4.2 --hard-moment "联调卡了三个小时，最后定位到环境配置" --happy-moment "重构上线后收到正向反馈" --self-note "允许自己辛苦，也要记得给努力一个回声"
```

只运行 `reflect --week DATE` 可读取已有复盘。用户要求个人化海报而复盘内容为空时，用一个问题同时询问“最难的一刻、值得开心的事、想对自己说的话”；用户希望直接生成时，使用已记录工作中的阻塞与成果作为事实回退，空缺处明确显示尚未记录。

生成沉浸式数据叙事海报：

```bash
python3 <script> poster --week 2026-08-31
python3 <script> poster --week 2026-08-31 --output /path/to/weeklylog.png --json
```

海报包含记录数、专注工作日、完成率、本周关键词、最难时刻、开心瞬间、幸福指数和给自己的话。默认输出 SVG；在支持 macOS Quick Look 的环境中，`.png` 输出会同时保留 SVG 源文件。生成后向用户展示图片并给出保存位置。

如果用户要多张少字多图的周度视觉故事，转交 `$weekglow`；如果要年度多页视觉回顾，转交 `$yearglow`。两个 skill 都只读本 skill 写入的事实，不会改写数据库。

生成年度长篇总结：

```bash
python3 <script> annual-report --year 2026
python3 <script> annual-report --year 2026 --output /path/to/weeklylog-2026.md
```

年度总结不是海报的简单拼接，而是按月份、项目、投入时长、最晚工作日、最耗时事项和每周复盘碎片组织的 Markdown 长文。用户说“年度总结”但没有指定年份时使用当前年份；指定“去年”时使用上一自然年。

导出给视觉 skill 或其他客户端的稳定 JSON 数据集：

```bash
python3 <script> dataset --week 2026-08-31 --json
python3 <script> dataset --year 2026 --json
```

数据集包含 `entries`、`reflections`、采集覆盖范围和单独的 `pending_candidates`；候选不计入正式统计。需要预览候选时显式使用 `dataset --draft`，输出会带 `draft: true` 和 `candidates`。

## 自动采集与 hook 集成

手动记录只保留需要本人判断的内容；自动采集用于捕捉 AI 协作的结构化标题/摘要、开始/结束时间和项目上下文。原始 prompt、模型回复和工具输出不写入 SQLite。自动采集先写入“候选”表，不会直接污染正式周报；确认后再转成工作记录。候选记录默认不计入周报和海报，深度周报会提示待确认数量。

查看、确认或忽略候选：

```bash
python3 <script> hook list --week 2026-08-31
python3 <script> hook promote 12
python3 <script> hook ignore 13
```

批量整理、合并或拆分候选：

```bash
python3 <script> hook promote-batch 12 13
python3 <script> hook ignore-batch 14,15
python3 <script> hook merge 16 17
python3 <script> hook split 18 --parts '[{"title":"子事项 A","summary":"...","evidence_ids":["..."]},{"title":"子事项 B","summary":"...","evidence_ids":["..."]}]'
```

辅助 Evidence、Outbox、保留清理与本地迁移：

```bash
python3 <script> evidence git --repo /path/to/repo --since '7 days ago' --json
python3 <script> outbox list --state pending --json
python3 <script> outbox claim --lease-minutes 10 --json
python3 <script> outbox ack EVENT_ID --claim-token CLAIM_TOKEN --json
python3 <script> cleanup --as-of 2026-08-31 --json
python3 <script> cleanup --as-of 2026-08-31 --execute --yes --json
python3 <script> backup --output /path/to/worklog.sqlite3 --json
python3 <script> export --output /path/to/worklog.json --json
python3 <script> import --input /path/to/worklog.json --json
```

`export` 的 JSON 版本固定为 `export_schema_version: 1`；未知未来的导出格式或 weeklylog schema 会拒绝导入。导入只接受本地文件，重复导入相同数据幂等，冲突或结构错误会返回机器可读错误而不会静默覆盖。Outbox ack/fail 必须回传 claim 返回的 token，避免租约过期后的旧消费者误确认。模板周报还支持 `{{time_summary}}`，显示 AI 可观测、人工确认和旧版/未分类时长。

Codex 原生 hook 配置片段可用下面的命令生成（只输出配置，不会改写现有 `~/.codex/hooks.json`）：

```bash
python3 <script> hook config --client codex > /tmp/weeklylog-codex-hooks.json
```

将输出合并到 `~/.codex/hooks.json` 后，在 Codex CLI 中运行 `/hooks` 审核并信任。默认配置捕捉 `SessionStart`、`UserPromptSubmit`、`Stop` 和 `SessionEnd`；自动来源始终先进入候选区，必须通过 `hook promote` 或 `hook promote-batch` 显式确认。

其他 AI 客户端只需把一个 JSON 事件通过 stdin 交给 `hook ingest`，即可复用同一套 SQLite、去重和候选审核逻辑；自动采集结果必须显式确认后才会成为正式记录。事件协议、隐私边界和客户端示例见 [references/integrations.md](references/integrations.md)。

自动记录的耗时表示 AI 会话/回合的可观测时长，不等同于全部人工工作时长；周报应明确标注这一点。不要把完整 transcript 或工具输出写入数据库，只保存短标题、摘要、时间和项目元数据。

## 数据边界

数据库保留在本机。除非用户明确要求，不上传、同步或发送其中的数据。需要备份或迁移时，先用 `path` 子命令确认实际数据库位置。
