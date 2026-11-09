"""ITHILIEN phase 1: the programmatic checks named in tests/capabilities/manifest.yaml.

Each check is `fn(workspace) -> (ok, detail)`. `workspace` is the install's root
(`paths.workspace_path()`). A check never raises: `_safe` turns an exception into
`(False, "<Type>: <message>")`. Every write goes to a temp directory, and nothing
calls a model. The one HTTP call that looks like Discord goes to a local stub.

Process-starting checks use `bin/self-test.py`'s `Server` helper, which has a
20 s reply timeout. The two `bin/` scripts are loaded with importlib and are not
edited. `bin/agent-check.py` runs these.
"""
from __future__ import annotations

import ast
import contextlib
import functools
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

LIB = Path(__file__).resolve().parent
ROOT = LIB.parent
for _p in (str(LIB), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import discord_embeds  # noqa: E402
import job_registry  # noqa: E402
import outbox  # noqa: E402
import registry  # noqa: E402
from lib.graph import embed as graph_embed  # noqa: E402
from lib.graph import tools as graph_tools  # noqa: E402
from lib.graph.store import open_graph  # noqa: E402
from lib.migrate import SCHEMA_VERSION  # noqa: E402
from lib.migrate import guard as migrate_guard  # noqa: E402

EXPECTED_TOOLS = ROOT / "tests" / "capabilities" / "expected_tools.json"
PROBE_TIMEOUT_S = 2
STUB_OWNER_ID = "1001"
STUB_CHANNEL_ID = "2002"


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(workspace):
        try:
            ok, detail = fn(workspace)
            return bool(ok), str(detail)
        except Exception as e:  # noqa: BLE001 - a check reports, it does not raise
            return False, f"{type(e).__name__}: {e}"
    return wrapper


@contextlib.contextmanager
def _sandbox_env(**kv):
    """Set env vars for the block; a None value unsets. Restored on exit."""
    saved = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _load_script(mod_name: str, rel: str):
    """Import a hyphenated script under bin/ without running its __main__."""
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _expected_tools() -> list:
    return json.loads(EXPECTED_TOOLS.read_text())["tools"]


def _list_tools() -> dict:
    """{server_script: {tool_name: tool_dict}} from a live tools/list on each server.

    The servers start in a temp workspace: listing tools must not write health files
    or audit rows into the install."""
    self_test = _load_script("self_test", "bin/self-test.py")
    out = {}
    with tempfile.TemporaryDirectory() as td, _sandbox_env(KARAKOS_HOME=td, WORKSPACE_ROOT=td):
        for script in sorted({t["server"] for t in _expected_tools()}):
            srv = self_test.Server(script, Path(td))
            try:
                srv.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                        "clientInfo": {"name": "agent-check", "version": "1"}})
                out[script] = {t["name"]: t for t in srv.call("tools/list").get("tools", [])}
            finally:
                srv.close()
    return out


def _required(tool: dict) -> list:
    return sorted((tool.get("inputSchema") or {}).get("required") or [])


# ---- MCP surface -----------------------------------------------------------------

@_safe
def mcp_tools_present(workspace):
    expected = _expected_tools()
    live = _list_tools()
    missing = [f"{t['server']}:{t['name']}" for t in expected
               if t["name"] not in live.get(t["server"], {})]
    if missing:
        return False, "missing: " + ", ".join(missing)
    return True, f"{len(expected)} tools present across {len(live)} servers"


@_safe
def mcp_tool_schemas(workspace):
    expected = _expected_tools()
    live = _list_tools()
    bad = []
    for t in expected:
        tool = live.get(t["server"], {}).get(t["name"])
        if tool is None:
            continue  # reported by mcp_tools_present
        have = _required(tool)
        if have != t["required"]:
            bad.append(f"{t['name']}: required {have}, expected {t['required']}")
    if bad:
        return False, "; ".join(bad)
    return True, f"required arguments match for {len(expected)} tools"


# ---- hooks -----------------------------------------------------------------------

@_safe
def hooks_installed(workspace):
    """The scripts bin/hooks-sync.py wires, given the flags in config/hooks.json.

    config/hooks.json names only flags, not scripts, so the wired set comes from
    hooks-sync's desired(). The file is read here and never written (load_hooks_config
    creates it when absent)."""
    hs = _load_script("hooks_sync", "bin/hooks-sync.py")
    cfg = dict(hs.DEFAULTS)
    cfg_path = ROOT / "config" / "hooks.json"
    if cfg_path.is_file():
        cfg.update(json.loads(cfg_path.read_text()))
    names = set()
    for groups in hs.desired(cfg).values():
        for group in groups:
            for hook in group["hooks"]:
                cmd = hook["command"]
                names.add(cmd.split("system/hooks/", 1)[1])
    hooks_dir = ROOT / "system" / "hooks"
    missing = [n for n in sorted(names) if not (hooks_dir / n).is_file()]
    not_exec = [n for n in sorted(names) if (hooks_dir / n).is_file()
                and not os.access(hooks_dir / n, os.X_OK)]
    if missing or not_exec:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if not_exec:
            parts.append("not executable: " + ", ".join(not_exec))
        return False, "; ".join(parts)
    return True, f"{len(names)} hook scripts present and executable"


# ---- scheduler -------------------------------------------------------------------

def _defines(path: Path, fn: str) -> bool:
    tree = ast.parse(path.read_text())
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == fn
               for n in tree.body)


@_safe
def scheduler_jobs(workspace):
    """Every job-table entry is registered by the scheduler's own apply_schedule, and
    each runnable target exists. Registers into a throwaway schedule.Scheduler, so
    nothing runs and nothing is written."""
    import schedule
    jobs = [j for j in job_registry.load_jobs(workspace) if j.kind == "job"]
    sched = schedule.Scheduler()
    for j in jobs:
        job_registry.apply_schedule(sched, j, lambda: None)
    registered = {t for s in sched.jobs for t in s.tags}
    missing = [j.name for j in jobs if j.name not in registered]
    problems = []
    if "ask-poll" not in {j.name for j in jobs}:
        problems.append("ask-poll not in the job table")
    for j in jobs:
        if isinstance(j.run, list):
            for arg in j.run:
                if arg.endswith(".py") and "/" in arg and not (ROOT / arg).is_file():
                    problems.append(f"{j.name}: {arg} not found")
        elif isinstance(j.run, str):
            mod, _, fn = j.run.partition(":")
            if mod == "scheduler":
                if fn not in ("heartbeat-primary", "heartbeat-monitor") and \
                        not _defines(ROOT / "bin" / "scheduler.py", fn):
                    problems.append(f"{j.name}: scheduler.{fn} not defined")
            else:
                spec = importlib.util.find_spec(mod)
                if spec is None or not spec.origin or not _defines(Path(spec.origin), fn):
                    problems.append(f"{j.name}: {mod}:{fn} not found")
    if missing or problems:
        out = []
        if missing:
            out.append("not registered: " + ", ".join(missing))
        out.extend(problems)
        return False, "; ".join(out)
    return True, f"{len(jobs)} jobs registered, ask-poll included"


# ---- durable stores --------------------------------------------------------------

@_safe
def outbox_roundtrip(workspace):
    with tempfile.TemporaryDirectory() as td:
        conn = outbox.open_store(Path(td) / "outbox.db")
        try:
            rid, _ = outbox.enqueue(conn, "agent-check", STUB_CHANNEL_ID, "agent-check ping")
            before = outbox.get(conn, rid)["status"]
            claimed = [r["id"] for r in outbox.claim_due(conn)]
            outbox.mark_delivered(conn, rid)
            after = outbox.get(conn, rid)["status"]
        finally:
            conn.close()
    if before != "pending" or claimed != [rid] or after != "delivered":
        return False, f"{rid}: {before} -> claimed {claimed} -> {after}"
    return True, f"{rid}: pending -> sending -> delivered"


@_safe
def memory_roundtrip(workspace):
    marker = f"agent-check marker {uuid.uuid4().hex[:12]}"
    with tempfile.TemporaryDirectory() as td, \
            _sandbox_env(KARAKOS_SEMANTIC_RECALL="0", KARAKOS_HOME=td, WORKSPACE_ROOT=td):
        graph_embed._reset()
        try:
            store = open_graph(td, create=True)
            wrote = graph_tools.memory_tool({"action": "write", "content": marker, "kind": "fact"},
                                            store, "agent-check")
            if wrote.get("status") != "ok":
                return False, f"write refused: {wrote}"
            got = graph_tools.memory_tool({"action": "recall", "query": marker},
                                          store, "agent-check")
            texts = [r.get("text") for r in got.get("results", [])]
        finally:
            graph_embed._reset()
    if marker not in texts:
        return False, f"recall did not return the written fact ({len(texts)} results)"
    return True, "fact written and recalled from a temp graph"


class _DiscordStub(http.server.BaseHTTPRequestHandler):
    """Answers the Discord REST calls ask.py makes, and records them."""
    seen: list = []

    def log_message(self, *args):
        pass

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, status, obj=None):
        raw = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        if obj is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_POST(self):
        _DiscordStub.seen.append(("POST", self.path, self._body()))
        self._send(200, {"id": "9000001"})

    def do_PATCH(self):
        _DiscordStub.seen.append(("PATCH", self.path, self._body()))
        self._send(200, {})

    def do_PUT(self):
        _DiscordStub.seen.append(("PUT", self.path, self._body()))
        self._send(204)


@_safe
def ask_roundtrip(workspace):
    """bin/ask.py: post an ask (Discord REST to a local stub), answer as an owner,
    check the stored status and the on_answer route. The wake is recorded, not run."""
    ask_mod = _load_script("ask_check", "bin/ask.py")
    _DiscordStub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _DiscordStub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            with _sandbox_env(KARAKOS_HOME=td, OWNER_DISCORD_ID=STUB_OWNER_ID,
                              ASK_EXTRA_OWNER_IDS=None, KARAKOS_SHARD=None,
                              KARAKOS_AGENT="agent-check", DISCORD_BOT_TOKEN="stub-token",
                              DISCORD_BOT_TOKEN_PRIMARY="stub-token"):
                ask_mod.ROOT = base
                ask_mod.ASK_DIR = base / "data" / "asks"
                ask_mod.LOG = base / "logs" / "ask.jsonl"
                ask_mod.API = f"http://127.0.0.1:{srv.server_address[1]}/api/v10"
                spec = base / "spec.json"
                spec.write_text(json.dumps({
                    "channel": STUB_CHANNEL_ID, "title": "agent-check", "problem": "stub",
                    "options": [{"label": "Yes"}, {"label": "No"}], "recommendation": 1}))
                ask_id = ask_mod.cmd_post(str(spec))
                route = ask_mod.load(ask_id)["on_answer"]
                woken = []
                ask_mod.wake = lambda a: (woken.append(a["on_answer"]) or True)
                outcome = ask_mod.cmd_answer(ask_id, 0, STUB_OWNER_ID)
                stored = ask_mod.load(ask_id)
    finally:
        srv.shutdown()
        srv.server_close()
    posted = [s for s in _DiscordStub.seen if s[0] == "POST" and s[1].endswith("/messages")]
    if len(posted) != 1:
        return False, f"expected one Discord post to the stub, saw {len(posted)}"
    if outcome != "answered" or stored["status"] != "answered":
        return False, f"answer outcome {outcome!r}, stored status {stored['status']!r}"
    if route != {"type": "poke", "agent": "agent-check"} or woken != [route]:
        return False, f"on_answer route {route} not the one woken ({woken})"
    return True, f"{ask_id} posted, answered, route {route['type']}:{route['agent']} built"


# ---- rendering and endpoints -----------------------------------------------------

@_safe
def embed_parse(workspace):
    good = ('Build finished.\n```embed\n{"title": "Build 42", "color": 3066993, '
            '"fields": [{"name": "Tests", "value": "pass", "inline": true}]}\n```\nDone.')
    text, embeds = discord_embeds.split_reply(good)
    if len(embeds) != 1 or embeds[0].get("title") != "Build 42":
        return False, f"valid block gave {len(embeds)} embeds"
    if "```" in text or "Build finished." not in text or "Done." not in text:
        return False, f"text after a valid block not kept as content: {text!r}"
    bad = "before\n```embed\n{not json\n```\nafter"
    btext, bembeds = discord_embeds.split_reply(bad)
    if bembeds or btext != bad:
        return False, "bad JSON did not stay as plain text"
    return True, "valid block became one embed; bad JSON left as text"


def _probe(url: str, token: str):
    """(status, parsed-json-or-None) for a GET, or None when nothing answers.
    An HTTP error keeps its JSON body: /health answers 503 with the reason."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

    def body_of(resp):
        try:
            return json.loads(resp.read().decode() or "null")
        except (ValueError, OSError):
            return None
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as r:
            return r.status, body_of(r)
    except urllib.error.HTTPError as e:
        return e.code, body_of(e)
    except (urllib.error.URLError, OSError):
        return None


@_safe
def session_finalize_endpoint(workspace):
    base = os.environ.get("AGENT_SERVER_URL") or \
        f"http://localhost:{os.environ.get('AGENT_SERVER_PORT', '18791')}"
    token = os.environ.get("AGENT_SERVER_TOKEN", "")
    health = _probe(base + "/health", token)
    if health is None:
        return True, "no live server; skipped"
    status, body = health
    body = body if isinstance(body, dict) else {}
    if status == 401:
        return False, "/health refused the token; check AGENT_SERVER_TOKEN"
    if status != 200 or body.get("status") != "healthy":
        state = body.get("status", f"ok={body.get('ok')!r}")
        return False, f"/health answered {status} and reports {state!r}, not 'healthy'"
    agents_resp = _probe(base + "/agents", token)
    if agents_resp is None or agents_resp[0] != 200 or not isinstance(agents_resp[1], dict):
        return False, f"/agents answered {agents_resp and agents_resp[0]}"
    listed = {a.get("name") for a in agents_resp[1].get("agents", [])}
    configured = set(registry.load_registry(workspace).ids())
    missing = sorted(configured - listed)
    if missing:
        return False, "/agents does not list: " + ", ".join(missing)
    return True, f"/health healthy; /agents lists {len(configured)} configured agents"


# ---- on-disk state ---------------------------------------------------------------

@_safe
def health_files_fresh(workspace):
    health = Path(workspace) / "data" / "health"
    if not health.is_dir():
        return True, "no health dir"
    jobs = {j.name: j for j in job_registry.load_jobs(workspace)}
    now = time.time()
    stale, fresh, unmatched = [], 0, []
    for f in sorted(health.glob("*.json")):
        job = jobs.get(f.stem)
        if job is None:
            unmatched.append(f.name)
            continue
        age = now - f.stat().st_mtime
        if age > job.max_age_s:
            stale.append(f"{f.name} is {int(age)}s old (limit {job.max_age_s}s)")
        else:
            fresh += 1
    detail = f"{fresh} fresh"
    if unmatched:
        detail += f"; not in the job table, ignored: {', '.join(unmatched)}"
    if stale:
        return False, "stale: " + "; ".join(stale)
    return True, detail


@_safe
def schema_stamp(workspace):
    data = Path(workspace) / "data"
    stamp = migrate_guard.read_stamp(data)
    if stamp is None:
        # data/agent-checks/ is this runner's own results log, not install state.
        state = [p for p in data.iterdir() if p.name != "agent-checks"] if data.is_dir() else []
        if not state:
            return True, "fresh install; no stamp yet (setup writes it)"
        return False, f"{data / '.schema-version'} missing on a non-empty data dir"
    if stamp["schema"] != SCHEMA_VERSION:
        return False, f"stamp says schema {stamp['schema']}, code is {SCHEMA_VERSION}"
    return True, f"schema {SCHEMA_VERSION}"
