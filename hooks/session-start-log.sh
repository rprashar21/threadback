#!/bin/bash
# SessionStart hook: records that a session began (or resumed) into
# ~/.claude/session-logs/<project-slug>/<session_id>.json, "start" section.
#
# Fast and synchronous by design — this only ever writes a few small fields
# through session_record.py's flock-protected merge, no LLM call, no network.
# If this hook never runs (or fails), nothing is lost: session_dashboard.py's
# reconciliation pass finds the session from its transcript file anyway.

set -uo pipefail

if [ -n "${CLAUDE_SESSION_LOG_SUMMARIZER:-}" ]; then
  exit 0
fi

PAYLOAD="$(cat)"

SESSION_ID="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("session_id",""))' 2>/dev/null || true)"
CWD="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("cwd",""))' 2>/dev/null || true)"
SOURCE="$(echo "$PAYLOAD" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("source",""))' 2>/dev/null || true)"

if [ -z "$SESSION_ID" ] || [ -z "$CWD" ]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RECORD_HELPER="$SCRIPT_DIR/../scripts/session_record.py"

PROJECT_SLUG="$(python3 "$RECORD_HELPER" slugify "$CWD" 2>/dev/null || true)"
if [ -z "$PROJECT_SLUG" ]; then
  exit 0
fi

STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
DATA="$(python3 -c 'import json,sys; print(json.dumps({"started_at": sys.argv[1], "source": sys.argv[2], "cwd": sys.argv[3]}))' "$STARTED_AT" "$SOURCE" "$CWD")"

python3 "$RECORD_HELPER" merge-section \
  --project-slug="$PROJECT_SLUG" \
  --session-id="$SESSION_ID" \
  --section="start" \
  --data="$DATA" \
  --event-ts="$STARTED_AT" \
  >/dev/null 2>&1 || true

exit 0
