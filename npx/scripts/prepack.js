#!/usr/bin/env node
// Vends a fresh copy of the repo's hooks/ and scripts/ into npx/vendor/ before
// `npm pack`/`npm publish`, so the published package is self-contained (npm
// only ships files inside this package directory, not repo siblings).
// vendor/ itself is gitignored — it's rebuilt from the real source on every
// pack, never hand-edited.
"use strict";

const fs = require("fs");
const path = require("path");

const REPO_ROOT = path.join(__dirname, "..", "..");
const VENDOR_DIR = path.join(__dirname, "..", "vendor");

const SOURCES = [
  { from: path.join(REPO_ROOT, "hooks"), to: path.join(VENDOR_DIR, "hooks") },
  { from: path.join(REPO_ROOT, "scripts"), to: path.join(VENDOR_DIR, "scripts") },
];

const SKIP_NAMES = new Set(["__pycache__", "hooks.json"]);

function copyDir(from, to) {
  fs.rmSync(to, { recursive: true, force: true });
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    if (SKIP_NAMES.has(entry.name)) continue;
    const src = path.join(from, entry.name);
    const dst = path.join(to, entry.name);
    if (entry.isDirectory()) {
      copyDir(src, dst);
    } else {
      fs.copyFileSync(src, dst);
    }
  }
}

for (const { from, to } of SOURCES) {
  copyDir(from, to);
  console.log(`vendored ${path.relative(REPO_ROOT, from)} -> ${path.relative(path.join(__dirname, ".."), to)}`);
}
