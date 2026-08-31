#!/usr/bin/env node

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const packageRoot = path.resolve(__dirname, "..");
const skillRoot = path.join(packageRoot, "skill");
const pythonScript = path.join(skillRoot, "scripts", "weeklylog.py");
const bundledSkills = {
  weeklylog: skillRoot,
  weekglow: path.join(packageRoot, "skills", "weekglow"),
  yearglow: path.join(packageRoot, "skills", "yearglow"),
};
const version = require(path.join(packageRoot, "package.json")).version;

function printHelp() {
  console.log(`weeklylog-cli ${version}

Usage:
  npx weeklylog-cli <command> [options]

Commands:
  install [--skills-dir PATH] [--force]  Install weeklylog, weekglow, and yearglow
  install --target PATH [--force]        Legacy: install only weeklylog to an exact path
  doctor                                Check the local runtime and database path
  weekglow [options]                    Generate weekly visual cards
  yearglow [options]                    Generate annual visual cards
  <weeklylog command>                    Pass through to the SQLite-backed CLI

Examples:
  npx weeklylog-cli install
  npx weeklylog-cli add --content "完成登录页重构" --minutes 90
  npx weeklylog-cli report --detail
  npx weeklylog-cli weekglow --week 2026-08-31 --format both
  npx weeklylog-cli hook config --client codex
`);
}

function fail(message) {
  console.error(`weeklylog: ${message}`);
  process.exitCode = 1;
}

function installSkill(args) {
  let target = null;
  let skillsDir = path.join(os.homedir(), ".codex", "skills");
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
    } else if (arg === "--skills-dir") {
      if (!args[index + 1]) {
        fail("--skills-dir 需要路径");
        return;
      }
      skillsDir = path.resolve(args[index + 1]);
      index += 1;
    } else {
      fail(`install 不支持参数：${arg}`);
      return;
    }
  }

  if (target) {
    if (fs.existsSync(target) && !force) {
      fail(`目标已存在：${target}；如需更新请加 --force`);
      return;
    }
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.cpSync(skillRoot, target, { recursive: true, force: true });
    console.log(`已安装 weeklylog skill：${target}`);
    return;
  }

  const targets = Object.entries(bundledSkills).map(([name, source]) => ({
    name,
    source,
    target: path.join(skillsDir, name),
  }));
  if (!force) {
    const existing = targets.find((item) => fs.existsSync(item.target));
    if (existing) {
      fail(`目标已存在：${existing.target}；如需更新请加 --force`);
      return;
    }
  }
  fs.mkdirSync(skillsDir, { recursive: true });
  for (const item of targets) {
    fs.cpSync(item.source, item.target, { recursive: true, force: true });
    console.log(`已安装 ${item.name} skill：${item.target}`);
  }
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
  const visualKind = args[0] === "weekglow" ? "week" : args[0] === "yearglow" ? "year" : null;
  const forwardedArgs = visualKind ? ["deck", "--kind", visualKind, ...args.slice(1)] : args;
  const result = spawnSync("python3", [pythonScript, ...forwardedArgs], {
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
