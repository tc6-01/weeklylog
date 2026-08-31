#!/usr/bin/env python3
"""Store and summarize weekly work items in a local SQLite database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DB = Path(
    os.environ.get("WEEKLYLOG_DB")
    or os.environ.get("WEEKLY_WORKLOG_DB")
    or str(Path.home() / ".codex" / "data" / "weeklylog" / "worklog.sqlite3")
).expanduser()
STATUSES = ("done", "in-progress", "blocked", "planned", "other")
HOOK_REVIEW_STATES = ("candidate", "promoted", "ignored")
STATUS_TITLES = {
    "done": "本周完成",
    "in-progress": "进行中",
    "blocked": "风险与阻塞",
    "planned": "后续计划",
    "other": "其他工作",
}


def iso_day(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期必须是 YYYY-MM-DD 格式") from exc


def mood_score(value: str) -> float:
    try:
        score = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("幸福指数必须是 1 到 5 的数字") from exc
    if not 1 <= score <= 5:
        raise argparse.ArgumentTypeError("幸福指数必须在 1 到 5 之间")
    return round(score, 1)


def week_range(day_value: str | None = None) -> tuple[str, str]:
    day = date.fromisoformat(day_value) if day_value else date.today()
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def split_tags(value: str | None) -> list[str]:
    if value is None:
        return []
    return sorted({tag.strip() for tag in value.split(",") if tag.strip()})


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("耗时必须是非负整数分钟") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("耗时必须是非负整数分钟")
    return parsed


def parse_local_datetime(value: str, work_date: str) -> tuple[datetime, bool]:
    """Parse a local date-time or HH:MM and return (value, has_explicit_date)."""
    clean = value.strip()
    if not clean:
        raise ValueError("时间不能为空")
    has_explicit_date = bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:[T ]|$)", clean))
    candidate = clean.replace(" ", "T", 1) if has_explicit_date else f"{work_date}T{clean}"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError("时间必须是 HH:MM、YYYY-MM-DD HH:MM 或 ISO 格式") from exc
    return parsed, has_explicit_date


def resolve_times(
    work_date: str,
    start_value: str | None,
    end_value: str | None,
    minutes_value: int | None,
    current: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, int | None]:
    """Normalize optional time fields and compute duration when possible."""
    current = current or {}
    raw_start = current.get("start_time") if start_value is None else start_value.strip()
    raw_end = current.get("end_time") if end_value is None else end_value.strip()
    current_minutes = current.get("duration_minutes")
    start_dt = end_dt = None
    end_has_date = False

    if raw_start:
        try:
            start_dt, _ = parse_local_datetime(raw_start, work_date)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if raw_end:
        try:
            end_dt, end_has_date = parse_local_datetime(raw_end, work_date)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if start_dt and end_dt and end_dt < start_dt:
        if not end_has_date:
            end_dt += timedelta(days=1)
        else:
            raise SystemExit("结束时间不能早于开始时间")

    if minutes_value is not None:
        duration = minutes_value
    elif start_dt and end_dt:
        duration = round((end_dt - start_dt).total_seconds() / 60)
    elif start_value is None and end_value is None:
        duration = current_minutes
    else:
        duration = None

    def serialize(value: datetime | None) -> str | None:
        return value.isoformat(timespec="minutes") if value else None

    return serialize(start_dt), serialize(end_dt), duration


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_date TEXT NOT NULL,
            content TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'done'
                CHECK (status IN ('done', 'in-progress', 'blocked', 'planned', 'other')),
            result TEXT NOT NULL DEFAULT '',
            start_time TEXT,
            end_time TEXT,
            duration_minutes INTEGER CHECK (duration_minutes >= 0),
            source TEXT NOT NULL DEFAULT 'manual',
            capture_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS entry_tags (
            entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            tag TEXT NOT NULL,
            PRIMARY KEY (entry_id, tag)
        );

        CREATE INDEX IF NOT EXISTS idx_entries_work_date ON entries(work_date);
        CREATE INDEX IF NOT EXISTS idx_entries_project ON entries(project);
        CREATE INDEX IF NOT EXISTS idx_entries_status ON entries(status);
        CREATE INDEX IF NOT EXISTS idx_entry_tags_tag ON entry_tags(tag);

        CREATE TABLE IF NOT EXISTS weekly_reflections (
            week_start TEXT PRIMARY KEY,
            mood REAL CHECK (mood BETWEEN 1 AND 5),
            headline TEXT NOT NULL DEFAULT '',
            hard_moment TEXT NOT NULL DEFAULT '',
            happy_moment TEXT NOT NULL DEFAULT '',
            self_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS automation_sessions (
            session_id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT '',
            client TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            started_at TEXT,
            ended_at TEXT,
            last_turn_id TEXT,
            last_prompt TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS automation_turns (
            session_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            ended_at TEXT,
            assistant_message TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'open'
                CHECK (state IN ('open', 'captured', 'ignored')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (session_id, turn_id)
        );

        CREATE TABLE IF NOT EXISTS capture_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            client TEXT NOT NULL DEFAULT '',
            session_id TEXT,
            turn_id TEXT,
            work_date TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'AI协作',
            work_status TEXT NOT NULL DEFAULT 'done'
                CHECK (work_status IN ('done', 'in-progress', 'blocked', 'planned', 'other')),
            start_time TEXT,
            end_time TEXT,
            duration_minutes INTEGER CHECK (duration_minutes >= 0),
            cwd TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '',
            review_state TEXT NOT NULL DEFAULT 'candidate'
                CHECK (review_state IN ('candidate', 'promoted', 'ignored')),
            entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_capture_candidates_week
            ON capture_candidates(work_date, review_state);
        CREATE INDEX IF NOT EXISTS idx_capture_candidates_session
            ON capture_candidates(session_id, turn_id);
        """
    )
    # Migrate databases created by earlier versions without rewriting user data.
    ensure_column(conn, "entries", "start_time", "TEXT")
    ensure_column(conn, "entries", "end_time", "TEXT")
    ensure_column(conn, "entries", "duration_minutes", "INTEGER")
    ensure_column(conn, "entries", "source", "TEXT NOT NULL DEFAULT 'manual'")
    ensure_column(conn, "entries", "capture_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)")
    conn.commit()
    return conn


def entry_by_id(conn: sqlite3.Connection, entry_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT e.*,
               COALESCE((
                   SELECT group_concat(tag, ',')
                   FROM (SELECT tag FROM entry_tags WHERE entry_id = e.id ORDER BY tag)
               ), '') AS tags
        FROM entries e
        WHERE e.id = ?
        """,
        (entry_id,),
    ).fetchone()
    return row_to_dict(row) if row else None


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = split_tags(item.get("tags"))
    return item


def replace_tags(conn: sqlite3.Connection, entry_id: int, tags: Iterable[str]) -> None:
    conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
    conn.executemany(
        "INSERT INTO entry_tags(entry_id, tag) VALUES (?, ?)",
        [(entry_id, tag) for tag in tags],
    )


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def reflection_for_week(
    conn: sqlite3.Connection, day_value: str | None
) -> dict[str, Any]:
    start, end = week_range(day_value)
    row = conn.execute(
        "SELECT * FROM weekly_reflections WHERE week_start = ?", (start,)
    ).fetchone()
    if row:
        item = dict(row)
    else:
        item = {
            "week_start": start,
            "mood": None,
            "headline": "",
            "hard_moment": "",
            "happy_moment": "",
            "self_note": "",
            "created_at": None,
            "updated_at": None,
        }
    item["week_end"] = end
    return item


def reflections_for_range(
    conn: sqlite3.Connection, start: str, end: str
) -> list[dict[str, Any]]:
    """Return reflections whose six-day week overlaps an inclusive date range."""
    range_start = date.fromisoformat(start)
    range_end = date.fromisoformat(end)
    rows = conn.execute(
        "SELECT * FROM weekly_reflections WHERE week_start <= ? ORDER BY week_start",
        (end,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        reflection_start = date.fromisoformat(item["week_start"])
        reflection_end = reflection_start + timedelta(days=6)
        if reflection_end >= range_start and reflection_start <= range_end:
            item["week_end"] = reflection_end.isoformat()
            result.append(item)
    return result


def reflect(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    current = reflection_for_week(conn, args.week)
    provided = {
        "mood": args.mood,
        "headline": args.headline,
        "hard_moment": args.hard_moment,
        "happy_moment": args.happy_moment,
        "self_note": args.self_note,
    }
    if all(value is None for value in provided.values()):
        print_json(current)
        return

    values = {
        name: current[name] if value is None else value.strip() if isinstance(value, str) else value
        for name, value in provided.items()
    }
    now = timestamp()
    created_at = current["created_at"] or now
    conn.execute(
        """
        INSERT INTO weekly_reflections(
            week_start, mood, headline, hard_moment, happy_moment, self_note,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(week_start) DO UPDATE SET
            mood = excluded.mood,
            headline = excluded.headline,
            hard_moment = excluded.hard_moment,
            happy_moment = excluded.happy_moment,
            self_note = excluded.self_note,
            updated_at = excluded.updated_at
        """,
        (
            current["week_start"],
            values["mood"],
            values["headline"],
            values["hard_moment"],
            values["happy_moment"],
            values["self_note"],
            created_at,
            now,
        ),
    )
    conn.commit()
    print_json(reflection_for_week(conn, args.week))


def add_entry(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    now = timestamp()
    work_date = args.date or date.today().isoformat()
    content = args.content.strip()
    if not content:
        raise SystemExit("工作事项不能为空")
    start_time, end_time, duration_minutes = resolve_times(
        work_date, args.start, args.end, args.minutes
    )
    cursor = conn.execute(
        """
        INSERT INTO entries(
            work_date, content, project, category, status, result,
            start_time, end_time, duration_minutes, source, capture_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work_date,
            content,
            args.project.strip(),
            args.category.strip(),
            args.status,
            args.result.strip(),
            start_time,
            end_time,
            duration_minutes,
            getattr(args, "source", "manual"),
            None,
            now,
            now,
        ),
    )
    entry_id = int(cursor.lastrowid)
    replace_tags(conn, entry_id, split_tags(args.tags))
    conn.commit()
    item = entry_by_id(conn, entry_id)
    if args.json:
        print_json(item)
    else:
        print(f"已记录 #{entry_id}: {work_date} {content}")


def capture_text(value: Any, limit: int = 800) -> str:
    """Normalize hook text and redact common credential-shaped values."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    value = re.sub(
        r"(?i)\b(?:sk-[a-z0-9_-]+|api[ _-]?key|access[ _-]?token|authorization|token|password|secret)\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+",
        "[已隐藏]",
        value,
    )
    value = " ".join(value.split()).strip()
    if len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def event_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=datetime.now().astimezone().tzinfo)
        except (OverflowError, OSError, ValueError):
            return None
    clean = str(value).strip()
    if not clean:
        return None
    if clean.endswith("Z"):
        clean = clean[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(clean)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def event_field(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload and payload[name] not in (None, ""):
            return payload[name]
    return None


def event_duration_minutes(
    payload: dict[str, Any], start: datetime | None, end: datetime | None
) -> int | None:
    raw = event_field(payload, "duration_minutes", "durationMinutes", "minutes")
    if raw not in (None, ""):
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = None
        if value is not None and value >= 0:
            return value
    if start and end:
        try:
            return max(0, round((end - start).total_seconds() / 60))
        except TypeError:
            return None
    return None


def event_kind(name: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return {
        "sessionstart": "session_start",
        "sessionend": "session_end",
        "userpromptsubmit": "prompt",
        "prompt": "prompt",
        "turnstart": "prompt",
        "turnend": "turn_end",
        "stop": "turn_end",
        "taskcompleted": "task_completed",
        "taskcomplete": "task_completed",
        "completed": "task_completed",
    }.get(normalized)


def hook_client(payload: dict[str, Any], event_name: str) -> str:
    value = event_field(payload, "client", "provider", "app", "source_client")
    if value:
        return capture_text(value, 80).lower().replace(" ", "-")
    if event_name.lower() in {
        "sessionstart",
        "sessionend",
        "userpromptsubmit",
        "stop",
        "pretooluse",
        "posttooluse",
        "permissionrequest",
        "subagentstart",
        "subagentstop",
    }:
        return "codex"
    return "generic"


def upsert_automation_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    client: str,
    source: str,
    cwd: str,
    started_at: str | None = None,
    ended_at: str | None = None,
    turn_id: str | None = None,
    prompt: str | None = None,
) -> None:
    now = timestamp()
    row = conn.execute(
        "SELECT * FROM automation_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO automation_sessions(
                session_id, source, client, cwd, started_at, ended_at,
                last_turn_id, last_prompt, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                source,
                client,
                cwd,
                started_at,
                ended_at,
                turn_id,
                prompt or "",
                now,
                now,
            ),
        )
        return
    updates: list[tuple[str, Any]] = []
    for name, value in (
        ("source", source),
        ("client", client),
        ("cwd", cwd),
        ("started_at", started_at),
        ("ended_at", ended_at),
        ("last_turn_id", turn_id),
        ("last_prompt", prompt),
    ):
        if value not in (None, ""):
            if name == "started_at" and row["started_at"]:
                continue
            updates.append((name, value))
    if updates:
        assignments = ", ".join(f"{name} = ?" for name, _ in updates)
        params = [value for _, value in updates] + [now, session_id]
        conn.execute(
            f"UPDATE automation_sessions SET {assignments}, updated_at = ? WHERE session_id = ?",
            params,
        )
    else:
        conn.execute(
            "UPDATE automation_sessions SET updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )


def upsert_automation_turn(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    turn_id: str,
    client: str,
    prompt: str,
    started_at: str,
) -> None:
    now = timestamp()
    row = conn.execute(
        "SELECT * FROM automation_turns WHERE session_id = ? AND turn_id = ?",
        (session_id, turn_id),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO automation_turns(
                session_id, turn_id, source, prompt, started_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, turn_id, client, prompt, started_at, now),
        )
        return
    conn.execute(
        """
        UPDATE automation_turns
        SET source = CASE WHEN ? <> '' THEN ? ELSE source END,
            prompt = CASE WHEN ? <> '' THEN ? ELSE prompt END,
            started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
            updated_at = ?
        WHERE session_id = ? AND turn_id = ?
        """,
        (client, client, prompt, prompt, started_at, now, session_id, turn_id),
    )


def open_turn_for_session(
    conn: sqlite3.Connection, session_id: str, turn_id: str | None
) -> dict[str, Any] | None:
    if turn_id:
        row = conn.execute(
            """
            SELECT * FROM automation_turns
            WHERE session_id = ? AND turn_id = ? AND state = 'open'
            """,
            (session_id, turn_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT * FROM automation_turns
            WHERE session_id = ? AND state = 'open'
            ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (session_id,),
        ).fetchone()
    return dict(row) if row else None


def candidate_by_id(
    conn: sqlite3.Connection, candidate_id: int
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM capture_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_capture_candidate(
    conn: sqlite3.Connection, candidate: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    existing = conn.execute(
        "SELECT * FROM capture_candidates WHERE capture_id = ?",
        (candidate["capture_id"],),
    ).fetchone()
    if existing:
        return dict(existing), False
    now = timestamp()
    cursor = conn.execute(
        """
        INSERT INTO capture_candidates(
            capture_id, source, client, session_id, turn_id, work_date,
            title, summary, project, category, work_status, start_time,
            end_time, duration_minutes, cwd, tags, review_state, created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
        """,
        (
            candidate["capture_id"],
            candidate["source"],
            candidate["client"],
            candidate.get("session_id"),
            candidate.get("turn_id"),
            candidate["work_date"],
            candidate["title"],
            candidate.get("summary", ""),
            candidate.get("project", ""),
            candidate.get("category", "AI协作"),
            candidate.get("work_status", "done"),
            candidate.get("start_time"),
            candidate.get("end_time"),
            candidate.get("duration_minutes"),
            candidate.get("cwd", ""),
            candidate.get("tags", ""),
            now,
            now,
        ),
    )
    item = candidate_by_id(conn, int(cursor.lastrowid))
    if item is None:
        raise SystemExit("自动采集候选写入失败")
    return item, True


def promote_capture(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    status: str | None = None,
    content: str | None = None,
    result: str | None = None,
    tags: str | None = None,
) -> dict[str, Any]:
    if candidate["review_state"] == "promoted" and candidate.get("entry_id"):
        item = entry_by_id(conn, int(candidate["entry_id"]))
        if item:
            return item
    existing = conn.execute(
        "SELECT id FROM entries WHERE capture_id = ?", (candidate["capture_id"],)
    ).fetchone()
    if existing:
        entry_id = int(existing[0])
    else:
        now = timestamp()
        final_content = (content if content is not None else candidate["title"]).strip()
        if not final_content:
            raise SystemExit("正式工作记录的内容不能为空")
        cursor = conn.execute(
            """
            INSERT INTO entries(
                work_date, content, project, category, status, result,
                start_time, end_time, duration_minutes, source, capture_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'hook', ?, ?, ?)
            """,
            (
                candidate["work_date"],
                final_content,
                candidate.get("project", ""),
                candidate.get("category", "AI协作"),
                status or candidate.get("work_status") or "done",
                (result if result is not None else candidate.get("summary", "")).strip(),
                candidate.get("start_time"),
                candidate.get("end_time"),
                candidate.get("duration_minutes"),
                candidate["capture_id"],
                now,
                now,
            ),
        )
        entry_id = int(cursor.lastrowid)
    replace_tags(
        conn,
        entry_id,
        split_tags(tags if tags is not None else candidate.get("tags", "")),
    )
    conn.execute(
        """
        UPDATE capture_candidates
        SET review_state = 'promoted', entry_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (entry_id, timestamp(), candidate["id"]),
    )
    conn.commit()
    item = entry_by_id(conn, entry_id)
    if item is None:
        raise SystemExit("自动采集候选转正失败")
    return item


def read_hook_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.event_file:
        text = Path(args.event_file).expanduser().read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    text = text.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"hook 输入必须是 JSON 对象：{exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("hook 输入必须是 JSON 对象")
    return payload


def hook_ingest(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    try:
        payload = read_hook_payload(args)
    except (OSError, SystemExit) as exc:
        if args.hook_output:
            print("{}")
            return
        raise exc
    if not payload:
        result = {"action": "ignored", "reason": "empty_payload"}
        if args.json:
            print_json(result)
        elif args.hook_output:
            print("{}")
        return

    event_name = str(event_field(payload, "hook_event_name", "event", "type") or "")
    kind = event_kind(event_name)
    client = hook_client(payload, event_name)
    session_id = capture_text(event_field(payload, "session_id", "sessionId"), 160)
    turn_id = capture_text(event_field(payload, "turn_id", "turnId"), 160)
    cwd = capture_text(event_field(payload, "cwd", "workdir", "working_directory"), 500)
    started_dt = event_datetime(
        event_field(payload, "started_at", "startedAt", "start_time", "startTime")
    )
    ended_dt = event_datetime(
        event_field(payload, "ended_at", "endedAt", "end_time", "endTime")
    )
    now_dt = datetime.now().astimezone()
    if kind == "session_start":
        if session_id:
            upsert_automation_session(
                conn,
                session_id=session_id,
                client=client,
                source=event_name,
                cwd=cwd,
                started_at=(started_dt or now_dt).isoformat(timespec="seconds"),
            )
            conn.commit()
        result = {"action": "session_started", "session_id": session_id, "client": client}
    elif kind == "session_end":
        if session_id:
            upsert_automation_session(
                conn,
                session_id=session_id,
                client=client,
                source=event_name,
                cwd=cwd,
                ended_at=(ended_dt or now_dt).isoformat(timespec="seconds"),
            )
            conn.commit()
        result = {"action": "session_ended", "session_id": session_id, "client": client}
    elif kind == "prompt":
        prompt = capture_text(
            event_field(payload, "prompt", "user_prompt", "message", "input"), 1000
        )
        if not prompt:
            result = {"action": "ignored", "reason": "empty_prompt"}
        else:
            if not session_id:
                session_id = "session-" + hashlib.sha256(
                    f"{client}|{cwd}".encode("utf-8")
                ).hexdigest()[:20]
            if not turn_id:
                turn_id = "turn-" + hashlib.sha256(
                    f"{session_id}|{prompt}|{started_dt.isoformat(timespec='seconds')}".encode(
                        "utf-8"
                    )
                ).hexdigest()[:20]
            started_text = (started_dt or now_dt).isoformat(timespec="seconds")
            upsert_automation_session(
                conn,
                session_id=session_id,
                client=client,
                source=event_name,
                cwd=cwd,
                started_at=started_text,
                turn_id=turn_id,
                prompt=prompt,
            )
            upsert_automation_turn(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                client=client,
                prompt=prompt,
                started_at=started_text,
            )
            conn.commit()
            result = {
                "action": "turn_started",
                "session_id": session_id,
                "turn_id": turn_id,
            }
    elif kind in {"turn_end", "task_completed"}:
        turn = open_turn_for_session(conn, session_id, turn_id) if session_id else None
        prompt = capture_text(
            event_field(payload, "title", "content", "prompt", "user_prompt"), 800
        ) or (capture_text(turn.get("prompt"), 800) if turn else "")
        summary = capture_text(
            event_field(
                payload,
                "summary",
                "result",
                "last_assistant_message",
                "assistant_message",
                "output",
            ),
            1200,
        )
        if not summary and turn:
            summary = capture_text(turn.get("assistant_message"), 1200)
        if not prompt and not summary:
            result = {"action": "ignored", "reason": "no_work_text"}
        else:
            if not turn_id and turn:
                turn_id = str(turn["turn_id"])
            start = started_dt or (event_datetime(turn.get("started_at")) if turn else None)
            end = ended_dt or now_dt
            work_date = str(event_field(payload, "work_date", "date") or "")
            if work_date:
                try:
                    work_date = date.fromisoformat(work_date).isoformat()
                except ValueError:
                    work_date = ""
            if not work_date:
                work_date = (start or end).astimezone().date().isoformat()
            project = capture_text(event_field(payload, "project", "project_name"), 200)
            if not project and cwd:
                project = Path(cwd).name
            category = capture_text(event_field(payload, "category"), 100) or "AI协作"
            raw_status = str(event_field(payload, "status", "work_status") or "done")
            work_status = raw_status if raw_status in STATUSES else "done"
            tags_value = event_field(payload, "tags")
            if isinstance(tags_value, list):
                tags_value = ",".join(str(value) for value in tags_value)
            tags = split_tags(str(tags_value or ""))
            if "自动采集" not in tags:
                tags.append("自动采集")
            capture_id = capture_text(
                event_field(payload, "capture_id", "event_id", "eventId", "id"), 200
            )
            if not capture_id:
                if session_id and turn_id:
                    seed = "|".join([client, session_id, turn_id, kind])
                else:
                    seed = "|".join(
                        [client, event_name, session_id, turn_id, work_date, prompt, summary]
                    )
                capture_id = f"{client}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            candidate_data = {
                "capture_id": capture_id,
                "source": "hook",
                "client": client,
                "session_id": session_id or None,
                "turn_id": turn_id or None,
                "work_date": work_date,
                "title": prompt or summary[:120],
                "summary": summary,
                "project": project,
                "category": category,
                "work_status": work_status,
                "start_time": start.isoformat(timespec="seconds") if start else None,
                "end_time": end.isoformat(timespec="seconds") if end else None,
                "duration_minutes": event_duration_minutes(payload, start, end),
                "cwd": cwd,
                "tags": ",".join(tags),
            }
            candidate, created = insert_capture_candidate(conn, candidate_data)
            if session_id and turn_id:
                conn.execute(
                    """
                    UPDATE automation_turns
                    SET ended_at = ?, assistant_message = ?, state = 'captured', updated_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        candidate.get("end_time"),
                        summary,
                        timestamp(),
                        session_id,
                        turn_id,
                    ),
                )
            conn.commit()
            promoted = None
            if args.auto_approve:
                promoted = promote_capture(conn, candidate)
                candidate = candidate_by_id(conn, int(candidate["id"])) or candidate
            result = {
                "action": "candidate_created" if created else "duplicate_ignored",
                "candidate": candidate,
                "promoted_entry": promoted,
            }
    else:
        result = {"action": "ignored", "reason": "event_not_recorded", "event": event_name}

    if args.hook_output:
        print("{}")
    elif args.json:
        print_json(result)


def candidate_range(args: argparse.Namespace) -> tuple[str, str]:
    if args.week:
        return week_range(args.week)
    if bool(args.from_date) != bool(args.to_date):
        raise SystemExit("--from-date 和 --to-date 必须一起使用")
    if args.from_date and args.to_date:
        if args.from_date > args.to_date:
            raise SystemExit("--from-date 不能晚于 --to-date")
        return args.from_date, args.to_date
    return week_range()


def hook_list(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    start, end = candidate_range(args)
    where = ["work_date BETWEEN ? AND ?"]
    params: list[Any] = [start, end]
    if args.state:
        where.append("review_state = ?")
        params.append(args.state)
    rows = conn.execute(
        f"SELECT * FROM capture_candidates WHERE {' AND '.join(where)} ORDER BY work_date, id",
        params,
    ).fetchall()
    items = [dict(row) for row in rows]
    if args.json:
        print_json({"from": start, "to": end, "count": len(items), "candidates": items})
        return
    if not items:
        print(f"{start} 至 {end} 没有自动采集候选。")
        return
    print("ID\t日期\t耗时\t状态\t客户端\t项目\t标题")
    for item in items:
        print(
            "\t".join(
                [
                    str(item["id"]),
                    item["work_date"],
                    format_duration(item["duration_minutes"]),
                    item["review_state"],
                    item["client"],
                    clipped(item["project"], 18),
                    clipped(item["title"], 70),
                ]
            )
        )


def hook_promote(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    candidate = candidate_by_id(conn, args.id)
    if candidate is None:
        raise SystemExit(f"找不到自动采集候选 #{args.id}")
    if args.status and args.status not in STATUSES:
        raise SystemExit(f"状态必须是：{', '.join(STATUSES)}")
    item = promote_capture(
        conn,
        candidate,
        status=args.status,
        content=args.content,
        result=args.result,
        tags=args.tags,
    )
    if args.json:
        print_json(item)
    else:
        print(f"已确认自动采集候选 #{args.id}，生成工作记录 #{item['id']}")


def hook_ignore(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    candidate = candidate_by_id(conn, args.id)
    if candidate is None:
        raise SystemExit(f"找不到自动采集候选 #{args.id}")
    conn.execute(
        "UPDATE capture_candidates SET review_state = 'ignored', updated_at = ? WHERE id = ?",
        (timestamp(), args.id),
    )
    conn.commit()
    if args.json:
        print_json(candidate_by_id(conn, args.id))
    else:
        print(f"已忽略自动采集候选 #{args.id}")


def hook_config(args: argparse.Namespace) -> None:
    if args.client != "codex":
        raise SystemExit("当前只内置 Codex 配置；其他客户端请使用 hook ingest 的通用 JSON 协议")
    script = shlex.quote(str(Path(__file__).resolve()))
    command = f"python3 {script} hook ingest --hook-output"
    if args.auto_approve:
        command += " --auto-approve"

    def handler(*, timeout: int = 10, async_run: bool = False) -> dict[str, Any]:
        item: dict[str, Any] = {
            "type": "command",
            "command": command,
            "timeout": timeout,
        }
        if async_run:
            item["async"] = True
        return item

    config = {
        "description": "Weeklylog automatic work capture.",
        "hooks": {
            "SessionStart": [{"hooks": [handler()]}],
            "UserPromptSubmit": [{"hooks": [handler()]}],
            "Stop": [{"hooks": [handler()]}],
            "SessionEnd": [{"matcher": "other", "hooks": [handler(timeout=3, async_run=False)]}],
        },
    }
    print(json.dumps(config, ensure_ascii=False, indent=2))


def resolve_list_range(args: argparse.Namespace) -> tuple[str, str]:
    if args.week and args.to_date:
        raise SystemExit("--week 不能与 --to-date 一起使用")
    if args.week:
        return week_range(args.week)
    if bool(args.from_date) != bool(args.to_date):
        raise SystemExit("--from-date 和 --to-date 必须一起使用")
    if args.from_date and args.to_date:
        if args.from_date > args.to_date:
            raise SystemExit("--from-date 不能晚于 --to-date")
        return args.from_date, args.to_date
    return week_range()


def query_entries(
    conn: sqlite3.Connection, args: argparse.Namespace
) -> tuple[list[dict[str, Any]], str, str]:
    start, end = resolve_list_range(args)
    where = ["e.work_date BETWEEN ? AND ?"]
    params: list[Any] = [start, end]

    if args.project:
        where.append("e.project = ?")
        params.append(args.project)
    if args.category:
        where.append("e.category = ?")
        params.append(args.category)
    if args.status:
        where.append("e.status = ?")
        params.append(args.status)
    if args.search:
        where.append(
            "(e.content LIKE ? OR e.result LIKE ? OR e.project LIKE ? OR e.category LIKE ?)"
        )
        needle = f"%{args.search}%"
        params.extend([needle, needle, needle, needle])
    for tag in args.tag:
        where.append(
            "EXISTS (SELECT 1 FROM entry_tags f WHERE f.entry_id = e.id AND f.tag = ?)"
        )
        params.append(tag)

    sql = f"""
        SELECT e.*,
               COALESCE((
                   SELECT group_concat(tag, ',')
                   FROM (SELECT tag FROM entry_tags WHERE entry_id = e.id ORDER BY tag)
               ), '') AS tags
        FROM entries e
        WHERE {' AND '.join(where)}
        ORDER BY e.work_date ASC, e.id ASC
    """
    rows = conn.execute(sql, params).fetchall()
    return [row_to_dict(row) for row in rows], start, end


def entries_for_week(
    conn: sqlite3.Connection, day_value: str | None
) -> tuple[list[dict[str, Any]], str, str]:
    query_args = argparse.Namespace(
        week=day_value,
        from_date=None,
        to_date=None,
        project=None,
        category=None,
        status=None,
        search=None,
        tag=[],
    )
    return query_entries(conn, query_args)


def pending_candidate_count(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) FROM capture_candidates
        WHERE work_date BETWEEN ? AND ? AND review_state = 'candidate'
        """,
        (start, end),
    ).fetchone()
    return int(row[0]) if row else 0


def pending_candidates(
    conn: sqlite3.Connection, start: str, end: str, limit: int = 10
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM capture_candidates
        WHERE work_date BETWEEN ? AND ? AND review_state = 'candidate'
        ORDER BY work_date, id LIMIT ?
        """,
        (start, end, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def clipped(value: str, width: int) -> str:
    clean = " ".join(value.split())
    return clean if len(clean) <= width else clean[: width - 1] + "…"


def list_entries(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    items, start, end = query_entries(conn, args)
    if args.json:
        print_json({"from": start, "to": end, "count": len(items), "entries": items})
        return
    if not items:
        print(f"{start} 至 {end} 暂无记录。")
        return
    print("ID\t日期\t开始\t结束\t耗时\t状态\t项目\t分类\t工作内容\t结果\t标签")
    for item in items:
        print(
            "\t".join(
                [
                    str(item["id"]),
                    item["work_date"],
                    item["start_time"] or "",
                    item["end_time"] or "",
                    format_duration(item["duration_minutes"]),
                    item["status"],
                    clipped(item["project"], 16),
                    clipped(item["category"], 12),
                    clipped(item["content"], 48),
                    clipped(item["result"], 36),
                    ",".join(item["tags"]),
                ]
            )
        )


def entries_for_range(
    conn: sqlite3.Connection, start: str, end: str
) -> list[dict[str, Any]]:
    query_args = argparse.Namespace(
        week=None,
        from_date=start,
        to_date=end,
        project=None,
        category=None,
        status=None,
        search=None,
        tag=[],
    )
    items, _, _ = query_entries(conn, query_args)
    return items


def dataset_export(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if bool(args.from_date) != bool(args.to_date):
        raise SystemExit("--from-date 和 --to-date 必须一起使用")
    if args.week and args.to_date:
        raise SystemExit("--week 不能与 --to-date 一起使用")
    if args.week:
        start, end = week_range(args.week)
        label = f"week:{start}"
    elif args.year:
        start, end = f"{args.year:04d}-01-01", f"{args.year:04d}-12-31"
        label = f"year:{args.year:04d}"
    elif args.from_date and args.to_date:
        if args.from_date > args.to_date:
            raise SystemExit("--from-date 不能晚于 --to-date")
        start, end = args.from_date, args.to_date
        label = f"range:{start}:{end}"
    else:
        start, end = week_range()
        label = f"week:{start}"

    items = entries_for_range(conn, start, end)
    payload = {
        "schema_version": 1,
        "range": {"from": start, "to": end, "label": label},
        "entries": items,
        "reflections": reflections_for_range(conn, start, end),
        "pending_candidates": pending_candidates(conn, start, end, limit=100),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded, encoding="utf-8")
        print(output_path)
    else:
        print(encoded, end="")


def update_entry(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    current = entry_by_id(conn, args.id)
    if current is None:
        raise SystemExit(f"找不到记录 #{args.id}")

    fields = {
        "work_date": args.date,
        "content": args.content.strip() if args.content is not None else None,
        "project": args.project.strip() if args.project is not None else None,
        "category": args.category.strip() if args.category is not None else None,
        "status": args.status,
        "result": args.result.strip() if args.result is not None else None,
    }
    if fields["content"] == "":
        raise SystemExit("工作事项不能为空")
    updates = [(name, value) for name, value in fields.items() if value is not None]
    time_changed = any(value is not None for value in (args.start, args.end, args.minutes))
    if time_changed:
        start_time, end_time, duration_minutes = resolve_times(
            fields["work_date"] or current["work_date"],
            args.start,
            args.end,
            args.minutes,
            current,
        )
        fields.update(
            {
                "start_time": start_time,
                "end_time": end_time,
                "duration_minutes": duration_minutes,
            }
        )
        updates = [
            (name, value)
            for name, value in fields.items()
            if value is not None and name not in {"start_time", "end_time", "duration_minutes"}
        ]
        updates.extend(
            (name, fields[name]) for name in ("start_time", "end_time", "duration_minutes")
        )

    if not updates and args.tags is None:
        raise SystemExit("没有提供需要更新的字段")

    if updates:
        assignments = ", ".join(f"{name} = ?" for name, _ in updates)
        params = [value for _, value in updates]
        params.extend([timestamp(), args.id])
        conn.execute(
            f"UPDATE entries SET {assignments}, updated_at = ? WHERE id = ?", params
        )
    if args.tags is not None:
        replace_tags(conn, args.id, split_tags(args.tags))
        conn.execute(
            "UPDATE entries SET updated_at = ? WHERE id = ?", (timestamp(), args.id)
        )
    conn.commit()
    item = entry_by_id(conn, args.id)
    if args.json:
        print_json(item)
    else:
        print(f"已更新记录 #{args.id}")


def delete_entry(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    item = entry_by_id(conn, args.id)
    if item is None:
        raise SystemExit(f"找不到记录 #{args.id}")
    if not args.yes:
        raise SystemExit("删除需要显式传入 --yes")
    conn.execute("DELETE FROM entries WHERE id = ?", (args.id,))
    conn.commit()
    print(f"已删除记录 #{args.id}: {item['work_date']} {item['content']}")


def format_duration(minutes: int | None) -> str:
    if minutes is None:
        return "未记录"
    hours, remainder = divmod(int(minutes), 60)
    if hours and remainder:
        return f"{hours}小时{remainder}分钟"
    if hours:
        return f"{hours}小时"
    return f"{remainder}分钟"


def format_clock(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%m-%d %H:%M")
    except ValueError:
        return value


def markdown_item(item: dict[str, Any]) -> str:
    labels = [value for value in (item["project"], item["category"]) if value]
    prefix = f"**{' / '.join(labels)}**：" if labels else ""
    details = []
    if item["result"]:
        details.append(f"成果：{item['result']}")
    if item["tags"]:
        details.append(f"标签：{', '.join(item['tags'])}")
    if item.get("source") and item["source"] != "manual":
        details.append(f"来源：{item['source']}")
    if item.get("duration_minutes") is not None:
        details.append(f"耗时：{format_duration(item['duration_minutes'])}")
    elif item.get("start_time") or item.get("end_time"):
        details.append(
            f"时间：{format_clock(item.get('start_time')) or '—'}–{format_clock(item.get('end_time')) or '—'}"
        )
    suffix = f"（{'；'.join(details)}）" if details else ""
    return f"- {item['work_date']} · {prefix}{item['content']}{suffix}"


def report(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if args.detail:
        detailed_report(args, conn)
        return
    items, start, end = entries_for_week(conn, args.week)
    reflection = reflection_for_week(conn, args.week)
    pending_captures = pending_candidate_count(conn, start, end)
    counts = {status: 0 for status in STATUSES}
    for item in items:
        counts[item["status"]] += 1

    lines = [f"# 周报（{start} 至 {end}）", ""]
    if pending_captures:
        lines.extend(
            [
                f"> 自动采集到 {pending_captures} 条候选，尚未计入正式统计；运行 `hook list --week {start}` 查看，确认后用 `hook promote ID` 入账。",
                "",
            ]
        )
    if not items:
        lines.append("本周暂无工作记录。")
    else:
        lines.extend(
            [
                f"> 共记录 {len(items)} 项：完成 {counts['done']} 项，进行中 {counts['in-progress']} 项，阻塞 {counts['blocked']} 项，计划 {counts['planned']} 项。",
                "",
            ]
        )
        for status in STATUSES:
            group = [item for item in items if item["status"] == status]
            if not group:
                continue
            lines.extend([f"## {STATUS_TITLES[status]}", ""])
            lines.extend(markdown_item(item) for item in group)
            lines.append("")

    reflection_lines = []
    if reflection["mood"] is not None:
        reflection_lines.append(f"- 本周幸福指数：{reflection['mood']:g} / 5")
    if reflection["hard_moment"]:
        reflection_lines.append(f"- 最难的一刻：{reflection['hard_moment']}")
    if reflection["happy_moment"]:
        reflection_lines.append(f"- 值得开心的事：{reflection['happy_moment']}")
    if reflection["self_note"]:
        reflection_lines.append(f"- 给自己的话：{reflection['self_note']}")
    if reflection_lines:
        lines.extend(["", "## 本周复盘", "", *reflection_lines, ""])

    output = "\n".join(lines).rstrip() + "\n"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(output, encoding="utf-8")
        print(output_path)
    else:
        print(output, end="")


def write_text_output(output: str, output_path: str | None) -> None:
    if output_path:
        path = Path(output_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        print(path)
    else:
        print(output, end="")


def item_datetime(item: dict[str, Any], field: str) -> datetime | None:
    value = item.get(field)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_late_finish(value: datetime | None) -> bool:
    return bool(value and (value.hour >= 22 or value.hour < 5))


def time_range_text(item: dict[str, Any]) -> str:
    start = format_clock(item.get("start_time"))
    end = format_clock(item.get("end_time"))
    if start and end:
        return f"{start}–{end}"
    if start:
        return f"{start} 开始"
    if end:
        return f"{end} 结束"
    return "时间未记录"


def project_summary(items: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    counts: Counter[str] = Counter()
    minutes: Counter[str] = Counter()
    for item in items:
        project = item["project"] or "未分类项目"
        counts[project] += 1
        if item["duration_minutes"] is not None:
            minutes[project] += int(item["duration_minutes"])
    return sorted(
        ((project, counts[project], minutes[project]) for project in counts),
        key=lambda value: (-value[2], -value[1], value[0]),
    )


def detailed_report(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    items, start, end = entries_for_week(conn, args.week)
    reflection = reflection_for_week(conn, args.week)
    pending_captures = pending_candidate_count(conn, start, end)
    counts = Counter(item["status"] for item in items)
    tracked = [item for item in items if item["duration_minutes"] is not None]
    total_minutes = sum(int(item["duration_minutes"]) for item in tracked)
    late_item = max(
        (item for item in items if item_datetime(item, "end_time")),
        key=lambda item: item_datetime(item, "end_time"),
        default=None,
    )
    late_night_items = [
        item
        for item in items
        if is_late_finish(item_datetime(item, "end_time"))
    ]
    earliest_item = min(
        (item for item in items if item_datetime(item, "start_time")),
        key=lambda item: item_datetime(item, "start_time"),
        default=None,
    )

    lines = [f"# 深度周报（{start} 至 {end}）", ""]
    lines.extend(
        [
            "## 一眼看懂",
            "",
            f"- 工作事项：{len(items)} 项（完成 {counts['done']}，进行中 {counts['in-progress']}，阻塞 {counts['blocked']}，计划 {counts['planned']}）",
            f"- 有耗时记录：{len(tracked)} 项，合计 {format_duration(total_minutes)}",
            f"- 覆盖工作日：{len({item['work_date'] for item in items})} 天",
            f"- 最早开始：{format_clock(earliest_item.get('start_time')) if earliest_item else '未记录'}",
            f"- 最晚结束：{format_clock(late_item.get('end_time')) if late_item else '未记录'}",
            f"- 晚间/凌晨结束（22:00–05:00）：{len(late_night_items)} 次",
            f"- 自动采集待确认：{pending_captures} 条（不计入正式统计）",
            "",
        ]
    )

    lines.extend(["## 每天发生了什么", ""])
    if not items:
        lines.append("本周暂无工作记录。")
    else:
        for work_date in sorted({item["work_date"] for item in items}):
            day_items = [item for item in items if item["work_date"] == work_date]
            day_minutes = sum(
                int(item["duration_minutes"])
                for item in day_items
                if item["duration_minutes"] is not None
            )
            lines.append(f"### {work_date} · {len(day_items)} 项 · {format_duration(day_minutes)}")
            for item in day_items:
                details = [time_range_text(item), item["status"]]
                if item["duration_minutes"] is not None:
                    details.append(format_duration(item["duration_minutes"]))
                lines.append(f"- {' · '.join(details)}：{item['content']}")
                if item["result"]:
                    lines.append(f"  - 成果：{item['result']}")
            lines.append("")

    if pending_captures:
        lines.extend(["## 自动采集候选（待确认）", ""])
        for candidate in pending_candidates(conn, start, end):
            details = [
                f"#{candidate['id']}",
                candidate["work_date"],
                format_duration(candidate["duration_minutes"]),
            ]
            lines.append(f"- {' · '.join(details)}：{candidate['title']}")
            if candidate["summary"]:
                lines.append(f"  - 摘要：{candidate['summary']}")
        lines.append("")

    lines.extend(["## 最耗时的事情", ""])
    if tracked:
        for index, item in enumerate(
            sorted(tracked, key=lambda value: (-int(value["duration_minutes"]), value["work_date"])), 1
        ):
            lines.append(
                f"{index}. **{format_duration(item['duration_minutes'])}** · {item['content']}（{item['work_date']}）"
            )
    else:
        lines.append("还没有耗时数据；补充 `--minutes` 或 `--start/--end` 后，这一节会自动出现。")
    lines.append("")

    lines.extend(["## 项目与投入", ""])
    summaries = project_summary(items)
    if summaries:
        lines.append("| 项目 | 事项数 | 已记录投入 |")
        lines.append("| --- | ---: | ---: |")
        for project, count, minutes in summaries:
            lines.append(f"| {project} | {count} | {format_duration(minutes)} |")
    else:
        lines.append("暂无项目数据。")
    lines.append("")

    blockers = [item for item in items if item["status"] == "blocked"]
    lines.extend(["## 风险与阻塞", ""])
    if blockers:
        for item in blockers:
            lines.append(f"- {item['work_date']}：{item['content']}")
            if item["result"]:
                lines.append(f"  - 当前信息：{item['result']}")
    else:
        lines.append("本周没有标记为阻塞的事项。")
    lines.append("")

    reflection_lines = []
    if reflection["mood"] is not None:
        reflection_lines.append(f"- 幸福指数：{reflection['mood']:g} / 5")
    if reflection["hard_moment"]:
        reflection_lines.append(f"- 最难的一刻：{reflection['hard_moment']}")
    if reflection["happy_moment"]:
        reflection_lines.append(f"- 值得开心的事：{reflection['happy_moment']}")
    if reflection["self_note"]:
        reflection_lines.append(f"- 给自己的话：{reflection['self_note']}")
    lines.extend(["## 本周复盘", ""])
    lines.extend(reflection_lines or ["还没有补充复盘。周末可以用 `reflect` 写下幸福指数、艰辛时刻和开心瞬间。"])
    lines.append("")

    untracked = len(items) - len(tracked)
    if untracked:
        lines.extend([
            "> 说明：本周有 "
            f"{untracked} 项未记录耗时，因此投入时长、最晚结束时间只代表已填写的数据。",
            "",
        ])
    write_text_output("\n".join(lines).rstrip() + "\n", args.output)


def annual_report(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    year = args.year
    start = f"{year:04d}-01-01"
    end = f"{year:04d}-12-31"
    query_args = argparse.Namespace(
        week=None,
        from_date=start,
        to_date=end,
        project=None,
        category=None,
        status=None,
        search=None,
        tag=[],
    )
    items, _, _ = query_entries(conn, query_args)
    pending_captures = pending_candidate_count(conn, start, end)
    tracked = [item for item in items if item["duration_minutes"] is not None]
    total_minutes = sum(int(item["duration_minutes"]) for item in tracked)
    counts = Counter(item["status"] for item in items)
    mood_rows = conn.execute(
        "SELECT * FROM weekly_reflections WHERE week_start <= ? ORDER BY week_start",
        (end,),
    ).fetchall()
    reflections = []
    year_start = date.fromisoformat(start)
    year_end = date.fromisoformat(end)
    for row in mood_rows:
        item = dict(row)
        reflection_start = date.fromisoformat(item["week_start"])
        reflection_end = reflection_start + timedelta(days=6)
        if reflection_end >= year_start and reflection_start <= year_end:
            reflections.append(item)
    work_weeks = {
        week_range(item["work_date"])[0]
        for item in items
    }

    lines = [f"# 年度工作总结（{year}）", ""]
    lines.extend(
        [
            "## 这一年，留下了什么",
            "",
            f"- 工作事项：{len(items)} 项（完成 {counts['done']}，进行中 {counts['in-progress']}，阻塞 {counts['blocked']}，计划 {counts['planned']}）",
            f"- 有耗时记录：{len(tracked)} 项，合计 {format_duration(total_minutes)}",
            f"- 工作日：{len({item['work_date'] for item in items})} 天",
            f"- 有工作记录的周数：{len(work_weeks)} 周",
            f"- 有复盘的周数：{len(reflections)} 周",
        ]
    )
    late_night_count = sum(
        1
        for item in items
        if is_late_finish(item_datetime(item, "end_time"))
    )
    lines.append(f"- 晚间/凌晨结束（22:00–05:00）：{late_night_count} 次")
    lines.append(f"- 自动采集待确认：{pending_captures} 条（不计入正式统计）")
    moods = [float(item["mood"]) for item in reflections if item["mood"] is not None]
    if moods:
        lines.append(f"- 平均幸福指数：{sum(moods) / len(moods):.1f} / 5（{len(moods)} 周有评分）")
    lines.append("")

    lines.extend(["## 每月节奏", "", "| 月份 | 事项数 | 完成 | 已记录投入 | 工作日 |", "| --- | ---: | ---: | ---: | ---: |"])
    for month in range(1, 13):
        month_items = [item for item in items if int(item["work_date"][5:7]) == month]
        month_minutes = sum(
            int(item["duration_minutes"])
            for item in month_items
            if item["duration_minutes"] is not None
        )
        lines.append(
            f"| {month:02d} 月 | {len(month_items)} | {sum(item['status'] == 'done' for item in month_items)} | {format_duration(month_minutes)} | {len({item['work_date'] for item in month_items})} |"
        )
    lines.append("")

    lines.extend(["## 最晚工作的日子", ""])
    late_items = sorted(
        (item for item in items if item_datetime(item, "end_time")),
        key=lambda item: item_datetime(item, "end_time"),
        reverse=True,
    )
    if late_items:
        for item in late_items[:10]:
            lines.append(f"- {time_range_text(item)}：{item['content']}")
    else:
        lines.append("还没有记录结束时间。")
    lines.append("")

    lines.extend(["## 年度最耗时工作", ""])
    if tracked:
        for index, item in enumerate(
            sorted(tracked, key=lambda value: -int(value["duration_minutes"]))[:10], 1
        ):
            lines.append(f"{index}. **{format_duration(item['duration_minutes'])}** · {item['content']}（{item['work_date']}）")
    else:
        lines.append("还没有耗时数据。")
    lines.append("")

    lines.extend(["## 项目分布", ""])
    summaries = project_summary(items)
    if summaries:
        lines.append("| 项目 | 事项数 | 已记录投入 |")
        lines.append("| --- | ---: | ---: |")
        for project, count, minutes in summaries:
            lines.append(f"| {project} | {count} | {format_duration(minutes)} |")
    else:
        lines.append("暂无项目数据。")
    lines.append("")

    lines.extend(["## 一年中的艰辛与开心", ""])
    memorable = [
        reflection
        for reflection in reflections
        if reflection["hard_moment"] or reflection["happy_moment"] or reflection["self_note"]
    ]
    if memorable:
        for reflection in memorable:
            lines.append(f"### {reflection['week_start']} 这一周")
            if reflection["hard_moment"]:
                lines.append(f"- 最难的一刻：{reflection['hard_moment']}")
            if reflection["happy_moment"]:
                lines.append(f"- 值得开心的事：{reflection['happy_moment']}")
            if reflection["self_note"]:
                lines.append(f"- 给自己的话：{reflection['self_note']}")
            lines.append("")
    else:
        lines.append("还没有保存年度复盘碎片。")
        lines.append("")

    if len(items) - len(tracked):
        lines.extend([
            f"> 说明：有 {len(items) - len(tracked)} 项没有耗时数据；年度投入统计只代表已填写的部分。",
            "",
        ])
    write_text_output("\n".join(lines).rstrip() + "\n", args.output)


def display_width(character: str) -> int:
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def wrap_display(text: str, max_width: int, max_lines: int) -> list[str]:
    clean = " ".join(text.split())
    if not clean:
        return [""]
    lines: list[str] = []
    current = ""
    current_width = 0
    for character in clean:
        width = display_width(character)
        if current and current_width + width > max_width:
            lines.append(current.rstrip())
            current = character.lstrip()
            current_width = display_width(current) if current else 0
        else:
            current += character
            current_width += width
    if current:
        lines.append(current.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and sum(display_width(char) for char in last) > max_width - 2:
            last = last[:-1]
        lines[-1] = last.rstrip("，。；、,. ") + "…"
    return lines


def svg_text_block(
    text: str,
    x: int,
    y: int,
    *,
    size: int,
    fill: str,
    max_width: int,
    max_lines: int,
    line_height: int,
    weight: int = 400,
    opacity: float = 1,
    letter_spacing: int = 0,
) -> str:
    lines = wrap_display(text, max_width, max_lines)
    text_elements = []
    for index, line in enumerate(lines):
        text_elements.append(
            f'<text x="{x}" y="{y + index * line_height}" font-size="{size}" '
            f'fill="{fill}" font-weight="{weight}" opacity="{opacity}" '
            f'letter-spacing="{letter_spacing}" '
            f'font-family="-apple-system, BlinkMacSystemFont, PingFang SC, Microsoft YaHei, sans-serif">'
            f'{escape(line)}</text>'
        )
    return "".join(text_elements)


def poster_story(
    items: list[dict[str, Any]], reflection: dict[str, Any]
) -> dict[str, Any]:
    counts = Counter(item["status"] for item in items)
    active_days = len({item["work_date"] for item in items})
    done_count = counts["done"]
    completion = round(done_count / len(items) * 100) if items else 0

    blocked_item = next((item for item in items if item["status"] == "blocked"), None)
    successful_item = next(
        (item for item in reversed(items) if item["status"] == "done" and item["result"]),
        None,
    ) or next((item for item in reversed(items) if item["status"] == "done"), None)

    if reflection["hard_moment"]:
        hard_moment = reflection["hard_moment"]
    elif blocked_item:
        hard_moment = blocked_item["content"]
        if blocked_item["result"]:
            hard_moment += f"：{blocked_item['result']}"
    else:
        hard_moment = "还没有写下这一刻"

    if reflection["happy_moment"]:
        happy_moment = reflection["happy_moment"]
    elif successful_item:
        happy_moment = successful_item["result"] or successful_item["content"]
    else:
        happy_moment = "还没有写下这一刻"

    if reflection["headline"]:
        headline = reflection["headline"]
    elif counts["blocked"]:
        headline = f"完成 {done_count} 件事，也扛过 {counts['blocked']} 个难关。"
    elif done_count:
        headline = "把普通的一周，认真地过成了进度。"
    elif items:
        headline = "故事还在继续，努力已经留下痕迹。"
    else:
        headline = "这一周，等待你写下第一条记录。"

    keyword_counter: Counter[str] = Counter()
    for item in items:
        keyword_counter.update(item["tags"])
        if item["project"]:
            keyword_counter[item["project"]] += 1
    keywords = [word for word, _ in keyword_counter.most_common(4)]

    return {
        "headline": headline,
        "hard_moment": hard_moment,
        "happy_moment": happy_moment,
        "self_note": reflection["self_note"] or "辛苦被看见，进步也值得被记住。",
        "mood": reflection["mood"],
        "count": len(items),
        "active_days": active_days,
        "completion": completion,
        "keywords": keywords,
    }


def build_poster_svg(
    items: list[dict[str, Any]],
    reflection: dict[str, Any],
    start: str,
    end: str,
) -> str:
    story = poster_story(items, reflection)
    week_number = date.fromisoformat(start).isocalendar().week
    mood_text = f"{story['mood']:g}" if story["mood"] is not None else "—"
    keywords = "  ".join(f"#{word}" for word in story["keywords"]) or "#努力存档"

    headline = svg_text_block(
        story["headline"], 76, 185, size=70, fill="#FFFFFF", max_width=25,
        max_lines=3, line_height=86, weight=760,
    )
    hard = svg_text_block(
        story["hard_moment"], 106, 795, size=31, fill="#FFFFFF", max_width=25,
        max_lines=5, line_height=47, weight=540,
    )
    happy = svg_text_block(
        story["happy_moment"], 582, 795, size=31, fill="#FFFFFF", max_width=25,
        max_lines=5, line_height=47, weight=540,
    )
    self_note = svg_text_block(
        story["self_note"], 76, 1264, size=32, fill="#FFFFFF", max_width=45,
        max_lines=2, line_height=46, weight=650,
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1440" viewBox="0 0 1080 1440">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#171329"/>
      <stop offset="0.48" stop-color="#25162F"/>
      <stop offset="1" stop-color="#0B1827"/>
    </linearGradient>
    <radialGradient id="coral"><stop offset="0" stop-color="#FF7567" stop-opacity=".7"/><stop offset="1" stop-color="#FF7567" stop-opacity="0"/></radialGradient>
    <radialGradient id="violet"><stop offset="0" stop-color="#7256FF" stop-opacity=".65"/><stop offset="1" stop-color="#7256FF" stop-opacity="0"/></radialGradient>
    <filter id="blur"><feGaussianBlur stdDeviation="30"/></filter>
  </defs>
  <rect width="1080" height="1440" fill="url(#bg)"/>
  <circle cx="955" cy="125" r="330" fill="url(#coral)" filter="url(#blur)"/>
  <circle cx="70" cy="1090" r="410" fill="url(#violet)" filter="url(#blur)"/>
  <circle cx="1020" cy="560" r="230" fill="none" stroke="#FFFFFF" stroke-opacity=".08" stroke-width="2"/>
  <circle cx="1020" cy="560" r="315" fill="none" stroke="#FFFFFF" stroke-opacity=".04" stroke-width="60"/>

  <text x="76" y="82" font-size="17" fill="#FFFFFF" opacity=".65" font-weight="700" letter-spacing="5" font-family="-apple-system, PingFang SC, sans-serif">WEEKLYLOG · WEEK {week_number:02d}</text>
  {headline}
  <text x="76" y="365" font-size="24" fill="#FFB4AA" font-weight="600" font-family="-apple-system, PingFang SC, sans-serif">{escape(start.replace('-', '.'))} — {escape(end[5:].replace('-', '.'))}</text>

  <g>
    <rect x="76" y="418" width="288" height="142" rx="25" fill="#FFFFFF" fill-opacity=".075" stroke="#FFFFFF" stroke-opacity=".13"/>
    <text x="106" y="482" font-size="49" fill="#FFFFFF" font-weight="760" font-family="-apple-system, PingFang SC, sans-serif">{story['count']}</text>
    <text x="106" y="525" font-size="18" fill="#FFFFFF" opacity=".58" font-family="-apple-system, PingFang SC, sans-serif">记录事项</text>
    <rect x="396" y="418" width="288" height="142" rx="25" fill="#FFFFFF" fill-opacity=".075" stroke="#FFFFFF" stroke-opacity=".13"/>
    <text x="426" y="482" font-size="49" fill="#FFFFFF" font-weight="760" font-family="-apple-system, PingFang SC, sans-serif">{story['active_days']}</text>
    <text x="426" y="525" font-size="18" fill="#FFFFFF" opacity=".58" font-family="-apple-system, PingFang SC, sans-serif">专注工作日</text>
    <rect x="716" y="418" width="288" height="142" rx="25" fill="#FFFFFF" fill-opacity=".075" stroke="#FFFFFF" stroke-opacity=".13"/>
    <text x="746" y="482" font-size="49" fill="#FFFFFF" font-weight="760" font-family="-apple-system, PingFang SC, sans-serif">{story['completion']}%</text>
    <text x="746" y="525" font-size="18" fill="#FFFFFF" opacity=".58" font-family="-apple-system, PingFang SC, sans-serif">完成率</text>
  </g>

  <rect x="76" y="606" width="448" height="440" rx="30" fill="#080B17" fill-opacity=".58" stroke="#FFFFFF" stroke-opacity=".11"/>
  <text x="106" y="676" font-size="34" fill="#FFC85F" font-family="-apple-system, PingFang SC, sans-serif">⚡</text>
  <text x="106" y="730" font-size="21" fill="#FFB4AA" font-weight="700" letter-spacing="2" font-family="-apple-system, PingFang SC, sans-serif">最难的一刻</text>
  {hard}

  <rect x="556" y="606" width="448" height="440" rx="30" fill="#080B17" fill-opacity=".58" stroke="#FFFFFF" stroke-opacity=".11"/>
  <text x="586" y="676" font-size="34" fill="#FFFFFF" font-family="-apple-system, PingFang SC, sans-serif">✦</text>
  <text x="586" y="730" font-size="21" fill="#FFB4AA" font-weight="700" letter-spacing="2" font-family="-apple-system, PingFang SC, sans-serif">值得开心的事</text>
  {happy}

  <text x="76" y="1125" font-size="18" fill="#FFFFFF" opacity=".52" font-family="-apple-system, PingFang SC, sans-serif">{escape(keywords)}</text>
  <line x1="76" y1="1174" x2="1004" y2="1174" stroke="#FFFFFF" stroke-opacity=".12"/>
  {self_note}
  <text x="932" y="1250" text-anchor="end" font-size="15" fill="#FFFFFF" opacity=".58" font-weight="700" letter-spacing="4" font-family="-apple-system, PingFang SC, sans-serif">MOOD</text>
  <text x="1004" y="1328" text-anchor="end" font-size="72" fill="#FF806F" font-weight="820" font-family="-apple-system, PingFang SC, sans-serif">{mood_text}</text>
  <text x="1004" y="1362" text-anchor="end" font-size="17" fill="#FFFFFF" opacity=".5" font-family="-apple-system, PingFang SC, sans-serif">幸福指数 / 5</text>
</svg>'''


def render_png(svg_path: Path, png_path: Path) -> None:
    sips = shutil.which("sips")
    if sips:
        png_path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sips, "-s", "format", "png", str(svg_path), "--out", str(png_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0 and png_path.exists():
            return

    qlmanage = shutil.which("qlmanage")
    if not qlmanage:
        raise SystemExit(f"已生成 SVG，但当前系统无法自动导出 PNG：{svg_path}")
    with tempfile.TemporaryDirectory(prefix="weeklylog-poster-") as temp_dir:
        result = subprocess.run(
            [qlmanage, "-t", "-s", "1440", "-o", temp_dir, str(svg_path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        candidates = list(Path(temp_dir).glob("*.png"))
        if result.returncode != 0 or not candidates:
            raise SystemExit(f"已生成 SVG，但 PNG 导出失败：{svg_path}")
        png_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidates[0], png_path)


def poster(
    args: argparse.Namespace, conn: sqlite3.Connection, db_path: Path
) -> None:
    items, start, end = entries_for_week(conn, args.week)
    reflection = reflection_for_week(conn, args.week)
    svg = build_poster_svg(items, reflection, start, end)

    if args.output:
        requested_path = Path(args.output).expanduser().resolve()
    else:
        requested_path = db_path.parent / "posters" / f"weeklylog-{start}.svg"
    if requested_path.suffix.lower() not in {".svg", ".png"}:
        requested_path = requested_path.with_suffix(".svg")

    png_path: Path | None = None
    if requested_path.suffix.lower() == ".png":
        png_path = requested_path
        svg_path = requested_path.with_suffix(".svg")
    else:
        svg_path = requested_path

    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(svg, encoding="utf-8")
    if png_path:
        render_png(svg_path, png_path)

    result = {
        "week_start": start,
        "week_end": end,
        "entry_count": len(items),
        "svg": str(svg_path),
        "png": str(png_path) if png_path else None,
        "reflection": reflection,
    }
    if args.json:
        print_json(result)
    else:
        print(png_path or svg_path)


def _deck_card_svg(
    *,
    eyebrow: str,
    title: str,
    metric: str,
    metric_label: str,
    body: str,
    footer: str,
    accent: str,
    index: int,
    total: int,
    bars: list[tuple[str, int]] | None = None,
) -> str:
    """Build one quiet, image-first card with exact data overlaid as SVG text."""
    title_svg = svg_text_block(
        title, 78, 220, size=62, fill="#FFFFFF", max_width=25,
        max_lines=3, line_height=78, weight=760,
    )
    body_svg = svg_text_block(
        body, 86, 730, size=31, fill="#F8F5FF", max_width=28,
        max_lines=7, line_height=48, weight=500, opacity=.92,
    )
    footer_svg = svg_text_block(
        footer, 86, 1225, size=21, fill="#FFFFFF", max_width=42,
        max_lines=2, line_height=32, weight=560, opacity=.68,
    )
    bars_svg = ""
    if bars:
        peak = max(value for _, value in bars) or 1
        x = 90
        for label, value in bars[:12]:
            height = int(190 * value / peak) if value else 4
            bars_svg += (
                f'<rect x="{x}" y="1060" width="44" height="{height}" rx="12" '
                f'fill="{accent}" fill-opacity=".82"/>'
                f'<text x="{x + 22}" y="1098" text-anchor="middle" font-size="15" '
                f'fill="#FFFFFF" opacity=".65" font-family="-apple-system, PingFang SC, sans-serif">'
                f'{escape(label)}</text>'
            )
            x += 72

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1080" height="1440" viewBox="0 0 1080 1440">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#111226"/>
      <stop offset=".55" stop-color="#24183C"/>
      <stop offset="1" stop-color="#0A2530"/>
    </linearGradient>
    <radialGradient id="glow"><stop offset="0" stop-color="{accent}" stop-opacity=".72"/><stop offset="1" stop-color="{accent}" stop-opacity="0"/></radialGradient>
    <filter id="blur"><feGaussianBlur stdDeviation="36"/></filter>
  </defs>
  <rect width="1080" height="1440" fill="url(#bg)"/>
  <circle cx="965" cy="150" r="360" fill="url(#glow)" filter="url(#blur)"/>
  <circle cx="70" cy="1175" r="390" fill="url(#glow)" opacity=".65" filter="url(#blur)"/>
  <circle cx="945" cy="650" r="310" fill="none" stroke="#FFFFFF" stroke-opacity=".07" stroke-width="2"/>
  <circle cx="945" cy="650" r="230" fill="none" stroke="#FFFFFF" stroke-opacity=".05" stroke-width="56"/>
  <path d="M0 380 C180 300 270 470 450 395 S760 280 1080 375" fill="none" stroke="#FFFFFF" stroke-opacity=".06" stroke-width="2"/>
  <text x="78" y="84" font-size="18" fill="#FFFFFF" opacity=".64" font-weight="700" letter-spacing="5" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif">{escape(eyebrow)}</text>
  {title_svg}
  <text x="84" y="510" font-size="116" fill="#FFFFFF" font-weight="820" letter-spacing="-3" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif">{escape(metric)}</text>
  <text x="90" y="562" font-size="20" fill="{accent}" font-weight="700" letter-spacing="3" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif">{escape(metric_label)}</text>
  <rect x="78" y="620" width="924" height="470" rx="38" fill="#050713" fill-opacity=".47" stroke="#FFFFFF" stroke-opacity=".10"/>
  <circle cx="920" cy="700" r="38" fill="{accent}" fill-opacity=".18"/>
  <circle cx="920" cy="700" r="10" fill="{accent}"/>
  {body_svg}
  {bars_svg}
  <line x1="78" y1="1170" x2="1002" y2="1170" stroke="#FFFFFF" stroke-opacity=".12"/>
  {footer_svg}
  <text x="1002" y="1362" text-anchor="end" font-size="16" fill="#FFFFFF" opacity=".45" font-weight="700" letter-spacing="4" font-family="-apple-system, BlinkMacSystemFont, PingFang SC, sans-serif">{index:02d} / {total:02d}</text>
</svg>'''


def _first_nonempty(values: Iterable[str], fallback: str) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return fallback


def _week_deck_specs(
    items: list[dict[str, Any]], reflection: dict[str, Any], start: str, end: str
) -> list[dict[str, Any]]:
    tracked = [item for item in items if item["duration_minutes"] is not None]
    total_minutes = sum(int(item["duration_minutes"]) for item in tracked)
    latest = max(
        (item for item in items if item_datetime(item, "end_time")),
        key=lambda item: item_datetime(item, "end_time"),
        default=None,
    )
    longest = max(tracked, key=lambda item: int(item["duration_minutes"]), default=None)
    late = [item for item in items if is_late_finish(item_datetime(item, "end_time"))]
    hard = _first_nonempty(
        [reflection.get("hard_moment", "")]
        + [item["content"] for item in items if item["status"] == "blocked"],
        "这一周还没有写下最难的一刻。",
    )
    happy = _first_nonempty(
        [reflection.get("happy_moment", "")]
        + [item.get("result", "") for item in reversed(items) if item["status"] == "done"],
        "这一周还没有写下值得开心的事。",
    )
    latest_text = "还没有记录结束时间。"
    if latest:
        latest_text = f"{latest['work_date']} {format_clock(latest.get('end_time'))} · {latest['content']}"
    longest_text = "还没有填写耗时。"
    if longest:
        longest_text = f"{longest['content']}（{longest['work_date']}）"
    mood = f"{reflection['mood']:g} / 5" if reflection.get("mood") is not None else "— / 5"
    return [
        {
            "eyebrow": "WEEKGLOW · WORK EVIDENCE",
            "title": "这一周，你没有白忙。",
            "metric": str(len(items)),
            "metric_label": f"条工作痕迹 · {start[5:].replace('-', '.')} — {end[5:].replace('-', '.')}",
            "body": "每一条记录，都是你把混乱变成进度的证据。",
            "footer": f"完成 {sum(item['status'] == 'done' for item in items)} 项 · 专注 {len({item['work_date'] for item in items})} 天 · 已记录投入 {format_duration(total_minutes) if tracked else '—'}",
            "accent": "#FF806F",
        },
        {
            "eyebrow": "WEEKGLOW · THE LATEST HOUR",
            "title": "最晚干活到几点？",
            "metric": format_clock(latest.get("end_time"))[6:] if latest else "—",
            "metric_label": "最后一次记录的结束时间",
            "body": latest_text,
            "footer": f"本周有 {len(late)} 次记录落在 22:00–05:00；时间是提醒，不是勋章。",
            "accent": "#FFC85F",
        },
        {
            "eyebrow": "WEEKGLOW · THE DEEPEST INVESTMENT",
            "title": "什么问题最耗时？",
            "metric": format_duration(longest["duration_minutes"]) if longest else "—",
            "metric_label": "单条记录最长投入",
            "body": longest_text,
            "footer": f"本周合计已记录投入 {format_duration(total_minutes) if tracked else '—'}；没有耗时也没关系，下周可以从一条开始。",
            "accent": "#9E8CFF",
        },
        {
            "eyebrow": "WEEKGLOW · THE HARD PART",
            "title": "你扛住的时刻。",
            "metric": str(sum(item["status"] == "blocked" for item in items)),
            "metric_label": "条被标记为阻塞的记录",
            "body": hard,
            "footer": "难题没有抹掉你的努力；它只是说明你曾经认真走到这里。",
            "accent": "#FF9EBC",
        },
        {
            "eyebrow": "WEEKGLOW · A SMALL WIN",
            "title": "值得开心的事。",
            "metric": str(sum(item["status"] == "done" for item in items)),
            "metric_label": "条完成记录",
            "body": happy,
            "footer": "把小小的完成收好，幸福感通常就是这样长出来的。",
            "accent": "#67D7C4",
        },
        {
            "eyebrow": "WEEKGLOW · NOTE TO SELF",
            "title": "给正在努力的你。",
            "metric": mood,
            "metric_label": "本周幸福指数",
            "body": reflection.get("self_note") or "辛苦被看见，进步也值得被记住。",
            "footer": "下周继续记录事实，也记得给自己留一点掌声。",
            "accent": "#79B8FF",
        },
    ]


def _year_deck_specs(
    items: list[dict[str, Any]], reflections: list[dict[str, Any]], year: int
) -> list[dict[str, Any]]:
    tracked = [item for item in items if item["duration_minutes"] is not None]
    total_minutes = sum(int(item["duration_minutes"]) for item in tracked)
    late = [item for item in items if is_late_finish(item_datetime(item, "end_time"))]
    longest = max(tracked, key=lambda item: int(item["duration_minutes"]), default=None)
    month_counts = [
        (f"{month:02d}", sum(int(item["work_date"][5:7]) == month for item in items))
        for month in range(1, 13)
    ]
    projects = project_summary(items)[:3]
    project_text = "；".join(
        f"{project} {count} 项 / {format_duration(minutes)}"
        for project, count, minutes in projects
    ) or "还没有项目分布。"
    hard = [item["hard_moment"] for item in reflections if item.get("hard_moment")]
    happy = [item["happy_moment"] for item in reflections if item.get("happy_moment")]
    notes = [item["self_note"] for item in reflections if item.get("self_note")]
    moods = [float(item["mood"]) for item in reflections if item.get("mood") is not None]
    average_mood = f"{sum(moods) / len(moods):.1f} / 5" if moods else "— / 5"
    longest_text = f"{longest['content']}（{longest['work_date']}）" if longest else "还没有填写耗时。"
    hard_text = "\n".join(f"· {value}" for value in hard[:3]) or "还没有保存艰辛时刻。"
    happy_text = "\n".join(f"· {value}" for value in happy[:3]) or "还没有保存开心瞬间。"
    note_text = "\n".join(f"· {value}" for value in notes[-3:]) or "给未来的自己留一句话。"
    return [
        {
            "eyebrow": f"YEAR GLOW · {year:04d} MEMORY",
            "title": f"这一年，你真的走了很远。",
            "metric": str(len(items)),
            "metric_label": "条工作记录",
            "body": "不是一张成绩单，而是一整年努力留下的光。",
            "footer": f"{len({item['work_date'] for item in items})} 个工作日 · {len({week_range(item['work_date'])[0] for item in items})} 周留下痕迹 · 已记录投入 {format_duration(total_minutes) if tracked else '—'}",
            "accent": "#FF806F",
        },
        {
            "eyebrow": "YEAR GLOW · YOUR RHYTHM",
            "title": "你的年度节奏。",
            "metric": str(max(month_counts, key=lambda value: value[1])[1] if items else 0),
            "metric_label": "单月最多记录",
            "body": "忙与缓都有意义，持续出现本身就是一种能力。",
            "footer": "每根柱子都代表一个月里，你曾经认真投入过。",
            "accent": "#9E8CFF",
            "bars": month_counts,
        },
        {
            "eyebrow": "YEAR GLOW · THE BIGGEST CHAPTER",
            "title": "什么问题最耗时？",
            "metric": format_duration(longest["duration_minutes"]) if longest else "—",
            "metric_label": "单条记录最长投入",
            "body": longest_text,
            "footer": f"全年累计已记录投入 {format_duration(total_minutes) if tracked else '—'}。",
            "accent": "#FFC85F",
        },
        {
            "eyebrow": "YEAR GLOW · LATE NIGHTS",
            "title": "你把多少夜晚留给了工作？",
            "metric": str(len(late)),
            "metric_label": "22:00–05:00 结束的记录",
            "body": "这些时间值得被看见，也值得提醒你：努力之外，休息同样是生产力。",
            "footer": "只统计你明确写下结束时间的记录。",
            "accent": "#79B8FF",
        },
        {
            "eyebrow": "YEAR GLOW · PROJECT CONSTELLATION",
            "title": "哪些项目陪你走得最久？",
            "metric": str(len(projects)),
            "metric_label": "最常出现的项目（最多 3 个）",
            "body": project_text,
            "footer": "项目名来自你的原始记录，没有做主观归因。",
            "accent": "#67D7C4",
        },
        {
            "eyebrow": "YEAR GLOW · THE HARD PART",
            "title": "一年中的艰辛时刻。",
            "metric": str(len(hard)),
            "metric_label": "条复盘里的难题",
            "body": hard_text,
            "footer": "你不需要把难过包装成鸡汤；记住自己走过，就已经很勇敢。",
            "accent": "#FF9EBC",
        },
        {
            "eyebrow": "YEAR GLOW · THE LIGHT",
            "title": "一年中的开心瞬间。",
            "metric": str(len(happy)),
            "metric_label": "条复盘里的小确幸",
            "body": happy_text,
            "footer": "这些不是琐事，是你愿意继续向前的理由。",
            "accent": "#67D7C4",
        },
        {
            "eyebrow": "YEAR GLOW · KEEP THIS",
            "title": "给未来的自己。",
            "metric": average_mood,
            "metric_label": "年度平均幸福指数",
            "body": note_text,
            "footer": f"{len(reflections)} 周留下复盘；明年也请继续把自己的努力还给自己。",
            "accent": "#FF806F",
        },
    ]


def deck(args: argparse.Namespace, conn: sqlite3.Connection, db_path: Path) -> None:
    if args.kind == "week":
        if args.year:
            raise SystemExit("week deck 不能与 --year 一起使用")
        items, start, end = entries_for_week(conn, args.week)
        reflections = [reflection_for_week(conn, args.week)]
        specs = _week_deck_specs(items, reflections[0], start, end)
        stem = f"weekglow-{start}"
    else:
        if args.week:
            raise SystemExit("year deck 不能与 --week 一起使用")
        year = args.year or date.today().year
        start, end = f"{year:04d}-01-01", f"{year:04d}-12-31"
        items = entries_for_range(conn, start, end)
        reflections = reflections_for_range(conn, start, end)
        specs = _year_deck_specs(items, reflections, year)
        stem = f"yearglow-{year:04d}"

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else db_path.parent / "decks" / stem
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, str]] = []
    total = len(specs)
    for index, spec in enumerate(specs, 1):
        svg_path = output_dir / f"{index:02d}-{stem}.svg"
        svg_path.write_text(
            _deck_card_svg(index=index, total=total, **spec), encoding="utf-8"
        )
        item = {"svg": str(svg_path)}
        if args.format in {"png", "both"}:
            png_path = svg_path.with_suffix(".png")
            render_png(svg_path, png_path)
            item["png"] = str(png_path)
        files.append(item)
    result = {
        "kind": args.kind,
        "range": {"from": start, "to": end},
        "entry_count": len(items),
        "card_count": len(files),
        "cards": files,
        "output_dir": str(output_dir),
    }
    if args.json:
        print_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将工作与每周复盘保存到本地 SQLite，并生成周报或成长海报。"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"SQLite 数据库路径（默认：{DEFAULT_DB}）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="初始化数据库")
    subparsers.add_parser("path", help="显示数据库路径")

    add = subparsers.add_parser("add", help="添加一条工作记录")
    add.add_argument("--date", type=iso_day, help="工作日期，默认今天")
    add.add_argument("--content", required=True, help="工作事项")
    add.add_argument("--project", default="", help="项目")
    add.add_argument("--category", default="", help="分类")
    add.add_argument("--status", choices=STATUSES, default="done", help="状态")
    add.add_argument("--result", default="", help="成果或影响")
    add.add_argument("--tags", default="", help="逗号分隔标签")
    add.add_argument("--start", help="开始时间：HH:MM 或 YYYY-MM-DD HH:MM")
    add.add_argument("--end", help="结束时间：HH:MM 或 YYYY-MM-DD HH:MM")
    add.add_argument("--minutes", type=nonnegative_int, help="投入分钟数")
    add.add_argument("--source", default="manual", help="记录来源，默认 manual")
    add.add_argument("--json", action="store_true", help="输出 JSON")

    listing = subparsers.add_parser("list", help="查询工作记录")
    range_group = listing.add_mutually_exclusive_group()
    range_group.add_argument("--week", type=iso_day, help="该日期所在自然周")
    range_group.add_argument("--from-date", type=iso_day, help="起始日期")
    listing.add_argument("--to-date", type=iso_day, help="结束日期")
    listing.add_argument("--project", help="精确匹配项目")
    listing.add_argument("--category", help="精确匹配分类")
    listing.add_argument("--status", choices=STATUSES, help="状态")
    listing.add_argument("--tag", action="append", default=[], help="必须包含的标签，可重复")
    listing.add_argument("--search", help="搜索事项、成果、项目或分类")
    listing.add_argument("--json", action="store_true", help="输出 JSON")

    dataset = subparsers.add_parser(
        "dataset", help="导出给周度/年度视觉 skill 使用的结构化 JSON"
    )
    dataset_range = dataset.add_mutually_exclusive_group()
    dataset_range.add_argument("--week", type=iso_day, help="导出该日期所在自然周")
    dataset_range.add_argument("--year", type=int, help="导出指定年份")
    dataset_range.add_argument("--from-date", type=iso_day, help="起始日期")
    dataset.add_argument("--to-date", type=iso_day, help="结束日期")
    dataset.add_argument("--output", help="可选的 JSON 输出路径")
    dataset.add_argument("--json", action="store_true", help="兼容机器调用；默认即输出 JSON")

    update = subparsers.add_parser("update", help="更新一条工作记录")
    update.add_argument("id", type=int, help="记录 ID")
    update.add_argument("--date", type=iso_day, help="工作日期")
    update.add_argument("--content", help="工作事项")
    update.add_argument("--project", help="项目；传空字符串可清空")
    update.add_argument("--category", help="分类；传空字符串可清空")
    update.add_argument("--status", choices=STATUSES, help="状态")
    update.add_argument("--result", help="成果或影响；传空字符串可清空")
    update.add_argument("--tags", help="替换全部标签；传空字符串可清空")
    update.add_argument("--start", help="开始时间；传空字符串可清空")
    update.add_argument("--end", help="结束时间；传空字符串可清空")
    update.add_argument("--minutes", type=nonnegative_int, help="投入分钟数")
    update.add_argument("--json", action="store_true", help="输出 JSON")

    delete = subparsers.add_parser("delete", help="删除一条工作记录")
    delete.add_argument("id", type=int, help="记录 ID")
    delete.add_argument("--yes", action="store_true", help="确认删除")

    report_parser = subparsers.add_parser("report", help="生成 Markdown 周报")
    report_parser.add_argument("--week", type=iso_day, help="该日期所在自然周，默认本周")
    report_parser.add_argument("--output", help="可选的 Markdown 输出路径")
    report_parser.add_argument("--detail", action="store_true", help="生成包含投入分析的深度周报")

    reflection = subparsers.add_parser("reflect", help="查看或补充一周的情绪复盘")
    reflection.add_argument("--week", type=iso_day, help="该日期所在自然周，默认本周")
    reflection.add_argument("--mood", type=mood_score, help="本周幸福指数，1 到 5")
    reflection.add_argument("--headline", help="海报主标题")
    reflection.add_argument("--hard-moment", help="本周最难的一刻")
    reflection.add_argument("--happy-moment", help="本周值得开心的事")
    reflection.add_argument("--self-note", help="写给自己的话")

    poster_parser = subparsers.add_parser("poster", help="生成沉浸式每周成长海报")
    poster_parser.add_argument("--week", type=iso_day, help="该日期所在自然周，默认本周")
    poster_parser.add_argument("--output", help="输出 .svg 或 .png 路径")
    poster_parser.add_argument("--json", action="store_true", help="输出 JSON")

    annual_parser = subparsers.add_parser("annual-report", help="生成年度长篇总结")
    annual_parser.add_argument("--year", type=int, default=date.today().year, help="年份，默认今年")
    annual_parser.add_argument("--output", help="可选的 Markdown 输出路径")

    deck_parser = subparsers.add_parser(
        "deck", help="生成多张低文字视觉卡片（weekglow/yearglow 共用）"
    )
    deck_parser.add_argument("--kind", choices=("week", "year"), required=True)
    deck_parser.add_argument("--week", type=iso_day, help="周卡片使用该日期所在自然周")
    deck_parser.add_argument("--year", type=int, help="年度卡片使用指定年份")
    deck_parser.add_argument("--output-dir", help="输出目录，默认写入数据库旁的 decks/")
    deck_parser.add_argument(
        "--format", choices=("svg", "png", "both"), default="svg",
        help="输出格式，默认 svg；png/both 需要系统有 sips 或 qlmanage",
    )
    deck_parser.add_argument("--json", action="store_true", help="输出结果 JSON")

    hook = subparsers.add_parser(
        "hook", help="接收 Codex 或其他 AI 客户端的自动采集事件"
    )
    hook_subparsers = hook.add_subparsers(dest="hook_command", required=True)

    ingest = hook_subparsers.add_parser(
        "ingest", help="从 stdin 或文件读取一个 JSON hook 事件"
    )
    ingest.add_argument("--event-file", help="JSON 事件文件；不传则从 stdin 读取")
    ingest.add_argument(
        "--auto-approve",
        action="store_true",
        help="写入正式工作记录，不经过候选确认（请先确认采集质量）",
    )
    ingest.add_argument("--json", action="store_true", help="输出处理结果 JSON")
    ingest.add_argument(
        "--hook-output",
        action="store_true",
        help="按 Codex hook 约定输出空 JSON，不把文本注入对话",
    )

    hook_listing = hook_subparsers.add_parser("list", help="查看自动采集候选")
    hook_range = hook_listing.add_mutually_exclusive_group()
    hook_range.add_argument("--week", type=iso_day, help="该日期所在自然周")
    hook_range.add_argument("--from-date", type=iso_day, help="起始日期")
    hook_listing.add_argument("--to-date", type=iso_day, help="结束日期")
    hook_listing.add_argument(
        "--state", choices=HOOK_REVIEW_STATES, default="candidate", help="审核状态"
    )
    hook_listing.add_argument("--json", action="store_true", help="输出 JSON")

    promote = hook_subparsers.add_parser("promote", help="确认一条候选并写入正式记录")
    promote.add_argument("id", type=int, help="候选 ID")
    promote.add_argument("--status", choices=STATUSES, help="覆盖工作状态")
    promote.add_argument("--content", help="覆盖工作事项标题")
    promote.add_argument("--result", help="覆盖成果摘要")
    promote.add_argument("--tags", help="替换标签；传空字符串可清空")
    promote.add_argument("--json", action="store_true", help="输出 JSON")

    ignore = hook_subparsers.add_parser("ignore", help="忽略一条自动采集候选")
    ignore.add_argument("id", type=int, help="候选 ID")
    ignore.add_argument("--json", action="store_true", help="输出 JSON")

    config = hook_subparsers.add_parser("config", help="输出客户端 hook 配置片段")
    config.add_argument("--client", choices=("codex",), default="codex")
    config.add_argument(
        "--auto-approve",
        action="store_true",
        help="配置为自动直接入账；默认保留候选待确认",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    db_path = Path(args.db).expanduser().resolve()

    if args.command == "path":
        print(db_path)
        return 0
    if args.command == "hook" and args.hook_command == "config":
        hook_config(args)
        return 0

    conn = connect(db_path)
    try:
        if args.command == "init":
            print(db_path)
        elif args.command == "add":
            add_entry(args, conn)
        elif args.command == "list":
            list_entries(args, conn)
        elif args.command == "dataset":
            dataset_export(args, conn)
        elif args.command == "update":
            update_entry(args, conn)
        elif args.command == "delete":
            delete_entry(args, conn)
        elif args.command == "report":
            report(args, conn)
        elif args.command == "reflect":
            reflect(args, conn)
        elif args.command == "poster":
            poster(args, conn, db_path)
        elif args.command == "annual-report":
            annual_report(args, conn)
        elif args.command == "deck":
            deck(args, conn, db_path)
        elif args.command == "hook":
            if args.hook_command == "ingest":
                hook_ingest(args, conn)
            elif args.hook_command == "list":
                hook_list(args, conn)
            elif args.hook_command == "promote":
                hook_promote(args, conn)
            elif args.hook_command == "ignore":
                hook_ignore(args, conn)
            else:
                parser.error(f"未知 hook 命令：{args.hook_command}")
        else:
            parser.error(f"未知命令：{args.command}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
