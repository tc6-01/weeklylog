#!/usr/bin/env node

import { DatabaseSync } from "node:sqlite";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import readline from "node:readline/promises";
import crypto from "node:crypto";

type Db = InstanceType<typeof DatabaseSync>;
type Row = Record<string, any>;

const ROOT = path.resolve(__dirname, "..");
const DEFAULT_DB = path.join(os.homedir(), ".codex", "data", "weeklylog", "worklog.sqlite3");
const VERSION = "0.4.0";
const STATUSES = new Set(["done", "in-progress", "blocked", "planned", "other"]);
const EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"];
const MAX_DB_BYTES = 10 * 1024 * 1024;
const SQLITE_PAGE_SIZE = 4096;
const SESSION_RETENTION_DAYS = 14;
const CANDIDATE_RETENTION_DAYS = 30;
const MAX_RETAINED_SESSIONS = 256;
const DB_PATHS = new WeakMap<Db, string>();

function die(message: string): never {
  throw new Error(message);
}

function now(): string {
  return new Date().toISOString();
}

function localDate(): string {
  const d = new Date();
  const offset = d.getTimezoneOffset() * 60000;
  return new Date(d.getTime() - offset).toISOString().slice(0, 10);
}

function validDate(value: string | undefined): string {
  const date = value || localDate();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) die(`日期必须是 YYYY-MM-DD 格式：${date}`);
  const parsed = new Date(`${date}T12:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== date) {
    die(`日期无效：${date}`);
  }
  return date;
}

function weekBounds(value?: string): { start: string; end: string } {
  const date = validDate(value);
  const d = new Date(`${date}T12:00:00Z`);
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() - day + 1);
  const start = d.toISOString().slice(0, 10);
  d.setUTCDate(d.getUTCDate() + 6);
  return { start, end: d.toISOString().slice(0, 10) };
}

function short(value: unknown, max: number): string {
  return String(value ?? "").replace(/\s+/g, " ").trim().slice(0, max);
}

function bool(args: string[], name: string): boolean {
  return args.includes(name);
}

function option(args: string[], name: string, required = false): string | undefined {
  const index = args.indexOf(name);
  if (index < 0) return undefined;
  const value = args[index + 1];
  if (!value || value.startsWith("--")) {
    if (required) die(`${name} 需要值`);
    return undefined;
  }
  return value;
}

function options(args: string[], name: string): string[] {
  const result: string[] = [];
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === name && args[i + 1] && !args[i + 1].startsWith("--")) result.push(args[i + 1]);
  }
  return result;
}

function json(value: unknown): void {
  console.log(JSON.stringify(value, null, 2));
}

function dbRun(db: Db, sql: string, ...params: any[]): any {
  return (db.prepare(sql) as any).run(...params);
}

function dbGet(db: Db, sql: string, ...params: any[]): Row | undefined {
  return (db.prepare(sql) as any).get(...params) as Row | undefined;
}

function dbAll(db: Db, sql: string, ...params: any[]): Row[] {
  return (db.prepare(sql) as any).all(...params) as Row[];
}

function tableExists(db: Db, table: string): boolean {
  return Boolean(dbGet(db, "SELECT 1 AS present FROM sqlite_master WHERE type = 'table' AND name = ?", table));
}

function setPageCeiling(db: Db): void {
  const pageSize = Number(dbGet(db, "PRAGMA page_size")?.page_size || SQLITE_PAGE_SIZE);
  const maxPages = Math.max(1, Math.floor(MAX_DB_BYTES / pageSize));
  try { db.exec(`PRAGMA max_page_count = ${maxPages}`); } catch { /* an older oversized file is compacted on close */ }
}

function columnNames(db: Db, table: string): Set<string> {
  return new Set(dbAll(db, `PRAGMA table_info(${table})`).map((row) => String(row.name)));
}

function ensureColumn(db: Db, table: string, column: string, definition: string): void {
  if (!columnNames(db, table).has(column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`);
}

function initSchema(db: Db): void {
  db.exec("PRAGMA foreign_keys = ON");
  // Hooks are short-lived one-shot processes. DELETE journaling avoids a growing
  // -wal sidecar and keeps the on-disk footprint predictable.
  try { db.exec("PRAGMA wal_checkpoint(TRUNCATE)"); } catch { /* fresh databases have no WAL */ }
  db.exec("PRAGMA journal_mode = DELETE");
  db.exec("PRAGMA busy_timeout = 3000");
  db.exec(`PRAGMA page_size = ${SQLITE_PAGE_SIZE}`);
  setPageCeiling(db);
  const legacySessions = tableExists(db, "automation_sessions");
  const legacyTurns = tableExists(db, "automation_turns");
  db.exec(`
    CREATE TABLE IF NOT EXISTS entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      work_date TEXT NOT NULL,
      content TEXT NOT NULL,
      project TEXT NOT NULL DEFAULT '',
      category TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'done',
      result TEXT NOT NULL DEFAULT '',
      start_time TEXT,
      end_time TEXT,
      duration_minutes INTEGER,
      source TEXT NOT NULL DEFAULT 'manual',
      capture_id TEXT,
      jira_key TEXT,
      ai_turn_count INTEGER,
      collaboration_note TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entry_tags (
      entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
      tag TEXT NOT NULL,
      PRIMARY KEY (entry_id, tag)
    );
    CREATE TABLE IF NOT EXISTS weekly_reflections (
      week_start TEXT PRIMARY KEY,
      mood REAL,
      headline TEXT NOT NULL DEFAULT '',
      hard_moment TEXT NOT NULL DEFAULT '',
      happy_moment TEXT NOT NULL DEFAULT '',
      self_note TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS capture_sessions (
      session_id TEXT PRIMARY KEY,
      source TEXT NOT NULL DEFAULT '',
      client TEXT NOT NULL DEFAULT '',
      cwd TEXT NOT NULL DEFAULT '',
      started_at TEXT,
      ended_at TEXT,
      last_turn_id TEXT NOT NULL DEFAULT '',
      turn_count INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
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
      work_status TEXT NOT NULL DEFAULT 'done',
      start_time TEXT,
      end_time TEXT,
      duration_minutes INTEGER,
      cwd TEXT NOT NULL DEFAULT '',
      tags TEXT NOT NULL DEFAULT '',
      review_state TEXT NOT NULL DEFAULT 'candidate',
      entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
      jira_key TEXT,
      ai_turn_count INTEGER,
      collaboration_note TEXT,
      confidence REAL NOT NULL DEFAULT 0.5,
      provenance TEXT NOT NULL DEFAULT 'heuristic',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_entries_work_date ON entries(work_date);
    CREATE INDEX IF NOT EXISTS idx_candidates_week ON capture_candidates(work_date, review_state);
  `);
  // Existing databases may predate one or more fields. Add only the fields this runtime needs.
  ensureColumn(db, "entries", "jira_key", "TEXT");
  ensureColumn(db, "entries", "ai_turn_count", "INTEGER");
  ensureColumn(db, "entries", "collaboration_note", "TEXT");
  ensureColumn(db, "capture_candidates", "jira_key", "TEXT");
  ensureColumn(db, "capture_candidates", "ai_turn_count", "INTEGER");
  ensureColumn(db, "capture_candidates", "collaboration_note", "TEXT");
  ensureColumn(db, "capture_candidates", "confidence", "REAL NOT NULL DEFAULT 0.5");
  ensureColumn(db, "capture_candidates", "provenance", "TEXT NOT NULL DEFAULT 'heuristic'");

  // Migrate only the aggregate turn count, then remove the transcript-shaped
  // tables entirely. No prompt, assistant reply, or tool output is retained.
  if (legacySessions) {
    let sessions: Row[] = [];
    try { sessions = dbAll(db, "SELECT session_id, source, client, cwd, started_at, ended_at, created_at, updated_at FROM automation_sessions"); } catch { sessions = []; }
    for (const session of sessions) {
      const turnCount = legacyTurns
        ? Number(dbGet(db, "SELECT COUNT(*) AS count FROM automation_turns WHERE session_id = ? AND state IN ('open', 'captured')", session.session_id)?.count || 0)
        : 0;
      dbRun(db, `INSERT OR IGNORE INTO capture_sessions(session_id, source, client, cwd, started_at, ended_at, turn_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
        short(session.session_id, 160), short(session.source, 80), short(session.client, 80), short(session.cwd, 500), session.started_at || null, session.ended_at || null, turnCount, session.created_at || now(), session.updated_at || session.created_at || now());
    }
  }
  if (legacyTurns) db.exec("DROP TABLE IF EXISTS automation_turns");
  if (legacySessions) db.exec("DROP TABLE IF EXISTS automation_sessions");
  db.exec("PRAGMA user_version = 4");
}

function openDb(dbPath: string): Db {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new DatabaseSync(dbPath);
  initSchema(db);
  DB_PATHS.set(db, dbPath);
  pruneStorage(db, dbPath);
  return db;
}

function storageBytes(dbPath: string): number {
  return [dbPath, `${dbPath}-wal`, `${dbPath}-shm`, `${dbPath}-journal`].reduce((total, file) => {
    try { return total + fs.statSync(file).size; } catch { return total; }
  }, 0);
}

function pruneStorage(db: Db, dbPath: string): void {
  const sessionCutoff = new Date(Date.now() - SESSION_RETENTION_DAYS * 86400000).toISOString();
  const candidateCutoff = new Date(Date.now() - CANDIDATE_RETENTION_DAYS * 86400000).toISOString();
  if (tableExists(db, "capture_sessions")) {
    dbRun(db, "DELETE FROM capture_sessions WHERE updated_at < ?", sessionCutoff);
    dbRun(db, `DELETE FROM capture_sessions WHERE session_id IN (
      SELECT session_id FROM capture_sessions
      ORDER BY updated_at DESC
      LIMIT -1 OFFSET ${MAX_RETAINED_SESSIONS}
    )`);
  }
  if (tableExists(db, "capture_candidates")) {
    dbRun(db, "DELETE FROM capture_candidates WHERE review_state IN ('ignored', 'promoted') AND updated_at < ?", candidateCutoff);
  }
  // VACUUM only when close to the quota; normal hooks should stay cheap.
  if (storageBytes(dbPath) > MAX_DB_BYTES * 0.9) {
    try { db.exec("PRAGMA incremental_vacuum"); db.exec("VACUUM"); } catch { /* keep the write path usable */ }
  }
  // Re-apply the page ceiling after a possible compaction. SQLite refuses to
  // grow beyond this many 4 KiB pages on new writes.
  setPageCeiling(db);
}

function closeDb(db: Db): void {
  const dbPath = DB_PATHS.get(db);
  try { if (dbPath) pruneStorage(db, dbPath); } finally {
    db.close();
    DB_PATHS.delete(db);
  }
}

function isStorageLimitError(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error);
  return /database or disk is full|database full|maximum.*page|SQLITE_FULL/i.test(message);
}

function tagsFor(db: Db, entryId: number): string[] {
  return dbAll(db, "SELECT tag FROM entry_tags WHERE entry_id = ? ORDER BY tag", entryId).map((row) => String(row.tag));
}

function withTags(db: Db, row: Row): Row {
  return { ...row, tags: tagsFor(db, Number(row.id)) };
}

function parseMinutes(value: string | undefined): number | null {
  if (value === undefined) return null;
  const minutes = Number(value);
  if (!Number.isInteger(minutes) || minutes < 0) die(`耗时必须是非负整数分钟：${value}`);
  return minutes;
}

function parseTime(value: string | undefined, date: string): string | null {
  if (!value) return null;
  if (/^\d{2}:\d{2}$/.test(value)) return `${date}T${value}:00`;
  return value;
}

function splitTags(value: string | undefined): string[] {
  return (value || "").split(",").map((tag) => short(tag, 80)).filter(Boolean).slice(0, 20);
}

function addEntry(db: Db, args: string[]): void {
  const content = option(args, "--content", true)!;
  const date = validDate(option(args, "--date"));
  const status = option(args, "--status") || "done";
  if (!STATUSES.has(status)) die(`不支持的状态：${status}`);
  const start = parseTime(option(args, "--start"), date);
  const end = parseTime(option(args, "--end"), date);
  const minutes = parseMinutes(option(args, "--minutes"));
  const timestamp = now();
  dbRun(db, `INSERT INTO entries
    (work_date, content, project, category, status, result, start_time, end_time, duration_minutes, source, jira_key, ai_turn_count, collaboration_note, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  date, short(content, 500), short(option(args, "--project") || "", 200), short(option(args, "--category") || "", 100), status,
  short(option(args, "--result") || "", 1200), start, end, minutes, short(option(args, "--source") || "manual", 80),
  short(option(args, "--jira-key") || "", 80) || null, parseMinutes(option(args, "--ai-turn-count")), short(option(args, "--collaboration-note") || "", 500) || null, timestamp, timestamp);
  const id = Number(dbGet(db, "SELECT last_insert_rowid() AS id")?.id);
  for (const tag of splitTags(option(args, "--tags"))) dbRun(db, "INSERT OR IGNORE INTO entry_tags(entry_id, tag) VALUES (?, ?)", id, tag);
  const result = withTags(db, dbGet(db, "SELECT * FROM entries WHERE id = ?", id)!);
  if (bool(args, "--json")) json(result);
  else console.log(`已记录 #${id}：${result.work_date} ${result.content}`);
}

function listEntries(db: Db, args: string[]): void {
  const clauses: string[] = [];
  const params: any[] = [];
  if (option(args, "--week")) {
    const range = weekBounds(option(args, "--week"));
    clauses.push("work_date BETWEEN ? AND ?"); params.push(range.start, range.end);
  } else if (option(args, "--from-date")) {
    clauses.push("work_date >= ?"); params.push(validDate(option(args, "--from-date")));
    if (option(args, "--to-date")) { clauses.push("work_date <= ?"); params.push(validDate(option(args, "--to-date"))); }
  } else if (option(args, "--to-date")) {
    clauses.push("work_date <= ?"); params.push(validDate(option(args, "--to-date")));
  }
  for (const field of ["project", "category", "status", "jira_key"]) {
    const value = option(args, field === "jira_key" ? "--jira-key" : `--${field}`);
    if (value !== undefined) { clauses.push(`${field} = ?`); params.push(value); }
  }
  const search = option(args, "--search");
  if (search) { clauses.push("(content LIKE ? OR result LIKE ? OR project LIKE ? OR category LIKE ?)"); params.push(`%${search}%`, `%${search}%`, `%${search}%`, `%${search}%`); }
  const where = clauses.length ? `WHERE ${clauses.join(" AND ")}` : "";
  const rows = dbAll(db, `SELECT * FROM entries ${where} ORDER BY work_date, COALESCE(start_time, created_at), id`, ...params).map((row) => withTags(db, row));
  const requiredTags = options(args, "--tag");
  const filtered = requiredTags.length ? rows.filter((row) => requiredTags.every((tag) => row.tags.includes(tag))) : rows;
  if (bool(args, "--json")) json({ count: filtered.length, entries: filtered });
  else for (const row of filtered) console.log(`#${row.id} ${row.work_date} ${row.content}${row.result ? ` — ${row.result}` : ""}`);
}

function updateEntry(db: Db, args: string[]): void {
  const id = Number(args.find((arg) => /^\d+$/.test(arg)));
  if (!id) die("update 需要记录 ID");
  const existing = dbGet(db, "SELECT * FROM entries WHERE id = ?", id);
  if (!existing) die(`记录不存在：${id}`);
  const fields: Record<string, any> = {};
  for (const field of ["content", "project", "category", "status", "result", "jira-key", "collaboration-note"]) {
    const value = option(args, `--${field}`);
    if (value !== undefined) {
      const name = field === "jira-key" ? "jira_key" : field === "collaboration-note" ? "collaboration_note" : field;
      const limit = name === "content" ? 500 : name === "result" ? 1200 : name === "collaboration-note" ? 500 : name === "project" ? 200 : name === "category" ? 100 : 80;
      fields[name] = short(value, limit);
    }
  }
  if (option(args, "--date") !== undefined) fields.work_date = validDate(option(args, "--date"));
  if (option(args, "--minutes") !== undefined) fields.duration_minutes = parseMinutes(option(args, "--minutes"));
  if (option(args, "--ai-turn-count") !== undefined) fields.ai_turn_count = parseMinutes(option(args, "--ai-turn-count"));
  for (const field of ["start", "end"]) if (option(args, `--${field}`) !== undefined) fields[`${field}_time`] = parseTime(option(args, `--${field}`), fields.work_date || existing.work_date);
  if (option(args, "--tags") !== undefined) {
    dbRun(db, "DELETE FROM entry_tags WHERE entry_id = ?", id);
    for (const tag of splitTags(option(args, "--tags"))) dbRun(db, "INSERT OR IGNORE INTO entry_tags(entry_id, tag) VALUES (?, ?)", id, tag);
  }
  if (fields.status && !STATUSES.has(fields.status)) die(`不支持的状态：${fields.status}`);
  const names = Object.keys(fields);
  if (names.length) {
    const sets = names.map((name) => `${name} = ?`).join(", ");
    dbRun(db, `UPDATE entries SET ${sets}, updated_at = ? WHERE id = ?`, ...names.map((name) => fields[name]), now(), id);
  }
  const result = withTags(db, dbGet(db, "SELECT * FROM entries WHERE id = ?", id)!);
  if (bool(args, "--json")) json(result); else console.log(`已更新 #${id}`);
}

function reflect(db: Db, args: string[]): void {
  const week = weekBounds(option(args, "--week")).start;
  const fields: Record<string, any> = {};
  for (const name of ["mood", "headline", "hard-moment", "happy-moment", "self-note"]) {
    const value = option(args, `--${name}`);
    if (value !== undefined) {
      if (name === "mood") {
        const mood = Number(value);
        if (!Number.isFinite(mood) || mood < 0 || mood > 5) die(`心情指数必须在 0 到 5 之间：${value}`);
        fields.mood = mood;
      } else {
        fields[name.replaceAll("-", "_")] = short(value, name === "headline" ? 240 : 1000);
      }
    }
  }
  const existing = dbGet(db, "SELECT * FROM weekly_reflections WHERE week_start = ?", week);
  if (Object.keys(fields).length) {
    const timestamp = now();
    if (existing) {
      const names = Object.keys(fields); dbRun(db, `UPDATE weekly_reflections SET ${names.map((name) => `${name} = ?`).join(", ")}, updated_at = ? WHERE week_start = ?`, ...names.map((name) => fields[name]), timestamp, week);
    } else {
      dbRun(db, `INSERT INTO weekly_reflections(week_start, mood, headline, hard_moment, happy_moment, self_note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`, week, fields.mood ?? null, fields.headline || "", fields.hard_moment || "", fields.happy_moment || "", fields.self_note || "", timestamp, timestamp);
    }
  }
  const result = dbGet(db, "SELECT * FROM weekly_reflections WHERE week_start = ?", week) || { week_start: week };
  if (bool(args, "--json") || !Object.keys(fields).length) json(result); else console.log(`已保存 ${week} 的复盘`);
}

function report(db: Db, args: string[]): void {
  const range = weekBounds(option(args, "--week"));
  const entries = dbAll(db, "SELECT * FROM entries WHERE work_date BETWEEN ? AND ? ORDER BY work_date, COALESCE(start_time, created_at), id", range.start, range.end);
  const reflection = dbGet(db, "SELECT * FROM weekly_reflections WHERE week_start = ?", range.start);
  const mostBackAndForth = [...entries]
    .filter((entry) => Number(entry.ai_turn_count || 0) > 0)
    .sort((a, b) => Number(b.ai_turn_count) - Number(a.ai_turn_count))[0];
  const pending = Number(dbGet(db, "SELECT COUNT(*) AS count FROM capture_candidates WHERE review_state = 'candidate' AND work_date BETWEEN ? AND ?", range.start, range.end)?.count || 0);
  const results = entries.filter((entry) => entry.result).map((entry) => `- ${entry.content}：${entry.result}`);
  const progress = entries.map((entry) => `- ${entry.content}（${entry.status}）`);
  const blocked = entries.filter((entry) => entry.status === "blocked").map((entry) => `- ${entry.content}`);
  const totalMinutes = entries.reduce((sum, entry) => sum + Number(entry.duration_minutes || 0), 0);
  const lines = [
    `# 工作复盘｜${range.start} ~ ${range.end}`, "",
    "## 本周核心成果", ...(results.length ? results : ["- 还没有记录可用成果。"]), "",
    "## 关键事项进展", ...(progress.length ? progress : ["- 还没有正式记录。"]), "",
    "## 问题、阻塞与风险", ...(blocked.length ? blocked : ["- 暂无已记录阻塞。"]), "",
    "## 投入概览", `- 正式记录：${entries.length} 条`, `- 已记录投入：${totalMinutes} 分钟`, "",
    "## 待确认记录", `- 自动采集候选：${pending} 条`, "",
    "## 协作回放",
    mostBackAndForth
      ? `- 本周最费沟通的一次：${mostBackAndForth.content}（${mostBackAndForth.ai_turn_count} 轮往返）${mostBackAndForth.collaboration_note ? `；${mostBackAndForth.collaboration_note}` : ""}`
      : "- 还没有记录 AI 协作轮数。",
    "",
    "## 下周计划", "- 仅展示用户明确记录的计划。",
  ];
  if (reflection?.hard_moment || reflection?.happy_moment || reflection?.self_note) {
    lines.push("", "## 本周复盘");
    if (reflection.hard_moment) lines.push(`- 最难的一刻：${reflection.hard_moment}`);
    if (reflection.happy_moment) lines.push(`- 值得开心：${reflection.happy_moment}`);
    if (reflection.self_note) lines.push(`- 写给自己：${reflection.self_note}`);
  }
  const output = `${lines.join("\n")}\n`;
  const outputPath = option(args, "--output");
  if (outputPath) { fs.mkdirSync(path.dirname(path.resolve(outputPath)), { recursive: true }); fs.writeFileSync(path.resolve(outputPath), output, "utf8"); console.log(path.resolve(outputPath)); }
  else console.log(output);
}

function dataset(db: Db, args: string[]): void {
  const range = weekBounds(option(args, "--week"));
  const entries = dbAll(db, "SELECT * FROM entries WHERE work_date BETWEEN ? AND ? ORDER BY work_date, COALESCE(start_time, created_at), id", range.start, range.end).map((row) => withTags(db, row));
  const reflections = dbAll(db, "SELECT * FROM weekly_reflections WHERE week_start = ?", range.start);
  const pending = dbAll(db, "SELECT * FROM capture_candidates WHERE review_state = 'candidate' AND work_date BETWEEN ? AND ? ORDER BY work_date, id", range.start, range.end);
  const result = { schema_version: 1, week_start: range.start, week_end: range.end, entries, reflections, pending_candidates: pending, coverage: { formal_records: entries.length, pending_candidates: pending.length } };
  const output = option(args, "--output");
  if (output) { fs.mkdirSync(path.dirname(path.resolve(output)), { recursive: true }); fs.writeFileSync(path.resolve(output), JSON.stringify(result, null, 2) + "\n", "utf8"); }
  json(result);
}

function eventKind(event: string): "session_start" | "session_end" | "prompt" | "stop" | "other" {
  const normalized = event.toLowerCase().replace(/[_-]/g, "");
  if (normalized === "sessionstart" || normalized === "sessionstarted") return "session_start";
  if (normalized === "sessionend" || normalized === "sessionended") return "session_end";
  if (normalized === "userpromptsubmit" || normalized === "prompt" || normalized === "turnstart") return "prompt";
  if (["stop", "turnend", "taskcompleted", "completed"].includes(normalized)) return "stop";
  return "other";
}

function hash(value: string): string {
  return crypto.createHash("sha256").update(value).digest("hex").slice(0, 24);
}

function observedTurnCount(db: Db, sessionId: string): number | null {
  if (!sessionId) return null;
  const count = Number(dbGet(db, "SELECT turn_count AS count FROM capture_sessions WHERE session_id = ?", sessionId)?.count || 0);
  return count || null;
}

function hookIngest(db: Db, args: string[]): void {
  let input = "";
  if (option(args, "--event-file")) input = fs.readFileSync(path.resolve(option(args, "--event-file")!), "utf8");
  else input = fs.readFileSync(0, "utf8");
  if (!input.trim()) { if (bool(args, "--hook-output")) console.log("{}"); else json({ action: "ignored", reason: "empty_payload" }); return; }
  let payload: Row;
  try { payload = JSON.parse(input) as Row; } catch { if (bool(args, "--hook-output")) { console.log("{}"); return; } die("hook 输入必须是 JSON 对象"); }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) die("hook 输入必须是 JSON 对象");
  const event = String(payload.hook_event_name || payload.event || payload.type || "");
  const kind = eventKind(event);
  const client = short(payload.client || payload.provider || "codex", 80);
  const sessionId = short(payload.session_id || payload.sessionId || "", 160);
  const turnId = short(payload.turn_id || payload.turnId || "", 160);
  const cwd = short(payload.cwd || payload.workdir || "", 500);
  const timestamp = now();
  try {
  if (kind === "session_start" && sessionId) {
    dbRun(db, `INSERT INTO capture_sessions(session_id, source, client, cwd, started_at, turn_count, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?) ON CONFLICT(session_id) DO UPDATE SET source=excluded.source, client=excluded.client, cwd=excluded.cwd, started_at=COALESCE(capture_sessions.started_at, excluded.started_at), ended_at=NULL, updated_at=excluded.updated_at`, sessionId, event, client, cwd, payload.started_at || timestamp, timestamp, timestamp);
  } else if (kind === "session_end" && sessionId) {
    dbRun(db, `UPDATE capture_sessions SET ended_at = ?, updated_at = ? WHERE session_id = ?`, payload.ended_at || timestamp, timestamp, sessionId);
  } else if (kind === "prompt") {
    const sid = sessionId || `session-${hash(`${client}|${cwd}`)}`;
    const tid = turnId || "";
    dbRun(db, `INSERT INTO capture_sessions(session_id, source, client, cwd, started_at, last_turn_id, turn_count, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
      ON CONFLICT(session_id) DO UPDATE SET
        source=excluded.source,
        client=excluded.client,
        cwd=excluded.cwd,
        ended_at=NULL,
        turn_count=capture_sessions.turn_count + CASE WHEN excluded.last_turn_id <> '' AND excluded.last_turn_id = capture_sessions.last_turn_id THEN 0 ELSE 1 END,
        last_turn_id=CASE WHEN excluded.last_turn_id <> '' THEN excluded.last_turn_id ELSE capture_sessions.last_turn_id END,
        updated_at=excluded.updated_at`, sid, event, client, cwd, payload.started_at || timestamp, tid, timestamp, timestamp);
  } else if (kind === "stop") {
    const title = short(payload.title || payload.content || "", 240);
    // Only accept a short structured summary/result from the hook payload.
    // Raw assistant replies and tool output are intentionally ignored.
    const summary = short(payload.summary || payload.result || "", 1200);
    if (title || summary) {
      const workDate = validDate(String(payload.work_date || payload.date || (payload.started_at || timestamp).slice(0, 10)));
      const captureId = short(payload.capture_id || payload.event_id || payload.eventId || payload.id || `${client}-${hash(`${client}|${sessionId}|${turnId}|${event}|${workDate}|${title}|${summary}`)}`, 200);
      const start = payload.started_at || null;
      const end = payload.ended_at || timestamp;
      const duration = payload.duration_minutes === undefined ? null : parseMinutes(String(payload.duration_minutes));
      const countSessionId = sessionId || `session-${hash(`${client}|${cwd}`)}`;
      const aiTurnCount = payload.ai_turn_count === undefined
        ? observedTurnCount(db, countSessionId)
        : parseMinutes(String(payload.ai_turn_count));
      const collaborationNote = short(payload.collaboration_note || payload.collaborationNote || "", 500) || null;
      const tags = splitTags(Array.isArray(payload.tags) ? payload.tags.join(",") : payload.tags);
      if (!tags.includes("自动采集")) tags.push("自动采集");
      const result = dbRun(db, `INSERT INTO capture_candidates(capture_id, source, client, session_id, turn_id, work_date, title, summary, project, category, work_status, start_time, end_time, duration_minutes, cwd, tags, review_state, jira_key, ai_turn_count, collaboration_note, confidence, provenance, created_at, updated_at) VALUES (?, 'hook', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(capture_id) DO UPDATE SET title=excluded.title, summary=excluded.summary, end_time=excluded.end_time, ai_turn_count=excluded.ai_turn_count, collaboration_note=excluded.collaboration_note, updated_at=excluded.updated_at`, captureId, client, sessionId || null, turnId || null, workDate, title || short(summary, 120) || "自动采集工作回合", summary, short(payload.project || payload.project_name || path.basename(cwd), 200), short(payload.category || "AI协作", 100), STATUSES.has(String(payload.status || payload.work_status)) ? String(payload.status || payload.work_status) : "done", start, end, duration, cwd, tags.join(","), short(payload.jira_key || payload.jiraKey || "", 80) || null, aiTurnCount, collaborationNote, Number(payload.confidence || 0.5), short(payload.provenance || "hook", 80), timestamp, timestamp);
      if (bool(args, "--hook-output")) console.log("{}"); else json({ action: result.changes ? "candidate_created" : "candidate_updated", candidate: dbGet(db, "SELECT * FROM capture_candidates WHERE capture_id = ?", captureId) });
      return;
    }
  }
  if (bool(args, "--hook-output")) console.log("{}"); else json({ action: kind === "other" ? "ignored" : kind });
  } catch (error) {
    if (!isStorageLimitError(error)) throw error;
    if (bool(args, "--hook-output")) console.log("{}");
    else json({ action: "ignored", reason: "storage_budget" });
  }
}

function hookList(db: Db, args: string[]): void {
  const range = option(args, "--week") ? weekBounds(option(args, "--week")) : null;
  const state = option(args, "--state") || "candidate";
  const rows = range
    ? dbAll(db, "SELECT * FROM capture_candidates WHERE review_state = ? AND work_date BETWEEN ? AND ? ORDER BY work_date, id", state, range.start, range.end)
    : dbAll(db, "SELECT * FROM capture_candidates WHERE review_state = ? ORDER BY work_date, id", state);
  if (bool(args, "--json")) json({ count: rows.length, candidates: rows }); else for (const row of rows) console.log(`#${row.id} ${row.work_date} ${row.title} — ${row.summary}`);
}

function promote(db: Db, id: number): Row {
  const candidate = dbGet(db, "SELECT * FROM capture_candidates WHERE id = ?", id);
  if (!candidate) die(`候选不存在：${id}`);
  if (candidate.review_state === "promoted" && candidate.entry_id) return dbGet(db, "SELECT * FROM entries WHERE id = ?", candidate.entry_id)!;
  db.exec("BEGIN");
  try {
    dbRun(db, `INSERT INTO entries(work_date, content, project, category, status, result, start_time, end_time, duration_minutes, source, capture_id, jira_key, ai_turn_count, collaboration_note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'hook', ?, ?, ?, ?, ?, ?)`, candidate.work_date, candidate.title, candidate.project, candidate.category, candidate.work_status, candidate.summary, candidate.start_time, candidate.end_time, candidate.duration_minutes, candidate.capture_id, candidate.jira_key || null, candidate.ai_turn_count || null, candidate.collaboration_note || null, now(), now());
    const entryId = Number(dbGet(db, "SELECT last_insert_rowid() AS id")?.id);
    for (const tag of splitTags(candidate.tags)) dbRun(db, "INSERT OR IGNORE INTO entry_tags(entry_id, tag) VALUES (?, ?)", entryId, tag);
    dbRun(db, "UPDATE capture_candidates SET review_state = 'promoted', entry_id = ?, updated_at = ? WHERE id = ?", entryId, now(), id);
    db.exec("COMMIT");
    return withTags(db, dbGet(db, "SELECT * FROM entries WHERE id = ?", entryId)!);
  } catch (error) { db.exec("ROLLBACK"); throw error; }
}

function hookReview(db: Db, command: string, args: string[]): void {
  if (command === "list") return hookList(db, args);
  const ids = args.filter((arg) => /^\d+$/.test(arg)).flatMap((arg) => arg.split(",")).map(Number);
  if (!ids.length) die(`${command} 需要候选 ID`);
  if (command === "promote" || command === "promote-batch") {
    const entries = ids.map((id) => promote(db, id));
    if (bool(args, "--json")) json(command === "promote" ? entries[0] : { count: entries.length, entries });
    else console.log(`已确认 ${entries.length} 条候选`);
    return;
  }
  if (command === "ignore" || command === "ignore-batch") {
    for (const id of ids) dbRun(db, "UPDATE capture_candidates SET review_state = 'ignored', updated_at = ? WHERE id = ?", now(), id);
    if (bool(args, "--json")) json({ count: ids.length, state: "ignored" }); else console.log(`已忽略 ${ids.length} 条候选`);
    return;
  }
  die(`未知 hook 命令：${command}`);
}

function hookConfig(args: string[]): void {
  const client = option(args, "--client") || "codex";
  if (client !== "codex") die("当前只支持 Codex 客户端");
  const scriptPath = path.resolve(option(args, "--script-path") || process.argv[1] || path.join(ROOT, "dist", "cli.js"));
  const command = `node ${JSON.stringify(scriptPath)} hook ingest --hook-output`;
  const handler = (timeout = 10) => ({ type: "command", command, timeout });
  json({ description: "Weeklylog automatic work capture.", hooks: { SessionStart: [{ hooks: [handler()] }], UserPromptSubmit: [{ hooks: [handler()] }], Stop: [{ hooks: [handler()] }], SessionEnd: [{ hooks: [handler(3)] }] } });
}

function mergeHooks(hooksPath: string, generated: Row): Row {
  let existing: Row = {};
  const existed = fs.existsSync(hooksPath);
  if (existed) {
    try { existing = JSON.parse(fs.readFileSync(hooksPath, "utf8") || "{}"); } catch { die(`无法解析现有 Codex hooks 配置：${hooksPath}`); }
    if (!existing || typeof existing !== "object" || Array.isArray(existing)) die(`现有 Codex hooks 配置必须是 JSON 对象：${hooksPath}`);
  }
  const merged: Row = { ...existing, hooks: { ...(existing.hooks || {}) } };
  for (const [event, entries] of Object.entries(generated.hooks || {})) {
    const current = Array.isArray(merged.hooks[event]) ? [...merged.hooks[event]] : [];
    for (const entry of entries as Row[]) {
      const command = entry.hooks?.[0]?.command;
      if (!current.some((item: Row) => item.hooks?.some((hook: Row) => hook.command === command))) current.push(entry);
    }
    merged.hooks[event] = current;
  }
  if (!merged.description) merged.description = generated.description;
  fs.mkdirSync(path.dirname(hooksPath), { recursive: true });
  fs.writeFileSync(`${hooksPath}.tmp-${process.pid}`, JSON.stringify(merged, null, 2) + "\n", "utf8");
  fs.renameSync(`${hooksPath}.tmp-${process.pid}`, hooksPath);
  return { path: hooksPath, existed, events: Object.keys(generated.hooks || {}) };
}

async function ask(rl: readline.Interface, prompt: string, fallback: string): Promise<string> {
  return (await rl.question(`${prompt} [${fallback}]: `)).trim() || fallback;
}

async function initWizard(args: string[]): Promise<string[]> {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  try {
    console.log("weeklylog 首次初始化向导\n");
    const client = await ask(rl, "AI 客户端（当前支持 Codex）", "codex");
    if (client.toLowerCase() !== "codex") die("当前只支持 Codex 客户端");
    const feature = await ask(rl, "安装功能（1=记录+周度回顾，2=仅记录）", "1");
    if (!["1", "2"].includes(feature)) die("功能选项只能是 1 或 2");
    const hooks = await ask(rl, "配置 Codex 自动记录 hooks？（y/n）", "y");
    const selected = [...args, "--client", "codex"];
    if (feature === "2") selected.push("--skills", "weeklylog");
    if (["n", "no", "否"].includes(hooks.toLowerCase())) selected.push("--skip-hooks");
    return selected;
  } finally { rl.close(); }
}

function copySkill(source: string, target: string, force: boolean): void {
  if (fs.existsSync(target) && !force) return;
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.cpSync(source, target, { recursive: true, force: true });
}

function initCommand(args: string[]): void {
  const client = option(args, "--client");
  if (client && client !== "codex") die("当前只支持 Codex 客户端");
  const dbPath = globalDbPath;
  const db = openDb(dbPath);
  try {
    const jsonOutput = bool(args, "--json");
    const skillsDir = path.resolve(option(args, "--skills-dir") || path.join(os.homedir(), ".codex", "skills"));
    const hooksPath = path.resolve(option(args, "--hooks-path") || path.join(os.homedir(), ".codex", "hooks.json"));
    const selected = option(args, "--skills")?.split(",").map((name) => name.trim()).filter(Boolean) || ["weeklylog", "weekglow"];
    const skipSkills = bool(args, "--skip-skills");
    const skipHooks = bool(args, "--skip-hooks");
    const force = bool(args, "--force");
    const skillTargets: string[] = [];
    if (!skipSkills) {
      for (const name of selected) {
        if (!["weeklylog", "weekglow"].includes(name)) die(`不支持的 Codex skill：${name}`);
        const source = name === "weeklylog" ? path.join(ROOT, "skill") : path.join(ROOT, "skills", "weekglow");
        const target = path.join(skillsDir, name);
        copySkill(source, target, force); skillTargets.push(target);
        if (name === "weeklylog") {
          const runtimeTarget = path.join(target, "scripts", "weeklylog.js");
          if (force || !fs.existsSync(runtimeTarget)) {
            fs.mkdirSync(path.dirname(runtimeTarget), { recursive: true });
            fs.copyFileSync(path.join(ROOT, "dist", "cli.js"), runtimeTarget);
          }
        }
      }
    }
    let hookResult: Row | null = null;
    if (!skipHooks) {
      const hookScript = skipSkills ? path.join(ROOT, "dist", "cli.js") : path.join(skillsDir, "weeklylog", "scripts", "weeklylog.js");
      const command = `node ${JSON.stringify(path.resolve(hookScript))} hook ingest --hook-output`;
      const generated = { description: "Weeklylog automatic work capture.", hooks: Object.fromEntries(EVENTS.map((event) => [event, [{ hooks: [{ type: "command", command, timeout: event === "SessionEnd" ? 3 : 10 }] }]])) };
      hookResult = mergeHooks(hooksPath, generated);
    }
    const result = { database: dbPath, schema_version: 4, storage_budget_bytes: MAX_DB_BYTES, client: client || "codex", skills: skillTargets, hooks: hookResult, next_step: "初始化完成，可以直接使用 weeklylog；cronjob 后续单独配置。" };
    if (jsonOutput) json(result);
    else { console.log(`已初始化数据库：${dbPath}`); for (const target of skillTargets) console.log(`Codex skill 已就绪：${target}`); if (hookResult) console.log(`已合并 Codex hooks：${hooksPath}`); console.log("初始化完成，可以直接使用 weeklylog。"); }
  } finally { closeDb(db); }
}

function writeSvgCards(db: Db, args: string[]): void {
  const range = weekBounds(option(args, "--week"));
  const entries = dbAll(db, "SELECT * FROM entries WHERE work_date BETWEEN ? AND ? ORDER BY work_date, id", range.start, range.end);
  const reflection = dbGet(db, "SELECT * FROM weekly_reflections WHERE week_start = ?", range.start);
  const mostBackAndForth = [...entries]
    .filter((entry) => Number(entry.ai_turn_count || 0) > 0)
    .sort((a, b) => Number(b.ai_turn_count) - Number(a.ai_turn_count))[0];
  const outputDir = path.resolve(option(args, "--output-dir") || path.join(path.dirname(globalDbPath), "weekglow", range.start));
  fs.mkdirSync(outputDir, { recursive: true });
  const cards = [
    ["WEEKLYLOG", `${range.start} — ${range.end}`],
    ["WORK TRACES", `${entries.length} 条正式记录`],
    ["MOST BACK-AND-FORTH", mostBackAndForth ? `${mostBackAndForth.content} · ${mostBackAndForth.ai_turn_count} 轮` : "还没有记录"],
    ["HARD MOMENT", reflection?.hard_moment || entries.find((entry) => entry.status === "blocked")?.content || "还没有记录"],
    ["SMALL WIN", reflection?.happy_moment || entries.find((entry) => entry.result)?.result || "还没有记录"],
    ["SIDE B", reflection?.self_note || "给自己的话还没有记录"],
  ];
  const paths: string[] = [];
  cards.forEach(([title, body], index) => {
    const safeBody = String(body).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1500" viewBox="0 0 1200 1500"><defs><linearGradient id="bg" x1="0" x2="1" y1="0" y2="1"><stop stop-color="#09090b"/><stop offset=".65" stop-color="#24080f"/><stop offset="1" stop-color="#7f1d1d"/></linearGradient></defs><rect width="1200" height="1500" fill="url(#bg)"/><circle cx="900" cy="430" r="260" fill="none" stroke="#ef4444" stroke-opacity=".3" stroke-width="2"/><circle cx="900" cy="430" r="180" fill="none" stroke="#fb7185" stroke-opacity=".25" stroke-width="2"/><text x="90" y="150" fill="#fca5a5" font-family="Arial" font-size="30" letter-spacing="8">${title}</text><text x="90" y="330" fill="#fff7ed" font-family="Arial, sans-serif" font-size="66" font-weight="700">${safeBody}</text><text x="90" y="1400" fill="#fecaca" font-family="Arial" font-size="24">weeklylog · ${index + 1}/${cards.length}</text></svg>`;
    const target = path.join(outputDir, `card-${String(index + 1).padStart(2, "0")}.svg`); fs.writeFileSync(target, svg, "utf8"); paths.push(target);
  });
  if (bool(args, "--json")) json({ output_dir: outputDir, files: paths }); else { console.log(`已生成 ${paths.length} 张周度卡片：${outputDir}`); for (const file of paths) console.log(file); }
}

function help(): void {
  console.log(`weeklylog-cli ${VERSION}\n\nUsage:\n  npx weeklylog-cli <command> [options]\n\nCommands:\n  init                         一次性初始化 Codex、skill 和 hooks\n  add --content TEXT           记录一条正式工作事项\n  list [--week DATE]           查询正式工作记录\n  update ID [options]          修改工作记录\n  reflect [options]            保存或查看每周复盘\n  report [--week DATE]         生成 Markdown 周报\n  dataset --week DATE          导出周报/视觉使用的数据集\n  hook ingest                  接收 Codex hook 事件\n  hook list/promote/ignore     审核自动采集候选\n  weekglow --week DATE         生成周度视觉卡片\n  doctor                       检查本地运行环境\n\ninit 可选参数：--client codex --skills-dir PATH --hooks-path PATH --skills weeklylog,weekglow --skip-skills --skip-hooks --force`);
}

let globalDbPath = process.env.WEEKLYLOG_DB || DEFAULT_DB;

async function main(): Promise<void> {
  const raw = process.argv.slice(2);
  if (!raw.length || raw[0] === "--help" || raw[0] === "-h") return help();
  if (raw[0] === "--version" || raw[0] === "-v") return console.log(VERSION);
  if (raw[0] === "--db") { globalDbPath = path.resolve(raw[1] || die("--db 需要路径")); raw.splice(0, 2); }
  const command = raw.shift()!;
  if (command === "init" && !option(raw, "--client") && !bool(raw, "--json") && process.stdin.isTTY && process.stdout.isTTY) {
    raw.push(...await initWizard([]));
  }
  if (command === "init") return initCommand(raw);
  if (command === "path") return console.log(path.resolve(globalDbPath));
  if (command === "doctor") {
    const dbPath = path.resolve(globalDbPath);
    return console.log(`node: ${process.version}\ndatabase: ${dbPath}\nstorage: ${storageBytes(dbPath)} / ${MAX_DB_BYTES} bytes`);
  }
  if (command === "hook" && raw[0] === "config") return hookConfig(raw.slice(1));
  const db = openDb(path.resolve(globalDbPath));
  try {
    if (command === "add") addEntry(db, raw);
    else if (command === "list") listEntries(db, raw);
    else if (command === "update") updateEntry(db, raw);
    else if (command === "reflect") reflect(db, raw);
    else if (command === "report") report(db, raw);
    else if (command === "dataset") dataset(db, raw);
    else if (command === "weekglow") writeSvgCards(db, raw);
    else if (command === "hook" && raw[0] === "ingest") hookIngest(db, raw.slice(1));
    else if (command === "hook" && raw[0]) hookReview(db, raw[0], raw.slice(1));
    else die(`未知命令：${command}`);
  } finally { closeDb(db); }
}

main().catch((error: unknown) => { console.error(`weeklylog: ${error instanceof Error ? error.message : String(error)}`); process.exitCode = 1; });
