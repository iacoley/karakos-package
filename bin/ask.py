#!/usr/bin/env python3
"""Non-blocking asks: post a question with options and a recommendation, end the turn,
wake the agent when an owner answers.

`ask_user` blocks the turn until a click. This is the other kind: the ask is a JSON
file under data/asks/, an embed with buttons (and 1-9 reactions as a fallback) in
Discord, and a wake message when the first owner answers.

    ask.py post --spec FILE      post an ask, print its id
    ask.py poll                  reactions and expiry (run every minute by the scheduler)
    ask.py answer ID INDEX USER  record a button press (called by the relay)
    ask.py list                  one line per ask
    ask.py cancel ID [--note T]
    ask.py resolve ID --note T   close an ask that was settled elsewhere

Stdlib only. State: data/asks/<id>.json. Log: logs/ask.jsonl.
"""
import argparse
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import paths  # noqa: E402
from discord_embeds import clip_embed  # noqa: E402

ROOT = paths.workspace_path()
ASK_DIR = ROOT / "data" / "asks"
LOG = ROOT / "logs" / "ask.jsonl"
API = "https://discord.com/api/v10"

CUSTOM_ID_PREFIX = "kask2"   # the blocking ask_user buttons use "kask"
MIN_OPTIONS, MAX_OPTIONS = 2, 9
DEFAULT_EXPIRES_S = 14 * 86400
POLL_MAX_PER_RUN = 25
BLUE, GREEN, GREY = 0x5865F2, 0x57F287, 0x99AAB5
NUMBER_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
                "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


class AskError(Exception):
    pass


# ---- identity and config ---------------------------------------------------------

def owner_ids():
    ids = {os.environ.get("OWNER_DISCORD_ID", "")}
    ids.update(x.strip() for x in os.environ.get("ASK_EXTRA_OWNER_IDS", "").split(","))
    return {i for i in ids if i.isdigit() and int(i) > 0}


def bot_token():
    return os.environ.get("DISCORD_BOT_TOKEN_PRIMARY") or os.environ.get("DISCORD_BOT_TOKEN") or ""


def channel_id(name_or_id):
    if str(name_or_id).isdigit():
        return str(name_or_id)
    try:
        cfg = json.loads((ROOT / "config" / "channels.json").read_text())
    except (OSError, ValueError):
        cfg = {}
    entry = (cfg.get("channels") or {}).get(str(name_or_id).lstrip("#"))
    if entry and str(entry.get("id", "")).isdigit():
        return str(entry["id"])
    raise AskError(f"unknown channel {name_or_id!r}")


def log(event, **kw):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "event": event, **kw}) + "\n")
    except OSError:
        pass


# ---- state: one file per ask, locked, atomically replaced -------------------------

def _path(ask_id):
    if not ask_id or not all(c.isalnum() or c == "-" for c in ask_id):
        raise AskError("bad ask id")
    return ASK_DIR / f"{ask_id}.json"


@contextmanager
def locked(ask_id):
    ASK_DIR.mkdir(parents=True, exist_ok=True)
    with open(ASK_DIR / f"{ask_id}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        yield


def load(ask_id):
    try:
        return json.loads(_path(ask_id).read_text())
    except (OSError, ValueError):
        raise AskError(f"no such ask {ask_id}")


def save(ask):
    p = _path(ask["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(ask, indent=1))
    os.replace(tmp, p)


def all_asks():
    out = []
    for p in sorted(ASK_DIR.glob("*.json")) if ASK_DIR.exists() else []:
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    return out


# ---- spec ------------------------------------------------------------------------

def validate(spec):
    if not isinstance(spec, dict):
        raise AskError("spec must be an object")
    for k in ("channel", "title", "problem"):
        if not isinstance(spec.get(k), str) or not spec[k].strip():
            raise AskError(f"missing {k}")
    opts = spec.get("options")
    if not isinstance(opts, list) or not (MIN_OPTIONS <= len(opts) <= MAX_OPTIONS):
        raise AskError(f"options must be a list of {MIN_OPTIONS}-{MAX_OPTIONS}")
    for o in opts:
        if not isinstance(o, dict) or not isinstance(o.get("label"), str) or not o["label"].strip():
            raise AskError("every option needs a label")
    rec = spec.get("recommendation")
    if rec is None:
        if not str(spec.get("no_recommendation") or "").strip():
            raise AskError("recommendation is null: give no_recommendation as the reason")
    elif isinstance(rec, bool) or not isinstance(rec, int) or not (0 <= rec < len(opts)):
        raise AskError("recommendation must be an index into options")
    route = spec.get("on_answer")
    if not isinstance(route, dict):
        raise AskError("missing on_answer")
    if route.get("type") == "buzz" and route.get("shard"):
        pass
    elif route.get("type") == "poke" and route.get("agent"):
        pass
    else:
        raise AskError('on_answer must be {"type":"buzz","shard":S} or {"type":"poke","agent":A}')
    exp = spec.get("expires_s", DEFAULT_EXPIRES_S)
    if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= 0:
        raise AskError("expires_s must be a positive number")


def build_embed(ask):
    state = ask["status"]
    color = {"open": BLUE, "answered": GREEN}.get(state, GREY)
    fields = []
    for i, o in enumerate(ask["options"]):
        name = f"{i + 1}. {o['label']}" + (" (recommended)" if ask.get("recommendation") == i else "")
        fields.append({"name": name, "value": o.get("rationale") or "​"})
    if ask.get("recommendation") is None and ask.get("no_recommendation"):
        fields.append({"name": "No recommendation", "value": ask["no_recommendation"]})
    if state == "answered":
        fields.append({"name": "Answer", "value": ask["answer"]["label"]})
    elif state != "open":
        fields.append({"name": state.capitalize(), "value": ask.get("note") or state})
    return clip_embed({"title": ask["title"], "description": ask["problem"],
                       "color": color, "fields": fields})


def components(ask):
    if ask["status"] != "open":
        return []
    btns = [{"type": 2, "style": 1, "label": o["label"][:80],
             "custom_id": f"{CUSTOM_ID_PREFIX}:{ask['id']}:{i}"} for i, o in enumerate(ask["options"])]
    return [{"type": 1, "components": btns[i:i + 5]} for i in range(0, len(btns), 5)]


# ---- Discord REST ----------------------------------------------------------------

def discord(method, path, body=None):
    """One REST call. Returns (status, json). Honours one 429 Retry-After."""
    token = bot_token()
    if not token:
        raise AskError("no Discord bot token in the environment")
    for attempt in (0, 1):
        req = urllib.request.Request(API + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Authorization", f"Bot {token}")
        req.add_header("User-Agent", "karakos-ask (local, 1)")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode() or "{}"
                return r.status, json.loads(raw)
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read().decode() or "{}")
            except (ValueError, OSError):
                data = {}
            if e.code == 429 and attempt == 0:
                time.sleep(min(float(data.get("retry_after", 1)), 10))
                continue
            return e.code, data
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": str(e)}
    return 0, {}


def render(ask):
    """Edit the posted message to match the ask's state."""
    if not ask.get("message_id"):
        return
    discord("PATCH", f"/channels/{ask['channel_id']}/messages/{ask['message_id']}",
            {"embeds": [build_embed(ask)], "components": components(ask)})


# ---- wake ------------------------------------------------------------------------

def wake(ask):
    """Run on_answer. Returns True when the wake was delivered."""
    route, msg = ask["on_answer"], f"ask {ask['id']} answered: {ask['answer']['label']}"
    if route["type"] == "buzz":
        sender = ask.get("asked_by") or ""
        url = os.environ.get("AGENT_SERVER_URL") or \
            f"http://localhost:{os.environ.get('AGENT_SERVER_PORT', '18791')}"
        req = urllib.request.Request(url + "/hive/buzz", method="POST", data=json.dumps(
            {"from": sender, "to": route["shard"], "message": msg}).encode())
        req.add_header("Authorization", f"Bearer {os.environ.get('AGENT_SERVER_TOKEN', '')}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status == 202:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        agent = route["shard"]          # buzz refused (self-call, unknown caller): poke instead
    else:
        agent = route["agent"]
    r = subprocess.run([str(ROOT / "bin" / "poke.sh"), "--agent", agent, "--source", "ask",
                        "--reply-channel", str(ask.get("channel_name") or "general"), msg],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


# ---- commands --------------------------------------------------------------------

def cmd_post(spec_file):
    spec = json.loads(Path(spec_file).read_text())
    validate(spec)
    cid = channel_id(spec["channel"])
    ask_id = "ask-" + uuid.uuid4().hex[:10]
    now = time.time()
    ask = {"id": ask_id, "status": "open", "created": now,
           "expires": now + float(spec.get("expires_s", DEFAULT_EXPIRES_S)),
           "channel_id": cid, "channel_name": str(spec["channel"]).lstrip("#") if not str(spec["channel"]).isdigit() else "",
           "title": spec["title"], "problem": spec["problem"],
           "options": [{"label": o["label"], "rationale": o.get("rationale", "")} for o in spec["options"]],
           "recommendation": spec.get("recommendation"),
           "no_recommendation": spec.get("no_recommendation", ""),
           "mention_owner": bool(spec.get("mention_owner")),
           "on_answer": spec["on_answer"],
           "asked_by": os.environ.get("KARAKOS_SHARD") or os.environ.get("KARAKOS_AGENT", ""),
           "message_id": None, "answer": None, "wake": "pending"}
    body = {"embeds": [build_embed(ask)], "components": components(ask)}
    owner = sorted(owner_ids())
    if ask["mention_owner"] and owner:
        body["content"] = " ".join(f"<@{o}>" for o in owner)
        body["allowed_mentions"] = {"users": owner}
    status, data = discord("POST", f"/channels/{cid}/messages", body)
    if status != 200 or "id" not in data:
        raise AskError(f"Discord refused the post ({status})")
    ask["message_id"] = data["id"]
    for e in NUMBER_EMOJI[:len(ask["options"])]:
        discord("PUT", f"/channels/{cid}/messages/{data['id']}/reactions/{urllib.parse.quote(e)}/@me")
        time.sleep(0.3)
    with locked(ask_id):
        save(ask)
    log("post", id=ask_id)
    return ask_id


def cmd_answer(ask_id, index, user_id):
    """Record an answer. Returns outcome: answered, already, closed, ignored."""
    if str(user_id) not in owner_ids():
        return "ignored"
    with locked(ask_id):
        ask = load(ask_id)
        if ask["status"] == "answered":
            return "already"
        if ask["status"] != "open" or time.time() > ask["expires"]:
            return "closed"
        if not (0 <= index < len(ask["options"])):
            return "ignored"
        ask["status"], ask["answer"] = "answered", {
            "index": index, "label": ask["options"][index]["label"], "by": str(user_id)}
        save(ask)                      # state first: a failed wake must not allow a second answer
    log("answered", id=ask_id, index=index)
    finish_answer(ask_id)
    return "answered"


def finish_answer(ask_id):
    with locked(ask_id):
        ask = load(ask_id)
        if ask["status"] != "answered" or ask.get("wake") == "sent":
            return
        ok = False
        try:
            ok = wake(ask)
        except (OSError, subprocess.SubprocessError):
            ok = False
        ask["wake"] = "sent" if ok else "wake_failed"
        save(ask)
    render(ask)
    log("wake", id=ask_id, ok=ok)


def close(ask_id, status, note):
    with locked(ask_id):
        ask = load(ask_id)
        if ask["status"] != "open":
            raise AskError(f"ask is already {ask['status']}")
        ask["status"], ask["note"] = status, note
        save(ask)
    render(ask)
    log(status, id=ask_id)


def cmd_poll():
    now, checked = time.time(), 0
    for ask in all_asks():
        if ask["status"] == "answered" and ask.get("wake") == "wake_failed":
            finish_answer(ask["id"])            # retry a wake that failed earlier
            continue
        if ask["status"] != "open":
            continue
        if now > ask["expires"]:
            close(ask["id"], "expired", "No answer before the ask expired.")
            continue
        if checked >= POLL_MAX_PER_RUN or not ask.get("message_id"):
            continue
        checked += 1
        for i, e in enumerate(NUMBER_EMOJI[:len(ask["options"])]):
            status, users = discord("GET", f"/channels/{ask['channel_id']}/messages/"
                                    f"{ask['message_id']}/reactions/{urllib.parse.quote(e)}?limit=25")
            if status != 200 or not isinstance(users, list):
                continue
            hit = next((u["id"] for u in users if str(u.get("id")) in owner_ids()), None)
            if hit and cmd_answer(ask["id"], i, hit) == "answered":
                break


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("post").add_argument("--spec", required=True)
    sub.add_parser("poll")
    p = sub.add_parser("answer")
    p.add_argument("id"), p.add_argument("index", type=int), p.add_argument("user")
    sub.add_parser("list")
    for name in ("cancel", "resolve"):
        p = sub.add_parser(name)
        p.add_argument("id"), p.add_argument("--note", default="")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "post":
            print(cmd_post(a.spec))
        elif a.cmd == "poll":
            cmd_poll()
        elif a.cmd == "answer":
            print(cmd_answer(a.id, a.index, a.user))
        elif a.cmd == "list":
            for k in all_asks():
                print(f"{k['id']}  {k['status']:9}  {k['title']}")
        elif a.cmd == "cancel":
            close(a.id, "cancelled", a.note or "Cancelled.")
        elif a.cmd == "resolve":
            if not a.note:
                raise AskError("resolve needs --note")
            close(a.id, "resolved", a.note)
    except AskError as e:
        print(f"ask: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
