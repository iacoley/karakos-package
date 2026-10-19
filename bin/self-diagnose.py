#!/usr/bin/env python3
"""Whole-install self-diagnosis (MIRDAIN G5). Read-only.

Usage: self-diagnose.py [--json] [--sections a,b] [--workspace DIR]
Exit 0 when nothing is worse than info, 1 when any warn or critical finding.
Sections: health, findings, heartbeats, hook, disk, schema, logs.
Not a replacement for `make preflight` (host-side); this runs inside the install.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from urllib import error as urlerror, request as urlrequest

RANK = {"critical": 0, "warn": 1, "info": 2}
SECTIONS = ("health", "findings", "heartbeats", "hook", "disk", "schema", "logs")
OUTBOX_STUCK_S = 600
DISK_WARN, DISK_CRIT = 0.90, 0.97
LOG_TAIL_BYTES = 64 * 1024
LOG_ERROR_WARN = 5


def _f(section, severity, why):
    return {"section": section, "severity": severity, "why": why}


def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"_corrupt": True}
    return data if isinstance(data, dict) else {"_corrupt": True}


def check_health(ws, fetch=None):
    url = os.environ.get("AGENT_SERVER_URL") or \
        f"http://127.0.0.1:{os.environ.get('AGENT_SERVER_PORT', '18791')}"
    try:
        if fetch:
            h = fetch()
        else:
            req = urlrequest.Request(url.rstrip("/") + "/health")
            tok = os.environ.get("AGENT_SERVER_TOKEN", "")
            if tok:
                req.add_header("Authorization", f"Bearer {tok}")
            with urlrequest.urlopen(req, timeout=5) as r:
                h = json.loads(r.read().decode())
    except (urlerror.URLError, OSError, ValueError) as e:
        return [_f("health", "critical", f"agent server unreachable: {e}")]
    out = []
    if h.get("status") != "healthy":
        out.append(_f("health", "critical", f"agent server status is {h.get('status')!r}"))
    for name, a in (h.get("agents") or {}).items():
        if not a.get("alive", True):
            out.append(_f("health", "critical", f"agent {name} is not alive"))
    if h.get("dead_letters"):
        out.append(_f("health", "warn", f"{h['dead_letters']} dead letter(s)"))
    ob = h.get("outbox") or {}
    if ob.get("dead"):
        out.append(_f("health", "warn", f"{ob['dead']} dead message(s) in the outbox"))
    age = ob.get("oldest_pending_age_s")
    if age is not None and age > OUTBOX_STUCK_S:
        out.append(_f("health", "warn", f"oldest pending outbox message is {int(age)}s old"))
    return out


def check_findings(ws):
    d = _read_json(Path(ws) / "data" / "health" / "findings.json")
    if d is None:
        return [_f("findings", "info", "no monitor findings file yet")]
    if d.get("_corrupt"):
        return [_f("findings", "warn", "data/health/findings.json is unreadable")]
    return [_f("findings", x.get("severity") if x.get("severity") in RANK else "warn",
               f"{x.get('kind')}:{x.get('subject')}: {x.get('why')}")
            for x in d.get("findings", [])]


def check_heartbeats(ws):
    hb = Path(ws) / "data" / "health" / "heartbeats"
    if not hb.is_dir():
        return [_f("heartbeats", "info", "no job heartbeats recorded yet")]
    out = []
    for p in sorted(hb.glob("*.json")):
        d = _read_json(p)
        if d is None or d.get("_corrupt"):
            out.append(_f("heartbeats", "warn", f"{p.stem}: heartbeat file is unreadable"))
        elif d.get("ok") is False:
            out.append(_f("heartbeats", "warn", f"{p.stem}: last run failed ({d.get('detail', '')})"))
    return out


def check_hook(ws):
    h = Path(ws) / ".git" / "hooks" / "pre-commit"
    if not h.exists():
        return [_f("hook", "warn", "protected-path pre-commit hook is not installed")]
    if not os.access(h, os.X_OK):
        return [_f("hook", "warn", "pre-commit hook exists but is not executable")]
    return []


def check_disk(ws):
    u = shutil.disk_usage(ws)
    frac = u.used / u.total if u.total else 0
    if frac >= DISK_CRIT:
        return [_f("disk", "critical", f"disk {frac:.0%} full")]
    if frac >= DISK_WARN:
        return [_f("disk", "warn", f"disk {frac:.0%} full")]
    return []


def check_schema(ws):
    d = _read_json(Path(ws) / "data" / ".schema-version")
    if d is None:
        return [_f("schema", "warn", "data/.schema-version is missing (run: karakos migrate)")]
    if d.get("_corrupt") or not isinstance(d.get("schema"), int):
        return [_f("schema", "warn", "data/.schema-version is unreadable")]
    return []


def check_logs(ws):
    out = []
    for p in sorted((Path(ws) / "logs").glob("*.log")):
        if p.name == "hook-events.log":
            continue
        try:
            with open(p, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - LOG_TAIL_BYTES))
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        n = sum(1 for ln in text.splitlines() if "Traceback" in ln or " ERROR" in ln)
        if n >= LOG_ERROR_WARN:
            out.append(_f("logs", "warn", f"{p.name}: {n} error lines in the last {LOG_TAIL_BYTES // 1024} KB"))
    return out


CHECKS = {"health": check_health, "findings": check_findings, "heartbeats": check_heartbeats,
          "hook": check_hook, "disk": check_disk, "schema": check_schema, "logs": check_logs}


def diagnose(ws, sections=None):
    out = []
    for s in sections or SECTIONS:
        try:
            out += CHECKS[s](ws)
        except Exception as e:  # noqa: BLE001  (a broken check is itself a finding)
            out.append(_f(s, "warn", f"check failed: {e}"))
    out.sort(key=lambda x: (RANK[x["severity"]], x["section"]))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--sections", default="")
    ap.add_argument("--workspace", default=os.environ.get(
        "WORKSPACE_ROOT", str(Path(__file__).resolve().parent.parent)))
    a = ap.parse_args(argv)
    secs = [s for s in a.sections.split(",") if s] or None
    bad = [s for s in secs or () if s not in SECTIONS]
    if bad:
        ap.error(f"unknown section(s): {', '.join(bad)} (choose from {', '.join(SECTIONS)})")
    findings = diagnose(a.workspace, secs)
    worst = [f for f in findings if f["severity"] != "info"]
    if a.json:
        print(json.dumps({"ok": not worst, "findings": findings}))
    elif not findings:
        print("OK: nothing to report")
    else:
        for f in findings:
            print(f"[{f['severity']}] {f['section']}: {f['why']}")
    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main())
