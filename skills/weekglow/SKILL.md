---
name: weekglow
description: 从 weeklylog 本地 SQLite 工作记录生成一周的多卡片成长回顾；用少量准确文字、柔和视觉和真实的艰辛/开心瞬间，让用户看见这一周的努力。用户要求周度海报、周度视觉总结、工作幸福感回顾或“这一周我到底做了什么”时使用；不负责写入或改写工作事实。
---

# Weekglow

`weekglow` 只负责把事实库讲成一组轻量的周度视觉卡片。事实来源是 `weeklylog`；不要凭空补成果、时长、情绪或项目归因。

## 工作流

1. 解析相对于本文件的 `weeklylog` 脚本路径。通常是 `$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py`，其中 `CODEX_HOME` 默认为 `~/.codex`。
2. 先读取结构化数据，确认周一至周日的范围、记录数量、耗时、最晚结束时间、阻塞事项和复盘字段：

   ```bash
   python3 "$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py" dataset --week 2026-08-31 --json
   ```

3. 生成多卡片输出。默认保留 SVG 源文件；macOS 上可同时生成 PNG：

   ```bash
   python3 "$CODEX_HOME/skills/weeklylog/scripts/weeklylog.py" deck \
     --kind week --week 2026-08-31 \
     --output-dir ./weekglow-2026-08-31 --format both --json
   ```

   也可以直接使用 npm 快捷命令：

   ```bash
   npx weeklylog-cli weekglow --week 2026-08-31 --format both
   ```

## 卡片叙事

默认输出 6 张：

- 开场：记录了多少条工作痕迹、覆盖几天、已记录投入多久。
- 最晚的一次：最后一次明确记录的结束时间和对应事项。
- 最耗时的一件事：单条记录最长投入及其问题标题。
- 扛住的时刻：复盘里的 `hard_moment`，没有时才回退到阻塞记录。
- 小小的胜利：复盘里的 `happy_moment`，没有时回退到完成记录的成果。
- 给自己的话：幸福指数和 `self_note`。

文案要短，数据要精确。没有记录就显示“还没有记录”，不要把缺失数据估算成事实。自动采集候选仍然单独显示，不默认算入卡片。

## 视觉规则

- 多图少字：一张卡只保留一个主数字或一个短句；把完整工作清单留在周报中。
- 先展示努力，再展示结论：使用柔和渐变、留白、抽象光圈或工作桌面意象；避免 KPI 榜单式羞耻感。
- 数字、时间、项目名由 SVG/排版层叠加，不能交给插画模型生成。
- 用户要求更个性化的照片感或插画时，可调用 `imagegen` 为每张卡生成无文字背景，再把上述准确数据叠加到卡片；不要生成 logo、水印或虚构人物经历。
- 生成后向用户展示卡片文件，并说明 SVG 是可编辑源、PNG 是分享版本。

## 边界

不要把 `weekglow` 当作记录工具。若用户补充了新的事实，先调用 `$weeklylog` 写入 SQLite，再重新读取 dataset 生成卡片。若用户只要 Markdown 周报，转交 `$weeklylog` 的 `report --detail`。
