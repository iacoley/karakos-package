#!/usr/bin/env bash
# UserPromptSubmit hook — appends one line per prompt to logs/hook-events.log.
#
# Wired via config/claude-settings.json and the --settings flag on the
# claude spawn line in bin/agent-server.py (issue #94). Its only job is to
# prove the hook event pipeline is actually live end-to-end; later hooks
# (symlink-edit guard, sleep-rewrite, deferred-work Stop hook, memory
# re-injection, declarative permissions — #95-#99) hang more hook entries
# off the same settings file.
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"
LOG_DIR="$WORKSPACE_ROOT/logs"
mkdir -p "$LOG_DIR"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) UserPromptSubmit" >> "$LOG_DIR/hook-events.log"
