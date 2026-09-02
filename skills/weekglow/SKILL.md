---
name: weekglow
description: 从 weeklylog 本地 SQLite 正式记录生成一周的少字多图视觉回顾。用户要求周度海报、这一周做了什么或工作幸福感回顾时使用；不负责写入或改写工作事实。
---

# Weekglow

`weekglow` 只消费 `$weeklylog` 写入的正式事实，不承担记录、候选审核或事实推断。

```bash
CLI="$HOME/.codex/skills/weeklylog/scripts/weeklylog.js"
node "$CLI" dataset --week 2026-08-31 --json
node "$CLI" weekglow --week 2026-08-31 --output-dir ./weekglow-2026-08-31 --json
```

默认生成六张 SVG 卡片：本周范围、正式记录数、本周最费沟通的一次、最难时刻、小小胜利和写给自己的话。沟通卡片只使用客观 AI 往返轮数和用户主动填写的协作原话，不做 sentiment score。缺失字段显示“还没有记录”，不估算数字或情绪。SVG 是可编辑源，数据层的数字和用户原话必须由排版层写入。

如果用户补充了新事实，先转交 `$weeklylog` 写入，再重新生成卡片。
