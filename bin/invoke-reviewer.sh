#!/usr/bin/env bash
# invoke-reviewer.sh — Invoke reviewer agent on a spec or PR
#
# Usage:
#   invoke-reviewer.sh                           # Read latest brief from inbox
#   invoke-reviewer.sh /path/to/spec.md          # Direct spec review
#   invoke-reviewer.sh --codebase-review brief   # GitHub repo codebase review

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"
AGENT_SERVER="http://127.0.0.1:${AGENT_SERVER_PORT:-18791}"
AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"

# Determine reviewer agent name from config
REVIEWER_AGENT=""
if [ -f "$WORKSPACE_ROOT/config/agents.json" ]; then
    REVIEWER_AGENT=$(python3 -c "
import json
cfg = json.load(open('$WORKSPACE_ROOT/config/agents.json'))
for name, info in cfg.get('agents', {}).items():
    prompt_path = info.get('system_prompt', '')
    if 'reviewer' in prompt_path or 'reviewer' in name:
        print(name)
        break
" 2>/dev/null || echo "")
fi

if [[ -z "$REVIEWER_AGENT" ]]; then
    echo "Error: no reviewer agent found in agents.json" >&2
    exit 1
fi

REVIEWER_DIR="$WORKSPACE_ROOT/agents/$REVIEWER_AGENT"
# Backend selection: "claude" (default) drives the Claude Code CLI; "ollama"
# routes through the minimal native runner against a local Ollama server.
AGENT_BACKEND="${AGENT_BACKEND:-claude}"
if [[ "$AGENT_BACKEND" == "ollama" ]]; then
    MODEL="${MODEL:-${OLLAMA_MODEL:-llama3.1}}"
else
    MODEL="${MODEL:-sonnet}"
fi
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

echo "Invoking reviewer: $REVIEWER_AGENT (backend=$AGENT_BACKEND, model=$MODEL, iteration=$ITERATION)"

# Select the engine. Both speak the same stream-json `result` event, so the
# verdict/cost extraction below is backend-agnostic.
if [[ "$AGENT_BACKEND" == "ollama" ]]; then
    ENGINE=(python3 "$WORKSPACE_ROOT/bin/ollama_runner.py" --mode agent)
else
    ENGINE=(claude)
fi

METADATA_FILE=$(mktemp)
trap "rm -f $METADATA_FILE" EXIT

# Run reviewer
timeout 3600 "${ENGINE[@]}" -p "$PROMPT" \
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
