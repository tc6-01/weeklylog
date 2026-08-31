---
name: yearglow
description: 从 weeklylog 本地 SQLite 工作记录生成年度多页视觉回顾；用少量准确文字、节奏图形和真实的艰辛/开心碎片，让用户在年末看见自己走过的路。用户要求年度海报、年度长图、年度视觉总结或工作幸福感年终回顾时使用；不负责写入或改写工作事实。
---

# Yearglow

`yearglow` 把一整年的工作事实做成一组可以慢慢翻看的视觉页面。它和 `$weeklylog` 的年度 Markdown 长文互补：长文负责细节，`yearglow` 负责让努力变得可感知。

## 工作流

1. 解析相对于本文件的 `weeklylog` 脚本路径。通常是 `$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py`，其中 `CODEX_HOME` 默认为 `~/.codex`。
2. 读取年度数据集，先确认记录范围、月份节奏、项目投入、晚间结束次数和每周复盘：

   ```bash
   python3 "$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py" dataset --year 2026 --json
   ```

3. 生成年度多页卡片。默认保留 SVG 源文件；macOS 上可同时生成 PNG：

   ```bash
   python3 "$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py" deck \
     --kind year --year 2026 \
     --output-dir ./yearglow-2026 --format both --json
   ```

   也可以直接使用 npm 快捷命令：

   ```bash
   npx weeklylog-cli yearglow --year 2026 --format both
   ```

## 页面叙事

默认输出 8 张：

- 开场：全年记录数、工作日、留下痕迹的周数和已记录投入。
- 年度节奏：按月记录数量的轻量柱状图。
- 最大章节：全年单条记录最长投入及其事项。
- 深夜页：明确记录在 22:00–05:00 结束的次数，并温和提醒休息。
- 项目星座：出现频率和已记录投入最高的最多 3 个项目。
- 艰辛时刻：复盘里的最多 3 条 `hard_moment`。
- 光亮时刻：复盘里的最多 3 条 `happy_moment`。
- 给未来的自己：年度平均幸福指数和最近几条 `self_note`。

没有记录的字段必须明确显示缺失，不要估算，也不要从项目名推断“影响力”。自动采集候选始终不计入正式年度统计，除非用户先用 `$weeklylog` 审核入账。

## 视觉规则

- 多页、少字、可分享：每页只放一个主数字或一个短句；完整明细留给 `annual-report`。
- 用渐变、抽象光圈、月份柱形和留白表现节奏，不做“加班越多越优秀”的排行榜。
- 所有数字、时间、项目名和用户原话由排版层叠加，不能让图像模型生成。
- 如果用户希望更有纪念感，可调用 `imagegen` 为各页制作无文字背景，再把事实层叠加；不要加入水印、虚构事件或未经记录的情绪判断。
- 生成后展示页面缩略图或文件列表，并同时保留 SVG 源和 PNG 分享版。

## 边界

不要在 `yearglow` 中补录事实。若用户补充工作内容或复盘，先调用 `$weeklylog` 写入 SQLite，再重新导出 dataset 和年度卡片。若用户要长篇叙事、年度风险清单或逐条明细，转交 `$weeklylog` 的 `annual-report`。
