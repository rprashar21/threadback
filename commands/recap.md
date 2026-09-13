---
description: Regenerate and open the local Claude Work Recap Dashboard
allowed-tools: [Bash]
---

Regenerate the recap dashboard and open it for the user.

1. Run: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_dashboard.py"` — it prints the dashboard's output path (an HTML file under `~/.claude/session-logs/`).
2. Open that path with the platform's default opener (`open` on macOS, `xdg-open` on Linux) so the user sees it in their browser.
3. Briefly tell the user it's open, and mention they can ask "what did we do last time" or similar to get a summary instead of opening the file.
