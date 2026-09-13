# Recap Dashboard

A local, cross-project work recap for [Claude Code](https://claude.com/claude-code).
Hooks record what happened in each session (start, activity checkpoints, and
an evidence-based summary — never a guessed status), and a single static
HTML dashboard shows what's done, what's blocked or in progress, and a
one-command way to resume any session. Everything runs locally; nothing
leaves your machine.

## Layout

```
hooks/     SessionStart / Stop / SessionEnd hook scripts
scripts/   session_record.py    — concurrency-safe per-session JSON records
           session_summarize.py — bounded, one-shot LLM summarization
           session_dashboard.py — generates the dashboard.html
```

This repo is currently wired into one machine's `~/.claude/` via symlinks
(`~/.claude/hooks/*` and `~/.claude/scripts/session_*.py` point in here).
Packaging this for others to install standalone (a proper install script,
license, etc.) is a follow-up, not done yet.
