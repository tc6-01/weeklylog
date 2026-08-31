#!/usr/bin/env node

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");
const skillRoot = path.join(packageRoot, "skill");
const pythonScript = path.join(skillRoot, "scripts", "weeklylog.py");
const version = require(path.join(packageRoot, "package.json")).version;

function printHelp() {
  console.log(`weeklylog-cli ${version}

Usage:
  npx weeklylog-cli <command> [options]

Commands:
  install [--target PATH] [--force]  Install the Codex skill into ~/.codex/skills/weeklylog
  doctor                            Check the local runtime and database path
  <weeklylog command>                Pass through to the SQLite-backed CLI

Examples:
  npx weeklylog-cli install
  npx weeklylog-cli add --content "完成登录页重构" --minutes 90
  npx weeklylog-cli report --detail
  npx weeklylog-cli hook config --client codex
`);
}

function fail(message) {
  console.error(`weeklylog: ${message}`);
  process.exitCode = 1;
}

function installSkill(args) {
  let target = path.join(os.homedir(), ".codex", "skills", "weeklylog");
  let force = false;
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (arg === "--force") {
      force = true;
    } else if (arg === "--target") {
      if (!args[index + 1]) {
        fail("--target 需要路径");
        return;
      }
      target = path.resolve(args[index + 1]);
      index += 1;
    } else {
      fail(`install 不支持参数：${arg}`);
      return;
    }
  }

  if (fs.existsSync(target) && !force) {
    fail(`目标已存在：${target}；如需更新请加 --force`);
    return;
  }
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.cpSync(skillRoot, target, { recursive: true, force: true });
  console.log(`已安装 weeklylog skill：${target}`);
}

function doctor() {
  const python = spawnSync("python3", ["--version"], { encoding: "utf8" });
  if (python.error || python.status !== 0) {
    fail("找不到 python3；weeklylog 需要 Python 3");
    return;
  }
  const db = process.env.WEEKLYLOG_DB || path.join(os.homedir(), ".codex", "data", "weeklylog", "worklog.sqlite3");
  console.log(`node: ${process.version}`);
  console.log(`python: ${(python.stdout || python.stderr).trim()}`);
  console.log(`database: ${db}`);
  console.log(`skill source: ${skillRoot}`);
}

const args = process.argv.slice(2);
if (args.length === 0 || args[0] === "--help" || args[0] === "-h") {
  printHelp();
} else if (args[0] === "--version" || args[0] === "-v") {
  console.log(version);
} else if (args[0] === "install") {
  installSkill(args.slice(1));
} else if (args[0] === "doctor") {
  doctor();
} else {
  const result = spawnSync("python3", [pythonScript, ...args], {
    stdio: "inherit",
    env: process.env,
  });
  if (result.error) {
    fail(`运行 Python CLI 失败：${result.error.message}`);
  } else if (typeof result.status === "number") {
    process.exitCode = result.status;
  } else {
    process.exitCode = 1;
  }
}
