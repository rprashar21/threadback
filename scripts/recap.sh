#!/bin/bash
# Single executable entry point for the recap workflow. The /recap command
# delegates here, and users can run this file directly without starting a
# second Claude session merely to interpret the skill instructions.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_SCRIPT="${RECAP_DASHBOARD_SCRIPT:-$SCRIPT_DIR/session_dashboard.py}"
PYTHON_BIN="${RECAP_PYTHON:-python3}"

if [ ! -f "$DASHBOARD_SCRIPT" ]; then
  printf 'recap: dashboard generator not found: %s\n' "$DASHBOARD_SCRIPT" >&2
  exit 1
fi

if ! GENERATOR_OUTPUT="$("$PYTHON_BIN" "$DASHBOARD_SCRIPT" "$@")"; then
  printf 'recap: dashboard generation failed.\n' >&2
  exit 1
fi

# session_dashboard.py prints the generated path on its final stdout line.
DASHBOARD_PATH="${GENERATOR_OUTPUT##*$'\n'}"
if [ -z "$DASHBOARD_PATH" ] || [ ! -f "$DASHBOARD_PATH" ]; then
  printf 'recap: generator did not return a readable dashboard path.\n' >&2
  exit 1
fi

OPENED=0
if [ "${RECAP_NO_OPEN:-0}" != "1" ]; then
  case "$(uname -s 2>/dev/null || true)" in
    Darwin)
      if command -v open >/dev/null 2>&1 && open "$DASHBOARD_PATH"; then
        OPENED=1
      fi
      ;;
    Linux)
      if command -v xdg-open >/dev/null 2>&1 && xdg-open "$DASHBOARD_PATH"; then
        OPENED=1
      fi
      ;;
  esac
fi

if [ "$OPENED" -eq 1 ]; then
  printf 'Threadback dashboard opened.\n'
elif [ "${RECAP_NO_OPEN:-0}" = "1" ]; then
  printf 'Threadback dashboard generated (automatic opening disabled).\n'
else
  printf 'Threadback dashboard generated, but could not be opened automatically.\n' >&2
fi
printf 'Dashboard: %s\n' "$DASHBOARD_PATH"
