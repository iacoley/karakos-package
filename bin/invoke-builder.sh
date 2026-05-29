#!/usr/bin/env bash
# invoke-builder.sh — Invoke builder agent on a spec/brief
#
# Usage:
#   invoke-builder.sh                       # Read latest brief from inbox
#   invoke-builder.sh /path/to/spec.md      # Build from specific file
#   invoke-builder.sh "task description"    # Direct task

set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/workspace}"
AGENT_SERVER="http://127.0.0.1:${AGENT_SERVER_PORT:-18791}"
AGENT_SERVER_TOKEN="${AGENT_SERVER_TOKEN:-}"

# Determine builder agent name from config
BUILDER_AGENT=""
if [ -f "$WORKSPACE_ROOT/config/agents.json" ]; then
    BUILDER_AGENT=$(python3 -c "
import json
cfg = json.load(open('$WORKSPACE_ROOT/config/agents.json'))
for name, info in cfg.get('agents', {}).items():
    prompt_path = info.get('system_prompt', '')
    if 'builder' in prompt_path or 'builder' in name:
        print(name)
        break
" 2>/dev/null || echo "")
fi

if [[ -z "$BUILDER_AGENT" ]]; then
    echo "Error: no builder agent found in agents.json" >&2
    exit 1
fi

BUILDER_DIR="$WORKSPACE_ROOT/agents/$BUILDER_AGENT"
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

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)        MODEL="$2"; shift 2 ;;
        --dispatch-id)  DISPATCH_ID="$2"; shift 2 ;;
        --output-format) OUTPUT_FORMAT="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: invoke-builder.sh [OPTIONS] [SPEC_PATH|TASK]"
            echo ""
            echo "Options:"
            echo "  --model MODEL          Claude model (default: sonnet)"
            echo "  --dispatch-id ID       Dispatch tracking ID"
            echo "  --output-format FMT    Output format (default: stream-json)"
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
    # Read latest brief from inbox
    INBOX="$WORKSPACE_ROOT/inbox/$BUILDER_AGENT"
    if [[ -d "$INBOX" ]]; then
        BRIEF_PATH=$(find "$INBOX" -name '*.md' -type f | sort | tail -1)
    fi
fi

if [[ -z "$BRIEF_PATH" && -z "$TASK" ]]; then
    echo "Error: no brief or task provided" >&2
    exit 1
fi

# Read spec content
if [[ -n "$BRIEF_PATH" ]]; then
    SPEC_CONTENT=$(cat "$BRIEF_PATH")

    # Parse frontmatter for repo/branch
    TARGET_BRANCH=$(echo "$SPEC_CONTENT" | grep -E '^target_branch:' | head -1 | awk '{print $2}' || echo "main")
    REPO=$(echo "$SPEC_CONTENT" | grep -E '^repo:' | head -1 | awk '{print $2}' || echo "")
    BRANCH_PREFIX=$(echo "$SPEC_CONTENT" | grep -E '^branch_prefix:' | head -1 | awk '{print $2}' || echo "${BUILDER_AGENT}/")
    REQUESTER=$(echo "$SPEC_CONTENT" | grep -E '^requester:' | head -1 | awk '{print $2}' || echo "")
    CALLBACK_CHANNEL=$(echo "$SPEC_CONTENT" | grep -E '^callback_channel:' | head -1 | awk '{print $2}' || echo "general")

    PROMPT="Build the following specification. Push a branch and file a PR when done.

Target branch: ${TARGET_BRANCH:-main}
Branch prefix: ${BRANCH_PREFIX}

---
$SPEC_CONTENT"
else
    PROMPT="$TASK"
    TARGET_BRANCH="main"
    REQUESTER=""
    CALLBACK_CHANNEL="general"
fi

# Determine working directory
WORK_DIR="$WORKSPACE_ROOT"
if [[ -n "${REPO:-}" && "$REPO" != "" ]]; then
    REPO_DIR="$WORKSPACE_ROOT/repos/$(basename "$REPO")"
    if [[ -d "$REPO_DIR" ]]; then
        WORK_DIR="$REPO_DIR"
    fi
fi

# Allowed tools for builder
ALLOWED_TOOLS="Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit"

# System prompt
SYSTEM_PROMPT="$BUILDER_DIR/SYSTEM_PROMPT.md"
if [[ ! -f "$SYSTEM_PROMPT" ]]; then
    echo "Error: builder system prompt not found: $SYSTEM_PROMPT" >&2
    exit 1
fi

echo "Invoking builder: $BUILDER_AGENT (backend=$AGENT_BACKEND, model=$MODEL)"
echo "Working directory: $WORK_DIR"

# Select the engine. Both speak the same stream-json `result` event, so the
# metadata extraction below is backend-agnostic.
if [[ "$AGENT_BACKEND" == "ollama" ]]; then
    ENGINE=(python3 "$WORKSPACE_ROOT/bin/ollama_runner.py" --mode agent)
else
    ENGINE=(claude)
fi

# Create metadata file for cost extraction
METADATA_FILE=$(mktemp)
trap "rm -f $METADATA_FILE" EXIT

# Run builder
timeout 21600 "${ENGINE[@]}" -p "$PROMPT" \
    --model "$MODEL" \
    --max-turns 200 \
    --system-prompt "$SYSTEM_PROMPT" \
    --allowedTools "$ALLOWED_TOOLS" \
    --output-format "$OUTPUT_FORMAT" \
    --verbose \
    --dangerously-skip-permissions \
    2>/dev/null | tee >(
        # Extract metadata from stream
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

# Extract results
PR_URL=""
SUMMARY=""
if [[ "$OUTPUT_FORMAT" == "stream-json" && -f "$METADATA_FILE" ]]; then
    RESULT_TEXT=$(python3 -c "
import json
try:
    data = json.load(open('$METADATA_FILE'))
    print(data.get('result', ''))
except:
    pass
" 2>/dev/null || echo "")

    # Extract PR URL from output
    PR_URL=$(echo "$RESULT_TEXT" | grep -oP 'https://github.com/[^\s]+/pull/\d+' | head -1 || echo "")
fi

# Post cost to agent server
if [[ -f "$METADATA_FILE" ]]; then
    python3 -c "
import json, os
try:
    data = json.load(open('$METADATA_FILE'))
    cost = data.get('total_cost_usd', 0)
    tokens_in = data.get('usage', {}).get('input_tokens', 0)
    tokens_out = data.get('usage', {}).get('output_tokens', 0)
    duration = data.get('duration_ms', 0)

    import urllib.request
    url = '$AGENT_SERVER/cost'
    payload = json.dumps({
        'agent': '$BUILDER_AGENT',
        'cost_delta': cost,
        'session_total': cost,
        'input_tokens': tokens_in,
        'output_tokens': tokens_out,
        'duration_ms': duration,
    }).encode()

    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('AGENT_SERVER_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'

    req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
    urllib.request.urlopen(req, timeout=5)
except Exception as e:
    pass
" 2>/dev/null || true
fi

# Poke back requester
if [[ -n "${REQUESTER:-}" && -n "$PR_URL" ]]; then
    "$WORKSPACE_ROOT/bin/poke.sh" --agent "$REQUESTER" --source "$BUILDER_AGENT" \
        --reply-channel "$CALLBACK_CHANNEL" \
        "Build complete. PR: $PR_URL"
elif [[ -n "${REQUESTER:-}" ]]; then
    "$WORKSPACE_ROOT/bin/poke.sh" --agent "$REQUESTER" --source "$BUILDER_AGENT" \
        --reply-channel "$CALLBACK_CHANNEL" \
        "Build finished (exit code: $EXIT_CODE). No PR URL found in output."
fi

# Archive brief
if [[ -n "$BRIEF_PATH" && -f "$BRIEF_PATH" ]]; then
    ARCHIVE_DIR="$WORKSPACE_ROOT/inbox/$BUILDER_AGENT/archive"
    mkdir -p "$ARCHIVE_DIR"
    mv "$BRIEF_PATH" "$ARCHIVE_DIR/$(date +%Y-%m-%d)-$(basename "$BRIEF_PATH")"
fi

exit $EXIT_CODE
