# Recap Dashboard

A local, cross-project work recap for [Claude Code](https://claude.com/claude-code).
Hooks record what happened in each session (start, activity checkpoints, and
an evidence-based summary — never a guessed status), and a single static
HTML dashboard shows what's done, what's blocked or in progress, and a
one-command way to resume any session. All data stays on your machine, but
end-of-session summaries and project recaps are produced by a real local
`claude -p` subprocess call, which sends that session's transcript to your
configured Claude model — this is not a fully offline pipeline.

## Requirements

- macOS or Linux. The record-locking (`fcntl`) this relies on is POSIX-only — **Windows is not supported**.
- `python3` and the `claude` CLI on `PATH`.

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
claude plugin marketplace add <owner>/recap-dashboard
claude plugin install recap-dashboard@recap-dashboard
```

This registers the `SessionStart`/`Stop`/`SessionEnd` hooks and the `/recap`
slash command automatically — no manual editing of `~/.claude/settings.json`.

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
