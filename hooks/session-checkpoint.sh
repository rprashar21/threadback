#!/bin/bash
# Stop hook: records a cheap, deterministic activity checkpoint into
# ~/.claude/session-logs/<project-slug>/<session_id>.json, "checkpoint"
# section. Runs on every turn, so it must stay fast and simple:
#   - no LLM call
#   - no reading/parsing of the transcript's contents
# Just `stat`s the transcript file for its size and mtime. This is enough to
# tell session_dashboard.py "there's been activity since the last summary"
# and "does this session still look live right now" — nothing more.

set -uo pipefail

if [ -n "${CLAUDE_SESSION_LOG_SUMMARIZER:-}" ]; then
  exit 0
fi

PAYLOAD="$(cat)"

SESSION_ID="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("session_id",""))' 2>/dev/null || true)"
CWD="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("cwd",""))' 2>/dev/null || true)"
TRANSCRIPT_PATH="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("transcript_path",""))' 2>/dev/null || true)"

if [ -z "$SESSION_ID" ] || [ -z "$CWD" ] || [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
  exit 0
fi

RECORD_HELPER="$HOME/.claude/scripts/session_record.py"
PROJECT_SLUG="$(python3 "$RECORD_HELPER" slugify "$CWD" 2>/dev/null || true)"
if [ -z "$PROJECT_SLUG" ]; then
  exit 0
fi

TRANSCRIPT_BYTES=$(stat -f%z "$TRANSCRIPT_PATH" 2>/dev/null || stat -c%s "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
TRANSCRIPT_MTIME_EPOCH=$(stat -f%m "$TRANSCRIPT_PATH" 2>/dev/null || stat -c%Y "$TRANSCRIPT_PATH" 2>/dev/null || echo 0)
CHECKED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TRANSCRIPT_MTIME="$(python3 -c 'import datetime,sys; print(datetime.datetime.utcfromtimestamp(int(sys.argv[1])).strftime("%Y-%m-%dT%H:%M:%SZ"))' "$TRANSCRIPT_MTIME_EPOCH" 2>/dev/null || echo "$CHECKED_AT")"

DATA="$(python3 -c 'import json,sys; print(json.dumps({"checked_at": sys.argv[1], "transcript_bytes": int(sys.argv[2]), "transcript_mtime": sys.argv[3]}))' "$CHECKED_AT" "$TRANSCRIPT_BYTES" "$TRANSCRIPT_MTIME")"

python3 "$RECORD_HELPER" merge-section \
  --project-slug "$PROJECT_SLUG" \
  --session-id "$SESSION_ID" \
  --section "checkpoint" \
  --data "$DATA" \
  --event-ts "$CHECKED_AT" \
  >/dev/null 2>&1 || true

exit 0
