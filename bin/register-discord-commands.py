#!/usr/bin/env python3
"""Register Karakos's Discord application ("slash") commands.

Guild-scoped registration (not global): guild commands appear immediately,
global ones take up to an hour to propagate.

Run automatically by `bin/entrypoint.sh` on every container start, so a
fresh install has working `/` commands with no extra script to run and no
documented follow-up step. The PUT below replaces the whole command set, so
re-running it when nothing changed is a no-op.

    python3 bin/register-discord-commands.py            # register/update
    python3 bin/register-discord-commands.py --list     # show what is registered
    python3 bin/register-discord-commands.py --clear    # remove all of them

Reads DISCORD_BOT_TOKEN_PRIMARY, DISCORD_BOT_ID_PRIMARY, and
DISCORD_SERVER_ID from the environment -- already present inside the
container via config/.env (see config/.env.template).

Every command here is dispatched by `DiscordAdapter.on_interaction` in
bin/relay.py, which routes it into the same `handle_sys_command` the text
intercept uses. The relay keeps a matching `SLASH_COMMANDS` set: a name
registered here with no entry there is a command that silently does nothing,
and tests/test_relay_slash_commands.py fails the build if the two drift.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Overridable for tests -- points at a local fake server instead of Discord.
API = os.environ.get("KARAKOS_DISCORD_API_BASE", "https://discord.com/api/v10")

STRING = 3
INTEGER = 4

AGENT_OPTION = {
    "name": "agent",
    "description": "Target agent (default: the channel's owning agent)",
    "type": STRING,
    "required": False,
}


def _cmd(name, description, options=None):
    c = {"name": name, "description": description, "type": 1}
    if options:
        c["options"] = options
    return c


# Option values are reassembled into the free-text `args` string that
# bin/relay.py's `handle_sys_command` branches already parse (see
# `slash_args` there), so the slash surface and the text surface stay one
# implementation rather than two that drift.
COMMANDS = [
    # --- whole-install, no target agent ---
    _cmd("status", "Agent + subprocess state and queue depth"),
    _cmd("health", "Health-monitor verdict for every component"),
    _cmd("usage", "Account usage vs rate-limit headroom"),
    _cmd("help", "List available commands"),

    # --- agent-targeted ---
    _cmd("cost", "Today's and this month's spend for an agent", [AGENT_OPTION]),
    _cmd("clear", "Clear an agent's session and restart it (destructive)", [AGENT_OPTION]),
    _cmd("reload", "Bounce an agent's subprocess, preserve session", [AGENT_OPTION]),
    _cmd("interrupt", "Stop an agent's current generation, keep the session", [AGENT_OPTION]),
    _cmd("kill", "Kill an agent's subprocess without respawning it", [AGENT_OPTION]),
    _cmd("flush", "Drop an agent's pending message queue", [AGENT_OPTION]),

    # --- shaped arguments ---
    _cmd("logs", "Tail a service log", [
        {"name": "service", "description": "Log name, e.g. relay or agent-server",
         "type": STRING, "required": True},
        {"name": "lines", "description": "How many lines (default 40, max 200)",
         "type": INTEGER, "required": False},
    ]),
]


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} not set in the environment")
    return value


def _request(method: str, url: str, token: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="show registered commands")
    ap.add_argument("--clear", action="store_true", help="deregister everything")
    args = ap.parse_args()

    token = _env("DISCORD_BOT_TOKEN_PRIMARY")
    app_id = _env("DISCORD_BOT_ID_PRIMARY")
    guild_id = _env("DISCORD_SERVER_ID")
    url = f"{API}/applications/{app_id}/guilds/{guild_id}/commands"

    if args.list:
        status, body = _request("GET", url, token)
        if status != 200:
            print(f"HTTP {status}: {body}", file=sys.stderr)
            return 1
        for c in body:
            subs = ", ".join(o["name"] for o in c.get("options", []) if o.get("type") == 1)
            print(f"/{c['name']}  [{subs}]" if subs else f"/{c['name']}")
        return 0

    payload = [] if args.clear else COMMANDS
    status, body = _request("PUT", url, token, payload)
    if status != 200:
        # 403 here means the bot was invited without the applications.commands
        # scope -- that needs a human with Manage Server to re-authorise it
        # through the OAuth2 URL generator. No amount of token or bot-
        # permission fixing helps (see docs/DISCORD_SETUP.md).
        print(f"HTTP {status}: {body}", file=sys.stderr)
        if status == 403:
            print(
                "This usually means the bot was invited without the "
                "'applications.commands' scope. Re-invite it via "
                "OAuth2 -> URL Generator with that scope checked, then "
                "re-run this script.",
                file=sys.stderr,
            )
        return 1

    print(f"{'Cleared' if args.clear else 'Registered'} {len(body)} command(s) in guild {guild_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
