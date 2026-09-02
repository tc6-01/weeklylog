const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { DatabaseSync } = require("node:sqlite");
const { test } = require("node:test");

const packageRoot = path.resolve(__dirname, "..");
const cliPath = path.join(packageRoot, "dist", "cli.js");

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "weeklylog-ts-test-"));
  return { directory, database: path.join(directory, "worklog.sqlite3") };
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

test("init configures Codex skills and hooks idempotently", () => {
  const item = fixture();
  const skillsDir = path.join(item.directory, "skills");
  const hooksPath = path.join(item.directory, "hooks.json");
  const first = runCli(item.database, [
    "init", "--client", "codex", "--skills-dir", skillsDir,
    "--hooks-path", hooksPath, "--json",
  ]);
  assert.equal(first.status, 0, first.stderr);
  const payload = JSON.parse(first.stdout);
  assert.equal(payload.schema_version, 4);
  assert.equal(payload.storage_budget_bytes, 10 * 1024 * 1024);
  assert.deepEqual(payload.skills.map((value) => path.basename(value)).sort(), ["weekglow", "weeklylog"]);
  assert.deepEqual(payload.hooks.events, ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]);
  assert.ok(fs.existsSync(path.join(skillsDir, "weeklylog", "SKILL.md")));
  assert.ok(fs.existsSync(path.join(skillsDir, "weeklylog", "scripts", "weeklylog.js")));
  assert.ok(fs.existsSync(path.join(skillsDir, "weekglow", "SKILL.md")));
  const hooks = JSON.parse(fs.readFileSync(hooksPath, "utf8"));
  assert.ok(hooks.hooks.Stop[0].hooks[0].command.includes(path.join(skillsDir, "weeklylog", "scripts", "weeklylog.js")));
  hooks.hooks.Custom = [{ hooks: [{ type: "command", command: "echo custom" }] }];
  fs.writeFileSync(hooksPath, JSON.stringify(hooks), "utf8");
  const second = runCli(item.database, [
    "init", "--client", "codex", "--skills-dir", skillsDir,
    "--hooks-path", hooksPath, "--json",
  ]);
  assert.equal(second.status, 0, second.stderr);
  const merged = JSON.parse(fs.readFileSync(hooksPath, "utf8"));
  assert.equal(merged.hooks.Custom[0].hooks[0].command, "echo custom");
  for (const event of ["SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"]) assert.equal(merged.hooks[event].length, 1);
});

test("manual records, reflections, datasets, and reports use SQLite", () => {
  const item = fixture();
  const add = runCli(item.database, [
    "add", "--date", "2026-08-31", "--content", "完成登录页重构",
    "--project", "移动端", "--status", "done", "--result", "减少重复组件",
    "--minutes", "90", "--ai-turn-count", "4",
    "--collaboration-note", "来回解释了几次，最后才对齐", "--tags", "前端,重构", "--json",
  ]);
  assert.equal(add.status, 0, add.stderr);
  assert.equal(JSON.parse(add.stdout).content, "完成登录页重构");
  assert.equal(JSON.parse(add.stdout).ai_turn_count, 4);
  assert.equal(JSON.parse(add.stdout).collaboration_note, "来回解释了几次，最后才对齐");
  const list = runCli(item.database, ["list", "--week", "2026-08-31", "--tag", "前端", "--json"]);
  assert.equal(JSON.parse(list.stdout).count, 1);
  const reflection = runCli(item.database, ["reflect", "--week", "2026-08-31", "--mood", "4.2", "--happy-moment", "顺利上线", "--json"]);
  assert.equal(JSON.parse(reflection.stdout).mood, 4.2);
  const dataset = runCli(item.database, ["dataset", "--week", "2026-08-31", "--json"]);
  assert.equal(JSON.parse(dataset.stdout).entries.length, 1);
  const report = runCli(item.database, ["report", "--week", "2026-08-31", "--detail"]);
  assert.equal(report.status, 0, report.stderr);
  assert.match(report.stdout, /完成登录页重构/);
  assert.match(report.stdout, /## 本周核心成果/);
  assert.match(report.stdout, /## 待确认记录/);
});

test("Codex hook events create reviewable candidates without storing prompt text", () => {
  const item = fixture();
  const promptText = "不要把这段完整提示词写进数据库";
  const prompt = runCli(item.database, ["hook", "ingest", "--json"], JSON.stringify({
    event: "UserPromptSubmit", client: "codex", session_id: "s1", turn_id: "t1",
    prompt: promptText, started_at: "2026-08-31T10:00:00Z",
  }));
  assert.equal(prompt.status, 0, prompt.stderr);
  assert.equal(JSON.parse(prompt.stdout).action, "prompt");
  const stop = runCli(item.database, ["hook", "ingest", "--json"], JSON.stringify({
    event: "Stop", client: "codex", session_id: "s1", turn_id: "t1",
    title: "完成 hook 采集迁移", summary: "验证候选写入和审核流程",
    project: "weeklylog", started_at: "2026-08-31T10:00:00Z", ended_at: "2026-08-31T10:15:00Z",
  }));
  assert.equal(stop.status, 0, stop.stderr);
  const candidate = JSON.parse(stop.stdout).candidate;
  assert.equal(candidate.review_state, "candidate");
  assert.equal(candidate.title, "完成 hook 采集迁移");
  assert.equal(candidate.ai_turn_count, 1);
  const listed = runCli(item.database, ["hook", "list", "--week", "2026-08-31", "--json"]);
  assert.equal(JSON.parse(listed.stdout).count, 1);
  const promoted = runCli(item.database, ["hook", "promote", String(candidate.id), "--json"]);
  assert.equal(JSON.parse(promoted.stdout).source, "hook");
  const data = runCli(item.database, ["dataset", "--week", "2026-08-31", "--json"]);
  const exported = JSON.stringify(JSON.parse(data.stdout));
  assert.equal(exported.includes(promptText), false);
  const report = runCli(item.database, ["report", "--week", "2026-08-31"]);
  assert.match(report.stdout, /本周最费沟通的一次/);
});

test("native Codex Stop payload derives only a short work candidate", () => {
  const item = fixture();
  const fullReply = "完成 SQLite hook 迁移\n已验证 10MB 上限和候选审核流程。\n" + "不应落盘的长回复 ".repeat(200);
  const stop = runCli(item.database, ["hook", "ingest", "--json"], JSON.stringify({
    hook_event_name: "Stop", client: "codex", session_id: "native-stop", turn_id: "turn-1",
    last_assistant_message: fullReply,
  }));
  assert.equal(stop.status, 0, stop.stderr);
  const candidate = JSON.parse(stop.stdout).candidate;
  assert.equal(candidate.title, "完成 SQLite hook 迁移");
  assert.match(candidate.summary, /已验证 10MB/);
  assert.equal(candidate.summary.includes("不应落盘的长回复"), false);
});

test("hook capture stores one aggregate session row and stays within the 10MB budget", () => {
  const item = fixture();
  for (let index = 0; index < 60; index += 1) {
    const result = runCli(item.database, ["hook", "ingest", "--hook-output"], JSON.stringify({
      event: "UserPromptSubmit", client: "codex", session_id: "compact-session", turn_id: `turn-${index}`,
      prompt: `这是一段不应进入 SQLite 的完整对话 ${"x".repeat(2000)}`,
    }));
    assert.equal(result.status, 0, result.stderr);
  }
  const db = new DatabaseSync(item.database);
  const tables = db.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all().map((row) => row.name);
  assert.equal(tables.includes("automation_turns"), false);
  assert.equal(tables.includes("automation_sessions"), false);
  const session = db.prepare("SELECT turn_count, last_turn_id FROM capture_sessions WHERE session_id = ?").get("compact-session");
  assert.equal(session.turn_count, 60);
  assert.equal(session.last_turn_id, "turn-59");
  db.close();
  const bytes = [item.database, `${item.database}-wal`, `${item.database}-shm`, `${item.database}-journal`].reduce((total, file) => {
    try { return total + fs.statSync(file).size; } catch { return total; }
  }, 0);
  assert.ok(bytes <= 10 * 1024 * 1024, `SQLite footprint is ${bytes} bytes`);
});

test("legacy transcript tables are removed while preserving only aggregate turn count", () => {
  const item = fixture();
  const legacy = new DatabaseSync(item.database);
  legacy.exec(`
    CREATE TABLE automation_sessions (session_id TEXT PRIMARY KEY, source TEXT, client TEXT, cwd TEXT, started_at TEXT, ended_at TEXT, last_turn_id TEXT, last_prompt TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE automation_turns (session_id TEXT, turn_id TEXT, source TEXT, prompt TEXT, started_at TEXT, ended_at TEXT, assistant_message TEXT, state TEXT, updated_at TEXT);
  `);
  legacy.prepare("INSERT INTO automation_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)").run("legacy", "hook", "codex", "/tmp", "2026-08-31", null, "t1", "SECRET PROMPT", "2026-08-31", "2026-08-31");
  legacy.prepare("INSERT INTO automation_turns VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)").run("legacy", "t1", "hook", "SECRET PROMPT", "2026-08-31", null, "SECRET REPLY", "open", "2026-08-31");
  legacy.close();
  const stop = runCli(item.database, ["hook", "ingest", "--json"], JSON.stringify({ event: "Stop", session_id: "legacy", title: "保留聚合计数", summary: "只保留必要事实" }));
  assert.equal(stop.status, 0, stop.stderr);
  assert.equal(JSON.parse(stop.stdout).candidate.ai_turn_count, 1);
  const migrated = new DatabaseSync(item.database);
  const tables = migrated.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all().map((row) => row.name);
  assert.equal(tables.includes("automation_sessions"), false);
  assert.equal(tables.includes("automation_turns"), false);
  assert.equal(migrated.prepare("SELECT turn_count FROM capture_sessions WHERE session_id = ?").get("legacy").turn_count, 1);
  assert.equal(JSON.stringify(migrated.prepare("SELECT * FROM capture_sessions").all()).includes("SECRET"), false);
  migrated.close();
});

test("weekglow generates six editable SVG cards", () => {
  const item = fixture();
  runCli(item.database, ["add", "--date", "2026-08-31", "--content", "完成周度卡片", "--result", "生成 SVG", "--json"]);
  const outputDir = path.join(item.directory, "weekglow");
  const result = runCli(item.database, ["weekglow", "--week", "2026-08-31", "--output-dir", outputDir, "--json"]);
  assert.equal(result.status, 0, result.stderr);
  const files = fs.readdirSync(outputDir).filter((name) => name.endsWith(".svg"));
  assert.equal(files.length, 6);
  assert.match(fs.readFileSync(path.join(outputDir, files[0]), "utf8"), /<svg/);
});

test("unsupported clients are rejected", () => {
  const item = fixture();
  const result = runCli(item.database, ["init", "--client", "other-ai"]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /当前只支持 Codex/);
});
