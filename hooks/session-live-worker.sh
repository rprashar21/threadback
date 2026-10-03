#!/bin/bash
# Detached worker spawned by session_record.py live-tick (via the Stop-hook
# checkpoint) to refresh the summary of a still-open session. Same shape as
# session-summarize-worker.sh; the only differences are the "live" summary
# source and releasing the in-flight lock when done.
#
# Args: TRANSCRIPT_PATH PROJECT_SLUG SESSION_ID CWD LOCK_PATH

set -uo pipefail

if [ -n "${CLAUDE_SESSION_LOG_SUMMARIZER:-}" ]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRANSCRIPT_PATH="$1"
PROJECT_SLUG="$2"
SESSION_ID="$3"
LOCK_PATH="${5:-}"
HOOK_ERROR_LOG="$HOME/.claude/session-logs/hook-errors.log"

trap '[ -n "$LOCK_PATH" ] && rm -f "$LOCK_PATH"' EXIT

if ! WORKER_ERR="$(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import session_summarize as ss
ss.run_bounded_summary(sys.argv[2], sys.argv[3], sys.argv[4], "live", timeout_secs=120)
' "$SCRIPT_DIR/../scripts" "$TRANSCRIPT_PATH" "$PROJECT_SLUG" "$SESSION_ID" 2>&1 1>/dev/null)"; then
  printf '[%s] session-live-worker.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$WORKER_ERR" >> "$HOOK_ERROR_LOG" 2>/dev/null || true
fi

exit 0
