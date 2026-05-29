# Running on local Ollama

Karakos can route some agent work through a **local [Ollama](https://ollama.com)
server** instead of Claude, keeping that traffic on your own hardware with no
API spend.

## What runs locally vs. on Claude

This is intentionally a **minimal** local mode — when you go local you give up
the heavy Claude Code harness (session resume, MCP orchestration, rich
permission policy, real cost accounting) in exchange for self-hosting. Only the
one-shot, request→response call sites are wired for Ollama:

| Task | Script | Backend-aware? |
|------|--------|----------------|
| Builder (code gen) | `bin/invoke-builder.sh` | ✅ |
| Reviewer (code review) | `bin/invoke-reviewer.sh` | ✅ |
| Session summarizer | `bin/summarize-session.py` | ✅ |
| Memory importance scoring | `bin/memory-maintenance.py` | ✅ |
| **Primary / Relay (always-on chat)** | `bin/agent-server.py` | ❌ — still Claude |

The always-on conversational agents stay on Claude because they depend on the
persistent stream-json subprocess, live tool/MCP loop, and session lifecycle
that the Claude Code CLI provides. Embeddings are already fully local
(`fastembed`) and need nothing here.

## The native runner

`bin/ollama_runner.py` is a small, dependency-free (stdlib `urllib`) shim that
talks to Ollama's `/api/chat` endpoint. It has two modes:

- **`oneshot`** — a single completion, no tools (summarizer, memory scoring).
- **`agent`** — a bounded tool-calling loop offering a minimal tool set:
  `bash`, `read_file`, `write_file`, `edit_file`. Claude's `--allowedTools`
  names are mapped onto these (`Bash`/`Grep`/`Glob` → `bash`; `Read`/`Write`/
  `Edit` map one-to-one; everything else is dropped).

In `agent` mode it emits the same newline-delimited stream-json `result` event
the Claude CLI produces, so `invoke-builder.sh` / `invoke-reviewer.sh` parse it
without changes. Local sessions run fully trusted — tools execute without a
permission prompt, matching the existing `--dangerously-skip-permissions` runs.

## Enabling it

### 1. Run Ollama and pull a model

On the host (or any reachable machine):

```bash
ollama serve                 # if not already running as a service
ollama pull llama3.1         # pick a model that supports tool calling
```

A model with solid **function-calling** support is strongly recommended for the
builder/reviewer loop (e.g. `llama3.1`, `qwen2.5-coder`, `mistral-nemo`). Weaker
models may fail to call tools reliably — this is the main limitation of local
mode.

### 2. Configure Karakos

Either answer **yes** at the "Local Model Backend" step of `setup.sh`, or set
these in `config/.env`:

```bash
AGENT_BACKEND=ollama
OLLAMA_HOST=http://host.docker.internal:11434   # Ollama running on the host
OLLAMA_MODEL=llama3.1
# OLLAMA_TIMEOUT=300                             # per-request HTTP timeout (s)
```

The container reaches a host-run Ollama via `host.docker.internal`, which
`config/docker-compose.yml` maps to the host gateway (needed on Linux; Docker
Desktop provides it automatically). If Ollama runs in its own container or on
another host, point `OLLAMA_HOST` at it directly.

### 3. Restart

```bash
docker compose -f config/docker-compose.yml up -d
```

## Per-invocation override

The backend is read from the `AGENT_BACKEND` env var, so you can flip a single
run without changing `.env`:

```bash
AGENT_BACKEND=ollama OLLAMA_MODEL=qwen2.5-coder ./bin/invoke-builder.sh spec.md
```

You can also drive the runner directly:

```bash
# one-shot text
echo "Summarize: ..." | python3 bin/ollama_runner.py --mode oneshot --model llama3.1

# agentic loop
python3 bin/ollama_runner.py --mode agent -p "Find and fix the bug in foo.py" \
    --allowedTools "Bash,Read,Write,Edit" --max-turns 20
```

## Notes & caveats

- **Cost tracking** reports `$0` for local runs (there is no API spend). The
  cost-limit checks therefore never trip for Ollama-backed tasks.
- **Tool fidelity** depends entirely on the local model. If the builder seems to
  "talk about" editing files instead of calling `write_file`/`edit_file`, switch
  to a stronger tool-calling model.
- This mode does **not** connect the MCP servers (`karakos-admin`,
  `system-tools`). The minimal tool set is `bash`/`read`/`write`/`edit` only.
