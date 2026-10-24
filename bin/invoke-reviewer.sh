#!/usr/bin/env bash
# invoke-reviewer.sh — Invoke reviewer agent on a spec or PR
#
# Usage:
#   invoke-reviewer.sh                           # Read latest brief from inbox
#   invoke-reviewer.sh /path/to/spec.md          # Direct spec review
#   invoke-reviewer.sh --codebase-review brief   # GitHub repo codebase review

set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/paths.sh"
AGENT_SERVER="http://127.0.0.1:${AGENT_SERVER_PORT:-18791}"
AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"

# Determine reviewer agent name from config/agents.yaml (role: reviewer)
REGISTRY_PY="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/../lib" && pwd)/registry.py"
REVIEWER_AGENT=$(python3 "$REGISTRY_PY" --workspace "$WORKSPACE_ROOT" role reviewer 2>/dev/null | head -1 || echo "")

if [[ -z "$REVIEWER_AGENT" ]]; then
    echo "Error: no reviewer agent found in agents.yaml" >&2
    exit 1
fi

REVIEWER_DIR="$WORKSPACE_ROOT/agents/$REVIEWER_AGENT"
MODEL="${MODEL:-sonnet}"
DISPATCH_ID="${DISPATCH_ID:-}"
OUTPUT_FORMAT="stream-json"
CODEBASE_REVIEW=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)            MODEL="$2"; shift 2 ;;
        --dispatch-id)      DISPATCH_ID="$2"; shift 2 ;;
        --output-format)    OUTPUT_FORMAT="$2"; shift 2 ;;
        --codebase-review)  CODEBASE_REVIEW=true; shift ;;
        --help|-h)
            echo "Usage: invoke-reviewer.sh [OPTIONS] [SPEC_PATH|TASK]"
            echo ""
            echo "Options:"
            echo "  --model MODEL          Claude model (default: sonnet)"
            echo "  --dispatch-id ID       Dispatch tracking ID"
            echo "  --codebase-review      Review a codebase/repo instead of a spec"
            exit 0
            ;;
        *)  break ;;
    esac
done

# Determine task source
BRIEF_PATH=""
TASK=""

if [[ $# -gt 0 ]]; then
    if [[ -f "$1" ]]; then
        BRIEF_PATH="$1"
    else
        TASK="$*"
    fi
else
    INBOX="$WORKSPACE_ROOT/inbox/$REVIEWER_AGENT"
    if [[ -d "$INBOX" ]]; then
        BRIEF_PATH=$(find "$INBOX" -name '*.md' -type f | sort | tail -1)
    fi
fi

if [[ -z "$BRIEF_PATH" && -z "$TASK" ]]; then
    echo "Error: no brief or task provided" >&2
    exit 1
fi

# Read spec content and detect review type
if [[ -n "$BRIEF_PATH" ]]; then
    SPEC_CONTENT=$(cat "$BRIEF_PATH")

    # Check frontmatter for type
    REVIEW_TYPE=$(echo "$SPEC_CONTENT" | grep -E '^type:' | head -1 | awk '{print $2}' || echo "spec-review")
    if [[ "$REVIEW_TYPE" == "codebase-review" ]]; then
        CODEBASE_REVIEW=true
    fi

    REQUESTER=$(echo "$SPEC_CONTENT" | grep -E '^requester:' | head -1 | awk '{print $2}' || echo "")
    CALLBACK_CHANNEL=$(echo "$SPEC_CONTENT" | grep -E '^callback_channel:' | head -1 | awk '{print $2}' || echo "general")
    ITERATION=$(echo "$SPEC_CONTENT" | grep -E '^iteration:' | head -1 | awk '{print $2}' || echo "1")

    PROMPT="Review the following specification or code change. Apply the review criteria from your system prompt.

Iteration: $ITERATION

---
$SPEC_CONTENT"
else
    PROMPT="$TASK"
    REQUESTER=""
    CALLBACK_CHANNEL="general"
    ITERATION="1"
fi

# Set allowed tools based on review type
if [[ "$CODEBASE_REVIEW" == "true" ]]; then
    ALLOWED_TOOLS="Bash,Read,Glob,Grep,Write,WebFetch"
else
    ALLOWED_TOOLS="Read,Glob,Grep,Write"
fi

SYSTEM_PROMPT="$REVIEWER_DIR/SYSTEM_PROMPT.md"
if [[ ! -f "$SYSTEM_PROMPT" ]]; then
    echo "Error: reviewer system prompt not found: $SYSTEM_PROMPT" >&2
    exit 1
fi

echo "Invoking reviewer: $REVIEWER_AGENT (model=$MODEL, iteration=$ITERATION)"

METADATA_FILE=$(mktemp)
trap "rm -f $METADATA_FILE" EXIT

# Run reviewer
timeout 3600 claude -p "$PROMPT" \
    --model "$MODEL" \
    --max-turns 200 \
    --system-prompt "$SYSTEM_PROMPT" \
    --allowedTools "$ALLOWED_TOOLS" \
    --output-format "$OUTPUT_FORMAT" \
    --verbose \
    --dangerously-skip-permissions \
    2>/dev/null | tee >(
        python3 -c "
import sys, json
for line in sys.stdin:
    try:
        event = json.loads(line.strip())
        if event.get('type') == 'result':
            with open('$METADATA_FILE', 'w') as f:
                json.dump(event, f)
    except:
        pass
" 2>/dev/null
    )

EXIT_CODE=$?

# Extract verdict
VERDICT=""
if [[ -f "$METADATA_FILE" ]]; then
    RESULT_TEXT=$(python3 -c "
import json
try:
    data = json.load(open('$METADATA_FILE'))
    print(data.get('result', ''))
except:
    pass
" 2>/dev/null || echo "")

    VERDICT=$(echo "$RESULT_TEXT" | grep -iE 'Verdict:\s*(APPROVE|REVISE|RETHINK)' | head -1 | grep -oiE '(APPROVE|REVISE|RETHINK)' || echo "UNKNOWN")
fi

# Post cost to agent server
if [[ -f "$METADATA_FILE" ]]; then
    python3 -c "
import json, os, urllib.request
try:
    data = json.load(open('$METADATA_FILE'))
    cost = data.get('total_cost_usd', 0)
    payload = json.dumps({
        'agent': '$REVIEWER_AGENT',
        'cost_delta': cost,
        'session_total': cost,
        'input_tokens': data.get('usage', {}).get('input_tokens', 0),
        'output_tokens': data.get('usage', {}).get('output_tokens', 0),
        'duration_ms': data.get('duration_ms', 0),
    }).encode()
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('AGENT_SERVER_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request('$AGENT_SERVER/cost', data=payload, headers=headers, method='POST')
    urllib.request.urlopen(req, timeout=5)
except:
    pass
" 2>/dev/null || true
fi

# Queue-owned run (spec 3.3): the dispatcher notifies the requester and owns the row.
if [[ -n "${KARAKOS_QUEUE_RUN:-}" ]]; then
    exit $EXIT_CODE
fi

# Poke back requester
if [[ -n "${REQUESTER:-}" ]]; then
    "$WORKSPACE_ROOT/bin/poke.sh" --agent "$REQUESTER" --source "$REVIEWER_AGENT" \
        --reply-channel "$CALLBACK_CHANNEL" \
        "Review complete (iteration $ITERATION). Verdict: ${VERDICT:-UNKNOWN}"
fi

# Archive brief
if [[ -n "$BRIEF_PATH" && -f "$BRIEF_PATH" ]]; then
    ARCHIVE_DIR="$WORKSPACE_ROOT/inbox/$REVIEWER_AGENT/archive"
    mkdir -p "$ARCHIVE_DIR"
    mv "$BRIEF_PATH" "$ARCHIVE_DIR/$(date +%Y-%m-%d)-$(basename "$BRIEF_PATH")"
fi

exit $EXIT_CODE
