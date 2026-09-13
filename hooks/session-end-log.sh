#!/bin/bash
# SessionEnd hook: records that a session ended, then hands the slow
# summarization off to a detached worker. Writes into the session's single
# JSON record at
#   ~/.claude/session-logs/<project-slug>/<session_id>.json
# via session_record.py, instead of a timestamp-named file — so a repeat
# SessionEnd for the same session updates one record instead of creating a
# duplicate.
#
# Claude Code pipes a JSON payload to stdin on SessionEnd, e.g.:
#   {"session_id": "...", "transcript_path": "...", "cwd": "...", "reason": "..."}
#
# Design notes:
# - This script must return almost fast. SessionEnd hooks share a small
#   execution budget (raised via an explicit `timeout` on this hook's entry
#   in settings.json). So it only ever does fast, local work: write a small
#   "end" marker (ended_at/reason — a fact, not a guess at status), then
#   hand the slow summarization off to a fully detached background worker
#   (session-summarize-worker.sh, launched via a new process session) and
#   exit. The worker writes the real "summary" section later, on its own
#   time, immune to this hook being cancelled.
# - This hook does NOT write a placeholder "summary" — per the no-guessing
#   rule, a session with no real summary yet is just "Unknown" by omission.
#   session_dashboard.py backfills a real summary lazily if the worker below
#   never completes.
# - Every JSON write goes through session_record.py's flock-protected,
#   per-section merge, so concurrent writers (this hook, the worker, a
#   Stop-hook checkpoint, session_dashboard.py's lazy summarizer) can never
#   interleave a read and a write on the same record.

set -uo pipefail

# Guard against recursion: the summarizer sub-agent (launched by the
# detached worker below) is itself a Claude Code session, so its own
# SessionEnd would normally re-trigger this hook and spawn another
# summarizer, forever. The sub-agent inherits this env var, so when its
# SessionEnd fires we bail out immediately instead of recursing.
if [ -n "${CLAUDE_SESSION_LOG_SUMMARIZER:-}" ]; then
  exit 0
fi

LOG_ROOT="$HOME/.claude/session-logs"
DEBUG_LOG="$LOG_ROOT/.last-payload.json"
mkdir -p "$LOG_ROOT"

PAYLOAD="$(cat)"
echo "$PAYLOAD" > "$DEBUG_LOG"

TRANSCRIPT_PATH="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("transcript_path",""))' 2>/dev/null || true)"
CWD="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("cwd",""))' 2>/dev/null || true)"
REASON="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("reason",""))' 2>/dev/null || true)"

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

# Skip near-empty sessions (transcript smaller than ~1KB is likely a no-op session)
TRANSCRIPT_SIZE=$(stat -f%z "$TRANSCRIPT_PATH" 2>/dev/null || stat -c%s "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
if [ "$TRANSCRIPT_SIZE" -lt 1024 ]; then
  exit 0
fi

if [ -z "$CWD" ]; then
  CWD="$(pwd)"
fi

# session_id is just the transcript's filename (without extension) — that's
# how Claude Code names transcript files under ~/.claude/projects/<slug>/.
SESSION_ID="$(basename "$TRANSCRIPT_PATH" .jsonl)"

RECORD_HELPER="$HOME/.claude/scripts/session_record.py"
PROJECT_SLUG="$(python3 "$RECORD_HELPER" slugify "$CWD" 2>/dev/null || true)"
if [ -z "$PROJECT_SLUG" ]; then
  exit 0
fi

ENDED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Fast, deterministic fact — not a status guess. Written synchronously so it
# survives even if the worker below never completes.
END_DATA="$(python3 -c 'import json,sys; print(json.dumps({"ended_at": sys.argv[1], "reason": sys.argv[2], "cwd": sys.argv[3]}))' "$ENDED_AT" "$REASON" "$CWD")"
python3 "$RECORD_HELPER" merge-section \
  --project-slug "$PROJECT_SLUG" \
  --session-id "$SESSION_ID" \
  --section "end" \
  --data "$END_DATA" \
  --event-ts "$ENDED_AT" \
  >/dev/null 2>&1 || true

# Hand the slow part off to a fully detached worker (new process
# session/group via start_new_session) so it survives this hook being
# cancelled or timed out by the harness, then return immediately.
WORKER="$HOME/.claude/hooks/session-summarize-worker.sh"
python3 -c '
import subprocess, sys
subprocess.Popen(
    ["bash"] + sys.argv[1:],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
)
' "$WORKER" "$TRANSCRIPT_PATH" "$PROJECT_SLUG" "$SESSION_ID" "$CWD" \
  >/dev/null 2>&1 || true

exit 0
