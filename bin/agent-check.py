#!/usr/bin/env python3
"""ITHILIEN phase 1: run the capability manifest and report PASS / FAIL / SKIP.

Reads tests/capabilities/manifest.yaml, runs each programmatic entry's check from
lib/agent_checks.py against the workspace, and prints one line per capability.
Agentic entries (phase 2) are reported as SKIP. Read-only except for one line
appended per run to data/agent-checks/results.jsonl.

    python3 bin/agent-check.py                     all programmatic checks
    python3 bin/agent-check.py --only ID[,ID]      selected capabilities
    python3 bin/agent-check.py --layer programmatic
    python3 bin/agent-check.py --json              one JSON document on stdout
    python3 bin/agent-check.py --layer agentic     the agentic battery (needs ITHILIEN_TEST_CHANNEL)
    python3 bin/agent-check.py --layer agentic --cases ID[,ID] --max-cost-usd 3

Exit 0 when nothing FAILs, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "lib"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agent_checks  # noqa: E402
import agent_cases  # noqa: E402
import agent_checks_agentic as agentic  # noqa: E402
import paths  # noqa: E402
import yaml  # noqa: E402

MANIFEST = ROOT / "tests" / "capabilities" / "manifest.yaml"
LAYERS = ("programmatic", "agentic")
REQUIRED_KEYS = ("id", "layer", "desc", "since", "check")


def load_manifest(path: Path = MANIFEST) -> list:
    entries = yaml.safe_load(path.read_text())
    if not isinstance(entries, list):
        raise ValueError(f"{path}: top level must be a list of capabilities")
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError(f"{path}: entry is not a mapping: {e!r}")
        missing = [k for k in REQUIRED_KEYS if k not in e]
        if missing:
            raise ValueError(f"{path}: entry {e.get('id', '?')!r} lacks {', '.join(missing)}")
    return entries


def run_one(entry: dict, workspace: Path) -> dict:
    cid, layer = entry["id"], entry["layer"]
    t0 = time.monotonic()
    if layer == "agentic":
        status, detail = "skip", "run with --layer agentic"
    elif layer == "programmatic":
        fn = getattr(agent_checks, str(entry["check"]), None)
        if not callable(fn):
            status, detail = "FAIL", f"no check function {entry['check']!r} in lib/agent_checks.py"
        else:
            ok, detail = fn(workspace)
            status = "skip" if ok is None else ("PASS" if ok else "FAIL")
    else:
        status, detail = "FAIL", f"unknown layer {layer!r}"
    return {"id": cid, "layer": layer, "status": status, "detail": detail,
            "ms": int((time.monotonic() - t0) * 1000)}


def select(entries: list, only: list | None, layer: str | None) -> list:
    out = [e for e in entries if not layer or e.get("layer") == layer]
    if only:
        known = {e["id"] for e in entries}
        out = [e for e in out if e["id"] in only]
        out += [{"id": i, "_unknown": True} for i in only if i not in known]
    return out


def version() -> str:
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "unknown"


def append_log(doc: dict, workspace: Path) -> None:
    try:
        d = workspace / "data" / "agent-checks"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "results.jsonl").open("a") as f:
            f.write(json.dumps(doc) + "\n")
    except OSError as e:
        print(f"warning: could not append data/agent-checks/results.jsonl: {e}", file=sys.stderr)


def run_agentic(a) -> int:
    """The agentic layer: scratch agents only, 3 runs per case, pass at 2, cost-capped."""
    channel = os.environ.get("ITHILIEN_TEST_CHANNEL", "")
    if not channel:
        print(f"SKIP {agentic.SKIP_NO_CHANNEL}")
        return 0
    workspace = paths.workspace_path(ROOT)
    token = os.environ.get("AGENT_SERVER_TOKEN", "")
    base = os.environ.get("AGENT_SERVER_URL") or \
        f"http://localhost:{os.environ.get('AGENT_SERVER_PORT', '18791')}"
    client = agentic.AgentClient(base, token, workspace / "logs" / "agent-streams", channel)
    ctx = agent_cases.Ctx(client=client, channel=channel, workspace=workspace,
                          meter=agentic.CostMeter(a.max_cost_usd),
                          env_names=agent_cases.allowlist_env_names(workspace))
    ids = [x.strip() for x in a.cases.split(",") if x.strip()] or None
    cases = dict(agent_cases.SHARED)
    if a.extras:
        extras_file = ROOT / "tests" / "capabilities" / "extras" / f"{a.extras}.yaml"
        if not extras_file.is_file():
            print(f"agent-check: no extras file {extras_file}", file=sys.stderr)
            return 1
        cases.update(agent_cases.load_extras(extras_file))
    unknown = [i for i in ids or [] if i not in cases]
    if unknown:
        print(f"agent-check: no such agentic case: {', '.join(unknown)}", file=sys.stderr)
        return 1
    report = agent_cases.run_battery(ctx, ids, cases)
    doc = {"version": version(), "layer": "agentic",
           "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **report}
    append_log(doc, workspace)
    if a.json:
        print(json.dumps(doc))
    else:
        for r in report["results"]:
            flag = " flaky" if r.get("flaky") else ""
            print(f"{r['status']:<4} {r['id']:<30} {r['passes']}/{r['runs']}{flag}  {r['detail']}")
        if report["aborted"]:
            print(f"aborted: {report['aborted']} (spent {report['spent_usd']} USD)")
    return 1 if any(r["status"] == "FAIL" for r in report["results"]) or \
        (report["aborted"] and not report["results"]) else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="print one JSON document")
    ap.add_argument("--only", default="", help="comma-separated capability ids")
    ap.add_argument("--layer", choices=LAYERS, help="run only this layer")
    ap.add_argument("--extras", default="", help="agentic: also run tests/capabilities/extras/NAME.yaml")
    ap.add_argument("--cases", default="", help="agentic: comma-separated case ids")
    ap.add_argument("--max-cost-usd", type=float, default=agentic.DEFAULT_MAX_COST_USD,
                    help="agentic: stop starting new cases at this spend (default 3.00)")
    a = ap.parse_args(argv)

    try:
        entries = load_manifest()
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"agent-check: bad manifest: {e}", file=sys.stderr)
        return 1
    if a.layer == "agentic":
        return run_agentic(a)
    only = [x.strip() for x in a.only.split(",") if x.strip()] or None
    chosen = select(entries, only, a.layer)
    workspace = paths.workspace_path(ROOT)
    agent_checks.reset_run_cache()

    results = []
    for entry in chosen:
        if entry.get("_unknown"):
            results.append({"id": entry["id"], "layer": "?", "status": "FAIL",
                            "detail": "no such capability in the manifest", "ms": 0})
        else:
            results.append(run_one(entry, workspace))

    doc = {"version": version(),
           "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "results": results}
    append_log(doc, workspace)

    if a.json:
        print(json.dumps(doc))
    else:
        for r in results:
            print(f"{r['status'].upper():<4} {r['id']:<26} {r['detail']} ({r['ms']} ms)")
    failed = [r for r in results if r["status"] == "FAIL"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
