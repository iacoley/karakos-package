#!/usr/bin/env python3
"""Install self-test (MIRDAIN G5, part 2). Read-only.

Runs bin/self-diagnose.py, then starts each MCP server over stdio and calls one
read-only tool on it: tools-server (initialize, tools/list, taskboard list,
memory status) and admin-server (initialize, tools/list, list_agents).
Prints PASS/FAIL per step; exit 0 when all pass, 1 otherwise. `--json` for tools.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../lib")))
from paths import workspace_root  # noqa: E402
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 20


def _load_diagnose():
    spec = importlib.util.spec_from_file_location("self_diagnose", ROOT / "bin" / "self-diagnose.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Server:
    """Minimal JSON-RPC-over-stdio client for one MCP server script."""

    def __init__(self, script, workspace):
        env = {**os.environ, "WORKSPACE_ROOT": str(workspace)}
        self.p = subprocess.Popen([sys.executable, str(ROOT / "mcp" / script)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=env, cwd=str(ROOT))
        self.n = 0

    def call(self, method, params=None):
        self.n += 1
        msg = {"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}
        box = []
        def io():
            self.p.stdin.write(json.dumps(msg) + "\n")
            self.p.stdin.flush()
            box.append(self.p.stdout.readline())
        t = threading.Thread(target=io, daemon=True)
        t.start()
        t.join(TIMEOUT_S)
        if not box:
            raise TimeoutError(f"{method}: no reply in {TIMEOUT_S}s")
        if not box[0]:
            raise RuntimeError(f"{method}: server exited")
        resp = json.loads(box[0])
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error'].get('message')}")
        return resp.get("result", {})

    def tool(self, name, args=None):
        res = self.call("tools/call", {"name": name, "arguments": args or {}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if isinstance(c, dict))
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"{name}: {data['error']}")
        return data

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self.p.kill()


def _step(results, name, fn):
    try:
        detail = fn()
        results.append({"step": name, "ok": True, "detail": detail or ""})
    except Exception as e:  # noqa: BLE001
        results.append({"step": name, "ok": False, "detail": str(e)})


def _server_steps(results, label, script, workspace, calls):
    try:
        s = Server(script, workspace)
    except OSError as e:
        results.append({"step": f"{label}: start", "ok": False, "detail": str(e)})
        return
    try:
        _step(results, f"{label}: initialize", lambda: s.call("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "self-test", "version": "1"}}) and "")
        def listing():
            n = len(s.call("tools/list").get("tools", []))
            if not n:
                raise RuntimeError("tools/list returned no tools")
            return f"{n} tools"
        _step(results, f"{label}: tools/list", listing)
        for tool, args in calls:
            _step(results, f"{label}: {tool}", lambda t=tool, a=args: (s.tool(t, a), "")[1])
    finally:
        s.close()


def run(workspace):
    results = []
    diag = _load_diagnose()
    bad = [f for f in diag.diagnose(str(workspace)) if f["severity"] != "info"]
    results.append({"step": "self-diagnose", "ok": not bad,
                    "detail": "; ".join(f"[{f['severity']}] {f['why']}" for f in bad)})
    _server_steps(results, "tools-server", "tools-server.py", workspace,
                  [("taskboard", {"action": "list"}), ("memory", {"action": "status"})])
    _server_steps(results, "admin-server", "admin-server.py", workspace,
                  [("list_agents", {})])
    return results


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--workspace", default=workspace_root(ROOT))
    a = ap.parse_args(argv)
    results = run(Path(a.workspace))
    ok = all(r["ok"] for r in results)
    if a.json:
        print(json.dumps({"ok": ok, "results": results}))
    else:
        for r in results:
            print(f"{'PASS' if r['ok'] else 'FAIL'} {r['step']}" + (f": {r['detail']}" if r["detail"] else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
