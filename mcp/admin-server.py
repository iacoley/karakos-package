#!/usr/bin/env python3
"""
karakos-admin MCP — JSON-RPC 2.0 over stdin/stdout

Exposes the agent-server's HTTP control surface as MCP tools so an operator
can manage the agent fleet from inside a chat ("spin up a builder named X",
"reload Amos", "how's the queue") instead of clicking through the dashboard.

Tools:
  - list_agents       GET  /agents
  - get_health        GET  /health
  - reload_agent      POST /agents/{name}/reload
  - reset_agent       POST /agents/{name}/reset       (destructive)
  - create_agent      shells bin/create-agent.sh, optionally overwrites
                      SYSTEM_PROMPT.md and reloads
  - get_usage         GET  /usage
  - get_cost          GET  /cost, or /cost/{agent}
  - tail_log          last N lines of logs/<service>.log (redacted)
  - set_effort        POST /agents/{name}/effort      (self-scoped)
  - set_model         POST /agents/{name}/model       (self-scoped)
  - get_queue         GET  /agents/{name}/queue
  - pause_agent       POST /agents/{name}/pause       (self-scoped)
  - resume_agent      POST /agents/{name}/resume      (self-scoped)
  - snapshot_persona  copy agents/<name>/persona/ and SYSTEM_PROMPT.md aside (self-scoped)
  - restore_persona   put a snapshot back; the current state is snapshotted first (self-scoped)
  - diagnose          bin/self-diagnose.py, read-only whole-install checks
  - get_findings      data/health/summary.md

Self-scoped tools (reload, reset, effort, pause, resume) let an agent act on
itself only; a primary agent or an operator (no KARAKOS_AGENT) may act on any.

No SDK dependency — manual JSON-RPC over stdio (initialize, tools/list,
tools/call, notifications/initialized) so we don't add another dep to
requirements.txt.
"""

from __future__ import annotations

import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from _handshake import initialize_result  # noqa: E402
from urllib import error as urlerror
from urllib import request as urlrequest

WORKSPACE_ROOT = Path(workspace_root())
SERVER_URL = os.environ.get("AGENT_SERVER_URL", f"http://127.0.0.1:{os.environ.get('AGENT_SERVER_PORT', '18791')}").rstrip("/")
TOKEN = os.environ.get("AGENT_SERVER_TOKEN", "")
CREATE_AGENT_SH = WORKSPACE_ROOT / "bin" / "create-agent.sh"

NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max", "default")
TAIL_DEFAULT = 40
TAIL_MAX = 200
PAUSE_MIN, PAUSE_MAX = 1, 1440
PROTOCOL_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

def _http(method: str, path: str, body: dict | None = None) -> dict:
    if not TOKEN:
        raise RuntimeError("AGENT_SERVER_TOKEN not set")
    url = f"{SERVER_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urlrequest.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urlerror.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"cannot reach {url}: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# tool implementations
# ---------------------------------------------------------------------------

def tool_list_agents(_args: dict) -> dict:
    return _http("GET", "/agents")


def tool_get_health(_args: dict) -> dict:
    return _http("GET", "/health")


def tool_reload_agent(args: dict) -> dict:
    name = args.get("name")
    if not name or not NAME_RE.match(name):
        raise ValueError(f"invalid agent name: {name!r}")
    _check_scope(name)
    snap = _snapshot_if_changed(name, "before reload")
    result = _http("POST", f"/agents/{name}/reload")
    if snap and isinstance(result, dict):
        result["persona_snapshot"] = snap["snapshot_id"]
    return result


def _snapshot_if_changed(name: str, reason: str):
    """Best effort: a failed snapshot never blocks the reload."""
    try:
        import persona_snapshots
        return persona_snapshots.snapshot_if_changed(WORKSPACE_ROOT, name, reason)
    except Exception:  # noqa: BLE001
        return None


def tool_snapshot_persona(args: dict) -> dict:
    import persona_snapshots
    name = _agent_name(args.get("name"))
    _check_scope(name)
    try:
        return persona_snapshots.snapshot(WORKSPACE_ROOT, name, args.get("reason") or "manual")
    except LookupError as e:
        raise ValueError(str(e)) from e


def tool_restore_persona(args: dict) -> dict:
    import persona_snapshots
    name = _agent_name(args.get("name"))
    _check_scope(name)
    try:
        return persona_snapshots.restore(WORKSPACE_ROOT, name, args.get("snapshot_id"))
    except LookupError as e:
        raise ValueError(str(e)) from e


def tool_reset_agent(args: dict) -> dict:
    """Destructive — wipes the agent's session. The agent loses conversation
    state. Use sparingly."""
    name = args.get("name")
    if not name or not NAME_RE.match(name):
        raise ValueError(f"invalid agent name: {name!r}")
    _check_scope(name)
    return _http("POST", f"/agents/{name}/reset")


def tool_create_agent(args: dict) -> dict:
    name = args.get("name")
    if not name or not NAME_RE.match(name):
        raise ValueError(f"invalid agent name: {name!r}")
    if not CREATE_AGENT_SH.exists():
        raise RuntimeError(f"{CREATE_AGENT_SH} not found")

    cmd = [str(CREATE_AGENT_SH)]
    if (template := args.get("template")):
        cmd += ["--template", template]
    if (model := args.get("model")):
        cmd += ["--model", model]
    if (mt := args.get("max_turns")):
        cmd += ["--max-turns", str(mt)]
    cmd.append(name)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"create-agent.sh timed out: {exc}") from exc

    if proc.returncode != 0:
        raise RuntimeError(f"create-agent.sh failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")

    result: dict = {"created": name, "stdout": proc.stdout.strip()}

    # Optional SYSTEM_PROMPT.md override + reload so the running subprocess
    # picks up the final prompt without a second create cycle.
    sp = args.get("system_prompt")
    if sp:
        sp_path = WORKSPACE_ROOT / "agents" / name / "SYSTEM_PROMPT.md"
        sp_path.parent.mkdir(parents=True, exist_ok=True)
        if (snap := _snapshot_if_changed(name, "before create_agent overwrote SYSTEM_PROMPT.md")):
            result["persona_snapshot"] = snap["snapshot_id"]
        sp_path.write_text(sp)
        result["system_prompt_written"] = str(sp_path)
        try:
            result["reload"] = _http("POST", f"/agents/{name}/reload")
        except RuntimeError as exc:
            result["reload_warning"] = str(exc)

    return result


def _agent_name(value) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        raise ValueError(f"invalid agent name: {value!r}")
    return value


def _caller() -> str:
    """Agent identity of the caller; empty for an operator or CLI."""
    return os.environ.get("KARAKOS_AGENT", "").strip()


def _role_of(agent: str) -> str | None:
    """agents.<agent>.role from config/agents.yaml; None on any read error."""
    try:
        import yaml  # PyYAML, in requirements.txt; a missing module means role unknown
        data = yaml.safe_load((WORKSPACE_ROOT / "config" / "agents.yaml").read_text()) or {}
        role = ((data.get("agents") or {}).get(agent) or {}).get("role")
    except Exception:  # noqa: BLE001 - unreadable or malformed registry: role unknown
        return None
    return role if isinstance(role, str) else None


def _check_scope(name: str) -> None:
    """An agent may manage only itself. Primary agents and operators may manage any."""
    caller = _caller()
    if not caller or caller == name:
        return
    if _role_of(caller) == "primary":
        return
    raise PermissionError(f"{caller} may only manage itself; ask the primary agent or the owner")


def _redact(text: str) -> str:
    try:
        lib = str(WORKSPACE_ROOT / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from redact import redact_text
    except Exception:  # noqa: BLE001 - no redaction available: serve the raw text
        return text
    return redact_text(text)


def tool_get_usage(_args: dict) -> dict:
    return _http("GET", "/usage")


def tool_get_cost(args: dict) -> dict:
    name = args.get("agent")
    if name is not None:
        return _http("GET", f"/cost/{_agent_name(name)}")
    return _http("GET", "/cost")


def tool_tail_log(args: dict) -> dict:
    service = args.get("service")
    if not isinstance(service, str) or not SERVICE_RE.match(service) or ".." in service:
        raise ValueError(f"invalid service name: {service!r}")
    raw = args.get("lines")
    try:
        lines = int(TAIL_DEFAULT if raw is None else raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"lines must be an integer, got {raw!r}") from exc
    lines = max(1, min(TAIL_MAX, lines))

    logs = WORKSPACE_ROOT / "logs"
    path = logs / f"{service}.log"
    if not path.is_file():
        available = sorted(p.stem for p in logs.glob("*.log")) if logs.is_dir() else []
        return {"error": f"no log named {service}", "available": available}

    with path.open(errors="replace") as fh:
        tail = list(deque(fh, maxlen=lines))
    text = _redact("".join(tail))
    return {"service": service, "lines": len(tail), "text": text}


def tool_set_effort(args: dict) -> dict:
    name = _agent_name(args.get("name"))
    level = args.get("level")
    if level not in EFFORT_LEVELS:
        raise ValueError(f"level must be one of {', '.join(EFFORT_LEVELS)}; got {level!r}")
    _check_scope(name)
    return _http("POST", f"/agents/{name}/effort", {"level": level})


def tool_set_model(args: dict) -> dict:
    name = _agent_name(args.get("name"))
    model = args.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model is required (an allowed id from config/models.yaml, or default)")
    _check_scope(name)
    return _http("POST", f"/agents/{name}/model", {"model": model.strip()})


def tool_diagnose(args: dict) -> dict:
    """Read-only. Runs bin/self-diagnose.py's checks in this process."""
    import importlib.util
    path = WORKSPACE_ROOT / "bin" / "self-diagnose.py"
    spec = importlib.util.spec_from_file_location("self_diagnose", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sections = args.get("sections")
    if sections is not None:
        if not isinstance(sections, list) or not all(isinstance(s, str) for s in sections):
            raise ValueError("sections must be a list of section names")
        bad = [s for s in sections if s not in mod.SECTIONS]
        if bad:
            raise ValueError(f"unknown section(s): {', '.join(bad)} (choose from {', '.join(mod.SECTIONS)})")
    findings = mod.diagnose(WORKSPACE_ROOT, sections or None)
    return {"ok": not any(f["severity"] in ("warn", "critical") for f in findings),
            "findings": findings}


def tool_get_queue(args: dict) -> dict:
    name = _agent_name(args.get("name"))
    return _http("GET", f"/agents/{name}/queue")


def tool_pause_agent(args: dict) -> dict:
    name = _agent_name(args.get("name"))
    _check_scope(name)
    minutes = args.get("minutes")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int)
                                or not (PAUSE_MIN <= minutes <= PAUSE_MAX)):
        raise ValueError(f"minutes must be an integer {PAUSE_MIN}..{PAUSE_MAX}, got {minutes!r}")
    return _http("POST", f"/agents/{name}/pause", {"minutes": minutes, "by": _caller() or "agent"})


def tool_resume_agent(args: dict) -> dict:
    name = _agent_name(args.get("name"))
    _check_scope(name)
    return _http("POST", f"/agents/{name}/resume", {})


def tool_get_findings(_args: dict) -> dict:
    path = WORKSPACE_ROOT / "data" / "health" / "summary.md"
    if not path.is_file():
        return {"summary": None, "note": "no findings file yet"}
    return {"summary": path.read_text(errors="replace")}


TOOLS = [
    {
        "name": "list_agents",
        "description": "List all registered agents and their state. No args.",
        "handler": tool_list_agents,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_health",
        "description": "Health snapshot of the agent-server: per-agent state, queue depth, alive/dead, session prefix. No args.",
        "handler": tool_get_health,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reload_agent",
        "description": "Bounce an agent's subprocess while preserving its session. Use after editing SYSTEM_PROMPT.md, persona files, or MCP config.",
        "handler": tool_reload_agent,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Agent name (lowercase, hyphens allowed)"}},
            "required": ["name"],
        },
    },
    {
        "name": "reset_agent",
        "description": "DESTRUCTIVE. Reset an agent's session — wipes conversation state and starts a fresh subprocess. Cannot be undone. Confirm with the user before calling.",
        "handler": tool_reset_agent,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Agent name to reset"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_agent",
        "description": "Scaffold and register a new agent via bin/create-agent.sh. Optional system_prompt overwrites agents/<name>/SYSTEM_PROMPT.md and reloads so the first turn uses the final prompt.",
        "handler": tool_create_agent,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "New agent name (lowercase, hyphens allowed, must start with a letter)"},
                "template": {"type": "string", "description": "Base template (default 'primary'). Examples: primary, monitor, builder, reviewer"},
                "model": {"type": "string", "description": "Claude model: opus, sonnet, haiku (default sonnet)"},
                "max_turns": {"type": "integer", "description": "Max agentic turns per message (default 200)"},
                "system_prompt": {"type": "string", "description": "Override agents/<name>/SYSTEM_PROMPT.md after scaffolding, then reload"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_usage",
        "description": "Rate-limit headroom for every agent (the usage governor). No args.",
        "handler": tool_get_usage,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_cost",
        "description": "Cost summary. No args: every agent. With agent: that agent only.",
        "handler": tool_get_cost,
        "inputSchema": {
            "type": "object",
            "properties": {"agent": {"type": "string", "description": "Agent name (optional)"}},
        },
    },
    {
        "name": "tail_log",
        "description": "Last lines of logs/<service>.log, secrets redacted. Default 40 lines, max 200. Missing service returns the available names.",
        "handler": tool_tail_log,
        "inputSchema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Log name without .log, e.g. relay or agent-server"},
                "lines": {"type": "integer", "description": "Lines from the end (1-200, default 40)"},
            },
            "required": ["service"],
        },
    },
    {
        "name": "set_model",
        "description": "Set an agent's model: an id listed in config/models.yaml, or default to drop the override. Self-scoped: an agent may set only its own; the primary may set any.",
        "handler": tool_set_model,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "model": {"type": "string", "description": "Allowed model id, or default"},
            },
            "required": ["name", "model"],
        },
    },
    {
        "name": "snapshot_persona",
        "description": "Copy agents/<name>/persona/ and SYSTEM_PROMPT.md to data/persona-snapshots/<name>/<id>/ (newest 10 kept). Taken automatically before create_agent overwrites a prompt and before a reload that follows a persona change. Self-scoped.",
        "handler": tool_snapshot_persona,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "reason": {"type": "string", "description": "Why (recorded with the snapshot)"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "restore_persona",
        "description": "Put a persona snapshot back (persona/ and SYSTEM_PROMPT.md). The current state is snapshotted first, so a restore can be undone. Call reload_agent afterwards. Self-scoped.",
        "handler": tool_restore_persona,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "snapshot_id": {"type": "string", "description": "Snapshot id, e.g. 20260101T000000Z"},
            },
            "required": ["name", "snapshot_id"],
        },
    },
    {
        "name": "diagnose",
        "description": "Read-only whole-install diagnosis: agent server health, outbox, findings, heartbeats, protected-path hook, disk, schema stamp, log errors, build-queue backlog and version drift. Returns a ranked list of findings (critical, warn, info). Optional sections limits the checks.",
        "handler": tool_diagnose,
        "inputSchema": {
            "type": "object",
            "properties": {
                "sections": {"type": "array", "items": {"type": "string"},
                             "description": "Subset of: health, findings, heartbeats, hook, disk, schema, logs, buildqueue, version"},
            },
        },
    },
    {
        "name": "set_effort",
        "description": "Set an agent's reasoning effort (low, medium, high, xhigh, max, or default). Self-scoped: an agent may set only its own.",
        "handler": tool_set_effort,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "level": {"type": "string", "enum": list(EFFORT_LEVELS)},
            },
            "required": ["name", "level"],
        },
    },
    {
        "name": "get_queue",
        "description": "What an agent still has to answer: queued messages and their state.",
        "handler": tool_get_queue,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Agent name"}},
            "required": ["name"],
        },
    },
    {
        "name": "pause_agent",
        "description": "Hold an agent's queue; the turn in progress finishes. Optional minutes (1-1440), else until resumed. Self-scoped.",
        "handler": tool_pause_agent,
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Agent name"},
                "minutes": {"type": "integer", "description": "Pause length, 1-1440; omit for open-ended"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "resume_agent",
        "description": "Release an operator or self pause and drain the queue. Self-scoped.",
        "handler": tool_resume_agent,
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Agent name"}},
            "required": ["name"],
        },
    },
    {
        "name": "get_findings",
        "description": "Latest health findings summary (data/health/summary.md). No args.",
        "handler": tool_get_findings,
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC loop
# ---------------------------------------------------------------------------

def _err(req_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "error": {"code": code, "message": message}, "id": req_id}


def _ok(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "result": result, "id": req_id}


def handle_request(req: dict) -> dict | None:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params", {}) or {}

    if method == "initialize":
        return _ok(req_id, initialize_result(params, "karakos-admin"))

    if method == "notifications/initialized":
        return None  # no response for notifications

    if method == "tools/list":
        listed = [{"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]} for t in TOOLS]
        return _ok(req_id, {"tools": listed})

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            return _err(req_id, -32601, f"Unknown tool: {name}")
        try:
            result = tool["handler"](args)
            return _ok(req_id, {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]})
        except (RuntimeError, ValueError, PermissionError) as exc:
            return _err(req_id, -32603, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _err(req_id, -32603, f"unexpected error: {exc}")

    return _err(req_id, -32601, f"Unknown method: {method}")


def main() -> int:
    # Test mode: --test-tool <name> [args_json]
    if len(sys.argv) > 1 and sys.argv[1] == "--test-tool":
        if len(sys.argv) < 3:
            print("Usage: admin-server.py --test-tool <name> [args_json]", file=sys.stderr)
            return 2
        name = sys.argv[2]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        tool = TOOL_BY_NAME.get(name)
        if not tool:
            print(f"unknown tool: {name}", file=sys.stderr)
            return 2
        try:
            print(json.dumps(tool["handler"](args), indent=2))
            return 0
        except (RuntimeError, ValueError, PermissionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # JSON-RPC stdio loop
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps(_err(None, -32700, "Parse error")) + "\n")
            sys.stdout.flush()
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
