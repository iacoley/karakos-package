#!/usr/bin/env python3
"""
Karakos MCP Tool Server — JSON-RPC 2.0 over stdin/stdout

Discovers tools from skills/*/tools.json, validates calls,
dispatches to skill scripts, maintains audit trail.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root, for lib.graph
from _handshake import initialize_result  # noqa: E402

WORKSPACE = Path(os.environ.get("WORKSPACE_ROOT", "/workspace"))
SKILLS_DIR = WORKSPACE / "skills"
HEALTH_FILE = WORKSPACE / "data" / "health" / "mcp-tools.json"
AUDIT_DB_PATH = WORKSPACE / "data" / "mcp-tools-audit.db"

# Maximum payload size for tool arguments
MAX_ARGS_SIZE = 65536

# Agent server, for the ask_user round trip (#101). KARAKOS_AGENT is set by
# bin/agent-server.py on the subprocess this tool server is a child of; it is
# how a question knows whose conversation to appear in.
AGENT_SERVER_PORT = os.environ.get("AGENT_SERVER_PORT", "18791")
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://localhost:{AGENT_SERVER_PORT}")
AGENT_SERVER_TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
KARAKOS_AGENT = os.environ.get("KARAKOS_AGENT", "")
# The shard this process serves (an agent id for a default shard). Runtime state
# is keyed by it, so it is what the server wants in POST /ask.
KARAKOS_SHARD = os.environ.get("KARAKOS_SHARD", "")

# Poll cadence while a question is on screen. Short enough that the agent
# resumes promptly after a click, long enough not to spin.
ASK_POLL_INTERVAL_SEC = float(os.environ.get("ASK_POLL_INTERVAL_SEC", "2.0"))
ASK_DEFAULT_TIMEOUT_SEC = 300

# =============================================================================
# Core Tools (ship by default)
# =============================================================================

CORE_TOOLS = [
    {
        "name": "workspace",
        "description": "System config, agent registry, version info. Actions: status (show workspace info), agents (list agents), config (show system config).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "agents", "config"],
                    "description": "The action to perform"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "session",
        "description": "Session lifecycle. Actions: finalize (ask for a fresh session; the reset happens at the end of this turn and a handoff note you write is given to the new session), load_last (the pending handoff note, if any).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["finalize", "load_last"],
                    "description": "The action to perform"
                },
                "agent": {
                    "type": "string",
                    "description": "Shard to act on. Defaults to the calling shard (KARAKOS_SHARD, else KARAKOS_AGENT)."
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "schedule",
        "description": (
            "Schedule your own future work. This is how a promise like "
            "\"I'll check back in 10 minutes\" becomes a mechanism instead of a "
            "sentence. Actions: create (schedule a message or command for later), "
            "list (show what is pending), cancel (drop a pending item by label). "
            "Scheduled items survive a restart of the container."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "cancel"],
                    "description": "The action to perform"
                },
                "when": {
                    "type": "string",
                    "description": "When to fire: a relative span ('10m', '+2h', '1h30m') or an absolute time ('2026-08-09 09:00')"
                },
                "message": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Message to deliver to yourself at that time (the usual case for a reminder)"
                },
                "command": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Shell command to run at that time, instead of a message"
                },
                "label": {
                    "type": "string",
                    "description": "Short name for this item; required for create and cancel"
                },
                "agent": {
                    "type": "string",
                    "description": "Agent to deliver the message to (default: primary agent)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "memory",
        "description": "Durable graph memory. Actions: write (store one fact, episode or pattern, optionally about a named subject), recall (ranked search over memory with one hop of linked entities; modes auto, keyword, recent), status (counts and embedding state). Deprecated aliases, removed in 2.1: remember (use write), facts (use recall with kinds=[\"fact\"]), recent (use recall with mode=\"recent\").",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write", "recall", "status", "remember", "facts", "recent"],
                    "description": "The action to perform"
                },
                "content": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "What to remember (write; required, non-empty, max 4000 chars)"
                },
                "kind": {
                    "type": "string",
                    "enum": ["fact", "episode", "pattern"],
                    "description": "Observation kind (write; default fact)"
                },
                "subkind": {
                    "type": "string",
                    "description": "Pattern subtype (write; required when kind is pattern)"
                },
                "subject": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Entity the observation is about; created as a topic if absent (write)"
                },
                "importance": {
                    "type": "number",
                    "description": "1-10 (write; default 8 fact, 5 episode, 6 pattern)"
                },
                "confidence": {
                    "type": "number",
                    "description": "0-1 confidence (write; default 0.8)"
                },
                "domain": {
                    "type": "string",
                    "description": "Category (write default 'general'; recall filter)"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Free-form tags (write)"
                },
                "query": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Search text (recall; required unless mode is recent)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (recall; default 10, max 50)"
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Restrict recall to these kinds"
                },
                "entity": {
                    "type": "string",
                    "description": "Restrict recall to observations about this entity name"
                },
                "mode": {
                    "type": "string",
                    "enum": ["auto", "keyword", "recent"],
                    "description": "Recall mode (default auto: hybrid when an embedding model is available, keyword otherwise)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "graph",
        "description": "Link entities in the memory graph. Actions: add_entity (create or update a named entity; returns the existing one on a name or alias match), add_edge (create or reinforce a typed relation between two entities, by name or id).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add_entity", "add_edge"],
                    "description": "The action to perform"
                },
                "name": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Entity name (add_entity; required, max 200)"
                },
                "kind": {
                    "type": "string",
                    "description": "Entity kind: person, agent, place, project, topic, thing or free text (add_entity; default thing, max 32)"
                },
                "summary": {
                    "type": "string",
                    "path_mode": "none",
                    "description": "Short description (add_entity; max 1000; replaces an existing one only when longer)"
                },
                "aliases": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alternative names (add_entity)"
                },
                "src": {
                    "type": "string",
                    "description": "Source entity name or numeric id (add_edge)"
                },
                "dst": {
                    "type": "string",
                    "description": "Target entity name or numeric id (add_edge)"
                },
                "relation": {
                    "type": "string",
                    "description": "Relation name, e.g. works_on (add_edge; a-z 0-9 _ and spaces, max 64)"
                },
                "weight": {
                    "type": "number",
                    "description": "0-5 (add_edge; default 1.0; an existing edge is reinforced instead)"
                },
                "create_missing": {
                    "type": "boolean",
                    "description": "Create unknown src/dst names as 'thing' entities (add_edge; default true)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "discord",
        "description": "Discord server read-only access. Actions: history (view messages), channels (list channels), online (list members).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["history", "channels", "online"],
                    "description": "The action to perform"
                },
                "channel": {
                    "type": "string",
                    "description": "Channel name or ID (required for history)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of messages (default 20, max 50)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "taskboard",
        "description": "Task and todo tracking. Actions: list (show tasks), add (create task), update (modify task), complete (mark done).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "update", "complete"],
                    "description": "The action to perform"
                },
                "title": {
                    "type": "string",
                    "description": "Task title (for add/update)"
                },
                "id": {
                    "type": "string",
                    "description": "Task ID (for update/complete)"
                },
                "status": {
                    "type": "string",
                    "description": "New status (for update)"
                },
                "notes": {
                    "type": "string",
                    "description": "Task notes (for update)"
                },
                "assignee": {
                    "type": "string",
                    "description": "Task assignee (for update)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "vault",
        "description": "Git-backed knowledge vault. Actions: pull (sync from remote), push (sync to remote), status (show git status).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["pull", "push", "status"],
                    "description": "The action to perform"
                },
                "message": {
                    "type": "string",
                    "description": "Commit message (for push)"
                }
            },
            "required": ["action"]
        }
    },
    {
        "name": "ask_user",
        "description": (
            "Put a multiple-choice question to the user and wait for their "
            "answer. Renders in Discord as an embed with one button per "
            "option; returns the option they clicked. This is the only way "
            "to ask a blocking question over this transport — the built-in "
            "AskUserQuestion tool is not available to agents running "
            "headless. Use it for decisions you cannot make yourself; do not "
            "use it for questions you could simply write out in your reply."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to put to the user",
                    "path_mode": "none"
                },
                "options": {
                    "type": "array",
                    "description": "2-10 choices. Each is a string, or an object with 'label' and optional 'description'.",
                    "items": {"type": ["string", "object"]}
                },
                "header": {
                    "type": "string",
                    "description": "Short title for the embed (optional)",
                    "path_mode": "none"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait for an answer (default 300, max 3600)"
                }
            },
            "required": ["question", "options"]
        }
    },
    {
        "name": "buzz",
        "description": (
            "Leave a message for another shard (an agent id or a shard id) and "
            "carry on. Use it for anything the other shard can do later without "
            "you; you will not see a result. The recipient handles it as an "
            "ordinary turn. Buzzes nest at most two deep and at most five per turn."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Agent id or shard id to message",
                    "path_mode": "none"
                },
                "message": {
                    "type": "string",
                    "description": "What the other shard should know or do",
                    "path_mode": "none"
                }
            },
            "required": ["to", "message"]
        }
    },
    {
        "name": "hive_call",
        "description": (
            "Ask another shard (an agent id or a shard id) a question and wait "
            "for its answer. This blocks your turn until the answer arrives; use "
            "it only when you cannot continue without it, otherwise use buzz. "
            "Calls nest at most two deep; a shard cannot call itself or one that "
            "is already waiting on it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Agent id or shard id to ask",
                    "path_mode": "none"
                },
                "question": {
                    "type": "string",
                    "description": "The question; the callee answers in one brief reply",
                    "path_mode": "none"
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds to wait (default 120, clamped to 5..900)"
                }
            },
            "required": ["to", "question"]
        }
    },
]


# =============================================================================
# Audit Database
# =============================================================================

def init_audit_db():
    """Initialize audit trail database."""
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUDIT_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY,
            timestamp TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            args_json TEXT,
            result_size_bytes INTEGER,
            duration_ms REAL,
            success INTEGER,
            error_msg TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name)")
    conn.commit()
    return conn


def log_audit(conn, tool_name, args_json, result_size, duration_ms, success, error_msg=None):
    """Record tool call in audit trail."""
    try:
        conn.execute(
            "INSERT INTO tool_calls (timestamp, tool_name, args_json, result_size_bytes, "
            "duration_ms, success, error_msg) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                tool_name,
                args_json[:1024] if args_json else None,
                result_size,
                duration_ms,
                1 if success else 0,
                error_msg,
            )
        )
        conn.commit()
    except Exception:
        pass


def write_health():
    """Write health heartbeat."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "healthy",
    }))


# =============================================================================
# Skill Discovery
# =============================================================================

def discover_skills() -> list[dict]:
    """Scan skills/*/tools.json for tool definitions.

    This package's skill convention is tools.json + scripts/ (see
    docs/EXTENDING.md). A skill directory that ships only a frontmatter-style
    SKILL.md (the Anthropic Agent Skills convention) has no tools.json for
    this server to read, so it is skipped — but skipped loudly, on stderr,
    naming the file, per issue #84.
    """
    tools = []
    if not SKILLS_DIR.exists():
        return tools

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        tools_file = skill_dir / "tools.json"
        if not tools_file.exists():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                sys.stderr.write(
                    f"Skipping {skill_md}: no tools.json in {skill_dir}. "
                    "This server only loads the tools.json + scripts/ skill "
                    "convention (docs/EXTENDING.md); frontmatter-only "
                    "SKILL.md files are not discovered.\n"
                )
            continue
        try:
            data = json.loads(tools_file.read_text())
            for tool in data.get("tools", []):
                tool["_skill_dir"] = str(skill_dir)
                tools.append(tool)
        except Exception as e:
            sys.stderr.write(f"Error loading {tools_file}: {e}\n")

    return tools


# =============================================================================
# Input Validation
# =============================================================================

def validate_args(args: dict, schema: dict) -> str | None:
    """Basic JSON Schema validation. Returns error message or None."""
    if schema.get("type") != "object":
        return None

    properties = schema.get("properties", {})
    required = schema.get("required", [])

    for field in required:
        if field not in args:
            return f"Missing required field: {field}"

    for key, value in args.items():
        if key not in properties:
            continue
        prop_schema = properties[key]

        # Type check
        expected_type = prop_schema.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"Field '{key}' must be a string"
        elif expected_type == "integer" and not isinstance(value, int):
            return f"Field '{key}' must be an integer"
        elif expected_type == "number" and not isinstance(value, (int, float)):
            return f"Field '{key}' must be a number"
        elif expected_type == "boolean" and not isinstance(value, bool):
            return f"Field '{key}' must be a boolean"

        # Enum check
        if "enum" in prop_schema and value not in prop_schema["enum"]:
            return f"Field '{key}' must be one of: {prop_schema['enum']}"

        # Path safety (reject traversal unless explicitly allowed).
        # `path_mode: "none"` marks a field as prose or a shell command rather
        # than a path: an ellipsis in "Ship it, or wait...?" or "remind me to
        # check the logs..." contains ".." and would otherwise be rejected as
        # traversal, which makes any free-text field unusable.
        #
        # #132 and #101 hit this same wall independently and landed on two
        # different spellings — a `free_text: True` boolean and this
        # `path_mode: "none"`. They are one axis, not two, so they are unified
        # here on path_mode, which main already used for "absolute". Keeping
        # both would mean every future free-text field has to guess which flag
        # this predicate actually reads.
        if isinstance(value, str) and ".." in value:
            if prop_schema.get("path_mode") not in ("absolute", "none"):
                return f"Field '{key}' contains path traversal"

    return None


# =============================================================================
# Tool Dispatch
# =============================================================================

def agent_server_request(method: str, path: str, payload: dict = None,
                         timeout: float = 15.0) -> tuple[int, dict]:
    """One request to the local agent server. Returns (status, body).

    Uses urllib rather than aiohttp on purpose: this server is a synchronous
    stdio JSON-RPC loop with no event loop of its own, and adding an async
    HTTP client here would mean adding a dependency to the one process that
    currently has none beyond the standard library.
    """
    url = f"{AGENT_SERVER_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {AGENT_SERVER_TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except (ValueError, OSError):
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def ask_user(args: dict, sleep=time.sleep, monotonic=time.monotonic) -> dict:
    """Put a question to the user and block until they answer it.

    The blocking is the feature. The agent's turn is suspended inside this
    tool call, so the answer arrives as a tool result in the same turn rather
    than as a fresh message the agent has to correlate itself.

    `sleep`/`monotonic` are injectable so the poll loop is testable without a
    real clock — the test drives the same loop the agent drives, and the
    deadline is real rather than a fixed sleep.
    """
    agent = args.get("agent") or KARAKOS_SHARD or KARAKOS_AGENT
    if not agent:
        return {
            "status": "error",
            "error": "No agent identity (KARAKOS_AGENT unset); cannot route the question",
        }

    timeout = args.get("timeout") or ASK_DEFAULT_TIMEOUT_SEC
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = ASK_DEFAULT_TIMEOUT_SEC

    status, body = agent_server_request("POST", "/ask", {
        "agent": agent,
        "question": args.get("question", ""),
        "options": args.get("options"),
        "header": args.get("header"),
        "timeout": timeout,
    })
    if status != 201:
        return {
            "status": "error",
            "error": body.get("error") or f"agent server returned {status}",
        }

    ask_id = body.get("ask_id")
    deadline = monotonic() + timeout + ASK_POLL_INTERVAL_SEC * 2

    while True:
        poll_status, poll_body = agent_server_request("GET", f"/ask/{ask_id}")
        state = poll_body.get("status")
        if poll_status == 200 and state == "answered":
            return {
                "status": "answered",
                "answer": poll_body.get("answer"),
                "answer_index": poll_body.get("answer_index"),
                "answered_by": poll_body.get("answered_by"),
                "question": poll_body.get("question"),
            }
        if poll_status == 404 or state == "expired":
            # 404 also covers "the agent server restarted while we waited" —
            # either way there is no answer coming and the agent should stop
            # holding the turn open.
            return {
                "status": "timeout",
                "error": "The user did not answer in time; decide without them or ask again.",
            }
        if monotonic() >= deadline:
            return {
                "status": "timeout",
                "error": "Timed out waiting for an answer.",
            }
        sleep(ASK_POLL_INTERVAL_SEC)


def _hive_identity():
    return KARAKOS_SHARD or KARAKOS_AGENT


def _hive_refusal(status: int, body: dict) -> dict:
    if status == 0:
        return {"status": "error", "error": "server_unreachable"}
    out = {"status": "error", "error": body.get("error") or f"agent server returned {status}"}
    if body.get("detail"):
        out["detail"] = body["detail"]
    return out


def buzz(args: dict) -> dict:
    """Leave a message for another shard. Never blocks, never raises."""
    me = _hive_identity()
    if not me:
        return {"status": "error", "error": "no_identity"}
    status, body = agent_server_request("POST", "/hive/buzz", {
        "from": me, "to": args.get("to", ""), "message": args.get("message", "")})
    if status == 202:
        return {"status": "queued", "to": body.get("to"),
                "message_id": body.get("message_id")}
    return _hive_refusal(status, body)


HIVE_POLL_WAIT_SEC = 20
HIVE_UNREACHABLE_LIMIT = 3


def hive_call(args: dict, monotonic=time.monotonic) -> dict:
    """Ask another shard a question and block until it answers.

    Same shape as ask_user: the turn is suspended inside this tool call and the
    answer arrives as the tool result. The server owns every deadline; the
    local guard below only stops a loop whose server went silent."""
    me = _hive_identity()
    if not me:
        return {"status": "error", "error": "no_identity"}
    payload = {"from": me, "to": args.get("to", ""), "question": args.get("question", "")}
    if args.get("timeout") is not None:
        payload["timeout"] = args["timeout"]
    status, body = agent_server_request("POST", "/hive/call", payload)
    if status != 202:
        return _hive_refusal(status, body)
    call_id = body.get("call_id")
    try:
        budget = min(900.0, max(5.0, float(args.get("timeout") or 120)))
    except (TypeError, ValueError):
        budget = 120.0
    give_up = monotonic() + budget + 60.0
    unreachable = 0
    while True:
        status, body = agent_server_request(
            "GET", f"/hive/call/{call_id}?wait={HIVE_POLL_WAIT_SEC}",
            timeout=HIVE_POLL_WAIT_SEC + 10)
        if status == 0:
            unreachable += 1
            if unreachable >= HIVE_UNREACHABLE_LIMIT:
                return {"status": "error", "error": "server_unreachable"}
            time.sleep(1.0)
            continue
        unreachable = 0
        state = body.get("status")
        if status == 200 and state == "answered":
            out = {"status": "answered", "answer": body.get("answer"),
                   "from": body.get("from")}
            if body.get("truncated"):
                out["truncated"] = True
            return out
        if status == 200 and state == "timeout":
            return {"status": "timeout", "error": "The callee did not answer in time; "
                    "decide without it or ask again."}
        if status == 200 and state in ("expired", "error"):
            out = {"status": state, "error": body.get("error")}
            if body.get("detail"):
                out["detail"] = body["detail"]
            return out
        if status == 404:
            return {"status": "error", "error": "unknown_call",
                    "detail": "the server no longer knows this call (restarted?)"}
        if status != 200:
            return _hive_refusal(status, body)
        if monotonic() >= give_up:
            return {"status": "timeout", "error": "Timed out waiting for an answer."}


def handle_core_tool(tool_name: str, args: dict) -> dict:
    """Handle built-in core tools."""

    if tool_name == "ask_user":
        return ask_user(args)

    if tool_name == "buzz":
        return buzz(args)

    if tool_name == "hive_call":
        return hive_call(args)

    if tool_name == "workspace":
        action = args.get("action", "status")
        if action == "status":
            config_path = WORKSPACE / ".karakos" / "config.json"
            config = {}
            if config_path.exists():
                config = json.loads(config_path.read_text())
            return {
                "system_name": config.get("system_name", os.environ.get("SYSTEM_NAME", "Karakos")),
                "version": config.get("version", "1.0.0"),
                "owner": config.get("owner_name", os.environ.get("OWNER_NAME", "User")),
                "workspace": str(WORKSPACE),
            }
        elif action == "agents":
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
            import registry as agent_registry
            try:
                return agent_registry.load_registry(WORKSPACE).legacy_view()
            except agent_registry.RegistryError as e:
                return {"agents": {}, "error": str(e)}
        elif action == "config":
            config_path = WORKSPACE / ".karakos" / "config.json"
            if config_path.exists():
                return json.loads(config_path.read_text())
            return {}

    elif tool_name == "session":
        action = args.get("action", "load_last")
        if action == "finalize":
            # "I want a fresh session." The server resets this shard at the end
            # of the current turn, after a handoff note written by this session.
            # Identity is the shard id the server set on the agent subprocess
            # (KARAKOS_SHARD, else KARAKOS_AGENT), the same source ask_user uses.
            shard = (args.get("agent") or KARAKOS_SHARD or KARAKOS_AGENT).strip()
            if not shard:
                return {"error": "No agent identity (KARAKOS_AGENT unset); "
                                 "cannot request a fresh session"}
            status, body = agent_server_request(
                "POST", f"/agents/{shard}/session/finalize", {}, timeout=10)
            if status != 200:
                return {"status": "error", "agent": shard,
                        "error": body.get("error") or f"agent server returned {status}"}
            return body
        elif action == "load_last":
            # The pending handoff note for this shard, if one is on disk.
            shard = (args.get("agent") or KARAKOS_SHARD or KARAKOS_AGENT).strip()
            if not shard:
                return {"error": "No agent identity (KARAKOS_AGENT unset); "
                                 "cannot load a session summary"}
            if "/" in shard or "\\" in shard or shard.startswith("."):
                return {"error": f"Invalid agent name: {shard!r}"}
            path = WORKSPACE / "data" / "handoff" / f"{shard}.md"
            if path.is_file():
                age_hours = (time.time() - path.stat().st_mtime) / 3600
                return {"status": "success", "summary": path.read_text(),
                        "age_hours": round(age_hours, 1), "path": str(path)}
            return {"status": "not_found"}

    elif tool_name == "schedule":
        # Shells out to bin/oneshot.py rather than importing it, for the same
        # reason skill tools are subprocesses: this server must not inherit a
        # scheduling bug as an unhandled exception in its JSON-RPC loop.
        action = args.get("action", "list")
        oneshot_bin = WORKSPACE / "bin" / "oneshot.py"
        if not oneshot_bin.exists():
            return {"error": f"Scheduler primitive not found: {oneshot_bin}"}

        if action == "create":
            label = args.get("label", "").strip()
            when = args.get("when", "").strip()
            if not label:
                return {"error": "A label is required to create a scheduled item"}
            if not when:
                return {"error": "A 'when' is required (e.g. '10m' or '2026-08-09 09:00')"}
            if not args.get("message") and not args.get("command"):
                return {"error": "Provide a message to deliver or a command to run"}
            cmd = ["python3", str(oneshot_bin), "--json", "schedule",
                   "--label", label, "--when", when]
            if args.get("message"):
                cmd += ["--message", args["message"]]
            if args.get("command"):
                cmd += ["--command", args["command"]]
            if args.get("agent"):
                cmd += ["--agent", args["agent"]]
        elif action == "list":
            cmd = ["python3", str(oneshot_bin), "--json", "list"]
        elif action == "cancel":
            label = args.get("label", "").strip()
            if not label:
                return {"error": "A label is required to cancel a scheduled item"}
            cmd = ["python3", str(oneshot_bin), "--json", "cancel", label]
        else:
            return {"error": f"Unknown schedule action: {action}"}

        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=30, cwd=str(WORKSPACE))
        except subprocess.TimeoutExpired:
            return {"error": "Scheduler timed out"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"error": result.stderr.strip() or "Scheduler produced no output"}

    elif tool_name in ("memory", "graph"):
        # One short-lived connection per call group, opened without creating
        # anything: a missing graph is a "run karakos migrate" error, never a
        # silently created empty database.
        from lib.graph import tools as graph_tools
        from lib.graph.schema import GraphNotInitialised
        from lib.graph.store import open_graph
        try:
            store = open_graph(WORKSPACE / "data", create=False)
        except GraphNotInitialised:
            return {"error": graph_tools.NOT_INITIALISED}
        fn = graph_tools.memory_tool if tool_name == "memory" else graph_tools.graph_tool
        return fn(args, store, KARAKOS_AGENT)

    elif tool_name == "discord":
        action = args.get("action", "channels")
        if action == "channels":
            channels_path = WORKSPACE / "config" / "channels.json"
            if channels_path.exists():
                return json.loads(channels_path.read_text())
            return {"channels": {}}
        elif action == "history":
            channel = args.get("channel", "general")
            limit = min(args.get("limit", 20), 50)
            # Read from JSONL capture
            today = datetime.now().strftime("%Y-%m-%d")
            log_path = WORKSPACE / "data" / "messages" / f"messages-{today}.jsonl"
            if not log_path.exists():
                return {"messages": [], "channel": channel}
            messages = []
            for line in log_path.read_text().strip().split("\n"):
                try:
                    msg = json.loads(line)
                    if msg.get("channel_name") == channel:
                        messages.append({
                            "ts": msg.get("ts", ""),
                            "author": msg.get("author_name", ""),
                            "content": msg.get("content", "")[:500],
                            "is_bot": msg.get("is_bot", False),
                        })
                except json.JSONDecodeError:
                    continue
            return {"messages": messages[-limit:], "channel": channel}
        elif action == "online":
            return {"error": "Online member list requires Discord API access"}

    elif tool_name == "taskboard":
        # Simple file-based task tracking
        tasks_file = WORKSPACE / "data" / "taskboard.json"
        action = args.get("action", "list")

        tasks = []
        if tasks_file.exists():
            tasks = json.loads(tasks_file.read_text()).get("tasks", [])

        if action == "list":
            return {"tasks": tasks}
        elif action == "add":
            task = {
                "id": f"task-{int(time.time())}",
                "title": args.get("title", "Untitled"),
                "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            tasks.append(task)
            tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
            return {"task": task}
        elif action == "update":
            task_id = args.get("id", "")
            for task in tasks:
                if task["id"] == task_id:
                    changed = {k: args[k] for k in ("status", "title", "notes", "assignee")
                               if args.get(k) is not None}
                    if not changed:
                        return {"error": "Nothing to update: give status, title, notes or assignee"}
                    task.update(changed)
                    task["updated_at"] = datetime.now(timezone.utc).isoformat()
                    tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
                    return {"task": task}
            return {"error": f"Task not found: {task_id}"}
        elif action == "complete":
            task_id = args.get("id", "")
            for task in tasks:
                if task["id"] == task_id:
                    task["status"] = "done"
                    task["completed_at"] = datetime.now(timezone.utc).isoformat()
                    tasks_file.write_text(json.dumps({"tasks": tasks}, indent=2))
                    return {"task": task}
            return {"error": f"Task not found: {task_id}"}

    elif tool_name == "vault":
        action = args.get("action", "status")
        vault_dir = WORKSPACE / "vault"
        if not vault_dir.exists():
            return {"error": "Vault directory not found. Create it with: git clone <repo> vault/"}

        if action == "status":
            result = subprocess.run(
                ["git", "status", "--short"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"status": result.stdout.strip(), "clean": not result.stdout.strip()}
        elif action == "pull":
            result = subprocess.run(
                ["git", "pull"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}
        elif action == "push":
            msg = args.get("message", "Auto-commit from vault tool")
            subprocess.run(["git", "add", "-A"], cwd=str(vault_dir))
            subprocess.run(["git", "commit", "-m", msg], cwd=str(vault_dir))
            result = subprocess.run(
                ["git", "push"], capture_output=True, text=True, cwd=str(vault_dir)
            )
            return {"output": result.stdout.strip(), "success": result.returncode == 0}

    return {"error": f"Unknown tool or action: {tool_name}"}


def handle_skill_tool(tool: dict, args: dict) -> dict:
    """Dispatch to a skill script."""
    skill_dir = Path(tool.get("_skill_dir", ""))
    scripts_dir = skill_dir / "scripts"

    # Find the implementation script
    script = None
    for ext in [".py", ".sh"]:
        candidate = scripts_dir / f"{tool['name']}{ext}"
        if candidate.exists():
            script = candidate
            break
    # Also check for a main script
    if not script:
        for ext in [".py", ".sh"]:
            candidate = scripts_dir / f"main{ext}"
            if candidate.exists():
                script = candidate
                break

    if not script:
        return {"error": f"No implementation script found for tool '{tool['name']}'"}

    # Execute skill script
    try:
        env = os.environ.copy()
        env["WORKSPACE_ROOT"] = str(WORKSPACE)
        env["TOOL_ARGS"] = json.dumps(args)

        if script.suffix == ".py":
            cmd = ["python3", str(script)]
        else:
            cmd = ["bash", str(script)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            cwd=str(skill_dir), env=env
        )

        if result.returncode != 0:
            return {"error": result.stderr.strip() or f"Script exited with code {result.returncode}"}

        # Try to parse as JSON
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"output": result.stdout.strip()}

    except subprocess.TimeoutExpired:
        return {"error": "Skill script timed out (60s limit)"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# JSON-RPC Server
# =============================================================================

def main():
    """Run MCP tool server (JSON-RPC 2.0 over stdin/stdout)."""
    audit_conn = init_audit_db()

    # Discover all tools
    skill_tools = discover_skills()
    all_tools = CORE_TOOLS + skill_tools

    # Build tool registry
    tool_registry = {}
    for tool in all_tools:
        tool_registry[tool["name"]] = tool

    # Test mode
    if len(sys.argv) > 1 and sys.argv[1] == "--test-tool":
        if len(sys.argv) < 3:
            print("Usage: tools-server.py --test-tool <name> [args_json]")
            sys.exit(1)
        tool_name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        if tool_name not in tool_registry:
            print(f"Unknown tool: {tool_name}")
            print(f"Available: {list(tool_registry.keys())}")
            sys.exit(1)
        result = handle_core_tool(tool_name, args) if tool_name in [t["name"] for t in CORE_TOOLS] else handle_skill_tool(tool_registry[tool_name], args)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    # Main JSON-RPC loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": None,
            }
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue

        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {})

        if method == "initialize":
            response = {
                "jsonrpc": "2.0",
                "result": initialize_result(params, "karakos-tools"),
                "id": req_id,
            }

        elif method == "ping":
            response = {"jsonrpc": "2.0", "result": {}, "id": req_id}

        elif method.startswith("notifications/"):
            continue  # notifications get no reply

        elif method == "tools/list":
            # Return all registered tools
            tools_list = []
            for tool in all_tools:
                tools_list.append({
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                })
            response = {
                "jsonrpc": "2.0",
                "result": {"tools": tools_list},
                "id": req_id,
            }

        elif method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments", {})
            args_json = json.dumps(args)

            if len(args_json) > MAX_ARGS_SIZE:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": f"Arguments too large ({len(args_json)} > {MAX_ARGS_SIZE})"},
                    "id": req_id,
                }
            elif tool_name not in tool_registry:
                response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    "id": req_id,
                }
            else:
                tool = tool_registry[tool_name]

                # Validate args
                validation_error = validate_args(args, tool.get("inputSchema", {}))
                if validation_error:
                    response = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32602, "message": validation_error},
                        "id": req_id,
                    }
                else:
                    start = time.time()
                    try:
                        if tool_name in [t["name"] for t in CORE_TOOLS]:
                            result = handle_core_tool(tool_name, args)
                        else:
                            result = handle_skill_tool(tool, args)

                        duration_ms = (time.time() - start) * 1000
                        result_json = json.dumps(result)
                        log_audit(audit_conn, tool_name, args_json, len(result_json), duration_ms, True)
                        write_health()

                        response = {
                            "jsonrpc": "2.0",
                            "result": {"content": [{"type": "text", "text": result_json}]},
                            "id": req_id,
                        }
                    except Exception as e:
                        duration_ms = (time.time() - start) * 1000
                        log_audit(audit_conn, tool_name, args_json, 0, duration_ms, False, str(e))

                        response = {
                            "jsonrpc": "2.0",
                            "error": {"code": -32603, "message": str(e)},
                            "id": req_id,
                        }
        elif "id" not in request:
            continue  # unknown notification: ignore
        else:
            response = {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
                "id": req_id,
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
