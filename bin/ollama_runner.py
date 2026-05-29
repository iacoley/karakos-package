#!/usr/bin/env python3
"""
ollama_runner.py — Minimal native runner for local Ollama-backed sessions.

This is deliberately NOT a reimplementation of Claude Code. When Karakos runs
against a local model we drop the heavy harness (session resume, rich
permission policy, MCP orchestration, real cost accounting) and keep only what
the one-shot call sites actually need:

  * oneshot — a single text completion, no tools.
              Used by summarize-session.py and memory-maintenance.py.
  * agent   — a small bounded tool-calling loop (bash/read/write/edit).
              Used by invoke-builder.sh and invoke-reviewer.sh.

It talks to Ollama's native /api/chat endpoint over stdlib urllib (no extra
deps). In `agent` mode it emits the same newline-delimited stream-json the
Claude CLI produces — an `assistant` event per turn and a single closing
`result` event — so the existing bash callers parse it without changes.

Config (env):
  OLLAMA_HOST    base URL of the Ollama server (default http://localhost:11434)
  OLLAMA_MODEL   default model tag when --model is not supplied (default llama3.1)
  OLLAMA_TIMEOUT per-request HTTP timeout in seconds (default 300)

Local sessions run fully trusted: tools execute without a permission prompt,
matching how the builder/reviewer already run under --dangerously-skip-permissions.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1")
HTTP_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "300"))

# Cap tool output so a runaway command can't blow up the context window.
MAX_TOOL_OUTPUT = 30000


def log(msg: str) -> None:
    """Progress goes to stderr; stdout is reserved for the stream-json protocol."""
    print(f"[ollama-runner] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Ollama transport
# --------------------------------------------------------------------------- #

def _chat(model: str, messages: list, tools: list | None = None) -> dict:
    """One non-streaming /api/chat call. Returns the decoded response dict."""
    payload = {"model": model, "messages": messages, "stream": False}
    if tools:
        payload["tools"] = tools

    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _resolve_system_prompt(value: str | None) -> str:
    """`--system-prompt` may be a literal string or a path. Read the file if it exists."""
    if not value:
        return ""
    p = Path(value)
    if p.is_file():
        try:
            return p.read_text()
        except Exception:
            return value
    return value


# --------------------------------------------------------------------------- #
# Minimal tool set
# --------------------------------------------------------------------------- #

def _truncate(text: str) -> str:
    if len(text) > MAX_TOOL_OUTPUT:
        return text[:MAX_TOOL_OUTPUT] + f"\n... [truncated, {len(text)} chars total]"
    return text


def _tool_bash(args: dict) -> str:
    cmd = args.get("command", "")
    if not cmd:
        return "error: no command provided"
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=600
        )
        out = proc.stdout + (("\n" + proc.stderr) if proc.stderr else "")
        return _truncate(out.strip() or f"(exit {proc.returncode}, no output)")
    except subprocess.TimeoutExpired:
        return "error: command timed out after 600s"
    except Exception as e:
        return f"error: {e}"


def _tool_read_file(args: dict) -> str:
    path = args.get("path", "")
    try:
        return _truncate(Path(path).read_text())
    except Exception as e:
        return f"error: {e}"


def _tool_write_file(args: dict) -> str:
    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"error: {e}"


def _tool_edit_file(args: dict) -> str:
    path = args.get("path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    try:
        p = Path(path)
        text = p.read_text()
        if old not in text:
            return "error: old_string not found in file"
        p.write_text(text.replace(old, new, 1))
        return f"edited {path}"
    except Exception as e:
        return f"error: {e}"


# OpenAI/Ollama function-calling schemas for the tools above.
TOOL_SCHEMAS = {
    "bash": {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its combined stdout/stderr. "
                           "Use for grep, glob/find, git, running tests, and general inspection.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read and return the contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute or relative file path."}},
                "required": ["path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, creating parent directories and overwriting if it exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace the first occurrence of old_string with new_string in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
}

TOOL_IMPLS = {
    "bash": _tool_bash,
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
}


def _select_tools(allowed: str) -> list:
    """Map Claude-style --allowedTools names onto our minimal native tool set.

    Bash/Grep/Glob all collapse onto our single `bash` tool (the model can
    grep/find through it), Read/Write/Edit map one-to-one. Unknown names
    (WebFetch, WebSearch, NotebookEdit, ...) are silently dropped — a minimal
    local runner doesn't pretend to offer them.
    """
    names = {n.strip() for n in (allowed or "").split(",") if n.strip()}
    selected = []
    if names & {"Bash", "Grep", "Glob"}:
        selected.append("bash")
    if "Read" in names:
        selected.append("read_file")
    if "Write" in names:
        selected.append("write_file")
    if "Edit" in names:
        selected.append("edit_file")
    # If the caller named no tools we recognise, fall back to bash so the
    # agent loop is at least functional rather than inert.
    if not selected:
        selected = ["bash"]
    return [TOOL_SCHEMAS[n] for n in selected]


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #

def run_oneshot(prompt: str, model: str | None = None, system: str | None = None) -> tuple[bool, str, dict]:
    """Single completion, no tools. Returns (ok, text, metadata).

    Imported directly by summarize-session.py and memory-maintenance.py.
    """
    model = model or DEFAULT_MODEL
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    start = time.time()
    try:
        resp = _chat(model, messages)
    except urllib.error.URLError as e:
        return False, "", {"error": f"ollama_unreachable: {e}", "duration_ms": (time.time() - start) * 1000}
    except Exception as e:
        return False, "", {"error": str(e), "duration_ms": (time.time() - start) * 1000}

    text = (resp.get("message", {}) or {}).get("content", "") or ""
    meta = {
        "duration_ms": (time.time() - start) * 1000,
        "input_tokens": resp.get("prompt_eval_count", 0),
        "output_tokens": resp.get("eval_count", 0),
        "total_cost_usd": 0.0,
    }
    return True, text.strip(), meta


def run_agent(prompt: str, model: str, system: str, allowed_tools: str, max_turns: int) -> int:
    """Bounded tool-calling loop. Emits stream-json to stdout, returns an exit code."""
    tools = _select_tools(allowed_tools)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    start = time.time()
    final_text = ""
    in_tokens = 0
    out_tokens = 0
    is_error = False

    def emit(event: dict) -> None:
        print(json.dumps(event), flush=True)

    for turn in range(max_turns):
        try:
            resp = _chat(model, messages, tools=tools)
        except Exception as e:
            is_error = True
            final_text = f"ollama request failed: {e}"
            log(final_text)
            break

        in_tokens += resp.get("prompt_eval_count", 0)
        out_tokens += resp.get("eval_count", 0)
        message = resp.get("message", {}) or {}
        content = message.get("content", "") or ""
        tool_calls = message.get("tool_calls", []) or []

        # Mirror the Claude CLI assistant-event shape for the bash callers.
        blocks = []
        if content:
            blocks.append({"type": "text", "text": content})
            final_text = content
        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            blocks.append({"type": "tool_use", "name": fn.get("name", "unknown")})
        if blocks:
            emit({"type": "assistant", "message": {"role": "assistant", "content": blocks}})

        # Keep the assistant turn in history so tool results have context.
        messages.append(message)

        if not tool_calls:
            break  # model produced a final answer

        for tc in tool_calls:
            fn = tc.get("function", {}) or {}
            name = fn.get("name", "")
            raw_args = fn.get("arguments", {})
            args = raw_args if isinstance(raw_args, dict) else _parse_args(raw_args)
            impl = TOOL_IMPLS.get(name)
            log(f"turn {turn + 1}: tool {name} {args!r}")
            result = impl(args) if impl else f"error: unknown tool {name}"
            messages.append({"role": "tool", "content": result})
    else:
        log(f"hit max_turns ({max_turns}) without a final answer")

    emit({
        "type": "result",
        "result": final_text,
        "is_error": is_error,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": in_tokens, "output_tokens": out_tokens},
        "duration_ms": (time.time() - start) * 1000,
    })
    return 1 if is_error else 0


def _parse_args(raw) -> dict:
    """Tool arguments arrive as a dict, but some models emit a JSON string."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# CLI — accepts the Claude-style flags the invoke scripts already pass so the
# call sites only have to swap the executable, not the argument list.
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal Ollama runner")
    parser.add_argument("--mode", choices=["oneshot", "agent"], default="agent")
    parser.add_argument("-p", "--prompt", dest="prompt", default="")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--system-prompt", dest="system_prompt", default="")
    parser.add_argument("--allowedTools", dest="allowed_tools", default="")
    parser.add_argument("--max-turns", dest="max_turns", type=int, default=50)
    # Accepted for drop-in compatibility with the Claude CLI invocation, ignored.
    parser.add_argument("--output-format", default="stream-json")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dangerously-skip-permissions", action="store_true")
    parser.add_argument("--session-id", default="")
    args, _unknown = parser.parse_known_args()

    if not args.prompt and not sys.stdin.isatty():
        args.prompt = sys.stdin.read()

    system = _resolve_system_prompt(args.system_prompt)

    if args.mode == "oneshot":
        ok, text, _meta = run_oneshot(args.prompt, model=args.model, system=system or None)
        if not ok:
            return 1
        print(text)
        return 0

    return run_agent(args.prompt, args.model, system, args.allowed_tools, args.max_turns)


if __name__ == "__main__":
    sys.exit(main())
