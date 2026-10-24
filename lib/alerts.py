"""Alert routing. `plan` is pure; `send` posts straight to Discord through
bin/discord-notify.sh. Never the agent poke path: that queues for an agent, and a wedged
agent never reads its queue."""
from __future__ import annotations

import os
import sys as _sys
_sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".")))
from paths import workspace_root  # noqa: E402
import subprocess
from datetime import datetime
from pathlib import Path

MAX_POST = 1900
CHANNEL_NOTE = "N more findings, see data/health/summary.md"


def _cut(text: str) -> str:
    return text if len(text) <= MAX_POST else text[:MAX_POST - 1] + "…"


def _mention(config) -> str:
    raw = os.environ.get(config["alerts"].get("mention_env", ""), "")
    return f"<@{raw}> " if raw.isdigit() and int(raw) != 0 else ""


def plan(findings, state, config, now: datetime):
    """-> (posts, new_state). A post is {text, channel, keys}. New state assumes
    every post is delivered; use `commit` to roll back the failed ones."""
    cfg = config["alerts"]
    t = now.timestamp()
    old = (state or {}).get("keys", {})
    keys = {k: dict(v) for k, v in old.items()}
    recent = [x for x in (state or {}).get("recent", []) if t - x < 600]
    cands = []  # (text, channel, [keys])
    current = {f.key: f for f in findings if f.severity in ("warn", "critical")}
    for key, f in current.items():
        ent = keys.get(key)
        line = f"[{f.severity}] {f.key}: {f.why}"
        if f.severity == "warn":
            if ent is None or ent["severity"] != "warn":
                if ent is not None and ent["severity"] == "critical":
                    continue  # downgrade: already alerted
                cands.append((f"⚠️ {line}", cfg["channel"], [key]))
                keys[key] = {"severity": "warn", "last": t, "repeats": 0}
        else:
            if ent is None or ent["severity"] != "critical":
                cands.append((f"🚨 {_mention(config)}{line}", cfg["critical_channel"], [key]))
                keys[key] = {"severity": "critical", "last": t, "repeats": 0}
            elif ent["repeats"] < cfg["max_repeats"] and t - ent["last"] >= cfg["repeat_critical_s"]:
                cands.append((f"🚨 {_mention(config)}{line} (still unresolved)",
                              cfg["critical_channel"], [key]))
                keys[key] = {"severity": "critical", "last": t, "repeats": ent["repeats"] + 1}
    for key in list(keys):
        if key not in current:
            cands.append((f"✅ resolved: {key}", cfg["channel"], [key]))
            del keys[key]
    budget = max(cfg["max_posts_per_10min"] - len(recent), 0)
    posts = []
    if len(cands) > budget:
        keep = cands[:max(budget - 1, 0)] if budget else []
        rest = cands[len(keep):]
        posts = [{"text": _cut(c[0]), "channel": c[1], "keys": c[2]} for c in keep]
        posts.append({"text": f"{len(rest)} more findings, see data/health/summary.md",
                      "channel": cfg["channel"], "keys": [k for c in rest for k in c[2]]})
    else:
        posts = [{"text": _cut(c[0]), "channel": c[1], "keys": c[2]} for c in cands]
    return posts, {"keys": keys, "recent": recent + [t] * len(posts)}


def commit(old_state, new_state, posts, results) -> dict:
    """Roll back state for the keys of every post whose send failed."""
    old = (old_state or {}).get("keys", {})
    keys = {k: dict(v) for k, v in new_state["keys"].items()}
    recent = list(new_state["recent"])
    for post, ok in zip(posts, results):
        if ok:
            continue
        if recent:
            recent.pop()
        for k in post["keys"]:
            if k in old:
                keys[k] = dict(old[k])
            else:
                keys.pop(k, None)
    return {"keys": keys, "recent": recent}


def _log_failure(workspace, msg: str) -> None:
    try:
        p = Path(workspace) / "logs" / "health-alerts.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(f"{datetime.now().isoformat()} [ERROR] {msg}\n")
    except OSError:
        pass


def send(post, config=None, workspace=None) -> bool:
    """Run bin/discord-notify.sh directly. True only if it actually sent."""
    ws = Path(workspace or workspace_root())
    channel = post.get("channel") or ((config or {}).get("alerts", {}).get("channel", "signals"))
    notify = ws / "bin" / "discord-notify.sh"
    if not notify.exists():
        _log_failure(ws, f"alert not sent: no {notify}")
        return False
    try:
        subprocess.run([str(notify), channel, _cut(post["text"])], check=True,
                       capture_output=True, timeout=30)
        return True
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"")
        stderr = stderr.decode(errors="replace").strip() if isinstance(stderr, bytes) else str(stderr)
        _log_failure(ws, f"alert send failed (exit {e.returncode}): {stderr or e}")
    except subprocess.TimeoutExpired:
        _log_failure(ws, "alert send failed: discord-notify.sh timed out after 30s")
    except OSError as e:
        _log_failure(ws, f"alert send failed: cannot run discord-notify.sh: {e}")
    return False
