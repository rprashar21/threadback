# Recap Dashboard

A local, cross-project work recap for [Claude Code](https://claude.com/claude-code).
Hooks record what happened in each session (start, activity checkpoints, and
an evidence-based summary — never a guessed status), and a single static
HTML dashboard shows what's done, what's blocked or in progress, and a
one-command way to resume any session. All data stays on your machine, but
end-of-session summaries are produced by a real local `claude -p` subprocess
call. A deterministic local pass removes thinking, metadata, attachments, and
oversized tool output first, then caps the evidence sent for each summary
at 40,000 characters. Project recaps are assembled locally from those existing
session summaries and make no additional model calls. This is therefore not a
fully offline pipeline, but model input is bounded and auditable.

Dashboard backfill makes at most two sequential summary calls per run, accepts
at most 80,000 evidence characters in aggregate, and gives each `claude -p`
call a $0.10 hard budget ceiling. Override the per-call ceiling with the
`RECAP_SUMMARY_MAX_BUDGET_USD` environment variable.

## Requirements

- macOS or Linux. The record-locking (`fcntl`) this relies on is POSIX-only — **Windows is not supported**.
- `python3` and the `claude` CLI on `PATH`.

## Running the recap

Run the same entry point used by the `/recap` command:

```bash
./scripts/recap.sh
```

It regenerates the dashboard, opens it with `open` on macOS or `xdg-open` on
Linux, and always prints the generated path. Set `RECAP_NO_OPEN=1` to generate
without opening a browser. Any arguments are forwarded to
`session_dashboard.py`, including `--summarize-session <session_id>`.

Sessions whose latest start/resume event has no newer end event are retained in
the dashboard. Once such a session has been open for two hours, project and
session views show an `Open for 2h…` indicator without invoking AI.

## Layout

```
hooks/     SessionStart / Stop / SessionEnd hook scripts, plus hooks.json (plugin hook declarations)
scripts/   session_record.py    — concurrency-safe per-session JSON records
           session_summarize.py — bounded, one-shot LLM summarization
           session_dashboard.py — generates the dashboard.html
commands/  recap.md — /recap slash command (plugin install only)
npx/       standalone installer, published separately (see below)
.claude-plugin/  plugin.json + marketplace.json (self-hosted single-plugin marketplace)
```

## Installing

### Option A: as a Claude Code plugin (recommended)

```
claude plugin marketplace add rprashar21/threadback
claude plugin install threadback@threadback
```

This registers the `SessionStart`/`Stop`/`SessionEnd` hooks and the `/recap`
slash command automatically. No manual editing of `~/.claude/settings.json`.

Restart Claude Code after installing, since hooks load at session start. Then
run `/recap` to generate and open the dashboard.

**Verify:** `claude plugin list` should show `threadback@threadback` at the
current version (see `.claude-plugin/plugin.json`).

**Update to the latest version:**

```
claude plugin marketplace update threadback
claude plugin update threadback@threadback
```

Restart Claude Code afterwards. Updates are keyed on the `version` in
`plugin.json`, so maintainers must bump it with every release or installed
copies will not update.

**Uninstall:**

```
claude plugin uninstall threadback@threadback
claude plugin marketplace remove threadback
```

Existing session records under `~/.claude/session-logs/` are left in place.

**Requirements:** `python3` (stdlib only), `bash`, and the `claude` CLI on
your `PATH` (used for session summaries).

Do not combine Option A with Option B or C on the same machine. The hooks
would be registered twice and every session would be recorded twice.

### Option B: npx installer

```
npx @8thlight/recap-dashboard-install
```

Copies `hooks/` and `scripts/` into `~/.claude-recap-dashboard/` and merges
the three hook entries into `~/.claude/settings.json` (existing hooks and
other settings are left untouched; safe to re-run). See `npx/` for the
installer source.

### Option C: manual symlink (this repo's own dev setup)

This repo's own development machine is wired into `~/.claude/` via manual
symlinks (`~/.claude/hooks/*` and `~/.claude/scripts/session_*.py` point back
into this repo) instead of using either installer above, so that editing
files here takes effect immediately. Only worth doing if you're actively
developing this tool itself.
