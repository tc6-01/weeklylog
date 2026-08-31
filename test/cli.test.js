const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { test } = require("node:test");

const packageRoot = path.resolve(__dirname, "..");
const cliPath = path.join(packageRoot, "bin", "weeklylog.js");

function temporaryDatabase() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "weeklylog-test-"));
  return {
    directory,
    database: path.join(directory, "worklog.sqlite3"),
  };
}

function runCli(database, args, input = "") {
  const result = spawnSync(process.execPath, [cliPath, "--db", database, ...args], {
    cwd: packageRoot,
    encoding: "utf8",
    input,
  });
  if (result.error) throw result.error;
  return result;
}

function createLegacyDatabase(database) {
  const legacySchema = String.raw`
    CREATE TABLE entries (
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
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE entry_tags (
      entry_id INTEGER NOT NULL,
      tag TEXT NOT NULL,
      PRIMARY KEY (entry_id, tag)
    );
    CREATE TABLE weekly_reflections (
      week_start TEXT PRIMARY KEY,
      mood REAL,
      headline TEXT NOT NULL DEFAULT '',
      hard_moment TEXT NOT NULL DEFAULT '',
      happy_moment TEXT NOT NULL DEFAULT '',
      self_note TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE capture_candidates (
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
      entry_id INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO entries(work_date, content, project, category, status, result, start_time, end_time, duration_minutes, source, capture_id, created_at, updated_at)
      VALUES ('2026-08-24', '修复旧版导入', 'weeklylog', '开发', 'done', '兼容旧数据', '2026-08-24T09:00:00+08:00', '2026-08-24T10:00:00+08:00', 60, 'manual', NULL, '2026-08-24T09:00:00+08:00', '2026-08-24T10:00:00+08:00');
    INSERT INTO entry_tags(entry_id, tag) VALUES (1, '迁移');
    INSERT INTO capture_candidates(capture_id, source, client, session_id, turn_id, work_date, title, summary, project, category, work_status, start_time, end_time, duration_minutes, cwd, tags, review_state, created_at, updated_at)
      VALUES ('legacy-capture-001', 'hook', 'legacy-client', 'legacy-session', 'legacy-turn', '2026-08-24', '保留旧候选', '旧版候选仍可审核', 'weeklylog', 'AI协作', 'in-progress', '2026-08-24T14:00:00+08:00', '2026-08-24T14:45:00+08:00', 45, '/tmp/weeklylog', '旧版', 'candidate', '2026-08-24T14:00:00+08:00', '2026-08-24T14:45:00+08:00');
    INSERT INTO weekly_reflections(week_start, mood, headline, hard_moment, happy_moment, self_note, created_at, updated_at)
      VALUES ('2026-08-24', 4.5, '守住数据', '迁移前很紧张', '旧记录保住了', '继续小步前进', '2026-08-24T18:00:00+08:00', '2026-08-24T18:00:00+08:00');
  `;
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.executescript(sys.argv[2])
connection.commit()
connection.close()
`, database, legacySchema], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

function createBrokenLegacyDatabase(database) {
  const schema = String.raw`
    CREATE TABLE entries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      work_date TEXT NOT NULL,
      content TEXT NOT NULL,
      project TEXT NOT NULL DEFAULT '',
      category TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'done',
      result TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE VIEW entry_tags AS SELECT 1 AS entry_id, '' AS tag;
  `;
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.executescript(sys.argv[2])
connection.commit()
connection.close()
`, database, schema], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

function createTranscriptDatabase(database) {
  const schema = String.raw`
    CREATE TABLE entries (
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
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE capture_candidates (
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
      entry_id INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    INSERT INTO entries(work_date, content, project, category, status, result, source, capture_id, created_at, updated_at)
      VALUES ('2026-08-31', '已确认的自动采集工作记录', 'weeklylog', 'AI协作', 'done', '已完成并验证', 'hook', 'legacy-capture-entry', '2026-08-31T09:00:00+08:00', '2026-08-31T09:00:00+08:00');
    INSERT INTO capture_candidates(capture_id, source, client, session_id, turn_id, work_date, title, summary, project, created_at, updated_at)
      VALUES ('legacy-capture-candidate', 'hook', 'legacy-client', 's1', 't1', '2026-08-31', '待审核候选标题', '待审核候选摘要', 'weeklylog', '2026-08-31T09:00:00+08:00', '2026-08-31T09:00:00+08:00');
    CREATE TABLE automation_sessions (
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
    CREATE TABLE automation_turns (
      session_id TEXT NOT NULL,
      turn_id TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT '',
      prompt TEXT NOT NULL DEFAULT '',
      started_at TEXT NOT NULL,
      ended_at TEXT,
      assistant_message TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL DEFAULT 'open',
      updated_at TEXT NOT NULL,
      PRIMARY KEY (session_id, turn_id)
    );
    INSERT INTO automation_sessions(session_id, last_prompt, created_at, updated_at)
      VALUES ('s1', 'SECRET_FULL_SESSION_PROMPT', '2026-08-31T09:00:00+08:00', '2026-08-31T09:00:00+08:00');
    INSERT INTO automation_turns(session_id, turn_id, prompt, started_at, assistant_message, updated_at)
      VALUES ('s1', 't1', 'SECRET_FULL_TURN_PROMPT', '2026-08-31T09:00:00+08:00', 'SECRET_FULL_TURN_REPLY', '2026-08-31T09:01:00+08:00');
  `;
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.executescript(sys.argv[2])
connection.commit()
connection.close()
`, database, schema], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

function createLegacyMigrationBackup(database) {
  const backup = `${database}.pre-migration-20260831T090000+0800.sqlite3`;
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.executescript('''
CREATE TABLE automation_sessions (
  session_id TEXT PRIMARY KEY,
  last_prompt TEXT NOT NULL DEFAULT ''
);
INSERT INTO automation_sessions(session_id, last_prompt)
VALUES ('old-session', 'SECRET_OLD_BACKUP_PROMPT');
''')
connection.commit()
connection.close()
`, backup], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return backup;
}

function setUserVersion(database, version) {
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute(f'PRAGMA user_version = {int(sys.argv[2])}')
connection.commit()
connection.close()
`, database, String(version)], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

function setLegacyTranscript(database) {
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("INSERT OR IGNORE INTO automation_sessions(session_id, created_at, updated_at) VALUES ('v2-session', '2026-08-31T09:00:00+08:00', '2026-08-31T09:00:00+08:00')")
connection.execute("INSERT OR IGNORE INTO automation_turns(session_id, turn_id, started_at, updated_at) VALUES ('v2-session', 'v2-turn', '2026-08-31T09:00:00+08:00', '2026-08-31T09:00:00+08:00')")
connection.execute("UPDATE automation_sessions SET last_prompt = 'SECRET_V2_LIVE_PROMPT'")
connection.execute("UPDATE automation_turns SET prompt = 'SECRET_V2_LIVE_TURN', assistant_message = 'SECRET_V2_LIVE_REPLY'")
connection.commit()
connection.close()
`, database], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

function readPragma(database, pragma) {
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
print(connection.execute(sys.argv[2]).fetchone()[0])
connection.close()
`, database, pragma], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

function databaseArtifacts(database) {
  const directory = path.dirname(database);
  const prefix = path.basename(database);
  return fs.readdirSync(directory)
    .filter((name) => name === prefix || name.startsWith(`${prefix}.`))
    .map((name) => fs.readFileSync(path.join(directory, name)));
}

test("fresh init exposes a versioned schema through the CLI", () => {
  const fixture = temporaryDatabase();
  const result = runCli(fixture.database, ["init", "--json"]);

  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    database: fs.realpathSync(fixture.database),
    schema_version: 3,
    migration_backup: null,
  });
  assert.equal(result.stderr, "");
});

test("legacy data is backed up once and remains available after migration", () => {
  const fixture = temporaryDatabase();
  createLegacyDatabase(fixture.database);

  const first = runCli(fixture.database, ["init", "--json"]);
  assert.equal(first.status, 0, first.stderr);
  const firstStatus = JSON.parse(first.stdout);
  assert.equal(firstStatus.schema_version, 3);
  assert.match(firstStatus.migration_backup, /worklog\.sqlite3\.pre-migration-/);
  assert.match(first.stderr, /迁移备份/);
  const backups = fs.readdirSync(fixture.directory).filter((name) =>
    name.startsWith("worklog.sqlite3.pre-migration-")
  );
  assert.equal(backups.length, 1);
  assert.ok(fs.statSync(path.join(fixture.directory, backups[0])).size > 0);

  const list = runCli(fixture.database, ["list", "--week", "2026-08-24", "--json"]);
  assert.equal(list.status, 0, list.stderr);
  const listed = JSON.parse(list.stdout);
  assert.equal(listed.count, 1);
  assert.equal(listed.entries[0].content, "修复旧版导入");
  assert.deepEqual(listed.entries[0].tags, ["迁移"]);
  assert.equal(listed.entries[0].duration_minutes, 60);

  const candidates = runCli(fixture.database, [
    "hook",
    "list",
    "--week",
    "2026-08-24",
    "--json",
  ]);
  assert.equal(candidates.status, 0, candidates.stderr);
  assert.equal(JSON.parse(candidates.stdout).count, 1);
  assert.equal(JSON.parse(candidates.stdout).candidates[0].duration_minutes, 45);

  const second = runCli(fixture.database, ["init", "--json"]);
  assert.equal(second.status, 0, second.stderr);
  assert.deepEqual(JSON.parse(second.stdout), {
    database: fs.realpathSync(fixture.database),
    schema_version: 3,
    migration_backup: null,
  });
  assert.equal(second.stderr, "");
  assert.equal(
    fs.readdirSync(fixture.directory).filter((name) =>
      name.startsWith("worklog.sqlite3.pre-migration-")
    ).length,
    1
  );
});

test("failed migrations roll back all schema changes", () => {
  const fixture = temporaryDatabase();
  createBrokenLegacyDatabase(fixture.database);

  const result = runCli(fixture.database, ["init", "--json"]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /index|view/i);
  assert.equal(readPragma(fixture.database, "PRAGMA user_version"), "0");
  assert.equal(readPragma(fixture.database, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_entries_work_date'"), "0");
  assert.equal(readPragma(fixture.database, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'weekly_reflections'"), "0");
});

test("migration redacts transcript fields in the live database and backup", () => {
  const fixture = temporaryDatabase();
  const oldBackup = createLegacyMigrationBackup(fixture.database);
  createTranscriptDatabase(fixture.database);
  setUserVersion(fixture.database, 1);

  const result = runCli(fixture.database, ["init", "--json"]);
  assert.equal(result.status, 0, result.stderr);
  const status = JSON.parse(result.stdout);
  assert.equal(status.schema_version, 3);
  assert.ok(status.migration_backup);

  assert.equal(readPragma(fixture.database, "SELECT last_prompt FROM automation_sessions WHERE session_id = 's1'"), "");
  assert.equal(readPragma(fixture.database, "SELECT prompt FROM automation_turns WHERE session_id = 's1'"), "");
  assert.equal(readPragma(fixture.database, "SELECT assistant_message FROM automation_turns WHERE session_id = 's1'"), "");
  assert.equal(readPragma(status.migration_backup, "SELECT last_prompt FROM automation_sessions WHERE session_id = 's1'"), "");
  assert.equal(readPragma(status.migration_backup, "SELECT prompt FROM automation_turns WHERE session_id = 's1'"), "");
  assert.equal(readPragma(status.migration_backup, "SELECT assistant_message FROM automation_turns WHERE session_id = 's1'"), "");
  assert.equal(readPragma(oldBackup, "SELECT last_prompt FROM automation_sessions WHERE session_id = 'old-session'"), "");
  assert.equal(readPragma(fixture.database, "SELECT title FROM capture_candidates WHERE capture_id = 'legacy-capture-candidate'"), "待审核候选标题");
  assert.equal(readPragma(fixture.database, "SELECT summary FROM capture_candidates WHERE capture_id = 'legacy-capture-candidate'"), "待审核候选摘要");
  assert.equal(readPragma(fixture.database, "SELECT content FROM entries WHERE capture_id = 'legacy-capture-entry'"), "已确认的自动采集工作记录");
  assert.equal(readPragma(fixture.database, "SELECT result FROM entries WHERE capture_id = 'legacy-capture-entry'"), "已完成并验证");
  const forbidden = [
    "SECRET_FULL_SESSION_PROMPT",
    "SECRET_FULL_TURN_PROMPT",
    "SECRET_FULL_TURN_REPLY",
    "SECRET_OLD_BACKUP_PROMPT",
  ];
  for (const artifact of databaseArtifacts(fixture.database).concat(databaseArtifacts(status.migration_backup))) {
    for (const value of forbidden) {
      assert.equal(artifact.includes(Buffer.from(value)), false, `${value} leaked into a database artifact`);
    }
  }
});

test("newer databases are rejected without changing their journal mode", () => {
  const fixture = temporaryDatabase();
  const result = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode = DELETE')
connection.execute('PRAGMA user_version = 99')
connection.commit()
connection.close()
`, fixture.database], { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);

  const cli = runCli(fixture.database, ["list", "--week", "2026-08-31", "--json"]);
  assert.notEqual(cli.status, 0);
  assert.match(cli.stderr, /高于当前 CLI 支持/);
  assert.equal(readPragma(fixture.database, "PRAGMA journal_mode"), "delete");
});

test("opening a current database also scrubs stale migration backups", () => {
  const fixture = temporaryDatabase();
  const staleBackup = createLegacyMigrationBackup(fixture.database);
  const init = runCli(fixture.database, ["init", "--json"]);
  assert.equal(init.status, 0, init.stderr);
  assert.equal(readPragma(staleBackup, "SELECT last_prompt FROM automation_sessions WHERE session_id = 'old-session'"), "");
  const rewrite = spawnSync("python3", ["-c", String.raw`
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("UPDATE automation_sessions SET last_prompt = 'SECRET_STALE_V2_BACKUP'")
connection.commit()
connection.close()
  `, staleBackup], { encoding: "utf8" });
  assert.equal(rewrite.status, 0, rewrite.stderr);
  setUserVersion(fixture.database, 3);
  setLegacyTranscript(fixture.database);
  const reopened = runCli(fixture.database, ["init", "--json"]);
  assert.equal(reopened.status, 0, reopened.stderr);
  assert.equal(readPragma(staleBackup, "SELECT last_prompt FROM automation_sessions WHERE session_id = 'old-session'"), "");
  assert.equal(readPragma(fixture.database, "SELECT last_prompt FROM automation_sessions"), "");
  assert.equal(readPragma(fixture.database, "SELECT prompt FROM automation_turns"), "");
  assert.equal(readPragma(fixture.database, "SELECT assistant_message FROM automation_turns"), "");
  for (const artifact of databaseArtifacts(staleBackup)) {
    assert.equal(artifact.includes(Buffer.from("SECRET_STALE_V2_BACKUP")), false);
  }
});

test("existing CLI commands remain usable through the package entry point", () => {
  const fixture = temporaryDatabase();
  const add = runCli(fixture.database, [
    "add",
    "--date",
    "2026-08-31",
    "--content",
    "完成登录页重构",
    "--minutes",
    "90",
    "--json",
  ]);
  assert.equal(add.status, 0, add.stderr);
  const added = JSON.parse(add.stdout);
  assert.equal(added.content, "完成登录页重构");
  assert.equal(added.human_duration_minutes, 90);

  const taggedAdd = runCli(fixture.database, [
    "add",
    "--date",
    "2026-08-31",
    "--content",
    "保留导出标签",
    "--minutes",
    "10",
    "--tags",
    "导出,验证",
    "--json",
  ]);
  assert.equal(taggedAdd.status, 0, taggedAdd.stderr);
  const exportedInline = runCli(fixture.database, ["export"]);
  assert.equal(exportedInline.status, 0, exportedInline.stderr);
  const exportedInlinePayload = JSON.parse(exportedInline.stdout);
  assert.deepEqual(
    exportedInlinePayload.entries.find((entry) => entry.content === "保留导出标签").tags,
    ["导出", "验证"]
  );

  const report = runCli(fixture.database, ["report", "--week", "2026-08-31"]);
  assert.equal(report.status, 0, report.stderr);
  assert.match(report.stdout, /完成登录页重构/);

  const reflection = runCli(fixture.database, [
    "reflect",
    "--week",
    "2026-08-31",
    "--mood",
    "4.2",
    "--happy-moment",
    "顺利上线",
  ]);
  assert.equal(reflection.status, 0, reflection.stderr);
  assert.equal(JSON.parse(reflection.stdout).mood, 4.2);

  const dataset = runCli(fixture.database, [
    "dataset",
    "--week",
    "2026-08-31",
    "--json",
  ]);
  assert.equal(dataset.status, 0, dataset.stderr);
  assert.equal(JSON.parse(dataset.stdout).entries.length, 2);

  const hook = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "task_completed",
      id: "test-session-001-turn-001",
      client: "test-client",
      session_id: "test-session-001",
      turn_id: "turn-001",
      title: "验证 hook 候选",
      summary: "完成一次自动采集回归",
      project: "weeklylog",
      started_at: "2026-08-31T10:00:00+08:00",
      ended_at: "2026-08-31T10:15:00+08:00",
    })
  );
  assert.equal(hook.status, 0, hook.stderr);
  assert.equal(JSON.parse(hook.stdout).action, "candidate_created");

  const candidates = runCli(fixture.database, [
    "hook",
    "list",
    "--week",
    "2026-08-31",
    "--json",
  ]);
  assert.equal(candidates.status, 0, candidates.stderr);
  assert.equal(JSON.parse(candidates.stdout).count, 1);
});

test("automatic capture does not persist raw prompt text in automation state", () => {
  const fixture = temporaryDatabase();
  const missingTime = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "prompt",
      session_id: "missing-time-session",
      prompt: "没有时间字段也应安全记录",
    })
  );
  assert.equal(missingTime.status, 0, missingTime.stderr);
  assert.equal(JSON.parse(missingTime.stdout).action, "turn_started");

  const prompt = "用户完整提示词：" + "非常详细的上下文。".repeat(200);
  const hook = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "prompt",
      id: "privacy-session-001-turn-001",
      client: "test-client",
      session_id: "privacy-session-001",
      turn_id: "turn-001",
      prompt,
      started_at: "2026-08-31T10:00:00+08:00",
    })
  );
  assert.equal(hook.status, 0, hook.stderr);
  assert.equal(readPragma(fixture.database, "SELECT length(last_prompt) FROM automation_sessions"), "0");
  assert.equal(readPragma(fixture.database, "SELECT length(prompt) FROM automation_turns"), "0");

  const completed = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "task_completed",
      id: "privacy-session-001-turn-001-complete",
      client: "test-client",
      session_id: "privacy-session-001",
      turn_id: "turn-001",
      title: "结构化工作标题",
      summary: "结构化工作摘要",
      started_at: "2026-08-31T10:00:00+08:00",
      ended_at: "2026-08-31T10:15:00+08:00",
    })
  );
  assert.equal(completed.status, 0, completed.stderr);
  assert.equal(readPragma(fixture.database, "SELECT length(assistant_message) FROM automation_turns"), "0");

  const legacyStop = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "Stop",
      id: "legacy-stop-001",
      client: "legacy-client",
      session_id: "legacy-session-001",
      turn_id: "legacy-turn-001",
      last_assistant_message: "旧客户端未提供结构化字段，但这段短摘要仍应生成候选。",
      started_at: "2026-08-31T11:00:00+08:00",
      ended_at: "2026-08-31T11:10:00+08:00",
    })
  );
  assert.equal(legacyStop.status, 0, legacyStop.stderr);
  const legacyPayload = JSON.parse(legacyStop.stdout);
  assert.equal(legacyPayload.action, "candidate_created");
  assert.match(legacyPayload.candidate.summary, /旧客户端未提供结构化字段/);
});

test("structured captures aggregate evidence and expose transactional outbox events", () => {
  const fixture = temporaryDatabase();
  const first = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "task_completed",
      id: "aggregate-001",
      client: "test-client",
      session_id: "aggregate-session",
      turn_id: "turn-001",
      task_key: "TASK-42",
      title: "修复登录超时",
      summary: "定位连接池问题",
      project: "accounts",
      started_at: "2026-08-31T10:00:00+08:00",
      ended_at: "2026-08-31T10:30:00+08:00",
    })
  );
  assert.equal(first.status, 0, first.stderr);
  const firstPayload = JSON.parse(first.stdout);
  assert.equal(firstPayload.action, "candidate_created");
  const second = runCli(
    fixture.database,
    ["hook", "ingest", "--json"],
    JSON.stringify({
      event: "task_completed",
      id: "aggregate-002",
      client: "test-client",
      session_id: "aggregate-session",
      turn_id: "turn-002",
      task_key: "TASK-42",
      title: "修复登录超时",
      summary: "完成回归验证",
      project: "accounts",
      started_at: "2026-08-31T11:00:00+08:00",
      ended_at: "2026-08-31T11:20:00+08:00",
    })
  );
  assert.equal(second.status, 0, second.stderr);
  const secondPayload = JSON.parse(second.stdout);
  assert.equal(secondPayload.action, "evidence_associated");
  assert.equal(secondPayload.candidate.evidence_count, 2);

  const listed = runCli(fixture.database, ["hook", "list", "--week", "2026-08-31", "--json"]);
  assert.equal(JSON.parse(listed.stdout).count, 1);
  const outbox = runCli(fixture.database, ["outbox", "list", "--json"]);
  const events = JSON.parse(outbox.stdout).events;
  assert.equal(events.length, 2);
  assert.deepEqual(events.map((event) => event.delivery_state), ["pending", "pending"]);
  const candidateEventPayload = JSON.parse(events[0].payload_json);
  assert.equal(candidateEventPayload.title, "修复登录超时");
  assert.equal(candidateEventPayload.project, "accounts");
  assert.equal(candidateEventPayload.status, "done");

  const claimed = runCli(fixture.database, ["outbox", "claim", "--lease-minutes", "5", "--json"]);
  assert.equal(claimed.status, 0, claimed.stderr);
  const claimedEvent = JSON.parse(claimed.stdout).event;
  assert.equal(claimedEvent.delivery_state, "claimed");
  assert.match(claimedEvent.claim_deadline, /T/);
  assert.ok(claimedEvent.claim_token);
  const acked = runCli(fixture.database, ["outbox", "ack", claimedEvent.event_id, "--claim-token", claimedEvent.claim_token, "--json"]);
  assert.equal(acked.status, 0, acked.stderr);
  assert.equal(JSON.parse(acked.stdout).delivery_state, "delivered");

  const promoted = runCli(fixture.database, ["hook", "promote-batch", String(firstPayload.candidate.id), "--json"]);
  assert.equal(promoted.status, 0, promoted.stderr);
  assert.equal(JSON.parse(promoted.stdout).count, 1);
  const recordEvents = JSON.parse(runCli(fixture.database, ["outbox", "list", "--json"]).stdout).events;
  const confirmedPayload = JSON.parse(recordEvents.find((event) => event.event_type === "record.confirmed").payload_json);
  assert.equal(confirmedPayload.ai_duration_minutes, 50);
  assert.deepEqual(confirmedPayload.evidence_ids, ["aggregate-001", "aggregate-002"]);
  const records = runCli(fixture.database, ["list", "--week", "2026-08-31", "--jira-key", "NOPE", "--json"]);
  assert.equal(JSON.parse(records.stdout).count, 0);
});

test("candidate merge and split preserve every attached evidence", () => {
  const fixture = temporaryDatabase();
  for (const [id, title] of [["merge-001", "独立事项 A"], ["merge-002", "独立事项 B"]]) {
    const result = runCli(
      fixture.database,
      ["hook", "ingest", "--json"],
      JSON.stringify({
        event: "task_completed",
        id,
        client: "test-client",
        session_id: id,
        turn_id: id,
        title,
        summary: `摘要 ${id}`,
        project: "merge-project",
        started_at: "2026-08-31T10:00:00+08:00",
        ended_at: "2026-08-31T10:10:00+08:00",
      })
    );
    assert.equal(result.status, 0, result.stderr);
  }
  const merged = runCli(fixture.database, ["hook", "merge", "1", "2", "--json"]);
  assert.equal(merged.status, 0, merged.stderr);
  assert.equal(JSON.parse(merged.stdout).evidence_count, 2);
  const exported = runCli(fixture.database, ["export"], "");
  assert.equal(exported.status, 0, exported.stderr);
  const exportedPayload = JSON.parse(exported.stdout);
  const evidenceIds = exportedPayload.evidence.map((item) => item.evidence_id);
  const parts = JSON.stringify([
    { title: "拆分事项 A", summary: "A", evidence_ids: [evidenceIds[0]] },
    { title: "拆分事项 B", summary: "B", evidence_ids: [evidenceIds[1]] },
  ]);
  const split = runCli(fixture.database, ["hook", "split", "1", "--parts", parts, "--json"]);
  assert.equal(split.status, 0, split.stderr);
  assert.equal(JSON.parse(split.stdout).candidates.length, 2);
});

test("Git evidence, retention cleanup, SQLite backup, and JSON round-trip are local and idempotent", () => {
  const fixture = temporaryDatabase();
  const repo = path.join(fixture.directory, "repo");
  fs.mkdirSync(repo);
  assert.equal(spawnSync("git", ["-C", repo, "init", "-q"]).status, 0);
  assert.equal(spawnSync("git", ["-C", repo, "config", "user.email", "test@example.com"]).status, 0);
  assert.equal(spawnSync("git", ["-C", repo, "config", "user.name", "Test User"]).status, 0);
  fs.writeFileSync(path.join(repo, "README.md"), "test\n");
  assert.equal(spawnSync("git", ["-C", repo, "add", "README.md"]).status, 0);
  assert.equal(spawnSync("git", ["-C", repo, "commit", "-qm", "完成 Git 证据测试"]).status, 0);

  const gitCapture = runCli(fixture.database, ["evidence", "git", "--repo", repo, "--json"]);
  assert.equal(gitCapture.status, 0, gitCapture.stderr);
  assert.equal(JSON.parse(gitCapture.stdout).count, 1);
  const backupPath = path.join(fixture.directory, "manual-backup.sqlite3");
  const backup = runCli(fixture.database, ["backup", "--output", backupPath, "--json"]);
  assert.equal(backup.status, 0, backup.stderr);
  assert.ok(fs.existsSync(backupPath));

  const exportPath = path.join(fixture.directory, "weeklylog.json");
  const exported = runCli(fixture.database, ["export", "--output", exportPath, "--json"]);
  assert.equal(exported.status, 0, exported.stderr);
  const importedFixture = temporaryDatabase();
  const imported = runCli(importedFixture.database, ["import", "--input", exportPath, "--json"]);
  assert.equal(imported.status, 0, imported.stderr);
  const repeated = runCli(importedFixture.database, ["import", "--input", exportPath, "--json"]);
  assert.equal(repeated.status, 0, repeated.stderr);
  const importedCandidates = runCli(importedFixture.database, ["hook", "list", "--week", "2026-08-31", "--json"]);
  assert.equal(JSON.parse(importedCandidates.stdout).count, 1);

  const dryRun = runCli(fixture.database, ["cleanup", "--as-of", "2026-12-01", "--json"]);
  assert.equal(dryRun.status, 0, dryRun.stderr);
  assert.equal(JSON.parse(dryRun.stdout).executed, false);
});

test("import rejects malformed collections with machine-readable JSON", () => {
  const fixture = temporaryDatabase();
  const input = path.join(fixture.directory, "invalid-export.json");
  fs.writeFileSync(input, JSON.stringify({
    export_schema_version: 1,
    weeklylog_schema_version: 3,
    entries: "not-an-array",
  }));
  const result = runCli(fixture.database, ["import", "--input", input, "--json"]);
  assert.notEqual(result.status, 0);
  assert.equal(JSON.parse(result.stdout).error.code, "invalid_collection");
});
