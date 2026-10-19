"""Friction sensor (MIRDAIN G6, build A). No LLM, read-only on the logs.

Scans the raw model streams in logs/agent-streams/*.jsonl for tool errors,
non-zero Bash exits, denied tools, rate-limit pressure and repeated Bash
commands, and appends one line per event to data/friction-log.jsonl. Progress is
kept in data/friction-sensor-state.json (byte offset per stream), so a rerun
only reads new lines. Nothing here edits a prompt, persona or config.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.redact import redact_text

LOG_NAME = "friction-log.jsonl"
STATE_NAME = "friction-sensor-state.json"
SUMMARY_REL = Path("friction") / "weekly.md"
REPEAT_AT = 3            # same Bash command this many times in one stream
RATE_HIGH = 0.9          # five-hour or weekly utilisation worth noting
DETAIL_MAX = 200
PENDING_MAX = 200
DENIED_MARKERS = ("Blocked:", "permission denied", "was denied", "not allowed", "requires approval")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _agent_of(path: Path) -> str:
    return path.name.rsplit("_", 1)[0]


def _clip(text) -> str:
    one = " ".join(str(text or "").split())
    return redact_text(one)[:DETAIL_MAX]


def _result_text(block) -> str:
    c = block.get("content")
    if isinstance(c, list):
        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def _sig(cmd: str) -> str:
    return hashlib.sha1(" ".join(cmd.split()).encode()).hexdigest()[:12]


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _scan_file(path: Path, st: dict) -> list:
    """New friction events from `path`; updates `st` (offset, pending, counts, rl)."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size < st.get("offset", 0):
        st.clear()  # truncated or replaced
    with open(path, "rb") as fh:
        fh.seek(st.get("offset", 0))
        data = fh.read()
    end = data.rfind(b"\n") + 1  # leave a half-written last line for next time
    st["offset"] = st.get("offset", 0) + end
    pending = st.setdefault("pending", {})
    counts = st.setdefault("counts", {})
    agent, events = _agent_of(path), []

    def add(kind, tool, detail):
        events.append({"agent": agent, "stream": path.name, "kind": kind,
                       "tool": tool, "detail": _clip(detail)})

    for raw in data[:end].splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        t = d.get("type")
        msg = d.get("message") if isinstance(d.get("message"), dict) else {}
        blocks = msg.get("content") if isinstance(msg.get("content"), list) else []
        if t == "assistant":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    name, inp = b.get("name", "?"), b.get("input") or {}
                    cmd = inp.get("command") if name == "Bash" else None
                    pending[b.get("id", "")] = [name, cmd[:DETAIL_MAX] if cmd else ""]
                    if cmd:
                        s = _sig(cmd)
                        counts[s] = counts.get(s, 0) + 1
                        if counts[s] == REPEAT_AT:
                            add("repeat", "Bash", f"x{REPEAT_AT}: {cmd}")
        elif t == "user":
            for b in blocks:
                if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                    continue
                name, cmd = pending.pop(b.get("tool_use_id", ""), ["unknown", ""])
                if not b.get("is_error"):
                    continue
                text = _result_text(b)
                if any(m.lower() in text.lower() for m in DENIED_MARKERS):
                    add("denied", name, text)
                elif name == "Bash" and text.startswith("Exit code"):
                    add("bash_exit", name, f"{text.splitlines()[0]} | {cmd}")
                else:
                    add("tool_error", name, text)
        elif t == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
            util = max([w.get("utilization", 0) or 0
                        for w in (info.get("unifiedWindows") or {}).values()
                        if isinstance(w, dict)] or [0])
            state = ("blocked" if info.get("status") != "allowed"
                     else "high" if util >= RATE_HIGH else "ok")
            if state != "ok" and st.get("rl") != state:
                add("rate_limit", info.get("rateLimitType", "?"),
                    f"status={info.get('status')} utilization={util:.2f}")
            st["rl"] = state
    while len(pending) > PENDING_MAX:
        pending.pop(next(iter(pending)))
    return events


def scan(workspace) -> dict:
    """Scan every stream once; append events; return {'scanned': n, 'events': n}."""
    ws = Path(workspace)
    state_path = ws / "data" / STATE_NAME
    state = _load(state_path, {})
    streams = state.setdefault("streams", {})
    out, scanned = [], 0
    for p in sorted((ws / "logs" / "agent-streams").glob("*.jsonl")):
        scanned += 1
        out += _scan_file(p, streams.setdefault(p.name, {}))
    stamp = _now()
    if out:
        log = ws / "data" / LOG_NAME
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            for e in out:
                fh.write(json.dumps({"ts": stamp, **e}) + "\n")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_path)
    return {"scanned": scanned, "events": len(out)}


def read_log(workspace, days=7) -> list:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    try:
        for line in (Path(workspace) / "data" / LOG_NAME).read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", "") >= cutoff:
                rows.append(r)
    except OSError:
        pass
    return rows


def summary_text(rows, days=7) -> str:
    lines = [f"# Friction summary, last {days} days", f"{len(rows)} events."]
    if rows:
        lines += ["", "## By kind"] + [f"- {k}: {n}" for k, n in
                                       Counter(r["kind"] for r in rows).most_common()]
        lines += ["", "## By tool"] + [f"- {k}: {n}" for k, n in
                                       Counter(r["tool"] for r in rows).most_common(10)]
        top = Counter((r["kind"], r["tool"], r["detail"][:80]) for r in rows).most_common(5)
        lines += ["", "## Repeated"] + [f"- {n}x {k}/{t}: {d}" for (k, t, d), n in top if n > 1]
    return "\n".join(lines) + "\n"


def write_summary(workspace, days=7) -> Path:
    path = Path(workspace) / "data" / SUMMARY_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(summary_text(read_log(workspace, days), days))
    os.replace(tmp, path)
    return path
