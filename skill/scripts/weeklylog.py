#!/usr/bin/env python3
"""Store and summarize weekly work items in a local SQLite database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
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
SCHEMA_VERSION = 3


@dataclass(frozen=True)
class MigrationResult:
    schema_version: int
    migration_backup: Path | None = None


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


def application_tables_exist(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        is not None
    )


def migration_backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    """Create a recoverable, privacy-redacted copy before changing the database."""
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-{stamp}.sqlite3")
    counter = 1
    while backup_path.exists():
        backup_path = db_path.with_name(
            f"{db_path.name}.pre-migration-{stamp}-{counter}.sqlite3"
        )
        counter += 1
    redacted = sqlite3.connect(":memory:")
    destination = sqlite3.connect(backup_path)
    try:
        redacted.execute("PRAGMA secure_delete = ON")
        destination.execute("PRAGMA secure_delete = ON")
        conn.commit()
        # Copy into memory first so raw prompts/replies never get written to
        # the long-lived backup file. The redaction is applied before the
        # memory database is persisted.
        conn.backup(redacted)
        redact_transcript_fields(redacted)
        redacted.commit()
        redacted.execute("VACUUM")
        redacted.backup(destination)
        destination.commit()
    except sqlite3.Error as exc:
        raise SystemExit(f"无法创建迁移备份：{exc}") from exc
    finally:
        redacted.close()
        destination.close()
    return backup_path


def redact_existing_migration_backups(db_path: Path) -> None:
    """Scrub legacy automation transcript columns from prior migration copies."""
    pattern = f"{db_path.name}.pre-migration-*.sqlite3"
    for backup_path in sorted(db_path.parent.glob(pattern)):
        backup = sqlite3.connect(backup_path)
        try:
            backup.execute("PRAGMA secure_delete = ON")
            redact_transcript_fields(backup)
            backup.commit()
            backup.execute("VACUUM")
        except sqlite3.Error as exc:
            raise SystemExit(f"无法清理历史迁移备份：{exc}") from exc
        finally:
            backup.close()


def migrate_to_schema_v1(conn: sqlite3.Connection) -> None:
    schema = """
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
    # sqlite3.executescript() implicitly commits an open transaction. Execute
    # each statement through the active transaction so a failed migration can
    # be rolled back completely.
    for statement in schema.split(";"):
        statement = statement.strip()
        if statement:
            conn.execute(statement)
    # Migrate databases created by earlier versions without rewriting work records.
    ensure_column(conn, "entries", "start_time", "TEXT")
    ensure_column(conn, "entries", "end_time", "TEXT")
    ensure_column(conn, "entries", "duration_minutes", "INTEGER")
    ensure_column(conn, "entries", "source", "TEXT NOT NULL DEFAULT 'manual'")
    ensure_column(conn, "entries", "capture_id", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_source ON entries(source)")


def redact_transcript_fields(conn: sqlite3.Connection) -> None:
    """Clear legacy automation transcript columns while retaining user records."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "automation_sessions" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_sessions)")}
        if "last_prompt" in columns:
            conn.execute("UPDATE automation_sessions SET last_prompt = ''")
    if "automation_turns" in tables:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(automation_turns)")}
        assignments = []
        if "prompt" in columns:
            assignments.append("prompt = ''")
        if "assistant_message" in columns:
            assignments.append("assistant_message = ''")
        if assignments:
            conn.execute(f"UPDATE automation_turns SET {', '.join(assignments)}")
    # Do not rewrite capture_candidates or entries here. Candidates may still
    # need review, and promoted entries are confirmed facts that users may have
    # edited. Their contents cannot be classified safely as transcript text.


def migrate_to_schema_v2(conn: sqlite3.Connection) -> None:
    """Remove transcript payloads retained by pre-v2 automatic capture."""
    redact_transcript_fields(conn)


def migrate_to_schema_v3(conn: sqlite3.Connection) -> None:
    """Add evidence, aggregation, integration-event and retention metadata."""
    redact_transcript_fields(conn)
    for table, column, definition in (
        ("entries", "work_item_id", "TEXT"),
        ("entries", "jira_key", "TEXT"),
        ("entries", "human_duration_minutes", "INTEGER"),
        ("entries", "ai_duration_minutes", "INTEGER"),
        ("entries", "elapsed_start", "TEXT"),
        ("entries", "elapsed_end", "TEXT"),
        ("entries", "confidence", "REAL"),
        ("entries", "aggregate_version", "INTEGER NOT NULL DEFAULT 1"),
        ("capture_candidates", "work_item_id", "TEXT"),
        ("capture_candidates", "task_key", "TEXT"),
        ("capture_candidates", "jira_key", "TEXT"),
        ("capture_candidates", "confidence", "REAL NOT NULL DEFAULT 0.5"),
        ("capture_candidates", "provenance", "TEXT NOT NULL DEFAULT 'heuristic'"),
        ("capture_candidates", "evidence_count", "INTEGER NOT NULL DEFAULT 0"),
        ("capture_candidates", "ai_duration_minutes", "INTEGER"),
        ("capture_candidates", "human_duration_minutes", "INTEGER"),
        ("capture_candidates", "elapsed_start", "TEXT"),
        ("capture_candidates", "elapsed_end", "TEXT"),
        ("capture_candidates", "merged_into", "INTEGER"),
        ("capture_candidates", "parent_candidate_id", "INTEGER"),
    ):
        ensure_column(conn, table, column, definition)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_work_item ON entries(work_item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_jira_key ON entries(jira_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_task_key ON capture_candidates(task_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_candidates_work_item ON capture_candidates(work_item_id)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evidence_id TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            external_id TEXT,
            client TEXT NOT NULL DEFAULT '',
            task_key TEXT,
            work_item_id TEXT,
            candidate_id INTEGER REFERENCES capture_candidates(id) ON DELETE SET NULL,
            work_date TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            project TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'done',
            start_time TEXT,
            end_time TEXT,
            ai_duration_minutes INTEGER,
            human_duration_minutes INTEGER,
            elapsed_start TEXT,
            elapsed_end TEXT,
            jira_key TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            provenance TEXT NOT NULL DEFAULT 'heuristic',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_source_external ON evidence(source, external_id) WHERE external_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_work_item ON evidence(work_item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_work_date ON evidence(work_date)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integration_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            aggregate_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            aggregate_version INTEGER NOT NULL,
            payload_schema_version INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT NOT NULL UNIQUE,
            payload_json TEXT NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'pending'
                CHECK (delivery_state IN ('pending', 'claimed', 'delivered', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            claim_deadline TEXT,
            claim_token TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    # Older v3 databases may already have the table without the lease column.
    ensure_column(conn, "integration_outbox", "claim_deadline", "TEXT")
    ensure_column(conn, "integration_outbox", "claim_token", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_state_available ON integration_outbox(delivery_state, available_at, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_aggregate ON integration_outbox(aggregate_type, aggregate_id, aggregate_version)")


def connect(db_path: Path) -> tuple[sqlite3.Connection, MigrationResult]:
    """Open the database and apply versioned, recoverable schema migrations."""
    existed = db_path.exists() and db_path.stat().st_size > 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current_version > SCHEMA_VERSION:
        conn.close()
        raise SystemExit(
            f"数据库版本 {current_version} 高于当前 CLI 支持的版本 {SCHEMA_VERSION}"
        )

    # A previous run may have left migration copies behind (for example after
    # an interrupted upgrade). Scrub their legacy automation fields whenever
    # this database is opened, not only while a migration is pending.
    redact_existing_migration_backups(db_path)
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("PRAGMA journal_mode = WAL")

    backup_path: Path | None = None
    if current_version < SCHEMA_VERSION:
        if existed and application_tables_exist(conn):
            backup_path = migration_backup(conn, db_path)
        try:
            conn.execute("BEGIN")
            if current_version < 1:
                migrate_to_schema_v1(conn)
            if current_version < 2:
                migrate_to_schema_v2(conn)
            if current_version < 3:
                migrate_to_schema_v3(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            # Schema/data changes are durable once committed. Maintenance
            # operations are best-effort and must not turn a successful
            # migration into a misleading failure that cannot be rolled back.
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
            except sqlite3.Error:
                pass
        except Exception:
            conn.rollback()
            conn.close()
            raise
    else:
        # Enforce transcript invariants even for databases created by an
        # intermediate build that already advertised the current version.
        try:
            conn.execute("BEGIN")
            migrate_to_schema_v3(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
    return conn, MigrationResult(SCHEMA_VERSION, backup_path)


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
            jira_key, human_duration_minutes, ai_duration_minutes,
            elapsed_start, elapsed_end, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            args.jira_key,
            duration_minutes if args.minutes is not None else None,
            None,
            start_time,
            end_time,
            now,
            now,
        ),
    )
    entry_id = int(cursor.lastrowid)
    replace_tags(conn, entry_id, split_tags(args.tags))
    enqueue_integration_event(
        conn,
        aggregate_type="confirmed_record",
        aggregate_id=str(entry_id),
        event_type="record.created",
        aggregate_version=1,
        payload={"entry_id": entry_id, "source": getattr(args, "source", "manual")},
    )
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
    # Keep the compatibility column empty; raw prompts are evidence, not
    # reportable summaries and must not be persisted by automatic capture.
    prompt = None
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
    # The prompt column remains for schema compatibility with older databases,
    # but new events only persist client-provided structured summaries.
    prompt = ""
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


def normalize_goal(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", normalized)
    return " ".join(normalized.split())


def capture_confidence(value: Any, default: float = 0.5) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, round(score, 3)))


def aggregate_candidate(
    conn: sqlite3.Connection, candidate: dict[str, Any]
) -> dict[str, Any] | None:
    """Find a still-pending candidate that is safe to extend with new evidence."""
    task_key = candidate.get("task_key")
    if task_key:
        row = conn.execute(
            "SELECT * FROM capture_candidates WHERE review_state = 'candidate' AND (task_key = ? OR work_item_id = ?) ORDER BY id LIMIT 1",
            (task_key, task_key),
        ).fetchone()
        return dict(row) if row else None
    project = candidate.get("project", "")
    goal = normalize_goal(candidate.get("title"))
    if not project or not goal:
        return None
    jira_key = candidate.get("jira_key")
    if jira_key:
        jira_rows = conn.execute(
            "SELECT * FROM capture_candidates WHERE review_state = 'candidate' AND project = ? AND jira_key = ? ORDER BY id DESC LIMIT 20",
            (project, jira_key),
        ).fetchall()
    else:
        jira_rows = []
    rows = conn.execute(
        "SELECT * FROM capture_candidates WHERE review_state = 'candidate' AND project = ? ORDER BY id DESC LIMIT 50",
        (project,),
    ).fetchall()
    candidate_day = date.fromisoformat(candidate["work_date"])
    for row in [*jira_rows, *rows]:
        if normalize_goal(row["title"]) != goal:
            continue
        # A Jira key is a strong association hint, but never an identity on
        # its own; conflicting explicit keys must keep work items separate.
        if jira_key and row["jira_key"] not in (None, jira_key):
            continue
        if not jira_key and row["jira_key"]:
            continue
        try:
            row_day = date.fromisoformat(row["work_date"])
        except (KeyError, ValueError):
            continue
        if abs((candidate_day - row_day).days) <= 1:
            return dict(row)
    return None


def merge_candidate_observation(
    conn: sqlite3.Connection,
    current: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    summaries = [value for value in (current.get("summary", ""), incoming.get("summary", "")) if value]
    # Keep the durable candidate summary bounded even when a long-running
    # task accumulates many Evidence observations.
    summary = capture_text("；".join(dict.fromkeys(summaries)), 1200)
    durations = [value for value in (current.get("duration_minutes"), incoming.get("duration_minutes")) if value is not None]
    ai_durations = [
        value
        for value in (
            current.get("ai_duration_minutes", current.get("duration_minutes")),
            incoming.get("ai_duration_minutes", incoming.get("duration_minutes")),
        )
        if value is not None
    ]
    human_durations = [
        value
        for value in (current.get("human_duration_minutes"), incoming.get("human_duration_minutes"))
        if value is not None
    ]
    elapsed_starts = [value for value in (current.get("elapsed_start", current.get("start_time")), incoming.get("elapsed_start", incoming.get("start_time"))) if value]
    elapsed_ends = [value for value in (current.get("elapsed_end", current.get("end_time")), incoming.get("elapsed_end", incoming.get("end_time"))) if value]
    start_values = [value for value in (current.get("start_time"), incoming.get("start_time")) if value]
    end_values = [value for value in (current.get("end_time"), incoming.get("end_time")) if value]
    tags = split_tags(",".join(value for value in (current.get("tags", ""), incoming.get("tags", "")) if value))
    conn.execute(
        """
        UPDATE capture_candidates
        SET summary = ?, start_time = ?, end_time = ?, duration_minutes = ?,
            tags = ?, ai_duration_minutes = ?, human_duration_minutes = ?,
            elapsed_start = ?, elapsed_end = ?, evidence_count = evidence_count + 1,
            confidence = MAX(confidence, ?), updated_at = ?
        WHERE id = ?
        """,
        (
            summary,
            min(start_values) if start_values else None,
            max(end_values) if end_values else None,
            sum(durations) if durations else None,
            ",".join(tags),
            sum(ai_durations) if ai_durations else None,
            sum(human_durations) if human_durations else None,
            min(elapsed_starts) if elapsed_starts else None,
            max(elapsed_ends) if elapsed_ends else None,
            capture_confidence(incoming.get("confidence"), 0.5),
            timestamp(),
            current["id"],
        ),
    )
    return candidate_by_id(conn, int(current["id"])) or current


def refresh_candidate_time_fields(conn: sqlite3.Connection, candidate_id: int) -> None:
    """Recompute separated time aggregates from the candidate's Evidence."""
    conn.execute(
        """
        UPDATE capture_candidates
        SET duration_minutes = (SELECT SUM(COALESCE(ai_duration_minutes, human_duration_minutes)) FROM evidence WHERE candidate_id = ?),
            ai_duration_minutes = (SELECT SUM(ai_duration_minutes) FROM evidence WHERE candidate_id = ?),
            human_duration_minutes = (SELECT SUM(human_duration_minutes) FROM evidence WHERE candidate_id = ?),
            start_time = (SELECT MIN(start_time) FROM evidence WHERE candidate_id = ?),
            end_time = (SELECT MAX(end_time) FROM evidence WHERE candidate_id = ?),
            elapsed_start = (SELECT MIN(elapsed_start) FROM evidence WHERE candidate_id = ?),
            elapsed_end = (SELECT MAX(elapsed_end) FROM evidence WHERE candidate_id = ?),
            evidence_count = (SELECT COUNT(*) FROM evidence WHERE candidate_id = ?),
            updated_at = ?
        WHERE id = ?
        """,
        (candidate_id, candidate_id, candidate_id, candidate_id, candidate_id, candidate_id, candidate_id, candidate_id, timestamp(), candidate_id),
    )


def insert_evidence(
    conn: sqlite3.Connection,
    evidence: dict[str, Any],
    candidate_id: int | None,
) -> tuple[dict[str, Any], bool]:
    existing = conn.execute(
        "SELECT * FROM evidence WHERE evidence_id = ?",
        (evidence["evidence_id"],),
    ).fetchone()
    if existing:
        return dict(existing), False
    now = timestamp()
    conn.execute(
        """
        INSERT INTO evidence(
            evidence_id, source, external_id, client, task_key, work_item_id,
            candidate_id, work_date, title, summary, project, category, status,
            start_time, end_time, ai_duration_minutes, human_duration_minutes,
            elapsed_start, elapsed_end, jira_key, confidence, provenance,
            metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            evidence["evidence_id"], evidence.get("source", "hook"), evidence.get("external_id"),
            evidence.get("client", ""), evidence.get("task_key"), evidence.get("work_item_id"),
            candidate_id, evidence["work_date"], evidence.get("title", ""), evidence.get("summary", ""),
            evidence.get("project", ""), evidence.get("category", ""), evidence.get("status", "done"),
            evidence.get("start_time"), evidence.get("end_time"), evidence.get("ai_duration_minutes"),
            evidence.get("human_duration_minutes"), evidence.get("elapsed_start"), evidence.get("elapsed_end"),
            evidence.get("jira_key"), capture_confidence(evidence.get("confidence"), 0.5),
            evidence.get("provenance", "heuristic"), json.dumps(evidence.get("metadata", {}), ensure_ascii=False, sort_keys=True),
            now, now,
        ),
    )
    row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence["evidence_id"],)).fetchone()
    if row is None:
        raise SystemExit("Evidence 写入失败")
    return dict(row), True


def event_snapshot(
    conn: sqlite3.Connection, aggregate_type: str, aggregate_id: str
) -> dict[str, Any]:
    """Return a safe, self-contained snapshot for integration consumers."""
    try:
        numeric_id = int(aggregate_id)
    except (TypeError, ValueError):
        return {}
    if aggregate_type == "candidate":
        candidate = candidate_by_id(conn, numeric_id)
        if not candidate:
            return {}
        evidence_ids = [
            row[0]
            for row in conn.execute(
                "SELECT evidence_id FROM evidence WHERE candidate_id = ? ORDER BY id",
                (numeric_id,),
            ).fetchall()
        ]
        return {
            "candidate_id": numeric_id,
            "work_item_id": candidate.get("work_item_id"),
            "task_key": candidate.get("task_key"),
            "title": capture_text(candidate.get("title"), 240),
            "summary": capture_text(candidate.get("summary"), 1200),
            "project": capture_text(candidate.get("project"), 200),
            "category": capture_text(candidate.get("category"), 120),
            "status": candidate.get("work_status"),
            "work_date": candidate.get("work_date"),
            "start_time": candidate.get("start_time"),
            "end_time": candidate.get("end_time"),
            "ai_duration_minutes": candidate.get("ai_duration_minutes", candidate.get("duration_minutes")),
            "human_duration_minutes": candidate.get("human_duration_minutes"),
            "elapsed_start": candidate.get("elapsed_start", candidate.get("start_time")),
            "elapsed_end": candidate.get("elapsed_end", candidate.get("end_time")),
            "jira_key": candidate.get("jira_key"),
            "confidence": candidate.get("confidence"),
            "provenance": candidate.get("provenance"),
            "evidence_ids": evidence_ids,
        }
    if aggregate_type == "confirmed_record":
        entry = entry_by_id(conn, numeric_id)
        if not entry:
            return {}
        return {
            "entry_id": numeric_id,
            "work_item_id": entry.get("work_item_id"),
            "title": capture_text(entry.get("content"), 240),
            "summary": capture_text(entry.get("result"), 1200),
            "project": capture_text(entry.get("project"), 200),
            "category": capture_text(entry.get("category"), 120),
            "status": entry.get("status"),
            "work_date": entry.get("work_date"),
            "start_time": entry.get("start_time"),
            "end_time": entry.get("end_time"),
            "duration_minutes": entry.get("duration_minutes"),
            "ai_duration_minutes": entry.get("ai_duration_minutes"),
            "human_duration_minutes": entry.get("human_duration_minutes"),
            "elapsed_start": entry.get("elapsed_start", entry.get("start_time")),
            "elapsed_end": entry.get("elapsed_end", entry.get("end_time")),
            "jira_key": entry.get("jira_key"),
            "tags": entry.get("tags", []),
            "source": entry.get("source"),
            "evidence_ids": [
                row[0]
                for row in conn.execute(
                    """
                    SELECT evidence_id FROM evidence
                    WHERE candidate_id = (SELECT id FROM capture_candidates WHERE entry_id = ?)
                    ORDER BY id
                    """,
                    (numeric_id,),
                ).fetchall()
            ],
        }
    return {}


def enqueue_integration_event(
    conn: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    aggregate_version: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Write a deterministic local integration event in the current transaction."""
    idempotency_key = f"{aggregate_type}:{aggregate_id}:{event_type}:{aggregate_version}"
    event_id = "evt-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    event_payload = {**event_snapshot(conn, aggregate_type, aggregate_id), **payload}
    encoded = json.dumps(event_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    now = timestamp()
    conn.execute(
        """
        INSERT OR IGNORE INTO integration_outbox(
            event_id, aggregate_type, aggregate_id, event_type,
            aggregate_version, payload_schema_version, idempotency_key,
            payload_json, delivery_state, attempts, available_at, claim_deadline, claim_token,
            last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'pending', 0, ?, NULL, NULL, '', ?, ?)
        """,
        (
            event_id,
            aggregate_type,
            aggregate_id,
            event_type,
            aggregate_version,
            idempotency_key,
            encoded,
            now,
            now,
            now,
        ),
    )
    row = conn.execute("SELECT * FROM integration_outbox WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        raise SystemExit("Integration Event 写入失败")
    return dict(row)


def insert_capture_candidate(
    conn: sqlite3.Connection, candidate: dict[str, Any], *, aggregate: bool = True
) -> tuple[dict[str, Any], str]:
    existing = conn.execute(
        "SELECT * FROM capture_candidates WHERE capture_id = ?",
        (candidate["capture_id"],),
    ).fetchone()
    if existing:
        return dict(existing), "duplicate"
    if aggregate:
        existing_work_item = aggregate_candidate(conn, candidate)
        if existing_work_item:
            return merge_candidate_observation(conn, existing_work_item, candidate), "associated"
    now = timestamp()
    cursor = conn.execute(
        """
        INSERT INTO capture_candidates(
            capture_id, source, client, session_id, turn_id, work_date,
            title, summary, project, category, work_status, start_time,
            end_time, duration_minutes, cwd, tags, task_key, work_item_id,
            jira_key, confidence, provenance, evidence_count, ai_duration_minutes,
            human_duration_minutes, elapsed_start, elapsed_end, review_state,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
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
            candidate.get("task_key"),
            candidate.get("work_item_id"),
            candidate.get("jira_key"),
            capture_confidence(candidate.get("confidence"), 0.5),
            candidate.get("provenance", "heuristic"),
            1,
            candidate.get("ai_duration_minutes", candidate.get("duration_minutes")),
            candidate.get("human_duration_minutes"),
            candidate.get("elapsed_start", candidate.get("start_time")),
            candidate.get("elapsed_end", candidate.get("end_time")),
            now,
            now,
        ),
    )
    item = candidate_by_id(conn, int(cursor.lastrowid))
    if item is None:
        raise SystemExit("自动采集候选写入失败")
    return item, "created"


def promote_capture(
    conn: sqlite3.Connection,
    candidate: dict[str, Any],
    *,
    status: str | None = None,
    content: str | None = None,
    result: str | None = None,
    tags: str | None = None,
    jira_key: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    if candidate["review_state"] == "promoted" and candidate.get("entry_id"):
        item = entry_by_id(conn, int(candidate["entry_id"]))
        if item:
            return item
    if candidate["review_state"] != "candidate":
        raise SystemExit("只能确认尚未审核的候选；已忽略候选不能直接转正")
    existing = conn.execute(
        "SELECT id FROM entries WHERE capture_id = ?", (candidate["capture_id"],)
    ).fetchone()
    created_entry = False
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
                work_item_id, jira_key, human_duration_minutes, ai_duration_minutes,
                elapsed_start, elapsed_end, confidence,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'hook', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                candidate.get("work_item_id"),
                jira_key if jira_key is not None else candidate.get("jira_key"),
                candidate.get("human_duration_minutes"),
                candidate.get("ai_duration_minutes", candidate.get("duration_minutes")),
                candidate.get("elapsed_start", candidate.get("start_time")),
                candidate.get("elapsed_end", candidate.get("end_time")),
                capture_confidence(candidate.get("confidence"), 0.5),
                now,
                now,
            ),
        )
        entry_id = int(cursor.lastrowid)
        created_entry = True
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
    outbox_event = None
    if created_entry:
        outbox_event = enqueue_integration_event(
            conn,
            aggregate_type="confirmed_record",
            aggregate_id=str(entry_id),
            event_type="record.confirmed",
            aggregate_version=1,
            payload={
                "entry_id": entry_id,
                "candidate_id": candidate["id"],
                "capture_id": candidate["capture_id"],
                "work_item_id": candidate.get("work_item_id"),
            },
        )
    if commit:
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
        raw_prompt = capture_text(
            event_field(payload, "prompt", "user_prompt", "message", "input"), 1000
        )
        if not raw_prompt:
            result = {"action": "ignored", "reason": "empty_prompt"}
        else:
            if not session_id:
                session_id = "session-" + hashlib.sha256(
                    f"{client}|{cwd}".encode("utf-8")
                ).hexdigest()[:20]
            if not turn_id:
                turn_id = "turn-" + hashlib.sha256(
                    f"{session_id}|{raw_prompt}|{(started_dt or now_dt).isoformat(timespec='seconds')}".encode(
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
                prompt=None,
            )
            upsert_automation_turn(
                conn,
                session_id=session_id,
                turn_id=turn_id,
                client=client,
                prompt="",
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
        title = capture_text(event_field(payload, "title", "content"), 240)
        summary = capture_text(
            event_field(
                payload,
                "summary",
                "result",
                # Compatibility fallbacks for older clients. These values are
                # bounded and redacted in memory before they become a candidate.
                "last_assistant_message",
                "assistant_message",
                "output",
            ),
            1200,
        )
        if not title and not summary:
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
            task_key = capture_text(
                event_field(payload, "task_key", "taskKey", "work_item_id", "workItemId"), 200
            )
            jira_key = capture_text(event_field(payload, "jira_key", "jiraKey"), 80)
            structured = bool(event_field(payload, "title", "content")) or bool(
                event_field(payload, "summary", "result")
            )
            provenance = capture_text(
                event_field(payload, "provenance", "summary_provenance"), 80
            ) or ("client-structured" if structured else "legacy-fallback")
            confidence = capture_confidence(
                event_field(payload, "confidence", "capture_confidence"),
                0.9 if structured else 0.4,
            )
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
                        [client, event_name, session_id, turn_id, work_date, title, summary]
                    )
                capture_id = f"{client}-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
            work_item_id = task_key or (
                "wi-" + hashlib.sha256(
                    f"{project}|{normalize_goal(title or summary)}".encode("utf-8")
                ).hexdigest()[:20]
            )
            candidate_data = {
                "capture_id": capture_id,
                "source": "hook",
                "client": client,
                "session_id": session_id or None,
                "turn_id": turn_id or None,
                "work_date": work_date,
                "title": title or clipped(summary, 120) or "自动采集工作回合",
                "summary": summary,
                "project": project,
                "category": category,
                "work_status": work_status,
                "start_time": start.isoformat(timespec="seconds") if start else None,
                "end_time": end.isoformat(timespec="seconds") if end else None,
                "duration_minutes": event_duration_minutes(payload, start, end),
                "cwd": cwd,
                "tags": ",".join(tags),
                "task_key": task_key or None,
                "work_item_id": work_item_id,
                "jira_key": jira_key or None,
                "confidence": confidence,
                "provenance": provenance,
            }
            candidate, candidate_action = insert_capture_candidate(conn, candidate_data)
            evidence_data = {
                "evidence_id": capture_id,
                "external_id": capture_id,
                "source": "hook",
                "client": client,
                "task_key": task_key or None,
                "work_item_id": work_item_id,
                "work_date": work_date,
                "title": title,
                "summary": summary,
                "project": project,
                "category": category,
                "status": work_status,
                "start_time": start.isoformat(timespec="seconds") if start else None,
                "end_time": end.isoformat(timespec="seconds") if end else None,
                "ai_duration_minutes": event_duration_minutes(payload, start, end),
                "elapsed_start": start.isoformat(timespec="seconds") if start else None,
                "elapsed_end": end.isoformat(timespec="seconds") if end else None,
                "jira_key": jira_key or None,
                "confidence": confidence,
                "provenance": provenance,
                "metadata": {"event": event_name, "cwd": cwd},
            }
            evidence, evidence_created = insert_evidence(
                conn, evidence_data, int(candidate["id"])
            )
            if evidence_created:
                refresh_candidate_time_fields(conn, int(candidate["id"]))
                candidate = candidate_by_id(conn, int(candidate["id"])) or candidate
            if session_id and turn_id:
                conn.execute(
                    """
                    UPDATE automation_turns
                    SET ended_at = ?, assistant_message = ?, state = 'captured', updated_at = ?
                    WHERE session_id = ? AND turn_id = ?
                    """,
                    (
                        candidate.get("end_time"),
                        "",
                        timestamp(),
                        session_id,
                        turn_id,
                    ),
                )
            outbox_event = None
            if candidate_action != "duplicate":
                outbox_event = enqueue_integration_event(
                    conn,
                    aggregate_type="candidate",
                    aggregate_id=str(candidate["id"]),
                    event_type=(
                        "candidate.created"
                        if candidate_action == "created"
                        else "candidate.evidence_associated"
                    ),
                    aggregate_version=int(candidate.get("evidence_count") or 1),
                    payload={
                        "candidate_id": candidate["id"],
                        "capture_id": capture_id,
                        "work_item_id": candidate.get("work_item_id"),
                        "task_key": candidate.get("task_key"),
                        "review_state": candidate.get("review_state"),
                        "evidence_id": evidence.get("evidence_id"),
                    },
                )
            conn.commit()
            promoted = None
            if args.auto_approve:
                promoted = promote_capture(conn, candidate)
                candidate = candidate_by_id(conn, int(candidate["id"])) or candidate
            result = {
                "action": {
                    "created": "candidate_created",
                    "associated": "evidence_associated",
                    "duplicate": "duplicate_ignored",
                }[candidate_action],
                "candidate": candidate,
                "evidence": evidence,
                "outbox_event": outbox_event,
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
        jira_key=args.jira_key,
    )
    if args.json:
        print_json(item)
    else:
        print(f"已确认自动采集候选 #{args.id}，生成工作记录 #{item['id']}")


def candidate_ids(values: list[str]) -> list[int]:
    ids: list[int] = []
    for value in values:
        for part in value.split(","):
            clean = part.strip()
            if not clean:
                continue
            try:
                parsed = int(clean)
            except ValueError as exc:
                raise SystemExit(f"候选 ID 无效：{clean}") from exc
            if parsed not in ids:
                ids.append(parsed)
    if not ids:
        raise SystemExit("至少需要一个候选 ID")
    return ids


def hook_promote_batch(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    ids = candidate_ids(args.ids)
    conn.execute("BEGIN")
    items = []
    try:
        for candidate_id in ids:
            candidate = candidate_by_id(conn, candidate_id)
            if candidate is None:
                raise SystemExit(f"找不到自动采集候选 #{candidate_id}")
            items.append(
                promote_capture(conn, candidate, commit=False)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if args.json:
        print_json({"count": len(items), "entries": items})
    else:
        print(f"已批量确认 {len(items)} 条自动采集候选")


def hook_ignore_batch(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    ids = candidate_ids(args.ids)
    conn.execute("BEGIN")
    try:
        for candidate_id in ids:
            candidate = candidate_by_id(conn, candidate_id)
            if candidate is None:
                raise SystemExit(f"找不到自动采集候选 #{candidate_id}")
            if candidate["review_state"] != "candidate":
                raise SystemExit("只能忽略尚未审核的候选")
            conn.execute(
                "UPDATE capture_candidates SET review_state = 'ignored', updated_at = ? WHERE id = ?",
                (timestamp(), candidate_id),
            )
            enqueue_integration_event(
                conn,
                aggregate_type="candidate",
                aggregate_id=str(candidate_id),
                event_type="candidate.ignored",
                aggregate_version=1,
                payload={"candidate_id": candidate_id, "review_state": "ignored"},
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = {"count": len(ids), "ids": ids, "state": "ignored"}
    if args.json:
        print_json(result)
    else:
        print(f"已批量忽略 {len(ids)} 条自动采集候选")


def hook_merge(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    ids = candidate_ids(args.ids)
    if len(ids) < 2:
        raise SystemExit("合并至少需要两个候选")
    conn.execute("BEGIN")
    try:
        candidates = [candidate_by_id(conn, candidate_id) for candidate_id in ids]
        if any(candidate is None for candidate in candidates):
            missing = ids[[candidate is None for candidate in candidates].index(True)]
            raise SystemExit(f"找不到自动采集候选 #{missing}")
        if any(candidate["review_state"] != "candidate" for candidate in candidates if candidate):
            raise SystemExit("只能合并尚未审核的候选")
        primary = candidates[0]
        assert primary is not None
        for candidate in candidates[1:]:
            assert candidate is not None
            primary = merge_candidate_observation(conn, primary, candidate)
            conn.execute(
                "UPDATE evidence SET candidate_id = ?, work_item_id = ? WHERE candidate_id = ?",
                (primary["id"], primary.get("work_item_id"), candidate["id"]),
            )
            conn.execute(
                "UPDATE capture_candidates SET review_state = 'ignored', merged_into = ?, updated_at = ? WHERE id = ?",
                (primary["id"], timestamp(), candidate["id"]),
            )
        conn.execute(
            "UPDATE capture_candidates SET evidence_count = (SELECT COUNT(*) FROM evidence WHERE candidate_id = ?), updated_at = ? WHERE id = ?",
            (primary["id"], timestamp(), primary["id"]),
        )
        merged = candidate_by_id(conn, int(primary["id"])) or primary
        enqueue_integration_event(
            conn,
            aggregate_type="candidate",
            aggregate_id=str(primary["id"]),
            event_type="candidate.merged",
            aggregate_version=int(merged.get("evidence_count") or 1),
            payload={"candidate_id": primary["id"], "merged_candidate_ids": ids[1:]},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if args.json:
        print_json(merged)
    else:
        print(f"已将候选 {','.join(map(str, ids))} 合并到 #{primary['id']}")


def hook_split(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    candidate = candidate_by_id(conn, args.id)
    if candidate is None:
        raise SystemExit(f"找不到自动采集候选 #{args.id}")
    if candidate["review_state"] != "candidate":
        raise SystemExit("只能拆分尚未审核的候选")
    try:
        parts = json.loads(args.parts)
    except json.JSONDecodeError as exc:
        raise SystemExit("--parts 必须是 JSON 数组") from exc
    if not isinstance(parts, list) or len(parts) < 2 or any(not isinstance(part, dict) for part in parts):
        raise SystemExit("--parts 必须包含至少两个对象")
    evidence_rows = conn.execute(
        "SELECT evidence_id FROM evidence WHERE candidate_id = ? ORDER BY id",
        (args.id,),
    ).fetchall()
    available = {row[0] for row in evidence_rows}
    assignments: list[tuple[dict[str, Any], list[str]]] = []
    assigned: set[str] = set()
    for part in parts:
        title = capture_text(part.get("title") or part.get("content"), 240)
        summary = capture_text(part.get("summary") or part.get("result"), 1200)
        evidence_ids = [str(value) for value in part.get("evidence_ids", [])]
        if not title:
            raise SystemExit("每个拆分事项都必须有 title")
        if any(value not in available or value in assigned for value in evidence_ids):
            raise SystemExit("Evidence 分配无效或重复，拆分已取消")
        assigned.update(evidence_ids)
        assignments.append((part, evidence_ids))
    if assigned != available:
        raise SystemExit("必须为每条 Evidence 指定且只指定一次归属")
    conn.execute("BEGIN")
    try:
        new_candidates = []
        for index, (part, evidence_ids) in enumerate(assignments, 1):
            title = capture_text(part.get("title") or part.get("content"), 240)
            summary = capture_text(part.get("summary") or part.get("result"), 1200)
            child_data = {
                "capture_id": f"split-{candidate['id']}-{index}-{hashlib.sha256(title.encode('utf-8')).hexdigest()[:12]}",
                "source": candidate["source"], "client": candidate["client"],
                "session_id": candidate.get("session_id"), "turn_id": candidate.get("turn_id"),
                "work_date": candidate["work_date"], "title": title, "summary": summary,
                "project": candidate.get("project", ""), "category": candidate.get("category", ""),
                "work_status": candidate.get("work_status", "done"),
                "start_time": candidate.get("start_time"), "end_time": candidate.get("end_time"),
                "duration_minutes": None, "cwd": candidate.get("cwd", ""),
                "tags": candidate.get("tags", ""), "task_key": None,
                "work_item_id": f"wi-split-{candidate['id']}-{index}",
                "confidence": candidate.get("confidence", 0.5), "provenance": "user-split",
            }
            # A split is an explicit user boundary correction; never let the
            # normal conservative auto-aggregation absorb a child candidate.
            child, _ = insert_capture_candidate(conn, child_data, aggregate=False)
            if evidence_ids:
                conn.execute(
                    "UPDATE evidence SET candidate_id = ?, work_item_id = ? WHERE evidence_id IN ({})".format(",".join("?" for _ in evidence_ids)),
                    [child["id"], child["work_item_id"], *evidence_ids],
                )
            refresh_candidate_time_fields(conn, int(child["id"]))
            new_candidates.append(candidate_by_id(conn, int(child["id"])))
        conn.execute(
            "UPDATE capture_candidates SET review_state = 'ignored', merged_into = NULL, updated_at = ? WHERE id = ?",
            (timestamp(), args.id),
        )
        enqueue_integration_event(
            conn,
            aggregate_type="candidate",
            aggregate_id=str(args.id),
            event_type="candidate.split",
            aggregate_version=len(new_candidates),
            payload={
                "candidate_id": args.id,
                "child_candidate_ids": [item["id"] for item in new_candidates],
                "children": [
                    event_snapshot(conn, "candidate", str(item["id"]))
                    for item in new_candidates
                ],
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    result = {"source_candidate_id": args.id, "candidates": new_candidates}
    if args.json:
        print_json(result)
    else:
        print(f"已将候选 #{args.id} 拆分为 {len(new_candidates)} 条候选")


def hook_ignore(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    candidate = candidate_by_id(conn, args.id)
    if candidate is None:
        raise SystemExit(f"找不到自动采集候选 #{args.id}")
    if candidate["review_state"] != "candidate":
        raise SystemExit("只能忽略尚未审核的候选")
    conn.execute(
        "UPDATE capture_candidates SET review_state = 'ignored', updated_at = ? WHERE id = ?",
        (timestamp(), args.id),
    )
    enqueue_integration_event(
        conn,
        aggregate_type="candidate",
        aggregate_id=str(args.id),
        event_type="candidate.ignored",
        aggregate_version=int(candidate.get("evidence_count") or 1),
        payload={"candidate_id": args.id, "review_state": "ignored"},
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


def outbox_list(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    where = []
    params: list[Any] = []
    if args.state:
        where.append("delivery_state = ?")
        params.append(args.state)
    if args.since:
        where.append("created_at >= ?")
        params.append(args.since)
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"SELECT * FROM integration_outbox{clause} ORDER BY id LIMIT ?",
        [*params, args.limit],
    ).fetchall()
    items = [dict(row) for row in rows]
    if args.json:
        print_json({"count": len(items), "events": items})
        return
    print("ID\t状态\t事件\t聚合\t聚合 ID\t尝试次数")
    for item in items:
        print(
            "\t".join(
                [
                    item["event_id"], item["delivery_state"], item["event_type"],
                    item["aggregate_type"], item["aggregate_id"], str(item["attempts"]),
                ]
            )
        )


def outbox_claim(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    now = timestamp()
    deadline = (datetime.now().astimezone() + timedelta(minutes=args.lease_minutes)).isoformat(timespec="seconds")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT * FROM integration_outbox
            WHERE (delivery_state IN ('pending', 'failed') AND available_at <= ?)
               OR (delivery_state = 'claimed' AND (claim_deadline IS NULL OR claim_deadline <= ?))
            ORDER BY id LIMIT 1
            """,
            (now, now),
        ).fetchone()
        if row is None:
            conn.commit()
            result = None
        else:
            claim_token = secrets.token_urlsafe(18)
            conn.execute(
                "UPDATE integration_outbox SET delivery_state = 'claimed', attempts = attempts + 1, claim_deadline = ?, claim_token = ?, updated_at = ? WHERE id = ?",
                (deadline, claim_token, now, row["id"]),
            )
            conn.commit()
            result = dict(conn.execute("SELECT * FROM integration_outbox WHERE id = ?", (row["id"],)).fetchone())
    except Exception:
        conn.rollback()
        raise
    if args.json:
        print_json({"event": result})
    elif result:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("没有可领取的 Integration Event。")


def outbox_ack(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    now = timestamp()
    cursor = conn.execute(
        "UPDATE integration_outbox SET delivery_state = 'delivered', claim_deadline = NULL, claim_token = NULL, updated_at = ? WHERE event_id = ? AND delivery_state = 'claimed' AND claim_token = ?",
        (now, args.event_id, args.claim_token),
    )
    if cursor.rowcount == 0:
        raise SystemExit("只能确认状态为 claimed 的 Integration Event")
    conn.commit()
    item = dict(conn.execute("SELECT * FROM integration_outbox WHERE event_id = ?", (args.event_id,)).fetchone())
    if args.json:
        print_json(item)
    else:
        print(f"已确认 Integration Event {args.event_id}")


def outbox_fail(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    now = timestamp()
    cursor = conn.execute(
        "UPDATE integration_outbox SET delivery_state = 'failed', claim_deadline = NULL, claim_token = NULL, last_error = ?, available_at = ?, updated_at = ? WHERE event_id = ? AND delivery_state = 'claimed' AND claim_token = ?",
        (capture_text(args.error, 500), now, now, args.event_id, args.claim_token),
    )
    if cursor.rowcount == 0:
        raise SystemExit("只能标记状态为 claimed 的 Integration Event")
    conn.commit()
    item = dict(conn.execute("SELECT * FROM integration_outbox WHERE event_id = ?", (args.event_id,)).fetchone())
    if args.json:
        print_json(item)
    else:
        print(f"已标记 Integration Event {args.event_id} 失败，可重试")


def git_evidence(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    repo = Path(args.repo).expanduser().resolve()
    if not (repo / ".git").exists():
        raise SystemExit(f"不是 Git 仓库：{repo}")
    command = ["git", "-C", str(repo), "log", "--format=%H%x09%aI%x09%s"]
    if args.since:
        command.extend(["--since", args.since])
    if args.until:
        command.extend(["--until", args.until])
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"Git 采集失败：{exc}") from exc
    records = []
    project = capture_text(args.project or repo.name, 200)
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        commit_hash, authored_at, subject = parts
        authored = event_datetime(authored_at)
        work_date = (authored or datetime.now().astimezone()).date().isoformat()
        task_key = capture_text(args.task_key, 200) if args.task_key else None
        work_item_id = task_key or f"git-{commit_hash[:20]}"
        candidate_data = {
            "capture_id": f"git-{commit_hash}",
            "source": "git",
            "client": "git",
            "work_date": work_date,
            "title": capture_text(subject, 240) or f"Git commit {commit_hash[:8]}",
            "summary": f"Git commit {commit_hash[:8]}",
            "project": project,
            "category": "Git Evidence",
            "work_status": "done",
            "start_time": authored.isoformat(timespec="seconds") if authored else None,
            "end_time": authored.isoformat(timespec="seconds") if authored else None,
            "duration_minutes": None,
            "cwd": str(repo),
            "tags": "git证据",
            "task_key": task_key,
            "work_item_id": work_item_id,
            "confidence": 0.7 if task_key else 0.4,
            "provenance": "git-commit",
        }
        candidate, action = insert_capture_candidate(conn, candidate_data)
        evidence, evidence_created = insert_evidence(
            conn,
            {
                "evidence_id": f"git-{commit_hash}",
                "external_id": commit_hash,
                "source": "git",
                "client": "git",
                "task_key": task_key,
                "work_item_id": candidate.get("work_item_id") or work_item_id,
                "work_date": work_date,
                "title": candidate_data["title"],
                "summary": candidate_data["summary"],
                "project": project,
                "category": "Git Evidence",
                "status": "done",
                "start_time": candidate_data["start_time"],
                "end_time": candidate_data["end_time"],
                "confidence": candidate_data["confidence"],
                "provenance": "git-commit",
                "metadata": {"repo": str(repo), "commit": commit_hash},
            },
            int(candidate["id"]),
        )
        outbox_event = None
        if evidence_created:
            refresh_candidate_time_fields(conn, int(candidate["id"]))
            candidate = candidate_by_id(conn, int(candidate["id"])) or candidate
            outbox_event = enqueue_integration_event(
                conn,
                aggregate_type="candidate",
                aggregate_id=str(candidate["id"]),
                event_type="evidence.git_added",
                aggregate_version=int(candidate.get("evidence_count") or 1),
                payload={"candidate_id": candidate["id"], "evidence_id": evidence["evidence_id"], "commit": commit_hash},
            )
        records.append({"candidate": candidate_by_id(conn, int(candidate["id"])), "evidence": evidence, "action": action, "outbox_event": outbox_event})
    conn.commit()
    result = {"repo": str(repo), "count": len(records), "records": records}
    if args.json:
        print_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


def cleanup_data(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    try:
        as_of = date.fromisoformat(args.as_of).isoformat() if args.as_of else date.today().isoformat()
    except ValueError as exc:
        raise SystemExit("--as-of 必须是 YYYY-MM-DD") from exc
    anchor = date.fromisoformat(as_of)
    evidence_cutoff = (anchor - timedelta(days=args.evidence_days)).isoformat()
    ignored_cutoff = (anchor - timedelta(days=args.ignored_candidate_days)).isoformat()
    plan = {
        "as_of": as_of,
        "evidence_cutoff": evidence_cutoff,
        "ignored_candidate_cutoff": ignored_cutoff,
        "evidence": int(
            conn.execute(
                """
                SELECT COUNT(*) FROM evidence e
                LEFT JOIN capture_candidates c ON c.id = e.candidate_id
                WHERE e.created_at < ? AND (e.candidate_id IS NULL OR c.review_state IN ('ignored', 'promoted'))
                """,
                (evidence_cutoff,),
            ).fetchone()[0]
        ),
        "automation_turns": int(
            conn.execute(
                "SELECT COUNT(*) FROM automation_turns WHERE updated_at < ?", (evidence_cutoff,)
            ).fetchone()[0]
        ),
        "automation_sessions": int(
            conn.execute(
                "SELECT COUNT(*) FROM automation_sessions WHERE updated_at < ?", (evidence_cutoff,)
            ).fetchone()[0]
        ),
        "ignored_candidates": int(
            conn.execute(
                "SELECT COUNT(*) FROM capture_candidates WHERE review_state = 'ignored' AND updated_at < ?",
                (ignored_cutoff,),
            ).fetchone()[0]
        ),
    }
    if args.execute:
        if not args.yes:
            raise SystemExit("实际清理需要 --execute --yes")
        conn.execute("BEGIN")
        try:
            conn.execute(
                "DELETE FROM evidence WHERE id IN (SELECT e.id FROM evidence e LEFT JOIN capture_candidates c ON c.id = e.candidate_id WHERE e.created_at < ? AND (e.candidate_id IS NULL OR c.review_state IN ('ignored', 'promoted')))",
                (evidence_cutoff,),
            )
            conn.execute(
                "DELETE FROM capture_candidates WHERE review_state = 'ignored' AND updated_at < ?",
                (ignored_cutoff,),
            )
            conn.execute("DELETE FROM automation_turns WHERE updated_at < ?", (evidence_cutoff,))
            conn.execute("DELETE FROM automation_sessions WHERE updated_at < ?", (evidence_cutoff,))
            conn.commit()
            plan["executed"] = True
        except Exception:
            conn.rollback()
            raise
    else:
        plan["executed"] = False
    if args.json:
        print_json(plan)
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))


def sqlite_backup(args: argparse.Namespace, conn: sqlite3.Connection, db_path: Path) -> None:
    output = Path(args.output).expanduser().resolve()
    if output == db_path:
        raise SystemExit("备份路径不能与当前数据库相同")
    if output.exists() and not args.force:
        raise SystemExit(f"备份文件已存在：{output}；如需覆盖请传 --force")
    output.parent.mkdir(parents=True, exist_ok=True)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(FULL)")
    destination = sqlite3.connect(output)
    try:
        conn.backup(destination)
        destination.commit()
    finally:
        destination.close()
    result = {
        "path": str(output),
        "schema_version": SCHEMA_VERSION,
        "bytes": output.stat().st_size,
    }
    if args.json:
        print_json(result)
    else:
        print(output)


def export_data(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    def rows(table: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]

    entries = [
        row_to_dict(row)
        for row in conn.execute(
            """
            SELECT e.*,
                   COALESCE((
                       SELECT group_concat(tag, ',')
                       FROM (SELECT tag FROM entry_tags WHERE entry_id = e.id ORDER BY tag)
                   ), '') AS tags
            FROM entries e
            ORDER BY e.id
            """
        ).fetchall()
    ]
    payload = {
        "export_schema_version": 1,
        "weeklylog_schema_version": SCHEMA_VERSION,
        "entries": entries,
        "reflections": rows("weekly_reflections"),
        "candidates": rows("capture_candidates"),
        "evidence": rows("evidence"),
        "outbox": rows("integration_outbox"),
        "automation_sessions": [
            {key: value for key, value in row.items() if key not in {"last_prompt"}}
            for row in rows("automation_sessions")
        ],
        "automation_turns": [
            {key: value for key, value in row.items() if key not in {"prompt", "assistant_message"}}
            for row in rows("automation_turns")
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding="utf-8")
        if args.json:
            print_json({"path": str(output), "export_schema_version": 1, "counts": {key: len(value) for key, value in payload.items() if isinstance(value, list)}})
        else:
            print(output)
    else:
        print(encoded, end="")


def import_data(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    class ImportFailure(Exception):
        def __init__(self, message: str, code: str = "import_error"):
            super().__init__(message)
            self.code = code

    def fail(message: str, code: str = "import_error") -> None:
        raise ImportFailure(message, code)

    def emit_failure(exc: ImportFailure) -> None:
        if args.json:
            print_json({
                "imported": False,
                "error": {"code": exc.code, "message": str(exc)},
            })

    path = Path(args.input).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        failure = ImportFailure(f"无法读取导入文件：{exc}", "invalid_input")
        emit_failure(failure)
        raise SystemExit(2) from exc
    if not isinstance(payload, dict) or payload.get("export_schema_version") != 1:
        failure = ImportFailure(
            "不支持的导出格式版本；需要 export_schema_version=1",
            "unsupported_export_schema",
        )
        emit_failure(failure)
        raise SystemExit(2)
    source_schema = payload.get("weeklylog_schema_version", 1)
    if not isinstance(source_schema, int) or source_schema > SCHEMA_VERSION:
        failure = ImportFailure(
            f"不支持的 weeklylog schema 版本：{source_schema}（当前最高 {SCHEMA_VERSION}）",
            "unsupported_weeklylog_schema",
        )
        emit_failure(failure)
        raise SystemExit(2)
    collection_names = (
        "entries", "reflections", "candidates", "evidence", "outbox",
        "automation_sessions", "automation_turns",
    )
    invalid_collections = [
        name for name in collection_names
        if name in payload and not isinstance(payload[name], list)
    ]
    if invalid_collections:
        failure = ImportFailure(
            "导出数据集合必须是 JSON 数组：" + ", ".join(invalid_collections),
            "invalid_collection",
        )
        emit_failure(failure)
        raise SystemExit(2)

    def ensure_same(table: str, key: str | tuple[str, ...], item: dict[str, Any]) -> None:
        keys = (key,) if isinstance(key, str) else key
        predicate = " AND ".join(f"{name} = ?" for name in keys)
        existing = conn.execute(
            f"SELECT * FROM {table} WHERE {predicate}",
            tuple(item[name] for name in keys),
        ).fetchone()
        if existing is not None:
            current = dict(existing)
            if any(current.get(name) != item.get(name) for name in item if name in current and name not in {"created_at", "updated_at"}):
                key_text = ", ".join(f"{name}={item.get(name)}" for name in keys)
                fail(f"导入冲突：{table} ({key_text})", "conflict")
            return
        columns = [name for name in item if name in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}]
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
            [item[name] for name in columns],
        )

    conn.execute("BEGIN")
    try:
        for item in payload.get("entries", []):
            entry = dict(item)
            tags = entry.pop("tags", [])
            ensure_same("entries", "id", entry)
            entry_id = int(entry["id"])
            for tag in tags:
                conn.execute("INSERT OR IGNORE INTO entry_tags(entry_id, tag) VALUES (?, ?)", (entry_id, str(tag)))
        for table, key in (
            ("weekly_reflections", "week_start"),
            ("capture_candidates", "id"),
            ("evidence", "id"),
            ("integration_outbox", "id"),
            ("automation_sessions", "session_id"),
            ("automation_turns", ("session_id", "turn_id")),
        ):
            payload_key = {
                "weekly_reflections": "reflections",
                "capture_candidates": "candidates",
                "integration_outbox": "outbox",
            }.get(table, table)
            for item in payload.get(payload_key, []):
                safe = dict(item)
                for forbidden in ("last_prompt", "prompt", "assistant_message"):
                    safe.pop(forbidden, None)
                if table == "automation_sessions":
                    safe.setdefault("last_prompt", "")
                if table == "automation_turns":
                    safe.setdefault("prompt", "")
                    safe.setdefault("assistant_message", "")
                ensure_same(table, key, safe)
        conn.commit()
    except ImportFailure as exc:
        conn.rollback()
        emit_failure(exc)
        raise SystemExit(2) from exc
    except Exception:
        conn.rollback()
        raise
    result = {
        "imported": True,
        "export_schema_version": 1,
        "counts": {key: len(value) for key, value in payload.items() if isinstance(value, list)},
    }
    if args.json:
        print_json(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


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
    if getattr(args, "jira_key", None):
        where.append("e.jira_key = ?")
        params.append(args.jira_key)
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
        jira_key=None,
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
    durations = time_summary(items)
    source_counts = dict(
        conn.execute(
            "SELECT COALESCE(source, 'manual'), COUNT(*) FROM entries WHERE work_date BETWEEN ? AND ? GROUP BY COALESCE(source, 'manual')",
            (start, end),
        ).fetchall()
    )
    pending = pending_candidates(conn, start, end, limit=100)
    payload = {
        "schema_version": 2,
        "dataset_schema_version": 2,
        "range": {"from": start, "to": end, "label": label},
        "entries": items,
        "reflections": reflections_for_range(conn, start, end),
        "pending_candidates": pending,
        "time_summary": durations,
        "coverage": {
            "entry_sources": source_counts,
            "notes": "会议、线下沟通和纯手动工作可能未被自动采集；请按需补录。",
        },
    }
    if getattr(args, "draft", False):
        payload["draft"] = True
        payload["candidates"] = pending
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(encoded, encoding="utf-8")
        if getattr(args, "json", False) and getattr(args, "output", None):
            print_json({"path": str(output_path), "schema_version": payload["schema_version"]})
            return
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
        "jira_key": args.jira_key.strip() if args.jira_key is not None else None,
    }
    if fields["content"] == "":
        raise SystemExit("工作事项不能为空")
    updates = [(name, value) for name, value in fields.items() if value is not None]
    time_changed = any(value is not None for value in (args.start, args.end, args.minutes))
    if time_changed:
        start_time, end_time, computed_duration = resolve_times(
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
                "elapsed_start": start_time,
                "elapsed_end": end_time,
            }
        )
        # Explicit --minutes is a human confirmation. A start/end-only edit
        # updates natural-span fields while preserving an existing AI or
        # human duration; legacy records may still refresh their old field.
        if args.minutes is not None or (
            current.get("ai_duration_minutes") is None
            and current.get("human_duration_minutes") is None
        ):
            fields["duration_minutes"] = computed_duration
        if args.minutes is not None:
            fields["human_duration_minutes"] = computed_duration
        updates = [
            (name, value)
            for name, value in fields.items()
            if value is not None
            and name not in {"start_time", "end_time", "elapsed_start", "elapsed_end", "duration_minutes"}
        ]
        updates.extend(
            (name, fields[name])
            for name in ("start_time", "end_time", "elapsed_start", "elapsed_end", "duration_minutes")
            if name in fields
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
    conn.execute(
        "UPDATE entries SET aggregate_version = aggregate_version + 1, updated_at = ? WHERE id = ?",
        (timestamp(), args.id),
    )
    updated_version = int(
        conn.execute("SELECT aggregate_version FROM entries WHERE id = ?", (args.id,)).fetchone()[0]
    )
    enqueue_integration_event(
        conn,
        aggregate_type="confirmed_record",
        aggregate_id=str(args.id),
        event_type="record.updated",
        aggregate_version=updated_version,
        payload={"entry_id": args.id, "aggregate_version": updated_version},
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
    enqueue_integration_event(
        conn,
        aggregate_type="confirmed_record",
        aggregate_id=str(args.id),
        event_type="record.deleted",
        aggregate_version=int(item.get("aggregate_version") or 1) + 1,
        payload={"entry_id": args.id, "previous": item},
    )
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
    if item.get("human_duration_minutes") is not None:
        details.append(f"人工确认时长：{format_duration(item['human_duration_minutes'])}")
    elif item.get("ai_duration_minutes") is not None:
        details.append(f"AI 可观测时长：{format_duration(item['ai_duration_minutes'])}")
    elif item.get("duration_minutes") is not None:
        details.append(f"旧版/未分类时长：{format_duration(item['duration_minutes'])}")
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
    durations = time_summary(items)
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

    lines.extend(
        [
            "## 投入概览",
            "",
            f"- AI 可观测时长：{format_duration(durations['ai_observed_minutes'])}",
            f"- 人工确认时长：{format_duration(durations['human_confirmed_minutes'])}",
            f"- 旧版/未分类时长：{format_duration(durations['legacy_or_unclassified_minutes'])}",
            f"- 有自然跨度的记录：{durations['elapsed_record_count']} 项（自然跨度不等同投入）",
            "",
        ]
    )

    planned = [item for item in items if item["status"] == "planned"]
    lines.extend(["## 下周计划（仅列出明确标记为 planned 的记录）", ""])
    lines.extend(markdown_item(item) for item in planned)
    if not planned:
        lines.append("还没有明确记录的下周计划。")
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
    if args.template:
        template_path = Path(args.template).expanduser().resolve()
        try:
            template = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit(f"无法读取周报模板：{exc}") from exc
        item_text = "\n".join(markdown_item(item) for item in items)
        coverage = "自动采集记录可能遗漏会议、线下沟通和纯手动工作，请按需补录。"
        output = (
            template.replace("{{summary}}", f"{start} 至 {end}，共 {len(items)} 项正式记录")
            .replace("{{items}}", item_text)
            .replace("{{coverage}}", coverage)
            .replace(
                "{{time_summary}}",
                "；".join(
                    [
                        f"AI 可观测 {format_duration(durations['ai_observed_minutes'])}",
                        f"人工确认 {format_duration(durations['human_confirmed_minutes'])}",
                        f"旧版/未分类 {format_duration(durations['legacy_or_unclassified_minutes'])}",
                    ]
                ),
            )
        )
        if not output.endswith("\n"):
            output += "\n"
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
        item_minutes, _ = report_duration(item)
        if item_minutes is not None:
            minutes[project] += item_minutes
    return sorted(
        ((project, counts[project], minutes[project]) for project in counts),
        key=lambda value: (-value[2], -value[1], value[0]),
    )


def time_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize AI-observed, human-confirmed and legacy durations separately."""
    ai_minutes = sum(
        int(item["ai_duration_minutes"])
        for item in items
        if item.get("ai_duration_minutes") is not None
    )
    human_minutes = sum(
        int(item["human_duration_minutes"])
        for item in items
        if item.get("human_duration_minutes") is not None
    )
    legacy_minutes = sum(
        int(item["duration_minutes"])
        for item in items
        if item.get("duration_minutes") is not None
        and item.get("ai_duration_minutes") is None
        and item.get("human_duration_minutes") is None
    )
    elapsed_count = sum(
        1
        for item in items
        if item.get("elapsed_start") or item.get("elapsed_end")
    )
    return {
        "ai_observed_minutes": ai_minutes,
        "human_confirmed_minutes": human_minutes,
        "legacy_or_unclassified_minutes": legacy_minutes,
        "elapsed_record_count": elapsed_count,
        "labels": {
            "ai_observed": "AI 可观测时长",
            "human_confirmed": "人工确认时长",
            "legacy_or_unclassified": "旧版/未分类时长",
            "elapsed": "自然跨度（不等同投入）",
        },
    }


def report_duration(item: dict[str, Any]) -> tuple[int | None, str]:
    """Choose a displayed duration without conflating its semantic source."""
    if item.get("human_duration_minutes") is not None:
        return int(item["human_duration_minutes"]), "人工确认"
    if item.get("ai_duration_minutes") is not None:
        return int(item["ai_duration_minutes"]), "AI 可观测"
    if item.get("duration_minutes") is not None:
        return int(item["duration_minutes"]), "旧版/未分类"
    return None, ""


def detailed_report(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    items, start, end = entries_for_week(conn, args.week)
    reflection = reflection_for_week(conn, args.week)
    pending_captures = pending_candidate_count(conn, start, end)
    counts = Counter(item["status"] for item in items)
    durations = time_summary(items)
    tracked = [item for item in items if report_duration(item)[0] is not None]
    total_minutes = sum(report_duration(item)[0] or 0 for item in tracked)
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
            f"- 有兼容时长记录：{len(tracked)} 项，合计 {format_duration(total_minutes)}（不代表人工投入）",
            f"- 覆盖工作日：{len({item['work_date'] for item in items})} 天",
            f"- 最早开始：{format_clock(earliest_item.get('start_time')) if earliest_item else '未记录'}",
            f"- 最晚结束：{format_clock(late_item.get('end_time')) if late_item else '未记录'}",
            f"- 晚间/凌晨结束（22:00–05:00）：{len(late_night_items)} 次",
            f"- 自动采集待确认：{pending_captures} 条（不计入正式统计）",
            f"- AI 可观测时长：{format_duration(durations['ai_observed_minutes'])}",
            f"- 人工确认时长：{format_duration(durations['human_confirmed_minutes'])}",
            f"- 旧版/未分类时长：{format_duration(durations['legacy_or_unclassified_minutes'])}",
            "",
        ]
    )

    lines.extend(["## 每天发生了什么", ""])
    if not items:
        lines.append("本周暂无工作记录。")
    else:
        for work_date in sorted({item["work_date"] for item in items}):
            day_items = [item for item in items if item["work_date"] == work_date]
            day_minutes = sum(report_duration(item)[0] or 0 for item in day_items)
            lines.append(f"### {work_date} · {len(day_items)} 项 · {format_duration(day_minutes)}")
            for item in day_items:
                details = [time_range_text(item), item["status"]]
                item_minutes, item_label = report_duration(item)
                if item_minutes is not None:
                    details.append(f"{item_label} {format_duration(item_minutes)}")
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
            sorted(tracked, key=lambda value: (-(report_duration(value)[0] or 0), value["work_date"])), 1
        ):
            item_minutes, item_label = report_duration(item)
            lines.append(
                f"{index}. **{format_duration(item_minutes)}** · {item['content']}（{item['work_date']}；{item_label}）"
            )
    else:
        lines.append("还没有耗时数据；补充 `--minutes` 或 `--start/--end` 后，这一节会自动出现。")
    lines.append("")

    lines.extend(["## 项目与记录时长", ""])
    summaries = project_summary(items)
    if summaries:
        lines.append("| 项目 | 事项数 | 已记录时长（兼容） |")
        lines.append("| --- | ---: | ---: |")
        for project, count, minutes in summaries:
            lines.append(f"| {project} | {count} | {format_duration(minutes)} |")
    else:
        lines.append("暂无项目数据。")
    lines.append("")

    lines.extend(
        [
            "## 时间语义分开看",
            "",
            f"- AI 可观测时长：{format_duration(durations['ai_observed_minutes'])}",
            f"- 人工确认时长：{format_duration(durations['human_confirmed_minutes'])}",
            f"- 旧版/未分类时长：{format_duration(durations['legacy_or_unclassified_minutes'])}",
            f"- 自然跨度记录：{durations['elapsed_record_count']} 项（不等同投入）",
            "",
        ]
    )

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
            f"{untracked} 项未记录耗时，因此时长与最晚结束时间只代表已填写的数据。",
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
    durations = time_summary(items)
    tracked = [item for item in items if report_duration(item)[0] is not None]
    total_minutes = sum(report_duration(item)[0] or 0 for item in tracked)
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
            f"- 有兼容时长记录：{len(tracked)} 项，合计 {format_duration(total_minutes)}（不代表人工投入）",
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
    lines.append(f"- AI 可观测时长：{format_duration(durations['ai_observed_minutes'])}")
    lines.append(f"- 人工确认时长：{format_duration(durations['human_confirmed_minutes'])}")
    lines.append(f"- 旧版/未分类时长：{format_duration(durations['legacy_or_unclassified_minutes'])}")
    moods = [float(item["mood"]) for item in reflections if item["mood"] is not None]
    if moods:
        lines.append(f"- 平均幸福指数：{sum(moods) / len(moods):.1f} / 5（{len(moods)} 周有评分）")
    lines.append("")

    lines.extend(["## 每月节奏", "", "| 月份 | 事项数 | 完成 | 已记录时长（兼容） | 工作日 |", "| --- | ---: | ---: | ---: | ---: |"])
    for month in range(1, 13):
        month_items = [item for item in items if int(item["work_date"][5:7]) == month]
        month_minutes = sum(report_duration(item)[0] or 0 for item in month_items)
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
            sorted(tracked, key=lambda value: -(report_duration(value)[0] or 0))[:10], 1
        ):
            item_minutes, item_label = report_duration(item)
            lines.append(f"{index}. **{format_duration(item_minutes)}** · {item['content']}（{item['work_date']}；{item_label}）")
    else:
        lines.append("还没有耗时数据。")
    lines.append("")

    lines.extend(["## 项目分布", ""])
    summaries = project_summary(items)
    if summaries:
        lines.append("| 项目 | 事项数 | 已记录时长（兼容） |")
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
            f"> 说明：有 {len(items) - len(tracked)} 项没有耗时数据；年度时长统计只代表已填写的部分。",
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
    durations = time_summary(items)
    tracked = [item for item in items if report_duration(item)[0] is not None]
    latest = max(
        (item for item in items if item_datetime(item, "end_time")),
        key=lambda item: item_datetime(item, "end_time"),
        default=None,
    )
    longest = max(tracked, key=lambda item: report_duration(item)[0] or 0, default=None)
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
            "footer": f"完成 {sum(item['status'] == 'done' for item in items)} 项 · 专注 {len({item['work_date'] for item in items})} 天 · AI 可观测 {format_duration(durations['ai_observed_minutes'])} · 人工确认 {format_duration(durations['human_confirmed_minutes'])}",
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
            "metric": format_duration(report_duration(longest)[0]) if longest else "—",
            "metric_label": "单条记录最长时长（字段见详情）",
            "body": longest_text,
            "footer": f"AI 可观测 {format_duration(durations['ai_observed_minutes'])} · 人工确认 {format_duration(durations['human_confirmed_minutes'])}；没有时长也没关系，下周可以从一条开始。",
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
    durations = time_summary(items)
    tracked = [item for item in items if report_duration(item)[0] is not None]
    late = [item for item in items if is_late_finish(item_datetime(item, "end_time"))]
    longest = max(tracked, key=lambda item: report_duration(item)[0] or 0, default=None)
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
            "footer": f"{len({item['work_date'] for item in items})} 个工作日 · {len({week_range(item['work_date'])[0] for item in items})} 周留下痕迹 · AI 可观测 {format_duration(durations['ai_observed_minutes'])} · 人工确认 {format_duration(durations['human_confirmed_minutes'])}",
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
            "metric": format_duration(report_duration(longest)[0]) if longest else "—",
            "metric_label": "单条记录最长时长（字段见详情）",
            "body": longest_text,
            "footer": f"全年 AI 可观测 {format_duration(durations['ai_observed_minutes'])} · 人工确认 {format_duration(durations['human_confirmed_minutes'])}。",
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

    init_parser = subparsers.add_parser("init", help="初始化数据库")
    init_parser.add_argument("--json", action="store_true", help="输出初始化状态 JSON")
    subparsers.add_parser("path", help="显示数据库路径")

    add = subparsers.add_parser("add", help="添加一条工作记录")
    add.add_argument("--date", type=iso_day, help="工作日期，默认今天")
    add.add_argument("--content", required=True, help="工作事项")
    add.add_argument("--project", default="", help="项目")
    add.add_argument("--category", default="", help="分类")
    add.add_argument("--status", choices=STATUSES, default="done", help="状态")
    add.add_argument("--result", default="", help="成果或影响")
    add.add_argument("--jira-key", help="关联的 Jira key（仅作引用）")
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
    listing.add_argument("--jira-key", help="精确匹配 Jira 引用")
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
    dataset.add_argument("--draft", action="store_true", help="显式包含待确认候选并标记为草稿")
    dataset.add_argument("--json", action="store_true", help="兼容机器调用；默认即输出 JSON")

    update = subparsers.add_parser("update", help="更新一条工作记录")
    update.add_argument("id", type=int, help="记录 ID")
    update.add_argument("--date", type=iso_day, help="工作日期")
    update.add_argument("--content", help="工作事项")
    update.add_argument("--project", help="项目；传空字符串可清空")
    update.add_argument("--category", help="分类；传空字符串可清空")
    update.add_argument("--status", choices=STATUSES, help="状态")
    update.add_argument("--result", help="成果或影响；传空字符串可清空")
    update.add_argument("--jira-key", help="关联的 Jira key；传空字符串可清空")
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
    report_parser.add_argument("--template", help="可选 Markdown 模板路径，支持 {{summary}}、{{items}}、{{coverage}}")

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
    promote.add_argument("--jira-key", help="覆盖 Jira 引用")
    promote.add_argument("--tags", help="替换标签；传空字符串可清空")
    promote.add_argument("--json", action="store_true", help="输出 JSON")

    promote_batch = hook_subparsers.add_parser("promote-batch", help="批量确认自动采集候选")
    promote_batch.add_argument("ids", nargs="+", help="候选 ID，可用逗号分隔")
    promote_batch.add_argument("--json", action="store_true")

    merge = hook_subparsers.add_parser("merge", help="合并多个尚未审核的候选")
    merge.add_argument("ids", nargs="+", help="候选 ID，可用逗号分隔")
    merge.add_argument("--json", action="store_true")

    split = hook_subparsers.add_parser("split", help="将候选按 Evidence 分拆")
    split.add_argument("id", type=int, help="候选 ID")
    split.add_argument("--parts", required=True, help="JSON 数组，每项包含 title、summary、evidence_ids")
    split.add_argument("--json", action="store_true")

    ignore = hook_subparsers.add_parser("ignore", help="忽略一条自动采集候选")
    ignore.add_argument("id", type=int, help="候选 ID")
    ignore.add_argument("--json", action="store_true", help="输出 JSON")

    ignore_batch = hook_subparsers.add_parser("ignore-batch", help="批量忽略自动采集候选")
    ignore_batch.add_argument("ids", nargs="+", help="候选 ID，可用逗号分隔")
    ignore_batch.add_argument("--json", action="store_true")

    config = hook_subparsers.add_parser("config", help="输出客户端 hook 配置片段")
    config.add_argument("--client", choices=("codex",), default="codex")
    config.add_argument(
        "--auto-approve",
        action="store_true",
        help="配置为自动直接入账；默认保留候选待确认",
    )

    outbox = subparsers.add_parser("outbox", help="查看和领取本地 Integration Event")
    outbox_subparsers = outbox.add_subparsers(dest="outbox_command", required=True)
    outbox_list_parser = outbox_subparsers.add_parser("list", help="列出本地事件")
    outbox_list_parser.add_argument(
        "--state", choices=("pending", "claimed", "delivered", "failed")
    )
    outbox_list_parser.add_argument("--since", help="只显示此时间之后创建的事件")
    outbox_list_parser.add_argument("--limit", type=nonnegative_int, default=100)
    outbox_list_parser.add_argument("--json", action="store_true")
    outbox_claim_parser = outbox_subparsers.add_parser("claim", help="领取一个待投递事件")
    outbox_claim_parser.add_argument(
        "--lease-minutes", type=nonnegative_int, default=10,
        help="领取租约时长（分钟），过期后可被其他消费者回收；默认 10",
    )
    outbox_claim_parser.add_argument("--json", action="store_true")
    outbox_ack_parser = outbox_subparsers.add_parser("ack", help="确认事件已投递")
    outbox_ack_parser.add_argument("event_id")
    outbox_ack_parser.add_argument("--claim-token", required=True)
    outbox_ack_parser.add_argument("--json", action="store_true")
    outbox_fail_parser = outbox_subparsers.add_parser("fail", help="标记事件投递失败并允许重试")
    outbox_fail_parser.add_argument("event_id")
    outbox_fail_parser.add_argument("--error", required=True)
    outbox_fail_parser.add_argument("--claim-token", required=True)
    outbox_fail_parser.add_argument("--json", action="store_true")

    evidence = subparsers.add_parser("evidence", help="采集辅助 Evidence")
    evidence_subparsers = evidence.add_subparsers(dest="evidence_command", required=True)
    git_parser = evidence_subparsers.add_parser("git", help="把 Git commit 元数据写入候选 Evidence")
    git_parser.add_argument("--repo", default=".")
    git_parser.add_argument("--since", help="Git 可接受的起始时间")
    git_parser.add_argument("--until", help="Git 可接受的结束时间")
    git_parser.add_argument("--project")
    git_parser.add_argument("--task-key")
    git_parser.add_argument("--json", action="store_true")

    cleanup = subparsers.add_parser("cleanup", help="预览或执行本地保留期清理")
    cleanup.add_argument("--as-of", help="清理基准日期，默认今天")
    cleanup.add_argument("--evidence-days", type=nonnegative_int, default=90)
    cleanup.add_argument("--ignored-candidate-days", type=nonnegative_int, default=30)
    cleanup.add_argument("--execute", action="store_true")
    cleanup.add_argument("--yes", action="store_true")
    cleanup.add_argument("--json", action="store_true")

    backup = subparsers.add_parser("backup", help="创建一致的 SQLite 备份")
    backup.add_argument("--output", required=True)
    backup.add_argument("--force", action="store_true")
    backup.add_argument("--json", action="store_true")

    export_parser = subparsers.add_parser("export", help="导出可恢复的 JSON 数据")
    export_parser.add_argument("--output")
    export_parser.add_argument("--json", action="store_true", help="输出导出状态 JSON（指定 --output 时有效）")

    import_parser = subparsers.add_parser("import", help="从 JSON 导入到本地数据库")
    import_parser.add_argument("--input", required=True)
    import_parser.add_argument("--json", action="store_true")

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

    conn, migration = connect(db_path)
    try:
        if migration.migration_backup:
            print(
                f"已创建迁移备份：{migration.migration_backup}",
                file=sys.stderr,
            )
        if args.command == "init":
            if args.json:
                print_json(
                    {
                        "database": str(db_path),
                        "schema_version": migration.schema_version,
                        "migration_backup": str(migration.migration_backup)
                        if migration.migration_backup
                        else None,
                    }
                )
            else:
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
            elif args.hook_command == "promote-batch":
                hook_promote_batch(args, conn)
            elif args.hook_command == "merge":
                hook_merge(args, conn)
            elif args.hook_command == "split":
                hook_split(args, conn)
            elif args.hook_command == "ignore":
                hook_ignore(args, conn)
            elif args.hook_command == "ignore-batch":
                hook_ignore_batch(args, conn)
            else:
                parser.error(f"未知 hook 命令：{args.hook_command}")
        elif args.command == "outbox":
            if args.outbox_command == "list":
                outbox_list(args, conn)
            elif args.outbox_command == "claim":
                outbox_claim(args, conn)
            elif args.outbox_command == "ack":
                outbox_ack(args, conn)
            elif args.outbox_command == "fail":
                outbox_fail(args, conn)
            else:
                parser.error(f"未知 outbox 命令：{args.outbox_command}")
        elif args.command == "evidence":
            if args.evidence_command == "git":
                git_evidence(args, conn)
            else:
                parser.error(f"未知 evidence 命令：{args.evidence_command}")
        elif args.command == "cleanup":
            cleanup_data(args, conn)
        elif args.command == "backup":
            sqlite_backup(args, conn, db_path)
        elif args.command == "export":
            export_data(args, conn)
        elif args.command == "import":
            import_data(args, conn)
        else:
            parser.error(f"未知命令：{args.command}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
