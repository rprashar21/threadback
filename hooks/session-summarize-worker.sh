#!/bin/bash
# Detached worker spawned by session-end-log.sh to do the slow part (asking
# an LLM sub-agent to read the transcript and write a summary) fully outside
# the hook's own process lifetime. Launched via `start_new_session` (a new
# process group/session), so if the harness cancels or kills the SessionEnd
# hook for running too long, this worker is unaffected and keeps running.
#
# Thin wrapper: the actual prompt, bounded `claude -p` invocation, and
# content-coverage-gated merge all live in session_summarize.py, shared with
# session_dashboard.py's lazy-summarization pass — one place, not two.
#
# Args: TRANSCRIPT_PATH PROJECT_SLUG SESSION_ID CWD

set -uo pipefail

# Same recursion guard as the main hook: the claude -p sub-agent launched by
# session_summarize.py is itself a session, and its own SessionEnd
# re-invokes session-end-log.sh, which checks this var and bails out
# immediately.
if [ -n "${CLAUDE_SESSION_LOG_SUMMARIZER:-}" ]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRANSCRIPT_PATH="$1"
PROJECT_SLUG="$2"
SESSION_ID="$3"

python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import session_summarize as ss
ss.run_bounded_summary(sys.argv[2], sys.argv[3], sys.argv[4], "session_end", timeout_secs=180)
' "$SCRIPT_DIR/../scripts" "$TRANSCRIPT_PATH" "$PROJECT_SLUG" "$SESSION_ID" \
  >/dev/null 2>&1 || true

exit 0
