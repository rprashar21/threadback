#!/usr/bin/env node
// Installs recap-dashboard's hooks + scripts for someone who isn't going
// through the Claude Code plugin marketplace: copies the vendored hooks/
// and scripts/ into a fixed install directory, then merges (never
// overwrites) the SessionStart/Stop/SessionEnd hook entries into
// ~/.claude/settings.json.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execSync } = require("child_process");

const INSTALL_DIR = path.join(os.homedir(), ".claude-recap-dashboard");
const VENDOR_DIR = path.join(__dirname, "..", "vendor");
const SETTINGS_PATH = path.join(os.homedir(), ".claude", "settings.json");

function fail(message) {
  console.error(`recap-dashboard-install: ${message}`);
  process.exit(1);
}

function commandExists(cmd) {
  try {
    execSync(`command -v ${cmd}`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function copyDir(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const src = path.join(from, entry.name);
    const dst = path.join(to, entry.name);
    if (entry.isDirectory()) {
      copyDir(src, dst);
    } else {
      fs.copyFileSync(src, dst);
      fs.chmodSync(dst, entry.name.endsWith(".sh") ? 0o755 : 0o644);
    }
  }
}

function loadSettings() {
  if (!fs.existsSync(SETTINGS_PATH)) {
    fs.mkdirSync(path.dirname(SETTINGS_PATH), { recursive: true });
    return {};
  }
  return JSON.parse(fs.readFileSync(SETTINGS_PATH, "utf8"));
}

function mergeHook(settings, event, command, extra) {
  settings.hooks = settings.hooks || {};
  settings.hooks[event] = settings.hooks[event] || [];
  const entries = settings.hooks[event];

  const alreadyPresent = entries.some((group) =>
    (group.hooks || []).some((h) => h.command === command)
  );
  if (alreadyPresent) return false;

  entries.push({
    matcher: "",
    hooks: [{ type: "command", command, ...extra }],
  });
  return true;
}

function main() {
  if (process.platform === "win32") {
    fail(
      "this tool relies on POSIX file locking (fcntl) and only supports macOS/Linux. Windows is not supported."
    );
  }
  if (!commandExists("python3")) {
    fail("python3 was not found on PATH. Install Python 3 and re-run.");
  }
  if (!commandExists("claude")) {
    fail(
      "the `claude` CLI was not found on PATH. Install Claude Code first: https://claude.com/claude-code"
    );
  }
  if (!fs.existsSync(VENDOR_DIR)) {
    fail("vendored hooks/scripts are missing from this package install — reinstall the package.");
  }

  copyDir(path.join(VENDOR_DIR, "hooks"), path.join(INSTALL_DIR, "hooks"));
  copyDir(path.join(VENDOR_DIR, "scripts"), path.join(INSTALL_DIR, "scripts"));

  const settings = loadSettings();
  const hooksDir = path.join(INSTALL_DIR, "hooks");

  const changed = [
    mergeHook(settings, "SessionStart", `bash "${path.join(hooksDir, "session-start-log.sh")}"`),
    mergeHook(settings, "Stop", `bash "${path.join(hooksDir, "session-checkpoint.sh")}"`),
    mergeHook(settings, "SessionEnd", `bash "${path.join(hooksDir, "session-end-log.sh")}"`, {
      timeout: 10,
    }),
  ].some(Boolean);

  fs.writeFileSync(SETTINGS_PATH, JSON.stringify(settings, null, 2) + "\n");

  console.log(`recap-dashboard installed to ${INSTALL_DIR}`);
  console.log(
    changed
      ? "Hooks registered in ~/.claude/settings.json (existing entries left untouched)."
      : "Hooks were already registered in ~/.claude/settings.json — nothing changed there."
  );
  console.log(
    `Restart Claude Code, then regenerate the dashboard any time with:\n  python3 ${path.join(
      INSTALL_DIR,
      "scripts",
      "session_dashboard.py"
    )}`
  );
}

main();
