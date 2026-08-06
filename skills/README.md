# Skill Authoring Guide

Skills are modular extensions that add tools to the Karakos MCP server. Each skill is self-contained, automatically discovered at startup, and provides one or more tools that agents can invoke.

## Table of Contents

1. [Directory Structure](#directory-structure)
2. [Tool Definition Schema](#tool-definition-schema)
3. [Implementation Scripts](#implementation-scripts)
4. [Resource Access](#resource-access)
5. [Input Validation](#input-validation)
6. [Error Handling](#error-handling)
7. [Testing](#testing)
8. [Complete Walkthrough](#complete-walkthrough)

---

## Directory Structure

Every skill follows this structure:

```
skills/
└── your-skill/
    ├── SKILL.md           # Documentation: actions, params, data locations
    ├── tools.json         # MCP tool schema (required)
    ├── scripts/
    │   ├── tool1.py       # Implementation (Python or Bash)
    │   └── tool2.sh
    └── assets/            # Static data, templates (optional)
        └── template.json
```

### Files Explained

- **`SKILL.md`**: Human-readable documentation for the skill. Describes what the skill does, what tools it provides, and how to use them.
- **`tools.json`**: Machine-readable tool definitions. This is required for the MCP server to discover your skill.
- **`scripts/`**: Implementation scripts. Each tool defined in `tools.json` should have a corresponding script.
- **`assets/`**: Optional directory for static files, templates, or data that your scripts need.

**Important**: Skills are self-contained. No central manifest or registration is needed — the MCP server scans `skills/*/tools.json` at startup.

---

## Tool Definition Schema

The `tools.json` file defines your skill's tools using JSON Schema. Here's the complete format:

```json
{
  "skill_name": "my-skill",
  "version": "1.0.0",
  "description": "Brief description of what this skill does",
  "tools": [
    {
      "name": "my_tool",
      "description": "What this tool does (1-2 sentences)",
      "inputSchema": {
        "type": "object",
        "properties": {
          "action": {
            "type": "string",
            "enum": ["create", "read", "update", "delete"],
            "description": "The operation to perform"
          },
          "content": {
            "type": "string",
            "description": "The content to process"
          },
          "count": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "description": "Number of items to return"
          }
        },
        "required": ["action"]
      }
    }
  ]
}
```

### Key Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `skill_name` | string | Yes | Unique identifier for your skill |
| `version` | string | Yes | Semantic version (e.g., "1.0.0") |
| `description` | string | Yes | Brief skill description |
| `tools` | array | Yes | List of tool definitions (see below) |

### Tool Definition Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Tool name (snake_case, must match script filename) |
| `description` | string | Yes | What the tool does |
| `inputSchema` | object | Yes | JSON Schema defining valid input parameters |

### Input Schema

The `inputSchema` follows [JSON Schema Draft 7](https://json-schema.org/specification-links.html#draft-7) spec. Common patterns:

**String parameter:**
```json
{
  "param_name": {
    "type": "string",
    "description": "What this parameter is for",
    "minLength": 1,
    "maxLength": 500
  }
}
```

**Enum (fixed choices):**
```json
{
  "action": {
    "type": "string",
    "enum": ["create", "read", "update", "delete"],
    "description": "The operation to perform"
  }
}
```

**Integer with constraints:**
```json
{
  "count": {
    "type": "integer",
    "minimum": 1,
    "maximum": 100,
    "description": "Number of items"
  }
}
```

**Boolean:**
```json
{
  "force": {
    "type": "boolean",
    "description": "Force operation even if risky"
  }
}
```

**Array:**
```json
{
  "tags": {
    "type": "array",
    "items": {"type": "string"},
    "description": "List of tags"
  }
}
```

**Required parameters:**
```json
{
  "required": ["action", "content"]
}
```

---

## Implementation Scripts

Scripts can be written in **Python** or **Bash**. The MCP server invokes them with arguments passed via the `TOOL_ARGS` environment variable.

### Python Implementation

**Script naming**: Must match the tool name in `tools.json`. For tool `my_tool`, create `scripts/my_tool.py`.

**Basic template:**

```python
#!/usr/bin/env python3
import json
import os
import sys

def main():
    # Read arguments from environment
    args_json = os.environ.get("TOOL_ARGS", "{}")
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid TOOL_ARGS JSON"}))
        sys.exit(1)

    # Extract parameters
    action = args.get("action")
    content = args.get("content", "")

    # Validate required parameters
    if not action:
        print(json.dumps({"error": "Missing required parameter: action"}))
        sys.exit(1)

    # Implement your logic here
    result = {
        "status": "success",
        "action": action,
        "output": f"Processed: {content}"
    }

    # Return JSON to stdout
    print(json.dumps(result))

if __name__ == "__main__":
    main()
```

### Bash Implementation

**Basic template:**

```bash
#!/usr/bin/env bash
set -euo pipefail

# Read arguments from environment
ARGS="${TOOL_ARGS:-{}}"

# Parse JSON using jq (if available) or python
ACTION=$(echo "$ARGS" | python3 -c "import sys,json; print(json.load(sys.stdin).get('action',''))")

if [ -z "$ACTION" ]; then
    echo '{"error": "Missing required parameter: action"}'
    exit 1
fi

# Implement your logic here
RESULT="Processed action: $ACTION"

# Return JSON
echo "{\"status\": \"success\", \"output\": \"$RESULT\"}"
```

### Script Execution

- **Timeout**: Scripts have a 60-second timeout. If your script needs longer, break it into async jobs.
- **Working directory**: Scripts run from the skill directory (`skills/your-skill/`).
- **Permissions**: Scripts run as the container user (no privilege escalation).
- **Exit code**: Exit 0 for success, non-zero for failure. Always print JSON to stdout.

---

## Resource Access

Scripts have access to environment variables and the workspace root.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `TOOL_ARGS` | JSON string of input parameters |
| `WORKSPACE_ROOT` | Absolute path to workspace (e.g., `/workspace`) |
| N/A | Anthropic auth handled by `claude login` — no API key in env |

### Accessing Workspace Files

```python
import os
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))

# Read from data directory
data_file = WORKSPACE / "data" / "my-skill" / "state.json"
if data_file.exists():
    with open(data_file) as f:
        data = json.load(f)

# Write to data directory
output_file = WORKSPACE / "data" / "my-skill" / "output.json"
output_file.parent.mkdir(parents=True, exist_ok=True)
with open(output_file, 'w') as f:
    json.dump(result, f)
```

### Skill Assets

```python
import os
from pathlib import Path

# Assets are in the same directory as the script
SKILL_DIR = Path(__file__).parent.parent  # scripts/ is one level down
template_file = SKILL_DIR / "assets" / "template.json"

with open(template_file) as f:
    template = json.load(f)
```

---

## Input Validation

The MCP server validates arguments against your `inputSchema` before calling your script. However, you should still validate within your script for:

1. **Semantic validity**: E.g., if `action=delete`, ensure `id` is provided.
2. **Path safety**: If accepting file paths, ensure they don't contain `..` or absolute paths (unless explicitly allowed).
3. **Data constraints**: E.g., if `count` should be > 0, check it even if schema allows 0.

### Path Safety Example

```python
def validate_path(path_str):
    """Ensure path is relative and doesn't escape workspace."""
    if ".." in path_str or path_str.startswith("/"):
        raise ValueError("Invalid path: must be relative and not contain '..'")
    return path_str
```

### String Validation Example

```python
import re

def validate_alphanumeric(value):
    """Ensure value contains only alphanumeric characters."""
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        raise ValueError("Invalid value: must be alphanumeric")
    return value
```

### Payload Size Limits

- **Maximum TOOL_ARGS size**: 64KB (enforced by MCP server)
- If you need to process large files, accept a file path parameter instead of the file content itself

---

## Error Handling

Always handle errors gracefully and return JSON.

### Success Response

```json
{
  "status": "success",
  "output": "Operation completed",
  "data": {
    "key": "value"
  }
}
```

### Error Response

```json
{
  "error": "Descriptive error message",
  "code": "ERROR_CODE"
}
```

### Python Error Handling

```python
def main():
    try:
        args = json.loads(os.environ.get("TOOL_ARGS", "{}"))

        # Your logic here
        result = perform_action(args)

        print(json.dumps({"status": "success", "output": result}))

    except ValueError as e:
        print(json.dumps({"error": f"Validation error: {e}"}))
        sys.exit(1)

    except Exception as e:
        print(json.dumps({"error": f"Unexpected error: {e}"}))
        sys.exit(1)
```

### Never Raise Unhandled Exceptions

Unhandled exceptions will cause the MCP server to log the error and return a generic error to the agent. Always catch and format errors as JSON.

---

## Testing

Before deploying your skill, test it locally.

### Manual Testing

1. **Set environment variables:**

```bash
export WORKSPACE_ROOT=/path/to/workspace
export TOOL_ARGS='{"action": "read", "content": "test"}'
```

2. **Run the script directly:**

```bash
python3 skills/my-skill/scripts/my_tool.py
```

3. **Verify output is valid JSON:**

```bash
python3 skills/my-skill/scripts/my_tool.py | python3 -m json.tool
```

### Test with MCP Server

The MCP server provides a test mode:

```bash
python3 mcp/tools-server.py --test-tool my_tool
```

This will:
1. Load your `tools.json`
2. Validate the schema
3. Prompt for test arguments
4. Invoke your script
5. Display the result

### Unit Testing

Create a `tests/` directory in your skill:

```python
# skills/my-skill/tests/test_my_tool.py
import json
import subprocess
import os

def test_my_tool():
    env = os.environ.copy()
    env["TOOL_ARGS"] = json.dumps({"action": "read", "content": "test"})
    env["WORKSPACE_ROOT"] = "/workspace"

    result = subprocess.run(
        ["python3", "scripts/my_tool.py"],
        env=env,
        capture_output=True,
        text=True,
        cwd="skills/my-skill"
    )

    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["status"] == "success"
```

Run with pytest:

```bash
pytest skills/my-skill/tests/
```

---

## Complete Walkthrough: Hello World Skill

Let's build a complete skill from scratch.

### Step 1: Create Directory Structure

```bash
cd skills
mkdir -p hello-world/scripts
mkdir -p hello-world/assets
```

### Step 2: Write Documentation

Create `skills/hello-world/SKILL.md`:

```markdown
# Hello World Skill

A minimal skill that demonstrates the basic structure.

## Tools

### hello_world

Greets the user with a custom message.

**Parameters:**
- `name` (string, required): The name to greet
- `enthusiastic` (boolean, optional): Add excitement (default: false)

**Example:**
```
hello_world(name="Alice", enthusiastic=true)
```

**Output:**
```json
{
  "status": "success",
  "greeting": "Hello, Alice!!!"
}
```
```

### Step 3: Define the Tool

Create `skills/hello-world/tools.json`:

```json
{
  "skill_name": "hello-world",
  "version": "1.0.0",
  "description": "A minimal example skill",
  "tools": [
    {
      "name": "hello_world",
      "description": "Greet the user with a custom message",
      "inputSchema": {
        "type": "object",
        "properties": {
          "name": {
            "type": "string",
            "description": "The name to greet",
            "minLength": 1
          },
          "enthusiastic": {
            "type": "boolean",
            "description": "Add extra excitement"
          }
        },
        "required": ["name"]
      }
    }
  ]
}
```

### Step 4: Implement the Script

Create `skills/hello-world/scripts/hello_world.py`:

```python
#!/usr/bin/env python3
"""Hello World tool implementation."""

import json
import os
import sys

def main():
    # Read arguments
    args_json = os.environ.get("TOOL_ARGS", "{}")
    try:
        args = json.loads(args_json)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON: {e}"}))
        sys.exit(1)

    # Extract parameters
    name = args.get("name")
    enthusiastic = args.get("enthusiastic", False)

    # Validate
    if not name:
        print(json.dumps({"error": "Missing required parameter: name"}))
        sys.exit(1)

    # Generate greeting
    if enthusiastic:
        greeting = f"Hello, {name}!!!"
    else:
        greeting = f"Hello, {name}."

    # Return result
    result = {
        "status": "success",
        "greeting": greeting
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
```

### Step 5: Make Script Executable

```bash
chmod +x skills/hello-world/scripts/hello_world.py
```

### Step 6: Test the Script

```bash
export TOOL_ARGS='{"name": "Alice", "enthusiastic": true}'
python3 skills/hello-world/scripts/hello_world.py
```

Expected output:
```json
{"status": "success", "greeting": "Hello, Alice!!!"}
```

### Step 7: Test with MCP Server

```bash
python3 mcp/tools-server.py --test-tool hello_world
```

Follow the prompts to test your tool.

### Step 8: Deploy

Your skill is now ready! The MCP server will discover it automatically on the next agent session start.

---

## Best Practices

1. **Keep scripts simple**: Each tool should do one thing well.
2. **Validate early**: Check all inputs before processing.
3. **Return structured data**: Always return JSON with a consistent format.
4. **Log to stderr**: Use stderr for logging, stdout for JSON output only.
5. **Document thoroughly**: Write clear `SKILL.md` docs for users.
6. **Handle timeouts**: If a task takes >60s, return a job ID and provide a separate status-check tool.
7. **No side effects in validation**: Don't modify state while validating inputs.

---

## Advanced Topics

### Calling External APIs

```python
import requests

def fetch_data(url):
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise ValueError(f"API request failed: {e}")
```

### Using Claude API

```python
import subprocess

def ask_claude(prompt):
    result = subprocess.run(
        ["claude", "-p", prompt, "--model", "haiku", "--max-turns", "1"],
        capture_output=True,
        text=True,
        timeout=30
    )
    return result.stdout.strip()
```

### Persistent State

```python
import json
from pathlib import Path

STATE_FILE = Path(os.environ["WORKSPACE_ROOT"]) / "data" / "my-skill" / "state.json"

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)
```

---

## Troubleshooting

### "Tool not found"
- Check that `tools.json` is in the correct location
- Verify the tool name in `tools.json` matches the script filename
- Restart the agent session to force MCP server reload

### "Invalid inputSchema"
- Validate your JSON Schema at [jsonschemavalidator.net](https://www.jsonschemavalidator.net/)
- Check that all required fields are present

### Script times out
- Reduce processing time or break into async tasks
- Return a job ID immediately and provide a status-check tool

### Script returns no output
- Check that you're printing to stdout, not stderr
- Ensure the script exits with code 0 on success

---

## Next Steps

- Explore `skills/hello-world/` for a complete working example
- Read the [MCP Protocol Spec](https://spec.modelcontextprotocol.io/) for advanced features
- Check the [Karakos Architecture](../docs/ARCHITECTURE.md) to understand how skills integrate

Happy building!
